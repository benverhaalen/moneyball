"""Exposure-conditioned rare event counts; no injury or exposure imputation.

Retrospective walk-forward diagnostics do not establish historical knowability.
All counts and opportunities must be observed explicitly. See the preregistration
in .moneyball/research/dossier400-rare-stat-methods.md.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from .warehouse import Warehouse, canonical, leakage_check, timestamp

COMPONENTS = {
    'pass_int': ('passing_interceptions', 'attempts', 'pass_att'),
    'rush_fum_lost': ('rushing_fumbles_lost', 'carries', 'rush_att'),
    'rec_fum_lost': ('receiving_fumbles_lost', 'receptions', 'rec'),
    'sack_fum_lost': ('sack_fumbles_lost', 'sacks_suffered', 'pass_sack'),
}
POSITIONS = ('QB', 'RB', 'WR', 'TE')
SPECIFICATIONS = [{'name': 'pool', 'history': 'pool', 'k': 0},
                  {'name': 'last_raw', 'history': 'last', 'k': 0},
                  {'name': 'available_history_raw', 'history': 'all', 'k': 0}] + [
    {'name': f'{h}_k{k}', 'history': h, 'k': k}
    for h in ('last', 'all', 'half1', 'half3') for k in (20, 100, 500, 2000)]


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def aggregate_history(rows, *, cutoff):
    """Aggregate complete observed component risk sets without inferring absence."""
    rows = list(rows)
    leakage_check(rows, cutoff, raise_on_error=True)
    groups, seen, audit = {}, set(), Counter()
    for row in rows:
        audit['input_weekly_rows'] += 1
        if row.get('season_type') != 'REG':
            audit['nonregular_rows_excluded'] += 1
            continue
        if row.get('position') not in POSITIONS:
            audit['other_position_rows_excluded'] += 1
            continue
        pid = str(row.get('player_id') or '')
        if not pid or pid in ('NA', 'None'):
            raise ValueError('Missing player identity')
        key = (pid, row['season'], row['week'])
        if key in seen:
            raise ValueError('Duplicate player/season/week history')
        seen.add(key)
        if not isinstance(row['season'], int) or not isinstance(row['week'], int):
            raise ValueError('Season and week must be integers')
        g = groups.setdefault(key[:2], {'player_id': pid, 'season': row['season'],
            'positions': Counter(), 'observed_rows': 0,
            'components': {c: {'events': 0, 'exposure': 0, 'missing_rows': 0, 'observed_rows': 0}
                           for c in COMPONENTS}})
        g['observed_rows'] += 1
        g['positions'][row['position']] += 1
        for c, (event, exposure, _) in COMPONENTS.items():
            y, n = row.get(event), row.get(exposure)
            if y is None or n is None:
                g['components'][c]['missing_rows'] += 1
                continue
            if not _number(y) or not _number(n) or int(y) != y or int(n) != n or not 0 <= y <= n:
                raise ValueError(f'Invalid count/exposure for {key} {c}: {y}/{n}')
            for name, v in (('events', y), ('exposure', n), ('observed_rows', 1)):
                g['components'][c][name] += v
        total = row.get('fumbles_lost_total')
        fs = [row.get(COMPONENTS[c][0]) for c in COMPONENTS if c != 'pass_int']
        if total is None or any(v is None for v in fs):
            audit['fumble_reconciliation_missing_rows'] += 1
        else:
            if not _number(total) or total < sum(fs):
                raise ValueError('Total fumbles lost contradict component sum')
            audit['fumble_reconciliation_rows'] += 1
            audit['total_fumbles_lost'] += total
            audit['modeled_component_fumbles_lost'] += sum(fs)
            audit['unmodeled_other_fumbles_lost'] += total - sum(fs)
    panel = []
    for g in groups.values():
        g['position'] = sorted(g.pop('positions').items(), key=lambda v: (-v[1], v[0]))[0][0]
        panel.append(g)
    panel.sort(key=lambda r: (r['season'], r['player_id']))
    audit['player_seasons'] = len(panel)
    audit['distinct_players'] = len({r['player_id'] for r in panel})
    return panel, dict(audit)


def _usable(r, component):
    x = r['components'][component]
    return not x['missing_rows'] and x['exposure'] > 0


def fit_rate_model(panel, component, *, before_season, specification):
    """Fit only strictly earlier complete player-season component observations."""
    if component not in COMPONENTS or specification not in SPECIFICATIONS:
        raise ValueError('Unknown component or preregistered specification')
    pools = defaultdict(lambda: {'events': 0, 'exposure': 0, 'player_seasons': 0})
    personal = defaultdict(lambda: {'events': 0., 'exposure': 0., 'raw_exposure': 0,
                                    'player_seasons': 0, 'seasons': []})
    for r in panel:
        if r['season'] >= before_season or not _usable(r, component):
            continue
        x = r['components'][component]
        for pos in (r['position'], 'ALL'):
            pools[pos]['events'] += x['events']
            pools[pos]['exposure'] += x['exposure']
            pools[pos]['player_seasons'] += 1
        lag, h = before_season - 1 - r['season'], specification['history']
        weight = (1 if lag == 0 else 0) if h == 'last' else (
            2 ** (-lag / int(h[4:])) if h.startswith('half') else 1)
        if h == 'pool' or weight == 0:
            continue
        q = personal[r['player_id']]
        q['events'] += weight * x['events']
        q['exposure'] += weight * x['exposure']
        q['raw_exposure'] += x['exposure']
        q['player_seasons'] += 1
        q['seasons'].append(r['season'])
    if not pools.get('ALL', {}).get('exposure'):
        raise ValueError('No training exposures for ' + component)
    return {'component': component, 'before_season': before_season,
            'specification': dict(specification), 'pools': dict(pools), 'personal': dict(personal)}


def rate_for(model, player_id, position):
    if position not in POSITIONS:
        raise ValueError('Position must be explicit QB/RB/WR/TE')
    pool = model['pools'].get(position) or model['pools']['ALL']
    mu = (pool['events'] + .5) / (pool['exposure'] + 1)
    p = model['personal'].get(str(player_id))
    k = model['specification']['k']
    if not p or not p['exposure'] or model['specification']['history'] == 'pool':
        value, source = mu, 'position_pool' if position in model['pools'] else 'all_position_fallback'
    else:
        value = (p['events'] + k * mu) / (p['exposure'] + k)
        source = model['specification']['name']
    return {'rate': value, 'method': source, 'selected_specification': model['specification']['name'],
            'position': position, 'pool_events': pool['events'], 'pool_exposure': pool['exposure'],
            'personal_weighted_events': p['events'] if p else 0,
            'personal_weighted_exposure': p['exposure'] if p else 0,
            'personal_observed_exposure': p['raw_exposure'] if p else 0,
            'history_years': p['seasons'] if p else [], 'rate_confidence_interval': None,
            'uncertainty': 'Point regularizer only; exposure trials and repeated seasons are not independent uncertainty units.'}


def evaluate(model, panel, season):
    cases, errors, absolute, logs = [], [], [], []
    component = model['component']
    for r in panel:
        if r['season'] != season or not _usable(r, component):
            continue
        y, n = (r['components'][component][k] for k in ('events', 'exposure'))
        p = rate_for(model, r['player_id'], r['position'])['rate']
        pred, clipped = n * p, min(1 - 1e-9, max(1e-9, p))
        errors.append((pred - y) ** 2)
        absolute.append(abs(pred - y))
        logs.append(-y * math.log(clipped) - (n-y) * math.log1p(-clipped))
        cases.append({'player_id': r['player_id'], 'position': r['position'], 'season': season,
                      'events': y, 'exposure': n, 'prediction': pred})
    if not cases:
        raise ValueError('No evaluation risk set')
    n = len(cases)
    exposure = sum(r['exposure'] for r in cases)
    observed, predicted = sum(r['events'] for r in cases), sum(r['prediction'] for r in cases)
    return {'season': season, 'player_seasons': n, 'players': len({r['player_id'] for r in cases}),
            'exposures': exposure, 'events': observed, 'predicted_events': predicted,
            'mse': sum(errors)/n, 'mae': sum(absolute)/n, 'mean_error': (predicted-observed)/n,
            'marginal_log_loss_per_exposure': sum(logs)/exposure, 'cases': cases}


def _summary(folds):
    cases = [r for f in folds for r in f['cases']]
    n = len(cases)
    return {'mse': sum((r['prediction']-r['events'])**2 for r in cases)/n,
            'players': len({r['player_id'] for r in cases}), 'player_seasons': n,
            'exposures': sum(r['exposure'] for r in cases), 'events': sum(r['events'] for r in cases),
            'folds': [{k:v for k,v in f.items() if k != 'cases'} for f in folds]}


def select_development(panel, *, seasons=(2020, 2021, 2022, 2023, 2024)):
    if any(r['season'] >= 2025 for r in panel):
        raise ValueError('Reserved holdout must not enter development selection')
    result = {}
    for c in COMPONENTS:
        candidates = []
        for spec in SPECIFICATIONS:
            folds = [evaluate(fit_rate_model(panel, c, before_season=y, specification=spec), panel, y)
                     for y in seasons]
            candidates.append({'specification': spec, **_summary(folds)})
        baseline = candidates[0]
        eligible = []
        for cand in candidates[1:]:
            wins = sum(a['mse'] < b['mse'] for a,b in zip(cand['folds'], baseline['folds']))
            cand['better_development_seasons'] = wins
            cand['relative_mse_improvement'] = 1-cand['mse']/baseline['mse']
            if wins >= 3 and cand['relative_mse_improvement'] >= .01:
                eligible.append(cand)
        selected = min(eligible, key=lambda r:r['mse']) if eligible else baseline
        result[c] = {'selected': selected['specification'], 'selected_development_mse': selected['mse'],
                     'baseline_development_mse': baseline['mse'], 'candidates': candidates}
    return result


def _write(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(canonical(obj)+'\n')
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def build(root='.moneyball', *, cutoff=None):
    root, cutoff = Path(root), timestamp(cutoff)
    path = root/'lab'/'derived'/'rare-stats-2026.json'
    if path.exists():
        old = json.loads(path.read_text())
        if old.get('status') == 'complete':
            return old  # Do not silently reuse the final holdout after model changes.
        raise ValueError('An incomplete selection receipt exists; inspect before repeating the reserved evaluation')
    warehouse = Warehouse(root/'lab')
    pre = warehouse.query('weekly', cutoff=cutoff, seasons=list(range(2018,2025)), positions=POSITIONS)
    panel, audit = aggregate_history(pre, cutoff=cutoff)
    selected = select_development(panel)
    frozen = {'status': 'selection_frozen_before_holdout', 'created_at': time.time(),
              'cutoff': cutoff, 'development_panel_sha256': hashlib.sha256(canonical(panel).encode()).hexdigest(),
              'development_audit': audit, 'development': selected}
    _write(path, frozen)
    # The held-out outcomes are first loaded only after selected methods are on disk.
    held_rows = warehouse.query('weekly', cutoff=cutoff, seasons=[2025], positions=POSITIONS)
    held, held_audit = aggregate_history(held_rows, cutoff=cutoff)
    deployed, tests = {}, {}
    for c, choice in selected.items():
        spec = choice['selected']
        own = evaluate(fit_rate_model(panel,c,before_season=2025,specification=spec), held, 2025)
        base = evaluate(fit_rate_model(panel,c,before_season=2025,specification=SPECIFICATIONS[0]), held, 2025)
        fails = own['mse'] > 1.05 * base['mse']
        final_spec = SPECIFICATIONS[0] if fails else spec
        tests[c] = {'selected': own, 'pool': base, 'relative_mse_improvement': 1-own['mse']/base['mse'],
                    'deployment_guard_failed': fails, 'deployed_specification': final_spec,
                    'confidence': 'Limited: one held-out season, repeated players, conditional realized exposure, unverified historical vintages.'}
        deployed[c] = fit_rate_model(panel+held,c,before_season=2026,specification=final_spec)
    provenance = {canonical(r['_provenance']): r['_provenance'] for r in pre+held_rows}
    report = {**frozen, 'status': 'complete', 'completed_at': time.time(), 'forecast_season': 2026,
              'schema_version': 1, 'method_code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'selection_receipt_sha256': hashlib.sha256(canonical(frozen).encode()).hexdigest(),
              'holdout_audit': held_audit, 'holdout': tests, 'models': deployed,
              'component_definitions': COMPONENTS, 'provenance': list(provenance.values()),
              'comparison_counts': {'candidate_specifications_per_component':len(SPECIFICATIONS),
                   'components':len(COMPONENTS), 'development_specifications':len(SPECIFICATIONS)*len(COMPONENTS),
                   'development_fold_fits':len(SPECIFICATIONS)*len(COMPONENTS)*5,
                   'reserved_selected_vs_pool_comparisons':4},
              'purpose':'Retrospective rate diagnostic and explicit-exposure 2026 component imputation; not a point-in-time or end-to-end preseason backtest.',
              'limitations':['Realized exposures and observed positions condition the retrospective evaluation.',
                  'No dated professional forecast comparator acquired for these historical component targets.',
                  'Offensive fumble components omit observed return/miscellaneous losses.',
                  'Available history starts in 2018; no-row is not injury or a zero-rate observation.',
                  'Rate estimates are point regularizers, not calibrated predictive distributions or confidence intervals.',
                  'Do not multiply a threshold median by a mean event rate and call the result an expected count.',
                  'A supplied professional event forecast is preserved; no redundant injury discount is applied.']}
    _write(path, report)
    return report


def impute_components(artifact, player_id, position, exposures, existing_stats=None, *, exposure_kind='mean'):
    """Expected counts from explicit expected opportunities. Identity is GSIS ID.

