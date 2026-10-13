"""Bounded mechanical action discovery and explicit roster transitions.

Ordering is reproducible coverage, never a value ranking. The caller supplies
preferences and dated evidence; transaction consent and future forecasts remain
unknown. Existing trade/waiver engines own feasibility and scoring.
"""
from copy import deepcopy
from itertools import combinations, zip_longest

from .pagination import decode_cursor, encode_cursor
from .store import DataError, digest
from .trades import compare_trade, _team_snapshot, _capacity, _lineup
from .waivers import evaluate_pickup

VERSION = "candidate-mechanics-v1"


def _feasible(comparison):
    return comparison.get("executable", comparison.get("roster_branch_feasible", False))


def _ids(raw, field):
    if not isinstance(raw, list) or any(not isinstance(x, str) or not x for x in raw) or len(set(raw)) != len(raw):
        raise DataError(field + " must contain unique exact string IDs")
    return raw


def _team_index(league):
    index = {}
    for tid, team in sorted(league["teams"].items()):
        snapshot = _team_snapshot(team, league["rules"].get("starters", []), league["rules"])
        membership = _lineup(snapshot["ordinary_player_ids"], league["rules"].get("starters", []),
                             league["players"], {p: 0 for p in snapshot["ordinary_player_ids"]}, set())
        index[tid] = {"roster": snapshot, "capacity": _capacity(snapshot),
                      "structural_slot_coverage": {"complete": membership["complete"], "filled_slots": membership["filled_slots"],
                          "required_slots": membership["required_slots"], "unfilled_slot_indexes": [s["slot_index"] for s in membership["selected"] if s["player_id"] is None],
                          "scope": "Observed position eligibility only; no forecast, health probability or team preference"},
                      "position_membership": {pos: sorted(pid for pid in team["player_ids"]
                          if pos in league["players"].get(pid, {}).get("positions", []))
                          for pos in sorted({p for pid in team["player_ids"] for p in league["players"].get(pid, {}).get("positions", [])})},
                      "pick_ids": sorted(p["id"] for p in league.get("picks", []) if p.get("owner_team_id") == tid)}
    return index


def _assets(league, tid):
    return [("player_id", p) for p in sorted(league["teams"][tid]["player_ids"])] + [
        ("pick_id", p["id"]) for p in sorted(league.get("picks", []), key=lambda p:p["id"]) if p.get("owner_team_id") == tid]


def _trade_stream(league, own, other, shapes):
    left, right = _assets(league, own), _assets(league, other)
    for a, b in shapes:
        for outgoing in combinations(left, a):
            for incoming in combinations(right, b):
                proposal = {"transfers": [], "pick_transfers": [], "drops": {own: [], other: []}}
                for source, target, package in ((own, other, outgoing), (other, own, incoming)):
                    for key, ident in package:
                        proposal["transfers" if key == "player_id" else "pick_transfers"].append(
                            {key: ident, "from_team": source, "to_team": target})
                # Name every minimally required ordinary cut; no implicit best cut.
                cut_options = []
                for tid in (own, other):
                    team = league["teams"][tid]
                    ordinary = set(team["player_ids"]) - set(team["reserve"]) - set(team["taxi"])
                    outgoing_ids = {t["player_id"] for t in proposal["transfers"] if t["from_team"] == tid}
                    incoming_ids = {t["player_id"] for t in proposal["transfers"] if t["to_team"] == tid}
                    after_ids = sorted((ordinary - outgoing_ids) | incoming_ids)
                    snapshot = _team_snapshot(team, league["rules"].get("starters", []), league["rules"])
                    limit = snapshot["limits"]["ordinary"]
                    required = max(0, len(after_ids) - limit) if limit is not None else 0
                    cut_options.append((after_ids, required))
                # Recreate the inner iterator; never materialize combinatorial cuts.
                for cut_left in combinations(*cut_options[0]):
                    for cut_right in combinations(*cut_options[1]):
                        candidate = deepcopy(proposal)
                        # compare_trade requires drops be initially owned, so incoming
                        # players cannot be immediately cut in a single exchange.
                        if set(cut_left) - set(league["teams"][own]["player_ids"]) or set(cut_right) - set(league["teams"][other]["player_ids"]):
                            continue
                        candidate["drops"] = {own: list(cut_left), other: list(cut_right)}
                        yield {"kind": "Trade", "proposal": candidate}


