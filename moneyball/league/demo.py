"""Synthetic, offline walkthrough in an automatically deleted private store."""
from tempfile import TemporaryDirectory
import time

from .service import Service
from .store import digest
from .lineups import packet
from .trades import compare_trade
from .waivers import evaluate_pickup


def fixture():
    """Invented IDs, names, rules and forecasts: no personal league material."""
    players = {pid: {"name": name, "positions": [pos], "external_ids": {}}
               for pid, name, pos in [
                   ("aq", "Harbor Quarterback", "QB"), ("ar", "Harbor Runner", "RB"),
                   ("aw", "Harbor Receiver", "WR"), ("ax", "Harbor Reserve Runner", "RB"),
                   ("bq", "Summit Quarterback", "QB"), ("br", "Summit Runner", "RB"),
                   ("bw", "Summit Receiver", "WR"), ("bx", "Summit Flex Receiver", "WR") ]}
    teams = {tid: {"id": tid, "name": name, "player_ids": ids, "starters": ids[:],
                    "reserve": [], "taxi": []} for tid, name, ids in [
                        ("a", "Harbor", ["aq", "ar", "aw", "ax"]),
                        ("b", "Summit", ["bq", "br", "bw", "bx"]) ]}
    return {"platform": "synthetic", "league_id": "demo", "name": "Offline FLEX demonstration",
            "season": 2026, "week": 5, "format": "redraft", "own_team_id": "a",
            "players": players, "teams": teams, "picks": [], "schedule": [], "transactions": [],
            "completeness": {"demo": "synthetic, no live account"},
            "rules": {"starters": [{"label": p, "eligible_positions": [p]} for p in ["QB", "RB", "WR"]]
                      + [{"label": "FLEX", "eligible_positions": ["RB", "WR", "TE"]}],
                      "bench_slots": 1, "reserve_slots": 0, "taxi_slots": 0, "position_limits": {},
                      "best_ball": False,
                      "scoring": {"weights": {"rush_yd": .1, "rec_yd": .1, "pass_yd": .04}, "unsupported": [], "nonlinear": []},
                      "waivers": {"type": "faab", "budget": 100}, "playoffs": {"teams": 2, "start_week": 15},
                      "trade": {}, "keeper": {"enabled": False}, "raw": {}}}


def forecasts(data, *, available_at=None):
    observed = time.time() if available_at is None else available_at
    values = {"aq": 10, "ar": 12, "aw": 8, "ax": 7, "bq": 10, "br": 5, "bw": 6, "bx": 11, "free": 9}
    return [{"player_id": pid, "source": "invented-demo-means", "season": 2026,
             "period": {"kind": "week", "week": 5}, "points": points,
             "scoring_hash": digest(data["rules"]["scoring"]), "available_at": observed,
             "conditioning": "synthetic_all_available", "source_url": "https://example.invalid/synthetic-demo"}
            for pid, points in values.items()]


def _name_lineups(result, players):
    for comparison in result.get("forecast_comparisons", []):
        for team in comparison.get("teams", {}).values():
            for side in ("before", "after"):
                for selected in team.get(side, {}).get("selected", []):
                    pid = selected.get("player_id")
                    selected["name"] = players.get(pid, {}).get("name")
    return result


def run():
    with TemporaryDirectory(prefix="moneyball-demo-") as home:
        service = Service(home)
        data = fixture()
        rows = forecasts(data)
        service._publish("demo", data, [{"source": "invented-demo", "scope": "synthetic only"}], rows[:-1])
        cutoff = time.time()
        proposal = {"transfers": [{"player_id": "ar", "from_team": "a", "to_team": "b"},
                                  {"player_id": "bx", "from_team": "b", "to_team": "a"}],
                    "pick_transfers": [], "drops": {}, "decision_at": cutoff}
        candidate = {"player_id": "free", "name": "Available Synthetic Receiver", "positions": ["WR"],
                     "external_ids": {}, "acquisition_status": "FREEAGENT", "observed_at": cutoff,
                     "expires_at": cutoff + 60, "source_url": "https://example.invalid/synthetic-pool"}
        trade = _name_lineups(compare_trade(data, proposal, rows[:-1]), data["players"])
        pickup = evaluate_pickup(data, "a", candidate, None, rows, evaluation_week=5, decision_at=cutoff)
        named_players = {**data["players"], "free": candidate}
        for comparison in pickup["forecast_comparisons"]:
            for side in ("before", "after"):
                for selected in (comparison.get(side) or {}).get("selected", []):
                    selected["name"] = named_players.get(selected["player_id"], {}).get("name")
        return {"mode": "offline_synthetic", "network_used": False, "persistent_store_created": False,
                "interpretation": "Invented evidence demonstrates accounting, not an NFL projection or a claim of winning strategy.",
                "context": service.context("demo"), "lineup": packet(service, "demo", fresh=False),
                "trade": trade, "pickup_vs_hold": pickup,
                "reading_guide": ["Trade moves Harbor Reserve Runner into RB and Summit Flex Receiver into FLEX; Summit's named lineup also changes.",
                                  "Pickup compares the named hold roster with the conditional acquisition roster.",
                                  "Research remains pending: the host must inspect references and save an exact-rule research report."]}
