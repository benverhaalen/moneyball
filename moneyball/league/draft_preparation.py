"""Persistent host-authored draft branches; no generated rankings or execution.

Native adaptation of dependency-bound reuse inspected in DVC's stage/cache.py
and ParamsDependency (pinned 56e59829512ff134aa269099a2099587b810b4dd).
See the retained architecture-build seed-layer-three report for original code,
license and transfer limits. Content reuse is distinct from board observation
readiness. Human verdicts are conditional, never deterministic cached truth.
"""
from copy import deepcopy
import math
import time
import uuid

from .store import DataError, digest, timestamp

VERSION = 'draft-preparation-v1'


def _text(value, label, maximum=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DataError(f'{label} requires nonempty bounded text')
    return value


def _age_limit(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
        raise DataError('max_age_seconds must be between zero and 300')
    return value


def _board(service, packet, league):
    """Do not accept caller-supplied picks/times as an authenticated observation."""
    if not isinstance(packet, dict) or not packet.get('board_hash') or not packet.get('draft_id'):
        raise DataError('Supply a saved draft_context observation')
    matches = []
    for key in service.store.keys('draft_board'):
        row = service.store.get('draft_board', key)
        data = row['data']
        identity = data.get('league_identity', {})
        if (str(data.get('draft_id')) == str(packet['draft_id']) and
                all(str(identity.get(k)) == str(league.get(k)) for k in
                    ('platform', 'league_id', 'season', 'own_team_id', 'strategy_key'))):
            matches.append(row)
    if not matches:
        raise DataError('No independently saved board for this league and draft')
    record = max(matches, key=lambda r: r['version_id'])
    board = record['data']
    if board.get('board_hash') != packet['board_hash']:
        raise DataError('Supplied board is superseded; get current draft_context')
    if board.get('oldest_observed_at') is None:
        raise DataError('Saved board is missing its independent observation timestamp')
    observed = timestamp(board['oldest_observed_at'])
    if observed > time.time() + 1:
        raise DataError('Draft observation is future dated')
    return record


def _own_picks(board, team_id):
    return [p for p in board['picks'] if str(p.get('team_id')) == str(team_id)]


def save(service, decision_id, branches, board, *, max_age_seconds=300):
    """Persist between-pick prose and contingencies against exact dependencies.

    branches are ordered host judgments, not a score-derived ranking. Every item
    names one provider player_id, rationale, strategy_rationale, risk, horizon,
    confidence, strongest_countercase and reversal_conditions. Conditions are
    explicit named predicates whose unknown state remains visible on lookup.
    """
    from .decision import _manifest
    limit = _age_limit(max_age_seconds)
    context = service.store.get('decision', decision_id)['data']
    if context.get('intent') != 'draft' or context['specification'].get('as_of') is not None:
        raise DataError('Live preparation requires a current draft decision')
    if not isinstance(branches, list) or not 1 <= len(branches) <= 24:
        raise DataError('Provide one to twenty-four ordered host-authored branches')
    normalized, ids, players = [], set(), set()
    for branch in branches:
        if not isinstance(branch, dict):
            raise DataError('Each draft branch must be an object')
        item = deepcopy(branch)
        for field in ('id', 'player_id', 'rationale', 'strategy_rationale', 'risk', 'horizon',
                      'confidence', 'strongest_countercase'):
            _text(item.get(field), field)
        if item['id'] in ids or item['player_id'] in players:
            raise DataError('Branch IDs and target player IDs must be distinct')
        ids.add(item['id']); players.add(item['player_id'])
        reversals = item.get('reversal_conditions')
        if not isinstance(reversals, list) or not reversals or len(reversals) > 24:
            raise DataError('Each branch needs explicit reversal_conditions')
        for reversal in reversals:
            _text(reversal, 'reversal condition')
        conditions = item.setdefault('conditions', [])
        if not isinstance(conditions, list) or len(conditions) > 24:
            raise DataError('conditions must be a bounded list')
        keys = set()
        for condition in conditions:
            if not isinstance(condition, dict) or type(condition.get('expected')) is not bool:
                raise DataError('Each condition requires key, statement and boolean expected')
            _text(condition.get('key'), 'condition key', 200)
            _text(condition.get('statement'), 'condition statement')
            if condition['key'] in keys:
                raise DataError('Duplicate condition key within a branch')
            keys.add(condition['key'])
        normalized.append(item)
    with service.store.read_view():
        snapshot = service.snapshot(context['alias'])
        league = snapshot['data']
        if snapshot['sha256'] != context['snapshot']['sha256'] or digest(league['rules']) != context['rules_hash']:
            raise DataError('League changed; prepare a new draft decision')
        observed = _board(service, board, league)
        raw = observed['data']
        if time.time() - raw['oldest_observed_at'] > limit:
            raise DataError('Board expired before preparation')
        selected = {p['player_id'] for p in raw['picks']}
        if selected & players:
            raise DataError('A preparation target has already been selected')
        manifest = _manifest(service, context['alias'], context['evidence_as_of'], league, sorted(players))
        preparation = {'preparation_id': uuid.uuid4().hex, 'method_version': VERSION,
            'decision_id': decision_id, 'decision_revision': context['revision'], 'alias': context['alias'],
            'league_binding': context['league_binding'], 'rules_hash': context['rules_hash'],
            'league_snapshot_hash': snapshot['sha256'], 'strategy_hash': digest(context['strategy']),
            'objective': context['objective'], 'horizon': context['horizon'],
            'manifest': manifest, 'manifest_hash': digest(manifest), 'focused_player_ids': sorted(players),
            'board_version_id': observed['version_id'], 'board_hash': raw['board_hash'],
            'draft_id': raw['draft_id'], 'prepared_picks': deepcopy(raw['picks']),
            'draft_structure_hash': digest({k: raw.get(k) for k in ('order', 'settings', 'status', 'type')}),
            'own_selections': _own_picks(raw, league['own_team_id']),
            'branches': normalized, 'gaps': context['gaps'], 'prepared_at': time.time(),
            'scope': 'Host-authored conditional branches; no inferred draft eligibility, demand or title odds'}
    service.store.put('draft_preparation', preparation['preparation_id'], preparation,
                      {'role': 'host_authored_between_pick_branches', 'method_version': VERSION})
    return preparation


def lookup(service, preparation_id, board, *, condition_values=None, max_age_seconds=20, deadline=None):
    """Cheap local recovery after compaction or a snipe; never fetch or pick.

    A superseded board packet is refused. Rival selections may remove/promote
    authored alternatives. Own roster/decision/rules/evidence changes invalidate
    their rationale; the original prose remains recoverable but is not promoted.
    """
    from .decision import _manifest
    limit = _age_limit(max_age_seconds)
    values = {} if condition_values is None else condition_values
    if not isinstance(values, dict) or any(not isinstance(k, str) or type(v) not in (bool, type(None)) for k, v in values.items()):
        raise DataError('condition_values must map explicit names to bool or null')
    end = timestamp(deadline) if deadline is not None else None
    with service.store.read_view():
        prep = service.store.get('draft_preparation', preparation_id)['data']
        context = service.store.get('decision', prep['decision_id'])['data']
        snapshot = service.snapshot(prep['alias'])
        league = snapshot['data']
        observation = _board(service, board, league)
        raw = observation['data']
        if str(raw['draft_id']) != str(prep['draft_id']):
            raise DataError('Current observation belongs to a different draft')
        current_manifest = _manifest(service, prep['alias'], time.time(), league, prep['focused_player_ids'])
        old = {(r['kind'], r['key']): r['sha256'] for r in prep['manifest']}
        new = {(r['kind'], r['key']): r['sha256'] for r in current_manifest}
        blockers = []
        if context['revision'] != prep['decision_revision']:
            blockers.append('decision_revision_changed')
        if digest(league['rules']) != prep['rules_hash']:
            blockers.append('rules_changed')
        if snapshot['sha256'] != prep['league_snapshot_hash']:
            blockers.append('league_state_changed')
        if digest(service.strategy(prep['alias'])) != prep['strategy_hash']:
            blockers.append('strategy_changed')
        if old != new:
            blockers.append('evidence_changed')
        if _own_picks(raw, league['own_team_id']) != prep['own_selections']:
            blockers.append('own_draft_roster_changed')
        if raw['picks'][:len(prep['prepared_picks'])] != prep['prepared_picks']:
            blockers.append('observed_pick_prefix_revised')
        if digest({k: raw.get(k) for k in ('order', 'settings', 'status', 'type')}) != prep['draft_structure_hash']:
            blockers.append('draft_structure_changed')
        age = time.time() - raw['oldest_observed_at']
        if age > limit:
            blockers.append('board_expired')
        if end is not None and time.time() >= end:
            blockers.append('deadline_expired')
        selected = {p['player_id'] for p in raw['picks']}
        removed, excluded, choices = [], [], []
        catalog = service.store.get('catalog', 'sleeper', required=False) if league['platform'] == 'sleeper' else None
        catalog = catalog['data'] if catalog else {}
        for authored in prep['branches']:
            item = deepcopy(authored)
            if item['player_id'] in selected:
                removed.append({'id': item['id'], 'player_id': item['player_id'], 'reason': 'observed_selected'})
                continue
            states = []
            for predicate in item['conditions']:
                actual = values.get(predicate['key'])
                status = 'unknown' if actual is None else 'met' if actual == predicate['expected'] else 'not_met'
                states.append({**predicate, 'observed_value': actual, 'status': status})
            item['condition_states'] = states
            if any(s['status'] == 'not_met' for s in states):
                excluded.append(item)
                continue
            identity = league.get('players', {}).get(item['player_id']) or catalog.get(item['player_id']) or {}
            item['positions'] = identity.get('positions') or identity.get('fantasy_positions') or []
            item['availability'] = 'not_observed_selected' if identity else 'unknown_identity_and_eligibility'
            item['readiness'] = 'invalidated' if blockers else 'conditional_unknowns' if any(s['status'] == 'unknown' for s in states) or not identity else 'conditional'
            choices.append(item)
    return {'preparation_id': preparation_id, 'decision_id': prep['decision_id'],
        'decision_revision': prep['decision_revision'], 'current_decision_revision': context['revision'],
        'method_version': VERSION, 'objective': prep['objective'], 'horizon': prep['horizon'],
        'board_hash': raw['board_hash'], 'prepared_board_hash': prep['board_hash'],
        'board_version_id': observation['version_id'], 'board_age_seconds': max(0, age),
        'alternative_coverage': {'distinct_positions': sorted({p for c in choices for p in c['positions']}),
            'distinct_horizons': sorted({c['horizon'] for c in choices}),
            'interpretation': 'Coverage of authored alternatives, not inferred strategic sufficiency'},
        'board_changed': raw['board_hash'] != prep['board_hash'], 'blockers': blockers,
        'first_useful_choice': choices[0] if choices and not blockers else None,
        'choices': choices, 'removed_targets': removed, 'excluded_by_conditions': excluded,
        'gaps': prep['gaps'], 'requires_repreparation': bool(blockers),
        'strategy_drift': any(b in blockers for b in ('rules_changed', 'strategy_changed', 'decision_revision_changed', 'own_draft_roster_changed', 'draft_structure_changed', 'observed_pick_prefix_revised')),
        'recheck': {'tool': 'draft_context', 'arguments': {'alias': prep['alias'], 'fresh': True,
                   'expected_board_hash': raw['board_hash'], 'player_ids': prep['focused_player_ids']},
                   'purpose': 'Final independent board recheck before the user acts'},
        'limitations': ['Authored order retained; no new ranking or ADP availability model',
            'Unselected is not certified draft eligibility; no supported live clock is inferred',
            'Unknown conditions remain conditional; caller-provided values are assertions, not independently verified facts',
            'No guarantee that the board stays unchanged after observation; user alone submits picks']}
