"""Synthetic provider journeys; no credentials or live leagues are used."""
import copy
import tempfile
import time
import unittest
from unittest.mock import patch

from art_of_the_deal import draft
from art_of_the_deal.service import Service
from art_of_the_deal.store import DataError
from test_service import league


class Reads:
    def __init__(self, values):
        self.values = copy.deepcopy(values)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.values[url]
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value), {"url": url, "checked_at": time.time(), "cache_hit": False, "freshness_limit": "GET is not proof of origin freshness"}


class ConnectedDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service(self.tmp.name)
        self.league, _ = league()
        self.league.update(platform="sleeper", league_id="123")
        self.league["teams"] = {"1": self.league["teams"]["a"], "2": self.league["teams"]["b"]}
        for tid, team in self.league["teams"].items():
            team["id"] = tid
        self.league["own_team_id"] = "1"
        with patch.object(self.service, "_fetch", return_value=(self.league, [], [])):
            self.service.connect("demo", "sleeper", "123", 2026, own_team_id="1")
        self.service.store.put("catalog", "sleeper", {"111": {"full_name": "Synthetic RB", "fantasy_positions": ["RB"]}, "222": {"full_name": "Synthetic QB", "fantasy_positions": ["QB"]}})
        self.info = {"draft_id": "456", "league_id": "123", "season": "2026", "sport": "nfl", "status": "drafting", "type": "snake", "settings": {"teams": 2, "rounds": 2, "pick_timer": 90}, "slot_to_roster_id": {"1": 1, "2": 2}, "draft_order": {"user-a": 1, "user-b": 2}, "last_picked": 1000}
        self.picks = [{"draft_id": "456", "pick_no": 1, "round": 1, "draft_slot": 1, "player_id": "111", "roster_id": "1", "metadata": {"first_name": "Synthetic", "last_name": "RB", "position": "RB"}}]

    def reads(self, *, picks=None, trades=None):
        return Reads({"https://api.sleeper.app/v1/league/123/drafts": [self.info],
                      "https://api.sleeper.app/v1/draft/456": self.info,
                      "https://api.sleeper.app/v1/draft/456/picks": self.picks if picks is None else picks,
                      "https://api.sleeper.app/v1/draft/456/traded_picks": [] if trades is None else trades})

    def packet(self, reads=None, **kwargs):
        with patch("art_of_the_deal.draft.Client", return_value=reads or self.reads()):
            return draft.context(self.service, "demo", **kwargs)

    def test_connected_sleeper_repeated_context_rules_research_shortlist_and_guards(self):
        reads = self.reads(trades=[{"season": "2026", "round": 2, "roster_id": 2, "owner_id": 1}])
        first = self.packet(reads, player_ids=["111", "222", "999"])
        self.assertEqual(first["own_selections"][0]["player_id"], "111")
        self.assertEqual(first["candidates"][0]["availability"], "observed_selected")
        self.assertEqual(first["candidates"][1]["availability"], "not_observed_selected")
        self.assertEqual(first["candidates"][2]["availability"], "unknown_eligibility")
        self.assertEqual(first["exact_rules"], self.league["rules"])
        self.assertEqual(first["strategy"]["research"]["status"], "pending_agent_research")
        self.assertEqual(first["order"]["traded_picks"][0]["owner_id"], 1)
        self.assertIsNone(first["timing"]["live_countdown_seconds"])
        self.assertEqual(len(reads.calls), 7)
        self.assertTrue(all(kwargs["ttl"] == 0 for _, kwargs in reads.calls))
        repeat = self.packet(reads, expected_board_hash=first["board_hash"])
        self.assertEqual(repeat["board_hash"], first["board_hash"])
        self.assertEqual(repeat["prefix_hash"], first["prefix_hash"])
        self.assertEqual(draft.context(self.service, "demo", fresh=False)["board_hash"], first["board_hash"])

    def test_board_guard_detects_same_length_correction_and_coherent_new_board_is_saved(self):
        first = self.packet()
        changed = copy.deepcopy(self.picks)
        changed[0]["player_id"] = "222"
        with self.assertRaisesRegex(DataError, "Draft board changed"):
            self.packet(self.reads(picks=changed), expected_board_hash=first["board_hash"])
        current = draft.context(self.service, "demo", fresh=False)
        self.assertNotEqual(current["prefix_hash"], first["prefix_hash"])
        self.assertEqual(current["recent_picks"][0]["player_id"], "222")

    def test_failed_or_inconsistent_reads_preserve_last_successful_snapshot(self):
        first = self.packet()
        broken = self.reads()
        broken.values["https://api.sleeper.app/v1/draft/456/picks"] = DataError("Source network failed")
        with self.assertRaises(DataError):
            self.packet(broken)
        racing = self.reads()
        responses = iter([self.picks, []])
        racing.values["https://api.sleeper.app/v1/draft/456/picks"] = lambda: next(responses)
        with self.assertRaisesRegex(DataError, "changed during retrieval"):
            self.packet(racing)
        self.assertEqual(draft.context(self.service, "demo", fresh=False)["board_hash"], first["board_hash"])
        self.assertEqual(self.service.store.events()[0]["kind"], "draft_refresh_failed")

    def test_gaps_are_visible_and_duplicate_player_identity_is_rejected(self):
        gap = copy.deepcopy(self.picks)
        gap[0]["pick_no"] = 2
        self.assertFalse(self.packet(self.reads(picks=gap))["pick_sequence_complete"])
        duplicate = copy.deepcopy(self.picks) * 2
        duplicate[1]["pick_no"] = 2
        with self.assertRaisesRegex(DataError, "Duplicate"):
            self.packet(self.reads(picks=duplicate))

    def test_ambiguous_drafts_require_selection_and_selected_draft_is_bound_to_league(self):
        reads = self.reads()
        extra = {**self.info, "draft_id": "789"}
        reads.values["https://api.sleeper.app/v1/league/123/drafts"].append(extra)
        with self.assertRaisesRegex(DataError, "Specify draft_id"):
            self.packet(reads)
        self.assertEqual(self.packet(reads, draft_id="456")["draft_id"], "456")
        reads.values["https://api.sleeper.app/v1/draft/456"]["league_id"] = "999"
        with self.assertRaisesRegex(DataError, "league/season"):
            self.packet(reads, draft_id="456")

    def test_cached_age_and_historical_cutoff_do_not_silently_refresh(self):
        first = self.packet()
        cutoff = time.time()
        with patch("art_of_the_deal.draft.time.time", return_value=time.time() + 31):
            with self.assertRaisesRegex(DataError, "expired"):
                draft.context(self.service, "demo", fresh=False)
            historical = draft.context(self.service, "demo", fresh=False, as_of=cutoff)
            self.assertTrue(historical["historical"])
            self.assertEqual(historical["board_hash"], first["board_hash"])
        with self.assertRaisesRegex(DataError, "cannot fetch"):
            self.packet(as_of=cutoff)

    def test_rule_change_invalidates_cached_board(self):
        self.packet()
        altered = copy.deepcopy(self.league)
        altered["rules"]["bench_slots"] = 5
        self.service._publish("demo", altered, [], [])
        with self.assertRaisesRegex(DataError, "No saved draft board"):
            draft.context(self.service, "demo", fresh=False)

    def test_mismatched_connected_configuration_and_missing_team_are_refused(self):
        config = self.service.store.get("config", "demo")["data"]
        self.service.store.put("config", "demo", {**config, "season": 2027})
        with self.assertRaisesRegex(DataError, "disagree"):
            self.packet()
        self.service.store.put("config", "demo", config)
        data = copy.deepcopy(self.league)
        data["own_team_id"] = None
        self.service._publish("demo", data, [], [])
        with self.assertRaisesRegex(DataError, "own team"):
            self.packet()

    def test_espn_observed_history_does_not_claim_live_clock_or_order(self):
        data = copy.deepcopy(self.league)
        data["platform"] = "espn"
        with patch.object(self.service, "_fetch", return_value=(data, [], [])):
            self.service.connect("espn-demo", "espn", "123", 2026, own_team_id="1")
        url = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/123?view=mDraftDetail"
        raw = {"id": 123, "seasonId": 2026, "draftDetail": {"drafted": True, "picks": [{"playerId": 111, "teamId": 1, "roundId": 1, "roundPickNumber": 1, "keeper": False}, {"playerId": 0, "teamId": 2}]}}
        reads = Reads({url: raw})
        with patch("art_of_the_deal.draft.Client", return_value=reads):
            packet = draft.context(self.service, "espn-demo", player_ids=["111", "222"])
        self.assertEqual(packet["support"], "observed_draft_detail_history_only")
        self.assertEqual(packet["status"], "provider_drafted")
        self.assertEqual(packet["pick_count"], 1)
        self.assertIsNone(packet["order"])
        self.assertIsNone(packet["timing"]["live_countdown_seconds"])
        self.assertEqual(len(reads.calls), 2)
        self.assertEqual(packet["candidates"][1]["availability"], "unknown_eligibility")
        reads.values[url]["seasonId"] = 2025
        with patch("art_of_the_deal.draft.Client", return_value=reads):
            with self.assertRaisesRegex(DataError, "league/season"):
                draft.context(self.service, "espn-demo")
        self.assertEqual(draft.context(self.service, "espn-demo", fresh=False)["board_hash"], packet["board_hash"])

    def test_provider_cache_age_refuses_publication_even_after_completed_get(self):
        first = self.packet()
        reads = self.reads()
        get = reads.get
        def aged(url, **kwargs):
            body, receipt = get(url, **kwargs)
            receipt["headers"] = {"age": "91"}
            return body, receipt
        reads.get = aged
        with self.assertRaisesRegex(DataError, "cache is too old"):
            self.packet(reads)
        self.assertEqual(draft.context(self.service, "demo", fresh=False)["board_hash"], first["board_hash"])

    def test_traded_ownership_change_during_fetch_refuses_publication(self):
        first = self.packet()
        reads = self.reads()
        trades = iter([[], [{"season": "2026", "round": 2, "roster_id": 2, "owner_id": 1}]])
        reads.values["https://api.sleeper.app/v1/draft/456/traded_picks"] = lambda: next(trades)
        with self.assertRaisesRegex(DataError, "changed during retrieval"):
            self.packet(reads)
        self.assertEqual(draft.context(self.service, "demo", fresh=False)["board_hash"], first["board_hash"])

    def test_espn_negative_defense_identity_is_supported(self):
        data = copy.deepcopy(self.league)
        data["platform"] = "espn"
        with patch.object(self.service, "_fetch", return_value=(data, [], [])):
            self.service.connect("espn-demo", "espn", "123", 2026, own_team_id="1")
        url = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/123?view=mDraftDetail"
        raw = {"id": 123, "seasonId": 2026, "draftDetail": {"drafted": True, "picks": [{"playerId": -16001, "teamId": 1, "roundId": 1, "roundPickNumber": 1}]}}
        with patch("art_of_the_deal.draft.Client", return_value=Reads({url: raw})):
            packet = draft.context(self.service, "espn-demo", player_ids=["-16001"])
        self.assertEqual(packet["candidates"][0]["availability"], "observed_selected")
