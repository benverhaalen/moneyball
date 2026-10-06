import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.dossier_swarm import Swarm, sha, write_json
from moneyball.dossier_synthesis import Synthesis
from moneyball.swarm_publication import validate_binding


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / '.moneyball'
        packet_dir = self.root / 'draft/player-packets'
        packet_dir.mkdir(parents=True)
        entries = []
        for pid, rank in [('protected', 180), ('p', 181), ('q', 182)]:
            packet = {'player_id': pid, 'name': 'Public Player', 'provider_references': {'note': 'median is not mean'},
                      'professional_forecasts': {'clay': {'stats': {'rec': 10, 'rec_yd': 100, 'rec_td': 2}}}}
            raw = json.dumps(packet).encode(); (packet_dir / (pid + '.json')).write_bytes(raw)
            entries.append({'player_id': pid, 'cohort_rank': rank, 'name': 'Public Player',
                            'path': pid + '.json', 'sha256': sha(raw)})
        write_json(packet_dir / 'index.json', {'information_cutoff': 1, 'players': entries})
        reviews = self.root / 'research/player-reviews'; reviews.mkdir(parents=True)
        (reviews / 'README.md').write_text('Use exact configured league scoring and complete rosters.')
        self.runtime = Swarm(self.root); self.runtime.seed(180, 182)
        self.engine = Synthesis(self.root, runtime=self.runtime)
        self.sources = []
        cache = self.runtime.base / 'source-cache'; cache.mkdir(exist_ok=True)
        for i in range(2):
            url = 'https://www.nfl.com/news/primary-' + str(i)
            data = ('<p>Public Player original reported fact ' + str(i) + '</p>').encode()
            raw = cache / (sha(data) + '.raw'); raw.write_bytes(data)
            receipt = {'url': url, 'final_url': url, 'status': 200, 'observed_at': time.time(),
                       'raw_path': str(raw), 'raw_sha256': sha(data)}
            write_json(cache / (sha(url.encode()) + '.json'), receipt)
            self.sources.append({'url': url, 'title': 'Original fact', 'published_at': '',
                                 'claim_supported': 'Bounded fact', 'read_firsthand': True, 'primary': True})

    def tearDown(self):
        self.tmp.cleanup()

    def scout(self, pid='p', bad_hash=False):
        base = self.runtime.base / 'scouts/jobs' / pid
        attempt = base / 'attempt-1'; attempt.mkdir(parents=True)
        receipts = self.runtime.archive(attempt, self.sources)
        with self.runtime.db() as db:
            row = dict(db.execute('SELECT * FROM jobs WHERE player_id=?', (pid,)).fetchone())
        report = {'player_id': pid, 'cohort_rank': row['rank'], 'available_at': time.time(),
                  'status': 'mismatch', 'semantic_claims_verified': False,
                  'cards': [{'claim': 'Not automatically true', 'excerpt': 'Public Player',
                             'verification_status': 'excerpt_verified' if i == 0 else 'mismatch',
                             'receipt': r, 'text_sha256': sha((attempt / r['local_text']).read_bytes())}
                            for i, r in enumerate(receipts)], 'searches': ['Actual scout query'], 'gaps': []}
        write_json(base / 'input-provenance.json', {'original_packet_sha256': row['packet_sha']})
        path = attempt / 'verified-facts.json'; write_json(path, report)
        write_json(base / 'latest.json', {'report_path': str(path), 'report_sha256': 'bad' if bad_hash else sha(path.read_bytes()),
                   'status': 'mismatch', 'attempt': 1, 'available_at': time.time()})
        return path

    def author(self, bad_math=False):
        return {'player_id': 'p', 'dossier_markdown': '# Public Player\n\nClay forecast and conditional opportunity. ' + 'evidence '*480,
                'sources': self.sources, 'searches': ['Inherited scout search, checked originals'],
                'unresolved': ['Future role'], 'falsification_tests': ['Check route access'],
                'packet_sections_read': ['professional_forecasts', 'provider_references'],
                'component_checks': [{'source_key': 'clay:0', 'observed_core_subtotal': 999 if bad_math else 32,
                                      'source_conditioning': 'Full NFL season observed core subtotal; missing fields unknown.'}]}

    def critic(self, approved=True):
        return {'player_id': 'p', 'approved': approved, 'issues': [] if approved else ['Role unsupported'],
                'checks_performed': ['Read both original sources, exact identity, forecast arithmetic and decision mechanism'],
                'verified_source_urls': [s['url'] for s in self.sources]}

    def state(self, pid='p'):
        with self.runtime.db() as db:
            return dict(db.execute('SELECT * FROM jobs WHERE player_id=?', (pid,)).fetchone())

    def test_claim_waits_for_scout_and_preserves_experiments(self):
        self.assertIsNone(self.engine.claim())
        self.scout('protected'); self.scout('p')
        row = self.engine.claim(); self.assertEqual(row['player_id'], 'p')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(Synthesis(self.root).claim())
        self.assertEqual(self.state('protected')['state'], 'pending')
        with self.assertRaises(ValueError):
            self.engine.claim(first=177)

    def test_wrong_scout_hash_fails_before_model_or_publication(self):
        self.scout(bad_hash=True)
        with patch.object(self.runtime, 'invoke') as invoke:
            result = self.engine.work(self.engine.claim())
        self.assertEqual(result['state'], 'error'); invoke.assert_not_called()
        self.assertFalse((self.root / 'research/player-reviews/p.md').exists())

    def test_literal_mismatch_is_preserved_and_sources_bound(self):
        self.scout(); row = self.engine.claim()
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic()]) as invoke:
            result = self.engine.work(row)
        self.assertEqual(result['state'], 'reviewed')
        directory = self.runtime.base / 'jobs/p/attempt-1'
        report = json.loads((directory / 'evidence/scout-report.json').read_text())
        self.assertEqual(report['report']['cards'][1]['verification_status'], 'mismatch')
        self.assertFalse(report['report']['semantic_claims_verified'])
        binding, _ = validate_binding(directory, json.loads((directory / 'source-receipts.json').read_text()))
        self.assertIn('evidence/scout-report.json', binding['sha256'])
        self.assertIn('source-10000.txt', binding['sha256'])
        self.assertEqual(invoke.call_args_list[0].kwargs['model'], 'claude-fable-5-1')
        self.assertEqual(invoke.call_args_list[1].kwargs['model'], 'claude-opus-5')
        self.assertFalse((self.root / 'research/player-reviews/p.md').exists())
        publication = self.runtime.publish('p')
        self.assertTrue(Path(publication['completion_manifest']).is_file())

    def test_repair_once_reaudits_and_counts_failed_round(self):
        self.scout()
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic(False), self.author(), self.critic()]):
            result = self.engine.work(self.engine.claim())
        self.assertEqual(result['state'], 'reviewed'); self.assertEqual(result['rounds'], 2)
        self.assertEqual(self.state()['attempts'], 2)
        self.assertEqual(self.engine.status()['synthesis_rounds'], {'passed': 1, 'rejected': 1})
        directory = self.runtime.base / 'jobs/p/attempt-2'
        self.assertTrue((directory / 'evidence/repair-feedback.json').exists())
        feedback = json.loads((directory / 'evidence/repair-feedback.json').read_text())
        old_body = directory / feedback['prior_sources'][0]['local_text']
        self.assertEqual(old_body.name, 'source-20000.txt')
        self.assertIn('Public Player original', old_body.read_text())
        self.assertTrue((directory.parent / 'attempt-1/critic.json').exists())
        self.runtime.publish('p')

    def test_math_rejection_survives_two_model_approvals(self):
        self.scout()
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(True), self.critic(), self.author(True), self.critic()]) as invoke:
            result = self.engine.work(self.engine.claim(), publish=True)
        self.assertEqual(result['state'], 'needs_revision'); self.assertEqual(invoke.call_count, 4)
        self.assertEqual(self.engine.status()['synthesis_rounds'], {'rejected': 2})
        self.assertFalse((self.root / 'research/player-reviews/p.md').exists())

    def test_evidence_mutation_during_critic_is_loud(self):
        self.scout()
        def model(directory, name, *args, **kwargs):
            if name == 'author':
                return self.author()
            (directory / 'source-10000.txt').write_text('changed')
            return self.critic()
        with patch.object(self.runtime, 'invoke', side_effect=model):
            result = self.engine.work(self.engine.claim())
        self.assertEqual(result['state'], 'error')
        self.assertIn('Evidence changed', result['error'])

    def test_new_provider_references_are_refreshed_before_read(self):
        self.scout()
        job = self.runtime.base / 'jobs/p'
        write_json(job / 'brief.json', {'stale': True})
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic()]):
            self.engine.work(self.engine.claim())
        brief = json.loads((job / 'attempt-1/brief.json').read_text())
        self.assertIn('provider_references', brief)

    def test_stale_audit_is_retained_without_transferring_approval(self):
        self.scout()
        base = self.runtime.base / 'scouts/audits/p'; base.mkdir(parents=True)
        path = base / 'report.json'; write_json(path, {'cards': [{'status': 'source_supported'}]})
        write_json(base / 'latest.json', {'report_path': str(path), 'report_sha256': sha(path.read_bytes()),
                                       'source_report_sha256': 'old'})
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic()]):
            self.engine.work(self.engine.claim())
        audit = json.loads((self.runtime.base / 'jobs/p/attempt-1/evidence/scout-audit.json').read_text())
        self.assertFalse(audit['source_report_matches'])
        self.assertIn('STALE', audit['interpretation'])

    def test_zero_wait_claims_only_available_bounded_jobs(self):
        self.scout()
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic()]):
            result = self.engine.run(workers=4, limit=8, wait_seconds=0)
        self.assertEqual(result['jobs_claimed'], 1)
        self.assertEqual(self.state('q')['state'], 'pending')

    def test_publication_conflict_preserves_reviewed_state(self):
        self.scout()
        with patch.object(self.runtime, 'invoke', side_effect=[self.author(), self.critic()]), \
             patch.object(self.runtime, 'publish', side_effect=ValueError('Existing manual review')):
            result = self.engine.work(self.engine.claim(), publish=True)
        self.assertEqual(result['state'], 'reviewed')
        self.assertIn('publication_error', result)
        self.assertEqual(self.state()['state'], 'reviewed')


if __name__ == '__main__':
    unittest.main()
