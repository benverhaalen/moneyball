"""Documented, noncommercial Sleeper reads; no undocumented projection calls."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import re
import math
from .store import DataError

BASE = "https://api.sleeper.app/v1/"
ELIGIBILITY = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"],
               "FLEX": ["RB", "WR", "TE"], "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
               "REC_FLEX": ["WR", "TE"], "WRRB_FLEX": ["WR", "RB"],
               "K": ["K"], "DEF": ["DST"], "DL": ["DL"], "LB": ["LB"], "DB": ["DB"],
               "IDP_FLEX": ["DL", "LB", "DB"]}


def numeric(value, label):
    if not re.fullmatch(r"\d+", str(value)):
        raise DataError(f"Invalid {label}")
    return str(value)


def scoring_rules(raw):
    weights, nonlinear, unsupported = {}, [], []
    for key,value in raw.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise DataError("Sleeper scoring values must be finite numbers")
        if value == 0:
            continue
        if key.startswith(("bonus_","pts_allow_","yds_allow_")):
            nonlinear.append({"key":key,"points":value,"requirement":"Expected trigger frequency/position-specific semantics, not a bonus applied to a mean"})
        else:
            weights[key] = value
    return {"weights":weights,"unsupported":unsupported,"nonlinear":nonlinear,"raw":dict(raw)}


def discover(client, username, season):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise DataError("Invalid Sleeper username or user ID")
    user, _ = client.get(BASE + "user/" + username, 86400)
    if not isinstance(user, dict) or not user.get("user_id"):
        raise DataError("Sleeper user not found")
    rows, _ = client.get(BASE + f"user/{user['user_id']}/leagues/nfl/{int(season)}", 300)
    return [{"league_id": x["league_id"], "name": x.get("name"), "season": x.get("season"), "user_id": user["user_id"]} for x in rows]


def fetch(client, config, all_weeks=True):
    lid = numeric(config["league_id"], "league ID")
    info, info_receipt = client.get(BASE + "league/" + lid, 300)
    if not isinstance(info, dict) or info.get("league_id") != lid or int(info.get("season", 0)) != int(config["season"]):
        raise DataError("Sleeper league/season mismatch")
    state, state_receipt = client.get(BASE + "state/nfl", 300)
    week = info.get("settings", {}).get("leg") or 1
    if str(state.get("season")) == str(config["season"]) and state.get("season_type") == "regular":
        week = state.get("leg") or state.get("week") or week
    week = max(1, min(18, int(week)))
    paths = {k: BASE + f"league/{lid}/{k}" for k in ("rosters", "users", "traded_picks", "winners_bracket", "losers_bracket")}
    paths["transactions"] = BASE + f"league/{lid}/transactions/{week}"
    for w in (range(1, 19) if all_weeks else [week]):
        paths[f"matchups:{w}"] = BASE + f"league/{lid}/matchups/{w}"
    def read(item):
        key, url = item
        ttl = 900 if key.startswith("matchups:") else 60
        return key, client.get(url, ttl)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = dict(pool.map(read, paths.items()))
    raw = {k: v[0] for k, v in responses.items()}
    if not all(isinstance(v, list) for v in raw.values()):
        raise DataError("Sleeper component schema changed; snapshot not published")
    catalog, catalog_receipt = client.get(BASE + "players/nfl", 86400, daily=True)
    if not isinstance(catalog, dict):
        raise DataError("Sleeper player catalog is not an object")
    raw.update(league=info, state=state, week=week)
    league = normalize(raw, catalog, config.get("own_team_id"), config.get("user_id"))
    receipts = [info_receipt, state_receipt, catalog_receipt] + [v[1] for v in responses.values()]
    return league, receipts, raw, catalog


def normalize(raw, catalog, own_team_id=None, user_id=None):
    info, rs = raw["league"], raw["league"].get("settings", {})
    slots = info.get("roster_positions")
    if not isinstance(slots, list) or not isinstance(info.get("scoring_settings"), dict):
        raise DataError("Missing exact Sleeper roster/scoring rules")
    starters, unknown_slots = [], []
    for slot in slots:
        if slot in ("BN", "IR", "TAXI"):
            continue
        eligible = ELIGIBILITY.get(slot, [])
        if not eligible:
            unknown_slots.append(slot)
        starters.append({"label": slot, "eligible_positions": eligible})
    fmt = {0: "redraft", 1: "keeper", 2: "dynasty"}.get(rs.get("type"), "unknown")
    teams, players, owners = {}, {}, {}
    users = {x["user_id"]: x for x in raw.get("users", [])}
    for team in raw["rosters"]:
        tid = str(team["roster_id"])
        if tid in teams:
            raise DataError("Duplicate Sleeper team ID")
        pids = [str(p) for p in team.get("players") or []]
        if len(pids) != len(set(pids)):
            raise DataError("Duplicate player within roster")
        owner_ids = [x for x in [team.get("owner_id"), *(team.get("co_owners") or [])] if x]
        owner = users.get(team.get("owner_id"), {})
        for pid in pids:
            if pid in owners:
                raise DataError("Player owned by multiple teams")
            owners[pid] = tid
            p = catalog.get(pid)
            if not isinstance(p, dict):
                raise DataError(f"Roster player {pid} missing from identity catalog")
            positions = ["DST" if v == "DEF" else v for v in p.get("fantasy_positions") or [p.get("position")]]
            players[pid] = {"name": p.get("full_name") or (p.get("first_name", "") + " " + p.get("last_name", "")).strip(),
                            "positions": positions, "injury_status": p.get("injury_status"), "team": p.get("team"),
                            "years_exp": p.get("years_exp"), "external_ids": {"sleeper_id": pid, "espn_id": p.get("espn_id"), "gsis_id": p.get("gsis_id")},
                            "identity_freshness": "daily catalog; current roster does not prove health or current NFL role"}
        teams[tid] = {"id": tid, "name": owner.get("metadata", {}).get("team_name") or owner.get("display_name") or f"Team {tid}",
                      "owner_ids": owner_ids, "player_ids": pids, "starters": [str(x) for x in team.get("starters") or [] if str(x) != "0"],
                      "reserve": [str(x) for x in team.get("reserve") or []], "taxi": [str(x) for x in team.get("taxi") or []], "record": team.get("settings", {})}
    if len(teams) != info.get("total_rosters", len(teams)):
        raise DataError("Roster count does not match league size")
    if user_id:
        mine = [t for t in teams if str(user_id) in [str(x) for x in teams[t]["owner_ids"]]]
        if own_team_id is not None:
            if str(own_team_id) not in mine:
                raise DataError("Selected team is not owned or co-owned by this Sleeper user")
        elif len(mine) == 1:
            own_team_id = mine[0]
        else:
            raise DataError("Could not uniquely establish league membership; specify an owned team ID")
    if own_team_id is not None and str(own_team_id) not in teams:
        raise DataError("Configured own team is not in this league")
    schedule = []
    for key, rows in raw.items():
        if not key.startswith("matchups:"):
            continue
        groups = defaultdict(list)
        for row in rows:
            if row.get("matchup_id") is not None:
                groups[row["matchup_id"]].append(row)
        for matchup, pair in groups.items():
            schedule.append({"week": int(key.split(":")[1]), "team_ids": [str(x["roster_id"]) for x in pair],
                             "matchup_id": matchup, "scores": {str(x["roster_id"]): x.get("points") for x in pair}})
    picks = []
    for p in raw.get("traded_picks", []):
        picks.append({"id": f"{p['season']}:{p['round']}:{p['roster_id']}", "season": int(p["season"]), "round": p["round"],
                      "origin_team_id": str(p["roster_id"]), "owner_team_id": str(p["owner_id"]), "provenance": "traded-pick ledger; untraded rights not enumerated"})
    scoring = dict(info["scoring_settings"])
    return {"platform": "sleeper", "league_id": info["league_id"], "season": int(info["season"]), "name": info.get("name"),
            "format": fmt, "week": raw["week"], "own_team_id": own_team_id,
            "rules": {"starters": starters, "team_count": info.get("total_rosters"), "bench_slots": slots.count("BN"), "reserve_slots": rs.get("reserve_slots"), "taxi_slots": rs.get("taxi_slots"),
                      "best_ball": bool(rs["best_ball"]) if "best_ball" in rs else None,
                      "scoring": scoring_rules(scoring),
                      "unknown_slots": unknown_slots,
                      "playoffs": {"teams": rs.get("playoff_teams"), "start_week": rs.get("playoff_week_start"), "round_weeks": None,
                                   "reseed": None, "byes": None, "raw_round_type": rs.get("playoff_round_type"), "raw_seed_type": rs.get("playoff_seed_type")},
                      "trade": {"deadline_week": rs.get("trade_deadline"), "review_days": rs.get("trade_review_days")},
                      "waivers": {"type": str(rs["waiver_type"]) if "waiver_type" in rs else None, "budget": rs.get("waiver_budget"), "clear_days": rs.get("waiver_clear_days")},
                      "keeper": {"raw_max_keepers": rs.get("max_keepers"), "format_type": rs.get("type")},
                      "taxi": {"max_years": rs.get("taxi_years"), "allow_vets": rs.get("taxi_allow_vets"), "deadline_raw": rs.get("taxi_deadline")},
                      "raw": rs},
            "teams": teams, "players": players, "picks": picks, "schedule": schedule,
            "transactions": raw.get("transactions", []), "brackets": {"winners": raw.get("winners_bracket", []), "losers": raw.get("losers_bracket", [])},
            "completeness": {"rosters": "complete", "schedule": "only observed matchup rows; absent future rows remain unknown",
                             "picks": "traded rights only; untraded rights and draft-order policy need explicit verification",
                             "pending_offers_waivers": "not exposed by documented public API", "current_health": "not certified by daily identity catalog"}}
