"""Read-only, deadline-bounded startup decisions conditioned on observed picks.

Fast state/survival updates are separate from conditional championship rollouts.
Every result carries its exact prefix hash; a changed prefix invalidates it.
"""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import random
import statistics
import threading
import time

from .drafting import draft_once, snake_owner
from .hypotheses import append_event
from .simulation import keyed_seed, positions, select_lineup, simulate, EXPERIMENTAL_STARTERS
from .store import digest


MODES = ('posterior', 'adp', 'lineup')


def prefix_key(picks):
    return digest([{k:p[k] for k in ('pick_no','draft_slot','player_id') if k in p}
                   for p in sorted(picks,key=lambda p:int(p['pick_no']))])


def state_from_picks(players, picks, *, slot=5, rounds=28):
    pool = {str(p['id']):p for p in players if positions(p)}
    ordered = sorted(picks,key=lambda p:int(p['pick_no']))
    if [int(p['pick_no']) for p in ordered] != list(range(1,len(ordered)+1)):
        raise ValueError('Live picks must be a contiguous unique prefix')
    if len(ordered)>rounds*12:
        raise ValueError('Draft prefix exceeds configured length')
    rosters = {str(t):[] for t in range(1,13)}
    taken = set()
    for pick in ordered:
        n=int(pick['pick_no']); owner=snake_owner(n); pid=str(pick['player_id'])
        if int(pick.get('draft_slot',owner))!=owner:
            raise ValueError('Traded or changed snake ownership needs an explicit adapter')
        if pid not in pool or pid in taken:
            raise ValueError('Observed player missing from forecasts or duplicated: '+pid)
        rosters[str(owner)].append(pid); taken.add(pid)
    upcoming = next((p for p in range(len(ordered)+1,rounds*12+1) if snake_owner(p)==slot),None)
    return {'rosters':rosters,'available':[p for p in pool if p not in taken],
        'observed_picks':len(ordered),'opponent_observations':len(ordered)-len(rosters[str(slot)]),
        'observations_per_opponent':{t:len(r) for t,r in rosters.items() if t!=str(slot)},
        'next_own_pick':upcoming,'on_clock':upcoming==len(ordered)+1,
        'picks_before_own_turn':None if upcoming is None else upcoming-len(ordered)-1,
        'prefix_hash':prefix_key(ordered)}


def shortlist(players, picks, *, slot=5, limit=6, requested=()):
    """Transparent pruning, not a claim that excluded choices are dominated."""
    state=state_from_picks(players,picks,slot=slot)
    pool={str(p['id']):p for p in players}; available=state['available']
    if requested:
        ids=list(dict.fromkeys(map(str,requested)))
        if any(p not in available for p in ids):
            raise ValueError('Requested live candidate is already selected or unforecasted')
        if len(ids)>12:
            raise ValueError('Live comparison caps at12 explicit candidates; use offline research for broader searches')
        return ids
    values={pid:float(p.get('mean') or 0) for pid,p in pool.items()}
    own=state['rosters'][str(slot)]
    value=lambda roster:sum(values[p] for p in select_lineup(roster,pool,values))
    before=value(own)
    adp=lambda p:(float(pool[p].get('adp') or 999),p)
    near=sorted(available,key=adp)
    pool_ids=list(near[:12])
    for pos in ('QB','RB','WR','TE'):
        pool_ids.extend(sorted((p for p in available if pos in positions(pool[p])),
            key=lambda p:(-values[p],adp(p)))[:3])
    gains={p:value(own+[p])-before for p in set(pool_ids)}
    by_gain=sorted(gains,key=lambda p:(-gains[p],adp(p)))
    # Keep both demand and contribution alternatives. Neither is the output objective.
    ordered=[]
    for pair in zip(near,by_gain):
        ordered.extend(pair)
        if len(set(ordered))>=limit:
            break
    return list(dict.fromkeys(ordered))[:limit]


