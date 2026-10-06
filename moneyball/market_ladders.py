"""Build auditable per-player market ladders, with source-specific assumptions.

No source count is an independent sample size. Single-sided sportsbook alts
are included only as explicit main-line-margin sensitivity scenarios.
"""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
import math
from pathlib import Path
import time

from .market_books import normalize_book
from .market_constraints import envelope,quantile_bounds,anchored_alternate_probability
from .market_distribution import devig
from .market_quotes import PlayerIndex
from .warehouse import Warehouse,canonical,timestamp

METHODS=('multiplicative','additive','power')


def summarize_curve(points):
    curve=envelope(points)
    curve['quantiles']=[quantile_bounds(curve,q) for q in (.1,.25,.5,.75,.9)] if curve['feasible'] else []
    curve['n_thresholds']=len(curve['points'])
    curve['mean']=None
    curve['mean_status']='Not identified by finite thresholds. Use an explicit prior/tail model with market_constraints.entropy_projection; no default is silently imposed.'
    return curve


def sportsbook_ladders(rows,index,*,as_of):
    groups=defaultdict(list);unmatched=[];excluded=[]
    for r in rows:
        if timestamp(r['known_at'])>as_of or timestamp(r['observed_at'])>as_of:
            raise ValueError('Sportsbook observation after cutoff')
        if r['subject_type']!='player' or r['season']!=2026 or r['period']!='regular_season':
            excluded.append({'subject':r['subject'],'reason':'Different target/horizon retained in raw artifact'});continue
        pid,match=index.match(r['subject'])
        if pid is None:unmatched.append({'name':r['subject'],'book':r['book'],'reason':match});continue
        groups[(r['book'],pid,r['stat'])].append(r)
    output=[]
    for key,rs in groups.items():
        mains=[r for r in rs if r.get('paired') and r['threshold_kind']=='main_over_under']
        alts=[r for r in rs if r['threshold_kind']=='alternate_milestone']
        base=[];failed=[]
        for r in mains:
            x=float(r['threshold'])
            if x.is_integer():
                failed.append({'threshold':x,'reason':'Integer O/U line needs push model'});continue
            probabilities=[devig(r['over_odds'],r['under_odds'],'american',method=m)['under'] for m in METHODS]
            base.append({'x':math.floor(x),'cdf_low':min(probabilities),'cdf_high':max(probabilities),
                'source_threshold':x,'quote_type':'paired_over_under',
                'over_odds':r['over_odds'],'under_odds':r['under_odds'],
                'source_sha256':r['source_sha256'],'source_url':r['source_url'],
                'observed_at':r['observed_at'],'margin_methods':list(METHODS)})
        sensitivity=list(base)
        for r in alts:
            if not mains:
                failed.append({'threshold':r['threshold'],'reason':'One-sided alternative has no same-player/stat paired anchor'});continue
            if r.get('comparator')!='ge':
                failed.append({'threshold':r['threshold'],'reason':'Unsupported alternate comparator'});continue
            anchor=min(mains,key=lambda a:abs(a['threshold']-r['threshold']))
            if canonical(r['action_rules'])!=canonical(anchor['action_rules']):
                failed.append({'threshold':r['threshold'],'reason':'Different action conditions from main-line anchor'});continue
            probs=[];valid_methods=[]
            for method in METHODS:
                try:
                    result=anchored_alternate_probability(r['yes_odds'],anchor['over_odds'],anchor['under_odds'],method)
                    probs.append(1-result['over']);valid_methods.append(method)
                except ValueError as e:failed.append({'threshold':r['threshold'],'method':method,'reason':str(e)})
            if probs:
                sensitivity.append({'x':math.ceil(r['threshold'])-1,'cdf_low':min(probs),'cdf_high':max(probs),
                    'source_threshold':r['threshold'],'quote_type':'single_sided_with_assumed_anchor_margin',
                    'yes_odds':r['yes_odds'],'source_sha256':r['source_sha256'],'source_url':r['source_url'],
                    'observed_at':r['observed_at'],'anchor_threshold':anchor['threshold'],
                    'margin_methods':valid_methods,'transfer_validated':False})
        output.append({'source_id':key[0],'player_id':key[1],'statistic':key[2],
            'kind':'sportsbook','season':2026,'horizon':'nfl_regular_season',
            'action_rules':rs[0]['action_rules'],'raw_records':len(rs),'paired_lines':len(mains),'alternate_lines':len(alts),
            'paired_only':summarize_curve(base),'with_anchored_alternates':summarize_curve(sensitivity),
            'unusable_or_failed_methods':failed,
            'interpretation':'Paired margin-method range and alternate-margin sensitivity are model assumptions, not CIs. No participation probability or whole-season mean is inferred.'})
    return output,unmatched,excluded


