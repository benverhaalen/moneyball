import copy
import unittest

from moneyball.dynasty_decision import compare_models


def report(actions):
    return {"baseline": "hold", "years": 3, "assumptions": {"future_policy": "fixture only"},
            "scenarios": {a: {"7": {"championship_by_year": [{"estimate": p} for p in ps],
                                      "expected_titles": {"estimate": sum(ps)}}}
                          for a, ps in actions.items()}}


class DynastyDecisionTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"models": {
            "a": report({"hold": [.1,.1,.1], "now": [.2,.08,.06], "later": [.06,.18,.22], "worse": [.05,.05,.05]}),
            "b": report({"hold": [.1,.1,.1], "now": [.2,.04,.03], "later": [.06,.12,.16], "worse": [.05,.05,.05]})}}

    def test_timing_tradeoff_and_horizon_count(self):
        result = compare_models(self.payload, 7)
        self.assertEqual(set(result["point_estimate_frontier"]), {"hold", "now", "later"})
        self.assertIn("hold", result["point_estimate_dominated_by"]["worse"])
        self.assertTrue(result["horizons"]["3"]["now"]["direction_changes_across_models"])
        self.assertAlmostEqual(result["horizons"]["3"]["later"]["models"]["a"]["expected_titles"], .46)
        self.assertAlmostEqual(result["horizons"]["1"]["later"]["models"]["a"]["regret_expected_titles"], .14)

    def test_duplicate_models_do_not_change_frontier_or_ranges(self):
        first = compare_models(self.payload, 7)
        repeated = copy.deepcopy(self.payload)
        repeated["models"]["a_duplicate"] = repeated["models"]["a"]
        second = compare_models(repeated, 7)
        self.assertEqual(first["point_estimate_frontier"], second["point_estimate_frontier"])
        self.assertEqual(first["horizons"]["3"]["now"]["delta_range"], second["horizons"]["3"]["now"]["delta_range"])

    def test_no_inferred_joint_title_probability(self):
        result = compare_models(self.payload, 7)
        self.assertNotIn("at_least_one_title", str(result["horizons"]))
        self.assertIn("cannot", result["interpretation"]["not_reported"])

    def test_missing_years_rejects_old_aggregate_only_report(self):
        del self.payload["models"]["a"]["scenarios"]["hold"]["7"]["championship_by_year"]
        with self.assertRaisesRegex(ValueError, "Annual championship"):
            compare_models(self.payload, 7)

    def test_malformed_input_is_a_clear_validation_error(self):
        for payload in ([], {"models": {"bad": "not a report"}}, {"models": {"bad": []}}):
            with self.assertRaises(ValueError):
                compare_models(payload, 7)

    def test_mismatched_actions_or_horizon_fail(self):
        with self.assertRaises(ValueError):
            compare_models(self.payload, 7, horizons=[5])
        del self.payload["models"]["b"]["scenarios"]["later"]
        with self.assertRaisesRegex(ValueError, "same actions"):
            compare_models(self.payload, 7)

    def test_inconsistent_probability_or_total_fails(self):
        self.payload["models"]["a"]["scenarios"]["hold"]["7"]["championship_by_year"][0]["estimate"] = float("nan")
        with self.assertRaises(ValueError):
            compare_models(self.payload, 7)
        self.payload["models"]["a"]["scenarios"]["hold"]["7"]["championship_by_year"][0]["estimate"] = .2
        with self.assertRaisesRegex(ValueError, "reconcile"):
            compare_models(self.payload, 7)

    def test_real_simulator_contract(self):
        from moneyball.simulation import simulate
        state = {str(t): [str(t)] for t in range(1, 13)}
        players = [{"id": str(t), "position": "QB", "mean": t, "sd": 0} for t in range(1, 13)]
        actual = simulate({"hold": state, "identical": state}, {"players": players},
                          draws=2, years=3, slots=["QB"], allow_assumptions=True)
        result = compare_models({"models": {"fixture": actual}}, 12)
        self.assertEqual(result["horizons"]["3"]["hold"]["models"]["fixture"]["expected_titles"], 3)
        self.assertEqual(result["horizons"]["3"]["identical"]["delta_range"], [0, 0])
        self.assertIn("frozen holdings", result["model_assumptions"]["fixture"]["future_policy"])
        evidence = result["model_evidence"]["fixture"]
        self.assertEqual(evidence["draws"], 2)
        self.assertEqual(evidence["seed"], actual["seed"])
        self.assertEqual(evidence["action_summaries"]["identical"]["paired_delta_championship_by_year"],
                         actual["scenarios"]["identical"]["12"]["paired_delta_championship_by_year"])
        self.assertIn("monte_carlo_se", evidence["action_summaries"]["hold"]["expected_titles"])


if __name__ == "__main__":
    unittest.main()
