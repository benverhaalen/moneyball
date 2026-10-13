import multiprocessing
import tempfile
import threading
import time
import unittest
from moneyball.league.refresh import Coordinator, ResourceKey
from moneyball.league.store import Store, DataError


def process_refresh(root, barrier, calls, results):
    coordinator = Coordinator(Store(root))
    barrier.wait()
    def fetch():
        with calls.get_lock():
            calls.value += 1
        time.sleep(.15)
        return {"rows": [1, 2]}
    results.put(coordinator.refresh(ResourceKey("test", "shared"), fetch, timeout=2))


def abandoned_worker(root, ready):
    coordinator = Coordinator(Store(root))
    coordinator._acquire(ResourceKey("synthetic", "rosters").key, .1, 0, True, time.time())
    ready.put(True)
    time.sleep(30)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.coordinator = Coordinator(self.store)
        self.key = ResourceKey("synthetic", "rosters")

    def tearDown(self):
        self.temp.cleanup()

    def test_after_call_barrier_does_not_accept_older_inflight_result(self):
        parked, release = threading.Event(), threading.Event()
        old_result = []
        def older_fetch():
            parked.set()
            self.assertTrue(release.wait(2))
            return {"board": "before-pick"}
        old = threading.Thread(target=lambda: old_result.append(
            self.coordinator.refresh(self.key, older_fetch, force=True, timeout=2)))
        old.start()
        self.assertTrue(parked.wait(1))
        time.sleep(.01)
        newer_result, fetches = [], []
        def newer_fetch():
            fetches.append(time.time())
            return {"board": "after-pick"}
        newer = threading.Thread(target=lambda: newer_result.append(
            Coordinator(self.store).refresh(self.key, newer_fetch,
                start_after_request=True, timeout=2)))
        newer.start()
        time.sleep(.03)
        self.assertEqual(fetches, [])
        release.set()
        old.join(3); newer.join(3)
        self.assertFalse(old.is_alive()); self.assertFalse(newer.is_alive())
        result = newer_result[0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["snapshot"]["data"], {"board": "after-pick"})
        self.assertEqual(len(fetches), 1)
        self.assertGreaterEqual(result["attempt"]["attempt_at"], result["request_started_not_before"])
        self.assertIn("provider caching", result["observation_semantics"])

    def test_after_call_barrier_timeout_never_certifies_old_result(self):
        good = self.coordinator.refresh(self.key, lambda: {"board": "old"})
        parked, finished = threading.Event(), threading.Event()
        original_finish = self.coordinator._finish
        def complete(*args, **kwargs):
            try:
                return original_finish(*args, **kwargs)
            finally:
                finished.set()
        self.coordinator._finish = complete
        def fetch():
            parked.wait(1)
            return {"board": "new"}
        result = self.coordinator.refresh(self.key, fetch,
            start_after_request=True, timeout=.02, lease_seconds=.04)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["snapshot"], good["snapshot"])
        self.assertNotIn("request_started_not_before", result)
        parked.set()
        self.assertTrue(finished.wait(2))

    def test_cross_process_coalescing(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        calls = context.Value("i", 0)
        results = context.Queue()
        workers = [context.Process(target=process_refresh, args=(self.temp.name, barrier, calls, results)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        snapshots = [results.get(timeout=1) for _ in workers]
        self.assertEqual(calls.value, 1)
        self.assertEqual(snapshots[0]["snapshot"]["version_id"], snapshots[1]["snapshot"]["version_id"])

    def test_failed_refresh_preserves_last_success_and_history(self):
        good = self.coordinator.refresh(self.key, lambda: {"score": 7})
        def fail():
            raise DataError("SECRET_TOKEN must not be stored")
        bad = self.coordinator.refresh(self.key, fail, force=True)
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(bad["snapshot"], good["snapshot"])
        self.assertEqual(bad["attempt"]["error_type"], "DataError")
        with self.store.db() as db:
            rows = db.execute("SELECT outcome,error_type FROM refresh_attempts ORDER BY generation").fetchall()
        self.assertEqual(rows, [("success", None), ("failed", "DataError")])
        self.assertNotIn(b"SECRET_TOKEN", self.store.path.read_bytes())

    def test_expired_lease_recovery_fences_late_writer(self):
        old, _ = self.coordinator._acquire(self.key.key, .02, 0, True, time.time())
        time.sleep(.03)
        other = Coordinator(Store(self.temp.name))
        new, status = other._acquire(self.key.key, 1, 0, True, time.time())
        self.assertEqual(status, "owned")
        self.assertTrue(other._finish(self.key.key, new, value={"winner": "new"}))
        self.assertFalse(self.coordinator._finish(self.key.key, old, value={"winner": "old"}))
        self.assertEqual(other._snapshot(self.key.key)["data"], {"winner": "new"})
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT outcome FROM refresh_attempts WHERE generation=1").fetchone()[0], "expired")

    def test_killed_process_lease_is_recoverable(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        worker = context.Process(target=abandoned_worker, args=(self.temp.name, ready))
        worker.start()
        self.assertTrue(ready.get(timeout=3))
        worker.terminate()
        worker.join(3)
        time.sleep(.12)
        result = self.coordinator.refresh(self.key, lambda: {"recovered": True}, timeout=1)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["snapshot"]["data"], {"recovered": True})
        self.assertEqual(result["attempt"]["generation"], 2)

    def test_expiry_alone_prevents_publish(self):
        token, _ = self.coordinator._acquire(self.key.key, .01, 0, True, time.time())
        time.sleep(.02)
        self.assertFalse(self.coordinator._finish(self.key.key, token, value=["late"]))
        self.assertIsNone(self.coordinator._snapshot(self.key.key))

    def test_locked_database_returns_bounded_timeout(self):
        import sqlite3
        blocker = sqlite3.connect(self.store.path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            start = time.monotonic()
            result = self.coordinator.refresh(self.key, lambda: self.fail("must not run"), timeout=.02)
            self.assertEqual(result["status"], "timeout")
            self.assertLess(time.monotonic() - start, .15)
        finally:
            blocker.rollback()
            blocker.close()

    def test_timeout_and_late_completion(self):
        ready = threading.Event()
        def slow():
            ready.wait(1)
            return {"done": True}
        start = time.monotonic()
        result = self.coordinator.refresh(self.key, slow, timeout=.04, lease_seconds=1)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, .2)
        ready.set()
        result = self.coordinator.refresh(self.key, lambda: self.fail("must join"), timeout=1)
        self.assertEqual(result["snapshot"]["data"], {"done": True})

    def test_keys_include_scope_parser_and_parameters(self):
        keys = [self.key.key, ResourceKey("synthetic", "rosters", access_scope="account2").key,
                ResourceKey("synthetic", "rosters", parser_version="2").key,
                ResourceKey("synthetic", "rosters", params={"week": 2}).key]
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(ResourceKey("x", "y", params={"a": 1, "b": 2}).key,
                         ResourceKey("x", "y", params={"b": 2, "a": 1}).key)

    def test_force_joins_running_and_cache_does_not_fetch(self):
        good = self.coordinator.refresh(self.key, lambda: [1])
        cached = self.coordinator.refresh(self.key, lambda: self.fail("unexpected fetch"))
        self.assertEqual(cached["status"], "cached")
        self.assertEqual(cached["snapshot"], good["snapshot"])
        barrier = threading.Barrier(2)
        calls = []
        results = []
        def run():
            barrier.wait()
            def fetch():
                calls.append(1)
                time.sleep(.1)
                return [2]
            results.append(Coordinator(Store(self.temp.name)).refresh(self.key, fetch, force=True, timeout=1))
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual([x["snapshot"]["data"] for x in results], [[2], [2]])

    def test_combined_receipt_keeps_success_and_attempt_generations_distinct(self):
        from unittest.mock import patch
        good = self.coordinator.refresh(self.key, lambda: [1])
        token, _ = self.coordinator._acquire(self.key.key, 1, 0, True, time.time())
        with patch.object(self.coordinator, "_snapshot", side_effect=AssertionError("split read")), \
             patch.object(self.coordinator, "state", side_effect=AssertionError("split read")):
            result = self.coordinator._result(self.key, "timeout")
        self.assertEqual(result["snapshot"], good["snapshot"])
        self.assertEqual(result["snapshot"]["provenance"]["refresh_generation"], 1)
        self.assertEqual(result["attempt"]["generation"], 2)
        self.assertEqual(result["attempt"]["outcome"], "running")
        self.assertIn("not an origin observation barrier", result["observation_semantics"])
        self.coordinator._finish(self.key.key, token, value=[2])

    def test_invalid_payload_fails_without_publication(self):
        result = self.coordinator.refresh(self.key, lambda: {"bad": float("nan")})
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["snapshot"])

    def test_independent_partial_resources_and_asof(self):
        first = self.coordinator.refresh(self.key, lambda: {"old": True})
        cutoff = first["snapshot"]["available_at"]
        self.coordinator.refresh(self.key, lambda: {"old": False}, force=True)
        result = self.coordinator.refresh(ResourceKey("synthetic", "news"), lambda: 1 / 0)
        self.assertEqual(result["status"], "failed")
        historical = self.store.get("refresh_resource", self.key.key, as_of=cutoff)
        self.assertEqual(historical["data"], {"old": True})
        self.assertEqual(self.coordinator._snapshot(self.key.key)["data"], {"old": False})


if __name__ == "__main__":
    unittest.main()
