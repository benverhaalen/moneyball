import gzip
import tempfile
import unittest

from moneyball.sources import HttpFetcher, FetchError, decode_csv, normalize, ingest
from moneyball.warehouse import Warehouse, ValidationError


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.w=Warehouse(self.tmp.name); self.now=100.0; self.sleeps=[]

    def sleep(self,seconds):
        self.sleeps.append(seconds); self.now+=seconds

    def fetcher(self,transport):
        return HttpFetcher(self.w,transport=transport,clock=lambda:self.now,sleep=self.sleep,min_interval=0)

    def test_etag_304_cache_and_offline_resume(self):
        seen=[]
        def transport(url,headers):
            seen.append(headers)
            return (200,{'ETag':'v1','Last-Modified':'yesterday'},b'a,b\n1,2\n') if len(seen)==1 else (304,{},b'')
        f=self.fetcher(transport); r=f.fetch('s','https://example.test/a')
        self.assertFalse(r['cache_hit'])
        self.assertTrue(f.fetch('s','https://example.test/a',offline=True)['cache_hit'])
        self.now+=100
        rr=f.fetch('s','https://example.test/a',force=True)
        self.assertTrue(rr['revalidated']); self.assertEqual(rr['id'],r['id'])
        self.assertEqual(seen[-1]['If-None-Match'],'v1')
        self.assertEqual(self.w.raw_bytes(r['id']),b'a,b\n1,2\n')
        with self.assertRaises(FetchError): f.fetch('s','https://example.test/missing',offline=True)

    def test_429_retry_and_long_retry_deferred(self):
        calls=[]
        def transport(u,h):
            calls.append(1)
            return (429,{'Retry-After':'2'},b'') if len(calls)==1 else (200,{},b'a,b\n1,2')
        self.fetcher(transport).fetch('s','https://example.test/a')
        self.assertEqual(self.sleeps,[2]); self.assertEqual(len(calls),2)
        with self.assertRaises(FetchError):
            self.fetcher(lambda u,h:(429,{'Retry-After':'61'},b'')).fetch('s','https://example.test/b')
        self.assertEqual(self.sleeps,[2])

    def test_corrupt_gzip_retains_raw_no_cache(self):
        f=self.fetcher(lambda u,h:(200,{'Content-Encoding':'gzip'},b'bad'))
        with self.assertRaises(FetchError): f.fetch('s','https://example.test/a')
        self.assertIsNone(self.w.cached_http('https://example.test/a'))
        self.assertEqual(self.w.summary()['raw']['receipts'],1)

    def test_csv_integrity_and_no_fantasy_stat_import(self):
        body=b'player_id,season,week,position,team,receptions,fantasy_points_ppr\n00-1,2025,1,WR,GB,5,99\n'
        rows=normalize('weekly',decode_csv(gzip.compress(body)))
        self.assertEqual(rows[0]['player_id'],'00-1')
        self.assertEqual(rows[0]['stats']['receptions'],5)
        self.assertNotIn('fantasy_points_ppr',rows[0]['stats'])
        self.assertNotIn('active',rows[0])
        with self.assertRaises(ValidationError): decode_csv(b'a,b\n1\n')
        with self.assertRaises(ValidationError): decode_csv(b'a,a\n1,2\n')

    def test_ingest_partial_failure_resume_and_temporal_honesty(self):
        def transport(url,headers):
            if '2025' in url: return 404,{},b''
            return 200,{},b'player_id,season,week,season_type,position,team,receptions\n00-1,2024,1,REG,WR,GB,5\n'
        f=self.fetcher(transport)
        r=ingest(self.w,datasets=['weekly'],seasons=[2024,2025],fetcher=f)
        self.assertFalse(r['ok']); self.assertTrue(r['partial'])
        self.assertEqual(len(self.w.query('weekly',200)),1)
        self.assertEqual(self.w.query('weekly',50),[])
        resume=ingest(self.w,datasets=['weekly'],seasons=[2024],fetcher=f,offline=True)
        self.assertTrue(resume['ok']); self.assertTrue(resume['receipts'][0]['unchanged'])


if __name__=='__main__': unittest.main()
