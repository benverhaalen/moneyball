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
        self.root = Path(root or os.environ.get("ART_OF_DEAL_HOME", "~/.local/share/art-of-the-deal")).expanduser().resolve()
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

    def put(self, kind, key, value, provenance=None):
        # Importing an old forecast does not make it knowable before this import.
        # Original dates remain in provenance for research; arbitrary backdating is forbidden.
        now = time.time()
        provenance = dict(provenance or {})
        provenance["locally_available_at"] = now
        sha = digest(value)
        with self.db() as db:
            cur = db.execute("INSERT INTO versions(kind,key,available_at,hash,body,provenance) VALUES (?,?,?,?,?,?)",
                             (kind, str(key), now, sha, encode(value), encode(provenance)))
            ident = cur.lastrowid
        return {"version_id": ident, "available_at": now, "sha256": sha}

    def get(self, kind, key, as_of=None, required=True):
        cutoff = timestamp(as_of)
        with self.db() as db:
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

    def keys(self, kind, as_of=None):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT key FROM versions WHERE kind=? AND available_at<=? ORDER BY key", (kind, timestamp(as_of)))]

    def event(self, kind, body):
        with self.db() as db:
            db.execute("INSERT INTO events(created_at,kind,body) VALUES (?,?,?)", (time.time(), kind, encode(body)))

    def events(self, limit=20):
        with self.db() as db:
            rows = db.execute("SELECT created_at,kind,body FROM events ORDER BY id DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
        return [{"at": r[0], "kind": r[1], "detail": json.loads(r[2])} for r in rows]
