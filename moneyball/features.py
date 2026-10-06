"""Audited candidate features and descriptive stability, separate from forecasting.

Exact identifiers only. Missing source observations remain missing. These
candidate measurements never change the selected points model or its holdout.
"""
from collections import Counter, defaultdict
import csv
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time

from .identity import canonical_team
from .modeling import measure_reliability
from .warehouse import Warehouse, WarehouseError, ValidationError, canonical, sha256, timestamp, leakage_check


RECEIVERS=('RB','WR','TE')
RUNNERS=('QB','RB')
ALL_POSITIONS=('QB','RB','WR','TE')

def _definition(source,field,positions=ALL_POSITIONS,*,denominator=None,scale=1,unit='per observed week',caveat=''):
    return {'source':source,'field':field,'denominator':denominator,'scale':scale,'positions':list(positions),
            'unit':unit,'caveat':caveat,'missing_policy':'null if source/numerator missing or denominator nonpositive; no zero imputation'}


DEFINITIONS={
    'targets':_definition('weekly','targets',RECEIVERS),
    'carries':_definition('weekly','carries',RUNNERS),
    'pass_attempts':_definition('weekly','attempts',('QB',)),
    'target_share':_definition('weekly','target_share',RECEIVERS,unit='source share',caveat='Source-reported team share; denominator not independently reconstructed.'),
    'air_yards_share':_definition('weekly','air_yards_share',RECEIVERS,unit='source signed share',caveat='Signed air yards can produce shares outside [0,1]; no clipping.'),
    'receiving_air_yards_per_target':_definition('weekly','receiving_air_yards',RECEIVERS,denominator='targets',unit='yards per target'),
    'receiving_yards_per_target':_definition('weekly','receiving_yards',RECEIVERS,denominator='targets',unit='yards per target'),
    'yac_per_reception':_definition('weekly','receiving_yards_after_catch',RECEIVERS,denominator='receptions',unit='yards per reception'),
    'catch_rate':_definition('weekly','receptions',RECEIVERS,denominator='targets',unit='receptions per target'),
    'receiving_td_rate':_definition('weekly','receiving_tds',RECEIVERS,denominator='targets',unit='TD per target'),
    'rushing_yards_per_carry':_definition('weekly','rushing_yards',RUNNERS,denominator='carries',unit='yards per carry'),
    'rushing_td_rate':_definition('weekly','rushing_tds',RUNNERS,denominator='carries',unit='TD per carry'),
    'completion_rate':_definition('weekly','completions',('QB',),denominator='attempts',unit='completions per attempt'),
    'passing_yards_per_attempt':_definition('weekly','passing_yards',('QB',),denominator='attempts',unit='yards per attempt'),
    'passing_td_rate':_definition('weekly','passing_tds',('QB',),denominator='attempts',unit='TD per attempt'),
    'passing_interception_rate':_definition('weekly','passing_interceptions',('QB',),denominator='attempts',unit='interceptions per attempt'),
    'offense_snaps':_definition('snap_counts','offense_snaps'),
    'offense_snap_share':_definition('snap_counts','offense_pct',unit='source fraction of team offensive snaps'),
    'pbp_target_share':_definition('opportunities','target_share_of_eligible_pass_attempts',RECEIVERS,unit='targets per eligible team pass attempt',caveat='Includes unattributed passes in denominator; excludes spikes/sacks/no-plays/two-point attempts.'),
    'pbp_rush_share':_definition('opportunities','rush_share_excluding_kneels',RUNNERS,unit='fraction of team non-kneel rushes'),
    'red_zone_targets':_definition('opportunities','red_zone_targets',RECEIVERS,caveat='Pre-snap yardline_100 between 0 and 20 inclusive; direct counts.'),
    'red_zone_rush_attempts':_definition('opportunities','red_zone_rush_attempts',RUNNERS,caveat='Pre-snap yardline_100 between 0 and 20 inclusive; kneels excluded.'),
    'ngs_time_to_throw':_definition('ngs_passing','avg_time_to_throw',('QB',),unit='source mean seconds'),
    'ngs_intended_air_yards':_definition('ngs_passing','avg_intended_air_yards',('QB',),unit='source mean yards'),
    'ngs_completion_above_expectation':_definition('ngs_passing','completion_percentage_above_expectation',('QB',),unit='percentage points; upstream model output'),
    'ngs_expected_completion_rate':_definition('ngs_passing','expected_completion_percentage',('QB',),scale=.01,unit='expected probability; upstream model output'),
    'ngs_receiver_separation':_definition('ngs_receiving','avg_separation',RECEIVERS,unit='source mean yards'),
    'ngs_yac_above_expectation':_definition('ngs_receiving','avg_yac_above_expectation',RECEIVERS,unit='yards; upstream model output'),
    'ngs_expected_yac':_definition('ngs_receiving','avg_expected_yac',RECEIVERS,unit='yards; upstream model output'),
    'ngs_rush_yards_above_expectation_per_attempt':_definition('ngs_rushing','rush_yards_over_expected_per_att',RUNNERS,unit='yards per qualified attempt; upstream model output'),
    'ngs_rush_fraction_above_expectation':_definition('ngs_rushing','rush_pct_over_expected',RUNNERS,unit='source fraction; upstream model output'),
    'ngs_expected_rush_yards_per_attempt':_definition('ngs_rushing','expected_rush_yards',RUNNERS,denominator='rush_attempts',unit='source expected total divided by reported rush attempts',caveat='Tracking-qualified attempts may differ from reported attempts; denominator mismatch must be audited.'),
}
for _name,_spec in DEFINITIONS.items():
    if _spec['source'].startswith('ngs_'):
        _spec['caveat']=(_spec['caveat']+' Qualification thresholds make missingness selective; week 0 summaries excluded.').strip()


