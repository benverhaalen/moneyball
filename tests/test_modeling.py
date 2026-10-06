import copy
import unittest

from moneyball.modeling import (
    ModelingError, score_stats, fit_model, project, brier_loss, crps_loss,
    measure_reliability, register_evaluation, evaluate_model, temporal_audit,
    measure_role_changes, estimate_horizon_transitions,
    register_source_ablation, evaluate_source_ablation,
    residual_distribution, build_participation_panel, measure_variance_bins, conditional_residual_distribution,
)
from moneyball.store import digest


SCORING = {"pass_yd": .04, "pass_td": 4., "pass_int": -1., "pass_2pt": 2.,
           "rush_yd": .1, "rush_td": 6., "rush_2pt": 2., "rec": 1.,
           "rec_yd": .1, "rec_td": 6., "rec_2pt": 2., "fum_lost": -2.}


def row(pid, season, week, points, **extra):
    stats = {k: 0. for k in SCORING}
    stats["rec_yd"] = points * 10
    return {"player_id": str(pid), "season": season, "week": week, "position": "WR",
            "stats": stats, "team": "A", "opponent_team": "B",
            "game_id": f"{season}_{week}_A_B", **extra}


def train_rows():
    return [row(pid, 2023, w, level + (-1 if w % 2 else 1))
            for pid, level in [("a", 5), ("b", 15), ("c", 25)] for w in range(1, 11)]


class ScoringTests(unittest.TestCase):
    def test_native_exact_scoring_not_published_points(self):
        stats = {k: 0 for k in SCORING}
        stats.update(pass_yd=250, pass_td=2, pass_int=1, rush_yd=30,
                     rush_td=1, rec=2, rec_yd=10, fum_lost=1, pts_ppr=999)
        result = score_stats(stats, SCORING, strict=True)
        self.assertEqual(result["points"], 27)
        self.assertTrue(result["complete"])

    def test_aggregate_fumbles_not_double_counted(self):
        result = score_stats({"fumbles_lost_total": 2, "sack_fumbles_lost": 1,
                              "rushing_fumbles_lost": 1, "receiving_fumbles_lost": 0},
                             {"fum_lost": -2}, source_schema="nflverse", strict=True)
        self.assertEqual(result["points"], -4)
        self.assertEqual(result["source_fields"]["fum_lost"], ["fumbles_lost_total"])

    def test_fumble_components_only_when_complete(self):
        result = score_stats({"sack_fumbles_lost": 1, "rushing_fumbles_lost": 2,
                              "receiving_fumbles_lost": 0}, {"fum_lost": -2}, source_schema="nflverse")
        self.assertEqual(result["points"], -6)
        with self.assertRaises(ModelingError):
            score_stats({"sack_fumbles_lost": 1}, {"fum_lost": -2}, source_schema="nflverse", strict=True)

    def test_special_teams_ambiguity_is_loud(self):
        r = score_stats({"special_teams_tds": 1, "fumble_recovery_opp": 2},
                        {"st_td": 6, "st_fum_rec": 1}, source_schema="nflverse")
        self.assertEqual(r["points"], 6)
        self.assertEqual(r["missing_scoring_keys"], ["st_fum_rec"])
        self.assertFalse(r["complete"])

    def test_missing_is_not_zero_without_explicit_contract(self):
        with self.assertRaises(ModelingError):
            score_stats({"rec": 1}, {"rec": 1, "pass_td": 4}, strict=True)
        self.assertEqual(score_stats({"rec": 1}, {"rec": 1, "pass_td": 4},
                                     sparse_zero=True, strict=True)["points"], 1)

    def test_interceptions_and_zero_negative_counts(self):
        r = score_stats({"passing_interceptions": 2, "passing_yards": -10},
                        {"pass_int": -1, "pass_yd": .04}, source_schema="nflverse", strict=True)
        self.assertAlmostEqual(r["points"], -2.4)


class FitProjectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = train_rows()
        self.model = fit_model(self.rows, SCORING, train_seasons=[2023])

    def test_holdout_not_in_fit_and_shrinkage_uses_train(self):
        m = fit_model(self.rows + [row("a", 2025, 1, 10000)], SCORING, train_seasons=[2023])
        self.assertEqual(m["players"], self.model["players"])
        self.assertGreater(m["players"]["a"]["mean"], 5)
        self.assertLess(m["players"]["c"]["mean"], 25)

    def test_duplicate_observation_refused(self):
        with self.assertRaises(ModelingError):
            fit_model(self.rows + self.rows[:1], SCORING, train_seasons=[2023])

    def test_missing_rows_do_not_invent_availability(self):
        self.assertIsNone(self.model["players"]["a"]["availability"])
        p = project(self.model, [{"id": "a", "position": "WR"}], season=2026, week=1, draws=32)
        self.assertIsNone(p["players"][0]["availability"])
        self.assertEqual(p["players"][0]["mean_definition"], "conditional_on_observed_active_output")

    def test_explicit_inactive_row_with_no_stats_contributes_denominator(self):
        rows = [dict(r, at_risk=True, active=True) for r in self.rows]
        rows.append({"player_id": "a", "season": 2023, "week": 11,
                     "position": "WR", "stats": {}, "at_risk": True, "active": False})
        m = fit_model(rows, SCORING, train_seasons=[2023])
        self.assertEqual(m["players"]["a"]["availability_n"], 11)
        self.assertLess(m["players"]["a"]["availability"], 1)

    def test_provider_mean_preserved_and_gsis_join(self):
        p = [{"player_id": "sleeper_a", "gsis_id": "a", "position": "WR", "team": "A"}]
        f = [{"player_id": "sleeper_a", "season": 2026, "week": 1,
              "mean": 20, "scoring_hash": digest(SCORING), "source_id": "pro"}]
        out = project(self.model, p, season=2026, week=1, forecast_rows=f, draws=1000, seed=42)
        self.assertEqual(out["players"][0]["mean"], 20)
        self.assertEqual(out["players"][0]["historical_observations"], 10)
        self.assertIsNone(out["players"][0]["availability"])
        self.assertAlmostEqual(out["players"][0]["sample_mean"], 20, delta=.25)

    def test_flat_history_does_not_create_certain_positive_projection(self):
        rows = train_rows() + [row("flat",2023,w,0) for w in range(1,11)]
        m = fit_model(rows,SCORING,train_seasons=[2023])
        uncertainty = residual_distribution(m,{"id":"flat","position":"WR"})
        self.assertEqual(uncertainty["historical_individual_sd"],0)
        self.assertGreater(uncertainty["sd"],0)
        out=project(m,[{"id":"flat","position":"WR"}],season=2026,week=1,
                    forecast_rows=[{"player_id":"flat","season":2026,"week":1,"mean":10}],draws=100)
        self.assertGreater(out["players"][0]["sd"],0)

    def test_availability_scenario_does_not_double_haircut_provider(self):
        p = [{"player_id": "a", "position": "WR"}]
        f = [{"player_id": "a", "season": 2026, "week": 1, "mean": 10}]
        out = project(self.model, p, season=2026, week=1, forecast_rows=f,
                      availability_overrides={"a": .5}, draws=10000, seed=3)
        x = out["players"][0]
        self.assertEqual(x["mean"], 10)
        self.assertEqual(x["active_mean"], 20)
        self.assertAlmostEqual(x["sample_mean"], 10, delta=.35)

    def test_projection_cannot_use_future_training_season(self):
        with self.assertRaises(ModelingError):
            project(self.model, [{"id": "a"}], season=2023, week=1)

    def test_wrong_scoring_and_duplicate_provider_rejected(self):
        p = [{"id": "a", "position": "WR"}]
        f = {"player_id": "a", "season": 2026, "week": 1, "mean": 10, "scoring_hash": "wrong"}
        with self.assertRaises(ModelingError):
            project(self.model, p, season=2026, week=1, forecast_rows=[f])
        f.pop("scoring_hash")
        with self.assertRaises(ModelingError):
            project(self.model, p, season=2026, week=1, forecast_rows=[f, f])

    def test_reproducible_joint_samples_and_measured_common_factor(self):
        p = [{"id": "a", "position": "WR", "team": "A", "game_id": "one"},
             {"id": "b", "position": "WR", "team": "A", "game_id": "one"}]
        x = project(self.model, p, season=2026, week=1, draws=100, seed=4)
        y = project(self.model, p, season=2026, week=1, draws=100, seed=4)
        self.assertEqual(x["joint_samples_by_id"], y["joint_samples_by_id"])
        self.assertGreater(x["players"][0]["factor_loadings"]["team"], 0)


