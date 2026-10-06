"""Draft research membership must not depend on having a model forecast.

Sleeper's observed dynasty_2qb ADP defines research coverage, not pick value.
Cross-source IDs are reconciled with observed ID fields or name/DOB/position;
unmatched players stay in the universe with an explicit missing crosswalk.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .store import Store
from .warehouse import Warehouse, canonical, leakage_check, timestamp

OFFENSE = {'QB','RB','WR','TE'}


def clean_id(value):
    return str(value).strip() if value not in (None,'') else None


def name_key(value):
    return re.sub('[^a-z0-9]', '', str(value or '').lower())


def names(row):
    result = [row.get('full_name'), row.get('display_name')]
    for first in ('first_name','common_first_name','football_name'):
        if row.get(first) and row.get('last_name'):
            result.append(row[first]+' '+row['last_name'])
    if row.get('suffix'):
        result += [n+' '+row['suffix'] for n in list(result) if n and not n.endswith(row['suffix'])]
    return sorted({x for x in result if x})


def make_universe(adp_rows, metadata, nfl_players, *, metadata_observed_at, as_of):
    cutoff=timestamp(as_of)
    if timestamp(metadata_observed_at)>cutoff:
        raise ValueError('Player cache was observed after cutoff')
    leakage_check(list(adp_rows)+list(nfl_players),cutoff,raise_on_error=True)
    cross=defaultdict(set); birth=defaultdict(set); by_gsis={}
    for r in nfl_players:
        gid=clean_id(r.get('gsis_id'))
        if not gid or not r.get('identity_verified'):continue
        if gid in by_gsis:raise ValueError('Duplicate NFL GSIS identity')
        by_gsis[gid]=r
        for key in ('espn_id','gsis_id'):
            if clean_id(r.get(key)):cross[(key,clean_id(r[key]))].add(gid)
        if r.get('birth_date') and r.get('position'):
            for name in names(r):birth[(name_key(name),r['birth_date'],r['position'])].add(gid)
    rows=[];seen=set()
    for r in adp_rows:
        if r.get('week') is not None or r.get('adp_type')!='adp_dynasty_2qb':continue
        pid=clean_id(r.get('player_id'))
        if not pid or pid in seen:raise ValueError('Duplicate or missing ADP player ID')
        seen.add(pid)
        p=metadata.get(pid,{})
        eligible=set(p.get('fantasy_positions') or r.get('fantasy_positions') or [r.get('position')])
        if not eligible & OFFENSE:continue
        value=r.get('adp')
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Invalid ADP')
        position=p.get('position') or r.get('position')
        if position not in OFFENSE:position=next(x for x in ('QB','RB','WR','TE') if x in eligible)
        ns=names(p) or [r['name']]
        matches=set();basis=[]
        for key in ('gsis_id','espn_id'):
            val=clean_id(p.get(key))
            if val and (key,val) in cross:
                matches |= cross[(key,val)];basis.append('exact_'+key+'_whitespace_normalized')
        if p.get('birth_date'):
            for name in ns:
                matched=birth.get((name_key(name),p['birth_date'],position),set())
                if matched:matches |= matched;basis.append('observed_name_birth_date_position')
        if len(matches)>1:raise ValueError('Conflicting NFL identity evidence for '+pid)
        gid=next(iter(matches)) if matches else None
        nfl=by_gsis.get(gid,{})
        rows.append({'player_id':pid,'name':p.get('full_name') or r['name'],
            'position':position,'fantasy_positions':sorted(eligible),'team':p.get('team') or r.get('team'),
            'age':p.get('age'),'birth_date':p.get('birth_date'),'years_exp':p.get('years_exp'),
            'gsis_id':gid,'adp':value,'adp_type':r['adp_type'],
            'adp_as_of':r['_provenance']['available_at'],'adp_provenance':r['_provenance'],
            'metadata_observed_at':metadata_observed_at,
            'identity_match':sorted(set(basis)) if gid else ['unmatched_preserved'],
            'identity_provenance':nfl.get('_provenance'),
            'observed_name_aliases':sorted(set(ns+names(nfl))),
            'external_ids':{key:clean_id(nfl.get(key)) for key in ('otc_id','espn_id','pfr_id','pff_id')},
            'research_membership_basis':'Observed platform dynasty_2qb ADP, independent of forecast or historical coverage.'})
    rows.sort(key=lambda r:(r['adp'],r['player_id']))
    for i,r in enumerate(rows,1):r['cohort_rank']=i
    return rows


def build(root='.moneyball',*,as_of=None,size=400,alias=None):
    root=Path(root);cutoff=timestamp(as_of);w=Warehouse(root/'lab');s=Store(root)
    cache=s.cached('players/nfl')
    if not cache:raise ValueError('Full Sleeper player cache required')
    snapshot=s.latest(s.resolve_alias(alias))
    if not snapshot or snapshot['created']>cutoff:raise ValueError('League identity not available at cutoff')
    season=int(snapshot['data']['league']['season'])
    adp=w.query('adp',cutoff=cutoff,source_id='sleeper_rotowire',seasons=[season])
    players=make_universe(adp,cache['data'],w.query('players',cutoff=cutoff),
        metadata_observed_at=cache['fetched'],as_of=cutoff)
    if len(players)<size:raise ValueError('Observed universe is smaller than requested cohort')
    directory=root/'draft';directory.mkdir(exist_ok=True)
    previous_path=directory/'top400-cohort.json'
    previous=json.loads(previous_path.read_text()) if previous_path.exists() else None
    old_ids={p['player_id'] for p in (previous or {}).get('players',[])}
    new_ids={p['player_id'] for p in players[:size]}
    body={'schema_version':2,'built_at':time.time(),'information_cutoff':cutoff,
        'season':season,'players':players[:size],'universe_size':len(players),
        'definition':'First '+str(size)+' offensive-eligible players by observed Sleeper adp_dynasty_2qb; forecast coverage is not an inclusion criterion. Ties use player ID.',
        'source_population_caveat':'ADP sample size, actual draft timestamp distribution and configuration equivalence remain unverified; ADP is an availability reference only.',
        'metadata_source_hash':cache['hash'],'metadata_observed_at':cache['fetched'],
        'adp_as_of':max(r['adp_as_of'] for r in players),
        'reconciliation':{'added_ids':sorted(new_ids-old_ids),'removed_ids':sorted(old_ids-new_ids),
                         'missing_gsis':sum(p['gsis_id'] is None for p in players[:size])}}
    if previous:
        previous_bytes=previous_path.read_bytes()
        archive=directory/'cohort-history';archive.mkdir(exist_ok=True)
        (archive/(hashlib.sha256(previous_bytes).hexdigest()+'.json')).write_bytes(previous_bytes)
    (directory/'player-universe.json').write_text(canonical({**body,'players':players})+'\n')
    previous_path.write_text(canonical(body)+'\n')
    return {'path':str(previous_path),'players':size,'universe_size':len(players),
            'gsis_matched':sum(p['gsis_id'] is not None for p in players[:size]),
            'adp_as_of':body['adp_as_of'],'reconciliation':body['reconciliation']}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',default='.moneyball');p.add_argument('--as-of');p.add_argument('--league',dest='alias')
    a=p.parse_args()
    try:print(canonical(build(a.root,as_of=a.as_of,alias=a.alias)))
    except (ValueError,KeyError,OSError) as exc:p.exit(2,canonical({'error':str(exc)})+'\n')


if __name__=='__main__':main()
