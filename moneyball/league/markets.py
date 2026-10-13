"""Bounded market evidence for one event and one precisely stated scope.

This module reads documented public market snapshots, validates caller-supplied
quotes, and converts paired odds into a labeled proportional no-vig approximation.
It does not place orders, project fantasy points, or infer season-long player value.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any
from urllib.parse import quote, urlencode

from .store import DataError, timestamp


SIDE_PAIRS = ({"over", "under"}, {"yes", "no"})
KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_NFL_SERIES = {
    "KXNFLPASSYDS": "single-game passing-yards thresholds",
    "KXNFLRSHYDS": "single-game rushing-yards thresholds",
    "KXNFLRECYDS": "single-game receiving-yards thresholds",
    "KXNFLPASSTDS": "single-game passing-touchdown thresholds",
    "KXNFLPASSINT": "single-game passing-interception thresholds",
    "KXNFLFFWEEKLEAD": "weekly fantasy-position leader contracts",
}


def market_sources() -> list[dict]:
    """List the public NFL series verified against Kalshi's current REST API."""
    return [
        {
            "provider": "kalshi", "series_ticker": ticker, "scope": scope,
            "access": "public_read_only_rest", "authentication": "none",
            "markets_url": KALSHI_BASE + "/markets?" + urlencode(
                {"series_ticker": ticker, "status": "open", "limit": 20, "mve_filter": "exclude"}
            ),
            "series_url": KALSHI_BASE + "/series/" + ticker,
        }
        for ticker, scope in KALSHI_NFL_SERIES.items()
    ]


def fetch_kalshi(client: Any, series_ticker: str, limit: int = 20) -> dict:
    """Fetch one verified Kalshi NFL series using public GET endpoints only."""
    if not isinstance(series_ticker, str) or not re.fullmatch(r"[A-Z0-9]{2,64}", series_ticker):
        raise DataError("Kalshi series ticker must contain only uppercase letters and digits")
    if series_ticker not in KALSHI_NFL_SERIES:
        raise DataError("Kalshi series is not in the verified NFL source registry")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise DataError("Kalshi market limit must be an integer from 1 to 50")
    series_url = KALSHI_BASE + "/series/" + series_ticker
    markets_url = KALSHI_BASE + "/markets?" + urlencode(
        {"series_ticker": series_ticker, "status": "open", "limit": limit, "mve_filter": "exclude"}
    )
    series_raw, series_receipt = client.get(series_url, ttl=3600, namespace="kalshi:series")
    markets_raw, markets_receipt = client.get(markets_url, ttl=60, namespace="kalshi:markets")
    series = series_raw.get("series") if isinstance(series_raw, dict) else None
    markets = markets_raw.get("markets") if isinstance(markets_raw, dict) else None
    if not isinstance(series, dict) or series.get("ticker") != series_ticker:
        raise DataError("Kalshi series response did not match the requested ticker")
    if not isinstance(markets, list) or len(markets) > limit:
        raise DataError("Kalshi markets response exceeded the requested bound or changed shape")
    observed_at = markets_receipt.get("checked_at")
    records, skipped = [], []
    for index, market in enumerate(markets):
        if not isinstance(market, dict) or not isinstance(market.get("ticker"), str):
            raise DataError("Kalshi market response contains an invalid row")
        # A zero ask means no executable two-sided quote at this observation.
        try:
            yes_ask = float(market.get("yes_ask_dollars"))
            no_ask = float(market.get("no_ask_dollars"))
        except (TypeError, ValueError):
            raise DataError("Kalshi market ask changed shape") from None
        if not 0 < yes_ask <= 1 or not 0 < no_ask <= 1:
            skipped.append({"market_id": market["ticker"], "reason": "missing_two_sided_executable_asks"})
            continue
        record = normalize_kalshi_market(market, observed_at=observed_at, series=series)
        record["raw"] = {"container": "raw.markets", "index": index, "market_id": market["ticker"],
                         "series_container": "raw.series"}
        records.append(record)
    return {
        "source": {"provider": "kalshi", "series_ticker": series_ticker,
                   "scope": KALSHI_NFL_SERIES[series_ticker], "access": "public_read_only_rest"},
        "observed_at": _time(observed_at, "receipt.checked_at"),
        "expires_at": min((row["expires_at"] for row in records), default=None),
        "horizon": [row["horizon"] for row in records],
        "records": records,
        "skipped": skipped,
        "receipts": [series_receipt, markets_receipt],
        "raw": {"series": series_raw, "markets": markets_raw},
        "limits": {"requested": limit, "returned": len(markets), "normalized": len(records)},
        "scope_note": "Evidence only; no orders, authentication, purchases, projection, or trade verdict.",
    }


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise DataError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DataError(f"{label} must be a finite number") from None
    if not math.isfinite(result):
        raise DataError(f"{label} must be a finite number")
    return result


