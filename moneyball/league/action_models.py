"""Explicit, read-only lineup and draft action accounting.

Reuses Moneyball's source-cohort qualification and exact-slot assignment rather
than assigning independent player values. Board absence is never eligibility.
"""
from copy import deepcopy

from .store import DataError, digest, timestamp
from .trades import (_capacity, _lineup, _normalize_forecasts, _ordinary_ids,
                     _player_positions, _team_snapshot, _position_capacity,
                     _position_limit_rules, _status_record, _UNAVAILABLE_STATUSES)


def lineup_action(service, alias, action, *, as_of):
    from .lineups import packet
    result = packet(service, alias, evaluation_week=action.get('week'), fresh=False,
                    unavailable=action.get('unavailable'), as_of=as_of)
    chosen = action.get('slot_player_ids')
    slots = result['rules']['starters']
    if not isinstance(chosen, list) or len(chosen) != len(slots):
        raise DataError('slot_player_ids must assign every starter slot in rule order')
    if any(not isinstance(p, str) or not p for p in chosen) or len(set(chosen)) != len(chosen):
        raise DataError('Lineup assignments require distinct nonempty player IDs')
    data = service.snapshot(alias, as_of=as_of)['data']
    team = data['teams'][str(data['own_team_id'])]
    ordinary = set(_ordinary_ids(team))
    excluded = set(result['excluded_player_ids'])
    for index, pid in enumerate(chosen):
        if pid not in ordinary:
            raise DataError('Lineup assignment includes an unowned, reserve or taxi player')
        if pid in excluded:
            raise DataError('Lineup assignment includes a dated unavailable player')
        if not set(_player_positions(data['players'][pid])) & set(slots[index]['eligible_positions']):
            raise DataError('Lineup assignment violates starter-slot eligibility')
    groups, _ = _normalize_forecasts(service.forecast_rows(alias, as_of), players=data['players'],
        relevant_ids=ordinary, league_season=data['season'], league_week=result['evaluation_week'],
        decision_at=timestamp(as_of), scoring=result['rules']['scoring'], scoring_hash=digest(result['rules']['scoring']))
    for row in result['forecast_comparisons']:
        group = next(g for g in groups.values() if g['source'] == row['source'] and
                     g['period'] == row['period'] and g['conditioning'] == row['conditioning'] and g['season'] == row['season'])
        row['optimized_hold'] = row.pop('modeled_lineup')
        row['named_after'] = None
        row['delta_points_vs_optimized_hold'] = None
        if row['status'] == 'complete_counterfactual':
            after = {'selected': [{'slot_index': i, 'slot_label': slots[i].get('label'),
                'player_id': p, 'points': group['points'][p]} for i, p in enumerate(chosen)],
                'selected_player_ids': chosen, 'total_points': sum(group['points'][p] for p in chosen),
                'complete': True, 'filled_slots': len(slots), 'required_slots': len(slots)}
            row['named_after'] = after
            row['delta_points_vs_optimized_hold'] = after['total_points'] - row['optimized_hold']['total_points']
        row['use'] = 'Named slot assignment versus the same cohort optimized hold; lock state unverified, no championship estimate.'
    result['action'] = deepcopy(action)
    result['legality'] = {'slot_assignment_valid': True, 'platform_executable': None,
                          'lock_state_verified': False}
    return result


