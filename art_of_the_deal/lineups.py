"""Source-coherent weekly lineup counterfactuals; never submit a lineup."""
import time

from .store import DataError, digest, timestamp
from .trades import (_lineup, _normalize_forecasts, _ordinary_ids,
                     _player_positions, _status_record, _UNAVAILABLE_STATUSES)


def packet(service, alias, *, evaluation_week=None, fresh=True, unavailable=None, as_of=None):
    cutoff = timestamp(as_of) if as_of is not None else None
    if cutoff is not None and cutoff > time.time():
        raise DataError("as_of cannot be in the future")
    snap = service.snapshot(alias, fresh=bool(fresh and cutoff is None), as_of=cutoff)
    cutoff = time.time() if cutoff is None else cutoff
    data = snap["data"]
    own = data.get("own_team_id")
    if own is None or str(own) not in data["teams"]:
        raise DataError("Select your team before requesting a lineup packet")
    team = data["teams"][str(own)]
    rules, players = data["rules"], data["players"]
    slots = rules.get("starters")
    if not isinstance(slots, list) or not 1 <= len(slots) <= 16:
        raise DataError("Lineup modeling supports 1 to 16 starter slots")
    if any(not isinstance(s, dict) or not isinstance(s.get("eligible_positions"), list)
           or not s["eligible_positions"] for s in slots):
        raise DataError("Starter slot eligibility must be translated before lineup modeling")
    week = data.get("week") if evaluation_week is None else evaluation_week
    if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 18:
        raise DataError("evaluation_week must be an integer from 1 to 18")
    if unavailable is None:
        unavailable = []
    if not isinstance(unavailable, (list, tuple, set)):
        raise DataError("unavailable must be a player ID list for the evaluation week")
    excluded = {str(p) for p in unavailable}
    if excluded - set(players):
        raise DataError("An unavailable player ID is unknown to this league")
    ordinary = _ordinary_ids(team)
    evidence = [{"code": "lock_state_not_verified", "requirement":
                 "Verify kickoff, current slot locks, and platform eligibility before acting; modeled lineups assume all included players can still move."}]
    for pid in ordinary:
        status, observed, status_week, season = _status_record(players[pid])
        if status in _UNAVAILABLE_STATUSES:
            if observed is not None and observed <= cutoff and status_week == week and season in (None, data["season"]):
                excluded.add(pid)
            else:
                evidence.append({"code": "unavailable_status_not_dated_for_evaluation_week", "player_id": pid})
    required = {pid for pid in ordinary if pid not in excluded and any(
        set(_player_positions(players[pid])) & set(s["eligible_positions"]) for s in slots)}
    groups, rejected = _normalize_forecasts(service.forecast_rows(alias, cutoff), players=players,
        relevant_ids=set(ordinary), league_season=data["season"], league_week=week,
        decision_at=cutoff, scoring=rules["scoring"], scoring_hash=digest(rules["scoring"]))
    comparisons = []
    for key in sorted(groups, key=str):
        g = groups[key]
        if g["period"]["kind"] != "week":
            continue
        missing = sorted(required - set(g["points"]))
        conflicts = sorted(required & set(g["conflicts"]))
        row = {"source": g["source"], "season": g["season"], "period": g["period"],
               "conditioning": g["conditioning"], "source_urls": g["source_urls"],
               "provider_updated_at": g["provider_updated_at"],
               "forecast_available_at_by_player": {p: g["available_at"][p] for p in sorted(required & set(g["points"]))},
               "missing_required_player_ids": missing, "conflicting_player_ids": conflicts,
               "modeled_lineup": None, "status": "incomplete"}
        if not missing and not conflicts:
            modeled = _lineup(ordinary, slots, players, g["points"], excluded)
            for selected in modeled["selected"]:
                selected["name"] = players[selected["player_id"]]["name"] if selected["player_id"] else None
            row["modeled_lineup"] = modeled
            row["status"] = "complete_counterfactual" if modeled["complete"] else "unfillable_slots"
        row["use"] = "Hypothetical legal lineup maximizing this provider's supplied weekly means; lock state unverified."
        comparisons.append(row)
    best_ball = rules.get("best_ball")
    if best_ball is True:
        evidence.append({"code": "best_ball_joint_distribution_required", "requirement":
                         "Expected realized best-ball utility requires joint weekly outcomes; selecting maximum means cannot estimate it."})
    elif best_ball is not False:
        evidence.append({"code": "lineup_mode_unknown", "requirement": "Verify whether this league uses managed lineups or best ball."})
    if not comparisons:
        evidence.append({"code": "weekly_forecasts_required"})
    return {"league": {"alias": alias, "name": data["name"], "season": data["season"],
                       "week": data.get("week"), "own_team_id": str(own)},
            "evaluation_week": week, "evidence_as_of": cutoff,
            "refresh_performed": bool(fresh and as_of is None),
            "snapshot_hash": snap["sha256"], "snapshot_available_at": snap["available_at"],
            "snapshot_age_seconds_at_decision": max(0, cutoff - snap["available_at"]),
            "strategy": service.strategy(alias, as_of=cutoff), "rules": rules,
            "team": service._team_view(data, str(own)), "excluded_player_ids": sorted(excluded),
            "forecast_comparisons": comparisons, "forecast_rejections": rejected,
            "evidence_needed": evidence, "recommendation": None,
            "scope": "Evidence packet for the host's rule-specific reasoning. No lineup changes; no win or best-ball utility estimate."}