def _time(value: Any, label: str) -> float:
    if value is None:
        raise DataError(f"{label} must be present")
    try:
        return timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"invalid {label}: {exc}") from None


def decimal_odds(value: Any, odds_format: str) -> float:
    """Convert conventional decimal or American odds to decimal odds."""
    odds = _number(value, "odds")
    fmt = str(odds_format).lower()
    if fmt == "decimal":
        if odds <= 1:
            raise DataError("decimal odds must be greater than 1")
        return odds
    if fmt == "american":
        if -100 < odds < 100:
            raise DataError("American odds magnitude must be at least 100")
        return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))
    raise DataError("odds_format must be decimal or american")


def _required_text(record: Any, key: str, label: str) -> str:
    if not isinstance(record, dict) or not isinstance(record.get(key), str) or not record[key].strip():
        raise DataError(f"{label}.{key} must be a nonempty string")
    return record[key]


def normalize_two_sided(
    *,
    source: dict,
    event: dict,
    scope: dict,
    outcomes: list[dict],
    observed_at: Any,
    expires_at: Any,
    horizon: dict,
    raw: Any = None,
) -> dict:
    """Validate and normalize one paired quote snapshot.

    Each outcome must repeat the event, scope, and observation timestamp.  This
    makes accidental pairing across games, player props, or scrape times loud.
    """
    provider = _required_text(source, "provider", "source")
    market_id = _required_text(source, "market_id", "source")
    _required_text(source, "url", "source")
    event_id = _required_text(event, "id", "event")
    scope_id = _required_text(scope, "id", "scope")
    _required_text(scope, "kind", "scope")
    for field in ("settlement", "void", "availability"):
        if field not in scope:
            raise DataError(f"scope.{field} must be present, even when unknown")
    if not isinstance(horizon, dict) or not isinstance(horizon.get("kind"), str):
        raise DataError("horizon.kind must be present")
    if str(horizon.get("event_id")) != event_id:
        raise DataError("horizon and market must describe the same event")

    observed = _time(observed_at, "observed_at")
    expiry = _time(expires_at, "expires_at")
    if expiry < observed:
        raise DataError("expires_at precedes observed_at")
    if not isinstance(outcomes, list) or len(outcomes) != 2:
        raise DataError("a two-sided quote requires exactly two outcomes")
    normalized_outcomes: dict[str, dict] = {}
    for outcome in outcomes:
        side = _required_text(outcome, "side", "outcome").lower()
        if side in normalized_outcomes:
            raise DataError("duplicate market side")
        if str(outcome.get("event_id")) != event_id:
            raise DataError("outcome quotes do not describe the same event")
        if str(outcome.get("scope_id")) != scope_id:
            raise DataError("outcome quotes do not describe the same scope")
        if _time(outcome.get("observed_at"), "outcome.observed_at") != observed:
            raise DataError("outcome quotes were not observed at the same timestamp")
        converted = decimal_odds(outcome.get("odds"), outcome.get("odds_format"))
        normalized_outcomes[side] = {
            "side": side,
            "odds": converted,
            "odds_format": "decimal",
            "input_odds": outcome.get("odds"),
            "input_odds_format": str(outcome.get("odds_format")).lower(),
            "raw_implied_probability": 1 / converted,
        }
    if set(normalized_outcomes) not in SIDE_PAIRS:
        raise DataError("market sides must be over/under or yes/no")
    total = sum(row["raw_implied_probability"] for row in normalized_outcomes.values())
    if total <= 0:
        raise DataError("market implied-probability total must be positive")
    no_vig = {side: row["raw_implied_probability"] / total for side, row in normalized_outcomes.items()}

    return {
        "kind": "two_sided_market_evidence",
        "source": deepcopy(source),
        "observed_at": observed,
        "expires_at": expiry,
        "horizon": deepcopy(horizon),
        "event": deepcopy(event),
        "scope": deepcopy(scope),
        "outcomes": normalized_outcomes,
        "probability": {
            "method": "proportional_no_vig_approximation",
            "values": no_vig,
            "raw_implied_total": total,
            "overround": total - 1,
            "fees_included": False,
        },
        "constraints": {
            "line_is_mean": False,
            "fantasy_points_projection": None,
            "causal_player_share": None,
            "cross_source_independence_assumed": False,
        },
        "raw": deepcopy(raw),
        "identity": {"provider": provider, "market_id": market_id, "event_id": event_id, "scope_id": scope_id},
    }


