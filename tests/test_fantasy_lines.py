import copy
import unittest

from moneyball.fantasy_lines import normalize_prizepicks


class FantasyLineTests(unittest.TestCase):
    def setUp(self):
        self.receipt={'http_status':200,'source_url':'https://example.org','sha256':'sample','observed_at':100,'available_at':100}
        self.body={'meta':{'total_pages':1},'included':[
            {'type':'new_player','id':'1','attributes':{'name':'One','position':'QB','team':'A'}},
            {'type':'league','id':'9','attributes':{'name':'NFL'}},
            {'type':'duration','id':'11','attributes':{'name':'Full'}},
            {'type':'game','id':'10','attributes':{'start_time':'2026-09-09T20:20:00-04:00'}}],
            'data':[{'id':'20','attributes':{'stat_type':'Fantasy Score','line_score':17.5,
                'start_time':'2026-09-09T20:20:00-04:00','odds_type':'standard'},
                'relationships':{k:{'data':{'type':k,'id':v}} for k,v in
                    [('new_player','1'),('league','9'),('duration','11'),('game','10')]}}]}

    def test_threshold_never_becomes_mean_median_or_probability(self):
        out=normalize_prizepicks(self.body,self.receipt,cutoff=101)['rows'][0]
        self.assertEqual(out['threshold'],17.5)
        for k in ('mean','median','variance','fair_probability_over','confidence_interval','allowed_wager_types'):
            self.assertIsNone(out[k])
        self.assertFalse(out['league_directly_interchangeable'])

    def test_alternate_and_over_only_retained(self):
        second=copy.deepcopy(self.body['data'][0]);second['id']='21'
        second['attributes'].update(odds_type='demon',line_score=25,allowed_wager_types='over')
        self.body['data'].append(second)
        out=normalize_prizepicks(self.body,self.receipt,cutoff=101)
        self.assertEqual(out['unique_source_players'],1)
        self.assertEqual(out['quote_count'],2)
        self.assertEqual(out['rows'][1]['allowed_wager_types'],'over')

    def test_missing_relationship_and_duplicate_fail(self):
        self.body['included'].pop()
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)
        self.setUp();self.body['data'].append(copy.deepcopy(self.body['data'][0]))
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)

    def test_partial_pagination_fails(self):
        self.body['meta']['total_pages']=2
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)

    def test_mismatched_sport_identifier_is_quarantined(self):
        self.body['included'][0]['attributes']['ppid']='NCAAFB_player_one'
        out=normalize_prizepicks(self.body,self.receipt,cutoff=101)['rows'][0]
        self.assertTrue(out['requires_identity_review'])
        self.assertEqual(out['threshold'],17.5)

    def test_asof_and_nonfinite_fail(self):
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=99)
        self.body['data'][0]['attributes']['line_score']=float('nan')
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)

    def test_time_disagreement_is_explicit_and_game_identity_fails(self):
        self.body['data'][0]['attributes']['start_time']='2026-09-10T20:20:00-04:00'
        out=normalize_prizepicks(self.body,self.receipt,cutoff=101)
        self.assertEqual(out['rows'][0]['projection_minus_game_start_seconds'],86400)
        self.assertIsNotNone(out['rows'][0]['timing_warning'])
        self.body['data'][0]['attributes']['game_id']='wrong'
        self.body['included'][-1]['attributes']['external_game_id']='right'
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)
        self.body['data']=[]
        with self.assertRaises(ValueError):normalize_prizepicks(self.body,self.receipt,cutoff=101)
