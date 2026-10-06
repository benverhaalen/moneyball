"""Durable read-only DOM observer and preauthored draft advice publisher.

The live transport connects only to an explicitly supplied existing localhost
CDP page. It never invokes agent-browser's auto-launching command dispatcher.
Only the fixed DOM collector is evaluated; no browser/navigation/selection API
exists here. Offline fixture mode and all decision/storage code use stdlib.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import draft_fastlane, draft_room, mock_lab
from .store import digest


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def validate_setup(setup):
    d = setup['draft']; s = d['settings']
    if (setup.get('schema_version') != 1 or setup.get('headless') is not True
            or not re.fullmatch(r'[A-Za-z0-9_-]+', setup.get('session_name', ''))
            or not re.fullmatch(r'[1-9][0-9]*', d.get('draft_id', ''))):
        raise ValueError('Explicit named headless session and numeric draft ID required')
    if (setup.get('own_slot') != 5 or s.get('teams') != 12 or s.get('rounds') != 28
            or s.get('pick_timer') != 120 or s.get('reversal_round', 0)
            or d.get('type') != 'snake' or setup.get('traded_picks')):
        raise ValueError('Experimental observer supports only slot5 / 12-team / 28-round / 120-second snake fixture')
    if setup.get('mode') == 'private_mock':
        if d['draft_id'] == mock_lab.REAL_DRAFT_ID or d.get('league_id') is not None or d.get('metadata', {}).get('type') != 'league_mock':
            raise ValueError('Private mock metadata does not match assigned draft')
    elif setup.get('mode') == 'real_advisory':
        binding = setup.get('room_binding') or {}
        own_roster = d.get('slot_to_roster_id', {}).get(str(setup.get('own_slot')))
        if (binding.get('draft_id') != d['draft_id'] or not d.get('league_id')
                or str(binding.get('league_id')) != str(d['league_id'])
                or own_roster is None or binding.get('own_roster_id') != own_roster
                or mock_lab._iso(binding.get('observed_at')) is None):
            raise ValueError('Real advisory requires explicit observed draft/league/own-roster room_binding')
    else:
        raise ValueError('Mode must be private_mock or real_advisory')
    if mock_lab._iso(setup.get('setup_observed_at')) is None:
        raise ValueError('Setup receipt needs timezone-aware setup_observed_at')
    ttl = setup.get('registry_max_age_seconds', 300)
    if isinstance(ttl, bool) or not isinstance(ttl, (int,float)) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError('Positive finite registry freshness-check TTL required')
    if '5' not in d['slot_to_roster_id'] or not isinstance(setup.get('league'), dict):
        raise ValueError('Exact slot-to-roster mapping and league rules required')


def collector_script(setup, watched_names):
    """Fixed reads adapted from the actually observed A2/B2 legacy Sleeper DOM."""
    validate_setup(setup)
    config = json.dumps({'id': setup['draft']['draft_id'], 'watched': watched_names})
    return r'''JSON.stringify((()=>{
 const cfg=CONFIG;
 if(location.origin!=='https://sleeper.com'||location.pathname!=='/draft/nfl/'+cfg.id||location.hash)throw Error('wrong assigned room');
 const identify=e=>{const attr=e.getAttribute('data-player-id')||e.getAttribute('player-id');if(attr)return attr;for(const i of e.querySelectorAll('img')){const m=i.src.match(/^https:\/\/sleepercdn\.com\/content\/nfl\/players\/(?:thumb\/)?([1-9][0-9]*)\.(?:jpg|png|webp)$/);if(m)return m[1];}return null;};
 const cells=[...document.querySelectorAll('.cell')].filter(e=>e.querySelector('.player')||e.className!=='cell false').map(e=>({id:e.id,cls:e.className,text:e.innerText,player_id:identify(e),images:[...e.querySelectorAll('img')].map(i=>i.src)}));
 for(const c of cells)if(c.player_id===null)delete c.player_id;
 const col=[...document.querySelectorAll('.team-column')][4];
 const rows=[...document.querySelectorAll('.player-rank-item2')].filter(e=>e.getClientRects().length).map((e,index)=>{const b=e.querySelector('.draft-button');const r={row_index:index,name:e.querySelector('.name-wrapper')?.childNodes[0]?.textContent,position:e.querySelector('.position')?.innerText.split(/\s+/)[0],team:e.querySelector('.team')?.innerText,draft_button_present:!!b,enabled:!!b&&!b.classList.contains('disable')&&!b.hasAttribute('disabled')&&b.getAttribute('aria-disabled')!=='true'};const pid=identify(e);if(pid)r.player_id=pid;return r;});
 const key=r=>JSON.stringify([r.name?.trim(),r.position?.trim(),r.team?.trim()]);const counts={};for(const r of rows)counts[key(r)]=(counts[key(r)]||0)+1;
 const selected=rows.filter((r,i)=>i<24||cfg.watched.includes(r.name?.trim())).map(r=>({...r,composite_row_count:counts[key(r)]}));
 return {source:'ui',url:location.href,observed_at:new Date().toISOString(),ownAutoPick:document.querySelector('.autopick-toggle input')?.checked,header:document.querySelector('.draft-header')?.innerText,own:col?.innerText,own_player_ids:col?[...col.querySelectorAll('.cell')].map(identify).filter(Boolean):[],cells,clock:document.querySelector('.timer-text')?.innerText,candidates:selected,slate_capture:{top_limit:24,rendered_count:rows.length,captured_top_count:Math.min(rows.length,24),complete_top_prefix:true}};
})())'''.replace('CONFIG', config)


def capture_existing_page(payload, *, connect=None):
    """Only Runtime.evaluate on one existing page; no discovery/creation fallback.

    Called in a disposable helper process with a hard parent timeout. websocket-
    client (already installed locally) handles the wire protocol, not a browser.
    """
    setup = payload['setup']; validate_setup(setup)
    u = urlsplit(setup.get('page_websocket_url', ''))
    if (u.scheme != 'ws' or u.hostname not in ('127.0.0.1', 'localhost')
            or not u.port or u.username or u.password or u.query or u.fragment
            or re.fullmatch(r'/devtools/page/[A-Za-z0-9_-]+', u.path) is None):
        raise ValueError('An explicit existing localhost CDP page endpoint is required')
    if connect is None:
        from websocket import create_connection
        connect = create_connection
    deadline = time.monotonic() + 4
    ws = connect(setup['page_websocket_url'], timeout=3, suppress_origin=True,
                 http_no_proxy=['localhost', '127.0.0.1'])
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
            'expression': collector_script(setup, payload['watched_names']),
            'returnByValue': True, 'silent': True, 'userGesture': False,
            'timeout': 3000, 'allowUnsafeEvalBlockedByCSP': False}}))
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError('DOM capture exceeded deadline')
            ws.settimeout(remaining); message = ws.recv()
            if len(message) > 2_000_000: raise ValueError('Oversized DOM response')
            response = json.loads(message)
            if response.get('id') != 1: continue
            if response.get('error') or response.get('result', {}).get('exceptionDetails'):
                raise ValueError('Existing page rejected fixed DOM collector')
            raw = response['result']['result']['value']
            frame = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(frame, dict): raise ValueError('DOM capture did not return a frame')
            return frame
    finally:
        # Closing this debugging socket does not close the page/browser/session.
        ws.close()


class ExistingPageCapture:
    def __call__(self, setup, watched_names):
        result = subprocess.run([sys.executable, '-m', 'moneyball.draft_advisor_watch', '_capture'],
            input=json.dumps({'setup': setup, 'watched_names': watched_names}),
            text=True, capture_output=True, timeout=4.5)
        if result.returncode: raise RuntimeError('Read-only capture helper failed')
        if len(result.stdout) > 2_000_000: raise ValueError('Oversized captured frame')
        return json.loads(result.stdout)


def normalize(frame, setup, catalog, *, now):
    """Parse actual cells with existing guards; never use an API fetch as freshness."""
    validate_setup(setup); draft_fastlane._time(now); errors = []
    if not isinstance(frame, dict): raise ValueError('Missing raw frame')
    json.dumps(frame, allow_nan=False)
    d = setup['draft']; u = urlsplit(frame.get('url', ''))
    if u.scheme != 'https' or u.netloc != 'sleeper.com' or u.path != '/draft/nfl/'+d['draft_id'] or u.fragment:
        errors.append('wrong_assigned_room')
    observed = mock_lab._iso(frame.get('observed_at'))
    age = None if observed is None else now-observed
    if observed is None or age < 0 or age > 15: errors.append('frame_not_fresh')
    if frame.get('source') != 'ui': errors.append('not_observed_ui')
    cells, issues = mock_lab._cells(frame); errors.extend(issues)
    drafted = [(p, c['player_id']) for p, c in sorted(cells.items()) if 'drafted' in c['classes']]
    if [p for p, _ in drafted] != list(range(1, len(drafted)+1)):
        errors.append('drafted_prefix_incomplete')
    own = [pid for p, pid in drafted if mock_lab.snake_slot(p) == setup['own_slot']]
    issues, _ = mock_lab._own(frame, cells, own); errors.extend(issues)
    current = [p for p, c in cells.items() if 'current-pick' in c['classes']]
    seconds = None; state = 'complete' if len(drafted) == 336 else 'unknown'
    if len(drafted) < 336:
        if current != [len(drafted)+1]: errors.append('current_cell_mismatch')
        else:
            seconds, error = mock_lab._clock(cells[current[0]])
            if error: errors.append(error)
            state = 'paused' if error == 'paused' else 'drafting' if not error else 'unknown'
    if re.search(r'\bpaused\b', str(frame.get('clock', ''))+'\n'+str(frame.get('header', '')), re.I):
        state = 'paused'; errors.append('paused')
    if seconds is not None and age is not None and age >= seconds:
        errors.append('observed_clock_may_have_expired')
    if frame.get('ownAutoPick') is not False: errors.append('own_autopick_not_explicitly_off')
    if not re.search(r'\b12 Teams\b', str(frame.get('header', ''))) or not re.search(r'\b28 Rounds\b', str(frame.get('header', ''))):
        errors.append('visible_settings_mismatch')
    normalized = None
    if not any(e in errors for e in ('drafted_prefix_incomplete', 'invalid_cell_id', 'duplicate_cell', 'duplicate_drafted_player')) and all(pid for _, pid in drafted):
        info = copy.deepcopy(d); info['status'] = state
        data = {'league': setup['league'], 'my_roster_id': d['slot_to_roster_id']['5'],
                'drafts': [{'info': info, 'traded_picks': [], 'picks': [
                    {'pick_no': p, 'player_id': pid, 'draft_slot': mock_lab.snake_slot(p)} for p, pid in drafted]}]}
        # board_state excludes complete drafts; retain complete state explicitly.
        if state == 'complete': info['status'] = 'paused'
        try:
            normalized = draft_room.board_state(data, checked_at=observed)
            if state == 'complete': normalized.update(draft_status='complete', on_clock=False)
        except (ValueError, KeyError, TypeError) as exc: errors.append('invalid_board:'+str(exc))
    identities, slate = [], []
    rows = frame.get('candidates', [])
    capture = frame.get('slate_capture', {})
    top_count = capture.get('captured_top_count')
    if (capture.get('complete_top_prefix') is not True or capture.get('top_limit') != 24
            or type(capture.get('rendered_count')) is not int or capture['rendered_count'] < 0
            or top_count != min(capture['rendered_count'], 24)):
        errors.append('complete_top_slate_not_observed')
    if not isinstance(rows, list): rows = []; errors.append('missing_candidate_rows')
    indexes = [r.get('row_index') for r in rows if isinstance(r, dict)]
    if len(indexes) != len(rows) or len(set(indexes)) != len(indexes) or (isinstance(top_count, int) and not set(range(top_count)) <= set(indexes)):
        errors.append('top_slate_rows_missing_or_duplicate')
    for row in rows:
        pid, method = mock_lab._identity(row)
        composite = mock_lab._row_composite(row)
        matches = [c for c in catalog if mock_lab._row_composite(c) == composite] if composite else []
        if pid is None and method == 'missing_player_identity' and len(matches) == 1:
            pid = matches[0].get('player_id'); method = 'unique_name_position_team_join'
        if (not mock_lab._number_id(pid) or type(row.get('composite_row_count')) is not int
                or row['composite_row_count'] != 1):
            errors.append('candidate_identity_needs_review'); pid = None
        identities.append({'row_index': row.get('row_index'), 'name': row.get('name'), 'player_id': pid,
                           'identity_evidence': method if pid else 'unresolved',
                           'button_enabled': row.get('draft_button_present') is True and row.get('enabled') is True})
        if pid: slate.append(pid)
    if len(slate) != len(set(slate)): errors.append('duplicate_slate_identity')
    return {'board': normalized, 'state': state, 'current_pick': current[0] if len(current) == 1 else None,
            'seconds_remaining': seconds, 'observed_at': observed, 'frame_age_seconds': age,
            'own_roster': own, 'prefix': [{'pick': p, 'player_id': pid} for p, pid in drafted],
            'candidate_identities': identities, 'slate_ids': slate, 'problems': list(dict.fromkeys(errors))}


class JsonCache:
    """Stat-invalidation cache; no source data is taken from the prepared plan."""
    def __init__(self): self.entries = {}

    def load(self, path):
        path = Path(path); stat = path.stat(); key = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self.entries.get(str(path))
        if cached is None or cached[0] != key:
            raw = path.read_bytes(); value = json.loads(raw)
            self.entries[str(path)] = (key, value, hashlib.sha256(raw).hexdigest(), stat.st_mtime)
        _, value, sha, modified = self.entries[str(path)]
        return value, {'path': str(path), 'sha256': sha, 'modified_at': modified}


def atomic_text(path, text):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.staged'); temp.write_text(text); temp.replace(path)


def compact_diff(previous, current):
    if not previous: return {'first_frame': True, 'added_picks': current['prefix'], 'removed_or_revised_prefix': False}
    a, b = previous['prefix'], current['prefix']
    return {'added_picks': b[len(a):] if b[:len(a)] == a else [],
            'removed_or_revised_prefix': b[:len(a)] != a,
            'new_slate_ids': sorted(set(current['slate_ids'])-set(previous['slate_ids'])),
            'lost_slate_ids': sorted(set(previous['slate_ids'])-set(current['slate_ids']))}


class AdvisorWatch:
    def __init__(self, *, setup_path, catalog_path, output, capture, plan_path=None,
                 deck_path=None, registry_path=None, observe_only=False):
        if type(observe_only) is not bool: raise ValueError('observe_only must be explicit boolean')
        self.observe_only = observe_only
        self.paths = dict(setup=setup_path, catalog=catalog_path)
        if not observe_only:
            if any(p is None for p in (plan_path,deck_path,registry_path)):
                raise ValueError('Advisory mode requires plan, deck and registry; use explicit observe_only otherwise')
            self.paths.update(plan=plan_path,deck=deck_path,registry=registry_path)
        self.output = Path(output); self.capture = capture; self.cache = JsonCache()
        self.previous = None; self.first_turns = {}; self.frame_sequence = 0
        self.last_published = None

    def publish(self, value):
        text = ['# Draft advice — '+value['status'], '',
                'Checked: '+iso(value['checked_at'])+'. Expires: '+iso(value['valid_until'])+'.',
                'Read-only. Never use this file after its expiry or after the board changes.', '']
        if value.get('mode') == 'observe_only':
            text += ['Observation-only mode: no advice is produced or evaluated.', '', '; '.join(value.get('problems', []))]
        elif value.get('result', {}).get('ready_to_recommend'):
            for option in value['result']['options']:
                text += [str(option['rank'])+'. '+option['exact_user_text'], '', 'Confidence: '+option['confidence'], '']
        else: text += ['No currently valid advice.', '', '; '.join(value.get('problems', []))]
        # Raw frame is written first; decision.json is the authoritative final pointer.
        atomic_text(self.output/'readable.md', '\n'.join(text)+'\n')
        draft_room.write_json(self.output/'decision.json', value)
        self.last_published = copy.deepcopy(value)
        return value

    def tick(self, *, now=None):
        supplied_now = now
        now = time.time() if now is None else now
        value = {'schema_version': 1, 'checked_at': now, 'valid_until': now,
                 'mode': 'observe_only' if self.observe_only else 'advisory',
                 'advice_evaluated': False,
                 'status': 'capture_failed', 'problems': [], 'writes_to_sleeper': False,
                 'result': {'ready_to_recommend': False, 'options': []}}
        try:
            inputs, receipts = {}, {}
            for name, path in self.paths.items(): inputs[name], receipts[name] = self.cache.load(path)
            setup, catalog = inputs['setup'], inputs['catalog']
            plan, deck, registry = (inputs.get(x) for x in ('plan','deck','registry'))
            validate_setup(setup)
            if not isinstance(catalog, list): raise ValueError('Current identity catalog must be a list')
            names = []
            if not self.observe_only:
                watched = plan['authored']['watched_player_ids']
                names = [deck['cards'][p]['name'] for p in watched if p in deck['cards']]
            frame = self.capture(setup, names)
            # Use receipt time after capture for freshness; tests can supply a frozen clock.
            checked = time.time() if supplied_now is None else supplied_now
            self.frame_sequence += 1
            raw_path = self.output/'frames'/f'{time.time_ns()}-{self.frame_sequence}.json'
            draft_room.write_json(raw_path, frame)
            value.update(checked_at=checked, raw_frame_path=str(raw_path),
                         raw_frame_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
            parsed = normalize(frame, setup, catalog, now=checked)
            registry_at = mock_lab._iso(registry.get('observed_at')) if registry else None
            registry_age = None if registry_at is None else checked-registry_at
            registry_issues = []
            if not self.observe_only:
                if registry_age is None: registry_issues.append('registry_freshness_receipt_missing')
                elif registry_age < 0: registry_issues.append('registry_freshness_receipt_in_future')
                elif registry_age > setup.get('registry_max_age_seconds', 300): registry_issues.append('registry_freshness_receipt_stale')
            value.update(checked_at=checked, status='not_ready', raw_frame_path=str(raw_path),
                raw_frame_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                normalized=parsed, diff=compact_diff(self.previous, parsed), input_receipts=receipts,
                registry_age_seconds=registry_age,
                registry_max_age_seconds=setup.get('registry_max_age_seconds', 300))
            self.previous = parsed
            value['problems'] = list(parsed['problems'])+registry_issues
            b = parsed['board']
            if b and b['on_clock'] and 'wrong_assigned_room' not in parsed['problems'] and 'frame_not_fresh' not in parsed['problems']:
                self.first_turns.setdefault(str(parsed['current_pick']), frame['observed_at'])
            value['first_own_turn_observations'] = dict(self.first_turns)
            if self.observe_only:
                value.update(status='observation_only', advice_evaluated=False)
                # This expiry concerns the observation, never recommendation permission.
                if not parsed['problems']:
                    value['valid_until'] = min(checked+4, parsed['observed_at']+15)
                return self.publish(value)
            if b and not parsed['problems'] and not registry_issues:
                ui = {'source': 'sleeper_rendered_ui', 'observed_at': parsed['observed_at'],
                      'draft_id': b['draft_id'], 'taken_player_ids': b['taken'], 'own_roster': b['own_roster'],
                      'current_pick': b['picks_made']+1, 'on_clock': b['on_clock'], 'auto_pick': frame['ownAutoPick']}
                value['advice_evaluated'] = True
                result = draft_fastlane.evaluate(plan, deck, b, ui, now=checked,
                    current_news_revision=registry.get('news_revision'), current_source_hashes=registry.get('sources'),
                    visible_slate_ids=parsed['slate_ids'], slate_scope='complete_top_visible_slate',
                    unreviewed_fact_ids=registry.get('unreviewed_fact_ids', []),
                    private_mock_id=b['draft_id'] if setup['mode']=='private_mock' else None,
                    mock_ui=frame, player_catalog=catalog)
                value['result'] = result; value['problems'].extend(result['problems'])
                enabled_ids = {r['player_id'] for r in parsed['candidate_identities'] if r['button_enabled']}
                unavailable = [o['player_id'] for o in result['options'] if o['player_id'] not in enabled_ids]
                if unavailable:
                    result.update(ready_to_recommend=False, options=[], needs_reasoning=True)
                    value['problems'].append('recommended_candidate_control_unavailable:'+','.join(unavailable))
                provisional_unavailable = [o['player_id'] for o in result.get('provisional_options', []) if o['player_id'] not in enabled_ids]
                if provisional_unavailable:
                    result['provisional_options'] = []
                    value['problems'].append('provisional_candidate_control_unavailable:'+','.join(provisional_unavailable))
                if result['ready_to_recommend']:
                    value['status'] = 'advice_ready'
                    value['valid_until'] = min(checked+4, parsed['observed_at']+15,
                                               parsed['observed_at']+parsed['seconds_remaining'])
                elif result['needs_reasoning']: value['status'] = 'needs_comparison'
            elif any('candidate_identity' in p for p in parsed['problems']): value['status'] = 'needs_comparison'
        except Exception as exc:
            value['problems'] = [type(exc).__name__+': '+str(exc)]
            value['last_retained_frame_observed_at'] = self.previous.get('observed_at') if self.previous else None
            if self.last_published and 'raw_frame_path' not in value:
                for key in ('raw_frame_path','raw_frame_sha256','normalized','first_own_turn_observations'):
                    if key in self.last_published: value[key] = copy.deepcopy(self.last_published[key])
                value['retained_frame_is_current'] = False
        return self.publish(value)

    def run(self, *, max_duration, interval=2, monotonic=time.monotonic, sleep=time.sleep):
        if not math.isfinite(max_duration) or max_duration <= 0 or not 1 <= interval <= 10:
            raise ValueError('Positive finite duration and 1–10 second interval required')
        end = monotonic()+max_duration; count = 0
        try:
            while monotonic() < end:
                started = monotonic(); self.tick(); count += 1
                sleep(max(0, min(interval-(monotonic()-started), end-monotonic())))
        finally:
            final = copy.deepcopy(self.last_published) if self.last_published else {
                'schema_version': 1, 'mode':'observe_only' if self.observe_only else 'advisory'}
            final.update(status='observer_stopped', checked_at=time.time(), valid_until=time.time(),
                         problems=['Configured duration ended or observer interrupted'],
                         result={'ready_to_recommend':False,'options':[]}, writes_to_sleeper=False,
                         retained_frame_is_current=False)
            self.publish(final)
        return count


def main():
    if len(sys.argv)>1 and sys.argv[1]=='_capture':
        try: print(json.dumps(capture_existing_page(json.load(sys.stdin))))
        except Exception: sys.exit(2)
        return
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ('setup','catalog','output'): ap.add_argument('--'+key, type=Path, required=True)
    for key in ('plan','deck','registry'): ap.add_argument('--'+key, type=Path)
    ap.add_argument('--observe-only', action='store_true', help='Capture durable board state; never evaluate or produce advice')
    ap.add_argument('--raw-ui-fixture', type=Path); ap.add_argument('--once', action='store_true')
    ap.add_argument('--max-duration', type=float, default=1800); ap.add_argument('--interval', type=float, default=2)
    args = ap.parse_args()
    if not args.observe_only and any(getattr(args,k) is None for k in ('plan','deck','registry')):
        ap.error('Advisory mode requires --plan, --deck and --registry; otherwise use --observe-only')
    capture = (lambda setup, watched: json.loads(args.raw_ui_fixture.read_text())) if args.raw_ui_fixture else ExistingPageCapture()
    watch = AdvisorWatch(**{key+'_path':getattr(args,key) for key in ('setup','plan','deck','catalog','registry')},
                         output=args.output, capture=capture, observe_only=args.observe_only)
    if args.once:
        result = watch.tick(); print(json.dumps({'status':result['status'],'problems':result['problems'],'output':str(args.output)}))
    else: print(json.dumps({'frames_attempted':watch.run(max_duration=args.max_duration,interval=args.interval),'output':str(args.output)}))


if __name__ == '__main__': main()
