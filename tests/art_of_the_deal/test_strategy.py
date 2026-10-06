import copy
import json
import unittest

from art_of_the_deal.strategy import strategy_profile


def league(**updates):
    base = {
        "platform": "fixture",
        "league_id": "L1",
        "name": "Rules Matter",
        "season": 2026,
        "format": "dynasty",
        "rules": {
            "starters": [
                {"label": "QB", "eligible_positions": ["QB"]},
                {"label": "RB", "eligible_positions": ["RB"]},
                {"label": "WR", "eligible_positions": ["WR"]},
                {"label": "FLEX", "eligible_positions": ["RB", "TE", "WR"]},
                {"label": "K", "eligible_positions": ["K"]},
                {"label": "DST", "eligible_positions": ["DST"]},
            ],
            "bench_slots": 8,
            "reserve_slots": 2,
            "taxi_slots": 3,
            "best_ball": False,
            "scoring": {
                "weights": {"rec": 1.0, "pass_td": 4.0, "fgm": 3.0, "dst_sack": 1.0},
                "unsupported": [],
                "nonlinear": [],
            },
            "playoffs": {"teams": 6, "start_week": 15, "round_weeks": [15, 16, 17], "reseed": False, "byes": 2},
            "trade": {"deadline_week": 11, "review_days": 2.0},
            "waivers": {"type": "faab", "budget": 100.0, "clear_days": 2.0},
            "keeper": {"enabled": True, "cost_rule": "none"},
            "raw": {},
        },
        "teams": {
            "2": {"player_ids": ["p2"], "starters": ["p2"]},
            "1": {"player_ids": ["p1"], "starters": ["p1"]},
        },
    }
    for key, value in updates.items():
        base[key] = value
    return base


def by_id(rows):
    return {row["id"]: row for row in rows}


