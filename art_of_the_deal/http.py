"""Bounded, cached GET transport. Credentials never enter receipts or error bodies."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from .store import DataError


class ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise DataError("Source redirect left its authorized HTTPS host; request stopped")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, store, force=False):
        self.store, self.force = store, force
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.opener = urllib.request.build_opener(ScopedRedirect())

    def get(self, url, ttl=60, headers=None, namespace="public", daily=False):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise DataError("Only HTTPS source URLs without embedded credentials are allowed")
        key = namespace + ":" + url
        old = self.store.get("http", key, required=False)
        if old and time.time() - old["data"]["checked_at"] < ttl and (not self.force or daily):
            return old["data"]["payload"], {**old["data"]["receipt"], "cache_hit": True}
        safe_headers = {"Accept": "application/json", "User-Agent": "art-of-the-deal-personal/0.1"}
        safe_headers.update(headers or {})
        for attempt in range(3):
            with self.lock:
                wait = max(0, self.next_request - time.monotonic())
                self.next_request = max(self.next_request, time.monotonic()) + 0.12
            if wait:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers=safe_headers)
                with self.opener.open(req, timeout=20) as response:
                    raw = response.read(40_000_001)
                    if len(raw) > 40_000_000:
                        raise DataError("Source response exceeds size limit")
                    sha = self.store.blob(raw)
                    receipt = {"url": url, "checked_at": time.time(), "raw_sha256": sha,
                               "http_status": response.status, "cache_hit": False,
                               "headers": {k.lower(): response.headers[k] for k in ("Age", "Date", "Cache-Control", "ETag", "Last-Modified") if k in response.headers},
                               "freshness_limit": "A completed GET is not proof of origin freshness; inspect cache/source dates."}
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise DataError("Source returned non-JSON; raw response retained privately") from None
                self.store.put("http", key, {"checked_at": receipt["checked_at"], "payload": payload, "receipt": receipt})
                return payload, receipt
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise DataError(f"Source requires authorized access or denied the request (HTTP {exc.code}); credentials were not logged") from None
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise DataError(f"Source HTTP {exc.code}") from None
                value = exc.headers.get("Retry-After", "")
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(value).timestamp() - time.time()
                    except (ValueError, TypeError):
                        delay = 2 ** attempt
                if delay > 30:
                    raise DataError("Source requested a longer retry delay; retry on the next refresh") from None
                time.sleep(max(0, delay))
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise DataError("Source network request failed; previous evidence remains dated and unchanged") from None
                time.sleep(2 ** attempt)
