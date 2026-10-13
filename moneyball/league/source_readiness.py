"""Decision-scoped source qualification; native forecast rows remain untouched.

Uses the native immutable Store and EvidenceIndex instead of another ingestion
framework. SQLite WHERE/NOT EXISTS enforce latest-at-cutoff selection (primary
reference: https://sqlite.org/lang_select.html). Reporting coverage is a scoped
observation, never a certificate that no other news exists.
"""
from collections import Counter
import json
from copy import deepcopy

from .source_contracts import normalize_forecast, qualify_forecast
from .store import DataError, timestamp, digest


def _registered_reports(store, refs, cutoff):
    """Read existing documents without creating an index or doing network IO."""
    if not refs:
        return []
    with store._read_db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_documents'").fetchone():
            return []
        rows = db.execute("""SELECT c.body,d.body,d.observed_at,d.id,d.hash
            FROM evidence_claims c JOIN evidence_documents d ON d.id=c.document_id
            WHERE d.observed_at<=? AND c.scope='shared'
            AND c.player_ref IN (%s)
            AND NOT EXISTS (SELECT 1 FROM evidence_documents newer
                WHERE newer.document_key=d.document_key AND newer.observed_at<=?
                AND (newer.observed_at>d.observed_at OR
                    (newer.observed_at=d.observed_at AND newer.id>d.id)))
            ORDER BY d.observed_at DESC,d.id DESC,c.id ASC""" % ",".join("?" for _ in refs),
            [cutoff, *refs, cutoff]).fetchall()
    result = []
    for r in rows:
        doc = json.loads(r[1])
        if digest(doc) != r[4]:
            raise DataError("Source reporting document integrity failure")
        result.append({"claim": json.loads(r[0]), "document": doc,
            "first_observed_at": r[2], "document_id": r[3], "sha256": r[4]})
    return result


def report_coverage(reports, *, player_refs, season, week=None, as_of=None, max_age_seconds=86400):
    """Require explicit period and publication/report date; retrieval isn't either.

    Callers may supply verified targeted reports without an automated feed. Each
    report has claim{player_ref,facet,kind,qualifiers{season,week}}, document{
    source{origin,url},published_at or event_at,coverage{season,week}}, and local
    first_observed_at. Publisher authority and entailment remain host judgments.
    """
    cutoff = timestamp(as_of)
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not 0 < max_age_seconds <= 30*86400:
        raise DataError("Report age budget must be positive and at most thirty days")
    refs = set(player_refs)
    accepted, rejected = [], []
    for report in reports:
        if not isinstance(report, dict):
            rejected.append({"reason": "invalid_report"}); continue
        claim, doc = report.get("claim", {}), report.get("document", {})
        if not isinstance(claim, dict) or not isinstance(doc, dict):
            rejected.append({"reason": "invalid_report"}); continue
        ref = claim.get("player_ref")
        if ref not in refs:
            continue
        qualifiers, coverage = claim.get("period", claim.get("qualifiers", {})), doc.get("coverage", {})
        # Native EvidenceIndex qualifiers are prose lists, not period metadata.
        if isinstance(qualifiers, list):
            qualifiers = {}
        if not isinstance(qualifiers, dict) or not isinstance(coverage, dict):
            rejected.append({"player_ref": ref, "reason": "invalid_period_metadata"}); continue
        report_season = qualifiers.get("season", coverage.get("season"))
        report_week = qualifiers.get("week", coverage.get("week"))
        reason = None
        if claim.get("kind", "observation") != "observation" or claim.get("facet") not in ("availability", "injury", "role", "news"):
            reason = "not_targeted_report_observation"
        elif report_season != season or isinstance(report_season, bool) or week is not None and (report_week != week or isinstance(report_week, bool)):
            reason = "report_period_missing_or_mismatched"
        else:
            source = doc.get("source", {})
            if not isinstance(source, dict) or not source.get("origin") or not source.get("url"):
                reason = "report_source_missing"
            else:
                try:
                    observed = timestamp(report["first_observed_at"])
                    issued = timestamp(doc.get("published_at") or doc["event_at"])
                    if observed > cutoff or issued > cutoff:
                        reason = "report_not_knowable_at_cutoff"
                    elif cutoff - issued > max_age_seconds:
                        reason = "report_outside_age_budget"
                except (KeyError, DataError, TypeError, ValueError):
                    reason = "report_date_unknown"
        if reason:
            rejected.append({"player_ref": ref, "reason": reason})
        else:
            accepted.append({"player_ref": ref, "facet": claim["facet"],
                "published_or_event_at": issued, "first_observed_at": observed,
                "source": deepcopy(source), "document_id": report.get("document_id"),
                "sha256": report.get("sha256")})
    covered = {r["player_ref"] for r in accepted}
    return {"status": "targeted_reports_present" if refs and refs <= covered else "partial" if covered else "not_requested" if not refs else "missing",
        "covered_player_refs": sorted(covered), "missing_player_refs": sorted(refs-covered),
        "accepted": accepted, "rejected": rejected,
        "scope": "Dated supplied observations for focused players; not exhaustive news coverage or source-truth certification"}


