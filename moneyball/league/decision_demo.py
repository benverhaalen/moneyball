"""Repeatable whole-question lifecycle with invented evidence and no accounts.

The host judgment below is a declared example, not an LLM-generated verdict.
Local timing measures evidence/mechanics only, never a complete host interaction.
"""
from copy import deepcopy
from tempfile import TemporaryDirectory
import time

from .demo import fixture, forecasts
from .service import Service
from .store import DataError, digest


def seed(service):
    """Publish a private synthetic league, complete means and two waiver targets."""
    data = fixture()
    # Fill capacity so every acquisition branch must account for its cut.
    data['rules']['bench_slots'] = 0
    now = time.time()
    rows = forecasts(data, available_at=now)
    rows.append({**rows[-1], 'player_id': 'fallback', 'points': 8})
    service.store.put('config', 'demo', {k: data[k] for k in ('platform', 'league_id', 'season')})
    service._publish('demo', data, [{'source': 'invented-whole-question', 'scope': 'synthetic'}], rows)
    pool = {'players': [
        {'player_id': pid, 'name': name, 'positions': ['WR'], 'external_ids': {},
         'acquisition_status': 'WAIVERS', 'execution_actionable': None}
        for pid, name in [('free', 'Available Synthetic Receiver'), ('fallback', 'Fallback Synthetic Receiver')]],
        'observed_at': now, 'expires_at': now + 3600,
        'horizon': {'kind': 'week', 'season': 2026, 'week': 5},
        'source': {'provider': 'synthetic', 'url': 'https://example.invalid/synthetic-pool'},
        'waiver_state': {'current_order': None, 'rules': data['rules']['waivers']},
        'league_binding': {k: data[k] for k in ('platform', 'league_id', 'season')},
        'ownership_digest': digest(sorted(p for t in data['teams'].values() for p in t['player_ids'])),
        'coverage': {'complete_membership_relative_to_snapshots': True, 'scope': 'Two invented candidates only'}}
    service.store.put('pool', 'demo', pool)
    service.store.put('pool_index', 'demo', pool)
    service.store.put('dossier', 'synthetic:free', {
        'summary': 'Invented role opportunity; availability is an assumption.',
        'evidence_checked_at': now, 'claims': [], 'sources': [],
        'open_questions': ['No live injury or transaction feed was consulted.']})
    seed_draft(service, data, now)
    return data


def seed_draft(service, data, now):
    """Invent matching board/roster observations with one ordinary vacancy."""
    from .draft import _cache_key, _identity
    data = deepcopy(data)
    data['rules']['bench_slots'] = 1
    data['players']['free'] = {'name': 'Available Synthetic Receiver', 'positions': ['WR'], 'external_ids': {}}
    service.store.put('config', 'demo-draft', {k: data[k] for k in ('platform', 'league_id', 'season')})
    service._publish('demo-draft', data, [{'source': 'invented-draft-observation'}], forecasts(data, available_at=now))
    board = {'draft_id': '123', 'league_identity': _identity(data), 'status': 'drafting', 'type': 'snake',
        'support': 'synthetic_saved_observation',
        'picks': [{'pick_no': i, 'player_id': pid, 'team_id': tid}
                  for i, (tid, pid) in enumerate(((tid, pid) for tid, team in data['teams'].items()
                                                  for pid in team['player_ids']), 1)],
        'pick_sequence_complete': True, 'order': None, 'settings': {},
        'timing': {'live_countdown_seconds': None}, 'source_receipts': [],
        'consistency': 'Invented internally consistent observations; no provider contacted'}
    board['board_hash'] = digest(board)
    board['prefix_hash'] = digest(board['picks'])
    board['oldest_observed_at'] = now
    service.store.put('draft_board', _cache_key(data, '123'), board, {'synthetic': True})


