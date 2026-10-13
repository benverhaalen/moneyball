"""Readiness must disclose limitations without erasing usable arithmetic."""
import copy
import tempfile
import time
import unittest

from moneyball.league.decision_readiness import describe
from moneyball.league.decision_demo import seed, scenario_specs
from moneyball.league.service import Service


class CapabilityReadinessTests(unittest.TestCase):
    def test_new_trade_players_get_frozen_reporting_checks_and_receipt_disclosures(self):
        from moneyball.league.evidence import EvidenceIndex
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory)
            seed(service)
            index = EvidenceIndex(service.store)
            now = time.time()
            def report(pid):
                return {'source': {'origin': 'invented', 'url': 'https://example.invalid/report',
                                  'capture_policy': 'link_only'},
                        'published_at': now - 10, 'coverage': {'season': 2026, 'week': 5},
                        'claims': [{'id': pid, 'player_ref': 'synthetic:' + pid,
                                    'facet': 'availability', 'text': 'Invented report'}]}
            index.index_document('free-report', report('free'))
            context = service.prepare_decision({'alias': 'demo', 'intent': 'trade',
                'question': 'Consider a newly introduced trade', 'refresh': False,
                'as_of': time.time(), 'player_ids': ['free']})
            self.assertEqual(context['source_readiness']['reporting']['status'], 'targeted_reports_present')
            # Later local reporting cannot leak into this prepared comparison.
            index.index_document('ar-report', report('ar'))
            actions = next(s for s in scenario_specs() if s['specification']['intent'] == 'trade')['actions']
            packet = service.compare_actions(context['decision_id'], actions, detail='compact')
            hold, swap = packet['actions']
            self.assertEqual(hold['source_readiness']['focused_player_ids'], ['free'])
            reporting = swap['source_readiness']['checks'][0]['reporting']
            self.assertEqual(reporting['missing_player_refs'], ['synthetic:ar', 'synthetic:bx'])
            gaps = swap['capability_readiness']['all_gaps']
            self.assertTrue(any(g.get('code') == 'targeted_reporting_missing_or_stale'
                                and g['player_refs'] == ['synthetic:ar', 'synthetic:bx']
                                for g in gaps if isinstance(g, dict)))
            from moneyball.league.store import digest
            self.assertEqual(digest(service.read_decision(context['decision_id'])['context']['source_readiness']),
                             digest(context['source_readiness']))
            # Preserve even the unchosen branch's new gaps in durable history.
            receipt = service.record_decision(context['decision_id'], {
                'comparison_id': packet['comparison_id'], 'choice': 'hold',
                'confidence': 'conditional', 'strongest_countercase': 'Trade may improve coverage',
                'reversal_conditions': ['Inspect new-player reports']})
            self.assertEqual(receipt['capability_readiness_by_action']['swap']['all_gaps'], gaps)

    def test_stale_reporting_does_not_erase_arithmetic_or_certify_advice(self):
        context = {'league_binding': {'season': 2026}, 'gaps': [
            {'code': 'league_refresh_failed', 'detail': 'Retained old snapshot'},
            {'code': 'retention_horizon_unspecified'}]}
        row = {'season': 2026, 'period': {'kind': 'week', 'week': 5},
               'status': 'complete_counterfactual', 'modeled_lineup': {'complete': True}}
        result = describe(context, {'accounting': {'forecast_comparisons': [row]}})
        caps = result['capabilities']
        self.assertEqual(caps['current_point_calculation']['status'], 'available_conditional')
        self.assertEqual(caps['current_decision_evidence']['status'], 'partial')
        self.assertEqual(caps['platform_execution']['status'], 'unverified')
        self.assertEqual(caps['championship_probability']['status'], 'not_estimated')
        self.assertEqual(result['all_gaps'], context['gaps'])
        self.assertNotIn('current_point_calculation', result['scoped_gaps'][1]['affects'])

    def test_forecast_status_alone_and_wrong_horizons_never_establish_arithmetic(self):
        context = {'league_binding': {'season': 2026}}
        complete = {'season': 2026, 'period': {'kind': 'week', 'week': 5},
                    'status': 'complete', 'teams': {'a': {
                        'before': {'complete': True}, 'after': {'complete': True}}}}
        cases = [dict(complete, season=2027), dict(complete, period={'kind': 'season'}),
                 dict(complete, teams={}), dict(complete, missing_required_player_ids=['p']),
                 dict(complete, conflicting_player_ids=['p']),
                 dict(complete, teams={'a': {'before': {'complete': True}, 'after': {'complete': False}}}),
                 {'season': 2026, 'period': {'kind': 'week'}, 'status': 'conditional_weekly_sensitivity',
                  'before': {'complete': False}, 'after': {'complete': True}}]
        for row in cases:
            with self.subTest(row=row):
                result = describe(context, {'accounting': {'forecast_comparisons': [row]}})
                self.assertEqual(result['capabilities']['current_point_calculation']['status'], 'not_computed')
        result = describe(context, {'accounting': {'forecast_comparisons': [complete]}})
        self.assertEqual(result['capabilities']['current_point_calculation']['cohort_count'], 1)

    def test_unknown_gap_is_preserved_conservatively_without_mutation(self):
        context = {'gaps': [{'code': 'new_unclassified_failure', 'nested': ['keep me']}]}
        original = copy.deepcopy(context)
        result = describe(context)
        self.assertEqual(result['scoped_gaps'][0]['scope_basis'], 'unknown_conservative')
        self.assertEqual(len(result['scoped_gaps'][0]['affects']), 7)
        result['all_gaps'][0]['nested'].append('changed')
        self.assertEqual(context, original)

    def test_current_roster_plan_does_not_claim_future_accounting(self):
        context = {'league_binding': {'season': 2026}}
        entry = {'kind': 'roster_plan', 'accounting': {'valid': True, 'steps': [
            {'action': {'kind': 'Hold'}, 'conditional_state_changes': {}}]}}
        self.assertEqual(describe(context, entry)['capabilities']['future_roster_accounting']['status'], 'not_compared')
        entry['accounting']['steps'][0]['action'] = {'kind': 'Retain', 'season': 2027}
        result = describe(context, entry)['capabilities']
        self.assertEqual(result['future_roster_accounting']['status'], 'available_conditional')
        self.assertEqual(result['future_valuation']['status'], 'not_estimated')

    def test_real_trade_and_blocked_future_alternative_survive_record_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(directory)
            seed(service)
            spec = next(s for s in scenario_specs() if s['specification']['intent'] == 'trade')
            context = service.prepare_decision(spec['specification'])
            actions = spec['actions'] + [{'id': 'future', 'kind': 'roster_plan', 'steps': [
                {'kind': 'Retain', 'season': 2027, 'teams': {'a': ['aq'], 'b': ['bq']}}]}]
            packet = service.compare_actions(context['decision_id'], actions, detail='compact')
            trade = packet['actions'][1]['capability_readiness']
            self.assertEqual(trade['capabilities']['current_point_calculation']['status'], 'available_conditional')
            future = packet['actions'][2]
            self.assertEqual(future['status'], 'blocked')
            self.assertEqual(future['capability_readiness']['capabilities']['future_roster_accounting']['status'], 'blocked')
            receipt = service.record_decision(context['decision_id'], {
                'comparison_id': packet['comparison_id'], 'choice': 'hold', 'confidence': 'conditional',
                'strongest_countercase': 'Future rights lack verified rules',
                'reversal_conditions': ['Exact retention rules arrive']})
            self.assertEqual(set(receipt['capability_readiness_by_action']), {'hold', 'swap', 'future'})
            self.assertEqual(receipt['capability_readiness_by_action']['future'], future['capability_readiness'])
            recovered = service.resume_decision(context['decision_id'], refresh=False)
            alternatives = recovered['capsule']['recovered_reasoning']['alternatives']
            self.assertEqual(alternatives[2]['capability_readiness'], future['capability_readiness'])


if __name__ == '__main__':
    unittest.main()
