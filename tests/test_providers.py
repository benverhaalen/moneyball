import json
import tempfile
import unittest
from unittest.mock import patch

from moneyball.providers import (IdentityCrosswalk, ProviderError, normalize_sleeper,
    normalize_fantasycalc, normalize_dynastyprocess, normalize_fantasypros,
    ingest_providers, SLEEPER_BASE, SLEEPER_QUERY)
from moneyball.warehouse import Warehouse


def sleeper_row(pid="1", stats=None, week=1):
    return {"player_id": pid, "season": "2026", "week": week,
            "company": "rotowire", "updated_at": 1,
            "player": {"first_name": "Test", "last_name": "Player", "position": "QB", "fantasy_positions": ["QB"]},
            "stats": {"pass_yd": 250, "pass_td": 2, "pass_int": 1, "pts_ppr": 999} if stats is None else stats}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.crosswalk = IdentityCrosswalk([{"sleeper_id": "1", "fantasypros_id": "123", "name": "Test Player", "position": "QB"}])

    def test_scoring_ignores_builtin_and_filters_placeholder(self):
        rows, adp, coverage = normalize_sleeper([
            sleeper_row(stats={"pass_yd": 250, "pass_td": 2, "pass_int": 1, "pts_ppr": 999, "adp_dynasty_2qb": 3, "adp_dynasty_ppr": 20}),
            sleeper_row("2", {"adp_dd_ppr": 1000})], 2026, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mean"], 17)
        self.assertEqual({r["adp_type"] for r in adp}, {"adp_dynasty_2qb", "adp_dynasty_ppr"})
        self.assertEqual(coverage["no_performance_projection"], 1)

    def test_no_forecasts_or_wrong_week_fail(self):
        with self.assertRaises(ProviderError):
            normalize_sleeper([sleeper_row(stats={"adp_dd_ppr": 10})], 2026, 1)
        with self.assertRaises(ProviderError):
            normalize_sleeper([sleeper_row(week=2)], 2026, 1)

    def test_nonfinite_and_duplicate_fail(self):
        with self.assertRaises(ProviderError):
            normalize_sleeper([sleeper_row(stats={"pass_yd": float("nan")})], 2026, 1)
        with self.assertRaises(ProviderError):
            normalize_sleeper([sleeper_row(), sleeper_row()], 2026, 1)

    def test_zero_forecast_not_missing(self):
        rows, _, _ = normalize_sleeper([sleeper_row(stats={"pass_yd": 0})], 2026, 1)
        self.assertEqual(rows[0]["mean"], 0)

    def test_missing_interceptions_are_unknown_not_reported_complete(self):
        scoring = {"pass_yd": .04, "pass_int": -1, "st_td": 6, "fum": 0}
        rows, _, _ = normalize_sleeper([
            sleeper_row(stats={"pass_yd": 250})], 2026, 1, scoring)
        row = rows[0]
        self.assertEqual(row["mean"], 10)
        self.assertEqual(row["mean_interpretation"], "observed_component_subtotal")
        self.assertEqual(row["projection_completeness"], "partial_sparse_zero_unverified")
        self.assertEqual(row["missing_scoring_fields"], ["pass_int", "st_td"])
        self.assertEqual(row["absent_scoring_fields"], ["pass_int", "st_td"])
        self.assertEqual(row["scoring_field_status"]["pass_int"], "absent")
        self.assertNotIn("fum", row["scoring_field_status"])
        self.assertNotIn("pass_int", row["stats"])

    def test_explicit_zero_and_null_are_distinct_from_absence(self):
        scoring = {"pass_yd": .04, "pass_int": -1, "fum_lost": -2, "st_td": 6}
        rows, _, _ = normalize_sleeper([
            sleeper_row(stats={"pass_yd": 250, "pass_int": 0, "fum_lost": None})], 2026, 1, scoring)
        row = rows[0]
        self.assertEqual(row["mean"], 10)
        self.assertEqual(row["explicit_zero_scoring_fields"], ["pass_int"])
        self.assertEqual(row["null_scoring_fields"], ["fum_lost"])
        self.assertEqual(row["absent_scoring_fields"], ["st_td"])
        self.assertEqual(row["missing_scoring_fields"], ["fum_lost", "st_td"])
        self.assertEqual(row["scoring_field_status"]["fum_lost"], "source_null")
        self.assertEqual(row["stats"]["pass_int"], 0)
        self.assertNotIn("fum_lost", row["stats"])

    def test_complete_means_all_requested_nonzero_keys_supplied(self):
        rows, _, _ = normalize_sleeper([
            sleeper_row(stats={"pass_yd": 250, "pass_int": 0})],
            2026, 1, {"pass_yd": .04, "pass_int": -1, "fum": 0})
        self.assertEqual(rows[0]["missing_scoring_fields"], [])
        self.assertEqual(rows[0]["projection_completeness"], "complete_for_requested_scoring_keys")
        self.assertEqual(rows[0]["mean_interpretation"], "score_of_provided_component_means")

    def test_null_only_projection_is_not_a_zero_mean(self):
        with self.assertRaises(ProviderError):
            normalize_sleeper([sleeper_row(stats={"pass_int": None})], 2026, 1)

    def test_identity_ambiguous_never_fuzzy(self):
        cross = IdentityCrosswalk([
            {"sleeper_id": "1", "fantasypros_id": "123", "name": "Test Player", "position": "QB"},
            {"sleeper_id": "2", "fantasypros_id": "123", "name": "Test Player", "position": "QB"}])
        with self.assertRaises(ProviderError):
            cross.resolve("123", "Test Player", "QB")
        self.assertEqual(self.crosswalk.resolve(None, "Test Player Jr.", "QB"), (None, "unmatched"))

    def test_identity_crosswalk_does_not_overwrite_historical_position(self):
        cross = IdentityCrosswalk([{"sleeper_id": "1", "fantasypros_id": "123", "name": "Test Player", "position": "TE"}])
        pid, method = cross.resolve("123", "Test Player", "WR")
        self.assertEqual(pid, "1")
        self.assertIn("current_position_differs", method)

    def test_market_not_projection_and_missing_id_quarantined(self):
        rows, unknown = normalize_fantasycalc([
            {"player": {"id": 10, "sleeperId": "1", "name": "Test Player", "position": "QB"}, "value": 12},
            {"player": {"id": 11, "name": "2027 First", "position": "PICK"}, "value": 100}], 2026)
        self.assertEqual(rows[0]["value_type"], "trade_market")
        self.assertNotIn("mean", rows[0])
        self.assertEqual(len(unknown), 1)

    def test_dynasty_formats_and_pick_quarantine(self):
        body = b'player,pos,ecr_1qb,ecr_2qb,value_1qb,value_2qb,scrape_date,fp_id\nTest Player,QB,20,2,200,900,2024-08-30,123\n2027 First,PICK,NA,NA,300,400,2024-08-30,NA\n'
        rows, unmatched = normalize_dynastyprocess(body, 2024, self.crosswalk)
        self.assertEqual({row["format"] for row in rows}, {"dynasty_1qb", "dynasty_2qb"})
        self.assertEqual(len(unmatched), 2)
        self.assertEqual(rows[0]["provider_scrape_date"], "2024-08-30")

    def test_html_grouped_headers_own_score_and_unknown_column(self):
        body = b'''<time datetime="2026-09-07 01:00:00">Today</time><table id="data"><thead>
        <tr><td></td><td colspan="3">PASSING</td><td>MISC</td></tr>
        <tr><th>Player</th><th>YDS</th><th>TDS</th><th>INTS</th><th>FPTS</th></tr></thead><tbody>
        <tr><td><a class="fp-player-link fp-id-123" fp-player-name="Test Player">Test Player</a> BUF</td>
        <td>4,000</td><td>30</td><td>10</td><td>999</td></tr></tbody></table>'''
        rows, unmatched = normalize_fantasypros(body, "QB", 2026, None, self.crosswalk)
        self.assertEqual(rows[0]["mean"], 270)
        self.assertEqual(rows[0]["team"], "BUF")
        self.assertIn("pass_2pt", rows[0]["missing_scoring_fields"])
        self.assertFalse(unmatched)
        with self.assertRaises(ProviderError):
            normalize_fantasypros(body.replace(b'<th>YDS</th>', b'<th>NEW</th>'), "QB", 2026, None, self.crosswalk)

    def test_fantasypros_null_component_is_not_an_explicit_zero(self):
        body = b'''<table id="data"><thead>
        <tr><td></td><td colspan="2">PASSING</td></tr>
        <tr><th>Player</th><th>YDS</th><th>INTS</th></tr></thead><tbody>
        <tr><td><a class="fp-player-link fp-id-123" fp-player-name="Test Player">Test Player</a> BUF</td>
        <td>4,000</td><td>-</td></tr></tbody></table>'''
        scoring = {"pass_yd": .04, "pass_int": -1, "st_td": 6, "fum": 0}
        rows, _ = normalize_fantasypros(body, "QB", 2026, None, self.crosswalk, scoring)
        row = rows[0]
        self.assertEqual(row["mean"], 160)
        self.assertEqual(row["null_scoring_fields"], ["pass_int"])
        self.assertEqual(row["absent_scoring_fields"], ["st_td"])
        self.assertEqual(row["missing_scoring_fields"], ["pass_int", "st_td"])
        self.assertEqual(row["mean_interpretation"], "observed_component_subtotal")
        zero_rows, _ = normalize_fantasypros(body.replace(b'<td>-</td>', b'<td>0</td>'), "QB", 2026, None, self.crosswalk, scoring)
        self.assertEqual(zero_rows[0]["explicit_zero_scoring_fields"], ["pass_int"])
        self.assertEqual(zero_rows[0]["missing_scoring_fields"], ["st_td"])

    def test_old_metadata_cannot_backdate_and_offline_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            warehouse = Warehouse(tmp)
            from moneyball.providers import SOURCE_METADATA
            for sid, data in SOURCE_METADATA.items():
                warehouse.register_source(sid, data)
            for week in (None, 1):
                url = f"{SLEEPER_BASE}/2026" + ("" if week is None else "/1") + "?" + SLEEPER_QUERY
                raw = warehouse.put_raw("sleeper_rotowire", url, json.dumps([sleeper_row(week=week)]).encode(), observed_at=100)
                warehouse.cache_http(url, raw["id"], checked_at=100)
            result = ingest_providers(warehouse, offline=True, include_fantasypros=False, archive_years=())
            self.assertEqual(len([s for s in result["sources"] if s["dataset"] == "projections"]), 2)
            self.assertEqual(warehouse.query("projections", cutoff=50), [])
            self.assertEqual(len(warehouse.query("projections", cutoff=100)), 2)
            again = ingest_providers(warehouse, offline=True, include_fantasypros=False, archive_years=())
            self.assertTrue(all(s["unchanged"] for s in again["sources"]))
            self.assertEqual(len(warehouse.query("projections")), 2)

    def test_undocumented_endpoint_never_networks_by_default_even_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("moneyball.providers.HttpFetcher") as mocked:
                mocked.return_value.fetch.side_effect = RuntimeError("fixture not cached")
                ingest_providers(Warehouse(tmp), force=True, include_fantasypros=False, archive_years=())
                calls = mocked.return_value.fetch.call_args_list
                sleeper_calls = [call for call in calls if call.args[0] == "sleeper_rotowire"]
                self.assertTrue(sleeper_calls)
                self.assertTrue(all(call.kwargs["offline"] for call in sleeper_calls))
                calc_calls = [call for call in calls if call.args[0] == "fantasycalc"]
                self.assertFalse(calc_calls[0].kwargs["force"])


if __name__ == "__main__":
    unittest.main()
