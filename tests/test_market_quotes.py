import copy
import unittest
from moneyball.market_quotes import (PlayerIndex, classify, id_aliases, normalize_record,
    normalize_run, payout_semantics, quote_band)


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.index=PlayerIndex([{'player_id':'1','name':'Example Player'}])
        self.receipt={'source_id':'polymarket_us_public','id':1,'sha256':'abc',
                      'url':'https://example.test/public','observed_at':100}
        self.market={'id':1,'title':'Will Example Player have over 10 passing touchdowns?',
            'description':'This resolves Yes if Example Player records over 10 passing touchdowns during the 2026 regular season.',
            'bestBidQuote':{'value':'.64','currency':'USD'},'bestAskQuote':{'value':'.65','currency':'USD'},
            'outcomePrices':['.98','.99'],'metadata':{'playerName':'Example Player'}}
        self.event={'id':2,'slug':'test','title':'2026 season'}

    def test_us_explicit_bbo_not_outcomeprices(self):
        r=normalize_record('polymarket_us_public',self.market,self.receipt,self.index,event=self.event)
        self.assertAlmostEqual(r['quote']['midpoint'],.645)
        self.assertEqual(r['target']['minimum_integer_outcome'],11)
        self.assertTrue(r['threshold_analysis_eligible'])

    def test_rules_control_integer_boundary(self):
        r=classify('10+ passing touchdowns','Will Example Player have 10+ passing touchdowns?',
            'If Example Player records over 10 passing touchdowns in the 2026 regular season, Yes.')
        self.assertEqual(r['minimum_integer_outcome'],11)
        r=classify('','Will Example Player have 10+ passing touchdowns?','2026 regular season')
        self.assertEqual(r['minimum_integer_outcome'],10)

    def test_partial_payout_and_fantasy_never_a_stat_cdf(self):
        self.market['description']+=' In a tie the payout is divided by the number tied.'
        r=normalize_record('polymarket_us_public',self.market,self.receipt,self.index,event=self.event)
        self.assertFalse(r['threshold_analysis_eligible'])
        c=classify('Most fantasy points weeks 15 through17','','Sleeper PPR 2026')
        self.assertEqual(c['kind'],'fantasy_scoring_context')

    def test_boundary_missing_and_crossed_prices_never_become_50_percent(self):
        for a,b in [(0,1),(None,.6),(.8,.2)]:
            q=quote_band(a,b);self.assertFalse(q['two_sided']);self.assertIsNone(q['midpoint'])
        for a in [-.1,1.1,True,float('nan')]:
            with self.assertRaises(ValueError):quote_band(a,.7)

    def test_cutoff_and_wrong_receipt_source_rejected(self):
        with self.assertRaises(ValueError):
            normalize_record('polymarket_us_public',self.market,self.receipt,self.index,event=self.event,as_of=90)
        self.receipt['source_id']='wrong'
        with self.assertRaises(ValueError):
            normalize_record('polymarket_us_public',self.market,self.receipt,self.index,event=self.event)

    def test_future_provider_update_disables_quote(self):
        self.market['updatedAt']='2026-09-08T00:00:00Z'
        r=normalize_record('polymarket_us_public',self.market,self.receipt,self.index,event=self.event)
        self.assertFalse(r['threshold_analysis_eligible'])

    def test_same_name_ambiguous_alias_requires_provenance(self):
        p=PlayerIndex([{'player_id':'1','name':'Same Name'},{'player_id':'2','name':'Same Name'}])
        self.assertEqual(p.match('Same Name'),(None,'ambiguous_name'))
        with self.assertRaises(ValueError):PlayerIndex([{'player_id':'1','name':'X'}],[{'player_id':'1','name':'Y'}])

    def test_duplicate_market_not_independent_evidence(self):
        record={'source_id':'polymarket_us_public','receipt':self.receipt,
                'event':{**self.event,'markets':[self.market,self.market]}}
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            normalize_run({'results':{'us':[record]}},self.index)

    def test_current_name_alias_link_must_be_observed(self):
        p=[{'player_id':'1','name':'Short Name','gsis_id':'00-1'}]
        r={'gsis_id':'00-1','display_name':'Long Name','identity_verified':True,
           '_provenance':{'available_at':30,'observed_at':30}}
        with self.assertRaises(Exception):id_aliases(p,[r],[],as_of=20)
        alias=id_aliases(p,[r],[],as_of=40)
        self.assertEqual(PlayerIndex(p,alias).match('Long Name')[0],'1')
