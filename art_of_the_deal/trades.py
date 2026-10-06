"""Deterministic trade accounting and forecast-coherent lineup comparison.

This module deliberately stops short of strategy.  It validates a proposed
exchange, traces complete roster changes, and compares legal lineups only when
the supplied evidence has a common source, period, conditioning, and scoring
basis.  It never fills missing forecasts with zero or combines providers.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any, Iterable

from .store import digest


_PERIOD_KINDS = {"week", "season", "rest_of_season"}
_UNAVAILABLE_STATUSES = {"OUT", "IR", "INACTIVE"}
_ESPN_POSITION_IDS = {
    "1": "QB", "2": "RB", "3": "WR", "4": "TE", "5": "K",
    "8": "DT", "9": "DE", "10": "LB", "11": "DL", "12": "CB",
    "13": "S", "14": "DB", "15": "IDP", "16": "DST", "18": "P",
    "19": "HC",
}


def _error(code: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, **detail}


def _as_id(value: Any) -> str:
    return str(value)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unique_ids(values: Any) -> tuple[list[str], list[str]]:
    if not isinstance(values, list):
        return [], []
    ids = [_as_id(value) for value in values]
    seen: set[str] = set()
    duplicates: list[str] = []
    for ident in ids:
        if ident in seen and ident not in duplicates:
            duplicates.append(ident)
        seen.add(ident)
    return ids, duplicates


def _period_key(period: dict[str, Any]) -> str:
    return json.dumps(period, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_period(raw: Any, league_season: Any, league_week: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict) or raw.get("kind") not in _PERIOD_KINDS:
        return None, "invalid_period"
    kind = raw["kind"]
    period: dict[str, Any] = {"kind": kind}
    if kind == "week":
        week = raw.get("week")
        if not isinstance(week, int) or isinstance(week, bool):
            return None, "invalid_week"
        if league_week is not None and week != league_week:
            return None, "wrong_week"
        period["week"] = week
    elif kind == "season":
        # A full-season forecast is not silently converted to an in-season ROS
        # or weekly forecast.
        if isinstance(league_week, int) and league_week > 1:
            return None, "full_season_not_current_horizon"
    return period, None


def _status_record(player: dict[str, Any]) -> tuple[str | None, float | None, int | None, Any]:
    raw = player.get("injury_status")
    if isinstance(raw, str):
        return raw.upper(), None, None, None
    if not isinstance(raw, dict):
        return None, None, None, None
    status = raw.get("status", raw.get("label"))
    status = status.upper() if isinstance(status, str) else None
    available_at = _finite_number(raw.get("available_at"))
    week = raw.get("week", raw.get("as_of_week"))
    week = week if isinstance(week, int) and not isinstance(week, bool) else None
    return status, available_at, week, raw.get("season")


def _player_positions(player: dict[str, Any]) -> tuple[str, ...]:
    raw = player.get("positions", [])
    if not isinstance(raw, list):
        return ()
    positions = {str(position).upper() for position in raw if str(position).strip()}
    if player.get("role_status") == "not_current_starter":
        positions.discard("QB")
    return tuple(sorted(positions))


def _lineup(
    player_ids: Iterable[str],
    slots: list[dict[str, Any]],
    players: dict[str, dict[str, Any]],
    points: dict[str, float],
    unavailable: set[str],
) -> dict[str, Any]:
    """Maximize supplied forecast points with O(players * 2**slots) DP."""
    slot_eligibility = [
        {str(position).upper() for position in slot.get("eligible_positions", [])}
        for slot in slots
    ]
    slot_labels = [str(slot.get("label", f"slot_{index + 1}")) for index, slot in enumerate(slots)]
    # mask -> (score, assignment tuple); an empty string marks an unfilled slot.
    states: dict[int, tuple[float, tuple[str, ...]]] = {0: (0.0, tuple("" for _ in slots))}
    eligible_candidates: list[str] = []
    excluded: dict[str, str] = {}
    for player_id in sorted(set(player_ids)):
        if player_id in unavailable:
            excluded[player_id] = "unavailable"
            continue
        if player_id not in points:
            excluded[player_id] = "missing_forecast"
            continue
        positions = set(_player_positions(players[player_id]))
        if not positions:
            excluded[player_id] = "no_eligible_service"
            continue
        eligible_candidates.append(player_id)
        updated = dict(states)
        for mask, (score, assignment) in states.items():
            for slot_index, allowed in enumerate(slot_eligibility):
                bit = 1 << slot_index
                if mask & bit or not (positions & allowed):
                    continue
                candidate_assignment = list(assignment)
                candidate_assignment[slot_index] = player_id
                candidate_assignment_tuple = tuple(candidate_assignment)
                candidate = (score + points[player_id], candidate_assignment_tuple)
                existing = updated.get(mask | bit)
                if existing is None or candidate[0] > existing[0] or (
                    candidate[0] == existing[0] and candidate[1] < existing[1]
                ):
                    updated[mask | bit] = candidate
        states = updated

    full_mask = (1 << len(slots)) - 1
    if full_mask in states:
        chosen_mask = full_mask
    else:
        chosen_mask = max(
            states,
            key=lambda mask: (mask.bit_count(), states[mask][0], tuple(-ord(c) for c in "|".join(states[mask][1]))),
        )
    total, assignment = states[chosen_mask]
    selected = [
        {
            "slot_index": index,
            "slot_label": slot_labels[index],
            "eligible_positions": sorted(slot_eligibility[index]),
            "player_id": player_id or None,
            "points": points[player_id] if player_id else None,
        }
        for index, player_id in enumerate(assignment)
    ]
    return {
        "selected": selected,
        "selected_player_ids": [item["player_id"] for item in selected if item["player_id"] is not None],
        "total_points": total,
        "filled_slots": chosen_mask.bit_count(),
        "required_slots": len(slots),
        "complete": chosen_mask == full_mask,
        "eligible_candidate_ids": eligible_candidates,
        "excluded": excluded,
    }


def _ordinary_ids(team: dict[str, Any]) -> list[str]:
    all_ids = set(team["player_ids"])
    return sorted(all_ids - set(team["reserve"]) - set(team["taxi"]))


def _team_snapshot(team: dict[str, Any], slots: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    ordinary = _ordinary_ids(team)
    active_limit = None
    if isinstance(rules.get("bench_slots"), int) and not isinstance(rules.get("bench_slots"), bool):
        active_limit = len(slots) + rules["bench_slots"]
    reserve_limit = rules.get("reserve_slots") if isinstance(rules.get("reserve_slots"), int) else None
    taxi_limit = rules.get("taxi_slots") if isinstance(rules.get("taxi_slots"), int) else None
    return {
        "id": team["id"],
        "name": team["name"],
        "player_ids": list(team["player_ids"]),
        "declared_starters": list(team["starters"]),
        "reserve": list(team["reserve"]),
        "taxi": list(team["taxi"]),
        "ordinary_player_ids": ordinary,
        "counts": {
            "ordinary": len(ordinary),
            "reserve": len(team["reserve"]),
            "taxi": len(team["taxi"]),
        },
        "limits": {"ordinary": active_limit, "reserve": reserve_limit, "taxi": taxi_limit},
    }


def _capacity(snapshot: dict[str, Any]) -> dict[str, Any]:
    counts = snapshot["counts"]
    limits = snapshot["limits"]
    ordinary_required = None if limits["ordinary"] is None else max(0, counts["ordinary"] - limits["ordinary"])
    reserve_required = None if limits["reserve"] is None else max(0, counts["reserve"] - limits["reserve"])
    taxi_required = None if limits["taxi"] is None else max(0, counts["taxi"] - limits["taxi"])
    known = all(value is not None for value in (ordinary_required, reserve_required, taxi_required))
    within = known and not any((ordinary_required, reserve_required, taxi_required))
    return {
        "known": known,
        "within_capacity": within if known else None,
        "required_drop": ordinary_required,
        "required_reserve_drop": reserve_required,
        "required_taxi_drop": taxi_required,
    }


def _position_limit_rules(raw: Any, platform: Any) -> tuple[dict[str, int], list[dict[str, Any]], bool]:
    """Translate enforceable roster-position caps without guessing provider IDs."""
    if raw is None:
        return {}, [], False
    if not isinstance(raw, dict):
        return {}, [{"key": None, "value": raw, "reason": "not_an_object"}], True
    limits: dict[str, int] = {}
    unresolved: list[dict[str, Any]] = []
    for raw_position, raw_limit in sorted(raw.items(), key=lambda row: str(row[0])):
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < -1:
            unresolved.append({"key": str(raw_position), "value": raw_limit, "reason": "invalid_limit"})
            continue
        if raw_limit == -1:
            continue
        key = str(raw_position).upper()
        position = _ESPN_POSITION_IDS.get(key) if platform == "espn" and key.isdigit() else key
        if not position:
            unresolved.append({"key": str(raw_position), "value": raw_limit, "reason": "untranslated_position"})
            continue
        if platform == "espn" and key.isdigit() and key not in _ESPN_POSITION_IDS:
            unresolved.append({"key": key, "value": raw_limit, "reason": "untranslated_espn_position"})
            continue
        if position in limits and limits[position] != raw_limit:
            unresolved.append({"key": key, "value": raw_limit, "reason": "conflicting_position_limit"})
            continue
        limits[position] = raw_limit
    return limits, unresolved, True


def _position_capacity(
    team: dict[str, Any],
    players: dict[str, dict[str, Any]],
    limits: dict[str, int],
    unresolved: list[dict[str, Any]],
    supplied: bool,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    multi_position_ids: list[str] = []
    for player_id in team["player_ids"]:
        positions = _player_positions(players[player_id])
        if len(positions) > 1 and any(position in limits for position in positions):
            multi_position_ids.append(player_id)
        for position in positions:
            counts[position] = counts.get(position, 0) + 1
    excess = {
        position: counts.get(position, 0) - limit
        for position, limit in sorted(limits.items())
        if counts.get(position, 0) > limit
    }
    return {
        "rules_supplied": supplied,
        "fully_translated": supplied and not unresolved and not multi_position_ids,
        "within_observed_limits": not excess,
        "counts": dict(sorted(counts.items())),
        "limits": dict(sorted(limits.items())),
        "excess": excess,
        "unresolved_rules": unresolved,
        "multi_position_player_ids": sorted(multi_position_ids),
    }


def _normalized_team(team_id: str, raw: Any, errors: list[dict[str, Any]]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    lists: dict[str, list[str]] = {}
    for field in ("player_ids", "starters", "reserve", "taxi"):
        values, duplicates = _unique_ids(raw.get(field, []))
        lists[field] = values
        if duplicates:
            errors.append(_error("duplicate_team_player", team_id=team_id, field=field, player_ids=duplicates))
    player_set = set(lists["player_ids"])
    for field in ("starters", "reserve", "taxi"):
        missing = sorted(set(lists[field]) - player_set)
        if missing:
            errors.append(_error("team_status_player_not_owned", team_id=team_id, field=field, player_ids=missing))
    overlap = sorted(set(lists["reserve"]) & set(lists["taxi"]))
    if overlap:
        errors.append(_error("conflicting_roster_status", team_id=team_id, player_ids=overlap))
    return {
        "id": _as_id(raw.get("id", team_id)),
        "name": str(raw.get("name", team_id)),
        **lists,
    }


def _normalize_forecasts(
    forecasts: Any,
    *,
    players: dict[str, dict[str, Any]],
    relevant_ids: set[str],
    league_season: Any,
    league_week: Any,
    decision_at: float | None,
    scoring: dict[str, Any],
    scoring_hash: str,
) -> tuple[dict[tuple[str, Any, str, str], dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, Any, str, str], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    if not isinstance(forecasts, list):
        return groups, [_error("forecasts_not_a_list")]

    weights = scoring.get("weights")
    unsupported_rules = scoring.get("unsupported")
    nonlinear_rules = scoring.get("nonlinear")
    scoring_schema_complete = (
        isinstance(weights, dict)
        and isinstance(unsupported_rules, list)
        and isinstance(nonlinear_rules, list)
        and all(_finite_number(value) is not None for value in weights.values())
    )
    weights_valid = scoring_schema_complete and bool(weights)
    nonlinear = isinstance(nonlinear_rules, list) and bool(nonlinear_rules)
    unsupported = isinstance(unsupported_rules, list) and bool(unsupported_rules)

    for index, raw in enumerate(forecasts):
        if not isinstance(raw, dict):
            rejected.append(_error("invalid_forecast_row", index=index))
            continue
        player_id = _as_id(raw.get("player_id"))
        if player_id not in players:
            rejected.append(_error("unknown_forecast_player", index=index, player_id=player_id))
            continue
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            rejected.append(_error("missing_forecast_source", index=index, player_id=player_id))
            continue
        if raw.get("season") != league_season:
            rejected.append(_error("wrong_forecast_season", index=index, player_id=player_id))
            continue
        period, period_error = _normalize_period(raw.get("period"), league_season, league_week)
        if period_error:
            rejected.append(_error(period_error, index=index, player_id=player_id))
            continue
        available_at = _finite_number(raw.get("available_at"))
        if available_at is None:
            rejected.append(_error("missing_forecast_available_at", index=index, player_id=player_id))
            continue
        if decision_at is not None and available_at > decision_at:
            rejected.append(
                _error(
                    "forecast_after_decision_cutoff",
                    index=index,
                    player_id=player_id,
                    available_at=available_at,
                    decision_at=decision_at,
                )
            )
            continue
        conditioning = raw.get("conditioning")
        if not isinstance(conditioning, str) or not conditioning.strip():
            rejected.append(_error("missing_forecast_conditioning", index=index, player_id=player_id))
            continue

        value: float | None = None
        basis: str | None = None
        supplied_points = _finite_number(raw.get("points"))
        if scoring_schema_complete and supplied_points is not None and raw.get("scoring_hash") == scoring_hash:
            value = supplied_points
            basis = "points_matching_scoring_hash"
        else:
            components = raw.get("components")
            if weights_valid and not nonlinear and not unsupported and isinstance(components, dict):
                component_values = {key: _finite_number(components.get(key)) for key in weights if float(weights[key]) != 0}
                if all(number is not None for number in component_values.values()):
                    value = sum(component_values[key] * float(weights[key]) for key in component_values)  # type: ignore[operator]
                    basis = "complete_linear_components"
        if value is None:
            rejected.append(
                _error(
                    "forecast_scoring_incompatible",
                    index=index,
                    player_id=player_id,
                    has_points=supplied_points is not None,
                    supplied_scoring_hash=raw.get("scoring_hash"),
                    expected_scoring_hash=scoring_hash,
                    scoring_schema_complete=scoring_schema_complete,
                    nonlinear=nonlinear,
                    unsupported=unsupported,
                )
            )
            continue

        key = (source, raw.get("season"), _period_key(period), conditioning)
        group = groups.setdefault(
            key,
            {
                "source": source,
                "season": raw.get("season"),
                "period": period,
                "conditioning": conditioning,
                "points": {},
                "basis": {},
                "available_at": {},
                "source_urls": set(),
                "provider_updated_at": set(),
                "conflicts": [],
            },
        )
        if isinstance(raw.get("source_url"), str) and raw["source_url"].strip():
            group["source_urls"].add(raw["source_url"])
        provider_updated_at = raw.get("provider_updated_at")
        if provider_updated_at is not None:
            group["provider_updated_at"].add(str(provider_updated_at))
        previous_time = group["available_at"].get(player_id)
        if previous_time is None or available_at > previous_time:
            group["points"][player_id] = value
            group["basis"][player_id] = basis
            group["available_at"][player_id] = available_at
        elif available_at == previous_time and group["points"].get(player_id) != value:
            group["conflicts"].append(player_id)

    # Rejected rows for irrelevant players are still retained for audit, but the
    # relevant ID set determines whether a comparison can be complete.
    for group in groups.values():
        group["missing_all_relevant_player_ids"] = sorted(relevant_ids - set(group["points"]))
        group["conflicts"] = sorted(set(group["conflicts"]))
        group["source_urls"] = sorted(group["source_urls"])
        group["provider_updated_at"] = sorted(group["provider_updated_at"])
    return groups, rejected


def compare_trade(
    league: dict[str, Any],
    proposal: dict[str, Any],
    forecasts: list[dict[str, Any]] | None = None,
    unavailable: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate and mechanically compare a fantasy trade proposal.

    Invalid ownership or schema facts are returned in ``errors``.  Capacity
    excess is a valid accounting result but makes ``executable`` false and is
    reported as an exact required-drop count.  Forecast comparisons are kept
    separate by source/period/conditioning and are withheld if any startable
    active-roster player lacks compatible evidence.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    evidence_needed: list[dict[str, Any]] = []
    if not isinstance(league, dict):
        return {"valid": False, "executable": False, "errors": [_error("league_not_an_object")]}
    if not isinstance(proposal, dict):
        return {"valid": False, "executable": False, "errors": [_error("proposal_not_an_object")]}

    rules = league.get("rules") if isinstance(league.get("rules"), dict) else {}
    raw_slots = rules.get("starters", [])
    slots: list[dict[str, Any]] = []
    if not isinstance(raw_slots, list):
        errors.append(_error("starters_not_a_list"))
    else:
        for index, raw_slot in enumerate(raw_slots):
            if not isinstance(raw_slot, dict) or not isinstance(raw_slot.get("eligible_positions"), list):
                errors.append(_error("invalid_starter_slot", slot_index=index))
                continue
            eligible = sorted({str(position).upper() for position in raw_slot["eligible_positions"]})
            if not eligible:
                errors.append(_error("empty_starter_eligibility", slot_index=index))
                continue
            slots.append({"label": str(raw_slot.get("label", f"slot_{index + 1}")), "eligible_positions": eligible})

    for field in ("bench_slots", "reserve_slots", "taxi_slots"):
        value = rules.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            errors.append(_error("invalid_roster_slot_count", field=field, value=value))

    scoring = rules.get("scoring") if isinstance(rules.get("scoring"), dict) else {}
    scoring_hash = digest(scoring)
    best_ball = rules.get("best_ball")
    if best_ball is not None and not isinstance(best_ball, bool):
        errors.append(_error("invalid_best_ball_rule", value=best_ball))
    raw_players = league.get("players") if isinstance(league.get("players"), dict) else {}
    players: dict[str, dict[str, Any]] = {}
    for raw_id, raw in raw_players.items():
        player_id = _as_id(raw_id)
        if not isinstance(raw, dict):
            errors.append(_error("invalid_player", player_id=player_id))
            continue
        positions = raw.get("positions")
        if not isinstance(positions, list) or not positions:
            errors.append(_error("invalid_player_positions", player_id=player_id))
        players[player_id] = deepcopy(raw)

    if unavailable is None:
        explicit_unavailable: set[str] = set()
    elif isinstance(unavailable, (str, bytes)):
        errors.append(_error("unavailable_not_an_id_list"))
        explicit_unavailable = set()
    else:
        try:
            explicit_unavailable = {_as_id(player_id) for player_id in unavailable}
        except TypeError:
            errors.append(_error("unavailable_not_an_id_list"))
            explicit_unavailable = set()
    unknown_unavailable = sorted(explicit_unavailable - set(players))
    if unknown_unavailable:
        errors.append(_error("unknown_unavailable_player", player_ids=unknown_unavailable))

    raw_teams = league.get("teams") if isinstance(league.get("teams"), dict) else {}
    teams: dict[str, dict[str, Any]] = {}
    for raw_id, raw_team in raw_teams.items():
        team_id = _as_id(raw_id)
        if not isinstance(raw_team, dict):
            errors.append(_error("invalid_team", team_id=team_id))
        teams[team_id] = _normalized_team(team_id, raw_team, errors)

    owner: dict[str, str] = {}
    for team_id, team in teams.items():
        for player_id in team["player_ids"]:
            if player_id not in players:
                errors.append(_error("owned_player_missing_from_directory", team_id=team_id, player_id=player_id))
            if player_id in owner and owner[player_id] != team_id:
                errors.append(
                    _error(
                        "player_owned_by_multiple_teams",
                        player_id=player_id,
                        team_ids=sorted({owner[player_id], team_id}),
                    )
                )
            owner[player_id] = team_id

    position_limits, unresolved_position_limits, position_limits_supplied = _position_limit_rules(
        rules.get("position_limits"), league.get("platform")
    )

    transfers = proposal.get("transfers", [])
    pick_transfers = proposal.get("pick_transfers", [])
    drops = proposal.get("drops", {})
    if not isinstance(transfers, list):
        errors.append(_error("transfers_not_a_list"))
        transfers = []
    if not isinstance(pick_transfers, list):
        errors.append(_error("pick_transfers_not_a_list"))
        pick_transfers = []
    if not isinstance(drops, dict):
        errors.append(_error("drops_not_an_object"))
        drops = {}

    normalized_transfers: list[dict[str, str]] = []
    transferred_ids: set[str] = set()
    affected: set[str] = set()
    for index, raw in enumerate(transfers):
        if not isinstance(raw, dict):
            errors.append(_error("invalid_transfer", index=index))
            continue
        player_id = _as_id(raw.get("player_id"))
        from_team = _as_id(raw.get("from_team"))
        to_team = _as_id(raw.get("to_team"))
        if player_id in transferred_ids:
            errors.append(_error("duplicate_asset_transfer", asset_type="player", asset_id=player_id))
        transferred_ids.add(player_id)
        if from_team not in teams or to_team not in teams:
            errors.append(_error("transfer_team_missing", index=index, from_team=from_team, to_team=to_team))
        elif from_team == to_team:
            errors.append(_error("self_transfer", asset_type="player", asset_id=player_id, team_id=from_team))
        elif owner.get(player_id) != from_team:
            errors.append(
                _error(
                    "transferor_does_not_own_player",
                    player_id=player_id,
                    from_team=from_team,
                    actual_owner=owner.get(player_id),
                )
            )
        elif player_id in teams[from_team]["reserve"] or player_id in teams[from_team]["taxi"]:
            errors.append(
                _error(
                    "roster_status_transfer_requires_explicit_placement",
                    player_id=player_id,
                    from_team=from_team,
                )
            )
        normalized_transfers.append({"player_id": player_id, "from_team": from_team, "to_team": to_team})
        affected.update((from_team, to_team))

    normalized_drops: dict[str, list[str]] = {}
    dropped_ids: set[str] = set()
    for raw_team_id, raw_ids in drops.items():
        team_id = _as_id(raw_team_id)
        if not isinstance(raw_ids, list):
            errors.append(_error("drop_player_ids_not_a_list", team_id=team_id))
        ids, duplicates = _unique_ids(raw_ids)
        normalized_drops[team_id] = ids
        affected.add(team_id)
        if team_id not in teams:
            errors.append(_error("drop_team_missing", team_id=team_id))
            continue
        if duplicates:
            errors.append(_error("duplicate_drop", team_id=team_id, player_ids=duplicates))
        for player_id in ids:
            if player_id in dropped_ids:
                errors.append(_error("duplicate_asset_drop", player_id=player_id))
            dropped_ids.add(player_id)
            if player_id in transferred_ids:
                errors.append(_error("asset_both_transferred_and_dropped", player_id=player_id))
            if owner.get(player_id) != team_id:
                errors.append(
                    _error(
                        "drop_team_does_not_own_player",
                        player_id=player_id,
                        team_id=team_id,
                        actual_owner=owner.get(player_id),
                    )
                )

    raw_picks = league.get("picks", [])
    picks_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_picks, list):
        errors.append(_error("picks_not_a_list"))
        raw_picks = []
    for raw in raw_picks:
        if not isinstance(raw, dict) or "id" not in raw:
            errors.append(_error("invalid_pick"))
            continue
        pick_id = _as_id(raw["id"])
        if pick_id in picks_by_id:
            errors.append(_error("duplicate_pick_id", pick_id=pick_id))
        picks_by_id[pick_id] = deepcopy(raw)

    normalized_pick_transfers: list[dict[str, str]] = []
    transferred_pick_ids: set[str] = set()
    league_format = league.get("format", "unknown")
    for index, raw in enumerate(pick_transfers):
        if not isinstance(raw, dict):
            errors.append(_error("invalid_pick_transfer", index=index))
            continue
        pick_id = _as_id(raw.get("pick_id"))
        from_team = _as_id(raw.get("from_team"))
        to_team = _as_id(raw.get("to_team"))
        if pick_id in transferred_pick_ids:
            errors.append(_error("duplicate_asset_transfer", asset_type="pick", asset_id=pick_id))
        transferred_pick_ids.add(pick_id)
        pick = picks_by_id.get(pick_id)
        if from_team not in teams or to_team not in teams:
            errors.append(_error("pick_transfer_team_missing", index=index, from_team=from_team, to_team=to_team))
        elif from_team == to_team:
            errors.append(_error("self_transfer", asset_type="pick", asset_id=pick_id, team_id=from_team))
        elif pick is None:
            errors.append(_error("missing_pick", pick_id=pick_id))
        elif _as_id(pick.get("owner_team_id")) != from_team:
            errors.append(
                _error(
                    "transferor_does_not_own_pick",
                    pick_id=pick_id,
                    from_team=from_team,
                    actual_owner=_as_id(pick.get("owner_team_id")),
                )
            )
        elif (
            league_format == "redraft"
            and isinstance(pick.get("season"), int)
            and isinstance(league.get("season"), int)
            and pick["season"] > league["season"]
            and not bool(rules.get("allow_future_pick_trades"))
        ):
            errors.append(_error("future_pick_not_allowed_in_redraft", pick_id=pick_id, season=pick["season"]))
        normalized_pick_transfers.append({"pick_id": pick_id, "from_team": from_team, "to_team": to_team})
        affected.update((from_team, to_team))

    evaluation_week = proposal.get("evaluation_week", league.get("week"))
    if evaluation_week is not None and (isinstance(evaluation_week, bool) or not isinstance(evaluation_week, int) or not 1 <= evaluation_week <= 18):
        errors.append(_error("invalid_evaluation_week"))
    decision_at = None
    if "decision_at" in proposal:
        decision_at = _finite_number(proposal.get("decision_at"))
        if decision_at is None:
            errors.append(_error("invalid_decision_at"))
    elif forecasts:
        warnings.append(_error("decision_at_missing_future_filter_not_applied"))

    result: dict[str, Any] = {
        "valid": not errors,
        "executable": False,
        "errors": errors,
        "warnings": warnings,
        "evidence_needed": evidence_needed,
        "league": {
            "platform": league.get("platform"),
            "format": league_format,
            "season": league.get("season"),
            "week": league.get("week"),
            "evaluation_week": evaluation_week,
            "scoring_hash": scoring_hash,
            "best_ball": best_ball,
        },
        "proposal": {
            "decision_at": decision_at,
            "evaluation_week": evaluation_week,
            "transfers": normalized_transfers,
            "pick_transfers": normalized_pick_transfers,
            "drops": normalized_drops,
        },
        "affected_team_ids": sorted(affected),
        "asset_change_count": len(normalized_transfers) + len(normalized_pick_transfers) + sum(map(len, normalized_drops.values())),
        "mechanical_change_zero": not normalized_transfers and not normalized_pick_transfers and not any(normalized_drops.values()),
        "teams": {},
        "mechanics_scope": {
            "ownership": "checked",
            "roster_cardinality": "checked when all slot counts are observed",
            "position_limits": "checked for translated supplied limits",
            "transaction_timing_and_consent": "not checked",
        },
        "forecast_comparisons": [],
        "result": None,
    }
    if errors:
        return result

    after_teams = deepcopy(teams)
    for transfer in normalized_transfers:
        player_id = transfer["player_id"]
        source = after_teams[transfer["from_team"]]
        target = after_teams[transfer["to_team"]]
        source["player_ids"].remove(player_id)
        if player_id in source["starters"]:
            source["starters"].remove(player_id)
        target["player_ids"].append(player_id)
    for team_id, player_ids in normalized_drops.items():
        team = after_teams[team_id]
        for player_id in player_ids:
            team["player_ids"].remove(player_id)
            if player_id in team["starters"]:
                team["starters"].remove(player_id)
            if player_id in team["reserve"]:
                team["reserve"].remove(player_id)
            if player_id in team["taxi"]:
                team["taxi"].remove(player_id)
    for team in after_teams.values():
        for field in ("player_ids", "starters", "reserve", "taxi"):
            team[field] = sorted(team[field])

    capacity_known = True
    capacity_clear = True
    position_limits_clear = True
    for team_id in sorted(affected):
        before = _team_snapshot(teams[team_id], slots, rules)
        after = _team_snapshot(after_teams[team_id], slots, rules)
        before_capacity = _capacity(before)
        capacity = _capacity(after)
        capacity_known = capacity_known and capacity["known"]
        capacity_clear = capacity_clear and capacity.get("within_capacity") is True
        position_capacity = _position_capacity(
            after_teams[team_id], players, position_limits,
            unresolved_position_limits, position_limits_supplied,
        )
        before_position_capacity = _position_capacity(
            teams[team_id], players, position_limits,
            unresolved_position_limits, position_limits_supplied,
        )
        position_limits_clear = position_limits_clear and position_capacity["within_observed_limits"]
        if not capacity["known"]:
            evidence_needed.append(_error("roster_capacity_rules_missing", team_id=team_id))
        if capacity.get("required_drop"):
            evidence_needed.append(
                _error("additional_drop_required", team_id=team_id, count=capacity["required_drop"])
            )
        if capacity.get("required_reserve_drop"):
            evidence_needed.append(
                _error("reserve_drop_required", team_id=team_id, count=capacity["required_reserve_drop"])
            )
        if capacity.get("required_taxi_drop"):
            evidence_needed.append(_error("taxi_drop_required", team_id=team_id, count=capacity["required_taxi_drop"]))
        if position_capacity["excess"]:
            evidence_needed.append(
                _error("position_limit_exceeded", team_id=team_id, excess=position_capacity["excess"])
            )
        if position_capacity["unresolved_rules"]:
            evidence_needed.append(
                _error(
                    "position_limit_rules_unresolved",
                    team_id=team_id,
                    rules=position_capacity["unresolved_rules"],
                )
            )
        if position_capacity["multi_position_player_ids"]:
            evidence_needed.append(
                _error(
                    "multi_position_limit_semantics_unresolved",
                    team_id=team_id,
                    player_ids=position_capacity["multi_position_player_ids"],
                )
            )
        incoming = sorted(t["player_id"] for t in normalized_transfers if t["to_team"] == team_id)
        outgoing = sorted(t["player_id"] for t in normalized_transfers if t["from_team"] == team_id)
        dropped = sorted(normalized_drops.get(team_id, []))
        result["teams"][team_id] = {
            "before": before,
            "after": after,
            "before_capacity": before_capacity,
            "capacity": capacity,
            "before_position_capacity": before_position_capacity,
            "position_capacity": position_capacity,
            "incoming_player_ids": incoming,
            "outgoing_player_ids": outgoing,
            "dropped_player_ids": dropped,
            "removed_declared_starter_ids": sorted(set(before["declared_starters"]) - set(after["declared_starters"])),
            "incoming_pick_ids": sorted(t["pick_id"] for t in normalized_pick_transfers if t["to_team"] == team_id),
            "outgoing_pick_ids": sorted(t["pick_id"] for t in normalized_pick_transfers if t["from_team"] == team_id),
        }

    # Backwards-compatible field with an explicit scope: this is mechanical
    # roster feasibility, never proof that a platform transaction can execute.
    result["executable"] = capacity_known and capacity_clear and position_limits_clear
    result["roster_feasibility"] = {
        "feasible_under_checked_rules": result["executable"],
        "cardinality_known": capacity_known,
        "cardinality_within_limits": capacity_clear if capacity_known else None,
        "observed_position_limits_within": position_limits_clear,
        "position_limits_fully_translated": position_limits_supplied and not unresolved_position_limits,
        "platform_transaction_executable": None,
    }
    if not result["mechanical_change_zero"]:
        evidence_needed.extend([
            _error(
                "hold_counterfactual_required",
                requirement="Name the legal lineup, bench coverage, and retained options if the trade is not made.",
            ),
            _error(
                "named_available_alternative_required",
                requirement="Verify each waiver or trade alternative is currently available and include its acquisition cost; do not use an abstract replacement player.",
            ),
            _error(
                "waiver_and_standings_state_required",
                requirement="Verify priority/budget, reset rule, pending claims, standings path, and the cost of preserving or spending the option.",
            ),
        ])
    if normalized_pick_transfers:
        evidence_needed.append(_error("pick_value_not_modeled", pick_ids=sorted(transferred_pick_ids)))

    if forecasts is None:
        if normalized_transfers or normalized_drops:
            evidence_needed.append(_error("forecasts_required_for_lineup_comparison"))
        return result

    relevant_ids: set[str] = set()
    eligible_ids: set[str] = set()
    status_unknown: set[str] = set()
    current_week_status_unavailable: set[str] = set()
    for team_id in affected:
        for team in (teams[team_id], after_teams[team_id]):
            relevant_ids.update(_ordinary_ids(team))
    for player_id in sorted(relevant_ids):
        player = players[player_id]
        status, status_at, status_week, status_season = _status_record(player)
        if status in _UNAVAILABLE_STATUSES:
            if (
                decision_at is not None
                and status_at is not None
                and status_at <= decision_at
                and status_week == evaluation_week
                and (status_season is None or status_season == league.get("season"))
            ):
                current_week_status_unavailable.add(player_id)
            else:
                status_unknown.add(player_id)
        positions = _player_positions(player)
        if any(
            set(positions) & set(slot["eligible_positions"]) for slot in slots
        ):
            eligible_ids.add(player_id)
    if status_unknown:
        evidence_needed.append(
            _error(
                "unavailable_status_not_dated_for_current_week",
                player_ids=sorted(status_unknown),
            )
        )

    groups, rejected_rows = _normalize_forecasts(
        forecasts,
        players=players,
        relevant_ids=relevant_ids,
        league_season=league.get("season"),
        league_week=evaluation_week,
        decision_at=decision_at,
        scoring=scoring,
        scoring_hash=scoring_hash,
    )
    result["forecast_rejections"] = rejected_rows

    complete_comparisons: list[dict[str, Any]] = []
    for key in sorted(groups, key=lambda item: (item[0], str(item[1]), item[2], item[3])):
        group = groups[key]
        unavailable_for_group: set[str] = set()
        if group["period"]["kind"] == "week":
            unavailable_for_group.update(explicit_unavailable)
            unavailable_for_group.update(current_week_status_unavailable)
        required_ids = eligible_ids - unavailable_for_group
        missing_required = sorted(required_ids - set(group["points"]))
        comparison: dict[str, Any] = {
            "source": group["source"],
            "season": group["season"],
            "period": group["period"],
            "conditioning": group["conditioning"],
            "interpretation": "Pre-outcome weekly lineup capacity under this provider" if group["period"]["kind"] == "week" else "Static horizon capacity proxy only: does not model weekly lineup changes, byes or future acquisitions; never a season win estimate",
            "forecast_available_at_by_player": dict(sorted(group["available_at"].items())),
            "forecast_available_at_oldest": min(group["available_at"].values()),
            "forecast_available_at_newest": max(group["available_at"].values()),
            "forecast_age_seconds_at_decision": {
                "oldest": max(0.0, decision_at - min(group["available_at"].values())),
                "newest": max(0.0, decision_at - max(group["available_at"].values())),
            } if decision_at is not None else None,
            "source_urls": group["source_urls"],
            "provider_updated_at": group["provider_updated_at"],
            "scoring_basis_by_player": dict(sorted(group["basis"].items())),
            "missing_all_relevant_player_ids": group["missing_all_relevant_player_ids"],
            "missing_required_player_ids": missing_required,
            "conflicting_player_ids": group["conflicts"],
            "status": "incomplete" if missing_required or group["conflicts"] else "complete",
            "teams": {},
        }
        if not result["executable"]:
            comparison["status"] = "incomplete"
            comparison["capacity_limitation"] = "Post-trade roster capacity is exceeded or cannot be verified."
        if best_ball is None:
            comparison["status"] = "incomplete"
            comparison["lineup_timing_limitation"] = (
                "The league's managed-lineup versus best-ball rule is unresolved."
            )
            evidence_needed.append(
                _error(
                    "lineup_timing_rule_required",
                    source=group["source"],
                    period=group["period"],
                    conditioning=group["conditioning"],
                )
            )
        elif best_ball:
            comparison["status"] = "incomplete"
            comparison["best_ball_limitation"] = (
                "Point forecasts do not identify the expected realized maximizing lineup; "
                "a joint player-outcome distribution is required."
            )
            evidence_needed.append(
                _error(
                    "best_ball_joint_outcome_distribution_required",
                    source=group["source"],
                    period=group["period"],
                    conditioning=group["conditioning"],
                )
            )
        if comparison["status"] == "complete":
            for team_id in sorted(affected):
                before_lineup = _lineup(
                    _ordinary_ids(teams[team_id]), slots, players, group["points"], unavailable_for_group
                )
                after_lineup = _lineup(
                    _ordinary_ids(after_teams[team_id]), slots, players, group["points"], unavailable_for_group
                )
                team_complete = before_lineup["complete"] and after_lineup["complete"]
                comparison["teams"][team_id] = {
                    "before": before_lineup,
                    "after": after_lineup,
                    "delta_points": after_lineup["total_points"] - before_lineup["total_points"]
                    if team_complete
                    else None,
                }
                if not team_complete:
                    comparison["status"] = "incomplete"
                    comparison.setdefault("lineup_capacity_failures", []).append(team_id)
            if comparison["status"] == "complete":
                complete_comparisons.append(comparison)
        result["forecast_comparisons"].append(comparison)

    if not groups:
        evidence_needed.append(_error("no_common_usable_forecast_cohort"))
    for comparison in result["forecast_comparisons"]:
        if comparison["missing_required_player_ids"]:
            evidence_needed.append(
                _error(
                    "missing_forecasts_withhold_delta",
                    source=comparison["source"],
                    period=comparison["period"],
                    conditioning=comparison["conditioning"],
                    player_ids=comparison["missing_required_player_ids"],
                )
            )

    # There is intentionally no cross-provider aggregate.  `result` is only a
    # convenience alias when exactly one coherent comparison exists.
    if len(complete_comparisons) == 1 and not normalized_pick_transfers:
        result["result"] = complete_comparisons[0]
    elif len(complete_comparisons) > 1:
        evidence_needed.append(
            _error(
                "multiple_complete_forecast_cohorts_no_automatic_selection",
                cohorts=[
                    {
                        "source": item["source"],
                        "period": item["period"],
                        "conditioning": item["conditioning"],
                    }
                    for item in complete_comparisons
                ],
            )
        )
    return result
