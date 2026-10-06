"""Isolated Terra writers and independent Sol critics for existing dossier jobs.

No Claude calls, accounts, or purchases. The existing arithmetic, original-source,
binding, repair and atomic-publication gates remain the completion criteria.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time

from .dossier_swarm import Swarm, sha, write_json
from .dossier_synthesis import Synthesis


class CodexRuntime(Swarm):
    def __init__(self, root='.moneyball', executable='codex'):
        super().__init__(root, executable)
        self.pause_path = self.base / 'codex-pause.json'

    def invoke(self, directory, name, prompt, output_schema, timeout=900,
               model='gpt-5.6-terra', effort='default'):
        if model not in ('gpt-5.6-terra', 'gpt-5.6-sol'):
            raise ValueError('Codex dossier workers are restricted to user-selected Terra/Sol')
        if self.pause_path.exists():
            self.stop.set()
            raise RuntimeError('Codex dispatch paused: ' + str(self.pause_path))
        directory = Path(directory).resolve()
        packet_path = directory / 'packet.json'
        if packet_path.exists():
            from .dossier_reader import decision_brief
            write_json(directory / 'evidence/decision-brief.json',
                       decision_brief(json.loads(packet_path.read_text())))
            prompt = ('Read evidence/decision-brief.json first as the compact reading guide. '
                      'Its original evidence sections remain authoritative and available.\n' + prompt)
        recovered_author = directory / 'evidence/recovered-author.json'
        if name == 'author' and recovered_author.exists():
            saved = json.loads(recovered_author.read_text())
            if saved.get('new_attempt_directory') == directory.name:
                value = saved['structured_output']
                if sha(json.dumps(value, sort_keys=True).encode()) != saved['structured_output_sha256']:
                    raise ValueError('Recovered author hash mismatch')
                write_json(directory / 'author.stdout.json', {
                    'engine': 'recovered-successful-codex-author', 'requested_model': model,
                    'structured_output': value, 'recovery_provenance': saved['provenance'],
                    'interpretation': 'No new author invocation; a fresh independent critic is required.'})
                return value
        schema_path = directory / (name + '.schema.json')
        result_path = directory / (name + '.codex-result.json')
        write_json(schema_path, output_schema)
        prompt = prompt.replace('650–1000 word dossier', '450–750 word dossier')
        prompt = prompt.replace('browser control, shell,\nmessages', 'browser control,\nmessages')
        prompt += ('\nThis worker runs in Codex, not Claude. Use native web search if needed. '
                   'Use read-only local commands solely to inspect the staged public evidence '
                   'and calculate arithmetic. No other workspace exploration, writes, accounts, '
                   'messages or browser control. Read the compact brief and 3–6 relevant archived '
                   'original bodies first; search only unresolved load-bearing facts. If scout '
                   'evidence is unavailable, independently obtain this player\'s original sources. '
                   'Return only the requested JSON, with no markdown fences.\n')
        if (directory / 'evidence/source-repair-targets.json').exists():
            prompt += '\nRead evidence/source-repair-targets.json for the exact remaining source-declaration/receipt mismatches.\n'
        if (directory / 'evidence/semantic-repair-targets.json').exists():
            prompt += '\nRead evidence/semantic-repair-targets.json. Make the smallest supported corrections to the existing case, not a fresh rewrite.\n'
        if name == 'critic':
            prompt += ('\nverified_source_urls is a publication receipt field, not a browsing history: '
                       'include only author-declared URLs whose successful archived receipt and '
                       'author read_firsthand=true you actually verified. If a needed citation is '
                       'missing or unread, reject with that exact URL and reason. Extra URLs read '
                       'during your audit belong in checks_performed; they cannot supply unarchived '
                       'support for an author claim. Do not approve unsupported claims merely by '
                       'omitting their URL from verified_source_urls.\n')
        argv = [self.executable, '--search', '-a', 'never', 'exec',
                '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                '--ephemeral', '--sandbox', 'read-only', '--json', '-m', model,
                '--output-schema', str(schema_path), '-o', str(result_path), prompt]
        write_json(directory / (name + '.invocation.json'), {
            'argv': argv, 'started_at': time.time(), 'requested_model': model,
            'effort': 'model default; no override', 'engine': 'codex-cli'})
        stream = directory / (name + '.events.jsonl')
        stderr = directory / (name + '.stderr.txt')
        with stream.open('w') as out, stderr.open('w') as err:
            process = subprocess.Popen(argv, cwd=directory, stdout=out, stderr=err,
                                       text=True, start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
                raise TimeoutError('Codex dossier call timed out')
        event_text = stream.read_text(); error_text = stderr.read_text()
        errors = []; last_turn_event = None
        for line in event_text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') in ('error', 'turn.failed'):
                errors.append(event)
            if event.get('type') in ('turn.completed', 'turn.failed'):
                last_turn_event = event.get('type')
        combined = json.dumps(errors) + '\n' + error_text
        if any(term in combined.lower() for term in ('usage limit', 'usage_limit', 'insufficient_quota',
                                                       'session limit', 'rate_limit')):
            write_json(self.pause_path, {'observed_at': time.time(), 'reason': combined[-6000:],
                       'retry_automatically': False, 'invocation': str(directory / (name + '.invocation.json'))})
            self.stop.set()
        # The CLI emits informational error events while reconnecting and can
        # subsequently finish successfully. Preserve them without discarding a
        # completed structured output. A terminal failure or cap still fails.
        recovered_transport = (process.returncode == 0 and last_turn_event == 'turn.completed'
            and result_path.exists() and errors and all(e.get('type') == 'error' and
                str(e.get('message', '')).startswith('Reconnecting...') for e in errors))
        if recovered_transport:
            write_json(directory / (name + '.recovered-transport.json'), {
                'observed_at': time.time(), 'events': errors,
                'interpretation': 'CLI recovered its own transport and completed; original events retained.'})
        if process.returncode or (errors and not recovered_transport):
            raise RuntimeError('Codex exit ' + str(process.returncode) + ': ' + combined[-4000:])
        if not result_path.exists():
            raise ValueError('Codex returned no structured dossier output')
        value = json.loads(result_path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Codex structured output is not an object')
        write_json(directory / (name + '.stdout.json'), {
            'engine': 'codex-cli', 'requested_model': model,
            'structured_output': value, 'completed_at': time.time()})
        return value


class CodexSynthesis(Synthesis):
    def __init__(self, root='.moneyball', executable='codex', runtime=None):
        runtime = runtime or CodexRuntime(root, executable)
        super().__init__(root, runtime=runtime, author_model='gpt-5.6-terra',
                         critic_model='gpt-5.6-sol', author_effort='default', critic_effort='default')

    def claim(self, first=229, last=400):
        if not 217 <= first <= last <= 400:
            raise ValueError('Codex queue owns ranks217–400 only')
        if getattr(self.runtime, 'pause_path', self.base / 'codex-pause.json').exists():
            self.runtime.stop.set(); return None
        with self.runtime.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""SELECT * FROM jobs WHERE rank BETWEEN ? AND ? AND
                (state='pending' OR (state='error' AND error LIKE 'Claude%')) ORDER BY rank""",
                              (first, last)).fetchall()
            for row in rows:
                pid = row['player_id']
                if not re.fullmatch(r'[A-Za-z0-9_-]+', pid):
                    raise ValueError('Unsafe player identifier')
                if (self.root / 'research/player-reviews' / (pid + '.md')).exists():
                    continue
                attempt = row['attempts'] + 1
                db.execute("UPDATE jobs SET state='running',attempts=?,updated=?,owner=?,error=NULL WHERE player_id=?",
                           (attempt, time.time(), os.getpid(), pid))
                return {**dict(row), 'attempts': attempt, 'state': 'running'}
        return None

    def _snapshot_scout(self, directory, row):
        latest = self.scouts / 'jobs' / row['player_id'] / 'latest.json'
        if latest.exists():
            return super()._snapshot_scout(directory, row)
        write_json(directory / 'evidence/scout-report.json', {
            'interpretation': 'No completed scout report exists. Missing evidence is not zero or approval.',
            'player_id': row['player_id'], 'cohort_rank': row['rank'],
            'report': {'cards': [], 'gaps': ['Independently research current primary facts for this player.'],
                       'semantic_claims_verified': False}, 'readable_originals': []})


