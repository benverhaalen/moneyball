import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock
from moneyball.league.http import Client
from moneyball.league.store import Store, DataError


class Response:
    status = 200
    headers = {"Date": "Mon, 12 Oct 2026 12:00:00 GMT", "Age": "30"}
    def __init__(self, body=b'{"rows":[1]}'):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.body


class HttpRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.url = 'https://source.test/public?week=2'
    def tearDown(self):
        self.temp.cleanup()

    def test_after_call_transport_bypasses_cache_and_exposes_local_barrier(self):
        old = Client(self.store)
        old.opener = Mock(open=Mock(return_value=Response(b'{"pick":1}')))
        old.get(self.url)
        current = Client(self.store, start_after_request=True)
        current.opener = Mock(open=Mock(return_value=Response(b'{"pick":2}')))
        payload, receipt = current.get(self.url)
        self.assertEqual(payload, {"pick":2})
        self.assertEqual(current.opener.open.call_count, 1)
        self.assertFalse(receipt['cache_hit'])
        self.assertGreaterEqual(receipt['refresh_attempt_at'], receipt['request_started_not_before'])
        self.assertEqual(receipt['headers']['age'], '30')
        self.assertIn('provider caching', receipt['refresh_observation_semantics'])

    def test_cross_client_coalesces_and_preserves_dates(self):
        clients = [Client(self.store, force=True) for _ in range(2)]
        calls = []
        def open_response(*args, **kwargs):
            calls.append(1)
            time.sleep(.08)
            return Response()
        for client in clients:
            client.opener = Mock(open=open_response)
        barrier = threading.Barrier(2)
        results = []
        def run(client):
            barrier.wait()
            results.append(client.get(self.url))
        threads = [threading.Thread(target=run, args=(client,)) for client in clients]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1]['checked_at'], results[1][1]['checked_at'])
        self.assertEqual(results[0][1]['headers']['age'], '30')
        cached = Client(self.store)
        cached.opener = Mock(open=Mock(side_effect=AssertionError('cache miss')))
        payload, receipt = cached.get(self.url)
        self.assertEqual(payload, {'rows':[1]})
        self.assertTrue(receipt['cache_hit'])
        self.assertEqual(receipt['checked_at'], results[0][1]['checked_at'])

    def test_failure_preserves_success_and_no_unfenced_http_write(self):
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        _, good = client.get(self.url)
        forced = Client(self.store, force=True)
        forced.opener = Mock(open=Mock(return_value=Response(b'bad json')))
        with self.assertRaisesRegex(DataError, 'non-JSON'):
            forced.get(self.url)
        _, preserved = client.get(self.url)
        self.assertEqual(preserved['checked_at'], good['checked_at'])
        self.assertEqual(preserved['refresh_attempt_outcome'], 'failed')
        self.assertEqual(self.store.keys('http'), ['public:'+self.url])
        self.assertEqual(len(self.store.keys('refresh_resource')), 0)

    def test_legacy_cache_read_and_new_success_wins(self):
        self.store.put('http', 'public:'+self.url, {'checked_at':time.time(), 'payload':{'legacy':True},
                       'receipt':{'checked_at':time.time(), 'url':self.url}})
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        self.assertEqual(client.get(self.url)[0], {'legacy':True})
        forced = Client(self.store, force=True)
        forced.opener = Mock(open=Mock(return_value=Response()))
        forced.get(self.url)
        self.assertEqual(client.get(self.url)[0], {'rows':[1]})

    def test_scope_and_response_headers_do_not_share(self):
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        client.get(self.url, headers={'X-Filter':'A'}, namespace='account-a')
        client.get(self.url, headers={'X-Filter':'B'}, namespace='account-a')
        client.get(self.url, headers={'X-Filter':'B'}, namespace='account-b')
        self.assertEqual(client.opener.open.call_count, 3)

    def test_private_account_scope_survives_rotating_cookie(self):
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        client.get(self.url, headers={"Cookie":"old-session"}, namespace="account:a")
        client.get(self.url, headers={"Cookie":"new-session"}, namespace="account:a")
        self.assertEqual(client.opener.open.call_count, 1)
        client.get(self.url, headers={"Cookie":"other-session"}, namespace="account:b")
        self.assertEqual(client.opener.open.call_count, 2)

    def test_public_credential_headers_are_partitioned(self):
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        client.get(self.url, headers={"Cookie":"session-A"})
        client.get(self.url, headers={"Cookie":"session-B"})
        client.get(self.url)
        self.assertEqual(client.opener.open.call_count, 3)

    def test_league_only_namespace_is_not_a_trusted_account(self):
        client = Client(self.store)
        client.opener = Mock(open=Mock(return_value=Response()))
        client.get(self.url, headers={"Cookie":"session-A"}, namespace="espn:123")
        client.get(self.url, headers={"Cookie":"session-B"}, namespace="espn:123")
        self.assertEqual(client.opener.open.call_count, 2)

    def test_deadline_limits_opener_and_retry_wait(self):
        client = Client(self.store, deadline=time.monotonic()+.2)
        error = urllib.error.HTTPError(self.url, 429, 'rate', {'Retry-After':'10'}, None)
        client.opener = Mock(open=Mock(side_effect=error))
        start = time.monotonic()
        with self.assertRaisesRegex(DataError, 'deadline'):
            client.get(self.url)
        self.assertLess(time.monotonic()-start, .3)
        self.assertLessEqual(client.opener.open.call_args.kwargs['timeout'], .2)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_uncooperative_opener_cannot_publish_after_deadline(self):
        client = Client(self.store, deadline=time.monotonic()+.03)
        completed = threading.Event()
        def slow(*args, **kwargs):
            time.sleep(.1)
            completed.set()
            return Response()
        client.opener = Mock(open=slow)
        with self.assertRaisesRegex(DataError, 'deadline'):
            client.get(self.url)
        completed.wait(1)
        time.sleep(.03)
        self.assertEqual(self.store.keys('http'), [])


if __name__ == '__main__':
    unittest.main()
