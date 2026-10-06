"""Optional NumPy implementation of the existing conditional league simulator.

NumPy is imported only when this module is selected. Random streams differ from
the stdlib reference; the distributions, shared candidate shocks, legal lineup
decisions, calendar treatment, and keyed tie breaks are preserved. No approximate
Gaussian CDF or different fantasy model is substituted for speed.
"""

from collections import Counter
import math
import random

import numpy as np

from .identity import canonical_team
from .simulation import (EXPERIMENTAL_STARTERS, _tilted, _weekly_mean, keyed_seed,
                         round_robin, select_lineup, summarize)


_ERF = np.frompyfunc(math.erf, 1, 1)


def simulate(states, projection, *, draws=1000, seed=1, years=1, schedule=None,
             nfl_schedule=None, slots=EXPERIMENTAL_STARTERS, allow_assumptions=False,
             persistent_shock=False, missing_forecast='reject'):
    """Same public arguments/results as simulation.simulate, optional NumPy.

    Replications are vectorized, but each is an independent simulated trajectory.
    The caller must still count a shared outer draft completion as one block when
    estimating uncertainty over draft policy. NumPy draws are shared across every
    candidate state; candidates never observe sampled outcomes during selection.
    """
    if not allow_assumptions:
        raise ValueError('Unverified bracket/schedule/future-policy semantics require explicit allow_assumptions=True')
    if missing_forecast not in ('reject', 'zero', 'carry'):
        raise ValueError('Unknown missing forecast policy')
    if draws < 2 or years < 1:
        raise ValueError('Need at least two draws and one year')
    if not states:
        raise ValueError('At least one league state required')
    rows = projection.get('players', projection) if isinstance(projection, dict) else projection
    players = {str(p.get('id', p.get('player_id'))): {**p, 'team': canonical_team(p.get('team'))} for p in rows}
    for p in players.values():
        if p['team'] is None:
            p['team_loading'] = 0.0
    support_model = projection.get('marginal_support') if isinstance(projection, dict) else None
    if support_model and projection.get('scoring_hash') is not None and support_model.get('scoring_hash') != projection['scoring_hash']:
        raise ValueError('Marginal support scoring differs from projection')
    names = list(states)
    teams = list(states[names[0]])
    if len(teams) != 12 or any(set(s) != set(teams) for s in states.values()):
        raise ValueError('Every scenario must contain the same twelve teams')
    for name, state in states.items():
        held = [str(p) for roster in state.values() for p in roster]
        if len(set(held)) != len(held):
            raise ValueError('Duplicate ownership in scenario '+name)
        missing = sorted(set(held)-set(players))
        if missing:
            raise ValueError('Missing player projections: '+repr(missing[:10]))
    origin = 'supplied' if schedule else 'assumed full rotation plus first three repeat rounds'
    schedule = {int(k): v for k, v in (schedule or round_robin(teams)).items()}
    if set(schedule) != set(range(1, 15)):
        raise ValueError('Regular season schedule must cover weeks 1–14')
    for pairs in schedule.values():
        scheduled = [p for pair in pairs for p in pair]
        if len(scheduled) != 12 or set(scheduled) != set(teams):
            raise ValueError('Each team must play exactly once per qualifying week')
    games = {}
    for game in nfl_schedule or []:
        if str(game.get('game_type', game.get('season_type', 'REG'))) != 'REG':
            continue
        for t in (canonical_team(game['home_team']), canonical_team(game['away_team'])):
            games[int(game['week']), t] = str(game['game_id'])
    hosts = sorted({p.get('team') for p in players.values()}-{None, ''})
    known_teams = {t for w, t in games}
    byes = {(w, t) for w in range(1, 18) for t in set(hosts) & known_teams if (w, t) not in games} if games else set()
    union = sorted({str(p) for s in states.values() for r in s.values() for p in r})
    count = len(union)
    player_index = {p: i for i, p in enumerate(union)}
    team_index = {t: i for i, t in enumerate(teams)}
    host_index = {t: i for i, t in enumerate(hosts)}
    # Extra zero host and outcome columns represent no host and empty slots.
    host_cols = np.array([host_index.get(players[p]['team'], len(hosts)) for p in union], dtype=int)
    tl = np.array([float(players[p].get('team_loading') or 0) for p in union])
    gl = np.array([float(players[p].get('game_loading') or 0) for p in union])
    factor_var = tl*tl+gl*gl
    if np.any(factor_var > 1+1e-10):
        raise ValueError('Factor loadings exceed unit residual variance')
    individual = np.sqrt(np.maximum(0, 1-factor_var))
    sd = np.array([float(players[p].get('sd') or 0) for p in union])
    residuals = [np.asarray(players[p].get('standardized_residuals') or [], dtype=float) for p in union]
    support = {}
    if support_model:
        from .marginals import support_for_player
        for pid in union:
            s = support_for_player(support_model, players[pid])
            support[pid] = (tuple(s['values']), tuple(s['counts']))

    # Exact assignment computed using only pre-outcome information.
    means, assignments, missing_masks, marginals, game_cols = {}, {}, {}, {}, {}
    for week in range(1, 18):
        gids = sorted({games.get((week, players[p]['team']), players[p]['team'] or p) for p in union})
        idx = {g: i for i, g in enumerate(gids)}
        game_cols[week] = (len(gids), np.array([idx[games.get((week, players[p]['team']), players[p]['team'] or p)] for p in union], dtype=int))
    for year in range(years):
        for week in range(1, 18):
            vals = {pid: 0.0 if (week, players[pid]['team']) in byes else
                    _weekly_mean(players[pid], week, year, missing_forecast) for pid in union}
            means[year, week] = np.array([vals[p] for p in union])
            absent = np.array([(week, players[p]['team']) in byes or
                (year == 0 and missing_forecast == 'zero' and bool(players[p].get('weekly_means'))
                 and str(week) not in players[p]['weekly_means'] and week not in players[p]['weekly_means']) for p in union], dtype=bool)
            missing_masks[year, week] = absent
            if support_model:
                marginals[year, week] = []
                for pid in union:
                    m = _tilted(*support[pid], vals[pid])
                    marginals[year, week].append((np.asarray(m['values']), np.asarray(m['cdf'])))
            elif any(sd[i] and not len(residuals[i]) and not absent[i] for i in range(count)):
                raise ValueError('Positive forecast SD requires measured residuals; no silent normal default')
            decision = {p: v*(float(players[p].get('annual_expected_multiplier', 1))**year if persistent_shock else 1) for p, v in vals.items()}
            assignment = np.full((len(names), 12, len(slots)), count, dtype=int)
            for s, name in enumerate(names):
                for t, team in enumerate(teams):
                    eligible = [str(p) for p in states[name][team] if (week, players[str(p)]['team']) not in byes]
                    lineup = select_lineup(eligible, players, decision, slots)
                    assignment[s, t, :len(lineup)] = [player_index[p] for p in lineup]
            assignments[year, week] = assignment

    rng = np.random.default_rng(keyed_seed(seed, 'numpy-conditional-trajectories-v1'))
    factors = np.ones((draws, count))
    titles = np.zeros((draws, len(names), 12), dtype=int)
    first = np.zeros_like(titles)
    by_year = np.zeros((years, draws, len(names), 12), dtype=np.int8)
    qualify = np.zeros((len(names), 12), dtype=int)
    row_index = np.arange(draws)[:, None]
    scenario_index = np.arange(len(names))[None, :]
    for year in range(years):
        if year and persistent_shock:
            for i, pid in enumerate(union):
                transitions = players[pid].get('year_transition_samples') or []
                if not transitions:
                    if float(players[pid].get('mean') or 0) == 0:
                        factors[:, i] = 0
                        continue
                    raise ValueError('Persistent horizon shocks require measured annual production transitions for positive-mean holdings')
                choices = np.asarray(transitions)[rng.integers(len(transitions), size=draws)]
                factors[:, i] *= choices
        scores = np.empty((draws, len(names), 17, 12))
        for week in range(1, 18):
            tz = np.zeros((draws, len(hosts)+1))
            tz[:, :len(hosts)] = rng.standard_normal((draws, len(hosts)))
            ng, gc = game_cols[week]
            gz = rng.standard_normal((draws, ng))
            common = tl*tz[:, host_cols] + gl*gz[:, gc]
            if support_model:
                z = individual*rng.standard_normal((draws, count))+common
                # Exact libm erf through a NumPy object ufunc; no tail approximation.
                u = (1+np.asarray(_ERF(z/math.sqrt(2)), dtype=float))/2
                outcomes = np.empty((draws, count+1))
                for i, (vals, cdf) in enumerate(marginals[year, week]):
                    outcomes[:, i] = vals[np.minimum(np.searchsorted(cdf, u[:, i], side='left'), len(vals)-1)]
            else:
                shock = np.zeros((draws, count))
                for i, resid in enumerate(residuals):
                    if len(resid):
                        shock[:, i] = resid[rng.integers(len(resid), size=draws)]
                outcomes = np.empty((draws, count+1))
                outcomes[:, :count] = means[year, week]+sd*(individual*shock+common)
            outcomes[:, :count] *= factors
            outcomes[:, count] = 0.0
            outcomes[:, np.flatnonzero(missing_masks[year, week])] = 0.0
            # The reference sums roster scores from left to right. Preserve that
            # addition order: NumPy's pairwise reduction could otherwise change
            # an exact floating-point score tie and hence a playoff winner.
            scores[:, :, week-1, :] = np.add.accumulate(
                outcomes[:, assignments[year, week]], axis=-1)[..., -1]

        wins = np.zeros((draws, len(names), 12))
        pf = np.zeros_like(wins); pa = np.zeros_like(wins)
        # Match the reference's keyed per-replication tie breaks exactly.
        tie = np.array([[random.Random(keyed_seed(seed, draw, year, 'seed', t)).random() for t in teams] for draw in range(draws)])[:, None, :]
        for week in range(1, 15):
            for a, b in schedule[week]:
                ai, bi = team_index[a], team_index[b]
                av, bv = scores[:, :, week-1, ai], scores[:, :, week-1, bi]
                wins[:, :, ai] += (av > bv)+.5*(av == bv)
                wins[:, :, bi] += (bv > av)+.5*(bv == av)
                pf[:, :, ai] += av; pf[:, :, bi] += bv
                pa[:, :, ai] += bv; pa[:, :, bi] += av
        seeds = np.lexsort((np.broadcast_to(tie, wins.shape), pa, pf, wins), axis=-1)[:, :, -8:][:, :, ::-1]
        if year == 0:
            for team in range(12):
                qualify[:, team] = (seeds == team).sum(axis=(0, 2))
        bracket = seeds[:, :, [0, 7, 3, 4, 1, 6, 2, 5]]
        for week in (15, 16, 17):
            candidates_scores = np.take_along_axis(scores[:, :, week-1, :], bracket, axis=2)
            candidates_ties = np.take_along_axis(np.broadcast_to(tie, wins.shape), bracket, axis=2)
            left_scores, right_scores = candidates_scores[:, :, ::2], candidates_scores[:, :, 1::2]
            take_right = (right_scores > left_scores) | ((right_scores == left_scores) & (candidates_ties[:, :, 1::2] > candidates_ties[:, :, ::2]))
            bracket = np.where(take_right, bracket[:, :, 1::2], bracket[:, :, ::2])
        champion = bracket[:, :, 0]
        titles[row_index, scenario_index, champion] += 1
        by_year[year, row_index, scenario_index, champion] = 1
        if year == 0:
            first[row_index, scenario_index, champion] = 1
    result = {}
    for s, name in enumerate(names):
        result[name] = {}
        for t, team in enumerate(teams):
            counts = titles[:, s, t]
            result[name][str(team)] = {
                'current_championship': summarize(first[:, s, t].tolist()),
                'championship_by_year': [summarize(by_year[y, :, s, t].tolist()) for y in range(years)],
                'paired_delta_championship_by_year': [summarize(
                    (by_year[y, :, s, t]-by_year[y, :, 0, t]).tolist()) for y in range(years)],
                'expected_titles': summarize(counts.tolist()),
                'at_least_one_title': summarize((counts > 0).astype(int).tolist()),
                'paired_delta_expected_titles': summarize((counts-titles[:, 0, t]).tolist()),
                'qualification_probability': float(qualify[s, t]/draws),
            }
    return {'scenarios': result, 'baseline': names[0], 'years': years, 'draws': draws, 'seed': seed,
        'engine': 'numpy_vectorized_v1', 'numpy_version': np.__version__,
        'confidence': 'conditional sensitivity experiment; empirical calibration not established',
        'assumptions': {
            'schedule': origin,
            'bracket': 'assumed conventional fixed 1–8/4–5/2–7/3–6, one week each',
            'seeding': 'no divisions: record, points for, points against, random final tie',
            'future_policy': 'frozen holdings; no rookie intake, trades, or future operator adaptation',
            'future_means': 'empirical annual production multipliers scale entire score process' if persistent_shock else 'current professional annual baseline persists; explicit scenario',
            'future_transition_cohorts': 'current output/age cohort held fixed; zero output is absorbing in this multiplicative scenario' if persistent_shock else 'none',
            'future_byes': 'current bye calendar repeated as a scenario; actual future schedules unknown',
            'availability': 'no fitted injury hazard; residuals only, no post-outcome substitutions',
            'missing_week_forecast': missing_forecast+'; zero/carry are sensitivity assumptions, not observed zero or licensed future forecasts',
            'unknown_host_team': 'no bye inferred; unknown identity cannot prove absence',
            'correlation': 'supplied fitted loadings; zero if unsupported; same samples for paired comparisons',
            'distribution': 'entropy weights on historical score support; Gaussian factor copula correlations not Pearson-matched' if support_model else 'centered historical residual bootstrap; shifted-score tail limitations apply',
            'lineups': 'pre-outcome expectation; future lineups use expected cohort decline, never the realized annual transition draw',
            'terminal_objective': 'finite expected championship count plus probability of at least one title; no invented discount rate',
            'implementation': 'NumPy PRNG changes exact random streams, not target distributions; Gaussian CDF uses exact math.erf',
        }}
