"""Discovery must not become reports, fresh knowledge or fuzzy identity."""
from copy import deepcopy
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.league.decision_demo import seed
from moneyball.league.news import parse, publish
from moneyball.league.service import Service
from moneyball.league.store import DataError


def response():
    return {'timestamp': '2026-10-13T22:00:00Z', 'feed': [{
        'id': 7, 'playerId': 42, 'headline': 'Invented general league article',
        'type': 'Story', 'published': '2026-10-01T12:00:00Z',
        'lastModified': '2026-10-13T12:00:00Z', 'story': 'PRIVATE ARTICLE BODY',
        'links': {'web': {'href': 'https://example.invalid/article'}}}]}


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(self.tmp.name)
        seed(self.service)
        league = deepcopy(self.service.snapshot('demo')['data'])
        league['platform'] = 'espn'
        league['players']['ar']['external_ids'] = {'espn_id': '42'}
        self.service.store.put('league', 'demo', league)
        self.target_name = league['players']['ar']['name']

    def transport(self, receipt):
        def get(url, **kwargs):
            if '/athletes/' in url:
                return {'athlete': {'id': '42', 'displayName': self.target_name}}, receipt
            return response(), receipt
        return get

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_preserves_dates_but_never_body_period_or_entailment(self):
        row = parse(response(), '42', 3)['leads'][0]
        self.assertNotIn('story', row)
        self.assertIsNone(row['period'])
        self.assertLess(row['published_at'], row['modified_at'])
        self.assertIn('unverified', row['association'])
        bad = response(); bad['feed'][0]['playerId'] = 43
        with self.assertRaises(DataError):
            parse(bad, '42', 3)
        with self.assertRaises(DataError):
            parse({'error': 'schema changed'}, '42', 3)

    def test_failed_refresh_retains_last_success_without_renewing_dates(self):
        receipt = {'checked_at': time.time()-200, 'cache_hit': False}
        with patch('moneyball.league.news.Client.get', side_effect=self.transport(receipt)):
            first = self.service.news_leads('demo', ['ar'])['players'][0]
        with patch('moneyball.league.news.Client.get', side_effect=DataError('Failure')):
            failed = self.service.news_leads('demo', ['ar'], refresh=True)['players'][0]
        self.assertEqual(failed['status'], 'refresh_failed')
        self.assertEqual(failed['version_id'], first['version_id'])
        self.assertEqual(failed['data'], first['data'])

    def test_historical_read_has_no_request_and_excludes_late_ingest(self):
        before = time.time()
        with patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            self.service.news_leads('demo', ['ar'])
        with patch('moneyball.league.news.Client.get') as get:
            old = self.service.news_leads('demo', ['ar'], as_of=before)
            self.assertEqual(old['players'][0]['status'], 'not_acquired')
            get.assert_not_called()
        with self.assertRaises(DataError):
            self.service.news_leads('demo', ['ar'], as_of=before, refresh=True)

    def test_missing_exact_crosswalk_never_joins_name_or_uses_old_mapping(self):
        with patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            self.service.news_leads('demo', ['ar'])
        league = deepcopy(self.service.snapshot('demo')['data'])
        league['platform'] = 'sleeper'
        league['players']['ar']['external_ids'] = {}
        self.service.store.put('league', 'demo', league)
        with patch('moneyball.league.news.Client.get') as get:
            result = self.service.news_leads('demo', ['ar'])
            self.assertEqual(result['players'][0]['status'], 'identity_unresolved')
            self.assertNotIn('data', result['players'][0])
            get.assert_not_called()

    def test_prepare_discovers_but_does_not_clear_reporting_and_consumes_leads(self):
        with patch.object(self.service, 'refresh'), patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            context = self.service.prepare_decision({'alias': 'demo', 'intent': 'research',
                'question': 'Inspect the receiver', 'player_ids': ['ar']})
        self.assertEqual(context['news_discovery']['players'][0]['status'], 'discovered')
        self.assertIn('espn:ar', context['source_readiness']['reporting']['missing_player_refs'])
        self.assertTrue(any(r['kind'] == 'news_leads' for r in context['manifest']))
        with patch('moneyball.league.news.Client.get') as get:
            cached = self.service.prepare_decision({'alias': 'demo', 'intent': 'research',
                'question': 'Inspect the receiver', 'player_ids': ['ar'], 'refresh': False})
            get.assert_not_called()
            self.assertEqual(cached['news_discovery']['players'][0]['status'], 'retained')

    def test_empty_missing_dates_and_bounds_do_not_certify_health(self):
        empty = parse({'feed': []}, '42', 3)
        self.assertFalse(empty['complete_history_established'])
        payload = response(); payload['feed'][0].pop('published')
        self.assertIsNone(parse(payload, '42', 3)['leads'][0]['published_at'])
        with patch('moneyball.league.news.Client.get') as get:
            result = self.service.news_leads('demo', ['unknown'])
            self.assertEqual(result['players'][0]['status'], 'identity_unresolved')
            get.assert_not_called()
        for ids, limit in [([],3), (['ar']*13,3), (['ar'],True)]:
            with patch('moneyball.league.news.Client.get') as get, self.assertRaises(DataError):
                self.service.news_leads('demo', ids, limit=limit)
            get.assert_not_called()

    def test_unowned_candidate_can_use_exact_pool_crosswalk(self):
        pool = deepcopy(self.service.store.get('pool_index', 'demo')['data'])
        pool['players'][0]['external_ids'] = {'espn_id': '42'}
        self.target_name = pool['players'][0]['name']
        pool['league_binding']['platform'] = 'espn'
        self.service.store.put('pool_index', 'demo', pool)
        with patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            result = self.service.news_leads('demo', ['free'])
        self.assertEqual(result['players'][0]['status'], 'discovered')
        self.assertEqual(result['players'][0]['espn_id'], '42')

    def test_other_namespace_pool_cannot_supply_a_candidate_crosswalk(self):
        pool = deepcopy(self.service.store.get('pool_index', 'demo')['data'])
        pool['players'][0]['external_ids'] = {'espn_id': '42'}
        self.service.store.put('pool_index', 'demo', pool)
        with patch('moneyball.league.news.Client.get') as get:
            result = self.service.news_leads('demo', ['free'])
            self.assertEqual(result['players'][0]['status'], 'identity_unresolved')
            get.assert_not_called()

    def test_older_collection_cannot_overwrite_newer_and_identical_is_idempotent(self):
        with patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            first = self.service.news_leads('demo', ['ar'])['players'][0]
        old = deepcopy(first['data'])
        old['last_successful_fetch_at'] -= 100
        old['leads'][0]['headline'] = 'Older result parsed late'
        result = publish(self.service.store, 'espn:ar', old)
        self.assertEqual(result['version_id'], first['version_id'])
        self.assertEqual(result['data'], first['data'])
        same = publish(self.service.store, 'espn:ar', first['data'])
        self.assertEqual(same['version_id'], first['version_id'])

    def test_concurrent_publication_rechecks_instead_of_overwriting(self):
        with patch('moneyball.league.news.Client.get', side_effect=self.transport({'checked_at': time.time()})):
            first = self.service.news_leads('demo', ['ar'])['players'][0]
        candidate = deepcopy(first['data'])
        candidate['last_successful_fetch_at'] += 10
        newer = deepcopy(candidate)
        newer['last_successful_fetch_at'] += 10
        original = self.service.store.put_checked
        def racing(*args, **kwargs):
            self.service.store.put('news_leads', 'espn:ar', newer)
            return original(*args, **kwargs)
        with patch.object(self.service.store, 'put_checked', side_effect=racing):
            result = publish(self.service.store, 'espn:ar', candidate)
        self.assertEqual(result['data'], newer)

    def test_invalid_item_identity_and_link_are_not_trusted(self):
        payload = response(); payload['feed'][0]['id'] = None
        with self.assertRaises(DataError):
            parse(payload, '42', 3)
        payload = response(); payload['feed'][0]['links']['web']['href'] = 'https:///no-host'
        self.assertEqual(parse(payload, '42', 3)['leads'][0]['source_url'],
                         'https://www.espn.com/nfl/player/_/id/42')


if __name__ == '__main__':
    unittest.main()
