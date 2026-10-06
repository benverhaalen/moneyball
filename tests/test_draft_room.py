import copy
import unittest

from moneyball.draft_room import POLICY, REASONS, board_state, check, compare, prepare
from moneyball.store import digest


def fixture():
    cards = {str(i): {'player_id': str(i), 'name': 'Player '+str(i),
        'position': 'WR', 'identity': {'adp': i, 'adp_type': 'adp_dynasty_2qb'},
        'forecasts': [], 'review': None, 'review_excerpts': {},
        'full_review': '', 'packet_path': '/unused'} for i in range(1, 30)}
    deck = {'cards': cards, 'policy': POLICY, 'content_hash': 'fixture'}
    data = {'league': {'roster_positions': ['QB', 'WR', 'FLEX', 'BN'],
                      'settings': {}, 'scoring_settings': {'rec': 1}},
            'my_roster_id': 7, 'drafts': [{'info': {'draft_id': 'mock',
                'status': 'drafting', 'type': 'snake',
                'settings': {'teams': 12, 'rounds': 28},
                'slot_to_roster_id': {'5': 7}}, 'traded_picks': [],
                'picks': [{'player_id': str(i), 'pick_no': i, 'draft_slot': i}
                          for i in range(1, 5)]}]}
    board = board_state(data, checked_at=100)
    choice = {**{k: 'An explicit five word reasoning explanation.' for k in REASONS},
              'player_id': '5', 'alternative_id': '6', 'requires_available': ['7']}
    return deck, data, board, {'target_pick': 5, 'choices': [choice]}


class DraftRoomTest(unittest.TestCase):
    def test_snake_turns_and_unknown_universe_pick(self):
        _, data, board, _ = fixture()
        self.assertEqual(board['next_own_pick'], 5)
        self.assertEqual(board['following_own_pick'], 20)
        self.assertTrue(board['on_clock'])
        data['drafts'][0]['picks'][0]['player_id'] = 'outside-research-cohort'
        self.assertIn('outside-research-cohort', board_state(data)['taken'])

    def test_pre_draft_is_not_on_clock(self):
        _, data, _, _ = fixture()
        data['drafts'][0]['info']['status'] = 'pre_draft'
        self.assertFalse(board_state(data)['on_clock'])

    def test_duplicate_and_ownership_errors_are_loud(self):
        _, data, _, _ = fixture()
        data['drafts'][0]['picks'][1]['player_id'] = '1'
        with self.assertRaises(ValueError): board_state(data)
        _, data, _, _ = fixture()
        data['drafts'][0]['picks'][1]['draft_slot'] = 10
        with self.assertRaises(ValueError): board_state(data)

    def test_prepared_plan_survives_irrelevant_pick_but_not_lost_dependency(self):
        deck, _, board, raw = fixture()
        plan = prepare(raw, deck, board)
        self.assertTrue(check(plan, deck, board, now=101)['ready_to_execute'])
        # Prefix updates are expected between picks; stated dependencies govern reuse.
        changed = {**board, 'prefix_hash': 'new', 'taken': board['taken'] + ['28']}
        self.assertTrue(check(plan, deck, changed, now=101)['ready_to_execute'])
        changed['taken'].append('7')
        self.assertIn('all_prepared_choices_or_dependencies_gone', check(plan, deck, changed, now=101)['problems'])

    def test_evidence_rules_roster_freshness_and_policy_invalidate(self):
        deck, _, board, raw = fixture(); plan = prepare(raw, deck, board)
        self.assertIn('board_not_fresh', check(plan, deck, board, now=116)['problems'])
        self.assertIn('board_not_fresh', check(plan, deck, board, now=90)['problems'])
        self.assertIn('rules_hash_changed', check(plan, deck, {**board, 'rules_hash': 'new'}, now=101)['problems'])
        self.assertIn('own_roster_changed', check(plan, deck, {**board, 'own_roster': ['22']}, now=101)['problems'])
        modified = copy.deepcopy(deck); modified['cards']['5']['full_review'] = 'updated injury report'
        self.assertIn('evidence_changed:5', check(plan, modified, board, now=101)['problems'])
        modified = copy.deepcopy(deck); modified['policy']['version'] = 2
        self.assertIn('strategy_changed', check(plan, modified, board, now=101)['problems'])

    def test_missing_future_reason_does_not_pass_as_dynasty(self):
        deck, _, board, raw = fixture()
        del raw['choices'][0]['future_pathway']
        with self.assertRaises(ValueError): prepare(raw, deck, board)

    def test_comparison_keeps_order_and_unknowns_without_ranking(self):
        deck, _, board, _ = fixture()
        output = compare(deck, board, ['7', '5'])
        self.assertEqual([c['player_id'] for c in output['candidates']], ['7', '5'])
        self.assertIsNone(output['recommendation'])
        self.assertTrue(all(c['calibrated_title_delta'] is None for c in output['candidates']))
        self.assertEqual(output['candidates'][0]['forecast_intermediates'], [])
        with self.assertRaises(ValueError): compare(deck, board, ['1'])

    def test_compact_subtotal_keeps_missingness_and_exposure(self):
        deck, _, board, _ = fixture()
        deck['cards']['5']['forecasts'] = [{'provider':'example','observed_core_subtotal':101.2,
            'stats':{'games':14,'rec':30},'unreported_scoring_fields':['fum_lost','st_td'],
            'source_conditioning':'Regular season including Week 18, conditional on stated games.'}]
        row = compare(deck,board,['5'])['candidates'][0]['forecast_intermediates'][0]
        self.assertEqual(row['unreported_scoring_fields'],['fum_lost','st_td'])
        self.assertEqual(row['reported_exposure'],{'games':14})
        self.assertIn('Week 18',row['source_conditioning'])
        self.assertFalse(row['is_complete_league_projection'])


if __name__ == '__main__':
    unittest.main()
