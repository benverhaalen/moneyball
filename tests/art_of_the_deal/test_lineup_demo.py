import json
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal.demo import fixture, forecasts, run
from art_of_the_deal.lineups import packet
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError


class LineupPacketTests(unittest.TestCase):
    def setup_service(self, data=None, rows=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = Service(temp.name)
        data = fixture() if data is None else data
        rows = forecasts(data)[:-1] if rows is None else rows
        service._publish("demo", data, [], rows)
        return service

    def test_named_lineup_and_pending_strategy(self):
        result = packet(self.setup_service(), "demo", fresh=False)
        row = result["forecast_comparisons"][0]
        self.assertEqual(row["status"], "complete_counterfactual")
        self.assertEqual(row["modeled_lineup"]["total_points"], 37)
        self.assertEqual(row["modeled_lineup"]["selected"][0]["name"], "Harbor Quarterback")
        self.assertIsNone(result["recommendation"])
        self.assertTrue(any(e["code"] == "lock_state_not_verified" for e in result["evidence_needed"]))

    def test_sources_cannot_fill_each_others_gaps(self):
        data = fixture()
        rows = forecasts(data)[:-1]
        rows[0]["source"] = "other-provider"
        result = packet(self.setup_service(data, rows), "demo", fresh=False)
        self.assertEqual(len(result["forecast_comparisons"]), 2)
        self.assertTrue(all(c["modeled_lineup"] is None for c in result["forecast_comparisons"]))

    def test_future_scoring_incompatible_rows_withhold_lineup(self):
        data = fixture()
        rows = forecasts(data)[:-1]
        rows[0]["available_at"] = time.time() + 3600
        rows[1]["scoring_hash"] = "other-rules"
        result = packet(self.setup_service(data, rows), "demo", fresh=False)
        self.assertIsNone(result["forecast_comparisons"][0]["modeled_lineup"])
        self.assertEqual({e["code"] for e in result["forecast_rejections"]},
                         {"forecast_after_decision_cutoff", "forecast_scoring_incompatible"})

    def test_reserve_taxi_forecasts_not_required(self):
        data = fixture()
        data["players"]["stash"] = {"name": "Taxi Stash", "positions": ["QB"]}
        data["teams"]["a"]["player_ids"].append("stash")
        data["teams"]["a"]["taxi"].append("stash")
        result = packet(self.setup_service(data), "demo", fresh=False)
        self.assertEqual(result["forecast_comparisons"][0]["status"], "complete_counterfactual")

    def test_bestball_is_mean_proxy_not_realized_utility(self):
        data = fixture()
        data["rules"]["best_ball"] = True
        result = packet(self.setup_service(data), "demo", fresh=False)
        self.assertTrue(any(e["code"] == "best_ball_joint_distribution_required" for e in result["evidence_needed"]))
        self.assertIsNone(result["recommendation"])

    def test_team_required_and_slot_limit(self):
        data = fixture()
        data["own_team_id"] = None
        with self.assertRaisesRegex(DataError, "Select your team"):
            packet(self.setup_service(data), "demo", fresh=False)
        data = fixture()
        data["rules"]["starters"] *= 5
        with self.assertRaisesRegex(DataError, "1 to 16"):
            packet(self.setup_service(data), "demo", fresh=False)

    def test_dated_status_future_week_and_historical_queries(self):
        data = fixture()
        data["players"]["ax"]["injury_status"] = {"status": "OUT", "week": 5,
              "season": 2026, "available_at": time.time() - 1}
        service = self.setup_service(data)
        cutoff = time.time()
        with patch.object(service, "refresh", side_effect=AssertionError("historical refresh")):
            result = packet(service, "demo", fresh=True, as_of=cutoff)
        self.assertIn("ax", result["excluded_player_ids"])
        self.assertFalse(result["refresh_performed"])
        future = packet(service, "demo", evaluation_week=6, fresh=False)
        self.assertNotIn("ax", future["excluded_player_ids"])
        self.assertEqual(future["forecast_comparisons"], [])

    def test_demo_no_network_and_named_both_team_counterfactuals(self):
        with patch("art_of_the_deal.http.Client.get", side_effect=AssertionError("Network forbidden")):
            result = run()
        self.assertFalse(result["network_used"])
        self.assertFalse(result["persistent_store_created"])
        self.assertEqual(result["trade"]["result"]["teams"]["a"]["delta_points"], -1)
        self.assertEqual(result["trade"]["result"]["teams"]["b"]["delta_points"], 1)
        self.assertEqual(result["pickup_vs_hold"]["result"]["delta_points"], 2)
        self.assertTrue(all(s["name"] for s in result["trade"]["result"]["teams"]["b"]["after"]["selected"]))
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
