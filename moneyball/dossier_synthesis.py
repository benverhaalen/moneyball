"""Source-backed Fable dossiers, independent Opus review, fail-closed publication.

Shares the existing Swarm queue, but only claims pending ranks181–400 for which
a completed scout receipt exists. Scout excerpts are evidence to audit, not facts
approved for publication. Every failed author/review round stays in its attempt.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

from .dossier_swarm import (AUTHOR_PROMPT, AUTHOR_SCHEMA, CRITIC_PROMPT,
                            CRITIC_SCHEMA, Swarm, sha, write_json)
from .swarm_publication import (validate_binding, validate_components,
                                validate_sources)


SYNTHESIS_PROMPT = """
You are the dossier synthesist, not a source scout. Read evidence/scout-report.json
and its source-N.txt originals as well as context.json, brief.json, rules.md and
the relevant evidence sections. Start with the brief, source-audit flags and the
three to six relevant archived official texts. Reuse acquired history and dated
professional metadata; do not re-research them. New WebSearch/WebFetch is only
for unresolved load-bearing facts or an original source you cannot read locally,
not for recapitulating the whole case. Reading the available original sources
satisfies the primary-reading requirement; no new search is needed to repeat
already resolved facts. Read provider_references explicitly: standalone
commercial median or injury probabilities retain their own target definition;
injury probability is not expected games missed. Include provider_references
under packet_sections_read after reading it. The scout's literal-match flag does
NOT approve semantic correctness, attribution, date interpretation, or
relevance to this player. Mismatched/rejected cards are rejected leads until you
independently resolve them. If evidence/scout-audit.json exists, read its per-card
results and limitations; it also is evidence, not authority.
Trust the actual archived original body over WebFetch's generated summaries.
Do not quote a summary as the original source. For every new source you use,
return its exact URL so it can be archived for independent review. The critic
will read newly archived original bodies before approving the dossier.
There is no age or contract-expiry rule sending production or value to zero in
2028. No annual mean or average target rate is a weekly floor. No single-player
projection comparison, crowd rank, target threshold or generic age heuristic
decides whether to draft the player: compare feasible complete roster paths.
Every branch must preserve retained/other-team employment possibilities where
they exist. Separate NFL performance from the probability and timing of earning
the opportunity to perform. Do not claim a QB/environment is irrelevant without
evidence. Future scenario ranges are not calibrated CIs or title probabilities.
Your `searches` may include clearly labeled inherited scout searches that you
used, plus your own actual searches; never claim to have personally run inherited
searches. Return all acquired professional source_key arithmetic checks exactly.
"""

STRONG_CRITIC_PROMPT = """
Read evidence/scout-report.json, any evidence/scout-audit.json, and archived
original source-N.txt bodies. Literal matching by source scouts is not semantic
verification. In particular verify the exact player, team, year, quoted wording,
date, who said it, and whether a fact concerns this player or another player on
the page. WebFetch summaries may contain invented quotations; the original
archived body controls. Unresolved evidence must not become an asserted fact.
Read provider_references as well as every acquired professional component row.
Reject a mean converted to a weekly floor; contract expiry or age used to force
future value to zero; an injury probability treated as expected absence; an
isolated projection or crowd rank used as the sufficient draft decision rule;
unfunded easy waiver replacement; or unsupported certainty about future team/QB
environment. Do not excuse these failures merely because the dossier elsewhere
contains disclaimers. Your approval is an independent semantic audit, never a
claim that the underlying projections or annual title probabilities are calibrated.
"""


def checked_file(path, within, expected=None):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(within).resolve()) or not path.is_file():
        raise ValueError('Evidence path is missing or outside its archive')
    if expected is not None and sha(path.read_bytes()) != expected:
        raise ValueError('Evidence hash mismatch: ' + str(path))
    return path


class Synthesis:
    def __init__(self, root='.moneyball', executable='claude', runtime=None,
                 author_model='claude-fable-5-1', author_effort='xhigh',
                 critic_model='claude-opus-5', critic_effort='high'):
        self.runtime = runtime or Swarm(root, executable)
        self.root = self.runtime.root
        self.base = self.runtime.base
        self.scouts = self.base / 'scouts'
        self.author_model, self.author_effort = author_model, author_effort
        self.critic_model, self.critic_effort = critic_model, critic_effort
        with self.runtime.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS synthesis_rounds (
                player_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                round_index INTEGER NOT NULL, state TEXT NOT NULL,
                started REAL NOT NULL, finished REAL, report_path TEXT,
                PRIMARY KEY(player_id,attempt))''')

    def claim(self, first=181, last=400):
        if not 181 <= first <= last <= 400:
            raise ValueError('Synthesis only owns ranks181–400')
        if (self.base / 'claude-pause.json').exists():
            self.runtime.stop.set()
            return None
        with self.runtime.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM jobs WHERE state='pending' AND rank BETWEEN ? AND ? ORDER BY rank",
                                  (first, last)).fetchall():
                pid = row['player_id']
                if not re.fullmatch(r'[A-Za-z0-9_-]+', pid):
                    raise ValueError('Unsafe player identifier')
                if not (self.scouts / 'jobs' / pid / 'latest.json').is_file():
                    continue
                attempt = row['attempts'] + 1
                db.execute("UPDATE jobs SET state='running',attempts=?,updated=?,owner=?,error=NULL WHERE player_id=?",
                           (attempt, time.time(), os.getpid(), pid))
                return {**dict(row), 'attempts': attempt, 'state': 'running'}
        return None

    def _snapshot_scout(self, directory, row):
        pid = row['player_id']
        source_dir = self.scouts / 'jobs' / pid
        latest_path = checked_file(source_dir / 'latest.json', source_dir)
        latest = json.loads(latest_path.read_text())
        path = checked_file(latest['report_path'], source_dir, latest['report_sha256'])
        report = json.loads(path.read_text())
        if report.get('player_id') != pid or report.get('cohort_rank') != row['rank']:
            raise ValueError('Scout identity/rank does not match queue')
        provenance = json.loads(checked_file(source_dir / 'input-provenance.json', source_dir).read_text())
        if provenance.get('original_packet_sha256') != row['packet_sha']:
            raise ValueError('Scout and synthesis packet versions differ')
        frozen = {'interpretation': 'Untrusted source leads; literal excerpts are not semantic approval.',
                  'latest': latest, 'latest_sha256': sha(latest_path.read_bytes()),
                  'source_report_path': str(path), 'source_report_sha256': sha(path.read_bytes()),
                  'report': report, 'readable_originals': [], 'source_input_provenance': provenance}
        texts = {}
        for card in report.get('cards', []):
            receipt = card.get('receipt') or {}
            if receipt.get('status') != 200 or receipt.get('archive_error'):
                continue
            checked_file(receipt['raw_path'], self.base / 'source-cache', receipt['raw_sha256'])
            name = receipt.get('local_text', '')
            if not re.fullmatch(r'source-\d+\.txt', name):
                raise ValueError('Unexpected scout text path')
            original = checked_file(path.parent / name, path.parent, card.get('text_sha256'))
            key = str(original)
            if key not in texts:
                copied = 'source-' + str(10000 + len(texts)) + '.txt'
                shutil.copyfile(original, directory / copied)
                texts[key] = copied
                frozen['readable_originals'].append({'url': receipt['url'], 'local_text': copied,
                    'text_sha256': sha(original.read_bytes()), 'raw_path': receipt['raw_path'],
                    'raw_sha256': receipt['raw_sha256'], 'observed_at': receipt['observed_at']})
        write_json(directory / 'evidence/scout-report.json', frozen)
        audit_base = self.scouts / 'audits' / pid
        audit_latest = audit_base / 'latest.json'
        if audit_latest.exists():
            pointer = json.loads(checked_file(audit_latest, audit_base).read_text())
            audit_path = checked_file(pointer['report_path'], audit_base, pointer['report_sha256'])
            audit = json.loads(audit_path.read_text())
            if audit.get('player_id', pid) != pid:
                raise ValueError('Source audit player identity mismatch')
            matching = pointer.get('source_report_sha256') == sha(path.read_bytes())
            write_json(directory / 'evidence/scout-audit.json', {
                'source_report_matches': matching,
                'interpretation': 'Per-card audit is evidence, not final approval.' if matching else
                                  'STALE AUDIT: refers to another scout version; do not transfer approval.',
                'latest': pointer, 'latest_sha256': sha(audit_latest.read_bytes()), 'report': audit})

    def _prepare(self, row, previous=None):
        pid = row['player_id']
        job = self.base / 'jobs' / pid
        directory = job / ('attempt-' + str(row['attempts']))
        # Never overwrite a previous model result or the experimental177–180 jobs.
        directory.mkdir(exist_ok=False)
        if previous is None:
            self.runtime.refresh_context(pid)
        source = previous or job
        for name in ('context.json', 'packet.json', 'rules.md', 'brief.json'):
            shutil.copyfile(source / name, directory / name)
        shutil.copytree(source / 'evidence', directory / 'evidence')
        if sha((directory / 'packet.json').read_bytes()) != row['packet_sha']:
            raise ValueError('Synthesis packet hash mismatch')
        if previous is None:
            self._snapshot_scout(directory, row)
            if (job / 'root-review-notes.md').exists():
                write_json(directory / 'evidence/root-review-notes.json', {
                    'notes': (job / 'root-review-notes.md').read_text(),
                    'sha256': sha((job / 'root-review-notes.md').read_bytes())})
        else:
            prior_receipts = json.loads((previous / 'source-receipts.json').read_text())
            for path in previous.glob('source-*.txt'):
                if int(path.stem.split('-')[1]) >= 10000:
                    shutil.copyfile(path, directory / path.name)
            # The failed first draft may have introduced useful new sources.
            # Preserve their exact bodies for repair, with names that cannot be
            # overwritten by the next author's archive(source-0,source-1,...).
            for i, receipt in enumerate(prior_receipts):
                if receipt.get('status') == 200 and not receipt.get('archive_error'):
                    original = checked_file(previous / receipt['local_text'], previous)
                    name = 'source-' + str(20000 + i) + '.txt'
                    shutil.copyfile(original, directory / name)
                    receipt = {**receipt, 'local_text': name,
                               'text_sha256': sha(original.read_bytes())}
                    prior_receipts[i] = receipt
            write_json(directory / 'evidence/repair-feedback.json', {
                'prior_draft': json.loads((previous / 'draft.json').read_text()),
                'prior_critic': json.loads((previous / 'critic.json').read_text()),
                'prior_gate_report': json.loads((previous / 'synthesis-gates.json').read_text()),
                'prior_sources': prior_receipts})
        return directory

    def _record(self, row, round_index, state, directory, finished=False):
        with self.runtime.db() as db:
            db.execute('''INSERT INTO synthesis_rounds VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(player_id,attempt) DO UPDATE SET state=excluded.state,
                finished=excluded.finished,report_path=excluded.report_path''',
                (row['player_id'], row['attempts'], round_index, state, time.time(),
                 time.time() if finished else None, str(directory / 'synthesis-gates.json')))

    def _round(self, row, directory, round_index):
        self._record(row, round_index, 'author_running', directory)
        prompt = AUTHOR_PROMPT + SYNTHESIS_PROMPT
        if (directory / 'evidence/root-review-notes.json').exists():
            prompt += '\nRead evidence/root-review-notes.json and address its listed failures.\n'
        if round_index:
            prompt += ('\nRead evidence/repair-feedback.json. This is the one allowed repair. '
                       'Resolve every factual, strategic, source and deterministic arithmetic '
                       'issue. Return the complete corrected dossier and all source checks.\n')
        value = self.runtime.invoke(directory, 'author', prompt, AUTHOR_SCHEMA,
                                    model=self.author_model, effort=self.author_effort)
        write_json(directory / 'draft.json', value)
        issues = []
        for check in (lambda: self.runtime.check_author(value, row['player_id']),
                      lambda: validate_components(value, json.loads((directory / 'context.json').read_text()))):
            try:
                check()
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(str(exc))
        if not any('professional_forecasts' in str(s) or 'professional_components' in str(s)
                   for s in value.get('packet_sections_read', [])):
            issues.append('Required professional evidence section not declared as read')
        if not any('provider_references' in str(s) for s in value.get('packet_sections_read', [])):
            issues.append('Required provider_references section not declared as read')
        packet = json.loads((directory / 'packet.json').read_text())
        prose = ''.join(value.get('dossier_markdown', '').lower().split())
        for name in ('clay', 'fftoday'):
            if packet.get('professional_forecasts', {}).get(name) and name not in prose:
                issues.append('Available provider omitted from dossier: ' + name)
        if len(value.get('sources', [])) >= 10000:
            raise ValueError('Author source archive collided with scout originals')
        receipts = self.runtime.archive(directory, value.get('sources', []))
        # Additional synthesis/scout provenance is bound through evidence/*.json,
        # and scout originals use source-N.txt paths accepted by the shared gate.
        inputs = ['draft.json', 'source-receipts.json', 'packet.json', 'context.json', 'rules.md', 'brief.json']
        inputs += [p.name for p in directory.glob('source-*.txt')]
        inputs += [str(p.relative_to(directory)) for p in (directory / 'evidence').glob('*.json')]
        bound = {name: sha((directory / name).read_bytes()) for name in inputs}
        self._record(row, round_index, 'critic_running', directory)
        critic = self.runtime.invoke(directory, 'critic', CRITIC_PROMPT + STRONG_CRITIC_PROMPT,
                                     CRITIC_SCHEMA, model=self.critic_model, effort=self.critic_effort)
        write_json(directory / 'critic.json', critic)
        if any(sha((directory / name).read_bytes()) != digest for name, digest in bound.items()):
            raise ValueError('Evidence changed during independent review')
        bound['critic.json'] = sha((directory / 'critic.json').read_bytes())
        write_json(directory / 'review-binding.json', {'schema_version': 1, 'bound_at': time.time(), 'sha256': bound})
        try:
            validate_sources(value, critic, receipts, directory, self.base / 'source-cache')
            binding, _ = validate_binding(directory, receipts)
            if any(binding['bound_at'] < r['observed_at'] for r in receipts if r.get('status') == 200):
                raise ValueError('Review binding predates source observation')
        except (ValueError, KeyError, TypeError) as exc:
            issues.append(str(exc))
        if critic.get('approved') is not True or critic.get('issues'):
            issues.extend(critic.get('issues') or ['Independent critic did not approve'])
        issues = list(dict.fromkeys(issues))
        report = {'player_id': row['player_id'], 'attempt': row['attempts'], 'round_index': round_index,
                  'available_at': time.time(), 'passed': not issues, 'issues': issues,
                  'author': {'requested_model': self.author_model, 'effort': self.author_effort},
                  'critic': {'requested_model': self.critic_model, 'effort': self.critic_effort},
                  'interpretation': 'Source/arithmetic/independent semantic gates; not calibrated forecast accuracy.'}
        write_json(directory / 'synthesis-gates.json', report)
        self._record(row, round_index, 'passed' if not issues else 'rejected', directory, finished=True)
        return report

    def work(self, row, publish=False):
        pid = row['player_id']; previous = None; directory = self.base / 'jobs' / pid
        try:
            for round_index in range(2):
                directory = self._prepare(row, previous)
                report = self._round(row, directory, round_index)
                if report['passed']:
                    self.runtime.state(pid, 'reviewed')
                    result = {'player_id': pid, 'state': 'reviewed', 'rounds': round_index + 1,
                              'attempt': row['attempts'], 'report_path': str(directory / 'synthesis-gates.json')}
                    if publish:
                        try:
                            result['publication'] = self.runtime.publish(pid)
                            result['state'] = 'published'
                        except Exception as exc:
                            # A destination conflict does not invalidate reviewed
                            # bytes; leave the gate available for safe resumption.
                            result['publication_error'] = str(exc)
                            write_json(directory / 'publication-error.json', {
                                'observed_at': time.time(), 'error': str(exc)})
                    return result
                if round_index == 0:
                    previous = directory
                    with self.runtime.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        current = db.execute('SELECT * FROM jobs WHERE player_id=?', (pid,)).fetchone()
                        if current['state'] != 'running' or current['owner'] != os.getpid() or current['attempts'] != row['attempts']:
                            raise ValueError('Queue ownership changed before repair')
                        row = {**row, 'attempts': row['attempts'] + 1}
                        db.execute('UPDATE jobs SET attempts=?,updated=? WHERE player_id=?', (row['attempts'], time.time(), pid))
            self.runtime.state(pid, 'needs_revision', '; '.join(report['issues'])[:3000])
            return {'player_id': pid, 'state': 'needs_revision', 'rounds': 2, 'issues': report['issues']}
        except Exception as exc:
            self.runtime.state(pid, 'error', str(exc)[:3000])
            self._record(row, int(previous is not None), 'error', directory, finished=True)
            if any(w in str(exc).lower() for w in ('authentication', 'not logged in', 'billing', 'credit balance')):
                self.runtime.stop.set()
            return {'player_id': pid, 'state': 'error', 'error': str(exc)}

    def status(self):
        with self.runtime.db() as db:
            states = dict(db.execute('SELECT state,count(*) FROM synthesis_rounds GROUP BY state').fetchall())
            players = db.execute('SELECT count(DISTINCT player_id) FROM synthesis_rounds').fetchone()[0]
        return {'queue': self.runtime.status(), 'synthesis_rounds': states,
                'players_attempted': players, 'rounds_attempted': sum(states.values()),
                'interpretation': 'All attempted rounds including rejected/error rounds; passes are not forecast-validation wins.'}

    def run(self, workers=4, limit=8, wait_seconds=600, poll_seconds=5,
            first=181, last=400, publish=False):
        if not 1 <= workers <= 32 or limit < 1 or wait_seconds < 0 or not 0 < poll_seconds <= 60:
            raise ValueError('Invalid workers, positive limit, bounded poll or wait budget')
        deadline = time.monotonic() + wait_seconds
        count = 0; lock = threading.Lock()
        def loop():
            nonlocal count
            while not self.runtime.stop.is_set():
                with lock:
                    if count >= limit:
                        return
                    row = self.claim(first, last)
                    if row:
                        count += 1
                if row is None:
                    if time.monotonic() >= deadline:
                        return
                    self.runtime.stop.wait(min(poll_seconds, max(0, deadline - time.monotonic())))
                    continue
                print(json.dumps(self.work(row, publish=publish)), flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in as_completed([pool.submit(loop) for _ in range(workers)]):
                future.result()
        return {'jobs_claimed': count, **self.status()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.moneyball')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--workers', type=int, default=4)
    run.add_argument('--limit', type=int, default=8)
    run.add_argument('--wait-seconds', type=float, default=600)
    run.add_argument('--poll-seconds', type=float, default=5)
    run.add_argument('--first', type=int, default=181)
    run.add_argument('--last', type=int, default=400)
    run.add_argument('--publish', action='store_true')
    sub.add_parser('status')
    args = parser.parse_args(); synthesis = Synthesis(args.root)
    result = synthesis.status() if args.command == 'status' else synthesis.run(
        workers=args.workers, limit=args.limit, wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds, first=args.first, last=args.last, publish=args.publish)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