class EvaluationTests(unittest.TestCase):
    def test_proper_score_known_values(self):
        self.assertEqual(brier_loss(.75, 1), .0625)
        self.assertEqual(crps_loss([5, 5], 5), 0)
        self.assertEqual(crps_loss([0, 2], 1), .5)
        self.assertEqual(crps_loss([0, 0], 2), 2)

    def test_empirical_crps_matches_pairwise_definition(self):
        xs, y = [-3, 0, 2, 2, 9], 1
        expected = sum(abs(x-y) for x in xs)/len(xs) - sum(abs(x-z) for x in xs for z in xs)/(2*len(xs)**2)
        self.assertAlmostEqual(crps_loss(xs, y), expected)

    def test_final_historical_data_cannot_be_called_validation(self):
        m = fit_model(train_rows(), SCORING, train_seasons=[2023], cutoff=1704067200)
        reg = register_evaluation(train_seasons=[2023], holdout_seasons=[2025], cutoff=1704067200)
        held = [row("a", 2025, 1, 7)]
        result = evaluate_model(m, held, SCORING, registry=reg)
        self.assertFalse(result["validation_gate"]["passed"])
        self.assertEqual(result["comparison_count"], 3)
        with self.assertRaises(ModelingError):
            evaluate_model(m, held, SCORING, registry=reg, require_validation=True)

    def test_vintage_and_timestamp_both_required(self):
        base = row("a", 2023, 1, 4, _provenance={"available_at": 200, "vintage_verified": True})
        self.assertFalse(temporal_audit([base], 100)["passed"])
        self.assertTrue(temporal_audit([base], 300)["passed"])
        base["_provenance"]["vintage_verified"] = False
        self.assertFalse(temporal_audit([base], 300)["passed"])

    def test_body_observed_now_is_valid_for_future_but_not_past(self):
        r = row("a", 2023, 1, 4, _provenance={"available_at": 200, "observed_at": 200, "vintage_verified": False})
        self.assertTrue(temporal_audit([r], 300)["passed"])
        self.assertFalse(temporal_audit([r], 100)["passed"])

    def test_source_ablation_blocks_unresolved_future(self):
        m = fit_model(train_rows(), SCORING, train_seasons=[2023])
        reg = register_source_ablation(["a", "b"], cutoff=100)
        fs = [{"player_id":"p", "season":2026, "week":1, "source_id":s, "mean":10} for s in ["a","b"]]
        out = evaluate_source_ablation(m, fs, [], registry=reg)
        self.assertEqual(out["evidence_status"], "blocked_no_resolved_common_outcomes")
        self.assertEqual(out["comparison_count"], 3)
        self.assertEqual(out["results"], {})

    def test_source_ablation_does_not_confuse_annual_overlap_with_weekly(self):
        m = fit_model(train_rows(), SCORING, train_seasons=[2023])
        reg = register_source_ablation(["a", "b"], cutoff=100)
        fs = [{"player_id":"p", "season":2026, "week":week, "source_id":source, "mean":10}
              for source,week in [("a",1),("b",None)]]
        result = evaluate_source_ablation(m, fs, [], registry=reg)
        self.assertEqual(result["common_forecast_player_weeks"], 0)
        self.assertIn("annual player overlap", " ".join(result["validation_gate"]["issues"]))

    def test_within_season_evaluation_needs_event_time_before_cutoff(self):
        training = [dict(r, _provenance={"available_at": 50,"vintage_verified": True}) for r in train_rows()]
        cutoff = 1756684800  # 2025-09-01; event time must now be independently known.
        m = fit_model(training, SCORING, train_seasons=[2023], cutoff=cutoff)
        reg = register_evaluation(train_seasons=[2023], holdout_seasons=[2025], cutoff=cutoff, created_at=cutoff-1)
        held = row("a",2025,1,7)
        self.assertFalse(evaluate_model(m,[held],SCORING,registry=reg)["validation_gate"]["passed"])
        held["event_at"] = cutoff+100
        self.assertTrue(evaluate_model(m,[held],SCORING,registry=reg)["validation_gate"]["passed"])
        held["event_at"] = cutoff-100
        self.assertFalse(evaluate_model(m,[held],SCORING,registry=reg)["validation_gate"]["passed"])

    def test_source_ablation_uses_common_support_and_predeclared_combinations(self):
        m = fit_model(train_rows(), SCORING, train_seasons=[2023])
        reg = register_source_ablation(["a", "b"], cutoff=100)
        fs = [{"player_id":"a", "season":2025, "week":1, "source_id":s, "mean":mean}
              for s,mean in [("a",5),("b",25)]]
        fs.append({"player_id":"b", "season":2025, "week":1, "source_id":"a", "mean":5})
        out = evaluate_source_ablation(m, fs, [row("a",2025,1,5),row("b",2025,1,10)], registry=reg)
        self.assertEqual(out["common_support_rows"], 1)
        self.assertEqual(set(out["results"]), {"a","b","a+b"})
        self.assertGreater(out["results"]["b"]["paired_delta_CRPS_vs_baseline"], 0)

    def test_registry_mutation_and_overlapping_train_test_refused(self):
        with self.assertRaises(ModelingError):
            register_evaluation(train_seasons=[2023], holdout_seasons=[2023], cutoff=100)
        m = fit_model(train_rows(), SCORING, train_seasons=[2023])
        reg = register_evaluation(train_seasons=[2023], holdout_seasons=[2025], cutoff=100)
        reg["models"].append("provider")
        with self.assertRaises(ModelingError):
            evaluate_model(m, [row("a", 2025, 1, 7)], SCORING, registry=reg)

    def test_available_vintage_and_prior_registration_can_pass(self):
        rows = [dict(r, _provenance={"available_at": 50, "vintage_verified": True}) for r in train_rows()]
        m = fit_model(rows, SCORING, train_seasons=[2023], cutoff=100)
        reg = register_evaluation(train_seasons=[2023], holdout_seasons=[2025], cutoff=100, created_at=99)
        result = evaluate_model(m, [row("a", 2025, 1, 7)], SCORING, registry=reg, require_validation=True)
        self.assertTrue(result["validation_gate"]["passed"])


