"""Evaluate preauthored draft branches locally; never score players or select one.

Compilation is the reasoning boundary. Evaluation only checks author-declared
conditions, unchanged evidence, and fresh rendered-board consistency using
draft_room.live_check. A ready recommendation is not permission to click.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time

from . import draft_room
from .mock_lab import verify_pick, verify_shortlist
from .store import digest

VERSION = 1
PROVISIONAL_LABEL = ('Provisional authored branch; the visible-slate comparison is incomplete. '
                     'This is not a best-player claim, recommendation, or permission to select.')


def _ids(value, name, *, allow_empty=True):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)
            or len(set(value)) != len(value) or (not value and not allow_empty)):
        raise ValueError(name + ' must be an explicit list of distinct player ID strings')
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing authored ' + name)
    return value


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Time must be finite epoch seconds')
    return value


def _payload_hash(value):
    return digest({k: v for k, v in value.items() if k != 'compiled_hash'})


def compile_plan(authored, deck, board, *, prepared_at=None):
    """Bind author order/text to next own pick, exact roster and analyzed evidence.

    live_mentions is an exhaustive author declaration of *actionable* player
    mentions in text/reasons. Factual NFL teammates and owned players are not
    availability dependencies. Software cannot verify prose semantics.
    """
    now = _time(time.time() if prepared_at is None else prepared_at)
    if authored.get('schema_version') != VERSION:
        raise ValueError('Unsupported authored schema_version')
    analyzed = _ids(authored.get('analyzed_player_ids'), 'analyzed_player_ids', allow_empty=False)
    watched = _ids(authored.get('watched_player_ids'), 'watched_player_ids')
    if not set(watched) <= set(analyzed):
        raise ValueError('Every watched player must be analyzed')
    _text(authored.get('news_revision'), 'news_revision')
    _text(authored.get('ordering_rationale'), 'ordering_rationale')
    if any(pid not in deck['cards'] for pid in analyzed):
        raise ValueError('Analyzed player missing from compiled deck')
    branches = authored.get('branches')
    if not isinstance(branches, list) or not 1 <= len(branches) <= 72:
        raise ValueError('Need one to seventy-two authored branches')
    branch_ids, choices, candidates, source_hashes, warnings = set(), [], set(), {}, []
    for b in branches:
        bid = _text(b.get('branch_id'), 'branch_id')
        if bid in branch_ids:
            raise ValueError('Duplicate branch_id')
        branch_ids.add(bid)
        pid = _text(b.get('player_id'), 'player_id')
        alt = _text(b.get('alternative_id'), 'alternative_id')
        _text(b.get('exact_user_text'), 'exact_user_text')
        _text(b.get('confidence'), 'confidence')
        _text(b.get('precedence_conditions'), 'precedence_conditions')
        if b.get('live_mentions_complete') is not True:
            raise ValueError('Author must attest live_mentions_complete')
        live = _ids(b.get('live_mentions'), 'live_mentions', allow_empty=False)
        required = _ids(b.get('requires_all_available'), 'requires_all_available')
        drafted = _ids(b.get('requires_drafted', []), 'requires_drafted')
        if pid not in live:
            raise ValueError('Selected player must be a live mention')
        all_refs = {pid, alt, *live, *required, *drafted}
        groups = b.get('requires_any_available')
        if not isinstance(groups, list):
            raise ValueError('Declare requires_any_available, even when empty')
        group_ids = set()
        for group in groups:
            gid = _text(group.get('group_id'), 'group_id')
            if gid in group_ids:
                raise ValueError('Duplicate group_id within branch')
            group_ids.add(gid)
            ids = _ids(group.get('player_ids'), 'group player_ids', allow_empty=False)
            count = group.get('min_count')
            if type(count) is not int or not 1 <= count <= len(ids):
                raise ValueError('Group min_count outside declared candidate count')
            all_refs.update(ids)
        if not all_refs <= set(analyzed):
            raise ValueError('Branch references an unanalyzed player')
        if set(drafted) & (set(live) | set(required)):
            raise ValueError('A player cannot be required both drafted and available')
        reasons = b.get('reasons', {})
        for key in draft_room.REASONS:
            if not isinstance(reasons.get(key), str) or len(reasons[key].split()) < 5:
                raise ValueError('Missing substantive reason: ' + key)
        refs = b.get('source_refs')
        if not isinstance(refs, list) or not refs:
            raise ValueError('Need explicit source_refs')
        for ref in refs:
            if (not isinstance(ref, dict) or not (ref.get('path') or ref.get('url'))
                    or not isinstance(ref.get('sha256'), str)
                    or re.fullmatch('[a-f0-9]{64}', ref['sha256']) is None):
                raise ValueError('Source reference needs path/url and sha256')
            key = ref.get('path') or ref['url']
            if key in source_hashes and source_hashes[key] != ref['sha256']:
                raise ValueError('Conflicting hashes for one source reference')
            source_hashes[key] = ref['sha256']
        comparison = b.get('comparison_bundles')
        if comparison is not None:
            left = _ids(comparison.get('chosen_then_later'), 'chosen_then_later', allow_empty=False)
            right = _ids(comparison.get('alternative_then_later'), 'alternative_then_later', allow_empty=False)
            if not (set(left) | set(right)) <= set(analyzed):
                raise ValueError('Comparison bundle contains unanalyzed player')
            if set(left) == set(right):
                if comparison.get('claims_bundle_total_advantage') is True:
                    raise ValueError('Identical completed bundles cannot have a membership-based total advantage')
                warnings.append({'branch_id': bid, 'code': 'identical_bundle_membership',
                    'meaning': 'Choice can only depend on acquisition order or different conditions, not the same bundle total.'})
        if pid not in candidates:
            # Existing preparation supplies bindings, not branch-selection logic.
            choices.append({**reasons, 'player_id': pid, 'alternative_id': alt,
                            'requires_available': []})
            candidates.add(pid)
    guard = draft_room.prepare({'target_pick': authored.get('target_pick'), 'choices': choices}, deck, board)
    guard['prepared_at'] = now
    guard['card_hashes'] = {pid: digest(deck['cards'][pid]) for pid in analyzed}
    guard['prepared_payload_hash'] = digest({k: v for k, v in guard.items() if k != 'prepared_payload_hash'})
    result = {'schema_version': VERSION, 'authored': copy.deepcopy(authored),
              'prepared_at': now, 'guard_plan': guard,
              'own_slot': board['own_slot'], 'source_hashes': source_hashes, 'warnings': warnings,
              'source_ref_hash': digest([b['source_refs'] for b in branches]),
              'scope': 'Conditional analyst judgment; no utility estimate or new player ranking.'}
    result['compiled_hash'] = _payload_hash(result)
    return result


def _branch_failures(branch, taken):
    failures = []
    # Never edit prose to remove a name: revoke the whole text containing it.
    gone = sorted((set(branch['live_mentions']) | set(branch['requires_all_available'])) & taken)
    if gone:
        failures.append({'code': 'live_target_or_required_player_drafted', 'player_ids': gone})
    pending = sorted(set(branch.get('requires_drafted', [])) - taken)
    if pending:
        failures.append({'code': 'required_draft_condition_not_met', 'player_ids': pending})
    for group in branch['requires_any_available']:
        available = [p for p in group['player_ids'] if p not in taken]
        if len(available) < group['min_count']:
            failures.append({'code': 'continuation_group_below_minimum', 'group_id': group['group_id'],
                             'remaining_ids': available, 'required_count': group['min_count']})
    return failures


def _option(branch, rank):
    return {'rank': rank, 'branch_id': branch['branch_id'], 'player_id': branch['player_id'],
            'exact_user_text': branch['exact_user_text'], 'confidence': branch['confidence'],
            'reasons': copy.deepcopy(branch['reasons']),
            'source_refs': copy.deepcopy(branch['source_refs']),
            'live_mentions': list(branch['live_mentions']),
            'precedence_conditions': branch['precedence_conditions']}


def evaluate(plan, deck, board, ui, *, now, current_news_revision, current_source_hashes, visible_slate_ids,
             slate_scope, unreviewed_fact_ids=(), private_mock_id=None,
             mock_ui=None, player_catalog=None):
    """Return up to three verbatim options, or precise blockers; no I/O or LLM.

    ui uses draft_room.live_check's sleeper_rendered_ui schema. visible_slate_ids
    is the *complete supplied* top-visible or full available slate, never merely
    the old shortlist. An outside analyzed ID forces review without claiming it
    is a better player. Only that coverage blocker permits separate provisional
    display of otherwise-valid authored branches; options stays empty and no
    recommendation is ready. Private mock callers can additionally supply the raw UI
    shape consumed by mock_lab.verify_pick. Real selection always stays manual.
    """
    checked = _time(now)
    out = {'ready_to_recommend': False, 'needs_reasoning': True, 'problems': [],
           'options': [], 'provisional_options': [], 'rejected_branches': [],
           'prepared_at': plan.get('prepared_at'),
           'checked_at': checked, 'writes_to_sleeper': False, 'ready_to_execute': False,
           'scope': 'Author-ranked conditional recommendations. No numerical utility or click permission.'}
    if plan.get('compiled_hash') != _payload_hash(plan):
        out['problems'] = ['compiled_plan_modified']; return out
    authored = plan['authored']
    try:
        visible = _ids(visible_slate_ids, 'visible_slate_ids', allow_empty=False)
        if slate_scope not in ('full_available_slate', 'complete_top_visible_slate'):
            raise ValueError('Declare full_available_slate or complete_top_visible_slate')
        taken = set(_ids(board.get('taken'), 'taken'))
        if board.get('picks_made') != len(taken):
            raise ValueError('picks_made differs from complete taken prefix')
    except ValueError as exc:
        out['problems'] = [str(exc)]; return out
    out['slate_scope'] = slate_scope
    out['warnings'] = copy.deepcopy(plan['warnings'])
    out['checked_prefix'] = board.get('prefix_hash')
    if board.get('own_slot') != plan['own_slot']:
        out['problems'].append('own_slot_changed')
    for key, value in plan['source_hashes'].items():
        if not isinstance(current_source_hashes, dict) or current_source_hashes.get(key) != value:
            out['problems'].append('source_reference_changed_or_unchecked:' + key)
    for source, stamp in [('board', board.get('checked_at')),
                          ('rendered_ui', ui.get('observed_at') if isinstance(ui, dict) else None)]:
        try:
            _time(stamp)
        except ValueError:
            out['problems'].append(source + '_timestamp_invalid')
    if current_news_revision != authored['news_revision']:
        out['problems'].append('news_revision_changed')
    if unreviewed_fact_ids:
        out['problems'].append('unreviewed_facts')
        out['unreviewed_fact_ids'] = list(unreviewed_fact_ids)
    stale_slate = sorted(set(visible) & taken)
    if stale_slate:
        out['problems'].append('visible_slate_contains_drafted_players')
        out['stale_slate_ids'] = stale_slate
    novel = sorted(set(visible) - set(authored['analyzed_player_ids']) - taken)
    if novel:
        out['problems'].append('novel_slider_needs_review')
        out['novel_player_ids'] = novel
    # Delegate timestamp/roster/rules/policy/evidence/turn/UI consistency guards.
    try:
        guard = draft_room.live_check(plan['guard_plan'], deck, board, ui, now=checked)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        out['problems'].append('invalid_board_or_ui:' + str(exc)); return out
    out['problems'].extend(guard['problems'])
    if not guard.get('on_clock', False):
        out['problems'].append('not_on_own_turn')
    if board.get('picks_made', -1) + 1 != authored['target_pick']:
        out['problems'].append('target_pick_not_current')
    if not guard.get('actionable_freshness_verified'):
        out['problems'].append('fresh_rendered_guard_not_ready')
    eligible, seen = [], set()
    for b in authored['branches']:
        failures = _branch_failures(b, taken)
        if failures:
            out['rejected_branches'].append({'branch_id': b['branch_id'], 'failures': failures})
        elif b['player_id'] not in seen:
            eligible.append(b); seen.add(b['player_id'])
    if not eligible:
        out['problems'].append('no_authored_branch_remains_valid')
    selected = eligible[:3]
    if selected:
        shortlist = verify_shortlist([b['player_id'] for b in selected], list(taken))
        out['problems'].extend(shortlist['blockers'])
    novelty_only = set(out['problems']) == {'novel_slider_needs_review'}
    if private_mock_id is not None and selected and (not out['problems'] or novelty_only):
        # Preserve the existing ready path's first-option check. Provisional
        # display has no selected option, so validate every displayed candidate.
        controls = []
        for b in selected if novelty_only else selected[:1]:
            pid = b['player_id']
            control = verify_pick(mock_ui or {}, allowed_mock_id=private_mock_id,
                expected_pick=authored['target_pick'],
                expected_board=[{'pick': i, 'player_id': p} for i, p in enumerate(board['taken'], 1)],
                expected_own_roster=board['own_roster'],
                candidate={'player_id': pid, 'name': deck['cards'][pid]['name']},
                now=datetime.fromtimestamp(checked, timezone.utc).isoformat(),
                player_catalog=player_catalog)
            controls.append({'player_id': pid, 'control': control})
            out['problems'].extend(control['blockers'])
        if novelty_only:
            out['mock_provisional_control_checks'] = controls
        else:
            out['mock_selected_control_check'] = controls[0]['control']
    out['problems'] = list(dict.fromkeys(out['problems']))
    if not out['problems']:
        out['ready_to_recommend'] = True
        out['needs_reasoning'] = False
        out['options'] = [_option(b, i) for i, b in enumerate(selected, 1)]
    elif out['problems'] == ['novel_slider_needs_review']:
        for i, b in enumerate(selected, 1):
            provisional = _option(b, i)
            provisional['author_rank'] = provisional.pop('rank')
            provisional.update({'comparison_status': 'incomplete_visible_slate_review',
                                'is_recommendation': False,
                                'display_label': PROVISIONAL_LABEL})
            out['provisional_options'].append(provisional)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('compile', 'evaluate'):
        p = sub.add_parser(name)
        p.add_argument('plan', type=Path); p.add_argument('--deck', type=Path, required=True)
        p.add_argument('--board', type=Path, required=True)
        if name == 'evaluate':
            p.add_argument('--ui', type=Path, required=True)
            p.add_argument('--slate', type=Path, required=True,
                           help='JSON {player_ids:[...], scope:complete_top_visible_slate}')
            p.add_argument('--news-revision', required=True)
            p.add_argument('--source-hashes', type=Path, required=True,
                           help='Current local source-revision registry {path_or_url:sha256}')
            p.add_argument('--unreviewed-facts', type=Path)
        p.add_argument('--output', type=Path)
    args = ap.parse_args(); started = time.perf_counter()
    try:
        load = lambda path: json.loads(path.read_text())
        plan, deck, board = load(args.plan), load(args.deck), load(args.board)
        if args.command == 'compile':
            if deck.get('content_hash') != digest({k: deck[k] for k in ('packet_build', 'policy', 'cards')}):
                raise ValueError('Compiled deck content hash mismatch; rebuild deck')
            result = compile_plan(plan, deck, board)
        else:
            slate = load(args.slate)
            result = evaluate(plan, deck, board, load(args.ui), now=time.time(),
                current_news_revision=args.news_revision, visible_slate_ids=slate['player_ids'],
                current_source_hashes=load(args.source_hashes),
                slate_scope=slate['scope'],
                unreviewed_fact_ids=load(args.unreviewed_facts) if args.unreviewed_facts else [])
        if args.output:
            draft_room.write_json(args.output, result)
        print(json.dumps({'elapsed_ms': round((time.perf_counter()-started)*1000, 3),
                          'result': result}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ap.exit(2, json.dumps({'error': str(exc)}) + '\n')


if __name__ == '__main__':
    main()
