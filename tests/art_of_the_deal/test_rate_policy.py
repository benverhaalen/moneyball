import multiprocessing
import tempfile
import time
import unittest
from email.utils import formatdate
from moneyball.league.rate_policy import RatePolicy, RateScope
from moneyball.league.store import Store, DataError


def acquire_process(root, barrier, output):
    policy = RatePolicy(Store(root), intervals={"synthetic": .06})
    barrier.wait()
    output.put(policy.acquire(RateScope("synthetic"), time.monotonic()+2))


class RateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.scope = RateScope("synthetic", "account-a")
        self.policy = RatePolicy(self.store, intervals={"synthetic": .06})
    def tearDown(self):
        self.temp.cleanup()

    def test_crossprocess_scope_spacing(self):
        ctx = multiprocessing.get_context("spawn")
        barrier, output = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=acquire_process, args=(self.temp.name, barrier, output)) for _ in range(2)]
        for process in processes: process.start()
        for process in processes:
            process.join(4)
            self.assertEqual(process.exitcode, 0)
        results = sorted([output.get(timeout=1) for _ in processes], key=lambda x:x['acquired_at'])
        self.assertGreaterEqual(results[1]['acquired_at']-results[0]['acquired_at'], .055)

    def test_accounts_independent_without_fabricated_quota(self):
        first = self.policy.acquire(self.scope, time.monotonic()+1)
        second = self.policy.acquire(RateScope("synthetic", "account-b"), time.monotonic()+1)
        self.assertLess(second['acquired_at']-first['acquired_at'], .04)
        result = self.policy.acquire(RateScope("unknown"), time.monotonic()+1)
        self.assertEqual(result['policy'], 'no_configured_quota')
        self.assertEqual(result['min_interval_seconds'], 0)

    def test_deadline_does_not_reserve_future_debt(self):
        self.policy.acquire(self.scope, time.monotonic()+1)
        start = time.monotonic()
        with self.assertRaises(DataError):
            self.policy.acquire(self.scope, time.monotonic()+.01)
        self.assertLess(time.monotonic()-start, .04)
        time.sleep(.07)
        self.policy.acquire(self.scope, time.monotonic()+.03)

    def test_shared_retryafter_pause_and_expiry(self):
        self.policy.defer_until(self.scope, .08)
        other = RatePolicy(Store(self.temp.name))
        with self.assertRaises(DataError):
            other.acquire(self.scope, time.monotonic()+.01)
        time.sleep(.09)
        self.assertEqual(other.acquire(self.scope, time.monotonic()+.1)['min_interval_seconds'], .06)

    def test_weaker_client_cannot_bypass_shared_configured_spacing(self):
        self.policy.acquire(self.scope, time.monotonic()+1)
        other = RatePolicy(Store(self.temp.name))
        with self.assertRaises(DataError):
            other.acquire(self.scope, time.monotonic()+.01)
        time.sleep(.07)
        result = other.acquire(self.scope, time.monotonic()+.1)
        self.assertEqual(result['min_interval_seconds'], .06)

    def test_retryafter_parser_http_date_invalid_and_nonshortening(self):
        now = 1700000000
        date = formatdate(now+10, usegmt=True)
        self.assertEqual(self.policy.retry_after_seconds(date, now), 10)
        for value in ('nan', 'inf', 'nonsense', None):
            self.assertIsNone(self.policy.retry_after_seconds(value, now))
        self.assertEqual(self.policy.retry_after_seconds('-1', now), 0)
        first = self.policy.defer_until(self.scope, 10, now=now)
        second = self.policy.defer_until(self.scope, 1, now=now)
        self.assertEqual(first['blocked_until'], second['blocked_until'])
        self.assertFalse(self.policy.defer_until(self.scope, 'bad')['updated'])

    def test_sleeper_guideline_is_conservative_pacing_not_quota(self):
        result = self.policy.acquire(RateScope('api.sleeper.app'), time.monotonic()+1)
        self.assertEqual(result['min_interval_seconds'], .12)


if __name__ == '__main__':
    unittest.main()
