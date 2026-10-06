"""Bounded, recoverable analyst context for an unpredictable draft.

This is a retrieval and handoff layer, not a ranking, forecast or click agent.
Full dossiers stay external. No text is silently truncated to fit a budget.
"""
import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import time

from .draft_room import POLICY, load_deck, write_json
from .store import digest

VERSION = 1
STATE_FIELDS = ('horizon_policy', 'user_preferences', 'roster_obligations',
                'failure_exposures', 'open_comparisons', 'unresolved_questions')
COMPARISON_FIELDS = ('question', 'current_effect', 'future_effect', 'capacity_cost',
                     'best_counterargument', 'reversal_trigger', 'confidence')


def _distinct(values, name):
    if (not isinstance(values, list) or any(not isinstance(p, str) or not p for p in values)
            or len(set(values)) != len(values)):
        raise ValueError(name + ' must be distinct nonempty string IDs')
    return values


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing explicit ' + name)
    return value


def _hash(doc):
    return digest({k: v for k, v in doc.items() if k != 'content_hash'})


def index_deck(deck):
    """Navigation for the ENTIRE acquired universe, without utility sorting.

    Paragraphs are addressable verbatim spans. A locator is never evidence of
    having read the paragraph, and matching a word is not a causal judgment.
    """
    players = {}
    for pid, c in deck['cards'].items():
        text = c.get('full_review') or ''
        spans = text.split('\n\n')
        players[pid] = {
            'player_id': pid, 'name': c['name'], 'position': c['position'],
            'team': c.get('identity', {}).get('team'),
            'adp_acquisition_reference_only': c.get('identity', {}).get('adp'),
            'card_hash': digest(c), 'review_hash': digest(text),
            'review_path': (c.get('review') or {}).get('review_path'),
            'packet_path': c.get('packet_path'),
            'information_cutoff': c.get('information_cutoff'),
            'paragraphs': [{'id': str(i), 'characters': len(p), 'sha256': digest(p)}
                           for i, p in enumerate(spans)],
            'full_review_characters': len(text),
        }
    out = {'schema_version': VERSION, 'deck_hash': deck.get('content_hash'),
           'players': players, 'scope': 'All acquired players, not all ownable players; no rankings.'}
    out['content_hash'] = _hash(out)
    return out


def _check_index(index, deck):
    if index.get('content_hash') != _hash(index):
        raise ValueError('Context index was modified')
    if index.get('deck_hash') != deck.get('content_hash'):
        raise ValueError('Deck revision changed; rebuild context index')


def _board(board):
    taken = _distinct(board['taken'], 'taken')
    if board['picks_made'] != len(taken):
        raise ValueError('Incomplete board prefix')
    own = _distinct(board['own_roster'], 'own_roster')
    if not set(own) <= set(taken):
        raise ValueError('Owned player absent from full taken prefix')
    return taken, own


