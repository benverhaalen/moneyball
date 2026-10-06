"""Versioned analytical warehouse. Event time is never information availability.

Raw bytes survive failed validation. Published dataset partitions are immutable
snapshots; readers only see a complete validated batch. This store is separate
from the existing public league cache.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
import re
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time


class WarehouseError(RuntimeError):
    pass


class ValidationError(WarehouseError):
    pass


class LeakageError(WarehouseError):
    pass


def timestamp(value=None):
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Naive timestamp requires an explicit timezone')
        result = value.timestamp()
    else:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Naive timestamp requires an explicit timezone')
        result = parsed.timestamp()
    if not math.isfinite(result):
        raise ValueError('Timestamp must be finite')
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def leakage_check(rows, cutoff, *, raise_on_error=False):
    """Check every feature row, including missing provenance; never infer dates."""
    cutoff = timestamp(cutoff)
    errors = []
    for index, row in enumerate(rows):
        p = row.get('_provenance') or {}
        if p.get('available_at') is None:
            errors.append({'row': index, 'reason': 'missing_availability_provenance'})
        elif timestamp(p['available_at']) > cutoff:
            errors.append({'row': index, 'reason': 'available_after_cutoff', 'available_at': p['available_at']})
        if p.get('historical_vintage_unknown') and p.get('observed_at') is not None and timestamp(p['observed_at']) > cutoff:
            errors.append({'row': index, 'reason': 'unknown_historical_vintage'})
    if errors and raise_on_error:
        raise LeakageError(canonical(errors[:10]))
    return errors


class Warehouse:
    def __init__(self, root='.moneyball/lab'):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.root / 'raw' / 'sha256'
        self.blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'warehouse.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY, updated_at REAL NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_receipts(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
                    sha256 TEXT NOT NULL, observed_at REAL NOT NULL, byte_count INTEGER NOT NULL, http_meta TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS http_cache(url TEXT PRIMARY KEY, raw_id INTEGER NOT NULL,
                    checked_at REAL NOT NULL, etag TEXT, last_modified TEXT);
                CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, dataset TEXT NOT NULL,
                    partition_key TEXT NOT NULL, raw_id INTEGER NOT NULL, observed_at REAL NOT NULL, event_min REAL,
                    event_max REAL, published_at REAL, available_at REAL NOT NULL, publication_evidence TEXT,
                    vintage_verified INTEGER NOT NULL, content_sha256 TEXT, row_count INTEGER NOT NULL,
                    status TEXT NOT NULL, validation TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS batch_lookup ON batches(dataset,source_id,partition_key,available_at,observed_at,id);
                CREATE TABLE IF NOT EXISTS records(batch_id INTEGER NOT NULL, record_key TEXT NOT NULL,
                    event_at REAL, row_hash TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(batch_id,record_key));
                CREATE INDEX IF NOT EXISTS records_event ON records(batch_id,event_at);
                CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL,
                    cutoff REAL, purpose TEXT NOT NULL, code_version TEXT NOT NULL, input_batches TEXT NOT NULL,
                    sha256 TEXT NOT NULL, body TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hypotheses(id INTEGER PRIMARY KEY, hypothesis_id TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, created_at REAL NOT NULL, severity TEXT NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_source(self, source_id, metadata):
        item = dict(metadata)
        item['source_id'] = source_id
        item.setdefault('access', 'public')
        item.setdefault('history', 'unknown')
        item.setdefault('license_status', 'unverified')
        item.setdefault('verification_status', 'unverified')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)', (source_id, time.time(), canonical(item)))
        return item

    def source_registry(self):
        with self.connect() as db:
            items = [json.loads(x['metadata']) for x in db.execute('SELECT metadata FROM sources ORDER BY source_id')]
            for item in items:
                rows = db.execute("SELECT dataset,partition_key,row_count,observed_at,available_at,id FROM batches WHERE source_id=? AND status='published' ORDER BY observed_at DESC,id DESC", (item['source_id'],)).fetchall()
                seen, current = set(), []
                for row in rows:
                    group = (row['dataset'], row['partition_key'])
                    if group not in seen:
                        seen.add(group)
                        current.append({'dataset':row['dataset'],'partition':row['partition_key'],'row_count':row['row_count'],
                                        'observed_at':row['observed_at'],'available_at':row['available_at'],'batch_id':row['id']})
                item['acquisition'] = {'published':bool(current),'current_partitions':current,'row_count':sum(x['row_count'] for x in current)}
            return items

    def put_raw(self, source_id, url, body, observed_at=None, http_meta=None):
        if not isinstance(body, bytes):
            raise TypeError('raw body must be bytes')
        observed = timestamp(observed_at)
        digest = sha256(body)
        path = self.blobs / (digest + '.gz')
        if path.exists():
            self._read_blob(digest)
        else:
            # Atomic content-addressed publication, deterministic gzip container.
            with tempfile.NamedTemporaryFile(dir=self.blobs, delete=False) as f:
                tmp = Path(f.name)
                f.write(gzip.compress(body, mtime=0))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, path)
                path.chmod(0o400)
            finally:
                tmp.unlink(missing_ok=True)
        with self.connect() as db:
            cur = db.execute('INSERT INTO raw_receipts(source_id,url,sha256,observed_at,byte_count,http_meta) VALUES(?,?,?,?,?,?)',
                             (source_id, url, digest, observed, len(body), canonical(http_meta or {})))
            raw_id = cur.lastrowid
        return self.raw_receipt(raw_id)

    def _read_blob(self, digest):
        try:
            body = gzip.decompress((self.blobs / (digest + '.gz')).read_bytes())
        except (OSError, EOFError) as exc:
            raise WarehouseError('Raw blob missing or corrupt: ' + digest) from exc
        if sha256(body) != digest:
            raise WarehouseError('Raw blob integrity mismatch: ' + digest)
        return body

    def raw_receipt(self, raw_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM raw_receipts WHERE id=?', (raw_id,)).fetchone()
        if row is None:
            raise WarehouseError('Unknown raw receipt: ' + str(raw_id))
        result = dict(row)
        result['http_meta'] = json.loads(result['http_meta'])
        return result

    def raw_bytes(self, raw_id):
        return self._read_blob(self.raw_receipt(raw_id)['sha256'])

    def cached_http(self, url):
        with self.connect() as db:
            row = db.execute('SELECT * FROM http_cache WHERE url=?', (url,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['raw'] = self.raw_receipt(result['raw_id'])
        # A cache hit is unusable if the immutable object fails its hash check.
        self.raw_bytes(result['raw_id'])
        return result

    def cache_http(self, url, raw_id, *, checked_at=None, etag=None, last_modified=None):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?)',
                       (url, raw_id, timestamp(checked_at), etag, last_modified))

    def publish(self, source_id, dataset, rows, *, raw_id, key_fields, partition='all', observed_at=None,
                published_at=None, publication_evidence=None, event_field=None, required_fields=(),
                allow_empty=False, ranges=None, max_row_drop_fraction=None):
        raw = self.raw_receipt(raw_id)
        self.raw_bytes(raw_id)
        if raw['source_id'] != source_id:
            raise ValidationError('Raw source identity does not match batch source')
        observed = timestamp(observed_at) if observed_at is not None else raw['observed_at']
        # Clean transforms cannot claim they observed bytes before retrieval.
        if observed < raw['observed_at']:
            raise ValidationError('Batch observation precedes its raw retrieval')
        published = timestamp(published_at) if published_at is not None else None
        evidence = publication_evidence or {}
        verified = bool(published is not None and evidence.get('verified') is True
                        and evidence.get('kind') in ('immutable_archive', 'record_publication', 'release_version')
                        and evidence.get('url') and evidence.get('content_sha256') == raw['sha256'])
        if verified and published > observed:
            raise ValidationError('Verified publication cannot follow observation')
        available = published if verified else observed
        errors, cleaned, seen, schemas, events = [], [], set(), set(), []
        try:
            for index, original in enumerate(rows):
                if not isinstance(original, dict):
                    raise ValueError('rows must be dictionaries')
                row = {k: v for k, v in original.items() if k != '_provenance'}
                schemas.update(row)
                for field in tuple(required_fields) + tuple(key_fields):
                    if field not in row or row[field] is None or row[field] == '':
                        errors.append(f'row {index}: missing {field}')
                key = canonical([row.get(k) for k in key_fields])
                if key in seen:
                    errors.append(f'row {index}: duplicate canonical key {key}')
                seen.add(key)
                for field, limits in (ranges or {}).items():
                    val = row.get(field)
                    if val is not None and (not isinstance(val, (int, float)) or not limits[0] <= val <= limits[1]):
                        errors.append(f'row {index}: {field} outside range {limits}')
                body = canonical(row)
                event = timestamp(row[event_field]) if event_field and row.get(event_field) else None
                if event is not None:
                    events.append(event)
                cleaned.append((key, event, sha256(body.encode()), body))
            if not key_fields:
                errors.append('key_fields cannot be empty')
            if not cleaned and not allow_empty:
                errors.append('empty batch')
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(str(exc))
        content_hash = sha256(canonical(sorted((r[0], r[2]) for r in cleaned)).encode())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute("SELECT * FROM batches WHERE source_id=? AND dataset=? AND partition_key=? AND status='published' ORDER BY observed_at DESC,id DESC LIMIT 1",
                               (source_id, dataset, str(partition))).fetchone()
            previous_count = prior['row_count'] if prior else 0
            if max_row_drop_fraction is not None and previous_count and len(cleaned) < previous_count * (1 - max_row_drop_fraction):
                errors.append('row count dropped beyond configured limit')
            validation = {'ok': not errors, 'errors': errors[:50], 'error_count': len(errors), 'schema': sorted(schemas),
                          'key_fields': list(key_fields), 'row_count': len(cleaned), 'previous_row_count': previous_count,
                          'row_delta': len(cleaned) - previous_count, 'ranges': ranges or {},
                          'event_min': min(events) if events else None, 'event_max': max(events) if events else None}
            if not errors and prior and prior['content_sha256'] == content_hash and prior['available_at'] <= available and bool(prior['vintage_verified']) == verified:
                result = self._batch_dict(prior)
                result['unchanged'] = True
                return result
            cur = db.execute('''INSERT INTO batches(source_id,dataset,partition_key,raw_id,observed_at,event_min,event_max,
                published_at,available_at,publication_evidence,vintage_verified,content_sha256,row_count,status,validation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (source_id, dataset, str(partition), raw_id, observed,
                validation['event_min'], validation['event_max'], published, available, canonical(evidence), int(verified),
                content_hash, len(cleaned), 'quarantined' if errors else 'published', canonical(validation)))
            batch_id = cur.lastrowid
            if not errors:
                db.executemany('INSERT INTO records VALUES (?,?,?,?,?)', [(batch_id, *r) for r in cleaned])
        if errors:
            self.alert('validation_failed', {'batch_id': batch_id, 'source_id': source_id, 'dataset': dataset, 'errors': errors[:10]}, severity='error')
            raise ValidationError(f'Batch {batch_id} quarantined: ' + '; '.join(errors[:5]))
        result = self.batch(batch_id)
        result['unchanged'] = False
        return result

    def quarantine(self, source_id, dataset, *, raw_id, reason, partition='all'):
        """Record a parser/schema failure before rows could reach publish()."""
        raw = self.raw_receipt(raw_id)
        if raw['source_id'] != source_id:
            raise ValidationError('Quarantine source differs from raw receipt')
        validation = {'ok': False, 'errors': [str(reason)], 'error_count': 1, 'row_count': 0}
        with self.connect() as db:
            cur = db.execute("""INSERT INTO batches(source_id,dataset,partition_key,raw_id,observed_at,
                available_at,vintage_verified,row_count,status,validation) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_id,dataset,str(partition),raw_id,raw['observed_at'],raw['observed_at'],0,0,'quarantined',canonical(validation)))
            batch_id = cur.lastrowid
        self.alert('validation_failed', {'batch_id':batch_id,'raw_id':raw_id,'reason':str(reason)}, severity='error')
        return self.batch(batch_id)

    @staticmethod
    def _batch_dict(row):
        item = dict(row)
        item['batch_id'] = item.pop('id')
        item['partition'] = item.pop('partition_key')
        for key in ('validation', 'publication_evidence'):
            item[key] = json.loads(item[key]) if item[key] else None
        item['vintage_verified'] = bool(item['vintage_verified'])
        return item

    def batch(self, batch_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
        if row is None:
            raise WarehouseError('Unknown batch: ' + str(batch_id))
        return self._batch_dict(row)

    def batches(self, dataset=None, *, include_quarantined=True):
        clauses, args = [], []
        if dataset:
            clauses.append('dataset=?'); args.append(dataset)
        if not include_quarantined:
            clauses.append("status='published'")
        with self.connect() as db:
            rows = db.execute('SELECT * FROM batches' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY id', args).fetchall()
        return [self._batch_dict(x) for x in rows]

    def query(self, dataset, cutoff=None, *, source_id=None, purpose='features', event_before=None, system_asof=None, positions=None, seasons=None, columns=None, limit=None, player_ids=None, identity_filters=None):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError('limit must be a positive integer or None')
        if identity_filters is not None and not isinstance(identity_filters, dict):
            raise ValueError('identity_filters must be a field-to-identifiers dictionary')
        filters = dict(identity_filters or {})
        if player_ids is not None:
            if 'player_id' in filters:
                raise ValueError('Specify player_id through player_ids or identity_filters, not both')
            filters['player_id'] = player_ids
        for field, values in filters.items():
            if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field):
                raise ValueError('Identity field must be a simple JSON field name')
            if isinstance(values, (str, bytes)):
                raise ValueError('Identity filters must contain identifier lists')
            try:
                values = list(values)
            except TypeError as exc:
                raise ValueError('Identity filters must contain iterable identifier lists') from exc
            if any(isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value) for value in values):
                raise ValueError('Identifiers must be nonempty strings or integers')
            filters[field] = list(dict.fromkeys(map(str, values)))
        if purpose not in ('features', 'outcomes', 'exploratory'):
            raise ValueError('purpose must be features, outcomes or exploratory')
        # Even outcome queries honor an explicit cutoff. The purpose label does
        # not grant permission to silently bypass temporal filtering.
        cutoff_time = timestamp(cutoff) if cutoff is not None else time.time()
        clauses, args = ["dataset=?", "status='published'", 'available_at<=?'], [dataset, cutoff_time]
        if source_id:
            clauses.append('source_id=?'); args.append(source_id)
        if system_asof is not None:
            clauses.append('observed_at<=?'); args.append(timestamp(system_asof))
        with self.connect() as db:
            batches = db.execute('SELECT * FROM batches WHERE ' + ' AND '.join(clauses) + ' ORDER BY available_at DESC,observed_at DESC,id DESC', args).fetchall()
            chosen, result = set(), []
            for b in batches:
                group = (b['source_id'], b['partition_key'])
                if group in chosen:
                    continue
                chosen.add(group)
                sql, params = 'SELECT body,event_at,row_hash FROM records WHERE batch_id=?', [b['id']]
                if event_before is not None:
                    sql += ' AND event_at IS NOT NULL AND event_at<=?'; params.append(timestamp(event_before))
                for field, values in (('position', positions), ('season', seasons)):
                    if values is not None:
                        values = list(values)
                        if not values:
                            sql += ' AND 0'
                        else:
                            sql += " AND json_extract(body, '$." + field + "') IN (" + ','.join('?' for _ in values) + ')'
                            params.extend(values)
                if filters:
                    alternatives = []
                    for field, identifiers in filters.items():
                        if identifiers:
                            alternatives.append("CAST(json_extract(body, '$." + field + "') AS TEXT) IN (" + ','.join('?' for _ in identifiers) + ')')
                            params.extend(identifiers)
                    sql += ' AND (' + ' OR '.join(alternatives) + ')' if alternatives else ' AND 0'
                sql += ' ORDER BY record_key'
                if limit is not None:
                    sql += ' LIMIT ?'; params.append(limit - len(result))
                provenance = {'source_id': b['source_id'], 'dataset': dataset, 'batch_id': b['id'], 'raw_id': b['raw_id'],
                    'partition': b['partition_key'], 'observed_at': b['observed_at'], 'available_at': b['available_at'],
                    'published_at': b['published_at'], 'publication_evidence': json.loads(b['publication_evidence'] or '{}'),
                    'vintage_verified': bool(b['vintage_verified']), 'historical_vintage_unknown': not bool(b['vintage_verified']),
                    'purpose': purpose, 'content_sha256': b['content_sha256']}
                for r in db.execute(sql, params):
                    if sha256(r['body'].encode()) != r['row_hash']:
                        raise WarehouseError('Cleaned record hash mismatch in batch ' + str(b['id']))
                    row = json.loads(r['body'])
                    if columns is not None:
                        row = {key: row.get(key) for key in columns}
                    row['_provenance'] = {**provenance, 'event_at': r['event_at']}
                    result.append(row)
                if limit is not None and len(result) >= limit:
                    break
        leakage_check(result, cutoff_time, raise_on_error=True)
        return result

    def record_artifact(self, name, body, *, input_batches, cutoff=None, purpose='exploratory', code_version='unknown', metadata=None):
        ids = sorted(set(int(x) for x in input_batches))
        batches = [self.batch(x) for x in ids]
        if any(x['status'] != 'published' for x in batches):
            raise ValidationError('Artifacts cannot depend on quarantined batches')
        cut = timestamp(cutoff) if cutoff is not None else None
        if purpose == 'features':
            if cut is None:
                raise LeakageError('Feature artifact requires an information cutoff')
            if any(x['available_at'] > cut for x in batches):
                raise LeakageError('Feature artifact includes inputs after cutoff')
        encoded = canonical(body)
        with self.connect() as db:
            cur = db.execute('INSERT INTO artifacts(name,created_at,cutoff,purpose,code_version,input_batches,sha256,body,metadata) VALUES(?,?,?,?,?,?,?,?,?)',
                (name, time.time(), cut, purpose, code_version, canonical(ids), sha256(encoded.encode()), encoded, canonical(metadata or {})))
            ident = cur.lastrowid
        return {'artifact_id': ident, 'name': name, 'sha256': sha256(encoded.encode()), 'input_batches': ids, 'cutoff': cut, 'purpose': purpose}

    def preregister(self, hypothesis_id, body, *, status='preregistered'):
        if status not in ('preregistered', 'running', 'supported', 'rejected', 'inconclusive', 'superseded'):
            raise ValueError('Unknown hypothesis status')
        with self.connect() as db:
            cur = db.execute('INSERT INTO hypotheses(hypothesis_id,created_at,status,body) VALUES(?,?,?,?)',
                             (hypothesis_id, time.time(), status, canonical(body)))
            ident = cur.lastrowid
        return {'entry_id': ident, 'hypothesis_id': hypothesis_id, 'status': status}

    def hypotheses(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM hypotheses ORDER BY id').fetchall()
        return [{**dict(r), 'body': json.loads(r['body'])} for r in rows]

    def alert(self, kind, body, *, severity='warning'):
        item = {'created_at': time.time(), 'severity': severity, 'kind': kind, 'body': body}
        encoded = canonical(item) + '\n'
        with self.connect() as db:
            db.execute('INSERT INTO alerts(created_at,severity,kind,body) VALUES(?,?,?,?)',
                       (item['created_at'], severity, kind, canonical(body)))
        fd = os.open(self.root / 'alerts.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded.encode())
        finally:
            os.close(fd)
        return item

    def summary(self):
        with self.connect() as db:
            raw = db.execute('SELECT count(*) AS receipts,count(DISTINCT sha256) AS blobs,sum(byte_count) AS bytes FROM raw_receipts').fetchone()
            groups = db.execute("SELECT dataset,source_id,partition_key,max(observed_at) observed_at FROM batches WHERE status='published' GROUP BY dataset,source_id,partition_key").fetchall()
            quarantined = db.execute("SELECT count(*) FROM batches WHERE status='quarantined'").fetchone()[0]
        return {'path': str(self.path), 'raw': dict(raw), 'published_partitions': [dict(x) for x in groups], 'quarantined_batches': quarantined}
