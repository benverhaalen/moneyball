import itertools
import unittest

from moneyball.coverage_stress import (
    compare_candidate_branches,
    exact_assignment,
    stress_scenarios,
)
from moneyball.simulation import ELIGIBILITY


def brute_force(roster, players, values, slots):
    best = None
    for length in range(min(len(roster), len(slots)) + 1):
        for chosen in itertools.permutations(roster, length):
            for indexes in itertools.combinations(range(len(slots)), length):
                if not all(
                    set(players[player_id].get("fantasy_positions") or [players[player_id]["position"]])
                    & ELIGIBILITY[slots[index]]
                    for player_id, index in zip(chosen, indexes)
                ):
                    continue
                candidate = (length, sum(values[player_id] for player_id in chosen))
                if best is None or candidate > best:
                    best = candidate
    return best


class CoverageStressTests(unittest.TestCase):
    def test_exact_assignment_matches_small_brute_force_with_dual_eligibility(self):
        players = {
            "q": {"position": "QB"},
            "r": {"position": "RB"},
            "w": {"position": "WR"},
            "tw": {"position": "TE", "fantasy_positions": ["TE", "WR"]},
        }
        values = {"q": 9, "r": -2, "w": 5, "tw": 6}
        slots = ("QB", "WR", "FLEX")
        result = exact_assignment(players, players, values, slots)
        brute = brute_force(tuple(players), players, values, slots)
        self.assertEqual((result["filled_slots"], result["score"]), brute)
        self.assertEqual(len({r["player_id"] for r in result["assignments"]}), 3)

    def test_negative_player_fills_mandatory_slot_and_missing_value_is_loud(self):
        players = {"r": {"position": "RB"}}
        result = exact_assignment(["r"], players, {"r": -4}, ("RB",))
        self.assertTrue(result["complete"])
        self.assertEqual(result["score"], -4)
        with self.assertRaisesRegex(ValueError, "Missing values"):
            exact_assignment(["r"], players, {}, ("RB",))

    def test_explicit_unavailability_changes_entrant(self):
        players = {player_id: {"position": "WR"} for player_id in ("a", "b", "c")}
        values = {"a": 10, "b": 8, "c": 3}
        results = stress_scenarios(
            players,
            players,
            values,
            {"nominal": (), "a_out": ("a",)},
            slots=("WR", "FLEX"),
        )
        nominal = {row["player_id"] for row in results["nominal"]["assignments"]}
        stressed = {row["player_id"] for row in results["a_out"]["assignments"]}
        self.assertEqual(nominal, {"a", "b"})
        self.assertEqual(stressed, {"b", "c"})

    def test_candidate_comparison_does_not_leak_later_roster_information(self):
        players = {
            "starter": {"position": "WR"},
            "candidate_a": {"position": "WR"},
            "candidate_b": {"position": "WR"},
            "future_star": {"position": "WR"},
        }
        values = {"starter": 10, "candidate_a": 4, "candidate_b": 3, "future_star": 100}
        branches = compare_candidate_branches(
            ["starter"],
            ["candidate_a", "candidate_b"],
            players,
            values,
            {"starter_out": ("starter",)},
            slots=("WR",),
        )
        used = {
            row["player_id"]
            for branch in branches.values()
            for row in branch["starter_out"]["assignments"]
        }
        self.assertNotIn("future_star", used)
        self.assertEqual(used, {"candidate_a", "candidate_b"})


if __name__ == "__main__":
    unittest.main()
