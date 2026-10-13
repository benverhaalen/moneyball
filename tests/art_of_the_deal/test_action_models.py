import copy
import unittest
from unittest.mock import patch

from moneyball.league.action_models import lineup_action, draft_pick_action
from moneyball.league.store import DataError
from test_trades import league, rows, player


class FakeService:
    def __init__(self):
        self.data = league()
        self.data.update(own_team_id='a', name='Synthetic')
        self.data['players']['ax'] = player('AX', 'WR')
        self.data['teams']['a']['player_ids'].append('ax')
        self.values = {'aq': 10, 'ar': 8, 'aw': 12, 'ax': 3}
    def snapshot(self, alias, **kwargs):
        return {'data': copy.deepcopy(self.data), 'sha256': 'snapshot', 'available_at': 10}
    def forecast_rows(self, alias, cutoff):
        return rows(self.data, self.values)
    def strategy(self, alias, **kwargs):
        return {}
    def _team_view(self, data, tid):
        return data['teams'][tid]


class ActionModelsTests(unittest.TestCase):
    def test_explicit_lineup_is_not_optimized_hold(self):
        service = FakeService()
        result = lineup_action(service, 'x', {'slot_player_ids': ['aq', 'ar', 'ax']}, as_of=20)
        row = result['forecast_comparisons'][0]
        self.assertEqual(row['named_after']['selected_player_ids'], ['aq', 'ar', 'ax'])
        self.assertEqual(row['delta_points_vs_optimized_hold'], -9)
        self.assertFalse(result['legality']['lock_state_verified'])
        self.assertIsNone(result['legality']['platform_executable'])

    def test_assignment_cannot_duplicate_or_bypass_slot_or_reserve(self):
        service = FakeService()
        for chosen in [['aq', 'aq', 'ax'], ['ar', 'aq', 'ax'], ['aq', 'ar']]:
            with self.assertRaises(DataError):
                lineup_action(service, 'x', {'slot_player_ids': chosen}, as_of=20)
        service.data['teams']['a']['reserve'] = ['ax']
        with self.assertRaises(DataError):
            lineup_action(service, 'x', {'slot_player_ids': ['aq', 'ar', 'ax']}, as_of=20)

    def test_missing_bench_forecast_blocks_optimized_comparison(self):
        service = FakeService()
        del service.values['aw']
        result = lineup_action(service, 'x', {'slot_player_ids': ['aq', 'ar', 'ax']}, as_of=20)
        self.assertIsNone(result['forecast_comparisons'][0]['named_after'])
        self.assertEqual(result['forecast_comparisons'][0]['missing_required_player_ids'], ['aw'])

    def board(self):
        return {'board_hash': 'b', 'pick_sequence_complete': True,
                'own_selections': [{'player_id': p} for p in ['aq', 'ar', 'aw', 'ax']],
                'candidates': [{'player_id': 'new', 'availability': 'not_observed_selected'}]}

    @patch('moneyball.league.draft.context')
    def test_draft_addition_preserves_roster_and_reconciles_observations(self, context):
        service = FakeService()
        service.data['players']['new'] = player('New', 'WR')
        service.values['new'] = 17
        context.return_value = self.board()
        result = draft_pick_action(service, 'x', {'player_id': 'new', 'board_hash': 'b'}, as_of=20)
        self.assertTrue(result['draft_roster_reconciled'])
        self.assertIn('new', result['after_roster']['player_ids'])
        self.assertNotIn('new', result['before_roster']['player_ids'])
        self.assertEqual(result['forecast_comparisons'][0]['delta_points'], 5)
        self.assertIsNone(result['legality']['platform_executable'])
        self.assertIsNone(result['championship_probability_delta'])
        self.assertEqual(context.call_args.kwargs['expected_board_hash'], 'b')

    @patch('moneyball.league.draft.context')
    def test_draft_roster_mismatch_is_not_union_and_capacity_is_explicit(self, context):
        service = FakeService()
        service.data['players']['new'] = player('New', 'WR')
        service.data['rules']['bench_slots'] = 1
        board = self.board()
        board['own_selections'].append({'player_id': 'different'})
        context.return_value = board
        result = draft_pick_action(service, 'x', {'player_id': 'new', 'board_hash': 'b'}, as_of=20)
        self.assertFalse(result['draft_roster_reconciled'])
        self.assertNotIn('different', result['after_roster']['player_ids'])
        self.assertEqual(result['capacity']['required_drop'], 1)

    @patch('moneyball.league.draft.context')
    def test_selected_owned_and_unbound_draft_candidates_refused(self, context):
        service = FakeService()
        service.data['players']['new'] = player('New', 'WR')
        context.return_value = self.board()
        with self.assertRaises(DataError):
            draft_pick_action(service, 'x', {'player_id': 'new'}, as_of=20)
        context.return_value['candidates'][0]['availability'] = 'observed_selected'
        with self.assertRaises(DataError):
            draft_pick_action(service, 'x', {'player_id': 'new', 'board_hash': 'b'}, as_of=20)
        context.return_value = self.board()
        service.data['teams']['b']['player_ids'].append('new')
        with self.assertRaises(DataError):
            draft_pick_action(service, 'x', {'player_id': 'new', 'board_hash': 'b'}, as_of=20)
