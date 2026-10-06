"""Compact loss-conscious reading view of an immutable player packet.

This is a navigation aid, never a replacement for the packet or its provenance.
It omits repeated source metadata and acquisition-rank overlays, not forecasts.
"""
from .dossier_swarm import brief_evidence
from .providers import EXPERIMENTAL_REFERENCE_SCORING, score_stats


def decision_brief(packet):
    brief = brief_evidence(packet)
    configured_scoring = (packet.get('league_configuration') or {}).get('scoring_settings')
    scoring = EXPERIMENTAL_REFERENCE_SCORING if configured_scoring is None else configured_scoring
    scoring_scope = 'experimental_reference_profile' if configured_scoring is None else 'observed_league_configuration'
    identity = packet.get('identity') or {}
    forecast_rows = []
    for provider, rows in brief['professional_forecasts'].items():
        for row in rows:
            original = row.get('stats') or {}
            fields = set(scoring) | {
                'games', 'gp', 'pass_att', 'pass_cmp', 'pass_sack',
                'rec_tgt', 'rush_att', 'pts_ppr'}
            forecast_rows.append({
                'source_key': row['source_key'], 'provider': provider,
                'stats': {k: v for k, v in original.items() if k in fields},
                'observed_core_subtotal': score_stats(original, scoring),
                'unreported_scoring_fields': sorted(set(scoring) - set(original)),
                'source_url': row.get('source_url'),
                'known_at': row.get('known_at'),
                'source_conditioning': row.get('source_conditioning') or row.get('conditioning'),
            })
    contracts = brief.get('contract_context') or {}
    providers = brief.get('provider_references') or {}
    history = brief.get('historical_player_context') or {}
    seasons = {}
    for year, data in (history.get('seasons') or {}).items():
        # All metric values/denominators/missingness in this already compact
        # derivative survive. The original remains accessible for provenance.
        seasons[year] = data
    market_rows = []
    for market in brief.get('market_component_ladders') or []:
        observations = []
        for observed in market.get('paired_observations') or []:
            observations.append({k: observed.get(k) for k in (
                'source_threshold', 'over_odds', 'under_odds', 'observed_at',
                'source_url', 'cdf_low', 'cdf_high', 'quote_type')})
        market_rows.append({
            'source_id': market.get('source_id'), 'kind': market.get('kind'),
            'statistic': market.get('statistic'), 'horizon': market.get('horizon'),
            'action_rules': market.get('action_rules'), 'paired_observations': observations,
            'alternate_line_count': market.get('alternate_line_count'),
            'packet_usage_gate': market.get('packet_usage_gate'),
        })
    weekly = []
    for row in (packet.get('professional_forecasts') or {}).get('weekly') or []:
        stats = row.get('stats') or {}
        weekly.append({
            **{k: row.get(k) for k in ('source_id', 'provider', 'source_url',
                'player_id', 'season', 'week', 'date', 'game_id', 'team',
                'opponent', 'provider_updated_at', 'source_conditioning',
                'conditioning', 'projection_completeness', 'missingness_caveat')},
            'provenance': row.get('_provenance'),
            'stats': {k: v for k, v in stats.items() if k in set(scoring) |
                      {'gp', 'games', 'pass_att', 'pass_cmp', 'rush_att', 'rec_tgt'}},
            'observed_core_subtotal': score_stats(stats, scoring),
            'unreported_scoring_fields': sorted(set(scoring) - set(stats)),
        })
    return {
        'schema_version': 1,
        'scoring_settings': scoring, 'scoring_scope': scoring_scope,
        'interpretation': (
            'Reading aid derived from immutable packet. Omitted provenance and '
            'historical contract duplicates remain in evidence/*.json. This is '
            'not a prediction model. Missing observations are not zero. Core '
            'subtotals use only applicable observed fields, not missing-as-zero '
            'imputation. Historical current-vintage summaries are not PIT features. '
            'Books are conditional thresholds, not means or calibrated CIs. '
            'Provider medians, downside/upside and injury incidence are distinct. '
            'All acquired forecasts are preserved here; no ranking decides utility.'),
        'identity': {k: identity.get(k) for k in (
            'player_id', 'name', 'team', 'position', 'birth_date', 'years_exp',
            'cohort_rank', 'adp', 'adp_type', 'adp_as_of', 'observed_name_aliases')},
        'professional_forecasts': forecast_rows,
        'weekly_professional_forecasts': {
            'count': len(weekly), 'rows': weekly,
            'original_path': 'evidence/professional_forecasts.json:weekly',
            'interpretation': 'Dated individual weekly rows, not season components. '
                'Check old-team/opponent and provider-update context. Do not '
                'subtract an independently generated weekly row from a season '
                'forecast or treat a stale team assignment as a current outlook.'},
        'provider_references': {
            'rank_preview': providers.get('rank_preview'),
            'acquisition_references': [
                {k: row.get(k) for k in ('originating_platform', 'adp', 'format',
                                        'known_at', 'source_url', 'sample_size')}
                for row in providers.get('platform_adp') or []],
            'other_reference_counts': {k: len(v) for k, v in providers.items()
                                       if isinstance(v, list) and k != 'rank_preview'},
        },
        'historical_player_context': {
            'seasons': seasons,
            'draft_evidence': history.get('draft_evidence'),
            'combine_evidence': history.get('combine_evidence'),
            'current_team_context': history.get('current_team_context'),
        },
        'contract_context': {k: contracts.get(k) for k in (
            'reported_active_contracts', 'annual_rows_2026_2028', 'source_url',
            'money_unit', 'observed_at', 'available_at', 'identity_match',
            'discarded_otc_identity_conflicts', 'fifth_year_option_status',
            'free_agency_year', 'free_agency_type', 'remaining_guaranteed_cash',
            'guarantee_vesting_triggers', 'void_years', 'interpretation')},
        'market_component_ladders': market_rows,
        'weekly_fantasy_score_quotes': brief.get('weekly_fantasy_score_quotes'),
        'news': brief.get('news'),
        'observed_player_metadata': brief.get('observed_player_metadata'),
        'coverage': brief.get('coverage'),
        'quarantined_evidence': brief.get('quarantined_evidence'),
    }
