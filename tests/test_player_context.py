import copy
import unittest

from moneyball.player_context import DATASETS, build_context, decompose, ratio, summed
from moneyball.warehouse import LeakageError, ValidationError


def row(dataset, **values):
    batch = DATASETS.index(dataset) + 1
    return {**values, '_provenance': {'dataset': dataset, 'source_id': 'nflverse_' + dataset,
        'batch_id': batch, 'raw_id': batch, 'observed_at': 50, 'available_at': 50,
        'historical_vintage_unknown': True, 'content_sha256': 'test'}}


class PlayerContextTests(unittest.TestCase):
    def setUp(self):
        self.players = [{'player_id': 'sleeper1', 'name': 'Example Receiver', 'position': 'WR',
                         'gsis_id': ' 00-example '}]
        self.data = {dataset: [] for dataset in DATASETS}
        self.data['players'] = [row('players', gsis_id='00-example', pfr_id=' Example01 ', identity_verified=True)]
        self.data['weekly'] = [row('weekly', player_id='00-example ', gsis_id='00-example ',
            season=2025, season_type='REG', week=1, game_id='g1', team='LA', position='WR',
            targets=10, receptions=6, receiving_yards=80, receiving_tds=1, carries=0, attempts=0),
            row('weekly', player_id='00-other', gsis_id='00-other', season=2025, season_type='REG',
            week=1, game_id='g1', team='LAR', position='RB', targets=5, receptions=4,
            receiving_yards=20, receiving_tds=0, carries=10, attempts=0)]

    def result(self):
        return build_context(self.players, self.data, cutoff=100)

    def profile(self):
        return self.result()['players']['sleeper1']

    def test_ratio_of_sums_preserves_denominator_and_missing_pairs(self):
        metric = ratio([{'a': 1, 'b': 1}, {'a': 1, 'b': 9}, {'a': None, 'b': 100}], 'a', 'b')
        self.assertEqual(metric['value'], .2)
        self.assertEqual(metric['numerator'], 2)
        self.assertEqual(metric['denominator'], 10)
        self.assertEqual(metric['missing_pair_rows'], 1)
        self.assertIsNone(ratio([], 'a', 'b')['value'])
        self.assertIsNone(summed([], 'a')['value'])

    def test_whitespace_exact_ids_and_team_alias_share(self):
        s = self.profile()['seasons']['2025']
        self.assertEqual(s['workload_and_output_counts']['targets']['value'], 10)
        self.assertAlmostEqual(s['team_opportunity_shares']['targets']['value'], 2 / 3)
        self.assertEqual(s['execution_rates']['receiving_yards_per_target']['value'], 8)

    def test_missing_history_does_not_become_injury_or_zero(self):
        s = self.profile()['seasons']['2023']
        self.assertEqual(s['n_observed_box_score_rows'], 0)
        self.assertIsNone(s['workload_and_output_counts']['targets']['value'])
        self.assertIsNone(s['execution_rates']['catch_rate']['value'])

    def test_duplicate_normalized_identity_and_future_provenance_fail(self):
        self.data['weekly'].append(copy.deepcopy(self.data['weekly'][0]))
        with self.assertRaisesRegex(ValidationError, 'Duplicate'):
            self.result()
        self.data['weekly'].pop()
        self.data['weekly'][0]['_provenance']['available_at'] = 101
        with self.assertRaises(LeakageError):
            self.result()

    def test_ngs_season_summary_is_not_added_to_weekly_and_denominator_exposed(self):
        for week, attempts, over, rate in [(0, 100, 180, 2), (1, 10, 16, 2)]:
            self.data['ngs_rushing'].append(row('ngs_rushing', player_id='00-example', season=2025,
                season_type='REG', week=week, rush_attempts=attempts, rush_yards_over_expected=over,
                rush_yards_over_expected_per_att=rate))
        r = self.profile()['seasons']['2025']['ngs']['rushing']
        self.assertEqual(r['n_published_weekly_rows'], 1)
        self.assertEqual(r['weekly_exposure_sum']['value'], 10)
        self.assertEqual(r['native_season_summary']['counts']['rush_attempts'], 100)
        self.assertEqual(r['native_season_summary']['inferred_tracking_attempts_from_ryoe_identity'], 90)

    def test_native_ngs_count_disagreement_is_exposed_not_overwritten(self):
        self.data['ngs_receiving'] = [row('ngs_receiving', player_id='00-example', season=2025,
            season_type='REG', week=0, targets=10, receptions=6, yards=79, rec_touchdowns=1)]
        summary = self.profile()['seasons']['2025']['ngs']['receiving']['native_season_summary']
        check = summary['box_score_count_reconciliation']['yards']
        self.assertEqual(summary['counts']['yards'], 79)
        self.assertEqual(check['weekly_box_sum'], 80)
        self.assertEqual(check['difference'], -1)
        self.assertEqual(check['status'], 'source_difference_requires_review')

    def test_snap_share_requires_full_snap_witness_and_is_not_route_share(self):
        self.data['snap_counts'] = [row('snap_counts', pfr_player_id='Example01', season=2025,
            game_type='REG', week=1, game_id='g1', team='LAR', offense_snaps=30, offense_pct=.5)]
        s = self.profile()['seasons']['2025']['snaps']
        self.assertIsNone(s['offense_share']['value'])
        self.assertIsNone(s['route_share'])
        self.data['snap_counts'].append(row('snap_counts', pfr_player_id='Lineman01', season=2025,
            game_type='REG', week=1, game_id='g1', team='LAR', offense_snaps=60, offense_pct=1))
        s = self.profile()['seasons']['2025']['snaps']
        self.assertEqual(s['offense_share']['value'], .5)
        self.assertEqual(s['offense_share']['denominator'], 60)

    def test_ftn_join_requires_real_eligible_target_and_exact_play(self):
        self.data['ftn_charting'] = [row('ftn_charting', game_id='g1', play_id='40', season=2025,
            is_catchable_ball=True, is_contested_ball=False, is_created_reception=False, is_drop=False)]
        self.data['pbp'] = [row('pbp', game_id='g1', play_id=40, season=2025, season_type='REG',
            play_type='pass', pass_attempt=1, sack=0, receiver_player_id=' 00-example ', passer_player_id='00-qb')]
        r = self.profile()['seasons']['2025']['ftn_charting']['receiving']
        self.assertEqual(r['n_joined_eligible_plays'], 1)
        self.assertEqual(r['rates']['is_catchable_ball']['numerator'], 1)
        self.data['pbp'][0]['qb_spike'] = 1
        self.assertEqual(self.profile()['seasons']['2025']['ftn_charting']['receiving']['n_joined_eligible_plays'], 0)

    def test_decomposition_conserves_change_without_age_effect(self):
        result = decompose([{'yards': 100, 'targets': 10}], [{'yards': 240, 'targets': 20}], 'yards', 'targets')
        self.assertEqual(result['opportunity_count_component'], 110)
        self.assertEqual(result['output_per_opportunity_component'], 30)
        self.assertEqual(result['total_output_change'], 140)
        self.assertEqual(result['status'], 'descriptive_arithmetic_only')

    def test_draft_outcomes_excluded_and_current_roster_status_retained(self):
        self.data['draft_picks'] = [row('draft_picks', gsis_id='00-example', season=2020,
            round=2, pick=40, team='LAR', college='Example U', age=21, games=100, car_av=70)]
        self.data['rosters'] = [row('rosters', gsis_id='00-example', season=2026,
            identity_verified=True, team='LAR', week=1, position='WR', status='ACT'),
            row('rosters', gsis_id='00-other', season=2026, identity_verified=True,
            team='LAR', week=1, position='WR', full_name='Other Receiver', status='RES')]
        p = self.profile()
        self.assertNotIn('car_av', p['draft_evidence']['records'][0])
        self.assertNotIn('games', p['draft_evidence']['records'][0])
        self.assertEqual(p['current_team_context']['other_offensive_roster_members'][0]['status'], 'RES')

    def test_nonfinite_or_negative_denominator_fail_loudly(self):
        with self.assertRaises(ValidationError):
            ratio([{'a': 2, 'b': -1}], 'a', 'b')
        with self.assertRaises(ValidationError):
            summed([{'a': float('nan')}], 'a')


if __name__ == '__main__':
    unittest.main()
