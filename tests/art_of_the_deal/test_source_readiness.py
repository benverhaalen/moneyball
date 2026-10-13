import tempfile
import time
from types import SimpleNamespace
import unittest

from moneyball.league.source_readiness import prepare_sources, prepare_action_sources, report_coverage, _registered_reports
from moneyball.league.store import Store, digest
from moneyball.league.evidence import EvidenceIndex


class ReadinessTests(unittest.TestCase):
    def test_expanded_action_scope_is_bounded_without_losing_tail_players(self):
        with tempfile.TemporaryDirectory() as home:
            focus = [f'p{i:02}' for i in range(25)]
            result = prepare_action_sources(SimpleNamespace(store=Store(home)), 'a', self.league,
                {'intent': 'lineup'}, [], focus, as_of=self.now)
            self.assertEqual(len(result['checks']), 3)
            self.assertEqual(result['focused_player_ids'], focus)
            missing = [ref for check in result['checks'] for ref in check['reporting']['missing_player_refs']]
            self.assertEqual(missing, ['sleeper:' + pid for pid in focus])
            self.assertTrue(all(len(check['reporting']['missing_player_refs']) <= 12 for check in result['checks']))

    def setUp(self):
        self.now = time.time()
        self.league = {"platform": "sleeper", "season": 2026, "week": 6,
            "format": "redraft", "rules": {"scoring": {"weights": {"rec": 1}}}}
        self.row = {"player_id": "one", "source": "author", "source_url": "https://example.org/points",
            "season": 2026, "period": {"kind": "week", "week": 6}, "points": 10,
            "scoring_hash": digest(self.league["rules"]["scoring"]), "available_at": self.now-100}
        self.report = {"claim": {"player_ref": "sleeper:one", "facet": "availability", "kind": "observation",
            "qualifiers": {"season": 2026, "week": 6}},
            "document": {"source": {"origin": "official", "url": "https://example.org/report"},
                "published_at": self.now-200}, "first_observed_at": self.now-100}

    def test_targeted_report_removes_missing_without_exhaustive_claim(self):
        r = report_coverage([self.report], player_refs=["sleeper:one"], season=2026, week=6, as_of=self.now)
        self.assertEqual(r["status"], "targeted_reports_present")
        self.assertFalse(r["missing_player_refs"])
        self.assertIn("not exhaustive", r["scope"])

    def test_previous_week_is_not_current_week_even_retrieved_today(self):
        report = {**self.report, "claim": {**self.report["claim"], "qualifiers": {"season": 2026, "week": 5}}}
        r = report_coverage([report], player_refs=["sleeper:one"], season=2026, week=6, as_of=self.now)
        self.assertEqual(r["status"], "missing")
        self.assertEqual(r["rejected"][0]["reason"], "report_period_missing_or_mismatched")

    def test_retrieval_without_report_date_does_not_certify_currency(self):
        report = {**self.report, "document": {"source": self.report["document"]["source"], "retrieved_at": self.now}}
        r = report_coverage([report], player_refs=["sleeper:one"], season=2026, week=6, as_of=self.now)
        self.assertEqual(r["rejected"][0]["reason"], "report_date_unknown")

    def test_prepare_retains_native_input_and_weekly_coverage(self):
        with tempfile.TemporaryDirectory() as home:
            service = SimpleNamespace(store=Store(home))
            original = dict(self.row)
            r = prepare_sources(service, "a", self.league, {"intent": "lineup", "player_ids": ["one", "two"]}, [self.row], as_of=self.now, reports=[self.report])
            self.assertEqual(self.row, original)
            self.assertEqual(r["compatible_row_count"], 1)
            self.assertEqual(r["required_periods"], [{"kind": "week", "week": 6}])
            self.assertEqual(r["gaps"][0]["missing"][0]["player_id"], "two")

    def test_dynasty_current_rows_do_not_become_future_odds(self):
        with tempfile.TemporaryDirectory() as home:
            r = prepare_sources(SimpleNamespace(store=Store(home)), "a", {**self.league, "format": "dynasty"},
                {"intent": "trade", "player_ids": ["one"]}, [self.row], as_of=self.now, reports=[self.report])
            self.assertIn("future_projection_evidence_required", [g["code"] for g in r["gaps"]])
            self.assertNotIn("targeted_reporting_missing_or_stale", [g["code"] for g in r["gaps"]])

    def test_registered_report_is_available_only_after_local_ingest(self):
        with tempfile.TemporaryDirectory() as home:
            service = SimpleNamespace(store=Store(home))
            doc = {**self.report["document"], "source": {**self.report["document"]["source"], "capture_policy": "link_only"},
                "coverage": {"season": 2026, "week": 6},
                "claims": [{**self.report["claim"], "qualifiers": [], "id": "claim", "text": "Reported practice participation"}]}
            version = EvidenceIndex(service.store).index_document("report", doc)
            spec = {"intent": "lineup", "player_ids": ["one"]}
            before = prepare_sources(service, "a", self.league, spec, [self.row], as_of=version["first_observed_at"]-.001)
            after = prepare_sources(service, "a", self.league, spec, [self.row], as_of=time.time())
            self.assertEqual(before["reporting"]["status"], "missing")
            self.assertEqual(after["reporting"]["status"], "targeted_reports_present")

    def test_later_migrated_older_revision_does_not_suppress_current_report(self):
        with tempfile.TemporaryDirectory() as home:
            store = Store(home)
            source = {"origin": "official", "url": "https://example.org/report", "capture_policy": "link_only"}
            old = {"source": source, "published_at": self.now-200,
                "coverage": {"season": 2026, "week": 5},
                "claims": [{"id": "old", "player_ref": "sleeper:one", "facet": "availability",
                    "text": "Previous week report", "qualifiers": []}]}
            preserved = store.put("dossier", "sleeper:one", old)
            current = {**old, "coverage": {"season": 2026, "week": 6},
                "claims": [{**old["claims"][0], "id": "new", "text": "Current week report"}]}
            index = EvidenceIndex(store)
            current_version = index.index_document("report", current)
            migrated = {**old, "preserved_store_version": {
                "version_id": preserved["version_id"], "key": "sleeper:one", "sha256": preserved["sha256"]}}
            older_version = index._index_document("report", migrated,
                preserved_record={**preserved, "key": "sleeper:one"})
            self.assertGreater(older_version["document_id"], current_version["document_id"])
            self.assertLess(older_version["first_observed_at"], current_version["first_observed_at"])
            reports = _registered_reports(store, ["sleeper:one"], time.time())
            self.assertEqual([r["claim"]["id"] for r in reports], ["new"])
            covered = report_coverage(reports, player_refs=["sleeper:one"], season=2026, week=6, as_of=time.time())
            self.assertEqual(covered["status"], "targeted_reports_present")

    def test_report_reads_use_existing_pinned_view(self):
        with tempfile.TemporaryDirectory() as home:
            store = Store(home)
            index = EvidenceIndex(store)
            doc = {"source": {"origin": "official", "url": "https://example.org/report", "capture_policy": "link_only"},
                "published_at": self.now-200, "coverage": {"season": 2026, "week": 6},
                "claims": [{"id": "first", "player_ref": "sleeper:one", "facet": "availability",
                    "text": "Current week report", "qualifiers": []}]}
            index.index_document("report", doc)
            with store.read_view(time.time()):
                # A separate canonical writer may publish during preparation;
                # the decision's established read view must not observe it.
                other = Store(home)
                EvidenceIndex(other).index_document("report", {**doc,
                    "claims": [{**doc["claims"][0], "id": "later", "text": "Later change"}]})
                self.assertEqual(_registered_reports(store, ["sleeper:one"], time.time())[0]["claim"]["id"], "first")
            self.assertEqual(_registered_reports(store, ["sleeper:one"], time.time())[0]["claim"]["id"], "later")


if __name__ == "__main__":
    unittest.main()
