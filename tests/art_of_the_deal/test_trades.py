import unittest

from art_of_the_deal.store import digest
from art_of_the_deal.trades import compare_trade


def player(name, position, **extra):
    return {"name": name, "positions": [position], "external_ids": {}, **extra}


def league(*, flex=False, superflex=False, bench_slots=3, format="redraft"):
    slots = [
        {"label": "QB", "eligible_positions": ["QB"]},
        {"label": "RB", "eligible_positions": ["RB"]},
        {"label": "WR", "eligible_positions": ["WR"]},
    ]
    if flex:
        slots.append({"label": "FLEX", "eligible_positions": ["RB", "WR", "TE"]})
    if superflex:
        slots.append({"label": "SUPER_FLEX", "eligible_positions": ["QB", "RB", "WR", "TE"]})
    scoring = {"weights": {"pass_yd": 0.04, "rush_yd": 0.1, "rec_yd": 0.1}, "unsupported": [], "nonlinear": []}
    return {
        "platform": "test",
        "format": format,
        "season": 2026,
        "week": 5,
        "rules": {
            "starters": slots,
            "bench_slots": bench_slots,
            "reserve_slots": 1,
            "taxi_slots": 0,
            "scoring": scoring,
            "best_ball": False,
        },
        "teams": {
            "a": {"id": "a", "name": "A", "player_ids": ["aq", "ar", "aw"], "starters": ["aq", "ar", "aw"], "reserve": [], "taxi": []},
            "b": {"id": "b", "name": "B", "player_ids": ["bq", "br", "bw"], "starters": ["bq", "br", "bw"], "reserve": [], "taxi": []},
        },
        "players": {
            "aq": player("AQ", "QB"), "ar": player("AR", "RB"), "aw": player("AW", "WR"),
            "bq": player("BQ", "QB"), "br": player("BR", "RB"), "bw": player("BW", "WR"),
        },
        "picks": [],
    }


def rows(league_data, values, *, source="same", available_at=10.0, kind="week", conditioning="active-adjusted"):
    period = {"kind": kind, **({"week": 5} if kind == "week" else {})}
    return [
        {
            "player_id": player_id,
            "source": source,
            "season": 2026,
            "period": period,
            "points": points,
            "scoring_hash": digest(league_data["rules"]["scoring"]),
            "available_at": available_at,
            "conditioning": conditioning,
        }
        for player_id, points in values.items()
    ]


