import json
import unittest

from art_of_the_deal.free_agents import fetch_espn_free_agents
from art_of_the_deal.store import DataError


def player(pid, name, position_id, status="FREEAGENT", points=8.0, percent_owned=4.0):
    stats = [] if points is None else [{"seasonId": 2040, "scoringPeriodId": 2, "statSourceId": 1,
                                       "statSplitTypeId": 1, "appliedTotal": points}]
    return {"id": pid, "status": status, "player": {"id": pid, "fullName": name,
            "defaultPositionId": position_id, "eligibleSlots": [20], "injuryStatus": "ACTIVE",
            "proTeamId": 1, "ownership": {"percentOwned": percent_owned, "percentStarted": 1.0}, "stats": stats}}


class FakeClient:
    def __init__(self): self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "kona_player_info" not in url:
            return {"settings": {"acquisitionSettings": {"waiverOrderReset": True}},
                    "status": {"standingsUpdateDate": 999},
                    "teams": [{"id": 4, "name": "Mine", "waiverRank": 2,
                               "record": {"overall": {"wins": 0}},
                               "transactionCounter": {"acquisitions": 1, "acquisitionBudgetSpent": 0}}]}, \
                   {"url": url, "checked_at": 1005, "http_status": 200}
        slot = json.loads(kwargs["headers"]["x-fantasy-filter"])["players"]["filterSlotIds"]["value"][0]
        rows = {0: [player(1, "Available QB", 1, points=0.0)],
                2: [player(2, "Waiver RB", 2, status="WAIVERS", points=None)]}.get(slot, [])
        return {"players": rows}, {"url": url, "checked_at": 1000 + slot, "http_status": 200}


class EspnFreeAgentTests(unittest.TestCase):
    def test_fetches_bounded_position_filters_and_preserves_zero_vs_missing_projection(self):
        client = FakeClient()
        result = fetch_espn_free_agents(client, {"platform": "espn", "league_id": "123", "season": 2040},
                                         {"Cookie": "secret-marker"}, scoring_period_id=2,
                                         positions=["QB", "RB"], limit=5)
        self.assertEqual(len(client.calls), 3)
        filters = [json.loads(call[1]["headers"]["x-fantasy-filter"])["players"] for call in client.calls[:2]]
        self.assertEqual(filters[0]["filterStatus"]["value"], ["FREEAGENT", "WAIVERS"])
        self.assertEqual(filters[0]["filterSlotIds"]["value"], [0])
        self.assertIn("filter", client.calls[0][1]["namespace"])
        by_id = {row["player_id"]: row for row in result["players"]}
        self.assertEqual(by_id["1"]["projected_points"], 0.0)
        self.assertIsNone(by_id["2"]["projected_points"])
        self.assertEqual(by_id["2"]["acquisition_route"], "provider_status_waivers")
        self.assertIsNone(by_id["1"]["execution_actionable"])
        self.assertIn("waiver_process_at_raw", by_id["2"])
        self.assertNotIn("secret-marker", json.dumps(result))
        self.assertFalse(result["scope"]["mutation"])
        self.assertTrue(result["waiver_state"]["reset_each_week"])
        self.assertIsNone(result["waiver_state"]["next_reset_rank"])

    def test_rejects_invalid_identity_period_position_and_limits_before_network(self):
        good = {"platform": "espn", "league_id": "123", "season": 2040}
        cases = [
            ({**good, "league_id": "12?x=1"}, {"scoring_period_id": 2}),
            (good, {"scoring_period_id": 0}),
            (good, {"scoring_period_id": 2, "positions": ["DST"]}),
            (good, {"scoring_period_id": 2, "limit": 51}),
        ]
        for config, kwargs in cases:
            with self.assertRaises(DataError):
                fetch_espn_free_agents(None, config, {}, **kwargs)

    def test_rejects_owned_rows_instead_of_claiming_availability(self):
        class Owned(FakeClient):
            def get(self, url, **kwargs):
                return {"players": [player(1, "Owned", 1, status="ONTEAM")]}, {"checked_at": 1}
        with self.assertRaises(DataError):
            fetch_espn_free_agents(Owned(), {"platform": "espn", "league_id": "123", "season": 2040},
                                   {}, scoring_period_id=2, positions=["QB"], include_waiver_state=False)

    def test_cache_namespace_covers_limit_and_zero_percent_owned_is_not_missing(self):
        class OwnershipClient(FakeClient):
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return {"players": [
                    player(2, "Missing ownership", 1, percent_owned=None),
                    player(1, "Zero ownership", 1, percent_owned=0.0),
                ]}, {"checked_at": 1}

        client = OwnershipClient()
        config = {"platform": "espn", "league_id": "123", "season": 2040}
        small = fetch_espn_free_agents(client, config, {}, scoring_period_id=2,
                                        positions=["QB"], limit=2, include_waiver_state=False)
        small_namespace = client.calls[-1][1]["namespace"]
        fetch_espn_free_agents(client, config, {}, scoring_period_id=2,
                               positions=["QB"], limit=3, include_waiver_state=False)
        large_namespace = client.calls[-1][1]["namespace"]
        self.assertNotEqual(small_namespace, large_namespace)
        self.assertEqual([row["player_id"] for row in small["players"]], ["1", "2"])
        self.assertEqual(small["players"][0]["percent_owned"], 0.0)
        self.assertIsNone(small["players"][1]["percent_owned"])


if __name__ == "__main__":
    unittest.main()
