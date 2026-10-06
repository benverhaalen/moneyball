import copy
import tempfile
import time
import unittest

from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError, digest
from art_of_the_deal.strategy import strategy_profile
from art_of_the_deal.trades import compare_trade


def league(*, week=1, best_ball=False, position_limits=None):
    scoring = {
        "weights": {"pass_yd": 0.04, "rush_yd": 0.1},
        "unsupported": [], "nonlinear": [],
    }
    rules = {
        "starters": [
            {"label": "QB", "eligible_positions": ["QB"]},
            {"label": "RB", "eligible_positions": ["RB"]},
        ],
        "bench_slots": 0, "reserve_slots": 0, "taxi_slots": 0,
        "best_ball": best_ball,
        "scoring": scoring,
        "position_limits": position_limits,
        "playoffs": {"teams": 4, "start_week": 15, "round_weeks": [15, 16], "reseed": False, "byes": 0},
        "trade": {"deadline_week": None, "review_days": 1.0},
        "waivers": {"type": "priority", "budget": 0.0, "clear_days": 1.0},
        "keeper": {"enabled": False},
        "raw": {},
    }
    data = {
        "platform": "test", "league_id": "L", "name": "Point in time",
        "season": 2026, "week": week, "format": "redraft", "own_team_id": "a",
        "rules": rules,
        "teams": {
            "a": {"id": "a", "name": "A", "player_ids": ["aq", "ar"], "starters": ["aq", "ar"], "reserve": [], "taxi": []},
            "b": {"id": "b", "name": "B", "player_ids": ["bq", "br"], "starters": ["bq", "br"], "reserve": [], "taxi": []},
        },
        "players": {
            "aq": {"name": "AQ", "positions": ["QB"], "external_ids": {}},
            "ar": {"name": "AR", "positions": ["RB"], "external_ids": {}},
            "bq": {"name": "BQ", "positions": ["QB"], "external_ids": {}},
            "br": {"name": "BR", "positions": ["RB"], "external_ids": {}},
        },
        "picks": [], "schedule": [], "completeness": {}, "source_receipts": [],
    }
    profile = strategy_profile(data)
    data["strategy_key"] = profile["rules_fingerprint"]
    return data, profile


def forecasts(data, values, *, week=1, available_at=None):
    available_at = time.time() if available_at is None else available_at
    return [
        {
            "player_id": player_id, "source": "platform-baseline", "season": 2026,
            "period": {"kind": "week", "week": week}, "points": points,
            "scoring_hash": digest(data["rules"]["scoring"]),
            "available_at": available_at, "conditioning": "provider_unspecified",
            "source_url": "https://example.test/projections", "provider_updated_at": None,
        }
        for player_id, points in values.items()
    ]


def proposal(**extra):
    return {
        "transfers": [
            {"player_id": "ar", "from_team": "a", "to_team": "b"},
            {"player_id": "br", "from_team": "b", "to_team": "a"},
        ],
        "pick_transfers": [], "drops": {}, **extra,
    }