def _num(value):
    if value is None or isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError):return None
    return value if math.isfinite(value) else None


def _period(row):return str(row.get('season_type') or row.get('game_type') or 'REG').upper()


def _index(rows,key):
    result={};ambiguous=set()
    for row in rows:
        item=key(row)
        if item is None:continue
        if item in result:ambiguous.add(item)
        else:result[item]=row
    for item in ambiguous:result.pop(item,None)
    return result,ambiguous


def _artifact_opportunities(warehouse,seasons,cutoff):
    """Read the materialized 44k player-game rows, never replay 389k PBP plays."""
    output=[];provenance={};audit={'artifacts':[],'unavailable_artifacts':[]}
    for season in seasons:
        with warehouse.connect() as db:
            versions=db.execute('SELECT * FROM artifacts WHERE name=? ORDER BY id DESC',['opportunities_'+str(season)]).fetchall()
        chosen=None
        for artifact in versions:
            batches=[warehouse.batch(x) for x in json.loads(artifact['input_batches'])]
            if not batches or any(b['status']!='published' or b['available_at']>cutoff for b in batches):continue
            if sha256(artifact['body'].encode())!=artifact['sha256']:
                raise WarehouseError('Opportunity artifact hash mismatch: '+str(artifact['id']))
            body=json.loads(artifact['body'])
            if sorted(body['input_batches'])!=sorted(b['batch_id'] for b in batches):
                raise ValidationError('Opportunity artifact input lineage mismatch')
            chosen=(artifact,body,batches);break
        if not chosen:
            audit['unavailable_artifacts'].append(season);continue
        artifact,body,batches=chosen
        sources=[]
        for b in batches:
            source={k:b[k] for k in ('source_id','dataset','batch_id','raw_id','available_at','observed_at','vintage_verified','content_sha256')}
            source.update(historical_vintage_unknown=not b['vintage_verified'],artifact_id=artifact['id'],
                          artifact_sha256=artifact['sha256'],derived_at=artifact['created_at'])
            sources.append(source)
        for row in body['rows']:
            if int(row['season'])!=season:raise ValidationError('Opportunity season disagrees with artifact name')
            output.append(row)
            provenance[(row['player_id'],row['game_id'])]=sources
        audit['artifacts'].append({'artifact_id':artifact['id'],'season':season,'row_count':len(body['rows']),
                                   'input_batches':[b['batch_id'] for b in batches]})
    return output,provenance,audit


