"""Connected-league draft observations shared by CLI and MCP.

Sleeper semantics: https://docs.sleeper.com/#drafts (inspected 2026-10-06).
ESPN history semantics: espn-api's BaseLeague._fetch_draft and
EspnFantasyRequests.get_league_draft (inspected 2026-10-06):
https://github.com/cwendt94/espn-api/blob/master/espn_api/base_league.py
https://github.com/cwendt94/espn-api/blob/master/espn_api/requests/espn_requests.py
No third-party implementation is copied. ESPN has no documented live draft
clock contract here. A completed GET and identical rereads cannot prove origin
freshness or prevent a pick after observation. No picks/transactions are made.
"""
import math
import re
import time

from .auth import espn_headers
from .http import Client
from .store import DataError, digest, timestamp


def _numeric(value, label):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise DataError(f"Invalid {label}")
    return str(value)


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise DataError(f"Invalid {label}")
    return value


def _identity(league):
    return {k: league.get(k) for k in ("platform", "league_id", "season", "own_team_id", "strategy_key")}


def _sleeper_info(info, league, draft_id):
    if not isinstance(info, dict) or str(info.get("draft_id")) != draft_id:
        raise DataError("Sleeper draft identity mismatch; previous board retained")
    if str(info.get("league_id")) != str(league["league_id"]) or str(info.get("season")) != str(league["season"]):
        raise DataError("Sleeper draft league/season mismatch; previous board retained")
    if info.get("sport") not in (None, "nfl") or not isinstance(info.get("settings"), dict) or not isinstance(info.get("status"), str):
        raise DataError("Invalid Sleeper draft schema; previous board retained")
    settings = info["settings"]
    if settings.get("teams") is not None and settings["teams"] != len(league["teams"]):
        raise DataError("Sleeper draft team count differs from connected league")
    order = info.get("slot_to_roster_id")
    if order is not None and (not isinstance(order, dict) or any(str(tid) not in league["teams"] for tid in order.values())):
        raise DataError("Sleeper draft order references unknown roster")
    # Messaging can change during retrieval without changing the board.
    return {k: info.get(k) for k in ("draft_id", "league_id", "season", "type", "status", "settings", "draft_order", "slot_to_roster_id", "last_picked", "start_time")}


