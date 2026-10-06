"""Public nflverse adapters and cache-aware HTTP retrieval (standard library).

Endpoint templates were read in nflverse/nflreadr's R loader sources on
2026-09-07. Mutable annual files are final-data observations, not historical
forecast vintages. Restart resumes at already cached and published partitions;
partial HTTP bodies are never mistaken for complete raw objects.
"""
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .warehouse import Warehouse, WarehouseError, ValidationError, canonical, timestamp


class FetchError(WarehouseError):
    pass


class HttpFetcher:
    def __init__(self, warehouse, *, transport=None, sleep=time.sleep, clock=time.time,
                 min_interval=0.2, max_attempts=4, max_wait=60, max_bytes=200_000_000):
        self.warehouse = warehouse
        self.transport = transport or self._http
        self.sleep, self.clock = sleep, clock
        self.min_interval, self.max_attempts = min_interval, max_attempts
        self.max_wait, self.max_bytes = min(max_wait, 60), max_bytes
        self.last_request = None

    def _http(self, url, headers):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise FetchError('Response exceeds byte limit: ' + url)
                length = response.headers.get('Content-Length')
                if length and int(length) != len(body):
                    raise FetchError('Truncated HTTP body: ' + url)
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:
            # Preserve useful response status and retry headers, no remote HTML dump.
            return exc.code, dict(exc.headers or {}), b''

    def _retry_delay(self, headers, attempt):
        value = headers.get('retry-after', '')
        try:
            return max(0, float(value))
        except (TypeError, ValueError):
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - self.clock())
            except (TypeError, ValueError, OverflowError):
                return min(2 ** attempt, 30)

    def fetch(self, source_id, url, *, force=False, offline=False, ttl=3600):
        if urllib.parse.urlparse(url).scheme not in ('https', 'http'):
            raise FetchError('Only HTTP(S) sources are supported')
        cached = self.warehouse.cached_http(url)
        now = self.clock()
        if cached and (offline or not force and now - cached['checked_at'] < ttl):
            result = dict(cached['raw'])
            if result['source_id'] != source_id:
                raise FetchError('Cached URL source identity differs')
            return {**result, 'cache_hit': True, 'revalidated': False,
                    'checked_at': cached['checked_at'], 'age_seconds': max(0, now-cached['checked_at']),
                    'stale': now-cached['checked_at'] >= ttl}
        if offline:
            raise FetchError('No cached object for offline resume: ' + url)
        headers = {'User-Agent': 'moneyball-personal-research/0.2', 'Accept': '*/*', 'Accept-Encoding': 'gzip'}
        if cached:
            if cached.get('etag'):
                headers['If-None-Match'] = cached['etag']
            if cached.get('last_modified'):
                headers['If-Modified-Since'] = cached['last_modified']
        waited = 0.0
        for attempt in range(self.max_attempts):
            if self.last_request is not None:
                delay = max(0, self.min_interval - (self.clock()-self.last_request))
                if delay:
                    self.sleep(delay)
            self.last_request = self.clock()
            try:
                status, response_headers, body = self.transport(url, headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.max_attempts-1:
                    raise FetchError('Network retrieval failed: ' + url) from exc
                delay = 2 ** attempt
                if waited + delay > self.max_wait:
                    raise FetchError('Retry wait budget exceeded: ' + url) from exc
                self.sleep(delay); waited += delay
                continue
            response_headers = {str(k).lower(): str(v) for k,v in response_headers.items()}
            if status == 304:
                if not cached:
                    raise FetchError('304 without a cached object: ' + url)
                self.warehouse.cache_http(url, cached['raw_id'], checked_at=self.clock(),
                    etag=response_headers.get('etag', cached.get('etag')),
                    last_modified=response_headers.get('last-modified', cached.get('last_modified')))
                return {**cached['raw'], 'cache_hit': True, 'revalidated': True,
                        'checked_at': self.clock(), 'age_seconds': 0, 'stale': False}
            if status in (429, 500, 502, 503, 504):
                delay = self._retry_delay(response_headers, attempt)
                if attempt == self.max_attempts-1 or delay > 60 or waited+delay > self.max_wait:
                    raise FetchError(f'HTTP {status}; retry deferred after wait budget: {url}')
                self.sleep(delay); waited += delay
                continue
            if status != 200:
                raise FetchError(f'HTTP {status}: {url}')
            if not isinstance(body, bytes) or len(body) > self.max_bytes:
                raise FetchError('Invalid or oversized HTTP body: ' + url)
            receipt = self.warehouse.put_raw(source_id, url, body, observed_at=self.clock(),
                http_meta={'status': status, 'headers': response_headers, 'etag': response_headers.get('etag'),
                           'last_modified': response_headers.get('last-modified'),
                           'publication_timestamp_trusted': False})
            # Decode once before cache publication to catch truncated compressed responses.
            try:
                if response_headers.get('content-encoding', '').lower() == 'gzip':
                    gzip.decompress(body)
            except (OSError, EOFError) as exc:
                self.warehouse.alert('raw_encoding_invalid', {'raw_id': receipt['id'], 'url': url}, severity='error')
                raise FetchError('Corrupt HTTP gzip response: ' + url) from exc
            self.warehouse.cache_http(url, receipt['id'], checked_at=self.clock(),
                etag=response_headers.get('etag'), last_modified=response_headers.get('last-modified'))
            return {**receipt, 'cache_hit': False, 'revalidated': False,
                    'checked_at': self.clock(), 'age_seconds': 0, 'stale': False}
        raise FetchError('Retry exhaustion: ' + url)


RELEASE = 'https://github.com/nflverse/nflverse-data/releases/download/'
LOADER = 'https://raw.githubusercontent.com/nflverse/nflreadr/main/R/'
# License of the R package does not license every underlying dataset.
DATASETS = {
    'weekly': dict(url=RELEASE+'stats_player/stats_player_week_{season}.csv', loader='load_stats.R',
        key=('player_id','season','week','season_type'), required=('player_id','season','week','position','team'),
        history='1999 onward per loader', cadence='revised during season; fetch daily or explicitly fresh',
        origin='nflverse NFL player box-score statistics', yearly=True),
    'schedules': dict(url='https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv', loader='load_schedules.R',
        key=('game_id',), required=('game_id','season','week','home_team','away_team'),
        history='historical and future games in current mutable file', cadence='schedule/results updates',
        origin='Lee Sharpe / nflverse nfldata', yearly=False),
    'rosters': dict(url=RELEASE+'rosters/roster_{season}.csv', loader='load_rosters.R',
        key=('season','team','player_id'), required=('season','team','player_id'),
        history='1920 onward per loader', cadence='current season roster revisions',
        origin='nflverse season-level rosters', yearly=True),
    'players': dict(url=RELEASE+'players/players.csv', loader='load_players.R',
        key=('player_id',), required=('player_id',), history='cross-source player identifiers, mutable present snapshot',
        cadence='identifier corrections and new entrants', origin='nflverse-players cross-source identifiers', yearly=False),
    'snap_counts': dict(url=RELEASE+'snap_counts/snap_counts_{season}.csv', loader='load_snap_counts.R',
        key=('game_id','pfr_player_id','team'), required=('game_id','pfr_player_id','team','season','week'),
        history='2012 onward per loader', cadence='weekly after games; upstream corrections possible',
        origin='Pro Football Reference through nflverse-pfr', yearly=True),
    'draft_picks': dict(url=RELEASE+'draft_picks/draft_picks.csv', loader='load_draft_picks.R',
        key=('season','pick'), required=('season','pick'), history='1980 onward per loader', cadence='annual and corrected',
        origin='Pro Football Reference through nflverse', yearly=False),
    'combine': dict(url=RELEASE+'combine/combine.csv', loader='load_combine.R',
        key=('season','player_name','pos','school'), required=('season','player_name'),
        history='2000 onward per loader', cadence='annual and corrected',
        origin='Pro Football Reference through nflverse', yearly=False),
}


def registry(warehouse):
    result = []
    for name, spec in DATASETS.items():
        result.append(warehouse.register_source('nflverse_'+name, {
            'dataset': name, 'origin': spec['origin'], 'url_template': spec['url'],
            'access': 'free public HTTP CSV; no authentication', 'cadence': spec['cadence'], 'history': spec['history'],
            'license_status': ('no explicit dataset license found in nfldata README; unresolved' if name == 'schedules' else 'repository LICENSE.md declares CC BY 4.0; upstream third-party terms not separately audited'),
            'license_evidence_url': ('https://raw.githubusercontent.com/nflverse/nfldata/master/README.md' if name == 'schedules' else 'https://raw.githubusercontent.com/nflverse/nflverse-data/main/LICENSE.md'),
            'terms_urls': ['https://github.com/nflverse/nflverse-data', 'https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html'],
            'quality': ['mutable snapshots can be corrected', 'missing rows do not identify inactivity or injury',
                        'historical publication vintage unknown', 'canonical identifiers require crosswalk checks'],
            'verification_status': 'endpoint construction verified against actual loader source 2026-09-07',
            'verification_url': LOADER+spec['loader'], 'historical_pit_status': 'not verified; outcome/exploratory only before first observation',
            'transform_version': 'nflverse-csv-v1', 'fantasy_fields_policy': 'source fantasy point columns excluded from normalized stats'}))
    return result


def decode_csv(body, http_meta=None):
    headers = (http_meta or {}).get('headers') or {}
    if body[:2] == b'\x1f\x8b' or headers.get('content-encoding', '').lower() == 'gzip':
        body = gzip.decompress(body)
    text = body.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    fields = reader.fieldnames
    if not fields or len(fields) != len(set(fields)) or len(fields) < 2:
        raise ValidationError('CSV header is missing, duplicated or not tabular')
    result = []
    for i, row in enumerate(reader):
        if None in row or any(v is None for v in row.values()):
            raise ValidationError(f'CSV row {i+2} does not match header length')
        result.append(row)
    return result


NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
IDENTITY_FIELDS = {'player_id','gsis_id','pfr_id','pfr_player_id','sleeper_id','espn_id','otc_id','pff_id','esb_id','smart_id','game_id','old_game_id','nfl_detail_id','sleeper','nfl_id'}


def _value(key, value):
    if value is None or value in ('','NA','NaN','nan','NULL'):
        return None
    if key in IDENTITY_FIELDS or key.endswith('_id') or not NUMBER.fullmatch(str(value)):
        return value
    val = float(value)
    if not math.isfinite(val):
        raise ValidationError('Nonfinite CSV numeric field: ' + key)
    return int(val) if val.is_integer() else val


def normalize(dataset, rows):
    result = []
    for original in rows:
        row = {k: _value(k,v) for k,v in original.items()}
        if dataset == 'weekly':
            row['gsis_id'] = row.get('player_id')
            row['identity_verified'] = bool(row.get('player_id'))
            if not row.get('player_id'):
                row['player_id'] = 'unresolved:' + str(row.get('game_id')) + ':' + str(row.get('team') or row.get('recent_team'))
                row['identity_status'] = 'unattributed_source_record_not_a_player'
            row['position'] = row.get('position') or 'UNKNOWN'
            row['team'] = row.get('team') or row.get('recent_team')
            # No inferred zero, active or injury status from presence/absence.
            row['observation_type'] = 'reported_box_score'
            row['stats'] = {k:v for k,v in row.items() if isinstance(v,(int,float)) and k not in ('season','week') and not k.startswith('fantasy_')}
        elif dataset in ('rosters','players'):
            row['player_id'] = row.get('gsis_id') or row.get('player_id')
            if not row['player_id']:
                # Identity-qualified fallback, never confused with GSIS/Sleeper.
                alternatives = [(k,row.get(k)) for k in ('pfr_id','espn_id','sleeper_id','esb_id') if row.get(k)]
                if alternatives:
                    k,v = alternatives[0]; row['player_id'] = k + ':' + str(v)
                else:
                    # Retain raw unidentified records; no fuzzy identity join.
                    from .warehouse import sha256
                    row['player_id'] = 'unresolved:' + sha256(canonical(original).encode())[:24]
            row['identity_verified'] = bool(row.get('gsis_id'))
        elif dataset in ('draft_picks','combine'):
            row['season'] = row.get('season') or row.get('draft_year') or row.get('year')
        if dataset == 'schedules' and row.get('gameday'):
            # Midnight date marker only, not kickoff and NEVER availability.
            row['event_at'] = str(row['gameday']) + 'T00:00:00+00:00'
            row['event_time_precision'] = 'calendar_day_not_kickoff'
        result.append(row)
    return result


def ingest(warehouse=None, *, datasets=('weekly','schedules','rosters','players','snap_counts','draft_picks','combine'),
           seasons=(2024,2025), force=False, offline=False, fetcher=None, ttl=86400):
    warehouse = warehouse or Warehouse()
    registry(warehouse)
    fetcher = fetcher or HttpFetcher(warehouse)
    requested = sorted(set(int(x) for x in seasons))
    receipts, failures = [], []
    for dataset in datasets:
        if dataset not in DATASETS:
            raise ValueError('Unknown dataset: ' + dataset)
        spec = DATASETS[dataset]
        for year in requested if spec['yearly'] else (None,):
            source_id = 'nflverse_' + dataset
            url = spec['url'].format(season=year)
            partition = str(year) if year else 'all'
            raw = None
            try:
                raw = fetcher.fetch(source_id, url, force=force, offline=offline, ttl=ttl)
                rows = normalize(dataset, decode_csv(warehouse.raw_bytes(raw['id']), raw['http_meta']))
                # Global files are kept in full for idempotence and future cohort work.
                # Season filters are a model/read concern, not mutation of a global snapshot.
                ranges = {'season': (1920, 2200)} if dataset != 'players' else {}
                if dataset in ('weekly','schedules','snap_counts'):
                    ranges['week'] = (1, 30)
                batch = warehouse.publish(source_id, dataset, rows, raw_id=raw['id'], key_fields=spec['key'],
                    partition=partition, required_fields=spec['required'], event_field='event_at', ranges=ranges)
                receipts.append({**batch, 'url': url, 'raw_sha256': raw['sha256'], 'raw_bytes': raw['byte_count'],
                                 'cache_hit': raw['cache_hit'], 'revalidated': raw['revalidated'],
                                 'unresolved_identities': sum(r.get('identity_verified') is False for r in rows),
                                 'historical_vintage': 'unknown; first observed availability only'})
            except (WarehouseError, ValueError, UnicodeError, OSError, EOFError) as exc:
                failure = {'dataset': dataset, 'partition': partition, 'url': url, 'error': str(exc),
                           'raw_id': raw['id'] if raw else None, 'status': 'failed'}
                failures.append(failure)
                if raw is not None and not str(exc).startswith('Batch '):
                    warehouse.quarantine(source_id, dataset, raw_id=raw['id'], reason=str(exc), partition=partition)
                warehouse.alert('ingestion_failed', failure, severity='error')
    return {'ok': not failures, 'partial': bool(failures and receipts), 'checked_at': time.time(),
            'receipts': receipts, 'failures': failures, 'requested_seasons': requested,
            'temporal_limit': 'Historical mutable files are usable as outcomes/exploration, not verified vintage features.'}
