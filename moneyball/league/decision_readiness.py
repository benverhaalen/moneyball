"""Capability-scoped disclosure, never permission or recommendation approval.

Transfer: CDS Hooks task-specific context and stale-guidance boundaries
(https://cds-hooks.hl7.org/2.0/). Keep the complete gap ledger; only explicit
typed codes may narrow a gap's effect. Unknown codes/prose stay conservative.
Existing calculators remain the authority for their conditional results.
"""
from copy import deepcopy


CAPABILITIES = (
    "snapshot_accounting", "current_point_calculation", "current_decision_evidence",
    "future_roster_accounting", "future_valuation", "platform_execution",
    "championship_probability",
)
_FUTURE = {"future_roster_accounting", "future_valuation", "championship_probability"}
_GAP_SCOPES = {
    "retention_horizon_unspecified": {"future_valuation", "championship_probability"},
    "future_projection_evidence_required": {"future_valuation", "championship_probability"},
    "forecasts_missing": {"current_point_calculation", "current_decision_evidence", "future_valuation", "championship_probability"},
    "compatible_forecast_coverage_incomplete": {"current_point_calculation", "current_decision_evidence", "future_valuation", "championship_probability"},
    "targeted_reporting_missing_or_stale": {"current_decision_evidence", "platform_execution", "championship_probability"},
    "news_coverage_not_certified": {"current_decision_evidence", "platform_execution", "championship_probability"},
    "league_refresh_failed": {"current_decision_evidence", "platform_execution", "championship_probability"},
    "decision_deadline_elapsed": {"current_decision_evidence", "platform_execution", "championship_probability"},
    "strategy_research_not_confirmed": {"current_decision_evidence", "future_valuation", "championship_probability"},
    "acquisition_universe_missing": {"current_decision_evidence", "future_valuation", "platform_execution", "championship_probability"},
    "lock_state_not_verified": {"platform_execution", "current_decision_evidence", "championship_probability"},
    "keeper_eligibility_or_cost_required": _FUTURE | {"platform_execution"},
    "keeper_limit_required": _FUTURE | {"platform_execution"},
    "keeper_limit_exceeded": _FUTURE | {"platform_execution"},
    "keeper_right_expired_or_unresolved": _FUTURE | {"platform_execution"},
    "keeper_cost_right_unavailable": _FUTURE | {"platform_execution"},
    "unsupported_keeper_cost_terms": _FUTURE | {"platform_execution"},
    "exact_retention_rules_required": _FUTURE | {"platform_execution"},
    "retention_policy_conflict": _FUTURE | {"platform_execution"},
}


def scoped_gaps(gaps):
    """Add scope without mutating or suppressing any original detail."""
    rows = []
    for index, gap in enumerate(gaps):
        code = gap.get("code") if isinstance(gap, dict) else None
        scopes = _GAP_SCOPES.get(code, set(CAPABILITIES))
        rows.append({"index": index,
                     "affects": [c for c in CAPABILITIES if c in scopes],
                     "scope_basis": "typed_code" if code in _GAP_SCOPES else "unknown_conservative"})
    return rows


def _details(accounting):
    """Inspect calculator envelopes, not duplicated full league snapshots."""
    yield accounting
    for key in ("mechanics", "comparison"):
        if isinstance(accounting.get(key), dict):
            yield from _details(accounting[key])
    for step in accounting.get("steps", []):
        if isinstance(step, dict):
            yield step
            if isinstance(step.get("comparison"), dict):
                yield from _details(step["comparison"])


def _complete_weekly(row, season):
    """Recognize complete calculator shapes, not a reassuring status alone."""
    if (not isinstance(row, dict) or row.get("season") != season
            or (row.get("period") or {}).get("kind") != "week"
            or row.get("missing_required_player_ids") or row.get("conflicting_player_ids")):
        return False
    status = row.get("status")
    if status == "complete_counterfactual":
        if "named_after" in row:
            return all((row.get(k) or {}).get("complete") is True
                       for k in ("optimized_hold", "named_after"))
        return (row.get("modeled_lineup") or {}).get("complete") is True
    if status == "complete":
        teams = row.get("teams") or {}
        return bool(teams) and all(
            all((team.get(k) or {}).get("complete") is True for k in ("before", "after"))
            for team in teams.values())
    if status == "conditional_weekly_sensitivity":
        return all((row.get(k) or {}).get("complete") is True for k in ("before", "after"))
    return False


def describe(context, entry=None):
    """An additive vector; legacy readiness and full gaps remain intact.

    `available_conditional` describes supplied arithmetic only. It never
    establishes freshness, execution, predictive skill or title probability.
    Even gap-free evidence is `unassessed`, not automatic recommendation approval.
    """
    gaps = deepcopy(context.get("gaps", []))
    rows, accounting = [], {}
    if entry is not None:
        for gap in entry.get("source_readiness", {}).get("gaps", []):
            if gap not in gaps:
                gaps.append(deepcopy(gap))
        accounting = entry.get("accounting", {})
        for detail in _details(accounting):
            for key in ("gaps", "evidence_needed", "warnings", "errors"):
                for gap in detail.get(key, []) or []:
                    if gap not in gaps:
                        gaps.append(deepcopy(gap))
            rows.extend(detail.get("forecast_comparisons", []))
        for gap in entry.get("gaps", []):
            if gap not in gaps:
                gaps.append(deepcopy(gap))
    scoped = scoped_gaps(gaps)
    result = {}
    for capability in CAPABILITIES:
        relevant = [row["index"] for row in scoped if capability in row["affects"]]
        result[capability] = {"status": "partial" if relevant else "unassessed", "gap_indices": relevant}
    blocked = entry is not None and entry.get("status") == "blocked"
    result["snapshot_accounting"]["status"] = (
        "blocked" if blocked else "available_conditional" if accounting else "not_compared")
    # Only a complete current-season weekly calculator result establishes that
    # arithmetic exists. Future/season totals cannot imply a current-week result.
    season = context.get("league_binding", {}).get("season")
    complete = [r for r in rows if _complete_weekly(r, season)]
    result["current_point_calculation"].update(
        status="blocked" if blocked else "available_conditional" if complete else "not_computed",
        cohort_count=len(complete),
        scope="Complete supplied weekly arithmetic; see cohort periods, conditioning and gaps before use")
    future_plan = entry is not None and entry.get("kind") == "roster_plan" and any(
        (step.get("action") or {}).get("kind") == "Retain"
        or (step.get("conditional_state_changes") or {}).get("season")
        for step in accounting.get("steps", []) if isinstance(step, dict))
    result["future_roster_accounting"]["status"] = (
        "blocked" if blocked and future_plan else
        "available_conditional" if future_plan and accounting.get("valid") is True else "not_compared")
    for capability in ("future_valuation", "championship_probability"):
        result[capability]["status"] = "not_estimated"
    result["platform_execution"]["status"] = "blocked" if blocked else "unverified"
    return {"version": 1, "capabilities": result, "all_gaps": gaps, "scoped_gaps": scoped,
            "scope": "Calculation support and evidence gaps, not a recommendation grade or authorization"}
