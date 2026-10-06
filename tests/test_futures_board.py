import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from moneyball.futures_board import export_board, read_json
from moneyball.simulation import simulate


def fixture():
    """Same synthetic deterministic simulation pattern as test_dynasty_decision."""
    state = {str(t): [str(t)] for t in range(1, 13)}
    players = [{"id": str(t), "position": "QB", "mean": t, "sd": 0}
               for t in range(1, 13)]
    swapped = copy.deepcopy(state)
    swapped["1"], swapped["12"] = swapped["12"], swapped["1"]
    return {"synthetic_software_fixture_only": True, "models": {
        "fixture": simulate({"hold": state, "swap": swapped}, {"players": players},
                            draws=2, years=3, slots=["QB"], allow_assumptions=True)}}


class FuturesBoardTests(unittest.TestCase):
    def setUp(self):
        self.payload = fixture()
        self.descriptors = {"fixture": {
            "future_policy_mode": "frozen_holdings",
            "horizon_semantics": "Synthetic three-year fixed-roster software example, labeled 2026–2028; no real forecasts.",
        }}

    def export(self, payload=None, **kwargs):
        return export_board(self.payload if payload is None else payload,
                            season_labels=kwargs.pop("season_labels", [2026, 2027, 2028]),
                            model_descriptors=kwargs.pop("model_descriptors", self.descriptors), **kwargs)

    def rows(self, action="hold"):
        return self.payload["models"]["fixture"]["scenarios"][action]

    def test_real_simulation_shape_all_teams_years_and_paired_uncertainty_preserved(self):
        original = copy.deepcopy(self.payload)
        board = self.export()
        self.assertEqual(board["input_kind"], "synthetic_software_fixture")
        self.assertEqual(board["team_ids"], list(map(str, range(1, 13))))
        model = board["models"]["fixture"]
        self.assertEqual(set(model["annual_boards"]), {"2026", "2027", "2028"})
        for actions in model["annual_boards"].values():
            self.assertEqual(sum(r["championship"]["estimate"] for r in actions["swap"]), 1)
            self.assertEqual(sum(r["delta_from_baseline"] for r in actions["swap"]), 0)
            self.assertEqual(actions["swap"][0]["delta_from_baseline"], 1)
            self.assertEqual(actions["swap"][-1]["delta_from_baseline"], -1)
        self.assertEqual(model["annual_boards"]["2026"]["swap"][0]["paired_delta_championship"],
                         self.rows("swap")["1"]["paired_delta_championship_by_year"][0])
        self.assertEqual(model["assumptions"], self.payload["models"]["fixture"]["assumptions"])
        self.assertEqual(self.payload, original)
        model["assumptions"]["future_policy"] = "modified output"
        self.assertEqual(self.payload, original)

    def test_baseline_only_without_paired_components(self):
        del self.payload["models"]["fixture"]["scenarios"]["swap"]
        for row in self.rows().values():
            row.pop("paired_delta_championship_by_year")
            row.pop("paired_delta_expected_titles")
        board = self.export()
        row = board["models"]["fixture"]["annual_boards"]["2026"]["hold"][0]
        self.assertIsNone(row["paired_delta_championship"])
        self.assertIn("no_paired_uncertainty", row["delta_uncertainty_status"])
        self.assertEqual(row["delta_from_baseline"], 0)

    def test_actions_without_paired_summaries_do_not_invent_standard_errors(self):
        for action in ("hold", "swap"):
            for row in self.rows(action).values():
                row.pop("paired_delta_championship_by_year")
                row.pop("paired_delta_expected_titles")
        row = self.export()["models"]["fixture"]["annual_boards"]["2026"]["swap"][0]
        self.assertEqual(row["delta_from_baseline"], 1)
        self.assertIsNone(row["paired_delta_championship"])

    def test_probability_and_paired_conservation_rejected(self):
        self.rows()["1"]["championship_by_year"][0]["estimate"] = .2
        self.rows()["1"].pop("expected_titles")
        self.rows()["1"].pop("current_championship")
        with self.assertRaisesRegex(ValueError, "championship conservation"):
            self.export()
        self.payload = fixture()
        self.rows("swap")["1"]["paired_delta_championship_by_year"][0]["estimate"] = .5
        self.rows("swap")["1"].pop("paired_delta_expected_titles")
        with self.assertRaisesRegex(ValueError, "paired delta conservation"):
            self.export()

    def test_conserved_but_wrong_paired_deltas_fail(self):
        for team in ("1", "12"):
            row = self.rows("swap")[team]
            row.pop("paired_delta_expected_titles")
            row["paired_delta_championship_by_year"][0]["estimate"] = 0
        with self.assertRaisesRegex(ValueError, "paired delta versus baseline"):
            self.export()

    def test_missing_annual_years_or_partial_paired_fail_loudly(self):
        self.rows()["1"]["championship_by_year"].pop()
        with self.assertRaisesRegex(ValueError, "complete annual"):
            self.export()
        self.payload = fixture()
        del self.rows("swap")["1"]["paired_delta_championship_by_year"]
        with self.assertRaisesRegex(ValueError, "all teams or none"):
            self.export()

    def test_aggregate_only_paired_delta_is_not_reconstructed(self):
        for row in self.rows().values():
            row.pop("paired_delta_championship_by_year")
        with self.assertRaisesRegex(ValueError, "aggregate paired delta lacks"):
            self.export()

    def test_duplicate_normalized_and_mismatched_team_ids(self):
        self.rows()[1] = self.rows()["1"]
        with self.assertRaisesRegex(ValueError, "duplicate team IDs"):
            self.export()
        self.payload = fixture()
        self.rows("swap")["13"] = self.rows("swap").pop("12")
        with self.assertRaisesRegex(ValueError, "same twelve team IDs"):
            self.export()
        self.payload = fixture()
        self.rows().pop("1")
        with self.assertRaisesRegex(ValueError, "exactly twelve"):
            self.export()

    def test_explicit_horizon_and_policy_are_required(self):
        for descriptors in ({}, {"fixture": {}}, {"fixture": {"future_policy_mode": "unknown"}}):
            with self.assertRaises(ValueError):
                self.export(model_descriptors=descriptors)
        for years in ([2026, 2027], [2026, 2028, 2029], [2026, 2026, 2027], [True, 2, 3]):
            with self.assertRaises(ValueError):
                self.export(season_labels=years)
        self.payload["models"]["fixture"]["assumptions"].pop("future_policy")
        with self.assertRaisesRegex(ValueError, "Source future_policy"):
            self.export()

    def test_adaptive_claim_cannot_overwrite_explicit_frozen_source(self):
        self.descriptors["fixture"]["future_policy_mode"] = "adaptive"
        with self.assertRaisesRegex(ValueError, "contradicts"):
            self.export()
        self.payload["models"]["fixture"]["assumptions"]["future_policy"] = "Synthetic adaptive policy description supplied by caller"
        result = self.export()
        self.assertEqual(result["models"]["fixture"]["descriptor"]["future_policy_mode"], "adaptive")
        self.assertIn("not certified", result["interpretation"]["future_policy"])

    def test_scenario_ranges_are_unweighted_not_confidence_intervals(self):
        alternative = copy.deepcopy(self.payload["models"]["fixture"])
        alternative["scenarios"]["swap"] = copy.deepcopy(alternative["scenarios"]["hold"])
        self.payload["models"]["alternative"] = alternative
        self.descriptors["alternative"] = copy.deepcopy(self.descriptors["fixture"])
        result = self.export()
        row = result["across_model_ranges"]["2026"]["swap"][0]
        self.assertEqual(row["championship_probability_range"], [0, 1])
        self.assertEqual(row["delta_point_estimate_range"], [0, 1])
        self.assertIn("not confidence", result["interpretation"]["ranges"])
        self.payload["models"]["duplicate"] = copy.deepcopy(alternative)
        self.descriptors["duplicate"] = copy.deepcopy(self.descriptors["fixture"])
        self.assertEqual(result["across_model_ranges"], self.export()["across_model_ranges"])

    def test_bad_numbers_uncertainty_meaning_and_totals(self):
        for value in (True, float("nan"), float("inf"), -.01, 1.01):
            self.payload = fixture()
            self.rows()["1"]["championship_by_year"][0]["estimate"] = value
            with self.assertRaises(ValueError):
                self.export()
        self.payload = fixture()
        self.rows()["1"]["championship_by_year"][0].pop("interval_meaning")
        with self.assertRaisesRegex(ValueError, "uncertainty meaning"):
            self.export()
        self.payload = fixture()
        self.rows()["1"]["expected_titles"]["estimate"] = 1
        with self.assertRaisesRegex(ValueError, "annual title sum"):
            self.export()

    def test_cli_duplicate_json_and_valid_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text('{"models":{},"models":{}}')
            with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
                read_json(path)
            path.write_text(json.dumps(self.payload))
            output = Path(temporary) / "board.json"
            command = [sys.executable, "-m", "moneyball.futures_board", str(path),
                       "--seasons", "2026,2027,2028", "--future-policy", "frozen_holdings",
                       "--horizon-semantics", "Synthetic software example, no player forecast",
                       "--output", str(output)]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), json.loads(output.read_text()))
            path.write_text('{"models":{},"models":{}}')
            failure = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failure.returncode, 2)
            self.assertIn("Duplicate JSON", json.loads(failure.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