def scenario_specs():
    """Ordinary questions, explicit aliases, named hold branches and constraints."""
    hold = {'id': 'hold', 'kind': 'hold', 'week': 5}
    swap = {'id': 'swap', 'kind': 'trade', 'proposal': {'transfers': [
        {'player_id': 'ar', 'from_team': 'a', 'to_team': 'b'},
        {'player_id': 'bx', 'from_team': 'b', 'to_team': 'a'}], 'pick_transfers': [], 'drops': {}}}
    scenarios = {
        'waiver': ('Plan my Tuesday waivers with a fallback and the actual cut.', [hold,
            {'id': 'add', 'kind': 'add_drop', 'player_id': 'free', 'drop_id': 'ax', 'week': 5},
            {'id': 'claims', 'kind': 'ordered_claims', 'week': 5, 'claims': [
                {'player_id': 'free', 'drop_player_id': 'ax', 'order': 1, 'faab_bid': 7, 'faab_budget_before': 100},
                {'player_id': 'fallback', 'drop_player_id': 'ax', 'order': 2, 'faab_bid': 3, 'faab_budget_before': 100}]}]),
        'trade': ('Should I swap my runner for Summit Flex Receiver?', [hold, swap]),
        'trade_discovery': ('Find an exchange involving my runner and inspect both squads.', [hold]),
        'mixed': ('Exchange a runner, claim a receiver, or wait while a new role hypothesis is unresolved?', [hold,
            swap, {'id': 'mixed-add', 'kind': 'add_drop', 'player_id': 'free', 'drop_id': 'ax', 'week': 5},
            {'id': 'unknown-policy', 'kind': 'unmodeled_policy',
             'description': 'Preserve an unsupported management idea without inventing mechanics'}]),
        'lineup': ('Who starts in my four slots this week?', [hold,
            {'id': 'named', 'kind': 'lineup', 'week': 5, 'slot_player_ids': ['aq', 'ar', 'aw', 'ax']}]),
        'research': ('What dated evidence could reverse the receiver recommendation?', [hold]),
        'rebuild': ('How does keeping these holdings next season change the roster?', [hold,
            {'id': 'retain', 'kind': 'roster_plan', 'steps': [{'kind': 'Retain', 'season': 2027,
             'teams': {'a': ['aq', 'ar', 'aw', 'ax'], 'b': ['bq', 'br', 'bw', 'bx']}}]}]),
    }
    specs = [{'specification': {'question': question, 'alias': 'demo', 'intent': intent,
              'refresh': False, 'player_ids': ['free'] if intent in ('waiver', 'research') else [],
              **({'horizon': [2026, 2027]} if intent == 'rebuild' else {})},
             'actions': deepcopy(actions)} for intent, (question, actions) in scenarios.items()]
    specs.append({'specification': {'question': 'Compare a current/future draft option with holding the vacancy',
        'alias': 'demo-draft', 'intent': 'draft', 'refresh': False, 'horizon': [2026, 2027], 'player_ids': ['free']},
        'actions': [deepcopy(hold)]})
    next(s for s in specs if s['specification']['intent'] == 'mixed')['worlds'] = [
        {'name': 'receiver_role_expands', 'weight': None, 'assumption': 'Unverified role-change hypothesis'},
        {'name': 'runner_role_persists', 'weight': None, 'assumption': 'No transition probability supplied'}]
    return specs


def complete_actions(service, scenario, context):
    """Discover finalists and read the saved board through real local operations."""
    actions = deepcopy(scenario['actions'])
    artifacts = {}
    if context['intent'] == 'trade_discovery':
        page = service.discover_candidates(context['decision_id'], limit=8, kinds=['trade'],
            focus_player_ids=['ar'], package_shapes=[[1, 1]])
        full = service.read_candidate_page(page['candidate_page_id'])
        finalist = next(row for row in full['candidates'] if row['legal_status'] == 'feasible_under_checked_rules')
        actions.append({**deepcopy(finalist['action']), 'id': 'discovered-finalist'})
        artifacts = {'screening': page, 'expanded_finalists': full}
    elif context['intent'] == 'draft':
        from .draft import context as draft_context
        board = draft_context(service, context['alias'], fresh=False, draft_id='123', player_ids=['free'], max_age_seconds=300)
        actions.append({'id': 'draft-free', 'kind': 'draft_pick', 'player_id': 'free',
            'draft_id': '123', 'board_hash': board['board_hash'], 'week': 5, 'max_age_seconds': 300})
        artifacts = {'board': board}
    return actions, artifacts


def judgment(comparison, choice='hold'):
    return {'comparison_id': comparison['comparison_id'], 'choice': choice,
            'confidence': 'conditional synthetic example',
            'strongest_countercase': 'The acquired receiver improves the invented current-week mean.',
            'reversal_conditions': ['Verified health, eligibility or preferred horizon changes the decision.'],
            'host': 'declared fixture; no model called'}


