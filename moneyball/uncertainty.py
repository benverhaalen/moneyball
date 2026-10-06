"""Auditable confidence bounds for independent, bounded simulation blocks.

These intervals describe Monte Carlo integration error conditional on one frozen
model and observed draft prefix. They do not establish that the model is correct.
If a block first samples a draft completion and then averages inner simulations,
the block average is ONE observation. Repeated inner outcomes do not make the
shared completion independent. Freeze candidates, model, and block design before
sampling; after observing a new real pick, start a new stream for the new state.

Default: two-sided Hoeffding (1963, Theorem 1, eq. 2.3), with a union bound over
K predeclared comparisons and sample sizes n, spending alpha/[K*n*(n+1)]. Since
sum(1/[n*(n+1)], n=1..infinity) = 1, all returned intervals cover simultaneously
with probability >= 1-alpha under the stated assumptions. This is a deliberately
simple confidence sequence, not Howard et al.'s sharper mixture implementation.
"""

import itertools
import math
import statistics


SCOPE = ('Monte Carlo integration uncertainty conditional on a fixed model, '
         'candidate family, observed draft prefix, and independent sampling blocks; '
         'excludes forecasting error, opponent-model error, and rule uncertainty')


def _parameters(lower, upper, alpha, comparisons):
    lower, upper, alpha = float(lower), float(upper), float(alpha)
    if not all(math.isfinite(x) for x in (lower, upper, alpha)) or lower >= upper:
        raise ValueError('Finite lower < upper bounds are required')
    if not 0 < alpha < 1:
        raise ValueError('alpha must be between zero and one')
    if isinstance(comparisons, bool) or not isinstance(comparisons, int) or comparisons < 1:
        raise ValueError('comparisons must be a positive, predeclared integer')
    return lower, upper, alpha, comparisons


def _radius(n, lower, upper, alpha, comparisons, anytime):
    # Logs avoid overflow in 2*K*n*(n+1)/alpha for large sampling budgets.
    log_ratio = math.log(2) + math.log(comparisons) - math.log(alpha)
    if anytime:
        log_ratio += math.log(n) + math.log(n + 1)
    return (upper - lower) * math.sqrt(log_ratio / (2 * n))


def paired_interval(samples, *, lower=-1.0, upper=1.0, alpha=0.05,
                    comparisons=1, anytime=True):
    """Bound the mean of already paired differences from independent blocks.

    For one-year champion indicators, differences lie in [-1, 1]. For an
    undiscounted sum of Y championships, they lie in [-Y, Y]. The supplied bounds
    must hold by construction, not be estimated from the observed min and max.

    ``comparisons`` covers every predeclared contrast inspected in this sampling
    family. ``anytime=False`` is valid only at a sample count fixed before seeing
    samples; using it to decide when to stop sampling invalidates that guarantee.
    ``standard_error`` is descriptive and is NOT the confidence-bound radius.
    """
    lower, upper, alpha, comparisons = _parameters(lower, upper, alpha, comparisons)
    values = [float(x) for x in samples]
    if any(not math.isfinite(x) or not lower <= x <= upper for x in values):
        raise ValueError('Every sample must be finite and inside the declared bounds')
    n = len(values)
    mean = statistics.fmean(values) if n else None
    se = statistics.stdev(values) / math.sqrt(n) if n > 1 else None
    radius = _radius(n, lower, upper, alpha, comparisons, anytime) if n else None
    return {
        'n': n,
        'n_independent_units': n,
        'mean': mean,
        'standard_error': se,
        'lower': max(lower, mean - radius) if n else lower,
        'upper': min(upper, mean + radius) if n else upper,
        'unclipped_radius': radius,
        'sample_bounds': [lower, upper],
        'family_confidence': 1 - alpha,
        'comparisons': comparisons,
        'anytime_valid': bool(anytime),
        'method': 'hoeffding_alpha_spending' if anytime else 'fixed_n_hoeffding',
        'scope': SCOPE,
        'warning': ('Bounds can be very wide at small n; simulation precision '
                    'does not measure calibration or an advantage in the real league'),
    }


