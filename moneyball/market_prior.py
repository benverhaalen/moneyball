"""Explicit prior sensitivity for incomplete market-implied distributions.

Market thresholds divide the support into intervals. Minimum relative entropy
changes the probability of each interval, retaining the prior shape within it.
The upper tail remains infinite; a convenient hard ceiling is never invented.
These are conditional models, not empirically calibrated confidence intervals.
"""
import math

from .market_constraints import entropy_projection


class Prior:
    """Declared distribution; width is an assumption unless separately validated."""

    def __init__(self, family, mean, dispersion, zero_mass=0.):
        from scipy import stats
        if not all(math.isfinite(v) and v > 0 for v in (mean, dispersion)):
            raise ValueError('Positive finite mean and dispersion required')
        if not math.isfinite(zero_mass) or not 0 <= zero_mass < 1:
            raise ValueError('Explicit structural-zero probability must be in [0, 1)')
        self.family, self.mean, self.dispersion = family, mean, dispersion
        self.zero_mass = zero_mass
        mean = mean / (1 - zero_mass)
        if family == 'lognormal':
            self.sigma = math.sqrt(math.log1p(dispersion ** 2))
            self.mu = math.log(mean) - self.sigma ** 2 / 2
            self.rv = stats.lognorm(self.sigma, scale=math.exp(self.mu))
        elif family == 'gamma':
            self.shape, self.scale = 1 / dispersion ** 2, mean * dispersion ** 2
            self.rv = stats.gamma(self.shape, scale=self.scale)
        elif family == 'negative_binomial':
            self.shape = 1 / dispersion ** 2
            self.p = self.shape / (self.shape + mean)
            self.rv = stats.nbinom(self.shape, self.p)
        else:
            raise ValueError('Unsupported prior family')

    @property
    def discrete(self):
        return self.family == 'negative_binomial'

    def boundary(self, x):
        # A continuous approximation to integer yardage evaluates P(X <= k)
        # at k + .5; counts use the actual integer CDF.
        return x if self.discrete else x + .5

    def descriptor(self):
        return {'family': self.family, 'mean': self.mean,
                'dispersion': self.dispersion,
                'structural_zero_mass': self.zero_mass,
                'zero_mass_interpretation': 'Explicit sensitivity assumption, separate from distributional zeros; positive-component mean adjusts to retain the declared overall mean.',
                'dispersion_definition': ('Latent intensity CV; total variance = mean + dispersion^2 * mean^2'
                    if self.discrete else 'Coefficient of variation'),
                'support': 'nonnegative integers' if self.discrete else 'nonnegative continuous yardage approximation',
                'width_calibrated': False,
                'negative_yardage_tail_excluded': not self.discrete}

    def cdf(self, x):
        return (1 - self.zero_mass) * self.rv.cdf(x) + self.zero_mass * (x >= 0)

    def sf(self, x):
        return (1 - self.zero_mass) * self.rv.sf(x) + self.zero_mass * (x < 0)

    def ppf(self, q):
        return 0. if q <= self.cdf(0) else self.rv.ppf((q - self.zero_mass) / (1 - self.zero_mass))

    def isf(self, q):
        return 0. if q >= self.sf(0) else self.rv.isf(q / (1 - self.zero_mass))

    def _interval_probability(self, rv, lo, hi):
        if lo == -math.inf:
            return float(rv.cdf(hi))
        if hi == math.inf:
            return float(rv.sf(lo))
        # Use survival differences in the upper tail to avoid cancellation.
        if rv.cdf(lo) > .5:
            return float(rv.sf(lo) - rv.sf(hi))
        return float(rv.cdf(hi) - rv.cdf(lo))

    def interval_moment(self, lo, hi, order):
        """E[X**order * 1(lo < X <= hi)] for order 0..3, including tails."""
        from scipy import stats
        if order not in range(4) or not lo < hi:
            raise ValueError('Ordered interval and moment order 0..3 required')
        if order == 0:
            return (1 - self.zero_mass) * self._interval_probability(self.rv, lo, hi) + self.zero_mass * (lo < 0 <= hi)
        if self.family == 'lognormal':
            shifted = stats.lognorm(self.sigma, scale=math.exp(self.mu + order * self.sigma ** 2))
            factor = math.exp(order * self.mu + order ** 2 * self.sigma ** 2 / 2)
            return (1 - self.zero_mass) * factor * self._interval_probability(shifted, lo, hi)
        if self.family == 'gamma':
            shifted = stats.gamma(self.shape + order, scale=self.scale)
            factor = self.scale ** order * math.prod(self.shape + j for j in range(order))
            return (1 - self.zero_mass) * factor * self._interval_probability(shifted, lo, hi)
        # Raw powers expressed using falling factorials; shifting a negative
        # binomial factorial moment adds its order to shape and the count.
        coefficients = {1: {1: 1}, 2: {1: 1, 2: 1}, 3: {1: 1, 2: 3, 3: 1}}[order]
        result = 0.
        for j, coefficient in coefficients.items():
            shifted = stats.nbinom(self.shape + j, self.p)
            factor = math.prod(self.shape + k for k in range(j)) * ((1 - self.p) / self.p) ** j
            result += coefficient * factor * self._interval_probability(shifted, lo - j, hi - j)
        return (1 - self.zero_mass) * result


