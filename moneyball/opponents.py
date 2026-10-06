"""Prequential choice forecasts for a small, fixed draft room.

ADP is demand evidence only. The five expert utilities and pooling constants
are transparent scenario choices, not estimates fitted to historical drafts.
The operational weights use approximate empirical-Bayes partial pooling;
they are not calibrated credible probabilities of a manager's true strategy.
"""
from collections import Counter
import hashlib
import json
import math
import random

from .simulation import keyed_seed, positions, select_lineup

EXPERTS = ('market', 'market_broad', 'lineup', 'qb_need', 'youth')
DEFAULTS = {'prior': [0.35, 0.20, 0.25, 0.10, 0.10], 'local_pooling_mass': 8.0,
            'shared_pooling_mass': 24.0, 'likelihood_temperature': 0.35,
            'surprise_probability': 0.03, 'teams': 12, 'rounds': 28}


def _owner(pick, teams=12):
    rnd, offset = divmod(pick - 1, teams)
    return offset + 1 if rnd % 2 == 0 else teams - offset


def _normalized(values):
    total = math.fsum(values)
    if total <= 0 or not math.isfinite(total):
        raise ValueError('Invalid choice weights')
    return [v / total for v in values]


def _softmax(values):
    peak = max(values)
    return _normalized([math.exp(v - peak) for v in values])


def _pool(players):
    result = {}
    for player in players:
        pid = str(player.get('id', player.get('player_id')))
        if not positions(player):
            continue
        if pid in result or pid == 'None':
            raise ValueError('Duplicate or missing player ID')
        mean = float(player.get('mean') or 0)
        adp = player.get('adp')
        if not math.isfinite(mean) or (adp is not None and (not math.isfinite(float(adp)) or float(adp) <= 0)):
            raise ValueError('Invalid player mean or ADP')
        result[pid] = player
    if not result:
        raise ValueError('No eligible players')
    return result


def _fingerprint(pool):
    values = [(pid, sorted(positions(p)), p.get('mean'), p.get('adp'), p.get('age'))
              for pid, p in sorted(pool.items())]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class ChoiceEngine:
    """Cache roster-specific entry thresholds; never score 554 full lineups."""
    def __init__(self, players, surprise_probability=0.03):
        self.pool = _pool(players)
        self.ids = tuple(sorted(self.pool))
        self.values = {pid: float(p.get('mean') or 0) for pid, p in self.pool.items()}
        self.adp = {pid: float(p.get('adp') or 999) for pid, p in self.pool.items()}
        self.eligibility = {pid: tuple(sorted(positions(p))) for pid, p in self.pool.items()}
        self.age = {pid: max(-2.0, min(2.0, (26 - float(p.get('age') or 26)) / 4))
                    for pid, p in self.pool.items()}
        self._threshold_cache = {}
        self.surprise = surprise_probability
        if not 0 < self.surprise < 1:
            raise ValueError('Surprise probability must be strictly between zero and one')

    def _thresholds(self, roster):
        if roster in self._threshold_cache:
            return self._threshold_cache[roster]
        # The weighted legal-assignment objective has an entry threshold for
        # each eligibility set. A high-value probe identifies its replacement
        # cost; comparing a candidate mean against that cost is exact for
        # nonnegative means. Negative means use the explicit slow path below.
        before = sum(self.values[p] for p in select_lineup(roster, self.pool, self.values))
        high = 1 + sum(abs(v) for v in self.values.values())
        groups = set(self.eligibility.values())
        thresholds = {}
        for group in groups:
            dummy = '__moneyball_opponent_probe__'
            if dummy in self.pool:
                raise ValueError('Reserved player ID used')
            pp = dict(self.pool); vv = dict(self.values)
            pp[dummy] = {'fantasy_positions': list(group)}; vv[dummy] = high
            after = sum(vv[p] for p in select_lineup([*roster, dummy], pp, vv))
            thresholds[group] = high - (after - before)
        if len(self._threshold_cache) >= 1024:
            self._threshold_cache.clear()
        self._threshold_cache[roster] = thresholds
        return thresholds

    def probabilities(self, available, roster, expert=None):
        ids = tuple(sorted(str(p) for p in available))
        if len(ids) != len(set(ids)) or any(p not in self.pool for p in ids):
            raise ValueError('Invalid available player universe')
        if not ids:
            raise ValueError('No available choice')
        if set(ids).intersection(roster):
            raise ValueError('Owned player remains in available choices')
        roster = tuple(sorted(roster))
        best_adp = min(self.adp[p] for p in ids)
        qbs = sum('QB' in self.eligibility[p] for p in roster)
        needed = EXPERTS if expert is None else (expert,)
        if any(e not in EXPERTS for e in needed):
            raise ValueError('Unknown choice expert')
        thresholds = self._thresholds(roster) if 'lineup' in needed else None
        result = {}
        for name in needed:
            utilities = []
            for pid in ids:
                gap = self.adp[pid] - best_adp
                if name == 'market':
                    utility = -gap / 8
                elif name == 'market_broad':
                    utility = -gap / 20
                elif name == 'lineup':
                    gain = max(0.0, self.values[pid] - thresholds[self.eligibility[pid]])
                    if self.values[pid] < 0 or any(self.values[p] < 0 for p in roster):
                        before = sum(self.values[p] for p in select_lineup(roster, self.pool, self.values))
                        gain = sum(self.values[p] for p in select_lineup([*roster, pid], self.pool, self.values)) - before
                    utility = gain / 3 - gap / 80
                elif name == 'qb_need':
                    bonus = 2.0 if qbs < 2 else -1.5 if qbs >= 3 else 0.0
                    utility = -gap / 12 + (bonus if 'QB' in self.eligibility[pid] else 0)
                else:
                    utility = -gap / 16 + self.age[pid]
                utilities.append(utility)
            base = _softmax(utilities)
            result[name] = {pid: (1 - self.surprise) * p + self.surprise / len(ids)
                            for pid, p in zip(ids, base)}
        return result[expert] if expert is not None else result


