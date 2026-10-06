import copy
import unittest

from moneyball.horizon import fit_horizon, transition_for_player
from moneyball.modeling import ModelingError


SCORING = {"rec": 1.0}


def fixture():
    schedules, stats, rosters = [], [], []
    for year in (2023, 2024):
        for game in range(16):
            game_id = f"{year}_{game}"
            team = f"T{2*game}"
            schedules.append({"season": year, "week": 1, "game_type": "REG", "game_id": game_id,
                              "home_team": team, "away_team": f"T{2*game+1}", "home_score": 1, "away_score": 0})
            stats.append({"player_id": f"coverage{game}", "season": year, "week": 1,
                          "game_id": game_id, "team": team, "position": "QB", "stats": {"rec": 0}})
        for i in range(12):
            pid = f"p{i}"
            rosters.append({"player_id": pid, "season": year, "week": 1, "position": "WR",
                            "team": "T0", "birth_date": f"{2000-i}-01-01", "years_exp": i})
            if year == 2023 or i > 1:
                points = (i+1) if year == 2023 else (i+1)*(2 if i % 2 else .5)
                stats.append({"player_id": pid, "season": year, "week": 1,
                              "game_id": f"{year}_0", "team": "T0", "position": "WR", "stats": {"rec": points}})
    kwargs = {"train_base_seasons": [2023], "complete_stats_snapshot": True,
              "expected_season_game_counts": {2023: 16, 2024: 16}}
    return stats, rosters, schedules, kwargs


class HorizonTests(unittest.TestCase):
    def test_complete_risk_set_keeps_zero_next_output(self):
        stats, rosters, schedules, kwargs = fixture()
        model = fit_horizon(stats, rosters, schedules, SCORING, **kwargs)
        self.assertEqual(model["transition_count"], 12)
        self.assertAlmostEqual(model["positions"]["WR"]["zero_fraction"], 2/12)
        self.assertEqual(model["last_outcome_season"], 2024)
        self.assertIn("not_validated", model["evidence_status"])

    def test_partial_season_and_missing_game_never_become_zero(self):
        stats, rosters, schedules, kwargs = fixture()
        with self.assertRaises(ModelingError):
            fit_horizon(stats, rosters, schedules[:-1], SCORING, **kwargs)
        with self.assertRaises(ModelingError):
            fit_horizon([r for r in stats if r["game_id"] != "2024_15"], rosters, schedules, SCORING, **kwargs)

    def test_complete_snapshot_assertion_required(self):
        stats, rosters, schedules, kwargs = fixture()
        kwargs["complete_stats_snapshot"] = False
        with self.assertRaises(ModelingError):
            fit_horizon(stats, rosters, schedules, SCORING, **kwargs)

    def test_no_future_or_postseason_leakage(self):
        stats, rosters, schedules, kwargs = fixture()
        model = fit_horizon(stats, rosters, schedules, SCORING, **kwargs)
        contaminated = stats + [{**stats[-1], "season": 2026, "stats": {"rec": 999999}},
                                {**stats[-1], "week": 20, "season_type": "POST", "stats": {"rec": 999999}}]
        other = fit_horizon(contaminated, rosters, schedules, SCORING, **kwargs)
        self.assertEqual(model["positions"], other["positions"])

    def test_mixture_reproducible_and_nonnegative_with_zero_state(self):
        stats, rosters, schedules, kwargs = fixture()
        model = fit_horizon(stats, rosters, schedules, SCORING, **kwargs)
        player = {"player_id": "p0", "position": "WR", "age": 24, "mean": 1}
        a = transition_for_player(model, player, season=2025, draws=1000, seed=3)
        b = transition_for_player(model, player, season=2025, draws=1000, seed=3)
        self.assertEqual(a["year_transition_samples"], b["year_transition_samples"])
        self.assertGreater(a["zero_output_probability"], 0)
        self.assertEqual(min(a["year_transition_samples"]), 0)
        self.assertFalse(a["unknown_history_transfer"])
        self.assertGreaterEqual(a["credibility"], 0)
        self.assertLessEqual(a["credibility"], 1)

    def test_unknown_and_zero_baselines_disclosed(self):
        stats, rosters, schedules, kwargs = fixture()
        model = fit_horizon(stats, rosters, schedules, SCORING, **kwargs)
        unknown = transition_for_player(model, {"player_id": "rookie", "position": "WR", "age": 21, "mean": 5}, season=2025)
        self.assertTrue(unknown["unknown_history_transfer"])
        self.assertIn("selected cohort", " ".join(unknown["limitations"]))
        zero = transition_for_player(model, {"player_id": "rookie", "position": "WR", "mean": 0}, season=2025)
        self.assertEqual(zero["status"], "unsupported_nonpositive_baseline")

    def test_tail_sensitivity_and_future_season_gate(self):
        stats, rosters, schedules, kwargs = fixture()
        stats[-1]["stats"]["rec"] = 10000
        model = fit_horizon(stats, rosters, schedules, SCORING, **kwargs)
        pool = model["positions"]["WR"]
        self.assertGreater(pool["truncated_count"], 0)
        self.assertGreater(pool["raw_ratio_mean"], pool["ratio_mean"])
        with self.assertRaises(ModelingError):
            transition_for_player(model, {"player_id": "p0", "position": "WR", "mean": 5}, season=2024)


if __name__ == "__main__":
    unittest.main()