def _sleeper(client, league, draft_id):
    from .sleeper import BASE
    receipts = []
    if draft_id is None:
        rows, receipt = client.get(BASE + f"league/{_numeric(league['league_id'], 'league ID')}/drafts", ttl=0)
        receipts.append(receipt)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DataError("Invalid Sleeper draft list")
        rows = [row for row in rows if str(row.get("league_id")) == str(league["league_id"]) and str(row.get("season")) == str(league["season"])]
        active = [row for row in rows if row.get("status") in ("drafting", "paused")]
        choices = active if active else rows
        if len(choices) != 1:
            raise DataError("Specify draft_id: this connected league has zero or multiple matching drafts")
        draft_id = choices[0].get("draft_id")
    draft_id = _numeric(draft_id, "draft ID")
    base = BASE + "draft/" + draft_id
    def read(path):
        body, receipt = client.get(base + path, ttl=0)
        receipts.append(receipt)
        return body
    first = _sleeper_info(read(""), league, draft_id)
    picks = read("/picks")
    trades = read("/traded_picks")
    last = _sleeper_info(read(""), league, draft_id)
    checked_picks = read("/picks")
    checked_trades = read("/traded_picks")
    if digest(first) != digest(last) or digest(picks) != digest(checked_picks) or digest(trades) != digest(checked_trades):
        raise DataError("Draft changed during retrieval; retry before reasoning. Previous board retained")
    if not isinstance(picks, list) or not isinstance(trades, list):
        raise DataError("Invalid Sleeper picks or traded ownership schema")
    normalized, numbers, players = [], set(), set()
    for row in picks:
        if not isinstance(row, dict) or row.get("draft_id") not in (None, draft_id):
            raise DataError("Sleeper pick identity mismatch")
        number = _positive(row.get("pick_no"), "pick number")
        pid = row.get("player_id")
        if not isinstance(pid, str) or not pid or number in numbers or pid in players:
            raise DataError("Duplicate or missing Sleeper selection identity")
        numbers.add(number); players.add(pid)
        tid = str(row["roster_id"]) if row.get("roster_id") is not None else None
        if tid is not None and tid not in league["teams"]:
            raise DataError("Sleeper pick references unknown roster")
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            raise DataError("Invalid Sleeper pick metadata")
        normalized.append({"pick_no": number, "player_id": pid, "team_id": tid,
                           "round": row.get("round"), "draft_slot": row.get("draft_slot"), "keeper": row.get("is_keeper"),
                           "name": " ".join(str(meta.get(k) or "") for k in ("first_name", "last_name")).strip() or None,
                           "position": meta.get("position")})
    for row in trades:
        if not isinstance(row, dict) or row.get("draft_id") not in (None, draft_id):
            raise DataError("Invalid Sleeper traded-pick identity")
        _positive(row.get("round"), "traded-pick round")
        for key in ("roster_id", "owner_id"):
            if str(row.get(key)) not in league["teams"]:
                raise DataError("Sleeper traded pick references unknown roster")
        if str(row.get("season")) != str(league["season"]):
            raise DataError("Sleeper traded pick season mismatch")
    normalized.sort(key=lambda row: row["pick_no"])
    consecutive = numbers == set(range(1, max(numbers, default=0) + 1))
    return {"draft_id": draft_id, "status": first["status"], "type": first["type"], "picks": normalized,
            "pick_sequence_complete": consecutive, "order": {"user_to_slot": first["draft_order"], "slot_to_team": first["slot_to_roster_id"], "traded_picks": trades},
            "settings": first["settings"], "timing": {"start_time_ms": first["start_time"], "last_picked_ms": first["last_picked"], "pick_timer_seconds": first["settings"].get("pick_timer"),
            "live_countdown_seconds": None, "interpretation": "Observed provider fields; do not derive a running clock from last_picked"},
            "support": "documented_read_only_board", "source_receipts": receipts,
            "consistency": "Board fields, picks and traded ownership matched bracketed rereads; endpoints are not atomic"}


def _espn(client, league, config, draft_id):
    if draft_id is not None:
        raise DataError("ESPN reads use the connected league/season; separate draft_id is unsupported")
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{int(league['season'])}/segments/0/"
           f"leagues/{_numeric(league['league_id'], 'league ID')}?view=mDraftDetail")
    headers = espn_headers(config.get("auth_mode", "none"))
    receipts, detail = [], None
    for _ in range(2):
        raw, receipt = client.get(url, ttl=0, headers=headers, namespace="espn:" + str(league["league_id"]))
        receipts.append(receipt)
        if not isinstance(raw, dict) or str(raw.get("id")) != str(league["league_id"]) or raw.get("seasonId") != league["season"]:
            raise DataError("ESPN draft league/season mismatch")
        current = raw.get("draftDetail")
        if not isinstance(current, dict):
            raise DataError("ESPN draft detail is unavailable; previous board retained")
        if detail is not None and digest(detail) != digest(current):
            raise DataError("ESPN draft detail changed during retrieval; previous board retained")
        detail = current
    raw_picks = detail.get("picks", [])
    if not isinstance(raw_picks, list):
        raise DataError("Invalid ESPN draft picks")
    picks, selected, slots = [], set(), set()
    for row in raw_picks:
        if not isinstance(row, dict):
            raise DataError("Invalid ESPN draft pick row")
        pid = row.get("playerId")
        # Unfilled rows are not evidence of player availability or a live order.
        if pid in (None, 0, "0"):
            continue
        # ESPN defense identities use negative IDs; league IDs do not.
        if isinstance(pid, bool) or not re.fullmatch(r"-?\d+", str(pid)):
            raise DataError("Invalid ESPN player ID")
        pid = str(pid)
        tid = str(row.get("teamId"))
        if tid not in league["teams"] or pid in selected:
            raise DataError("ESPN draft pick references unknown team or duplicate player")
        selected.add(pid)
        round_id, round_pick = row.get("roundId"), row.get("roundPickNumber")
        # Keep provider labels (including keeper/auction zero labels) intact.
        if any(type(value) is not int or value < 0 for value in (round_id, round_pick)):
            raise DataError("Invalid ESPN round/pick labels")
        slot = (round_id, round_pick)
        if slot in slots:
            raise DataError("Duplicate ESPN draft slot")
        slots.add(slot)
        picks.append({"player_id": pid, "team_id": tid, "round": round_id, "round_pick": round_pick,
                      "keeper": row.get("keeper"), "bid_amount": row.get("bidAmount")})
    picks.sort(key=lambda row: (row["round"], row["round_pick"]))
    return {"draft_id": f"espn:{league['league_id']}:{league['season']}", "status": "provider_drafted" if detail.get("drafted") is True else "not_confirmed_drafted",
            "type": None, "picks": picks, "pick_sequence_complete": None, "order": None, "settings": None,
            "timing": {"live_countdown_seconds": None, "interpretation": "Live timer, current picker and live event feed are unsupported"},
            "support": "observed_draft_detail_history_only", "source_receipts": receipts,
            "consistency": "Draft detail matched two GETs; neither implies a supported live draft stream"}


