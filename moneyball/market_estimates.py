"""Source-separated experimental season estimates, with an explicit prior grid.

No model wins by fitting today's quotes. Every declared sensitivity is retained;
numerical fit is not validation of the forecast, its injury model or its tails.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import time

from .market_prior import Prior, condition_prior
from .store import Store
from .warehouse import canonical, timestamp


CORE_SCORING = {'pass_yd': .04, 'pass_td': 4., 'pass_int': -1.,
                'rush_yd': .1, 'rush_td': 6., 'rec': 1., 'rec_yd': .1, 'rec_td': 6.}
DISPERSIONS = (.2, .4, .7)
ZERO_MASSES = (0., .15)


def source_anchors(rows, *, cutoff):
    output = {}
    for row in rows:
        if timestamp(row['known_at']) > cutoff:
            raise ValueError('Projection acquired after decision cutoff')
        if row.get('season') != 2026 or row.get('kind') != 'statistical_projection_components':
            raise ValueError('Wrong projection target')
        pid = row.get('player_id')
        if not pid:
            continue
        if pid in output:
            raise ValueError('Ambiguous duplicate projection identity')
        output[pid] = row
    return output


def component_subtotal(stats, scoring):
    """Do not label a subtotal a complete fantasy score or fill absent stats."""
    terms = {k: stats[k] * v for k, v in scoring.items() if stats.get(k) is not None}
    return {'subtotal': sum(terms.values()), 'terms': terms,
            'missing_core_components': sorted(set(scoring) - set(terms)),
            'not_in_this_subtotal': ['fumbles lost', 'two-point conversions', 'return/recovery and other rare scoring events'],
            'complete_league_projection': False}


def variants(statistic):
    for yard_family in ('gamma', 'lognormal'):
        for spread in DISPERSIONS:
            for atom in ZERO_MASSES:
                yield {'scenario_id': f'{yard_family}-d{spread:g}-zero{atom:g}',
                       'family': yard_family if statistic.endswith('_yd') else 'negative_binomial',
                       'dispersion': spread, 'zero_mass': atom}


def fit_ladder(ladder, anchor):
    if ladder['action_rules'].get('eligibility_condition_unknown'):
        return [], [{'reason': 'Participation/void target not verified; quote retained for comparison, not interpreted as an actual-season distribution'}]
    statistic = ladder['statistic']
    mean = anchor['stats'].get(statistic)
    if mean is None or mean <= 0:
        return [], [{'reason': 'Missing/nonpositive professional anchor', 'anchor_value': mean}]
    modes = ['paired_only']
    if ladder['alternate_lines']:
        modes.append('with_anchored_alternates')
    fits, failures, memo = [], [], {}
    for mode in modes:
        curve = ladder[mode]
        if not curve['points'] or not curve['feasible']:
            failures.append({'mode': mode, 'reason': 'Empty or inconsistent source curve; no repair performed'})
            continue
        for variant in variants(statistic):
            key = (mode, variant['family'], variant['dispersion'], variant['zero_mass'])
            try:
                if key not in memo:
                    memo[key] = condition_prior(curve, Prior(variant['family'], mean,
                                                            variant['dispersion'], variant['zero_mass']))
                fitted = memo[key]
                fits.append({'mode': mode, **variant, **fitted})
            except (ValueError, FloatingPointError) as error:
                failures.append({'mode': mode, **variant, 'reason': str(error)})
    return fits, failures


def build(root='.moneyball', *, alias=None):
    root = Path(root)
    cutoff = time.time()
    paths = {'cohort': root/'draft/top400-cohort.json',
             'ladders': root/'markets/derived/player-ladders.json',
             'anchor': root/'research/dossier400-professionals/fftoday-components.json'}
    raw = {name: path.read_bytes() for name, path in paths.items()}
    cohort, ladder_data = json.loads(raw['cohort']), json.loads(raw['ladders'])
    if ladder_data['information_cutoff'] > cutoff:
        raise ValueError('Market features after cutoff')
    if ladder_data['cohort_sha256'] != hashlib.sha256(raw['cohort']).hexdigest():
        raise ValueError('Market/cohort version mismatch')
    anchors = source_anchors(json.loads(raw['anchor']), cutoff=cutoff)
    league = Store(root).latest(Store(root).resolve_alias(alias))
    if not league or league['created'] > cutoff:
        raise ValueError('Exact league scoring not available at cutoff')
    actual_scoring = league['data']['league']['scoring_settings']
    scoring = {key: actual_scoring[key] for key in CORE_SCORING}
    if scoring != CORE_SCORING:
        raise ValueError('League scoring changed; review this explicitly scoped analysis before proceeding')
    ids = {p['player_id'] for p in cohort['players']}
    fitted_groups, failures, counts = [], [], Counter()
    started = time.time()
    for ladder in ladder_data['ladders']:
        if ladder['kind'] != 'sportsbook' or ladder['player_id'] not in ids:
            continue
        pid, stat, source = ladder['player_id'], ladder['statistic'], ladder['source_id']
        identity = {'player_id': pid, 'statistic': stat, 'source_id': source}
        if pid not in anchors:
            failures.append({**identity, 'reason': 'No matched FFToday anchor; no replacement silently chosen'})
            continue
        fits, failed = fit_ladder(ladder, anchors[pid])
        failures.extend({**identity, **failure} for failure in failed)
        if fits:
            fitted_groups.append({**identity, 'action_rules': ladder['action_rules'],
                                  'prior_anchor': {'source_id': anchors[pid]['source_id'],
                                      'value': anchors[pid]['stats'][stat],
                                      'known_at': anchors[pid]['known_at'],
                                      'raw_sha256': anchors[pid]['raw_sha256'],
                                      'mean_interpretation': 'Analyst assumption: displayed point is an expected total; provider does not explicitly establish this.',
                                      'conditioning': anchors[pid]['conditioning']},
                                  'models': fits})
            counts[source] += 1
        if sum(counts.values()) and sum(counts.values()) % 50 == 0:
            print(canonical({'fitted_source_stat_groups': sum(counts.values()),
                             'elapsed_seconds': round(time.time() - started, 1)}), flush=True)
    # Aggregation is only of component expectations. Joint predictive intervals
    # require additional modeling and are deliberately not produced here.
    replacements = defaultdict(dict)
    for group in fitted_groups:
        for model in group['models']:
            key = (group['player_id'], group['source_id'], model['mode'], model['scenario_id'])
            replacements[key][group['statistic']] = model['mean']
    player_outputs = {}
    for pid in ids:
        if pid not in anchors:
            player_outputs[pid] = {'status': 'missing_current_professional_anchor'}
            continue
        anchor = anchors[pid]
        source_scenarios = []
        for (player_id, source, mode, scenario), updated in replacements.items():
            if player_id != pid:
                continue
            scored = component_subtotal({**anchor['stats'], **updated}, scoring)
            base = component_subtotal(anchor['stats'], scoring)
            source_scenarios.append({'source_id': source, 'mode': mode, 'scenario_id': scenario,
                                     'action_condition': 'Market-updated components are conditional on this book action rule being satisfied. Probability of satisfying action is not estimated; this is not an unconditional league projection.',
                                     'market_updated_components': updated,
                                     'other_components': 'Retain this professional source; absent remains unknown.',
                                     'core_scoring': scored,
                                     'change_in_reported_component_subtotal': scored['subtotal'] - base['subtotal']})
        player_outputs[pid] = {'name': anchor['name'], 'position': anchor['position'],
                              'status': 'experimental_prior_sensitivity' if source_scenarios else 'professional_component_reference_only',
                              'professional_component_reference': component_subtotal(anchor['stats'], scoring),
                              'scenarios': source_scenarios,
                              'championship_odds': None,
                              'fantasy_joint_distribution': None,
                              'uncertainty_note': 'No validation-selected mixture or preferred scenario. Differences are model/source sensitivity, not confidence limits.'}
    report = {'schema_version': 1, 'created_at': time.time(), 'information_cutoff': cutoff,
              'inputs': {name: {'path': str(paths[name]), 'sha256': hashlib.sha256(body).hexdigest()}
                         for name, body in raw.items()},
              'scoring_subset': scoring,
              'league_snapshot_hash': league['hash'],
              'target': 'NFL2026 full regular season; includes week18, which may differ from league scoring weeks. Actual fantasy-playoff weekly allocation remains a separate model.',
              'assumptions': ['FFToday displayed components provisionally interpreted as expected actual-season totals; exposure semantics remain unverified.',
                              'Book-updated components assume action is satisfied; no-action probability is missing. The structural-zero sensitivity concerns zero output within that conditional target, not an estimated probability of never playing.',
                              'Prior width and structural-zero mass are declared sensitivity assumptions, not measured injury risk.',
                              'Each source and paired/alternate mode remains separate; correlated books are not independent samples.',
                              'Positive continuous yardage approximations exclude negative season totals; inappropriate low-volume cases must remain gated.',
                              'Marginal constraints do not establish joint coherence, injury weeks, team opportunity budgets, future ability or dynasty utility.'],
              'comparison_counts': {'attempted_model_records_including_failures': sum(len(x['models']) for x in fitted_groups) + len(failures),
                                    'successful_model_records': sum(len(x['models']) for x in fitted_groups),
                                    'failures_or_exclusions': len(failures),
                                    'source_stat_groups': dict(counts),
                                    'distinct_count_priors_per_curve': 6,
                                    'distinct_yardage_priors_per_curve': 12,
                                    'count_models_duplicated_across_yardage_family_scenario_labels': True,
                                    'historical_predictive_validation_trials': 0},
              'marginals': fitted_groups, 'failures': failures, 'players': player_outputs,
              'complete_calibrated_distributions': False, 'confidence_intervals': None}
    destination = root/'markets/derived/player-season-estimates.json'
    destination.write_text(canonical(report) + '\n')
    return {'path': str(destination), 'comparison_counts': report['comparison_counts'],
            'players_with_market_sensitivity': sum(bool(p.get('scenarios')) for p in player_outputs.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.moneyball')
    parser.add_argument('--league', dest='alias')
    args = parser.parse_args()
    print(canonical(build(args.root, alias=args.alias)))
