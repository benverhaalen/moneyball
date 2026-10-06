import os
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal import pipelines
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError
from test_service import league


class ConnectedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("MONEYBALL_LAB_DIR", None)
        self.service = Service(self.tmp.name)
        self.w = pipelines.warehouse(self.service)
        data, _ = league()
        data["platform"] = "sleeper"
        data["players"]["aq"]["external_ids"] = {"gsis_id": "GSIS-Q", "espn_id": "101"}
        self.service.store.put("league", "x", data)

    def publish(self, rows, observed=None):
        raw = self.w.put_raw("fixture", "https://example.test/data", b"synthetic", observed_at=observed)
        return self.w.publish("fixture", "weekly", rows, raw_id=raw["id"], key_fields=("id",))

    def test_catalog_exposes_terms_without_network_or_claiming_acquisition(self):
        result = pipelines.catalog(self.service)
        self.assertTrue(result["datasets"])
        self.assertTrue(result["registered_sources"])
        self.assertFalse(any(d["acquired"] for d in result["datasets"]))
        self.assertTrue(all(s.get("license_status") for s in result["registered_sources"]))

    def test_focused_query_preserves_namespace_cutoff_and_bound(self):
        self.publish([
            {"id": "right", "player_id": "GSIS-Q", "season": 2026},
            {"id": "also-right", "espn_id": "101", "season": 2026},
            {"id": "wrong-namespace", "player_id": "101", "season": 2026},
        ])
        result = pipelines.query(self.service, "weekly", seasons=[2026], alias="x", player_ids=["aq"], limit=1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["more_available"])
        all_rows = pipelines.query(self.service, "weekly", seasons=[2026], alias="x", player_ids=["aq"])["rows"]
        self.assertEqual({r["id"] for r in all_rows}, {"right", "also-right"})
        self.assertTrue(all(r["_provenance"]["available_at"] <= result["evidence_as_of"] for r in all_rows))
        self.assertFalse(pipelines.query(self.service, "weekly", seasons=[2026], as_of=1)["rows"])

    def test_invalid_or_unknown_inputs_do_not_query_unbounded(self):
        for kw in ({"seasons": []}, {"seasons": [True]}, {"seasons": [2026], "limit": True},
                   {"seasons": [2026], "as_of": time.time()+120},
                   {"seasons": [2026], "player_ids": ["aq"]},
                   {"seasons": [2026], "alias": "x", "player_ids": ["unknown"]}):
            with self.assertRaises(DataError):
                pipelines.query(self.service, "weekly", **kw)

    def test_sync_uses_existing_adapter_and_explicit_offline_failure(self):
        result = pipelines.sync(self.service, ["weekly"], [2026], offline=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["results"][0]["failures"])
        self.assertFalse(self.w.query("weekly"))
        with self.assertRaises(DataError):
            pipelines.sync(self.service, ["unlicensed-invented"], [2026])
