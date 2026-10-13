"""Bounded dated discovery leads, not verified player reports or forecasts.

Query mechanism inspected in gtonic/nfl_mcp at 5021452 (MIT); implementation
uses Moneyball's native transport. No upstream article text or fuzzy ID joins
are exposed. A response's playerId may reflect the query, not entailment.
"""
from copy import deepcopy
import math
import time
from urllib.parse import urlencode, urlparse

from .http import Client
from .store import DataError, timestamp
from .identity import qualify, assertions, BLOCKED

ENDPOINT = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"


def parse(payload, espn_id, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("feed"), list):
        raise DataError("Player-news response lacks its feed array")
    rows = []
    for item in payload["feed"]:
        if not isinstance(item, dict) or str(item.get("playerId")) != espn_id:
            raise DataError("Player-news response contains a mismatched query identity")
        if not isinstance(item.get("headline"), str) or item.get("id") is None or not str(item["id"]):
            raise DataError("Player-news item lacks its identity or headline")
        links = item.get("links") or {}
        web = links.get("web") or {} if isinstance(links, dict) else {}
        url = web.get("href") if isinstance(web, dict) else None
        parsed = urlparse(url if isinstance(url, str) else "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            url = "https://www.espn.com/nfl/player/_/id/" + espn_id
        dates, gaps = {}, []
        for field in ("published", "lastModified"):
            try:
                dates[field] = timestamp(item[field]) if item.get(field) else None
            except (DataError, TypeError, ValueError):
                dates[field] = None
            if dates[field] is None:
                gaps.append(field + "_unknown")
        rows.append({"source_item_id": str(item["id"]), "espn_id": espn_id,
            "headline": item["headline"], "source_url": url,
            "type": item.get("type"), "premium": item.get("premium"),
            "published_at": dates["published"], "modified_at": dates["lastModified"],
            "date_gaps": gaps, "period": None,
            "association": "Returned for this player query; relevance and entailment unverified"})
    return {"leads": rows[:limit], "returned_item_count": len(rows),
        "omitted_item_count": max(0, len(rows)-limit),
        "complete_history_established": False, "provider_timestamp": payload.get("timestamp")}


def publish(store, key, data):
    """An older transport observation cannot replace a newer discovery record.

    The publication epoch protects the comparison itself from concurrent writes.
    This orders collection receipts, not the truth or relevance of news claims.
    """
    for _ in range(5):
        epoch = store.version_epoch()
        if data.get("identity_binding_version_id") is not None:
            binding = store.get("player_identity_binding", key, required=False)
            if (not binding or binding["version_id"] != data["identity_binding_version_id"]
                    or binding["data"].get("qualification_token") != data.get("identity_qualification_token")
                    or binding["data"].get("status") in BLOCKED):
                raise DataError("Identity qualification changed before news publication; repeat binding")
        latest = store.get("news_leads", key, required=False)
        if (latest and latest["data"].get("espn_id") != data["espn_id"]
                and latest["data"]["last_successful_fetch_at"] > data["last_successful_fetch_at"]):
            raise DataError("Newer discovery uses a different crosswalk; repeat identity binding")
        if latest and latest["data"].get("espn_id") == data["espn_id"]:
            previous = latest["data"]
            if previous == data or previous["last_successful_fetch_at"] > data["last_successful_fetch_at"]:
                return latest
        try:
            receipt = store.put_checked("news_leads", key, data, epoch,
                {"role": "unverified_discovery", "source_url": data["source"]["url"]})
            return {**receipt, "data": data}
        except DataError:
            if store.version_epoch() == epoch:
                raise
    raise DataError("Concurrent evidence publication prevented news update; retry discovery")


def discover(service, alias, player_ids, *, refresh=False, as_of=None, limit=3, deadline=None):
    if (not isinstance(player_ids, list) or not player_ids or len(player_ids) > 12
            or any(not isinstance(p, str) or not p for p in player_ids)):
        raise DataError("News discovery needs one to twelve exact league player IDs")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 6:
        raise DataError("News lead limit must be an integer from one to six per player")
    if not isinstance(refresh, bool):
        raise DataError("News refresh must be boolean")
    if as_of is not None and refresh:
        raise DataError("Historical news discovery cannot acquire current information")
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise DataError("News deadline must be a finite monotonic timestamp")
    league_record = service.snapshot(alias, as_of=as_of)
    league = league_record["data"]
    catalog = dict(league.get("players", {}))
    pool = service.store.get("pool_index", alias, as_of, required=False) or service.store.get("pool", alias, as_of, required=False)
    if pool and all(pool["data"].get("league_binding", {}).get(k) == league.get(k)
                    for k in ("platform", "league_id", "season")):
        for player in pool["data"].get("players", []):
            if isinstance(player, dict) and isinstance(player.get("player_id"), str):
                catalog.setdefault(player["player_id"], player)
    platform = league["platform"]
    deadline = deadline if deadline is not None else time.monotonic()+10
    client = Client(service.store, force=False, deadline=deadline)
    def native_unchanged():
        current = service.snapshot(alias)
        current_pool = service.store.get("pool_index", alias, required=False) or service.store.get("pool", alias, required=False)
        return (current["sha256"] == league_record["sha256"]
                and (current_pool or {}).get("sha256") == (pool or {}).get("sha256"))
    results = []
    for pid in dict.fromkeys(player_ids):
        player = catalog.get(pid)
        key = platform+":"+pid
        prior = service.store.get("news_leads", key, as_of, required=False)
        row = {"player_ref": key, "espn_id": None, "status": "not_acquired"}
        binding = None
        if player is None or platform not in ("espn", "sleeper"):
            row.update(status="identity_unresolved", reason="Exact catalog identity required; names are not joined")
        else:
            try:
                binding = qualify(service, platform, pid, player, client, as_of=as_of,
                                  refresh=refresh, native_check=native_unchanged)
            except (DataError, OSError, TimeoutError) as exc:
                # A failed target refresh may retain a *previously qualified*
                # binding only for exactly unchanged assertions. Never renew dates.
                saved = service.store.get("player_identity_binding", key, as_of, required=False)
                inputs = assertions(service, platform, pid, player, as_of)
                if saved and saved["data"].get("inputs_hash") == inputs["inputs_hash"] and native_unchanged():
                    binding = saved
                row.update(status="refresh_failed", error_type=type(exc).__name__,
                           failure_effect="Retained identity/news keeps original dates; not current reporting")
            if binding:
                identity = binding["data"]
                eid = identity.get("espn_id")
                row.update(espn_id=eid, identity_binding=deepcopy(identity),
                           identity_binding_version_id=binding["version_id"])
                allowed = identity["status"] not in BLOCKED and binding["version_id"] is not None
                if (prior and allowed and prior["data"].get("espn_id") == eid
                        and prior["data"].get("identity_qualification_token") == identity.get("qualification_token")):
                    row.update(data=deepcopy(prior["data"]), version_id=prior["version_id"])
                    if row["status"] != "refresh_failed":
                        row["status"] = "retained"
                if not allowed and row["status"] != "refresh_failed":
                    # Before any qualification existed, historical discovery is
                    # simply not acquired. Known conflicts are explicit.
                    row.update(status="not_acquired" if as_of is not None and identity["status"] == "target_unresolved"
                               else "identity_" + identity["status"], reason="; ".join(identity["reasons"]))
                    row["next_tools"] = ["sync_pipeline(fantasy_ids)", "Inspect retained raw identity assertions"]
            else:
                allowed = False
        if binding and allowed and as_of is None and row["status"] != "refresh_failed":
            try:
                client.force = refresh
                url = ENDPOINT + "?" + urlencode({"limit": limit, "playerId": eid})
                payload, receipt = client.get(url, ttl=300, namespace="public:espn-news")
                if not native_unchanged():
                    raise DataError("Native catalog changed before news publication; repeat discovery")
                data = {**parse(payload, eid, limit), "espn_id": eid,
                    "identity_binding_version_id": binding["version_id"],
                    "identity_qualification_token": identity["qualification_token"],
                    "source": {"origin": "ESPN discovery; item authorship must be inspected",
                        "url": url, "capture_policy": "link_only", "rights": "No redistribution entitlement established"},
                    "last_successful_fetch_at": receipt["checked_at"], "transport": receipt}
                record = publish(service.store, key, data)
                row.update(status="discovered", data=deepcopy(record["data"]), version_id=record["version_id"])
            except (DataError, OSError, TimeoutError) as exc:
                row.update(status="refresh_failed", error_type=type(exc).__name__,
                           failure_effect="Retained discovery keeps its original dates; not current reporting")
                current = service.store.get("player_identity_binding", key, required=False)
                if (not current or current["data"].get("status") in BLOCKED
                        or current["data"].get("qualification_token") != identity.get("qualification_token")
                        or not native_unchanged()):
                    row.pop("data", None)
                    row.pop("version_id", None)
                    row.update(status="identity_changed", reason="Qualification changed during news request; old leads detached")
        if "data" in row:
            omitted = max(0, len(row["data"]["leads"])-limit)
            row["data"]["leads"] = row["data"]["leads"][:limit]
            row["data"]["omitted_item_count"] += omitted
        results.append(row)
    return {"players": results, "as_of": timestamp(as_of), "refresh_requested": refresh,
        "scope": "Untrusted discovery metadata, not verified reporting, historical completeness or projection evidence. Inspect originals before indexing claims; absent leads do not mean absent news."}
