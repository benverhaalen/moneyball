import copy
import unittest

from moneyball.draft_room import live_check, prepare
from tests.test_draft_room import fixture


class RenderedBoardTest(unittest.TestCase):
    def setUp(self):
        self.deck, _, self.board, raw = fixture()
        self.plan = prepare(raw, self.deck, self.board)
        self.ui = {'source': 'sleeper_rendered_ui', 'observed_at': 100,
                   'draft_id': 'mock', 'taken_player_ids': list(self.board['taken']),
                   'own_roster': [], 'current_pick': 5, 'on_clock': True,
                   'auto_pick': False}

    def run_check(self, ui):
        return live_check(self.plan, self.deck, self.board, ui, now=101)

    def test_recent_api_fetch_cannot_authorize_without_ui(self):
        result = self.run_check(None)
        self.assertFalse(result['ready_to_execute'])
        self.assertIsNone(result['first_viable_prepared_choice'])

    def test_identical_current_observation_passes(self):
        self.assertTrue(self.run_check(self.ui)['ready_to_execute'])

    def test_same_count_different_player_is_not_current_prefix(self):
        self.ui['taken_player_ids'][0] = '29'
        self.assertIn('rendered_board_mismatch:taken_player_ids', self.run_check(self.ui)['problems'])

    def test_newer_ui_turn_rejects_cached_api(self):
        self.ui['current_pick'] = 6
        self.assertFalse(self.run_check(self.ui)['ready_to_execute'])

    def test_wrong_room_roster_mode_and_turn_are_rejected(self):
        for key, value in [('draft_id', 'real'), ('own_roster', ['20']),
                           ('auto_pick', True), ('auto_pick', None), ('on_clock', False)]:
            with self.subTest(key=key, value=value):
                ui = copy.deepcopy(self.ui); ui[key] = value
                self.assertFalse(self.run_check(ui)['ready_to_execute'])

    def test_old_future_and_non_numeric_observation_times_rejected(self):
        for value in (0, 102, None, True, '100', float('nan')):
            with self.subTest(value=value):
                ui = copy.deepcopy(self.ui); ui['observed_at'] = value
                self.assertFalse(self.run_check(ui)['ready_to_execute'])


if __name__ == '__main__':
    unittest.main()
