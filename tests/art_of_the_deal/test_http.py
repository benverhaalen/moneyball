import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from art_of_the_deal.http import Client, ScopedRedirect
from art_of_the_deal.store import DataError, Store


class Response(io.BytesIO):
    status=200
    headers={"Date":"Mon, 14 Sep 2026 05:00:00 GMT","Set-Cookie":"secret-response-cookie"}


class TransportTests(unittest.TestCase):
    def test_cached_read_preserves_vintage_and_does_not_persist_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root); c=Client(s)
            with patch.object(c.opener,"open",return_value=Response(b'{"rows":[1]}')) as request:
                payload,first=c.get("https://example.test/data",headers={"Cookie":"secret-request-cookie"})
                again,last=c.get("https://example.test/data",headers={"Cookie":"changed-cookie"})
                self.assertEqual(request.call_count,1)
            self.assertEqual(payload,again)
            self.assertEqual(first["checked_at"],last["checked_at"])
            self.assertTrue(last["cache_hit"])
            record=s.get("http","public:https://example.test/data")
            self.assertNotIn("cookie",json.dumps(record).lower())
            self.assertEqual((s.blobs/first["raw_sha256"]).read_bytes(),b'{"rows":[1]}')

    def test_credential_redirect_cannot_leave_https_origin_host(self):
        redirect=ScopedRedirect()
        req=urllib.request.Request("https://allowed.test/data",headers={"Cookie":"secret"})
        for dest in ("https://other.test/data","http://allowed.test/data"):
            with self.assertRaises(DataError):
                redirect.redirect_request(req,None,302,"Found",{},dest)

    def test_denied_auth_does_not_log_response_or_replace_good_data(self):
        with tempfile.TemporaryDirectory() as root:
            s=Store(root); c=Client(s,force=True)
            error=urllib.error.HTTPError("https://example.test/data",403,"Forbidden",{},io.BytesIO(b'secret-cookie'))
            with patch.object(c.opener,"open",side_effect=error):
                with self.assertRaisesRegex(DataError,"HTTP 403") as exc:
                    c.get("https://example.test/data",headers={"Cookie":"secret"})
            self.assertNotIn("secret-cookie",str(exc.exception))
            self.assertEqual(s.keys("http"),[])


if __name__=="__main__": unittest.main()
