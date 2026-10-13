"""Whole-question examples and real stdio transport, exclusively invented data."""
import asyncio
import importlib.util
import unittest

from moneyball.league.decision_demo import run, run_mcp


class DecisionDemoTests(unittest.TestCase):
    def test_unsupported_provider_refresh_preserves_conditional_context(self):
        import tempfile
        from unittest.mock import patch
        from moneyball.league.decision_demo import seed
        from moneyball.league.service import Service
        with tempfile.TemporaryDirectory() as home:
            service = Service(home)
            seed(service)
            before = service.snapshot('demo')
            with patch('moneyball.league.service.Client') as client:
                context = service.prepare_decision({'alias': 'demo', 'question': 'Plan Tuesday waivers',
                                                   'intent': 'waiver'})
                client.assert_not_called()
            self.assertEqual(context['refresh']['status'], 'failed')
            self.assertEqual(context['snapshot']['available_at'], before['available_at'])
            self.assertIn('league_refresh_failed', {g['code'] for g in context['gaps']})
            from moneyball.league.operations import acquisition_context
            from moneyball.league.store import DataError
            with self.assertRaisesRegex(DataError, 'No live acquisition adapter'):
                acquisition_context(service, 'demo')
            self.assertEqual(service.store.get('pool', 'demo')['data']['players'][0]['player_id'], 'free')

    @classmethod
    def setUpClass(cls):
        cls.result = run()

    def test_complete_waiver_cut_and_ordered_contingent_branches(self):
        journey = self.result['journeys'][0]
        add, ordered = journey['comparison']['actions'][1:]
        pickup = add['accounting']['comparison']
        self.assertTrue(pickup['valid'])
        forecast = pickup['forecast_comparisons'][0]
        self.assertEqual(forecast['delta_points'], 2)
        self.assertNotIn('ax', forecast['after']['selected_player_ids'])
        self.assertIn('free', forecast['after']['selected_player_ids'])
        claims = ordered['accounting']['comparison']
        self.assertEqual(claims['declared_preference_order'], ['free', 'fallback'])
        self.assertIsNone(claims['claim_probabilities'])
        self.assertIn('Independent conditional branches', claims['execution_semantics'])
        self.assertEqual([s['conditional_on_this_target_being_acquired']['claim_cost']['faab_budget_after_if_success']
                          for s in claims['scenarios']], [93, 97])

    def test_all_questions_recover_initial_history_and_rebind_receipts(self):
        self.assertEqual({j['context']['intent'] for j in self.result['journeys']},
                         {'waiver', 'trade', 'trade_discovery', 'lineup', 'research', 'rebuild', 'mixed', 'draft'})
        for journey in self.result['journeys']:
            with self.subTest(intent=journey['context']['intent']):
                self.assertEqual(journey['history_revision'], 1)
                self.assertEqual(journey['resume']['context']['revision'], 2)
                self.assertEqual(journey['rebound_receipt']['revision'], 2)
                self.assertTrue(journey['resume']['comparisons_require_recheck'])
                self.assertEqual({t['team_id'] for t in journey['context']['all_team_index']}, {'a', 'b'})
                self.assertIn('news_coverage_not_certified', {g['code'] for g in journey['context']['gaps']})
        self.assertIn('Roster/rules changed', self.result['recovery']['changed_state_rejected'])
        self.assertTrue(self.result['recovery']['resume']['content_changed'])

    def test_discovery_expands_original_then_compares_finalist_with_hold(self):
        journey = next(j for j in self.result['journeys'] if j['context']['intent'] == 'trade_discovery')
        page, full = (journey['artifacts'][key] for key in ('screening', 'expanded_finalists'))
        self.assertEqual(page['coverage']['team_ids_screened'], ['a', 'b'])
        self.assertIn('no value ranking', page['coverage']['ordering'])
        self.assertEqual(full['decision_id'], journey['context']['decision_id'])
        actions = journey['comparison']['actions']
        self.assertEqual([a['kind'] for a in actions], ['hold', 'trade'])
        self.assertIn('Counterparty consent is unknown', actions[1]['execution_conditions'])
        self.assertEqual(set(actions[1]['accounting']['mechanics']['teams']), {'a', 'b'})
        self.assertTrue(all(a['championship_probability_delta'] is None for a in actions))

    def test_mixed_escape_retains_unknown_worlds_and_blocks_invented_mechanics(self):
        journey = next(j for j in self.result['journeys'] if j['context']['intent'] == 'mixed')
        comparison = journey['comparison']
        self.assertEqual(comparison['world_status'], 'uncomputed_assumptions')
        self.assertEqual(len(comparison['worlds']), 2)
        unsupported = comparison['actions'][-1]
        self.assertEqual(unsupported['status'], 'blocked')
        self.assertEqual(unsupported['gaps'], ['Unsupported action kind'])
        self.assertEqual(journey['receipt']['judgment']['choice'], 'hold')
        self.assertTrue(journey['resume']['comparisons_require_recheck'])

    def test_draft_compares_reconciled_vacancy_and_persists_board_dependency(self):
        journey = next(j for j in self.result['journeys'] if j['context']['intent'] == 'draft')
        branch = journey['comparison']['actions'][1]
        accounting = branch['accounting']
        self.assertEqual(branch['status'], 'mechanical_comparison')
        self.assertTrue(accounting['draft_roster_reconciled'])
        self.assertTrue(accounting['capacity']['within_capacity'])
        self.assertIn('free', accounting['after_roster']['player_ids'])
        self.assertNotIn('free', accounting['before_roster']['player_ids'])
        self.assertEqual(accounting['forecast_comparisons'][0]['delta_points'], 2)
        self.assertIsNone(accounting['legality']['platform_executable'])
        self.assertEqual(journey['receipt']['judgment']['choice'], 'draft-free')
        self.assertTrue(any(ref['kind'] == 'draft_board' for ref in journey['receipt']['consumed_manifest']))
        self.assertIsNone(branch['championship_probability_delta'])

    def test_future_transition_and_honest_host_analysis_boundary(self):
        future = self.result['explicit_future_roster_plan']
        self.assertTrue(future['valid'])
        self.assertEqual(future['after']['season'], 2027)
        self.assertEqual(future['after']['teams']['a']['player_ids'], ['aq', 'ar', 'aw', 'ax'])
        self.assertIsNone(future['championship_probability_delta'])
        self.assertFalse(self.result['model_called'])
        self.assertFalse(self.result['network_used'])
        self.assertFalse(self.result['persistent_store_created'])
        self.assertGreater(self.result['timing']['local_service_seconds'], 0)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK not installed')
    def test_actual_stdio_draft_recovery_and_partial_maintenance(self):
        from moneyball.league.decision_demo import run_mcp_extensions
        result = asyncio.run(run_mcp_extensions())
        self.assertEqual(len(result['trace']), 6)
        options = result['trace'][2]['result']
        self.assertEqual(options['first_useful_choice']['player_id'], 'ar')
        job = result['trace'][-1]['result']['jobs'][0]
        self.assertEqual(job['outcome'], 'partial')
        self.assertIsNone(job['last_success'])
        self.assertFalse(result['provider_network_used'])

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK not installed')
    def test_actual_stdio_server_prepare_compare_record_resume(self):
        result = asyncio.run(run_mcp())
        self.assertEqual(result['mode'], 'real_stdio_synthetic_evidence')
        self.assertEqual(len(result['trace']), 43)
        self.assertEqual([entry['tool'] for entry in result['trace'][:5]],
            ['prepare_decision', 'decision_evidence', 'compare_actions', 'record_decision', 'resume_decision'])
        prepared = [entry['result'] for entry in result['trace'] if entry['tool'] == 'prepare_decision']
        self.assertEqual({p['intent'] for p in prepared},
                         {'waiver', 'trade', 'trade_discovery', 'lineup', 'research', 'rebuild', 'mixed', 'draft'})
        self.assertTrue(all(p['alias'] == ('demo-draft' if p['intent'] == 'draft' else 'demo') for p in prepared))
        self.assertIn('discover_decision_candidates', [entry['tool'] for entry in result['trace']])
        self.assertIn('read_candidate_page', [entry['tool'] for entry in result['trace']])
        self.assertIn('draft_context', [entry['tool'] for entry in result['trace']])
        self.assertFalse(result['model_called'])
        self.assertFalse(result['provider_network_used'])


if __name__ == '__main__':
    unittest.main()