def checkpoint(authored, deck, board, *, created_at=None):
    """Freeze explicitly authored reasoning; never summarize prior chat.

    Facts retain source-span references. Open comparisons retain contrary
    evidence/reversal conditions. The author remains responsible for completeness
    and for whether the cited material supports their interpretation.
    """
    taken, own = _board(board)
    if authored.get('schema_version') != VERSION:
        raise ValueError('Unsupported ledger schema')
    for key in STATE_FIELDS:
        if key not in authored:
            raise ValueError('Missing ledger field: ' + key)
    _text(authored['horizon_policy'], 'horizon_policy')
    _text(authored.get('news_revision'), 'news_revision')
    for key in STATE_FIELDS[1:]:
        if not isinstance(authored[key], list):
            raise ValueError(key + ' must be an explicit list')
    cards = deck['cards']; seen = set(); available_comparison_ids = {}
    for item in authored['open_comparisons']:
        cid = _text(item.get('comparison_id'), 'comparison_id')
        if cid in seen:
            raise ValueError('Duplicate comparison ID')
        seen.add(cid)
        for key in COMPARISON_FIELDS:
            _text(item.get(key), key)
        pids = _distinct(item.get('player_ids'), 'comparison player_ids')
        if len(pids) < 2:
            raise ValueError('Comparison needs a genuine competing choice')
        _distinct(item.get('requires_available'), 'requires_available')
        if not set(item['requires_available']) <= set(pids):
            raise ValueError('Availability dependency absent from comparison')
        if not set(pids) <= set(cards):
            raise ValueError('Comparison references unknown dossier')
        # player_ids are acquisition options/continuations, not arbitrary NFL
        # teammate mentions. Derive dependencies rather than trusting a second
        # manually duplicated list to remember the strongest alternative.
        available_comparison_ids[cid] = [p for p in pids if p not in taken]
        refs = item.get('evidence_spans')
        if not isinstance(refs, list) or not refs:
            raise ValueError('Comparison needs verbatim evidence span locators')
        for ref in refs:
            pid = ref.get('player_id'); paragraph = ref.get('paragraph_id')
            if pid not in pids or not isinstance(paragraph, str) or not paragraph.isdigit():
                raise ValueError('Invalid comparison evidence locator')
            spans = cards[pid]['full_review'].split('\n\n')
            if int(paragraph) >= len(spans) or ref.get('sha256') != digest(spans[int(paragraph)]):
                raise ValueError('Evidence span changed or invalid')
    screenings = authored.get('screenings')
    if not isinstance(screenings, dict):
        raise ValueError('screenings must explicitly distinguish unseen from screened')
    for pid, record in screenings.items():
        if pid not in cards or record.get('status') not in ('consider', 'defer', 'reject_for_this_roster'):
            raise ValueError('Invalid player screening record')
        for key in ('reason', 'reopen_if'):
            _text(record.get(key), 'screening ' + key)
        if record.get('card_hash') != digest(cards[pid]):
            raise ValueError('Screening evidence changed')
        if not isinstance(record.get('roster_sensitive'), bool):
            raise ValueError('Declare roster sensitivity')
    watched = _distinct(authored.get('watched_player_ids'), 'watched_player_ids')
    if not set(watched) <= set(cards):
        raise ValueError('Unknown watched player')
    result = {'schema_version': VERSION, 'created_at': time.time() if created_at is None else created_at,
              'draft_id': board['draft_id'], 'own_slot': board['own_slot'],
              'rules_hash': board['rules_hash'], 'policy_hash': digest(deck['policy']),
              'own_roster': own, 'taken_prefix': taken, 'authored': copy.deepcopy(authored),
              'available_comparison_ids': available_comparison_ids,
              'referenced_card_hashes': {p: digest(cards[p]) for p in
                  set(screenings) | set(watched) | {p for x in authored['open_comparisons'] for p in x['player_ids']}},
              'scope': 'Analyst claims with evidence pointers; no completeness or optimality guarantee.'}
    result['content_hash'] = _hash(result)
    return result


def recover(ledger, deck, board, *, news_revision):
    taken, own = _board(board)
    if ledger.get('content_hash') != _hash(ledger):
        raise ValueError('Ledger modified after checkpoint')
    for key in ('draft_id', 'own_slot', 'rules_hash'):
        if ledger.get(key) != board.get(key):
            raise ValueError('Recovery mismatch: ' + key)
    if ledger['policy_hash'] != digest(deck['policy']):
        raise ValueError('Strategy changed')
    prior = ledger['taken_prefix']
    if taken[:len(prior)] != prior:
        raise ValueError('Draft prefix revised; explicit reconciliation required')
    roster_changed = own != ledger['own_roster']
    changed = [p for p, h in ledger['referenced_card_hashes'].items()
               if p not in deck['cards'] or digest(deck['cards'][p]) != h]
    news_changed = news_revision != ledger['authored']['news_revision']
    comparisons = []
    for item in ledger['authored']['open_comparisons']:
        invalid = []
        if roster_changed: invalid.append('own_roster_changed')
        if news_changed: invalid.append('news_changed')
        if set(item['player_ids']) & set(changed): invalid.append('evidence_changed')
        dependencies = set(item['requires_available']) | set(
            ledger['available_comparison_ids'][item['comparison_id']])
        gone = sorted(dependencies & set(taken))
        if gone: invalid.append('required_player_drafted')
        comparisons.append({**item, 'requires_reconsideration': invalid, 'lost_dependencies': gone})
    screenings = {}
    for pid, item in ledger['authored']['screenings'].items():
        if pid in taken: continue
        invalid = pid in changed or news_changed or (roster_changed and item['roster_sensitive'])
        screenings[pid] = {**item, 'needs_rescreening': invalid}
    return {'new_picks': taken[len(prior):], 'roster_changed': roster_changed,
            'news_changed': news_changed, 'changed_evidence': changed,
            'comparisons': comparisons, 'screenings': screenings,
            'state_requires_reconsideration': roster_changed or news_changed or bool(changed),
            'scope': 'Recovery reconciles observations; it does not validate the analyst judgment.'}


