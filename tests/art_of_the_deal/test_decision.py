"""Decision lifecycle invariants with synthetic evidence and no external calls."""
import copy
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.league import decision
from moneyball.league.demo import fixture, forecasts
from moneyball.league.service import Service
from moneyball.league.store import DataError
from test_league_research import report


class DecisionTests(unittest.TestCase):
    def test_compact_comparison_preserves_qualifications_and_originals(self):
        import json
        from moneyball.league.decision_demo import seed
        seed(self.service)
        context = self.prepare(intent='waiver')
        actions = [{'id': 'hold', 'kind': 'hold', 'week': 5},
                   {'id': 'add', 'kind': 'add_drop', 'player_id': 'free', 'drop_id': 'ax', 'week': 5}]
        packet = self.service.compare_actions(context['decision_id'], actions, detail='compact')
        full = self.service.read_decision(context['decision_id'], packet['comparison_id'])['comparison']
        compact = packet['actions'][1]['accounting']['comparison']
        original = full['actions'][1]['accounting']['comparison']
        for key in ('forecast_comparisons', 'evidence_needed', 'warnings', 'errors', 'roster_changes'):
            self.assertEqual(compact[key], original[key])
        self.assertEqual(compact['after']['roster'], original['after']['roster'])
        self.assertEqual(compact['after']['capacity'], original['after']['capacity'])
        self.assertEqual(compact['after']['position_capacity'], original['after']['position_capacity'])
        self.assertIn('inventory_expansion_path', compact['after']['coverage'])
        self.assertIn('players', original['after']['coverage'])
        self.assertEqual(packet['actions'][1]['accounting']['decision_support'], full['actions'][1]['accounting']['decision_support'])
        self.assertLess(len(json.dumps(packet)), len(json.dumps(full)))
        self.assertEqual(packet['consumed_manifest'], full['consumed_manifest'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.data = fixture()
        self.service._publish('demo', self.data, [], forecasts(self.data)[:-1])

    def test_record_rejects_unconsumed_or_forged_manifest_reference(self):
        context = self.prepare(player_ids=['ar'])
        compared = self.compare(context)
        judgment = self.judgment(compared)
        judgment['consulted_evidence'] = [copy.deepcopy(compared['consumed_manifest'][0])]
        receipt = decision.record(self.service, context['decision_id'], judgment)
        self.assertEqual(receipt['validated_references']['citation_status'], 'references_checked')
        judgment['consulted_evidence'][0]['sha256'] = 'forged'
        with self.assertRaisesRegex(DataError, 'not consumed'):
            decision.record(self.service, context['decision_id'], judgment)

    def test_claim_refs_must_exist_and_be_consumed_in_exact_player_scope(self):
        from moneyball.league.evidence import EvidenceIndex
        index = EvidenceIndex(self.service.store)
        index.index_document('role', {'source': {'url': 'https://example.test/role',
            'origin': 'synthetic', 'capture_policy': 'permitted_excerpt'}, 'coverage': {'status': 'partial'},
            'claims': [{'id': 'one', 'player_ref': self.data['platform'] + ':ar',
                'text': 'Role is conditional', 'facet': 'role'}]})
        context = self.prepare(player_ids=['ar'])
        compared = self.compare(context)
        hit = index.search(player_refs=[self.data['platform'] + ':ar'])['items'][0]['hit_id']
        judgment = self.judgment(compared)
        judgment['claim_refs'] = [hit]
        receipt = decision.record(self.service, context['decision_id'], judgment)
        self.assertEqual(receipt['validated_references']['claims'][0]['hit_id'], hit)
        judgment['claim_refs'] = [999999]
        with self.assertRaises(DataError):
            decision.record(self.service, context['decision_id'], judgment)

    def test_consumed_draft_board_change_blocks_record_without_roster_change(self):
        board = self.service.store.put('draft_board', 'demo-board', {'picks': [], 'oldest_observed_at': time.time()})
        ref = {'kind': 'draft_board', 'key': 'demo-board', **{k: board[k] for k in ('version_id', 'sha256', 'available_at')}}
        context = self.prepare(intent='draft')
        with patch('moneyball.league.action_models.draft_pick_action', return_value={
                'draft_observation': {'board_ref': ref}}):
            compared = decision.compare(self.service, context['decision_id'], [
                {'id': 'hold', 'kind': 'hold'}, {'id': 'pick', 'kind': 'draft_pick', 'player_id': 'ar'}])
        decision.record(self.service, context['decision_id'], self.judgment(compared, 'pick'))
        self.service.store.put('draft_board', 'demo-board', {'picks': ['ar'], 'oldest_observed_at': time.time()})
        with self.assertRaisesRegex(DataError, 'Consumed evidence changed'):
            decision.record(self.service, context['decision_id'], self.judgment(compared, 'pick'))

    def test_resume_recovers_countercase_reversals_and_alternatives_as_stale(self):
        context = self.prepare()
        compared = self.compare(context)
        judgment = self.judgment(compared)
        decision.record(self.service, context['decision_id'], judgment)
        recovered = decision.resume(self.service, context['decision_id'], refresh=False)
        saved = recovered['capsule']['recovered_reasoning']
        self.assertEqual(saved['judgment']['strongest_countercase'], judgment['strongest_countercase'])
        self.assertEqual(saved['judgment']['reversal_conditions'], judgment['reversal_conditions'])
        self.assertEqual({a['id'] for a in saved['alternatives']}, {'hold', 'swap'})
        self.assertEqual(saved['status'], 'historical_reasoning_requires_recheck')
        self.assertTrue(recovered['comparisons_require_recheck'])

    def test_action_contract_normalizes_discovery_fields_and_rejects_conflicts(self):
        self.assertEqual(decision._normalize_action({'id': 'x', 'kind': 'AddDrop',
            'add_player_id': 'free', 'drop_player_id': 'ax'}),
            {'id': 'x', 'kind': 'add_drop', 'player_id': 'free', 'drop_id': 'ax'})
        with self.assertRaisesRegex(DataError, 'Conflicting'):
            decision._normalize_action({'kind': 'add_drop', 'player_id': 'one', 'add_player_id': 'two'})
        with self.assertRaisesRegex(DataError, 'requires player_id'):
            decision._normalize_action({'kind': 'add_drop'})

    def test_compact_discovery_preserves_originals_and_outputs_usable_action_shapes(self):
        from moneyball.league.decision_demo import seed
        seed(self.service)
        context = self.prepare(intent='waiver')
        page = self.service.discover_candidates(context['decision_id'], kinds=['add_drop'])
        full = self.service.read_candidate_page(page['candidate_page_id'])
        self.assertEqual(len(page['candidates']), len(full['candidates']))
        self.assertEqual(page['coverage'], full['coverage'])
        self.assertTrue(all(r['action']['kind'] == 'add_drop' for r in page['candidates']))
        row = next(r for r in page['candidates'] if r['action'].get('drop_id') == 'ax')
        action = {**row['action'], 'id': 'add'}
        compared = decision.compare(self.service, context['decision_id'], [{'id': 'hold', 'kind': 'hold'}, action])
        self.assertNotEqual(compared['actions'][1]['status'], 'blocked')
        self.assertIn('decision_support', full['candidates'][0]['comparison'])
        import json
        self.assertLess(len(json.dumps(page)), len(json.dumps(full))/2)

    def test_nested_invalid_acquisition_cannot_be_recorded(self):
        context = self.prepare()
        with patch('moneyball.league.operations.pickup_packet', return_value={
                'comparison': {'valid': False, 'errors': ['capacity violated']}}):
            compared = decision.compare(self.service, context['decision_id'], [
                {'id': 'hold', 'kind': 'hold'},
                {'id': 'add', 'kind': 'add_drop', 'player_id': 'bx'}])
        self.assertEqual(compared['actions'][1]['status'], 'blocked')
        with self.assertRaises(DataError):
            decision.record(self.service, context['decision_id'], self.judgment(compared, 'add'))

    def prepare(self, **kwargs):
        return decision.prepare(self.service, {'question': 'Should I trade my runner?',
            'alias': 'demo', 'intent': 'trade', 'refresh': False, **kwargs})

    def compare(self, context):
        return decision.compare(self.service, context['decision_id'], [
            {'id': 'hold', 'kind': 'hold'},
            {'id': 'swap', 'kind': 'trade', 'proposal': {'transfers': [
                {'player_id': 'ar', 'from_team': 'a', 'to_team': 'b'},
                {'player_id': 'bx', 'from_team': 'b', 'to_team': 'a'}],
                'pick_transfers': [], 'drops': {}}}])

    def judgment(self, comparison, choice='hold'):
        return {'comparison_id': comparison['comparison_id'], 'choice': choice,
            'confidence': 'conditional', 'strongest_countercase': 'Unverified availability',
            'reversal_conditions': ['A corrected lock rule changes feasibility']}

    def test_question_alias_and_intent_are_required_without_recent_inference(self):
        for spec in ({'alias': 'demo'}, {'question': 'Help'},
                     {'question': 'Help', 'alias': 'demo', 'intent': 'unknown'}):
            with self.subTest(spec=spec), self.assertRaises(DataError):
                decision.prepare(self.service, spec)

    def test_prepare_binds_all_teams_and_pending_evidence_without_title_claim(self):
        context = self.prepare(player_ids=['ar'])
        self.assertEqual({t['team_id'] for t in context['all_team_index']}, {'a', 'b'})
        self.assertEqual(context['focused_evidence'][0]['player_id'], 'ar')
        self.assertEqual(context['readiness'], 'partial')
        self.assertIn('news_coverage_not_certified', {g['code'] for g in context['gaps']})
        self.assertEqual(context['snapshot']['sha256'], self.service.snapshot('demo')['sha256'])
        self.assertTrue(all(r['available_at'] <= context['evidence_as_of'] for r in context['manifest']))

    def test_historical_prepare_performs_no_refresh_and_excludes_later_data(self):
        cutoff = time.time()
        changed = copy.deepcopy(self.data)
        changed['teams']['a']['name'] = 'Later name'
        self.service._publish('demo', changed, [], [])
        with patch.object(self.service, 'refresh', side_effect=AssertionError('Network forbidden')):
            context = self.prepare(as_of=cutoff, refresh=True)
        self.assertEqual(context['all_team_index'][0]['name'], 'Harbor')
        self.assertFalse(context['refresh']['requested'])
        self.assertLessEqual(context['snapshot']['available_at'], cutoff)

    def test_failed_refresh_preserves_snapshot_date_and_hash(self):
        before = self.service.snapshot('demo')
        with patch.object(self.service, 'refresh', side_effect=DataError('upstream failed')):
            context = self.prepare(refresh=True)
        self.assertEqual(context['refresh']['status'], 'failed')
        self.assertEqual(context['snapshot']['available_at'], before['available_at'])
        self.assertEqual(context['snapshot']['sha256'], before['sha256'])
        self.assertIn('league_refresh_failed', {g['code'] for g in context['gaps']})

    def test_missing_own_team_can_prepare_but_personalized_hold_is_blocked(self):
        self.data['own_team_id'] = None
        self.service._publish('demo', self.data, [], [])
        context = self.prepare()
        self.assertIn('own_team_required', {g['code'] for g in context['gaps']})
        compared = decision.compare(self.service, context['decision_id'], [{'id': 'hold', 'kind': 'hold'}])
        self.assertEqual(compared['actions'][0]['status'], 'blocked')

    def test_comparison_requires_unique_ids_and_hold(self):
        context = self.prepare()
        for actions in ([], [{'id': 'trade', 'kind': 'trade'}],
                        [{'id': 'x', 'kind': 'hold'}, {'id': 'x', 'kind': 'hold'}], ['bad']):
            with self.subTest(actions=actions), self.assertRaises(DataError):
                decision.compare(self.service, context['decision_id'], actions)

    def test_trade_has_complete_both_team_counterfactual_and_no_odds(self):
        compared = self.compare(self.prepare())
        for branch in compared['actions']:
            self.assertIsNone(branch['championship_probability_delta'])
        trade = compared['actions'][1]['accounting']
        self.assertEqual(set(trade['mechanics']['forecast_comparisons'][0]['teams']), {'a', 'b'})
        self.assertEqual(trade['mechanics']['forecast_comparisons'][0]['teams']['a']['delta_points'], -1)
        self.assertEqual(trade['mechanics']['forecast_comparisons'][0]['teams']['b']['delta_points'], 1)

    def test_later_forecasts_do_not_change_an_earlier_comparison(self):
        context = self.prepare()
        first = self.compare(context)
        rows = forecasts(self.data)[:-1]
        rows[0]['points'] = 1000
        self.service.store.put('forecasts', 'demo:later', rows)
        second = self.compare(context)
        self.assertEqual(first['actions'][0]['accounting'], second['actions'][0]['accounting'])
        self.assertEqual(first['actions'][1]['accounting']['mechanics'], second['actions'][1]['accounting']['mechanics'])

    def test_sources_cannot_fill_each_others_missing_forecasts(self):
        rows = forecasts(self.data)[:-1]
        rows[0]['source'] = 'other-source'
        self.service.store.put('forecasts', 'demo:espn', rows)
        context = self.prepare()
        hold = decision.compare(self.service, context['decision_id'], [{'id': 'hold', 'kind': 'hold'}])
        comparisons = hold['actions'][0]['accounting']['forecast_comparisons']
        self.assertTrue(comparisons)
        self.assertTrue(all(c['modeled_lineup'] is None for c in comparisons))

    def test_worlds_are_retained_without_imaginary_weights_or_winner(self):
        worlds = [{'id': 'healthy', 'weight': None}, {'id': 'reduced_role', 'weight': None}]
        result = decision.compare(self.service, self.prepare()['decision_id'], [{'id': 'hold', 'kind': 'hold'}], worlds)
        self.assertEqual(result['worlds'], worlds)
        self.assertEqual(result['world_status'], 'uncomputed_assumptions')
        self.assertNotIn('winner', result)

    def test_record_rejects_cross_decision_comparison(self):
        one, two = self.prepare(), self.prepare()
        comparison = self.compare(one)
        with self.assertRaisesRegex(DataError, 'another decision'):
            decision.record(self.service, two['decision_id'], self.judgment(comparison))

    def test_record_rejects_stale_revision_even_with_identical_resources(self):
        context = self.prepare()
        comparison = self.compare(context)
        decision.resume(self.service, context['decision_id'], refresh=False)
        with self.assertRaisesRegex(DataError, 'revision'):
            decision.record(self.service, context['decision_id'], self.judgment(comparison))

    def test_changed_rules_reject_record_and_resume_exposes_change(self):
        context = self.prepare()
        comparison = self.compare(context)
        changed = copy.deepcopy(self.data)
        changed['rules']['bench_slots'] += 1
        self.service._publish('demo', changed, [], [])
        with self.assertRaisesRegex(DataError, 'changed'):
            decision.record(self.service, context['decision_id'], self.judgment(comparison))
        resumed = decision.resume(self.service, context['decision_id'], refresh=False)
        self.assertTrue(resumed['comparisons_require_recheck'])
        self.assertTrue(any(r['kind'] == 'league' for r in resumed['changed_resources']))
        self.assertEqual(resumed['capsule']['revision'], 2)
        self.assertNotEqual(context['rules_hash'], resumed['capsule']['rules_hash'])

    def test_historical_resume_cannot_fetch_later_evidence(self):
        context = self.prepare(as_of=time.time())
        with self.assertRaisesRegex(DataError, 'Historical'):
            decision.resume(self.service, context['decision_id'], refresh=True)

    def test_record_is_local_receipt_not_execution(self):
        context = self.prepare()
        comparison = self.compare(context)
        before = self.service.snapshot('demo')['sha256']
        receipt = decision.record(self.service, context['decision_id'], self.judgment(comparison))
        self.assertEqual(receipt['status'], 'host_judgment_conditional')
        self.assertEqual(self.service.snapshot('demo')['sha256'], before)
        self.assertEqual(self.service.store.get('decision_receipt', context['decision_id'])['data'], receipt)

    def test_caller_owned_trade_conditions_are_not_mutated(self):
        context = self.prepare()
        actions = [{'id': 'hold', 'kind': 'hold'}, {'id': 'swap', 'kind': 'trade',
            'execution_conditions': [], 'proposal': {'transfers': [
                {'player_id': 'ar', 'from_team': 'a', 'to_team': 'b'},
                {'player_id': 'bx', 'from_team': 'b', 'to_team': 'a'}], 'drops': {}, 'pick_transfers': []}}]
        original = copy.deepcopy(actions)
        decision.compare(self.service, context['decision_id'], actions)
        self.assertEqual(actions, original)

    def test_manifest_excludes_other_league_dossier_and_includes_consumed_research(self):
        self.service.save_research('demo', report(self.service.research_plan('demo')))
        self.service.store.put('dossier', 'espn:unrelated', {'summary': 'Other league'})
        context = self.prepare(player_ids=['ar'])
        refs = {(r['kind'], r['key']) for r in context['manifest']}
        self.assertNotIn(('dossier', 'espn:unrelated'), refs)
        self.assertIn(('league_research', self.service.research_plan('demo')['research_key']), refs)
        self.assertNotIn('strategy_research_not_confirmed', {g['code'] for g in context['gaps']})

    def test_comparison_manifest_binds_newly_focused_dossier(self):
        self.service.store.put('dossier', 'synthetic:bx', {'summary': 'Before decision',
            'evidence_checked_at': time.time(), 'claims': [], 'sources': [], 'open_questions': []})
        context = self.prepare()
        self.assertNotIn(('dossier', 'synthetic:bx'), {(r['kind'], r['key']) for r in context['manifest']})
        comparison = self.compare(context)
        self.assertIn(('dossier', 'synthetic:bx'), {(r['kind'], r['key']) for r in comparison['consumed_manifest']})
        self.service.store.put('dossier', 'synthetic:bx', {'summary': 'Changed availability'})
        with self.assertRaisesRegex(DataError, 'Consumed evidence changed'):
            decision.record(self.service, context['decision_id'], self.judgment(comparison))

    def test_new_forecast_content_requires_recomparison_before_receipt(self):
        context = self.prepare()
        comparison = self.compare(context)
        rows = forecasts(self.data)[:-1]
        rows[0]['points'] += 1
        self.service.store.put('forecasts', 'demo:espn', rows)
        with self.assertRaisesRegex(DataError, 'Consumed evidence changed'):
            decision.record(self.service, context['decision_id'], self.judgment(comparison))

    def test_identical_reobserved_forecasts_do_not_invalidate_content(self):
        context = self.prepare()
        comparison = self.compare(context)
        rows = self.service.store.get('forecasts', 'demo:espn')['data']
        self.service.store.put('forecasts', 'demo:espn', rows)
        receipt = decision.record(self.service, context['decision_id'], self.judgment(comparison))
        before = next(r for r in receipt['consumed_manifest'] if r['kind'] == 'forecasts')
        after = next(r for r in receipt['recheck_manifest'] if r['kind'] == 'forecasts')
        self.assertEqual(before['sha256'], after['sha256'])
        self.assertNotEqual(before['version_id'], after['version_id'])

    def test_evidence_expansion_pins_new_focus_to_original_cutoff(self):
        original = {'summary': 'Original role', 'evidence_checked_at': time.time(),
            'claims': [], 'sources': [], 'open_questions': []}
        self.service.store.put('dossier', 'synthetic:bx', original)
        context = self.prepare()
        self.service.store.put('dossier', 'synthetic:bx', {**original, 'summary': 'Later role'})
        result = decision.evidence(self.service, context['decision_id'], focus=['bx'])
        self.assertEqual(result['players'][0]['preserved_dossier']['summary'], 'Original role')
        self.assertTrue(all(r['available_at'] <= context['evidence_as_of'] for r in result['manifest']))
        with self.assertRaisesRegex(DataError, 'cursors'):
            decision.evidence(self.service, context['decision_id'], focus=['bx'], cursor='ignored')

    def test_contradictory_explicit_league_identity_is_rejected(self):
        with self.assertRaises(DataError):
            self.prepare(league_refs=[{'alias': 'demo', 'platform': 'espn', 'league_id': 'wrong', 'season': 2030}])


if __name__ == '__main__':
    unittest.main()