class StrategyProfileTests(unittest.TestCase):
    def test_is_deterministic_and_supports_generic_k_dst_slots(self):
        a = league()
        b = copy.deepcopy(a)
        b["teams"] = {key: b["teams"][key] for key in reversed(list(b["teams"]))}
        b["rules"]["scoring"]["weights"] = {
            key: b["rules"]["scoring"]["weights"][key]
            for key in reversed(list(b["rules"]["scoring"]["weights"]))
        }
        self.assertEqual(json.dumps(strategy_profile(a), sort_keys=True), json.dumps(strategy_profile(b), sort_keys=True))
        lineup = by_id(strategy_profile(a)["mechanisms"])["complete_lineup_substitution"]
        slots = lineup["rule_evidence"][0]["value"]
        self.assertIn({"label": "K", "eligible_positions": ["K"]}, slots)
        self.assertIn({"label": "DST", "eligible_positions": ["DST"]}, slots)

    def test_superflex_changes_operational_obligation_not_just_prose(self):
        ordinary = strategy_profile(league())
        changed = league()
        changed["rules"]["starters"].append(
            {"label": "SUPER_FLEX", "eligible_positions": ["QB", "RB", "WR", "TE"]}
        )
        superflex = strategy_profile(changed)
        self.assertNotIn("quarterback_optional_capacity", by_id(ordinary["mechanisms"]))
        self.assertIn("quarterback_optional_capacity", by_id(superflex["mechanisms"]))
        self.assertNotIn("measure_qb_optional_slot_replacement", by_id(ordinary["obligations"]))
        self.assertEqual(
            by_id(superflex["obligations"])["measure_qb_optional_slot_replacement"]["operation"],
            "compare_qb_and_non_qb_occupants_of_shared_slots",
        )

    def test_best_ball_setting_changes_required_lineup_operation(self):
        managed = strategy_profile(league())
        best = league()
        best["rules"]["best_ball"] = True
        best = strategy_profile(best)
        self.assertEqual(by_id(managed["obligations"])["respect_lineup_timing"]["operation"], "use_pre_outcome_lineup")
        self.assertEqual(by_id(best["obligations"])["respect_lineup_timing"]["operation"], "use_realized_best_lineup")

    def test_capacity_change_is_exact_input_to_drop_branch_operation(self):
        eight = strategy_profile(league())
        smaller = league()
        smaller["rules"]["bench_slots"] = 4
        four = strategy_profile(smaller)
        m8 = by_id(eight["mechanisms"])["post_trade_capacity"]
        m4 = by_id(four["mechanisms"])["post_trade_capacity"]
        self.assertNotEqual(m8["rule_evidence"], m4["rule_evidence"])
        self.assertIn("enumerate_capacity_branches", by_id(four["obligations"]))

    def test_nonlinear_scoring_blocks_complete_total_and_requests_joint_evidence(self):
        candidate = league()
        raw_rule = {"key": "bonus_pass_yd_300", "points": 3}
        candidate["rules"]["scoring"]["nonlinear"] = [raw_rule]
        profile = strategy_profile(candidate)
        self.assertTrue(profile["scoring_support"]["blocked"])
        self.assertFalse(profile["scoring_support"]["complete"])
        scoring = by_id(profile["mechanisms"])["exact_scoring_translation"]
        self.assertIn("joint event distribution", " ".join(scoring["evidence_needed"]))
        nonlinear_evidence = next(row for row in scoring["rule_evidence"] if row["path"] == "/rules/scoring/nonlinear")
        self.assertEqual(nonlinear_evidence["value"], [raw_rule])
        self.assertEqual(
            by_id(profile["obligations"])["gate_scoring_completeness"]["operation"],
            "block_complete_total_or_require_explicit_partial_scope",
        )

    def test_ppr_change_changes_exact_scoring_evidence(self):
        ppr = strategy_profile(league())
        zero = league()
        zero["rules"]["scoring"]["weights"]["rec"] = 0.0
        standard = strategy_profile(zero)
        ppr_evidence = by_id(ppr["mechanisms"])["exact_scoring_translation"]["rule_evidence"][0]
        standard_evidence = by_id(standard["mechanisms"])["exact_scoring_translation"]["rule_evidence"][0]
        self.assertEqual(ppr_evidence["path"], "/rules/scoring/weights")
        self.assertEqual(ppr_evidence["value"]["rec"], 1.0)
        self.assertEqual(standard_evidence["value"]["rec"], 0.0)

    def test_zero_residual_is_redraft_only_and_conflicts_are_loud(self):
        dynasty = strategy_profile(league())
        self.assertIsNone(dynasty["horizon"]["residual_seasons"])

        redraft_input = league(format="redraft")
        redraft_input["rules"]["keeper"] = {"enabled": False}
        redraft = strategy_profile(redraft_input)
        self.assertEqual(redraft["horizon"]["residual_seasons"], 0)
        self.assertEqual(by_id(redraft["obligations"])["apply_asset_horizon"]["operation"], "set_postseason_residual_to_zero")

        conflict_input = league(format="redraft")
        conflict = strategy_profile(conflict_input)
        self.assertIsNone(conflict["horizon"]["residual_seasons"])
        self.assertIn("/format_conflicts_with_/rules/keeper", conflict["missing_rules"])

    def test_keeper_unknown_surfaces_missing_rule_and_future_evidence(self):
        candidate = league(format="keeper")
        candidate["rules"]["keeper"] = {}
        profile = strategy_profile(candidate)
        self.assertIn("/rules/keeper/enabled_or_count", profile["missing_rules"])
        self.assertIsNone(profile["horizon"]["residual_seasons"])
        horizon = by_id(profile["mechanisms"])["retained_asset_horizon"]
        self.assertIn("keeper limits/costs or complete future-right ownership", horizon["evidence_needed"])
        self.assertEqual(
            by_id(profile["obligations"])["apply_asset_horizon"]["blocked_by"],
            ["/rules/keeper/enabled_or_count"],
        )

    def test_unknown_counts_remain_unknown_and_block_capacity_claim(self):
        candidate = league()
        candidate["rules"]["bench_slots"] = None
        candidate["rules"]["reserve_slots"] = None
        candidate["rules"]["taxi_slots"] = None
        profile = strategy_profile(candidate)
        self.assertIn("/rules/bench_slots", profile["missing_rules"])
        capacity = by_id(profile["mechanisms"])["post_trade_capacity"]
        self.assertFalse(capacity["active"])
        self.assertEqual([row["value"] for row in capacity["rule_evidence"]], [None, None, None])
        self.assertEqual(
            set(by_id(profile["obligations"])["enumerate_capacity_branches"]["blocked_by"]),
            {"/rules/bench_slots", "/rules/reserve_slots", "/rules/taxi_slots"},
        )

    def test_roster_only_refresh_does_not_change_cached_strategy_profile(self):
        before = league()
        after = copy.deepcopy(before)
        after["teams"]["1"]["player_ids"] = ["new-a", "new-b"]
        after["teams"]["1"]["starters"] = ["new-a"]
        after["teams"]["3"] = {"player_ids": ["p3"], "starters": ["p3"]}
        a = strategy_profile(before)
        b = strategy_profile(after)
        self.assertEqual(a["rules_fingerprint"], b["rules_fingerprint"])
        self.assertEqual(a, b)
        self.assertIn("roster-only refresh", a["cache_policy"]["do_not_recompile_for"])

    def test_configured_league_size_is_a_structural_input(self):
        a = league()
        a["rules"]["team_count"] = 12
        b = copy.deepcopy(a)
        b["rules"]["team_count"] = 16
        self.assertNotEqual(strategy_profile(a)["rules_fingerprint"], strategy_profile(b)["rules_fingerprint"])
        self.assertEqual(strategy_profile(b)["competition_structure"]["league_teams"], 16)

    def test_rule_change_updates_fingerprint_and_compiled_checklist(self):
        before = strategy_profile(league())
        changed = league()
        changed["rules"]["starters"].append(
            {"label": "SUPER_FLEX", "eligible_positions": ["QB", "RB", "WR", "TE"]}
        )
        after = strategy_profile(changed)
        self.assertNotEqual(before["rules_fingerprint"], after["rules_fingerprint"])
        self.assertIn("measure_qb_optional_slot_replacement", {row["obligation_id"] for row in after["action_checklist"]})
        self.assertNotIn("measure_qb_optional_slot_replacement", {row["obligation_id"] for row in before["action_checklist"]})


if __name__ == "__main__":
    unittest.main()
