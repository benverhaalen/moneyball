import math
import unittest

from moneyball.market_constraints import envelope
from moneyball.market_prior import Prior, condition_prior


class MarketPriorTests(unittest.TestCase):
    def test_complete_moments_match_scipy(self):
        for family in ('lognormal', 'gamma', 'negative_binomial'):
            prior = Prior(family, 20, .4)
            for order in range(4):
                self.assertAlmostEqual(prior.interval_moment(-math.inf, math.inf, order),
                                       float(prior.rv.moment(order)), places=7)

    def test_negative_binomial_partial_moments(self):
        prior = Prior('negative_binomial', 8, .5)
        for order in range(4):
            expected = sum(k ** order * prior.rv.pmf(k) for k in range(4, 12))
            self.assertAlmostEqual(prior.interval_moment(3, 11, order), expected, places=8)

    def test_continuous_partial_moments_numerical_integration(self):
        from scipy.integrate import quad
        for family in ('gamma', 'lognormal'):
            prior = Prior(family, 15, .7)
            for order in (1, 2, 3):
                expected = quad(lambda x: x ** order * prior.rv.pdf(x), 2, 26)[0]
                self.assertAlmostEqual(prior.interval_moment(2, 26, order), expected, places=7)

    def test_already_compatible_prior_is_unchanged(self):
        for family in ('gamma', 'lognormal', 'negative_binomial'):
            prior = Prior(family, 30, .4)
            cdf = float(prior.rv.cdf(prior.boundary(28)))
            curve = envelope([{'x': 28, 'cdf_low': cdf - .01, 'cdf_high': cdf + .01}])
            fit = condition_prior(curve, prior)
            self.assertAlmostEqual(fit['mean'], 30, places=6)
            self.assertAlmostEqual(fit['variance'], prior.rv.var(), places=6)

    def test_one_threshold_changes_mass_not_truncates_tail(self):
        prior = Prior('gamma', 40, .4)
        fit = condition_prior(envelope([{'x': 25, 'cdf_low': .5, 'cdf_high': .5}]), prior)
        self.assertAlmostEqual(fit['posterior_interval_masses'][0], .5, places=6)
        self.assertIsNone(fit['intervals'][-1]['upper_inclusive'])
        self.assertLess(fit['mean'], 40)
        self.assertIsNone(fit['confidence_interval'])

    def test_empty_or_inconsistent_constraints_fail(self):
        prior = Prior('gamma', 40, .4)
        for curve in (envelope([]), envelope([{'x': 10, 'cdf_low': .8, 'cdf_high': .9},
                                              {'x': 20, 'cdf_low': .1, 'cdf_high': .2}])):
            with self.assertRaises(ValueError):
                condition_prior(curve, prior)

    def test_structural_zero_is_preserved_and_mean_retained(self):
        for family in ('gamma', 'lognormal', 'negative_binomial'):
            prior = Prior(family, 20, .4, zero_mass=.3)
            self.assertAlmostEqual(prior.interval_moment(-math.inf, math.inf, 0), 1)
            self.assertAlmostEqual(prior.interval_moment(-math.inf, math.inf, 1), 20)
            self.assertEqual(prior.ppf(.2), 0)
            cdf = float(prior.cdf(prior.boundary(15)))
            fit = condition_prior(envelope([{'x': 15, 'cdf_low': cdf - .001, 'cdf_high': cdf + .001}]), prior)
            self.assertAlmostEqual(fit['mean'], 20, places=6)
            self.assertEqual(fit['quantiles'][0]['value'], 0)


if __name__ == '__main__':
    unittest.main()
