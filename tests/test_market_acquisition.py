import gzip
import tempfile
import unittest
from moneyball.market_acquisition import Collector, decode_json, relevant_kalshi_series


class DiscoveryTests(unittest.TestCase):
    def test_compressed_object_and_bad_root(self):
        self.assertEqual(decode_json(gzip.compress(b'{"events":[]}')),{'events':[]})
        with self.assertRaises(ValueError):decode_json(b'[]')

    def test_kalshi_sport_filter(self):
        rows=[{'ticker':'KXNFLSEASONPASSYDS','category':'Sports'},
              {'ticker':'NFLXMVP','category':'Financials'},
              {'ticker':'KXNBASEASON','category':'Sports'}]
        self.assertEqual(relevant_kalshi_series(rows),rows[:1])

    def test_repeated_global_page_is_incomplete_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            c=Collector(d)
            c.get=lambda *a,**k:({'events':[{'id':1,'active':True,'closed':False}],
                'pagination':{'hasMore':True}}, {'observed_at':10})
            self.assertEqual(len(c.global_events(['one'],max_pages=3)),1)
            self.assertFalse(c.coverage[0]['pagination_exhausted'])
            self.assertTrue(c.failures)

    def test_all_pages_and_closed_event_filter(self):
        with tempfile.TemporaryDirectory() as d:
            c=Collector(d);calls=[]
            def get(s,p,q):
                page=q['page'];calls.append(page)
                return {'events':[{'id':page,'active':True,'closed':page==1}],
                        'pagination':{'hasMore':page<2}}, {'observed_at':10+page}
            c.get=get
            self.assertEqual(len(c.global_events(['one'])),1)
            self.assertEqual(calls,[1,2]);self.assertTrue(c.coverage[0]['pagination_exhausted'])
