"""Compile observed league rules into deterministic trade-evaluation obligations.

This module describes how rules change the mechanical comparison that a caller
must perform.  It does not rank assets, forecast behavior, or recommend trades.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any
from .strategy_sources import method as verified_method


FORMATS = {"dynasty", "redraft", "keeper", "unknown"}
RESEARCH_VERSION = "strategy-mechanisms-v2"


def _stable(value: Any) -> Any:
    """Return a JSON-like value with deterministic mapping order."""
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return deepcopy(value)


def _evidence(path: str, value: Any) -> dict[str, Any]:
    return {"path": path, "value": _stable(value)}


def _method(handle: str) -> dict[str, Any]:
    # References support methods, not football inputs or an empirical edge.
    return verified_method(handle)


def _mechanism(
    ident: str,
    *,
    active: bool,
    activation: str,
    evidence: list[dict[str, Any]],
    implication: str,
    evidence_needed: list[str],
    falsification: str,
    method: str,
) -> dict[str, Any]:
    return {
        "id": ident,
        "active": active,
        "activation": activation,
        "rule_evidence": evidence,
        "method": _method(method),
        "implication": implication,
        "evidence_needed": evidence_needed,
        "falsification": falsification,
    }


def _obligation(
    ident: str,
    operation: str,
    objective: str,
    requires: list[str],
    *,
    blocked_by: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": ident,
        "operation": operation,
        "objective": objective,
        "requires": requires,
        "blocked_by": blocked_by or [],
    }


def _optional_number(value: Any, path: str, missing: list[str]) -> int | float | None:
    if value is None:
        missing.append(path)
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(path + " must be a finite number or null")
    return value


def _optional_count(value: Any, path: str, missing: list[str]) -> int | None:
    value = _optional_number(value, path, missing)
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise ValueError(path + " must be a nonnegative integer or null")
    return value


def _optional_bool(value: Any, path: str, missing: list[str]) -> bool | None:
    if value is None:
        missing.append(path)
        return None
    if not isinstance(value, bool):
        raise ValueError(path + " must be a boolean or null")
    return value


def _optional_text(value: Any, path: str, missing: list[str]) -> str | None:
    if value is None:
        missing.append(path)
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(path + " must be a nonempty string or null")
    return value


def _normalized_starters(value: Any, missing: list[str]) -> list[dict[str, Any]] | None:
    if value is None:
        missing.append("/rules/starters")
        return None
    if not isinstance(value, list):
        raise ValueError("/rules/starters must be a list or null")
    result = []
    for index, slot in enumerate(value):
        path = f"/rules/starters/{index}"
        if not isinstance(slot, dict):
            raise ValueError(path + " must be an object")
        label = slot.get("label")
        eligible = slot.get("eligible_positions")
        if not isinstance(label, str) or not label:
            raise ValueError(path + "/label must be a nonempty string")
        if not isinstance(eligible, list) or not eligible or any(
            not isinstance(position, str) or not position for position in eligible
        ):
            raise ValueError(path + "/eligible_positions must contain nonempty strings")
        result.append({"label": label, "eligible_positions": sorted(set(eligible))})
    return result


def _normalized_weights(value: Any, missing: list[str]) -> dict[str, float] | None:
    if value is None:
        missing.append("/rules/scoring/weights")
        return None
    if not isinstance(value, dict):
        raise ValueError("/rules/scoring/weights must be an object or null")
    result = {}
    for stat in sorted(value):
        weight = value[stat]
        if not isinstance(stat, str) or not stat:
            raise ValueError("scoring-stat names must be nonempty strings")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
            raise ValueError("scoring weight for " + stat + " must be finite")
        result[stat] = float(weight)
    return result


def _raw_list(value: Any, path: str, missing: list[str]) -> list[Any] | None:
    if value is None:
        missing.append(path)
        return None
    if not isinstance(value, list):
        raise ValueError(path + " must be a list or null")
    return _stable(value)


def _keeper_retention_signal(keeper: dict[str, Any]) -> bool | None:
    """Return whether retention is explicitly enabled; None means unresolved."""
    if "enabled" in keeper:
        enabled = keeper["enabled"]
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("/rules/keeper/enabled must be a boolean or null")
        if enabled is not None:
            return enabled
    for key in ("max_keepers", "keepers_per_team", "count"):
        if key in keeper and keeper[key] is not None:
            value = keeper[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"/rules/keeper/{key} must be a nonnegative integer or null")
            return value > 0
    return None


def _rules_fingerprint(league_format: str, rules: dict[str, Any]) -> str:
    """Identify only stable strategy inputs, never rosters or opportunities."""
    payload = {
        "research_version": RESEARCH_VERSION,
        "format": league_format,
        "rules": _stable(rules),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def strategy_profile(league: dict[str, Any]) -> dict[str, Any]:
    """Derive rule-conditioned mechanisms for a mechanical trade comparison.

    ``league`` is the normalized league snapshot described by the service schema.
    Every conclusion is traceable to an exact JSON path/value. Unknown rules stay
    unknown; unsupported scoring never becomes a complete projected total.
    """
    if not isinstance(league, dict):
        raise ValueError("league must be an object")
    rules = league.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("league.rules must be an object")

    missing: list[str] = []
    league_format = league.get("format", "unknown")
    if league_format not in FORMATS:
        raise ValueError("league.format must be dynasty, redraft, keeper, or unknown")
    if league_format == "unknown":
        missing.append("/format")
    season = league.get("season")
    if isinstance(season, bool) or not isinstance(season, int):
        raise ValueError("league.season must be an integer")

    starters = _normalized_starters(rules.get("starters"), missing)
    bench_slots = _optional_count(rules.get("bench_slots"), "/rules/bench_slots", missing)
    reserve_slots = _optional_count(rules.get("reserve_slots"), "/rules/reserve_slots", missing)
    taxi_slots = _optional_count(rules.get("taxi_slots"), "/rules/taxi_slots", missing)
    best_ball = _optional_bool(rules.get("best_ball"), "/rules/best_ball", missing)

    scoring = rules.get("scoring")
    if scoring is None:
        scoring = {}
        missing.append("/rules/scoring")
    if not isinstance(scoring, dict):
        raise ValueError("/rules/scoring must be an object")
    weights = _normalized_weights(scoring.get("weights"), missing)
    unsupported = _raw_list(scoring.get("unsupported"), "/rules/scoring/unsupported", missing)
    nonlinear = _raw_list(scoring.get("nonlinear"), "/rules/scoring/nonlinear", missing)

    playoffs = rules.get("playoffs")
    if playoffs is None:
        playoffs = {}
        missing.append("/rules/playoffs")
    if not isinstance(playoffs, dict):
        raise ValueError("/rules/playoffs must be an object")
    playoff_teams = _optional_count(playoffs.get("teams"), "/rules/playoffs/teams", missing)
    playoff_start = _optional_count(playoffs.get("start_week"), "/rules/playoffs/start_week", missing)
    reseed = _optional_bool(playoffs.get("reseed"), "/rules/playoffs/reseed", missing)
    byes = _optional_count(playoffs.get("byes"), "/rules/playoffs/byes", missing)
    round_weeks = playoffs.get("round_weeks")
    if round_weeks is None:
        missing.append("/rules/playoffs/round_weeks")
    elif not isinstance(round_weeks, list) or any(
        isinstance(week, bool) or not isinstance(week, int) or week < 1 for week in round_weeks
    ):
        raise ValueError("/rules/playoffs/round_weeks must be a list of positive integers or null")
    else:
        round_weeks = list(round_weeks)

    trade = rules.get("trade")
    if trade is None:
        trade = {}
        missing.append("/rules/trade")
    if not isinstance(trade, dict):
        raise ValueError("/rules/trade must be an object")
    deadline = _optional_count(trade.get("deadline_week"), "/rules/trade/deadline_week", missing)
    review_days = _optional_number(trade.get("review_days"), "/rules/trade/review_days", missing)

    waivers = rules.get("waivers")
    if waivers is None:
        waivers = {}
        missing.append("/rules/waivers")
    if not isinstance(waivers, dict):
        raise ValueError("/rules/waivers must be an object")
    waiver_type = _optional_text(waivers.get("type"), "/rules/waivers/type", missing)
    waiver_budget = _optional_number(waivers.get("budget"), "/rules/waivers/budget", missing)
    waiver_clear = _optional_number(waivers.get("clear_days"), "/rules/waivers/clear_days", missing)

    keeper = rules.get("keeper")
    if keeper is None:
        keeper = {}
        missing.append("/rules/keeper")
    if not isinstance(keeper, dict):
        raise ValueError("/rules/keeper must be an object")
    retention = _keeper_retention_signal(keeper)
    if league_format == "keeper" and retention is None:
        missing.append("/rules/keeper/enabled_or_count")

    if not isinstance(league.get("teams"), dict):
        raise ValueError("league.teams must be an object")

    mechanisms: list[dict[str, Any]] = []
    obligations: list[dict[str, Any]] = []

    if starters is not None:
        mechanisms.append(_mechanism(
            "complete_lineup_substitution",
            active=bool(starters),
            activation="At least one indexed starting slot is configured.",
            evidence=[_evidence("/rules/starters", starters)],
            implication="Compare complete before/after legal assignments and report every entrant, exit, and unfilled slot.",
            evidence_needed=["platform player eligibility", "same-horizon source-coherent projections", "current deployability"],
            falsification="No assignment is usable if any configured slot or relevant player eligibility is unresolved.",
            method="maximum_weight_legal_slot_assignment",
        ))
        obligations.append(_obligation(
            "evaluate_complete_lineups",
            "assign_every_configured_starter_slot_before_and_after",
            "Measure the trade through actual substitutions rather than sums of transferred players.",
            ["rules.starters", "player eligibility", "projection set", "deployability"],
        ))

        multi = [slot for slot in starters if len(slot["eligible_positions"]) > 1]
        if multi:
            mechanisms.append(_mechanism(
                "multi_eligible_slot_competition",
                active=True,
                activation="One or more starting slots accepts multiple positions.",
                evidence=[_evidence("/rules/starters", starters)],
                implication="A player can displace a starter at another position; positional deltas cannot be evaluated independently.",
                evidence_needed=["one joint assignment across all indexed slots"],
                falsification="The mechanism is inactive if every starting slot has exactly one eligible position.",
                method="joint_slot_opportunity_cost",
            ))
            obligations.append(_obligation(
                "joint_flex_assignment",
                "solve_multi_position_slots_jointly",
                "Preserve the opportunity cost created by shared eligible slots.",
                ["full starter-slot eligibility matrix"],
            ))
        qb_optional = [slot for slot in multi if "QB" in slot["eligible_positions"]]
        if qb_optional:
            mechanisms.append(_mechanism(
                "quarterback_optional_capacity",
                active=True,
                activation="A multi-position starter slot includes QB.",
                evidence=[_evidence("/rules/starters", starters)],
                implication="Report which actual alternative a QB displaces; do not apply a generic format premium.",
                evidence_needed=["deployable QB alternatives on both rosters", "joint slot assignment"],
                falsification="No QB-specific optional capacity exists when QB appears only in dedicated slots or not at all.",
                method="observed_replacement_path",
            ))
            obligations.append(_obligation(
                "measure_qb_optional_slot_replacement",
                "compare_qb_and_non_qb_occupants_of_shared_slots",
                "Identify the roster-specific source of QB benefit without a universal premium.",
                ["QB deployability", "all shared-slot alternatives"],
            ))

    mechanisms.append(_mechanism(
        "lineup_selection_timing",
        active=best_ball is not None,
        activation="Best-ball status is observed." if best_ball is not None else "Best-ball status is unresolved.",
        evidence=[_evidence("/rules/best_ball", best_ball)],
        implication=(
            "Evaluate realized eligible lineup maxima under the platform's best-ball timing rules."
            if best_ball is True else
            "Evaluate pre-outcome start decisions and do not credit hindsight selection."
            if best_ball is False else
            "Do not choose between pre-outcome and realized lineup selection until the rule is known."
        ),
        evidence_needed=["best-ball calculation and substitution semantics" if best_ball else "pre-lock availability and lineup choices"],
        falsification="A different verified best-ball setting reverses the required lineup timing treatment.",
        method="rule_conditioned_lineup_timing",
    ))
    obligations.append(_obligation(
        "respect_lineup_timing",
        "use_realized_best_lineup" if best_ball is True else "use_pre_outcome_lineup" if best_ball is False else "block_timing_dependent_comparison",
        "Match lineup selection to the observed rule rather than hindsight convenience.",
        ["rules.best_ball"],
        blocked_by=["/rules/best_ball"] if best_ball is None else [],
    ))

    capacity_evidence = [
        _evidence("/rules/bench_slots", bench_slots),
        _evidence("/rules/reserve_slots", reserve_slots),
        _evidence("/rules/taxi_slots", taxi_slots),
    ]
    mechanisms.append(_mechanism(
        "post_trade_capacity",
        active=any(value is not None for value in (bench_slots, reserve_slots, taxi_slots)),
        activation="At least one roster-capacity count is observed.",
        evidence=capacity_evidence,
        implication="Unequal packages require explicit legal placement or drop branches; unknown counts never mean zero slots.",
        evidence_needed=["ordinary roster count", "reserve/taxi eligibility", "player lock/status", "candidate drops"],
        falsification="A branch is invalid if it exceeds a verified capacity or uses an ineligible reserve/taxi placement.",
        method="capacity_constrained_holdings_conservation",
    ))
    obligations.append(_obligation(
        "enumerate_capacity_branches",
        "enumerate_legal_placements_and_required_drops",
        "Compare final legal rosters rather than a pre-cut upper bound.",
        ["bench/reserve/taxi counts", "placement eligibility", "drop candidates"],
        blocked_by=[path for path in ("/rules/bench_slots", "/rules/reserve_slots", "/rules/taxi_slots") if path in missing],
    ))

    scoring_blockers = []
    if unsupported:
        scoring_blockers.append("/rules/scoring/unsupported")
    if nonlinear:
        scoring_blockers.append("/rules/scoring/nonlinear")
    complete_scoring = weights is not None and unsupported == [] and nonlinear == []
    mechanisms.append(_mechanism(
        "exact_scoring_translation",
        active=weights is not None,
        activation="A canonical scoring-weight map is observed." if weights is not None else "Scoring weights are unresolved.",
        evidence=[
            _evidence("/rules/scoring/weights", weights),
            _evidence("/rules/scoring/unsupported", unsupported),
            _evidence("/rules/scoring/nonlinear", nonlinear),
        ],
        implication=(
            "Rescore source event projections under these exact weights."
            if complete_scoring else
            "A complete projected total is blocked; return only an explicitly accepted supported subtotal with every omitted component."
        ),
        evidence_needed=(
            ["projected event means for each nonzero canonical stat", "matching scoring hash"]
            + (["expected bonus-event frequency or a joint event distribution for every nonlinear rule"] if nonlinear else [])
            + (["a verified canonical mapping for every unsupported raw rule"] if unsupported else [])
        ),
        falsification="Any applicable nonzero rule without compatible projected-event semantics invalidates a complete total.",
        method="rule_exact_event_rescoring",
    ))
    obligations.append(_obligation(
        "gate_scoring_completeness",
        "rescore_exactly" if complete_scoring else "block_complete_total_or_require_explicit_partial_scope",
        "Prevent a matching hash or provider total from hiding missing and nonlinear scoring semantics.",
        ["canonical rule mapping", "projection component coverage", "component aggregation semantics"],
        blocked_by=scoring_blockers + (["/rules/scoring/weights"] if weights is None else []),
    ))

    mechanisms.append(_mechanism(
        "postseason_service_window",
        active=playoff_teams is not None or playoff_start is not None,
        activation="At least one postseason timing rule is observed.",
        evidence=[
            _evidence("/rules/playoffs/teams", playoff_teams),
            _evidence("/rules/playoffs/start_week", playoff_start),
            _evidence("/rules/playoffs/round_weeks", round_weeks),
            _evidence("/rules/playoffs/reseed", reseed),
            _evidence("/rules/playoffs/byes", byes),
        ],
        implication="Expose late-week availability, bye, and coverage effects; do not infer title probability from playoff size.",
        evidence_needed=["exact playoff calendar", "NFL schedule", "week-specific deployability"],
        falsification="Unknown bracket or reseeding semantics block claims that depend on a specific postseason path.",
        method="finite_postseason_service_window",
    ))

    mechanisms.append(_mechanism(
        "trade_execution_timing",
        active=deadline is not None or review_days is not None,
        activation="A trade deadline or review duration is observed.",
        evidence=[_evidence("/rules/trade/deadline_week", deadline), _evidence("/rules/trade/review_days", review_days)],
        implication="Keep mechanical roster benefit separate from whether the transaction can clear before relevant games.",
        evidence_needed=["current league week/time", "lock status", "verified review/veto semantics"],
        falsification="The trade is not executable if verified timing prevents completion before the claimed service window.",
        method="transaction_timeline_feasibility",
    ))
    obligations.append(_obligation(
        "check_trade_timeline",
        "evaluate_submission_and_clearance_timing",
        "Avoid treating an unavailable or late-clearing trade as current roster service.",
        ["deadline", "review duration", "current league time"],
        blocked_by=[path for path in ("/rules/trade/deadline_week", "/rules/trade/review_days") if path in missing],
    ))

    mechanisms.append(_mechanism(
        "replacement_acquisition_friction",
        active=any(value is not None for value in (waiver_type, waiver_budget, waiver_clear)),
        activation="At least one waiver rule is observed.",
        evidence=[
            _evidence("/rules/waivers/type", waiver_type),
            _evidence("/rules/waivers/budget", waiver_budget),
            _evidence("/rules/waivers/clear_days", waiver_clear),
        ],
        implication="Do not credit a future replacement without an observed available player, acquisition cost, delay, and competing claims.",
        evidence_needed=["current free-agent pool", "team budget/priority", "waiver timing", "roster space"],
        falsification="A replacement branch fails if the named player is unavailable or cannot be acquired under the observed rules.",
        method="feasible_replacement_branch",
    ))
    obligations.append(_obligation(
        "price_replacement_as_action",
        "require_named_feasible_acquisition_for_replacement_credit",
        "Compare trade-plus-management and hold-plus-management symmetrically.",
        ["available player ID", "waiver rules", "team acquisition resources"],
        blocked_by=[path for path in ("/rules/waivers/type", "/rules/waivers/budget", "/rules/waivers/clear_days") if path in missing],
    ))

    format_evidence = [_evidence("/format", league_format), _evidence("/rules/keeper", keeper)]
    if league_format == "redraft" and retention is not True:
        residual_seasons = 0
        horizon_status = "current_season_only"
    else:
        residual_seasons = None
        horizon_status = (
            "retention_rules_required" if league_format in {"keeper", "unknown"}
            else "future_rights_open_ended" if league_format == "dynasty"
            else "format_retention_conflict"
        )
    if league_format == "redraft" and retention is True:
        missing.append("/format_conflicts_with_/rules/keeper")
    mechanisms.append(_mechanism(
        "retained_asset_horizon",
        active=league_format != "unknown",
        activation="League format is observed." if league_format != "unknown" else "League format is unresolved.",
        evidence=format_evidence,
        implication=(
            "No player or right receives residual value after this season."
            if residual_seasons == 0 else
            "Preserve future player/right optionality and obligations without a universal age curve, variance premium, or terminal value."
        ),
        evidence_needed=(
            ["current-season service and remaining schedule"] if residual_seasons == 0 else
            ["keeper limits/costs or complete future-right ownership", "dated role and contract evidence", "explicit horizon scenarios"]
        ),
        falsification="Observed retention or keeper rights falsify a zero-residual horizon; an explicit redraft with no retention falsifies carryover value.",
        method="format_conditioned_asset_horizon",
    ))
    obligations.append(_obligation(
        "apply_asset_horizon",
        "set_postseason_residual_to_zero" if residual_seasons == 0 else "preserve_unpriced_future_options",
        "Use the observed retention structure without importing a generic redraft/dynasty doctrine.",
        ["format", "keeper/rights rules"],
        blocked_by=["/rules/keeper/enabled_or_count"] if league_format == "keeper" and retention is None else [],
    ))

    obligations.append(_obligation(
        "validate_ownership",
        "conserve_every_transferred_asset_across_all_teams",
        "Ensure the proposed state is mechanically possible on the frozen snapshot.",
        ["all teams", "platform-scoped IDs", "snapshot identity"],
    ))

    # Deduplicate paths introduced by a missing container and its fields while
    # retaining sorted, deterministic output.
    missing_rules = sorted(set(missing))
    rules_fingerprint = _rules_fingerprint(league_format, rules)
    open_questions = [
        {
            "id": "resolve:" + path,
            "question": "What is the verified value and platform meaning of " + path + "?",
            "blocks": [row["id"] for row in obligations if path in row["blocked_by"]],
        }
        for path in missing_rules
    ]
    if nonlinear:
        open_questions.append({
            "id": "resolve_nonlinear_scoring",
            "question": "Does the projection source supply expected trigger frequency or the joint distribution for each nonlinear scoring rule?",
            "blocks": ["gate_scoring_completeness"],
        })
    if unsupported:
        open_questions.append({
            "id": "map_unsupported_scoring",
            "question": "Can every unsupported nonzero scoring rule be mapped to a compatible projected event with verified aggregation semantics?",
            "blocks": ["gate_scoring_completeness"],
        })
    return {
        "schema_version": 1,
        "research_version": RESEARCH_VERSION,
        "rules_fingerprint": rules_fingerprint,
        "profile_scope": "stable_rule_conditioned_strategy",
        "cache_policy": {
            "recompile_when": ["rules_fingerprint changes", "research_version changes"],
            "do_not_recompile_for": ["roster-only refresh", "new candidate trade", "projection-only refresh", "standings-only refresh"],
        },
        "objective": "Help the calling session choose actions that improve championship prospects over the league's actual ownership horizon. Legal lineup service is an intermediate diagnostic, not the objective or a title estimate.",
        "competition_structure": {
            "league_teams": rules.get("team_count"),
            "playoff_teams": playoff_teams,
            "round_weeks": round_weeks,
            "interpretation": "Qualification, seeding and elimination are separate bottlenecks. A smaller qualifying field increases the importance of maintaining a viable regular-season path; no numeric win probabilities follow from field size alone.",
            "risk_rule": "No universal ceiling or safety premium. Evaluate whether this action improves the relevant matchups while preserving qualification and subsequent service.",
        },
        "league": {
            "platform": league.get("platform"),
            "league_id": str(league["league_id"]) if league.get("league_id") is not None else None,
            "name": league.get("name"),
            "season": season,
            "format": league_format,
        },
        "horizon": {
            "residual_seasons": residual_seasons,
            "status": horizon_status,
            "utility": "finite horizon supplied by caller; no universal variance or terminal-value rule",
        },
        "scoring_support": {
            "complete": complete_scoring,
            "blocked": not complete_scoring,
            "supported_linear_stats": sorted(weights or {}),
            "unsupported": unsupported,
            "nonlinear": nonlinear,
        },
        "mechanisms": mechanisms,
        "obligations": obligations,
        "operative_priorities": [
            "Freeze one fresh dynamic league packet and validate asset ownership.",
            "Apply the cached rule profile to enumerate legal post-trade roster branches.",
            "Compare complete before/after assignments under one source-coherent scoring view at a time.",
            "Expose deployability, scoring, future-right, and replacement gaps as reversal questions.",
            "Leave preference, negotiation, and final reasoning to the calling session.",
        ],
        "action_checklist": [
            {"step": index + 1, "obligation_id": row["id"], "operation": row["operation"]}
            for index, row in enumerate(obligations)
        ],
        "dynamic_inputs_required": [
            "snapshot hash and endpoint timestamps",
            "all current team holdings and future rights",
            "candidate trade and explicit placement/drop branches",
            "current deployability and locks",
            "source-coherent projections and component coverage",
            "free-agent pool and acquisition resources when replacement is claimed",
        ],
        "open_questions": open_questions,
        "missing_rules": missing_rules,
        "interpretation": "Reusable deterministic rule implications only. Dynamic opportunities live in the refreshed league packet. Magnitudes, player quality, market price, acceptance, and trade preference remain with the calling session.",
    }
