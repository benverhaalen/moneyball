"""Public Sleeper draft observations, immutable snapshots and deterministic replay.

All remote operations are GET. ``kind`` is explicit; there is no default draft ID.
Raw archives are unmodified decoded JSON, hashed with Store's canonical encoding;
separate HTTP wire-body hashes and cache headers are retained on network reads.
Wire bytes are not separately archived. No model is invoked.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

from .sleeper import Client, DataError
from .store import Store, digest, encode


class CaptureError(DataError):
    pass


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.staged')
    tmp.write_text(encode(value) + '\n')
    tmp.replace(path)


def _read(path):
    return json.loads(Path(path).read_text())


def _append(path, value):
    with Path(path).open('a') as handle:
        handle.write(encode(value) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _directory(root, draft_id):
    if not re.fullmatch(r'[0-9]+', str(draft_id)):
        raise CaptureError('Explicit numeric draft ID required')
    return Path(root).expanduser().resolve() / 'draft' / 'runs' / str(draft_id)


@contextmanager
def _locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.capture.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _kind(directory, draft_id, kind):
    if kind not in ('mock', 'live'):
        raise CaptureError('kind must explicitly be mock or live')
    path = directory / 'identity.json'
    identity = {'schema_version': 1, 'draft_id': str(draft_id), 'kind': kind,
                'classification': 'explicit caller declaration, checked against league_id when available',
                'remote_writes_supported': False}
    if path.exists() and _read(path) != identity:
        raise CaptureError('Capture kind/identity cannot change inside an existing draft run')
    if not path.exists():
        _write(path, identity)


def _validate(data, draft_id, kind):
    info, picks, trades = data['info'], data['picks'], data['traded_picks']
    if info.get('draft_id') != str(draft_id):
        raise CaptureError('Draft response identity mismatch')
    if kind == 'mock' and info.get('league_id'):
        raise CaptureError('Declared mock has a league_id; refusing to mix a league draft into mock capture')
    if not isinstance(info.get('settings'), dict) or not isinstance(info.get('status'), str):
        raise CaptureError('Missing draft settings/status')
    if not isinstance(picks, list) or not isinstance(trades, list):
        raise CaptureError('Picks/ownership response must be a list')
    seen = set()
    for row in picks:
        if not isinstance(row, dict):
            raise CaptureError('Pick row must be an object')
        number = row.get('pick_no')
        if type(number) is not int or number < 1 or number in seen:
            raise CaptureError('Pick numbers must be unique positive integers')
        seen.add(number)
        if not isinstance(row.get('player_id'), str) or not row['player_id']:
            raise CaptureError('Pick is missing string player identity')
        if row.get('draft_id') not in (None, str(draft_id)):
            raise CaptureError('Pick draft identity mismatch')
    if any(not isinstance(row, dict) for row in trades):
        raise CaptureError('Traded ownership rows must be objects')
    for row in trades:
        if row.get('draft_id') not in (None, str(draft_id)):
            raise CaptureError('Traded ownership draft identity mismatch')
    warnings = []
    if seen and seen != set(range(1, max(seen) + 1)):
        warnings.append('Pick sequence has gaps; preserve evidence, do not infer skipped selections')
    # No false atomicity: each public endpoint is fetched at a different instant.
    return {'atomic_snapshot': False, 'validation': 'identity, response shapes and unique pick numbers',
            'ownership_interpretation': 'draft_order/slot_to_roster_id and traded_picks retained; no inferred transfer execution',
            'warnings': warnings,
            'limitation': 'A selection or ownership change can occur between endpoints; recheck before action'}


def changes(previous, current):
    """Include undo/correction and ownership changes, not just appended picks."""
    previous = previous or {'info': {}, 'picks': [], 'traded_picks': []}
    old = {r['pick_no']: r for r in previous['picks']}
    new = {r['pick_no']: r for r in current['picks']}
    return {
        'added_picks': [new[n] for n in sorted(new.keys() - old.keys())],
        'removed_picks': [old[n] for n in sorted(old.keys() - new.keys())],
        'corrected_picks': [{'before': old[n], 'after': new[n]} for n in sorted(old.keys() & new.keys()) if old[n] != new[n]],
        'info_fields_changed': sorted(k for k in previous['info'].keys() | current['info'].keys()
                                      if previous['info'].get(k) != current['info'].get(k)),
        'traded_ownership_changed': previous['traded_picks'] != current['traded_picks'],
    }


def _load_snapshot(directory, snapshot_hash):
    item = _read(directory / 'snapshots' / (snapshot_hash + '.json'))
    if item.get('snapshot_hash') != snapshot_hash or digest(item['data']) != snapshot_hash:
        raise CaptureError('Archived draft snapshot hash mismatch')
    for section, sha in item['source_payload_hashes'].items():
        raw = _read(directory / 'raw' / (sha + '.json'))
        if digest(raw) != sha or raw != item['data'][section]:
            raise CaptureError('Archived raw source hash/content mismatch: ' + section)
    return item


def _events(directory):
    path = directory / 'events.jsonl'
    result, previous = [], None
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        event = json.loads(line)
        unsigned = {k: v for k, v in event.items() if k != 'event_hash'}
        if event.get('event_hash') != digest(unsigned):
            raise CaptureError('Replay event hash mismatch')
        if event['sequence'] != len(result) + 1 or event['previous_snapshot_hash'] != previous:
            raise CaptureError('Replay event chain is inconsistent')
        previous = event['snapshot_hash']
        result.append(event)
    return result


def capture(root, draft_id, *, kind, offline=False, client=None, accept_observed_regression=False, picks_cache_probe=False):
    directory = _directory(root, draft_id)
    with _locked(directory):
        _kind(directory, draft_id, kind)
        if offline and picks_cache_probe:
            raise CaptureError('A cache probe requires a network read')
        client = client or Client(Store(directory / 'cache'), offline=offline, force=not offline,
                                  draft_picks_cache_probe=picks_cache_probe)
        start = time.time()
        paths = {'info': f'draft/{draft_id}', 'picks': f'draft/{draft_id}/picks',
                 'traded_picks': f'draft/{draft_id}/traded_picks'}
        data, errors, evidence, raw_hashes = {}, {}, {}, {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {pool.submit(client.get, path, 0): (part, path) for part, path in paths.items()}
            for future in as_completed(pending):
                part, path = pending[future]
                try:
                    value = future.result()
                    data[part] = value
                    sha = digest(value)
                    raw_hashes[part] = sha
                    evidence[part] = dict(client.evidence[path])
                    if evidence[part]['sha256'] != sha:
                        raise CaptureError('Client source hash mismatch: ' + path)
                    dest = directory / 'raw' / (sha + '.json')
                    if not dest.exists():
                        _write(dest, value)
                    elif digest(_read(dest)) != sha:
                        raise CaptureError('Previously archived raw data corrupted: ' + part)
                except Exception as exc:
                    errors[part] = f'{type(exc).__name__}: {exc}'
        checked = time.time()
        receipt = {'schema_version': 1, 'draft_id': str(draft_id), 'kind': kind,
                   'checked_at': checked, 'started_at': start, 'ok': not errors,
                   'sources': evidence, 'source_payload_hashes': raw_hashes,
                   'hash_scope': 'canonical decoded payload archive; separate HTTP wire-body hash retained on network reads, wire bytes not separately archived',
                   'picks_cache_probe': picks_cache_probe,
                   'network_requests': sum(not e['cache_hit'] for e in evidence.values()),
                   'errors': errors}
        consistency = None
        if not errors:
            try:
                consistency = _validate(data, draft_id, kind)
            except Exception as exc:
                errors['validation'] = f'{type(exc).__name__}: {exc}'
        if errors:
            receipt.update(ok=False, published=False)
            _append(directory / 'checks.jsonl', receipt)
            raise CaptureError('Partial/invalid draft capture; previous published snapshot preserved: ' + encode(errors))
        consistency['actionable_freshness_verified'] = False
        consistency['freshness_gate'] = 'Current UI-confirmed complete prefix required externally before any pick judgment; network fetch alone is insufficient'
        consistency['http_cache'] = {part: e.get('http', {}).get('headers') for part, e in evidence.items()}
        for part, e in evidence.items():
            headers = e.get('http', {}).get('headers', {})
            if not headers:
                consistency['warnings'].append(part + ': HTTP cache freshness metadata unavailable')
            elif headers.get('age') or headers.get('cf-cache-status', '').upper() in ('UPDATING', 'STALE'):
                consistency['warnings'].append(part + ': intermediary cache may lag current draft state; Age=' + headers.get('age', 'unknown') + ', cache=' + headers.get('cf-cache-status', 'unknown'))
        sha = digest(data)
        latest_path = directory / 'latest.json'
        latest = _read(latest_path) if latest_path.exists() else None
        journal = _events(directory)
        if journal:
            last = journal[-1]
            # Journal is the commit record; recover a crash between append and pointer publication.
            if latest is None or latest['sequence'] != last['sequence'] or latest['snapshot_hash'] != last['snapshot_hash']:
                latest = {'snapshot_hash': last['snapshot_hash'], 'sequence': last['sequence'],
                          'last_checked_at': last['observed_at'], 'sources': last['sources']}
        elif latest is not None:
            raise CaptureError('Latest draft pointer exists without committed event history')
        old = _load_snapshot(directory, latest['snapshot_hash'])['data'] if latest else None
        changed = latest is None or latest['snapshot_hash'] != sha
        delta = changes(old, data)
        if old is not None and (delta['removed_picks'] or delta['corrected_picks']) and not accept_observed_regression:
            receipt.update(ok=False, published=False, consistency=consistency,
                           errors={'prefix': 'Observed prefix regressed or changed; stale API cache or real undo/correction unresolved'},
                           rejected_changes=delta)
            _append(directory / 'checks.jsonl', receipt)
            raise CaptureError('Draft prefix regression/correction rejected; preserve previous state until an explicit current observation confirms the change')
        consistency['regression_explicitly_accepted'] = bool(accept_observed_regression and (delta['removed_picks'] or delta['corrected_picks']))
        if changed:
            snapshot_path = directory / 'snapshots' / (sha + '.json')
            if not snapshot_path.exists():
                _write(snapshot_path, {'schema_version': 1, 'draft_id': str(draft_id), 'kind': kind,
                                      'snapshot_hash': sha, 'data': data, 'first_observed_at': checked,
                                      'source_payload_hashes': raw_hashes})
            else:
                _load_snapshot(directory, sha)
            # Events refer to immutable snapshots. Undo returning to an old state is still a new event.
            event = {'schema_version': 1, 'sequence': 1 if latest is None else latest['sequence'] + 1,
                     'observed_at': checked, 'snapshot_hash': sha,
                     'previous_snapshot_hash': latest['snapshot_hash'] if latest else None,
                     'draft_id': str(draft_id), 'kind': kind, 'changes': delta,
                     'sources': evidence, 'consistency': consistency}
            event['event_hash'] = digest(event)
            _append(directory / 'events.jsonl', event)
        sequence = (1 if latest is None else latest['sequence'] + 1) if changed else latest['sequence']
        receipt.update(changed=changed, published=True, snapshot_hash=sha, sequence=sequence,
                       consistency=consistency, changes=delta, pick_count=len(data['picks']),
                       status=data['info']['status'])
        # A freshness receipt is updated even when data are identical; snapshots are memoized.
        _write(latest_path, {'snapshot_hash': sha, 'sequence': sequence, 'last_checked_at': checked,
                             'draft_id': str(draft_id), 'kind': kind, 'sources': evidence})
        _append(directory / 'checks.jsonl', receipt)
        return receipt


def replay(root, draft_id, *, kind, as_of=None):
    directory = _directory(root, draft_id)
    if not (directory / 'identity.json').exists():
        raise CaptureError('No captured draft with that ID')
    identity = _read(directory / 'identity.json')
    if identity['kind'] != kind:
        raise CaptureError('Requested kind does not match captured draft')
    events_path = directory / 'events.jsonl'
    if not events_path.exists():
        raise CaptureError('No complete published snapshot; inspect checks.jsonl for partial failures')
    picked = None
    for event in _events(directory):
        item = _load_snapshot(directory, event['snapshot_hash'])
        if event['draft_id'] != str(draft_id) or event['kind'] != kind:
            raise CaptureError('Replay event identity mismatch')
        for section, source in event['sources'].items():
            if source['sha256'] != item['source_payload_hashes'].get(section):
                raise CaptureError('Replay source evidence hash mismatch')
        if as_of is None or event['observed_at'] <= as_of:
            picked = (event, item)
    if picked is None:
        raise CaptureError('No complete observation existed by requested as-of time')
    event, item = picked
    return {'schema_version': 1, 'draft_id': str(draft_id), 'kind': kind,
            'as_of': as_of, 'observed_at': event['observed_at'], 'sequence': event['sequence'],
            'snapshot_hash': event['snapshot_hash'], 'data': item['data'],
            'sources': event['sources'], 'consistency': event['consistency'],
            'scope': 'Observed state as of capture time; not reconstruction of unseen intermediate server events'}


def compact(receipt, limit=10):
    """CLI summaries bound output; the complete event remains in the journal."""
    result = dict(receipt)
    delta = dict(result.get('changes', {}))
    for key in ('added_picks', 'removed_picks', 'corrected_picks'):
        rows = delta.get(key, [])
        delta[key + '_count'] = len(rows)
        delta[key] = rows[-limit:]
        if len(rows) > limit:
            delta[key + '_omitted_from_display'] = len(rows) - limit
    result['changes'] = delta
    return result


def _as_of(value):
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('as-of ISO timestamp must include a timezone')
        return dt.timestamp()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('capture', 'watch', 'replay'))
    parser.add_argument('--draft-id', required=True)
    parser.add_argument('--kind', choices=('mock', 'live'), required=True)
    parser.add_argument('--root', default='.moneyball')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--as-of')
    parser.add_argument('--picks-cache-probe', action='store_true', help='Capture only: one public unique-query picks read; still requires current UI prefix confirmation')
    parser.add_argument('--accept-observed-regression', action='store_true', help='Capture only: explicit confirmation of an observed real undo/correction; not a freshness bypass')
    parser.add_argument('--seconds', type=float, help='Required bounded watch duration, at most 3600')
    parser.add_argument('--interval', type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        if args.command == 'replay':
            print(encode(replay(args.root, args.draft_id, kind=args.kind, as_of=_as_of(args.as_of))))
        elif args.command == 'capture':
            print(encode(compact(capture(args.root, args.draft_id, kind=args.kind, offline=args.offline, accept_observed_regression=args.accept_observed_regression, picks_cache_probe=args.picks_cache_probe))))
        else:
            if args.picks_cache_probe:
                raise CaptureError('Origin cache probing is permitted only for one capture, never a watch')
            if args.accept_observed_regression:
                raise CaptureError('Regression confirmation is permitted only for one capture, never a watch')
            if args.seconds is None or not 0 < args.seconds <= 3600 or args.interval < 1:
                raise CaptureError('Watch requires 0 < seconds <= 3600 and interval >= 1')
            end = time.monotonic() + args.seconds
            while True:
                print(encode(compact(capture(args.root, args.draft_id, kind=args.kind, offline=args.offline))), flush=True)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(args.interval, remaining))
                if time.monotonic() >= end:
                    break
    except (CaptureError, OSError, ValueError, KeyError) as exc:
        print(encode({'ok': False, 'error': str(exc), 'draft_id': args.draft_id, 'kind': args.kind}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
