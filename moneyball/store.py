"""Local response cache and change history; SQLite transactions protect readers."""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "data.sqlite3"
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS responses (
                    path TEXT PRIMARY KEY, fetched REAL NOT NULL,
                    hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, alias TEXT NOT NULL, created REAL NOT NULL,
                    hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_alias ON snapshots(alias,id);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, alias TEXT NOT NULL, created REAL NOT NULL,
                    body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY, name TEXT, position TEXT, team TEXT, body TEXT);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cached(self, path):
        with self.connect() as db:
            row = db.execute("SELECT fetched,hash,body FROM responses WHERE path=?", (path,)).fetchone()
        return None if row is None else {"fetched": row[0], "hash": row[1], "data": json.loads(row[2])}

    def cache(self, path, value):
        fetched, sha = time.time(), digest(value)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)", (path, fetched, sha, encode(value)))
            if path == "players/nfl":
                db.execute("DELETE FROM players")
                db.executemany("INSERT INTO players VALUES (?,?,?,?,?)", [
                    (pid, p.get("full_name") or (p.get("first_name", "") + " " + p.get("last_name", "")).strip(),
                     p.get("position"), p.get("team"), encode(p)) for pid, p in value.items()])
        return {"fetched": fetched, "hash": sha, "data": value}

    def resolve_alias(self, alias=None):
        """Select a unique configured/cached league; never choose among several."""
        if alias is not None:
            return alias
        config_path = self.root / "config.json"
        configured = json.loads(config_path.read_text()).get("leagues", {}) if config_path.exists() else {}
        with self.connect() as db:
            cached = {row[0] for row in db.execute("SELECT DISTINCT alias FROM snapshots")}
        aliases = set(configured) | cached
        if len(aliases) != 1:
            raise ValueError("Supply --league/--alias explicitly; available leagues: " + ", ".join(sorted(aliases)))
        return next(iter(aliases))

    def latest(self, alias):
        with self.connect() as db:
            row = db.execute("SELECT id,created,hash,body FROM snapshots WHERE alias=? ORDER BY id DESC LIMIT 1", (alias,)).fetchone()
        return None if row is None else {"id": row[0], "created": row[1], "hash": row[2], "data": json.loads(row[3])}

    def record(self, alias, data, receipt):
        old = self.latest(alias)
        sha = digest(data)
        changed = old is None or old["hash"] != sha
        receipt.update(snapshot_hash=sha, changed=changed)
        with self.connect() as db:
            if changed:
                db.execute("INSERT INTO snapshots(alias,created,hash,body) VALUES (?,?,?,?)", (alias, time.time(), sha, encode(data)))
            db.execute("INSERT INTO runs(alias,created,body) VALUES (?,?,?)", (alias, time.time(), encode(receipt)))
        return receipt

    def last_run(self, alias):
        with self.connect() as db:
            row = db.execute("SELECT body FROM runs WHERE alias=? ORDER BY id DESC LIMIT 1", (alias,)).fetchone()
        return json.loads(row[0]) if row else None

    def player(self, pid):
        with self.connect() as db:
            row = db.execute("SELECT name,position,team FROM players WHERE id=?", (str(pid),)).fetchone()
        return {"id": str(pid), "name": row[0], "position": row[1], "team": row[2]} if row else {"id": str(pid)}
