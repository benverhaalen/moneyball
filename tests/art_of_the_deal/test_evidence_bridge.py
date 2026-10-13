import tempfile
import unittest
from unittest.mock import patch

from moneyball.league.evidence import EvidenceIndex
from moneyball.league.evidence_bridge import evidence_for_decision, index_manifest_for_decision, migrate_dossiers
from moneyball.league.store import Store, DataError


class EvidenceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.league = {"platform": "sleeper", "league_id": "1", "season": 2026, "rules_hash": "r"}

    def dossier(self, text="He might start, unless the incumbent returns."):
        return {"claims": [{"text": text, "facet": "role", "qualifiers": ["unless incumbent returns"]}],
                "sources": [{"url": "https://example.test/source"}], "evidence_checked_at": 5,
                "full_review": "Coach did not guarantee a role. " + text,
                "reversal_conditions": ["Incumbent returns"]}

    def test_actual_local_dates_and_namespace_and_future_revisions(self):
        with patch("moneyball.league.store.time.time", return_value=100):
            old = self.store.put("dossier", "sleeper:7", self.dossier())
            self.store.put("dossier", "espn:7", self.dossier("Different player."))
        with patch("moneyball.league.store.time.time", return_value=200):
            self.store.put("dossier", "sleeper:7", self.dossier("He no longer starts."))
        result = evidence_for_decision(self.store, self.league, ["7"], 150)
        self.assertEqual(len(result["indexed_cards"]), 1)
        self.assertIn("might start", result["indexed_cards"][0]["claim"]["text"])
        self.assertEqual(result["indexed_cards"][0]["first_observed_at"], 100)
        self.assertEqual(result["manifest_refs"][0]["version_id"], old["version_id"])
        self.assertIn("did not guarantee", result["indexed_cards"][0]["parent"]["text"])
        self.assertEqual(evidence_for_decision(self.store, self.league, ["7"], 50)["indexed_cards"], [])
        current = evidence_for_decision(self.store, self.league, ["7"], 250)
        self.assertIn("no longer", current["indexed_cards"][0]["claim"]["text"])
        self.assertEqual(current["indexed_cards"][0]["first_observed_at"], 200)
        # Re-migrate old history after newer migration: dates, not insertion IDs, select latest.
        evidence_for_decision(self.store, self.league, ["7"], 150)
        self.assertIn("no longer", evidence_for_decision(self.store, self.league, ["7"], 250)["indexed_cards"][0]["claim"]["text"])

    def test_concurrent_migration_cannot_enter_pinned_decision_reads(self):
        # The dossier was already knowable, but a different process materializes
        # its derived index only after our snapshot begins. Timestamps alone do
        # not distinguish this interleaving because migration preserves history.
        self.store.put("dossier", "sleeper:7", self.dossier())
        index = EvidenceIndex(self.store)
        rival = Store(self.tmp.name)
        with self.store.read_view():
            self.assertEqual(index.search(player_refs=["sleeper:7"])["total_matching"], 0)
            migrate_dossiers(rival, ["sleeper:7"])
            self.assertEqual(index.search(player_refs=["sleeper:7"])["total_matching"], 0)
            self.assertEqual(index_manifest_for_decision(self.store, self.league, ["7"]), [])
        self.assertEqual(index.search(player_refs=["sleeper:7"])["total_matching"], 1)

    def test_read_view_does_not_initialize_schema_or_run_migrations(self):
        with self.store.read_view():
            with self.assertRaisesRegex(DataError, "Initialize evidence indexing"):
                EvidenceIndex(self.store)
            with self.assertRaisesRegex(DataError, "before opening a decision read view"):
                migrate_dossiers(self.store, ["sleeper:7"])
        EvidenceIndex(self.store)
        self.store.put("dossier", "sleeper:7", self.dossier())
        migrate_dossiers(self.store, ["sleeper:7"])
        epoch = self.store.version_epoch()
        with self.store.read_view():
            result = evidence_for_decision(self.store, self.league, ["7"])
        self.assertEqual(len(result["indexed_cards"]), 1)
        self.assertEqual(self.store.version_epoch(), epoch)

    def test_big_caveats_require_expansion_without_loss(self):
        dossier = self.dossier()
        caveat = "Important uncertain conditional detail. " * 400
        dossier["claims"][0]["qualifiers"] = [caveat]
        self.store.put("dossier", "sleeper:7", dossier)
        result = evidence_for_decision(self.store, self.league, ["7"])
        card = result["indexed_cards"][0]
        self.assertEqual(card["claim"]["qualifiers_count"], 1)
        expanded = EvidenceIndex(self.store).read(card["hit_id"])
        self.assertEqual(expanded["claim"]["qualifiers"], [caveat])

    def test_manifest_read_only_and_tombstone_invalidation(self):
        index = EvidenceIndex(self.store)
        document = {"source": {"url": "https://example.test", "origin": "team", "capture_policy": "permitted_excerpt"},
                    "claims": [{"id": "x", "text": "A role claim.", "facet": "role", "player_ref": "sleeper:7"}]}
        with patch("moneyball.league.evidence.time.time", return_value=100):
            index.index_document("direct-report", document)
        previous = index_manifest_for_decision(self.store, self.league, ["7"], 150)
        self.assertEqual(len(previous), 1)
        with patch("moneyball.league.evidence.time.time", return_value=200):
            document["claims"] = []
            index.index_document("direct-report", document)
        revised = index_manifest_for_decision(self.store, self.league, ["7"], 250)
        self.assertNotEqual(previous[0]["sha256"], revised[0]["sha256"])
        self.assertEqual(index_manifest_for_decision(self.store, self.league, ["7"], 150), previous)
        self.assertEqual(index_manifest_for_decision(self.store, self.league, ["8"], 250), [])
        with self.store.db() as db:
            before = db.execute("SELECT COUNT(*) FROM evidence_documents").fetchone()[0]
        index_manifest_for_decision(self.store, self.league, ["7"], 250)
        with self.store.db() as db:
            self.assertEqual(before, db.execute("SELECT COUNT(*) FROM evidence_documents").fetchone()[0])

    def test_legacy_truncated_excerpt_restored_from_retained_original(self):
        dossier = self.dossier()
        dossier["archive_import"] = {"kind": "old"}
        dossier["claims"] = [{"text": "He starts", "section": "opening", "truncated": True}]
        complete = "He starts only if the incumbent is unavailable; this is not certain."
        dossier["legacy_evidence"] = {"review_excerpts": {"opening": complete}}
        self.store.put("dossier", "sleeper:7", dossier)
        result = evidence_for_decision(self.store, self.league, ["7"])
        self.assertEqual(result["indexed_cards"][0]["claim"]["text"], complete)
        self.assertIn("Archived dynasty", result["indexed_cards"][0]["claim"]["qualifiers"][-1])


    def test_direct_publish_stamps_after_write_lock_and_epoch_marker(self):
        index = EvidenceIndex(self.store)
        document = {"source": {"url": "https://example.test", "origin": "team", "capture_policy": "permitted_excerpt"},
                    "claims": [{"id": "x", "text": "A role claim.", "facet": "role", "player_ref": "sleeper:7"}]}
        with patch("moneyball.league.evidence.time.time", side_effect=[100, 200]):
            receipt = index.index_document("direct", document)
        self.assertEqual(receipt["first_observed_at"], 200)
        marker = self.store.get("evidence_index_epoch", "direct", as_of=250)
        self.assertEqual(marker["available_at"], 200)
        self.assertEqual(marker["data"]["document_id"], receipt["document_id"])
        self.assertEqual(index.search(as_of=150)["total_matching"], 0)

    def test_missing_and_malformed_claims_stay_partial(self):
        dossier = self.dossier()
        dossier["claims"] = [{"not_text": 3}, {"text": "Unknown classification", "kind": "hunch"}]
        self.store.put("dossier", "sleeper:7", dossier)
        result = evidence_for_decision(self.store, self.league, ["7", "8"])
        self.assertEqual(result["coverage"]["missing_player_refs"], ["sleeper:8"])
        card = result["indexed_cards"][0]
        self.assertEqual(card["claim"]["kind"], "interpretation")
        self.assertIn("hunch", card["claim"]["qualifiers"][0])
        self.assertTrue(card["coverage"]["gaps"])


if __name__ == "__main__":
    unittest.main()