def evaluate(projection, picks, *, slot=5, candidates=(), candidate_limit=6,
             nfl_schedule=None, budget_seconds=40, max_blocks=64, inner_draws=8,
             years=1, seed=1, persistent_shock=False, missing_forecast='zero',
             modes=MODES, on_update=None, cancelled=None):
    """Each independent outer block contains paired full-draft completions.

    Deadline checks occur between full paired blocks. Network polling runs on
    another thread; stale generations never publish a recommendation. First cold
    distribution setup may exceed a tiny requested budget; timings are exposed.
    """
    from . import opponents
    from .uncertainty import paired_interval, candidate_race
    if budget_seconds<=0 or max_blocks<1 or inner_draws<2 or years<1:
        raise ValueError('Positive live budget/blocks/years and at least2 inner draws required')
    if not modes or any(m not in MODES for m in modes):
        raise ValueError('Unknown live opponent scenario')
    started=time.monotonic(); players=projection['players']
    state=state_from_picks(players,picks,slot=slot)
    if state['next_own_pick'] is None:
        return {**state,'stage':'complete','recommendation':None,'writes_performed':False}
    ids=shortlist(players,picks,slot=slot,limit=candidate_limit,requested=candidates)
    if len(ids)<2:
        raise ValueError('At least2 available candidates required for a live comparison')
    model=opponents.fit(players,picks,our_slot=slot)
    pool={str(p['id']):p for p in players}
    result={**state,'stage':'state_updated','created_at':time.time(),
        'candidates':[{'id':p,'name':pool[p]['name'],'position':pool[p]['position']} for p in ids],
        'opponent_model':model,'recommendation':None,'writes_performed':False,
        'interpretation':'conditional simulation, not calibrated championship odds',
        'candidate_scope':'pruned alternatives; excluded players have not been proved inferior',
        'before_clock_semantics':'force candidate at next own turn if available, otherwise identical greedy continuation; no actionable pick before own turn',
        'years':years,'inner_draws_per_block':inner_draws,'modes':list(modes),
        'seed':seed,'persistent_shock':persistent_shock,'missing_forecast':missing_forecast,
        'distribution':'entropy' if projection.get('marginal_support') else 'residual',
        'forecast_age_hours':(time.time()-projection['created_at'])/3600}
    result['forecast_fingerprint']=digest(projection)
    result['input_batches']=projection.get('input_batches',[])
    result['code_fingerprint']=digest({p.name:p.read_text() for p in Path(__file__).parent.glob('*.py')})
    if on_update: on_update(result)
    # Cheap next-turn demand prediction is useful while the slower title model
    # warms its cache. These are predictive probabilities, not confidence in the
    # correctness of the opponent model. Finite MC intervals exclude that error.
    result['fast_availability']={}
    if state['on_clock'] and state['next_own_pick']>=325:
        result['fast_availability']={'status':'no_following_own_pick'}
    elif state['on_clock']:
        for candidate in ids:
            if cancelled and cancelled():
                return {**result,'stage':'cancelled_new_state','recommendation':None}
            result['fast_availability'][candidate]=opponents.simulate_until(players,picks,ids,
                draws=128,seed=keyed_seed(seed,'fast-wait',state['prefix_hash']),
                model=model,our_slot=slot,first_pick=candidate)
    else:
        result['fast_availability']['next_turn']=opponents.simulate_until(players,picks,ids,
            draws=128,seed=keyed_seed(seed,'fast-wait',state['prefix_hash']),model=model,our_slot=slot)
    result['next_opponent_forecast']=opponents.next_forecast(players,model,
        feature_available_at=projection['created_at'])
    result.update(stage='availability_updated',elapsed_seconds=time.monotonic()-started)
    if on_update: on_update(result)
    samples={m:{p:[] for p in ids} for m in modes}
    availability={m:Counter() for m in modes}
    next_survival={m:{p:Counter() for p in ids} for m in modes}
    examples={m:{} for m in modes}
    cycle=0; assumptions=None
    while cycle<max_blocks:
        if cancelled and cancelled():
            return {**result,'stage':'cancelled_new_state','recommendation':None}
        if cycle and time.monotonic()-started>=budget_seconds:
            break
        # Complete every model before considering the deadline, so model counts
        # stay balanced and a fast model is never silently given more weight.
        for mode in modes:
            states={}; completions={}
            for candidate in ids:
                if cancelled and cancelled():
                    return {**result,'stage':'cancelled_new_state','recommendation':None}
                selector=opponents.make_selector(players,model,
                    seed=keyed_seed(seed,'live-opponents',mode,cycle),mode=mode)
                draft=draft_once(players,slot=slot,policy='flexible',
                    observed_picks=picks,forced_first=candidate,
                    seed=keyed_seed(seed,'live-own',mode,cycle),rival_selector=selector)
                states[candidate]=draft['rosters']; completions[candidate]=draft
            outcome=simulate(states,projection,draws=inner_draws,
                seed=keyed_seed(seed,'live-outcome',mode,cycle),years=years,
                nfl_schedule=nfl_schedule,allow_assumptions=True,
                persistent_shock=persistent_shock,missing_forecast=missing_forecast)
            assumptions=outcome['assumptions']
            for candidate in ids:
                samples[mode][candidate].append(outcome['scenarios'][candidate][str(slot)]['expected_titles']['estimate'])
                d=completions[candidate]
                availability[mode][candidate]+=int(d['forced_first_available'])
                ownturn=d['candidate_decision_pick']
                following=next((p for p in range(ownturn+1,337) if snake_owner(p)==slot),None)
                if following is not None and d['forced_first_available']:
                    taken_before={x['player_id'] for x in d['trace'] if x['pick_no']<following}
                    for target in ids:
                        if target!=candidate:
                            next_survival[mode][candidate][target]+=int(target not in taken_before)
                if cycle==0:
                    examples[mode][candidate]=[x for x in d['trace'] if x['draft_slot']==slot and x['pick_no']>=ownturn][:3]
        cycle+=1
        reports={}
        for mode in modes:
            baseline=ids[0]
            rows=[]
            for candidate in ids:
                paired=[a-b for a,b in zip(samples[mode][candidate],samples[mode][baseline])]
                rows.append({'id':candidate,'name':pool[candidate]['name'],
                    'expected_titles':statistics.fmean(samples[mode][candidate]),
                    'delta_vs_reference':statistics.fmean(paired),
                    'paired_ci':paired_interval(paired,lower=-years,upper=years,
                        alpha=.05/len(modes),comparisons=max(1,len(ids)*(len(ids)-1)//2)),
                    'available_at_decision_fraction':availability[mode][candidate]/cycle,
                    'conditional_survival_to_following_own_pick':
                        ({p:next_survival[mode][candidate][p]/availability[mode][candidate] for p in ids if p!=candidate}
                         if state['next_own_pick']<325 and availability[mode][candidate] else None),
                    'survival_conditional_sample_count':availability[mode][candidate],
                    'example_next_three_picks':examples[mode][candidate]})
            race=candidate_race(samples[mode],lower=0,upper=years,
                alpha=.05/len(modes),family_size=len(ids),minimum_samples=32)
            reports[mode]={'rows':rows,'race':race,'reference_candidate':baseline}
        regrets={p:max(max(statistics.fmean(v) for v in samples[m].values())-
                         statistics.fmean(samples[m][p]) for m in modes) for p in ids}
        robust=min(ids,key=lambda p:(regrets[p],-statistics.fmean(samples[modes[0]][p]),p))
        certified=[reports[m]['race'].get('certified_winner') for m in modes]
        result.update(stage='evaluating',completed_outer_blocks_per_mode=cycle,
            elapsed_seconds=time.monotonic()-started,scenario_reports=reports,
            samples_by_mode=samples,maximum_regret_estimates=regrets,
            simulation_assumptions=assumptions,
            provisional_choice=robust if state['on_clock'] else None,
            recommendation=robust if state['on_clock'] and all(c==robust for c in certified) else None,
            decision_status='simultaneous_MC_dominance_only' if state['on_clock'] and all(c==robust for c in certified)
                else 'unresolved; point-estimate minimax regret is an explicit fallback, not statistical certification',
            confidence_scope='Intervals cover Monte Carlo integration under each fixed model; neither opponent truth nor professional forecast calibration is covered')
        if on_update: on_update(result)
    result.update(stage='budget_complete',elapsed_seconds=time.monotonic()-started,
        budget_seconds=budget_seconds,budget_overrun_seconds=max(0,time.monotonic()-started-budget_seconds))
    if on_update: on_update(result)
    return result


def load_inputs(warehouse, store, alias, *, distribution='entropy',horizon='transition',seed=1):
    from .lab import read_json
    projection=read_json(warehouse.root/'derived'/'projection.json')
    snap=store.latest(alias)
    if not snap: raise ValueError('Run context --fresh before live evaluation')
    league=snap['data']['league']
    if projection['scoring_hash']!=digest(league['scoring_settings']):
        raise ValueError('Projection scoring differs from current league')
    if tuple(p for p in league['roster_positions'] if p!='BN')!=EXPERIMENTAL_STARTERS:
        raise ValueError('Unsupported changed starter configuration')
    if league['settings'].get('playoff_teams')!=8 or league['settings'].get('playoff_week_start')!=15:
        raise ValueError('Unsupported changed playoff structure')
    d=next((d for d in snap['data']['drafts'] if d['info']['status']!='complete'),None)
    if not d: raise ValueError('No unfinished draft')
    info=d['info']
    if info.get('type')!='snake' or info['settings'].get('rounds')!=28 or info['settings'].get('reversal_round',0) or d.get('traded_picks'):
        raise ValueError('Live adapter requires verified28-round snake without traded picks or reversal')
    owner=next(r for r in snap['data']['rosters'] if r['roster_id']==snap['data']['my_roster_id'])
    slots={int(info['draft_order'][str(u)]) for u in [owner.get('owner_id'),*(owner.get('co_owners') or [])]
           if str(u) in info.get('draft_order',{})}
    if len(slots)!=1: raise ValueError('Cannot verify own draft slot')
    if distribution=='entropy':
        m=read_json(warehouse.root/'derived'/'marginal-support.json')
        if m['scoring_hash']!=projection['scoring_hash'] or max(m['train_seasons'])>=projection['season']:
            raise ValueError('Stale marginal support or future training data')
        projection['marginal_support']=m
    if horizon=='transition':
        from .horizon import transition_for_player
        m=read_json(warehouse.root/'derived'/'horizon-production.json')
        if m['scoring_hash']!=projection['scoring_hash'] or m['last_outcome_season']>=projection['season']:
            raise ValueError('Stale annual model or future training data')
        for player in projection['players']:
            player.update(transition_for_player(m,player,baseline_mean=player['mean'],season=projection['season'],draws=512,seed=seed))
    return {'projection':projection,'slot':slots.pop(),'draft_id':info['draft_id'],
        'picks':d.get('picks') or [],'league_hash':snap['hash'],
        'draft_order':info.get('draft_order'),
        'league_id':league['league_id'],'nfl_schedule':warehouse.query('schedules',seasons=[projection['season']])}


class LiveFeed:
    """Polling independent of calculation; compare-and-publish by exact prefix."""
    def __init__(self,store,warehouse,inputs,*,offline=False):
        self.store,self.w,self.inputs=store,warehouse,inputs
        self.offline=offline; self.lock=threading.RLock(); self.stop=threading.Event()
        self.current={'picks':inputs['picks'],'prefix_hash':prefix_key(inputs['picks']),
            'last_checked_at':None,'error':None,'result':None}

    def poll(self):
        from .sleeper import Client
        client=Client(self.store,offline=self.offline)
        pid=self.inputs['draft_id']
        picks=client.get('draft/'+pid+'/picks',0)
        info=client.get('draft/'+pid,30)
        trades=client.get('draft/'+pid+'/traded_picks',30)
        league=client.get('league/'+self.inputs['league_id'],30)
        if digest(league['scoring_settings'])!=self.inputs['projection']['scoring_hash']:
            raise ValueError('League scoring changed; restart with rebuilt projections')
        if tuple(p for p in league['roster_positions'] if p!='BN')!=EXPERIMENTAL_STARTERS:
            raise ValueError('League starter configuration changed')
        if league['settings'].get('playoff_teams')!=8 or league['settings'].get('playoff_week_start')!=15:
            raise ValueError('League playoff structure changed')
        if info.get('type')!='snake' or info['settings'].get('rounds')!=28 or info['settings'].get('reversal_round',0) or trades:
            raise ValueError('Live draft order mechanics changed')
        if info.get('draft_order')!=self.inputs['draft_order']:
            raise ValueError('Live draft ownership changed')
        state_from_picks(self.inputs['projection']['players'],picks,slot=self.inputs['slot'])
        key=prefix_key(picks)
        fetched=client.evidence['draft/'+pid+'/picks']['fetched_at']
        with self.lock:
            changed=key!=self.current['prefix_hash']
            self.current.update(picks=picks,prefix_hash=key,last_checked_at=fetched,error=None,
                platform_status=info['status'])
            if changed: self.current['result']=None
        # Archive one immutable prefix receipt, rather than repeat unchanged data.
        from .lab import write_json
        path=self.w.root/'live'/'observations'/(key+'.json')
        if not path.exists():
            write_json(path,{'observed_at':fetched,'picks':picks,'evidence':client.evidence})
        write_json(self.w.root/'live'/'latest.json',self.snapshot())
        return changed

    def publish(self,key,result):
        from .lab import write_json
        with self.lock:
            if key!=self.current['prefix_hash'] or self.current['error']:
                return False
            self.current['result']=json.loads(json.dumps(result))
            write_json(self.w.root/'live'/'latest.json',self.snapshot())
            if result['stage']=='availability_updated':
                forecast=result.get('next_opponent_forecast')
                if forecast:
                    from .opponents import archive_forecast
                    archive_forecast(self.w.root/'live'/'forecasts',forecast)
            if result['stage']=='budget_complete':
                write_json(self.w.root/'live'/'decisions'/(digest(result)+'.json'),result)
        return True

    def snapshot(self):
        with self.lock:
            x=json.loads(json.dumps(self.current))
        age=None if x['last_checked_at'] is None else time.time()-x['last_checked_at']
        x['network_age_seconds']=age
        x['stale']=age is None or age>15 or bool(x['error'])
        x['actionable']=not x['stale'] and x.get('platform_status')=='drafting' and bool(x.get('result',{}).get('on_clock') if x.get('result') else False)
        if not x['actionable'] and x.get('result'):
            x['result']['recommendation']=None
            x['result']['provisional_choice']=None
        return x

    def run_polling(self,interval=5):
        while not self.stop.is_set():
            try: self.poll()
            except Exception as exc:
                with self.lock:
                    self.current['error']=str(exc)
                from .lab import write_json
                write_json(self.w.root/'live'/'latest.json',self.snapshot())
            self.stop.wait(interval)


PAGE='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moneyball live draft</title><style>body{background:#10161c;color:#e8edf1;font:16px system-ui;max-width:1180px;margin:40px auto;padding:0 24px}h1{font-size:30px}p{line-height:1.5;color:#b8c6d2}.flag{padding:14px;border:1px solid #426373;border-radius:6px}table{border-collapse:collapse;width:100%;margin:24px 0}th,td{text-align:left;padding:12px;border-bottom:1px solid #34424d}small{color:#9bafbf}b{color:#90dfc2}.bad{color:#ffb5a1}pre{white-space:pre-wrap}</style>
<h1>Moneyball · live draft</h1><p>Championship comparisons update from observed picks. Source age and unresolved uncertainty remain visible.</p><div id="status" class="flag">Connecting…</div><div id="board"></div><p id="detail"></p>
<script>
const esc=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=x=>Number.isFinite(x)?x.toFixed(3):'—';
let renderedKey=null;
async function update(){try{let s=await(await fetch('/state',{cache:'no-store'})).json(),r=s.result;
document.getElementById('status').innerHTML=(s.stale?'<strong class="bad">STALE — do not act on this board.</strong>':'<b>Feed current</b>')+' · '+esc(s.platform_status)+' · '+s.picks.length+' observed picks · age '+fmt(s.network_age_seconds)+' seconds'+(s.error?' · '+esc(s.error):'');
if(!r){renderedKey=null;document.getElementById('board').textContent='New state received; evaluation pending.';return;}
let key=[r.prefix_hash,r.stage,r.completed_outer_blocks_per_mode,s.stale,s.actionable].join('|');if(key===renderedKey)return;renderedKey=key;
let modes=r.scenario_reports||{},cols=Object.keys(modes);let text='<p>Next own pick: '+esc(r.next_own_pick)+' · '+(r.on_clock?'Your turn':'Waiting for '+r.picks_before_own_turn+' intervening selections')+' · '+esc(r.stage)+' · '+fmt(r.elapsed_seconds)+' seconds</p>';
text+='<p>'+esc(r.opponent_observations)+' real opponent choices observed. Forecast inputs are '+fmt(r.forecast_age_hours)+' hours old. These model scenarios are not calibrated odds.</p>';
let fast=r.fast_availability||{};
if(fast.next_turn){text+='<h2>Availability at your next turn</h2><table><tr><th>Candidate</th><th>Modeled chance still available</th></tr>';for(let c of r.candidates){let f=fast.next_turn.candidates.find(x=>x.player_id===c.id);text+='<tr><td>'+esc(c.name)+'</td><td>'+fmt(f?.survival_probability)+'</td></tr>';}text+='</table>';}
else if(r.on_clock&&Object.keys(fast).length&&!fast.status){text+='<h2>If you wait until the following turn</h2><p>Rows are the player selected now; cells are modeled availability of each column player at the following own pick. This is conditional on the opponent model.</p><table><tr><th>Select now</th>'+r.candidates.map(c=>'<th>'+esc(c.name)+'</th>').join('')+'</tr>';for(let c of r.candidates){text+='<tr><td>'+esc(c.name)+'</td>'+r.candidates.map(t=>'<td>'+(c.id===t.id?'—':fmt(fast[c.id]?.candidates.find(x=>x.player_id===t.id)?.survival_probability))+'</td>').join('')+'</tr>';}text+='</table>';}
text+='<h2>Championship comparison</h2>';
text+='<table><tr><th>Candidate</th>'+cols.map(m=>'<th>'+esc(m)+'<br><small>expected titles / '+r.years+' year(s)</small></th>').join('')+'<th>Worst model regret</th></tr>';
for(let c of r.candidates){text+='<tr><td>'+esc(c.name)+(r.provisional_choice===c.id?' <b>· provisional</b>':'')+'</td>'+cols.map(m=>'<td>'+fmt(modes[m].rows.find(x=>x.id===c.id)?.expected_titles)+'</td>').join('')+'<td>'+fmt(r.maximum_regret_estimates?.[c.id])+'</td></tr>';}
text+='</table>';
for(let m of cols){text+='<details><summary>'+esc(m)+' — intervals for differences from reference</summary><p>Anytime simultaneous Monte Carlo bounds only. They exclude errors in player forecasts, opponent beliefs and league-rule assumptions.</p><table><tr><th>Candidate</th><th>Difference</th><th>Lower</th><th>Upper</th></tr>';for(let c of modes[m].rows){text+='<tr><td>'+esc(c.name)+'</td><td>'+fmt(c.delta_vs_reference)+'</td><td>'+fmt(c.paired_ci.lower)+'</td><td>'+fmt(c.paired_ci.upper)+'</td></tr>';}text+='</table></details>';}
document.getElementById('board').innerHTML=text;
document.getElementById('detail').textContent=(r.decision_status||'Updating the opponent model…')+' · '+(r.completed_outer_blocks_per_mode||0)+' independent draft blocks per model. '+(r.confidence_scope||'No calibrated win probabilities.');
}catch(e){document.getElementById('status').textContent='Connection failed — board stale: '+e;}}
update();setInterval(update,1000);
</script>'''


def serve(feed,port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/','/state'):
                self.send_error(404); return
            body=PAGE.encode() if self.path=='/' else json.dumps(feed.snapshot()).encode()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8' if self.path=='/' else 'application/json')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server


def run(warehouse,store,alias,*,args):
    alias = store.resolve_alias(alias)
    from .lab import write_json
    from .cli import emit
    inputs=load_inputs(warehouse,store,alias,distribution=args.distribution,horizon=args.horizon,seed=args.seed)
    feed=LiveFeed(store,warehouse,inputs,offline=args.offline)
    feed.poll()
    server=serve(feed,args.port) if args.watch_live else None
    if args.watch_live:
        threading.Thread(target=feed.run_polling,args=(5,),daemon=True).start()
        emit({'live_url':f'http://127.0.0.1:{args.port}','poll_seconds':5,'writes_performed':False})
    last=None
    try:
        while True:
            current=feed.snapshot(); key=current['prefix_hash']
            if key!=last and not current['error']:
                last=key
                append_event(warehouse.root,{'kind':'preregister_live_comparison','prefix_hash':key,
                    'opponent_real_n':len(current['picks'])-sum(snake_owner(int(p['pick_no']))==inputs['slot'] for p in current['picks']),
                    'candidates':args.candidates,'candidate_limit':args.candidate_limit,'modes':MODES,
                    'budget_seconds':args.budget_seconds,'outer_block_cap':args.max_blocks,
                    'years':args.years,'inner_draws':args.live_inner_draws,
                    'seed':args.seed,'distribution':args.distribution,'horizon':args.horizon,
                    'missing_forecast':args.missing_forecast,'projection_hash':digest(inputs['projection']),
                    'success':'prospective choice scores improve and benefit survives scenario stress; simulation confidence alone is insufficient'})
                def publish(r):
                    if feed.publish(key,r) and r['stage'] in ('state_updated','availability_updated','budget_complete'):
                        emit({'stage':r['stage'],'prefix_hash':key,'on_clock':r.get('on_clock'),
                            'outer_blocks':r.get('completed_outer_blocks_per_mode',0),
                            'elapsed_seconds':r.get('elapsed_seconds'),
                            'provisional_choice':r.get('provisional_choice'),
                            'path':str(warehouse.root/'live'/'latest.json')})
                result=evaluate(inputs['projection'],current['picks'],slot=inputs['slot'],
                    candidates=args.candidates or (),candidate_limit=args.candidate_limit,
                    nfl_schedule=inputs['nfl_schedule'],budget_seconds=args.budget_seconds,
                    max_blocks=args.max_blocks,inner_draws=args.live_inner_draws,years=args.years,seed=args.seed,
                    persistent_shock=args.horizon=='transition',missing_forecast=args.missing_forecast,
                    on_update=publish,cancelled=lambda:feed.stop.is_set() or feed.snapshot()['prefix_hash']!=key)
                if not args.watch_live:
                    # One-shot calculation must re-read before claiming currentness.
                    if not args.offline: feed.poll()
                    if feed.current['prefix_hash']!=key:
                        return {'status':'superseded_by_new_pick','recommendation':None,'path':str(warehouse.root/'live'/'latest.json')}
                    return {'status':result['stage'],'path':str(warehouse.root/'live'/'latest.json'),
                        'elapsed_seconds':result.get('elapsed_seconds'),'on_clock':result.get('on_clock'),
                        'completed_outer_blocks_per_mode':result.get('completed_outer_blocks_per_mode'),
                        'provisional_choice':(feed.snapshot().get('result') or {}).get('provisional_choice'),
                        'certified_choice':(feed.snapshot().get('result') or {}).get('recommendation'),
                        'stale':feed.snapshot()['stale'],'actionable':feed.snapshot()['actionable'],
                        'confidence':'conditional simulation only'}
            if not args.watch_live: break
            feed.stop.wait(.5)
    except KeyboardInterrupt:
        return {'status':'stopped','writes_performed':False}
    finally:
        feed.stop.set()
        if server: server.shutdown()