def condition_prior(curve, prior):
    """Fit one explicitly selected source/settlement curve, keeping all tails."""
    if not curve['feasible'] or not curve['points']:
        raise ValueError('Nonempty, feasible market curve required')
    points = curve['points']
    edges = [-math.inf] + [prior.boundary(p['x']) for p in points] + [math.inf]
    intervals = list(zip(edges, edges[1:]))
    masses = [prior.interval_moment(lo, hi, 0) for lo, hi in intervals]
    if any(p <= 0 for p in masses):
        raise ValueError('Prior has numerically unresolved interval mass; widen support model or omit fit explicitly')
    # A representative of each interval is enough for threshold constraints.
    # Actual moments below use the entire conditional prior, not this proxy.
    representatives = [p['x'] for p in points] + [points[-1]['x'] + 1]
    fit = entropy_projection(curve, representatives, masses, tolerance=1e-7)
    weights = fit['weights']
    moments = [sum(w * prior.interval_moment(lo, hi, k) / mass
                   for w, mass, (lo, hi) in zip(weights, masses, intervals)) for k in (1, 2, 3)]
    mean, second, third = moments
    variance = max(0., second - mean ** 2)
    skew = (third - 3 * mean * second + 2 * mean ** 3) / variance ** 1.5 if variance else None
    quantiles = []
    for q in (.05, .1, .25, .5, .75, .9, .95):
        cumulative = 0.
        for w, mass, (lo, hi) in zip(weights, masses, intervals):
            if cumulative + w >= q:
                fraction = (q - cumulative) / w
                # isf avoids loss of precision when a bin is deep in a tail.
                if prior.cdf(lo) > .5:
                    value = float(prior.isf(prior.sf(lo) - fraction * mass))
                else:
                    value = float(prior.ppf(prior.cdf(lo) + fraction * mass))
                quantiles.append({'probability': q, 'value': value})
                break
            cumulative += w
    return {'prior': prior.descriptor(), 'mean': mean, 'variance': variance, 'skew': skew,
            'quantiles': quantiles, 'thresholds': [p['x'] for p in points],
            'prior_interval_masses': masses, 'posterior_interval_masses': weights,
            'posterior_zero_output_probability': weights[0] * float(prior.cdf(0)) / masses[0]
                if points[0]['x'] >= 0 else None,
            'kl_divergence': fit['kl_divergence'],
            'max_constraint_violation': fit['max_constraint_violation'],
            'intervals': [{'lower_exclusive': None if lo == -math.inf else lo,
                           'upper_inclusive': None if hi == math.inf else hi} for lo, hi in intervals],
            'confidence_interval': None, 'empirically_calibrated': False,
            'interpretation': 'Model-conditional distribution under declared prior, market probability interpretation, and action/settlement assumptions. Quantiles are predictive-model quantiles, not confidence limits. No cross-stat dependence inferred.'}
