"""Versioned top-cohort source packets, not completed dossiers or rankings.

Only local observed inputs are read. Source rows retain their original target,
scoring, conditioning and dates; uncertain identity joins are quarantined.
"""
import argparse
from collections import Counter, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .draft_dossiers import clay_index
from .identity import canonical_team
from .market_quotes import PlayerIndex, name_key
from .store import Store, digest
from .warehouse import Warehouse, canonical, leakage_check, timestamp

SOURCE_PATHS = {
    'contracts':'research/dossier400-contracts/top400-contracts.json',
    'player_context':'research/dossier400-player-context.json',
    'market_ladders':'markets/derived/player-ladders.json',
    'market_sensitivity':'markets/derived/player-season-estimates.json',
    'fantasy_lines':'research/dossier400-fantasy-lines/prizepicks-fantasy-score.json',
    'professional_summary':'research/dossier400-professionals/summary.json',
    'professional_registry':'research/dossier400-professionals/source-registry.json',
    'fftoday':'research/dossier400-professionals/fftoday-components.json',
    'platform_adp':'research/dossier400-professionals/draftsharks-platform-adp.json',
    'rank_overlay':'research/dossier400-professionals/draftsharks-rank-overlay.json',
    'rank_preview':'research/dossier400-professionals/draftsharks-rank-preview.json',
    'news':'research/dossier400-professionals/draftsharks-news.json',
    'fantasypros_adp':'research/dossier400-professionals/fantasypros-adp-preview.json',
    'clay':'research/draft-source-samples/clay-2026-components.json',
    'reviewed_aliases':'research/reviewed-player-name-aliases.json',
}
KNOWABILITY = {'observed_at','available_at','known_at','built_at','information_cutoff',
               'as_of','metadata_observed_at','adp_as_of','source_published_at','source_updated_at'}
STATS = {'pass_yd','pass_td','rush_yd','rush_td','rec','rec_yd','rec_td'}
SCOPE = ('Compiled research evidence only. Not completed individualized dossiers, '
         'not calibrated rankings, and not recommendations. No new forecast or title odds are estimated.')


def time_check(value, cutoff, path='input'):
    """Check knowability dates, not future event dates or contract years."""
    if isinstance(value,dict):
        for key,item in value.items():
            if key in KNOWABILITY and item is not None and timestamp(item)>cutoff:
                raise ValueError('Evidence later than cutoff: '+path+'.'+key)
            time_check(item,cutoff,path+'.'+key)
    elif isinstance(value,list):
        for i,item in enumerate(value):time_check(item,cutoff,path+'['+str(i)+']')


def _unique(rows,key,label):
    out={}
    for row in rows:
        value=str(row[key])
        if value in out:raise ValueError('Duplicate '+label+' identity '+value)
        out[value]=row
    return out


def _source_context(body):
    if not isinstance(body,dict):return None
    return {k:copy.deepcopy(v) for k,v in body.items()
            if k not in {'players','rows','ladders','marginals','failures','crosswalk','unmatched','unmatched_sportsbook_subjects','excluded_other_targets'}}


def _identity_gate(row,player):
    reasons=[]
    if row.get('position') and row['position'] not in player.get('fantasy_positions',[player['position']]):
        reasons.append('source_position_disagrees')
    if row.get('gsis_id') and player.get('gsis_id') and str(row['gsis_id']).strip()!=str(player['gsis_id']).strip():
        reasons.append('source_gsis_disagrees')
    return reasons


def _fantasy_team(row):
    """Use an observed source-specific alias only with its corroborating label."""
    team=canonical_team(row.get('team'))
    raw=row.get('raw_player',{}).get('attributes',{})
    if team=='JAC' and raw.get('market')=='Jacksonville' and raw.get('team_name')=='Jaguars':
        return 'JAX',{'source_team':'JAC','canonical_team':'JAX',
            'evidence':{'source_player_id':row.get('source_player_id'),
                        'market':raw['market'],'team_name':raw['team_name']}}
    return team,None