def _cache_key(league, draft_id):
    return digest({"identity": _identity(league), "requested_draft_id": draft_id})


def context(service, alias, *, fresh=True, draft_id=None, player_ids=None, expected_board_hash=None, max_age_seconds=20, as_of=None):
    """Read a bounded draft packet; failed reads never replace the saved board.

    expected_board_hash binds advice to the entire board, not merely pick count.
    An updated coherent observation is saved even when that advice guard fails.
    Cached expired observations are refused; as_of is explicitly historical.
    """
    if not isinstance(fresh, bool):
        raise DataError("fresh must be a boolean")
    if as_of is not None and fresh:
        raise DataError("Historical draft queries cannot fetch future evidence")
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 300:
        raise DataError("Draft max_age_seconds must be between 0 and 300")
    if player_ids is None:
        player_ids = []
    if not isinstance(player_ids, list) or len(player_ids) > 30 or any(not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid) for pid in player_ids) or len(set(player_ids)) != len(player_ids):
        raise DataError("Use at most 30 distinct provider player IDs")
    if draft_id is not None:
        draft_id = _numeric(draft_id, "draft ID")
    league_record = service.snapshot(alias, as_of=as_of)
    league = league_record["data"]
    config = service.store.get("config", alias, as_of)["data"]
    if any(str(config.get(k)) != str(league.get(k)) for k in ("platform", "league_id", "season")):
        raise DataError("Connected configuration and league snapshot disagree; refresh league before drafting")
    if league.get("own_team_id") not in league["teams"]:
        raise DataError("Connect your own team before requesting draft advice")
    key = _cache_key(league, draft_id)
    previous = service.store.get("draft_board", key, as_of, required=False)
    if fresh:
        client = Client(service.store, force=True)
        try:
            if league["platform"] == "sleeper":
                board = _sleeper(client, league, draft_id)
            elif league["platform"] == "espn":
                board = _espn(client, league, config, draft_id)
            else:
                raise DataError("Connected draft adapters support Sleeper and ESPN only")
            board["league_identity"] = _identity(league)
            # Receipts/timestamps do not change the structural board hash.
            board["board_hash"] = digest({k: v for k, v in board.items() if k not in ("source_receipts", "consistency")})
            board["prefix_hash"] = digest(board["picks"])
            checked = [timestamp(r["checked_at"]) for r in board["source_receipts"]]
            if not checked or max(checked) > time.time() + 60 or time.time() - min(checked) > max_age_seconds:
                raise DataError("Draft observations are expired; previous board retained")
            for source in board["source_receipts"]:
                age = source.get("headers", {}).get("age")
                if age is not None:
                    try:
                        known_age = float(age)
                    except (TypeError, ValueError):
                        raise DataError("Invalid source cache Age; previous board retained") from None
                    if not math.isfinite(known_age) or known_age < 0 or known_age > max_age_seconds:
                        raise DataError("Source cache is too old for live draft reasoning; previous board retained")
            board["oldest_observed_at"] = min(checked)
            receipt = service.store.put("draft_board", key, board, {"alias": alias, "sources": board["source_receipts"]})
            record = {**receipt, "data": board}
        except Exception as exc:
            service.store.event("draft_refresh_failed", {"alias": alias, "error": str(exc) if isinstance(exc, DataError) else type(exc).__name__})
            raise
    else:
        if previous is None:
            raise DataError("No saved draft board for these league rules; request fresh draft context")
        record, board = previous, previous["data"]
    observation_age = time.time() - board["oldest_observed_at"]
    if as_of is None and observation_age > max_age_seconds:
        raise DataError("Saved draft observation expired; request fresh draft context")
    if expected_board_hash is not None and expected_board_hash != board["board_hash"]:
        raise DataError("Draft board changed; discard advice bound to the earlier hash and request context again")
    selected = {row["player_id"]: row for row in board["picks"]}
    catalog_record = service.store.get("catalog", "sleeper", as_of, required=False) if league["platform"] == "sleeper" else None
    catalog = catalog_record["data"] if catalog_record else {}
    roster_owners = {pid: tid for tid, team in league["teams"].items() for pid in team["player_ids"]}
    candidates = []
    for pid in player_ids:
        identity = league["players"].get(pid) or catalog.get(pid) or {}
        pick = selected.get(pid)
        candidates.append({"player_id": pid, "name": identity.get("name") or identity.get("full_name"),
                           "positions": identity.get("positions") or identity.get("fantasy_positions"),
                           "observed_selection": pick, "roster_owner_at_league_snapshot": roster_owners.get(pid),
                           "availability": "observed_selected" if pick else "unknown_eligibility" if pid not in league["players"] and pid not in catalog else "not_observed_selected",
                           "interpretation": "Absence from observed picks does not certify draft eligibility, keeper rights or current availability"})
    mine = league["own_team_id"]
    return {"alias": alias, "historical": as_of is not None, "league_identity": _identity(league), "league_snapshot_hash": league_record["sha256"],
            "league_snapshot_available_at": league_record["available_at"], "league_snapshot_age_seconds": round(time.time() - league_record["available_at"], 1),
            "board_available_at": record["available_at"], "board_age_seconds": round(observation_age, 1), "board_hash": board["board_hash"], "prefix_hash": board["prefix_hash"],
            "draft_id": board["draft_id"], "status": board["status"], "type": board["type"], "support": board["support"], "pick_count": len(board["picks"]),
            "recent_picks": board["picks"][-12:], "own_selections": [row for row in board["picks"] if row["team_id"] == mine],
            "own_roster_at_league_snapshot": service._team_view(league, mine), "order": board["order"], "draft_settings": board["settings"], "timing": board["timing"],
            "pick_sequence_complete": board["pick_sequence_complete"], "candidates": candidates,
            "exact_rules": league["rules"], "strategy": service.strategy(alias, as_of=as_of), "source_receipts": board["source_receipts"], "consistency": board["consistency"],
            "limitations": ["No predictions, rankings, simulated demand, clock or current picker are invented", "Refresh league separately if rules/team identity changed; league and draft endpoints are not atomic", "Board can change immediately after observation; recheck expected_board_hash before relying on advice", "No pick or transaction execution"],
            "next_tools": [{"tool": "draft_context", "arguments": {"alias": alias, "fresh": True, "draft_id": draft_id, "expected_board_hash": board["board_hash"], "player_ids": player_ids}, "purpose": "Recheck board before using a recommendation"}, {"tool": "league_strategy", "purpose": "Use connected-league research and exact rules for conditional candidate comparison"}, {"tool": "player_evidence", "purpose": "Inspect dated role/injury evidence for the shortlist"}]}
