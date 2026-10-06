"""Experimental fixed-shape fixture checks for an explicitly assigned private Sleeper mock.

No browser, API, filesystem, clock, or selection actions occur here. Callers must
call ``require_ready(verify_pick(...))`` immediately before an action, then repeat
the same room/turn/identity checks in the fresh click operation. A result is not a
reusable permission token: the UI can change after any observation.

Input UI is run-b/ui.js's observed shape: url, observed_at (timezone-aware ISO),
ownAutoPick (literal bool), header, own (team-column text), cells [{id, cls, text,
images}], candidates [{name, position, enabled}]. The collector must additionally
observe ``draft_button_present: true``; absence of a button must not count as an
enabled button. Candidate identity can use a directly observed player_id/image,
or an exact, unique name/position/team join against the supplied current catalog.
That join is explicitly labelled; it is never called an observed DOM player ID.
The catalog is [{player_id, name, position, team}], not an ADP or ranking lookup.

verify_pick keyword contract:
  allowed_mock_id: one explicitly assigned numeric private mock ID; caller verifies league_mock metadata
  expected_pick: absolute integer pick, in a 12-team snake, our slot 5
  expected_board: complete ordered prefix [{pick: 1, player_id: "..."}, ...]
  expected_own_roster: ordered IDs from previous own picks
  candidate: {player_id: "...", name: "..."}
  now: timezone-aware ISO timestamp supplied by the caller
  player_catalog: needed only for rows without observed IDs/images

Cells are not ordered by pick in the real UI. Drafted-cell player images supply
identity, while the own-column's pick/name/position text independently checks the
selected team column. Missing/ambiguous identity or an incomplete prefix blocks.
Public API freshness never supplies readiness. Optional ui.source, when present,
must say "ui". Readiness checks cannot prove a collector truthfully observed UI.

audit_run accepts append-ordered event dictionaries, including existing run-b
observation/recommendation/receipt events and explicit click_attempt/failure/
timeout/autopick/aborted events. Every observed own turn or explicitly attempted
own pick stays in the denominator. Repeated events remain counted, while unique
attempt counts are not invented when attempt_id is absent. Recommendation time
means recorded availability of nonempty exact_user_facing_text, not proof the manager
read it. Earliest active-own-turn observation is only an upper bound on onset;
a preceding earlier-pick UI observation can provide a lower bound. Clock digits
are not reverse-engineered into an exact onset or assumed rounding rule. Receipt
time is read-back time, not click time. Pre-click delivery requires a timestamped
click_attempt; a receipt alone cannot establish it or prove a manual selection.
"""

from datetime import datetime
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

# Reserved synthetic sentinel used by legacy fixture guards, never a real room.
RESERVED_FIXTURE_ID = '9000000000000000001'
REAL_DRAFT_ID = RESERVED_FIXTURE_ID  # Backward-compatible import; not a room whitelist.
TEAMS = 12
OWN_SLOT = 5
ROUNDS = 28
MAX_AGE_SECONDS = 15
TURN_SECONDS = 120
_CELL = re.compile(r'draft-cell-([1-9][0-9]*)\Z')
_CLOCK = re.compile(r'[0-9]{2}:[0-5][0-9]\Z')
_PLAYER_IMAGE = re.compile(r'/content/nfl/players/(?:thumb/)?([1-9][0-9]*)\.(?:jpg|png|webp)\Z')


class MockLabBlocked(RuntimeError):
    """Hard stop: callers must not continue with a draft action."""


def _number_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]*', value))