def _fantasy_season(row):
    start=row.get('game_start_time')
    if start is None:return None
    dt=datetime.fromtimestamp(timestamp(start),timezone.utc)
    # Calendar convention only: January/February belong to the previous NFL season.
    # This does not infer a week number or regular-season/postseason eligibility.
    return dt.year-1 if dt.month<=2 else dt.year


def assemble_packets(cohort,universe,sources,forecasts,metadata,league,*,cutoff,expected_count=400):
    """Pure packet construction; no retrieval, imputation, or value aggregation."""
    cutoff=timestamp(cutoff)
    if len(cohort.get('players',[]))!=expected_count:
        raise ValueError('Expected exactly '+str(expected_count)+' cohort players')
    players=_unique(cohort['players'],'player_id','cohort')
    all_players=_unique(universe['players'],'player_id','universe')
    if set(players)-set(all_players):raise ValueError('Cohort ID absent from universe')
    for pid,p in players.items():
        if p!=all_players[pid]:raise ValueError('Cohort and universe identity records disagree')
    season=cohort['season']
    if int(league['season'])!=season:raise ValueError('League and cohort season disagree')
    for label,obj in [('cohort',cohort),('universe',universe),('sources',sources),('metadata',metadata)]:
        time_check(obj,cutoff,label)
    leakage_check(forecasts,cutoff,raise_on_error=True)
    aliases=[]
    for p in all_players.values():
        for name in p.get('observed_name_aliases',[]):
            aliases.append({'name':name,'player_id':p['player_id'],
                'provenance':p.get('identity_provenance') or p['adp_provenance']})
    aliases.extend(sources.get('reviewed_aliases') or [])
    contracts=sources.get('contracts') or {}
    for row in contracts.get('players',[]):
        for record in row.get('all_contract_summary_records',[]):
            aliases.append({'name':record['player'],'player_id':row['player_id'],
                'provenance':{'available_at':contracts['built_at'],'source_id':'nflverse_contracts_otc',
                              'identity_match':row.get('identity_match')}})
    index=PlayerIndex(list(all_players.values()),aliases)
    diagnostics={'unmatched_source_rows':[],'quarantine_count_by_section':Counter()}
    packets={pid:{'schema_version':1,'player_id':pid,'name':p['name'],'draft_season':season,
        'information_cutoff':cutoff,'identity':copy.deepcopy(p),'scope':SCOPE,
        'league_configuration':copy.deepcopy(league),
        'observed_player_metadata':copy.deepcopy(metadata.get('players',{}).get(pid)),
        'metadata_context':{k:copy.deepcopy(v) for k,v in metadata.items() if k!='players'},
        'professional_forecasts':{'season':[],'weekly':[],'clay':None,'fftoday':[],
            'interpretation':'Components and source targets retained; missing primitives are unknown, source point forecasts are not automatically means.'},
        'provider_references':{'platform_adp':[],'rank_overlay':[],'rank_preview':[],'fantasypros_adp':[],
            'interpretation':'Configuration-specific acquisition/reference artifacts. Ranks are not utility, floor/ceiling are not calibrated intervals, and provider medians are not means.'},
        'news':{'items':[],'interpretation':'Dated secondary reports, not independently verified health/role probabilities. No news is not evidence of health.'},
        'contract_context':None,'historical_player_context':None,
        'market_component_ladders':[], 'weekly_fantasy_score_quotes':[], 'market_sensitivity':None,
        'quarantined_evidence':[],
        'decision_outputs':{'annual_championship_probabilities':None,'marginal_title_effect':None,
            'status':'Requires validated player-world distributions, actual twelve-team holdings, feasible decision alternatives and future-policy scenarios.'},
        'in_depth_review_completed':False,'calibrated_ranking':None} for pid,p in players.items()}

    def quarantine(pid,section,row,reasons):
        diagnostics['quarantine_count_by_section'][section]+=1
        item={'section':section,'reasons':list(reasons),'source_record':copy.deepcopy(row)}
        if pid in packets:packets[pid]['quarantined_evidence'].append(item)
        else:diagnostics['unmatched_source_rows'].append(item)

    seen=set()
    for row in forecasts:
        pid=str(row['player_id'])
        if pid not in packets:continue
        key=(pid,row['source_id'],row.get('season'),row.get('week'))
        if key in seen:raise ValueError('Duplicate provider/player/horizon forecast')
        seen.add(key);why=_identity_gate(row,players[pid])
        if row.get('season')!=season:why.append('different_forecast_season')
        if row.get('scoring_hash')!=digest(league['scoring_settings']):why.append('different_scoring_hash')
        if why:quarantine(pid,'professional_forecasts',row,why);continue
        packets[pid]['professional_forecasts']['season' if row.get('week') is None else 'weekly'].append(copy.deepcopy(row))
    clay,clay_unmatched=clay_index(sources.get('clay'),list(all_players.values()),as_of=cutoff,
                                   scoring=league['scoring_settings'],season=season)
    diagnostics['clay_unmatched']=clay_unmatched
    for pid,row in clay.items():
        if pid in packets:packets[pid]['professional_forecasts']['clay']=copy.deepcopy(row)

    for label in ('fftoday','platform_adp','rank_overlay','rank_preview','fantasypros_adp'):
        identities=defaultdict(set)
        for row in sources.get(label) or []:
            pid=str(row.get('player_id'))
            if row.get('player_id') is not None and row.get('source_player_id'):
                identities[(row.get('source_id'),str(row['source_player_id']))].add(pid)
            if pid not in packets:continue
            why=_identity_gate(row,players[pid])
            if row.get('season')!=season:why.append('different_source_season')
            if row.get('known_at') is None:raise ValueError('Professional source lacks knowability')
            if not row.get('identity_match'):why.append('source_join_provenance_missing')
            if why:quarantine(pid,label,row,why);continue
            destination=packets[pid]['professional_forecasts'] if label=='fftoday' else packets[pid]['provider_references']
            destination[label].append(copy.deepcopy(row))
        if any(len(v)>1 for v in identities.values()):raise ValueError('Source player ID maps to multiple cohort identities: '+label)
    for article in sources.get('news') or []:
        if article.get('known_at') is None:raise ValueError('News lacks knowability')
        matched=set()
        for subject in article.get('players',[]):
            pid=str(subject.get('player_id'))
            if pid not in packets or pid in matched:continue
            matched.add(pid)
            if not subject.get('identity_match'):quarantine(pid,'news',article,['source_join_provenance_missing']);continue
            packets[pid]['news']['items'].append(copy.deepcopy(article))
    contract_index=_unique(contracts.get('players',[]),'player_id','contract')
    context=sources.get('player_context') or {}
    for pid,packet in packets.items():
        for label,record,field in [('contracts',contract_index.get(pid),'contract_context'),
            ('player_context',context.get('players',{}).get(pid),'historical_player_context')]:
            if record is None:continue
            why=_identity_gate(record,players[pid])
            if why:quarantine(pid,label,record,why)
            else:packet[field]=copy.deepcopy(record)
    for row in (sources.get('market_ladders') or {}).get('ladders',[]):
        pid=str(row['player_id'])
        if pid not in packets:continue
        why=[]
        if row.get('season')!=season or row.get('horizon')!='nfl_regular_season':why.append('wrong_component_market_horizon')
        if row.get('statistic') not in STATS:why.append('not_supported_component_threshold')
        if row.get('kind') not in ('exchange','sportsbook'):why.append('unrecognized_market_kind')
        if why:quarantine(pid,'market_ladders',row,why);continue
        packet_row=copy.deepcopy(row)
        packet_row['packet_usage_gate']='Conditional threshold/price restrictions only; no inferred player mean or calibrated confidence interval.'
        packets[pid]['market_component_ladders'].append(packet_row)
    for row in (sources.get('fantasy_lines') or {}).get('rows',[]):
        pid,match=index.match(row['player_name']);why=[]
        if pid is None:quarantine(None,'fantasy_lines',row,[match]);continue
        if pid not in packets:continue
        why.extend(_identity_gate(row,players[pid]))
        team,team_evidence=_fantasy_team(row)
        if team!=canonical_team(players[pid].get('team')):why.append('source_team_disagrees')
        if row.get('requires_identity_review'):why.extend(row.get('identity_warnings') or ['source_identity_review_required'])
        if row.get('duration')!='Full' or row.get('stat_type')!='Fantasy Score':why.append('wrong_fantasy_market_target')
        if row.get('status')!='pre_game' or row.get('is_live') or row.get('in_game'):why.append('not_current_pregame_quote')
        if _fantasy_season(row)!=season:why.append('different_or_unknown_fantasy_quote_season')
        if why:quarantine(pid,'fantasy_lines',row,why);continue
        joined=copy.deepcopy(row)
        joined['packet_identity_join']={'player_id':pid,'method':match+' plus matching position and normalized team',
            'observed_alias_evidence':copy.deepcopy(index.alias_evidence.get(name_key(row['player_name']),[])),
            'source_team_alias_evidence':team_evidence}
        joined['packet_calendar_season']=season
        joined['league_week_eligibility_verified']=False
        joined['packet_usage_gate']='Weekly source-scoring threshold only. No direct league forecast or season/future extrapolation.'
        packets[pid]['weekly_fantasy_score_quotes'].append(joined)
    sensitivity=sources.get('market_sensitivity') or {}
    if sensitivity:
        if sensitivity.get('created_at') is None:raise ValueError('Market sensitivity lacks creation timestamp')
        time_check({'built_at':sensitivity['created_at']},cutoff,'market_sensitivity')
        if not sensitivity.get('target','').startswith('NFL'+str(season)+' full regular season;'):
            raise ValueError('Market sensitivity has an unrecognized target horizon')
        for pid,record in sensitivity.get('players',{}).items():
            if pid not in packets:continue
            why=_identity_gate(record,players[pid])
            if record.get('name'):
                resolved,method=index.match(record['name'])
                if resolved!=pid:why.append('sensitivity_player_name_disagrees')
            elif record.get('scenarios') or record.get('professional_component_reference'):
                why.append('sensitivity_numeric_record_lacks_identity_label')
            if why:quarantine(pid,'market_sensitivity',record,why);continue
            packets[pid]['market_sensitivity']={
                'artifact_reference':'sources/market-sensitivity.json','json_pointer':'/players/'+pid,
                'source_context_in_manifest':'market_sensitivity','created_at':sensitivity['created_at'],
                'information_cutoff':sensitivity['information_cutoff'],
                'packet_identity_join':{'player_id':pid,'method':'cohort-bound local ID plus observed name/alias when provided',
                    'observed_alias_evidence':copy.deepcopy(index.alias_evidence.get(name_key(record.get('name') or ''),[]))},
                'interpretation':'Analyst width/structural-zero sensitivity, not calibrated confidence limits. '
                    'Full NFL season including week18; covered-component subtotal only, not complete league scoring or title utility.',
                'record':copy.deepcopy(record),
                'fitting_failures':[copy.deepcopy(r) for r in sensitivity.get('failures',[]) if str(r.get('player_id'))==pid]}
    for packet in packets.values():
        p=packet['professional_forecasts'];h=packet['historical_player_context'] or {};c=packet['contract_context'] or {}
        packet['coverage']={'warehouse_season_forecast':bool(p['season']),'warehouse_weekly_forecast_rows':len(p['weekly']),
            'clay_components':p['clay'] is not None,'fftoday_components':bool(p['fftoday']),
            'news_items':len(packet['news']['items']),'contract_records':bool(c),
            'active_contract_record':bool(c.get('reported_active_contracts')),
            'historical_context_record':bool(h),
            'observed_box_score_history':any(s.get('n_observed_box_score_rows',0)>0 for s in h.get('seasons',{}).values()),
            'market_component_ladders':len(packet['market_component_ladders']),
            'market_sensitivity_scenarios':len((packet['market_sensitivity'] or {}).get('record',{}).get('scenarios',[])),
            'weekly_fantasy_score_quotes':len(packet['weekly_fantasy_score_quotes']),
            'quarantined_records':len(packet['quarantined_evidence'])}
        packet['missing_evidence']=[k for k in ('contract_context','historical_player_context') if packet[k] is None]
        if not any([p['season'],p['clay'],p['fftoday']]):packet['missing_evidence'].append('current_professional_components')
        packet['missing_evidence'] += ['individualized_future_role_paths','calibrated_injury_and_recovery_distribution',
            'calibrated_incremental_source_value','team_specific_marginal_title_comparison']
    diagnostics['quarantine_count_by_section']=dict(diagnostics['quarantine_count_by_section'])
    return list(packets.values()),diagnostics


