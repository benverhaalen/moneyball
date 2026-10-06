"""Fast dossier comparisons and explicit, revocable draft instructions.

No player utility is inferred from ADP, points, age or missing future data.
The compiled deck is a retrieval cache. Prepared choices are human/LLM judgment,
with required reasons and dependencies; validation is not proof of good strategy.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import time

from .dossier_reader import decision_brief
from .player_research import verified_packet
from .review_audit import audit, digest as file_digest
from .store import Store, digest


POLICY = {
    'version': 1,
    'objective': 'Improve feasible championship opportunities now and in subsequent seasons.',
    'rules': [
        'Compare actual roster continuations, not isolated player points or career totals.',
        'ADP is an acquisition timing reference; its buffer is not a probability interval.',
        'Missing future forecasts are unknown, not zero; no automatic age bonus or penalty.',
        'State current contribution, future pathway and the strongest competing choice.',
        'A vacated role must be earned; a future trade requires a willing counterparty.',
        'Price ordinary roster capacity, taxi restrictions and overlapping failure paths.',
        'Do not count information twice when professional forecasts already include it.',
        'Use title percentages only with visible model assumptions and calibration status.',
    ],
}
REASONS = ('current_roster_effect', 'future_pathway', 'continuation',
           'risk_and_capacity', 'acquisition_timing', 'why_over_alternative', 'reversal_trigger')


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.staged')
    temp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    temp.replace(path)


def excerpts(markdown):
    """Verbatim navigation excerpts, never generated claims or new summaries."""
    blocks = [p.strip() for p in re.split(r'\n\s*\n', markdown)
              if len(p.split()) >= 25 and not p.lstrip().startswith(('#', '|'))]
    output = {}
    patterns = {'future': r'2027|2028|succession|future|contract',
                'risk': r'injur|risk|correlat|capacity|uncertain',
                'reversal': r'falsif|revers|invalidate'}
    if blocks:
        output['opening'] = blocks[0]
    for label, pattern in patterns.items():
        found = next((p for p in blocks if re.search(pattern, p, re.I)), None)
        if found:
            output[label] = found
    return output


def build(root):
    root = Path(root).resolve()
    directory = root / 'draft/player-packets'
    index = json.loads((directory / 'index.json').read_text())
    completed = {p['player_id']: p for p in audit(root)['players']}
    cards = {}
    for entry in index['players']:
        packet = verified_packet(directory, entry)
        brief = decision_brief(packet)
        pid = entry['player_id']
        review = completed.get(pid)
        md = Path(review['review_path']).read_text() if review else ''
        cards[pid] = {
            'player_id': pid, 'name': entry['name'], 'position': entry['position'],
            'identity': packet['identity'], 'packet_path': str(directory / entry['path']),
            'packet_sha256': entry['sha256'], 'information_cutoff': packet['information_cutoff'],
            'forecasts': brief['professional_forecasts'],
            'markets': brief['market_component_ladders'],
            'contracts': brief['contract_context'], 'coverage': packet['coverage'],
            'review': review, 'review_excerpts': excerpts(md),
            'full_review': md, 'calibrated_title_delta': None,
        }
    deck = {'schema_version': 1, 'built_at': time.time(), 'packet_build': index['build_id'],
            'policy': POLICY, 'cards': cards}
    deck['content_hash'] = digest({'packet_build': index['build_id'], 'policy': POLICY, 'cards': cards})
    path = root / 'draft/room/deck.json'
    write_json(path, deck)
    return {'deck': str(path), 'players': len(cards), 'completed_dossiers': len(completed),
            'content_hash': deck['content_hash'], 'calibrated_title_model': False}


def board_state(data, *, checked_at=None):
    league = data['league']
    drafts = [d for d in data['drafts'] if d['info']['status'] != 'complete']
    if len(drafts) != 1:
        raise ValueError('Need exactly one unfinished draft')
    draft = drafts[0]; info = draft['info']; settings = info['settings']
    if info['type'] != 'snake' or settings.get('reversal_round', 0) or draft.get('traded_picks'):
        raise ValueError('This adapter requires a snake draft without reversal or traded picks')
    teams = int(settings['teams']); rounds = int(settings['rounds'])
    if teams < 2 or rounds < 1:
        raise ValueError('Draft needs at least two teams and one round')
    slots = [int(s) for s, r in info['slot_to_roster_id'].items() if r == data['my_roster_id']]
    if len(slots) != 1:
        raise ValueError('Cannot verify own draft slot')
    own_slot = slots[0]
    if not 1 <= own_slot <= teams:
        raise ValueError('Own draft slot outside configured team count')
    owner = lambda n: ((n-1) % teams + 1 if (n-1)//teams % 2 == 0 else teams-(n-1) % teams)
    picks = sorted(draft['picks'], key=lambda p: int(p['pick_no']))
    if [int(p['pick_no']) for p in picks] != list(range(1, len(picks)+1)):
        raise ValueError('Picks must be a contiguous unique prefix')
    if len(picks) > teams * rounds:
        raise ValueError('More picks than draft capacity')
    taken = [str(p['player_id']) for p in picks]
    if len(set(taken)) != len(taken):
        raise ValueError('Duplicate drafted player')
    squads = {str(s): [] for s in range(1, teams+1)}
    for p in picks:
        slot = owner(int(p['pick_no']))
        if int(p.get('draft_slot', slot)) != slot:
            raise ValueError('Observed ownership differs from supported snake order')
        squads[str(slot)].append(str(p['player_id']))
    turns = [n for n in range(len(picks)+1, teams*rounds+1) if owner(n) == own_slot]
    return {'draft_id': info['draft_id'], 'draft_status': info['status'],
            'own_slot': own_slot, 'picks_made': len(picks), 'taken': taken, 'squads': squads,
            'own_roster': squads[str(own_slot)], 'next_own_pick': turns[0] if turns else None,
            'following_own_pick': turns[1] if len(turns)>1 else None,
            'on_clock': bool(turns and turns[0] == len(picks)+1 and info['status'] == 'drafting'),
            'checked_at': checked_at,
            'rules_hash': digest({'league': {k: league.get(k) for k in ('scoring_settings', 'roster_positions', 'settings')},
                                  'draft_settings': settings, 'slots': info['slot_to_roster_id']}),
            'prefix_hash': digest(picks), 'starters': [p for p in league['roster_positions'] if p != 'BN']}


def load_board(root, alias):
    store = Store(root); snapshot = store.latest(store.resolve_alias(alias))
    if not snapshot:
        raise ValueError('No league snapshot; run context --fresh')
    data = snapshot['data']; run = store.last_run(alias) or {}
    # A failed refresh must not inherit the freshness of an unrelated endpoint.
    checked = None
    if run.get('ok') and run.get('snapshot_hash') == snapshot['hash']:
        drafts = [d for d in data['drafts'] if d['info']['status'] != 'complete']
        if len(drafts) == 1:
            source = run.get('sources', {}).get('draft/' + drafts[0]['info']['draft_id'] + '/picks', {})
            checked = source.get('fetched_at')
    return board_state(data, checked_at=checked)


def load_deck(root):
    deck = json.loads((Path(root) / 'draft/room/deck.json').read_text())
    expected = digest({k: deck[k] for k in ('packet_build', 'policy', 'cards')})
    if deck['content_hash'] != expected:
        raise ValueError('Compiled deck content hash mismatch; rebuild')
    return deck


def verify_current_references(root, deck, ids):
    """Check requested cards against current publications without rereading 400 dossiers."""
    root = Path(root).resolve(); ids = set(map(str, ids))
    index = json.loads((root / 'draft/player-packets/index.json').read_text())
    entries = {p['player_id']: p for p in index['players']}
    manifests = {}
    for path in (root / 'research/player-reviews').glob('batch*.json'):
        doc = json.loads(path.read_text())
        if not isinstance(doc, dict): continue
        rows = doc.get('reviews', doc.get('players', []))
        if not isinstance(rows, list): continue
        for row in rows:
            if isinstance(row, dict) and row.get('player_id') in ids and row.get('review_sha256'):
                pid = row['player_id']
                if pid in manifests: raise ValueError('Duplicate published review: ' + pid)
                manifests[pid] = row
    for pid in ids:
        card = deck['cards'][pid]; entry = entries.get(pid)
        if not entry or entry['sha256'] != card['packet_sha256']:
            raise ValueError('Packet index changed; rebuild deck: ' + pid)
        if file_digest(Path(card['packet_path'])) != card['packet_sha256']:
            raise ValueError('Packet changed on disk: ' + pid)
        published = manifests.get(pid); compiled = card['review']
        if bool(published) != bool(compiled):
            raise ValueError('Review publication changed; rebuild deck: ' + pid)
        if compiled:
            if published['review_sha256'] != compiled['review_sha256']:
                raise ValueError('Review revised; rebuild deck: ' + pid)
            if file_digest(Path(compiled['review_path'])) != compiled['review_sha256']:
                raise ValueError('Review changed on disk: ' + pid)
            if file_digest(Path(compiled['evidence_path'])) != compiled['evidence_sha256']:
                raise ValueError('Review evidence changed; rebuild deck: ' + pid)


def compare(deck, board, ids, *, buffer=4, full=False):
    ids = list(dict.fromkeys(map(str, ids)))
    if not 1 <= len(ids) <= 24:
        raise ValueError('Scan one to twenty-four explicit candidates')
    if full and len(ids) > 8:
        raise ValueError('Full dossier comparison caps at eight; scan first, then narrow')
    if not isinstance(buffer, int) or buffer < 0:
        raise ValueError('ADP buffer must be a nonnegative integer')
    rows = []
    for pid in ids:
        if pid in board['taken'] or pid not in deck['cards']:
            raise ValueError('Selected or uncovered candidate: ' + pid)
        c = deck['cards'][pid]; ident = c['identity']; adp = ident.get('adp')
        # A precise subtotal must never outlive the missingness/exposure that
        # qualifies it when a compact reading view is handed to a new worker.
        forecasts = [{**{k: f.get(k) for k in ('source_key', 'provider', 'observed_core_subtotal',
                      'source_conditioning', 'known_at', 'unreported_scoring_fields', 'source_url')},
                      'reported_exposure': {k: f.get('stats', {})[k] for k in ('games', 'gp')
                                            if k in f.get('stats', {})},
                      'component_vector_location': c['packet_path'],
                      'is_complete_league_projection': False} for f in c['forecasts']]
        rows.append({'player_id': pid, 'name': c['name'], 'position': c['position'],
                     'nfl_team': ident.get('team'), 'years_exp': ident.get('years_exp'),
                     'acquisition_only': {'adp': adp, 'type': ident.get('adp_type'),
                         'as_of': ident.get('adp_as_of'), 'rough_range': None if adp is None else [max(1, adp-buffer), adp+buffer],
                         'not_a_survival_probability': True},
                     'forecast_intermediates': forecasts, 'review': c['review'],
                     'verbatim_navigation_excerpts': c['review_excerpts'] if len(ids) <= 8 and not full else None,
                     'full_review': c['full_review'] if full else None,
                     'packet_path': c['packet_path'], 'calibrated_title_delta': None})
    positions = Counter(deck['cards'][p]['position'] if p in deck['cards'] else 'UNKNOWN' for p in board['own_roster'])
    return {'board': board, 'policy': deck['policy'], 'deck_hash': deck['content_hash'],
            'current_roster_position_counts': dict(positions), 'candidates': rows,
            'required_pick_reasons': list(REASONS), 'recommendation': None,
            'scope': 'Retrieval and decision discipline. Position counts do not certify legal lineup coverage. '
                     'Core forecast totals may differ in exposure, missing fields and Week18 coverage. '
                     'Excerpts are verbatim navigation aids; read full case for qualifications. '
                     'No player ranking or championship probability is inferred.'}


def prepare(plan, deck, board):
    """Bind an ordered judgment to inputs; alternatives are conditional, not scores."""
    if plan.get('target_pick') != board['next_own_pick'] or board['next_own_pick'] is None:
        raise ValueError('Plan must target the next own pick')
    choices = plan.get('choices')
    if not isinstance(choices, list) or not choices:
        raise ValueError('Need at least one prepared choice')
    seen = set(); referenced = set()
    for choice in choices:
        pid = str(choice['player_id'])
        if pid in seen or pid in board['taken'] or pid not in deck['cards']:
            raise ValueError('Invalid prepared candidate: ' + pid)
        seen.add(pid); referenced.add(pid)
        alternative = str(choice.get('alternative_id', ''))
        if alternative == pid or alternative not in deck['cards'] or alternative in board['taken']:
            raise ValueError('Need a distinct available competing candidate')
        referenced.add(alternative)
        for reason in REASONS:
            if not isinstance(choice.get(reason), str) or len(choice[reason].split()) < 5:
                raise ValueError('Missing substantive reason: ' + reason)
        dependencies = choice.get('requires_available')
        if not isinstance(dependencies, list):
            raise ValueError('Declare requires_available, including an empty list if none')
        for dep in dependencies:
            if str(dep) not in deck['cards'] or str(dep) in board['taken']:
                raise ValueError('Unavailable continuation dependency: ' + str(dep))
            referenced.add(str(dep))
    result = copy.deepcopy({**plan, 'prepared_at': time.time(), 'draft_id': board['draft_id'],
            'own_roster': board['own_roster'], 'rules_hash': board['rules_hash'],
            'policy_hash': digest(deck['policy']), 'prepared_prefix': board['prefix_hash'],
            'prepared_taken_prefix': board['taken'],
            'card_hashes': {p: digest(deck['cards'][p]) for p in referenced},
            'epistemic_status': 'Prepared analyst judgment, not model-estimated optimality.'})
    result['prepared_payload_hash'] = digest(result)
    return result


def check(plan, deck, board, *, now=None, max_age=15):
    now = time.time() if now is None else now
    problems = []
    if plan.get('prepared_payload_hash') != digest({k: v for k, v in plan.items() if k != 'prepared_payload_hash'}):
        return {'ready_to_execute': False, 'problems': ['prepared_plan_modified'],
                'first_viable_prepared_choice': None, 'writes_to_sleeper': False}
    prior = plan.get('prepared_taken_prefix')
    if not isinstance(prior, list) or board['taken'][:len(prior)] != prior:
        problems.append('draft_prefix_revised')
    for key in ('draft_id', 'own_roster', 'rules_hash'):
        if plan.get(key) != board.get(key): problems.append(key + '_changed')
    if plan.get('policy_hash') != digest(deck['policy']): problems.append('strategy_changed')
    if plan.get('target_pick') != board['next_own_pick']: problems.append('target_pick_changed')
    if board['checked_at'] is None or not 0 <= now-board['checked_at'] <= max_age:
        problems.append('board_not_fresh')
    for pid, h in plan.get('card_hashes', {}).items():
        if pid not in deck['cards'] or digest(deck['cards'][pid]) != h:
            problems.append('evidence_changed:' + pid)
    viable = [c for c in plan['choices'] if str(c['player_id']) not in board['taken']
              and not any(str(p) in board['taken'] for p in c['requires_available'])]
    if not viable: problems.append('all_prepared_choices_or_dependencies_gone')
    return {'ready_to_execute': not problems and board['on_clock'], 'problems': problems,
            'first_viable_prepared_choice': viable[0] if viable and not problems else None,
            'on_clock': board['on_clock'], 'checked_prefix': board['prefix_hash'],
            'writes_to_sleeper': False,
            'scope': 'Checks declared dependencies and freshness. Unmodeled news and strategic interactions still require judgment.'}


def live_check(plan, deck, board, ui_observation=None, *, now=None, max_age=15):
    """Require a current rendered-board observation in addition to API receipt time.

    A newly fetched Sleeper response can still contain an old cached pick prefix.
    The caller must supply actual observed UI values, never infer them from that
    same response. This validates consistency, not the origin of caller assertions.
    """
    now = time.time() if now is None else now
    result = check(plan, deck, board, now=now, max_age=max_age)
    problems = result['problems']
    ui = ui_observation or {}
    if ui.get('source') != 'sleeper_rendered_ui':
        problems.append('rendered_board_confirmation_required')
    else:
        observed = ui.get('observed_at')
        if (not isinstance(observed, (int, float)) or isinstance(observed, bool)
                or not 0 <= now-observed <= max_age):
            problems.append('rendered_board_not_fresh')
        for key, expected in (
            ('draft_id', board['draft_id']), ('taken_player_ids', board['taken']),
            ('own_roster', board['own_roster']),
            ('current_pick', board['picks_made'] + 1),
        ):
            if ui.get(key) != expected:
                problems.append('rendered_board_mismatch:' + key)
        if ui.get('on_clock') is not True:
            problems.append('rendered_board_not_our_turn')
        if ui.get('auto_pick') is not False:
            problems.append('manual_pick_mode_not_confirmed')
    if problems:
        result['ready_to_execute'] = False
        result['first_viable_prepared_choice'] = None
    result['actionable_freshness_verified'] = not problems and result.get('on_clock', False)
    result['scope'] = ('Prepared judgment and agreement with caller-supplied current rendered UI. '
                       'API fetch time alone is insufficient. Actual selection remains user-controlled.')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='.moneyball'); p.add_argument('--alias', default=None)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('build')
    c = sub.add_parser('compare'); c.add_argument('ids', nargs='+'); c.add_argument('--full', action='store_true'); c.add_argument('--buffer', type=int, default=4)
    for command in ('prepare', 'check'):
        q = sub.add_parser(command); q.add_argument('plan', type=Path)
        if command == 'check':
            q.add_argument('--ui-observation', type=Path,
                           help='Fresh rendered Sleeper board receipt; API-only checks cannot authorize a pick')
    args = p.parse_args(); started = time.perf_counter()
    try:
        if args.command == 'build': result = build(args.root)
        else:
            deck = load_deck(args.root); board = load_board(args.root, args.alias)
            if args.command == 'compare':
                verify_current_references(args.root, deck, args.ids)
                result = compare(deck, board, args.ids, buffer=args.buffer, full=args.full)
            else:
                plan = json.loads(args.plan.read_text())
                if args.command == 'prepare':
                    result = prepare(plan, deck, board)
                    verify_current_references(args.root, deck, result['card_hashes'])
                    out = Path(args.root) / 'draft/room/plans' / (str(time.time_ns()) + '.json')
                    write_json(out, result); result = {'saved_plan': str(out), 'plan': result}
                else:
                    verify_current_references(args.root, deck, plan.get('card_hashes', {}))
                    ui = json.loads(args.ui_observation.read_text()) if args.ui_observation else None
                    result = live_check(plan, deck, board, ui)
        result['elapsed_ms'] = round((time.perf_counter()-started)*1000, 3)
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, json.dumps({'error': str(exc)}) + '\n')


if __name__ == '__main__':
    main()
