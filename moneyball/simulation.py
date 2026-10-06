"""Conditional championship experiments, with common random numbers.

This is a policy evaluator, not a claim that the input forecasts are calibrated.
Lineups use information known before outcomes. Championship uncertainty and
Monte Carlo error are deliberately reported separately.
"""
from collections import Counter, defaultdict
from bisect import bisect_left
from functools import lru_cache
import hashlib
import math
import random
import statistics
from .identity import canonical_team


# Explicit legacy simulation fixture; production callers supply exact league slots.
EXPERIMENTAL_STARTERS = ('QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'FLEX', 'FLEX', 'SUPER_FLEX')
RATRACE_STARTERS = EXPERIMENTAL_STARTERS  # Historical artifact compatibility.
ELIGIBILITY = {'QB': {'QB'}, 'RB': {'RB'}, 'WR': {'WR'}, 'TE': {'TE'},
               'FLEX': {'RB', 'WR', 'TE'}, 'SUPER_FLEX': {'QB', 'RB', 'WR', 'TE'}}


def keyed_seed(seed, *parts):
    return int.from_bytes(hashlib.sha256(repr((seed, parts)).encode()).digest()[:8], 'big')


def positions(player):
    return set(player.get('fantasy_positions') or [player.get('position')]) & {'QB', 'RB', 'WR', 'TE'}


def select_lineup(roster, players, values, slots=EXPERIMENTAL_STARTERS):
    """Maximize pre-outcome expectation subject to unique, legal assignments.

    Returns empty slots when a team cannot field a complete lineup. Single-
    position single-position rosters have a fast exact solution; dual eligibility uses
    a bit-mask assignment dynamic program. Neither path reads realized points.
    """
    ids = sorted({str(p) for p in roster if str(p) in players})
    if tuple(slots) == EXPERIMENTAL_STARTERS and all(len(positions(players[p])) == 1 for p in ids):
        groups = defaultdict(list)
        for pid in ids:
            groups[next(iter(positions(players[pid])))].append(pid)
        for group in groups.values():
            group.sort(key=lambda p: (-values.get(p, 0), p))
        chosen = []
        for pos, count in (('QB', 1), ('RB', 2), ('WR', 3), ('TE', 1)):
            chosen.extend(groups[pos][:count])
            groups[pos] = groups[pos][count:]
        flex = sorted(groups['RB'] + groups['WR'] + groups['TE'], key=lambda p: (-values.get(p, 0), p))
        chosen.extend(flex[:2])
        final = flex[2:] + groups['QB']
        if final:
            chosen.append(max(final, key=lambda p: (values.get(p, 0), p)))
        # Scores can be negative. A configured mandatory slot is still filled
        # when eligible players exist; do not invent a free bench-to-zero option.
        return chosen
    dp = {0: (0.0, ())}
    for pid in ids:
        legal = positions(players[pid])
        updated = dict(dp)
        for mask, (value, chosen) in dp.items():
            for i, slot in enumerate(slots):
                if not mask & (1 << i) and legal & ELIGIBILITY[slot]:
                    newmask = mask | (1 << i)
                    candidate = (value + values.get(pid, 0), chosen + (pid,))
                    if newmask not in updated or candidate[0] > updated[newmask][0]:
                        updated[newmask] = candidate
        dp = updated
    best = max(dp, key=lambda mask: (mask.bit_count(), dp[mask][0]))
    return list(dp[best][1])


def round_robin(team_ids, weeks=14):
    """Explicit fallback schedule, not the league's observed schedule."""
    teams = sorted(team_ids, key=str)
    if len(teams) % 2:
        raise ValueError('Even team count required')
    rotation, schedule = list(teams), {}
    for week in range(1, weeks+1):
        schedule[week] = [(rotation[i], rotation[-1-i]) for i in range(len(teams)//2)]
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]
    return schedule


def summarize(samples):
    n = len(samples)
    mean = statistics.fmean(samples) if n else 0.0
    se = statistics.stdev(samples)/math.sqrt(n) if n > 1 else None
    return {'estimate': mean, 'monte_carlo_se': se, 'draws': n,
            'interval_meaning': 'simulation sampling error only; excludes forecast and policy misspecification'}


def _weekly_mean(player, week, year, missing_forecast='reject'):
    if year > 0:
        # Future weekly matchups/roles are unpublished. Use the stated annual
        # baseline scenario; never fabricate missing current-season starts.
        factors = player.get('year_mean_factors') or [1.0]
        return float(player.get('mean',0))*float(factors[min(year,len(factors)-1)])
    values = player.get('weekly_means') or {}
    if values and str(week) not in values and week not in values:
        if missing_forecast == 'reject':
            raise ValueError('Missing professional weekly forecast: '+str(player.get('id'))+' week '+str(week))
        base = 0.0 if missing_forecast == 'zero' else player.get('observed_week_mean',player.get('mean',0.0))
    else:
        base = values.get(str(week), values.get(week, player.get('mean', 0.0)))
    factors = player.get('year_mean_factors') or [1.0]
    factor = factors[min(year, len(factors)-1)]
    return float(base) * float(factor)


@lru_cache(maxsize=12000)
def _tilted(values, counts, mean):
    from .marginals import tilt_distribution
    if mean == 0:
        return {'values':[0.0],'cdf':[1.0],'method':'explicit zero-contribution scenario; not an entropy fit'}
    return tilt_distribution({'values':values,'counts':counts},mean)


def _draw_player(player, mean, rng, team_z, game_z, year_factor, marginal=None):
    """Residual bootstrap around a professional center; no guessed injury rate."""
    sd = float(player.get('sd') or 0)
    team_loading = float(player.get('team_loading') or 0)
    game_loading = float(player.get('game_loading') or 0)
    factor_var = team_loading**2 + game_loading**2
    if factor_var > 1 + 1e-10:
        raise ValueError('Factor loadings exceed unit residual variance')
    if marginal is not None:
        z = math.sqrt(max(0,1-factor_var))*rng.gauss(0,1)+team_loading*team_z+game_loading*game_z
        uniform = (1+math.erf(z/math.sqrt(2)))/2
        idx = min(bisect_left(marginal['cdf'],uniform),len(marginal['values'])-1)
        return marginal['values'][idx]*year_factor
    residuals = player.get('standardized_residuals') or []
    if residuals:
        shock = residuals[rng.randrange(len(residuals))]
    elif sd:
        raise ValueError('Positive forecast SD requires measured residuals; no silent normal default')
    else:
        shock = 0
    noise = sd * (math.sqrt(max(0, 1-factor_var))*shock + team_loading*team_z + game_loading*game_z)
    # No clipping: negative fantasy scores are legal, and clipping would inflate
    # the professional mean. A zero/absence mixture must be fitted separately.
    return (mean + noise)*year_factor


def simulate(states, projection, *, draws=1000, seed=1, years=1, schedule=None,
             nfl_schedule=None, slots=EXPERIMENTAL_STARTERS, allow_assumptions=False,
             persistent_shock=False, missing_forecast='reject'):
    """Evaluate named complete league states using the same random trajectories.

    states = {scenario_name: {roster_id: [player_id, ...]}}. Holdings stay frozen
    across future seasons; changing future policies needs a separate extension.
    No playoff seed manipulation, hindsight lineups, or automatic transactions.
    """
    if not allow_assumptions:
        raise ValueError('Unverified bracket/schedule/future-policy semantics require explicit allow_assumptions=True')
    if missing_forecast not in ('reject','zero','carry'):
        raise ValueError('Unknown missing forecast policy')
    if draws < 2 or years < 1:
        raise ValueError('Need at least two draws and one year')
    rows = projection.get('players', projection) if isinstance(projection, dict) else projection
    players = {str(p.get('id', p.get('player_id'))): {**p,'team':canonical_team(p.get('team'))} for p in rows}
    for p in players.values():
        if p['team'] is None:
            p['team_loading'] = 0.0
    support_model = projection.get('marginal_support') if isinstance(projection,dict) else None
    if support_model and isinstance(projection,dict) and projection.get('scoring_hash') is not None and support_model.get('scoring_hash')!=projection['scoring_hash']:
        raise ValueError('Marginal support scoring differs from projection')
    if support_model:
        from .marginals import support_for_player
        for p in players.values():
            support = support_for_player(support_model,p)
            p['_support_values'],p['_support_counts'] = tuple(support['values']),tuple(support['counts'])
    teams = list(next(iter(states.values())))
    if len(teams) != 12 or any(set(s) != set(teams) for s in states.values()):
        raise ValueError('Every scenario must contain the same twelve teams')
    for name, state in states.items():
        held = [str(p) for roster in state.values() for p in roster]
        if len(set(held)) != len(held):
            raise ValueError('Duplicate ownership in scenario '+name)
        missing = sorted(set(held)-set(players))
        if missing:
            raise ValueError('Missing player projections: '+repr(missing[:10]))
    schedule_origin = 'supplied' if schedule else 'assumed full rotation plus first three repeat rounds'
    schedule = {int(k): v for k,v in (schedule or round_robin(teams)).items()}
    if set(schedule) != set(range(1,15)):
        raise ValueError('Regular season schedule must cover weeks 1–14')
    for pairs in schedule.values():
        scheduled = [p for pair in pairs for p in pair]
        if len(scheduled) != 12 or set(scheduled) != set(teams):
            raise ValueError('Each team must play exactly once per qualifying week')
    games, byes = {}, set()
    for game in nfl_schedule or []:
        if str(game.get('game_type', game.get('season_type','REG'))) != 'REG':
            continue
        week = int(game['week'])
        for t in (canonical_team(game['home_team']),canonical_team(game['away_team'])):
            games[(week,t)] = str(game['game_id'])
    all_host_teams = {p.get('team') for p in players.values()} - {None, ''}
    if games:
        known_teams = {t for w,t in games}
        byes = {(w,t) for w in range(1,18) for t in all_host_teams & known_teams if (w,t) not in games}
    union = sorted({str(p) for s in states.values() for r in s.values() for p in r})
    # Pre-outcome lineups are deterministic at this information date. Later
    # injury reports must be supplied as a new projection, not inferred from
    # that same week's simulated box score.
    values, lineups, marginals = {}, {}, {}
    for year in range(years):
        for week in range(1,18):
            vals = {p: 0.0 if (week,players[p].get('team')) in byes else
                _weekly_mean(players[p],week,year,missing_forecast) for p in union}
            if year == 0:
                for p in union:
                    if (week,players[p].get('team')) in byes:
                        vals[p] = 0.0
            values[year,week] = vals
            if support_model:
                for pid,mean in vals.items():
                    p = players[pid]
                    marginals[year,week,pid] = _tilted(p['_support_values'],p['_support_counts'],mean)
            decision_values = {p:v*(float(players[p].get('annual_expected_multiplier',1))**year if persistent_shock else 1) for p,v in vals.items()}
            for name,state in states.items():
                for team,roster in state.items():
                    eligible = [str(p) for p in roster if (week,players[str(p)].get('team')) not in byes]
                    lineups[name,year,week,team] = select_lineup(eligible, players, decision_values, slots)
    titles = {name: {t:[0]*draws for t in teams} for name in states}
    first = {name: {t:[0]*draws for t in teams} for name in states}
    by_year = {name: {t:[[0]*draws for _ in range(years)] for t in teams} for name in states}
    qualify = {name: {t:0 for t in teams} for name in states}
    for draw in range(draws):
        rng = random.Random(keyed_seed(seed,draw))
        year_factors = {p:1.0 for p in union}
        for year in range(years):
            if year and persistent_shock:
                for p in union:
                    transitions = players[p].get('year_transition_samples') or []
                    if not transitions:
                        if float(players[p].get('mean') or 0)==0:
                            year_factors[p]=0.0
                            continue
                        raise ValueError('Persistent horizon shocks require measured annual production transitions for positive-mean holdings')
                    year_factors[p] *= transitions[rng.randrange(len(transitions))]
            week_scores = {}
            for week in range(1,18):
                tz = {t:rng.gauss(0,1) for t in sorted(all_host_teams)}
                game_ids = sorted({games.get((week,players[p].get('team')), players[p].get('team') or p) for p in union})
                gz = {g:rng.gauss(0,1) for g in game_ids}
                outcomes = {}
                for p in union:
                    player = players[p]
                    host = player.get('team')
                    gid = games.get((week,host),host or p)
                    mean = values[year,week][p]
                    absent_forecast = bool(player.get('weekly_means')) and str(week) not in player['weekly_means'] and week not in player['weekly_means']
                    outcomes[p] = 0.0 if (week,host) in byes or (year==0 and missing_forecast=='zero' and absent_forecast) else _draw_player(player,mean,rng,tz.get(host,0),gz[gid],year_factors[p],marginals.get((year,week,p)))
                for name in states:
                    week_scores[name,week] = {t:sum(outcomes[p] for p in lineups[name,year,week,t]) for t in teams}
            for name in states:
                wins, pf, pa = Counter(), Counter(), Counter()
                # Identical random tie breaks across scenario states.
                tie = {t:random.Random(keyed_seed(seed,draw,year,'seed',t)).random() for t in teams}
                for week in range(1,15):
                    scores = week_scores[name,week]
                    for a,b in schedule[week]:
                        av,bv = scores[a],scores[b]
                        wins[a] += 1 if av > bv else .5 if av == bv else 0
                        wins[b] += 1 if bv > av else .5 if av == bv else 0
                        pf[a] += av; pf[b] += bv; pa[a] += bv; pa[b] += av
                seeds = sorted(teams,key=lambda t:(wins[t],pf[t],pa[t],tie[t]),reverse=True)[:8]
                if year == 0:
                    for t in seeds:
                        qualify[name][t] += 1
                bracket = [seeds[i] for i in (0,7,3,4,1,6,2,5)]
                for week in (15,16,17):
                    scores = week_scores[name,week]
                    bracket = [max(bracket[i:i+2],key=lambda t:(scores[t],tie[t])) for i in range(0,len(bracket),2)]
                champion = bracket[0]
                titles[name][champion][draw] += 1
                by_year[name][champion][year][draw] = 1
                if year == 0:
                    first[name][champion][draw] = 1
    result = {}
    baseline = next(iter(states))
    for name in states:
        result[name] = {}
        for t in teams:
            counts = titles[name][t]
            paired = [a-b for a,b in zip(counts,titles[baseline][t])]
            result[name][str(t)] = {'current_championship':summarize(first[name][t]),
                'championship_by_year':[summarize(samples) for samples in by_year[name][t]],
                'paired_delta_championship_by_year':[summarize([a-b for a,b in zip(samples,base_samples)])
                    for samples,base_samples in zip(by_year[name][t],by_year[baseline][t])],
                'expected_titles':summarize(counts),'at_least_one_title':summarize([int(x>0) for x in counts]),
                'paired_delta_expected_titles':summarize(paired),
                'qualification_probability':qualify[name][t]/draws}
    return {'scenarios':result,'baseline':baseline,'years':years,'draws':draws,'seed':seed,
        'confidence':'conditional sensitivity experiment; empirical calibration not established',
        'assumptions':{'schedule':schedule_origin,'bracket':'assumed conventional fixed 1–8/4–5/2–7/3–6, one week each',
            'seeding':'no divisions: record, points for, points against, random final tie',
            'future_policy':'frozen holdings; no rookie intake, trades, or future operator adaptation',
            'future_means':'empirical annual production multipliers scale entire score process' if persistent_shock else 'current professional annual baseline persists; explicit scenario',
            'future_transition_cohorts':'current output/age cohort held fixed; zero output is absorbing in this multiplicative scenario' if persistent_shock else 'none',
            'future_byes':'current bye calendar repeated as a scenario; actual future schedules unknown',
            'availability':'no fitted injury hazard; residuals only, no post-outcome substitutions',
            'missing_week_forecast':missing_forecast+'; zero/carry are sensitivity assumptions, not observed zero or licensed future forecasts',
            'unknown_host_team':'no bye inferred; unknown identity cannot prove absence',
            'correlation':'supplied fitted loadings; zero if unsupported; same samples for paired comparisons',
            'distribution':'entropy weights on historical score support; Gaussian factor copula correlations not Pearson-matched' if support_model else 'centered historical residual bootstrap; shifted-score tail limitations apply',
            'lineups':'pre-outcome expectation; future lineups use expected cohort decline, never the realized annual transition draw',
            'terminal_objective':'finite expected championship count plus probability of at least one title; no invented discount rate'}}


def apply_move(state, move, *, max_ordinary=25):
    """Pure hypothetical mutation. Legal ownership checks, no platform writes.

    move: {from_team:..., to_team:..., players:[...]}; or {team, add, drop}.
    List of transfers supports any bilateral player package. Draft rights need
    an explicit future policy and are refused rather than assigned fake value.
    """
    result = {str(t):list(map(str,r)) for t,r in state.items()}
    if move.get('picks'):
        raise ValueError('Draft rights require supplied future draft-policy scenarios; no invented pick price')
    transfers = move.get('transfers')
    if transfers is not None:
        outgoing = []
        for transfer in transfers:
            a,b = str(transfer['from_team']),str(transfer['to_team'])
            if a == b or a not in result or b not in result:
                raise ValueError('Invalid transfer counterparties')
            for p in map(str,transfer['players']):
                if p not in result[a] or p in outgoing:
                    raise ValueError('Transfer violates current ownership')
                outgoing.append(p)
        for transfer in transfers:
            for p in map(str,transfer['players']):
                result[str(transfer['from_team'])].remove(p)
                result[str(transfer['to_team'])].append(p)
    else:
        t = str(move['team'])
        owned = {p for r in result.values() for p in r}
        for p in map(str,move.get('drop',[])):
            if p not in result[t]:
                raise ValueError('Cannot drop an unowned player')
            result[t].remove(p)
        for p in map(str,move.get('add',[])):
            if p in owned:
                raise ValueError('Cannot add an owned player')
            result[t].append(p)
    return {'state':result,'capacity_warnings':[{'team':t,'holdings':len(r),'ordinary_limit':max_ordinary,
        'meaning':'IR/taxi eligibility or draft/trade over-limit handling must be supplied before operational use'}
        for t,r in result.items() if len(r)>max_ordinary], 'writes_performed':False}
