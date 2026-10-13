"""Conditional analytical evidence must reach receipts without becoming a verdict."""
from copy import deepcopy
from datetime import datetime, timezone
import tempfile
import unittest

from moneyball.league.decision_demo import seed, judgment
from moneyball.league.service import Service
from moneyball.league.store import DataError

class ExperimentDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.league = seed(self.service)
        stamp = datetime.now(timezone.utc).isoformat()
        binding = {'provider': self.league['platform'], 'league_id': self.league['league_id'],
            'season': self.league['season'], 'own_team_id': self.league['own_team_id'],
            'period': {'kind': 'week', 'week': 5}, 'scoring': deepcopy(self.league['rules']['scoring']), 'as_of': stamp}
        stats = {key: 0 for key in binding['scoring']['weights']}
        stats['rec'], stats['rec_yd'] = 4, 60
        expected = {**stats, 'rec': 5, 'rec_yd': 40}
        self.request = {'alias': 'demo', 'module': 'observed_opportunity', 'binding': binding,
            'dataset': {'binding': deepcopy(binding), 'source_id': 'invented', 'source_version': 'v1',
                'first_observed_at': stamp, 'available_at': stamp, 'evidence_refs': ['fixture:source']},
            'rows': [{'entity_id': self.league['platform'] + ':ar', 'observed_stats': stats, 'expected_stats': expected}]}

    def prepare(self):
        return self.service.prepare_decision({'alias': 'demo', 'intent': 'research', 'question': 'Inspect opportunity', 'refresh': False})

    def compare(self, context, key):
        return self.service.compare_actions(context['decision_id'], [{'id': 'hold', 'kind': 'hold', 'week': 5, 'experiment_ids': [key]}], detail='compact')

    def test_result_citable_recoverable_and_never_title_odds(self):
        result = self.service.run_experiment(self.request)
        context = self.prepare()
        comparison = self.compare(context, result['experiment_id'])
        evidence = comparison['actions'][0]['analytical_evidence'][0]
        self.assertEqual(evidence['output_preview']['rows'], result['rows'])
        self.assertNotIn('request', evidence['original'])
        self.assertIsNone(comparison['actions'][0]['championship_probability_delta'])
        proposed = judgment(comparison)
        proposed['consulted_evidence'] = evidence['evidence_refs']
        receipt = self.service.record_decision(context['decision_id'], proposed)
        self.assertEqual(receipt['validated_references']['citation_status'], 'references_checked')
        full = self.service.read_decision(context['decision_id'], comparison['comparison_id'])['comparison']
        self.assertEqual(full['actions'][0]['analytical_evidence'][0]['original']['request'], self.request)
        resumed = self.service.resume_decision(context['decision_id'], refresh=False)
        self.assertTrue(resumed['comparisons_require_recheck'])

    def test_new_result_requires_repreparation(self):
        context = self.prepare()
        result = self.service.run_experiment(self.request)
        branch = self.compare(context, result['experiment_id'])['actions'][0]
        self.assertEqual(branch['status'], 'blocked')
        self.assertIn('resume before comparing', branch['gaps'][0])

    def test_changed_result_invalidates_record(self):
        result = self.service.run_experiment(self.request)
        context = self.prepare()
        comparison = self.compare(context, result['experiment_id'])
        saved = self.service.store.get('experiment_result', result['experiment_id'])['data']
        self.service.store.put('experiment_result', result['experiment_id'], {**saved, 'assurance': 'Revised qualification'})
        with self.assertRaisesRegex(DataError, 'Consumed evidence changed'):
            self.service.record_decision(context['decision_id'], judgment(comparison))

    def test_other_roster_binding_and_blocked_inputs_cannot_be_attached(self):
        result = self.service.run_experiment(self.request)
        league = deepcopy(self.league)
        league['teams']['a']['name'] += ' changed'
        self.service._publish('demo', league, [], [])
        branch = self.compare(self.prepare(), result['experiment_id'])['actions'][0]
        self.assertEqual(branch['status'], 'blocked')
        self.assertIn('binding', branch['gaps'][0])
        self.request['binding']['scoring']['weights']['rec'] = 100
        self.request['dataset']['binding'] = deepcopy(self.request['binding'])
        blocked = self.service.run_experiment(self.request)
        branch = self.compare(self.prepare(), blocked['experiment_id'])['actions'][0]
        self.assertEqual(branch['status'], 'blocked')

if __name__ == '__main__':
    unittest.main()
