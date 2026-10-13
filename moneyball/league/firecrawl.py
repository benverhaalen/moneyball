"""Optional Alexandria feeds: bounded requests, private evidence, qualified forecasts.

Contracts are discovered at runtime. No SDK, browser session, or fantasy write access.
"""
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy

from .store import DataError, digest

API = "https://api.firecrawl.dev/v2/"
PROVIDERS = {"fftoday-com", "draftsharks-com", "cbssports-com",
             "startwho-com", "fantasyfootballcalculator-com", "fantasydata-com", "nfl-com"}
WEEKLY = "fantasy-sports-rankings/weekly_projections"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Firecrawl redirect refused; credentials stay on the API host")


class Client:
    def __init__(self, store):
        self.store = store
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, endpoint, body):
        if endpoint not in ("search", "scrape"):
            raise DataError("Unsupported Firecrawl endpoint")
        key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
        if not key:
            raise DataError("Set FIRECRAWL_API_KEY privately in the agent environment")
        request_id = str(uuid.uuid4())
        req = urllib.request.Request(API + endpoint,
            data=json.dumps(body, allow_nan=False).encode(), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json", "x-request-id": request_id})
        # A timed-out paid POST may already have executed. Never retry automatically.
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise DataError("Firecrawl response exceeds the 10 MB limit")
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise DataError(f"Firecrawl HTTP {exc.code}; check access, credits and provider terms in the dashboard. No automatic retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DataError("Firecrawl request outcome unknown; no automatic retry. Previous forecasts retained") from None
        except (ValueError, UnicodeError):
            raise DataError("Firecrawl returned invalid JSON; previous forecasts retained") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DataError("Firecrawl reported failure; inspect provider access/terms in the dashboard")
        receipt = {"gateway": "firecrawl_alexandria", "url": API + endpoint,
                   "request_id": request_id, "checked_at": time.time(),
                   "raw_sha256": self.store.blob(raw),
                   "freshness_limit": "Retrieval time does not establish provider issue time"}
        return payload.get("data"), receipt


def status(service):
    return {"configured": bool(os.environ.get("FIRECRAWL_API_KEY", "").strip()),
            "authentication_tested": False, "providers": sorted(PROVIDERS),
            "forecast_adapter": {"provider": "fftoday-com", "capability": WEEKLY},
            "setup": "Set FIRECRAWL_API_KEY in the environment inherited by the CLI/MCP server; never paste it into chat",
            "next": "Discover a full tool contract, then sync with explicit options and max_credits",
            "limits": "Other capabilities are dated evidence only. Provider terms must already be accepted by an authorized admin."}


def discover(service, query, limit=10):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise DataError("Supply a discovery query of 1..500 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise DataError("Discovery limit must be 1..20")
    data, receipt = Client(service.store).post("search", {
        "query": query, "sources": ["alexandria"], "toolDetail": "full", "limit": limit})
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        raise DataError("Firecrawl discovery contract changed; no tool execution")
    tools = [t for t in data["tools"] if isinstance(t, dict) and t.get("provider") in PROVIDERS]
    service.store.put("firecrawl_catalog", digest(query), {"tools": tools}, receipt)
    return {"tools": tools, "receipt": receipt, "execution_performed": False,
            "coverage": "Bounded search results, not an exhaustive catalogue"}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _name(value):
    return " ".join(str(value or "").casefold().split())


def _players(service, alias, league):
    players = deepcopy(league["players"])
    pool = service.store.get("pool", alias, required=False)
    if pool and pool["data"].get("league_binding") == {k: league[k] for k in ("platform", "league_id", "season")}:
        for player in pool["data"].get("players", []):
            players.setdefault(str(player["player_id"]), player)
    return players


def qualify(data, options, league, players, player_map=None, component_map=None, retrieved_at=None):
    """FFToday weekly stat components only; preset points never acquire a league hash.

    component_map maps inspected provider stat names to canonical league components.
    It is retained in provenance, not inferred from spelling or a scoring preset.
    """
    if not isinstance(data, dict) or any(data.get(k) != options.get(k) for k in ("season", "week", "position", "scoring")):
        raise DataError("FFToday response period/position/scoring differs from the explicit request; old forecasts retained")
    url = data.get("source_url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or parsed.hostname not in ("www.fftoday.com", "fftoday.com") or parsed.username or parsed.password:
        raise DataError("FFToday response lacks a valid origin URL")
    items = data.get("players")
    if not isinstance(items, list) or not 1 <= len(items) <= 25000:
        raise DataError("FFToday players must be a nonempty bounded list")
    scoring = league["rules"]["scoring"]
    weights = scoring.get("weights", {})
    mapping = component_map or {}
    if (not isinstance(mapping, dict) or len(mapping) > 100 or
        any(not isinstance(k, str) or not isinstance(v, str) or v not in weights for k, v in mapping.items()) or
        len(set(mapping.values())) != len(mapping)):
        raise DataError("component_map must uniquely map inspected stat keys to league scoring components")
    crosswalk = player_map or {}
    if not isinstance(crosswalk, dict) or len(crosswalk) > 25000:
        raise DataError("player_map must be a bounded provider-ID to league-ID object")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in crosswalk.items()):
        raise DataError("player_map keys and league IDs must be strings")
    accepted, gaps, seen = [], [], set()
    now = time.time() if retrieved_at is None else retrieved_at
    observed = data.get("observed_at_ms")
    if not _number(observed) or observed / 1000 > now + 60 or now - observed / 1000 > 86400:
        return [], [{"reason": "provider_observation_missing_stale_or_future"}]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            gaps.append({"index": index, "reason": "invalid_player_record"}); continue
        provider_id = str(item.get("player_id"))
        pid = crosswalk.get(provider_id)
        identity_basis = "explicit_crosswalk"
        if pid is None:
            matches = [p for p, record in players.items()
                       if _name(record.get("name")) == _name(item.get("name"))
                       and options["position"] in record.get("positions", [])
                       and record.get("team") and record["team"] == item.get("team")]
            pid = matches[0] if len(matches) == 1 else None
            identity_basis = "unique_exact_name_position_team"
        target = players.get(str(pid))
        if (not target or not item.get("name") or _name(target.get("name")) != _name(item["name"])
            or options["position"] not in target.get("positions", [])
            or (target.get("team") and target["team"] != item.get("team"))):
            gaps.append({"index": index, "provider_id": provider_id, "name": item.get("name"), "reason": "identity_unresolved"}); continue
        if str(pid) in seen:
            raise DataError("Duplicate matched player within FFToday cohort; old forecasts retained")
        seen.add(str(pid))
        stats = item.get("stats", {})
        components = {target_key: stats[source_key] for source_key, target_key in mapping.items()
                      if isinstance(stats, dict) and _number(stats.get(source_key))}
        needed = [k for k, v in weights.items() if _number(v) and v != 0]
        missing = sorted(set(needed) - set(components))
        if not needed or missing or scoring.get("unsupported") != [] or scoring.get("nonlinear") != []:
            gaps.append({"index": index, "player_id": str(pid), "reason": "scoring_components_incomplete",
                         "missing_components": missing}); continue
        accepted.append({"player_id": str(pid), "source": "fftoday", "season": data["season"],
            "period": {"kind": "week", "week": data["week"]}, "components": components,
            "forecast_statistic": "provider_point_estimate", "conditioning": "provider_unspecified",
            "available_at": now, "source_url": url, "provider_updated_at": data.get("last_updated"),
            "valid_until": min(now + 3600, observed / 1000 + 86400),
            "provider_observed_at_ms": data.get("observed_at_ms"), "provider_player_id": provider_id,
            "identity_basis": identity_basis,
            "freshness_limit": "Issue time/availability conditioning unverified; inspect dated evidence before action"})
    return accepted, gaps


def sync(service, alias, provider, capability, options, max_credits, player_map=None, component_map=None):
    league = service.snapshot(alias)["data"]
    if provider not in PROVIDERS or not isinstance(capability, str) or not 1 <= len(capability) <= 200:
        raise DataError("Choose an allowlisted fantasy/NFL provider and explicit capability")
    if not isinstance(options, dict) or len(options) > 30 or len(json.dumps(options)) > 10000:
        raise DataError("Supply bounded tool options")
    if not _number(max_credits) or not 0 < max_credits <= 100:
        raise DataError("Supply an explicit credit ceiling greater than zero and at most 100")
    if provider == "fftoday-com" and capability == WEEKLY:
        if (options.get("season") != league["season"] or isinstance(options.get("week"), bool)
            or not isinstance(options.get("week"), int) or not 1 <= options["week"] <= 18
            or options.get("position") not in ("QB", "RB", "WR", "TE", "K")
            or options.get("scoring") not in ("STD", "HALF", "PPR", "ESPN", "YAHOO", "FFPC", "UNDERDOG", "CBS", "NFL", "NFFC")):
            raise DataError("FFToday weekly requests require connected season, explicit week 1..18, position and scoring preset")
    catalogue = discover(service, provider + " " + capability, 20)
    matches = [t for t in catalogue["tools"] if t.get("provider") == provider and t.get("capability") == capability]
    if len(matches) != 1:
        raise DataError("Exact tool contract not found uniquely; inspect discovery without executing")
    contract = matches[0]
    price = contract.get("creditsCost")
    if not _number(price) or price < 0 or contract.get("perRecord") is not False or price > max_credits:
        raise DataError("Tool price is unknown, per-record or exceeds the credit ceiling; execution stopped")
    fields = contract.get("options")
    if not isinstance(fields, list) or any(not isinstance(f, dict) or not isinstance(f.get("name"), str) for f in fields):
        raise DataError("Tool lacks an inspectable options contract")
    allowed = {f["name"] for f in fields}
    if set(options) - allowed or any(f.get("required") and f["name"] not in options for f in fields):
        raise DataError("Options differ from the discovered contract")
    for field in fields:
        if field["name"] not in options:
            continue
        value = options[field["name"]]
        if (field.get("type") == "number" and not _number(value) or
            field.get("type") == "string" and not isinstance(value, str) or
            field.get("oneOf") and str(value) not in {str(v) for v in field["oneOf"]}):
            raise DataError("Option type or enum differs from discovered contract")
    result, receipt = Client(service.store).post("scrape", {"alexandria": {
        "provider": provider, "capability": capability, "options": options}})
    items = result.get("alexandria") if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("error") or items[0].get("success") is False:
        raise DataError("Alexandria tool failed or returned an unexpected envelope; old forecasts retained. Review provider terms/access in dashboard")
    data = items[0].get("data")
    if not isinstance(data, dict):
        raise DataError("Alexandria tool returned no object data; old forecasts retained")
    binding = {k: league[k] for k in ("platform", "league_id", "season")}
    feed_key = alias + ":" + provider + ":" + capability + ":" + digest(options)
    receipt.update({"provider": provider, "capability": capability, "options": options,
                    "contract_sha256": digest(contract), "listed_credits": price, "league_binding": binding})
    rows, gaps = [], []
    if provider == "fftoday-com" and capability == WEEKLY:
        rows, gaps = qualify(data, options, league, _players(service, alias, league), player_map, component_map, receipt["checked_at"])
    version = service.store.put("firecrawl_feed", feed_key, {
        "payload": data, "qualification_gaps": gaps, "qualified_rows": len(rows)}, receipt)
    forecast_version = None
    if rows:
        # Position and period get separate keys. Do not replace another cohort/week.
        forecast_key = f"{alias}:firecrawl:{provider}:{league['season']}:{options['week']}:{options['position']}:{options['scoring']}"
        forecast_version = service.store.put("forecasts", forecast_key, rows,
            {**receipt, "component_map": component_map or {}, "player_map": player_map or {}})
    return {"status": "forecast_acquired" if rows else "evidence_only", "feed_key": feed_key,
            "evidence_version": version, "forecast_version": forecast_version,
            "qualified_rows": len(rows), "qualification_gaps": gaps[:50], "gap_count": len(gaps),
            "receipt": receipt, "interpretation": "Challenger evidence; no measured accuracy edge. Existing forecasts retained when qualification fails."}


def _preview(value, limit, depth=0):
    if depth > 5:
        return {"preview_omitted": True}
    if isinstance(value, dict):
        result = {k: _preview(v, limit, depth + 1) for k, v in list(value.items())[:100]}
        if len(value) > 100:
            result["fields_omitted"] = len(value) - 100
        return result
    if isinstance(value, list):
        return [_preview(v, limit, depth + 1) for v in value[:limit]]
    return value[:2000] + "…" if isinstance(value, str) and len(value) > 2000 else value


def query(service, alias, limit=10, as_of=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise DataError("Feed query limit must be 1..50")
    league = service.snapshot(alias, as_of=as_of)["data"]
    binding = {k: league[k] for k in ("platform", "league_id", "season")}
    records = []
    for key in service.store.keys("firecrawl_feed", as_of):
        if not key.startswith(alias + ":"):
            continue
        record = service.store.get("firecrawl_feed", key, as_of)
        if record["provenance"].get("league_binding") == binding:
            records.append({"feed_key": key, "available_at": record["available_at"],
                            "receipt": record["provenance"], **record["data"]})
    records.sort(key=lambda r: r["available_at"], reverse=True)
    for record in records[:limit]:
        for field in ("players", "injuries", "news", "articles"):
            if isinstance(record["payload"].get(field), list):
                values = record["payload"][field]
                record["payload"][field] = values[:limit]
                record["payload"][field + "_omitted"] = max(0, len(values) - limit)
        record["qualification_gaps"] = record["qualification_gaps"][:limit]
        record["payload"] = _preview(record["payload"], limit)
        if len(json.dumps(record["payload"])) > 25000:
            record["payload"] = {"preview_omitted": True, "fields": list(record["payload"])[:100],
                                 "reason": "Preview exceeds 25 KB; full response remains private in the evidence store"}
    return {"feeds": records[:limit], "total_feeds": len(records), "as_of": as_of,
            "interpretation": "Untrusted provider content; inspect origin dates and identity before actionable reasoning"}


def qualify_saved(service, alias, feed_key, player_map=None, component_map=None):
    """Resolve inspected mappings without paying for another execution or redating evidence."""
    league = service.snapshot(alias)["data"]
    record = service.store.get("firecrawl_feed", feed_key)
    receipt = record["provenance"]
    if (not feed_key.startswith(alias + ":") or receipt.get("league_binding") !=
        {k: league[k] for k in ("platform", "league_id", "season")} or
        receipt.get("provider") != "fftoday-com" or receipt.get("capability") != WEEKLY):
        raise DataError("Saved feed is not a matching FFToday weekly league feed")
    options = receipt["options"]
    rows, gaps = qualify(record["data"]["payload"], options, league, _players(service, alias, league),
                         player_map, component_map, receipt["checked_at"])
    if rows and time.time() > min(r["valid_until"] for r in rows):
        return {"status": "evidence_only", "qualified_rows": 0, "qualification_gaps": [{"reason": "forecast_expired_refresh_required"}], "execution_performed": False}
    version = None
    if rows:
        key = f"{alias}:firecrawl:fftoday-com:{league['season']}:{options['week']}:{options['position']}:{options['scoring']}"
        version = service.store.put("forecasts", key, rows,
            {**receipt, "component_map": component_map or {}, "player_map": player_map or {},
             "qualified_from_evidence_version": record["version_id"]})
    return {"status": "forecast_acquired" if rows else "evidence_only", "qualified_rows": len(rows),
            "qualification_gaps": gaps[:50], "gap_count": len(gaps), "forecast_version": version,
            "execution_performed": False, "original_retrieved_at": receipt["checked_at"]}
