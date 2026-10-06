"""Synthetic public-response fixtures; no real or mock draft is modified or queried."""
import copy
import io
import hashlib
from unittest.mock import patch
import json
from pathlib import Path
import tempfile
import unittest

from moneyball.draft_capture import CaptureError, capture, replay
from moneyball.sleeper import Client, DataError
from moneyball.store import Store, digest


class DraftCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.did = '12345'
        self.info = {'draft_id': self.did, 'league_id': None, 'status': 'drafting',
                     'settings': {'teams': 12, 'rounds': 28}, 'draft_order': {'alice': 5},
                     'slot_to_roster_id': {'5': 7}, 'type': 'snake'}
        self.picks, self.trades = [], []
        self.fail = None
        self.calls = []

    def client(self):
        def transport(path):
            self.calls.append(path)
            if path == self.fail:
                raise DataError('fixture rate limit/network failure')
            if path.endswith('/picks'):
                return copy.deepcopy(self.picks)
            if path.endswith('/traded_picks'):
                return copy.deepcopy(self.trades)
            return copy.deepcopy(self.info)
        return Client(Store(self.root / 'draft/runs' / self.did / 'cache'),
                      force=True, transport=transport)

    def run_capture(self, kind='mock', **kwargs):
        return capture(self.root, self.did, kind=kind, client=self.client(), **kwargs)

    def pick(self, number, player='111'):
        return {'draft_id': self.did, 'pick_no': number, 'player_id': player,
                'round': 1, 'draft_slot': 5, 'roster_id': 7, 'picked_by': 'alice',
                'metadata': {'position': 'QB', 'first_name': 'Test'}}

    def test_initial_state_preserves_settings_ownership_and_original_hash(self):
        receipt = self.run_capture()
        self.assertTrue(receipt['changed'])
        state = replay(self.root, self.did, kind='mock')
        self.assertEqual(state['data']['info'], self.info)
        self.assertEqual(state['sources']['info']['sha256'], digest(self.info))
        self.assertFalse(state['consistency']['atomic_snapshot'])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(receipt['kind'], 'mock')

    def test_unchanged_snapshots_are_memoized_but_freshness_is_recorded(self):
        first = self.run_capture()
        second = self.run_capture()
        run = self.root / 'draft/runs' / self.did
        self.assertFalse(second['changed'])
        self.assertEqual(first['snapshot_hash'], second['snapshot_hash'])
        self.assertEqual(len(list((run / 'snapshots').glob('*.json'))), 1)
        self.assertEqual(len((run / 'events.jsonl').read_text().splitlines()), 1)
        self.assertEqual(len((run / 'checks.jsonl').read_text().splitlines()), 2)
        self.assertGreaterEqual(second['checked_at'], first['checked_at'])

    def test_add_correct_undo_and_asof_replay(self):
        initial = self.run_capture()
        self.picks = [self.pick(1)]
        one = self.run_capture()
        self.assertEqual(one['changes']['added_picks'][0]['player_id'], '111')
        self.picks = [self.pick(1, '222')]
        fix = self.run_capture(accept_observed_regression=True)
        self.assertEqual(fix['changes']['corrected_picks'][0]['before']['player_id'], '111')
        self.picks = []
        undo = self.run_capture(accept_observed_regression=True)
        self.assertEqual(undo['snapshot_hash'], initial['snapshot_hash'])
        self.assertEqual(undo['sequence'], 4)
        historical = replay(self.root, self.did, kind='mock', as_of=one['checked_at'])
        self.assertEqual(historical['data']['picks'][0]['player_id'], '111')
        self.assertEqual(replay(self.root, self.did, kind='mock')['data']['picks'], [])

    def test_partial_fetch_archives_successes_without_replacing_previous(self):
        before = self.run_capture()
        self.picks = [self.pick(1)]
        self.fail = 'draft/' + self.did + '/traded_picks'
        with self.assertRaisesRegex(CaptureError, 'previous published snapshot preserved'):
            self.run_capture()
        state = replay(self.root, self.did, kind='mock')
        self.assertEqual(state['snapshot_hash'], before['snapshot_hash'])
        run = self.root / 'draft/runs' / self.did
        self.assertTrue((run / 'raw' / (digest(self.picks) + '.json')).exists())
        checks = [json.loads(x) for x in (run / 'checks.jsonl').read_text().splitlines()]
        self.assertFalse(checks[-1]['published'])
        self.fail = None
        self.assertEqual(self.run_capture()['pick_count'], 1)

    def test_ownership_only_change_is_not_lost(self):
        self.run_capture()
        self.trades = [{'draft_id': self.did, 'round': 2, 'roster_id': 7, 'previous_owner_id': 7, 'owner_id': 9}]
        result = self.run_capture()
        self.assertTrue(result['changed'])
        self.assertTrue(result['changes']['traded_ownership_changed'])
        self.assertEqual(replay(self.root, self.did, kind='mock')['data']['traded_picks'], self.trades)

    def test_mock_live_separation_and_identity_failure(self):
        self.run_capture()
        with self.assertRaisesRegex(CaptureError, 'kind/identity'):
            self.run_capture(kind='live')
        self.info['league_id'] = '999'
        with self.assertRaisesRegex(CaptureError, 'Declared mock has a league_id'):
            self.run_capture()
        self.info['league_id'] = None
        self.info['draft_id'] = '999'
        with self.assertRaisesRegex(CaptureError, 'identity mismatch'):
            self.run_capture()

    def test_corruption_is_loud_and_no_future_asof_observation(self):
        result = self.run_capture()
        with self.assertRaisesRegex(CaptureError, 'No complete observation'):
            replay(self.root, self.did, kind='mock', as_of=result['checked_at'] - 1)
        raw = self.root / 'draft/runs' / self.did / 'raw' / (digest(self.info) + '.json')
        raw.write_text('{}')
        with self.assertRaisesRegex(CaptureError, 'raw source'):
            replay(self.root, self.did, kind='mock')

    def test_duplicate_picks_fail_and_gaps_are_visible_not_silently_filled(self):
        self.run_capture()
        self.picks = [self.pick(1), self.pick(1, '222')]
        with self.assertRaisesRegex(CaptureError, 'unique positive'):
            self.run_capture()
        self.picks = [self.pick(2)]
        result = self.run_capture()
        self.assertTrue(result['consistency']['warnings'])
        self.assertEqual(result['pick_count'], 1)

    def test_offline_reuses_isolated_cache_without_network(self):
        first = self.run_capture()
        again = capture(self.root, self.did, kind='mock', offline=True)
        self.assertEqual(again['snapshot_hash'], first['snapshot_hash'])
        self.assertEqual(again['network_requests'], 0)

    def test_resume_recovers_committed_event_with_stale_latest_pointer(self):
        self.run_capture()
        run = self.root / 'draft/runs' / self.did
        previous_pointer = (run / 'latest.json').read_bytes()
        self.picks = [self.pick(1)]
        committed = self.run_capture()
        (run / 'latest.json').write_bytes(previous_pointer)
        resumed = self.run_capture()
        self.assertFalse(resumed['changed'])
        self.assertEqual(resumed['sequence'], 2)
        self.assertEqual(resumed['snapshot_hash'], committed['snapshot_hash'])
        self.assertEqual(replay(self.root, self.did, kind='mock')['sequence'], 2)

    def test_replay_rejects_tampered_observation_time(self):
        self.run_capture()
        path = self.root / 'draft/runs' / self.did / 'events.jsonl'
        row = json.loads(path.read_text())
        row['observed_at'] = 0
        path.write_text(json.dumps(row) + '\n')
        with self.assertRaisesRegex(CaptureError, 'event hash'):
            replay(self.root, self.did, kind='mock')

    def test_fresh_fetch_never_proves_board_freshness_and_prefix_cannot_regress(self):
        self.picks = [self.pick(1)]
        first = self.run_capture()
        self.assertFalse(first['consistency']['actionable_freshness_verified'])
        self.picks = []
        with self.assertRaisesRegex(CaptureError, 'prefix regression'):
            self.run_capture()
        self.assertEqual(replay(self.root, self.did, kind='mock')['data']['picks'][0]['player_id'], '111')

    def test_http_cache_age_is_retained_and_warned(self):
        client = self.client()
        original = client.get
        def get(path, ttl):
            data = original(path, ttl)
            client.evidence[path]['http'] = {'headers': {'age': '108', 'cf-cache-status': 'UPDATING', 'cache-control': 'public,s-maxage=30,stale-while-revalidate=300'}}
            return data
        client.get = get
        result = capture(self.root, self.did, kind='mock', client=client)
        self.assertEqual(result['sources']['picks']['http']['headers']['age'], '108')
        self.assertTrue(any('intermediary cache' in x for x in result['consistency']['warnings']))
        self.assertFalse(result['consistency']['actionable_freshness_verified'])

    def test_client_keeps_wire_hash_cache_headers_and_scoped_probe_url(self):
        raw = b'[ {"pick_no": 1, "player_id": "111"} ]'
        class Response(io.BytesIO):
            status = 200
            url = 'https://api.sleeper.app/v1/draft/12345/picks?draft_room_observed_at=fixture'
            headers = {'Age': '280', 'CF-Cache-Status': 'UPDATING', 'Set-Cookie': 'must-not-log'}
        client = Client(Store(self.root / 'testcache'), force=True, draft_picks_cache_probe=True)
        with patch('moneyball.sleeper.urllib.request.urlopen', return_value=Response(raw)) as fetch:
            value = client.get('draft/12345/picks', 0)
        source = client.evidence['draft/12345/picks']
        self.assertEqual(source['http']['wire_body_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(source['sha256'], source['http']['wire_body_sha256'])
        self.assertEqual(source['sha256'], digest(value))
        self.assertNotIn('set-cookie', source['http']['headers'])
        self.assertEqual(source['http']['headers']['age'], '280')
        self.assertIn('?draft_room_observed_at=', fetch.call_args.args[0].full_url)


if __name__ == '__main__':
    unittest.main()
