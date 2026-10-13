"""One-shot durable maintenance using the same acquisition paths as questions.

Original narrow adaptation of DBOS's owner-token checkpoint fence (MIT,
61426cb95bcd749dd18137b4eb483d02b9975d7d). No workflow framework is bundled.
A receipt is acquisition status, never certification that advice has all inputs.
"""
from contextlib import contextmanager
import inspect
import json
import math
import sqlite3
import threading
import time
import uuid

from .store import DataError, digest, encode, timestamp


class Maintenance:
    """Durable opt-in jobs; run_due is shared by scheduled and on-demand callers.

    Dead workers are reclaimed after their lease. Caller timeout does not cancel
    provider I/O. Fencing protects this ledger; existing provider publication is
    governed by that provider's own coordinator/atomic ingest. No exactly-once
    external execution claim is made. Config contains identities, never secrets.
    """
    def __init__(self, service):
        self.service = service
        self.store = service.store
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS maintenance_jobs (
              id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_hash TEXT NOT NULL,
              cadence REAL NOT NULL, enabled INTEGER NOT NULL, generation INTEGER NOT NULL,
              owner TEXT, expires_at REAL, last_attempt REAL, last_success REAL,
              next_due REAL NOT NULL, outcome TEXT, error_type TEXT, receipt TEXT);
            CREATE TABLE IF NOT EXISTS maintenance_runs (
              job_id TEXT NOT NULL, generation INTEGER NOT NULL, spec_hash TEXT NOT NULL,
              started_at REAL NOT NULL, completed_at REAL, outcome TEXT NOT NULL,
              error_type TEXT, receipt TEXT, PRIMARY KEY(job_id,generation));
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.store.path, timeout=.05)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def configure(self, job_id, spec, *, cadence_seconds, enabled=True):
        if not isinstance(job_id, str) or not job_id or len(job_id) > 100:
            raise DataError("Supply a nonempty job ID of at most 100 characters")
        if isinstance(cadence_seconds, bool) or not isinstance(cadence_seconds, (int, float)) or not math.isfinite(cadence_seconds) or cadence_seconds <= 0:
            raise DataError("Cadence must be positive finite seconds")
        if not isinstance(enabled, bool):
            raise DataError("enabled must be boolean")
        spec = self._validate_spec(spec)
        body, sha = encode(spec), digest(spec)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM maintenance_jobs WHERE id=?", (job_id,)).fetchone()
            now = time.time()
            if old and old['spec_hash'] == sha:
                db.execute("UPDATE maintenance_jobs SET cadence=?,enabled=? WHERE id=?", (cadence_seconds, int(enabled), job_id))
            else:
                if old and old['owner']:
                    db.execute("UPDATE maintenance_runs SET outcome='superseded',completed_at=? WHERE job_id=? AND generation=? AND outcome='running'", (now, job_id, old['generation']))
                generation = old['generation'] + 1 if old else 0
                db.execute("INSERT INTO maintenance_jobs(id,spec,spec_hash,cadence,enabled,generation,next_due,outcome) VALUES(?,?,?,?,?,?,?,'configured') ON CONFLICT(id) DO UPDATE SET spec=excluded.spec,spec_hash=excluded.spec_hash,cadence=excluded.cadence,enabled=excluded.enabled,generation=excluded.generation,owner=NULL,expires_at=NULL,last_attempt=NULL,last_success=NULL,next_due=excluded.next_due,outcome='configured',error_type=NULL,receipt=NULL", (job_id, body, sha, cadence_seconds, int(enabled), generation, now))
        return self.status(job_id)

    @staticmethod
    def _validate_spec(spec):
        if not isinstance(spec, dict):
            raise DataError("Job spec must be an object")
        kind = spec.get('kind')
        allowed = {'league': {'kind', 'alias'}, 'forecast': {'kind', 'alias', 'weeks'}, 'source': {'kind', 'datasets', 'seasons', 'offline'}}
        if kind not in allowed or set(spec) - allowed[kind]:
            raise DataError("Job kind is league, forecast or source; unknown fields are forbidden")
        if kind in ('league', 'forecast'):
            if not isinstance(spec.get('alias'), str) or not spec['alias'] or len(spec['alias']) > 100:
                raise DataError("League and forecast jobs require an explicit alias")
            if 'weeks' in spec and (not isinstance(spec['weeks'], list) or not spec['weeks'] or len(spec['weeks']) > 18 or any(isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 18 for w in spec['weeks'])):
                raise DataError("Forecast weeks must be explicit values in 1..18")
        else:
            from .pipelines import _seasons
            from moneyball.sources import DATASETS
            from moneyball.advanced import SPECS
            _seasons(spec.get('seasons'))
            ds = spec.get('datasets')
            if not isinstance(ds, list) or not ds or len(ds) > 8 or any(not isinstance(d, str) or d not in {*DATASETS, *SPECS} for d in ds):
                raise DataError("Source jobs require one to eight registered datasets")
            if 'offline' in spec and not isinstance(spec['offline'], bool):
                raise DataError("offline must be boolean")
        return json.loads(encode(spec))

    def status(self, job_id=None):
        with self._db() as db:
            rows = db.execute("SELECT * FROM maintenance_jobs" + (" WHERE id=?" if job_id else "") + " ORDER BY next_due,id", (job_id,) if job_id else ()).fetchall()
        now = time.time()
        jobs = []
        for row in rows:
            r = dict(row)
            r['spec'] = json.loads(r['spec'])
            r['receipt'] = json.loads(r['receipt']) if r['receipt'] else None
            r['due'] = bool(r['enabled'] and (r['next_due'] <= now or (r['owner'] and r['expires_at'] <= now)))
            r['running'] = bool(r['owner'] and r['expires_at'] > now)
            r['recoverable_expired'] = bool(r['owner'] and r['expires_at'] <= now)
            r.pop('owner')
            jobs.append(r)
        if job_id and not jobs:
            raise DataError("Unknown maintenance job")
        return {'jobs': jobs, 'checked_at': now, 'scope': 'Local acquisition jobs; freshness and forecast qualification remain source-specific'}

    def _acquire(self, job_id, lease_seconds, force=False):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            r = db.execute('SELECT * FROM maintenance_jobs WHERE id=?', (job_id,)).fetchone()
            now = time.time()
            if not r or not r['enabled'] or (r['owner'] and r['expires_at'] > now):
                return None
            if not force and not (r['next_due'] <= now or r['owner']):
                return None
            if r['owner']:
                db.execute("UPDATE maintenance_runs SET outcome='expired',completed_at=? WHERE job_id=? AND generation=? AND outcome='running'", (now, job_id, r['generation']))
            generation, owner = r['generation'] + 1, uuid.uuid4().hex
            db.execute("UPDATE maintenance_jobs SET generation=?,owner=?,expires_at=?,last_attempt=?,outcome='running',error_type=NULL WHERE id=?", (generation, owner, now + lease_seconds, now, job_id))
            db.execute("INSERT INTO maintenance_runs VALUES(?,?,?,?,NULL,'running',NULL,NULL)", (job_id, generation, r['spec_hash'], now))
            return (generation, owner), json.loads(r['spec'])

    def _finish(self, job_id, token, receipt=None, error=None, partial=False):
        body = encode(receipt) if receipt is not None else None
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            r = db.execute('SELECT * FROM maintenance_jobs WHERE id=?', (job_id,)).fetchone()
            if not r or (r['generation'], r['owner']) != token or r['expires_at'] <= now:
                return False
            outcome = 'failed' if error else ('partial' if partial else 'success')
            error_type = type(error).__name__ if error else None
            success = now if outcome == 'success' else r['last_success']
            # Failed/partial jobs retry sooner; success advances only on actual success.
            delay = r['cadence'] if outcome == 'success' else min(r['cadence'], 60)
            db.execute("UPDATE maintenance_jobs SET owner=NULL,expires_at=NULL,last_success=?,next_due=?,outcome=?,error_type=?,receipt=? WHERE id=?", (success, now + delay, outcome, error_type, body, job_id))
            db.execute("UPDATE maintenance_runs SET completed_at=?,outcome=?,error_type=?,receipt=? WHERE job_id=? AND generation=?", (now, outcome, error_type, body, job_id, token[0]))
            return True

    def _execute(self, spec, deadline):
        if spec['kind'] == 'league':
            return self.service.refresh(spec['alias'], deadline=deadline)
        if spec['kind'] == 'forecast':
            kwargs = {'weeks': spec.get('weeks'), 'force': True}
            if 'deadline' in inspect.signature(self.service.refresh_forecasts).parameters:
                kwargs['deadline'] = deadline
            return self.service.refresh_forecasts(spec['alias'], **kwargs)
        from .pipelines import sync
        return sync(self.service, spec['datasets'], spec['seasons'], force=True, offline=spec.get('offline', False))

    def run_due(self, *, job_ids=None, deadline=None, timeout_seconds=30, force=False):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise DataError("timeout_seconds must be finite and nonnegative")
        if not isinstance(force, bool):
            raise DataError("force must be boolean")
        if job_ids is not None and (not isinstance(job_ids, list) or len(job_ids) > 100 or any(not isinstance(j, str) for j in job_ids)):
            raise DataError("job_ids must be a bounded list of IDs")
        end = time.monotonic() + timeout_seconds
        if deadline is not None:
            end = min(end, time.monotonic() + max(0, timestamp(deadline) - time.time()))
        jobs = self.status()['jobs']
        if job_ids is not None:
            if set(job_ids) - {j['id'] for j in jobs}:
                raise DataError("Unknown maintenance job")
            jobs = [j for j in jobs if j['id'] in job_ids]
        selected = [j for j in jobs if j['enabled'] and (force or j['due'] or j['running'])]
        started, events = [], []
        for job in selected:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                acquired = self._acquire(job['id'], max(.001, remaining), force)
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                continue
            if acquired:
                token, spec = acquired
                event = threading.Event()
                def work(job_id=job['id'], owned=token, specification=spec, done=event):
                    try:
                        result = self._execute(specification, end)
                        if not isinstance(result, dict):
                            raise DataError('Acquisition must return an object')
                        failed = result.get('ok') is False or result.get('status') in {'failed', 'error', 'timeout'}
                        partial = bool(result.get('status') and result['status'] not in {'acquired', 'success', 'cached', 'refreshed', 'ready'})
                        self._finish(job_id, owned, result, error=DataError('Acquisition failed') if failed else None, partial=partial)
                    except Exception as exc:
                        try:
                            self._finish(job_id, owned, error=exc)
                        except sqlite3.OperationalError:
                            pass  # durable lease expiry makes this job recoverable
                    finally:
                        done.set()
                threading.Thread(target=work, daemon=True).start()
                started.append(job['id'])
                events.append(event)
        # Poll all jobs together, not one full timeout per job.
        while time.monotonic() < end:
            state = self.status()['jobs']
            if all(not j['running'] for j in state if j['id'] in {s['id'] for s in selected}):
                break
            time.sleep(min(.02, max(0, end - time.monotonic())))
        state = self.status()['jobs']
        requested = {j['id'] for j in selected}
        return {'jobs': [j for j in state if j['id'] in requested], 'started': started,
                'deadline_exhausted': time.monotonic() >= end,
                'semantics': 'One bounded pass, joining active jobs; no daemon. Timed-out I/O may finish later; expired owners cannot update this ledger. Provider publication has its own fencing/atomic validation. Successful acquisition does not certify complete decision evidence.'}

    def history(self, job_id, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise DataError('History limit must be 1..100')
        self.status(job_id)
        with self._db() as db:
            rows = db.execute('SELECT * FROM maintenance_runs WHERE job_id=? ORDER BY generation DESC LIMIT ?', (job_id, limit)).fetchall()
        return [{**dict(r), 'receipt': json.loads(r['receipt']) if r['receipt'] else None} for r in rows]