class ServicePointInTimeTests(unittest.TestCase):
    def _service(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = Service(temp.name)
        data, profile = league(position_limits={})
        service.store.put("strategy", profile["rules_fingerprint"], profile)
        league_receipt = service.store.put("league", "x", data)
        observed = time.time()
        service.store.put("forecasts", "x:platform", forecasts(
            data, {"aq": 10, "ar": 8, "bq": 10, "br": 12}, available_at=observed
        ))
        return service, data, league_receipt

    def test_explicit_decision_cutoff_is_preserved_across_snapshot_forecast_and_strategy(self):
        service, data, first_receipt = self._service()
        cutoff = time.time()
        time.sleep(0.01)
        changed = copy.deepcopy(data)
        changed["name"] = "Later snapshot"
        service.store.put("league", "x", changed)
        service.store.put("forecasts", "x:platform", forecasts(
            data, {"aq": 10, "ar": 30, "bq": 10, "br": 1}, available_at=time.time()
        ))

        packet = service.trade_packet("x", proposal(decision_at=cutoff), fresh=True)
        self.assertEqual(packet["decision_at"], cutoff)
        self.assertEqual(packet["evidence_as_of"], cutoff)
        self.assertFalse(packet["refresh_performed"])
        self.assertEqual(packet["snapshot_hash"], first_receipt["sha256"])
        comparison = packet["mechanics"]["comparisons"][0]
        self.assertEqual(comparison["teams"]["a"]["delta_provider_baseline_points"], 4)
        self.assertEqual(comparison["source_urls"], ["https://example.test/projections"])
        self.assertEqual(comparison["use"].split(";")[0], "Attributed provider baseline for a legal-lineup counterfactual")

    def test_future_evaluation_week_selects_future_week_without_relabeling_league_week(self):
        service, data, _ = self._service()
        service.store.put("forecasts", "x:week2", forecasts(
            data, {"aq": 11, "ar": 7, "bq": 9, "br": 13}, week=2
        ))
        packet = service.trade_packet("x", proposal(evaluation_week=2), fresh=False)
        comparisons = packet["mechanics"]["comparisons"]
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(packet["league"]["week"], 1)
        self.assertEqual(comparisons[0]["period"], {"kind": "week", "week": 2})

    def test_invalid_or_future_decision_cutoffs_fail_loudly(self):
        service, _, _ = self._service()
        with self.assertRaises(DataError):
            service.trade_packet("x", proposal(decision_at=True), fresh=False)
        with self.assertRaises(DataError):
            service.trade_packet("x", proposal(decision_at=time.time() + 120), fresh=False)


class OperativeMechanicsTests(unittest.TestCase):
    def test_missing_scoring_schema_cannot_manufacture_zero_component_points(self):
        data, _ = league(position_limits={})
        data["rules"]["scoring"] = {}
        rows = [
            {"player_id": player_id, "source": "components", "season": 2026,
             "period": {"kind": "week", "week": 1}, "components": {},
             "available_at": 10, "conditioning": "unconditional"}
            for player_id in data["players"]
        ]
        result = compare_trade(data, proposal(decision_at=20), rows)
        self.assertIsNone(result["result"])
        self.assertTrue(result["forecast_rejections"])
        self.assertTrue(all(not row["scoring_schema_complete"] for row in result["forecast_rejections"]))

    def test_unknown_lineup_timing_blocks_an_operative_delta(self):
        data, _ = league(best_ball=None, position_limits={})
        result = compare_trade(
            data, proposal(decision_at=20),
            forecasts(data, {"aq": 10, "ar": 8, "bq": 10, "br": 12}, available_at=10),
        )
        self.assertIsNone(result["result"])
        self.assertEqual(result["forecast_comparisons"][0]["status"], "incomplete")
        self.assertIn("lineup_timing_rule_required", {row["code"] for row in result["evidence_needed"]})

    def test_observed_position_limit_violation_blocks_roster_feasibility(self):
        data, _ = league(position_limits={"RB": 1})
        data["players"]["ax"] = {"name": "AX", "positions": ["RB"], "external_ids": {}}
        data["teams"]["a"]["player_ids"].append("ax")
        data["rules"]["bench_slots"] = 1
        result = compare_trade(data, proposal())
        self.assertFalse(result["executable"])
        self.assertEqual(result["teams"]["a"]["position_capacity"]["excess"], {"RB": 1})
        self.assertIn("position_limit_exceeded", {row["code"] for row in result["evidence_needed"]})

    def test_undated_explicit_unavailability_applies_to_week_not_ros(self):
        data, _ = league(position_limits={})
        data["players"]["aq2"] = {"name": "AQ2", "positions": ["QB"], "external_ids": {}}
        data["teams"]["a"]["player_ids"].append("aq2")
        data["rules"]["bench_slots"] = 1
        values = {"aq": 20, "aq2": 5, "ar": 8, "bq": 10, "br": 12}
        weekly = forecasts(data, values, available_at=10)
        weekly_result = compare_trade(data, proposal(decision_at=20), weekly, unavailable=["aq"])
        self.assertIn("aq2", weekly_result["result"]["teams"]["a"]["before"]["selected_player_ids"])

        ros = copy.deepcopy(weekly)
        for row in ros:
            row["period"] = {"kind": "rest_of_season"}
        ros_result = compare_trade(data, proposal(decision_at=20), ros, unavailable=["aq"])
        self.assertIn("aq", ros_result["result"]["teams"]["a"]["before"]["selected_player_ids"])


if __name__ == "__main__":
    unittest.main()
