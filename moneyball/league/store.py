"""Immutable versions, content-addressed raw data and conservative as-of queries."""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime


class DataError(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp(value=None):
    if value is None:
        return time.time()
    if isinstance(value, bool):
        raise DataError("Timestamp must be a number or timezone-aware ISO date, not a boolean")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            try:
                d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DataError("Timestamp must be a number or timezone-aware ISO date") from exc
            if d.tzinfo is None:
                raise DataError("An as-of date must include its timezone")
            value = d.timestamp()
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError("Timestamp must be a number or timezone-aware ISO date") from exc
    if not math.isfinite(value):
        raise DataError("Timestamp must be finite")
    return value


class Store:
    def __init__(self, root=None):
        # Reuse an existing old installation without copying or relabeling history.
        old_default = Path("~/.local/share/art-of-the-deal").expanduser()
        default = old_default if (old_default / "evidence.sqlite3").exists() else Path("~/.local/share/moneyball").expanduser()
        self.root = Path(root or os.environ.get("MONEYBALL_HOME") or os.environ.get("ART_OF_DEAL_HOME") or default).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.root / "raw"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "evidence.sqlite3"
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS versions (
              id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
              available_at REAL NOT NULL, hash TEXT NOT NULL, body TEXT NOT NULL,
              provenance TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS versions_lookup ON versions(kind,key,available_at,id);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, created_at REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            """)
        self.path.chmod(0o600)
        self._read_state = threading.local()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def blob(self, raw: bytes):
        sha = hashlib.sha256(raw).hexdigest()
        path = self.blobs / sha
        temporary = self.blobs / f".{sha}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            try:
                # A same-directory hard link publishes complete bytes atomically
                # and fails harmlessly if another writer won the race.
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise DataError("Raw blob integrity failure")
        return sha

    @contextmanager
    def read_view(self, cutoff=None):
        """Pin one thread's reads to a SQLite snapshot and knowability cutoff.

        Writers always use independent connections. Nested views retain the outer
        snapshot and may narrow its cutoff; they cannot widen it. This freezes
        actual visible versions, rather than relying on publication timestamps
        alone to distinguish a transaction committing during decision assembly.
        """
        requested = timestamp(cutoff)
        previous = getattr(self._read_state, "view", None)
        if previous is not None:
            connection, outer_cutoff = previous
            self._read_state.view = (connection, min(requested, outer_cutoff))
            try:
                yield self
            finally:
                self._read_state.view = previous
            return
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            # BEGIN alone is deferred: the first read establishes the snapshot.
            connection.execute("SELECT COALESCE(MAX(id),0) FROM versions").fetchone()
            self._read_state.view = (connection, requested)
            yield self
        finally:
            self._read_state.view = None
            connection.rollback()
            connection.close()

    @contextmanager
    def _read_db(self):
        view = getattr(self._read_state, "view", None)
        if view is not None:
            yield view[0]
        else:
            with self.db() as db:
                yield db

    @property
    def in_read_view(self):
        """Whether this thread has an immutable read snapshot open."""
        return getattr(self._read_state, "view", None) is not None

    def _read_cutoff(self, as_of=None):
        view = getattr(self._read_state, "view", None)
        cutoff = timestamp(as_of) if as_of is not None else (view[1] if view else time.time())
        return min(cutoff, view[1]) if view else cutoff

    def put(self, kind, key, value, provenance=None):
        # Imports become locally knowable at publication, never at an old label.
        return self.put_bundle([(kind, key, value, provenance)])[0]

    def get(self, kind, key, as_of=None, required=True):
        cutoff = self._read_cutoff(as_of)
        with self._read_db() as db:
            row = db.execute("SELECT id,available_at,hash,body,provenance FROM versions WHERE kind=? AND key=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1",
                             (kind, str(key), cutoff)).fetchone()
        if row is None:
            if required:
                raise DataError(f"No {kind} record for {key} at requested cutoff")
            return None
        data = json.loads(row[3])
        if digest(data) != row[2]:
            raise DataError(f"Stored content integrity failure: {kind}/{key}")
        return {"version_id": row[0], "available_at": row[1], "sha256": row[2],
                "data": data, "provenance": json.loads(row[4])}

    def put_bundle(self, entries):
        """Atomically publish related records after acquiring the write lock.

        Serialization precedes lock acquisition; the common observation stamp
        excludes any time spent waiting behind another writer. Exact commit
        instants are not represented by this clock; use read_view for coherent
        concurrent decision reads.
        """
        prepared = []
        for kind, key, value, provenance in entries:
            p = dict(provenance or {})
            encode(p)  # Reject unsupported provenance before any publication.
            prepared.append((kind, str(key), digest(value), encode(value), p))
        receipts = []
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            for kind, key, sha, body, provenance in prepared:
                p = dict(provenance, locally_available_at=now)
                cur = db.execute("INSERT INTO versions(kind,key,available_at,hash,body,provenance) VALUES (?,?,?,?,?,?)",
                                 (kind, key, now, sha, body, encode(p)))
                receipts.append({"version_id": cur.lastrowid, "available_at": now, "sha256": sha})
        return receipts

    @staticmethod
    def _publication_epoch(db):
        versions = db.execute("SELECT COALESCE(MAX(id),0) FROM versions").fetchone()[0]
        index_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_documents'").fetchone()
        if index_exists:
            documents = db.execute("SELECT COALESCE(MAX(id),0) FROM evidence_documents").fetchone()[0]
            return (versions, documents)
        return versions

    def version_epoch(self):
        """Opaque publication guard; includes optional same-database evidence index.

        Legacy stores without the index retain their integer token. Indexed stores
        return (version_id, document_id). Callers should compare tokens for exact
        equality rather than interpret them as a count or timestamp.
        """
        with self._read_db() as db:
            return self._publication_epoch(db)

    def put_checked(self, kind, key, value, epoch, provenance=None):
        """Reject publication racing a host recheck, conservatively across versions."""
        p = dict(provenance or {})
        encode(p)
        sha, body = digest(value), encode(value)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._publication_epoch(db) != epoch:
                raise DataError("Evidence changed during recheck; repeat validation")
            now = time.time()
            p["locally_available_at"] = now
            cur = db.execute("INSERT INTO versions(kind,key,available_at,hash,body,provenance) VALUES (?,?,?,?,?,?)",
                             (kind, str(key), now, sha, body, encode(p)))
        return {"version_id": cur.lastrowid, "available_at": now, "sha256": sha}

    def keys(self, kind, as_of=None):
        with self._read_db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT key FROM versions WHERE kind=? AND available_at<=? ORDER BY key", (kind, self._read_cutoff(as_of)))]

    def event(self, kind, body):
        with self.db() as db:
            db.execute("INSERT INTO events(created_at,kind,body) VALUES (?,?,?)", (time.time(), kind, encode(body)))

    def events(self, limit=20):
        with self.db() as db:
            rows = db.execute("SELECT created_at,kind,body FROM events ORDER BY id DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
        return [{"at": r[0], "kind": r[1], "detail": json.loads(r[2])} for r in rows]
