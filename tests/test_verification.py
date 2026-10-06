import copy
import tempfile
import unittest
from unittest.mock import patch
from moneyball.store import Store
from moneyball.verification import check_team
from moneyball.sleeper import DataError
from moneyball.cli import ensure_players


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = {"league_id": "123", "user_id": "456"}
        self.state = {"league_id": "123", "roster_id": 7, "players": ["a", "b"], "starters": ["a", "b"], "taxi": [], "reserve": []}

    def test_starter_order_mismatch_fails(self):
        expected = copy.deepcopy(self.state)
        expected["starters"] = ["b", "a"]
        with patch("moneyball.verification.team_state", return_value=self.state):
            result = check_team(self.store, self.config, expected)
        self.assertFalse(result["ok"])
        self.assertIn("starters", result["differences"])

    def test_roster_membership_order_does_not_matter(self):
        expected = copy.deepcopy(self.state)
        expected["players"] = ["b", "a"]
        with patch("moneyball.verification.team_state", return_value=self.state):
            self.assertTrue(check_team(self.store, self.config, expected)["ok"])

    def test_wrong_league_cannot_verify(self):
        with self.assertRaises(DataError):
            check_team(self.store, self.config, {**self.state, "league_id": "999"})

    def test_warm_player_search_does_not_parse_full_catalog(self):
        self.store.cache("players/nfl", {"a": {"full_name": "Alpha"}})
        with patch.object(self.store, "cached", side_effect=AssertionError("Parsed full catalog")):
            self.assertTrue(ensure_players(self.store)["cache_hit"])
