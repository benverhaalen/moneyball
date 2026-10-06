"""Startup policy experiments. ADP models demand, never player production."""
from collections import Counter
import math
import random
import statistics
from .simulation import keyed_seed, positions, select_lineup, simulate, EXPERIMENTAL_STARTERS


POLICIES = ('market', 'flexible', 'lookahead', 'two_qb_early', 'qb_anchor', 'three_qb', 'four_qb')


def snake_owner(pick, teams=12):
    if pick < 1:
        raise ValueError('Picks are one-indexed')
    round_index, offset = divmod(pick-1, teams)
    return offset+1 if round_index % 2 == 0 else teams-offset


def _sample_demand(pool, noise, seed):
    rng = random.Random(seed)
    return {team:{pid:float(p.get('adp') or 999)+rng.gauss(0,noise)
                  for pid,p in sorted(pool.items())} for team in range(1,13)}


def draft_once(players, *, policy='flexible', slot=5, rounds=28, seed=1,
               demand_noise=8.0, observed_picks=(), forced_first=None, rivals='adp',
               planning_draws=4, planning_seed=None, rival_selector=None):
    """Build complete rival rosters before evaluating the championship goal.

    The continuation is a transparent heuristic, not a solved equilibrium.
    Demand noise is an uncalibrated scenario parameter, reported downstream.
    """
    if policy not in POLICIES or not 1 <= slot <= 12 or rivals not in ('adp','lineup','mixed') or planning_draws < 1:
        raise ValueError('Invalid draft policy or slot')
    if rival_selector is not None and policy == 'lookahead':
        raise ValueError('External rival selector requires a nonrecursive continuation; no silently mismatched planning model')
    # NFL position (e.g. FB or DB) is not the platform's fantasy eligibility.
    pool = {str(p.get('id',p.get('player_id'))):p for p in players if positions(p)}
    if len(pool) < rounds*12:
        raise ValueError('Not enough eligible projected players to complete startup')
    demand = _sample_demand(pool,demand_noise,seed)
    if planning_seed is None:
        planning_seed = keyed_seed(seed,'independent-planning-prior')
    roster = {str(t):[] for t in range(1,13)}
    taken, trace = set(), []
    observed = {int(p['pick_no']):p for p in observed_picks}
    if len(observed) != len(observed_picks):
        raise ValueError('Duplicate observed pick number')
    if observed and set(observed) != set(range(1,max(observed)+1)):
        raise ValueError('Observed draft prefix must be contiguous')
    if observed and max(observed) > rounds*12:
        raise ValueError('Observed draft prefix exceeds configured draft length')
    picks_before_ben, first_pick, first_available = [], None, False
    candidate_checked, decision_pick, decision_player = False, None, None
    values = {pid:float(p.get('mean') or 0) for pid,p in pool.items()}

    def value(ids):
        lineup = select_lineup(ids,pool,values)
        return sum(values[p] for p in lineup)

    def candidates(available, team, preferences, *, bounded=False):
        # At most 24 rollout alternatives: twelve earliest modeled demands,
        # plus three best projected legal options at each dedicated position.
        by_demand = sorted(available,key=lambda p:(preferences[team][p],p))
        ids = by_demand[:12 if bounded else 32]
        for pos in ('QB','RB','WR','TE'):
            ids.extend(sorted((p for p in available if pos in positions(pool[p])),
                              key=lambda p:(-values[p],preferences[team][p],p))[:3 if bounded else 2])
        return list(dict.fromkeys(ids))

    def manager_policy(team):
        if team == slot:
            return policy
        return 'flexible' if rivals == 'lineup' or (rivals == 'mixed' and team%2 == 0) else 'market'

    def choose(team, pick, existing, available, own_policy, preferences):
        """One-step continuation shared by actual and counterfactual rivals."""
        counts = Counter(pos for p in existing for pos in positions(pool[p]))
        rnd = (pick-1)//12+1
        target = 2 if own_policy in ('two_qb_early','three_qb','four_qb') and rnd<=2 else 1 if own_policy=='qb_anchor' and rnd==1 else 0
        if own_policy == 'three_qb' and rnd<=8 and (8-rnd)<3-counts['QB']:
            target = 3
        if own_policy == 'four_qb' and rnd<=12 and (12-rnd)<4-counts['QB']:
            target = 4
        if counts['QB'] < target:
            qbs = [p for p in available if 'QB' in positions(pool[p])]
            if qbs:
                return max(qbs,key=lambda p:(values[p],-preferences[team][p],p))
        if own_policy != 'market':
            before = value(existing)
            gains = {p:value(existing+[p])-before for p in candidates(available,team,preferences)}
            if max(gains.values()) > 1e-8:
                return max(gains,key=lambda p:(gains[p],-preferences[team][p],p))
        # Dedicated position coverage is a continuation heuristic, not a
        # platform roster cap. The evaluated lineup remains an exact assignment.
        minimum = {'QB':1,'RB':2,'WR':3,'TE':1}
        need = {pos:max(0,n-counts[pos]) for pos,n in minimum.items()}
        if sum(need.values()) >= rounds-len(existing):
            valid = [p for p in available if any(need[pos]>0 for pos in positions(pool[p]))]
        else:
            valid = available
        return min(valid or available,key=lambda p:(preferences[team][p],p))

    def lookahead(pick, available):
        """Two own turns; no realized outcomes and no recursive tree search.

        Each alternative changes rival rosters and remaining supply. Planning
        preferences are independent prior draws, never actual future private
        rival preferences. Common planning scenarios compare all alternatives.
        Current holdings are observed, but the prior is not fitted to picks.
        """
        existing = roster[str(slot)]
        next_pick = next((n for n in range(pick+1,rounds*12+1) if snake_owner(n)==slot),None)
        if next_pick is None:
            return choose(slot,pick,existing,available,'flexible',demand)
        candidate_ids = candidates(available,slot,demand,bounded=True)
        before = value(existing)
        if max(value(existing+[p])-before for p in candidate_ids) <= 1e-8:
            return choose(slot,pick,existing,available,'flexible',demand)
        terminal = {p:0.0 for p in candidate_ids}
        for scenario in range(planning_draws):
            planning = _sample_demand(pool,demand_noise,keyed_seed(planning_seed,pick,scenario))
            for candidate in candidate_ids:
                trial_rosters = {t:list(ids) for t,ids in roster.items()}
                trial_rosters[str(slot)].append(candidate)
                trial_available = [p for p in available if p != candidate]
                for intervening in range(pick+1,next_pick):
                    rival = snake_owner(intervening)
                    selected = choose(rival,intervening,trial_rosters[str(rival)],trial_available,manager_policy(rival),planning)
                    trial_rosters[str(rival)].append(selected)
                    trial_available.remove(selected)
                following = choose(slot,next_pick,trial_rosters[str(slot)],trial_available,'flexible',planning)
                terminal[candidate] += value(trial_rosters[str(slot)]+[following])/planning_draws
        # Equal terminal value favors the less replaceable candidate now.
        return max(terminal,key=lambda p:(terminal[p],-demand[slot][p],values[p],p))

    for pick in range(1,rounds*12+1):
        team = snake_owner(pick)
        existing = roster[str(team)]
        available = [p for p in pool if p not in taken]
        selected = None
        if pick in observed:
            item = observed[pick]
            if int(item.get('draft_slot',team)) != team:
                raise ValueError('Observed prefix conflicts with snake ownership; traded startup picks need explicit order')
            selected = str(item['player_id'])
            if selected not in available:
                raise ValueError('Observed pick missing from universe or duplicated')
        elif team == slot and not candidate_checked:
            candidate_checked = True
            decision_pick = pick
            first_available = forced_first is not None and str(forced_first) in available
            if first_available:
                selected = str(forced_first)
        if selected is None:
            if rival_selector is not None and team != slot:
                selected = str(rival_selector(team,pick,list(existing),list(available),
                    {t:list(ids) for t,ids in roster.items()}))
                if selected not in available:
                    raise ValueError('Rival selector returned unavailable or unknown player')
            else:
                own_policy = manager_policy(team)
                selected = lookahead(pick,available) if own_policy == 'lookahead' else choose(team,pick,existing,available,own_policy,demand)
        existing.append(selected); taken.add(selected)
        trace.append({'pick_no':pick,'draft_slot':team,'player_id':selected})
        if pick == decision_pick:
            decision_player = selected
        if team == slot and first_pick is None:
            first_pick = selected
        elif first_pick is None:
            picks_before_ben.append(selected)
    holdings = {t:list(r) for t,r in roster.items()}
    taxi, cuts = {}, {}
    for team, ids in roster.items():
        protected = set(select_lineup(ids,pool,values))
        tail = sorted((p for p in ids if p not in protected),key=lambda p:(values[p],-float(pool[p].get('adp') or 999)))
        rookies = [p for p in tail if pool[p].get('years_exp') == 0]
        taxi[team] = rookies[:3]
        active = [p for p in ids if p not in taxi[team]]
        releases = [p for p in tail if p in active][:max(0,len(active)-25)]
        cuts[team] = releases
        roster[team] = [p for p in active if p not in releases]
    return {'rosters':roster,'holdings':holdings,'taxi':taxi,'cuts':cuts,'trace':trace,'first_pick':first_pick,'forced_first_available':first_available,
        'candidate_decision_pick':decision_pick,
        'decision_player_id':decision_player,
        'before_ben':picks_before_ben,'policy':policy,'demand_noise':demand_noise,
        'planning_draws':planning_draws if policy=='lookahead' else 0,
        'planning_information':'observed holdings plus independent ADP-prior draws; actual future rival preference shocks are hidden'}


