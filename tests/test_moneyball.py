import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from moneyball.store import Store, digest
from moneyball.sleeper import Client, DataError, context, league_week, sync


class MoneyballTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.league = {"league_id": "123", "season": "2026", "name": "Test", "status": "pre_draft", "settings": {"leg": 1}, "scoring_settings": {"rec": 1}, "roster_positions": ["QB", "BN"]}
        self.config = {"league_id": "123", "season": "2026", "user_id": "456"}
        self.responses = {"league/123": self.league, "state/nfl": {"season": "2026", "season_type": "regular", "leg": 1},
            "league/123/rosters": [{"roster_id": 7, "owner_id": "456", "players": None, "starters": ["0"], "taxi": None, "reserve": None}],
            "league/123/users": [], "league/123/drafts": [{"draft_id": "789"}],
            "draft/789": {"draft_id": "789", "status": "pre_draft", "settings": {}, "draft_order": None},
            "draft/789/picks": [], "draft/789/traded_picks": []}

    def client(self, **kwargs):
        return Client(self.store, transport=lambda p: copy.deepcopy(self.responses.get(p, [])), **kwargs)

    def test_cache_avoids_network_and_can_read_offline(self):
        first = self.client()
        first.get("state/nfl")
        c = Client(self.store, transport=lambda p: self.fail("Unexpected network"))
        self.assertEqual(c.get("state/nfl")["season"], "2026")
        self.assertTrue(c.evidence["state/nfl"]["cache_hit"])
        self.assertEqual(Client(self.store, offline=True).get("state/nfl", ttl=0)["season"], "2026")
        with self.assertRaises(DataError):
            Client(self.store, offline=True).get("user/missing")

    def test_stale_network_failure_is_not_silently_fresh(self):
        self.client().get("state/nfl")
        def fail(path):
            raise DataError("offline")
        with self.assertRaises(DataError):
            Client(self.store, force=True, transport=fail).get("state/nfl")

    def test_shape_drift_does_not_overwrite_good_cache(self):
        self.client().get("league/123")
        with self.assertRaises(DataError):
            Client(self.store, force=True, transport=lambda p: {"error": "bad"}).get("league/123")
        self.assertEqual(self.store.cached("league/123")["data"], self.league)

    def test_endpoint_allowlist(self):
        for path in ("https://evil.example/", "../user/456", "league/123/rosters?token=abc", "league/123/transactions/100"):
            with self.assertRaises(DataError):
                self.client().get(path)

    def test_snapshot_unchanged_and_predraft_null_roster(self):
        first = sync(self.client(), self.config, "test")
        second = sync(self.client(), self.config, "test")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["network_requests"], 0)
        result = context(self.store, "test", self.config)
        self.assertEqual(result["team"]["bench"], [])
        self.assertIsNone(result["drafts"][0]["my_slot"])

    def test_full_backfill_survives_incremental_update(self):
        self.responses["league/123/matchups/3"] = [{"roster_id": 7, "points": 100}]
        sync(self.client(), self.config, "test", full=True)
        sync(self.client(), self.config, "test")
        self.assertEqual(self.store.latest("test")["data"]["matchups"]["3"][0]["points"], 100)

    def test_wrong_season_and_missing_owner_refuse_snapshot(self):
        with self.assertRaises(DataError):
            sync(self.client(), {**self.config, "season": "2027"}, "test")
        with self.assertRaises(DataError):
            sync(self.client(), {**self.config, "user_id": "wrong"}, "test")
        self.assertIsNone(self.store.latest("test"))

    def test_co_owner_and_bench_excludes_taxi_ir(self):
        self.responses["league/123/rosters"] = [{"roster_id": 7, "owner_id": "other", "co_owners": ["456"], "players": ["a", "b", "c", "d"], "starters": ["a"], "reserve": ["c"], "taxi": ["d"]}]
        sync(self.client(), self.config, "test")
        result = context(self.store, "test", self.config)
        self.assertEqual(result["team"]["bench"], [{"id": "b"}])

    def test_rollover_uses_2026_not_calendar_year_or_league_season(self):
        state = {"season": "2026", "league_season": "2027", "season_type": "regular", "leg": 18}
        self.assertEqual(league_week(self.league, state), 18)
        self.assertEqual(league_week(self.league, {"season": "2027", "season_type": "pre", "week": 3}), 1)

    def test_players_daily_even_when_fresh(self):
        self.store.cache("players/nfl", {"a": {"full_name": "Alpha", "position": "QB", "team": "GB"}})
        c = Client(self.store, force=True, transport=lambda p: self.fail("Must not redownload players"))
        c.get("players/nfl", 86400)
        self.assertEqual(self.store.player("a")["name"], "Alpha")

    def test_cache_corruption_detected(self):
        self.client().get("state/nfl")
        with self.store.connect() as db:
            db.execute("UPDATE responses SET body=? WHERE path=?", ('{"season":"wrong"}', "state/nfl"))
        with self.assertRaises(DataError):
            self.client().get("state/nfl")

    def test_partial_failure_keeps_previous_complete_snapshot(self):
        sync(self.client(), self.config, "test")
        before = self.store.latest("test")
        def transport(path):
            if path == "draft/789/picks":
                raise DataError("failed")
            return copy.deepcopy(self.responses.get(path, []))
        with self.assertRaises(DataError):
            sync(Client(self.store, force=True, transport=transport), self.config, "test")
        self.assertEqual(self.store.latest("test"), before)


if __name__ == "__main__":
    unittest.main()
