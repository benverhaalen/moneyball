"""Shared provider/account request spacing and Retry-After pauses.

Sleeper's primary API documentation recommends staying below 1000 calls/minute
(https://docs.sleeper.com/, inspected 2026-10-13). The 0.12s default is our
conservative 500/minute pacing choice, not an asserted provider hard quota.
Other providers have no invented rate quota; configure intervals explicitly.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import math
import sqlite3
import time
from .store import DataError, digest


@dataclass(frozen=True)
class RateScope:
    provider: str
    account_scope: str = "public"

    @property
    def key(self):
        if not all(isinstance(x, str) and x for x in (self.provider, self.account_scope)):
            raise DataError("Rate scope must contain nonempty non-secret identifiers")
        return digest({"provider": self.provider, "account": self.account_scope})


class RatePolicy:
    def __init__(self, store, intervals=None):
        self.store = store
        self.intervals = {"api.sleeper.app": .12}
        self.intervals.update(intervals or {})
        if any(not math.isfinite(v) or v < 0 for v in self.intervals.values()):
            raise DataError("Rate intervals must be finite and nonnegative")
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS rate_policy (scope_key TEXT PRIMARY KEY, "
                       "next_at REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0, "
                       "min_interval REAL NOT NULL DEFAULT 0)")
            if "min_interval" not in {r[1] for r in db.execute("PRAGMA table_info(rate_policy)")}:
                db.execute("ALTER TABLE rate_policy ADD COLUMN min_interval REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.store.path, timeout=.05)
        try:
            with db:
                yield db
        finally:
            db.close()

    def acquire(self, resource, deadline):
        """Acquire permission now; deadline is an absolute monotonic timestamp.

        Does not reserve a future slot: abandoned callers leave no queued debt.
        Rechecks shared pauses after every wait, so a new Retry-After cannot be
        bypassed by a waiter holding an old reservation. SQLite contention can
        exceed a tiny deadline by at most its 50ms busy wait per operation.
        """
        if not math.isfinite(deadline):
            raise DataError("Rate deadline must be finite")
        key = resource.key
        interval = self.intervals.get(resource.provider, 0)
        started = time.monotonic()
        while time.monotonic() < deadline:
            try:
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    now = time.time()
                    row = db.execute("SELECT next_at,blocked_until,min_interval FROM rate_policy WHERE scope_key=?", (key,)).fetchone()
                    allowed_at = max(row[:2]) if row else 0
                    effective_interval = max(interval, row[2] if row else 0)
                    delay = max(0, allowed_at - now)
                    if delay == 0:
                        db.execute("INSERT INTO rate_policy VALUES(?,?,0,?) ON CONFLICT(scope_key) "
                                   "DO UPDATE SET next_at=excluded.next_at,min_interval=excluded.min_interval",
                                   (key, now + effective_interval, effective_interval))
                        return {"scope_key": key, "acquired_at": now,
                                "waited_seconds": time.monotonic() - started,
                                "min_interval_seconds": effective_interval,
                                "policy": "configured_spacing" if effective_interval else "no_configured_quota"}
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                delay = .01
            remaining = deadline - time.monotonic()
            if delay >= remaining:
                break
            time.sleep(min(max(.001, delay), .05, max(0, remaining)))
        raise DataError("Shared provider rate policy exceeds request deadline; retry later")

    @staticmethod
    def retry_after_seconds(value, now=None):
        now = time.time() if now is None else now
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    return None
                seconds = date.timestamp() - now
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0, seconds) if math.isfinite(seconds) else None

    def defer_until(self, resource, retry_after, now=None):
        """Persist a shared pause from Retry-After seconds or an HTTP date.

        Invalid Retry-After yields no mutation; a shorter response cannot erase
        a longer existing pause. Account identifiers must be opaque, not secrets.
        """
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise DataError("Pause observation time must be finite")
        seconds = self.retry_after_seconds(retry_after, now)
        if seconds is None:
            return {"updated": False, "reason": "invalid_retry_after"}
        key = resource.key
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO rate_policy VALUES(?,0,?,?) ON CONFLICT(scope_key) "
                       "DO UPDATE SET blocked_until=MAX(rate_policy.blocked_until,excluded.blocked_until),"
                       "min_interval=MAX(rate_policy.min_interval,excluded.min_interval)",
                       (key, now + seconds, self.intervals.get(resource.provider, 0)))
            blocked = db.execute("SELECT blocked_until FROM rate_policy WHERE scope_key=?", (key,)).fetchone()[0]
        return {"updated": True, "scope_key": key, "blocked_until": blocked}
