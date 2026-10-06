"""Documented Sleeper GET endpoints only. No credentials are required or sent."""
import json
import hashlib
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from .store import digest

BASE = "https://api.sleeper.app/v1/"


class DataError(RuntimeError):
    pass


def contract(path):
    if re.fullmatch(r"state/nfl|user/[A-Za-z0-9_.-]+|league/\d+|draft/\d+|players/nfl", path):
        return dict
    if re.fullmatch(r"user/[A-Za-z0-9_.-]+/(leagues|drafts)/nfl/\d{4}|league/\d+/(rosters|users|drafts|traded_picks|winners_bracket|losers_bracket)|league/\d+/(matchups|transactions)/\d{1,2}|draft/\d+/(picks|traded_picks)|players/nfl/trending/(add|drop)\?lookback_hours=24&limit=25", path):
        return list
    raise DataError(f"Unsupported public endpoint: {path}")


def validate(path, data):
    if not isinstance(data, contract(path)):
        raise DataError(f"Missing resource or response shape changed: {path}")
    required = {}
    if re.fullmatch(r"league/\d+", path):
        required = {"league_id": str, "season": str, "settings": dict, "scoring_settings": dict, "roster_positions": list}
    elif re.fullmatch(r"draft/\d+", path):
        required = {"draft_id": str, "settings": dict, "status": str}
    elif path == "state/nfl":
        required = {"season": str}
    elif re.fullmatch(r"user/[A-Za-z0-9_.-]+", path):
        required = {"user_id": str}
    for key, kind in required.items():
        if not isinstance(data.get(key), kind):
            raise DataError(f"Response shape changed: {path}.{key}")
    if isinstance(data, list) and any(not isinstance(x, dict) for x in data):
        raise DataError(f"Expected object list: {path}")
    if path.endswith('/rosters'):
        if any(not isinstance(r.get('roster_id'), int) for r in data):
            raise DataError(f"Missing roster identity: {path}")


class Client:
    def __init__(self, store, *, offline=False, force=False, transport=None, draft_picks_cache_probe=False):
        self.store, self.offline, self.force = store, offline, force
        self.transport = transport or self._http
        self.evidence = {}
        self.response_metadata = {}
        self.draft_picks_cache_probe = draft_picks_cache_probe
        self.lock = threading.Lock()
        self.next_request = 0.0

    def _http(self, path):
        for attempt in range(4):
            with self.lock:
                delay = max(0, self.next_request - time.monotonic())
                self.next_request = max(self.next_request, time.monotonic()) + 0.1
            if delay:
                time.sleep(delay)
            try:
                url = BASE + path
                if self.draft_picks_cache_probe and re.fullmatch(r'draft/\d+/picks', path):
                    # Public ignored-query behavior verified on the rehearsal mock only.
                    # Opt-in one-shot probe; a cache MISS is not proof of current board state.
                    url += '?draft_room_observed_at=' + str(time.time_ns())
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "moneyball-personal/0.1"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read()
                    allowed = ('Age', 'Cache-Control', 'CF-Cache-Status', 'Date', 'ETag', 'Last-Modified', 'Via')
                    self.response_metadata[path] = {
                        'status': response.status, 'url': response.url,
                        'headers': {k.lower(): response.headers[k] for k in allowed if response.headers.get(k) is not None},
                        'wire_body_sha256': hashlib.sha256(raw).hexdigest(), 'wire_body_bytes': len(raw),
                        'interpretation': 'Network response time is not origin-state freshness; intermediary caches can serve stale draft state',
                    }
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise DataError(f"Sleeper HTTP {exc.code}: {path}") from None
                retry = exc.headers.get("Retry-After", "")
                try:
                    wait = float(retry)
                except ValueError:
                    try:
                        wait = parsedate_to_datetime(retry).timestamp() - time.time()
                    except (ValueError, TypeError):
                        wait = 2 ** attempt
                if wait > 60:
                    raise DataError(f"Sleeper asks for a {wait:.0f}s pause; retry later") from None
                time.sleep(max(0, wait))
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise DataError(f"Sleeper network unavailable: {path}") from None
                time.sleep(2 ** attempt)
            except (ValueError, UnicodeError):
                raise DataError(f"Sleeper returned invalid JSON: {path}") from None

    def get(self, path, ttl=60):
        contract(path)
        cached = self.store.cached(path)
        # Players are deliberately refreshed no more than daily, even with --fresh.
        force = self.force and path != "players/nfl"
        hit = cached is not None and (self.offline or (not force and time.time() - cached["fetched"] < ttl))
        if hit:
            entry = cached
            if digest(entry["data"]) != entry["hash"]:
                raise DataError(f"Cached response hash mismatch: {path}")
        else:
            if self.offline:
                raise DataError(f"Not cached for offline use: {path}")
            value = self.transport(path)
            validate(path, value)
            entry = self.store.cache(path, value)
        validate(path, entry["data"])
        self.evidence[path] = {"url": BASE + path, "fetched_at": entry["fetched"], "age_seconds": round(time.time() - entry["fetched"], 3), "cache_hit": hit, "sha256": entry["hash"]}
        if not hit and path in self.response_metadata:
            self.evidence[path]['http'] = dict(self.response_metadata[path])
        return entry["data"]

    def many(self, paths):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda item: self.get(item[0], item[1]), paths.items()))
        return dict(zip(paths, results))