def _aggregate_provenance(sources):
    unique={canonical(s):s for s in sources}
    sources=list(unique.values())
    return {'source_id':'derived_candidate_features','dataset':'candidate_features','sources':sources,
        'available_at':max(s['available_at'] for s in sources),'observed_at':max(s['observed_at'] for s in sources),
        'vintage_verified':all(s.get('vintage_verified',False) for s in sources),
        'historical_vintage_unknown':any(not s.get('vintage_verified',False) for s in sources),
        'input_batches':sorted({s['batch_id'] for s in sources}),'purpose':'exploratory'}


def enrich_features(warehouse=None, *, seasons=range(2018,2026), cutoff=None):
    warehouse=warehouse or Warehouse();seasons=sorted(set(int(x) for x in seasons));limit=timestamp(cutoff)
    query=lambda ds,**kw:warehouse.query(ds,cutoff=limit,purpose='exploratory',**kw)
    weekly=[r for r in query('weekly',seasons=seasons,positions=ALL_POSITIONS,source_id='nflverse_weekly')
            if _period(r)=='REG' and r.get('identity_verified',True)]
    unique_weekly,duplicate_weekly=_index(weekly,lambda r:(r['player_id'],r['season'],r['week']))
    if duplicate_weekly:raise ValidationError('Duplicate base player-season-week observations')
    weekly=list(unique_weekly.values())
    snaps,snap_ambiguous=_index(query('snap_counts',seasons=seasons),lambda r:(r.get('pfr_player_id'),r.get('game_id')) if r.get('pfr_player_id') and r.get('game_id') else None)
    rosters=query('rosters',seasons=seasons)
    players=query('players')
    seasonal=defaultdict(list);global_ids=defaultdict(list)
    for r in rosters:
        if r.get('gsis_id') and r.get('pfr_id'):
            seasonal[(r['season'],r['gsis_id'])].append(r)
    for r in players:
        if r.get('gsis_id') and r.get('pfr_id'):global_ids[r['gsis_id']].append(r)
    opportunities,opp_provenance,artifact_audit=_artifact_opportunities(warehouse,seasons,limit)
    opp_index,opp_ambiguous=_index(opportunities,lambda r:(r['player_id'],r['game_id']))
    ngs={};ngs_audit={}
    for kind in ('passing','receiving','rushing'):
        dataset='ngs_'+kind;all_rows=query(dataset,seasons=seasons)
        candidates=[r for r in all_rows if r.get('week',0)>0 and _period(r)=='REG']
        index,ambiguous=_index(candidates,lambda r:(r['player_id'],r['season'],r['week']))
        ngs[dataset]=index;ngs_audit[dataset]={'source_rows':len(all_rows),'week_zero_excluded':sum(r.get('week')==0 for r in all_rows),
            'nonregular_excluded':sum(r.get('week',0)>0 and _period(r)!='REG' for r in all_rows),'ambiguous_keys':len(ambiguous)}
    joins=Counter();output=[];all_batches=set();metric_status={name:Counter() for name in DEFINITIONS}
    for base in weekly:
        pid,season,week=base['player_id'],base['season'],base['week'];game=base.get('game_id')
        sources=[base['_provenance']];namespaces={'weekly':base.get('stats') or base};join_status={}
        crosswalk=seasonal.get((season,pid)) or global_ids.get(pid) or []
        pfrs={r['pfr_id'] for r in crosswalk}
        snap=None
        if len(pfrs)==1 and game:
            pfr=next(iter(pfrs));snap=snaps.get((pfr,game))
            join_status['snap_counts']='matched' if snap else 'missing_game_record'
        elif len(pfrs)>1:join_status['snap_counts']='ambiguous_exact_crosswalk'
        else:join_status['snap_counts']='missing_exact_crosswalk_or_game'
        if snap:
            namespaces['snap_counts']=snap;sources.append(snap['_provenance'])
            sources.extend(r['_provenance'] for r in crosswalk)
        opp=opp_index.get((pid,game))
        join_status['opportunities']='matched' if opp else 'missing_or_ambiguous_game_record'
        if opp:
            namespaces['opportunities']=opp;sources.extend(opp_provenance[(pid,game)])
        for dataset,index in ngs.items():
            found=index.get((pid,season,week))
            if found and canonical_team(found.get('team'))!=canonical_team(base.get('team')):
                join_status[dataset]='team_conflict';found=None
            else:join_status[dataset]='matched' if found else 'not_qualified_missing_or_ambiguous'
            if found:namespaces[dataset]=found;sources.append(found['_provenance'])
        metrics={};denominators={}
        for name,spec in DEFINITIONS.items():
            value=None;source=namespaces.get(spec['source']);den=None
            if base['position'] not in spec['positions']:status='position_not_applicable'
            elif source is None:status='source_observation_missing'
            else:
                value=_num(source.get(spec['field']))
                status='observed' if value is not None else 'numerator_missing'
                if spec['denominator']:
                    den=_num(source.get(spec['denominator']))
                    if den is None:status='denominator_missing'
                    elif den<=0:status='denominator_nonpositive'
                    value=value/den if value is not None and den is not None and den>0 else None
                if value is not None:value*=spec['scale']
            if spec['denominator']:denominators[name]=den
            metrics[name]=value
            metric_status[name][status]+=1
        provenance=_aggregate_provenance(sources);all_batches.update(provenance['input_batches'])
        result={k:base.get(k) for k in ('player_id','season','week','season_type','position','team','game_id','stats')}
        result.update(metrics=metrics,metric_denominators=denominators,feature_join_status=join_status,_provenance=provenance)
        output.append(result)
        joins.update(source+':'+status for source,status in join_status.items())
    leakage_check(output,limit,raise_on_error=True)
    coverage={}
    for metric,spec in DEFINITIONS.items():
        eligible=[r for r in output if r['position'] in spec['positions']]
        observed=[r for r in eligible if r['metrics'][metric] is not None]
        values=[r['metrics'][metric] for r in observed]
        coverage[metric]={'eligible_observed_box_score_rows':len(eligible),'observed_feature_rows':len(observed),
            'missing_feature_rows':len(eligible)-len(observed),'zero_rows':sum(v==0 for v in values),
            'observation_status_counts':dict(metric_status[metric]),
            'minimum':min(values) if values else None,'maximum':max(values) if values else None,
            'by_season':{str(s):{'eligible':sum(r['season']==s for r in eligible),'observed':sum(r['season']==s for r in observed)} for s in seasons},
            'by_position':{p:{'eligible':sum(r['position']==p for r in eligible),'observed':sum(r['position']==p for r in observed)} for p in spec['positions']}}
    return {'rows':output,'input_batches':sorted(all_batches),'definitions':DEFINITIONS,'cutoff':limit,
        'coverage':{'row_count':len(output),'seasons':seasons,'metrics':coverage,'joins':dict(joins),
            'ambiguous_snap_keys':len(snap_ambiguous),'ambiguous_opportunity_keys':len(opp_ambiguous),
            'ngs_source_audit':ngs_audit,'opportunity_artifacts':artifact_audit,
            'candidate_count':len(DEFINITIONS),'evidence_status':'descriptive_only_no_model_selection',
            'omissions':['FTN/participation not transformed to route/pressure player features: player-role attribution requires a separately audited play join.',
                'Depth/injury report fields not converted to numerical health or workload labels.',
                'No expected fantasy score weights invented. NGS expected fields are upstream model outputs, not established skill.'],
            'population':'Observed regular-season QB/RB/WR/TE box-score rows; not the complete roster-week at-risk population.'}}