class CodexRepair(CodexSynthesis):
    """One additional, explicitly requested pass after correcting input navigation.

    This mode never silently retries an ordinary model error. Original failed
    attempts and judgments remain intact; a repair is a new counted comparison.
    """
    marker_name = 'codex-extra-repair.json'
    source_only = False
    semantic_only = False

    def claim(self, first=217, last=400):
        if not 217 <= first <= last <= 400:
            raise ValueError('Codex repair owns ranks217–400 only')
        if self.runtime.pause_path.exists():
            self.runtime.stop.set(); return None
        with self.runtime.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM jobs WHERE state='needs_revision' AND rank BETWEEN ? AND ? ORDER BY rank",
                              (first, last)).fetchall()
            for row in rows:
                pid = row['player_id']
                if self.source_only and not str(row['error']).startswith('Critic verified URLs'):
                    continue
                if self.semantic_only and str(row['error']).startswith('Critic verified URLs'):
                    continue
                marker = self.base / 'jobs' / pid / self.marker_name
                if marker.exists() or (self.root / 'research/player-reviews' / (pid + '.md')).exists():
                    continue
                previous = self.base / 'jobs' / pid / ('attempt-' + str(row['attempts']))
                if not all((previous / name).exists() for name in
                           ('draft.json', 'critic.json', 'synthesis-gates.json', 'source-receipts.json')):
                    continue
                write_json(marker, {'claimed_at': time.time(), 'prior_attempt': row['attempts'],
                    'prior_failures': row['error'], 'reason': 'Explicit extra repair after input-navigation correction; no automatic further retries.'})
                attempt = row['attempts'] + 1
                db.execute("UPDATE jobs SET state='running',attempts=?,updated=?,owner=?,error=NULL WHERE player_id=?",
                           (attempt, time.time(), os.getpid(), pid))
                return {**dict(row), 'attempts': attempt, 'state': 'running',
                        'previous_attempt': str(previous)}
        return None

    def work(self, row, publish=False):
        pid = row['player_id']; directory = self.base / 'jobs' / pid
        try:
            directory = self._prepare(row, Path(row['previous_attempt']))
            write_json(directory / 'evidence/root-review-notes.json', {
                'notes': 'This is an explicitly counted additional correction pass. Read prior gate issues AND prior critic issues in repair-feedback. Read evidence/professional_forecasts.json including all weekly rows, and weekly_fantasy_score_quotes even if season market ladders are empty. Disclose weekly coverage, meaningful disagreement, stale team/opponent conditioning, and playoff scope without turning means into floors. Packet OTC data may be cited as packet-derived without pretending a live OTC URL was read. Do not mark a timed-out URL read firsthand. Do not invent board availability, draft windows, or2mandatoryQBs. A conditional decision rule may stop short of recommending this player absent actual alternatives. Preserve real source uncertainty rather than inventing missing facts to satisfy a critic.'})
            report = self._round(row, directory, 2)
            if report['passed']:
                self.runtime.state(pid, 'reviewed')
                result = {'player_id': pid, 'state': 'reviewed', 'additional_repair': True,
                          'attempt': row['attempts']}
                if publish:
                    result['publication'] = self.runtime.publish(pid)
                    result['state'] = 'published'
                return result
            self.runtime.state(pid, 'needs_revision', '; '.join(report['issues'])[:3000])
            return {'player_id': pid, 'state': 'needs_revision', 'additional_repair': True,
                    'issues': report['issues']}
        except Exception as exc:
            self.runtime.state(pid, 'error', str(exc)[:3000])
            self._record(row, 2, 'error', directory, finished=True)
            return {'player_id': pid, 'state': 'error', 'error': str(exc)}