def cached_books(root,manifest,cutoff):
    if not Path(manifest).exists():return {},[]
    w=Warehouse(Path(root)/'markets');requests=json.loads(Path(manifest).read_text())
    wanted={r['url']:r for r in requests};output={};failures=[]
    with w.connect() as db:
        records=db.execute('SELECT r.* FROM http_cache c JOIN raw_receipts r ON r.id=c.raw_id').fetchall()
    for rec in records:
        r=dict(rec);item=wanted.get(r['url'])
        if not item or r['observed_at']>cutoff:continue
        try:
            from .market_acquisition import decode_json
            q=normalize_book({**item,'receipt':r,'data':decode_json(w.raw_bytes(r['id']))})
            output[(item['source_id'],item['market_id'])]=q
        except (ValueError,KeyError,TypeError) as e:failures.append({'raw_id':r['id'],'reason':str(e)})
    return output,failures


def exchange_ladders(rows,index,books,*,as_of):
    groups=defaultdict(list)
    for r in rows:
        if r['observed_at']>as_of:raise ValueError('Exchange observation after cutoff')
        t=r['target'];pid,_=index.match(t.get('player_name'))
        if t['kind']!='player_stat_threshold' or t['season']!=2026 or t['horizon']!='nfl_regular_season' or not pid:continue
        if r['settlement']['payout_type']!='binary_threshold_subject_to_rules':continue
        if 'provider_update_later_than_observation' in r['quote']['flags']:continue
        group=r['series_ticker'] if r['source_id']=='kalshi_public' else r['event_id']
        groups[(r['source_id'],pid,t['statistic'],group)].append(r)
    result=[]
    for key,rs in groups.items():
        points=[];depth_points=[];updated=0
        for r in rs:
            b=books.get((r['source_id'],r['market_id']))
            if b:updated+=1
            q=b['top'] if b else r['quote']
            d=b['size_sensitivity']['100'] if b else None
            for quote,target,basis in [(q,points,'orderbook_top' if b else 'catalog_bbo_unverified_by_book'),
                                       (d,depth_points,'100_share_depth_sensitivity')]:
                if quote is None or not quote['two_sided']:continue
                target.append({'x':r['target']['cdf_at_integer'],'cdf_low':1-quote['yes_ask'],
                    'cdf_high':1-quote['yes_bid'],'market_id':r['market_id'],
                    'raw_id':b['raw_id'] if b else r['raw_id'],'raw_sha256':b['raw_sha256'] if b else r['raw_sha256'],
                    'source_url':b['url'] if b else r['api_url'],'observed_at':b['observed_at'] if b else r['observed_at'],
                    'basis':basis})
        result.append({'source_id':key[0],'player_id':key[1],'statistic':key[2],'event_or_series_id':key[3],
            'kind':'exchange','season':2026,'horizon':'nfl_regular_season',
            'market_records':len(rs),'orderbooks_verified':updated,
            'settlement':rs[0]['settlement'],'top_quotes':summarize_curve(points),
            'depth_100_share_sensitivity':summarize_curve(depth_points),
            'interpretation':'Within event/series restrictions only. Other event lines, venues, fees, action/cancellation rules and depth remain separate.'})
    return result