def _atomic_json(path,body):
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(canonical(body)+'\n')
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def build(root='.moneyball',*,as_of=None,alias=None):
    root=Path(root);cutoff=timestamp(as_of);inputs={};sources={}
    def read(label,relative,required=False):
        path=root/relative
        if not path.exists():
            if required:raise ValueError('Required packet input missing: '+str(path))
            inputs[label]={'path':str(path),'status':'missing'};return None
        raw=path.read_bytes();inputs[label]={'path':str(path),'status':'observed','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        return json.loads(raw)
    cohort=read('cohort','draft/top400-cohort.json',True);universe=read('universe','draft/player-universe.json',True)
    for label,path in SOURCE_PATHS.items():sources[label]=read(label,path)
    for label,key in [('contracts','cohort_sha256'),('player_context','cohort_sha256'),('market_ladders','cohort_sha256'),('professional_summary','cohort_source_sha256')]:
        if sources[label] and sources[label].get(key)!=inputs['cohort']['sha256']:
            raise ValueError('Source artifact uses a different cohort: '+label)
    for label in ('contracts','professional_summary'):
        if sources[label] and sources[label].get('identity_universe_sha256')!=inputs['universe']['sha256']:
            raise ValueError('Source artifact uses a different identity universe: '+label)
    if sources['market_sensitivity']:
        for source_key,packet_key in [('cohort','cohort'),('ladders','market_ladders'),('anchor','fftoday')]:
            if sources['market_sensitivity'].get('inputs',{}).get(source_key,{}).get('sha256')!=inputs[packet_key].get('sha256'):
                raise ValueError('Market sensitivity input differs: '+source_key)
    store=Store(root);snapshot=store.latest(store.resolve_alias(alias));cache=store.cached('players/nfl')
    if not snapshot or not cache:raise ValueError('Observed league and full player cache required')
    if snapshot['created']>cutoff or cache['fetched']>cutoff:raise ValueError('Store observations later than packet cutoff')
    if sources['market_sensitivity'] and sources['market_sensitivity'].get('league_snapshot_hash')!=snapshot['hash']:
        raise ValueError('Market sensitivity uses a different league snapshot')
    w=Warehouse(root/'lab');forecasts=w.query('projections',cutoff=cutoff,seasons=[cohort['season']])
    selected=[r for r in forecasts if str(r['player_id']) in {p['player_id'] for p in cohort['players']}]
    inputs['warehouse_forecasts']={'status':'observed','dataset':'projections','sha256':digest(selected),'rows':len(selected),
        'source_batches':sorted({r['_provenance']['batch_id'] for r in selected})}
    inputs['league_snapshot']={'status':'observed','sha256':snapshot['hash'],'observed_at':snapshot['created']}
    inputs['player_metadata']={'status':'observed','sha256':cache['hash'],'observed_at':cache['fetched']}
    metadata={'observed_at':cache['fetched'],'available_at':cache['fetched'],'source_hash':cache['hash'],
        'source_id':'sleeper_public_player_directory','interpretation':'Observed statuses and role labels, not future probabilities.',
        'players':cache['data']}
    packets,diagnostics=assemble_packets(cohort,universe,sources,selected,metadata,snapshot['data']['league'],cutoff=cutoff)
    code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    build_id=digest({'inputs':inputs,'cutoff':cutoff,'code':code_hash})[:24]
    dest=root/'draft'/'player-packets';builds=dest/'builds';builds.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.building-',dir=builds));final=builds/build_id
    coverage={k:sum(bool(p['coverage'][k]) for p in packets) for k in packets[0]['coverage']}
    manifest={'schema_version':1,'build_id':build_id,'built_at':time.time(),'information_cutoff':cutoff,'scope':SCOPE,
        'player_count':len(packets),'inputs':inputs,'code_sha256':code_hash,'coverage_players':coverage,
        'source_context':{k:_source_context(sources[k]) for k in ('contracts','player_context','market_ladders','market_sensitivity','fantasy_lines','professional_summary','clay')},
        'source_registry':sources.get('professional_registry'),'league_configuration':copy.deepcopy(snapshot['data']['league']),
        'source_independence':'Source/quote counts are coverage, not independent forecasts or calibration sample size.',
        'missing_source_artifacts':[k for k,v in inputs.items() if v['status']=='missing']}
    entries=[]
    try:
        for packet in packets:
            packet['build_id']=build_id;packet['manifest']='manifest.json'
            name=packet['player_id']+'.json';data=(canonical(packet)+'\n').encode();(staging/name).write_bytes(data)
            entries.append({'player_id':packet['player_id'],'name':packet['name'],'position':packet['identity']['position'],
                'cohort_rank':packet['identity']['cohort_rank'],'path':'builds/'+build_id+'/'+name,
                'sha256':hashlib.sha256(data).hexdigest(),'coverage':packet['coverage'],
                'in_depth_review_completed':False})
        (staging/'manifest.json').write_text(canonical(manifest)+'\n')
        (staging/'diagnostics.json').write_text(canonical(diagnostics)+'\n')
        if sources['market_sensitivity']:
            archived=staging/'sources'/'market-sensitivity.json';archived.parent.mkdir()
            shutil.copyfile(inputs['market_sensitivity']['path'],archived)
            if hashlib.sha256(archived.read_bytes()).hexdigest()!=inputs['market_sensitivity']['sha256']:
                raise ValueError('Market sensitivity source changed while archiving')
        # Do not publish a mixed build if a parent task replaced an input mid-read.
        for item in inputs.values():
            if item.get('path') and item['status']=='observed':
                if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['sha256']:
                    raise ValueError('Source changed while building packets: '+item['path'])
        if final.exists():
            # Fixed inputs/cutoff/code identify an immutable build. Reusing it is
            # safe only after its complete contents reconcile with this rebuild.
            original=json.loads((final/'manifest.json').read_text())
            if {k:v for k,v in original.items() if k!='built_at'}!={k:v for k,v in manifest.items() if k!='built_at'}:
                raise ValueError('Existing packet build manifest differs: '+build_id)
            for entry in entries:
                path=final/(entry['player_id']+'.json')
                if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
                    raise ValueError('Existing packet build is corrupt: '+str(path))
            if (final/'diagnostics.json').read_bytes()!=(staging/'diagnostics.json').read_bytes():
                raise ValueError('Existing packet diagnostics differ: '+build_id)
            if sources['market_sensitivity']:
                if hashlib.sha256((final/'sources'/'market-sensitivity.json').read_bytes()).hexdigest()!=inputs['market_sensitivity']['sha256']:
                    raise ValueError('Existing market sensitivity archive is corrupt: '+build_id)
            manifest=original
        else:
            os.replace(staging,final)
        summary={'schema_version':1,'build_id':build_id,'built_at':manifest['built_at'],'information_cutoff':cutoff,
            'player_count':400,'scope':SCOPE,'manifest':'builds/'+build_id+'/manifest.json',
            'diagnostics':'builds/'+build_id+'/diagnostics.json','coverage_players':coverage,'players':entries}
        _atomic_json(dest/'index.json',summary)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return {k:v for k,v in summary.items() if k!='players'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',default='.moneyball');p.add_argument('--as-of');p.add_argument('--league',dest='alias')
    a=p.parse_args();print(canonical(build(a.root,as_of=a.as_of,alias=a.alias)))


if __name__=='__main__':main()