class CodexCombined(CodexRepair):
    """Drain fresh work first, then use freed slots for bounded repairs."""
    def claim(self, first=217, last=400):
        row = CodexSynthesis.claim(self, first, last)
        return row if row is not None else CodexRepair.claim(self, first, last)

    def work(self, row, publish=False):
        if 'previous_attempt' in row:
            return CodexRepair.work(self, row, publish=publish)
        return CodexSynthesis.work(self, row, publish=publish)


class CodexSourceRepair(CodexRepair):
    """One targeted pass for source-only failures after exact diagnostics exist."""
    marker_name = 'codex-source-targeted-repair.json'
    source_only = True

    def _prepare(self, row, previous=None):
        directory = super()._prepare(row, previous)
        from .swarm_publication import validate_sources
        old = Path(previous)
        try:
            validate_sources(json.loads((old / 'draft.json').read_text()),
                json.loads((old / 'critic.json').read_text()),
                json.loads((old / 'source-receipts.json').read_text()), old,
                self.base / 'source-cache')
        except ValueError as exc:
            detail = str(exc)
        else:
            raise ValueError('Source-only recovery found no source defect')
        write_json(directory / 'evidence/source-repair-targets.json', {
            'exact_failure': detail, 'prior_attempt': str(old),
            'instruction': 'Preserve the already substantive case. Actually read necessary archived originals, declare accurate source-read status, and include any necessary missing exact URL for archiving. A packet-derived claim can remain explicitly packet-attributed without claiming a live URL was read. Do not blindly change booleans, fabricate receipts, or strip support needed by the narrative. A fresh independent critic must read the corrected sources.'})
        return directory


