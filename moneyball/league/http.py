"""Bounded, cached GET transport. Credentials never enter receipts or error bodies."""
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from .store import DataError, digest
from .refresh import Coordinator, ResourceKey
from .rate_policy import RatePolicy, RateScope


class ScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise DataError("Source redirect left its authorized HTTPS host; request stopped")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, store, force=False, deadline=None, start_after_request=False):
        if deadline is not None and not math.isfinite(deadline):
            raise DataError("HTTP deadline must be a finite monotonic timestamp")
        if not isinstance(start_after_request, bool):
            raise DataError("start_after_request must be boolean")
        self.start_after_request = start_after_request
        self.store, self.force, self.deadline = store, force, deadline
        self.coordinator = Coordinator(store)
        self.rate_policy = RatePolicy(store)
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.opener = urllib.request.build_opener(ScopedRedirect())

    def get(self, url, ttl=60, headers=None, namespace="public", daily=False):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise DataError("Only HTTPS source URLs without embedded credentials are allowed")
        key = namespace + ":" + url
        safe_headers = {"Accept": "application/json", "User-Agent": "art-of-the-deal-personal/0.1"}
        safe_headers.update(headers or {})
        # Header identity affects the response (ESPN filters/access); persist only
        # its digest. Caller namespace remains part of the private access scope.
        credential_headers = {k.lower(): v for k, v in safe_headers.items() if k.lower() in ("cookie", "authorization")}
        header_identity = digest({k.lower(): v for k, v in safe_headers.items() if k.lower() not in ("cookie", "authorization")})
        # The account: prefix is an explicit caller assertion of stable logical
        # account identity. League-only namespaces (including ESPN) are not that
        # assertion: credential-bearing requests are partitioned by digest.
        # Neither credential values nor their header names enter stored receipts.
        trusted_account_scope = namespace.startswith("account:")
        credential_partitioned = bool(credential_headers) and not trusted_account_scope
        scope = {"namespace": namespace, "headers": header_identity}
        if credential_partitioned:
            scope["credential_partition"] = digest(credential_headers)
        access_scope = digest(scope)
        resource = ResourceKey(parsed.hostname, "json_get", access_scope=access_scope,
                               parser_version="json-v1", params={"url": url})
        # Read old installations without copying/backdating history. Once this
        # resource has a coordinator row, an older legacy cache cannot supersede it.
        if self.coordinator.state(resource) is None and not credential_partitioned:
            old = self.store.get("http", key, required=False)
            if (old and old["data"]["receipt"].get("cache_scope", access_scope) == access_scope
                    and time.time() - old["data"]["checked_at"] < ttl and (not self.force or daily) and not self.start_after_request):
                return old["data"]["payload"], {**old["data"]["receipt"], "cache_hit": True}
        deadline = self.deadline if self.deadline is not None else time.monotonic() + 95
        remaining = max(0, deadline - time.monotonic())
        errors = []
        def fetch():
            try:
                value = self._fetch(url, safe_headers, deadline)
                value["receipt"]["cache_scope"] = access_scope
                return value
            except Exception as exc:
                errors.append(exc)
                raise
        result = self.coordinator.refresh(resource, fetch, ttl=ttl,
                                          force=self.force and not daily, timeout=remaining,
                                          lease_seconds=max(1, remaining + 1),
                                          publish_kind="http", publish_key=key,
                                          start_after_request=self.start_after_request)
        if result["status"] not in ("success", "cached"):
            if errors and isinstance(errors[0], DataError):
                raise errors[0]
            if result["status"] == "timeout":
                raise DataError("Source refresh exceeded its cumulative deadline; previous evidence remains unchanged")
            raise DataError("Source refresh failed; previous evidence remains dated and unchanged")
        body = result["snapshot"]["data"]
        receipt = {**body["receipt"], "cache_hit": result["status"] == "cached",
                   "refresh_generation": result["snapshot"]["provenance"]["refresh_generation"],
                   "refresh_observation_semantics": result["observation_semantics"],
                   "refresh_attempt_outcome": result["attempt"]["outcome"],
                   "refresh_attempt_at": result["attempt"]["attempt_at"],
                   "refresh_error_type": result["attempt"]["error_type"]}
        if self.start_after_request:
            receipt["request_started_not_before"] = result["request_started_not_before"]
        return body["payload"], receipt

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DataError("Source refresh exceeded its cumulative deadline; previous evidence remains unchanged")
        return remaining

    def _sleep(self, seconds, deadline):
        remaining = self._remaining(deadline)
        if seconds >= remaining:
            raise DataError("Source retry wait exceeds the cumulative deadline; retry on the next refresh")
        time.sleep(max(0, seconds))

    def _fetch(self, url, safe_headers, deadline):
        # Provider-wide public scope intentionally covers requests for different
        # leagues/accounts sharing this local installation and outbound IP.
        # Cache isolation remains independently keyed by access scope above.
        rate_scope = RateScope(urllib.parse.urlparse(url).hostname)
        for attempt in range(3):
            self._remaining(deadline)
            self.rate_policy.acquire(rate_scope, deadline)
            with self.lock:
                wait = max(0, self.next_request - time.monotonic())
                self.next_request = max(self.next_request, time.monotonic()) + 0.12
            if wait:
                self._sleep(wait, deadline)
            try:
                req = urllib.request.Request(url, headers=safe_headers)
                with self.opener.open(req, timeout=min(20, self._remaining(deadline))) as response:
                    raw = response.read(40_000_001)
                    self._remaining(deadline)
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
                self._remaining(deadline)
                # The coordinator alone publishes validated response data after
                # its generation/owner/expiry fence; blobs are immutable evidence.
                return {"checked_at": receipt["checked_at"], "payload": payload, "receipt": receipt}
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise DataError(f"Source requires authorized access or denied the request (HTTP {exc.code}); credentials were not logged") from None
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise DataError(f"Source HTTP {exc.code}") from None
                value = exc.headers.get("Retry-After", "")
                if exc.code == 429:
                    self.rate_policy.defer_until(rate_scope, value or 2 ** attempt)
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(value).timestamp() - time.time()
                    except (ValueError, TypeError):
                        delay = 2 ** attempt
                if not math.isfinite(delay) or delay > 30:
                    raise DataError("Source requested a longer retry delay; retry on the next refresh") from None
                self._sleep(max(0, delay), deadline)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise DataError("Source network request failed; previous evidence remains dated and unchanged") from None
                self._sleep(2 ** attempt, deadline)