def _weights(model, slot):
    key = str(slot)
    manager = model['managers'][key]
    settings = model['settings']
    prior = settings['prior']
    # Leave-own-manager-out pooling prevents counting an observation both as
    # population evidence and individual evidence in its own prediction.
    others = [m for s, m in model['managers'].items() if s not in (key, str(model['our_slot']))]
    shared = _normalized([prior[k] * settings['shared_pooling_mass'] +
                          sum(m['responsibility_sum'][k] for m in others)
                          for k in range(len(EXPERTS))])
    raw = _softmax([math.log(shared[k]) + settings['likelihood_temperature'] * manager['log_likelihood'][k]
                    for k in range(len(EXPERTS))])
    n = manager['observed_picks']
    local_weight = n / (n + settings['local_pooling_mass'])
    pooled = [(1 - local_weight) * shared[k] + local_weight * raw[k] for k in range(len(EXPERTS))]
    return {'shared_prior': dict(zip(EXPERTS, shared)),
            'tempered_local_posterior': dict(zip(EXPERTS, raw)),
            'predictive_weights': dict(zip(EXPERTS, pooled)), 'local_weight': local_weight}


def _new_model(pool, our_slot, settings):
    if not 1 <= our_slot <= settings['teams']:
        raise ValueError('Invalid own draft slot')
    return {'version': 1, 'our_slot': our_slot, 'settings': settings,
            'player_fingerprint': _fingerprint(pool), 'observed_picks': [],
            'available': sorted(pool), 'rosters': {str(s): [] for s in range(1, settings['teams'] + 1)},
            'managers': {str(s): {'observed_picks': 0, 'log_likelihood': [0.] * len(EXPERTS),
                                'responsibility_sum': [0.] * len(EXPERTS)}
                         for s in range(1, settings['teams'] + 1)},
            'prequential': [], 'confidence': 'uncalibrated behavioral scenarios; weights are not confidence intervals',
            'pooling_method': 'tempered finite-expert likelihood with leave-manager-out empirical pooling and explicit local shrinkage'}


