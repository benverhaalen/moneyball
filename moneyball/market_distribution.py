"""Inspect two-sided market thresholds without inventing a mean or a CI.

Inputs follow the public Odds API event-odds example actually inspected on
2026-09-07. Prices are not independent samples. A fitted curve is a conditional
market-implied CDF under the selected margin and settlement assumptions.
"""
from datetime import datetime
from collections import defaultdict
import math
import random
import statistics


class MarketDataError(ValueError):
    pass


def _finite(value):
    if isinstance(value, bool):
        raise MarketDataError('Boolean is not a numeric observation')
    try:
        result = float(value)
    except (ValueError, TypeError) as error:
        raise MarketDataError('Missing or nonnumeric observation') from error
    if not math.isfinite(result):
        raise MarketDataError('Nonfinite observation')
    return result


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as error:
        raise MarketDataError('Explicit timezone-aware timestamp required') from error
    if result.tzinfo is None:
        raise MarketDataError('Explicit timezone-aware timestamp required')
    return result.timestamp()


def implied_probability(price, odds_format='decimal'):
    price = _finite(price)
    if odds_format == 'decimal':
        if price <= 1:
            raise MarketDataError('Decimal price must exceed one')
        return 1 / price
    if odds_format == 'american':
        if abs(price) < 100:
            raise MarketDataError('American price magnitude must be at least 100')
        return 100 / (price + 100) if price > 0 else -price / (100 - price)
    raise MarketDataError('Unknown odds format')


def devig(over_price, under_price, odds_format='decimal', method='multiplicative'):
    """Return a sensitivity assumption, not identified true probabilities.

    Multiplicative, additive, and power methods are described by Clarke,
    Kovalchik & Ingram (2017), doi:10.11648/j.ajss.20170506.12.
    """
    q = [implied_probability(x, odds_format) for x in (over_price, under_price)]
    total = sum(q)
    if method == 'multiplicative':
        p = [x / total for x in q]
    elif method == 'additive':
        p = [x - (total - 1) / 2 for x in q]
        if min(p) < 0 or max(p) > 1:
            raise MarketDataError('Additive margin allocation gives invalid probability')
    elif method == 'power':
        lo, hi = 0.0, 1.0
        while sum(x ** hi for x in q) > 1:
            hi *= 2
        for _ in range(80):
            mid = (lo + hi) / 2
            if sum(x ** mid for x in q) > 1:
                lo = mid
            else:
                hi = mid
        p = [x ** ((lo + hi) / 2) for x in q]
    else:
        raise MarketDataError('Unknown margin allocation method')
    return {'over': p[0], 'under': p[1], 'overround': total - 1,
            'method': method, 'interpretation': 'conditional on action and no push'}


def normalize_event(event, *, observed_at, market_keys, odds_format='decimal',
                    integer_valued=True, cutoff=None):
    """Normalize inspected event JSON; names are NOT resolved to player IDs.

    No integer thresholds (pushes), one-sided milestones, late quotes, missing
    timestamps, or unpaired outcomes are silently accepted. Caller must verify
    the bookmaker's action/void conditions before interpreting any output.
    """
    observed = _time(observed_at)
    cutoff_time = observed if cutoff is None else _time(cutoff)
    if observed > cutoff_time:
        raise MarketDataError('Response was observed after decision cutoff')
    start = _time(event.get('commence_time'))
    if observed >= start or not event.get('id'):
        raise MarketDataError('Pregame event ID and observation required')
    if not isinstance(integer_valued, bool):
        raise MarketDataError('Explicit integer-valued support declaration required')
    rows, seen = [], set()
    for book in event.get('bookmakers', []):
        if not book.get('key'):
            raise MarketDataError('Book identity required')
        for market in book.get('markets', []):
            if market.get('key') not in market_keys:
                continue
            quote = market.get('last_update', book.get('last_update'))
            quoted = _time(quote)
            if quoted > min(observed, cutoff_time) or quoted >= start:
                raise MarketDataError('Quote is after observation/cutoff/kickoff')
            pairs = {}
            for item in market.get('outcomes', []):
                name, player = item.get('name'), item.get('description')
                if name not in ('Over', 'Under') or not player:
                    raise MarketDataError('Two-sided named player market required')
                line = _finite(item.get('point'))
                if integer_valued and line.is_integer():
                    raise MarketDataError('Integer line requires a separate push model')
                key = (player, line)
                pair = pairs.setdefault(key, {})
                if name in pair:
                    raise MarketDataError('Duplicate outcome')
                pair[name] = _finite(item.get('price'))
            for (player, line), pair in pairs.items():
                if set(pair) != {'Over', 'Under'}:
                    raise MarketDataError('Both sides at the same threshold required')
                identity = (event['id'], book['key'], market['key'], player, line)
                if identity in seen:
                    raise MarketDataError('Duplicate market quote')
                seen.add(identity)
                # Also validates prices before publishing a normalized record.
                devig(pair['Over'], pair['Under'], odds_format)
                rows.append({'event_id': event['id'], 'book': book['key'],
                             'market': market['key'], 'player_name': player,
                             'threshold': line, 'over_price': pair['Over'],
                             'under_price': pair['Under'], 'odds_format': odds_format,
                             'quoted_at': quote, 'observed_at': observed_at,
                             'commence_time': event['commence_time'],
                             'integer_valued': integer_valued,
                             'settlement': 'conditional_on_action_no_push'})
    if not rows:
        raise MarketDataError('No requested paired markets found')
    return rows