def normalize_kalshi_market(
    market: dict,
    *,
    observed_at: Any,
    series: dict | None = None,
    endpoint_url: str = "https://external-api.kalshi.com/trade-api/v2",
) -> dict:
    """Normalize one public Kalshi binary market using contemporaneous asks.

    Contract prices omit fees here.  Using YES and NO asks makes the exchange
    spread explicit; their proportional normalization remains an approximation.
    """
    if not isinstance(market, dict):
        raise DataError("Kalshi market must be an object")
    ticker = _required_text(market, "ticker", "market")
    event_ticker = _required_text(market, "event_ticker", "market")
    yes_ask = _number(market.get("yes_ask_dollars"), "yes ask")
    no_ask = _number(market.get("no_ask_dollars"), "no ask")
    if not 0 < yes_ask <= 1 or not 0 < no_ask <= 1:
        raise DataError("Kalshi asks must be in (0, 1]")
    expiry = market.get("expected_expiration_time") or market.get("latest_expiration_time") or market.get("close_time")
    if expiry is None:
        raise DataError("Kalshi market expiration is missing")
    series = series if isinstance(series, dict) else {}
    if isinstance(series.get("series"), dict):
        series = series["series"]
    rules_primary = market.get("rules_primary")
    rules_secondary = market.get("rules_secondary")
    scope = {
        "id": ticker,
        "kind": "threshold" if market.get("floor_strike") is not None or market.get("cap_strike") is not None else "binary",
        "label": market.get("title"),
        "metric": series.get("title"),
        "subject": market.get("yes_sub_title"),
        "operator": market.get("strike_type"),
        "line": {"floor": market.get("floor_strike"), "cap": market.get("cap_strike")},
        "settlement": rules_primary,
        "void": None,
        "availability": rules_secondary,
    }
    source = {
        "provider": "kalshi",
        "market_id": ticker,
        "url": endpoint_url.rstrip("/") + "/markets/" + quote(ticker, safe=""),
        "terms_url": series.get("contract_terms_url"),
        "settlement_sources": deepcopy(series.get("settlement_sources")),
        "licensing": "not established by the public response; review provider terms before redistribution",
    }
    outcome_base = {"event_id": event_ticker, "scope_id": ticker, "observed_at": observed_at, "odds_format": "decimal"}
    return normalize_two_sided(
        source=source,
        event={"id": event_ticker, "label": market.get("title")},
        scope=scope,
        outcomes=[
            {**outcome_base, "side": "yes", "odds": 1 / yes_ask},
            {**outcome_base, "side": "no", "odds": 1 / no_ask},
        ],
        observed_at=observed_at,
        expires_at=expiry,
        horizon={"kind": "single_event", "event_id": event_ticker,
                 "starts_at": market.get("occurrence_datetime"), "ends_at": expiry},
        raw={"market": market, "series": series},
    )


def evidence_bundle(records: list[dict], *, requested_scope_ids: list[Any], as_of: Any) -> dict:
    """Return usable evidence without pooling books, lines, or event contracts."""
    cutoff = _time(as_of, "as_of")
    if not isinstance(records, list):
        raise DataError("records must be a list")
    usable = []
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != "two_sided_market_evidence":
            raise DataError("invalid market evidence record")
        for field in ("source", "observed_at", "expires_at", "horizon"):
            if field not in record:
                raise DataError(f"market evidence is missing {field}")
        observed = _time(record["observed_at"], "record.observed_at")
        expires = _time(record["expires_at"], "record.expires_at")
        if observed <= cutoff <= expires:
            usable.append(deepcopy(record))
    requested = [str(scope_id) for scope_id in requested_scope_ids]
    found = {row["identity"]["scope_id"] for row in usable}
    return {
        "kind": "market_evidence_bundle",
        "source": [{"provider": row["source"]["provider"], "market_id": row["source"]["market_id"]} for row in usable],
        "observed_at": cutoff,
        "expires_at": min((row["expires_at"] for row in usable), default=None),
        "horizon": [row["horizon"] for row in usable],
        "evidence": usable,
        "requested_scope_ids": requested,
        "missing_scope_ids": [scope_id for scope_id in requested if scope_id not in found],
        "combined_probability": None,
        "fantasy_points_projection": None,
        "dependence": "unknown; sources, alternate lines, and same-event markets are not treated as independent",
    }
