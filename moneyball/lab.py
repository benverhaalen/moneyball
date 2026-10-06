"""Reproducible research orchestration, separate from the live league cache."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import statistics
import subprocess
import sys
import tempfile
import time

from .warehouse import Warehouse, leakage_check, canonical
from .sources import ingest
from .hypotheses import initialize, append_event
from .store import digest
from . import modeling
from .identity import canonical_team


def write_json(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as handle:
            handle.write(canonical(body)+'\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return str(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def _write_csv(path, rows):
    rows = list(rows)
    if not rows:
        Path(path).write_text('')
        return str(path)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w',newline='') as handle:
        writer = csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    return str(path)


def _snapshot(store, alias):
    snap = store.latest(store.resolve_alias(alias))
    if not snap:
        raise ValueError('No league configuration; run context --fresh')
    return snap


def persist_league(warehouse, store, alias):
    """Copy immutable observed league evidence into the analytical lineage."""
    snap = _snapshot(store,alias)
    warehouse.register_source('sleeper_league',{'origin':'Sleeper documented public /v1 API',
        'access':'existing read-only Client', 'cadence':'five minutes while Mac awake',
        'history':'locally observed snapshots only','terms_urls':['https://docs.sleeper.com/'],
        'license_status':'public API explicitly documented; personal use, no redistribution assumption',
        'verification_status':'actual API reads plus signed-in UI checks'})
    raw = warehouse.put_raw('sleeper_league','local-observed:'+alias,canonical(snap['data']).encode(),observed_at=snap['created'])
    receipt = warehouse.publish('sleeper_league','league_snapshots',
        [{'alias':alias,'snapshot_hash':snap['hash'],'league':snap['data']['league'],
          'rosters':snap['data']['rosters'],'drafts':snap['data']['drafts']}],
        raw_id=raw['id'],key_fields=('alias',),partition=alias)
    return receipt


def _current_players(warehouse, store, season_rows, roster_rows, cutoff):
    ids = {str(r['player_id']) for r in season_rows}
    crosswalk = {str(r['sleeper_id']):r for r in roster_rows if r.get('sleeper_id')}
    with store.connect() as db:
        source = db.execute("SELECT fetched FROM responses WHERE path='players/nfl'").fetchone()
        cached = {str(r[0]):json.loads(r[1]) for r in db.execute(
            'SELECT id,body FROM players WHERE id IN ('+','.join('?' for _ in ids)+')',sorted(ids))}
    if source is None or not cached:
        raise ValueError('Current player metadata missing; run sync/players first')
    warehouse.register_source('sleeper_player_metadata',{'origin':'documented Sleeper /v1/players/nfl',
        'access':'selective copy of existing API cache, no additional HTTP','cadence':'daily',
        'history':'observed revisions only','license_status':'documented public API; personal use',
        'quality':['raw layer here contains a reserialized selected subset, not original wire bytes'],
        'verification_status':'current cached response with original retrieval timestamp'})
    metadata = [{'player_id':pid,**p} for pid,p in cached.items()]
    raw = warehouse.put_raw('sleeper_player_metadata','local-copy:https://api.sleeper.app/v1/players/nfl',
        canonical(metadata).encode(),observed_at=source[0],http_meta={'selected_subset':True,'original_wire_bytes':False})
    warehouse.publish('sleeper_player_metadata','player_metadata',metadata,raw_id=raw['id'],key_fields=('player_id',))
    # An older cutoff reads an older archived revision or returns no metadata.
    # It must never use today's age, injury designation, or corrected crosswalk.
    cached = {str(p['player_id']):p for p in warehouse.query('player_metadata',cutoff=cutoff)}
    output = []
    for row in season_rows:
        pid = str(row['player_id']); p = cached.get(pid,{})
        x = crosswalk.get(pid,{})
        eligible = row.get('fantasy_positions') or [row['position']]
        analytical_position = row['position'] if row['position'] in ('QB','RB','WR','TE') else next((p for p in eligible if p in ('QB','RB','WR','TE')),row['position'])
        output.append({'player_id':pid,'name':row['name'],'position':analytical_position,'source_position':row['position'],
            'fantasy_positions':row.get('fantasy_positions'),'team':row.get('team'),
            'gsis_id':p.get('gsis_id') or x.get('gsis_id'),'age':p.get('age'),
            'years_exp':p.get('years_exp'),'injury_status':p.get('injury_status')})
    return output


def make_projection(warehouse, store, model, *, season=2026, cutoff=None, draws=256):
    cut = time.time() if cutoff is None else cutoff
    modeling.validate_variance_dependency(model,season=season,cutoff=cut)
    forecasts = warehouse.query('projections',cutoff=cut,source_id='sleeper_rotowire',seasons=[season])
    if not forecasts:
        raise ValueError('No acquired professional projections at this cutoff')
    if any(r.get('scoring_hash') != model['scoring_hash'] for r in forecasts):
        raise ValueError('At least one weekly/season projection uses a different scoring profile; re-normalize all cached provider partitions with exact league scoring')
    season_rows = [r for r in forecasts if r['week'] is None]
    roster_rows = warehouse.query('rosters',cutoff=cut,seasons=[season],positions=['QB','RB','WR','TE'])
    current = _current_players(warehouse,store,season_rows,roster_rows,cut)
    base = modeling.project(model,current,season=season,week=1,draws=draws,seed=17,forecast_rows=[f for f in forecasts if f['week']==1])
    adp_rows = warehouse.query('adp',cutoff=cut,source_id='sleeper_rotowire',seasons=[season])
    adp = {str(r['player_id']):r['adp'] for r in adp_rows if r.get('adp_type')=='adp_dynasty_2qb' and r.get('week') is None}
    by_id = defaultdict(dict)
    weekly_components = defaultdict(dict)
    season_components = {}
    for r in forecasts:
        if r['week'] is not None:
            by_id[str(r['player_id'])][str(r['week'])] = r['mean']
            weekly_components[str(r['player_id'])][str(r['week'])] = modeling.projection_component_metadata(r)
        else:
            season_components[str(r['player_id'])] = modeling.projection_component_metadata(r)
    out = []
    schedule = warehouse.query('schedules',cutoff=cut,seasons=[season])
    scheduled_weeks = defaultdict(set)
    for g in schedule:
        if g.get('game_type') == 'REG':
            for t in (g['home_team'],g['away_team']):
                scheduled_weeks[canonical_team(t)].add(int(g['week']))
    for p in base['players']:
        if p.get('status') == 'unmodeled':
            continue
        pid = p['id']
        p['gsis_id'] = next((x.get('gsis_id') for x in current if x['player_id']==pid),None)
        p['source_team'] = p.get('team')
        p['team'] = canonical_team(p.get('team'))
        weekly = by_id.get(pid,{})
        # No gp division. Missing weekly forecasts remain explicit missingness.
        # Tail players can retain observed professional weeks, but are refused
        # by strict decision consumers when a missing week is load-bearing.
        nonzero = [float(v) for w,v in weekly.items() if int(w)<=17 and v > 0]
        p['weekly_means'] = weekly
        p['weekly_component_metadata'] = weekly_components.get(pid,{})
        p['season_component_metadata'] = season_components.get(pid,modeling.projection_component_metadata(None))
        p['observed_week_mean'] = statistics.fmean(nonzero) if nonzero else 0.0
        active_weeks = scheduled_weeks.get(p['team'],set(range(1,19))) & set(range(1,18))
        p['mean'] = sum(float(weekly.get(str(w),0)) for w in active_weeks)/len(active_weeks) if active_weeks else 0.0
        p['mean_definition'] = 'conservative draft heuristic: known forecast total divided by scheduled playing weeks1–17; unknown contributions assumed zero, not measured'
        components = {str(w):p['weekly_component_metadata'].get(str(w),modeling.projection_component_metadata(None))
                      for w in active_weeks}
        partial = sorted(int(w) for w,c in components.items() if c['projection_completeness']=='partial_sparse_zero_unverified')
        unknown = sorted(int(w) for w,c in components.items() if c['projection_completeness']=='unknown')
        known_missing = sorted({key for c in components.values() for key in c.get('missing_scoring_fields') or []})
        p['component_partial_weeks'] = partial
        p['component_completeness_unknown_weeks'] = unknown
        p['known_missing_scoring_fields'] = known_missing
        p['missing_scoring_fields'] = None if unknown else known_missing
        p['projection_completeness'] = ('partial_sparse_zero_unverified' if partial else
            'unknown' if unknown or not components else 'complete_for_requested_scoring_keys')
        p['mean_interpretation'] = ('observed_component_subtotal' if partial else
            'unknown_component_completeness' if unknown or not components else 'score_of_provided_component_means')
        p['mean_definition'] += '; component interpretation: '+p['mean_interpretation']
        p['completeness_scope'] = 'Aggregate of scheduled playing weeks1–17; inspect weekly_component_metadata for each provider audit and individual event applicability caveat.'
        # These direct fields previously described only the week1 base draw;
        # retain the complete per-week audits above rather than mislabeling
        # those week1 values as an audit of the aggregate draft heuristic.
        for field in ('absent_scoring_fields','null_scoring_fields','explicit_zero_scoring_fields','scoring_field_status'):
            p.pop(field,None)
        p['missingness_caveat'] = 'Numeric centers are unchanged. Partial component forecasts remain subtotals; missing audits or missing forecast weeks remain unknown. No event-level imputation is performed.'
        p['missing_forecast_weeks'] = [w for w in range(1,18) if str(w) not in weekly]
        p['missing_nonbye_forecast_weeks'] = sorted(w for w in active_weeks if str(w) not in weekly)
        p['missing_week_policy'] = 'preserve missingness; simulator must explicitly reject/zero/carry; never fill silently'
        p['adp'] = adp.get(pid)
        p['demand_source'] = 'Sleeper adp_dynasty_2qb' if pid in adp else 'unranked; scenario tail only'
        # Use measured centered historical residuals, not sample means generated
        # above. This prevents adding the common factor a second time.
        record = next((model['players'][str(k)] for k in (pid,next((x.get('gsis_id') for x in current if x['player_id']==pid),None)) if k is not None and str(k) in model['players']),None)
        cohort = model['groups'].get((record or {}).get('group',p['position']),{})
        uncertainty = modeling.conditional_residual_distribution(model,p,mean=p['mean'])
        residuals = uncertainty.get('residuals') or cohort.get('residuals') or []
        center = statistics.fmean(residuals) if residuals else 0
        sd = statistics.pstdev(residuals) if len(residuals)>1 else 0
        p['sd'] = sd
        p['standardized_residuals'] = [(x-center)/sd for x in residuals] if sd else [0.0]
        p['variance_method'] = uncertainty.get('method','measured position cohort; not personal certainty from a stale zero-output role')
        p['variance_diagnostics'] = {k:v for k,v in uncertainty.items() if k not in ('residuals','standardized_residuals')}
        loadings = p.get('factor_loadings') or {}
        p['team_loading'] = loadings.get('team',0)/sd if sd else 0
        p['game_loading'] = loadings.get('game',0)/sd if sd else 0
        total = p['team_loading']**2+p['game_loading']**2
        if total>1:
            p['team_loading'] /= math.sqrt(total); p['game_loading'] /= math.sqrt(total)
        p.pop('samples',None)
        out.append(p)
    return {'schema_version':1,'players':out,'created_at':time.time(),'cutoff':cut,'season':season,
        'model_id':model['id'],'scoring_hash':model['scoring_hash'],
        'projection_batches':sorted({r['_provenance']['batch_id'] for r in forecasts}),
        'input_batches':sorted({r['_provenance']['batch_id'] for r in forecasts+roster_rows+adp_rows+schedule+
            warehouse.query('player_metadata',cutoff=cut)}),
        'identity_matched_histories':sum(p['historical_observations']>0 for p in out),
        'adp_covered':sum(p['adp'] is not None for p in out),
        'component_completeness_counts':dict(Counter(p['projection_completeness'] for p in out)),
        'confidence':'professional component centers may be subtotals; missing completeness is unknown; residual shape and correlation remain uncalibrated',
        'limitations':base['limitations']+model['factors'].get('limitations',[])}


def build(warehouse, store, alias, *, season=2026):
    """Recompute derived data from stored raw/cleaned evidence, without HTTP."""
    initialize(warehouse.root)
    snap = _snapshot(store,alias); scoring = snap['data']['league']['scoring_settings']
    lineage = persist_league(warehouse,store,alias)
    cut = time.time()
    rows = warehouse.query('weekly',cutoff=cut,positions=['QB','RB','WR','TE'],seasons=list(range(2018,season)))
    if not rows:
        raise ValueError('No historical statistics; run lab ingest first')
    append_event(warehouse.root,{'kind':'derived_build','train_seasons':sorted({int(r['season']) for r in rows}),
        'metric_windows':[2,4,6],'gap':1,'evaluation_status':'descriptive only; no new holdout testing during rebuild'})
    model = modeling.fit_model(rows,scoring,train_seasons=sorted({int(r['season']) for r in rows}),cutoff=cut)
    model = modeling.bind_variance_dependency(model,rows,cutoff=cut)
    reliability = modeling.measure_reliability(rows,scoring,metrics=list(modeling.METRICS)+['league_points'])
    horizon = modeling.estimate_horizon_transitions(rows,scoring)
    changes = modeling.measure_role_changes(rows,scoring)
    projection = make_projection(warehouse,store,model,season=season,cutoff=cut)
    out = warehouse.root/'derived'; out.mkdir(exist_ok=True)
    paths = {name:write_json(out/(name+'.json'),body) for name,body in (
        ('model-current',model),('reliability',reliability),('horizon-observation-transitions',horizon),
        ('role-changes',changes),('projection',projection))}
    paths['reliability_csv'] = _write_csv(out/'reliability.csv',reliability['curves'])
    # Positive-factor matrix: mathematically PSD, explicitly incomplete for
    # touch competition. Empirical signed covariance lives in model diagnostics.
    ps = projection['players']
    with (out/'correlation-factor-matrix.csv').open('w',newline='') as f:
        w = csv.writer(f); w.writerow(['player_id']+[p['id'] for p in ps])
        for p in ps:
            w.writerow([p['id']]+[1.0 if p['id']==q['id'] else
                p['team_loading']*q['team_loading']+p['game_loading']*q['game_loading']
                if p.get('team') and p.get('team')==q.get('team') else 0.0 for q in ps])
    paths['correlation_matrix'] = str(out/'correlation-factor-matrix.csv')
    batches = sorted({r['_provenance']['batch_id'] for r in rows})+projection['input_batches']+[lineage['batch_id']]
    receipt = warehouse.record_artifact('current_projection',projection,input_batches=batches,cutoff=cut,purpose='features',
        code_version=digest({p.name:p.read_text() for p in Path(__file__).parent.glob('*.py')}))
    coverage = {'created_at':time.time(),'rules_hash':snap['hash'],'paths':paths,'projection_players':len(ps),
        'adp_covered':projection['adp_covered'],'training_rows':len(rows),'reliability_comparisons':reliability['comparison_count'],
        'scoring_audit':model['scoring_audit'],'receipt':receipt,
        'validation':'building an artifact does not establish out-of-sample predictive or strategic edge'}
    write_json(out/'build-receipt.json',coverage)
    return coverage


def mispricing_map(warehouse, projection):
    """Record disagreements without laundering rankings into title value."""
    year = projection['season']
    rows = warehouse.query('consensus_values',seasons=[year])
    players = {p['id']:p for p in projection['players']}
    report = []
    for r in rows:
        pid = str(r['player_id'])
        if pid not in players:
            continue
        p = players[pid]
        report.append({'player_id':pid,'name':p['name'],'position':p['position'],'source':r['source_id'],
            'format':r.get('format'),'consensus_value':r.get('value'),'consensus_rank':r.get('rank'),
            'format_matched_adp':p.get('adp'),'marginal_championship_value':None,
            'mispricing_status':'not_established; ordinal source disagreement is not a price error',
            'configuration_mismatch':'test 1QB versus2QB field only' if '1qb' in str(r.get('format')).lower() else 'not established',
            'staleness_cause':None,'name_recognition_cause':None,'availability_cause':None,
            'missing_counterparty_evidence':'no internal offer acceptance history before startup'})
    path = _write_csv(warehouse.root/'derived'/'consensus-disagreements.csv',report)
    return {'path':path,'rows':len(report),'confirmed_mispricings':0,
        'next_test':'Join decision-specific paired championship deltas and actual counterparty prices; do not compare unlike units'}


def draft_report(warehouse, store, alias, *, draft_draws=8, draws=80, years=1, seed=1, noise=8.0, candidates=(), rivals='adp',
                 distribution='residual',horizon='frozen',missing_forecast='zero'):
    from .drafting import experiment, POLICIES
    projection = read_json(warehouse.root/'derived'/'projection.json')
    code_at_start = digest({p.name:p.read_text() for p in Path(__file__).parent.glob('*.py')})
    if distribution=='entropy':
        projection['marginal_support'] = read_json(warehouse.root/'derived'/'marginal-support.json')
        support = projection['marginal_support']
        if support.get('scoring_hash')!=projection['scoring_hash'] or max(support['train_seasons'])>=projection['season']:
            raise ValueError('Marginal support has stale scoring or overlapping future training seasons; rebuild it')
    if horizon=='transition':
        from .horizon import transition_for_player
        hm = read_json(warehouse.root/'derived'/'horizon-production.json')
        if hm.get('scoring_hash')!=projection['scoring_hash'] or hm['last_outcome_season']>=projection['season']:
            raise ValueError('Horizon model has stale scoring or training overlaps projection season; rebuild it')
        for p in projection['players']:
            p.update(transition_for_player(hm,p,baseline_mean=p['mean'],season=projection['season'],draws=512,seed=seed))
        projection['horizon_model_id'] = hm['id']
    snap = _snapshot(store,alias)
    if projection['scoring_hash'] != digest(snap['data']['league']['scoring_settings']):
        raise ValueError('Scoring changed after projection build')
    draft = next((d for d in snap['data']['drafts'] if d.get('info',d).get('status')!='complete'),None)
    if not draft:
        raise ValueError('No unfinished startup draft')
    # Existing normalized snapshot nests API draft in details.
    details = draft.get('info',draft.get('details',draft.get('draft',draft)))
    user_id = str(snap['data'].get('my_user_id') or '')
    my_roster = snap['data']['my_roster_id']
    own = next(r for r in snap['data']['rosters'] if r['roster_id']==my_roster)
    candidates_owner = [str(own.get('owner_id'))]+list(map(str,own.get('co_owners') or []))
    order = details.get('draft_order') or {}
    slots = [int(order[u]) for u in candidates_owner if u in order]
    slot = slots[0] if len(set(slots))==1 else None
    if slot is None:
        raise ValueError('Cannot verify own draft slot from observed draft order')
    if details.get('type') not in (None,'snake') or details.get('settings',{}).get('reversal_round',0):
        raise ValueError('Unsupported draft mechanics; no silent snake assumption')
    from .simulation import EXPERIMENTAL_STARTERS
    if tuple(p for p in snap['data']['league']['roster_positions'] if p!='BN') != EXPERIMENTAL_STARTERS:
        raise ValueError('Starter configuration changed; update and validate draft evaluator')
    if details.get('settings',{}).get('rounds') != 28 or draft.get('traded_picks'):
        raise ValueError('Changed startup rounds or traded picks require an explicit order adapter')
    append_event(warehouse.root,{'kind':'preregister_experiment','family':'startup_v1','policies':
        list(POLICIES),
        'candidates':list(candidates),'noise':noise,'years':years,'draft_draws':draft_draws,'outcome_draws':draws,'rivals':rivals,
        'distribution':distribution,'horizon':horizon,'missing_forecast':missing_forecast,
        'success':'positive delta across demand/correlation sensitivity; MC superiority alone is insufficient',
        'selection':'report every tested policy, no declaration of a validated winner'})
    nfl = warehouse.query('schedules',seasons=[projection['season']])
    result = experiment(projection['players'],slot=slot,draft_draws=draft_draws,outcome_draws=draws,
        years=years,seed=seed,demand_noise=noise,observed_picks=draft.get('picks') or [],nfl_schedule=nfl,
        forced_candidates=candidates,rivals=rivals,projection_metadata=projection,
        persistent_shock=horizon=='transition',missing_forecast=missing_forecast)
    result.update({'league_snapshot_hash':snap['hash'],'projection_created_at':projection['created_at'],
        'projection_age_hours':(time.time()-projection['created_at'])/3600,'draft_slot':slot})
    result.update(distribution=distribution,horizon=horizon,missing_forecast=missing_forecast,code_at_start=code_at_start)
    for r in result['policies']:
        lookup = {p['id']:p['name'] for p in projection['players']}
        r['first_pick_names'] = {lookup.get(p,p):n for p,n in r['first_pick_frequencies'].items()}
        r['next_unobserved_pick_names'] = {lookup.get(p,p):n for p,n in r.get('next_unobserved_pick_frequencies',{}).items()}
    path = warehouse.root/'derived'/f'draft-{distribution}-{horizon}-{rivals}-noise{noise:g}-years{years}-seed{seed}.json'
    write_json(path,result)
    result['artifact_receipt'] = warehouse.record_artifact('draft_policy_experiment',result,
        input_batches=projection['input_batches'],cutoff=time.time(),purpose='features',
        code_version=code_at_start,
        metadata={'projection_model_id':projection['model_id'],'league_snapshot_hash':snap['hash'],
                  'evidence_status':'uncalibrated_scenario_experiment'})
    write_json(path,result)
    append_event(warehouse.root,{'kind':'experiment_result','family':'startup_v1','path':str(path),
        'comparisons':result['comparison_count'],'status':'conditional_scenario_only'})
    return {'path':str(path),'years':years,'comparisons':result['comparison_count'],
        'policies':[{k:v for k,v in r.items() if k not in ('example','first_pick_frequencies')} for r in result['policies']]}


def refresh(warehouse, store, alias, *, season=2026, force=False, offline=False):
    from .providers import ingest_providers
    with (warehouse.root/'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        results = {}
        results['history'] = ingest(warehouse,datasets=('rosters','schedules'),seasons=(season,),force=force,offline=offline)
        games = warehouse.query('schedules',seasons=[season])
        if any(g.get('game_type')=='REG' and g.get('home_score') is not None and g.get('away_score') is not None for g in games):
            results['weekly'] = ingest(warehouse,datasets=('weekly',),seasons=(season,),force=force,offline=offline)
        else:
            results['weekly'] = {'ok':True,'status':'not_due_no_completed_regular_games','failures':[]}
        scoring = _snapshot(store,alias)['data']['league']['scoring_settings']
        results['providers'] = ingest_providers(warehouse,season=season,weeks=(1,15,16,17),force=force,offline=offline,
            scoring=scoring,include_fantasypros=False)
        path = write_json(warehouse.root/'last-refresh.json',{'created_at':time.time(),'results':results})
    ok = results['history']['ok'] and results['weekly']['ok'] and results['providers']['status']=='ok'
    return {'ok':ok,'path':path,'alerts':str(warehouse.root/'alerts.jsonl'),
        'failure_count':sum(len(r.get('failures',[])) for r in results.values()),
        'refresh_status':'complete' if ok else 'partial_failure; prior valid partitions preserved'}


def schedule(warehouse, action, *, interval=3600):
    label = 'com.moneyball.research'
    path = Path.home()/'Library'/'LaunchAgents'/(label+'.plist')
    domain = f'gui/{os.getuid()}'
    if action == 'status':
        r = subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True,text=True)
        return {'installed':path.exists(),'loaded':r.returncode==0,'details':r.stdout or r.stderr}
    if action == 'remove':
        subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
        path.unlink(missing_ok=True)
        return {'removed':label}
    if interval < 900:
        raise ValueError('Research refresh minimum interval15minutes; documented league watcher is separate')
    project = Path(__file__).resolve().parents[1]
    item = {'Label':label,'ProgramArguments':[sys.executable,'-m','moneyball','--data-dir',str(warehouse.root.parent),'lab','refresh'],
        'WorkingDirectory':str(project),'StartInterval':interval,'RunAtLoad':False,'ProcessType':'Background','Umask':0o077,
        'StandardOutPath':str(warehouse.root/'refresh.out.log'),'StandardErrorPath':str(warehouse.root/'refresh.err.log')}
    path.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    path.write_bytes(plistlib.dumps(item))
    r = subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True,text=True)
    if r.returncode:
        raise ValueError('Schedule written but load failed: '+r.stderr)
    return {'installed':True,'plist':str(path),'interval_seconds':interval,
        'runs_while':'Mac awake and user logged in','alerts':str(warehouse.root/'alerts.jsonl'),
        'projection_refresh':'subject to provider rights gate; cached-only sources are not polled'}


def dispatch(args, store):
    warehouse = Warehouse(store.root/'lab')
    action = args.action
    if args.cutoff is not None and action != 'query':
        raise ValueError('--cutoff is currently supported by lab query only; current-decision commands cannot pretend to replay a historical information set')
    if action == 'init':
        return {'hypotheses':initialize(warehouse.root),'warehouse':warehouse.summary()}
    if action == 'status':
        return warehouse.summary()
    if action == 'sources':
        return warehouse.source_registry()
    if action == 'alerts':
        with warehouse.connect() as db:
            return [{**dict(r),'body':json.loads(r['body'])} for r in db.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT ?', (args.limit,))]
    if action == 'query':
        if not args.dataset:
            raise ValueError('--dataset required')
        rows = warehouse.query(args.dataset,cutoff=args.cutoff,source_id=args.source,positions=args.positions,seasons=args.seasons)
        return {'rows':rows[:args.limit],'matched':len(rows),'cutoff':args.cutoff,'leakage_errors':leakage_check(rows,args.cutoff or time.time())}
    if action == 'ingest':
        return ingest(warehouse,datasets=args.datasets or ('weekly','rosters','schedules','players','snap_counts','draft_picks','combine'),
            seasons=args.seasons or tuple(range(2018,2027)),force=args.fresh,offline=args.offline)
    if action == 'advanced':
        from .advanced import ingest_advanced
        return ingest_advanced(warehouse,datasets=args.datasets or ('weekly_rosters','depth_charts','injuries','ngs','pbp'),
            seasons=args.seasons or (2025,2026),force=args.fresh,offline=args.offline)
    if action == 'opportunities':
        from .advanced import materialize_opportunities
        return materialize_opportunities(warehouse,seasons=args.seasons or tuple(range(2018,2026)))
    if action == 'features':
        from .features import enrich_features, measure_features
        enriched = enrich_features(warehouse,seasons=args.seasons or tuple(range(2018,2026)))
        result = measure_features(enriched,warehouse=warehouse)
        return {'result':result,'note':'descriptive reliability; no automatic forecast-feature promotion'}
    if action == 'incremental':
        from .incremental import run_incremental
        scoring = _snapshot(store,args.league)['data']['league']['scoring_settings']
        return run_incremental(warehouse,scoring)
    if action == 'depth':
        from .advanced import latest_depth_chart
        rows = latest_depth_chart(warehouse,season=args.season)
        if args.positions:
            rows = [r for r in rows if r.get('position') in args.positions]
        return {'rows':rows[:args.limit],'matched':len(rows),'semantics':'latest observed team snapshots; timestamps differ by team'}
    if action == 'providers':
        from .providers import ingest_providers
        scoring = _snapshot(store,args.league)['data']['league']['scoring_settings']
        return ingest_providers(warehouse,season=args.season,weeks=args.weeks or tuple(range(1,19)),
            scoring=scoring,force=args.fresh,offline=args.offline)
    if action == 'build':
        return build(warehouse,store,args.league,season=args.season)
    if action == 'mispricing':
        return mispricing_map(warehouse,read_json(warehouse.root/'derived'/'projection.json'))
    if action == 'draft':
        return draft_report(warehouse,store,args.league,draft_draws=args.draft_draws,draws=args.draws,years=args.years,
            seed=args.seed,noise=args.noise,candidates=args.candidates or (),rivals=args.rivals,
            distribution=args.distribution,horizon=args.horizon,missing_forecast=args.missing_forecast)
    if action == 'live':
        from .live import run
        return run(warehouse,store,args.league,args=args)
    if action == 'refresh':
        return refresh(warehouse,store,args.league,season=args.season,force=args.fresh,offline=args.offline)
    if action == 'schedule':
        return schedule(warehouse,args.schedule_action,interval=args.interval)
    if action == 'move':
        from .simulation import apply_move, simulate
        if not args.input:
            raise ValueError('--input move.json required')
        move = read_json(args.input)
        snap = _snapshot(store,args.league)
        state = {str(r['roster_id']):r.get('players') or [] for r in snap['data']['rosters']}
        if not any(state.values()) and not args.state:
            raise ValueError('Predraft rosters are empty; use lab draft, or explicitly supply a hypothetical --state')
        if args.state:
            state = read_json(args.state)
        transformed = apply_move(state,move)
        if transformed['capacity_warnings']:
            raise ValueError('Supply only output-eligible ordinary holdings; taxi/IR/cuts must be specified first')
        projection = read_json(warehouse.root/'derived'/'projection.json')
        result = simulate({'before':state,'after':transformed['state']},projection,draws=args.draws,years=args.years,
            seed=args.seed,nfl_schedule=warehouse.query('schedules',seasons=[args.season]),allow_assumptions=True,
            missing_forecast='zero')
        result['move'] = move; result['writes_performed'] = False
        path = write_json(warehouse.root/'derived'/'last-move.json',result)
        return {'path':path,'results':result['scenarios'],'confidence':result['confidence']}
    raise ValueError('Unknown lab action')
