import math
import unittest

from moneyball.marginals import fit_support, support_for_player, tilt_distribution
from moneyball.modeling import ModelingError


class MarginalTests(unittest.TestCase):
    def test_two_point_weights_match_closed_form(self):
        result = tilt_distribution([0, 2], .5)
        self.assertAlmostEqual(result['probabilities'][0], .75)
        self.assertAlmostEqual(result['probabilities'][1], .25)
        self.assertAlmostEqual(result['mean'], .5)
        self.assertAlmostEqual(result['variance'], .75)
        self.assertAlmostEqual(result['ess'], 1/(.75**2+.25**2))

    def test_duplicate_compression_preserves_base_measure_and_ess(self):
        raw = tilt_distribution([0, 0, 0, 4], 1)
        compressed = tilt_distribution({'values':[0,4], 'counts':[3,1]}, 1)
        self.assertEqual(raw['probabilities'], compressed['probabilities'])
        self.assertAlmostEqual(raw['ess'], 4)
        self.assertAlmostEqual(raw['ess_fraction'], 1)

    def test_support_and_negative_scores_preserved(self):
        result = tilt_distribution([-2, 0, 0, 2, 6, 20], .1)
        self.assertEqual(result['values'], [-2,0,2,6,20])
        self.assertTrue(all(p > 0 and math.isfinite(p) for p in result['probabilities']))
        self.assertAlmostEqual(sum(result['probabilities']), 1)
        self.assertAlmostEqual(result['mean'], .1)
        self.assertEqual(result['cdf'][-1], 1)
        self.assertEqual(result['cdf'], sorted(result['cdf']))

    def test_outside_and_boundary_fail_without_silent_clipping(self):
        for target in (-1, 0, 2, 3):
            with self.assertRaises(ModelingError):
                tilt_distribution([0,2], target)
        equal = tilt_distribution([3,3,3], 3)
        self.assertEqual(equal['variance'], 0)
        self.assertEqual(equal['ess'], 3)

    def test_large_scale_and_high_target_stable(self):
        result = tilt_distribution([-1000, 0, 1000], 999)
        self.assertAlmostEqual(result['mean'], 999, places=8)
        self.assertLess(result['ess'], 1.01)
        with self.assertRaises(ModelingError):
            tilt_distribution([0,float('nan')], 0)

    def test_support_fit_filters_future_and_selects_known_bins(self):
        rows = []
        for pid, level in [('a',1),('b',5),('c',10),('d',20)]:
            for week in (1,2):
                rows.append({'player_id':pid,'position':'WR','season':2023,'week':week,
                             'stats':{'rec':level+week-1}})
        rows.append({'player_id':'future','position':'WR','season':2025,'week':1,'stats':{'rec':999}})
        model = fit_support(rows, {'rec':1}, train_seasons=[2023])
        selected = support_for_player(model, {'position':'WR','mean':1.5})
        self.assertEqual(selected['values'], [1,2])
        self.assertEqual(selected['n'], 2)
        self.assertEqual(tilt_distribution(selected,1.5)['mean'], 1.5)
        self.assertNotIn(999, [v for b in model['groups']['WR']['bins'].values() for v in b['values']])


if __name__ == '__main__':
    unittest.main()