def isotonic(values):
    """Equal-weight least-squares PAVA; weights do not imply sample precision."""
    blocks = []
    for value in values:
        blocks.append([_finite(value), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b, a = blocks.pop(), blocks.pop()
            n = a[1] + b[1]
            blocks.append([(a[0] * a[1] + b[0] * b[1]) / n, n])
    return [mean for mean, n in blocks for _ in range(n)]


def fit_curve(rows, *, support, method='multiplicative', repair=False,
              max_adjustment=0.05, max_quote_span_seconds=300):
    """Fit one book/player/event/market only; return finite-support bounds.

    All uncertainty bounds concern incomplete identification under the supplied
    support and adjusted thresholds. They are NOT confidence intervals.
    """
    if not rows:
        raise MarketDataError('At least one threshold required')
    low, high = map(_finite, support)
    if low >= high:
        raise MarketDataError('Finite support must have increasing endpoints')
    identities = {(r['event_id'], r['book'], r['market'], r['player_name'],
                   r['integer_valued'], r['settlement']) for r in rows}
    if len(identities) != 1:
        raise MarketDataError('Do not pool books, people, events or settlement rules')
    rows = sorted(rows, key=lambda r: r['threshold'])
    xs = [_finite(r['threshold']) for r in rows]
    if len(set(xs)) != len(xs) or any(not low < x < high for x in xs):
        raise MarketDataError('Unique thresholds strictly inside finite support required')
    quotes = [_time(r['quoted_at']) for r in rows]
    if max(quotes) - min(quotes) > max_quote_span_seconds:
        raise MarketDataError('Threshold quotes are not synchronous enough')
    adjusted = [devig(r['over_price'], r['under_price'], r['odds_format'], method)
                for r in rows]
    raw = [r['under'] for r in adjusted]
    fitted = isotonic(raw)
    change = max(abs(a - b) for a, b in zip(raw, fitted))
    if change > 1e-12 and not repair:
        raise MarketDataError('Inconsistent CDF; explicit isotonic repair required')
    if change > max_adjustment:
        raise MarketDataError('Isotonic repair exceeds stated tolerance')
    # Extremal CDF steps bound the integral of survival. For integer support,
    # mass can be allocated sharply to integer interval endpoints instead.
    integer = rows[0]['integer_valued']
    if integer and (not low.is_integer() or not high.is_integer()):
        raise MarketDataError('Integer-valued support requires integer endpoints')
    boundaries = [low] + xs + [high]
    probabilities = [0.0] + fitted + [1.0]
    mean_low = mean_high = 0.0
    for i in range(len(probabilities) - 1):
        mass = probabilities[i + 1] - probabilities[i]
        a = boundaries[i] if i == 0 or not integer else math.floor(boundaries[i]) + 1
        b = boundaries[i + 1] if not integer else math.floor(boundaries[i + 1])
        mean_low += mass * a
        mean_high += mass * b
    return {'identity': dict(zip(('event_id', 'book', 'market', 'player_name'),
                                  next(iter(identities))[:4])),
            'support_assumption': [low, high], 'integer_valued': integer,
            'points': [{'threshold': x, 'cdf': p, 'raw_cdf': q}
                       for x, p, q in zip(xs, fitted, raw)],
            'mean_identification_bounds': [mean_low, mean_high],
            'margin_method': method, 'max_isotonic_adjustment': change,
            'n_thresholds': len(xs), 'n_books': 1, 'independent_sample_size': None,
            'conditional_on': 'book action/void rules, no push, devig, finite support',
            'confidence_interval': None, 'empirically_calibrated': False}


def quantile_bounds(curve, probability):
    """Outer bounds for the inverse-CDF quantile; no interpolated fake tails."""
    q = _finite(probability)
    if not 0 < q < 1:
        raise MarketDataError('Quantile probability must be strictly between zero and one')
    low, high = curve['support_assumption']
    for point in curve['points']:
        x, p = point['threshold'], point['cdf']
        if p < q:
            low = math.floor(x) + 1 if curve['integer_valued'] else x
        elif p >= q:
            high = math.floor(x) if curve['integer_valued'] else x
            break
    return [low, high]


def _empirical_quantile(values, probability):
    values = sorted(values)
    if not values:
        raise MarketDataError('Empty empirical distribution')
    return values[max(0, math.ceil(len(values) * probability) - 1)]


def _wilson(successes, n):
    """95% IID reference per NIST e-Handbook prc241; not a dependence fix."""
    p, z = successes / n, 1.959963984540054
    denominator = 1 + z*z/n
    center = (p + z*z/(2*n))/denominator
    radius = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/denominator
    return [center-radius, center+radius]


def game_total_coverage(rows, *, bootstrap_draws=2000, seed=20260907):
    """Fixed preregistered retrospective market-content sanity check.

    Calibration 2018-23; two untouched-by-this-function evaluations 2024/25;
    complete REG games only. No historical availability is manufactured.
    Future rows cannot alter the calibration intervals. No parameter search.
    """
    groups, seen = defaultdict(list), set()
    for row in rows:
        season = row.get('season')
        if season not in range(2018, 2026) or row.get('game_type') != 'REG':
            continue
        if any(row.get(k) is None for k in ('home_score', 'away_score', 'total_line')):
            continue
        if not row.get('game_id') or row['game_id'] in seen:
            raise MarketDataError('Unique game identity required')
        seen.add(row['game_id'])
        total = _finite(row['home_score']) + _finite(row['away_score'])
        if row.get('total') is not None and _finite(row['total']) != total:
            raise MarketDataError('Total does not reconcile to both final scores')
        groups[season].append((row.get('week'), total-_finite(row['total_line'])))
    calibration = [x for year in range(2018, 2024) for _, x in groups[year]]
    if not calibration:
        raise MarketDataError('Missing calibration games')
    rng = random.Random(seed)
    result = {'calibration_years': list(range(2018, 2024)), 'calibration_n': len(calibration),
              'calibration_weeks': sum(len({w for w, _ in groups[y]}) for y in range(2018, 2024)),
              'comparison_count': 4, 'specifications_tried': 1,
              'bootstrap_draws': bootstrap_draws, 'seed': seed,
              'point_in_time_verified': False,
              'scope': 'retrospective game totals; not player/fantasy or title intervals',
              'calibration_residual_mean': statistics.fmean(calibration), 'evaluations': []}
    for nominal, alpha in ((.8, .1), (.9, .05)):
        lo = _empirical_quantile(calibration, alpha)
        hi = _empirical_quantile(calibration, 1-alpha)
        for year in (2024, 2025):
            test = groups[year]
            if not test:
                raise MarketDataError('Missing preregistered evaluation season')
            clusters = defaultdict(list)
            for week, residual in test:
                if week is None:
                    raise MarketDataError('Week needed for cluster diagnostic')
                clusters[week].append(int(lo <= residual <= hi))
            blocks = list(clusters.values())
            successes, n = sum(map(sum, blocks)), sum(map(len, blocks))
            boot = []
            for _ in range(bootstrap_draws):
                selected = [rng.choice(blocks) for _ in blocks]
                boot.append(sum(map(sum, selected))/sum(map(len, selected)))
            interval = [_empirical_quantile(boot, .025), _empirical_quantile(boot, .975)]
            result['evaluations'].append({'season': year, 'nominal_coverage': nominal,
                'n_games': n, 'n_week_clusters': len(blocks), 'covered_games': successes,
                'observed_coverage': successes/n, 'residual_interval': [lo, hi],
                'interval_width': hi-lo, 'interval_half_width': (hi-lo)/2,
                'coverage_wilson95_iid_reference': _wilson(successes, n),
                'coverage_week_bootstrap95': interval,
                'nominal_outside_week_bootstrap': not interval[0] <= nominal <= interval[1],
                'limitations': ['Only18 week clusters; repeated teams across weeks remain dependent',
                    'Conditional on fitted historical residual quantiles; no calibration-set resampling',
                    'Current bulk market vintages are unverified; no decision-time validity claim',
                    'Four coverage comparisons; no adjusted discovery claim']})
    return result