class DiagnosticsTests(unittest.TestCase):
    def test_conditional_variance_bins_measure_output_level(self):
        rows = [row(pid, 2023, week, mean + (-sd if week % 2 else sd))
                for pid, mean, sd in [("low", 1, .2), ("mid", 5, 1), ("high", 10, 3), ("top", 20, 6)]
                for week in range(1,11)]
        model = fit_model(rows, SCORING, train_seasons=[2023])
        low = conditional_residual_distribution(model, {"player_id": "new", "position": "WR", "mean": 1})
        high = conditional_residual_distribution(model, {"player_id": "new", "position": "WR", "mean": 20})
        self.assertLess(low["sd"], high["sd"])
        self.assertAlmostEqual(sum(low["residuals"])/len(low["residuals"]), 0)
        self.assertEqual(low["professional_center"], 1)
        self.assertIn("unvalidated", " ".join(low["limitations"]))

    def test_participation_requires_snapshot_and_game_source_coverage(self):
        roster = [{"player_id": "a", "season": 2025, "week": w, "position": "WR", "team": "A"}
                  for w in (1, 2, 3)]
        games = [{"season": 2025, "week": 1, "home_team": "A", "away_team": "B",
                  "game_id": "g", "home_score": 1, "away_score": 2, "game_type": "REG"},
                 {"season": 2025, "week": 3, "home_team": "A", "away_team": "B",
                  "game_id": "p", "home_score": 1, "away_score": 2, "game_type": "POST"}]
        stats = [row("other", 2025, 1, 5)]
        snaps = [{"season": 2025, "week": 1, "team": "A", "offense_snaps": 50}]
        unknown = build_participation_panel(stats, roster, games, snaps)
        self.assertEqual(unknown["rows"], [])
        complete = build_participation_panel(stats, roster, games, snaps, complete_snapshot=True)
        self.assertEqual(len(complete["rows"]), 1)
        self.assertFalse(complete["rows"][0]["active"])
        self.assertFalse(complete["rows"][0]["medical_availability"])
        self.assertEqual(complete["diagnostics"]["excluded_or_unresolved"]["bye_or_uncompleted_game"], 2)

    def test_participation_exact_snap_join_and_reported_injury(self):
        rosters = [{"player_id": "a", "gsis_id": "a", "pfr_id": "pfra", "season": 2025,
                    "week": 1, "position": "WR", "team": "A"}]
        games = [{"season": 2025, "week": 1, "home_team": "A", "away_team": "B",
                  "game_id": "g", "home_score": 1, "away_score": 2}]
        snaps = [{"season": 2025, "week": 1, "team": "A", "offense_snaps": 1,
                  "pfr_player_id": "pfra", "offense_pct": .02}]
        panel = build_participation_panel([], rosters, games, snaps)
        self.assertTrue(panel["rows"][0]["active"])
        self.assertEqual(panel["rows"][0]["stats"]["offense_pct"], .02)

    def test_reliability_separates_positions_and_counts_windows(self):
        result = measure_reliability(train_rows(), SCORING, metrics=["league_points"], windows=(2,), gap=1)
        adjacent = next(x for x in result["curves"] if x["kind"] == "adjacent")
        self.assertEqual(adjacent["units"], 3)
        self.assertAlmostEqual(adjacent["correlation"], 1)
        self.assertEqual(result["evidence_status"], "exploratory_historical_association")

    def test_missing_week_not_inserted_as_zero(self):
        rows = [r for r in train_rows() if r["week"] != 2]
        result = measure_reliability(rows, SCORING, metrics=["league_points"], windows=(2,), gap=1)
        self.assertTrue(all(x["units"] == 0 for x in result["curves"]))

    def test_gaps_do_not_claim_injury_recovery(self):
        result = measure_role_changes(train_rows(), SCORING)
        self.assertEqual(result["recovery_windows"], [])

    def test_role_flags_timestamp_when_post_window_known(self):
        rows = train_rows()
        for r in rows:
            r["stats"]["offense_pct"] = .2 if r["week"] <= 5 else .8
        result = measure_role_changes(rows, SCORING)
        self.assertTrue(result["role_changes"])
        for r in result["role_changes"]:
            self.assertEqual(r["known_only_after_week"], r["after_week"])

    def test_horizon_stats_retention_not_survival(self):
        rows = train_rows() + [row("a", 2024, 1, 8)]
        r = estimate_horizon_transitions(rows, SCORING)
        self.assertAlmostEqual(r["groups"]["WR"]["next_year_observed_fraction"], 1/3)
        self.assertFalse(r["usable_as_individual_survival"])


if __name__ == "__main__":
    unittest.main()