def draft_pick_action(service, alias, action, *, as_of):
    from .draft import context
    cutoff = timestamp(as_of)
    pid = action.get('player_id')
    if not isinstance(pid, str) or not pid:
        raise DataError('Draft pick requires a provider player_id')
    if not isinstance(action.get('board_hash'), str) or not action['board_hash']:
        raise DataError('Draft pick requires the observed board_hash')
    board = context(service, alias, fresh=False, draft_id=action.get('draft_id'), player_ids=[pid],
                    expected_board_hash=action['board_hash'], max_age_seconds=action.get('max_age_seconds', 20), as_of=cutoff)
    max_age = action.get('max_age_seconds', 20)
    if cutoff - board.get('board_available_at', cutoff) > max_age:
        raise DataError('Saved board observation expired at decision time; refresh and prepare a new decision')
    candidate = board['candidates'][0]
    if candidate['availability'] == 'observed_selected':
        raise DataError('Candidate was already selected on the saved draft board')
    data = deepcopy(service.snapshot(alias, as_of=cutoff)['data'])
    team = data['teams'][str(data['own_team_id'])]
    slots = data['rules'].get('starters')
    if not isinstance(slots, list) or not 1 <= len(slots) <= 16 or any(
            not isinstance(slot, dict) or not isinstance(slot.get('eligible_positions'), list)
            or not slot['eligible_positions'] for slot in slots):
        raise DataError('Draft sensitivity requires one to sixteen translated starter slots')
    players = data['players']
    if pid not in players and data['platform'] == 'sleeper':
        catalog = service.store.get('catalog', 'sleeper', cutoff, required=False)
        raw = catalog['data'].get(pid) if catalog else None
        if raw:
            players[pid] = {**raw, 'name': raw.get('name') or raw.get('full_name') or pid,
                            'positions': raw.get('positions') or raw.get('fantasy_positions') or []}
    if pid not in players or not _player_positions(players[pid]):
        raise DataError('Candidate identity and eligible positions are unavailable')
    owners = [tid for tid, t in data['teams'].items() if pid in t['player_ids']]
    if owners:
        raise DataError('Candidate is owned in the saved league snapshot; reconcile keeper/roster rights before modeling')
    picked = {r['player_id'] for r in board['own_selections']}
    ordinary = set(_ordinary_ids(team))
    roster_reconciled = picked == ordinary and board['pick_sequence_complete'] is True
    # Keep the two observations separate: no union silently manufactures a roster.
    before = _team_snapshot(team, data['rules']['starters'], data['rules'])
    after_team = deepcopy(team)
    after_team['player_ids'].append(pid)
    after = _team_snapshot(after_team, data['rules']['starters'], data['rules'])
    capacity = _capacity(after)
    limits, unresolved, supplied = _position_limit_rules(data['rules'].get('position_limits'), data['platform'])
    position_capacity = _position_capacity(after_team, players, limits, unresolved, supplied)
    gaps = ['Draft eligibility, keeper rights, current picker and remaining roster policy are not certified by board absence',
            'Future service and championship changes require conditional forecasts and future management policies']
    if not roster_reconciled:
        gaps.append('Observed own draft selections and ordinary league roster do not establish a reconciled draft roster')
    if capacity['within_capacity'] is not True:
        gaps.append('Post-pick roster capacity is missing or exceeded; explicit later release/promotion policy required')
    if not position_capacity['fully_translated']:
        gaps.append('Position-capacity rules are not fully translated')
    week = action.get('week', data.get('week'))
    if type(week) is not int or not 1 <= week <= 18:
        raise DataError('Draft weekly sensitivity requires an evaluation week from 1 to 18')
    groups, rejected = _normalize_forecasts(service.forecast_rows(alias, cutoff), players=players,
        relevant_ids=ordinary | {pid}, league_season=data['season'], league_week=week, decision_at=cutoff,
        scoring=data['rules']['scoring'], scoring_hash=digest(data['rules']['scoring']))
    comparisons = []
    required = ordinary | {pid}
    excluded = set()
    for player_id in required:
        status, observed, status_week, season = _status_record(players[player_id])
        if status in _UNAVAILABLE_STATUSES:
            if observed is not None and observed <= cutoff and status_week == week and season in (None, data['season']):
                excluded.add(player_id)
            else:
                gaps.append('Unavailable status is not dated for evaluation week: ' + player_id)
    if data['rules'].get('best_ball') is not False:
        gaps.append('Managed-lineup means do not estimate best-ball utility; lineup mode/joint distributions required')
    for g in groups.values():
        if g['period']['kind'] != 'week':
            continue
        missing = sorted(required - set(g['points']))
        conflicts = sorted(required & set(g['conflicts']))
        row = {'source': g['source'], 'season': g['season'], 'period': g['period'],
               'conditioning': g['conditioning'], 'missing_required_player_ids': missing,
               'conflicting_player_ids': conflicts, 'before': None, 'after': None, 'delta_points': None,
               'status': 'incomplete'}
        if not missing and not conflicts:
            row['before'] = _lineup(ordinary, data['rules']['starters'], players, g['points'], excluded)
            row['after'] = _lineup(required, data['rules']['starters'], players, g['points'], excluded)
            if row['before']['complete'] and row['after']['complete']:
                row['delta_points'] = row['after']['total_points'] - row['before']['total_points']
            row['status'] = 'conditional_weekly_sensitivity'
        comparisons.append(row)
    return {'action': deepcopy(action), 'evidence_as_of': cutoff, 'board_hash': board['board_hash'],
            'candidate': candidate, 'draft_observation': board, 'before_roster': before, 'after_roster': after,
            'capacity': capacity, 'position_capacity': position_capacity,
            'draft_roster_reconciled': roster_reconciled, 'legality': {'platform_executable': None},
            'forecast_comparisons': comparisons, 'forecast_rejections': rejected, 'gaps': gaps,
            'championship_probability_delta': None,
            'scope': 'Conditional named addition to saved roster, not a verified live draft action or dynasty valuation'}
