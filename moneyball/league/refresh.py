"""Resource refresh coalescing with SQLite leases and fenced immutable publication.

Mechanism inspired by Home Assistant's DataUpdateCoordinator (Apache-2.0,
8c014f3e4ce2d987a94d16db9e0d7c19dda89b00): preserve last successful data
while recording failed attempts independently. This implementation is original;
its cross-process lease/fencing layer is deliberately different. Providers own
HTTP limits/retries and schema validation; callbacks must return validated JSON.
"""
from dataclasses import dataclass, field
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from .store import DataError, digest, encode


@dataclass(frozen=True)
class ResourceKey:
    provider: str
    resource: str
    access_scope: str = "public"
    parser_version: str = "1"
    params: dict = field(default_factory=dict)

    @property
    def key(self):
        # Scope is an opaque non-secret account ID or caller-created digest.
        # Never put tokens, cookies, headers, or credential-bearing URLs here.
        if not all(isinstance(x, str) and x for x in (
                self.provider, self.resource, self.access_scope, self.parser_version)):
            raise DataError("Resource identity fields must be nonempty strings")
        return digest({"provider": self.provider, "resource": self.resource,
                       "scope": self.access_scope, "parser": self.parser_version,
                       "params": self.params})


class Coordinator:
    def __init__(self, store, poll_interval=0.02):
        self.store = store
        self.poll_interval = max(0.001, float(poll_interval))
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS refresh_state (
              resource_key TEXT PRIMARY KEY, generation INTEGER NOT NULL,
              owner TEXT, expires_at REAL, attempt_at REAL, completed_at REAL,
              outcome TEXT, error_type TEXT, success_id INTEGER);
            CREATE TABLE IF NOT EXISTS refresh_attempts (
              resource_key TEXT NOT NULL, generation INTEGER NOT NULL,
              started_at REAL NOT NULL, completed_at REAL, outcome TEXT NOT NULL,
              error_type TEXT, PRIMARY KEY(resource_key,generation));
            """)

    @contextmanager
    def _db(self):
        # A busy writer must not impose Store's 30-second timeout on a draft.
        db = sqlite3.connect(self.store.path, timeout=0.05)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, resource):
        with self._db() as db:
            row = db.execute("SELECT * FROM refresh_state WHERE resource_key=?",
                             (resource.key,)).fetchone()
            return dict(row) if row else None

    def _snapshot(self, key):
        # Same transaction observes pointer and version: failed attempts never
        # relabel last-success dates, and a reader never sees half a publish.
        with self._db() as db:
            row = db.execute("SELECT v.id,v.available_at,v.hash,v.body,v.provenance "
                             "FROM refresh_state s JOIN versions v ON v.id=s.success_id "
                             "WHERE s.resource_key=?", (key,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[3])
        if digest(value) != row[2]:
            raise DataError("Refresh resource integrity failure")
        return {"version_id": row[0], "available_at": row[1], "sha256": row[2],
                "data": value, "provenance": json.loads(row[4])}

    def _result(self, resource, status):
        # A single SQL statement binds last-success and the attempt observed now.
        # They may be different generations after a failure or an active refresh;
        # that distinction is explicit, never a synthetic merged receipt.
        with self._db() as db:
            row = db.execute("SELECT s.*, v.id AS version_id, v.available_at AS version_at, "
                             "v.hash AS version_hash, v.body AS version_body, "
                             "v.provenance AS version_provenance FROM refresh_state s "
                             "LEFT JOIN versions v ON s.success_id=v.id WHERE s.resource_key=?",
                             (resource.key,)).fetchone()
        if row is None:
            return {"status": status, "snapshot": None, "attempt": None,
                    "observation_semantics": "join_current; completion is not an origin observation barrier"}
        attempt = {k: row[k] for k in row.keys() if not k.startswith("version_")}
        snapshot = None
        if row["version_id"] is not None:
            value = json.loads(row["version_body"])
            if digest(value) != row["version_hash"]:
                raise DataError("Refresh resource integrity failure")
            snapshot = {"version_id": row["version_id"], "available_at": row["version_at"],
                        "sha256": row["version_hash"], "data": value,
                        "provenance": json.loads(row["version_provenance"])}
        return {"status": status, "snapshot": snapshot, "attempt": attempt,
                "observation_semantics": "join_current; completion is not an origin observation barrier"}

    def _acquire(self, key, lease_seconds, ttl, force, started_at, start_after_request=False):
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM refresh_state WHERE resource_key=?", (key,)).fetchone()
            if row:
                if row["owner"] and row["expires_at"] > now:
                    return None, "waiting"
                if (row["completed_at"] and row["completed_at"] >= started_at
                        and (not start_after_request or row["attempt_at"] >= started_at)):
                    return None, row["outcome"]  # share this wave, including failure
                if not force and not start_after_request and row["success_id"]:
                    success_at = db.execute("SELECT available_at FROM versions WHERE id=?", (row["success_id"],)).fetchone()[0]
                    if now - success_at < ttl:
                        return None, "cached"
                if row["owner"]:
                    db.execute("UPDATE refresh_attempts SET completed_at=?,outcome='expired' "
                               "WHERE resource_key=? AND generation=? AND outcome='running'",
                               (now, key, row["generation"]))
                generation = row["generation"] + 1
            else:
                generation = 1
            owner = uuid.uuid4().hex
            db.execute("INSERT INTO refresh_state(resource_key,generation,owner,expires_at,attempt_at,outcome) "
                       "VALUES(?,?,?,?,?,'running') ON CONFLICT(resource_key) DO UPDATE SET "
                       "generation=excluded.generation,owner=excluded.owner,expires_at=excluded.expires_at,"
                       "attempt_at=excluded.attempt_at,completed_at=NULL,outcome='running',error_type=NULL",
                       (key, generation, owner, now + lease_seconds, now))
            db.execute("INSERT INTO refresh_attempts VALUES(?,?,?,NULL,'running',NULL)", (key, generation, now))
            return (generation, owner), "owned"

    def _finish(self, key, token, value=None, error=None, publish_kind="refresh_resource", publish_key=None):
        now = time.time()
        # Validate serialization before taking the writer lock.
        body = encode(value) if error is None else None
        sha = digest(value) if error is None else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM refresh_state WHERE resource_key=?", (key,)).fetchone()
            if (not row or (row["generation"], row["owner"]) != token
                    or row["expires_at"] <= now):
                return False
            outcome = "failed" if error is not None else "success"
            error_type = type(error).__name__ if error is not None else None
            ident = row["success_id"]
            if error is None:
                provenance = encode({"locally_available_at": now, "refresh_generation": token[0]})
                ident = db.execute("INSERT INTO versions(kind,key,available_at,hash,body,provenance) "
                                   "VALUES(?,?,?,?,?,?)",
                                   (publish_kind, key if publish_key is None else publish_key, now, sha, body, provenance)).lastrowid
            db.execute("UPDATE refresh_state SET owner=NULL,expires_at=NULL,completed_at=?,"
                       "outcome=?,error_type=?,success_id=? WHERE resource_key=?",
                       (now, outcome, error_type, ident, key))
            db.execute("UPDATE refresh_attempts SET completed_at=?,outcome=?,error_type=? "
                       "WHERE resource_key=? AND generation=?", (now, outcome, error_type, key, token[0]))
            return True

    def refresh(self, resource, fetch, *, ttl=60, force=False, timeout=10, lease_seconds=30,
                publish_kind="refresh_resource", publish_key=None, start_after_request=False):
        """Return {status, snapshot, attempt}; status describes this request.

        Callbacks run in a daemon thread; the caller's wait is bounded. Timeout
        does not cancel provider I/O. Expired/replaced workers cannot publish.
        An explicit force joins in-flight work but bypasses age cache. It does NOT
        guarantee that origin observation began after this request. Exceptions
        retain last success and expose only their class, never exception bodies.
        With start_after_request=True, older in-flight work is allowed to finish
        but cannot satisfy this caller: a new local fetch attempt is required.
        This is a local request-start barrier, not a provider freshness guarantee.
        One callback must validate/publish one atomic resource; compose independent
        resources at the service layer and expose their individual coverage.
        """
        if any(not math.isfinite(x) or x < 0 for x in (ttl, timeout, lease_seconds)) or lease_seconds == 0:
            raise DataError("Refresh durations must be finite and nonnegative; lease must be positive")
        if not isinstance(start_after_request, bool):
            raise DataError("start_after_request must be boolean")
        key = resource.key
        started_at = time.time()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                token, status = self._acquire(key, lease_seconds, ttl, force, started_at, start_after_request)
                if token:
                    def work(acquired=token):
                        try:
                            value = fetch()
                            self._finish(key, acquired, value=value, publish_kind=publish_kind, publish_key=publish_key)
                        except Exception as exc:
                            try:
                                self._finish(key, acquired, error=exc)
                            except sqlite3.Error:
                                pass  # lease recovery owns retry; no unfenced write
                    threading.Thread(target=work, daemon=True).start()
                elif status != "waiting":
                    result = self._result(resource, status)
                    if start_after_request:
                        result["observation_semantics"] = "local_request_started_after_call; provider caching may retain older origin data"
                        result["request_started_not_before"] = started_at
                    return result
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            # The launching caller shares its own completion by started_at.
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        try:
            return self._result(resource, "timeout")
        except sqlite3.OperationalError:
            return {"status": "timeout", "snapshot": None, "attempt": None}
