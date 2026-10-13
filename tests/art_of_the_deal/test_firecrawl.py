"""Synthetic gateway fixtures; no licensed provider tables or live credentials."""
import copy
import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal import firecrawl
from art_of_the_deal.lineups import packet
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError
from test_service import league, proposal


class FirecrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.data, profile = league(position_limits={})
        for player in self.data['players'].values():
            player['team'] = 'TST'
        self.service.store.put('league', 'x', self.data)
        self.service.store.put('strategy', profile['rules_fingerprint'], profile)
        self.options = dict(season=2026, week=1, position='QB', scoring='PPR')
        self.mapping = {'passing_yards': 'pass_yd', 'rushing_yards': 'rush_yd'}

    def payload(self, options=None):
        options = options or self.options
        return {**options, 'source_url': 'https://www.fftoday.com/rankings/playerwkproj.php',
                'observed_at_ms': time.time() * 1000,
                'players': [{'player_id': p, 'name': player['name'], 'team': 'TST',
                    'projected_points': 999, 'stats': {'passing_yards': 100, 'rushing_yards': (80 if p=='br' else 40)}}
                    for p, player in self.data['players'].items() if options['position'] in player['positions']]}

    def replies(self, options=None, price=5, per_record=False, payload=None):
        receipt = {'checked_at': time.time(), 'request_id': 'synthetic'}
        contract = {'provider': 'fftoday-com', 'capability': firecrawl.WEEKLY,
                    'creditsCost': price, 'perRecord': per_record,
                    'options': [{'name': k, 'required': True} for k in self.options]}
        return [({'tools': [contract]}, receipt),
                ({'alexandria': [{'data': payload or self.payload(options)}]}, receipt)]

    def sync(self, options=None, **kwargs):
        return firecrawl.sync(self.service, 'x', 'fftoday-com', firecrawl.WEEKLY,
                              options or self.options, 5, component_map=self.mapping, **kwargs)

    def test_feed_reaches_real_lineup_and_trade_paths_and_preserves_week_position_cohorts(self):
        for pos in ['QB', 'RB']:
            options = {**self.options, 'position': pos}
            with patch.object(firecrawl.Client, 'post', side_effect=self.replies(options)):
                result = self.sync(options)
            self.assertEqual(result['qualified_rows'], 2)
        self.assertEqual(len(self.service.forecast_rows('x')), 4)
        lineup = packet(self.service, 'x', fresh=False)
        comparison = lineup['forecast_comparisons'][0]
        self.assertEqual(comparison['source'], 'fftoday')
        self.assertEqual(comparison['status'], 'complete_counterfactual')
        self.assertTrue(comparison['freshness_limits'])
        trade = self.service.trade_packet('x', proposal(), fresh=False)
        comparison = trade['mechanics']['comparisons'][0]
        self.assertEqual(comparison['teams']['a']['delta_provider_baseline_points'], 4)
        self.assertEqual(comparison['forecast_statistics'], ['provider_point_estimate'])
        options = {**self.options, 'week': 2}
        with patch.object(firecrawl.Client, 'post', side_effect=self.replies(options)):
            self.sync(options)
        self.assertEqual(len(self.service.forecast_rows('x')), 6)
        self.assertEqual(len(trade['mechanics']['comparisons']), 1)

    def test_credit_unknown_and_over_budget_never_execute(self):
        for price, per_record in [(6, False), (None, False), (5, True), (True, False)]:
            with self.subTest(price=price, per_record=per_record):
                with patch.object(firecrawl.Client, 'post', side_effect=self.replies(price=price, per_record=per_record)) as post:
                    with self.assertRaises(DataError): self.sync()
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(post.call_args.args[0], 'search')
                    self.assertEqual(post.call_args.args[1]['sources'], ['alexandria'])
                    self.assertEqual(post.call_args.args[1]['toolDetail'], 'full')

    def test_bound_waiver_candidate_is_rescored_in_actual_pickup_packet(self):
        from art_of_the_deal.operations import pickup_packet
        candidate = {'player_id':'fa3','name':'Available RB','positions':['RB'],'team':'TST',
                     'acquisition_status':'WAIVERS','execution_actionable':None}
        now = time.time()
        self.service.store.put('pool','x',{'players':[candidate], 'observed_at':now,
            'source':{'url':'https://example.test/pool'}, 'expires_at':now+600,
            'horizon':{'kind':'week','season':2026,'week':1}, 'waiver_state':{},
            'league_binding':{k:self.data[k] for k in ('platform','league_id','season')}})
        for pos in ('QB','RB'):
            options = {**self.options,'position':pos}
            data = self.payload(options)
            if pos == 'RB':
                data['players'].append({'player_id':'provider-fa3','name':'Available RB','team':'TST',
                    'stats':{'passing_yards':100,'rushing_yards':100}})
            with patch.object(firecrawl.Client,'post',side_effect=self.replies(options,payload=data)):
                self.sync(options)
        pickup = pickup_packet(self.service,'x','fa3','ar',evaluation_week=1,fresh=False)
        comparison = pickup['comparison']['comparisons'][0]
        self.assertEqual(comparison['source'],'fftoday')
        self.assertEqual(comparison['delta_points'],6)
        self.assertTrue(comparison['freshness_limits'])
        self.assertEqual(pickup['decision_support']['retention_review']['status'],'required_before_drop_recommendation')

    def test_failed_wrong_week_or_tool_error_preserves_old_batch_and_date(self):
        with patch.object(firecrawl.Client, 'post', side_effect=self.replies()): self.sync()
        key = self.service.store.keys('forecasts')[0]
        original = self.service.store.get('forecasts', key)
        for payload in [self.payload({**self.options, 'week': 2}), None]:
            replies = self.replies(payload=payload)
            if payload is None: replies[1] = ({'alexandria': [{'error': 'THIRD_PARTY_DATA_TERMS_REQUIRED'}]}, replies[1][1])
            with patch.object(firecrawl.Client, 'post', side_effect=replies):
                with self.assertRaises(DataError): self.sync()
            self.assertEqual(self.service.store.get('forecasts', key), original)

    def test_missing_components_and_identity_stay_evidence_then_qualify_without_second_charge(self):
        data = self.payload()
        for player in data['players']: player['team'] = 'DIFFERENT'
        with patch.object(firecrawl.Client, 'post', side_effect=self.replies(payload=data)):
            result = self.sync()
        self.assertEqual(result['status'], 'evidence_only')
        self.assertFalse(self.service.forecast_rows('x'))
        with patch.object(firecrawl.Client, 'post', side_effect=self.replies()):
            result = firecrawl.sync(self.service,'x','fftoday-com',firecrawl.WEEKLY,self.options,5)
        self.assertEqual(result['qualification_gaps'][0]['reason'], 'scoring_components_incomplete')
        with patch.object(firecrawl.Client, 'post') as post:
            qualified = firecrawl.qualify_saved(self.service,'x',result['feed_key'],component_map=self.mapping)
            post.assert_not_called()
        self.assertEqual(qualified['qualified_rows'],2)
        rows = self.service.forecast_rows('x')
        self.assertNotIn('scoring_hash', rows[0])
        self.assertNotIn('points', rows[0])
        self.assertEqual(rows[0]['available_at'], result['receipt']['checked_at'])

    def test_stale_observation_and_expired_rows_cannot_influence_decisions(self):
        data = self.payload(); data['observed_at_ms'] = (time.time()-90000)*1000
        with patch.object(firecrawl.Client,'post',side_effect=self.replies(payload=data)):
            result = self.sync()
        self.assertEqual(result['qualified_rows'],0)
        rows, _ = firecrawl.qualify(self.payload(),self.options,self.data,self.data['players'],component_map=self.mapping)
        rows[0]['valid_until'] = time.time()-1
        self.service.store.put('forecasts','x:synthetic',rows)
        result = packet(self.service,'x',fresh=False)
        self.assertTrue(any(r['code']=='forecast_expired' for r in result['forecast_rejections']))
        rows[0]['valid_until'] = time.time()+100
        rows[0]['forecast_statistic'] = 'median'
        self.service.store.put('forecasts','x:synthetic',rows)
        result = packet(self.service,'x',fresh=False)
        self.assertTrue(any(r['code']=='forecast_statistic_not_expected_points' for r in result['forecast_rejections']))

    def test_duplicate_and_nonlinear_do_not_become_forecasts(self):
        data = self.payload(); data['players'].append(copy.deepcopy(data['players'][0]))
        with self.assertRaises(DataError):
            firecrawl.qualify(data,self.options,self.data,self.data['players'],component_map=self.mapping)
        self.data['rules']['scoring']['nonlinear'] = ['bonus']
        rows, gaps = firecrawl.qualify(self.payload(),self.options,self.data,self.data['players'],component_map=self.mapping)
        self.assertFalse(rows); self.assertEqual(len(gaps),2)

    def test_feed_hidden_after_alias_rebound_and_historical_access_preserved(self):
        with patch.object(firecrawl.Client,'post',side_effect=self.replies()): result=self.sync()
        cutoff=time.time()
        changed=copy.deepcopy(self.data); changed['league_id']='other'
        self.service.store.put('league','x',changed)
        self.assertFalse(self.service.forecast_rows('x'))
        self.assertFalse(firecrawl.query(self.service,'x')['feeds'])
        self.assertEqual(len(firecrawl.query(self.service,'x',as_of=cutoff)['feeds']),1)
        with self.assertRaises(DataError): firecrawl.qualify_saved(self.service,'x',result['feed_key'],component_map=self.mapping)

    def test_transport_no_key_secret_receipt_or_automatic_retry(self):
        client=firecrawl.Client(self.service.store)
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(DataError): client.post('scrape',{})
        class Response(io.BytesIO):
            pass
        with patch.dict(os.environ,{'FIRECRAWL_API_KEY':'synthetic-secret'}):
            with patch.object(client.opener,'open',return_value=Response(b'{"success":true,"data":{}}')) as opened:
                _,receipt=client.post('search',{'query':'test'})
                request=opened.call_args.args[0]
                self.assertEqual(request.get_header('Authorization'),'Bearer synthetic-secret')
                self.assertNotIn('synthetic-secret',json.dumps(receipt))
            with patch.object(client.opener,'open',side_effect=TimeoutError()) as opened:
                with self.assertRaises(DataError): client.post('scrape',{})
                self.assertEqual(opened.call_count,1)
        with self.assertRaises(DataError):
            firecrawl.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test')


if __name__=='__main__': unittest.main()