def _pick(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= TEAMS * ROUNDS


def snake_slot(pick):
    if not _pick(pick):
        raise ValueError('Pick must be an integer from 1 through 336')
    round_index, offset = divmod(pick - 1, TEAMS)
    return offset + 1 if round_index % 2 == 0 else TEAMS - offset


def _label(pick):
    return f'{(pick - 1) // TEAMS + 1}.{(pick - 1) % TEAMS + 1}'


def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        epoch = parsed.timestamp()
        return epoch if math.isfinite(epoch) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _room(url, allowed):
    if not _number_id(allowed) or allowed == REAL_DRAFT_ID:
        return False
    try:
        u = urlsplit(url)
        return (u.scheme == 'https' and u.netloc == 'sleeper.com'
                and u.path == '/draft/nfl/' + allowed and not u.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def _lines(value):
    return [x.strip() for x in value.splitlines() if x.strip()] if isinstance(value, str) else []


def _identity(item):
    """Return actual observed ID or reason, never a name-derived ID."""
    ids = set()
    if 'player_id' in item:
        if not _number_id(item['player_id']):
            return None, 'invalid_observed_player_id'
        ids.add(item['player_id'])
    images = item.get('images', [])
    if not isinstance(images, list):
        return None, 'invalid_images'
    for image in images:
        if not isinstance(image, str):
            return None, 'invalid_image_url'
        try:
            u = urlsplit(image)
            match = _PLAYER_IMAGE.fullmatch(u.path)
        except ValueError:
            return None, 'invalid_image_url'
        if match:
            if u.scheme != 'https' or u.netloc != 'sleepercdn.com':
                return None, 'unrecognized_player_image_origin'
            ids.add(match.group(1))
    if len(ids) > 1:
        return None, 'conflicting_player_ids'
    return (next(iter(ids)), 'observed_player_id_or_image') if ids else (None, 'missing_player_identity')


def _cells(ui):
    cells, blockers = {}, []
    if not isinstance(ui.get('cells'), list):
        return {}, ['missing_cells']
    for cell in ui['cells']:
        if not isinstance(cell, dict):
            blockers.append('invalid_cell'); continue
        match = _CELL.fullmatch(cell.get('id', '')) if isinstance(cell.get('id'), str) else None
        if not match or not _pick(int(match.group(1))):
            blockers.append('invalid_cell_id'); continue
        pick = int(match.group(1))
        if pick in cells:
            blockers.append('duplicate_cell'); continue
        classes = cell.get('cls')
        if not isinstance(classes, str):
            blockers.append('invalid_cell_class'); continue
        classes = set(classes.split())
        text = _lines(cell.get('text'))
        if not text or text[0] != _label(pick):
            blockers.append('cell_pick_label_mismatch')
        player_id, identity_status = _identity(cell)
        if 'drafted' in classes and player_id is None:
            blockers.append('drafted_' + identity_status)
        if player_id and 'drafted' not in classes:
            blockers.append('player_identity_in_undrafted_cell')
        if 'drafted' in classes and 'current-pick' in classes:
            blockers.append('current_cell_already_drafted')
        image_id, _ = _identity({'images': cell.get('images', [])})
        cells[pick] = {'player_id': player_id, 'image_player_id': image_id,
                       'classes': classes, 'lines': text}
    drafted = [v['player_id'] for v in cells.values() if 'drafted' in v['classes'] and v['player_id']]
    if len(drafted) != len(set(drafted)):
        blockers.append('duplicate_drafted_player')
    return cells, blockers


def _clock(cell):
    lines = cell['lines']
    if any(re.search(r'\bpaused\b', x, re.I) for x in lines):
        return None, 'paused'
    clocks = [x for x in lines if _CLOCK.fullmatch(x)]
    if len(clocks) != 1:
        return None, 'missing_or_ambiguous_clock'
    minutes, seconds = map(int, clocks[0].split(':'))
    value = 60 * minutes + seconds
    if not 0 < value <= TURN_SECONDS:
        return None, 'clock_not_active_within_120_seconds'
    return value, None


def _prefix(expected, pick):
    if not isinstance(expected, list) or len(expected) != pick - 1:
        return None
    pairs = []
    for index, item in enumerate(expected, 1):
        if not isinstance(item, dict) or type(item.get('pick')) is not int or item['pick'] != index or not _number_id(item.get('player_id')):
            return None
        pairs.append((index, item['player_id']))
    return pairs if len({x[1] for x in pairs}) == len(pairs) else None


def _own(ui, cells, expected):
    blockers = []
    actual = [c['player_id'] for pick, c in sorted(cells.items())
              if snake_slot(pick) == OWN_SLOT and 'drafted' in c['classes']]
    if (not isinstance(expected, list) or any(not _number_id(x) for x in expected)
            or len(set(expected)) != len(expected) or actual != expected):
        blockers.append('own_roster_id_mismatch')
    if 'own_player_ids' in ui and ui['own_player_ids'] != actual:
        blockers.append('own_explicit_ids_mismatch')
    text = _lines(ui.get('own'))
    sections = {}
    active = None
    for line in text:
        if re.fullmatch(r'[1-9][0-9]*\.[1-9][0-9]*', line):
            if line in sections:
                blockers.append('duplicate_own_pick_label')
            sections[line] = []
            active = line
        elif active:
            sections[active].append(line)
    own_picks = [p for p in range(1, TEAMS * ROUNDS + 1) if snake_slot(p) == OWN_SLOT]
    if set(sections) != {_label(p) for p in own_picks}:
        blockers.append('missing_or_wrong_own_column')
    for pick in own_picks:
        section = sections.get(_label(pick), [])
        cell = cells.get(pick)
        # Real UI versions sometimes put the countdown only in the global cell,
        # while the matching own-column cell is blank. Compare player content,
        # not duplicated controls; the global current-cell clock is mandatory.
        drafted = cell is not None and 'drafted' in cell['classes']
        wanted = cell['lines'][1:] if drafted else []
        if not drafted:
            section = [x for x in section if not _CLOCK.fullmatch(x) and x.casefold() != 'paused']
        if section != wanted:
            blockers.append('own_text_cell_mismatch'); break
    return blockers, actual


def _common(ui, allowed, now):
    blockers = []
    if not isinstance(ui, dict):
        return ['missing_ui_observation'], None
    if ui.get('source', 'ui') != 'ui':
        blockers.append('not_ui_evidence')
    if not _room(ui.get('url'), allowed):
        blockers.append('wrong_or_forbidden_room')
    observed, current = _iso(ui.get('observed_at')), _iso(now)
    if observed is None or current is None:
        blockers.append('invalid_iso_observation_time'); return blockers, None
    age = current - observed
    if age < 0:
        blockers.append('future_observation')
    elif age > MAX_AGE_SECONDS:
        blockers.append('stale_observation')
    return blockers, age


def _row_composite(row):
    name = row.get('name')
    position = _lines(row.get('position'))
    team = row.get('team', position[1] if len(position) > 1 else None)
    if not isinstance(name, str) or not name.strip() or not position or not isinstance(team, str) or not team.strip():
        return None
    if len(position) > 1 and position[1] != team:
        return None
    return name.strip(), position[0], team.strip()


def verify_pick(ui, *, allowed_mock_id, expected_pick, expected_board,
                expected_own_roster, candidate, now, player_catalog=None):
    blockers, age = _common(ui, allowed_mock_id, now)
    if not isinstance(ui, dict):
        return {'ready': False, 'blockers': blockers}
    if not _pick(expected_pick) or (_pick(expected_pick) and snake_slot(expected_pick) != OWN_SLOT):
        blockers.append('not_expected_own_snake_pick')
    if ui.get('ownAutoPick') is not False:
        blockers.append('own_autopick_not_explicitly_off')
    if any(re.search(r'\bpaused\b', x, re.I) for x in _lines(ui.get('header')) + _lines(ui.get('own'))):
        blockers.append('paused')
    cells, errors = _cells(ui); blockers.extend(errors)
    current = [p for p, c in cells.items() if 'current-pick' in c['classes']]
    seconds = None
    if current != [expected_pick]:
        blockers.append('current_pick_mismatch')
    else:
        seconds, error = _clock(cells[expected_pick])
        if error:
            blockers.append(error)
        elif age is not None and age >= seconds:
            blockers.append('observed_clock_may_have_expired')
    expected = _prefix(expected_board, expected_pick) if _pick(expected_pick) else None
    drafted = [(p, c['player_id']) for p, c in sorted(cells.items()) if 'drafted' in c['classes']]
    if expected is None:
        blockers.append('invalid_expected_full_prefix')
    elif drafted != expected:
        blockers.append('board_prefix_changed_or_incomplete')
    own_errors, actual_own = _own(ui, cells, expected_own_roster); blockers.extend(own_errors)
    identity_evidence = None
    if not isinstance(candidate, dict) or not _number_id(candidate.get('player_id')) or not isinstance(candidate.get('name'), str) or not candidate['name'].strip():
        blockers.append('invalid_candidate_identity')
    else:
        pid, name = candidate['player_id'], candidate['name'].strip()
        if pid in [x[1] for x in drafted]:
            blockers.append('candidate_already_drafted')
        rows = ui.get('candidates')
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            blockers.append('missing_candidate_rows')
            rows = []
        matching = [r for r in rows if isinstance(r.get('name'), str) and r['name'].strip() == name]
        if len(matching) != 1:
            blockers.append('missing_or_ambiguous_candidate_row')
        else:
            row = matching[0]
            if row.get('draft_button_present') is not True or row.get('enabled') is not True:
                blockers.append('candidate_button_not_observed_enabled')
            observed_id, evidence = _identity(row)
            if observed_id:
                identity_evidence = evidence
                if observed_id != pid:
                    blockers.append('candidate_identity_replaced')
            elif evidence != 'missing_player_identity':
                blockers.append('candidate_' + evidence)
            else:
                composite = _row_composite(row)
                catalog = player_catalog if isinstance(player_catalog, list) else []
                catalog_matches = [p for p in catalog if isinstance(p, dict)
                                   and _row_composite(p) == composite] if composite else []
                if not composite or len(catalog_matches) != 1:
                    blockers.append('missing_or_ambiguous_candidate_catalog_identity')
                elif catalog_matches[0].get('player_id') != pid:
                    blockers.append('candidate_catalog_id_mismatch')
                else:
                    identity_evidence = 'unique_name_position_team_join'
    fingerprint = None
    if not blockers:
        try:
            fingerprint = hashlib.sha256(json.dumps(ui, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError, OverflowError):
            blockers.append('non_json_or_nonfinite_observation')
    blockers = list(dict.fromkeys(blockers))
    return {'ready': not blockers, 'blockers': blockers, 'mock_id': allowed_mock_id,
            'pick': expected_pick, 'observed_at': ui.get('observed_at'), 'age_seconds': age,
            'visible_remaining_seconds': seconds, 'actual_own_roster': actual_own,
            'identity_evidence': identity_evidence,
            'snapshot_sha256': fingerprint,
            'action_contract': 'Hard stop on any blocker; recheck in the fresh click operation. No public API substitution.'}


def require_ready(result):
    if not isinstance(result, dict) or result.get('ready') is not True or result.get('blockers') != []:
        raise MockLabBlocked('; '.join(result.get('blockers', ['invalid verifier result']))
                             if isinstance(result, dict) else 'invalid verifier result')
    return result


def verify_shortlist(player_ids, taken_player_ids):
    """Block publishing a recommendation containing a drafted/ambiguous identity.

    ``taken_player_ids`` must come from the freshly verified full UI board prefix.
    This checks every communicated alternative, not only the proposed selection.
    It does not prove a not-yet-taken player is displayed in the current UI list.
    """
    blockers = []
    if not isinstance(player_ids, list) or not player_ids or any(not _number_id(x) for x in player_ids):
        blockers.append('missing_shortlist_identity')
    elif len(player_ids) != len(set(player_ids)):
        blockers.append('duplicate_shortlist_identity')
    if not isinstance(taken_player_ids, list) or any(not _number_id(x) for x in taken_player_ids):
        blockers.append('invalid_taken_identity_list')
    elif isinstance(player_ids, list) and any(x in taken_player_ids for x in player_ids):
        blockers.append('shortlist_contains_drafted_player')
    return {'ready': not blockers, 'blockers': blockers}


def verify_receipt(receipt, *, allowed_mock_id, expected_pick, expected_player_id,
                   expected_board, expected_own_roster, now):
    ui = receipt.get('readback') if isinstance(receipt, dict) else None
    blockers, _ = _common(ui, allowed_mock_id, now)
    if not isinstance(ui, dict):
        return {'verified': False, 'blockers': blockers, 'actual_player_id': None}
    if not _pick(expected_pick) or (_pick(expected_pick) and snake_slot(expected_pick) != OWN_SLOT):
        blockers.append('not_expected_own_snake_pick')
    if receipt.get('pick') != expected_pick or receipt.get('intended_id') != expected_player_id:
        blockers.append('receipt_intent_mismatch')
    cells, errors = _cells(ui); blockers.extend(errors)
    prefix = _prefix(expected_board, expected_pick) if _pick(expected_pick) else None
    actual_prefix = [(p, c['player_id']) for p, c in sorted(cells.items())
                     if _pick(expected_pick) and p < expected_pick and 'drafted' in c['classes']]
    if prefix is None or prefix != actual_prefix:
        blockers.append('receipt_prior_prefix_mismatch')
    cell = cells.get(expected_pick, {})
    actual = cell.get('image_player_id') if 'drafted' in cell.get('classes', set()) else None
    if not _number_id(expected_player_id) or actual != expected_player_id:
        blockers.append('receipt_actual_id_mismatch')
    wanted = list(expected_own_roster) + [expected_player_id] if isinstance(expected_own_roster, list) else None
    own_errors, _ = _own(ui, cells, wanted); blockers.extend(own_errors)
    return {'verified': not blockers, 'blockers': list(dict.fromkeys(blockers)),
            'actual_player_id': actual, 'scope': 'Exact selection read-back, not proof the selection was manual.'}


def _bounds(timestamp, first_active, preceding):
    if timestamp is None:
        return None
    return {'lower_seconds': max(0.0, timestamp - first_active) if first_active is not None else 0.0,
            'upper_seconds': max(0.0, timestamp - preceding) if preceding is not None else None,
            'interpretation': 'Elapsed from unknown true onset; null upper means unbounded from recorded evidence.'}


def audit_run(events, *, allowed_mock_id, now, target_seconds=45):
    """Audit complete event denominators and conservative latency; never take actions."""
    if not _number_id(allowed_mock_id) or allowed_mock_id == REAL_DRAFT_ID:
        raise ValueError('An explicitly allowed private mock ID is required')
    end = _iso(now)
    if end is None or isinstance(target_seconds, bool) or not isinstance(target_seconds, (int, float)) or not math.isfinite(target_seconds) or target_seconds <= 0:
        raise ValueError('Valid ISO now and positive finite target are required')
    if not isinstance(events, list):
        raise ValueError('Events must be an append-ordered list')
    timeline, by_pick, issues, run_events = [], {}, [], []
    kinds = ('recommendation', 'click_attempt', 'selection_attempt', 'receipt', 'failure', 'timeout', 'autopick', 'aborted')
    def bucket(pick):
        return by_pick.setdefault(pick, {'events': [], 'observations': [], 'completed_observations': [],
                                       'reported_automatic_events': []})
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            issues.append({'event_index': index, 'issue': 'invalid_event'}); continue
        kind = event.get('type')
        pick = event.get('pick')
        if kind == 'aborted' and pick is None:
            # An aborted run need not invent a particular attempted pick.
            # Keep the original event and separately labelled reported automatic
            # selections; image read-backs establish IDs, not how they were picked.
            run_events.append({'event_index': index, 'event': event})
            for reported in event.get('automatic_picks_observed', []):
                if isinstance(reported, dict) and _pick(reported.get('pick')) and snake_slot(reported['pick']) == OWN_SLOT:
                    bucket(reported['pick'])['reported_automatic_events'].append(
                        {'event_index': index, 'player_id': reported.get('player_id')})
                else:
                    issues.append({'event_index': index, 'issue': 'invalid_reported_automatic_pick'})
            if event.get('mock_id', allowed_mock_id) != allowed_mock_id or _iso(event.get('recorded_at')) is None:
                issues.append({'event_index': index, 'issue': 'invalid_run_abort_identity_or_time'})
            continue
        if kind in kinds:
            if not _pick(pick) or snake_slot(pick) != OWN_SLOT:
                issues.append({'event_index': index, 'issue': 'missing_or_wrong_attempt_pick'})
            else:
                bucket(pick)['events'].append((index, event))
                if event.get('mock_id', allowed_mock_id) != allowed_mock_id:
                    issues.append({'event_index': index, 'issue': 'wrong_event_room'})
        ui = event if kind == 'observation' else event.get('readback') if kind == 'receipt' else None
        if ui is None:
            continue
        if not isinstance(ui, dict) or not _room(ui.get('url'), allowed_mock_id) or ui.get('source', 'ui') != 'ui':
            issues.append({'event_index': index, 'issue': 'invalid_ui_room_or_source'}); continue
        timestamp = _iso(ui.get('observed_at'))
        if timestamp is None or timestamp > end:
            issues.append({'event_index': index, 'issue': 'invalid_or_future_ui_time'}); continue
        cells, errors = _cells(ui)
        for completed_pick, cell in cells.items():
            if snake_slot(completed_pick) == OWN_SLOT and 'drafted' in cell['classes']:
                bucket(completed_pick)['completed_observations'].append(
                    {'timestamp': timestamp, 'player_id': cell['image_player_id']})
        if errors:
            issues.append({'event_index': index, 'issue': 'ui_cell_validation_errors', 'blockers': errors})
        current = [p for p, c in cells.items() if 'current-pick' in c['classes']]
        if len(current) != 1:
            continue
        active_pick = current[0]
        remaining, error = _clock(cells[active_pick])
        paused = error == 'paused' or any(re.search(r'\bpaused\b', x, re.I)
                                         for x in _lines(ui.get('header')) + _lines(ui.get('own')))
        observed = {'timestamp': timestamp, 'pick': active_pick, 'active': remaining is not None and not paused,
                    'paused': paused, 'event_index': index, 'auto_pick': ui.get('ownAutoPick') is True}
        timeline.append(observed)
        if snake_slot(active_pick) == OWN_SLOT:
            bucket(active_pick)['observations'].append(observed)
    timeline.sort(key=lambda x: (x['timestamp'], x['event_index']))
    regressed = any(b['pick'] < a['pick'] for a, b in zip(timeline, timeline[1:]))
    if regressed:
        issues.append({'issue': 'ui_pick_regression_onset_bounds_not_trusted'})
    results = []
    for pick, data in sorted(by_pick.items()):
        observations = data['observations']
        active = [x['timestamp'] for x in observations if x['active']]
        first = min(active) if active else None
        first_seen = min((x['timestamp'] for x in observations), default=None)
        previous = [x['timestamp'] for x in timeline if first_seen is not None
                    and x['timestamp'] < first_seen and x['pick'] < pick]
        preceding = max(previous) if previous and not regressed else None
        if regressed:
            first = None
        counts, recommendations, clicks, receipts = {}, [], [], []
        explicit_attempt_ids = set()
        for index, event in data['events']:
            kind = event['type']; counts[kind] = counts.get(kind, 0) + 1
            if isinstance(event.get('attempt_id'), str) and event['attempt_id']:
                explicit_attempt_ids.add(event['attempt_id'])
            timestamp = _iso(event.get('recorded_at'))
            valid_time = timestamp is not None and timestamp <= end
            if not valid_time and kind != 'receipt':
                issues.append({'event_index': index, 'issue': 'invalid_or_future_event_time'})
            if event.get('mock_id', allowed_mock_id) != allowed_mock_id:
                continue
            if kind == 'recommendation' and valid_time and isinstance(event.get('exact_user_facing_text'), str) and event['exact_user_facing_text'].strip():
                recommendations.append(timestamp)
            elif kind in ('click_attempt', 'selection_attempt') and valid_time:
                clicks.append(timestamp)
            elif kind == 'receipt':
                ui = event.get('readback')
                actual, valid = None, False
                if isinstance(ui, dict) and _room(ui.get('url'), allowed_mock_id) and ui.get('source', 'ui') == 'ui':
                    receipt_time = _iso(ui.get('observed_at'))
                    cells, errors = _cells(ui)
                    cell = cells.get(pick, {})
                    actual = cell.get('image_player_id') if 'drafted' in cell.get('classes', set()) else None
                    valid = (not errors and receipt_time is not None and receipt_time <= end
                             and _number_id(event.get('intended_id')) and actual == event['intended_id'])
                receipts.append({'event_index': index, 'actual_player_id': actual,
                                 'intended_id': event.get('intended_id'), 'actual_id_matches': valid})
        recommended = min(recommendations) if recommendations else None
        bounds = _bounds(recommended, first, preceding)
        status = 'missing_recommendation'
        if bounds:
            status = ('definitely_late' if bounds['lower_seconds'] > target_seconds else
                      'guaranteed_within_target' if bounds['upper_seconds'] is not None and bounds['upper_seconds'] <= target_seconds else
                      'unresolved_onset')
        delivered_before_click = recommended is not None and bool(clicks) and recommended <= min(clicks)
        results.append({'pick': pick, 'event_counts': counts, 'observed_own_turn': bool(observations),
                        'observed_completed_own_pick': bool(data['completed_observations']),
                        'explicit_attempt_ids_count': len(explicit_attempt_ids),
                        'distinct_actual_attempt_count': None if not explicit_attempt_ids else len(explicit_attempt_ids),
                        'earliest_active_observed_epoch': first, 'onset_lower_epoch': preceding,
                        'recommendation_recorded_epoch': recommended, 'recommendation_latency_bounds': bounds,
                        'timing_status': status, 'pre_click_delivery_verified': delivered_before_click,
                        'click_latency_bounds': _bounds(min(clicks), first, preceding) if clicks else None,
                        'paused_observation_count': sum(x['paused'] for x in observations),
                        'own_autopick_observation_count': sum(x['auto_pick'] for x in observations),
                        'reported_automatic_events': data['reported_automatic_events'],
                        'receipts': receipts,
                        'recorded_failure_count': sum(counts.get(x, 0) for x in ('failure', 'timeout', 'autopick', 'aborted'))})
    statuses = {name: sum(x['timing_status'] == name for x in results)
                for name in ('missing_recommendation', 'definitely_late', 'guaranteed_within_target', 'unresolved_onset')}
    return {'mock_id': allowed_mock_id, 'event_count': len(events), 'evaluated_pick_count': len(results),
            'denominator': 'Union of every recorded own-turn exposure, completed own pick visible on the board and explicit own-pick attempt, including failures and absent recommendations.',
            'timing_counts': statuses, 'picks': results, 'issues': issues,
            'run_events': run_events,
            'interpretation': 'UI observation and recorded-message timing audit; not exact onset, manual-click proof, decision quality, or championship probability.'}
