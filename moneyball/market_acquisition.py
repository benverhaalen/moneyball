"""Read-only, cached market discovery with immutable response receipts.

API paths and schemas were read in provider documentation and called firsthand
on 2026-09-08. No order endpoints, credentials, geo overrides or price edits.
Search completeness is measured, never inferred from one successful response.
"""
import argparse
import gzip
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from .sources import HttpFetcher
from .warehouse import Warehouse, WarehouseError, canonical


PROVIDERS = {
    'kalshi_public': {
        'base': 'https://external-api.kalshi.com/trade-api/v2',
        'docs': 'https://docs.kalshi.com/api-reference/market/get-markets',
        'terms': ['https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf',
                  'https://kalshi-public-docs.s3.amazonaws.com/kalshi-data-terms-of-service.pdf'],
        'rights_note': 'Restrictive intended-use terms were recorded in earlier research. Current user expressly requested private factual collection and analysis. This does not establish redistribution or commercial/model licensing rights; no such rights are claimed.',
    },
    'polymarket_global_public': {
        'base': 'https://gamma-api.polymarket.com',
        'docs': 'https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles',
        'terms': ['https://polymarket.com/tos'],
        'rights_note': 'Public researcher access documented; no redistribution licence inferred. Global and US markets are separate venues.',
    },
    'polymarket_us_public': {
        'base': 'https://gateway.polymarket.us',
        'docs': 'https://docs.polymarket.us/api-reference/introduction',
        'terms': ['https://polymarket.us/tos'],
        'rights_note': 'Documented public read-only market API. No redistribution licence inferred. outcomePrices is not assumed to be a Yes/No probability vector.',
    },
}

GLOBAL_QUERIES = (
    'Pro Football 2026 Passing', 'Pro Football 2026 Rushing',
    'Pro Football 2026 Receiving', 'Pro Football 2026 Receptions',
    'Pro Football 2026 Touchdowns', 'Pro Football 2026 Interceptions',
    'Pro Football 2026 MVP', 'Pro Football 2026 Rookie',
    'Pro Football 2026 Offensive Player', 'Pro Football 2026 Comeback',
    'Pro Football 2026 Fantasy', 'Pro Football 2026 Games Played',
)
US_QUERIES = ('Regular Season', 'Passing Yards', 'Passing TDs', 'Rushing Yards',
              'Rushing TDs', 'Receiving Yards', 'Receiving TDs', 'Receptions',
              'MVP', 'Offensive Rookie', 'Offensive Player', 'Comeback', 'Fantasy')


def decode_json(body):
    if body[:2] == b'\x1f\x8b':
        body = gzip.decompress(body)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('API root must be an object')
    return value


def relevant_kalshi_series(series):
    return [s for s in series if s.get('category') == 'Sports'
            and 'NFL' in s.get('ticker', '')
            and re.search('SEASON|LEADER|MVP|OROTY|OPOY|COMEBACK|EVERYWEEK|T100|NEXTTEAM|DEPTH|FFPLAYOFF|FF50', s['ticker'])]


def nfl_us_event(event):
    return (event.get('slug', '').startswith('nfl-') or
            any(t.get('slug') == 'nfl' for t in event.get('tags', [])))


