import math
import unittest

from moneyball.uncertainty import (candidate_race, paired_interval,
                                   required_independent_samples)


class UncertaintyTests(unittest.TestCase):
    def test_known_fixed_n_bound(self):
        got = paired_interval([0.1, 0.3, 0.2, 0.4], anytime=False)
        self.assertAlmostEqual(got['mean'], 0.25)
        self.assertAlmostEqual(got['unclipped_radius'], 2*math.sqrt(math.log(40)/8))
        self.assertAlmostEqual(got['standard_error'], math.sqrt(1/60)/2)

    def test_anytime_and_multiple_candidates_widen(self):
        samples = [0.1, 0.2]*50
        fixed = paired_interval(samples, anytime=False)
        anytime = paired_interval(samples)
        multiple = paired_interval(samples, comparisons=28)
        self.assertLess(fixed['unclipped_radius'], anytime['unclipped_radius'])
        self.assertLess(anytime['unclipped_radius'], multiple['unclipped_radius'])
        self.assertAlmostEqual(anytime['unclipped_radius'],
                               2*math.sqrt(math.log(2*100*101/.05)/200))

    def test_empty_and_single_sample_are_not_fake_certainty(self):
        got = paired_interval([], lower=-3, upper=3)
        self.assertEqual((got['lower'], got['upper']), (-3, 3))
        self.assertIsNone(got['mean'])
        got = paired_interval([0])
        self.assertIsNone(got['standard_error'])
        self.assertGreater(got['upper'], got['lower'])

    def test_constant_output_has_zero_se_but_positive_bound(self):
        got = paired_interval([.2]*16)
        self.assertEqual(got['standard_error'], 0)
        self.assertGreater(got['unclipped_radius'], 0)

    def test_reject_bad_parameters_and_observations(self):
        for kwargs in ({'lower': 1, 'upper': 1}, {'lower': math.nan},
                       {'alpha': 0}, {'alpha': 1}, {'comparisons': 0},
                       {'comparisons': 1.5}, {'comparisons': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                paired_interval([0], **kwargs)
        for samples in ([1.1], [math.nan], [math.inf]):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                paired_interval(samples)

    def test_budget_is_smallest_bound_satisfying_request(self):
        for anytime in (False, True):
            n = required_independent_samples(.1, comparisons=6, anytime=anytime)
            got = paired_interval([0]*n, comparisons=6, anytime=anytime)
            before = paired_interval([0]*(n-1), comparisons=6, anytime=anytime)
            self.assertLessEqual(got['unclipped_radius'], .1)
            self.assertGreater(before['unclipped_radius'], .1)

    def test_small_block_count_cannot_resolve(self):
        got = candidate_race({'a': [1]*16, 'b': [0]*16})
        self.assertEqual(got['status'], 'unresolved')
        self.assertIsNone(got['certified_winner'])
        self.assertEqual(got['n_independent_units'], 16)

    def test_eliminate_only_positive_simultaneous_paired_bound(self):
        got = candidate_race({'a': [1]*1000, 'b': [0]*1000, 'c': [.4]*1000})
        self.assertEqual(got['certified_winner'], 'a')
        self.assertEqual(got['survivors'], ['a'])
        self.assertEqual(got['simultaneous_comparisons'], 3)
        self.assertIn('a', got['eliminated']['b'])
        self.assertTrue(all('scope' in row['a_minus_b'] for row in got['paired_intervals']))

    def test_minimum_samples_and_practical_margin_are_respected(self):
        samples = {'a': [1]*1000, 'b': [.5]*1000}
        self.assertIsNone(candidate_race(samples, minimum_samples=1001)['certified_winner'])
        self.assertIsNone(candidate_race(samples, practical_margin=.9)['certified_winner'])

    def test_pairing_preserves_common_random_numbers(self):
        got = candidate_race({'a': [0, 1]*50, 'b': [0, 1]*50})
        diff = got['paired_intervals'][0]['a_minus_b']
        self.assertEqual(diff['mean'], 0)
        self.assertEqual(diff['standard_error'], 0)
        self.assertEqual(got['survivors'], ['a', 'b'])

    def test_family_does_not_shrink_when_candidates_removed(self):
        samples = {'a': [1]*100, 'b': [0]*100}
        full = candidate_race(samples, family_size=8)
        smaller = candidate_race(samples)
        self.assertEqual(full['simultaneous_comparisons'], 28)
        self.assertGreater(full['paired_intervals'][0]['a_minus_b']['unclipped_radius'],
                           smaller['paired_intervals'][0]['a_minus_b']['unclipped_radius'])

    def test_reject_unpaired_and_invalid_candidate_data(self):
        for samples, kwargs in (({}, {}), ({'a': [0], 'b': []}, {}),
                ({'a': [math.nan]}, {}), ({'a': [2]}, {}),
                ({'a': [0], 'b': [0]}, {'family_size': 1}),
                ({'a': [0]}, {'minimum_samples': 1}),
                ({'a': [0]}, {'practical_margin': -1})):
            with self.subTest(samples=samples, kwargs=kwargs), self.assertRaises(ValueError):
                candidate_race(samples, **kwargs)

    def test_single_candidate_never_certifies_unseen_comparisons(self):
        self.assertIsNone(candidate_race({'a': [1]*1000})['certified_winner'])


if __name__ == '__main__':
    unittest.main()