Caller must align forecast horizons. Missing exposure is unknown, even for a
position where it is unusual. Existing professional event predictions win.
"""
    if artifact.get('status') != 'complete':
        raise ValueError('A completed, evaluated rate artifact is required')
    if exposure_kind != 'mean':
        raise ValueError('Expected event counts require mean exposure, not a prop threshold or median')
    existing, estimates = dict(existing_stats or {}), {}
    for c, (_, _, key) in COMPONENTS.items():
        rate = rate_for(artifact['models'][c], player_id, position)
        v = existing.get(c)
        n = exposures.get(key)
        if v is not None:
            if not _number(v) or v < 0: raise ValueError('Invalid supplied event forecast')
            estimates[c] = {'mean':v,'status':'supplied_forecast_preserved'}
        elif n is None:
            estimates[c] = {'mean':None,'status':'missing_exposure','required_exposure':key,'rate':rate}
        else:
            if not _number(n) or n < 0: raise ValueError('Invalid expected exposure')
            estimates[c] = {'mean': n*rate['rate'], 'status':'exposure_conditioned_imputation',
                            'exposure':n,'exposure_key':key,'rate':rate}
    fs = [estimates[c]['mean'] for c in COMPONENTS if c != 'pass_int']
    complete = existing.get('fum_lost')
    if complete is not None and (not _number(complete) or complete < 0):
        raise ValueError('Invalid supplied total-fumble forecast')
    return {'player_id':str(player_id),'identity_namespace':'GSIS','position':position,
            'forecast_season':artifact['forecast_season'],'components':estimates,
            'offensive_fumbles_lost_subtotal':sum(fs) if all(v is not None for v in fs) else None,
            'fum_lost':complete, 'total_fumbles_status':'supplied_forecast_preserved' if complete is not None else 'unmodeled_return_or_miscellaneous_losses',
            'uncertainty':'No calibrated player-count distribution or exposure uncertainty supplied; point imputation only.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default='.moneyball')
    args = parser.parse_args()
    result = build(args.data_dir)
    print(canonical({k:result[k] for k in ('status','comparison_counts','development_audit','holdout_audit')}))


if __name__ == '__main__':
    main()
