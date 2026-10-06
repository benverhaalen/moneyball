"""Normalize a saved nflverse/OTC contract snapshot without pricing player roles.

The current bulk source is Parquet; CSV is stale. Only reading Parquet needs the
already-installed pyarrow dependency. Transformation and tests use stdlib.
"""
import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import json
import math
from pathlib import Path
import time

from .warehouse import canonical, timestamp

SUMMARY_FIELDS = ('player','position','team','is_active','year_signed','years','value','apy',
                  'guaranteed','apy_cap_pct','inflated_value','inflated_apy',
                  'inflated_guaranteed','player_page','otc_id','gsis_id')
MONEY_FIELDS = ('value','apy','guaranteed','inflated_value','inflated_apy','inflated_guaranteed')


def _id(value):
    return str(value).strip() if value is not None else None


def _unique(records):
    return list({canonical(r):copy.deepcopy(r) for r in records}.values())


def normalize_contracts(rows, universe, cohort, receipt, *, cutoff):
    """Join exact observed OTC or GSIS IDs; conflicting IDs fail loudly.

Names only generate review candidates. Contract length never becomes an expiry
date; annual cap rows can contain void years and old-team charges.
"""
    cutoff = timestamp(cutoff)
    if timestamp(receipt['observed_at']) > cutoff or timestamp(receipt['available_at']) > cutoff:
        raise ValueError('Contract snapshot was not observed by the cutoff')
    if timestamp(universe['information_cutoff']) > cutoff or timestamp(universe['built_at']) > cutoff:
        raise ValueError('Identity universe was not knowable by cutoff')
    cohort_created = cohort.get('built_at',cohort.get('created_at'))
    if cohort_created is None or timestamp(cohort_created) > cutoff:
        raise ValueError('Cohort was not knowable by cutoff')
    all_players = {str(p['player_id']):p for p in universe['players']}
    if len(all_players) != len(universe['players']):raise ValueError('Duplicate universe player IDs')
    wanted = [str(p['player_id']) for p in cohort['players']]
    if len(wanted) != len(set(wanted)):raise ValueError('Duplicate cohort player IDs')
    by_otc, by_gsis, by_name = defaultdict(list), defaultdict(set), defaultdict(set)
    for row in rows:
        if not _id(row.get('otc_id')) or not row.get('player') or not isinstance(row.get('is_active'),bool):
            raise ValueError('Invalid contract identity/status')
        for field in MONEY_FIELDS:
            value = row.get(field)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):
                raise ValueError('Nonfinite contract economics')
        otc, gsis = _id(row['otc_id']), _id(row.get('gsis_id'))
        by_otc[otc].append(row)
        if gsis:by_gsis[gsis].add(otc)
        by_name[row['player']].add(otc)
    normalized, unmatched, crosswalk, count = [], [], [], Counter()
    for pid in wanted:
        if pid not in all_players:raise ValueError('Cohort player missing from identity universe')
        p = all_players[pid]
        otc = _id((p.get('external_ids') or {}).get('otc_id'))
        gsis = _id(p.get('gsis_id'))
        choices = by_gsis.get(gsis,set()) if gsis else set()
        original_choices = set(choices)
        if len(choices)>1:
            position_choices = {i for i in choices if p['position'] in {r['position'] for r in by_otc[i]}}
            if len(position_choices)==1:choices=position_choices
        if otc in by_otc:
            source_gsis = {_id(r.get('gsis_id')) for r in by_otc[otc] if r.get('gsis_id')}
            if gsis and source_gsis and gsis not in source_gsis:
                raise ValueError('Contract OTC identity reports a conflicting GSIS ID for '+pid)
            if choices and otc not in choices:
                raise ValueError('Observed OTC and GSIS identifiers conflict for '+pid)
            method = 'exact_observed_otc_id'
        elif len(choices) == 1:
            otc = next(iter(choices))
            method = ('exact_observed_gsis_id_with_position_disambiguation' if len(original_choices)>1
                      else 'exact_observed_gsis_id_whitespace_normalized')
        elif len(choices) > 1:
            raise ValueError('GSIS ID maps to multiple OTC identities for '+pid)
        else:
            names = set(p.get('observed_name_aliases') or []) | {p['name']}
            candidates = set().union(*(by_name.get(n,set()) for n in names))
            unmatched.append({'player_id':pid,'name':p['name'],'position':p['position'],
                'gsis_id':gsis,'otc_id':otc,'status':'no_exact_id_match',
                'candidate_identities_not_joined':[{'otc_id':x,'source_name':by_otc[x][0]['player'],
                    'source_positions':sorted({r['position'] for r in by_otc[x]}),
                    'source_gsis_ids':sorted({r['gsis_id'] for r in by_otc[x] if r.get('gsis_id')}),
                    'source_url':by_otc[x][0]['player_page']} for x in sorted(candidates)]})
            count['unmatched'] += 1
            continue
        rs = by_otc[otc]
        active = _unique([{k:r.get(k) for k in SUMMARY_FIELDS} for r in rs if r['is_active']])
        seasons = _unique([s for r in rs for s in (r.get('season_history') or [])
                           if s and any(v is not None for v in s.values())])
        histories = _unique([s for r in rs for s in (r.get('contract_history') or [])
                             if s and any(v is not None for v in s.values())])
        future = {str(y):[s for s in seasons if str(s.get('year')) == str(y)] for y in (2026,2027,2028)}
        count['matched'] += 1
        count['matched_with_active_contract'] += bool(active)
        count['multiple_active_contract_records'] += len(active)>1
        for y,v in future.items():count['annual_rows_'+y+'_covered_players'] += bool(v)
        link = {'player_id':pid,'name':p['name'],'otc_id':otc,'gsis_id':gsis,
                'source_gsis_ids':sorted({r['gsis_id'] for r in rs if r.get('gsis_id')}),
                'identity_match':method,'identity_provenance':copy.deepcopy(p.get('identity_provenance')),
                'identity_universe_method':copy.deepcopy(p.get('identity_match')),
                'discarded_otc_identity_conflicts':[{'otc_id':i,'source_name':by_otc[i][0]['player'],
                    'positions':sorted({r['position'] for r in by_otc[i]})} for i in sorted(original_choices-{otc})]}
        crosswalk.append(link)
        normalized.append({**link,'position':p['position'],'current_reported_nfl_team':p.get('team'),
            'source_url':rs[0]['player_page'],'source_id':'nflverse_contracts_otc',
            'available_at':receipt['available_at'],'observed_at':receipt['observed_at'],
            'source_sha256':receipt['sha256'],'money_unit':'USD millions; ratios are proportions',
            'all_contract_summary_records':_unique([{k:r.get(k) for k in SUMMARY_FIELDS} for r in rs]),
            'reported_active_contracts':active,'reported_active_contract_count':len(active),
            'annual_source_rows':seasons,'annual_rows_2026_2028':future,'contract_history':histories,
            'contract_expiration_year':None,'free_agency_year':None,'free_agency_type':None,
            'fifth_year_option_status':None,'void_years':None,'guarantee_vesting_triggers':None,
            'release_trade_dead_cap_scenarios':None,'release_trade_cap_savings_scenarios':None,
            'remaining_guaranteed_cash':None,
            'interpretation':'Observed reported contract economics only. Annual rows may include void years, old-team charges and historical contracts; they do not establish retention, role, free-agency, or career duration.',
            'prohibited_inferences':['year_signed+years equals expiry','max annual row equals service end',
                'guaranteed is remaining future guarantee','guaranteed_salary is only base salary',
                'option_bonus proves exercised fifth-year option','no active contract proves retirement'],
            'missing_fields_are_zero':False,'automatic_role_probability_adjustment':None})
    return {'schema_version':1,'source_id':'nflverse_contracts_otc','built_at':time.time(),'information_cutoff':cutoff,
        'source_receipt':receipt,'cohort_count':len(wanted),'coverage':dict(count),
        'source_row_count':len(rows),'source_unique_otc_ids':len(by_otc),
        'players':normalized,'unmatched':unmatched,'crosswalk':crosswalk,
        'license_status':'nflverse repository declares CC BY 4.0; upstream OTC systematic-collection and derivative-use terms remain separately unresolved',
        'automated_direct_otc_enrichment_enabled':False,
        'scope':'Descriptive contract research. No fitted retention, departure, retirement, injury, production, or championship model.'}


