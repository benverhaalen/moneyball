import unittest

from art_of_the_deal.store import digest
from art_of_the_deal.waivers import evaluate_claim_scenarios, evaluate_pickup


SCORING = {"weights": {"fantasy_points": 1.0}, "unsupported": [], "nonlinear": []}


def league(*, bench=1, position_limits=None, waiver_type="priority"):
    return {
        "platform": "espn",
        "format": "redraft",
        "season": 2026,
        "week": 2,
        "rules": {
            "starters": [
                {"label": "RB", "eligible_positions": ["RB"]},
                {"label": "WR", "eligible_positions": ["WR"]},
                {"label": "FLEX", "eligible_positions": ["RB", "WR"]},
            ],
            "bench_slots": bench,
            "reserve_slots": 0,
            "taxi_slots": 0,
            "position_limits": position_limits if position_limits is not None else {},
            "scoring": SCORING,
            "best_ball": False,
            "waivers": {"type": waiver_type, "budget": 100 if waiver_type == "faab" else 0,
                         "clear_days": 1},
        },
        "players": {
            "rb1": {"name": "RB One", "positions": ["RB"]},
            "rb2": {"name": "RB Two", "positions": ["RB"]},
            "wr1": {"name": "WR One", "positions": ["WR"]},
            "wr2": {"name": "WR Two", "positions": ["WR"]},
        },
        "teams": {
            "a": {"id": "a", "name": "A", "player_ids": ["rb1", "rb2", "wr1", "wr2"],
                  "starters": ["rb1", "wr1", "wr2"], "reserve": [], "taxi": []},
            "b": {"id": "b", "name": "B", "player_ids": [], "starters": [], "reserve": [], "taxi": []},
        },
        "picks": [],
    }


def candidate(player_id="fa", status="WAIVERS", position="WR"):
    return {
        "player_id": player_id,
        "name": "Free Agent " + player_id,
        "positions": [position],
        "acquisition_status": status,
        "acquisition_route": "provider_status_waivers" if status == "WAIVERS" else "provider_status_freeagent",
        "observed_at": 100.0,
        "expires_at": 300.0,
        "horizon": {"kind": "week", "season": 2026, "week": 2},
        "on_team_id": None,
        "execution_actionable": None,
        "lineup_locked": False,
        "roster_locked": False,
    }


def forecasts(values, *, available_at=100.0, source="test"):
    return [{
        "player_id": player_id,
        "source": source,
        "season": 2026,
        "period": {"kind": "week", "week": 2},
        "points": value,
        "scoring_hash": digest(SCORING),
        "available_at": available_at,
        "conditioning": "status_and_opponent_at_cutoff",
        "source_url": "https://projection.example/week2",
    } for player_id, value in values.items()]


