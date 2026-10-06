import unittest
from art_of_the_deal.trade_decision import frame
from art_of_the_deal.trades import compare_trade
from art_of_the_deal.demo import fixture, forecasts


class DecisionSupportTests(unittest.TestCase):
    def test_both_teams_diagnostics_without_automatic_winner(self):
        data = fixture()
        proposal = {"transfers": [{"player_id":"ar", "from_team":"a", "to_team":"b"},
                                  {"player_id":"bx", "from_team":"b", "to_team":"a"}]}
        result = frame(data, compare_trade(data, proposal, forecasts(data)))
        changes = result["cohort_diagnostics"][0]["team_changes"]
        self.assertEqual({r["team_id"] for r in changes}, {"a", "b"})
        self.assertIsNone(result["who_benefits"])
        self.assertIn("strongest reason", result["answer_contract"]["countercase"])
        self.assertIn("uncertainty", result["answer_contract"]["roughly_even"])

    def test_missing_evidence_is_not_equality(self):
        result = frame(fixture(), {"forecast_comparisons": []})
        self.assertEqual(result["cohort_diagnostics"], [])
        self.assertIsNone(result["recommendation"])
        self.assertTrue(any("do not imply equality" in text for text in result["limits"]))
