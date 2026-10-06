import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneyball.dossier_codex import (CodexRuntime, CodexSynthesis, CodexRepair,
                                    CodexCombined, CodexTransportRecovery)
from moneyball.dossier_swarm import Swarm, sha, write_json


class CodexAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / '.moneyball'
        folder = self.root / 'draft/player-packets'; folder.mkdir(parents=True)
        entries = []
        for pid, rank in [('protected', 180), ('p', 229), ('q', 230)]:
            raw = json.dumps({'player_id': pid, 'name': 'Player', 'professional_forecasts': {}}).encode()
            (folder / (pid + '.json')).write_bytes(raw)
            entries.append({'player_id': pid, 'cohort_rank': rank, 'name': 'Player', 'path': pid + '.json', 'sha256': sha(raw)})
        write_json(folder / 'index.json', {'information_cutoff': 1, 'players': entries})
        reviews = self.root / 'research/player-reviews'; reviews.mkdir(parents=True)
        (reviews / 'README.md').write_text('Rules')
        self.runtime = CodexRuntime(self.root); self.runtime.seed(180, 230)
        self.engine = CodexSynthesis(self.root, runtime=self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_models_and_missing_scout_are_not_false_approval(self):
        self.assertEqual(self.engine.author_model, 'gpt-5.6-terra')
        self.assertEqual(self.engine.critic_model, 'gpt-5.6-sol')
        row = self.engine.claim(); self.assertEqual(row['player_id'], 'p')
        directory = self.engine._prepare(row)
        report = json.loads((directory / 'evidence/scout-report.json').read_text())
        self.assertEqual(report['report']['cards'], [])
        self.assertFalse(report['report']['semantic_claims_verified'])
        with self.runtime.db() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE player_id='protected'").fetchone()[0], 'pending')

    def test_only_old_claude_failure_is_automatically_reclaimed(self):
        self.runtime.state('p', 'error', 'Claude exit 1: ')
        self.runtime.state('q', 'error', 'Codex exit 1: bad schema')
        self.assertEqual(self.engine.claim()['player_id'], 'p')
        self.assertIsNone(self.engine.claim())

    def test_cli_isolation_models_and_structured_output(self):
        directory = self.runtime.base / 'jobs/p'; captured = []
        def process(argv, **kwargs):
            captured.append((argv, kwargs))
            Path(argv[argv.index('-o') + 1]).write_text('{"player_id":"p"}')
            class Result:
                returncode = 0
                def wait(self, timeout): return 0
            return Result()
        with patch('moneyball.dossier_codex.subprocess.Popen', side_effect=process):
            result = self.runtime.invoke(directory, 'critic', 'Read sources.', {'type': 'object'}, model='gpt-5.6-sol')
        argv, options = captured[0]
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-5.6-sol')
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
        for flag in ['--ignore-user-config', '--ignore-rules', '--ephemeral', '--output-schema', '--search']:
            self.assertIn(flag, argv)
        self.assertEqual(result, {'player_id': 'p'})
        self.assertTrue(options['start_new_session'])

    def test_structured_limit_nonzero_exit_pauses_every_controller(self):
        directory = self.runtime.base / 'jobs/p'
        def process(argv, **kwargs):
            kwargs['stdout'].write(json.dumps({'type': 'error', 'message': 'usage_limit reached'}) + '\n')
            class Result:
                returncode = 1
                def wait(self, timeout): return 1
            return Result()
        with patch('moneyball.dossier_codex.subprocess.Popen', side_effect=process):
            with self.assertRaisesRegex(RuntimeError, 'usage_limit'):
                self.runtime.invoke(directory, 'author', 'Read.', {'type': 'object'})
        self.assertTrue(self.runtime.pause_path.exists())
        self.assertIsNone(CodexSynthesis(self.root).claim())
        with patch('moneyball.dossier_codex.subprocess.Popen') as invoke:
            with self.assertRaisesRegex(RuntimeError, 'paused'):
                CodexRuntime(self.root).invoke(directory, 'author', 'Read.', {'type': 'object'})
            invoke.assert_not_called()

    def test_recovered_reconnect_does_not_discard_successful_structured_result(self):
        directory = self.runtime.base / 'jobs/p'
        def process(argv, **kwargs):
            kwargs['stdout'].write(json.dumps({'type': 'error', 'message': 'Reconnecting...2/5'}) + '\n')
            kwargs['stdout'].write(json.dumps({'type': 'turn.completed'}) + '\n')
            Path(argv[argv.index('-o') + 1]).write_text('{"player_id":"p"}')
            class Result:
                returncode = 0
                def wait(self, timeout): return 0
            return Result()
        with patch('moneyball.dossier_codex.subprocess.Popen', side_effect=process):
            result = self.runtime.invoke(directory, 'author', 'Read.', {'type': 'object'})
        self.assertEqual(result['player_id'], 'p')
        self.assertTrue((directory / 'author.recovered-transport.json').exists())

    def test_astra_is_never_dispatched(self):
        with patch('moneyball.dossier_codex.subprocess.Popen') as invoke:
            with self.assertRaisesRegex(ValueError, 'Terra/Sol'):
                self.runtime.invoke(self.runtime.base / 'jobs/p', 'author', '', {}, model='gpt-6-astra')
        invoke.assert_not_called()

    def test_explicit_extra_repair_is_one_pass_and_never_claims_live_job(self):
        row = self.engine.claim()
        directory = self.engine._prepare(row)
        for name in ('draft.json', 'critic.json', 'synthesis-gates.json', 'source-receipts.json'):
            write_json(directory / name, {'original': True})
        self.runtime.state('p', 'needs_revision', 'Missing weekly evidence')
        self.engine.claim()  # q remains owned/running, never taken by repair.
        repair = CodexRepair(self.root, runtime=self.runtime)
        claimed = repair.claim()
        self.assertEqual(claimed['player_id'], 'p')
        self.assertEqual(claimed['attempts'], 2)
        self.assertEqual(claimed['previous_attempt'], str(directory))
        self.assertEqual(json.loads((directory / 'draft.json').read_text()), {'original': True})
        self.runtime.state('p', 'needs_revision', 'Still rejected')
        self.assertIsNone(repair.claim())
        with self.runtime.db() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE player_id='q'").fetchone()[0], 'running')

    def test_combined_prioritizes_fresh_jobs_before_bounded_repair(self):
        row = self.engine.claim()
        directory = self.engine._prepare(row)
        for name in ('draft.json', 'critic.json', 'synthesis-gates.json', 'source-receipts.json'):
            write_json(directory / name, {})
        self.runtime.state('p', 'needs_revision', 'Correctable issue')
        engine = CodexCombined(self.root, runtime=self.runtime)
        first = engine.claim()
        self.assertEqual(first['player_id'], 'q')
        self.assertNotIn('previous_attempt', first)
        second = engine.claim()
        self.assertEqual(second['player_id'], 'p')
        self.assertIn('previous_attempt', second)

    def test_transport_recovery_reuses_completed_author_without_changing_original(self):
        row = self.engine.claim(); original = self.engine._prepare(row)
        author = {'player_id': 'p', 'dossier_markdown': 'Observed original author'}
        write_json(original / 'author.codex-result.json', author)
        (original / 'author.events.jsonl').write_text(json.dumps({'type': 'turn.completed'}) + '\n')
        self.runtime.state('p', 'error', 'Codex exit 0: Reconnecting...')
        engine = CodexTransportRecovery(self.root, runtime=self.runtime)
        claimed = engine.claim(); directory = engine._prepare(claimed)
        self.assertNotEqual(directory, original)
        with patch('moneyball.dossier_codex.subprocess.Popen') as invoke:
            result = self.runtime.invoke(directory, 'author', 'Read.', {})
            invoke.assert_not_called()
        self.assertEqual(result, author)
        self.assertEqual(json.loads((original / 'author.codex-result.json').read_text()), author)
        self.assertIn('fresh independent critic', (directory / 'author.stdout.json').read_text())

    def test_claude_nonzero_structured_session_limit_stops_other_claude_claims(self):
        runtime = Swarm(self.root)
        def process(argv, **kwargs):
            kwargs['stdout'].write(json.dumps({'type': 'result', 'is_error': True,
                'error': 'rate_limit', 'result': "You've hit your session limit · resets 4pm (America/Chicago)"}) + '\n')
            class Result:
                returncode = 1
                def wait(self, timeout): return 1
            return Result()
        with patch('moneyball.dossier_swarm.subprocess.Popen', side_effect=process) as invoke:
            with self.assertRaisesRegex(RuntimeError, 'session limit'):
                runtime.invoke(runtime.base / 'jobs/p', 'limit-test', 'Read.', {'type': 'object'})
            self.assertEqual(invoke.call_count, 1)
        pause = json.loads((runtime.base / 'claude-pause.json').read_text())
        self.assertIn('4pm', pause['reason'])
        self.assertIsNone(Swarm(self.root).claim())
        # The user-authorized Codex provider remains separate from Claude's cap.
        self.assertEqual(self.engine.claim()['player_id'], 'p')


if __name__ == '__main__':
    unittest.main()
