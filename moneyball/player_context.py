"""Descriptive workload/execution context, never a player-value forecast.

Uses observed warehouse vintages and exact, whitespace-normalized identifiers.
Null is not zero; games with no row are not diagnosed as inactive or injured.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from .draft_universe import clean_id
from .identity import canonical_team
from .warehouse import Warehouse, ValidationError, canonical, leakage_check, timestamp


POSITIONS = ('QB', 'RB', 'WR', 'TE')
COUNTS = ('attempts', 'completions', 'passing_yards', 'passing_tds', 'passing_interceptions',
          'carries', 'rushing_yards', 'rushing_tds', 'targets', 'receptions',
          'receiving_yards', 'receiving_tds', 'receiving_air_yards', 'receiving_yards_after_catch')
RATES = {
    'completion_rate': ('completions', 'attempts'),
    'passing_yards_per_attempt': ('passing_yards', 'attempts'),
    'passing_td_rate': ('passing_tds', 'attempts'),
    'passing_interception_rate': ('passing_interceptions', 'attempts'),
    'rushing_yards_per_carry': ('rushing_yards', 'carries'),
    'rushing_td_rate': ('rushing_tds', 'carries'),
    'catch_rate': ('receptions', 'targets'),
    'receiving_yards_per_target': ('receiving_yards', 'targets'),
    'receiving_td_rate': ('receiving_tds', 'targets'),
    'air_yards_per_target': ('receiving_air_yards', 'targets'),
    'yards_after_catch_per_reception': ('receiving_yards_after_catch', 'receptions'),
}
NGS = {
    'passing': {'counts': ('attempts', 'completions', 'pass_yards', 'pass_touchdowns', 'interceptions'),
        'fields': ('avg_time_to_throw', 'avg_intended_air_yards', 'avg_completed_air_yards',
                   'aggressiveness', 'expected_completion_percentage', 'completion_percentage_above_expectation')},
    'receiving': {'counts': ('targets', 'receptions', 'yards', 'rec_touchdowns'),
        'fields': ('avg_cushion', 'avg_separation', 'avg_intended_air_yards', 'avg_yac',
                   'avg_expected_yac', 'avg_yac_above_expectation', 'percent_share_of_intended_air_yards')},
    'rushing': {'counts': ('rush_attempts', 'rush_yards', 'rush_touchdowns'),
        'fields': ('efficiency', 'avg_time_to_los', 'percent_attempts_gte_eight_defenders',
                   'expected_rush_yards', 'rush_yards_over_expected', 'rush_yards_over_expected_per_att',
                   'rush_pct_over_expected')},
}
NGS_UNITS = {
    'avg_time_to_throw': 'seconds; sacks excluded in source definition',
    'avg_intended_air_yards': 'yards per source-eligible target/attempt',
    'avg_completed_air_yards': 'yards per source-eligible completion',
    'aggressiveness': 'percent of source passing attempts into tight coverage',
    'expected_completion_percentage': 'percent; upstream expected-completion model output',
    'completion_percentage_above_expectation': 'percentage points; upstream model residual',
    'avg_cushion': 'yards at snap on source-eligible targets',
    'avg_separation': 'yards from nearest defender at catch/incompletion; targeted plays only',
    'avg_yac': 'yards per source-eligible reception',
    'avg_expected_yac': 'yards per source-eligible reception; upstream model output',
    'avg_yac_above_expectation': 'yards per source-eligible reception; upstream model residual',
    'percent_share_of_intended_air_yards': 'percent of source team intended air yards',
    'efficiency': 'distance traveled divided by rushing yards; not generic performance quality',
    'avg_time_to_los': 'seconds before reaching line of scrimmage',
    'percent_attempts_gte_eight_defenders': 'percent of source rushes facing at least eight box defenders',
    'expected_rush_yards': 'yards; upstream tracking model total over qualified plays',
    'rush_yards_over_expected': 'yards; upstream tracking-model residual total',
    'rush_yards_over_expected_per_att': 'yards per tracking-qualified attempt',
    'rush_pct_over_expected': 'fraction of tracking-qualified rushes exceeding expected yards',
}
NGS_COUNT_CROSSCHECK = {
    'attempts': 'attempts', 'completions': 'completions', 'pass_yards': 'passing_yards',
    'pass_touchdowns': 'passing_tds', 'interceptions': 'passing_interceptions',
    'targets': 'targets', 'receptions': 'receptions', 'yards': 'receiving_yards',
    'rec_touchdowns': 'receiving_tds', 'rush_attempts': 'carries',
    'rush_yards': 'rushing_yards', 'rush_touchdowns': 'rushing_tds',
}
DATASETS = ('weekly', 'snap_counts', 'players', 'rosters', 'ngs_passing', 'ngs_receiving',
            'ngs_rushing', 'ftn_charting', 'pbp', 'draft_picks', 'combine')


def numeric(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError('Boolean is not a numerical observation')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError('Invalid numerical observation') from exc
    if not math.isfinite(result):
        raise ValidationError('Nonfinite numerical observation')
    return result


def regular(row):
    return str(row.get('season_type') or row.get('game_type') or '').upper() == 'REG'


def gid(row):
    return clean_id(row.get('gsis_id') or row.get('player_id'))


def sources(rows):
    found = {}
    for row in rows:
        p = row.get('_provenance') or {}
        if p.get('batch_id') is None:
            raise ValidationError('Context input lacks a source batch')
        found[p['batch_id']] = {k: p.get(k) for k in ('source_id', 'dataset', 'batch_id', 'raw_id',
            'available_at', 'observed_at', 'historical_vintage_unknown', 'content_sha256')}
    return [found[key] for key in sorted(found)]


def summed(rows, field):
    values = [numeric(r.get(field)) for r in rows]
    present = [v for v in values if v is not None]
    return {'value': sum(present) if present else None, 'n_observed_rows': len(present),
            'n_candidate_rows': len(rows), 'missing_rows': len(rows) - len(present),
            'field': field, 'aggregation': 'sum_on_observed_rows'}


def ratio(rows, numerator, denominator):
    pairs = [(numeric(r.get(numerator)), numeric(r.get(denominator))) for r in rows]
    observed = [(a, b) for a, b in pairs if a is not None and b is not None]
    if any(b < 0 for _, b in observed):
        raise ValidationError('Negative exposure denominator')
    a = sum(x for x, _ in observed) if observed else None
    b = sum(x for _, x in observed) if observed else None
    return {'value': a / b if b is not None and b > 0 else None,
            'numerator': a, 'denominator': b, 'numerator_field': numerator,
            'denominator_field': denominator, 'n_paired_rows': len(observed),
            'n_positive_exposure_rows': sum(y > 0 for _, y in observed),
            'n_candidate_rows': len(rows), 'missing_pair_rows': len(rows) - len(observed),
            'aggregation': 'ratio_of_paired_sums_not_mean_of_weekly_ratios'}


def unique(rows, key, label):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValidationError('Duplicate normalized ' + label + ': ' + str(k))
        result[k] = row
    return result


def decompose(before, after, output_field, exposure_field):
    """Symmetric two-factor arithmetic allocation, not causal attribution."""
    a, b = ratio(before, output_field, exposure_field), ratio(after, output_field, exposure_field)
    if a['value'] is None or b['value'] is None or a['missing_pair_rows'] or b['missing_pair_rows']:
        return {'status': 'insufficient_complete_positive_exposure', 'before': a, 'after': b}
    volume = (b['denominator'] - a['denominator']) * (a['value'] + b['value']) / 2
    execution = (a['denominator'] + b['denominator']) / 2 * (b['value'] - a['value'])
    change = b['numerator'] - a['numerator']
    if not math.isclose(volume + execution, change, abs_tol=1e-8, rel_tol=1e-10):
        raise ValidationError('Factor decomposition failed conservation')
    return {'status': 'descriptive_arithmetic_only', 'total_output_change': change,
            'opportunity_count_component': volume, 'output_per_opportunity_component': execution,
            'before': a, 'after': b,
            'caveat': 'Exposure includes participation, role and team volume. Per-opportunity output includes skill, scheme, teammates, opposition and luck. Neither term identifies a causal mechanism.'}


def build_context(players, data, *, cutoff, seasons=(2023, 2024, 2025), current_season=2026):
    cutoff = timestamp(cutoff)
    seasons = tuple(sorted(set(seasons)))
    unique(players, lambda p: str(p['player_id']), 'cohort player')
    exact = [p for p in players if clean_id(p.get('gsis_id'))]
    unique(exact, lambda p: clean_id(p['gsis_id']), 'cohort GSIS')
    for rows in data.values():
        leakage_check(rows, cutoff, raise_on_error=True)
    for p in players:
        if p.get('identity_provenance'):
            leakage_check([{'_provenance': p['identity_provenance']}], cutoff, raise_on_error=True)
    weekly = [r for r in data.get('weekly', []) if r.get('season') in seasons and regular(r)]
    unique(weekly, lambda r: (gid(r), r['season'], r['week']), 'weekly player-season-week')
    by_player_season = defaultdict(list)
    team_games = defaultdict(list)
    for row in weekly:
        by_player_season[(gid(row), row['season'])].append(row)
        team_games[(row['season'], canonical_team(row['team']), row['week'])].append(row)
    team_den = {key: {f: summed(rows, f) for f in ('attempts', 'carries', 'targets')}
                for key, rows in team_games.items()}

    # An exact GSIS→PFR crosswalk is required. Conflicting IDs are quarantined.
    cross = defaultdict(list)
    for row in data.get('players', []) + data.get('rosters', []):
        if row.get('identity_verified') and gid(row) and clean_id(row.get('pfr_id')):
            cross[gid(row)].append(row)
    pfr_by_gsis = {}
    ambiguous = []
    for player_id, rows in cross.items():
        ids = {clean_id(r['pfr_id']) for r in rows}
        if len(ids) == 1:
            pfr_by_gsis[player_id] = next(iter(ids))
        else:
            ambiguous.append(player_id)
    snaps = [r for r in data.get('snap_counts', []) if r.get('season') in seasons and regular(r)]
    snap_index = unique(snaps, lambda r: (clean_id(r['pfr_player_id']), r['game_id'], canonical_team(r['team'])), 'snap player-game-team')
    snap_team = defaultdict(list)
    snap_players = defaultdict(list)
    for row in snap_index.values():
        snap_team[(row['game_id'], canonical_team(row['team']))].append(row)
        snap_players[(clean_id(row['pfr_player_id']), row['season'])].append(row)
    total_snaps = {}
    for key, rows in snap_team.items():
        witnesses = {numeric(r.get('offense_snaps')) for r in rows if numeric(r.get('offense_pct')) == 1}
        witnesses.discard(None)
        if len(witnesses) > 1:
            raise ValidationError('Conflicting full-snap witnesses')
        if witnesses:
            total = next(iter(witnesses))
            if any(numeric(r.get('offense_snaps')) is not None and numeric(r['offense_snaps']) > total for r in rows):
                raise ValidationError('Player snaps exceed full-snap witness')
            total_snaps[key] = total

    ngs = {}
    ngs_coverage = {}
    for kind, spec in NGS.items():
        rows = [r for r in data.get('ngs_' + kind, []) if r.get('season') in seasons and regular(r)]
        unique(rows, lambda r: (gid(r), r['season'], r['week']), 'NGS ' + kind)
        ngs[kind] = defaultdict(list)
        for r in rows:
            ngs[kind][(gid(r), r['season'])].append(r)
        den = spec['counts'][0]
        values = [numeric(r.get(den)) for r in rows if r.get('week', 0) > 0 and r.get(den) is not None]
        ngs_coverage[kind] = {'weekly_rows': len(values), 'season_summary_rows': sum(r.get('week') == 0 for r in rows),
            'observed_minimum_weekly_exposure': min(values) if values else None, 'exposure_field': den,
            'published_weekly_positions': sorted({r.get('position') for r in rows if r.get('week', 0) > 0 and r.get('position')}),
            'minimum_interpretation': 'Empirical minimum among published rows, not proof of the provider inclusion rule.'}

    # FTN has no player IDs: exact play ID and game ID join to PBP is mandatory.
    chart = unique(data.get('ftn_charting', []), lambda r: (r.get('game_id'), clean_id(r.get('play_id'))), 'FTN game-play')
    chart_player = defaultdict(list)
    ftn_audit = Counter()
    pbp = [r for r in data.get('pbp', []) if r.get('season') in seasons and regular(r)]
    unique(pbp, lambda r: (r['game_id'], clean_id(r['play_id'])), 'PBP game-play')
    for play in pbp:
        if play.get('play_type') not in ('pass', 'run') or play.get('play_deleted') == 1 or play.get('two_point_attempt') == 1:
            continue
        ftn = chart.get((play['game_id'], clean_id(play['play_id'])))
        if ftn is None:
            ftn_audit['eligible_pbp_plays_without_chart'] += 1
            continue
        ftn_audit['eligible_pbp_plays_joined'] += 1
        passing = play.get('pass_attempt') == 1 and play.get('sack') != 1 and play.get('qb_spike') != 1
        receiving = passing and clean_id(play.get('receiver_player_id'))
        rushing = play.get('rush_attempt') == 1 and play.get('qb_kneel') != 1
        for role, valid, key in [('passing', passing, 'passer_player_id'), ('receiving', receiving, 'receiver_player_id'), ('rushing', rushing, 'rusher_player_id')]:
            player_id = clean_id(play.get(key))
            if valid and player_id:
                chart_player[(player_id, play['season'], role)].append((play, ftn))

    roster_rows = [r for r in data.get('rosters', []) if r.get('season') == current_season]
    roster_by_player, roster_by_team = defaultdict(list), defaultdict(list)
    for row in roster_rows:
        if row.get('identity_verified') and gid(row):
            roster_by_player[gid(row)].append(row)
        if row.get('position') in POSITIONS:
            roster_by_team[canonical_team(row.get('team'))].append(row)
    cohort_by_gsis = {clean_id(p.get('gsis_id')): str(p['player_id']) for p in exact}
    drafts = defaultdict(list)
    combines = defaultdict(list)
    for row in data.get('draft_picks', []):
        if clean_id(row.get('gsis_id')):
            drafts[clean_id(row['gsis_id'])].append(row)
    for row in data.get('combine', []):
        if clean_id(row.get('pfr_id')):
            combines[clean_id(row['pfr_id'])].append(row)

    output = {}
    for player in players:
        player_id, gsis = str(player['player_id']), clean_id(player.get('gsis_id'))
        pfr = pfr_by_gsis.get(gsis)
        profile = {'player_id': player_id, 'name': player['name'], 'position': player['position'],
            'gsis_id': gsis, 'identity_status': 'exact_gsis' if gsis else 'no_exact_gsis_history_not_joined',
            'pfr_id': pfr, 'pfr_crosswalk_status': 'ambiguous' if gsis in ambiguous else 'matched' if pfr else 'missing',
            'identity_sources': sources(cross.get(gsis, [])), 'seasons': {}, 'changes': {}}
        for season in seasons:
            rows = sorted(by_player_season.get((gsis, season), []), key=lambda r: r['week']) if gsis else []
            s = {'observed_box_score_weeks': [r['week'] for r in rows], 'n_observed_box_score_rows': len(rows),
                 'teams': sorted({canonical_team(r['team']) for r in rows}),
                 'workload_and_output_counts': {f: summed(rows, f) for f in COUNTS},
                 'execution_rates': {name: ratio(rows, a, b) for name, (a, b) in RATES.items()},
                 'weekly_sources': sources(rows), 'team_opportunity_shares': {}}
            for field in ('targets', 'carries', 'attempts'):
                paired = []
                for row in rows:
                    total = team_den[(season, canonical_team(row['team']), row['week'])][field]
                    # Missing team component cannot silently lower the denominator.
                    denominator = total['value'] if not total['missing_rows'] else None
                    paired.append({'own': row.get(field), 'team': denominator})
                s['team_opportunity_shares'][field] = ratio(paired, 'own', 'team')
                s['team_opportunity_shares'][field]['scope'] = 'Same team-weeks as player box-score rows; all reported players/unattributed rows enter team denominator. Not full-season participation share.'
            own_snaps = snap_players.get((pfr, season), []) if pfr else []
            pairs = [{'own': r.get('offense_snaps'), 'team': total_snaps.get((r['game_id'], canonical_team(r['team'])))} for r in own_snaps]
            s['snaps'] = {'n_observed_rows': len(own_snaps), 'offense_snaps': summed(own_snaps, 'offense_snaps'),
                'offense_share': ratio(pairs, 'own', 'team'),
                'source_weekly_shares': [{'week': r['week'], 'team': r['team'], 'offense_snaps': r.get('offense_snaps'), 'offense_pct': r.get('offense_pct')} for r in sorted(own_snaps, key=lambda x: x['week'])],
                'denominator_method': 'Team offensive snaps from a same-game player reported at 100%; if none, denominator remains missing. Source percentage rounding remains a limitation.',
                'sources': sources(own_snaps), 'route_share': None, 'route_share_status': 'Snap share is not route share; routes not in this source.'}
            s['ngs'] = {}
            for kind, spec in NGS.items():
                nr = ngs[kind].get((gsis, season), []) if gsis else []
                native = [r for r in nr if r['week'] == 0]
                week_rows = [r for r in nr if r['week'] > 0]
                weekly_field = {'passing': 'attempts', 'receiving': 'targets', 'rushing': 'carries'}[kind]
                minimum = ngs_coverage[kind]['observed_minimum_weekly_exposure']
                candidate_weeks = {r['week'] for r in rows if minimum is not None and r.get(weekly_field) is not None and r[weekly_field] >= minimum}
                published_weeks = {r['week'] for r in week_rows}
                item = {'native_season_summary': None, 'n_published_weekly_rows': len(week_rows),
                    'published_weeks': sorted(r['week'] for r in week_rows),
                    'weekly_exposure_sum': summed(week_rows, spec['counts'][0]),
                    'coverage_vs_observed_box_score_weeks': {'ngs_weeks': len({r['week'] for r in week_rows}), 'box_score_weeks': len(rows)},
                    'eligibility_audit': {'empirical_population_minimum': minimum, 'weekly_box_exposure_field': weekly_field,
                        'box_score_weeks_at_or_above_empirical_minimum': sorted(candidate_weeks),
                        'such_weeks_without_ngs_row': sorted(candidate_weeks - published_weeks),
                        'published_weeks_without_such_box_score_row': sorted(published_weeks - candidate_weeks),
                        'interpretation': 'Tests only a possible exposure filter, not full NGS eligibility. Position, tracking coverage and source differences can also explain missingness.'},
                    'sources': sources(nr), 'missing_policy': 'Not published/qualified is unknown; never zero. Week 0 is a separate native season summary, never added to weekly data.'}
                if native:
                    r = native[0]
                    summary = {'team_label': r.get('team'), 'counts': {f: r.get(f) for f in spec['counts']},
                        'metrics': {f: {'value': r.get(f), 'unit': NGS_UNITS[f], 'numerator': None, 'exact_metric_denominator': None,
                            'n_source_summary_rows': 1, 'aggregation': 'native_provider_season_summary_not_reweighted'} for f in spec['fields']}}
                    checks = {}
                    for field in spec['counts']:
                        box = summed(rows, NGS_COUNT_CROSSCHECK[field])
                        actual = numeric(r.get(field))
                        comparable = actual is not None and box['value'] is not None and not box['missing_rows']
                        checks[field] = {'ngs_native': actual, 'weekly_box_sum': box['value'],
                            'box_field': NGS_COUNT_CROSSCHECK[field], 'n_box_rows': box['n_observed_rows'],
                            'difference': actual - box['value'] if comparable else None,
                            'status': 'agrees' if comparable and math.isclose(actual, box['value'], abs_tol=1e-7) else 'source_difference_requires_review' if comparable else 'not_comparable'}
                    summary['box_score_count_reconciliation'] = checks
                    if kind == 'rushing':
                        total, rate = numeric(r.get('rush_yards_over_expected')), numeric(r.get('rush_yards_over_expected_per_att'))
                        implied = total / rate if total is not None and rate not in (None, 0) else None
                        if implied is not None and math.isclose(implied, round(implied), abs_tol=1e-5) and 0 < round(implied) <= r['rush_attempts']:
                            summary['inferred_tracking_attempts_from_ryoe_identity'] = round(implied)
                        else:
                            summary['inferred_tracking_attempts_from_ryoe_identity'] = None
                        summary['denominator_caveat'] = 'RYOE total/per-attempt ratio can reveal a smaller tracking-qualified count. It is inferred, not a provider count field; all-carry weighting is not justified.'
                    item['native_season_summary'] = summary
                s['ngs'][kind] = item
            s['ftn_charting'] = {}
            for role, flags in [('passing', ('is_interception_worthy', 'is_throw_away', 'is_play_action', 'is_screen_pass', 'is_rpo')),
                                ('receiving', ('is_catchable_ball', 'is_contested_ball', 'is_created_reception', 'is_drop')),
                                ('rushing', ('is_qb_sneak',))]:
                joined = chart_player.get((gsis, season, role), []) if gsis else []
                measures = {}
                for flag in flags:
                    values = []
                    for _, row in joined:
                        v = row.get(flag)
                        if v is not None and not isinstance(v, bool):
                            raise ValidationError('FTN charting flag is not boolean: ' + flag)
                        values.append({'flag': int(v) if v is not None else None, 'charted_eligible_play': 1})
                    measures[flag] = ratio(values, 'flag', 'charted_eligible_play')
                pull_dates = sorted({r.get('date_pulled') for _, r in joined if r.get('date_pulled')})
                s['ftn_charting'][role] = {'n_joined_eligible_plays': len(joined), 'n_games': len({p['game_id'] for p, _ in joined}),
                    'rates': measures, 'sources': sources([r for pair in joined for r in pair]),
                    'source_date_pulled_range': [pull_dates[0], pull_dates[-1]] if pull_dates else None,
                    'date_pulled_interpretation': 'When nflverse pulled FTN, not verified first public availability.',
                    'meaning': 'FTN manual labels on exact matched PBP plays, conditioned on eligible passing attempts, targeted passes, or non-kneel rushes. Not verified physical skill or forecast.'}
            profile['seasons'][str(season)] = s
        for before, after in zip(seasons, seasons[1:]):
            left = by_player_season.get((gsis, before), []) if gsis else []
            right = by_player_season.get((gsis, after), []) if gsis else []
            profile['changes'][str(before) + '_to_' + str(after)] = {
                role: decompose(left, right, value, exposure) for role, value, exposure in
                [('passing', 'passing_yards', 'attempts'), ('rushing', 'rushing_yards', 'carries'), ('receiving', 'receiving_yards', 'targets')]}
        d = drafts.get(gsis, []) if gsis else []
        cb = combines.get(pfr, []) if pfr else []
        profile['draft_evidence'] = {'records': [{k: r.get(k) for k in ('season', 'round', 'pick', 'team', 'college', 'age', 'position')} for r in d],
            'sources': sources(d), 'status': 'observed' if d else 'no_exact_matched_record_not_proof_undrafted',
            'excluded_fields': 'All career outcomes, career AV, honors and final playing year are excluded from draft-time evidence.'}
        profile['combine_evidence'] = {'records': [{k: r.get(k) for k in ('season', 'pos', 'school', 'ht', 'wt', 'forty', 'bench', 'vertical', 'broad_jump', 'cone', 'shuttle')} for r in cb],
            'sources': sources(cb), 'status': 'observed' if cb else 'no_exact_matched_record_not_zero_athleticism',
            'caveat': 'Historical combine tests do not measure current physical capability.'}
        own_roster = roster_by_player.get(gsis, []) if gsis else []
        teams = sorted({canonical_team(r['team']) for r in own_roster})
        competitors = []
        for team in teams:
            for r in roster_by_team[team]:
                if gid(r) == gsis:
                    continue
                competitors.append({'gsis_id': gid(r), 'player_id': cohort_by_gsis.get(gid(r)),
                    'name': r.get('full_name'), 'position': r.get('position'), 'team': team,
                    'same_position': r.get('position') == player['position'], 'status': r.get('status'),
                    'roster_week': r.get('week'), 'season': r['season'],
                    'observed_at': r['_provenance']['observed_at'], 'available_at': r['_provenance']['available_at']})
        profile['current_team_context'] = {'season': current_season, 'roster_teams': teams,
            'status': 'observed_season_roster_membership' if own_roster else 'no_exact_current_roster_match',
            'own_roster': [{k: r.get(k) for k in ('team', 'week', 'status', 'status_description_abbr', 'depth_chart_position')} for r in own_roster],
            'other_offensive_roster_members': competitors, 'sources': sources(own_roster + [r for t in teams for r in roster_by_team[t]]),
            'caveat': 'Season-roster snapshot may retain inactive/released players; status and observed dates preserved. Membership is not a role, depth ranking, health clearance, or allocation forecast.'}
        output[player_id] = profile
    all_sources = sources([r for rows in data.values() for r in rows])
    return {'schema_version': 1, 'built_at': datetime.now(timezone.utc).isoformat(), 'as_of': cutoff,
        'historical_seasons': list(seasons), 'current_roster_season': current_season, 'players': output,
        'sources': all_sources, 'coverage': {'players': len(output),
            'with_exact_gsis': len(exact), 'with_weekly_history': sum(any(s['n_observed_box_score_rows'] for s in p['seasons'].values()) for p in output.values()),
            'with_snap_history': sum(any(s['snaps']['n_observed_rows'] for s in p['seasons'].values()) for p in output.values()),
            'with_ngs_native_season_summary': sum(any(s['ngs'][k]['native_season_summary'] for s in p['seasons'].values() for k in NGS) for p in output.values()),
            'with_ftn_player_context': sum(any(s['ftn_charting'][k]['n_joined_eligible_plays'] for s in p['seasons'].values() for k in NGS) for p in output.values()),
            'with_draft_evidence': sum(bool(p['draft_evidence']['records']) for p in output.values()),
            'with_combine_evidence': sum(bool(p['combine_evidence']['records']) for p in output.values()),
            'with_current_roster': sum(bool(p['current_team_context']['roster_teams']) for p in output.values()),
            'ngs_population': ngs_coverage, 'ftn_join': dict(ftn_audit), 'ambiguous_gsis_pfr_crosswalks': ambiguous,
            'team_games_without_full_snap_witness': len(snap_team) - len(total_snaps)},
        'interpretation': 'Descriptive evidence for player-world scenarios. No predictive validation, skill/age effect, future role probability, championship probability or new forecast is estimated.',
        'historical_vintage_caveat': 'Revised historical source files were first observed in 2026. These are current historical descriptions, not valid pre-decision 2023–25 inputs. Dates are never backfilled from the season or FTN date_pulled.'}


def build(*, data_root='.moneyball', cutoff=None, output=None):
    root = Path(data_root)
    cutoff = timestamp(cutoff)
    cohort_path = root / 'draft/top400-cohort.json'
    cohort = json.loads(cohort_path.read_text())
    players = cohort['players']
    if timestamp(cohort['information_cutoff']) > cutoff:
        raise ValidationError('Cohort identity/selection comes from after cutoff')
    w = Warehouse(root / 'lab')
    data = {}
    for dataset in DATASETS:
        years = [2023, 2024, 2025, 2026] if dataset == 'rosters' else [2025] if dataset in ('ftn_charting', 'pbp') else None if dataset in ('players', 'draft_picks', 'combine') else [2023, 2024, 2025]
        data[dataset] = w.query(dataset, cutoff=cutoff, seasons=years, purpose='exploratory', source_id='nflverse_' + dataset)
    result = build_context(players, data, cutoff=cutoff)
    result['cohort_sha256'] = hashlib.sha256(cohort_path.read_bytes()).hexdigest()
    result['source_registry'] = [{k: s.get(k) for k in ('source_id', 'origin', 'url_template', 'license_status', 'verification_url')}
        for s in w.source_registry() if s['source_id'] in {x['source_id'] for x in result['sources']}]
    destination = Path(output) if output else root / 'research/dossier400-player-context.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + '.tmp')
    temp.write_text(canonical(result) + '\n')
    temp.replace(destination)
    return {'path': str(destination.resolve()), 'coverage': result['coverage'], 'as_of': cutoff}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', default='.moneyball')
    parser.add_argument('--cutoff')
    parser.add_argument('--output')
    args = parser.parse_args()
    print(json.dumps(build(data_root=args.data_root, cutoff=args.cutoff, output=args.output), indent=2))


if __name__ == '__main__':
    main()
