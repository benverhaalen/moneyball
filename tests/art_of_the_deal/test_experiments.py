"""Synthetic, literal oracles for optional conditional analyses."""
from copy import deepcopy
import unittest

from moneyball.league.experiments import observed_opportunity, role_state_futures, weekly_competition, run_experiment
from moneyball.league.store import DataError


def envelope():
    binding = {'provider': 'sleeper', 'league_id': 'fiction', 'season': 2026,
               'own_team_id': 'a', 'period': {'kind': 'week', 'week': 6},
               'scoring': {'weights': {'rec': 1, 'rec_yd': .1}},
               'as_of': '2026-10-13T12:00:00Z'}
    return {'binding': binding, 'dataset': {'binding': deepcopy(binding),
            'source_id': 'synthetic-supplied', 'source_version': 'fixture-v1',
            'first_observed_at': '2026-10-13T10:00:00Z',
            'available_at': '2026-10-13T09:00:00Z', 'evidence_refs': ['fixture:source']}}


def weekly():
    request = envelope()
    request.update(lineup_lock_at='2026-10-14T12:00:00Z', starters=[{'eligible_positions': ['WR']}],
        players={p: {'positions': ['WR']} for p in ('sleeper:1', 'sleeper:2', 'sleeper:3')},
        branches={'hold': {'a': {'roster': ['sleeper:1', 'sleeper:3'], 'lineup': ['sleeper:1']},
                           'b': {'roster': ['sleeper:2'], 'lineup': ['sleeper:2']}},
                  'change': {'a': {'roster': ['sleeper:1', 'sleeper:3'], 'lineup': ['sleeper:3']},
                             'b': {'roster': ['sleeper:2'], 'lineup': ['sleeper:2']}}},
        baseline='hold', matchups=[['a', 'b']],
        worlds=[{'id': 'good', 'weight': .5, 'stats': {'sleeper:1': {'rec': 10, 'rec_yd': 0},
                'sleeper:2': {'rec': 12, 'rec_yd': 0}, 'sleeper:3': {'rec': 13, 'rec_yd': 0}}},
                {'id': 'bad', 'weight': .5, 'stats': {'sleeper:1': {'rec': 0, 'rec_yd': 0},
                'sleeper:2': {'rec': 2, 'rec_yd': 0}, 'sleeper:3': {'rec': 3, 'rec_yd': 0}}}])
    for holdings in request['branches'].values():
        for team in holdings.values():
            team['lineup_selected_at'] = '2026-10-13T11:00:00Z'
    return request