def fit(players, observed_picks=(), our_slot=5, settings=None):
    """Replay a contiguous actual prefix, scoring BEFORE updating each choice.

    Same supplied player snapshot is used throughout. Caller must enforce its
    availability time; replaying today's ADP against an old draft is not a
    point-in-time validation. Our own picks change availability/rosters but
    never train the opponent model or its shared population prior.
    """
    cfg = {**DEFAULTS, **(settings or {})}
    if cfg['teams'] != 12 or cfg['rounds'] != 28:
        raise ValueError('This adapter supports the experimental 12-team, 28-round snake fixture only')
    cfg['prior'] = _normalized(list(cfg['prior']))
    if len(cfg['prior']) != len(EXPERTS) or min(cfg['prior']) <= 0:
        raise ValueError('Every expert requires a positive prior')
    if min(cfg['local_pooling_mass'], cfg['shared_pooling_mass'], cfg['likelihood_temperature']) <= 0:
        raise ValueError('Pooling and learning settings must be positive')
    engine = ChoiceEngine(players, cfg['surprise_probability'])
    model = _new_model(engine.pool, our_slot, cfg)
    ordered = sorted(observed_picks, key=lambda p: int(p['pick_no']))
    if [int(p['pick_no']) for p in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError('Observed picks must be a unique contiguous prefix')
    if len(ordered) > cfg['teams'] * cfg['rounds']:
        raise ValueError('Observed prefix exceeds configured draft length')
    for item in ordered:
        pick = int(item['pick_no']); slot = _owner(pick, cfg['teams']); key = str(slot)
        pid = str(item['player_id'])
        if int(item.get('draft_slot', slot)) != slot:
            raise ValueError('Traded picks require an explicit draft order; snake prefix conflict')
        if pid not in model['available']:
            raise ValueError('Observed player missing from snapshot or already selected')
        if slot != our_slot:
            experts = engine.probabilities(model['available'], model['rosters'][key])
            weights = _weights(model, slot)['predictive_weights']
            probabilities = {p: sum(weights[e] * experts[e][p] for e in EXPERTS) for p in model['available']}
            actual = probabilities[pid]
            manager = model['managers'][key]
            row = {'pick_no': pick, 'draft_slot': slot, 'player_id': pid,
                   'manager_n_before': manager['observed_picks'], 'available_count': len(model['available']),
                   'probability_before_observation': actual,
                   'negative_log_score': -math.log(actual),
                   'multiclass_brier': sum(v*v for v in probabilities.values()) - 2*actual + 1,
                   'market_negative_log_score': -math.log(experts['market'][pid]),
                   'expert_probabilities': {e: experts[e][pid] for e in EXPERTS}}
            model['prequential'].append(row)
            responsibility = _normalized([weights[e] * experts[e][pid] ** cfg['likelihood_temperature'] for e in EXPERTS])
            for k, expert in enumerate(EXPERTS):
                manager['log_likelihood'][k] += math.log(experts[expert][pid])
                manager['responsibility_sum'][k] += responsibility[k]
            manager['observed_picks'] += 1
        model['rosters'][key].append(pid); model['available'].remove(pid)
        model['observed_picks'].append({'pick_no': pick, 'draft_slot': slot, 'player_id': pid})
    for slot in range(1, cfg['teams'] + 1):
        model['managers'][str(slot)].update(_weights(model, slot))
    n = len(model['prequential'])
    model['sample_size'] = {'actual_picks_total': len(ordered), 'opponent_decisions': n,
                            'opponents_with_observations': sum(m['observed_picks'] > 0 for m in model['managers'].values()),
                            'per_manager': {s: m['observed_picks'] for s,m in model['managers'].items()},
                            'unit_warning': 'A choice from 554 players is one observation, not 554; choices and managers are dependent.'}
    model['score_summary'] = {'n': n, 'mean_negative_log_score': sum(r['negative_log_score'] for r in model['prequential']) / n if n else None,
                              'mean_market_negative_log_score': sum(r['market_negative_log_score'] for r in model['prequential']) / n if n else None}
    return model


def predict(players, model, manager_slot, available=None, roster=None, mode='posterior'):
    """All available players get positive mass; no hidden candidate truncation."""
    engine = ChoiceEngine(players, model['settings']['surprise_probability'])
    if _fingerprint(engine.pool) != model['player_fingerprint']:
        raise ValueError('Player features changed; refit to the current snapshot')
    ids = model['available'] if available is None else available
    owned = model['rosters'][str(manager_slot)] if roster is None else roster
    if mode in ('adp', 'lineup'):
        return engine.probabilities(ids, owned, 'market' if mode == 'adp' else 'lineup')
    if mode != 'posterior':
        raise ValueError('Unknown prediction mode')
    experts = engine.probabilities(ids, owned)
    weights = _weights(model, manager_slot)['predictive_weights']
    return {pid: sum(weights[e] * experts[e][pid] for e in EXPERTS) for pid in ids}


def _draw(probabilities, uniform):
    cumulative = 0.0
    for key, probability in probabilities.items():
        cumulative += probability
        if uniform < cumulative:
            return key
    return next(reversed(probabilities))


def make_selector(players, model, seed=1, mode='posterior'):
    """Callback for drafting.draft_once; no simulated choice trains beliefs.

    One persistent expert is sampled per manager per rollout. Keyed pick-level
    uniforms permit common random numbers across candidate counterfactuals.
    """
    if mode not in ('posterior', 'adp', 'lineup'):
        raise ValueError('Unknown selector mode')
    engine = ChoiceEngine(players, model['settings']['surprise_probability'])
    if _fingerprint(engine.pool) != model['player_fingerprint']:
        raise ValueError('Player features changed; refit to the current snapshot')
    types = {slot: ('market' if mode == 'adp' else 'lineup') if mode != 'posterior' else
             _draw(_weights(model, slot)['predictive_weights'], random.Random(keyed_seed(seed, 'type', slot)).random())
             for slot in range(1, model['settings']['teams'] + 1)}

    def choose(team, pick, existing, available, roster=None):
        probabilities = engine.probabilities(available, existing, types[int(team)])
        u = random.Random(keyed_seed(seed, 'choice', int(team), pick)).random()
        return _draw(probabilities, u)

    choose.manager_types = types
    return choose


def simulate_until(players, observed_picks=(), candidates=(), draws=128, seed=1,
                   our_slot=5, model=None, first_pick=None, mode='posterior'):
    """Availability at our next turn, optionally after taking first_pick now.

    If currently our turn, first_pick defines the counterfactual and the target
    becomes our following turn. Without it, availability now is deterministic.
    Intervals quantify Monte Carlo sampling only, not behavioral uncertainty.
    """
    if draws < 2 or len(candidates) > 500:
        raise ValueError('Need at least two rollouts and at most 500 candidates')
    model = fit(players, observed_picks, our_slot) if model is None else model
    if model['our_slot'] != our_slot:
        raise ValueError('Own slot disagrees with fitted model')
    if observed_picks and model['observed_picks'] != [dict(pick_no=int(p['pick_no']), draft_slot=int(p.get('draft_slot', _owner(int(p['pick_no'])))), player_id=str(p['player_id'])) for p in sorted(observed_picks, key=lambda p:int(p['pick_no']))]:
        raise ValueError('Observed prefix disagrees with fitted model')
    next_pick = len(model['observed_picks']) + 1
    end = model['settings']['teams'] * model['settings']['rounds']
    if next_pick > end:
        raise ValueError('Draft has finished')
    target = next((p for p in range(next_pick, end + 1) if _owner(p) == our_slot), None)
    initial = [str(p) for p in candidates]
    if len(initial) != len(set(initial)) or any(p not in _pool(players) for p in initial):
        raise ValueError('Invalid candidate IDs')
    if first_pick is not None:
        first_pick = str(first_pick)
        if _owner(next_pick) != our_slot or first_pick not in model['available']:
            raise ValueError('First-pick counterfactual requires our current turn and an available player')
        target = next((p for p in range(next_pick + 1, end + 1) if _owner(p) == our_slot), None)
    if target is None:
        raise ValueError('No following own pick exists')
    counts = Counter(); trace = []
    for draw in range(draws):
        available = list(model['available'])
        rosters = {s:list(r) for s,r in model['rosters'].items()}
        start = next_pick
        if first_pick is not None:
            rosters[str(our_slot)].append(first_pick); available.remove(first_pick); start += 1
        selector = make_selector(players, model, keyed_seed(seed, 'rollout', draw), mode)
        for pick in range(start, target):
            team = _owner(pick)
            chosen = selector(team, pick, rosters[str(team)], available, rosters)
            rosters[str(team)].append(chosen); available.remove(chosen)
            if draw == 0:
                trace.append({'pick_no':pick, 'draft_slot':team, 'player_id':chosen})
        counts.update(p for p in initial if p in available)
    estimates = []
    for pid in initial:
        p = counts[pid] / draws
        z = 1.959963984540054
        center = (p + z*z/(2*draws))/(1 + z*z/draws)
        half = z*math.sqrt(p*(1-p)/draws + z*z/(4*draws*draws))/(1+z*z/draws)
        deterministic = pid not in model['available'] or pid == first_pick or target == next_pick
        estimates.append({'player_id':pid, 'survival_probability':p, 'monte_carlo_se': math.sqrt(p*(1-p)/draws),
                          'mc_wilson_95': [p,p] if deterministic else [max(0, center-half), min(1, center+half)],
                          'deterministic_from_observed_state': deterministic})
    return {'target_pick':target, 'first_pick':first_pick, 'mode':mode, 'draws':draws,
            'intervening_picks':target-next_pick-(first_pick is not None),
            'candidates':estimates, 'example_trace':trace, 'sample_size':model['sample_size'],
            'interval_meaning':'simulation sampling only; no behavioral calibration or forecast uncertainty',
            'confidence':model['confidence']}


def next_forecast(players, model, *, mode='posterior', created_at=None,
                  feature_available_at=None):
    """Receipt for the NEXT actual rival pick, before that outcome is known.

    Returning a receipt does not certify point-in-time eligibility. A later
    scorer must independently establish that the actual choice became public
    after created_at. When our turn is next, no rival choice can yet be forecast
    without conditioning on our still-undecided selection, so return None.
    """
    import time
    created_at = time.time() if created_at is None else float(created_at)
    if not math.isfinite(created_at):
        raise ValueError('A finite forecast creation timestamp is required')
    if feature_available_at is not None:
        feature_available_at = float(feature_available_at)
        if not math.isfinite(feature_available_at) or feature_available_at > created_at:
            raise ValueError('Forecast features were not yet available at creation')
    pick = len(model['observed_picks']) + 1
    if pick > model['settings']['teams'] * model['settings']['rounds']:
        return None
    slot = _owner(pick, model['settings']['teams'])
    if slot == model['our_slot']:
        return None
    probabilities = predict(players, model, slot, mode=mode)
    prefix_hash = hashlib.sha256(json.dumps(model['observed_picks'],sort_keys=True).encode()).hexdigest()
    identity = {'player_fingerprint':model['player_fingerprint'], 'prefix_hash':prefix_hash,
                'settings':model['settings'], 'mode':mode, 'probabilities':probabilities}
    forecast_id = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return {'forecast_id':forecast_id, 'created_at':created_at,
            'feature_available_at':feature_available_at, 'target_pick_no':pick,
            'target_draft_slot':slot, 'prefix_hash':prefix_hash,
            'observed_prefix':model['observed_picks'], 'mode':mode,
            'player_fingerprint':model['player_fingerprint'],
            'settings':model['settings'], 'sample_size':model['sample_size'],
            'probabilities':probabilities,
            'validation_status':'pending actual outcome and independently verified publication time',
            'prospective_requirement':'input vintage <= created_at < actual choice public timestamp; replay scores do not qualify',
            'confidence':model['confidence']}


def archive_forecast(directory, forecast):
    """Atomically create one immutable forecast; retain the earliest receipt.

    A later identical call returns the original archived receipt, never changes
    its timestamp. Different features/settings/probabilities create a new ID.
    """
    from pathlib import Path
    import os
    import tempfile
    if forecast is None:
        return None
    root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    forecast_id=forecast['forecast_id']
    if len(forecast_id)!=64 or any(c not in '0123456789abcdef' for c in forecast_id):
        raise ValueError('Invalid forecast ID')
    path=root/(forecast_id+'.json')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=root,delete=False) as stream:
            temporary=Path(stream.name)
            json.dump(forecast,stream,sort_keys=True,indent=2,allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary,path)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    original=json.loads(path.read_text())
    identity = {k:original[k] for k in ('player_fingerprint','prefix_hash','settings','mode','probabilities')}
    actual_id = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    if original.get('forecast_id') != forecast_id or actual_id != forecast_id:
        raise ValueError('Archived forecast identity mismatch')
    return {'path':str(path.resolve()),'forecast':original}
