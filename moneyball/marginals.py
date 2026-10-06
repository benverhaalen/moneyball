"""Support-preserving distribution sensitivity around professional means.

One-dimensional exponential reweighting follows Hainmueller (2012), §§3.1–3.6,
especially Eq.7: https://www.mit.edu/~jhainm/Paper/eb.pdf (read 2026-09-07).
Only the constrained weighting math transfers. His causal evidence does not
validate these fantasy outcome distributions or a Gaussian factor copula.
"""
from collections import Counter, defaultdict
import math
import statistics
import time

from .modeling import (ModelingError, POSITIONS, CORE_OFFENSE, _id, _group,
                       _position, _regular, _score, _availability, _num,
                       measure_variance_bins, temporal_audit)
from .store import digest


def fit_support(rows, scoring, *, train_seasons, cutoff=None):
    """Retain measured score support in position/observed-output quartile bins."""
    years = sorted(set(map(int, train_seasons)))
    selected = [r for r in rows if int(r['season']) in years and _regular(r)
                and _position(r) in POSITIONS and _availability(r) != 0]
    if not years or not selected:
        raise ModelingError('Nonempty explicit training seasons and observations required')
    units, seen, missing = defaultdict(list), set(), Counter()
    for row in selected:
        key = (_id(row), int(row['season']), int(row['week']))
        if key in seen:
            raise ModelingError('Duplicate score-support observation')
        seen.add(key)
        score = _score(row, scoring)
        missing.update(score['missing_scoring_keys'])
        if CORE_OFFENSE & set(score['missing_scoring_keys']):
            raise ModelingError('Missing core scoring components in score support')
        units[(_group(row), _id(row), int(row['season']))].append(score['points'])
    diagnostics = measure_variance_bins(selected, scoring)
    pools = defaultdict(Counter)
    for (group, pid, year), values in units.items():
        if len(values) < 2:
            continue
        mean = statistics.mean(values)
        edges = diagnostics['groups'][group]['edges']
        bucket = sum(mean > edge for edge in edges)
        pools[(group, bucket)].update(values)
    groups = {}
    for group, diagnostic in diagnostics['groups'].items():
        bins = {}
        for (g, bucket), counts in pools.items():
            if g != group:
                continue
            values = sorted(counts)
            bins[str(bucket)] = {'values': values, 'counts': [counts[x] for x in values],
                                 'n': sum(counts.values()), 'unique_scores': len(values)}
        groups[group] = {'edges': diagnostic['edges'], 'bins': bins}
    model = {'schema_version': 1, 'created_at': time.time(), 'train_seasons': years,
             'groups': groups, 'scoring_hash': digest(scoring), 'missing_scoring_keys': dict(missing),
             'training_temporal_audit': temporal_audit(selected, cutoff) if cutoff is not None else {'passed': False, 'reason': 'no_cutoff'},
             'evidence_status': 'preregister_for_future_distribution_sensitivity_not_calibrated',
             'limitations': ['Realized season means define historical bins; indexing them with professional expected means is an unvalidated transfer.',
                             'Reweighting preserves observed support and the requested mean, not forecast accuracy or unseen future extreme scores.',
                             'No medical availability mixture or causal interpretation is implied.',
                             'Any Gaussian factor copula is an additional uncalibrated dependence assumption; this module does not fit Pearson correlations.']}
    model['id'] = digest(model)
    return model


def support_for_player(model, player, mean=None):
    target = _num(player.get('mean') if mean is None else mean)
    group = _group(player)
    entry = model['groups'].get(group)
    if target is None or entry is None:
        raise ModelingError('No finite target or measured support cohort')
    bucket = str(sum(target > edge for edge in entry['edges']))
    if bucket not in entry['bins']:
        raise ModelingError('Empty measured score-support bin')
    return {**entry['bins'][bucket], 'group': group, 'bin': int(bucket),
            'support_model_id': model['id']}