def run():
    started = time.perf_counter()
    with TemporaryDirectory(prefix='moneyball-decision-demo-') as home:
        service = Service(home)
        original = seed(service)
        journeys = []
        for scenario in scenario_specs():
            context = service.prepare_decision(scenario['specification'])
            evidence = service.decision_evidence(context['decision_id'])
            actions, artifacts = complete_actions(service, scenario, context)
            compared = service.compare_actions(context['decision_id'], actions, worlds=scenario.get('worlds'))
            choice = 'draft-free' if context['intent'] == 'draft' else 'hold'
            receipt = service.record_decision(context['decision_id'], judgment(compared, choice))
            saved_at = service.store.get('decision', context['decision_id'])['available_at']
            resumed = Service(home).resume_decision(context['decision_id'], refresh=False)
            historical = service.store.get('decision', context['decision_id'], saved_at)['data']
            rebound = service.compare_actions(context['decision_id'], actions, worlds=scenario.get('worlds'))
            second_receipt = service.record_decision(context['decision_id'], judgment(rebound, choice))
            journeys.append({'context': context, 'evidence': evidence, 'comparison': compared,
                'receipt': receipt, 'resume': resumed, 'history_revision': historical['revision'], 'artifacts': artifacts,
                'rebound_receipt': second_receipt})
        # A changed squad must invalidate the old comparison, then resume safely.
        selected = journeys[0]
        changed = deepcopy(original)
        changed['teams']['a']['name'] = 'Harbor revised observation'
        service._publish('demo', changed, [], [])
        try:
            service.record_decision(selected['context']['decision_id'], judgment(rebound))
        except DataError as exc:
            mismatch_rejected = str(exc)
        else:
            raise AssertionError('Cross-decision comparison was accepted')
        compared = service.compare_actions(selected['context']['decision_id'], scenario_specs()[0]['actions'])
        try:
            service.record_decision(selected['context']['decision_id'], judgment(compared))
        except DataError as exc:
            changed_state_rejected = str(exc)
        else:
            raise AssertionError('Changed roster was accepted')
        recovery = service.resume_decision(selected['context']['decision_id'], refresh=False)
        recovered_comparison = service.compare_actions(selected['context']['decision_id'], scenario_specs()[0]['actions'])
        recovered_receipt = service.record_decision(selected['context']['decision_id'], judgment(recovered_comparison))
        from .candidates import compare_roster_plan, discover_candidates
        future = deepcopy(original)
        future['format'] = 'dynasty'
        future['rules']['retention'] = {'mode': 'all'}
        future_spec = next(s for s in scenario_specs() if s['specification']['intent'] == 'rebuild')
        future_plan = compare_roster_plan(future, future_spec['actions'][1], forecasts(future))
        discovery = discover_candidates(original, service.store.get('pool', 'demo')['data'],
            limit=8, forecasts=service.forecast_rows('demo'), decision_at=time.time())
        return {'mode': 'offline_synthetic_whole_question', 'network_used': False,
                'persistent_store_created': False, 'model_called': False,
                'timing': {'local_service_seconds': time.perf_counter() - started,
                           'scope': 'Synthetic local evidence and mechanics; excludes host reasoning and network'},
                'journeys': journeys, 'candidate_discovery': discovery, 'explicit_future_roster_plan': future_plan,
                'recovery': {'changed_state_rejected': changed_state_rejected,
                             'cross_decision_rejected': mismatch_rejected, 'resume': recovery,
                             'receipt': recovered_receipt},
                'limits': ['Invented evidence is not a current NFL forecast.',
                           'News coverage and platform execution remain unverified.',
                           'Example judgments are declared fixtures, not strategic validation.',
                           'No league action, message, account or authentication was used.']}


