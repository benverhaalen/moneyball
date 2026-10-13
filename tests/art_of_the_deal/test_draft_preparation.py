"""Meaningful live-readiness discriminators with synthetic saved observations."""
from copy import deepcopy
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.league import decision, draft_preparation
from moneyball.league.demo import fixture, forecasts
from moneyball.league.service import Service
from moneyball.league.store import DataError, digest


class DraftPreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = Service(self.directory.name)
        self.league = fixture()
        self.service._publish('demo', self.league, [], forecasts(self.league))
        self.context = decision.prepare(self.service, {'alias': 'demo', 'question': 'Choose my next dynasty pick',
            'intent': 'draft', 'horizon': '2026 through 2028', 'refresh': False})
        self.board = self.publish_board()

    def publish_board(self, picks=None, observed=None):
        league = self.service.snapshot('demo')['data']
        board = {'draft_id': '123', 'league_identity': {k: league.get(k) for k in
                 ('platform', 'league_id', 'season', 'own_team_id', 'strategy_key')},
                 'picks': picks or [], 'oldest_observed_at': observed or time.time(),
                 'status': 'drafting'}
        board['board_hash'] = digest({k: v for k, v in board.items() if k != 'oldest_observed_at'})
        self.service.store.put('draft_board', 'demo-draft', board, {'synthetic': True})
        return {'draft_id': '123', 'board_hash': board['board_hash']}

    def branches(self):
        return [{'id': 'runner', 'player_id': 'ar', 'rationale': 'Immediate named slot contribution',
            'strategy_rationale': 'Balance immediate contention with future capacity, conditional on role.',
            'risk': 'Workload uncertainty', 'horizon': '2026', 'confidence': 'conditional',
            'strongest_countercase': 'Receiver is more durable',
            'reversal_conditions': ['A reduced workload report'],
            'conditions': [{'key': 'role_maintained', 'statement': 'Recent role is maintained', 'expected': True}]},
            {'id': 'receiver', 'player_id': 'bx', 'rationale': 'Future role option with distinct current cost',
            'strategy_rationale': 'Preserve next-year role upside without pretending title odds are known.',
            'risk': 'Delayed opportunity', 'horizon': '2027-2028', 'confidence': 'conditional',
            'strongest_countercase': 'No role change materializes',
            'reversal_conditions': ['Competition persists']}]

    def save(self):
        return draft_preparation.save(self.service, self.context['decision_id'], self.branches(), self.board)

    def test_context_reset_preserves_nuance_cross_position_and_horizon(self):
        saved = self.save()
        reopened = Service(self.directory.name)
        with patch.object(reopened, 'refresh', side_effect=AssertionError('No network on clock')):
            result = draft_preparation.lookup(reopened, saved['preparation_id'], self.board)
        self.assertEqual(result['first_useful_choice']['rationale'], self.branches()[0]['rationale'])
        self.assertEqual(result['choices'][0]['condition_states'][0]['status'], 'unknown')
        self.assertEqual({c['horizon'] for c in result['choices']}, {'2026', '2027-2028'})
        self.assertEqual(result['choices'][1]['strongest_countercase'], 'No role change materializes')
        self.assertFalse(result['requires_repreparation'])
        self.assertTrue(result['choices'][0]['positions'])

    def test_rival_snipe_promotes_ready_authored_alternative_without_reranking(self):
        saved = self.save()
        board = self.publish_board([{'player_id': 'ar', 'team_id': 'b', 'pick_no': 1}])
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertEqual(result['first_useful_choice']['id'], 'receiver')
        self.assertEqual(result['removed_targets'][0]['player_id'], 'ar')
        self.assertTrue(result['board_changed'])
        self.assertFalse(result['requires_repreparation'])

    def test_own_selection_invalidates_old_roster_reasoning(self):
        saved = self.save()
        board = self.publish_board([{'player_id': 'ar', 'team_id': self.league['own_team_id'], 'pick_no': 1}])
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertIn('own_draft_roster_changed', result['blockers'])
        self.assertIsNone(result['first_useful_choice'])
        self.assertTrue(result['strategy_drift'])

    def test_unknowns_not_converted_to_facts_and_false_conditions_excluded(self):
        saved = self.save()
        result = draft_preparation.lookup(self.service, saved['preparation_id'], self.board,
                                         condition_values={'role_maintained': False})
        self.assertEqual(result['first_useful_choice']['id'], 'receiver')
        self.assertEqual(result['excluded_by_conditions'][0]['condition_states'][0]['status'], 'not_met')
        with self.assertRaises(DataError):
            draft_preparation.lookup(self.service, saved['preparation_id'], self.board,
                                     condition_values={'role_maintained': 'yes'})

    def test_superseded_packet_refused_even_if_caller_forges_picks_and_age(self):
        saved = self.save()
        self.publish_board([{'player_id': 'ar', 'team_id': 'b', 'pick_no': 1}])
        forged = {**self.board, 'picks': [], 'oldest_observed_at': time.time()}
        with self.assertRaisesRegex(DataError, 'superseded'):
            draft_preparation.lookup(self.service, saved['preparation_id'], forged)

    def test_expired_observation_and_deadline_show_first_answer_gap(self):
        saved = self.save()
        board = self.publish_board(observed=time.time() - 40)
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board,
                                         deadline=time.time() - 1)
        self.assertEqual(set(result['blockers']), {'board_expired', 'deadline_expired'})
        self.assertIsNone(result['first_useful_choice'])
        self.assertEqual(len(result['choices']), 2)

    def test_changed_rules_never_reuses_strategy_verdict(self):
        saved = self.save()
        changed = deepcopy(self.league)
        changed['rules']['bench'] = changed['rules'].get('bench', 5) + 1
        self.service._publish('demo', changed, [], forecasts(changed))
        board = self.publish_board()
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertIn('rules_changed', result['blockers'])
        self.assertIsNone(result['first_useful_choice'])

    def test_equal_content_observation_can_reuse_prose_but_new_revision_cannot(self):
        saved = self.save()
        board = self.publish_board()
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertFalse(result['board_changed'])
        self.assertIsNotNone(result['first_useful_choice'])
        decision.resume(self.service, self.context['decision_id'], refresh=False)
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertIn('decision_revision_changed', result['blockers'])
        self.assertIsNone(result['first_useful_choice'])

    def test_changed_evidence_invalidates_even_with_unchanged_board(self):
        saved = self.save()
        changed = forecasts(self.league)
        changed[0]['points'] += 8
        key = self.service.store.keys('forecasts')[0]
        self.service.store.put('forecasts', key, changed, {'synthetic': True})
        result = draft_preparation.lookup(self.service, saved['preparation_id'], self.board)
        self.assertIn('evidence_changed', result['blockers'])

    def test_rewritten_board_prefix_invalidates_rival_only_reasoning(self):
        self.board = self.publish_board([{'player_id': 'ax', 'team_id': 'b', 'pick_no': 1}])
        saved = self.save()
        revised = self.publish_board([{'player_id': 'ay', 'team_id': 'b', 'pick_no': 1}])
        result = draft_preparation.lookup(self.service, saved['preparation_id'], revised)
        self.assertIn('observed_pick_prefix_revised', result['blockers'])
        self.assertIsNone(result['first_useful_choice'])

    def test_all_sniped_returns_empty_honest_slate(self):
        saved = self.save()
        board = self.publish_board([{'player_id': 'ar', 'team_id': 'b', 'pick_no': 1},
                                    {'player_id': 'bx', 'team_id': 'b', 'pick_no': 2}])
        result = draft_preparation.lookup(self.service, saved['preparation_id'], board)
        self.assertIsNone(result['first_useful_choice'])
        self.assertEqual(result['choices'], [])
        self.assertEqual(len(result['removed_targets']), 2)

    def test_missing_saved_timestamp_cannot_be_replaced_by_caller_timestamp(self):
        saved = self.save()
        row = self.service.store.get('draft_board', 'demo-draft')['data']
        row.pop('oldest_observed_at')
        self.service.store.put('draft_board', 'demo-draft', row, {'synthetic': True})
        forged = {**self.board, 'oldest_observed_at': time.time()}
        with self.assertRaisesRegex(DataError, 'missing.*timestamp'):
            draft_preparation.lookup(self.service, saved['preparation_id'], forged)

    def test_future_independent_timestamp_refused_despite_unchanged_content(self):
        saved = self.save()
        board = self.publish_board(observed=time.time() + 120)
        with self.assertRaisesRegex(DataError, 'future dated'):
            draft_preparation.lookup(self.service, saved['preparation_id'], board)

    def test_falsy_nonmapping_conditions_are_not_silently_treated_as_unknown(self):
        saved = self.save()
        for invalid in (False, [], '', 0):
            with self.subTest(value=invalid), self.assertRaises(DataError):
                draft_preparation.lookup(self.service, saved['preparation_id'], self.board,
                                         condition_values=invalid)

    def test_no_nuance_free_branch_and_no_historical_live_preparation(self):
        branch = self.branches()[0]
        branch.pop('strategy_rationale')
        with self.assertRaises(DataError):
            draft_preparation.save(self.service, self.context['decision_id'], [branch], self.board)
        historical = decision.prepare(self.service, {'alias': 'demo', 'question': 'Historical draft',
            'intent': 'draft', 'horizon': '2027', 'as_of': time.time(), 'refresh': False})
        with self.assertRaises(DataError):
            draft_preparation.save(self.service, historical['decision_id'], self.branches(), self.board)


if __name__ == '__main__':
    unittest.main()