class TradeComparisonTests(unittest.TestCase):
    def test_empty_trade_is_valid_zero_mechanical_change(self):
        data = league()
        result = compare_trade(data, {"transfers": [], "pick_transfers": [], "drops": {}})
        self.assertTrue(result["valid"])
        self.assertTrue(result["executable"])
        self.assertTrue(result["mechanical_change_zero"])
        self.assertEqual(result["asset_change_count"], 0)
        self.assertIsNone(result["result"])

    def test_swap_asset_accounting_is_symmetric(self):
        data = league()
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ],
            "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 10, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9})
        result = compare_trade(data, proposal, forecasts)
        self.assertTrue(result["valid"])
        self.assertEqual(result["teams"]["a"]["incoming_player_ids"], ["br"])
        self.assertEqual(result["teams"]["b"]["outgoing_player_ids"], ["br"])
        self.assertEqual(result["result"]["teams"]["a"]["delta_points"], -4)
        self.assertEqual(result["result"]["teams"]["b"]["delta_points"], 4)

    def test_flex_assignment_cascades_after_trade(self):
        data = league(flex=True, bench_slots=3)
        data["players"].update({"ax": player("AX", "RB"), "bx": player("BX", "WR")})
        data["teams"]["a"]["player_ids"].append("ax")
        data["teams"]["a"]["starters"].append("ax")
        data["teams"]["b"]["player_ids"].append("bx")
        data["teams"]["b"]["starters"].append("bx")
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "bx", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 10, "ar": 12, "aw": 8, "ax": 7, "bq": 10, "br": 5, "bw": 6, "bx": 11})
        result = compare_trade(data, proposal, forecasts)
        before = result["result"]["teams"]["a"]["before"]
        after = result["result"]["teams"]["a"]["after"]
        self.assertEqual(set(before["selected_player_ids"]), {"aq", "ar", "aw", "ax"})
        self.assertEqual(set(after["selected_player_ids"]), {"aq", "ax", "aw", "bx"})
        self.assertEqual(result["result"]["teams"]["a"]["delta_points"], -1)
        self.assertEqual(len(after["selected_player_ids"]), len(set(after["selected_player_ids"])))

    def test_missing_forecast_withholds_delta_and_never_zeros(self):
        data = league()
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 10, "ar": 11, "aw": 9, "bq": 10, "bw": 9})
        result = compare_trade(data, proposal, forecasts)
        comparison = result["forecast_comparisons"][0]
        self.assertEqual(comparison["status"], "incomplete")
        self.assertIn("br", comparison["missing_required_player_ids"])
        self.assertIsNone(result["result"])
        self.assertEqual(comparison["teams"], {})

    def test_explicitly_unavailable_qb_uses_backup_but_undated_out_does_not_gate(self):
        data = league(superflex=True, bench_slots=4)
        data["players"].update({"aq2": player("AQ2", "QB"), "ax": player("AX", "RB"), "bx": player("BX", "RB")})
        data["teams"]["a"]["player_ids"].extend(["aq2", "ax"])
        data["teams"]["b"]["player_ids"].append("bx")
        proposal = {
            "transfers": [
                {"player_id": "aw", "from_team": "a", "to_team": "b"},
                {"player_id": "bw", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 20, "aq2": 12, "ar": 8, "aw": 7, "ax": 5, "bq": 15, "br": 8, "bw": 6, "bx": 4})
        result = compare_trade(data, proposal, forecasts, unavailable=["aq"])
        selected = result["result"]["teams"]["a"]["before"]["selected_player_ids"]
        self.assertNotIn("aq", selected)
        self.assertIn("aq2", selected)

        data["players"]["aq"]["injury_status"] = "OUT"
        result_without_dated_status = compare_trade(data, proposal, forecasts)
        selected = result_without_dated_status["result"]["teams"]["a"]["before"]["selected_player_ids"]
        self.assertIn("aq", selected)
        self.assertTrue(any(item["code"] == "unavailable_status_not_dated_for_current_week" for item in result_without_dated_status["evidence_needed"]))

    def test_not_current_starter_removes_qb_service_only(self):
        data = league(superflex=True, bench_slots=4)
        data["players"].update({
            "aq2": {"name": "AQ2", "positions": ["QB"], "role_status": "not_current_starter", "external_ids": {}},
            "ax": player("AX", "RB"), "bx": player("BX", "RB"),
        })
        data["teams"]["a"]["player_ids"].extend(["aq2", "ax"])
        data["teams"]["b"]["player_ids"].append("bx")
        proposal = {
            "transfers": [
                {"player_id": "aw", "from_team": "a", "to_team": "b"},
                {"player_id": "bw", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 20, "aq2": 30, "ar": 8, "aw": 7, "ax": 5, "bq": 15, "br": 8, "bw": 6, "bx": 4})
        result = compare_trade(data, proposal, forecasts)
        self.assertNotIn("aq2", result["result"]["teams"]["a"]["before"]["selected_player_ids"])

    def test_dated_out_status_gates_current_week_but_not_ros(self):
        data = league()
        data["players"]["aq"]["injury_status"] = {
            "status": "OUT", "available_at": 10, "week": 5, "season": 2026,
        }
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        weekly = rows(data, {"aq": 20, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9})
        weekly_result = compare_trade(data, proposal, weekly)
        # Team A cannot fill its QB slot once the dated current-week OUT is applied.
        self.assertIsNone(weekly_result["result"])
        self.assertIn("a", weekly_result["forecast_comparisons"][0]["lineup_capacity_failures"])

        ros = rows(data, {"aq": 20, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9}, kind="rest_of_season")
        ros_result = compare_trade(data, proposal, ros)
        self.assertIn("aq", ros_result["result"]["teams"]["a"]["before"]["selected_player_ids"])

    def test_best_ball_point_means_do_not_create_false_lineup_delta(self):
        data = league()
        data["rules"]["best_ball"] = True
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 10, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9})
        result = compare_trade(data, proposal, forecasts)
        self.assertIsNone(result["result"])
        self.assertEqual(result["forecast_comparisons"][0]["status"], "incomplete")
        self.assertTrue(any(item["code"] == "best_ball_joint_outcome_distribution_required" for item in result["evidence_needed"]))

    def test_forecast_after_decision_cutoff_is_rejected(self):
        data = league()
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = rows(data, {"aq": 10, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9}, available_at=21)
        result = compare_trade(data, proposal, forecasts)
        self.assertIsNone(result["result"])
        self.assertTrue(result["forecast_rejections"])
        self.assertTrue(all(item["code"] == "forecast_after_decision_cutoff" for item in result["forecast_rejections"]))

    def test_future_pick_is_rejected_for_redraft(self):
        data = league()
        data["picks"] = [{"id": "p1", "season": 2027, "round": 1, "origin_team_id": "a", "owner_team_id": "a"}]
        proposal = {"transfers": [], "pick_transfers": [{"pick_id": "p1", "from_team": "a", "to_team": "b"}], "drops": {}}
        result = compare_trade(data, proposal)
        self.assertFalse(result["valid"])
        self.assertIn("future_pick_not_allowed_in_redraft", {item["code"] for item in result["errors"]})

    def test_two_for_one_requires_explicit_drop_when_over_capacity(self):
        data = league(bench_slots=0)
        data["players"]["ax"] = player("AX", "RB")
        data["teams"]["a"]["player_ids"].append("ax")
        # Existing roster A is already one over. Sending two and receiving one
        # cures it; receiving two for one on B creates one required drop.
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "ax", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {},
        }
        result = compare_trade(data, proposal)
        self.assertTrue(result["valid"])
        self.assertFalse(result["executable"])
        self.assertEqual(result["teams"]["b"]["capacity"]["required_drop"], 1)
        self.assertTrue(any(item["code"] == "additional_drop_required" for item in result["evidence_needed"]))

        forecasts = rows(data, {"aq": 10, "ar": 11, "aw": 9, "ax": 8, "bq": 10, "br": 7, "bw": 9})
        forecast_result = compare_trade(data, {**proposal, "decision_at": 20}, forecasts)
        self.assertIsNone(forecast_result["result"])
        self.assertEqual(forecast_result["forecast_comparisons"][0]["status"], "incomplete")

    def test_complete_components_are_scored_but_wrong_hash_points_are_not(self):
        data = league()
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        forecasts = []
        for player_id, yards in {"aq": 100, "ar": 90, "aw": 80, "bq": 100, "br": 70, "bw": 80}.items():
            forecasts.append({
                "player_id": player_id, "source": "components", "season": 2026,
                "period": {"kind": "week", "week": 5},
                "components": {"pass_yd": yards, "rush_yd": 0, "rec_yd": 0},
                "points": 999, "scoring_hash": "wrong", "available_at": 10,
                "conditioning": "unconditional",
            })
        result = compare_trade(data, proposal, forecasts)
        self.assertIsNotNone(result["result"])
        self.assertEqual(set(result["result"]["scoring_basis_by_player"].values()), {"complete_linear_components"})

    def test_providers_are_not_averaged_or_auto_selected(self):
        data = league()
        proposal = {
            "transfers": [
                {"player_id": "ar", "from_team": "a", "to_team": "b"},
                {"player_id": "br", "from_team": "b", "to_team": "a"},
            ], "pick_transfers": [], "drops": {}, "decision_at": 20,
        }
        values = {"aq": 10, "ar": 11, "aw": 9, "bq": 10, "br": 7, "bw": 9}
        forecasts = rows(data, values, source="one") + rows(data, values, source="two")
        result = compare_trade(data, proposal, forecasts)
        self.assertEqual(len(result["forecast_comparisons"]), 2)
        self.assertIsNone(result["result"])
        self.assertTrue(any(item["code"] == "multiple_complete_forecast_cohorts_no_automatic_selection" for item in result["evidence_needed"]))


if __name__ == "__main__":
    unittest.main()
