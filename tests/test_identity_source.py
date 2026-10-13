import tempfile
import unittest

from moneyball.sources import DATASETS, HttpFetcher, decode_csv, ingest, normalize, registry
from moneyball.warehouse import ValidationError, Warehouse


HEADER = 'mfl_id,sleeper_id,espn_id,gsis_id,pfr_id,age,team,db_season\n'
GOOD = (HEADER + '00010,00133,00014172,00-0000001,SyntPl00,29,XX,2020\n'
        + '00011,00133,00005580,00-0000002,SyntPl01,30,YY,2020\n').encode()


class IdentitySourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.warehouse = Warehouse(self.tmp.name)
        self.now = 100.0

    def ingest_body(self, body, **kwargs):
        fetcher = HttpFetcher(self.warehouse, transport=lambda url, headers: (200, {}, body),
                              clock=lambda: self.now, min_interval=0)
        return ingest(self.warehouse, datasets=['fantasy_ids'], seasons=[2020, 2025],
                      fetcher=fetcher, **kwargs)

    def test_exact_string_ids_and_raw_row_provenance(self):
        result = self.ingest_body(GOOD)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['receipts']), 1)
        receipt = result['receipts'][0]
        self.assertEqual(receipt['source_id'], 'dynastyprocess_ids')
        self.assertEqual(receipt['partition'], 'all')
        rows = self.warehouse.query('fantasy_ids', 100)
        self.assertEqual(rows[0]['mfl_id'], '00010')
        self.assertEqual(rows[0]['sleeper_id'], '00133')
        self.assertEqual(rows[0]['espn_id'], '00014172')
        self.assertEqual(rows[0]['gsis_id'], '00-0000001')
        self.assertEqual(rows[0]['pfr_id'], 'SyntPl00')
        self.assertEqual(rows[0]['source_row_number'], 2)
        self.assertFalse(rows[0]['identity_verified'])
        self.assertEqual(rows[0]['identity_status'], 'recorded_crosswalk_not_identity_proof')
        self.assertFalse({'age', 'team', 'db_season', 'season'} & rows[0].keys())
        provenance = rows[0]['_provenance']
        self.assertEqual(provenance['observed_at'], 100)
        self.assertEqual(provenance['available_at'], 100)
        self.assertTrue(provenance['historical_vintage_unknown'])
        self.assertEqual(self.warehouse.raw_bytes(provenance['raw_id']), GOOD)
        self.assertEqual(self.warehouse.raw_receipt(provenance['raw_id'])['sha256'], receipt['raw_sha256'])
        self.assertEqual(self.warehouse.query('fantasy_ids', 99), [])

    def test_repeated_keys_and_identical_rows_are_never_discarded(self):
        line = '00010,00133,00014172,00-0000001,SyntPl00,29,XX,2020\n'
        body = (HEADER + line + line + '00010,00133,00005580,00-0000002,SyntPl01,30,YY,2020\n').encode()
        result = self.ingest_body(body)
        self.assertTrue(result['ok'])
        rows = self.warehouse.query('fantasy_ids', identity_filters={'sleeper_id': ['00133']})
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['source_row_number'] for r in rows], [2, 3, 4])
        self.assertEqual({r['espn_id'] for r in rows}, {'00014172', '00005580'})

    def test_missing_optional_ids_are_retained_and_invalid_ids_rejected(self):
        rows = normalize('fantasy_ids', decode_csv((HEADER + '1,NA,,NULL,,29,XX,2020\n').encode()))
        self.assertIsNone(rows[0]['sleeper_id'])
        self.assertIsNone(rows[0]['espn_id'])
        self.assertIsNone(rows[0]['gsis_id'])
        legacy = normalize('fantasy_ids', decode_csv((HEADER + '1,1,2,ABC123456,x,29,XX,2020\n').encode()))
        self.assertEqual(legacy[0]['gsis_id'], 'ABC123456')
        self.assertFalse(legacy[0]['identity_verified'])
        for line in ('NA,1,2,00-0000001,x,29,XX,2020',
                     '1,1.0,2,00-0000001,x,29,XX,2020',
                     '1,1,2e4,00-0000001,x,29,XX,2020',
                     '1,1,2,bad,x,29,XX,2020'):
            with self.subTest(line=line), self.assertRaises(ValidationError):
                normalize('fantasy_ids', decode_csv((HEADER + line + '\n').encode()))

    def test_idempotent_refresh_and_bad_refresh_preserve_last_success(self):
        first = self.ingest_body(GOOD)
        rows = self.warehouse.query('fantasy_ids')
        self.now = 200
        repeated = self.ingest_body(GOOD, force=True)
        self.assertTrue(repeated['receipts'][0]['unchanged'])
        self.assertEqual(first['receipts'][0]['batch_id'], repeated['receipts'][0]['batch_id'])
        for body in ((HEADER + '1,1,bad,00-0000001,x,29,XX,2020\n').encode(),
                     HEADER.encode(), b'mfl_id,espn_id\n1,2\n', b'mfl_id,sleeper_id\n1\n'):
            self.now += 100
            with self.subTest(body=body):
                failed = self.ingest_body(body, force=True)
                self.assertFalse(failed['ok'])
                self.assertEqual(self.warehouse.query('fantasy_ids'), rows)
                self.assertEqual(self.warehouse.batches('fantasy_ids')[-1]['status'], 'quarantined')
        current = next(s for s in self.warehouse.source_registry() if s['source_id'] == 'dynastyprocess_ids')
        self.assertEqual(current['acquisition']['current_partitions'][0]['observed_at'], 100)

    def test_explicit_license_and_source_specific_urls(self):
        sources = {s['source_id']: s for s in registry(self.warehouse)}
        source = sources['dynastyprocess_ids']
        self.assertIn('General Public License version 3', source['license_status'])
        self.assertNotIn('CC BY', source['license_status'])
        self.assertEqual(source['license_evidence_url'], 'https://raw.githubusercontent.com/dynastyprocess/data/master/LICENSE')
        self.assertTrue(source['verification_url'].endswith('/nflreadr/main/R/load_ffverse.R'))
        self.assertEqual(source['producer_url'], 'https://github.com/dynastyprocess/data')
        self.assertEqual(source['upstream_primary_key'], 'mfl_id')
        self.assertFalse(DATASETS['fantasy_ids']['yearly'])
        self.assertIn('CC BY 4.0', sources['nflverse_players']['license_status'])
        # The identity dataset is explicitly requested, never added to normal ingestion.
        self.assertNotIn('fantasy_ids', ingest.__kwdefaults__['datasets'])


if __name__ == '__main__':
    unittest.main()
