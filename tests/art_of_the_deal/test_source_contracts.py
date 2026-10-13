import tempfile
import time
import unittest

from moneyball.league.source_contracts import normalize_forecast, qualify_forecast, qualified_registry
from moneyball.league.store import Store, DataError, digest


class SourceContractsTests(unittest.TestCase):
    def setUp(self):
        self.league = {"season": 2026, "rules": {"scoring": {"weights": {"rec": 1, "rec_yd": .1}, "nonlinear": [], "unsupported": []}}}
        self.row = {"player_id": "one", "source": "espn", "source_url": "https://example.org/forecast",
            "season": 2026, "period": {"kind": "week", "week": 6}, "points": 12,
            "scoring_hash": digest(self.league["rules"]["scoring"]), "available_at": time.time()-100}

    def test_unspecified_is_not_mean_or_known_conditioning(self):
        c = normalize_forecast(self.row)
        self.assertEqual(c.statistic, "provider_unspecified")
        self.assertIn("availability_conditioning_unknown", c.gaps)
        self.assertTrue(qualify_forecast(c, self.league)["compatible"])
        self.assertFalse(qualify_forecast(c, self.league, require_mean=True)["compatible"])

    def test_exact_scoring_period_and_knowability(self):
        c = normalize_forecast(self.row)
        self.assertFalse(qualify_forecast(c, self.league, period={"kind": "week", "week": 7})["compatible"])
        self.assertFalse(qualify_forecast(c, self.league, as_of=self.row["available_at"]-1)["compatible"])
        other = {**self.league, "rules": {"scoring": {"weights": {"rec": .5}}}}
        self.assertFalse(qualify_forecast(c, other)["compatible"])

    def test_components_require_all_scoring_primitives(self):
        row = {k: v for k, v in self.row.items() if k not in ("points", "scoring_hash")}
        row["components"] = {"rec": 4}
        self.assertFalse(qualify_forecast(normalize_forecast(row), self.league)["compatible"])
        row["components"]["rec_yd"] = 60
        self.assertTrue(qualify_forecast(normalize_forecast(row), self.league)["compatible"])

    def test_gateway_not_author_or_independence(self):
        c = normalize_forecast({**self.row, "source": "fftoday", "gateway": "firecrawl", "forecast_statistic": "median"})
        self.assertEqual(c.origin, "fftoday")
        self.assertEqual(c.gateway, "firecrawl")
        self.assertIsNone(c.dependency_group)
        self.assertFalse(qualify_forecast(c, self.league, require_mean=True)["compatible"])

    def test_invalid_contracts_fail_loudly(self):
        for update in ({"points": float("nan")}, {"points": True}, {"season": True},
                       {"period": {"kind": "week", "week": True}},
                       {"source_url": "https://user:password@example.org/"},
                       {"available_at": time.time()+1000}, {"scoring_hash": "PPR"}):
            with self.subTest(update=update), self.assertRaises(DataError):
                normalize_forecast({**self.row, **update})

    def test_registry_failure_preserves_actual_success_date(self):
        with tempfile.TemporaryDirectory() as home:
            store = Store(home)
            version = store.put("forecasts", "league:espn", [self.row])
            store.event("source_refresh_failed", {"source_id": "espn", "status": "failed"})
            source = qualified_registry(store, "espn")["sources"][0]
            self.assertEqual(source["runtime"]["last_stored_forecast"]["locally_available_at"], version["available_at"])
            self.assertIsNotNone(source["runtime"]["last_labeled_failure"])
            self.assertFalse(source["runtime"]["accuracy_evaluated"])
            earlier = qualified_registry(store, "espn", as_of=version["available_at"]-1)["sources"][0]
            self.assertIsNone(earlier["runtime"]["last_stored_forecast"])


if __name__ == "__main__":
    unittest.main()
