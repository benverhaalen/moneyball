"""Atomic publication and validate/insert races, synthetic local SQLite only."""
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from moneyball.league import decision
from moneyball.league.demo import fixture, forecasts
from moneyball.league.service import Service
from moneyball.league.store import DataError, Store


class DecisionAtomicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.store = self.service.store
        self.data = fixture()
        self.service._publish('demo', self.data, [], forecasts(self.data)[:-1])

    def comparison(self, historical=False):
        cutoff = time.time()
        context = decision.prepare(self.service, {'question': 'Keep this lineup?',
            'alias': 'demo', 'intent': 'lineup', 'refresh': False,
            **({'as_of': cutoff} if historical else {})})
        comparison = decision.compare(self.service, context['decision_id'], [{'id': 'hold', 'kind': 'hold'}])
        judgment = {'comparison_id': comparison['comparison_id'], 'choice': 'hold',
            'confidence': 'conditional', 'strongest_countercase': 'Lock state unverified',
            'reversal_conditions': ['Corrected availability']}
        return context, comparison, judgment

    def test_bundle_has_one_local_observation_and_atomic_asof_boundary(self):
        receipts = self.store.put_bundle([
            ('example', 'a', {'v': 1}, {'origin': 'synthetic'}),
            ('example', 'b', {'v': 2}, {'origin': 'synthetic'})])
        cutoff = receipts[0]['available_at']
        self.assertEqual(receipts[1]['available_at'], cutoff)
        for key in ('a', 'b'):
            self.assertIsNone(self.store.get('example', key, cutoff - .001, required=False))
            record = self.store.get('example', key, cutoff)
            self.assertEqual(record['available_at'], cutoff)
            self.assertEqual(record['provenance']['locally_available_at'], cutoff)

    def test_bundle_sql_failure_rolls_back_earlier_insert(self):
        epoch = self.store.version_epoch()
        with self.store.db() as db:
            db.execute("CREATE TRIGGER abort_second BEFORE INSERT ON versions WHEN NEW.kind='fail-me' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaisesRegex(Exception, 'synthetic failure'):
            self.store.put_bundle([('example', 'first', {'v': 1}, None),
                                   ('fail-me', 'second', {'v': 2}, None)])
        self.assertIsNone(self.store.get('example', 'first', required=False))
        self.assertIsNone(self.store.get('fail-me', 'second', required=False))
        self.assertEqual(self.store.version_epoch(), epoch)

    def test_bundle_invalid_serialization_publishes_nothing(self):
        epoch = self.store.version_epoch()
        with self.assertRaises(ValueError):
            self.store.put_bundle([('example', 'first', {'v': 1}, None),
                                   ('example', 'bad', {'v': float('nan')}, None)])
        self.assertEqual(self.store.version_epoch(), epoch)
        self.assertIsNone(self.store.get('example', 'first', required=False))

    def test_service_publishes_league_and_native_forecasts_at_same_vintage(self):
        league = self.service.snapshot('demo')
        forecasts_record = self.store.get('forecasts', 'demo:espn')
        self.assertEqual(league['available_at'], forecasts_record['available_at'])
        self.assertEqual(league['provenance']['locally_available_at'], forecasts_record['provenance']['locally_available_at'])

    def test_service_bad_forecast_bundle_retains_prior_roster_and_forecasts(self):
        before = self.service.snapshot('demo')
        before_forecasts = self.store.get('forecasts', 'demo:espn')
        data = copy.deepcopy(self.data)
        data['teams']['a']['name'] = 'Must not publish'
        rows = forecasts(data)[:-1]
        rows[0]['points'] = float('nan')
        with self.assertRaises(ValueError):
            self.service._publish('demo', data, [], rows)
        self.assertEqual(self.service.snapshot('demo')['version_id'], before['version_id'])
        self.assertEqual(self.store.get('forecasts', 'demo:espn')['version_id'], before_forecasts['version_id'])

    def test_checked_insert_rejects_another_process_connection_epoch_change(self):
        rival = Store(self.temp.name)
        epoch = self.store.version_epoch()
        rival.put('example', 'new', {'value': 'concurrent'})
        with self.assertRaisesRegex(DataError, 'changed during recheck'):
            self.store.put_checked('example', 'guarded', {'valid': True}, epoch)
        self.assertIsNone(self.store.get('example', 'guarded', required=False))

    def test_two_checked_writers_with_one_epoch_cannot_both_publish(self):
        epoch = self.store.version_epoch()
        gate = threading.Barrier(2)
        results = []
        lock = threading.Lock()
        def worker(key):
            store = Store(self.temp.name)
            gate.wait(timeout=5)
            try:
                store.put_checked('example', key, {'value': key}, epoch)
                result = 'saved'
            except DataError:
                result = 'rejected'
            with lock:
                results.append(result)
        threads = [threading.Thread(target=worker, args=(key,)) for key in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['saved', 'rejected'])
        self.assertEqual(len(self.store.keys('example')), 1)

    def test_record_catches_race_after_successful_content_recheck(self):
        context, comparison, judgment = self.comparison()
        original = self.store.put_checked
        def insert_after_rival_write(kind, key, value, epoch, provenance=None):
            Store(self.temp.name).put('example', 'racing-version', {'v': 1})
            return original(kind, key, value, epoch, provenance)
        with patch.object(self.store, 'put_checked', side_effect=insert_after_rival_write):
            with self.assertRaisesRegex(DataError, 'changed during recheck'):
                decision.record(self.service, context['decision_id'], judgment)
        self.assertIsNone(self.store.get('decision_receipt', context['decision_id'], required=False))

    def test_historical_receipt_can_be_saved_despite_new_current_roster_and_forecasts(self):
        context, comparison, judgment = self.comparison(historical=True)
        data = copy.deepcopy(self.data)
        data['rules']['bench_slots'] += 1
        data['teams']['a']['name'] = 'New state'
        rows = forecasts(data)[:-1]
        rows[0]['points'] = 1000
        self.service._publish('demo', data, [], rows)
        receipt = decision.record(self.service, context['decision_id'], judgment)
        self.assertEqual(receipt['status'], 'historical_host_judgment')
        self.assertTrue(all(r['available_at'] <= context['evidence_as_of'] for r in receipt['recheck_manifest']))
        self.assertEqual(receipt['consumed_manifest'], comparison['consumed_manifest'])
        self.assertEqual(self.service.snapshot('demo')['data']['teams']['a']['name'], 'New state')

    def test_blocked_writers_do_not_backdate_publication_to_wait_start(self):
        import moneyball.league.store as module
        for method in ('put', 'put_bundle', 'put_checked'):
            with self.subTest(method=method):
                writer = Store(self.temp.name)
                prepared = threading.Event()
                errors = []
                original_encode = module.encode
                payload = {'late_value': method}
                def encoding(value):
                    if value == payload:
                        prepared.set()
                    return original_encode(value)
                epoch = self.store.version_epoch()
                def work():
                    try:
                        if method == 'put_bundle':
                            writer.put_bundle([('blocked', method, payload, None)])
                        elif method == 'put_checked':
                            writer.put_checked('blocked', method, payload, epoch)
                        else:
                            writer.put('blocked', method, payload)
                    except Exception as exc:
                        errors.append(exc)
                with self.store.db() as blocker:
                    blocker.execute('BEGIN IMMEDIATE')
                    with patch.object(module, 'encode', side_effect=encoding):
                        thread = threading.Thread(target=work)
                        thread.start()
                        self.assertTrue(prepared.wait(timeout=5))
                        cutoff = time.time()
                        self.assertIsNone(self.store.get('blocked', method, cutoff, required=False))
                        blocker.commit()
                        thread.join(timeout=10)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertIsNone(self.store.get('blocked', method, cutoff, required=False))
                self.assertGreater(self.store.get('blocked', method)['available_at'], cutoff)

    def test_read_view_pins_actual_snapshot_despite_later_permitted_timestamps(self):
        self.store.put('view', 'existing', {'v': 1})
        rival = Store(self.temp.name)
        with self.store.read_view(time.time() + 3600):
            epoch = self.store.version_epoch()
            rival.put('view', 'existing', {'v': 2})
            rival.put('view', 'new', {'v': 3})
            self.assertEqual(self.store.get('view', 'existing')['data'], {'v': 1})
            self.assertIsNone(self.store.get('view', 'new', required=False))
            self.assertEqual(self.store.keys('view'), ['existing'])
            self.assertEqual(self.store.version_epoch(), epoch)
        self.assertEqual(self.store.get('view', 'existing')['data'], {'v': 2})
        self.assertEqual(self.store.keys('view'), ['existing', 'new'])
        self.assertGreater(self.store.version_epoch(), epoch)

    def test_nested_read_view_narrows_cutoff_and_restores_outer_view(self):
        self.store.put('view', 'existing', {'v': 1})
        with self.store.read_view(time.time() + 3600):
            with self.store.read_view(0):
                self.assertIsNone(self.store.get('view', 'existing', required=False))
                self.assertIsNone(self.store.get('view', 'existing', time.time() + 3600, required=False))
            self.assertEqual(self.store.get('view', 'existing')['data'], {'v': 1})

    def test_read_view_does_not_redirect_writes_or_other_threads(self):
        self.store.put('view', 'existing', {'v': 1})
        result = []
        with self.store.read_view(time.time() + 3600):
            self.store.put('view', 'existing', {'v': 2})
            self.assertEqual(self.store.get('view', 'existing')['data'], {'v': 1})
            thread = threading.Thread(target=lambda: result.append(self.store.get('view', 'existing')['data']))
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result, [{'v': 2}])
        self.assertEqual(self.store.get('view', 'existing')['data'], {'v': 2})

    def test_read_view_exception_releases_snapshot_and_restores_reads(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.store.read_view(time.time() + 3600):
                raise RuntimeError('synthetic')
        self.store.put('view', 'after', {'v': 1})
        self.assertEqual(self.store.get('view', 'after')['data'], {'v': 1})
        with self.store.read_view():
            self.assertEqual(self.store.get('view', 'after')['data'], {'v': 1})

    def test_epoch_fences_optional_index_publication(self):
        from moneyball.league.evidence import EvidenceIndex
        legacy = self.store.version_epoch()
        self.assertIsInstance(legacy, int)
        index = EvidenceIndex(Store(self.temp.name))
        before = self.store.version_epoch()
        self.assertEqual(before, (legacy, 0))
        index.index_document('synthetic-news', {
            'source': {'url': 'https://example.test/news', 'origin': 'synthetic',
                       'capture_policy': 'permitted_excerpt'},
            'sections': [], 'claims': [{'id': 'role', 'player_ref': 'synthetic:aq',
                'facet': 'role', 'text': 'Synthetic limited practice report.'}]})
        after = self.store.version_epoch()
        self.assertGreaterEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])
        with self.assertRaisesRegex(DataError, 'changed during recheck'):
            self.store.put_checked('receipt', 'bad', {'decision': 'hold'}, before)
        self.assertIsNone(self.store.get('receipt', 'bad', required=False))
        receipt = self.store.put_checked('receipt', 'good', {'decision': 'hold'}, after)
        self.assertIsInstance(receipt['version_id'], int)

    def test_epoch_fences_legacy_document_only_publication(self):
        from moneyball.league.evidence import EvidenceIndex
        from moneyball.league.store import digest, encode
        EvidenceIndex(Store(self.temp.name))
        before = self.store.version_epoch()
        # Synthetic legacy publisher: historical implementations had no Store
        # marker. Retain protection when encountering such same-file publishers.
        document = {'source': {'url': 'https://example.test/legacy', 'origin': 'synthetic',
                              'capture_policy': 'link_only'}, 'sections': [], 'claims': []}
        rival = Store(self.temp.name)
        with rival.db() as db:
            db.execute('INSERT INTO evidence_documents(document_key,observed_at,hash,body) VALUES(?,?,?,?)',
                       ('legacy', time.time(), digest(document), encode(document)))
        after = self.store.version_epoch()
        self.assertEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])
        with self.assertRaisesRegex(DataError, 'changed during recheck'):
            self.store.put_checked('receipt', 'legacy-race', {}, before)

    def test_record_rejects_index_commit_after_manifest_validation(self):
        from moneyball.league.evidence import EvidenceIndex
        index = EvidenceIndex(Store(self.temp.name))
        context, comparison, judgment = self.comparison()
        original = self.store.put_checked
        errors = []
        def publish():
            try:
                index.index_document('racing-news', {
                    'source': {'url': 'https://example.test/late-news', 'origin': 'synthetic',
                               'capture_policy': 'permitted_excerpt'},
                    'sections': [], 'claims': [{'id': 'availability', 'player_ref': 'synthetic:aq',
                        'facet': 'availability', 'text': 'Synthetic changed availability.'}]})
            except Exception as exc:
                errors.append(exc)
        def insert_after_index_commit(kind, key, value, epoch, provenance=None):
            thread = threading.Thread(target=publish)
            thread.start()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            return original(kind, key, value, epoch, provenance)
        with patch.object(self.store, 'put_checked', side_effect=insert_after_index_commit):
            with self.assertRaisesRegex(DataError, 'changed during recheck'):
                decision.record(self.service, context['decision_id'], judgment)
        self.assertIsNone(self.store.get('decision_receipt', context['decision_id'], required=False))

    def test_read_view_pins_optional_index_epoch(self):
        from moneyball.league.evidence import EvidenceIndex
        index = EvidenceIndex(Store(self.temp.name))
        with self.store.read_view(time.time() + 3600):
            before = self.store.version_epoch()
            index.index_document('new-index-fact', {
                'source': {'url': 'https://example.test/fact', 'origin': 'synthetic',
                           'capture_policy': 'link_only'}, 'sections': [], 'claims': []})
            self.assertEqual(self.store.version_epoch(), before)
        after = self.store.version_epoch()
        self.assertGreaterEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])

    def test_resume_identical_content_still_requires_explicit_new_comparison(self):
        context, comparison, judgment = self.comparison()
        resumed = decision.resume(self.service, context['decision_id'], refresh=False)
        self.assertFalse(resumed['content_changed'])
        self.assertTrue(resumed['comparisons_require_recheck'])
        with self.assertRaisesRegex(DataError, 'revision'):
            decision.record(self.service, context['decision_id'], judgment)


if __name__ == '__main__':
    unittest.main()