def prepare_sources(service, alias, league, specification, forecast_rows, *, as_of=None, reports=None):
    """Prepare semantic contracts and operation-specific coverage without refresh.

    Explicit ``forecast_periods`` controls comparison horizons. Lineup/waiver
    defaults require the requested/current week. Other operations disclose all
    observed horizons without silently selecting a discount rate. No title odds
    are manufactured when weekly or multiyear predictive evidence is missing.
    """
    cutoff = timestamp(as_of)
    spec = specification
    deadline = timestamp(spec["deadline"]) if spec.get("deadline") is not None else None
    focus = spec.get("player_ids", [])
    if not isinstance(focus, list) or len(focus) > 12 or any(not isinstance(p, str) for p in focus):
        raise DataError("Source preparation needs at most twelve exact player IDs")
    intent = spec.get("intent", "mixed")
    week = spec.get("week", league.get("week"))
    periods = spec.get("forecast_periods")
    if periods is None:
        periods = [{"kind": "week", "week": week}] if intent in ("lineup", "waiver") else []
    if not isinstance(periods, list) or len(periods) > 20 or any(not isinstance(p, dict) for p in periods):
        raise DataError("Supply at most twenty explicit forecast periods")
    wanted = set(focus)
    contracts, rejected, compatible = [], [], []
    for index, row in enumerate(forecast_rows):
        if not isinstance(row, dict):
            rejected.append({"index": index, "reason": "invalid_contract"})
            continue
        if wanted and str(row.get("player_id")) not in wanted:
            continue
        try:
            c = normalize_forecast(row, rights=row.get("rights"), coverage=row.get("coverage"), now=cutoff)
            checks = [qualify_forecast(c, league, period=p, as_of=cutoff) for p in periods] if periods else [qualify_forecast(c, league, as_of=cutoff)]
            if not any(check["compatible"] for check in checks):
                rejected.append({"index": index, "player_id": c.player_id,
                    "reasons": sorted({reason for check in checks for reason in check["reasons"]})})
            else:
                compatible.append(c)
            contracts.append({"contract": c.to_dict(), "qualification": checks})
        except (DataError, AttributeError, TypeError) as exc:
            rejected.append({"index": index, "reason": "invalid_contract", "error_type": type(exc).__name__})
    missing = []
    for pid in focus:
        if periods:
            for period in periods:
                if not any(c.player_id == pid and c.period == period for c in compatible):
                    missing.append({"player_id": pid, "period": period})
        elif not any(c.player_id == pid for c in compatible):
            missing.append({"player_id": pid, "period": None})
    refs = [league["platform"] + ":" + pid for pid in focus]
    news = report_coverage(_registered_reports(service.store, refs, cutoff) if reports is None else reports,
        player_refs=refs, season=league["season"], week=week, as_of=cutoff,
        max_age_seconds=spec.get("report_age_budget_seconds", 86400))
    future_needed = league.get("format") in ("dynasty", "keeper") and intent in ("draft", "trade", "trade_discovery", "rebuild", "mixed")
    gaps = []
    if not compatible or missing:
        gaps.append({"code": "compatible_forecast_coverage_incomplete", "missing": missing,
            "effect": "Numeric current contribution requires matching dated forecasts; do not substitute rankings or missing-as-zero"})
    if news["missing_player_refs"]:
        gaps.append({"code": "targeted_reporting_missing_or_stale", "player_refs": news["missing_player_refs"],
            "effect": "Investigate decision-sensitive role/availability reports; this does not mean no news exists"})
    if future_needed:
        gaps.append({"code": "future_projection_evidence_required", "effect": "Current-period projections do not identify future service or championship probability; use explicitly sourced conditional future worlds"})
    if deadline is not None and deadline <= cutoff:
        gaps.append({"code": "decision_deadline_elapsed", "effect": "Evidence preparation cannot establish that an action remains feasible after its declared deadline"})
    return {"status": "partial" if gaps else "prepared_with_semantic_limits", "forecast_contracts": contracts,
        "compatible_row_count": len(compatible), "rejections": rejected,
        "origin_counts": dict(Counter(c.origin for c in compatible)),
        "independence_established": False, "required_periods": periods,
        "reporting": news, "gaps": gaps, "as_of": cutoff,
        "deadline": deadline, "seconds_to_deadline_at_cutoff": deadline-cutoff if deadline is not None else None,
        "recheck": "Source issue times and targeted reporting must be rechecked before the action lock; later deadline does not extend evidence currency",
        "scope": "Source readiness only; retained native forecasts drive mechanical comparisons. No accuracy, completeness, recommendation or title-probability certification."}


def prepare_action_sources(service, alias, league, specification, forecast_rows, player_ids, *, as_of):
    """Qualify an expanded action without widening the public preparation limit.

    A comparison may introduce people absent from the original question. Read
    them at the same frozen cutoff, in bounded groups, retaining every check.
    This performs no network acquisition and cannot renew old evidence.
    """
    focus = sorted(set(player_ids))
    checks, gaps = [], []
    for start in range(0, len(focus), 12):
        check = prepare_sources(service, alias, league,
            {**specification, "player_ids": focus[start:start + 12]},
            forecast_rows, as_of=as_of)
        checks.append(check)
        for gap in check["gaps"]:
            if gap not in gaps:
                gaps.append(deepcopy(gap))
    return {"focused_player_ids": focus, "as_of": as_of, "checks": checks,
            "gaps": gaps, "scope": "Action-specific local evidence qualification; not acquisition or recommendation approval"}
