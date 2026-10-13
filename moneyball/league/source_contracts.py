"""Semantic source envelopes; access evidence never certifies forecast accuracy.

Original adapter over the existing ESPN/FFToday contracts. Missing metadata is
retained as unknown, never inferred from a page label or acquisition timestamp.
"""
from dataclasses import dataclass, asdict
from copy import deepcopy
import math
import time
from urllib.parse import urlparse
from .store import DataError, digest, timestamp


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _date(value):
    return None if value is None else timestamp(value)


@dataclass(frozen=True)
class ForecastContract:
    player_id: str
    season: int
    origin: str
    gateway: str | None
    source_url: str
    target: str
    units: str
    period: dict
    statistic: str
    conditioning: str
    scoring_hash: str | None
    values: dict
    provider_updated_at: float | None
    first_observed_at: float | None
    locally_available_at: float | None
    rights: dict
    coverage: dict
    dependency_group: str | None
    gaps: tuple

    def to_dict(self):
        return asdict(self)


def normalize_forecast(row, *, rights=None, coverage=None, now=None):
    """Validate a legacy row without claiming a mean, independence, or entitlement.

    The returned envelope is descriptive. ``qualify_forecast`` decides whether
    its exact target can support a requested comparison; it does not rescore.
    """
    if not isinstance(row, dict):
        raise DataError("Forecast must be an object")
    if rights is not None and not isinstance(rights, dict) or coverage is not None and not isinstance(coverage, dict):
        raise DataError("Rights and coverage must be explicit objects")
    if any(row.get(k) is not None and (not isinstance(row[k], str) or not row[k]) for k in ("gateway", "dependency_group")):
        raise DataError("Gateway and dependency group must be named strings or unknown")
    now = time.time() if now is None else timestamp(now)
    pid, season, origin = row.get("player_id"), row.get("season"), row.get("origin", row.get("source"))
    if not isinstance(pid, (str, int)) or isinstance(pid, bool) or not str(pid):
        raise DataError("Forecast player identity is required")
    if isinstance(season, bool) or not isinstance(season, int) or not 1920 <= season <= 2200:
        raise DataError("Forecast season is invalid")
    if not isinstance(origin, str) or not origin:
        raise DataError("Forecast author is required separately from gateway")
    url = row.get("source_url")
    parsed = urlparse(url if isinstance(url, str) else "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise DataError("Forecast requires a credential-free origin URL")
    period = row.get("period")
    if not isinstance(period, dict) or period.get("kind") not in ("week", "season", "rest_of_season"):
        raise DataError("Forecast requires an explicit supported period")
    if period["kind"] == "week" and (isinstance(period.get("week"), bool) or not isinstance(period.get("week"), int) or not 1 <= period["week"] <= 22):
        raise DataError("Weekly forecast needs an integer week in 1..22")
    values = {}
    if "points" in row:
        values["points"] = row["points"]
    if "components" in row:
        if not isinstance(row["components"], dict) or not row["components"]:
            raise DataError("Forecast components must be a nonempty object")
        values["components"] = deepcopy(row["components"])
    if not values:
        raise DataError("Forecast contains no numerical target")
    nums = ([values["points"]] if "points" in values else []) + list(values.get("components", {}).values())
    if any(not _number(v) for v in nums):
        raise DataError("Forecast values must be finite numbers; missing is not zero")
    target = row.get("target", "fantasy_points" if "points" in values else "scoring_components")
    units = row.get("units", "points" if target == "fantasy_points" else "event_counts")
    if not isinstance(target, str) or not target or not isinstance(units, str) or not units:
        raise DataError("Forecast target and units must be named")
    statistic = row.get("forecast_statistic", "provider_unspecified")
    conditioning = row.get("conditioning", "provider_unspecified")
    if not isinstance(statistic, str) or not statistic or not isinstance(conditioning, str) or not conditioning:
        raise DataError("Forecast statistic and conditioning must be named")
    issue = _date(row.get("provider_updated_at"))
    observed = _date(row.get("first_observed_at", row.get("available_at")))
    local = _date(row.get("local_batch_available_at", row.get("available_at")))
    if any(v is not None and v > now + 60 for v in (issue, observed, local)):
        raise DataError("Forecast timestamps cannot be future observations")
    if observed is not None and local is not None and observed > local:
        raise DataError("First observation cannot follow local availability")
    gaps = []
    for key, value in (("provider_updated_at", issue), ("first_observed_at", observed), ("locally_available_at", local)):
        if value is None:
            gaps.append(key + "_unknown")
    if statistic == "provider_unspecified":
        gaps.append("forecast_statistic_unknown")
    if conditioning == "provider_unspecified":
        gaps.append("availability_conditioning_unknown")
    if not rights:
        gaps.append("retention_and_reuse_rights_unknown")
    if not coverage:
        gaps.append("provider_coverage_unknown")
    scoring_hash = row.get("scoring_hash")
    if scoring_hash is not None and (not isinstance(scoring_hash, str) or len(scoring_hash) != 64 or any(c not in "0123456789abcdef" for c in scoring_hash)):
        raise DataError("Scoring hash must be a canonical SHA-256")
    return ForecastContract(str(pid), season, origin, row.get("gateway"), url,
        target, units, deepcopy(period), statistic, conditioning, scoring_hash,
        values, issue, observed, local, deepcopy(rights or {}), deepcopy(coverage or {}),
        row.get("dependency_group"), tuple(gaps))


def qualify_forecast(contract, league, *, period=None, as_of=None, require_mean=False):
    """Exact compatibility, with unresolved semantics visible to the host.

    Unknown statistic can remain a provider baseline, but cannot become a mean
    or distribution. Compatibility is not an accuracy or currency certificate.
    """
    if not isinstance(contract, ForecastContract):
        raise DataError("Supply a normalized ForecastContract")
    scoring = league.get("rules", {}).get("scoring", {})
    reasons = []
    if contract.season != league.get("season"):
        reasons.append("season_mismatch")
    if period is not None and contract.period != period:
        reasons.append("period_mismatch")
    if as_of is not None:
        cutoff = timestamp(as_of)
        if contract.locally_available_at is None or contract.locally_available_at > cutoff:
            reasons.append("not_locally_knowable_at_decision")
        if contract.provider_updated_at is not None and contract.provider_updated_at > cutoff:
            reasons.append("provider_issue_after_decision")
    if contract.target == "fantasy_points":
        if contract.units != "points":
            reasons.append("target_units_mismatch")
        if contract.scoring_hash != digest(scoring):
            reasons.append("exact_scoring_unverified")
    elif contract.target == "scoring_components":
        needed = {k for k, v in scoring.get("weights", {}).items() if _number(v) and v != 0}
        missing = needed - set(contract.values.get("components", {}))
        if contract.units != "event_counts" or not needed or missing or scoring.get("unsupported") or scoring.get("nonlinear"):
            reasons.append("scoring_components_incomplete_or_nonlinear")
    else:
        reasons.append("target_not_fantasy_comparable")
    if require_mean and contract.statistic != "mean":
        reasons.append("mean_not_identified")
    return {"compatible": not reasons, "reasons": reasons, "semantic_gaps": list(contract.gaps),
            "interpretation": "Exact-target provider baseline only; no demonstrated predictive superiority"}


def qualified_registry(store, source_id=None, *, as_of=None):
    """Registry plus observed forecast versions, independently dated failures.

    Coordinator attempts are intentionally not assigned to authors by URL
    guessing: a gateway, league adapter and author may have different identities.
    Only source-labeled events are attributed. No endpoint is called here.
    """
    from .sources import registry
    result = registry(source_id)
    cutoff = timestamp(as_of)
    forecasts = {}
    for key in store.keys("forecasts", cutoff):
        batch = store.get("forecasts", key, cutoff)
        for row in batch["data"]:
            if not isinstance(row, dict):
                continue
            origin = row.get("origin", row.get("source"))
            prior = forecasts.get(origin)
            if prior is None or batch["available_at"] > prior["locally_available_at"]:
                forecasts[origin] = {"locally_available_at": batch["available_at"],
                    "version_id": batch.get("version_id"), "sha256": batch["sha256"],
                    "provider_updated_at": row.get("provider_updated_at"),
                    "interpretation": "Stored forecast batch; not evidence of latest provider revision"}
    failures = {}
    # SQL reads the complete eligible event history, not a bounded UI sample.
    import json
    with store.db() as db:
        rows = db.execute("SELECT created_at,kind,body FROM events WHERE created_at<=? ORDER BY id", (cutoff,)).fetchall()
    for row in rows:
        body = json.loads(row[2])
        source = body.get("source_id", body.get("source")) if isinstance(body, dict) else None
        if source and ("fail" in row[1] or body.get("status") == "failed"):
            failures[source] = {"attempt_at": row[0], "event_kind": row[1],
                                "last_success_preserved": True}
    for source in result["sources"]:
        source["runtime"] = {"last_stored_forecast": forecasts.get(source["id"]),
            "last_labeled_failure": failures.get(source["id"]),
            "accuracy_evaluated": False, "independence_established": False}
    result["as_of"] = cutoff
    result["runtime_scope"] = "Local evidence only; unknown is not failure or absence. League refresh failures without source labels are not assigned to vendors."
    return result
