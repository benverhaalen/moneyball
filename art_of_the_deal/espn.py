"""Conservative normalization of ESPN fantasy-football league reads.

The caller owns retrieval and authentication.  This module accepts the JSON
returned by ESPN's league ``m*`` views, performs no network access, and keeps
provider-specific rules visible whenever their meaning cannot be translated
without loss.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

from .store import DataError, digest, timestamp


# lineupSlotId is a roster slot, while defaultPositionId is a player position.
SLOT_ELIGIBILITY = {
    0: ("QB", ["QB"]),
    1: ("TQB", ["QB"]),
    2: ("RB", ["RB"]),
    3: ("RB/WR", ["RB", "WR"]),
    4: ("WR", ["WR"]),
    5: ("WR/TE", ["WR", "TE"]),
    6: ("TE", ["TE"]),
    7: ("SUPER_FLEX", ["QB", "RB", "WR", "TE"]),
    8: ("DT", ["DT"]),
    9: ("DE", ["DE"]),
    10: ("LB", ["LB"]),
    11: ("DL", ["DT", "DE"]),
    12: ("CB", ["CB"]),
    13: ("S", ["S"]),
    14: ("DB", ["CB", "S"]),
    15: ("IDP_FLEX", ["DT", "DE", "LB", "CB", "S"]),
    16: ("DST", ["DST"]),
    17: ("K", ["K"]),
    18: ("P", ["P"]),
    19: ("HC", ["HC"]),
    23: ("FLEX", ["RB", "WR", "TE"]),
}
PLAYER_POSITION = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 8: "DT",
    9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB",
    15: "IDP", 16: "DST", 18: "P", 19: "HC",
}
NON_STARTER_SLOTS = {20, 21, 22, 24, 25}

# These ESPN stat IDs are simple additive quantities.  Event bands and bonuses
# are deliberately excluded because a generic aggregate projection cannot
# reproduce them without the provider's joint event/distance distribution.
LINEAR_SCORING = {
    0: "pass_att", 1: "pass_cmp", 2: "pass_inc", 3: "pass_yd",
    4: "pass_td", 19: "pass_2pt", 20: "pass_int",
    23: "rush_att", 24: "rush_yd", 25: "rush_td", 26: "rush_2pt",
    41: "rec", 42: "rec_yd", 43: "rec_td", 44: "rec_2pt",
    53: "rec", 58: "targets", 62: "two_pt", 63: "fum_rec_td",
    68: "fum", 72: "fum_lost", 73: "turnover",
    83: "fg_made", 84: "fg_att", 85: "fg_missed",
    86: "xp_made", 87: "xp_att", 88: "xp_missed",
    94: "dst_td", 95: "dst_int", 96: "dst_fum_rec",
    97: "dst_blocked_kick", 98: "dst_safety", 99: "dst_sack",
    101: "kick_return_td", 102: "punt_return_td",
    103: "int_return_td", 104: "fum_return_td", 105: "dst_st_td",
    106: "dst_forced_fum", 114: "kick_return_yd", 115: "punt_return_yd",
    205: "dst_2pt_return", 206: "dst_2pt_return",
}
NONLINEAR_SCORING_IDS = {
    *range(5, 19), *range(27, 41), 45, 46, *range(47, 53), 54, 55,
    56, 57, 74, 75, 76, 77, 78, 79, 80, 81, 82,
    89, 90, 91, 92, 120, 121, 122, 123, 124, 125,
    127, 128, 129, 130, 131, 132, 133, 134, 135, 136,
    *range(148, 155), *range(160, 175), 187, 198, 201, 202, 203,
}


def _mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise DataError(f"Missing or invalid {path}")
    return value


def _count(slots: dict, slot_id: int) -> int | None:
    key = str(slot_id)
    if key not in slots and slot_id not in slots:
        return None
    value = slots.get(key, slots.get(slot_id))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataError(f"Invalid ESPN lineup-slot count for {slot_id}")
    return value


def _format(draft: dict) -> str:
    current = draft.get("keeperCount")
    future = draft.get("keeperCountFuture")
    values = [value for value in (current, future) if value is not None]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise DataError("Invalid ESPN keeper count")
    if any(value > 0 for value in values):
        return "keeper"
    if current == 0 and future == 0:
        return "redraft"
    return "unknown"


def _scoring(raw_scoring: dict) -> dict:
    items = raw_scoring.get("scoringItems")
    if not isinstance(items, list):
        raise DataError("Missing exact ESPN scoring items")
    weights: dict[str, float] = {}
    unsupported, nonlinear = [], []
    for item in items:
        if not isinstance(item, dict):
            raise DataError("Invalid ESPN scoring item")
        try:
            stat_id = int(item["statId"])
        except (KeyError, TypeError, ValueError):
            unsupported.append(item)
            continue
        points = item.get("points")
        overrides = item.get("pointsOverrides")
        # Presence matters: {"16": 0} is a real position-specific rule.
        if isinstance(overrides, dict) and len(overrides) > 0:
            unsupported.append(item)
            continue
        if overrides is not None and not isinstance(overrides, dict):
            unsupported.append(item)
            continue
        if item.get("isReverseItem") is True:
            unsupported.append(item)
            continue
        if stat_id in NONLINEAR_SCORING_IDS:
            nonlinear.append(item)
            continue
        canonical = LINEAR_SCORING.get(stat_id)
        if canonical is None or isinstance(points, bool) or not isinstance(points, (int, float)) or not math.isfinite(points):
            unsupported.append(item)
            continue
        if canonical in weights:
            # Two provider stats sharing a label cannot be collapsed losslessly.
            unsupported.append(item)
            continue
        weights[canonical] = float(points)
    return {"weights": weights, "unsupported": unsupported, "nonlinear": nonlinear, "raw": raw_scoring}


def _playoffs(schedule: dict) -> dict:
    teams = schedule.get("playoffTeamCount")
    regular_periods = schedule.get("matchupPeriodCount")
    periods = schedule.get("matchupPeriods")
    start_week = None
    round_weeks = None
    if isinstance(regular_periods, int) and regular_periods >= 0 and isinstance(periods, dict):
        playoff_periods = []
        for key, scoring_weeks in periods.items():
            if not str(key).isdigit() or int(key) <= regular_periods or not isinstance(scoring_weeks, list):
                continue
            numeric_weeks = [week for week in scoring_weeks if isinstance(week, int) and not isinstance(week, bool)]
            if numeric_weeks:
                playoff_periods.append(min(numeric_weeks))
        round_weeks = sorted(playoff_periods)
        start_week = round_weeks[0] if round_weeks else None
    byes = None
    if isinstance(teams, int) and teams > 0:
        bracket = 1
        while bracket < teams:
            bracket *= 2
        byes = bracket - teams
    return {
        "teams": teams if isinstance(teams, int) and teams >= 0 else None,
        "start_week": start_week,
        "round_weeks": round_weeks,
        "reseed": schedule.get("playoffReseed") if isinstance(schedule.get("playoffReseed"), bool) else None,
        "byes": byes,
        "raw": schedule,
    }


def _deadline(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _side(side: Any) -> dict | None:
    if not isinstance(side, dict) or side.get("teamId") is None:
        return None
    points_by_period = side.get("pointsByScoringPeriod")
    return {
        "team_id": str(side["teamId"]),
        "points": side.get("totalPoints"),
        "projected_points": side.get("totalProjectedPointsLive", side.get("totalProjectedPoints")),
        "points_by_scoring_period": points_by_period if isinstance(points_by_period, dict) else {},
    }


def normalize(raw: dict, league_id: Any, season: int, own_team_id: Any = None) -> dict:
    """Normalize one complete ESPN league read without fetching or mutating it."""
    if not isinstance(raw, dict):
        raise DataError("ESPN raw response must be an object")
    if str(raw.get("id")) != str(league_id) or raw.get("seasonId") != int(season):
        raise DataError("ESPN league/season mismatch")
    settings = _mapping(raw.get("settings"), "ESPN settings")
    roster_rules = _mapping(settings.get("rosterSettings"), "ESPN roster settings")
    slot_counts = _mapping(roster_rules.get("lineupSlotCounts"), "ESPN lineup-slot counts")
    scoring_rules = _mapping(settings.get("scoringSettings"), "ESPN scoring settings")
    draft_rules = _mapping(settings.get("draftSettings"), "ESPN draft settings")
    schedule_rules = _mapping(settings.get("scheduleSettings"), "ESPN schedule settings")
    acquisition_rules = _mapping(settings.get("acquisitionSettings"), "ESPN acquisition settings")
    trade_rules = _mapping(settings.get("tradeSettings"), "ESPN trade settings")

    starters, unknown_slots = [], []
    for raw_slot, raw_count in sorted(slot_counts.items(), key=lambda row: int(row[0])):
        try:
            slot_id, count = int(raw_slot), int(raw_count)
        except (TypeError, ValueError):
            raise DataError("Invalid ESPN lineup-slot count") from None
        if isinstance(raw_count, bool) or count < 0:
            raise DataError(f"Invalid ESPN lineup-slot count for {raw_slot}")
        if count == 0 or slot_id in NON_STARTER_SLOTS:
            continue
        definition = SLOT_ELIGIBILITY.get(slot_id)
        if definition is None:
            definition = (f"ESPN_SLOT_{slot_id}", [f"UNKNOWN_ESPN_SLOT_{slot_id}"])
            unknown_slots.append({"slot_id": slot_id, "count": count})
        label, eligible = definition
        starters.extend({"label": label, "eligible_positions": list(eligible)} for _ in range(count))

    team_rows = raw.get("teams")
    if not isinstance(team_rows, list):
        raise DataError("Missing ESPN teams")
    teams, players, owners = {}, {}, {}
    for team in team_rows:
        if not isinstance(team, dict) or team.get("id") is None:
            raise DataError("Invalid ESPN team")
        team_id = str(team["id"])
        if team_id in teams:
            raise DataError("Duplicate ESPN team ID")
        entries = team.get("roster", {}).get("entries")
        if not isinstance(entries, list):
            raise DataError(f"Missing roster entries for ESPN team {team_id}")
        player_ids, active, reserve = [], [], []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("playerId") is None:
                raise DataError(f"Invalid roster entry for ESPN team {team_id}")
            player_id = str(entry["playerId"])
            if player_id in owners:
                raise DataError("ESPN player appears on multiple rosters")
            owners[player_id] = team_id
            player_ids.append(player_id)
            slot_id = entry.get("lineupSlotId")
            if slot_id == 21:
                reserve.append(player_id)
            elif slot_id not in NON_STARTER_SLOTS:
                active.append(player_id)
            pool = entry.get("playerPoolEntry")
            player = pool.get("player") if isinstance(pool, dict) else None
            if not isinstance(player, dict):
                raise DataError(f"Missing ESPN player identity for {player_id}")
            position_id = player.get("defaultPositionId")
            position = PLAYER_POSITION.get(position_id, f"UNKNOWN_ESPN_POSITION_{position_id}")
            stats = player.get("stats")
            if stats is None:
                stats = []
            if not isinstance(stats, list):
                raise DataError(f"Invalid ESPN stats for {player_id}")
            players[player_id] = {
                "name": player.get("fullName") or " ".join(
                    part for part in (player.get("firstName"), player.get("lastName")) if part
                ),
                "positions": [position],
                "eligible_slots": list(player.get("eligibleSlots") or []),
                "injury_status": player.get("injuryStatus"),
                "pro_team_id": player.get("proTeamId"),
                "external_ids": {"espn_id": player_id},
                "raw_stats": stats,
            }
        if len(player_ids) != len(set(player_ids)):
            raise DataError("Duplicate player within ESPN roster")
        teams[team_id] = {
            "id": team_id,
            "name": team.get("name") or team.get("location") or team.get("nickname") or f"Team {team_id}",
            "owner_ids": [str(owner) for owner in team.get("owners") or []],
            "player_ids": player_ids,
            "starters": active,
            "reserve": reserve,
            "taxi": [],
            "record": team.get("record") or {},
        }
    expected_size = settings.get("size")
    if isinstance(expected_size, int) and expected_size != len(teams):
        raise DataError("ESPN roster count does not match league size")
    if own_team_id is not None and str(own_team_id) not in teams:
        raise DataError("Configured own team is not in this ESPN league")

    schedule = []
    period_map = schedule_rules.get("matchupPeriods") or {}
    for matchup in raw.get("schedule") or []:
        if not isinstance(matchup, dict):
            raise DataError("Invalid ESPN schedule row")
        period = matchup.get("matchupPeriodId")
        scoring_periods = period_map.get(str(period), [period] if period is not None else [])
        home, away = _side(matchup.get("home")), _side(matchup.get("away"))
        sides = [side for side in (home, away) if side is not None]
        schedule.append({
            "week": min(scoring_periods) if scoring_periods else period,
            "scoring_periods": scoring_periods,
            "team_ids": [side["team_id"] for side in sides],
            "matchup_id": str(matchup["id"]) if matchup.get("id") is not None else None,
            "scores": {side["team_id"]: side["points"] for side in sides},
            "projected_scores": {side["team_id"]: side["projected_points"] for side in sides},
            "winner": matchup.get("winner"),
        })

    using_budget = acquisition_rules.get("isUsingAcquisitionBudget")
    acquisition_type = acquisition_rules.get("acquisitionType")
    waiver_type = {
        "WAIVERS_TRADITIONAL": "traditional",
        "WAIVERS_BLIND": "faab",
        "FREEAGENT_ONLY": "free_agent",
    }.get(acquisition_type, acquisition_type.lower() if isinstance(acquisition_type, str) else None)
    keeper_count, future_count = draft_rules.get("keeperCount"), draft_rules.get("keeperCountFuture")
    keeper_enabled = None
    if isinstance(keeper_count, int) and isinstance(future_count, int):
        keeper_enabled = keeper_count > 0 or future_count > 0
    waiver_hours = acquisition_rules.get("waiverHours")
    clear_days = waiver_hours / 24 if isinstance(waiver_hours, (int, float)) and not isinstance(waiver_hours, bool) else None
    revision_hours = trade_rules.get("revisionHours")
    review_days = revision_hours / 24 if isinstance(revision_hours, (int, float)) and not isinstance(revision_hours, bool) else None

    return {
        "platform": "espn", "league_id": str(league_id), "name": settings.get("name"),
        "season": int(season), "week": int(raw.get("scoringPeriodId") or 1),
        "format": _format(draft_rules), "own_team_id": str(own_team_id) if own_team_id is not None else None,
        "rules": {
            "starters": starters, "bench_slots": _count(slot_counts, 20),
            "reserve_slots": _count(slot_counts, 21), "taxi_slots": 0,
            "team_count": settings.get("size"),
            "best_ball": False if scoring_rules.get("scoringType") == "H2H_POINTS" and roster_rules.get("lineupLocktimeType") == "INDIVIDUAL_GAME" and roster_rules.get("autoPilotTypeSupported") == "NONE" else None,
            "lineup_timing_evidence": {"source":"https://support.espn.com/hc/en-us/articles/115003857552-ESPN-Fantasy-Football", "observed_fields":["scoringType=H2H_POINTS", "lineupLocktimeType=INDIVIDUAL_GAME", "autoPilotTypeSupported=NONE"], "interpretation":"Managed pre-game lineup under documented ESPN model; other configurations remain unresolved"},
            "scoring": _scoring(scoring_rules), "unknown_slots": unknown_slots,
            "position_limits": roster_rules.get("positionLimits"),
            "playoffs": _playoffs(schedule_rules),
            "trade": {"deadline_week": None, "deadline_at": _deadline(trade_rules.get("deadlineDate")),
                      "review_days": review_days, "raw": trade_rules},
            "waivers": {"type": waiver_type,
                        "budget": acquisition_rules.get("acquisitionBudget") if using_budget is True else 0.0 if using_budget is False else None,
                        "clear_days": clear_days, "raw": acquisition_rules},
            "keeper": {"enabled": keeper_enabled, "count": keeper_count, "future_count": future_count,
                       "order_type": draft_rules.get("keeperOrderType"), "raw": draft_rules},
            "raw": settings,
        },
        "teams": teams, "players": players, "picks": [], "schedule": schedule,
        "raw": {"store_kind": "espn_raw", "store_key": f"{league_id}:{int(season)}", "sha256": digest(raw)},
        "completeness": {
            "rosters": "complete for the authenticated league snapshot",
            "schedule": "provider schedule rows in the snapshot",
            "picks": "not normalized from ESPN draft-detail data",
            "pending_offers_waivers": "not established by this league snapshot",
            "current_health": "provider injury label only; no inferred playing status",
        },
    }


def projections(raw: dict, league: dict, available_at: Any) -> list[dict]:
    """Extract ESPN weekly projections, excluding actual and season-total rows."""
    season = league.get("season")
    if isinstance(season, bool) or not isinstance(season, int):
        raise DataError("Normalized ESPN league season must be an integer")
    scoring = league.get("rules", {}).get("scoring")
    if not isinstance(scoring, dict):
        raise DataError("Normalized ESPN scoring rules are missing")
    known_at = timestamp(available_at)
    scoring_hash = digest(scoring)
    rows, seen = [], set()
    for team in raw.get("teams") or []:
        for entry in team.get("roster", {}).get("entries") or []:
            player = entry.get("playerPoolEntry", {}).get("player", {})
            player_id = str(entry.get("playerId"))
            for stat in player.get("stats") or []:
                if not isinstance(stat, dict):
                    continue
                # source 1 is ESPN projection; split 1 is a weekly row.
                if stat.get("seasonId") != season or stat.get("statSourceId") != 1 or stat.get("statSplitTypeId") != 1:
                    continue
                week = stat.get("scoringPeriodId")
                points = stat.get("appliedTotal")
                if isinstance(week, bool) or not isinstance(week, int) or week < 1:
                    continue
                if isinstance(points, bool) or not isinstance(points, (int, float)) or not math.isfinite(points):
                    continue
                key = (player_id, week)
                if key in seen:
                    raise DataError(f"Duplicate ESPN weekly projection for player {player_id}, week {week}")
                seen.add(key)
                rows.append({"player_id": player_id, "source": "espn", "season": season,
                             "period": {"kind": "week", "week": week}, "points": float(points),
                             "scoring_hash": scoring_hash, "available_at": known_at,
                             "conditioning": "provider_unspecified"})
    return sorted(rows, key=lambda row: (row["period"]["week"], row["player_id"]))