def make_index(root,*,as_of):
    root=Path(root);players=json.loads((root/'draft'/'player-universe.json').read_text())['players'];aliases=[]
    for p in players:
        for name in p.get('observed_name_aliases',[]):
            provenance=p.get('identity_provenance') or p['adp_provenance']
            if timestamp(provenance['available_at'])<=as_of:
                aliases.append({'player_id':p['player_id'],'name':name,'provenance':provenance})
    manual=root/'research'/'reviewed-player-name-aliases.json'
    if manual.exists():
        aliases.extend(a for a in json.loads(manual.read_text()) if timestamp(a['provenance']['available_at'])<=as_of)
    contracts=root/'research'/'dossier400-contracts'/'top400-contracts.json'
    if contracts.exists():
        data=json.loads(contracts.read_text())
        if data['built_at']<=as_of:
            for p in data['players']:
                for r in p.get('all_contract_summary_records',[]):
                    aliases.append({'player_id':p['player_id'],'name':r['player'],
                        'provenance':{'artifact':str(contracts),'available_at':data['built_at'],'identity_match':p.get('identity_match')}})
    return PlayerIndex(players,aliases)


def build(root='.moneyball'):
    root=Path(root);cutoff=time.time();index=make_index(root,as_of=cutoff)
    cohort=json.loads((root/'draft'/'top400-cohort.json').read_text());ids={p['player_id'] for p in cohort['players']}
    path=root/'markets'/'normalized'/'1788881122523-all-quotes.jsonl'
    quotes=[json.loads(x) for x in path.open()]
    directory=root/'research'/'dossier400-sportsbooks'
    bpaths=[directory/'normalized-season-props.json']+sorted(directory.glob('normalized-*-season-props.json'))
    book_rows=[];seen=set()
    for bpath in bpaths:
        bs=json.loads(bpath.read_text())
        for r in bs['records']:
            identity=(r['book'],r['source_sha256'],r['subject'],r['stat'],r['threshold'],r['threshold_kind'])
            if identity in seen:continue
            seen.add(identity);book_rows.append(r)
    book_ladders,unmatched,excluded=sportsbook_ladders(book_rows,index,as_of=cutoff)
    books,failures=cached_books(root,root/'markets'/'top400-orderbook-requests.json',cutoff)
    exchanges=exchange_ladders(quotes,index,books,as_of=cutoff)
    ladders=[x for x in book_ladders+exchanges if x['player_id'] in ids]
    result={'schema_version':1,'built_at':time.time(),'information_cutoff':cutoff,
        'cohort_sha256':hashlib.sha256((root/'draft'/'top400-cohort.json').read_bytes()).hexdigest(),
        'sportsbook_inputs':[{ 'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in bpaths],
        'exchange_input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'ladders':ladders,'unmatched_sportsbook_subjects':unmatched,'excluded_other_targets':excluded,
        'orderbook_parse_failures':failures,'verified_orderbooks':len(books),
        'coverage':{'players':len({x['player_id'] for x in ladders}),
            'sportsbook_players':len({x['player_id'] for x in book_ladders if x['player_id'] in ids}),
            'by_source':dict(Counter(x['source_id'] for x in ladders)),
            'inconsistent_exchange_ladders':sum(not x['top_quotes']['feasible'] for x in exchanges),
            'inconsistent_sportsbook_alt_ladders':sum(not x['with_anchored_alternates']['feasible'] for x in book_ladders)},
        'complete_distributions':False,'confidence_intervals_estimated':False,
        'next_dependency':'Explicit historical/professional prior scenarios and tail sensitivity before deriving whole-season fantasy means, variance and skew.'}
    dest=root/'markets'/'derived';dest.mkdir(exist_ok=True)
    p=dest/'player-ladders.json';p.write_text(canonical(result)+'\n')
    return {'path':str(p),'coverage':result['coverage'],'verified_orderbooks':len(books),'orderbook_parse_failures':len(failures)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default='.moneyball');a=p.parse_args()
    print(canonical(build(a.root)))