class WaiverEvaluationTests(unittest.TestCase):
    def test_full_lineup_and_flex_cascade_use_same_forecast_cohort(self):
        data = league()
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8, "fa": 14})
        result = evaluate_pickup(data, "a", candidate(), "wr2", rows,
                                 evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertTrue(result["roster_branch_feasible"])
        self.assertFalse(result["platform_transaction_completed"])
        comparison = result["result"]
        self.assertEqual(comparison["delta_points"], 4)
        self.assertEqual(comparison["before"]["selected_player_ids"], ["rb1", "wr1", "rb2"])
        self.assertEqual(comparison["after"]["selected_player_ids"], ["rb1", "fa", "wr1"])
        self.assertEqual(result["acquisition"]["execution_status"], "hypothetical_not_completed")
        self.assertIsNone(result["fantasy_title_probability_delta"])

    def test_no_drop_with_full_roster_requires_explicit_branch_and_withholds_delta(self):
        data = league()
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8, "fa": 14})
        result = evaluate_pickup(data, "a", candidate(), forecasts=rows,
                                 evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertFalse(result["roster_branch_feasible"])
        self.assertEqual(result["after"]["capacity"]["required_drop"], 1)
        self.assertIsNone(result["result"])
        self.assertIn("explicit_drop_required", {row["code"] for row in result["evidence_needed"]})

    def test_no_drop_is_valid_when_verified_ordinary_space_exists(self):
        data = league(bench=2)
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8, "fa": 14})
        result = evaluate_pickup(data, "a", candidate(), forecasts=rows,
                                 evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertTrue(result["roster_branch_feasible"])
        self.assertEqual(result["after"]["capacity"]["required_drop"], 0)
        self.assertEqual(result["result"]["delta_points"], 4)

    def test_missing_candidate_forecast_is_unknown_not_zero(self):
        data = league()
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8})
        result = evaluate_pickup(data, "a", candidate(), "wr2", rows,
                                 evaluation_week=2, decision_at=200)
        comparison = result["forecast_comparisons"][0]
        self.assertEqual(comparison["status"], "incomplete")
        self.assertIn("fa", comparison["missing_required_player_ids"])
        self.assertIsNone(comparison["delta_points"])
        self.assertIsNone(result["result"])

    def test_ownership_future_pool_status_locked_drop_and_position_limit_are_checked(self):
        data = league(position_limits={"WR": 2})
        owned = candidate("wr1")
        self.assertIn("candidate_already_owned", {
            row["code"] for row in evaluate_pickup(data, "a", owned, "wr2", decision_at=200)["errors"]
        })
        future = candidate()
        future["horizon"]["week"] = 3
        self.assertIn("candidate_pool_period_not_current", {
            row["code"] for row in evaluate_pickup(data, "a", future, "wr2", decision_at=200)["errors"]
        })
        data["players"]["wr2"]["roster_locked"] = True
        locked = evaluate_pickup(data, "a", candidate(), "wr2", decision_at=200)
        self.assertTrue(locked["valid"])
        self.assertIn("drop_player_currently_roster_locked", {
            row["code"] for row in locked["evidence_needed"]
        })
        data["players"]["wr2"]["roster_locked"] = False
        limited = evaluate_pickup(data, "a", candidate(), "rb2", decision_at=200)
        self.assertFalse(limited["roster_branch_feasible"])
        self.assertIn("position_limit_exceeded", {row["code"] for row in limited["evidence_needed"]})

    def test_current_waiver_lock_preserves_post_clearance_comparison(self):
        data = league()
        locked_candidate = candidate()
        locked_candidate["roster_locked"] = True
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8, "fa": 14})
        result = evaluate_pickup(data, "a", locked_candidate, "wr2", rows,
                                 evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertTrue(result["roster_branch_feasible"])
        self.assertEqual(result["result"]["delta_points"], 4)
        self.assertIn("candidate_currently_roster_locked", {
            row["code"] for row in result["evidence_needed"]
        })
        self.assertFalse(result["platform_transaction_completed"])

    def test_unresolved_raw_position_cap_preserves_checked_conditional_baseline(self):
        data = league(position_limits={"0": 0, "WR": 3})
        rows = forecasts({"rb1": 12, "rb2": 10, "wr1": 11, "wr2": 8, "fa": 14})
        result = evaluate_pickup(data, "a", candidate(), "wr2", rows,
                                 evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertTrue(result["roster_branch_feasible"])
        self.assertTrue(result["roster_feasibility"]["feasible_under_checked_rules"])
        self.assertFalse(result["roster_feasibility"]["position_limits_fully_translated"])
        self.assertIsNone(result["roster_feasibility"]["platform_transaction_legal"])
        self.assertEqual(result["result"]["delta_points"], 4)
        self.assertIn("position_limit_rules_unresolved", {
            row["code"] for row in result["evidence_needed"]
        })

    def test_waiver_and_free_agent_routes_are_distinct(self):
        data = league()
        waiver = evaluate_pickup(data, "a", candidate(status="WAIVERS"), "wr2", decision_at=200)
        free = evaluate_pickup(data, "a", candidate(status="FREEAGENT"), "wr2", decision_at=200)
        self.assertTrue(waiver["acquisition"]["claim_required"])
        self.assertFalse(free["acquisition"]["claim_required"])
        self.assertFalse(waiver["acquisition"]["claim_success_guaranteed"])
        self.assertFalse(free["platform_transaction_completed"])

    def test_claim_scenarios_track_order_and_cost_without_probabilities(self):
        data = league(waiver_type="faab")
        claims = [
            {"order": 1, "candidate": candidate("fa1"), "drop_player_id": "wr2",
             "faab_bid": 27, "faab_budget_before": 80},
            {"order": 2, "candidate": candidate("fa2", position="RB"), "drop_player_id": "rb2"},
        ]
        result = evaluate_claim_scenarios(data, "a", claims, evaluation_week=2, decision_at=200)
        self.assertTrue(result["valid"])
        self.assertEqual(result["declared_preference_order"], ["fa1", "fa2"])
        first = result["scenarios"][0]["conditional_on_this_target_being_acquired"]
        second = result["scenarios"][1]["conditional_on_this_target_being_acquired"]
        self.assertEqual(first["claim_cost"]["faab_budget_after_if_success"], 53)
        self.assertIsNone(result["claim_probabilities"])
        self.assertIn("conditional", result["cost_semantics"])
        self.assertFalse(first["platform_transaction_completed"])
        self.assertIn("faab_bid_unknown", {row["code"] for row in second["evidence_needed"]})

    def test_priority_is_not_inferred_from_score_or_standings(self):
        data = league(waiver_type="priority")
        data["teams"]["a"]["record"] = {"points_for": 61.9, "wins": 0, "losses": 1}
        result = evaluate_claim_scenarios(
            data, "a", [{"candidate": candidate(), "drop_player_id": "wr2"}],
            evaluation_week=2, decision_at=200,
        )
        branch = result["scenarios"][0]["conditional_on_this_target_being_acquired"]
        self.assertIn("current_priority_unknown", {row["code"] for row in branch["evidence_needed"]})
        self.assertIsNone(branch["claim_cost"]["priority_before"])


if __name__ == "__main__":
    unittest.main()
