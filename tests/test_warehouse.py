import tempfile
import unittest
from pathlib import Path

from moneyball.warehouse import Warehouse, WarehouseError, ValidationError, LeakageError, leakage_check


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = Warehouse(self.tmp.name)

    def publish(self, rows, observed=100, **kwargs):
        raw = self.w.put_raw('test', 'https://example.test/data', str(rows).encode(), observed_at=observed)
        return self.w.publish('test', 'weekly', rows, raw_id=raw['id'], key_fields=('id',), **kwargs)

    def test_bounded_namespace_filters_preserve_latest_partition_and_cutoff(self):
        self.publish([{'id':'a','player_id':'101','espn_id':'99'},
                      {'id':'b','player_id':'99','espn_id':101}], observed=100, partition='first')
        self.publish([{'id':'c','gsis_id':'GSIS-A'}, {'id':'d','passer_player_id':'GSIS-A'}], observed=110, partition='second')
        self.assertEqual([r['id'] for r in self.w.query('weekly', 150, identity_filters={'espn_id':['101']})], ['b'])
        self.assertEqual([r['id'] for r in self.w.query('weekly', 150, player_ids=['101'])], ['a'])
        rows = self.w.query('weekly', 150, identity_filters={'gsis_id':['GSIS-A'], 'passer_player_id':['GSIS-A']}, limit=1)
        self.assertEqual([r['id'] for r in rows], ['c'])
        self.assertEqual(rows[0]['_provenance']['available_at'], 110)
        self.assertEqual(len(self.w.query('weekly', 150, limit=3)), 3)
        self.assertEqual(self.w.query('weekly', 150, player_ids=[]), [])
        self.publish([{'id':'e','player_id':'101'}], observed=200, partition='first')
        self.assertEqual([r['id'] for r in self.w.query('weekly', 150, player_ids=['101'])], ['a'])
        self.assertEqual([r['id'] for r in self.w.query('weekly', 250, player_ids=['101'])], ['e'])
        for extra in ({'limit':True}, {'limit':0}, {'limit':1.5}, {'player_ids':'101'},
                      {'identity_filters':{'espn_id':[True]}}, {'identity_filters':{"player_id') OR 1=1":['101']}},
                      {'identity_filters':[]}, {'player_ids':['101'], 'identity_filters':{'player_id':['99']}}):
            with self.assertRaises(ValueError): self.w.query('weekly', 150, **extra)

    def test_revision_asof_and_deleted_rows(self):
        self.publish([{'id': 'a','score': 2},{'id':'b','score':4}], observed=100)
        self.publish([{'id':'a','score':3}], observed=200)
        self.assertEqual([r['score'] for r in self.w.query('weekly',150)], [2,4])
        self.assertEqual([r['score'] for r in self.w.query('weekly',250)], [3])
        self.assertEqual(self.w.query('weekly',99), [])
        self.assertEqual([r['score'] for r in self.w.query('weekly',250,system_asof=150)], [2,4])

    def test_unknown_vintage_cannot_backdate_from_event_or_last_modified(self):
        raw = self.w.put_raw('test','https://example.test/data',b'row',observed_at=200,
                             http_meta={'last_modified':'Thu, 01 Jan 1970 00:00:10 GMT'})
        self.w.publish('test','weekly',[{'id':'a','event':10}],raw_id=raw['id'],key_fields=('id',),
                       published_at=20,event_field='event')
        self.assertEqual(self.w.query('weekly',150), [])
        row = self.w.query('weekly',250,purpose='outcomes')[0]
        self.assertEqual(row['_provenance']['available_at'],200)
        self.assertTrue(leakage_check([row],150))
        with self.assertRaises(LeakageError): leakage_check([row],150,raise_on_error=True)

    def test_verified_archive_requires_hash_binding_and_system_asof(self):
        raw = self.w.put_raw('test','https://example.test/archive',b'row',observed_at=200)
        self.w.publish('test','weekly',[{'id':'a'}],raw_id=raw['id'],key_fields=('id',),published_at=20,
                       publication_evidence={'verified':True,'kind':'immutable_archive','url':'https://example.test/archive','content_sha256':raw['sha256']})
        self.assertEqual(len(self.w.query('weekly',100)),1)
        self.assertEqual(self.w.query('weekly',100,system_asof=100),[])

    def test_invalid_batch_quarantined_raw_retained_no_partial_publish(self):
        self.publish([{'id':'a','score':2}],observed=100)
        with self.assertRaises(ValidationError):
            self.publish([{'id':'a','score':3},{'id':'a','score':4}],observed=200)
        self.assertEqual(self.w.query('weekly',300)[0]['score'],2)
        q = self.w.batches()[-1]
        self.assertEqual(q['status'],'quarantined')
        self.assertTrue(self.w.raw_bytes(q['raw_id']))
        self.assertTrue((Path(self.tmp.name)/'alerts.jsonl').exists())

    def test_idempotent_same_raw_batch(self):
        raw=self.w.put_raw('test','https://example.test',b'a',observed_at=100)
        a=self.w.publish('test','weekly',[{'id':'a'}],raw_id=raw['id'],key_fields=('id',))
        b=self.w.publish('test','weekly',[{'id':'a'}],raw_id=raw['id'],key_fields=('id',))
        self.assertEqual(a['batch_id'],b['batch_id']); self.assertTrue(b['unchanged'])

    def test_nan_range_empty_and_row_drop_rejected(self):
        self.publish([{'id':str(i),'week':1} for i in range(4)],observed=100)
        for rows, extras in [([{'id':'a','x':float('nan')}],{}),([{'id':'a','week':99}],{'ranges':{'week':(1,30)}}),([],{}),([{'id':'a','week':1}],{'max_row_drop_fraction':0.5})]:
            with self.assertRaises(ValidationError): self.publish(rows,observed=200,**extras)
        self.assertEqual(len(self.w.query('weekly',300)),4)

    def test_raw_integrity_failure_loud(self):
        r=self.w.put_raw('test','https://example.test',b'abc',observed_at=100)
        p=self.w.blobs/(r['sha256']+'.gz'); p.chmod(0o600); p.write_bytes(b'corrupt')
        with self.assertRaises(WarehouseError): self.w.raw_bytes(r['id'])

    def test_lineage_rejects_future_feature_inputs_and_keeps_hypothesis_history(self):
        b=self.publish([{'id':'a'}],observed=200)
        with self.assertRaises(LeakageError):
            self.w.record_artifact('bad',{},input_batches=[b['batch_id']],cutoff=100,purpose='features')
        a=self.w.record_artifact('good',{'x':1},input_batches=[b['batch_id']],cutoff=300,purpose='features',code_version='test')
        self.assertEqual(a['input_batches'],[b['batch_id']])
        self.w.preregister('h',{'test':'frozen'})
        self.w.preregister('h',{'reason':'heldout failure'},status='rejected')
        self.assertEqual([h['status'] for h in self.w.hypotheses()],['preregistered','rejected'])

    def test_parser_quarantine_and_clean_integrity(self):
        raw=self.w.put_raw('test','https://example.test',b'bad csv',observed_at=100)
        q=self.w.quarantine('test','weekly',raw_id=raw['id'],reason='bad header')
        self.assertEqual(q['status'],'quarantined')
        self.assertEqual(self.w.query('weekly',200),[])
        b=self.publish([{'id':'a'}],observed=200)
        with self.w.connect() as db:
            db.execute("UPDATE records SET body='{}' WHERE batch_id=?",(b['batch_id'],))
        with self.assertRaises(WarehouseError): self.w.query('weekly',300)

    def test_filtered_reads_and_unchanged_network_observation(self):
        b=self.publish([{'id':'a','position':'QB','season':2025},{'id':'b','position':'K','season':2024}],observed=100)
        repeat=self.publish([{'id':'a','position':'QB','season':2025},{'id':'b','position':'K','season':2024}],observed=200)
        self.assertEqual(b['batch_id'],repeat['batch_id'])
        rows=self.w.query('weekly',300,positions=['QB'],seasons=[2025],columns=['id'])
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['id'],'a'); self.assertNotIn('season',rows[0])

    def test_feature_check_missing_provenance_and_naive_dates(self):
        self.assertTrue(leakage_check([{'a':1}],100))
        with self.assertRaises(ValueError): self.w.query('weekly','2020-01-01')
        self.assertEqual(self.w.query('weekly','2027-01-01T00:00:00Z'),[])


if __name__=='__main__': unittest.main()