def _write(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return str(path)


def measure_features(enriched, *, warehouse=None, windows=(2,4,6), gap=1, output_dir=None):
    """Report all fixed candidates; no model fit, ranking, or holdout reselection."""
    warehouse=warehouse or Warehouse();definitions=enriched['definitions']
    specification={'definitions':definitions,'windows':list(windows),'gap':gap,'seasons':enriched['coverage']['seasons'],
        'input_batches':enriched['input_batches'],'information_cutoff':enriched['cutoff'],
        'evidence_status':'descriptive_historical_association','selection_rule':'Report every listed candidate; no feature or model promoted by this analysis.',
        'holdout_status':'2025 already inspected; not confirmatory. A future frozen forecast study must be registered separately.'}
    spec_hash=sha256(canonical(specification).encode());hypothesis_id='expanded_feature_description_'+spec_hash[:16]
    if not any(r['hypothesis_id']==hypothesis_id for r in warehouse.hypotheses()):
        warehouse.preregister(hypothesis_id,specification,status='preregistered')
    curves=[]
    for metric in definitions:
        adapted=[{'player_id':r['player_id'],'position':r['position'],'season':r['season'],'week':r['week'],
                  'season_type':r['season_type'],'stats':{'target_share':r['metrics'][metric]}} for r in enriched['rows']]
        measured=measure_reliability(adapted,{},metrics=['target_share'],windows=windows,gap=gap)
        for row in measured['curves']:
            row['metric']=metric
            if 'window_games' in row:row['window_calendar_weeks']=row.pop('window_games')
            row['uncertainty_status']='No confidence interval; repeated players, selection and dependence are not treated as iid.'
            curves.append(row)
    result={'generated_at':time.time(),'specification_hash':spec_hash,'registry_id':hypothesis_id,
        'curves':curves,'metrics':list(definitions),'definitions':definitions,'windows':list(windows),'gap':gap,
        'comparison_count':len(curves),'evidence_status':'exploratory_historical_association',
        'input_batches':enriched['input_batches'],'cutoff':enriched['cutoff'],
        'diagnostics':{'curves_with_defined_correlation':sum(r.get('correlation') is not None for r in curves),
            'correlation_bins':{label:sum(r.get('correlation') is not None and lo<=r['correlation']<hi for r in curves)
                for label,lo,hi in [('negative',-1.00001,0),('0_to_0.25',0,.25),('0.25_to_0.5',.25,.5),('0.5_to_0.75',.5,.75),('0.75_to_1',.75,1.00001)]},
            'interpretation':'Bins count descriptive correlations, not calibration or independent confirmations.'},
        'limitations':measured['limitations']+['Calendar-week windows require observations throughout; byes and missingness cause selection.',
            'Different candidate source coverage means correlations are not an equal-population tournament.',
            'Qualification thresholds and denominator sizes change noise; ratios are unweighted weekly averages.',
            'Upstream NGS model-output stability does not establish stable player skill.',
            'No historical point-in-time validation or 2025 confirmatory holdout claim.']}
    artifact=warehouse.record_artifact('reliability_expanded',result,input_batches=enriched['input_batches'],
        cutoff=enriched['cutoff'],purpose='exploratory',code_version='candidate-features-v1',metadata={'specification_hash':spec_hash})
    result['artifact']=artifact
    out=Path(output_dir) if output_dir else warehouse.root/'derived'
    coverage={**enriched['coverage'],'definitions':definitions,'input_batches':enriched['input_batches'],'cutoff':enriched['cutoff']}
    files={'coverage':_write(out/'feature-coverage.json',canonical(coverage)+'\n'),
           'reliability':_write(out/'reliability-expanded.json',canonical(result)+'\n')}
    buffer=io.StringIO();fields=list(dict.fromkeys(k for r in curves for k in r))
    writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader();writer.writerows(curves)
    files['csv']=_write(out/'reliability-expanded.csv',buffer.getvalue())
    return {**result,'files':files}