class CodexSemanticRepair(CodexRepair):
    """One surgical edit comparison after broader rewrites left identified defects."""
    marker_name = 'codex-surgical-semantic-repair.json'
    semantic_only = True

    def _prepare(self, row, previous=None):
        directory = super()._prepare(row, previous)
        old = Path(previous)
        write_json(directory / 'evidence/semantic-repair-targets.json', {
            'prior_gate_report': json.loads((old / 'synthesis-gates.json').read_text()),
            'prior_critic': json.loads((old / 'critic.json').read_text()),
            'instruction': 'Preserve all correct case-specific facts, source comparisons, uncertainty and causal analysis. Make the smallest supported edits resolving each remaining issue. Do not invent a numeric threshold to satisfy a demand for concreteness; an explicit observation window and conditional comparator can be a policy test, labeled unvalidated. A Cook-absence or similar contingency must be tested when that condition occurs; low usage while the incumbent plays does not falsify it. Do not fabricate board alternatives; state conditional decision criteria and withhold an unconditional pick when those facts are absent. Distinguish source publication/update dates from receipt and narrative dates. Read every acquired provider reference and weekly series; preserve meaningful disagreements rather than turning them into a consensus or calibrated distribution. Verify any needed new facts against an original. If the evidence is insufficient, say what remains unresolved without replacing it with generic confidence. A new independent audit is required.'})
        return directory


class CodexErrataRepair(CodexSemanticRepair):
    """One final, separately counted correction of the latest exact defects."""
    marker_name = 'codex-final-errata-repair.json'