def experiment(players, *, slot=5, policies=POLICIES, draft_draws=12, outcome_draws=100,
               years=1, seed=1, demand_noise=8.0, observed_picks=(), nfl_schedule=None,
               forced_candidates=(), rivals='adp', missing_forecast='zero', projection_metadata=None,
               persistent_shock=False):
    if draft_draws < 2:
        raise ValueError('At least two draft completions needed')
    labels = list(policies)+['candidate:'+str(p) for p in forced_candidates]
    if not labels or labels[0] != 'market':
        raise ValueError('First policy must be market baseline')
    estimates = {p:[] for p in labels}
    deltas = {p:[] for p in labels}
    first = {p:Counter() for p in labels}
    next_choice = {p:Counter() for p in labels}
    availability = Counter()
    examples = {}
    for i in range(draft_draws):
        completions,states = {},{}
        for label in labels:
            candidate = label.split(':',1)[1] if label.startswith('candidate:') else None
            policy = 'lookahead' if candidate else label
            d = draft_once(players,policy=policy,slot=slot,seed=keyed_seed(seed,'draft',i),
                demand_noise=demand_noise,observed_picks=observed_picks,forced_first=candidate,rivals=rivals)
            states[label] = d['rosters']; completions[label] = d
            first[label][d['first_pick']] += 1
            if d['decision_player_id'] is not None:
                next_choice[label][d['decision_player_id']] += 1
            availability[label] += int(d['forced_first_available'])
        result = simulate(states,{**(projection_metadata or {}),'players':players},draws=outcome_draws,seed=keyed_seed(seed,'outcome',i),
            years=years,nfl_schedule=nfl_schedule,allow_assumptions=True,missing_forecast=missing_forecast,persistent_shock=persistent_shock)
        for label in labels:
            metric = result['scenarios'][label][str(slot)]
            estimates[label].append(metric['expected_titles']['estimate'])
            deltas[label].append(metric['paired_delta_expected_titles']['estimate'])
            if i == 0:
                examples[label] = {'roster':states[label][str(slot)],
                    'taxi':completions[label]['taxi'][str(slot)],'cuts':completions[label]['cuts'][str(slot)],
                    'first_six':[x for x in completions[label]['trace'] if x['draft_slot']==slot][:6]}
    report = []
    for label in labels:
        se = statistics.stdev(deltas[label])/math.sqrt(draft_draws)
        report.append({'policy':label,'expected_titles':statistics.fmean(estimates[label]),
            'delta_expected_titles_vs_market':statistics.fmean(deltas[label]),
            'between_completion_standard_error':se,'completion_delta_min':min(deltas[label]),
            'completion_delta_max':max(deltas[label]),'first_pick_frequencies':dict(first[label]),
            'next_unobserved_pick_frequencies':dict(next_choice[label]),
            'forced_candidate_availability':availability[label]/draft_draws if label.startswith('candidate:') else None,
            'confidence':'uncalibrated conditional experiment; no claim of a proven edge',
            'example':examples[label]})
    report.sort(key=lambda r:-r['expected_titles'])
    return {'policies':report,'years':years,'draft_draws':draft_draws,'outcome_draws_each':outcome_draws,
        'comparison_count':len(labels)-1,'seed':seed,'demand_noise':demand_noise,'rival_policy':rivals,
        'methods':{'demand':'format-matched ADP plus independent fixed manager/player Gaussian preference noise',
            'own_continuation':'lookahead: at most24 current alternatives, adaptive intervening rival picks, then greedy legal lineup at next own turn; flexible: one-step marginal lineup; zero gain: demand order',
            'evaluation':'paired full-league schedule and fixed-bracket Monte Carlo',
            'candidate_interpretation':'policy selects candidate at next unobserved own pick if available; otherwise same lookahead continuation',
            'scope':'initial holdings only; no simulated trading or replacement market'},
        'limitations':['ADP demand noise is a scenario parameter, not fitted to these eleven managers',
            'Greedy continuation can undervalue waiting, reserves, rookie option value, and multiyear age effects',
            'Two-turn lookahead truncates later opportunity costs and searches at most24 current candidates with4 independent planning preference scenarios; the ADP prior is not updated from revealed picks and future actual rival preference shocks remain hidden',
            'Three lowest-mean reserve rookies use taxi if eligible; remaining excess above25 is cut. Promotion options are not optimized',
            'Changing our picks changes rival holdings: deltas include displacement effects',
            'Simulation sampling error understates total uncertainty; estimates are not validated winning probabilities'],
        'simulation_assumptions':result['assumptions']}
