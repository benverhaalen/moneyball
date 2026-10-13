"""Optional supplied-data experiments; no acquisition, fitting or league writes.

The opportunity/role arithmetic reuses workbench_math. ffopportunity's documented
separation of observed opportunities from future prediction informs the xFP
contract (https://github.com/ffverse/ffopportunity); its GPL code and CC-BY-SA
models/data are not bundled. nflseedR's separation of supplied outcomes and
competition mechanics informs the weekly boundary
(https://github.com/nflverse/nflseedR); no NFL standings rules are transferred.

Every request has ``binding`` (provider, league_id, season, own_team_id, period,
scoring, as_of) and ``dataset`` (matching binding, source_id, source_version,
first_observed_at, available_at, evidence_refs). Dates are timezone-aware ISO
strings. An old season alone never establishes historical availability.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json

from .store import DataError
from .workbench_math import calculate, distribution, number, score_projection

METHOD_VERSION = 'supplied-experiments-v1'


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DataError(f'{label} requires a nonempty string')
    return value


def _date(value, label):
    try:
        parsed = datetime.fromisoformat(_text(value, label).replace('Z', '+00:00'))
    except ValueError as exc:
        raise DataError(f'{label} requires an ISO timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataError(f'{label} requires a timezone')
    return parsed


def _entity(value):
    value = _text(value, 'entity_id')
    if ':' not in value or not all(value.split(':', 1)):
        raise DataError('entity_id must include its provider namespace')
    return value


def _envelope(request, module):
    if not isinstance(request, dict):
        raise DataError('Experiment request must be an object')
    binding = request.get('binding')
    if not isinstance(binding, dict):
        raise DataError('Explicit experiment binding is required')
    for key in ('provider', 'league_id', 'own_team_id'):
        _text(binding.get(key), key)
    season = binding.get('season')
    if isinstance(season, bool) or not isinstance(season, int) or season < 1900:
        raise DataError('season must be an explicit integer')
    period = binding.get('period')
    if not isinstance(period, dict) or period.get('kind') not in ('week', 'weeks'):
        raise DataError('period requires kind week or weeks')
    weeks = [period.get('week')] if period['kind'] == 'week' else period.get('weeks')
    if not isinstance(weeks, list) or not weeks or any(
            isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 18 for w in weeks) or len(set(weeks)) != len(weeks):
        raise DataError('period requires distinct explicit NFL weeks 1..18')
    cutoff = _date(binding.get('as_of'), 'as_of')
    scoring = binding.get('scoring')
    if not isinstance(scoring, dict) or not isinstance(scoring.get('weights'), dict) or not scoring['weights']:
        raise DataError('Explicit scoring weights are required')
    for component, weight in scoring['weights'].items():
        _text(component, 'scoring component')
        number(weight, 'scoring weight')
    try:
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False,
                                               separators=(',', ':')).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise DataError('Experiment input must be finite JSON data') from exc
    out = {'module': module, 'method_version': METHOD_VERSION, 'promotion': 'implemented',
           'status': 'conditional', 'binding': deepcopy(binding), 'input_sha256': fingerprint,
           'missing_inputs': [], 'assurance': 'Supplied-data arithmetic; calibration and predictive utility unestablished',
           'title_probabilities': None, 'writes_performed': False}
    dataset = request.get('dataset')
    if not isinstance(dataset, dict):
        out.update(status='blocked', missing_inputs=['dated_dataset'])
        return out
    if dataset.get('binding') != binding:
        out.update(status='blocked', missing_inputs=['compatible_dataset_binding'])
        return out
    for key in ('source_id', 'source_version'):
        _text(dataset.get(key), key)
    refs = dataset.get('evidence_refs')
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r for r in refs):
        raise DataError('Dataset requires evidence_refs')
    out['source'] = deepcopy(dataset)
    known = max(_date(dataset.get('first_observed_at'), 'first_observed_at'),
                _date(dataset.get('available_at'), 'available_at'))
    if known > cutoff:
        out.update(status='blocked', missing_inputs=['dataset_known_at_decision_time'])
    if scoring.get('unsupported') or scoring.get('nonlinear'):
        out.update(status='blocked', missing_inputs=out['missing_inputs'] + ['supported_linear_scoring'])
    return out


def _score(stats, scoring, zero=None):
    return score_projection({'stats': stats, 'availability': 1,
                             'zero_components': zero or []}, scoring)


def observed_opportunity(request):
    """Score observed and supplied expected components; optionally vary allocation.

    ``rows`` contain entity_id, observed_stats, expected_stats, zero_components,
    and optional scenario_opportunity (workbench_math's branch schema). Expected
    components must be acquired, versioned estimates, not inferred from outcomes.
    An unavailable branch yields no delta, never an implicit zero.
    """
    out = _envelope(request, 'observed_opportunity')
    if out['status'] == 'blocked':
        return out
    rows = request.get('rows')
    if not isinstance(rows, list) or not rows:
        return {**out, 'status': 'blocked', 'missing_inputs': ['opportunity_rows']}
    results, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise DataError('Opportunity rows must be objects')
        pid = _entity(row.get('entity_id'))
        if pid in seen:
            raise DataError('Duplicate opportunity entity; aggregate a declared period first')
        seen.add(pid)
        result = {'entity_id': pid, 'delta_actual_minus_xfp': None, 'scenario_delta_vs_xfp': None}
        for field, key in [('observed_stats', 'observed'), ('expected_stats', 'xfp')]:
            if not isinstance(row.get(field), dict):
                result[key] = {'status': 'missing_components', 'mean': None, 'missing': [field]}
            else:
                result[key] = _score(row[field], out['binding']['scoring'], row.get('zero_components'))
        if all(result[k]['mean'] is not None for k in ('observed', 'xfp')):
            result['delta_actual_minus_xfp'] = result['observed']['mean'] - result['xfp']['mean']
        if 'scenario_opportunity' in row:
            result['scenario'] = score_projection({'opportunity': row['scenario_opportunity'],
                'availability': 1, 'zero_components': row.get('zero_components', [])}, out['binding']['scoring'])
            if result['scenario']['mean'] is not None and result['xfp']['mean'] is not None:
                result['scenario_delta_vs_xfp'] = result['scenario']['mean'] - result['xfp']['mean']
        results.append(result)
    gaps = [r['entity_id'] + ':' + k for r in results for k in ('observed', 'xfp', 'scenario')
            if k in r and r[k]['mean'] is None]
    return {**out, 'status': 'partial' if gaps else 'conditional', 'missing_inputs': gaps, 'rows': results,
            'use': 'Descriptive opportunity accounting; actual-minus-xFP is not an identified talent effect or future forecast'}


def role_state_futures(request):
    """Propagate supplied homogeneous transitions and score explicit role emissions.

    One entity_id; initial/transitions follow workbench_math.role_transition.
    state_projections covers every state including exit, each with explicit stats
    or opportunity, availability and any known zero_components. steps equals the
    declared number of future weeks. This is a role sensitivity, not a fitted HMM.
    """
    out = _envelope(request, 'role_state_futures')
    if out['status'] == 'blocked':
        return out
    pid = _entity(request.get('entity_id'))
    period = out['binding']['period']
    weeks = [period['week']] if period['kind'] == 'week' else period['weeks']
    if request.get('steps') != len(weeks):
        raise DataError('Role steps must equal declared target weeks')
    if weeks != sorted(weeks) or any(b != a + 1 for a, b in zip(weeks, weeks[1:])):
        raise DataError('Homogeneous weekly transitions require contiguous ordered weeks')
    initial = request.get('initial')
    transitions = request.get('transitions')
    if not initial or not transitions:
        return {**out, 'status': 'blocked', 'missing_inputs': ['supplied_role_transitions']}
    path = calculate({'kind': 'role_transition', 'initial': initial,
                      'transitions': transitions, 'steps': request['steps']})['state_path']
    projections = request.get('state_projections')
    if not isinstance(projections, dict) or set(projections) != set(path[0]):
        return {**out, 'status': 'blocked', 'missing_inputs': ['complete_state_projections'], 'state_path': path}
    if any(not isinstance(row, dict) for row in projections.values()):
        raise DataError('State projections must be objects')
    emissions = {state: score_projection(row, out['binding']['scoring']) for state, row in projections.items()}
    missing = [state for state, row in emissions.items() if row['mean'] is None]
    return {**out, 'status': 'partial' if missing else 'conditional', 'missing_inputs': missing,
            'entity_id': pid, 'state_path': path, 'state_projections': emissions,
            'weekly_points': [{'week': week, 'mean': None if missing else sum(
                state[state_id] * emissions[state_id]['mean'] for state_id in state)}
                for week, state in zip(weeks, path[1:])],
            'use': 'Supplied role persistence and emissions; no fitted transitions, duration, retention or future acquisition policy'}


def weekly_competition(request):
    """Evaluate fixed pre-lock lineups on supplied weighted JOINT scoring worlds.

    ``starters`` uses normalized eligible_positions slots. ``players`` maps
    namespaced entity IDs to positions. ``branches`` maps branch -> team ->
    {roster, lineup, lineup_selected_at}; lineup is in slot order. lineup_lock_at
    bounds the pre-lock decision time; these are supplied timestamps, not a
    platform lock verification. ``matchups`` contains team pairs.
    ``worlds`` contains {id, weight, stats: {entity: scoring_components}}.
    Every world includes every selected player in every branch. Same world IDs
    preserve dependence and paired comparisons. Weights are explicit assumptions,
    not empirical odds; no independent resampling or hindsight optimization.
    """
    out = _envelope(request, 'weekly_competition')
    if out['status'] == 'blocked':
        return out
    if out['binding']['period']['kind'] != 'week':
        raise DataError('Weekly competition requires one exact week')
    branches, players, slots = request.get('branches'), request.get('players'), request.get('starters')
    worlds, matchups = request.get('worlds'), request.get('matchups')
    if any(not x for x in (branches, players, slots, worlds, matchups)):
        return {**out, 'status': 'blocked', 'missing_inputs': ['joint_worlds_fixed_lineups_and_exact_matchups']}
    if not isinstance(branches, dict) or not isinstance(players, dict) or not isinstance(slots, list):
        raise DataError('Invalid branches, players or starter slots')
    if any(not isinstance(s, dict) or not isinstance(s.get('eligible_positions'), list)
           or not s['eligible_positions'] for s in slots):
        raise DataError('Starter slots require eligible_positions')
    if any(not isinstance(holdings, dict) for holdings in branches.values()):
        raise DataError('Branches must map team IDs to holdings')
    cutoff = _date(out['binding']['as_of'], 'as_of')
    lock_at = _date(request.get('lineup_lock_at'), 'lineup_lock_at')
    if cutoff > lock_at:
        return {**out, 'status': 'blocked', 'missing_inputs': ['pre_lock_decision_time']}
    teams = set(next(iter(branches.values())))
    if out['binding']['own_team_id'] not in teams:
        raise DataError('Own team is absent from competition')
    required = set()
    for branch, holdings in branches.items():
        _text(branch, 'branch')
        if not isinstance(holdings, dict) or set(holdings) != teams:
            raise DataError('Branches must cover identical teams')
        owned = set()
        for tid, team in holdings.items():
            _text(tid, 'team_id')
            if not isinstance(team, dict) or not isinstance(team.get('roster'), list) or not isinstance(team.get('lineup'), list):
                raise DataError('Team requires roster and fixed lineup')
            selected_at = _date(team.get('lineup_selected_at'), 'lineup_selected_at')
            if selected_at > cutoff:
                return {**out, 'status': 'blocked', 'missing_inputs': ['lineup_known_at_decision_time']}
            roster, lineup = team['roster'], team['lineup']
            for pid in roster + lineup:
                _entity(pid)
            if len(set(roster)) != len(roster) or owned & set(roster):
                raise DataError('Branches must preserve unique league ownership')
            owned.update(roster)
            if len(lineup) != len(slots) or len(set(lineup)) != len(lineup) or not set(lineup) <= set(roster):
                raise DataError('Fixed lineup must assign each slot with a distinct owned player')
            for slot, pid in zip(slots, lineup):
                player = players.get(pid)
                if not isinstance(player, dict) or not isinstance(player.get('positions'), list) or not set(player['positions']) & set(slot['eligible_positions']):
                    raise DataError('Fixed lineup violates explicit position eligibility')
            required.update(lineup)
    if not isinstance(matchups, list) or any(not isinstance(pair, list) or len(pair) != 2
            or any(not isinstance(t, str) for t in pair) or pair[0] == pair[1] or not set(pair) <= teams for pair in matchups):
        raise DataError('Matchups require explicit distinct competition teams')
    if len({t for pair in matchups for t in pair}) != 2 * len(matchups):
        raise DataError('A team cannot appear in multiple weekly matchups')
    if not isinstance(worlds, list) or any(not isinstance(w, dict) for w in worlds):
        raise DataError('Worlds must be objects')
    ids = [_text(w.get('id'), 'world id') for w in worlds]
    if len(set(ids)) != len(ids):
        raise DataError('World IDs must be unique')
    weights = distribution({w['id']: w.get('weight') for w in worlds}, 'world weights')
    scores, missing = [], []
    for world in worlds:
        stats = world.get('stats')
        if not isinstance(stats, dict):
            raise DataError('World stats must be an object')
        points = {}
        for pid in sorted(required):
            if pid not in stats:
                missing.append(world['id'] + ':' + pid)
                continue
            scored = _score(stats[pid], out['binding']['scoring'])
            if scored['mean'] is None:
                missing.append(world['id'] + ':' + pid + ':' + ','.join(scored.get('missing', [])))
            else:
                points[pid] = scored['mean']
        scores.append(points)
    if missing:
        return {**out, 'status': 'blocked', 'missing_inputs': missing}
    baseline = request.get('baseline')
    if baseline not in branches:
        raise DataError('Explicit baseline branch is required')
    result = {}
    for branch, holdings in branches.items():
        totals = [{tid: sum(score[p] for p in team['lineup']) for tid, team in holdings.items()} for score in scores]
        means = {tid: sum(weights[w['id']] * totals[i][tid] for i, w in enumerate(worlds)) for tid in teams}
        covariance = {a: {b: sum(weights[w['id']] * (totals[i][a] - means[a]) *
                         (totals[i][b] - means[b]) for i, w in enumerate(worlds)) for b in sorted(teams)} for a in sorted(teams)}
        games = []
        for a, b in matchups:
            margins = [total[a] - total[b] for total in totals]
            base_margins = [sum(score[p] for p in branches[baseline][a]['lineup']) -
                            sum(score[p] for p in branches[baseline][b]['lineup']) for score in scores]
            games.append({'teams': [a, b], 'conditional_win_mass': sum(weights[w['id']] for i, w in enumerate(worlds) if margins[i] > 0),
                'conditional_tie_mass': sum(weights[w['id']] for i, w in enumerate(worlds) if margins[i] == 0),
                'mean_margin': sum(weights[w['id']] * margins[i] for i, w in enumerate(worlds)),
                'paired_delta_mean_margin': sum(weights[w['id']] * (margins[i] - base_margins[i]) for i, w in enumerate(worlds))})
        result[branch] = {'team_mean_points': means, 'team_covariance': covariance, 'matchups': games,
                          'world_team_points': [{'world_id': w['id'], 'points': totals[i]} for i, w in enumerate(worlds)]}
    return {**out, 'baseline': baseline, 'branches': result,
            'dependence': 'Exactly the supplied joint worlds; weights not normalized or fitted',
            'use': 'Conditional weekly head-to-head only; no schedule, playoff, title or multi-year probabilities',
            'legality_scope': 'Unique ownership and slot eligibility checked; capacity, locks and platform execution unverified',
            'sampling_error': None, 'sampling_scope': 'Exact finite-world enumeration; excludes model and input uncertainty'}


def run_experiment(request):
    """Single transport-friendly dispatch; module names are explicit opt-ins."""
    functions = {'observed_opportunity': observed_opportunity, 'role_state_futures': role_state_futures,
                 'weekly_competition': weekly_competition}
    if not isinstance(request, dict) or request.get('module') not in functions:
        raise DataError('Unknown optional experiment module')
    return functions[request['module']](request)