class CodexTransportRecovery(CodexSynthesis):
    """Reuse a genuinely completed author; always obtain a fresh bound critic."""
    def claim(self, first=217, last=400):
        if not 217 <= first <= last <= 400:
            raise ValueError('Codex recovery owns ranks217–400 only')
        if self.runtime.pause_path.exists():
            self.runtime.stop.set(); return None
        with self.runtime.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM jobs WHERE rank BETWEEN ? AND ? AND state='error' AND error LIKE 'Codex exit 0:%' ORDER BY rank",
                                  (first, last)).fetchall():
                previous = self.base / 'jobs' / row['player_id'] / ('attempt-' + str(row['attempts']))
                result = previous / 'author.codex-result.json'; events = previous / 'author.events.jsonl'
                if not result.exists() or not events.exists():
                    continue
                parsed = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
                terminal = [v['type'] for v in parsed if v.get('type') in ('turn.failed', 'turn.completed')]
                if not terminal or terminal[-1] != 'turn.completed':
                    continue
                author = json.loads(result.read_text())
                if author.get('player_id') != row['player_id']:
                    continue
                attempt = row['attempts'] + 1
                db.execute("UPDATE jobs SET state='running',attempts=?,updated=?,owner=?,error=NULL WHERE player_id=?",
                           (attempt, time.time(), os.getpid(), row['player_id']))
                return {**dict(row), 'attempts': attempt, 'state': 'running',
                        'recover_previous': str(previous)}
        return None

    def _prepare(self, row, previous=None):
        if previous is not None:
            return super()._prepare(row, previous)
        old = Path(row['recover_previous'])
        directory = old.parent / ('attempt-' + str(row['attempts']))
        shutil.copytree(old, directory)
        author = json.loads((old / 'author.codex-result.json').read_text())
        write_json(directory / 'evidence/recovered-author.json', {
            'new_attempt_directory': directory.name, 'structured_output': author,
            'structured_output_sha256': sha(json.dumps(author, sort_keys=True).encode()),
            'provenance': {'original_result': str(old / 'author.codex-result.json'),
                           'original_result_sha256': sha((old / 'author.codex-result.json').read_bytes()),
                           'original_events_sha256': sha((old / 'author.events.jsonl').read_bytes()),
                           'recovered_at': time.time(),
                           'reason': 'Completed CLI result was incorrectly rejected for a recovered reconnect event.'}})
        return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.moneyball')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--workers', type=int, default=4)
    run.add_argument('--limit', type=int, default=4)
    run.add_argument('--first', type=int, default=229)
    run.add_argument('--last', type=int, default=400)
    run.add_argument('--wait-seconds', type=int, default=0,
                     help='Bounded wait for newly eligible correction jobs; no duplicate calls')
    run.add_argument('--publish', action='store_true')
    run.add_argument('--repair-failed', action='store_true',
                     help='One explicitly counted extra repair using previous failed draft and audit')
    run.add_argument('--fresh-then-repair', action='store_true',
                     help='Use available slots for fresh cases first, then bounded extra repairs')
    run.add_argument('--recover-transport', action='store_true',
                     help='Reuse completed author output discarded by the old reconnect classifier; fresh critic')
    run.add_argument('--repair-source-only', action='store_true',
                     help='One source-targeted correction after exact URL diagnostics; full fresh audit')
    run.add_argument('--repair-semantic', action='store_true',
                     help='One surgical correction pass over known substantive failures; fresh audit')
    run.add_argument('--repair-final-errata', action='store_true',
                     help='One separately counted final exact-defect correction; full fresh audit')
    sub.add_parser('status')
    args = parser.parse_args()
    engine_type = CodexErrataRepair if getattr(args, 'repair_final_errata', False) else (
        CodexSemanticRepair if getattr(args, 'repair_semantic', False) else (
        CodexSourceRepair if getattr(args, 'repair_source_only', False) else (
        CodexTransportRecovery if getattr(args, 'recover_transport', False) else (
        CodexCombined if getattr(args, 'fresh_then_repair', False) else (
        CodexRepair if getattr(args, 'repair_failed', False) else CodexSynthesis)))))
    engine = engine_type(args.root)
    result = engine.status() if args.command == 'status' else engine.run(
        workers=args.workers, limit=args.limit, first=args.first, last=args.last,
        wait_seconds=args.wait_seconds, publish=args.publish)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
