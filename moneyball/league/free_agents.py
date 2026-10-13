"""Bounded ESPN free-agent reads; no roster mutation or transaction claims."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .espn import PLAYER_POSITION
from .store import DataError, digest


ESPN_LEAGUE_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
POSITION_SLOTS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6}


def fetch_espn_free_agents(
    client: Any,
    config: dict,
    auth_headers: dict | None,
    *,
    scoring_period_id: int,
    positions: list[str] | tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    limit: int = 10,
    include_waiver_state: bool = True,
) -> dict:
    """Read league-specific ESPN free agents/waivers for selected positions.

    Authentication is supplied by the caller and passed directly to ``Client``;
    it is never returned or included in receipts by this function.
    """
    if not isinstance(config, dict) or config.get("platform") != "espn":
        raise DataError("ESPN league configuration is required")
    league_id, season = str(config.get("league_id", "")), config.get("season")
    if not re.fullmatch(r"\d{1,20}", league_id) or isinstance(season, bool) or not isinstance(season, int):
        raise DataError("Invalid ESPN league identity")
    if isinstance(scoring_period_id, bool) or not isinstance(scoring_period_id, int) or not 1 <= scoring_period_id <= 18:
        raise DataError("ESPN scoring period must be an integer from 1 to 18")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise DataError("ESPN free-agent limit must be an integer from 1 to 50 per position")
    if not isinstance(positions, (list, tuple)) or not positions:
        raise DataError("Request at least one ESPN position")
    normalized_positions = []
    for position in positions:
        if position not in POSITION_SLOTS:
            raise DataError("Supported ESPN free-agent positions are QB, RB, WR, and TE")
        if position not in normalized_positions:
            normalized_positions.append(position)
    if not isinstance(auth_headers, dict):
        auth_headers = {}

    url = ESPN_LEAGUE_BASE.format(season=season, league_id=league_id) + (
        "?view=kona_player_info&scoringPeriodId=" + str(scoring_period_id)
    )
    receipts, raw_by_position, players = [], {}, {}
    for position in normalized_positions:
        filters = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "filterSlotIds": {"value": [POSITION_SLOTS[position]]},
                "limit": limit,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
            }
        }
        headers = dict(auth_headers)
        headers["x-fantasy-filter"] = json.dumps(filters, sort_keys=True, separators=(",", ":"))
        raw, receipt = client.get(url, ttl=60, headers=headers,
                                  namespace=(f"espn:{league_id}:free_agents:{position}:week{scoring_period_id}:"
                                             f"filter{digest(filters)[:16]}"))
        rows = raw.get("players") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or len(rows) > limit:
            raise DataError("ESPN free-agent response exceeded the requested bound or changed shape")
        raw_by_position[position] = raw
        receipts.append({"position": position, **receipt})
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise DataError("ESPN free-agent response contains an invalid row")
            player = row.get("player")
            if not isinstance(player, dict):
                pool = row.get("playerPoolEntry")
                player = pool.get("player") if isinstance(pool, dict) else None
            if not isinstance(player, dict) or player.get("id") is None:
                raise DataError("ESPN free-agent row is missing player identity")
            pool = row.get("playerPoolEntry")
            status = row.get("status") or (pool.get("status") if isinstance(pool, dict) else None)
            if status not in ("FREEAGENT", "WAIVERS"):
                raise DataError("ESPN returned a player outside the requested acquisition states")
            player_id = str(player["id"])
            projected_points = None
            projection_row = None
            for stat in player.get("stats") or []:
                if not isinstance(stat, dict):
                    continue
                if (stat.get("seasonId") == season and stat.get("scoringPeriodId") == scoring_period_id
                        and stat.get("statSourceId") == 1 and stat.get("statSplitTypeId") == 1):
                    value = stat.get("appliedTotal")
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        continue
                    if projection_row is not None:
                        raise DataError(f"Duplicate ESPN weekly projection for free agent {player_id}")
                    projected_points, projection_row = float(value), stat
            existing = players.get(player_id)
            if existing is None:
                default_position = PLAYER_POSITION.get(
                    player.get("defaultPositionId"), f"UNKNOWN_ESPN_POSITION_{player.get('defaultPositionId')}"
                )
                existing = players[player_id] = {
                    "player_id": player_id,
                    "name": player.get("fullName") or " ".join(
                        part for part in (player.get("firstName"), player.get("lastName")) if part
                    ),
                    "positions": [default_position],
                    "matched_filters": [],
                    "eligible_slots": list(player.get("eligibleSlots") or []),
                    "injury_status": player.get("injuryStatus"),
                    "pro_team_id": player.get("proTeamId"),
                    "acquisition_status": status,
                    "acquisition_route": "provider_status_freeagent" if status == "FREEAGENT" else "provider_status_waivers",
                    "execution_actionable": None,
                    "waiver_process_at_raw": row.get("waiverProcessDate"),
                    "lineup_locked": row.get("lineupLocked"),
                    "roster_locked": row.get("rosterLocked"),
                    "trade_locked": row.get("tradeLocked"),
                    "on_team_id": row.get("onTeamId"),
                    "percent_owned": (player.get("ownership") or {}).get("percentOwned"),
                    "percent_started": (player.get("ownership") or {}).get("percentStarted"),
                    "projected_points": projected_points,
                    "projection": ({"source": "espn", "season": season,
                                    "period": {"kind": "week", "week": scoring_period_id},
                                    "points": projected_points, "conditioning": "provider_unspecified"}
                                   if projection_row is not None else None),
                    "raw_refs": [],
                }
            elif existing["acquisition_status"] != status or existing["projected_points"] != projected_points:
                raise DataError("ESPN returned conflicting free-agent rows across position filters")
            existing["matched_filters"].append(position)
            existing["raw_refs"].append({"position": position, "index": index})

    waiver_state, state_raw = None, None
    if include_waiver_state:
        state_url = ESPN_LEAGUE_BASE.format(season=season, league_id=league_id) + (
            "?view=mTeam&view=mSettings&view=mStandings&view=mMatchup"
        )
        state_raw, state_receipt = client.get(
            state_url, ttl=60, headers=dict(auth_headers), namespace=f"espn:{league_id}:waiver_state"
        )
        waiver_state = normalize_waiver_state(state_raw, config.get("own_team_id"))
        receipts.append({"component": "waiver_state", **state_receipt})
    observed_at = max((receipt.get("checked_at", 0) for receipt in receipts), default=0)
    return {
        "source": {"provider": "espn", "view": "kona_player_info", "url": url,
                   "league_id": league_id, "season": season},
        "observed_at": observed_at,
        "expires_at": None,
        "horizon": {"kind": "week", "season": season, "week": scoring_period_id},
        "players": sorted(
            players.values(),
            key=lambda row: (
                -(float(row["percent_owned"]) if isinstance(row["percent_owned"], (int, float))
                  and not isinstance(row["percent_owned"], bool) else -1.0),
                row["name"], row["player_id"],
            ),
        ),
        "receipts": receipts,
        "raw": {"players_by_position": raw_by_position, "waiver_state": state_raw},
        "waiver_state": waiver_state,
        "scope": {
            "statuses": ["FREEAGENT", "WAIVERS"], "positions": normalized_positions,
            "limit_per_position": limit,
            "claims": "League-specific status at observation; WAIVERS requires a claim.",
            "unknowns": ["competing or pending claims", "transaction locks", "drop feasibility",
                         "successful acquisition", "post-observation roster changes",
                         "whether a future scoring-period FREEAGENT status is currently actionable"],
            "period_context": "Acquisition status is provider output for the requested scoring period; it is not a current UI-action read.",
            "mutation": False,
        },
    }


def normalize_waiver_state(raw: dict, own_team_id: Any = None) -> dict:
    """Expose current provider waiver order without predicting the next reset."""
    if not isinstance(raw, dict):
        raise DataError("ESPN waiver-state response must be an object")
    settings = raw.get("settings")
    acquisition = settings.get("acquisitionSettings") if isinstance(settings, dict) else None
    teams = raw.get("teams")
    if not isinstance(acquisition, dict) or not isinstance(teams, list):
        raise DataError("ESPN waiver-state response changed shape")
    order = []
    for team in teams:
        if not isinstance(team, dict) or team.get("id") is None:
            raise DataError("ESPN waiver-state response contains an invalid team")
        rank = team.get("waiverRank")
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
            raise DataError("Invalid ESPN waiver rank")
        counter = team.get("transactionCounter") or {}
        order.append({
            "rank": rank, "team_id": str(team["id"]), "name": team.get("name"),
            "record": (team.get("record") or {}).get("overall"),
            "acquisitions": counter.get("acquisitions"),
            "acquisition_budget_spent": counter.get("acquisitionBudgetSpent"),
        })
    order.sort(key=lambda row: (row["rank"] is None, row["rank"] if row["rank"] is not None else 10**9))
    own = str(own_team_id) if own_team_id is not None else None
    own_row = next((row for row in order if row["team_id"] == own), None)
    if own is not None and own_row is None:
        raise DataError("Configured own team is missing from ESPN waiver order")
    status = raw.get("status") or {}
    reset = acquisition.get("waiverOrderReset")
    reset = reset if isinstance(reset, bool) else None
    return {
        "current_order": order,
        "own_team_id": own,
        "own_current_rank": own_row["rank"] if own_row else None,
        "reset_each_week": reset,
        "reset_basis": "inverse_standings" if reset is True else "move_to_last_after_claim" if reset is False else None,
        "acquisition_type": acquisition.get("acquisitionType"),
        "waiver_hours": acquisition.get("waiverHours"),
        "waiver_process_days": acquisition.get("waiverProcessDays"),
        "waiver_process_hour_raw": acquisition.get("waiverProcessHour"),
        "next_reset_rank": None,
        "tied_record_ordering": None,
        "claim_success_guaranteed": False,
        "pending_or_competing_claims": "not_exposed_by_this_read",
        "standings_update_at_raw": status.get("standingsUpdateDate"),
        "waiver_last_execution_at_raw": status.get("waiverLastExecutionDate"),
        "waiver_process_status_raw": status.get("waiverProcessStatus"),
        "official_policy_url": "https://support.espn.com/hc/en-us/articles/4407164936980-Waiver-Order-Overview-and-Free-Agent-Budget-Tiebreaker",
        "interpretation": "Current provider order only. A weekly reset uses inverse standings, but tied-record ordering was not verified.",
        "raw": {"acquisitionSettings": acquisition, "status": status},
    }