def tilt_distribution(support, target, *, tolerance=1e-10, max_iterations=160):
    """Positive exponential weights with a specified first moment.

    Supports either observed values or compressed {values, counts}. Bisection
    solves the single mean constraint after log-sum-exp stabilization. No score
    clipping, support expansion, negative weights or concealed target rounding.
    """
    target = _num(target)
    if target is None or tolerance <= 0 or max_iterations < 1:
        raise ModelingError('Finite target, positive tolerance and iterations required')
    if isinstance(support, dict):
        values = [_num(v) for v in support.get('values', [])]
        counts = [_num(v) for v in support.get('counts', [1]*len(values))]
    else:
        counter = Counter(support)
        values, counts = [_num(v) for v in counter], list(counter.values())
    if not values or len(values) != len(counts) or None in values or any(c is None or c <= 0 for c in counts):
        raise ModelingError('Nonempty finite support with positive counts required')
    pairs = sorted(zip(values, counts))
    values, counts = map(list, zip(*pairs))
    lo_value, hi_value = values[0], values[-1]
    if target < lo_value or target > hi_value:
        raise ModelingError('Professional target outside the observed support convex hull')
    if hi_value > lo_value and target in (lo_value, hi_value):
        raise ModelingError('A support-boundary target requires a degenerate limit, not finite positive exponential weights')
    n = sum(counts)
    span = hi_value-lo_value
    xs = [(v-target)/span for v in values] if span else [0.0]*len(values)
    logs = [math.log(c) for c in counts]
    def weights(lam):
        terms = [base+lam*x for base,x in zip(logs,xs)]
        peak = max(terms)
        mass = [math.exp(t-peak) for t in terms]
        total = math.fsum(mass)
        return [x/total for x in mass]
    def error(lam):
        probs = weights(lam)
        return math.fsum(p*(v-target) for p,v in zip(probs,values)), probs
    lam, iterations = 0.0, 0
    err, probs = error(lam)
    if abs(err) > tolerance:
        lower, upper = -1.0, 1.0
        for _ in range(64):
            if error(lower)[0] <= 0 and error(upper)[0] >= 0:
                break
            lower *= 2; upper *= 2
        else:
            raise ModelingError('Unable to bracket finite positive moment weights')
        for iterations in range(1, max_iterations+1):
            lam = (lower+upper)/2
            err, probs = error(lam)
            if abs(err) <= tolerance:
                break
            if err < 0:
                lower = lam
            else:
                upper = lam
        if abs(err) > tolerance:
            raise ModelingError('Moment reweighting did not converge to the requested tolerance')
    if any(p <= 0 or not math.isfinite(p) for p in probs):
        raise ModelingError('Extreme overlap requires numerically zero weights; refuse a false positivity claim')
    mean = math.fsum(p*v for p,v in zip(probs,values))
    variance = math.fsum(p*(v-mean)**2 for p,v in zip(probs,values))
    third = math.fsum(p*(v-mean)**3 for p,v in zip(probs,values))
    cumulative, total = [], 0.0
    for p in probs:
        total += p
        cumulative.append(min(1.0,total))
    cumulative[-1] = 1.0
    ess = 1/math.fsum(p*p/c for p,c in zip(probs,counts))
    return {'values': values, 'probabilities': probs, 'cdf': cumulative, 'target': target,
            'mean': mean, 'mean_error': mean-target, 'variance': variance, 'sd': math.sqrt(variance),
            'skewness': third/variance**1.5 if variance else 0.0,
            'ess': ess, 'ess_fraction': ess/n, 'observation_count': n,
            'unique_score_count': len(values), 'lambda_scaled': lam, 'iterations': iterations,
            'support_min': lo_value, 'support_max': hi_value,
            'max_individual_weight': max(p/c for p,c in zip(probs,counts)),
            'method': 'one-dimensional exponential moment reweighting on observed scores',
            'evidence_status': 'uncalibrated_support_preserving_distribution_sensitivity'}