def frontier(index, board, recovery, *, watched=(), visible=(), challenger=()):
    """Full-universe review queues. ADP never removes a candidate.

    Priority lists are authored/observed attention requests, NOT utility order.
    Unseen players remain discoverable even with no ADP, projections or tags.
    """
    taken, _ = _board(board); taken = set(taken); players = index['players']
    available = [p for p in players if p not in taken]
    screened = recovery['screenings']
    unscreened = [p for p in available if p not in screened or screened[p]['needs_rescreening']]
    requests = []
    for label, group in [('visible_board', visible), ('authored_watch', watched), ('independent_challenger', challenger)]:
        for pid in dict.fromkeys(group):
            if pid in taken: continue
            requests.append({'player_id': pid, 'reason': label,
                             'has_dossier': pid in players,
                             'screened_for_current_state': pid in screened and not screened[pid]['needs_rescreening']})
    by_position = {}
    for pid in unscreened:
        by_position.setdefault(players[pid]['position'], []).append(pid)
    return {'available_ids': available, 'unscreened_ids_by_position': by_position,
            'attention_requests': requests, 'available_count': len(available),
            'unscreened_count': len(unscreened),
            'screening_complete_for_acquired_universe': not unscreened,
            'unknown_observed_ids': [p for p in dict.fromkeys(visible) if p not in players and p not in taken],
            'scope': 'No recommendation or value ranking. All acquired available players remain in the denominator.'}


def pack(index, ledger, deck, board, *, news_revision, focus_ids, visible=(), challenger=(),
         max_characters=42000, generated_at=None):
    """Small prompt with full chosen cases and explicit omitted evidence handles.

    Oversize is an explicit failure, never arbitrary truncation. A recovery pack
    is not actionable confirmation of a live UI or a completed player review.
    """
    _check_index(index, deck)
    ids = _distinct(focus_ids, 'focus_ids')
    if not 1 <= len(ids) <= 6:
        raise ValueError('Choose one to six full cases; page rather than silently truncating')
    r = recover(ledger, deck, board, news_revision=news_revision)
    f = frontier(index, board, r, watched=ledger['authored']['watched_player_ids'],
                 visible=visible, challenger=challenger)
    cases = []
    for pid in ids:
        if pid not in index['players'] or pid in board['taken']:
            raise ValueError('Focused candidate unavailable or outside acquired dossiers: ' + pid)
        c = deck['cards'][pid]
        if digest(c) != index['players'][pid]['card_hash']:
            raise ValueError('Indexed card changed')
        cases.append({'locator': index['players'][pid], 'full_review': c['full_review'],
                      'packet_not_loaded': True,
                      'scope': 'Full authored review included; numerical packet remains external and may be needed.'})
    factual_roster = [{'player_id': p, 'name': deck['cards'].get(p, {}).get('name'),
                       'position': deck['cards'].get(p, {}).get('position')}
                      for p in board['own_roster']]
    attention = {row['player_id'] for row in f['attention_requests']}
    relevant_screenings = {p:v for p,v in r['screenings'].items() if p in set(ids) | attention}
    context = {'schema_version': VERSION, 'strategy': deck['policy'],
               'board': {k: board.get(k) for k in ('draft_id','picks_made','next_own_pick','following_own_pick',
                    'checked_at','on_clock','starters','rules_hash','prefix_hash')},
               'own_roster': factual_roster,
               'position_counts_not_legal_coverage': dict(Counter(x['position'] for x in factual_roster)),
               'authored_state': {k: ledger['authored'][k] for k in STATE_FIELDS if k != 'open_comparisons'},
               'recovery': {k:v for k,v in r.items() if k != 'screenings'},
               'relevant_screening_receipts': relevant_screenings,
               'discovery': {'available_count': f['available_count'], 'unscreened_count': f['unscreened_count'],
                    'unscreened_counts_by_position': {k:len(v) for k,v in f['unscreened_ids_by_position'].items()},
                    'attention_requests': f['attention_requests'], 'unknown_observed_ids': f['unknown_observed_ids'],
                    'attention_outside_focus_ids': sorted(attention-set(ids)),
                    'search_incomplete': bool(f['unscreened_count'] or f['unknown_observed_ids']),
                    'scope': 'Focused review does not establish superiority over omitted candidates.'},
               'cases': cases, 'recommendation': None, 'ready_to_execute': False,
               'not_in_context': ['Entire board event history', 'Other players full dossiers',
                                 'Raw forecast and source packets', 'Automatic validation of source support'],
               'recovery_contract': 'Reconsider flagged obligations/comparisons, read needed external evidence, '
                    'challenge the favorite, then write a new board-bound choice. Do not replay old chat.'}
    size = len(json.dumps(context, ensure_ascii=False, separators=(',', ':')))
    state_size = len(json.dumps({k:v for k,v in context.items() if k != 'cases'},
                               ensure_ascii=False, separators=(',', ':')))
    if size > max_characters:
        return {'within_budget': False, 'characters': size, 'max_characters': max_characters,
                'state_characters': state_size, 'state_alone_exceeds_budget':state_size > max_characters,
                'context': None, 'action': ('Explicitly retire/revise ledger state before loading any cases.'
                    if state_size > max_characters else 'Narrow focus or explicitly revise ledger; no evidence was silently cut.'),
                'full_frontier': f}
    result = {'within_budget': True, 'characters': size, 'state_characters':state_size,
            'max_characters': max_characters, 'generated_at': time.time() if generated_at is None else generated_at,
            'bindings': {'ledger_hash':ledger['content_hash'],'index_hash':index['content_hash'],
                'deck_hash':deck['content_hash'],'board_hash':digest(board),'news_revision':news_revision},
            'budget_unit': 'Unicode characters, not model tokens', 'context': context, 'full_frontier': f}
    result['content_hash'] = _hash(result)
    return result