async def run_mcp():
    """Exercise the actual stdio server process using the optional official SDK."""
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    started = time.perf_counter()
    with TemporaryDirectory(prefix='moneyball-mcp-demo-') as home:
        seed(Service(home))
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'moneyball.league.server', '--transport', 'stdio'],
            env={**os.environ, 'MONEYBALL_HOME': home})
        trace = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                async def call(name, arguments):
                    tick = time.perf_counter()
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise AssertionError(f'{name}: {result.content}')
                    payload = result.structuredContent
                    if payload is None:
                        import json
                        payload = json.loads(next(c.text for c in result.content if c.type == 'text'))
                    trace.append({'tool': name, 'seconds': time.perf_counter() - tick, 'result': payload})
                    return payload
                for scenario in scenario_specs():
                    context = await call('prepare_decision', {'specification': scenario['specification']})
                    await call('decision_evidence', {'decision_id': context['decision_id']})
                    actions = deepcopy(scenario['actions'])
                    if context['intent'] == 'trade_discovery':
                        page = await call('discover_decision_candidates', {'decision_id': context['decision_id'],
                            'limit': 8, 'kinds': ['trade'], 'focus_player_ids': ['ar'], 'package_shapes': [[1, 1]]})
                        full = await call('read_candidate_page', {'candidate_page_id': page['candidate_page_id']})
                        finalist = next(row for row in full['candidates'] if row['legal_status'] == 'feasible_under_checked_rules')
                        actions.append({**deepcopy(finalist['action']), 'id': 'discovered-finalist'})
                    elif context['intent'] == 'draft':
                        board = await call('draft_context', {'alias': 'demo-draft', 'fresh': False,
                            'draft_id': '123', 'player_ids': ['free'], 'max_age_seconds': 300})
                        actions.append({'id': 'draft-free', 'kind': 'draft_pick', 'player_id': 'free',
                            'draft_id': '123', 'board_hash': board['board_hash'], 'week': 5, 'max_age_seconds': 300})
                    compared = await call('compare_actions', {'decision_id': context['decision_id'], 'actions': actions,
                        'worlds': scenario.get('worlds')})
                    choice = 'draft-free' if context['intent'] == 'draft' else 'hold'
                    await call('record_decision', {'decision_id': context['decision_id'], 'judgment': judgment(compared, choice)})
                    await call('resume_decision', {'decision_id': context['decision_id'], 'refresh': False})
        return {'mode': 'real_stdio_synthetic_evidence', 'server': initialized.serverInfo.model_dump(),
                'advertised_tools': [t.name for t in tools.tools], 'trace': trace,
                'timing': {'subprocess_service_seconds': time.perf_counter() - started,
                           'scope': 'SDK initialization and local tools, excluding LLM/user interaction'},
                'model_called': False, 'provider_network_used': False}


async def run_mcp_extensions():
    """Installed transport for draft recovery and unsupported-source maintenance.

    Uses an invented saved board. This does not assert a live provider clock or
    test a host model's time to reason. No network provider is contacted.
    """
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from .store import digest
    with TemporaryDirectory(prefix='moneyball-mcp-extensions-') as home:
        seeded = Service(home)
        seed(seeded)
        league = seeded.snapshot('demo')['data']
        seeded.store.put('config', 'demo', {k: league[k] for k in ('platform', 'league_id', 'season')})
        raw = {'draft_id': '123', 'league_identity': {k: league.get(k) for k in
               ('platform', 'league_id', 'season', 'own_team_id', 'strategy_key')},
               'picks': [], 'oldest_observed_at': time.time(), 'status': 'drafting'}
        raw['board_hash'] = digest({k: v for k, v in raw.items() if k != 'oldest_observed_at'})
        seeded.store.put('draft_board', 'synthetic-board', raw, {'synthetic': True})
        board = {'draft_id': '123', 'board_hash': raw['board_hash']}
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'moneyball.league.server', '--transport', 'stdio'],
            env={**os.environ, 'MONEYBALL_HOME': home})
        trace = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, args):
                    result = await session.call_tool(name, args)
                    if result.isError:
                        raise AssertionError(f'{name}: {result.content}')
                    payload = result.structuredContent
                    if payload is None:
                        import json
                        payload = json.loads(next(c.text for c in result.content if c.type == 'text'))
                    trace.append({'tool': name, 'result': payload})
                    return payload
                context = await call('prepare_decision', {'specification': {'alias': 'demo',
                    'intent': 'draft', 'question': 'Recover conditional alternatives before my pick',
                    'refresh': False, 'horizon': [2026, 2027]}})
                branches = [{'id': 'runner', 'player_id': 'ar', 'rationale': 'Current role option',
                    'strategy_rationale': 'Preserve explicit current/future construction tradeoff',
                    'risk': 'Role uncertain', 'horizon': '2026', 'confidence': 'conditional fixture',
                    'strongest_countercase': 'Receiver future role is more valuable',
                    'reversal_conditions': ['Role changes']}]
                prepared = await call('save_draft_preparation', {'decision_id': context['decision_id'],
                    'branches': branches, 'board': board})
                await call('prepared_draft_options', {'preparation_id': prepared['preparation_id'], 'board': board})
                await call('configure_maintenance', {'job_id': 'forecast-baseline',
                    'specification': {'kind': 'forecast', 'alias': 'demo'}, 'cadence_seconds': 3600})
                await call('run_maintenance', {'job_ids': ['forecast-baseline'], 'timeout_seconds': 2})
                await call('maintenance_status', {'job_id': 'forecast-baseline'})
        return {'mode': 'real_stdio_synthetic_extensions', 'trace': trace,
                'model_called': False, 'provider_network_used': False}
