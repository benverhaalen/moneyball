import copy
import unittest

try:
    from moneyball.simulation_fast import simulate as fast
except ImportError:
    fast = None
from moneyball.simulation import simulate as reference


def fixture():
    players, state = [], {}
    pos = ['QB', 'QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'RB', 'WR']
    for team in range(1, 13):
        state[str(team)] = []
        for i, position in enumerate(pos):
            pid = str(team)+':'+str(i)
            state[str(team)].append(pid)
            players.append({'id': pid, 'position': position, 'mean': 100 if team == 1 else 1, 'sd': 0})
    return {'players': players}, state


@unittest.skipIf(fast is None, 'Optional NumPy not installed')
class FastSimulationTests(unittest.TestCase):
    def compare(self, states, projection, **kwargs):
        args = dict(draws=8, years=3, allow_assumptions=True, seed=63)
        args.update(kwargs)
        slow = reference(states, projection, **args)
        quick = fast(states, projection, **args)
        self.assertEqual(slow['scenarios'], quick['scenarios'])
        return quick

    def test_deterministic_league_and_identical_pair_match_reference(self):
        projection, state = fixture()
        result = self.compare({'a': state, 'same': state}, projection)
        self.assertEqual(result['scenarios']['a']['1']['expected_titles']['estimate'], 3)
        self.assertTrue(all(t['paired_delta_expected_titles']['estimate'] == 0 for t in result['scenarios']['same'].values()))

    def test_all_zero_ties_match_reference_keyed_tie_breaks(self):
        projection, state = fixture()
        for player in projection['players']:
            player['mean'] = 0
        self.compare({'a': state}, projection, draws=30)

    def test_zero_annual_transition_removes_noise_and_stays_absorbing(self):
        projection, state = fixture()
        for player in projection['players']:
            player['year_transition_samples'] = [0] if player['id'].startswith('1:') else [1]
            if player['id'].startswith('1:'):
                player['sd'] = 1000
                player['standardized_residuals'] = [1]
        result = self.compare({'a': state}, projection, persistent_shock=True)
        self.assertEqual(result['scenarios']['a']['1']['expected_titles']['estimate'], 1)

    def test_missing_week_zero_does_not_leak_residual_noise(self):
        projection, state = fixture()
        for player in projection['players']:
            if player['id'].startswith('1:'):
                player.update(weekly_means={'1': 100}, sd=1000, standardized_residuals=[1])
        result = self.compare({'a': state}, projection, years=1, missing_forecast='zero')
        self.assertEqual(result['scenarios']['a']['1']['current_championship']['estimate'], 0)
        result = self.compare({'a': state}, projection, years=1, missing_forecast='carry')
        self.assertEqual(result['scenarios']['a']['1']['current_championship']['estimate'], 1)
        with self.assertRaises(ValueError):
            fast({'a': state}, projection, allow_assumptions=True)

    def test_bye_alias_and_no_hindsight_match(self):
        projection, state = fixture()
        for player in projection['players']:
            if player['id'].startswith('1:'):
                player['team'] = 'LAR'
        # Synthetic playoff bye is intentional to expose calendar mistakes.
        schedule = [{'week': w, 'home_team': 'LA', 'away_team': 'SF', 'game_id': str(w)} for w in range(1, 18) if w != 15]
        result = self.compare({'a': state}, projection, nfl_schedule=schedule)
        self.assertEqual(result['scenarios']['a']['1']['expected_titles']['estimate'], 0)

    def test_stochastic_titles_conserved_and_identical_states_pair_exactly(self):
        projection, state = fixture()
        for player in projection['players']:
            player.update(mean=10, sd=8, standardized_residuals=[-1, 0, 1], team='NE', team_loading=.3, game_loading=.2)
        result = fast({'a': state, 'same': state}, projection, draws=128, years=3, allow_assumptions=True)
        for name, rows in result['scenarios'].items():
            self.assertAlmostEqual(sum(t['expected_titles']['estimate'] for t in rows.values()), 3)
            self.assertAlmostEqual(sum(t['current_championship']['estimate'] for t in rows.values()), 1)
            self.assertAlmostEqual(sum(t['qualification_probability'] for t in rows.values()), 8)
            for year in range(3):
                self.assertAlmostEqual(sum(t['championship_by_year'][year]['estimate'] for t in rows.values()),1)
            for stats in rows.values():
                self.assertEqual(stats['championship_by_year'][0],stats['current_championship'])
                self.assertAlmostEqual(sum(y['estimate'] for y in stats['championship_by_year']),stats['expected_titles']['estimate'])
        self.assertTrue(all(t['paired_delta_expected_titles']['estimate'] == 0 for t in result['scenarios']['same'].values()))
        self.assertTrue(all(y['estimate']==0 and y['monte_carlo_se']==0
            for t in result['scenarios']['same'].values() for y in t['paired_delta_championship_by_year']))

    def test_season_timing_and_signed_paired_deltas_match_reference(self):
        projection, state = fixture()
        for player in projection['players']:
            team = int(player['id'].split(':')[0])
            player['mean'] = 100 if team<=3 else 1
            player['year_mean_factors'] = [int(team==y+1) for y in range(3)] if team<=3 else [1,1,1]
        swapped = {team:list(roster) for team,roster in state.items()}
        swapped['1'],swapped['2'] = swapped['2'],swapped['1']
        result = self.compare({'a':state,'swap':swapped},projection)
        self.assertEqual([y['estimate'] for y in result['scenarios']['a']['1']['championship_by_year']],[1,0,0])
        self.assertEqual([y['estimate'] for y in result['scenarios']['swap']['1']['paired_delta_championship_by_year']],[-1,1,0])
        self.assertIn('frozen holdings',result['assumptions']['future_policy'])

    def test_entropy_model_pairing_and_annual_zero(self):
        projection, state = fixture()
        for player in projection['players']:
            player.update(mean=10, team_loading=.7, game_loading=.2,
                          year_transition_samples=[0])
        projection['marginal_support'] = {
            'id': 'test-support',
            'groups': {pos: {'edges': [], 'bins': {'0': {'values': [0, 20], 'counts': [1, 1]}}}
                       for pos in ('QB', 'RB', 'WR', 'TE')},
        }
        result = fast({'a': state, 'same': state}, projection, draws=256,
                      years=3, persistent_shock=True, allow_assumptions=True)
        self.assertTrue(all(t['paired_delta_expected_titles']['estimate'] == 0 for t in result['scenarios']['same'].values()))
        self.assertAlmostEqual(sum(t['expected_titles']['estimate'] for t in result['scenarios']['a'].values()), 3)
        # Later years have literal zero outcomes; every winner must match the
        # reference keyed tie-break process, independent of entropy random draws.
        zero = copy.deepcopy(projection)
        for player in zero['players']:
            player['mean'] = 0
        tied = reference({'a': state}, zero, draws=256, years=3,
                         persistent_shock=True, allow_assumptions=True)
        for team, stats in result['scenarios']['a'].items():
            later = stats['expected_titles']['estimate']-stats['current_championship']['estimate']
            other = tied['scenarios']['a'][team]
            expected_later = other['expected_titles']['estimate']-other['current_championship']['estimate']
            self.assertAlmostEqual(later, expected_later)

    def test_inputs_unchanged_and_ownership_errors_loud(self):
        projection, state = fixture()
        original = copy.deepcopy(projection)
        fast({'a': state}, projection, draws=2, allow_assumptions=True)
        self.assertEqual(projection, original)
        state['2'].append(state['1'][0])
        with self.assertRaises(ValueError):
            fast({'a': state}, projection, draws=2, allow_assumptions=True)
        with self.assertRaises(ValueError):
            fast({}, projection)


if __name__ == '__main__':
    unittest.main()
