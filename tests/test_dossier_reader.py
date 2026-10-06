import copy
import unittest

from moneyball.dossier_reader import decision_brief


class DecisionBriefTest(unittest.TestCase):
    def test_connected_league_scoring_replaces_reference_profile(self):
        packet = {'league_configuration': {'scoring_settings': {'pass_int':-3, 'rec':.5}},
                  'professional_forecasts': {'clay':[{'stats':{'pass_int':2,'rec':4,'pass_yd':100}}]}}
        brief = decision_brief(packet)
        self.assertEqual(brief['professional_forecasts'][0]['observed_core_subtotal'], -4)
        self.assertEqual(brief['scoring_scope'], 'observed_league_configuration')
        self.assertEqual(decision_brief({})['scoring_scope'], 'experimental_reference_profile')

    def test_forecast_scoring_retains_negative_and_zero_fields(self):
        packet = {'identity': {'player_id': 'a'}, 'professional_forecasts': {
            'clay': [{'stats': {'pass_yd': 100, 'pass_td': 0, 'pass_int': 2,
                               'rush_yd': -3, 'rec': 0, 'games': 6}}]}}
        before = copy.deepcopy(packet)
        result = decision_brief(packet)['professional_forecasts'][0]
        self.assertAlmostEqual(result['observed_core_subtotal'], 1.7)
        self.assertEqual(result['stats']['games'], 6)
        self.assertEqual(result['stats']['pass_td'], 0)
        self.assertIn('fum_lost', result['unreported_scoring_fields'])
        self.assertEqual(packet, before)

    def test_preview_definitions_and_actual_cash_are_not_reinterpreted(self):
        preview = [{'median': 150, 'injury_prob': '47%',
                    'floor_definition': 'barring injury'}]
        annual = {'2028': [{'cash_paid': 7.3, 'guaranteed_salary': 0}]}
        result = decision_brief({'provider_references': {'rank_preview': preview},
                                 'contract_context': {'annual_rows_2026_2028': annual}})
        self.assertEqual(result['provider_references']['rank_preview'], preview)
        self.assertEqual(result['contract_context']['annual_rows_2026_2028'], annual)

    def test_unknown_history_is_not_filled(self):
        result = decision_brief({})
        self.assertEqual(result['historical_player_context']['seasons'], {})
        self.assertEqual(result['professional_forecasts'], [])
        self.assertIsNone(result['contract_context']['remaining_guaranteed_cash'])

    def test_weekly_rows_preserve_team_and_vintage_without_merging_season(self):
        row = {'stats': {'rec': 2, 'rec_yd': 30}, 'week': 18, 'team': 'OLD',
               'opponent': 'BUF', 'provider_updated_at': 123,
               '_provenance': {'available_at': 456}}
        packet = {'professional_forecasts': {'weekly': [row]}}
        result = decision_brief(packet)
        self.assertEqual(result['professional_forecasts'], [])
        weekly = result['weekly_professional_forecasts']
        self.assertEqual(weekly['count'], 1)
        self.assertEqual(weekly['rows'][0]['team'], 'OLD')
        self.assertEqual(weekly['rows'][0]['week'], 18)
        self.assertEqual(weekly['rows'][0]['provenance']['available_at'], 456)
        self.assertEqual(weekly['rows'][0]['observed_core_subtotal'], 5)


if __name__ == '__main__':
    unittest.main()