def league_week(league, state):
    # In January the NFL season still belongs to the previous calendar year.
    # league_season can roll early; never use it to change a configured season.
    if str(state.get("season")) == league["season"] and state.get("season_type") == "regular":
        candidate = state.get("leg") or state.get("week")
    else:
        candidate = league["settings"].get("leg") or league["settings"].get("start_week") or 1
    return max(1, min(18, int(candidate or 1)))


def sync(client, config, alias, *, full=False, week=None):
    start = time.time()
    lid = config["league_id"]
    prefix = f"league/{lid}"
    league = client.get(prefix, 300)
    if league["league_id"] != lid or league["season"] != config["season"]:
        raise DataError("League identity/season changed; explicitly configure the next season")
    state = client.get("state/nfl", 300)
    current = week or league_week(league, state)
    paths = {f"{prefix}/{part}": 60 for part in ("rosters", "users", "drafts", "traded_picks", "winners_bracket", "losers_bracket")}
    weeks = range(1, 19) if full else sorted({current, max(1, current - 1)})
    for w in weeks:
        paths[f"{prefix}/matchups/{w}"] = 60 if w >= current - 1 else 86400
        paths[f"{prefix}/transactions/{w}"] = 60 if w >= current - 1 else 86400
    raw = client.many(paths)
    rosters = raw[f"{prefix}/rosters"]
    mine = [r for r in rosters if r.get("owner_id") == config["user_id"] or config["user_id"] in (r.get("co_owners") or [])]
    if len(mine) != 1:
        raise DataError(f"Expected one owned roster; found {len(mine)}. Recheck membership.")
    draft_paths = {}
    for draft in raw[f"{prefix}/drafts"]:
        did = draft["draft_id"]
        for suffix in ("", "/picks", "/traded_picks"):
            draft_paths[f"draft/{did}{suffix}"] = 5
    drafts_raw = client.many(draft_paths)
    data = {"league": league, "state": state, "week": current, "my_roster_id": mine[0]["roster_id"],
            "rosters": rosters, "users": raw[f"{prefix}/users"], "traded_picks": raw[f"{prefix}/traded_picks"],
            "winners_bracket": raw[f"{prefix}/winners_bracket"], "losers_bracket": raw[f"{prefix}/losers_bracket"],
            "drafts": [{"info": drafts_raw[f"draft/{d['draft_id']}"], "picks": drafts_raw[f"draft/{d['draft_id']}/picks"],
                        "traded_picks": drafts_raw[f"draft/{d['draft_id']}/traded_picks"]} for d in raw[f"{prefix}/drafts"]],
            "matchups": {str(w): raw[f"{prefix}/matchups/{w}"] for w in weeks},
            "transactions": {str(w): raw[f"{prefix}/transactions/{w}"] for w in weeks}}
    # Keep backfilled weeks across incremental syncs.
    old = client.store.latest(alias)
    if old and old["data"]["league"]["league_id"] == lid:
        for part in ("matchups", "transactions"):
            data[part] = {**old["data"].get(part, {}), **data[part]}
    changed_sections = [k for k, v in data.items() if not old or old["data"].get(k) != v]
    receipt = {"ok": True, "alias": alias, "checked_at": time.time(), "elapsed_ms": round((time.time()-start)*1000),
               "network_requests": sum(not e["cache_hit"] for e in client.evidence.values()),
               "sources": client.evidence, "changed_sections": changed_sections, "week": current,
               "verification": "validated API identity, membership and response shapes; local SHA-256 integrity (unsigned)"}
    previous_run = client.store.last_run(alias)
    if old and previous_run and old["data"]["league"]["league_id"] == lid:
        receipt["sources"] = {**previous_run["sources"], **client.evidence}
    return client.store.record(alias, data, receipt)


