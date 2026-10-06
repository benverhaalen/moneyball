import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneyball.dossier_swarm import Swarm, sha, write_json, public_url, TextExtractor


class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / '.moneyball'
        packets = self.root / 'draft/player-packets'
        packets.mkdir(parents=True)
        raw = b'{"player_id":"p","name":"Public Player"}'
        (packets / 'p.json').write_bytes(raw)
        write_json(packets / 'index.json', {'information_cutoff': 1,
            'players': [{'player_id': 'p', 'cohort_rank': 177, 'name': 'Public Player',
                         'path': 'p.json', 'sha256': sha(raw)}]})
        reviews = self.root / 'research/player-reviews'
        reviews.mkdir(parents=True)
        (reviews / 'README.md').write_text('Rules')
        self.swarm = Swarm(self.root)

    def tearDown(self): self.tmp.cleanup()

    def test_seed_is_idempotent_but_version_change_is_loud(self):
        self.assertEqual(self.swarm.seed(177, 177)['new_jobs'], 1)
        self.assertEqual(self.swarm.seed(177, 177)['new_jobs'], 0)
        index = self.root / 'draft/player-packets/index.json'
        data = json.loads(index.read_text()); data['players'][0]['sha256'] = 'changed'
        write_json(index, data)
        with self.assertRaisesRegex(ValueError, 'version changed'): self.swarm.seed(177, 177)

    def test_claim_is_exclusive_and_running_is_not_published(self):
        self.swarm.seed(177, 177)
        self.assertEqual(self.swarm.claim()['player_id'], 'p')
        self.assertIsNone(Swarm(self.root).claim())
        with self.assertRaisesRegex(ValueError, 'review has not passed'): self.swarm.publish('p')

    def test_new_rules_require_observed_configuration_without_personal_defaults(self):
        self.swarm.seed(177, 177)
        rules = (self.swarm.base / 'jobs/p/rules.md').read_text()
        self.assertIn('Exact observed league configuration: null', rules)
        self.assertIn('Missing rules remain unresolved', rules)
        self.assertIn('Map individual-player versus team-defense source fields', rules)
        self.assertNotIn('st_td=6', rules)
        self.assertNotIn('Taxi rookie only', rules)
        self.assertNotIn('slot5', rules)

    def test_existing_manual_review_is_never_overwritten(self):
        (self.root / 'research/player-reviews/p.md').write_text('manual')
        with self.assertRaisesRegex(ValueError, 'overwritten'): self.swarm.seed(177, 177)

    def test_recovery_keeps_live_owner_and_retries_dead_owner(self):
        self.swarm.seed(177, 177); self.swarm.claim()
        self.assertEqual(self.swarm.recover()['recovered'], [])
        with patch('moneyball.dossier_swarm.os.kill', side_effect=ProcessLookupError):
            self.assertEqual(self.swarm.recover()['recovered'], ['p'])
        self.assertEqual(self.swarm.claim()['attempts'], 1)

    def test_author_validation_fails_identity_and_missing_evidence(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.swarm.check_author({'player_id': 'other'}, 'p')
        with self.assertRaisesRegex(ValueError, 'Missing research'):
            self.swarm.check_author({'player_id': 'p', 'dossier_markdown': 'word '*500}, 'p')

    def test_retry_does_not_reset_success_or_inflight(self):
        self.swarm.seed(177, 177); self.swarm.claim()
        with self.assertRaises(ValueError): self.swarm.retry('p')
        self.swarm.state('p', 'needs_revision', 'bad fact')
        self.swarm.retry('p')
        self.assertEqual(self.swarm.status()['states'], {'pending': 1})

    def test_source_archive_preserves_bytes_and_cache_avoids_request(self):
        self.swarm.seed(177, 177)
        source = {'url': 'https://example.com/primary', 'primary': True}
        data = b'<html><script>ignored</script><p>Public fact</p></html>'
        cache = self.swarm.base / 'source-cache'; cache.mkdir()
        raw = cache / (sha(data)+'.raw'); raw.write_bytes(data)
        import time
        write_json(cache / (sha(source['url'].encode())+'.json'), {
            'url': source['url'], 'status': 200, 'observed_at': time.time(),
            'raw_path': str(raw), 'raw_sha256': sha(data)})
        directory = self.swarm.base / 'jobs/p'
        with patch('urllib.request.build_opener', side_effect=AssertionError('no network')):
            receipt = self.swarm.archive(directory, [source])[0]
        self.assertEqual(receipt['raw_sha256'], sha(data))
        self.assertEqual((directory/'source-0.txt').read_text(), 'Public fact')

    def test_private_sources_rejected_before_request(self):
        for url in ('http://example.com/', 'https://name:secret@example.com/'):
            with self.assertRaises(ValueError): public_url(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(ValueError, 'Non-public'): public_url('https://example.com/')

    def test_publication_requires_critic_and_unchanged_packet(self):
        self.swarm.seed(177,177); self.swarm.claim(); self.swarm.state('p','reviewed')
        folder = self.swarm.base / 'jobs/p/attempt-1'; folder.mkdir()
        write_json(folder/'draft.json', {'player_id':'p','dossier_markdown':'word '*500,
                  'sources':[{'url':'https://example.com/'}],'searches':['query'],
                  'falsification_tests':['observe change'],'unresolved':[]})
        write_json(folder/'critic.json', {'approved':False})
        with self.assertRaisesRegex(ValueError,'Review gate'): self.swarm.publish('p')
        write_json(folder/'critic.json', {'approved':True})
        (folder/'packet.json').write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'Packet changed'): self.swarm.publish('p')


if __name__ == '__main__': unittest.main()
