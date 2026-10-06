import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from art_of_the_deal.legacy import import_moneyball_deck
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError, digest


OLD_CUTOFF = 1_700_000_000.0
OLD_REVIEW = 1_700_000_100.0


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(self.root / "private")

    def tearDown(self):
        self.temp.cleanup()

    def _card(self, sleeper_id="101", espn_id="9001", name="Exact Player"):
        sidecar = {
            "schema_version": 1,
            "player_id": sleeper_id,
            "name": name,
            "known_at": OLD_REVIEW,
            "available_at": OLD_REVIEW,
            "packet": {"information_cutoff": OLD_CUTOFF},
            "primary_sources": [{
                "url": "https://team.example/news/exact-player",
                "title": ["Exact player source"],
                "observed_at": OLD_REVIEW,
                "raw_sha256": "a" * 64,
            }],
            "remaining_gaps": ["Current role is unknown"],
            "reversal_test": "Reverse if the verified role is gone.",
        }
        sidecar_path = self.root / (sleeper_id + ".reviewed.json")
        raw = json.dumps(sidecar, sort_keys=True).encode()
        sidecar_path.write_bytes(raw)
        identity = {
            "player_id": sleeper_id,
            "name": name,
            "position": "WR",
            "external_ids": {"espn_id": espn_id} if espn_id is not None else {},
            "identity_match": ["exact_espn_id"],
        }
        return {
            "player_id": sleeper_id,
            "name": name,
            "position": "WR",
            "identity": identity,
            "packet_path": "/private/legacy-packet.json",
            "packet_sha256": "b" * 64,
            "information_cutoff": OLD_CUTOFF,
            "forecasts": [{
                "provider": "legacy_projection",
                "source_url": "https://projection.example/player",
                "known_at": OLD_CUTOFF,
            }],
            "markets": [],
            "contracts": None,
            "coverage": {"historical_context_record": True},
            "review": {
                "evidence_path": str(sidecar_path),
                "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                "available_at": OLD_REVIEW,
                "status": "Individual research pass; open inputs remain.",
            },
            "review_excerpts": {
                "opening": "An exact old observation, with its original limits.",
                "future": "Future role was explicitly uncertain.",
                "risk": "The reserve path required a named replacement.",
                "reversal": "Refresh role and availability before using this conclusion.",
            },
            "full_review": "# Exact Player\n\nComplete preserved old review.",
            "calibrated_title_delta": None,
        }

    def _deck(self, cards):
        deck = {
            "schema_version": 1,
            "built_at": OLD_REVIEW + 10,
            "packet_build": "legacy-build",
            "policy": {"version": 1, "rules": ["Missing is not zero."]},
            "cards": cards,
        }
        deck["content_hash"] = digest({key: deck[key] for key in ("packet_build", "policy", "cards")})
        path = self.root / "deck.json"
        path.write_text(json.dumps(deck, sort_keys=True))
        return path

    def _league(self, platform, player_id):
        return {
            "platform": platform,
            "league_id": "1",
            "name": "Test",
            "format": "redraft",
            "season": 2026,
            "week": 2,
            "strategy_key": "unused",
            "rules": {"starters": [{"label": "WR", "eligible_positions": ["WR"]}]},
            "teams": {"1": {"id": "1", "name": "A", "player_ids": [player_id],
                              "starters": [player_id], "reserve": [], "taxi": []}},
            "players": {player_id: {"name": "Exact Player", "positions": ["WR"]}},
        }

    def test_old_review_is_available_now_but_remains_stale_and_full_is_explicit(self):
        path = self._deck({"101": self._card()})
        first = import_moneyball_deck(self.service, path)
        self.assertEqual(first["dossier_revisions_written"], 2)
        self.assertEqual(first["sleeper_identities"], 1)
        self.assertEqual(first["espn_identities"], 1)

        saved = self.service.store.get("dossier", "sleeper:101")
        self.assertGreater(saved["available_at"], OLD_REVIEW)
        self.assertEqual(saved["data"]["evidence_checked_at"], OLD_REVIEW)
        self.assertEqual(saved["data"]["archive_import"]["original_information_cutoff"], OLD_CUTOFF)
        self.assertEqual(saved["data"]["archive_import"]["freshness_status"], "archival_not_current")
        self.assertEqual(saved["data"]["sources"][0]["record"]["url"],
                         "https://team.example/news/exact-player")
        self.assertEqual(saved["data"]["identity"]["exact_player_refs"],
                         ["sleeper:101", "espn:9001"])

        self.service.store.put("league", "espn-test", self._league("espn", "9001"))
        compact = self.service.evidence("espn-test", ["9001"])[0]
        self.assertEqual(compact["research_status"], "refresh_required")
        self.assertNotIn("full_review", compact["preserved_dossier"])
        self.assertIn("Archived dynasty draft research", compact["preserved_dossier"]["summary"])
        self.assertNotIn("draft_edge", compact["preserved_dossier"]["summary"])
        expanded = self.service.evidence("espn-test", ["9001"], expanded=True)[0]
        self.assertEqual(expanded["preserved_dossier"]["full_review"],
                         "# Exact Player\n\nComplete preserved old review.")

        with self.service.store.db() as db:
            before = db.execute("SELECT count(*) FROM versions WHERE kind='dossier'").fetchone()[0]
        second = import_moneyball_deck(self.service, path)
        with self.service.store.db() as db:
            after = db.execute("SELECT count(*) FROM versions WHERE kind='dossier'").fetchone()[0]
        self.assertEqual(second["status"], "already_imported")
        self.assertEqual(second["dossier_revisions_written"], 0)
        self.assertEqual(before, after)

    def test_duplicate_exact_espn_identity_fails_before_any_write(self):
        cards = {
            "101": self._card("101", "9001", "Exact Player"),
            "102": self._card("102", "9001", "Second Player"),
        }
        path = self._deck(cards)
        with self.assertRaisesRegex(DataError, "Duplicate ESPN"):
            import_moneyball_deck(self.service, path)
        self.assertEqual(self.service.store.keys("dossier"), [])

    def test_card_key_identity_collision_and_content_tampering_fail_loudly(self):
        card = self._card("101", None)
        path = self._deck({"different": card})
        with self.assertRaisesRegex(DataError, "Card key"):
            import_moneyball_deck(self.service, path)
        self.assertEqual(self.service.store.keys("dossier"), [])

        clean = self._deck({"101": card})
        deck = json.loads(clean.read_text())
        deck["cards"]["101"]["name"] = "Tampered"
        clean.write_text(json.dumps(deck))
        with self.assertRaisesRegex(DataError, "content hash mismatch"):
            import_moneyball_deck(self.service, clean)
        self.assertEqual(self.service.store.keys("dossier"), [])


if __name__ == "__main__":
    unittest.main()
