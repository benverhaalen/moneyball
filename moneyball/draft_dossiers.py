"""Precompute inspectable player evidence for a dynasty draft; never rank utility.

Build reads the existing local warehouse only. Fast retrieval uses a separate
SQLite index. Professional forecasts, retrospective diagnostics, observations
and unreviewed future theses remain different objects.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .store import Store, digest
from .warehouse import Warehouse, WarehouseError, canonical, leakage_check, timestamp
from .providers import EXPERIMENTAL_REFERENCE_SCORING


QUESTIONS = {
    'current_contribution': 'What production can this player add to our actual legal lineup now, compared with the alternative pick?',
    'future_path': 'If league rules retain future rights, which future role, capability, team and availability paths support this choice, including failure? Otherwise evaluate only the current decision horizon.',
    'availability': 'What is current participation/limitation evidence versus forecast injury incidence, games missed and role recovery?',
    'opportunity': 'What creates or removes opportunities: competitors, staff, QB, contracts, assignment and task fit?',
    'execution': 'What evidence about execution adds information beyond the professional forecast and opportunity context?',
    'coverage_and_dependence': 'Which existing failure states does this holding cover or compound, and does it become redundant?',
    'acquisition_and_waiting_cost': 'What is forgone now and likely available next turn; what capacity is consumed until the player becomes useful?',
    'continuation': 'If production fades or the prospect fails, what feasible remaining picks or future rights replace it?',
    'source_confidence': 'Which source predicts this exact target/horizon; what is its dated validation and conditioning convention?',
    'reversal': 'What observable fact or plausible future state would make the alternative pick better?',
}
HISTORY_FIELDS = ('attempts', 'completions', 'passing_yards', 'passing_tds', 'passing_interceptions',
                  'carries', 'rushing_yards', 'rushing_tds', 'targets', 'receptions',
                  'receiving_yards', 'receiving_tds', 'receiving_air_yards')
EXPECTED_FIELDS = {
    'pass_yd': 'pass_yards_gained_exp', 'pass_td': 'pass_touchdown_exp',
    'pass_int': 'pass_interception_exp', 'pass_2pt': 'pass_two_point_conv_exp',
    'rush_yd': 'rush_yards_gained_exp', 'rush_td': 'rush_touchdown_exp',
    'rush_2pt': 'rush_two_point_conv_exp', 'rec': 'receptions_exp',
    'rec_yd': 'rec_yards_gained_exp', 'rec_td': 'rec_touchdown_exp',
    'rec_2pt': 'rec_two_point_conv_exp',
}
# Verified by current Clay team names and multiple exact-name/position matches.
CLAY_TEAM_ALIASES = {'ARZ':'ARI','BLT':'BAL','CLV':'CLE','HST':'HOU','LAR':'LA'}


def clay_index(payload, players, *, as_of, scoring, season):
    if payload is None:return {}, []
    if not payload.get('source_url') or not payload.get('known_at') or timestamp(payload['known_at']) > as_of:
        raise ValueError('Clay source lacks valid source/knowability')
    by_key=defaultdict(list)
    for p in players:by_key[(p['name'],p['position'],p['team'])].append(str(p.get('id') or p['player_id']))
    out, unmatched, seen = {}, [], set()
    for row in payload['rows']:
        if row.get('season') != season:raise ValueError('Clay season mismatch')
        key=(row['player_name'],row['position'],CLAY_TEAM_ALIASES.get(row['source_team_code'],row['source_team_code']))
        if key in seen:raise ValueError('Duplicate Clay player/team/position')
        seen.add(key)
        ids=by_key.get(key,[])
        if len(ids) != 1:
            unmatched.append({'player_name':row['player_name'],'position':row['position'],'source_team_code':row['source_team_code'],
                              'reason':'no_unique_exact_name_position_team_match'})
            continue
        components=copy.deepcopy(row['components'])
        if any(not _finite(x) for x in components.values()):raise ValueError('Nonfinite Clay component')
        out[ids[0]]={'source_id':'espn_mike_clay_pdf','source_url':payload['source_url'],
            'known_at':payload['known_at'],'source_stated_updated':payload.get('source_stated_updated'),
            'source_pdf_sha256':payload.get('source_pdf_sha256'),'source_page':row['source_page'],
            'season':season,'stats':components,'source_player_name':row['player_name'],
            'source_team_code':row['source_team_code'],'identity_match':'exact name, position and explicitly normalized team; no fuzzy matching',
            'source_conditioning':copy.deepcopy(payload.get('source_conditioning')),
            'league_observed_component_subtotal':sum(v*scoring.get(k,0) for k,v in components.items()),
            'missing_scoring_keys':sorted(k for k,w in scoring.items() if w and k not in components),
            'interpretation':'Current-season displayed components, conditional exposure retained; no injury adjustment, averaging, future projection or source-rank utility applied.'}
    return out,unmatched


def expected_opportunity_index(payload, *, as_of, scoring, draft_season):
    """Re-score descriptive ffopportunity components, never future forecasts."""
    if payload is None:
        return {}
    for key in ('source_id','source_url','observed_at','available_at','interpretation','rows'):
        if key not in payload:raise ValueError('Opportunity source missing '+key)
    if timestamp(payload['available_at']) > as_of or timestamp(payload['observed_at']) > as_of:
        raise ValueError('Opportunity source was not knowable at cutoff')
    if payload.get('season') != draft_season-1:
        raise ValueError('Opportunity diagnostic must be previous NFL season')
    groups, seen = defaultdict(list), set()
    for r in payload['rows']:
        if int(r['season']) != draft_season-1:raise ValueError('Mixed opportunity source seasons')
        pid, week = str(r['player_id']), int(r['week'])
        if pid in ('NA','','None') or not 1 <= week <= 18:continue
        key = (pid,r['game_id'],r.get('posteam'))
        if key in seen:raise ValueError('Duplicate opportunity row')
        seen.add(key);groups[pid].append(r)
    result = {}
    for pid, rows in groups.items():
        expected, actual = defaultdict(float), defaultdict(float)
        missing, matched_rows = Counter(), Counter()
        for r in rows:
            for key, field in EXPECTED_FIELDS.items():
                a, b = r.get(field), r.get(field.removesuffix('_exp'))
                if a in (None,'','NA') or b in (None,'','NA'):
                    missing[key] += 1;continue
                a,b = float(a),float(b)
                if not _finite(a) or not _finite(b):raise ValueError('Nonfinite opportunity component')
                expected[key] += a;actual[key] += b;matched_rows[key] += 1
        ep = sum(v*scoring.get(k,0) for k,v in expected.items()) if expected else None
        ap = sum(v*scoring.get(k,0) for k,v in actual.items()) if actual else None
        result[pid] = {'status':'retrospective_diagnostic_not_forecast' if expected else 'no_matched_opportunity_components','season':draft_season-1,
            'observed_rows':len(rows),'weeks':sorted({int(r['week']) for r in rows}),
            'expected_component_totals':dict(expected), 'actual_same_component_totals':dict(actual),
            'league_expected_component_subtotal':ep,'actual_same_component_subtotal':ap,
            'actual_minus_expected_same_components':ap-ep if ep is not None and ap is not None else None,
            'matched_component_coverage':'partial' if missing else 'all_supported_components_observed',
            'missing_component_rows':dict(missing),'matched_component_rows':dict(matched_rows),
            'unsupported_scoring_keys':sorted(k for k,w in scoring.items() if w and k not in EXPECTED_FIELDS),
            'scoring_hash':digest(scoring),'source_url':payload['source_url'],'available_at':payload['available_at'],
            'source_id':payload['source_id'],'raw_sha256':payload.get('raw_sha256'),
            'license':payload.get('license'),'limitations':payload.get('limitations',[]),
            'interpretation':'2025 realized-opportunity expected subtotal rescored to league weights. The difference is descriptive, not a projected rebound or a 2026 ranking. No expected lost fumbles; actual comparison omits identical components.'}
    return result


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _provenance(rows):
    found = {}
    for row in rows:
        p = row.get('_provenance') or {}
        found[canonical(p)] = p
    return list(found.values())


def _forecast(row):
    return {k: copy.deepcopy(row.get(k)) for k in (
        'source_id', 'provider', 'season', 'week', 'projection_type', 'stats',
        'mean', 'mean_interpretation', 'projection_completeness',
        'missing_scoring_fields', 'explicit_zero_scoring_fields',
        'scoring_hash', 'uncertainty_status', 'season_denominator', '_provenance')}


def _history(rows, season):
    kept = [r for r in rows if r.get('season') == season and r.get('season_type') == 'REG']
    # Preserve the observation denominator; missing rows never become missed games.
    weeks = [r.get('week') for r in kept]
    if len(weeks) != len(set(weeks)):
        raise ValueError('Duplicate player/week history')
    totals = {k: sum(r[k] for r in kept if _finite(r.get(k)))
              if any(_finite(r.get(k)) for r in kept) else None for k in HISTORY_FIELDS}
    return {'season': season, 'status': 'observed_box_score_history' if kept else 'no_matched_history',
            'observed_weeks': sorted(weeks), 'observed_rows': len(kept), 'totals': totals,
            'missing_field_rows': {k: sum(not _finite(r.get(k)) for r in kept) for k in HISTORY_FIELDS},
            'interpretation': 'Descriptive prior-season totals. Rows are observations, not a full health risk set; absence of rows is not injury or zero talent.',
            'provenance': _provenance(kept)}


def make_cards(bundle, forecasts, metadata, history, *, as_of, scoring, evidence=None, opportunity=None, clay=None):
    """Pure transformation of versioned inputs. No imputation or pick advice."""
    as_of = timestamp(as_of)
    if not isinstance(bundle, dict) or not isinstance(bundle.get('players'), list):
        raise ValueError('Projection bundle requires player rows')
    season = bundle.get('season')
    if isinstance(season, bool) or not isinstance(season, int):
        raise ValueError('Projection season must be an integer')
    if bundle.get('scoring_hash') != digest(scoring):
        raise ValueError('Projection and league scoring disagree')
    if bundle.get('cutoff') is None or timestamp(bundle['cutoff']) > as_of:
        raise ValueError('Projection cutoff is unknown or later than dossier cutoff')
    if bundle.get('created_at') is None or timestamp(bundle['created_at']) > as_of:
        raise ValueError('Projection artifact did not exist at dossier cutoff')
    all_rows = list(forecasts) + list(metadata) + list(history)
    leakage_check(all_rows, as_of, raise_on_error=True)
    if any(r.get('scoring_hash') != digest(scoring) for r in forecasts):
        raise ValueError('Provider forecast scoring disagrees with league')
    by_forecast, by_history = defaultdict(list), defaultdict(list)
    opportunity_rows = expected_opportunity_index(opportunity,as_of=as_of,scoring=scoring,draft_season=season)
    clay_rows,_ = clay_index(clay,bundle['players'],as_of=as_of,scoring=scoring,season=season)
    seen_forecasts = set()
    for row in forecasts:
        if row.get('season') != season:
            raise ValueError('Forecast season does not match draft season')
        key = (str(row['player_id']), row['source_id'], row.get('week'))
        if key in seen_forecasts:
            raise ValueError('Duplicate provider/player/horizon forecast')
        seen_forecasts.add(key)
        by_forecast[str(row['player_id'])].append(row)
    by_meta = {}
    for row in metadata:
        pid = str(row['player_id'])
        if pid in by_meta:
            raise ValueError('Duplicate current player metadata')
        by_meta[pid] = row
    for row in history:
        by_history[str(row['player_id'])].append(row)
    evidence = evidence or {}
    if not isinstance(evidence, dict):
        raise ValueError('Evidence must map player IDs to dated case cards')
    ids, cards = set(), []
    for p in bundle['players']:
        pid = str(p.get('id') or p.get('player_id') or '')
        if not pid or pid in ids:
            raise ValueError('Missing or duplicate projected player ID')
        ids.add(pid)
        meta, fs = by_meta.get(pid, {}), by_forecast.get(pid, [])
        reviews = copy.deepcopy(evidence.get(pid, []))
        if not isinstance(reviews, list):
            raise ValueError('Each player evidence value must be a list')
        for review in reviews:
            if not isinstance(review, dict) or not review.get('target') or not review.get('source_url'):
                raise ValueError('Case evidence needs target and source_url')
            if review.get('known_at') is None or timestamp(review['known_at']) > as_of:
                raise ValueError('Case evidence lacks valid knowability at the cutoff')
            if not review.get('claim') or not review.get('counterevidence') or not review.get('baseline_overlap'):
                raise ValueError('Case evidence needs claim, counterevidence and baseline_overlap')
        # Injury designations are observations. They cannot become incidence probabilities.
        health = {k: copy.deepcopy(meta.get(k)) for k in (
            'injury_status', 'injury_body_part', 'injury_notes', 'injury_start_date',
            'practice_description', 'practice_participation', 'status')}
        health.update({'provenance': meta.get('_provenance'), 'future_injury_probability': None,
            'expected_games_missed': None, 'restored_role_probability': None,
            'interpretation': 'Reported status only. Null or Active does not establish health or zero future injury risk. No second injury haircut applied to professional forecasts.'})
        season_forecasts = [_forecast(r) for r in fs if r.get('week') is None]
        weekly = [_forecast(r) for r in fs if r.get('week') is not None]
        missing_weeks = list(p.get('missing_nonbye_forecast_weeks') or [])
        gaps = ['individualized_future_paths_missing', 'quantified_injury_risk_missing',
                'team_and_board_specific_title_comparison_required', 'professional_source_conditioning_requires_review']
        if not season_forecasts: gaps.append('season_professional_forecast_missing')
        if missing_weeks: gaps.append('scheduled_week_forecasts_missing')
        if not reviews: gaps.append('player_specific_mechanism_review_missing')
        priorities=[]
        if p.get('years_exp') == 0:
            priorities.append('Rookie: research NFL assignment, translated college evidence and competitor paths; no NFL history is not zero upside.')
        if health.get('injury_status') or health.get('practice_participation'):
            priorities.append('A status/practice designation is present: obtain dated current-health interpretation before turning it into a production or recovery adjustment.')
        if missing_weeks:
            priorities.append('Missing scheduled-week forecasts: a small current subtotal may reflect source omission, not projected lack of value.')
        if pid in clay_rows:
            priorities.append('Two current numerical views available: reconcile games, injury treatment, role and rounding before averaging or calling disagreement an edge.')
        if p.get('gsis_id') not in opportunity_rows:
            priorities.append('No matched xFP diagnostic: do not assign zero opportunity or prefer experienced players merely because their data are richer.')
        card = {
            'schema_version': 1, 'player_id': pid, 'name': p.get('name'),
            'identity': {k: copy.deepcopy(p.get(k)) for k in ('gsis_id','position','fantasy_positions','team','age','years_exp')},
            'identity_provenance': {'source':'projection_bundle','model_id':bundle.get('model_id'),
                'cutoff':bundle['cutoff'],'artifact_created_at':bundle['created_at'],
                'interpretation':'Identity fields copied from this projection bundle, including its historical crosswalk and team normalization. Bundle SHA256 and inputs are in the build receipt.'},
            'current_metadata_provenance': meta.get('_provenance'),
            'as_of': as_of, 'draft_season': season,
            'acquisition_reference': {'adp': p.get('adp'), 'source': p.get('demand_source'),
                'as_of': bundle['cutoff'], 'role': 'rough availability only, not dynasty utility',
                'buffer': None, 'buffer_interpretation': 'user scenario, not a confidence interval'},
            'current_production': {'season_forecasts': season_forecasts, 'weekly_forecasts': weekly,
                'independent_semantics_crosscheck': clay_rows.get(pid,{'status':'no_exact_match_or_source_not_acquired'}),
                'scheduled_weeks_missing': missing_weeks,
                'source_family_count_observed': len({r.get('source_id') for r in fs}),
                'source_independence': 'Provider labels are not verified independent forecast families',
                'component_applicability': 'Missing main offense, sparse uncommon events and inapplicable entity fields require separate audits.',
                'interpretation': 'Forecast components are intermediate evidence; not a ranking, completed-team title estimate or independent adjustment.'},
            'availability': health,
            'current_role_observations': {k: copy.deepcopy(meta.get(k)) for k in ('depth_chart_order','depth_chart_position','team','years_exp')},
            'role_observation_caveat': 'Provider metadata is a dated reported depth position, not a forecast of future routes, targets or job security.',
            'history': _history(by_history.get(str(p.get('gsis_id')), []), season-1),
            'expected_opportunity': opportunity_rows.get(str(p.get('gsis_id')), {'status':'no_matched_opportunity_diagnostic','forecast_adjustment':None}),
            'future_paths': {str(y): {'status': 'not_yet_estimated', 'role_distribution': None,
                'production_distribution': None, 'required': ['NFL assignment and competition','capability/development','availability','team/personnel context','ownership and feasible replacements']}
                for y in (season+1, season+2)},
            'uncertainty_audit': {k: copy.deepcopy(p.get(k)) for k in ('calibration_status','variance_method','historical_observations','availability_source')},
            'case_evidence': reviews,
            'draft_checklist': dict(QUESTIONS),
            'live_decision': {'status': 'requires_actual_squad_board_and_feasible_comparisons',
                'annual_title_deltas': None, 'best_alternative': None, 'continuation_requirements': None,
                'current_value_rule': 'A current contributor needs a defensible multi-year comparison and feasible succession; youth alone is not value either.'},
            'review_gaps': gaps,
            'review_priorities_from_coverage': priorities,
            'decision_status': 'evidence_dossier_only_not_an_automatic_draft_recommendation',
            'input_model_id': bundle.get('model_id'),
        }
        cards.append(card)
    if set(evidence) - ids:
        raise ValueError('Evidence contains an unknown player ID; join must be explicit')
    return cards


def write_index(path, cards, manifest):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent); os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute('CREATE TABLE cards(player_id TEXT PRIMARY KEY,name TEXT NOT NULL,position TEXT,nfl_team TEXT,adp REAL,body TEXT NOT NULL)')
            db.execute('CREATE INDEX cards_name ON cards(name COLLATE NOCASE)')
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,body TEXT NOT NULL)')
            for c in cards:
                db.execute('INSERT INTO cards VALUES (?,?,?,?,?,?)',(c['player_id'],c['name'],c['identity']['position'],c['identity']['team'],c['acquisition_reference']['adp'],canonical(c)))
            db.execute('INSERT INTO metadata VALUES (?,?)',('manifest',canonical(manifest)))
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def lookup(path, player):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT body FROM cards WHERE player_id=? OR name=? COLLATE NOCASE',(str(player),str(player))).fetchall()
    if len(rows) != 1:raise ValueError('Player must match one exact ID or full name; matches='+str(len(rows)))
    return json.loads(rows[0][0])


def compact(card):
    result = copy.deepcopy(card)
    result['current_production']['weekly_forecasts'] = {
        'count': len(card['current_production']['weekly_forecasts']),
        'detail': 'Use --full to retrieve the component rows and provenance; not re-averaged here.'}
    for row in result['current_production']['season_forecasts']:
        row['stats'] = {k:v for k,v in (row.get('stats') or {}).items() if k in set(EXPERIMENTAL_REFERENCE_SCORING)|{'pass_att','rush_att','rec_tgt'}}
    return result


def render_card(c):
    """Human-readable view of evidence, without a fabricated player thesis."""
    identity=c['identity'];f=c['current_production'];x=dict(c['expected_opportunity']);a=c['acquisition_reference']
    if 'league_expected_component_subtotal' not in x:
        x['league_expected_component_subtotal'] = x.get('ratrace_expected_component_subtotal')
    lines=[f"# {c['name']}: draft evidence dossier",'',
        f"{identity['position']} · {identity['team']} · observed age {identity.get('age')} · Sleeper ID {c['player_id']}",'',
        f"Information cutoff: {datetime.fromtimestamp(c['as_of'],timezone.utc).isoformat()}. This is a prepared evidence record, **not a completed league recommendation**.",'',
        f"ADP: {a['adp']} ({a['source']}). This describes rough availability, not player value.",'',
        '## Current professional inputs','',
        '| Source | Receptions | Receiving yards | Receiving TD | Passing yards | Passing TD | INT | Rush yards | Rush TD |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    rows=[(r.get('source_id'),r.get('stats') or {}) for r in f['season_forecasts']]
    cross=f['independent_semantics_crosscheck']
    if cross.get('stats'):rows.append(('Mike Clay — separate exposure assumptions',cross['stats']))
    for source,s in rows:
        vals=[str(s[k]) if k in s and s[k] is not None else 'missing' for k in ('rec','rec_yd','rec_td','pass_yd','pass_td','pass_int','rush_yd','rush_td')]
        lines.append('| '+str(source)+' | '+' | '.join(vals)+' |')
    lines+=['','Components are intermediate forecasts. Missing is not zero; no blind averaging or extra injury haircut is applied.',
            f"Scheduled weeks lacking a provider forecast: {f['scheduled_weeks_missing']}.",'']
    if cross.get('source_url'):
        lines += [f"[Mike Clay source]({cross['source_url']}), page {cross['source_page']}; displayed games: {cross['stats'].get('games')}. The guide describes 17-game forecasts and separate injury adjustment; some displayed game counts differ. Preserve that ambiguity until aligned with other sources.",'']
    lines += ['## Opportunity and health evidence','',
        f"Reported injury designation: {c['availability'].get('injury_status')!r}. Null/Active does not mean zero injury risk. Individual injury, missed-game and restored-role probabilities are **not yet supplied**.",'',
        f"Prior NFL season: {c['history']['observed_rows']} observed box-score rows. Missing rows are not inferred injuries.",'']
    if x['status']=='retrospective_diagnostic_not_forecast':
        lines += [f"2025 expected-opportunity subtotal under configured league weights: {x['league_expected_component_subtotal']:.2f}; actual minus expected over identical included components: {x['actual_minus_expected_same_components']:.2f}.",
            f"[ffopportunity source]({x['source_url']}). Descriptive realized-opportunity diagnostic; expected lost fumbles and other unsupported events are omitted from both sides. This is **not a projected rebound or 2026 forecast**.",'']
        if x.get('missing_component_rows'):
            lines += [f"Partial component coverage: {x['missing_component_rows']}. Subtotals cover only observed matched pairs; these missing values are not zeros.",'']
    else:lines += ['No matched expected-opportunity history. This is unobserved, not zero future value.','']
    lines += ['## League decision horizon that still needs to be established','',
        'Future individualized role and production paths, when league rules retain rights: **not yet estimated**. Current age or a small current projection must not silently decide future value.','']
    lines += [f"- {text}" for text in c['review_priorities_from_coverage']]+['']
    lines += [f"- {text}" for text in c['draft_checklist'].values()]
    lines += ['','## Reviewed mechanism evidence','']
    if not c['case_evidence']:lines += ['No individual mechanism review has been attached yet. The data import is complete for the listed sources; the football thesis is not.']
    for r in c['case_evidence']:
        lines += [f"- {r['target']}: {r['claim']} ([source]({r['source_url']})); counterevidence: {r['counterevidence']}; baseline overlap: {r['baseline_overlap']}."]
        lines += [f"  First knowable here: {r['known_at']}. Confidence: {r.get('confidence', 'not supplied')}."]
        if r.get('falsification_test'):
            lines += [f"  Test or reversal trigger: {r['falsification_test']}"]
    return '\n'.join(lines)+'\n'


def build(root, *, alias=None, evidence=None, as_of=None):
    root = Path(root); as_of = timestamp(as_of)
    path = root/'lab'/'derived'/'projection.json'
    bundle = json.loads(path.read_text())
    store, w = Store(root), Warehouse(root/'lab')
    snap = store.latest(store.resolve_alias(alias))
    if not snap:raise ValueError('Observed league configuration missing')
    if snap['created'] > as_of:raise ValueError('League snapshot was not observed by cutoff')
    season = int(snap['data']['league']['season'])
    if bundle['season'] != season:raise ValueError('Projection is for a different league season')
    forecasts = w.query('projections',cutoff=as_of,seasons=[season])
    metadata = w.query('player_metadata',cutoff=as_of)
    history = w.query('weekly',cutoff=as_of,seasons=[season-1],positions=['QB','RB','WR','TE'])
    opportunity_path = root/'research'/'draft-source-samples'/f'ffopportunity-weekly-{season-1}.json'
    opportunity = json.loads(opportunity_path.read_text()) if opportunity_path.exists() else None
    clay_path=root/'research'/'draft-source-samples'/f'clay-{season}-components.json'
    clay=json.loads(clay_path.read_text()) if clay_path.exists() else None
    cards = make_cards(bundle,forecasts,metadata,history,as_of=as_of,
                       scoring=snap['data']['league']['scoring_settings'],evidence=evidence,opportunity=opportunity,clay=clay)
    _,clay_unmatched=clay_index(clay,bundle['players'],as_of=as_of,scoring=snap['data']['league']['scoring_settings'],season=season)
    manifest = {'schema_version':1,'built_at':time.time(),'information_cutoff':as_of,
        'league_snapshot_hash':snap['hash'],'league_snapshot_observed_at':snap['created'],
        'league_last_check':{k:v for k,v in (store.last_run(alias) or {}).items() if k in ('checked_at','ok','snapshot_hash','changed')},
        'projection_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'projection_cutoff':bundle['cutoff'],'cards':len(cards),'adp_covered':sum(c['acquisition_reference']['adp'] is not None for c in cards),
        'case_reviewed':sum(bool(c['case_evidence']) for c in cards),
        'history_matched':sum(c['history']['observed_rows']>0 for c in cards),
        'opportunity_matched':sum(c['expected_opportunity']['status']=='retrospective_diagnostic_not_forecast' for c in cards),
        'opportunity_input_sha256':hashlib.sha256(opportunity_path.read_bytes()).hexdigest() if opportunity else None,
        'clay_matched':sum('stats' in c['current_production']['independent_semantics_crosscheck'] for c in cards),
        'clay_unmatched_count':len(clay_unmatched),
        'clay_input_sha256':hashlib.sha256(clay_path.read_bytes()).hexdigest() if clay else None,
        'gaps':dict(Counter(g for c in cards for g in c['review_gaps'])),
        'scope':'Projected candidate universe only; not every active NFL player. Research cards, not a fitted dynasty ranking.',
        'network_calls':0,'output':str(root/'draft'/'dossiers.sqlite3')}
    write_index(root/'draft'/'dossiers.sqlite3',cards,manifest)
    (root/'draft'/'clay-unmatched.json').write_text(json.dumps(clay_unmatched,indent=2)+'\n')
    (root/'draft'/'dossier-build.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',default='.moneyball')
    sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('--evidence',type=Path);b.add_argument('--as-of');b.add_argument('--alias',default=None)
    q=sub.add_parser('show');q.add_argument('player');q.add_argument('--full',action='store_true');q.add_argument('--markdown-file',type=Path)
    args=p.parse_args()
    try:
        if args.command=='build':
            reviews=json.loads(args.evidence.read_text()) if args.evidence else None
            result=build(args.data_dir,alias=args.alias,evidence=reviews,as_of=args.as_of)
        else:
            result=lookup(Path(args.data_dir)/'draft'/'dossiers.sqlite3',args.player)
            if args.markdown_file:
                args.markdown_file.parent.mkdir(parents=True,exist_ok=True)
                args.markdown_file.write_text(render_card(result))
            if not args.full:result=compact(result)
        print(canonical(result))
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,WarehouseError) as exc:
        p.exit(2,json.dumps({'error':str(exc)})+'\n')


if __name__=='__main__':main()
