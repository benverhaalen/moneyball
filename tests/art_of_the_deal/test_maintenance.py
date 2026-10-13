import tempfile
import threading
import time
import unittest
from moneyball.league.maintenance import Maintenance
from moneyball.league.store import Store, DataError


class FakeService:
    def __init__(self, root):
        self.store = Store(root)
        self.calls = []
        self.fail = False
        self.delay = 0
    def refresh(self, alias, deadline=None):
        self.calls.append((alias, deadline))
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError('private credential must not be saved')
        return {'ok': True, 'alias': alias}
    def refresh_forecasts(self, alias, weeks=None, force=False, deadline=None):
        self.calls.append((alias, weeks, force, deadline))
        return {'status': 'provider_import_required'}


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = FakeService(self.temp.name)
        self.jobs = Maintenance(self.service)
    def tearDown(self):
        self.temp.cleanup()
    def configure(self, name='league'):
        return self.jobs.configure(name, {'kind': 'league', 'alias': 'demo'}, cadence_seconds=300)
    def test_catch_up_then_cadence(self):
        self.configure()
        result = self.jobs.run_due(timeout_seconds=1)
        self.assertEqual(result['jobs'][0]['outcome'], 'success')
        self.assertIsNotNone(result['jobs'][0]['last_success'])
        self.assertEqual(self.jobs.run_due(timeout_seconds=1)['started'], [])
        self.assertEqual(len(self.service.calls), 1)
    def test_failure_does_not_advance_success_or_leak_exception(self):
        self.configure()
        self.jobs.run_due(timeout_seconds=1)
        before = self.jobs.status('league')['jobs'][0]['last_success']
        self.service.fail = True
        self.jobs.run_due(timeout_seconds=1, force=True)
        row = self.jobs.status('league')['jobs'][0]
        self.assertEqual(before, row['last_success'])
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['error_type'], 'RuntimeError')
        self.assertNotIn('private', str(self.jobs.history('league')))
    def test_concurrent_callers_join_one_job(self):
        self.configure()
        self.service.delay = .12
        barrier = threading.Barrier(2)
        results = []
        def run():
            barrier.wait()
            results.append(Maintenance(self.service).run_due(timeout_seconds=1))
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(len(self.jobs.history('league')), 1)
    def test_expired_owner_fenced_and_resumable(self):
        self.configure()
        first, _ = self.jobs._acquire('league', .02)
        time.sleep(.04)
        second, _ = self.jobs._acquire('league', 1)
        self.assertFalse(self.jobs._finish('league', first, {'ok': True}))
        self.assertTrue(self.jobs._finish('league', second, {'ok': True}))
        self.assertEqual([r['outcome'] for r in self.jobs.history('league')], ['success', 'expired'])
    def test_config_changed_while_worker_runs(self):
        self.configure()
        first, _ = self.jobs._acquire('league', 1)
        self.jobs.configure('league', {'kind': 'league', 'alias': 'different'}, cadence_seconds=10)
        self.assertFalse(self.jobs._finish('league', first, {'ok': True}))
        self.assertIsNone(self.jobs.status('league')['jobs'][0]['last_success'])
    def test_forecast_unavailable_is_partial_not_success(self):
        self.jobs.configure('forecast', {'kind': 'forecast', 'alias': 'demo'}, cadence_seconds=300)
        result = self.jobs.run_due(timeout_seconds=1)
        self.assertEqual(result['jobs'][0]['outcome'], 'partial')
        self.assertIsNone(result['jobs'][0]['last_success'])
        self.assertTrue(self.service.calls[0][2])
    def test_deadline_bounded_and_late_completion_not_success(self):
        self.configure()
        self.service.delay = .15
        start = time.monotonic()
        result = self.jobs.run_due(timeout_seconds=.02)
        self.assertLess(time.monotonic() - start, .1)
        self.assertTrue(result['deadline_exhausted'])
        time.sleep(.18)
        self.assertIsNone(self.jobs.status('league')['jobs'][0]['last_success'])
    def test_disabled_and_invalid_specs(self):
        self.jobs.configure('off', {'kind': 'league', 'alias': 'demo'}, cadence_seconds=3, enabled=False)
        self.assertEqual(self.jobs.run_due(timeout_seconds=.1)['started'], [])
        for spec in ({'kind': 'league', 'alias': 'demo', 'cookie': 'secret'}, {'kind': 'forecast', 'alias': 'demo', 'weeks': [True]}, {'kind': 'source', 'datasets': ['fake'], 'seasons': [2026]}):
            with self.assertRaises(DataError):
                self.jobs.configure('bad', spec, cadence_seconds=3)
    def test_same_configuration_preserves_success(self):
        self.configure()
        self.jobs.run_due(timeout_seconds=1)
        old = self.jobs.status('league')['jobs'][0]
        self.configure()
        new = self.jobs.status('league')['jobs'][0]
        self.assertEqual(old['last_success'], new['last_success'])
        self.assertEqual(old['generation'], new['generation'])
    def test_source_uses_existing_shared_sync(self):
        from unittest.mock import patch
        self.jobs.configure('source', {'kind': 'source', 'datasets': ['weekly'], 'seasons': [2026]}, cadence_seconds=300)
        with patch('moneyball.league.pipelines.sync', return_value={'ok': True}) as sync:
            self.jobs.run_due(timeout_seconds=1)
        sync.assert_called_once_with(self.service, ['weekly'], [2026], force=True, offline=False)


if __name__ == '__main__':
    unittest.main()