def context(store, alias, config):
    snap = store.latest(alias)
    if not snap:
        raise DataError("No snapshot. Run sync first.")
    d, run = snap["data"], store.last_run(alias)
    league = d["league"]
    mine = next(r for r in d["rosters"] if r["roster_id"] == d["my_roster_id"])
    players = mine.get("players") or []
    starters, reserve, taxi = [mine.get(k) or [] for k in ("starters", "reserve", "taxi")]
    users = {u["user_id"]: u for u in d["users"]}
    draft_summaries = []
    for draft in d["drafts"]:
        info = draft["info"]
        start_time = info.get("start_time")
        draft_summaries.append({"id": info["draft_id"], "status": info["status"], "type": info.get("type"),
            "start_utc": datetime.fromtimestamp(start_time/1000, timezone.utc).isoformat() if start_time else None,
            "my_slot": (info.get("draft_order") or {}).get(config["user_id"]), "rounds": info["settings"].get("rounds"),
            "pick_timer": info["settings"].get("pick_timer"), "picks_made": len(draft["picks"]),
            "recent_picks": draft["picks"][-3:]})
    source_ages = [time.time() - e["fetched_at"] for e in run["sources"].values()]
    return {"league": league["name"].strip(), "league_id": league["league_id"], "season": league["season"],
        "status": league["status"], "week": d["week"], "checked_at": run["checked_at"],
        "checked_age_seconds": round(time.time()-run["checked_at"]), "oldest_source_age_seconds": round(max(source_ages, default=0)),
        "snapshot_hash": snap["hash"], "my_roster_id": d["my_roster_id"], "roster_positions": league["roster_positions"],
        "scoring_settings": league["scoring_settings"],
        "rules": {k: league["settings"].get(k) for k in ("type", "num_teams", "waiver_type", "waiver_budget", "waiver_clear_days", "waiver_day_of_week", "daily_waivers", "trade_deadline", "trade_review_days", "playoff_teams", "playoff_week_start", "reserve_slots", "taxi_slots", "taxi_years", "taxi_deadline", "draft_rounds")},
        "team": {"settings": mine.get("settings"), "starters": [store.player(p) for p in starters],
            "bench": [store.player(p) for p in players if p not in set(starters+reserve+taxi)],
            "reserve": [store.player(p) for p in reserve], "taxi": [store.player(p) for p in taxi]},
        "opponents": [{"roster_id": r["roster_id"], "name": (users.get(r.get("owner_id"), {}).get("metadata") or {}).get("team_name") or users.get(r.get("owner_id"), {}).get("display_name"),
            "players": len(r.get("players") or [])} for r in d["rosters"] if r["roster_id"] != d["my_roster_id"]],
        "drafts": draft_summaries, "matchups": d["matchups"].get(str(d["week"]), []),
        "recent_transactions": d["transactions"].get(str(d["week"]), [])[-5:],
        "writes": {"supported": False, "reason": "Public API is read-only; authenticated mutation capture and end-to-end validation remain pending."}}