class ExperimentsTests(unittest.TestCase):
    def test_descriptive_components_and_allocation_delta(self):
        request = envelope()
        request['rows'] = [{'entity_id': 'sleeper:1', 'observed_stats': {'rec': 4, 'rec_yd': 60},
            'expected_stats': {'rec': 5, 'rec_yd': 40},
            'zero_components': ['rec_td'],
            'scenario_opportunity': {'receiving': {'team_dropbacks': 40, 'route_share': .5,
                'targets_per_route': .5, 'catch_rate': .5, 'yards_per_catch': 10, 'td_per_target': 0}}}]
        # A derived rec_td component cannot also be declared zero.
        with self.assertRaises(DataError):
            observed_opportunity(request)
        request['rows'][0]['zero_components'] = []
        result = observed_opportunity(request)
        row = result['rows'][0]
        self.assertEqual(row['observed']['mean'], 10)
        self.assertEqual(row['xfp']['mean'], 9)
        self.assertEqual(row['delta_actual_minus_xfp'], 1)
        self.assertEqual(row['scenario']['mean'], 10)
        self.assertEqual(row['scenario_delta_vs_xfp'], 1)
        self.assertEqual(row['scenario']['trace']['targets'], 10)

    def test_missing_opportunity_never_scores_as_zero(self):
        request = envelope()
        request['rows'] = [{'entity_id': 'sleeper:1', 'observed_stats': {'rec': 4},
                            'expected_stats': {'rec': 5, 'rec_yd': 40}}]
        result = observed_opportunity(request)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['rows'][0]['delta_actual_minus_xfp'])
        self.assertEqual(result['rows'][0]['observed']['missing'], ['rec_yd'])

    def test_old_season_is_not_historical_knowability(self):
        request = weekly()
        request['binding']['season'] = 2020
        request['binding']['as_of'] = '2020-10-13T12:00:00Z'
        request['dataset']['binding'] = deepcopy(request['binding'])
        result = weekly_competition(request)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['missing_inputs'], ['dataset_known_at_decision_time'])

    def test_scoring_and_period_mismatch_block(self):
        for key, value in [('period', {'kind': 'week', 'week': 7}),
                           ('scoring', {'weights': {'rec': .5}})]:
            request = weekly()
            request['dataset']['binding'][key] = value
            self.assertEqual(weekly_competition(request)['missing_inputs'], ['compatible_dataset_binding'])

    def test_role_exit_state_propagates_conditional_points(self):
        request = envelope()
        request['binding']['period'] = {'kind': 'weeks', 'weeks': [6, 7]}
        request['dataset']['binding'] = deepcopy(request['binding'])
        request.update(entity_id='sleeper:1', steps=2, initial={'starter': 1, 'exit': 0},
            transitions={'starter': {'starter': .5, 'exit': .5}, 'exit': {'starter': 0, 'exit': 1}},
            state_projections={'starter': {'stats': {'rec': 5, 'rec_yd': 50}, 'availability': 1},
                               'exit': {'stats': {'rec': 0, 'rec_yd': 0}, 'availability': 0}})
        result = role_state_futures(request)
        self.assertEqual(result['weekly_points'], [{'week': 6, 'mean': 5}, {'week': 7, 'mean': 2.5}])
        self.assertEqual(result['state_path'][-1], {'starter': .25, 'exit': .75})
        del request['state_projections']['exit']
        self.assertEqual(role_state_futures(request)['status'], 'blocked')

    def test_joint_worlds_preserve_correlation_and_paired_delta(self):
        result = weekly_competition(weekly())
        hold, change = result['branches']['hold'], result['branches']['change']
        self.assertEqual(hold['team_mean_points'], {'a': 5, 'b': 7})
        self.assertEqual(hold['team_covariance']['a']['b'], 25)
        self.assertEqual(hold['matchups'][0]['conditional_win_mass'], 0)
        self.assertEqual(change['matchups'][0]['conditional_win_mass'], 1)
        self.assertEqual(change['matchups'][0]['paired_delta_mean_margin'], 3)
        self.assertIsNone(result['title_probabilities'])
        # Same player marginals, opposite dependence: matchup masses change.
        request = weekly()
        request['worlds'][0]['stats']['sleeper:2']['rec'] = 2
        request['worlds'][1]['stats']['sleeper:2']['rec'] = 12
        anti = weekly_competition(request)['branches']['hold']
        self.assertEqual(anti['team_mean_points'], {'a': 5, 'b': 7})
        self.assertEqual(anti['team_covariance']['a']['b'], -25)
        self.assertEqual(anti['matchups'][0]['conditional_win_mass'], .5)

    def test_missing_player_world_blocks_complete_comparison(self):
        request = weekly()
        del request['worlds'][1]['stats']['sleeper:3']
        result = weekly_competition(request)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['missing_inputs'], ['bad:sleeper:3'])

    def test_no_silent_world_weight_normalization(self):
        request = weekly()
        request['worlds'][0]['weight'] = .8
        with self.assertRaises(DataError):
            weekly_competition(request)

    def test_fixed_lineups_validate_ownership_and_slots(self):
        request = weekly()
        request['branches']['hold']['b']['lineup'] = ['sleeper:1']
        with self.assertRaises(DataError):
            weekly_competition(request)
        request = weekly()
        request['players']['sleeper:1']['positions'] = ['QB']
        with self.assertRaises(DataError):
            weekly_competition(request)

    def test_zero_world_scores_represent_tie_mass(self):
        request = weekly()
        for world in request['worlds']:
            for stats in world['stats'].values():
                stats['rec'] = 0
        result = weekly_competition(request)
        self.assertEqual(result['branches']['hold']['matchups'][0]['conditional_tie_mass'], 1)

    def test_pruned_assumption_changes_fingerprint_without_mutation(self):
        request = weekly()
        old = deepcopy(request)
        first = weekly_competition(request)
        self.assertEqual(request, old)
        request['worlds'][0]['weight'] = 1
        request['worlds'][1]['weight'] = 0
        second = weekly_competition(request)
        self.assertEqual(second['branches']['hold']['team_mean_points'], {'a': 10, 'b': 12})
        self.assertNotEqual(first['input_sha256'], second['input_sha256'])

    def test_lineup_selected_after_decision_cannot_be_pre_lock_policy(self):
        request = weekly()
        request['branches']['change']['a']['lineup_selected_at'] = '2026-10-14T10:00:00Z'
        self.assertEqual(weekly_competition(request)['missing_inputs'], ['lineup_known_at_decision_time'])
        request = weekly()
        request['lineup_lock_at'] = '2026-10-12T12:00:00Z'
        self.assertEqual(weekly_competition(request)['missing_inputs'], ['pre_lock_decision_time'])

    def test_unknown_and_unconfigured_modules_are_explicit(self):
        with self.assertRaises(DataError):
            run_experiment({'module': 'calibrated_titles'})
        request = envelope()
        request.pop('dataset')
        self.assertEqual(observed_opportunity(request)['missing_inputs'], ['dated_dataset'])


class ServiceExperimentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from moneyball.league.service import Service
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.request = envelope()
        self.request.update(alias='fixture', module='observed_opportunity', rows=[{
            'entity_id': 'sleeper:1', 'observed_stats': {'rec': 4, 'rec_yd': 60},
            'expected_stats': {'rec': 5, 'rec_yd': 40}}])
        self.league = {'platform': 'sleeper', 'league_id': 'fiction', 'season': 2026,
            'own_team_id': 'a', 'teams': {'a': {}, 'b': {}},
            'rules': {'scoring': deepcopy(self.request['binding']['scoring']),
                      'starters': [{'eligible_positions': ['WR']}]}}
        self.publish(self.league)

    def publish(self, league, when='2026-10-13T08:00:00Z'):
        from unittest.mock import patch
        from moneyball.league.store import timestamp
        with patch('moneyball.league.store.time.time', return_value=timestamp(when)):
            return self.service.store.put('league', 'fixture', league)

    def test_saved_rule_binding_and_immutable_receipt(self):
        result = self.service.run_experiment(self.request)
        self.assertEqual(result['rows'][0]['delta_actual_minus_xfp'], 1)
        self.assertTrue(result['league_binding']['verified'])
        saved_request = self.service.store.get('experiment_request', result['experiment_id'])
        saved_result = self.service.store.get('experiment_result', result['experiment_id'])
        self.assertEqual(saved_request['data']['rows'][0]['expected_stats'], {'rec': 5, 'rec_yd': 40})
        self.assertEqual(saved_result['data']['rows'][0]['delta_actual_minus_xfp'], 1)
        self.assertEqual(saved_result['provenance']['snapshot_version'], result['league_binding']['snapshot_version'])
        self.assertIsNone(saved_result['data']['title_probabilities'])

    def test_caller_cannot_substitute_other_league_or_scoring(self):
        self.request['binding']['scoring'] = {'weights': {'rec': 10, 'rec_yd': .1}}
        self.request['dataset']['binding'] = deepcopy(self.request['binding'])
        result = self.service.run_experiment(self.request)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('saved_league_binding:scoring', result['missing_inputs'])
        self.assertFalse(result['league_binding']['verified'])
        self.assertEqual(self.service.store.get('experiment_result', result['experiment_id'])['data']['status'], 'blocked')

    def test_historical_snapshot_and_source_knowledge_stay_separate(self):
        changed = deepcopy(self.league)
        changed['rules']['scoring'] = {'weights': {'rec': .5, 'rec_yd': .1}}
        new = self.publish(changed, '2026-10-13T13:00:00Z')
        result = self.service.run_experiment(self.request)
        self.assertEqual(result['rows'][0]['xfp']['mean'], 9)
        self.assertNotEqual(result['league_binding']['snapshot_version'], new['version_id'])
        self.request['dataset']['first_observed_at'] = '2026-10-13T13:00:00Z'
        blocked = self.service.run_experiment(self.request)
        self.assertTrue(blocked['league_binding']['verified'])
        self.assertEqual(blocked['missing_inputs'], ['dataset_known_at_decision_time'])

    def test_missing_historical_snapshot_and_unknown_own_team_block(self):
        self.request['binding']['as_of'] = '2020-10-13T12:00:00Z'
        self.request['dataset']['binding'] = deepcopy(self.request['binding'])
        result = self.service.run_experiment(self.request)
        self.assertIn('saved_league_known_at_decision_time', result['missing_inputs'])
        self.request = envelope()
        self.request.update(alias='fixture', module='observed_opportunity', rows=[])
        league = deepcopy(self.league)
        league['own_team_id'] = None
        self.publish(league, '2026-10-13T11:00:00Z')
        result = self.service.run_experiment(self.request)
        self.assertIn('verified_saved_own_team', result['missing_inputs'])

    def test_weekly_starter_rules_must_match_saved_rules(self):
        request = weekly()
        request.update(alias='fixture', module='weekly_competition')
        good = self.service.run_experiment(request)
        self.assertEqual(good['branches']['hold']['team_covariance']['a']['b'], 25)
        request['starters'] = [{'eligible_positions': ['WR', 'TE']}]
        self.assertEqual(self.service.run_experiment(request)['missing_inputs'], ['saved_league_starter_binding'])
        unknown = deepcopy(self.league)
        unknown['rules']['unknown_slots'] = ['UNTRANSLATED']
        self.publish(unknown, '2026-10-13T11:00:00Z')
        self.assertIn('translated_saved_starter_rules', self.service.run_experiment(request)['missing_inputs'])


if __name__ == '__main__':
    unittest.main()
