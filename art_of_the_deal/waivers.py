"""Deterministic waiver and free-agent pickup comparisons.

This module evaluates a hypothetical successful acquisition as a roster branch.
It never claims that a waiver was won, a free agent remained available, or an
unknown bid/priority state guarantees execution.
"""

from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any, Iterable

from .store import digest, timestamp
from .trades import (
    _UNAVAILABLE_STATUSES,
    _capacity,
    _lineup,
    _normalize_forecasts,
    _normalized_team,
    _ordinary_ids,
    _player_positions,
    _position_capacity,
    _position_limit_rules,
    _status_record,
    _team_snapshot,
)


_POOL_STATUSES = {"FREEAGENT", "WAIVERS"}


def _error(code: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, **detail}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _decision_time(value: Any, errors: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> float:
    if value is None:
        warnings.append(_error("decision_at_defaulted_to_local_clock"))
        return time.time()
    try:
        return timestamp(value)
    except (TypeError, ValueError):
        errors.append(_error("invalid_decision_at"))
        return time.time()


def _slots(rules: dict[str, Any], errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_slots = rules.get("starters")
    if not isinstance(raw_slots, list) or not raw_slots:
        errors.append(_error("starters_not_a_nonempty_list"))
        return []
    result = []
    for index, raw in enumerate(raw_slots):
        if not isinstance(raw, dict) or not isinstance(raw.get("eligible_positions"), list):
            errors.append(_error("invalid_starter_slot", slot_index=index))
            continue
        eligible = sorted({str(position).upper() for position in raw["eligible_positions"] if str(position)})
        if not eligible:
            errors.append(_error("empty_starter_eligibility", slot_index=index))
            continue
        result.append({"label": str(raw.get("label", "slot_" + str(index + 1))),
                       "eligible_positions": eligible})
    return result


def _coverage(
    team: dict[str, Any],
    slots: list[dict[str, Any]],
    players: dict[str, dict[str, Any]],
    selected: Iterable[str] = (),
    unavailable: Iterable[str] = (),
) -> dict[str, Any]:
    selected_set = set(selected)
    unavailable_set = set(unavailable)
    ordinary = _ordinary_ids(team)
    rows = []
    for player_id in ordinary:
        positions = set(_player_positions(players[player_id]))
        eligible_slots = [
            {"slot_index": index, "slot_label": slot["label"]}
            for index, slot in enumerate(slots)
            if positions & set(slot["eligible_positions"])
        ]
        rows.append({
            "player_id": player_id,
            "positions": sorted(positions),
            "eligible_slots": eligible_slots,
            "selected_starter": player_id in selected_set,
            "bench_coverage": (
                player_id not in selected_set
                and player_id not in unavailable_set
                and bool(eligible_slots)
            ),
            "explicitly_unavailable_for_evaluation_week": player_id in unavailable_set,
        })
    return {
        "ordinary_player_ids": ordinary,
        "players": rows,
        "bench_coverage_player_ids": [row["player_id"] for row in rows if row["bench_coverage"]],
        "interpretation": (
            "Eligibility inventory only. Coverage has value only in a named state where the player "
            "is available and changes a legal pre-lock lineup."
        ),
    }


def _base_result(
    league: Any,
    team_id: Any,
    candidate: Any,
    drop_player_id: Any,
    evaluation_week: Any,
    decision_at: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    result = {
        "valid": False,
        "roster_branch_feasible": False,
        "platform_transaction_completed": False,
        "errors": errors,
        "warnings": warnings,
        "evidence_needed": evidence,
        "team_id": str(team_id),
        "candidate": deepcopy(candidate) if isinstance(candidate, dict) else candidate,
        "drop_player_id": str(drop_player_id) if drop_player_id is not None else None,
        "evaluation_week": evaluation_week,
        "decision_at": decision_at,
        "before": None,
        "after": None,
        "forecast_comparisons": [],
        "result": None,
        "fantasy_title_probability_delta": None,
        "interpretation": (
            "A conditional roster branch, not proof of acquisition, a normative provider baseline, "
            "a waiver-win probability, or a title-probability estimate."
        ),
    }
    if not isinstance(league, dict):
        errors.append(_error("league_not_an_object"))
    if not isinstance(candidate, dict):
        errors.append(_error("candidate_not_an_object"))
    return result, errors, warnings, evidence


def evaluate_pickup(
    league: dict[str, Any],
    team_id: str,
    candidate: dict[str, Any],
    drop_player_id: str | None = None,
    forecasts: list[dict[str, Any]] | None = None,
    evaluation_week: int | None = None,
    decision_at: float | str | None = None,
    unavailable: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Compare hold with the roster after one hypothetical successful pickup."""
    result, errors, warnings, evidence = _base_result(
        league, team_id, candidate, drop_player_id, evaluation_week, decision_at
    )
    if errors:
        return result

    rules = league.get("rules") if isinstance(league.get("rules"), dict) else {}
    slots = _slots(rules, errors)
    for field in ("bench_slots", "reserve_slots", "taxi_slots"):
        value = rules.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            errors.append(_error("invalid_roster_slot_count", field=field, value=value))

    week = league.get("week") if evaluation_week is None else evaluation_week
    result["evaluation_week"] = week
    if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 18:
        errors.append(_error("invalid_evaluation_week"))
    cutoff = _decision_time(decision_at, errors, warnings)
    result["decision_at"] = cutoff

    candidate_id = str(candidate.get("player_id"))
    if candidate.get("player_id") is None or not candidate_id:
        errors.append(_error("candidate_player_id_required"))
    name = candidate.get("name")
    positions = candidate.get("positions")
    if not isinstance(name, str) or not name.strip():
        errors.append(_error("candidate_name_required"))
    if not isinstance(positions, list) or not positions or any(not str(position) for position in positions):
        errors.append(_error("candidate_positions_required"))
        normalized_positions: list[str] = []
    else:
        normalized_positions = sorted({str(position).upper() for position in positions})
    acquisition_status = candidate.get("acquisition_status")
    if acquisition_status not in _POOL_STATUSES:
        errors.append(_error("candidate_not_in_supported_pool_state", status=acquisition_status))
    observed_at = _finite(candidate.get("observed_at"))
    if observed_at is None:
        errors.append(_error("candidate_pool_observation_time_required"))
    elif observed_at > cutoff:
        errors.append(_error("candidate_pool_observed_after_decision", observed_at=observed_at))
    expires_at = candidate.get("expires_at")
    if expires_at is None:
        evidence.append(_error(
            "pool_expiry_unknown",
            requirement="Confirm the player still has this acquisition status immediately before action.",
        ))
    else:
        expires = _finite(expires_at)
        if expires is None:
            errors.append(_error("invalid_candidate_pool_expiry"))
        elif expires < cutoff:
            errors.append(_error("candidate_pool_observation_expired", expires_at=expires))
    if candidate.get("on_team_id") not in (None, 0, "0"):
        errors.append(_error("candidate_pool_row_reports_owner", on_team_id=str(candidate.get("on_team_id"))))
    if candidate.get("roster_locked") is True:
        evidence.append(_error(
            "candidate_currently_roster_locked",
            requirement=(
                "Compare the stated post-clearance branch, but verify when the lock clears and whether "
                "the claim/direct add can execute before the evaluated lineup period."
            ),
        ))
    if candidate.get("execution_actionable") is not True:
        evidence.append(_error(
            "pickup_actionability_unverified",
            requirement="Confirm the current UI permits this exact claim or add; a provider pool row is not execution proof.",
        ))

    pool_horizon = candidate.get("horizon")
    if pool_horizon is not None:
        if not isinstance(pool_horizon, dict) or pool_horizon.get("kind") != "week" or (
            isinstance(pool_horizon.get("week"), bool) or not isinstance(pool_horizon.get("week"), int)
        ):
            errors.append(_error("invalid_candidate_pool_horizon"))
        elif isinstance(league.get("week"), int) and pool_horizon["week"] != league["week"]:
            errors.append(_error(
                "candidate_pool_period_not_current",
                pool_week=pool_horizon["week"], league_week=league.get("week"),
            ))
    else:
        evidence.append(_error(
            "candidate_pool_period_required",
            requirement="Preserve the free-agent batch week; a future-period FREEAGENT label does not prove current add actionability.",
        ))

    raw_players = league.get("players") if isinstance(league.get("players"), dict) else {}
    players: dict[str, dict[str, Any]] = {}
    for raw_id, raw in raw_players.items():
        player_id = str(raw_id)
        if not isinstance(raw, dict):
            errors.append(_error("invalid_player", player_id=player_id))
            continue
        players[player_id] = deepcopy(raw)
    if candidate_id in players:
        current = players[candidate_id]
        if current.get("name") != name or sorted(str(p).upper() for p in current.get("positions", [])) != normalized_positions:
            errors.append(_error("candidate_identity_conflicts_with_player_directory", player_id=candidate_id))
        candidate_player = deepcopy(current)
        for key in ("injury_status", "role_status", "external_ids"):
            if key in candidate and key not in candidate_player:
                candidate_player[key] = deepcopy(candidate[key])
        players[candidate_id] = candidate_player
    else:
        players[candidate_id] = {
            "name": name,
            "positions": normalized_positions,
            **{key: deepcopy(candidate[key]) for key in ("injury_status", "role_status", "external_ids") if key in candidate},
        }

    raw_teams = league.get("teams") if isinstance(league.get("teams"), dict) else {}
    teams: dict[str, dict[str, Any]] = {}
    for raw_id, raw_team in raw_teams.items():
        ident = str(raw_id)
        if not isinstance(raw_team, dict):
            errors.append(_error("invalid_team", team_id=ident))
        teams[ident] = _normalized_team(ident, raw_team, errors)
    target_id = str(team_id)
    if target_id not in teams:
        errors.append(_error("team_missing", team_id=target_id))

    owners: dict[str, str] = {}
    for ident, team in teams.items():
        for player_id in team["player_ids"]:
            if player_id in owners and owners[player_id] != ident:
                errors.append(_error("player_owned_by_multiple_teams", player_id=player_id,
                                     team_ids=sorted({owners[player_id], ident})))
            owners[player_id] = ident
            if player_id not in players:
                errors.append(_error("owned_player_missing_from_directory", team_id=ident, player_id=player_id))
    if candidate_id in owners:
        errors.append(_error("candidate_already_owned", player_id=candidate_id, owner_team_id=owners[candidate_id]))

    drop_id = str(drop_player_id) if drop_player_id is not None else None
    if drop_id is not None and target_id in teams:
        if owners.get(drop_id) != target_id:
            errors.append(_error("drop_player_not_owned_by_team", player_id=drop_id,
                                 actual_owner=owners.get(drop_id), team_id=target_id))
        elif players.get(drop_id, {}).get("roster_locked") is True:
            evidence.append(_error(
                "drop_player_currently_roster_locked",
                player_id=drop_id,
                requirement=(
                    "The post-clearance add/drop branch may be compared, but current lock state does not "
                    "prove that this conditional drop will be accepted when waivers process."
                ),
            ))
        if drop_id == candidate_id:
            errors.append(_error("candidate_cannot_be_drop_player", player_id=candidate_id))

    if unavailable is None:
        explicit_unavailable: set[str] = set()
    elif isinstance(unavailable, (str, bytes)):
        errors.append(_error("unavailable_not_an_id_list"))
        explicit_unavailable = set()
    else:
        try:
            explicit_unavailable = {str(player_id) for player_id in unavailable}
        except TypeError:
            errors.append(_error("unavailable_not_an_id_list"))
            explicit_unavailable = set()
    unknown_unavailable = sorted(explicit_unavailable - set(players))
    if unknown_unavailable:
        errors.append(_error("unknown_unavailable_player", player_ids=unknown_unavailable))

    if errors:
        return result

    before_team = deepcopy(teams[target_id])
    after_team = deepcopy(before_team)
    if drop_id is not None:
        after_team["player_ids"].remove(drop_id)
        for field in ("starters", "reserve", "taxi"):
            if drop_id in after_team[field]:
                after_team[field].remove(drop_id)
    after_team["player_ids"].append(candidate_id)
    for field in ("player_ids", "starters", "reserve", "taxi"):
        after_team[field] = sorted(after_team[field])

    before_snapshot = _team_snapshot(before_team, slots, rules)
    after_snapshot = _team_snapshot(after_team, slots, rules)
    before_capacity = _capacity(before_snapshot)
    after_capacity = _capacity(after_snapshot)
    limits, unresolved_limits, limits_supplied = _position_limit_rules(
        rules.get("position_limits"), league.get("platform")
    )
    before_position = _position_capacity(
        before_team, players, limits, unresolved_limits, limits_supplied
    )
    after_position = _position_capacity(
        after_team, players, limits, unresolved_limits, limits_supplied
    )
    # Match the trade comparator: unresolved provider fields do not erase the
    # conclusions available from translated limits and exact cardinality.  They
    # do keep full platform legality unknown.
    roster_feasible = (
        after_capacity["known"]
        and after_capacity["within_capacity"] is True
        and after_position["within_observed_limits"]
    )
    result["roster_branch_feasible"] = roster_feasible
    result["roster_feasibility"] = {
        "feasible_under_checked_rules": roster_feasible,
        "cardinality_known": after_capacity["known"],
        "cardinality_within_limits": (
            after_capacity["within_capacity"] if after_capacity["known"] else None
        ),
        "observed_position_limits_within": after_position["within_observed_limits"],
        "position_limits_fully_translated": after_position["fully_translated"],
        "platform_transaction_legal": None,
        "scope": (
            "Conditional post-acquisition roster mechanics only; unresolved raw rules, current locks, "
            "claim processing, and platform acceptance remain separate."
        ),
    }
    if not after_capacity["known"]:
        evidence.append(_error("roster_capacity_rules_missing"))
    for field, code in (
        ("required_drop", "explicit_drop_required"),
        ("required_reserve_drop", "reserve_drop_required"),
        ("required_taxi_drop", "taxi_drop_required"),
    ):
        if after_capacity.get(field):
            evidence.append(_error(code, count=after_capacity[field]))
    if after_position["excess"]:
        evidence.append(_error("position_limit_exceeded", excess=after_position["excess"]))
    if not limits_supplied:
        evidence.append(_error(
            "position_limit_rules_missing",
            requirement="Verify that no roster-position maximum blocks this pickup branch.",
        ))
    if after_position["unresolved_rules"]:
        evidence.append(_error("position_limit_rules_unresolved", rules=after_position["unresolved_rules"]))
    if after_position["multi_position_player_ids"]:
        evidence.append(_error("multi_position_limit_semantics_unresolved",
                               player_ids=after_position["multi_position_player_ids"]))

    result["before"] = {
        "roster": before_snapshot,
        "capacity": before_capacity,
        "position_capacity": before_position,
        "coverage": _coverage(
            before_team, slots, players, before_team["starters"], explicit_unavailable
        ),
    }
    result["after"] = {
        "roster": after_snapshot,
        "capacity": after_capacity,
        "position_capacity": after_position,
        "coverage": _coverage(
            after_team, slots, players, after_team["starters"], explicit_unavailable
        ),
    }
    result["roster_changes"] = {
        "added_player_id": candidate_id,
        "dropped_player_id": drop_id,
        "removed_declared_starter": drop_id in before_team["starters"] if drop_id else False,
        "gained_ordinary_coverage_player_ids": sorted(
            set(_ordinary_ids(after_team)) - set(_ordinary_ids(before_team))
        ),
        "lost_ordinary_coverage_player_ids": sorted(
            set(_ordinary_ids(before_team)) - set(_ordinary_ids(after_team))
        ),
    }
    claim_required = acquisition_status == "WAIVERS"
    result["acquisition"] = {
        "status_at_observation": acquisition_status,
        "route": candidate.get("acquisition_route"),
        "observed_at": observed_at,
        "expires_at": _finite(expires_at) if expires_at is not None else None,
        "claim_required": claim_required,
        "conditional_on_success": True,
        "claim_success_guaranteed": False,
        "execution_actionable": candidate.get("execution_actionable"),
        "waiver_process_at_raw": candidate.get("waiver_process_at_raw"),
        "lineup_locked": candidate.get("lineup_locked"),
        "roster_locked": candidate.get("roster_locked"),
        "waiver_state": deepcopy(candidate.get("waiver_state")),
        "execution_status": "hypothetical_not_completed",
        "route_explanation": (
            "A WAIVERS row requires a claim and can fail because of higher claims, bids, tiebreaks, or changed status."
            if claim_required else
            "A FREEAGENT row describes a direct-add route at observation, but availability and successful submission still require confirmation."
        ),
    }
    evidence.extend([
        _error("acquisition_completion_required",
               requirement="Do not put the candidate on the actual roster until an independent post-transaction read confirms it."),
        _error("waiver_cost_state_required",
               requirement="Preserve current priority or budget, reset/tiebreak rules, and pending/competing claims when they affect the choice."),
    ])
    result["valid"] = True

    if forecasts is None:
        evidence.append(_error("forecasts_required_for_lineup_comparison"))
        return result
    if not isinstance(forecasts, list):
        errors.append(_error("forecasts_not_a_list"))
        result["valid"] = False
        return result

    relevant_ids = set(_ordinary_ids(before_team)) | set(_ordinary_ids(after_team))
    eligible_ids: set[str] = set()
    dated_status_unavailable: set[str] = set()
    status_unknown: set[str] = set()
    for player_id in sorted(relevant_ids):
        positions_for_player = set(_player_positions(players[player_id]))
        if any(positions_for_player & set(slot["eligible_positions"]) for slot in slots):
            eligible_ids.add(player_id)
        status, status_at, status_week, status_season = _status_record(players[player_id])
        if status in _UNAVAILABLE_STATUSES:
            if (status_at is not None and status_at <= cutoff and status_week == week
                    and (status_season is None or status_season == league.get("season"))):
                dated_status_unavailable.add(player_id)
            else:
                status_unknown.add(player_id)
    if status_unknown:
        evidence.append(_error("unavailable_status_not_dated_for_evaluation_week",
                               player_ids=sorted(status_unknown)))

    scoring = rules.get("scoring") if isinstance(rules.get("scoring"), dict) else {}
    scoring_hash = digest(scoring)
    groups, rejected = _normalize_forecasts(
        forecasts,
        players=players,
        relevant_ids=relevant_ids,
        league_season=league.get("season"),
        league_week=week,
        decision_at=cutoff,
        scoring=scoring,
        scoring_hash=scoring_hash,
    )
    result["forecast_rejections"] = rejected
    complete = []
    best_ball = rules.get("best_ball")
    for key in sorted(groups, key=lambda item: (item[0], str(item[1]), item[2], item[3])):
        group = groups[key]
        unavailable_for_period: set[str] = set()
        if group["period"]["kind"] == "week":
            unavailable_for_period |= explicit_unavailable
            unavailable_for_period |= dated_status_unavailable
        missing = sorted((eligible_ids - unavailable_for_period) - set(group["points"]))
        comparison = {
            "source": group["source"],
            "season": group["season"],
            "period": group["period"],
            "conditioning": group["conditioning"],
            "source_urls": group["source_urls"],
            "provider_updated_at": group["provider_updated_at"],
            "scoring_basis_by_player": dict(sorted(group["basis"].items())),
            "missing_required_player_ids": missing,
            "conflicting_player_ids": group["conflicts"],
            "status": "incomplete" if missing or group["conflicts"] else "complete",
            "before": None,
            "after": None,
            "delta_points": None,
            "interpretation": (
                "Provider-specific pre-outcome lineup comparison under one common cohort. "
                "The provider is an attributed baseline, not a normative player ranking."
            ),
        }
        if not roster_feasible:
            comparison["status"] = "incomplete"
            comparison["capacity_limitation"] = "The hypothetical post-pickup roster is not feasible under checked rules."
        if best_ball is None:
            comparison["status"] = "incomplete"
            comparison["lineup_timing_limitation"] = "Managed versus best-ball timing is unresolved."
            evidence.append(_error("lineup_timing_rule_required", source=group["source"]))
        elif best_ball is True:
            comparison["status"] = "incomplete"
            comparison["best_ball_limitation"] = "Player means do not identify an expected realized best-ball maximum."
            evidence.append(_error("best_ball_joint_outcome_distribution_required", source=group["source"]))
        if comparison["status"] == "complete":
            before_lineup = _lineup(
                _ordinary_ids(before_team), slots, players, group["points"], unavailable_for_period
            )
            after_lineup = _lineup(
                _ordinary_ids(after_team), slots, players, group["points"], unavailable_for_period
            )
            comparison["before"] = before_lineup
            comparison["after"] = after_lineup
            if before_lineup["complete"] and after_lineup["complete"]:
                comparison["delta_points"] = after_lineup["total_points"] - before_lineup["total_points"]
                complete.append(comparison)
                result["before"]["coverage"] = _coverage(
                    before_team, slots, players, before_lineup["selected_player_ids"], unavailable_for_period
                )
                result["after"]["coverage"] = _coverage(
                    after_team, slots, players, after_lineup["selected_player_ids"], unavailable_for_period
                )
            else:
                comparison["status"] = "incomplete"
                comparison["lineup_capacity_limitation"] = "A required starter slot could not be filled."
        if missing:
            evidence.append(_error("missing_forecasts_withhold_delta", source=group["source"],
                                   period=group["period"], conditioning=group["conditioning"],
                                   player_ids=missing))
        result["forecast_comparisons"].append(comparison)

    if not groups:
        evidence.append(_error("no_common_usable_forecast_cohort"))
    if len(complete) == 1:
        result["result"] = complete[0]
    elif len(complete) > 1:
        evidence.append(_error(
            "multiple_complete_forecast_cohorts_no_automatic_selection",
            cohorts=[{"source": row["source"], "period": row["period"],
                      "conditioning": row["conditioning"]} for row in complete],
        ))
    return result


def evaluate_claim_scenarios(
    league: dict[str, Any],
    team_id: str,
    claims: list[dict[str, Any]],
    forecasts: list[dict[str, Any]] | None = None,
    evaluation_week: int | None = None,
    decision_at: float | str | None = None,
    unavailable: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Enumerate caller-ordered claim outcomes without assigning probabilities.

    Each row is evaluated independently conditional on that acquisition being
    the one that succeeds.  Order is a stated preference order, not a claim
    that the platform will process conditional drops or fallbacks that way.
    """
    errors: list[dict[str, Any]] = []
    if not isinstance(claims, list) or not claims or len(claims) > 20:
        return {"valid": False, "errors": [_error("claims_must_be_a_nonempty_list_of_at_most_20")],
                "scenarios": [], "claim_probabilities": None}
    seen: set[str] = set()
    scenarios = []
    waiver_type = ((league.get("rules") or {}).get("waivers") or {}).get("type") if isinstance(league, dict) else None
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not isinstance(claim.get("candidate"), dict):
            errors.append(_error("invalid_claim_scenario", index=index))
            continue
        candidate_id = str(claim["candidate"].get("player_id"))
        if candidate_id in seen:
            errors.append(_error("duplicate_claim_target", player_id=candidate_id))
            continue
        seen.add(candidate_id)
        declared_order = claim.get("order", index + 1)
        if isinstance(declared_order, bool) or not isinstance(declared_order, int) or declared_order != index + 1:
            errors.append(_error("claim_order_must_match_list_order", index=index, order=declared_order))
            continue
        branch = evaluate_pickup(
            league, team_id, claim["candidate"], claim.get("drop_player_id"), forecasts,
            evaluation_week, decision_at, unavailable,
        )

        bid = claim.get("faab_bid")
        budget_before = claim.get("faab_budget_before")
        priority_before = claim.get("priority_before")
        priority_after = claim.get("priority_after_if_success")
        cost: dict[str, Any] = {
            "waiver_type": waiver_type,
            "faab_bid": bid,
            "faab_budget_before": budget_before,
            "faab_budget_after_if_success": None,
            "priority_before": priority_before,
            "priority_after_if_success": priority_after,
            "competing_claims": deepcopy(claim.get("competing_claims", "unknown")),
            "success_probability": None,
        }
        if bid is not None:
            bid_number = _finite(bid)
            if bid_number is None or bid_number < 0:
                errors.append(_error("invalid_faab_bid", index=index))
            elif budget_before is not None:
                budget_number = _finite(budget_before)
                if budget_number is None or budget_number < bid_number:
                    errors.append(_error("faab_bid_exceeds_or_has_invalid_budget", index=index))
                else:
                    cost["faab_budget_after_if_success"] = budget_number - bid_number
        elif waiver_type == "faab" and claim["candidate"].get("acquisition_status") == "WAIVERS":
            branch["evidence_needed"].append(_error(
                "faab_bid_unknown",
                requirement="An unknown bid cannot be modeled as a certain win or a known budget cost.",
            ))
        for label, value in (("priority_before", priority_before), ("priority_after_if_success", priority_after)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                errors.append(_error("invalid_waiver_priority", index=index, field=label))
        if waiver_type == "priority" and priority_before is None and (
            claim["candidate"].get("acquisition_status") == "WAIVERS"
        ):
            branch["evidence_needed"].append(_error(
                "current_priority_unknown",
                requirement="Supply the observed current rank; do not infer rank 1 from score or standings alone.",
            ))
        branch["claim_cost"] = cost
        scenarios.append({
            "order": declared_order,
            "candidate_id": candidate_id,
            "conditional_on_this_target_being_acquired": branch,
        })

    return {
        "valid": not errors and all(row["conditional_on_this_target_being_acquired"]["valid"] for row in scenarios),
        "errors": errors,
        "team_id": str(team_id),
        "declared_preference_order": [row["candidate_id"] for row in scenarios],
        "scenarios": scenarios,
        "claim_probabilities": None,
        "title_probability_delta": None,
        "execution_semantics": (
            "Independent conditional branches in caller-declared preference order. Verify platform fallback, "
            "conditional-drop, tiebreak, and processing rules before submitting multiple claims."
        ),
        "cost_semantics": (
            "FAAB budget-after and priority-after fields are caller-supplied bookkeeping conditional on this "
            "branch succeeding. They do not establish claim success, actual debit, queue order, or reset behavior."
        ),
    }