def required_independent_samples(half_width, *, lower=-1.0, upper=1.0,
                                 alpha=0.05, comparisons=1, anytime=True):
    """Conservative budget for an UNCLIPPED bound half-width, not a power claim.

    Counts independent outer blocks for a nested simulation. Narrowing this
    interval cannot remove errors in the assumed outcome or opponent models.
    """
    lower, upper, alpha, comparisons = _parameters(lower, upper, alpha, comparisons)
    half_width = float(half_width)
    if not math.isfinite(half_width) or half_width <= 0:
        raise ValueError('half_width must be finite and positive')
    hi = 1
    while _radius(hi, lower, upper, alpha, comparisons, anytime) > half_width:
        hi *= 2
    lo = max(1, hi // 2)
    while lo < hi:
        mid = (lo + hi) // 2
        if _radius(mid, lower, upper, alpha, comparisons, anytime) <= half_width:
            hi = mid
        else:
            lo = mid + 1
    return lo


def candidate_race(candidate_samples, *, lower=0.0, upper=1.0, alpha=0.05,
                   family_size=None, minimum_samples=32, practical_margin=0.0):
    """Eliminate only candidates beaten by simultaneous paired sequences.

    Mapping values contain aligned candidate outcomes/averages for the SAME
    independent blocks, using common random numbers where possible. Candidates
    must be fixed before these draws. ``family_size`` is the number of candidates
    reserved BEFORE sampling (not the number surviving). It must never shrink.

    This minimal implementation samples every candidate on every block. An
    adaptive scheduler may pause a predeclared contrast and use its own paired
    sample count, but must retain its allocation of family error. Never create a
    fresh low-alpha family after examining the old one to obtain certification.
    """
    if not candidate_samples:
        raise ValueError('At least one candidate is required')
    if any(not isinstance(name, str) for name in candidate_samples):
        raise ValueError('Candidate names must be strings')
    names = sorted(candidate_samples)
    if family_size is None:
        family_size = len(names)
    if isinstance(family_size, bool) or not isinstance(family_size, int) or family_size < len(names):
        raise ValueError('family_size must include every candidate considered')
    if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or minimum_samples < 2:
        raise ValueError('minimum_samples must be an integer of at least two')
    practical_margin = float(practical_margin)
    if not math.isfinite(practical_margin) or practical_margin < 0:
        raise ValueError('practical_margin must be finite and nonnegative')
    comparisons = max(1, family_size * (family_size - 1) // 2)
    lower, upper, alpha, comparisons = _parameters(lower, upper, alpha, comparisons)
    samples = {name: [float(x) for x in candidate_samples[name]] for name in names}
    lengths = {len(v) for v in samples.values()}
    if len(lengths) != 1:
        raise ValueError('Candidate samples must be aligned, equally sized blocks')
    if any(not math.isfinite(x) or not lower <= x <= upper for v in samples.values() for x in v):
        raise ValueError('Candidate outcomes must lie within declared bounds')
    n = lengths.pop()
    pairs, defeated_by = [], {name: [] for name in names}
    regret_bounds = {name: 0.0 for name in names}
    for a, b in itertools.combinations(names, 2):
        interval = paired_interval((x-y for x, y in zip(samples[a], samples[b])),
            lower=lower-upper, upper=upper-lower, alpha=alpha,
            comparisons=comparisons, anytime=True)
        pairs.append({'a': a, 'b': b, 'a_minus_b': interval})
        regret_bounds[a] = max(regret_bounds[a], -interval['lower'])
        regret_bounds[b] = max(regret_bounds[b], interval['upper'])
        if n >= minimum_samples:
            if interval['lower'] > practical_margin:
                defeated_by[b].append(a)
            elif interval['upper'] < -practical_margin:
                defeated_by[a].append(b)
    survivors = [name for name in names if not defeated_by[name]]
    means = {name: statistics.fmean(v) if n else None for name, v in samples.items()}
    leader = max(names, key=lambda name: (means[name], name)) if n else None
    # A one-candidate input certifies nothing about alternatives never simulated.
    certified = (survivors[0] if len(names) > 1 and len(survivors) == 1
                 and n >= minimum_samples else None)
    return {
        'n_independent_units': n,
        'family_size': family_size,
        'simultaneous_comparisons': comparisons,
        'family_confidence': 1-alpha,
        'minimum_samples': minimum_samples,
        'practical_margin': practical_margin,
        'candidate_means': means,
        'point_estimate_leader': leader,
        'certified_winner': certified,
        'status': 'resolved_under_model' if certified else 'unresolved',
        'survivors': survivors,
        'eliminated': {name: winners for name, winners in defeated_by.items() if winners},
        'max_regret_upper_bounds': regret_bounds,
        'paired_intervals': pairs,
        'scope': SCOPE,
        'deadline_rule': ('At the time budget, return unresolved survivors and a '
                          'clearly labeled point-estimate choice; do not lower '
                          'the confidence threshold to manufacture a winner'),
    }