def _pool_rows(pool):
    if pool is None:
        return []
    if not isinstance(pool, dict) or not isinstance(pool.get("players"), list):
        raise DataError("Pool must preserve players and membership coverage")
    ids = _ids([r.get("player_id") for r in pool["players"]], "pool player_ids")
    return sorted(pool["players"], key=lambda p: p["player_id"])


def discover_candidates(league, pool=None, *, limit=20, cursor=None, kinds=None,
                        focus_player_ids=None, forecasts=None, decision_at=None,
                        package_shapes=None):
    """Page broad explicit candidates; hold is supplied on every page.

    Complete membership is only asserted relative to caller-preserved coverage.
    All rival teams are indexed before enumeration; round-robin team streams
    prevent the first team's packages consuming every first-page position.
    Default packages include 1:1, 1:2 and 2:1 players and owned picks, plus every
    unowned pool member paired with no-cut/all owned-cut alternatives. Larger
    and multi-team packages are explicitly outside this bounded universe.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise DataError("Candidate limit must be 1..50")
    own = league.get("own_team_id")
    if own not in league.get("teams", {}):
        raise DataError("Exact own team required")
    kinds = kinds or ["Trade", "AddDrop"]
    if not isinstance(kinds, list) or set(kinds) - {"Trade", "AddDrop"}:
        raise DataError("Candidate kinds must be Trade/AddDrop")
    focus = set(_ids(focus_player_ids or [], "focus_player_ids"))
    shapes = package_shapes or [[1, 1], [1, 2], [2, 1]]
    if not isinstance(shapes, list) or len(shapes) > 3 or any(not isinstance(s, (list, tuple)) or len(s) != 2 or any(isinstance(n, bool) or not isinstance(n, int) or n not in (1, 2) for n in s) for s in shapes):
        raise DataError("Package shapes require up to three pairs of counts 1 or 2")
    validation = compare_trade(league, {"drops": {tid: [] for tid in league["teams"]}})
    if not validation.get("valid"):
        raise DataError("Cannot discover against invalid league ownership/rules: " + ",".join(e["code"] for e in validation.get("errors", [])))
    rows = _pool_rows(pool)
    expected_binding = {k: league.get(k) for k in ("platform", "league_id", "season")}
    pool_binding = (pool or {}).get("league_binding")
    if pool_binding is not None and pool_binding != expected_binding:
        raise DataError("Acquisition membership belongs to different league/season")
    observed_ownership = (pool or {}).get("ownership_digest")
    current_ownership = digest(sorted({p for t in league["teams"].values() for p in t["player_ids"]}))
    ownership_current = observed_ownership is None or observed_ownership == current_ownership
    pool_membership_verified = pool_binding == expected_binding and ownership_current
    binding = digest({"version": VERSION, "league": league, "pool": pool, "kinds": kinds,
                      "focus": sorted(focus), "shapes": shapes, "forecasts": forecasts, "decision_at": decision_at})
    offset, cutoff, _ = decode_cursor(cursor, binding) if cursor else (0, decision_at or 0, None)
    index = _team_index(league)
    streams = []
    if "Trade" in kinds:
        streams.extend(_trade_stream(league, own, tid, shapes) for tid in sorted(index) if tid != own)
    owned = {p for t in league["teams"].values() for p in t["player_ids"]}
    excluded_pool = sorted(r["player_id"] for r in rows if r["player_id"] in owned)
    if "AddDrop" in kinds:
        def acquisitions():
            for row in rows:
                if row["player_id"] in owned:
                    continue
                candidate = {**row, **{k: pool[k] for k in ("observed_at", "expires_at", "horizon", "source") if k in pool}}
                for drop in [None] + sorted(league["teams"][own]["player_ids"]):
                    yield {"kind": "AddDrop", "player_id": row["player_id"], "drop_player_id": drop, "candidate": candidate}
        streams.append(acquisitions())
    page, total_seen, has_more = [], 0, False
    # Lazy enumeration bounds mechanical evaluation to this page; no scoring
    # is needed to retain a complementary/future-facing package.
    for group in zip_longest(*streams):
        for action in group:
            if action is None:
                continue
            ids = {t["player_id"] for t in action.get("proposal", {}).get("transfers", [])} | {action.get("player_id"), action.get("drop_player_id")}
            if focus and not focus.intersection(ids):
                continue
            if total_seen >= offset:
                if len(page) == limit:
                    has_more = True
                    break
                if action["kind"] == "Trade":
                    proposal = {**action["proposal"], **({"decision_at": decision_at} if decision_at is not None else {})}
                    comparison = compare_trade(league, proposal, forecasts)
                else:
                    comparison = evaluate_pickup(league, own, action["candidate"], action["drop_player_id"], forecasts, league.get("week"), decision_at)
                page.append({"id": digest(action), "action": action, "comparison": comparison,
                             "legal_status": "invalid" if not comparison.get("valid") else "feasible_under_checked_rules" if _feasible(comparison) else "review_required",
                             "acceptance_probability": None})
            total_seen += 1
        if has_more:
            break
    hold = compare_trade(league, {"drops": {tid: [] for tid in index}, **({"decision_at": decision_at} if decision_at is not None else {})}, forecasts)
    return {"version": VERSION, "hold": {"kind": "Hold", "comparison": hold}, "candidates": page,
            "all_team_index": index, "next_cursor": encode_cursor(binding, offset + len(page), cutoff) if has_more else None,
            "provenance": {"league_digest": digest(league), "pool_digest": digest(pool), "source_receipts": league.get("source_receipts", []), "pool_source": (pool or {}).get("source")},
            "coverage": {"team_ids_screened": sorted(index), "owned_player_ids": sorted(owned), "pool_member_count": len(rows),
                         "pool_owned_conflicts": excluded_pool, "pool_complete_membership_relative_to_snapshots": bool(pool_membership_verified and ((pool or {}).get("coverage", {}).get("complete_membership_relative_to_snapshots") or
                             ((pool or {}).get("scope", "").startswith("Complete unowned active") and observed_ownership is not None))),
                         "pool_binding_verified": pool_binding == expected_binding,
                         "pool_ownership_binding_current": ownership_current,
                         "pool_membership_gaps": (["pool_binding_missing"] if pool is not None and pool_binding is None else []) +
                             (["pool_ownership_snapshot_changed"] if not ownership_current else []),
                         "pool_execution_eligibility_verified": bool((pool or {}).get("coverage", {}).get("execution_eligibility_verified")),
                         "returned": len(page), "enumeration_complete": not has_more, "package_shapes": shapes,
                         "ordering": "round_robin_team_and_pool_then_exact_asset_ids; no value ranking",
                         "omitted": ["packages larger than configured shapes", "multi-team exchanges", "unknown/unobserved pool members", "forecast-driven recommendations"]},
            "verdict": None, "championship_probability_delta": None}


def _apply_trade(state, comparison):
    for tid, row in comparison["teams"].items():
        after = row["after"]
        for key in ("player_ids", "reserve", "taxi"):
            state["teams"][tid][key] = list(after[key])
        state["teams"][tid]["starters"] = list(after["declared_starters"])
    for transfer in comparison["proposal"]["pick_transfers"]:
        next(p for p in state.get("picks", []) if p["id"] == transfer["pick_id"])["owner_team_id"] = transfer["to_team"]


def compare_roster_plan(league, plan, forecasts=None):
    """Sequential explicit roster branches; no future acquisition policy inferred.

    steps accept Hold, Trade(proposal), AddDrop(candidate, team_id,
    drop_player_id), Placement(team_id, player_id, destination='ordinary'|
    'reserve'|'taxi', eligibility_verified=True), ReleaseRight(team_id,pick_id),
    ExerciseRight(team_id,pick_id,player_id,eligibility_verified=True), Retain(season,
    teams={team_id:[ids]}, placements={team_id:{reserve:[],taxi:[],
    eligibility_verified:True}}). Future retention must name every team's holdings;
    omitted future placements mean ordinary roster, never inherited IR/taxi.
    Canonical observed rules.retention: mode='all'|'none'|'keeper'; keeper
    requires max_keepers and players={id:{eligible:bool,cost_pick_id:str|null}}.
    Unsupported provider retention extras remain explicit gaps. Pick costs are
    consumed, with exact ownership checked; no scalar keeper/player value.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not 1 <= len(plan["steps"]) <= 30:
        raise DataError("RosterPlan requires 1..30 explicit steps")
    state, outcomes, gaps = deepcopy(league), [], []
    hold = compare_trade(league, {"drops": {tid: [] for tid in league["teams"]}}, forecasts)
    for ordinal, step in enumerate(plan["steps"]):
        if not isinstance(step, dict):
            raise DataError("Roster plan steps must be objects")
        trial = deepcopy(state)
        kind = step.get("kind")
        step_gaps = []
        if kind in ("Trade", "Hold"):
            proposal = step.get("proposal", {}) if kind == "Trade" else {"drops": {tid: [] for tid in trial["teams"]}}
            comparison = compare_trade(trial, proposal, forecasts)
            if comparison.get("valid") and _feasible(comparison):
                _apply_trade(trial, comparison)
        elif kind == "AddDrop":
            tid = step.get("team_id", trial.get("own_team_id"))
            comparison = evaluate_pickup(trial, tid, step.get("candidate", {}), step.get("drop_player_id"), forecasts, trial.get("week"), step.get("decision_at"))
            if comparison.get("valid") and _feasible(comparison):
                row = comparison["after"]["roster"]
                trial["players"][step["candidate"]["player_id"]] = deepcopy(step["candidate"])
                for key in ("player_ids", "reserve", "taxi"):
                    trial["teams"][tid][key] = list(row[key])
                trial["teams"][tid]["starters"] = list(row["declared_starters"])
        elif kind == "Placement":
            tid, pid, destination = step.get("team_id"), step.get("player_id"), step.get("destination")
            if tid not in trial["teams"] or pid not in trial["teams"][tid]["player_ids"] or destination not in ("ordinary", "reserve", "taxi"):
                raise DataError("Placement requires owned player and exact destination")
            if destination != "ordinary" and step.get("eligibility_verified") is not True:
                step_gaps.append({"code": "placement_eligibility_unverified", "player_id": pid})
            team = trial["teams"][tid]
            for key in ("reserve", "taxi", "starters"):
                if pid in team[key]:
                    team[key].remove(pid)
            if destination != "ordinary":
                team[destination].append(pid)
            comparison = compare_trade(trial, {"drops": {tid: []}}, forecasts)
        elif kind in ("ReleaseRight", "ExerciseRight"):
            tid, right = step.get("team_id"), step.get("pick_id")
            pick = next((p for p in trial.get("picks", []) if p["id"] == right), None)
            if tid not in trial["teams"] or not pick or pick.get("owner_team_id") != tid:
                raise DataError("Future-right operation requires an exact currently owned pick")
            if kind == "ExerciseRight":
                pid = step.get("player_id")
                if not isinstance(pid, str) or pid not in trial["players"] or any(pid in t["player_ids"] for t in trial["teams"].values()):
                    raise DataError("ExerciseRight requires a known unowned player")
                if step.get("eligibility_verified") is not True or pick.get("season") != trial["season"]:
                    step_gaps.append({"code": "right_exercise_eligibility_or_season_unverified", "pick_id": right})
                trial["teams"][tid]["player_ids"].append(pid)
            trial["picks"] = [p for p in trial["picks"] if p["id"] != right]
            comparison = compare_trade(trial, {"drops": {tid: []}}, forecasts)
        elif kind == "Retain":
            season, retained = step.get("season"), step.get("teams")
            if isinstance(season, bool) or not isinstance(season, int) or season != trial["season"] + 1 or not isinstance(retained, dict) or set(retained) != set(trial["teams"]):
                raise DataError("Retain requires next season and explicit holdings for every team")
            policy = trial["rules"].get("retention", {})
            mode = policy.get("mode")
            if mode not in ("all", "none", "keeper"):
                step_gaps.append({"code": "exact_retention_rules_required"})
            consumed = set()
            for tid, raw in retained.items():
                ids = _ids(raw, "retained players")
                owned = set(trial["teams"][tid]["player_ids"])
                if set(ids) - owned:
                    raise DataError("Cannot retain unowned players")
                if mode == "none" and ids or mode == "all" and set(ids) != owned:
                    step_gaps.append({"code": "retention_policy_conflict", "team_id": tid})
                if mode == "keeper":
                    maximum = policy.get("max_keepers")
                    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
                        step_gaps.append({"code": "keeper_limit_required", "team_id": tid})
                    elif len(ids) > maximum:
                        step_gaps.append({"code": "keeper_limit_exceeded", "team_id": tid})
                    for pid in ids:
                        terms = policy.get("players", {}).get(pid, {})
                        if terms.get("eligible") is not True or "cost_pick_id" not in terms:
                            step_gaps.append({"code": "keeper_eligibility_or_cost_required", "player_id": pid})
                            continue
                        expires = terms.get("expires_after_season")
                        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int) or expires < season):
                            step_gaps.append({"code": "keeper_right_expired_or_unresolved", "player_id": pid})
                        # A scalar price/escalation is not equivalent to a pick.
                        if any(k in terms for k in ("salary", "round_escalation", "contract_years")):
                            step_gaps.append({"code": "unsupported_keeper_cost_terms", "player_id": pid})
                        cost = terms["cost_pick_id"]
                        if cost is not None:
                            pick = next((p for p in trial.get("picks", []) if p["id"] == cost), None)
                            if not pick or pick.get("owner_team_id") != tid or pick.get("season") != season or cost in consumed:
                                step_gaps.append({"code": "keeper_cost_right_unavailable", "player_id": pid, "pick_id": cost})
                            consumed.add(cost)
                team = trial["teams"][tid]
                team["player_ids"] = list(ids)
                # Future memberships are an explicit transition, not retained
                # current-year reserve/taxi eligibility or declared starters.
                placement = step.get("placements", {}).get(tid, {})
                reserve = _ids(placement.get("reserve", []), "future reserve")
                taxi = _ids(placement.get("taxi", []), "future taxi")
                if set(reserve + taxi) - set(ids) or set(reserve) & set(taxi):
                    raise DataError("Future placement must be disjoint retained membership")
                if (reserve or taxi) and placement.get("eligibility_verified") is not True:
                    step_gaps.append({"code": "future_placement_eligibility_unverified", "team_id": tid})
                team["reserve"], team["taxi"], team["starters"] = list(reserve), list(taxi), []
            trial["picks"] = [p for p in trial.get("picks", []) if p["id"] not in consumed]
            trial["season"] = season
            # Current injury/role observations cannot cross into a future year.
            for player in trial["players"].values():
                player.pop("injury_status", None)
                player.pop("role_status", None)
            comparison = compare_trade(trial, {"drops": {tid: [] for tid in trial["teams"]}}, forecasts)
        else:
            raise DataError("Unsupported roster plan step kind")
        applied = bool(comparison.get("valid") and _feasible(comparison) and not step_gaps)
        changes = {"season": {"before": state["season"], "after": trial["season"]},
                   "teams": {tid: {"released_player_ids": sorted(set(state["teams"][tid]["player_ids"]) - set(team["player_ids"])),
                                    "acquired_player_ids": sorted(set(team["player_ids"]) - set(state["teams"][tid]["player_ids"])),
                                    "reserve_before": state["teams"][tid]["reserve"], "reserve_after": team["reserve"],
                                    "taxi_before": state["teams"][tid]["taxi"], "taxi_after": team["taxi"]}
                             for tid, team in trial["teams"].items()},
                   "rights_before": deepcopy(state.get("picks", [])), "rights_after": deepcopy(trial.get("picks", []))}
        outcomes.append({"index": ordinal, "action": deepcopy(step), "comparison": comparison, "applied": applied,
                         "conditional_state_changes": changes, "gaps": step_gaps})
        gaps.extend(step_gaps)
        if not applied:
            gaps.append({"code": "roster_plan_stopped_at_unverified_step", "index": ordinal})
            break
        state = trial
    return {"version": VERSION, "valid": not gaps and len(outcomes) == len(plan["steps"]),
            "hold": hold, "steps": outcomes, "after": state, "gaps": gaps,
            "remaining_step_count": len(plan["steps"]) - len(outcomes),
            "future_management": plan.get("future_management"), "verdict": None,
            "championship_probability_delta": None,
            "scope": "Conditional explicit transitions only; transaction timing/consent and future performance not predicted"}