def build(root='.moneyball'):
    root=Path(root);base=root/'research'/'dossier400-contracts'
    receipt=json.loads((base/'contracts-receipt.json').read_text())
    raw=base/'historical_contracts.parquet'
    if hashlib.sha256(raw.read_bytes()).hexdigest()!=receipt['sha256']:
        raise ValueError('Contract raw snapshot hash mismatch')
    import pyarrow.parquet as pq
    table=pq.read_table(raw)
    cohort_path=root/'draft'/'top400-cohort.json'; universe_path=root/'draft'/'player-universe.json'
    result=normalize_contracts(table.to_pylist(),json.loads(universe_path.read_text()),
        json.loads(cohort_path.read_text()),receipt,cutoff=time.time())
    result['source_stated_updated']=(table.schema.metadata or {}).get(b'nflverse_timestamp',b'').decode()
    result['cohort_sha256']=hashlib.sha256(cohort_path.read_bytes()).hexdigest()
    result['identity_universe_sha256']=hashlib.sha256(universe_path.read_bytes()).hexdigest()
    result['code_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # These are independent source artifacts, deliberately not main dossier writes.
    for filename,obj in [('top400-contracts.json',result),('unmatched.json',result['unmatched']),
                         ('crosswalk.json',result['crosswalk'])]:
        (base/filename).write_text(canonical(obj)+'\n')
    return {k:v for k,v in result.items() if k not in ('players','crosswalk','unmatched')}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir',default='.moneyball')
    args=p.parse_args();print(canonical(build(args.data_dir)))


if __name__=='__main__':main()
