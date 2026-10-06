import unittest
from moneyball.market_books import normalize_book


class BookTests(unittest.TestCase):
    def item(self,sid,data):
        return {'source_id':sid,'market_id':'example','url':'https://example.test/example/book',
                'receipt':{'observed_at':100,'id':1,'sha256':'abc'},'data':data}

    def test_global_not_assumed_sorted_and_zero_size_removed(self):
        d={'timestamp':'100000','asset_id':'x',
           'bids':[{'price':'.9','size':'0'},{'price':'.1','size':'200'},{'price':'.6','size':'10'}],
           'asks':[{'price':'.9','size':'200'},{'price':'.7','size':'.01'}]}
        r=normalize_book(self.item('polymarket_global_public',d))
        self.assertEqual(r['top']['yes_bid'],.6);self.assertEqual(r['top']['yes_ask'],.7)
        self.assertGreater(r['size_sensitivity']['10']['yes_ask'],.89)

    def test_kalshi_no_bids_imply_yes_asks(self):
        d={'orderbook_fp':{'yes_dollars':[['.2','10'],['.3','15']],
                           'no_dollars':[['.6','10'],['.1','100']]}}
        r=normalize_book(self.item('kalshi_public',d))
        self.assertAlmostEqual(r['top']['yes_ask'],.4)
        self.assertEqual(r['top']['yes_bid'],.3)

    def test_us_true_book_can_be_wider_than_catalog(self):
        d={'marketData':{'marketSlug':'example','transactTime':'2026-09-08T00:00:00Z',
            'bids':[{'px':{'value':'.19','currency':'USD'},'qty':'249'}],
            'offers':[{'px':{'value':'.57','currency':'USD'},'qty':'.01'},
                      {'px':{'value':'.58','currency':'USD'},'qty':'3.8'}]}}
        r=normalize_book(self.item('polymarket_us_public',d))
        self.assertAlmostEqual(r['top']['spread'],.38)
        self.assertIsNone(r['size_sensitivity']['100']['yes_ask'])