def verify_pack(result, index, ledger, deck, board, *, news_revision, now, max_age=15):
    """Detect stale/tampered handoffs. This is NOT a rendered-board action guard."""
    problems = []
    if result.get('within_budget') is not True or result.get('content_hash') != _hash(result):
        return {'valid_context':False, 'problems':['missing_or_modified_working_pack'], 'ready_to_execute':False}
    expected = {'ledger_hash':ledger['content_hash'],'index_hash':index['content_hash'],
                'deck_hash':deck['content_hash'],'board_hash':digest(board),'news_revision':news_revision}
    if result.get('bindings') != expected: problems.append('working_pack_bindings_changed')
    import math
    for name, stamp in [('pack',result.get('generated_at')),('board',board.get('checked_at'))]:
        if (isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp)
                or not 0 <= now-stamp <= max_age): problems.append(name+'_not_fresh')
    return {'valid_context':not problems,'problems':problems,'ready_to_execute':False,
            'scope':'Handoff integrity/freshness only; actual rendered-board verification and reasoning remain required.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.moneyball'))
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('index'); p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('checkpoint')
    p.add_argument('--authored', type=Path, required=True); p.add_argument('--board', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('pack')
    for key in ('index', 'ledger', 'board', 'output'):
        p.add_argument('--'+key, type=Path, required=True)
    p.add_argument('--news-revision', required=True); p.add_argument('--players', nargs='+', required=True)
    p.add_argument('--visible', nargs='*', default=[]); p.add_argument('--challenger', nargs='*', default=[])
    p.add_argument('--max-characters', type=int, default=42000)
    a = parser.parse_args(); deck = load_deck(a.root)
    read = lambda path: json.loads(path.read_text())
    if a.command == 'index':
        result = index_deck(deck)
    elif a.command == 'checkpoint':
        result = checkpoint(read(a.authored), deck, read(a.board))
    else:
        result = pack(read(a.index), read(a.ledger), deck, read(a.board),
                      news_revision=a.news_revision, focus_ids=a.players,
                      visible=a.visible, challenger=a.challenger, max_characters=a.max_characters)
    if a.command == 'checkpoint':
        # Preserve the complete old reasoning state even when callers keep
        # replacing one convenient latest-ledger path between turns.
        archive = a.output.parent / 'checkpoint-history' / (result['content_hash'] + '.json')
        if archive.exists() and read(archive) != result:
            raise ValueError('Immutable checkpoint archive collision')
        if not archive.exists():
            write_json(archive, result)
    write_json(a.output, result)
    print(json.dumps({'output': str(a.output.resolve()), 'command': a.command,
                      'within_budget': result.get('within_budget'), 'characters': result.get('characters'),
                      'indexed_players': len(result.get('players', {})), 'ready_to_execute': False}))


if __name__ == '__main__':
    main()