class Collector:
    def __init__(self, root='.moneyball/markets', *, ttl=900, force=False, offline=False, fetcher=None):
        self.root = Path(root)
        self.warehouse = Warehouse(self.root)
        self.fetcher = fetcher or HttpFetcher(self.warehouse, min_interval=.3, max_attempts=3)
        self.ttl, self.force, self.offline = ttl, force, offline
        self.receipts, self.failures, self.coverage = [], [], []
        for sid, spec in PROVIDERS.items():
            self.warehouse.register_source(sid, {
                'origin': sid, 'access': 'documented public read-only HTTP; no account',
                'documentation_url': spec['docs'], 'terms_urls': spec['terms'],
                'license_status': spec['rights_note'],
                'cadence': 'explicit pre-decision snapshots; 15-minute request cache default',
                'history': 'first observation now; edited old markets are not historical vintages',
                'verification_status': 'endpoint and actual schema inspected firsthand 2026-09-08',
                'quality': ['metadata update time may differ from price update time',
                            'search may cap or ignore requested limits',
                            'thin/one-sided books cannot identify accurate probabilities',
                            'settlement horizon and participation conditions vary'],
            })

    def get(self, sid, path, parameters=None):
        url = PROVIDERS[sid]['base'] + path
        if parameters:
            url += '?' + urlencode(parameters)
        try:
            raw = self.fetcher.fetch(sid, url, ttl=self.ttl, force=self.force, offline=self.offline)
            data = decode_json(self.warehouse.raw_bytes(raw['id']))
            receipt = {k: raw.get(k) for k in ('id','sha256','observed_at','checked_at','cache_hit','revalidated','stale')}
            receipt.update(source_id=sid, url=url)
            self.receipts.append(receipt)
            return data, receipt
        except (WarehouseError, ValueError, OSError) as exc:
            failure = {'source_id':sid, 'url':url, 'observed_at':time.time(), 'error':str(exc)}
            self.failures.append(failure)
            with (self.root/'attempts.jsonl').open('a') as f:
                f.write(canonical(failure)+'\n')
            raise

    def global_events(self, queries=GLOBAL_QUERIES, max_pages=15):
        events = {}
        for query in queries:
            seen_pages, finished = set(), False
            count_before = len(events)
            for page in range(1, max_pages+1):
                try:
                    data, receipt = self.get('polymarket_global_public','/public-search',
                        {'q':query,'limit_per_type':100,'page':page,'search_profiles':'false','search_tags':'false'})
                except (WarehouseError, ValueError, OSError):
                    break
                rows = data.get('events')
                if not isinstance(rows, list):
                    raise ValueError('Global search lacks events list')
                signature = tuple(sorted(str(e['id']) for e in rows))
                if signature in seen_pages and rows:
                    self.failures.append({'source_id':'polymarket_global_public','query':query,
                        'error':'Pagination repeated a page; completeness unverified','page':page})
                    break
                seen_pages.add(signature)
                for e in rows:
                    if e.get('active') and not e.get('closed'):
                        old = events.get(str(e['id']))
                        if old is None or receipt['observed_at'] >= old['receipt']['observed_at']:
                            events[str(e['id'])] = {'source_id':'polymarket_global_public','event':e,'receipt':receipt}
                if not data.get('pagination',{}).get('hasMore'):
                    finished = True
                    break
            self.coverage.append({'source_id':'polymarket_global_public','query':query,'pages':page,
                'pagination_exhausted':finished,'new_open_events':len(events)-count_before})
        return list(events.values())

    def us_events(self, queries=US_QUERIES):
        events = {}
        for query in queries:
            try:
                data, receipt = self.get('polymarket_us_public','/v1/search',{'query':query,'limit':100})
            except (WarehouseError, ValueError, OSError):
                continue
            rows = data.get('events')
            if not isinstance(rows, list):
                raise ValueError('US search lacks events list')
            for e in rows:
                if nfl_us_event(e) and e.get('active') and not e.get('closed'):
                    events[str(e['id'])] = {'source_id':'polymarket_us_public','event':e,'receipt':receipt}
            self.coverage.append({'source_id':'polymarket_us_public','query':query,
                'returned_events':len(rows),'requested_limit':100,
                'pagination_exhausted':False,
                'limitation':'Search response has no pagination metadata; observed 50-result cap requires overlapping targeted searches.'})
        return list(events.values())

    def kalshi_markets(self, max_pages=30):
        data, receipt = self.get('kalshi_public','/series')
        if not isinstance(data.get('series'), list):
            raise ValueError('Kalshi series list missing')
        selected = relevant_kalshi_series(data['series'])
        markets = {}
        for series in selected:
            cursor, seen, finished, count = '', set(), False, 0
            for page in range(1,max_pages+1):
                parameters = {'series_ticker':series['ticker'],'status':'open','limit':200}
                if cursor: parameters['cursor'] = cursor
                try:
                    data, receipt = self.get('kalshi_public','/markets',parameters)
                except (WarehouseError, ValueError, OSError):
                    break
                rows = data.get('markets')
                if not isinstance(rows,list):raise ValueError('Kalshi markets list missing')
                for m in rows:
                    if m['ticker'] in markets:raise ValueError('Duplicate Kalshi market across paginated series')
                    markets[m['ticker']] = {'source_id':'kalshi_public','market':m,'series':series,'receipt':receipt}
                    count += 1
                cursor = data.get('cursor')
                if not cursor:
                    finished = True
                    break
                if cursor in seen:raise ValueError('Kalshi pagination cursor repeated')
                seen.add(cursor)
            self.coverage.append({'source_id':'kalshi_public','series':series['ticker'],
                'pages':page,'pagination_exhausted':finished,'open_markets':count})
        return list(markets.values())

    def run(self, provider='all'):
        started = time.time()
        actions = {'global':self.global_events,'us':self.us_events,'kalshi':self.kalshi_markets}
        results = {}
        for key, action in actions.items():
            if provider not in ('all',key):continue
            results[key] = action()
            print(canonical({'provider':key,'records':len(results[key]),'requests':len(self.receipts),
                             'failures':len(self.failures)}),flush=True)
        run = {'schema_version':1,'started_at':started,'completed_at':time.time(),
               'results':results,'receipts':self.receipts,'coverage':self.coverage,'failures':self.failures,
               'scope':'Public facts and market discovery. No probability calibration, player identity resolution or fantasy projection claimed.'}
        directory = self.root/'runs';directory.mkdir(exist_ok=True)
        path = directory/(str(int(started*1000))+'-'+provider+'.json')
        path.write_text(canonical(run)+'\n')
        return {'path':str(path),'counts':{k:len(v) for k,v in results.items()},
                'failed_attempts':len(self.failures),'complete_goal':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.moneyball/markets')
    parser.add_argument('--provider',choices=('all','global','us','kalshi'),default='all')
    parser.add_argument('--force',action='store_true');parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    try:
        print(canonical(Collector(args.root,force=args.force,offline=args.offline).run(args.provider)))
    except (WarehouseError,ValueError,OSError,KeyError,TypeError) as exc:
        parser.exit(2,canonical({'error':str(exc)})+'\n')


if __name__=='__main__':main()
