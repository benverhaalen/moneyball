"""Bounded cross-source operations used by both interfaces."""
from copy import deepcopy
from collections import Counter
import time
from .auth import espn_headers
from .http import Client
from .store import DataError, digest, timestamp

def acquisition_context(service, alias, positions=None, limit=10, fresh=False, query=None, _league=None, _player_ids=None):
    d = _league if _league is not None else service.snapshot(alias, fresh=fresh)["data"]
    config = service.store.get("config", alias)["data"]
    positions = positions or ["QB","RB","WR","TE"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise DataError("Acquisition pool limit must be an integer from 1 to 50")
    if _player_ids is not None and (not isinstance(_player_ids, list) or not 1 <= len(_player_ids) <= 20):
        raise DataError("Focused acquisition lookup requires one to twenty exact IDs")
    if config["platform"] == "espn":
        from .free_agents import fetch_espn_free_agents
        packet = fetch_espn_free_agents(Client(service.store,force=fresh),config,espn_headers(config["auth_mode"]),
                                       scoring_period_id=d["week"],positions=positions,limit=limit)
    else:
        catalog = service.store.get("catalog","sleeper")
        if _player_ids is not None:
            positions = sorted({pos for pid in _player_ids for pos in
                                (catalog["data"].get(pid, {}).get("fantasy_positions") or
                                 [catalog["data"].get(pid, {}).get("position")]) if pos})
        owned = {p for t in d["teams"].values() for p in t["player_ids"]}
        rows = []
        for pid,p in catalog["data"].items():
            name = p.get("full_name") or (p.get("first_name","")+" "+p.get("last_name","")).strip()
            pos = p.get("fantasy_positions") or [p.get("position")]
            if pid in owned or p.get("active") is not True or not set(pos)&set(positions) or (query and query.casefold() not in name.casefold() and query != pid) or (_player_ids is not None and pid not in _player_ids):
                continue
            rows.append({"player_id":pid,"name":name,"positions":pos,"injury_status":p.get("injury_status"),"acquisition_status":"UNOWNED_STATUS_UNVERIFIED","execution_actionable":None,"projection":None})
        rows.sort(key=lambda p:p["name"])
        packet = {"players":rows[:min(50,max(1,limit))],"observed_at":catalog["available_at"],
                  "source":{"provider":"sleeper","url":"https://docs.sleeper.com/"},
                  "horizon":{"kind":"week","season":d["season"],"week":d["week"]},
                  "waiver_state":{"current_order":None,"own_current_rank":None,"rules":d["rules"]["waivers"]},
                  "scope":{"population":"Unowned active entries from daily catalog; sorted alphabetically, not a recommendation","total_matches":len(rows),"unknowns":["current waiver/free-agent state","priority order semantics","pending claims","current role and health"]},"receipts":[]}
    packet["league_binding"] = {k: d[k] for k in ("platform", "league_id", "season")}
    service.store.put("pool",alias,packet,{"source_receipts":packet.get("receipts",[])})
    if query:
        packet = deepcopy(packet)
        packet["players"] = [p for p in packet["players"] if query.casefold() in p["name"].casefold() or query==p["player_id"]]
    compact = {k:v for k,v in packet.items() if k!="raw"}
    if compact.get("waiver_state"):
        compact["waiver_state"] = {k:v for k,v in compact["waiver_state"].items() if k!="raw"}
    order = (compact.get("waiver_state") or {}).get("current_order") or []
    own_rank = (compact.get("waiver_state") or {}).get("own_current_rank")
    ahead = {r["team_id"] for r in order if own_rank is not None and r.get("rank") is not None and r["rank"]<own_rank}
    compact["teams_currently_ahead"] = [{"id":tid,"name":d["teams"][tid]["name"],
         "relevant_roster":[{"id":p,"name":d["players"][p]["name"],"positions":d["players"][p]["positions"],"injury_status":d["players"][p].get("injury_status")} for p in d["teams"][tid]["player_ids"] if set(d["players"][p]["positions"])&set(positions)]} for tid in sorted(ahead) if tid in d["teams"]]
    compact["prediction_scope"] = "Roster needs support explicit claim/no-claim scenarios, not known intentions or calibrated probabilities. Current order may reset before processing."
    return compact

def _pickup_inputs(service, alias, player_ids, evaluation_week, fresh, as_of=None):
    if fresh and as_of is not None:
        raise DataError("Historical pickup evaluation cannot refresh future evidence")
    cutoff=timestamp(as_of)
    if cutoff>time.time()+60:
        raise DataError("Decision cutoff cannot be in the future")
    d = service.snapshot(alias,fresh=fresh,as_of=as_of)["data"]
    if d.get("own_team_id") not in d["teams"]:
        raise DataError("Connect the user's exact team before evaluating a pickup")
    stored = service.store.get("pool",alias,as_of,required=False)
    if fresh:
        acquisition_context(service,alias,limit=50,fresh=fresh,_league=d,
                            _player_ids=list(map(str, player_ids)) if d["platform"] == "sleeper" else None)
        stored = service.store.get("pool",alias)
    if not stored:
        raise DataError("No preserved current acquisition pool at this cutoff; fetch acquisition_context first")
    pool = stored["data"]
    candidates={}
    for pid in map(str,player_ids):
        row = next((p for p in pool["players"] if p["player_id"]==pid),None)
        if row is None:
            raise DataError("Candidate is not in the bounded observed pool; query acquisition_context for the matching position before evaluation")
        candidates[pid]={**row, **{k:pool.get(k) for k in ("observed_at","expires_at","source","horizon")}}
    week = d["week"] if evaluation_week is None else evaluation_week
    if isinstance(week,bool) or not isinstance(week,int) or not 1<=week<=18:
        raise DataError("Evaluation week must be an integer in 1..18")
    relevant=set(d["teams"][d["own_team_id"]]["player_ids"]) | set(candidates)
    forecasts = [r for r in service.forecast_rows(alias,as_of) if r["player_id"] in relevant]
    if d["platform"] == "espn":
        from .free_agents import fetch_espn_free_agents
        config = service.store.get("config",alias)["data"]
        key=f"{alias}:{week}"
        future_record=service.store.get("forecast_pool",key,as_of,required=False)
        if fresh:
            positions=sorted({pos for row in candidates.values() for pos in row["positions"]})
            future = fetch_espn_free_agents(Client(service.store),config,espn_headers(config["auth_mode"]),
                                           scoring_period_id=week,positions=positions,limit=50,include_waiver_state=False)
            future["scoring_hash"]=digest(d["rules"]["scoring"])
            service.store.put("forecast_pool",key,future)
            future_record=service.store.get("forecast_pool",key)
        if future_record:
            future=future_record["data"]
            for projected in future["players"]:
                if projected["player_id"] in candidates and projected.get("projection"):
                    # Only the forecast crosses periods; acquisition status stays at the current period.
                    forecasts.append({**projected["projection"],"player_id":projected["player_id"],"available_at":future["observed_at"],
                                      "local_batch_available_at":future_record["available_at"],
                                      "scoring_hash":future.get("scoring_hash"),"source_url":future["source"]["url"]})
    # For a fresh decision include the just-acquired evidence. Historical cutoffs stay fixed.
    return d,pool,candidates,forecasts,week,(time.time() if as_of is None else cutoff)

def pickup_packet(service, alias, player_id, drop_player_id=None, evaluation_week=None, fresh=True, unavailable=None, expanded=False, as_of=None):
    from .waivers import evaluate_pickup
    d,pool,candidates,forecasts,week,cutoff=_pickup_inputs(service,alias,[player_id],evaluation_week,fresh,as_of)
    candidate=candidates[str(player_id)]
    result = evaluate_pickup(d,d["own_team_id"],candidate,drop_player_id,forecasts,week,cutoff,unavailable)
    packet = {"league":{"alias":alias,"name":d["name"],"format":d["format"]}, "strategy":service.strategy(alias,as_of=as_of),
              "decision_at":cutoff,"evidence_as_of":as_of,"refresh_performed":fresh,
              "candidate":candidate,"waiver_state":{k:v for k,v in (pool.get("waiver_state") or {}).items() if k!="raw"},
              "comparison":result,"verdict":None,"championship_probability_delta":None,
              "decision_questions":["What starter or coverage improves after the actual drop?","What can the same priority or budget buy instead?","Which teams ahead could want the target, and which fallback remains in each scenario?","What changes under a hold or trade alternative?"]}
    receipt=service.store.put("pickup_packet",alias,packet)
    if expanded:
        return {**packet,"receipt":receipt}
    players={**d["players"],**candidates}
    packet["comparison"] = compact_pickup(result,players)
    return {**packet,"receipt":receipt,"expanded_packet":"saved_packet(alias,kind='pickup_packet')"}

def claim_scenarios(service,alias,claims,evaluation_week=None,fresh=True,unavailable=None,expanded=False,as_of=None):
    """Reachable ordered alternatives; conditional successes, not a platform queue emulator."""
    from .waivers import evaluate_claim_scenarios
    if not isinstance(claims,list) or not 1<=len(claims)<=20 or any(not isinstance(c,dict) or not c.get("player_id") for c in claims):
        raise DataError("Supply 1..20 ordered claims with exact player_id and any drop_player_id")
    d,pool,candidates,forecasts,week,cutoff=_pickup_inputs(service,alias,[c["player_id"] for c in claims],evaluation_week,fresh,as_of)
    inputs=[{**c,"candidate":candidates[str(c["player_id"])]} for c in claims]
    result=evaluate_claim_scenarios(d,d["own_team_id"],inputs,forecasts,week,cutoff,unavailable)
    packet={"league":{"alias":alias,"name":d["name"]},"strategy":service.strategy(alias,as_of=as_of),
            "decision_at":cutoff,"comparison":result,"waiver_state":{k:v for k,v in (pool.get("waiver_state") or {}).items() if k!="raw"},
            "verdict":None,"championship_probability_delta":None}
    receipt=service.store.put("claim_packet",alias,packet)
    if not expanded:
        for branch in result.get("scenarios",[]):
            key="conditional_on_this_target_being_acquired"
            branch[key]=compact_pickup(branch[key],{**d["players"],**candidates})
    return {**packet,"receipt":receipt}

def compact_pickup(result, players):
    def names(ids):
        return [{"id":p,"name":players.get(p,{}).get("name",p)} for p in ids]
    cohorts=[]
    for row in result.get("forecast_comparisons",[]):
        before,after=row.get("before"),row.get("after")
        cohort={k:row.get(k) for k in ("source","period","status","conditioning","source_urls","delta_points","interpretation","capacity_limitation","lineup_timing_limitation")}
        if before and after:
            b,a=set(before["selected_player_ids"]),set(after["selected_player_ids"])
            cohort["new_starters"]=names(sorted(a-b)); cohort["displaced_starters"]=names(sorted(b-a))
            cohort["before_lineup"]=[{"slot":r["slot_label"],"id":r["player_id"],"name":players.get(r["player_id"],{}).get("name")} for r in before["selected"]]
            cohort["after_lineup"]=[{"slot":r["slot_label"],"id":r["player_id"],"name":players.get(r["player_id"],{}).get("name")} for r in after["selected"]]
        cohort["missing_forecasts"]=names(row.get("missing_required_player_ids",[]))
        cohorts.append(cohort)
    output={k:result.get(k) for k in ("valid","errors","warnings","evidence_needed","roster_branch_feasible","roster_feasibility","platform_transaction_completed","evaluation_week","claim_cost")}
    candidate=result.get("candidate") or {}
    output["candidate"]={k:candidate.get(k) for k in ("player_id","name","positions","acquisition_status")}
    output["comparisons"]=cohorts
    output["rejected_forecast_counts"]=dict(Counter(r["code"] for r in result.get("forecast_rejections",[])))
    output["drop_player"]=names([result["drop_player_id"]]) if result.get("drop_player_id") else []
    output["coverage"]={side:{"counts":result.get(side,{}).get("roster",{}).get("counts"),
                            "bench":names([r["player_id"] for r in result.get(side,{}).get("coverage",{}).get("players",[]) if r.get("bench_coverage")])} for side in ("before","after")}
    return output
