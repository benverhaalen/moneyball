import unittest
from moneyball.market_constraints import (envelope, quantile_bounds,
    finite_support_mean_bounds,anchored_alternate_probability,entropy_projection)


class ConstraintsTests(unittest.TestCase):
    def test_inconsistent_quotes_do_not_get_averaged(self):
        c=envelope([{'x':99,'cdf_low':.9,'cdf_high':.95},
                    {'x':100,'cdf_low':.3,'cdf_high':.4}])
        self.assertFalse(c['feasible']);self.assertTrue(c['violations'])

    def test_monotone_envelope_and_unidentified_tails(self):
        c=envelope([{'x':9,'cdf_low':.3,'cdf_high':.7},
                    {'x':19,'cdf_low':.2,'cdf_high':.5}])
        self.assertEqual(c['points'][0]['cdf_high'],.5)
        self.assertEqual(c['points'][1]['cdf_low'],.3)
        self.assertIsNone(quantile_bounds(c,.1)['lower'])
        self.assertEqual(quantile_bounds(c,.1)['upper'],9)
        self.assertEqual(quantile_bounds(c,.9)['lower'],20)
        self.assertIsNone(quantile_bounds(c,.9)['upper'])

    def test_mean_requires_support_and_median_not_mean(self):
        c=envelope([{'x':49,'cdf_low':.5,'cdf_high':.5}])
        b=finite_support_mean_bounds(c,0,100)
        self.assertEqual([b['lower'],b['upper']],[25,74.5])

    def test_anchored_margin_consistent_at_mainline_not_a_paired_alt(self):
        for method in ['multiplicative','additive','power']:
            r=anchored_alternate_probability(-110,-110,-110,method)
            self.assertAlmostEqual(r['over'],.5)
            self.assertFalse(r['paired_alternate']);self.assertFalse(r['empirically_calibrated'])

    def test_entropy_matches_closed_form_reweighting(self):
        c=envelope([{'x':1,'cdf_low':.75,'cdf_high':.75}])
        r=entropy_projection(c,[0,1,2,3],[.25]*4)
        for a,b in zip(r['weights'],[.375,.375,.125,.125]):self.assertAlmostEqual(a,b,places=6)
        self.assertAlmostEqual(r['mean'],1,places=6)

    def test_entropy_feasible_prior_unchanged(self):
        c=envelope([{'x':1,'cdf_low':.4,'cdf_high':.6}])
        self.assertEqual(entropy_projection(c,[0,1,2,3],[.25]*4)['weights'],[.25]*4)

    def test_missing_tail_support_rejected(self):
        c=envelope([{'x':10,'cdf_low':.4,'cdf_high':.6}])
        with self.assertRaises(ValueError):entropy_projection(c,[0,1,2],[1,1,1])
