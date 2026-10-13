import copy
import unittest

from moneyball.league.candidates import discover_candidates, compare_roster_plan
from moneyball.league.store import DataError
from test_trades import league, player, rows


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.data = league(bench_slots=0, format='keeper')
        self.data['own_team_id'] = 'a'
        self.data['rules']['position_limits'] = {}
        self.data['teams']['c'] = copy.deepcopy(self.data['teams']['b'])
        self.data['teams']['c'].update(id='c', name='C', player_ids=['cq', 'cr', 'cw'], starters=[])
        self.data['players'].update({p: player(p, pos) for p, pos in [('cq','QB'),('cr','RB'),('cw','WR')]})
        self.pool = {'players': [{'player_id': p, 'name': p, 'positions':['WR'], 'acquisition_status':'FREEAGENT'} for p in ['aaa','zzz']],
                     'observed_at':10, 'horizon':{'kind':'week','week':5,'season':2026},
                     'coverage': {'complete_membership_relative_to_snapshots':True}, 'source':{'provider':'synthetic'}}

    def test_round_robin_all_teams_and_non_alphabetic_pool_coverage(self):
        page = discover_candidates(self.data, self.pool, limit=3, decision_at=20)
        self.assertEqual(page['coverage']['team_ids_screened'], ['a','b','c'])
        self.assertEqual(page['candidates'][0]['comparison']['affected_team_ids'], ['a','b'])
        self.assertEqual(page['candidates'][1]['comparison']['affected_team_ids'], ['a','c'])
        self.assertEqual(page['candidates'][2]['action']['player_id'], 'aaa')
        all_rows = page['candidates'][:]
        while page['next_cursor']:
            page = discover_candidates(self.data, self.pool, limit=50, decision_at=20, cursor=page['next_cursor'])
            all_rows.extend(page['candidates'])
        self.assertIn('zzz', {r['action'].get('player_id') for r in all_rows})
        self.assertEqual(len(all_rows), len({r['id'] for r in all_rows}))
        self.assertTrue(page['coverage']['enumeration_complete'])
        self.assertIsNone(page['verdict'])

    def test_capacity_cuts_both_sides_and_missing_forecasts_never_zero(self):
        page = discover_candidates(self.data, limit=50, kinds=['Trade'], package_shapes=[[2,1]], decision_at=20)
        for row in page['candidates']:
            comparison = row['comparison']
            self.assertTrue(comparison['valid'])
            self.assertTrue(comparison['executable'])
            affected = comparison['affected_team_ids']
            receiver = next(t for t in affected if t != 'a')
            self.assertEqual(len(comparison['teams'][receiver]['dropped_player_ids']), 1)
            self.assertIsNone(comparison['result'])
            self.assertIsNone(row['acceptance_probability'])
        incomplete = rows(self.data, {'aq':10})
        page = discover_candidates(self.data, limit=1, forecasts=incomplete, decision_at=20)
        self.assertIsNone(page['candidates'][0]['comparison']['result'])
        self.assertEqual(page['candidates'][0]['comparison']['forecast_comparisons'][0]['status'], 'incomplete')

    def test_changed_roster_or_forecast_invalidates_cursor(self):
        first = discover_candidates(self.data, limit=1)
        changed = copy.deepcopy(self.data)
        changed['teams']['c']['player_ids'].remove('cw')
        with self.assertRaises(DataError):
            discover_candidates(changed, limit=1, cursor=first['next_cursor'])
        with self.assertRaises(DataError):
            discover_candidates(self.data, limit=1, cursor=first['next_cursor'], forecasts=[])

    def test_unverified_pool_not_promoted_and_owned_conflict_excluded(self):
        self.pool['players'][0]['player_id'] = 'aw'
        self.pool['coverage'] = {}
        result = discover_candidates(self.data, self.pool, limit=50, kinds=['AddDrop'], decision_at=20)
        self.assertEqual(result['coverage']['pool_owned_conflicts'], ['aw'])
        self.assertFalse(result['coverage']['pool_complete_membership_relative_to_snapshots'])
        self.assertFalse(result['coverage']['pool_execution_eligibility_verified'])
        self.assertTrue(any(r['legal_status'] == 'feasible_under_checked_rules' for r in result['candidates']))

    def test_future_pick_packages_retained_without_value(self):
        self.data['picks'] = [{'id':'p1','owner_team_id':'a','season':2027,'round':1}, {'id':'p2','owner_team_id':'b','season':2027,'round':2}]
        page = discover_candidates(self.data, limit=50, kinds=['Trade'], package_shapes=[[1,1]])
        rights = [r for r in page['candidates'] if r['action']['proposal']['pick_transfers']]
        self.assertTrue(rights)
        self.assertTrue(all(r['acceptance_probability'] is None for r in rights))


class RosterPlanTests(unittest.TestCase):
    def setUp(self):
        self.data = league(bench_slots=1, format='keeper')
        self.data['own_team_id'] = 'a'
        self.data['rules']['position_limits'] = {}
        self.data['picks'] = [{'id':'p1','owner_team_id':'a','season':2027,'round':1}]

    def test_keeper_exact_cost_consumes_owned_right_and_releases_others(self):
        self.data['rules']['retention'] = {'mode':'keeper','max_keepers':1,'players':{'ar':{'eligible':True,'cost_pick_id':'p1'}}}
        plan = {'steps':[{'kind':'Retain','season':2027,'teams':{'a':['ar'],'b':[]}}]}
        before = copy.deepcopy(self.data)
        result = compare_roster_plan(self.data, plan)
        self.assertTrue(result['valid'])
        self.assertEqual(result['after']['teams']['a']['player_ids'], ['ar'])
        self.assertEqual(result['after']['picks'], [])
        self.assertEqual(self.data, before)
        self.assertIsNone(result['championship_probability_delta'])

    def test_unknown_keeper_cost_blocks_future_only_and_preserves_state(self):
        self.data['rules']['retention'] = {'mode':'keeper','max_keepers':1,'players':{'ar':{'eligible':True}}}
        plan = {'steps':[{'kind':'Hold'}, {'kind':'Retain','season':2027,'teams':{'a':['ar'],'b':[]}}, {'kind':'Hold'}]}
        result = compare_roster_plan(self.data, plan)
        self.assertFalse(result['valid'])
        self.assertTrue(result['steps'][0]['applied'])
        self.assertFalse(result['steps'][1]['applied'])
        self.assertEqual(result['after']['season'], 2026)
        self.assertEqual(result['remaining_step_count'], 1)
        self.assertEqual(result['gaps'][0]['code'], 'keeper_eligibility_or_cost_required')

    def test_dynasty_label_never_infers_retention(self):
        self.data['format'] = 'dynasty'
        result = compare_roster_plan(self.data, {'steps':[{'kind':'Retain','season':2027,'teams':{'a':['aq','ar','aw'],'b':['bq','br','bw']}}]})
        self.assertFalse(result['valid'])
        self.assertEqual(result['gaps'][0]['code'], 'exact_retention_rules_required')

    def test_sequential_trade_then_drop_uses_changed_owner_and_rights(self):
        trade = {'kind':'Trade','proposal':{'pick_transfers':[{'pick_id':'p1','from_team':'a','to_team':'b'}]}}
        result = compare_roster_plan(self.data, {'steps':[trade, {'kind':'ReleaseRight','team_id':'b','pick_id':'p1'}]})
        self.assertTrue(result['valid'])
        self.assertEqual(result['after']['picks'], [])
        with self.assertRaises(DataError):
            compare_roster_plan(self.data, {'steps':[trade, {'kind':'ReleaseRight','team_id':'a','pick_id':'p1'}]})

    def test_exercise_right_and_placement_capacity_known_not_assumed(self):
        self.data['picks'][0]['season'] = 2026
        self.data['players']['rookie'] = player('Rookie','WR')
        result = compare_roster_plan(self.data, {'steps':[{'kind':'ExerciseRight','team_id':'a','pick_id':'p1','player_id':'rookie','eligibility_verified':True}]})
        self.assertTrue(result['valid'])
        self.assertIn('rookie', result['after']['teams']['a']['player_ids'])
        self.data['rules']['taxi_slots'] = 1
        plan = {'steps':[{'kind':'Placement','team_id':'a','player_id':'aw','destination':'taxi'}]}
        self.assertFalse(compare_roster_plan(self.data, plan)['valid'])
        plan['steps'][0]['eligibility_verified'] = True
        result = compare_roster_plan(self.data, plan)
        self.assertTrue(result['valid'])
        self.assertEqual(result['after']['teams']['a']['taxi'], ['aw'])

    def test_expired_or_duplicate_keeper_cost_cannot_be_consumed(self):
        self.data['rules']['retention'] = {'mode':'keeper','max_keepers':2,'players':{'aq':{'eligible':True,'cost_pick_id':'p1'},'ar':{'eligible':True,'cost_pick_id':'p1'}}}
        result = compare_roster_plan(self.data, {'steps':[{'kind':'Retain','season':2027,'teams':{'a':['aq','ar'],'b':[]}}]})
        self.assertFalse(result['valid'])
        self.assertIn('keeper_cost_right_unavailable', [g['code'] for g in result['gaps']])

    def test_future_forecast_must_match_year_not_current(self):
        self.data['rules']['retention'] = {'mode':'all'}
        forecast = rows(self.data, {pid:10 for pid in self.data['players']})
        result = compare_roster_plan(self.data, {'steps':[{'kind':'Retain','season':2027,'teams':{'a':['aq','ar','aw'],'b':['bq','br','bw']}}]}, forecast)
        self.assertTrue(result['valid'])
        comparison = result['steps'][0]['comparison']
        self.assertIsNone(comparison['result'])
        self.assertTrue(comparison['forecast_rejections'])


class CandidateEdgeTests(unittest.TestCase):
    def test_duplicate_owner_is_not_a_verified_universe(self):
        data = league()
        data['own_team_id'] = 'a'
        data['teams']['b']['player_ids'].append('aq')
        with self.assertRaises(DataError):
            discover_candidates(data)

    def test_future_taxi_promotes_to_ordinary_and_requires_capacity(self):
        data = league(bench_slots=0, format='dynasty')
        data['rules']['retention'] = {'mode':'all'}
        data['rules']['taxi_slots'] = 1
        data['players']['ax'] = player('AX','WR')
        data['teams']['a']['player_ids'].append('ax')
        data['teams']['a']['taxi'] = ['ax']
        step = {'kind':'Retain','season':2027,'teams':{'a':['aq','ar','aw','ax'],'b':['bq','br','bw']}}
        result = compare_roster_plan(data, {'steps':[step]})
        self.assertFalse(result['valid'])
        self.assertEqual(result['after']['teams']['a']['taxi'], ['ax'])
        step['placements'] = {'a':{'taxi':['ax'],'eligibility_verified':True}}
        self.assertTrue(compare_roster_plan(data, {'steps':[step]})['valid'])

    def test_add_drop_applies_complete_roster_and_pool_candidate(self):
        data = league(bench_slots=0)
        data['own_team_id'] = 'a'
        candidate = {'player_id':'fa','name':'FA','positions':['WR'],'acquisition_status':'FREEAGENT',
                     'observed_at':10,'horizon':{'kind':'week','week':5,'season':2026}}
        result = compare_roster_plan(data, {'steps':[{'kind':'AddDrop','candidate':candidate,'drop_player_id':'aw','decision_at':20}]})
        self.assertTrue(result['valid'])
        self.assertIn('fa', result['after']['teams']['a']['player_ids'])
        self.assertNotIn('aw', result['after']['teams']['a']['player_ids'])
        self.assertEqual(result['steps'][0]['conditional_state_changes']['teams']['a']['released_player_ids'], ['aw'])

    def test_changed_scoring_withholds_deltas(self):
        data = league()
        data['own_team_id'] = 'a'
        forecast = rows(data, {p:10 for p in data['players']})
        data['rules']['scoring']['weights']['rush_yd'] = 0.2
        result = discover_candidates(data, limit=1, forecasts=forecast, decision_at=20)
        comparison = result['candidates'][0]['comparison']
        self.assertIsNone(comparison['result'])
        self.assertTrue(comparison['forecast_rejections'])


class PoolBindingTests(unittest.TestCase):
    def test_full_pool_index_requires_current_ownership_and_namespace(self):
        from moneyball.league.store import digest
        data = league()
        data.update(own_team_id='a', league_id='synthetic')
        owned = sorted(p for t in data['teams'].values() for p in t['player_ids'])
        pool = {'players':[], 'league_binding':{k:data[k] for k in ('platform','league_id','season')},
                'ownership_digest':digest(owned), 'catalog_available_at':10,
                'scope':'Complete unowned active QB/RB/WR/TE membership relative to observed catalog and all observed rosters; transaction eligibility remains unknown'}
        result = discover_candidates(data, pool, limit=1)
        self.assertTrue(result['coverage']['pool_complete_membership_relative_to_snapshots'])
        pool['ownership_digest'] = 'old'
        result = discover_candidates(data, pool, limit=1)
        self.assertFalse(result['coverage']['pool_complete_membership_relative_to_snapshots'])
        self.assertEqual(result['coverage']['pool_membership_gaps'], ['pool_ownership_snapshot_changed'])
        pool['league_binding']['league_id'] = 'other'
        with self.assertRaises(DataError):
            discover_candidates(data, pool)


class PreparedCandidateIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from moneyball.league.service import Service
        from moneyball.league.demo import fixture, forecasts
        from moneyball.league.store import digest
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(self.temp.name)
        self.data = fixture()
        self.service._publish('demo', self.data, [], forecasts(self.data))
        self.pool = {'players':[{'player_id':pid,'name':pid,'positions':['WR'],
                                'acquisition_status':'FREEAGENT'} for pid in ['aaa','zzz']],
                     'league_binding':{k:self.data[k] for k in ('platform','league_id','season')},
                     'ownership_digest':digest(sorted(p for t in self.data['teams'].values() for p in t['player_ids'])),
                     'coverage':{'complete_membership_relative_to_snapshots':True},
                     'observed_at':1, 'horizon':{'kind':'week','week':5,'season':2026}}
        self.service.store.put('pool_index','demo',self.pool)
        self.context = self.service.prepare_decision({'alias':'demo','question':'Find trades and rebuild',
                                                     'intent':'trade_discovery','refresh':False})

    def test_wrapper_reads_complete_pool_at_prepared_cutoff_after_later_updates(self):
        first = self.service.discover_candidates(self.context['decision_id'], limit=50, kinds=['add_drop'])
        self.assertEqual({r['action']['player_id'] for r in first['candidates']}, {'aaa','zzz'})
        self.assertTrue(first['coverage']['pool_complete_membership_relative_to_snapshots'])
        changed = copy.deepcopy(self.pool)
        changed['players'] = [{'player_id':'later','name':'later','positions':['WR'],'acquisition_status':'FREEAGENT'}]
        self.service.store.put('pool_index','demo',changed)
        second = self.service.discover_candidates(self.context['decision_id'], limit=50, kinds=['add_drop'])
        self.assertEqual(first, second)
        self.assertEqual(first['evidence_as_of'],self.context['evidence_as_of'])
        self.assertTrue(all(r['available_at'] <= first['evidence_as_of'] for r in first['consumed_manifest']))

    def test_roster_plan_dispatch_replaces_placeholder_and_expands_nested_focus(self):
        action = {'id':'plan','kind':'roster_plan','plan':{'steps':[
            {'kind':'trade','proposal':{'transfers':[{'player_id':'ar','from_team':'a','to_team':'b'},
                                                   {'player_id':'bx','from_team':'b','to_team':'a'}],
                                      'drops':{'a':['ax']}}}]}}
        result = self.service.compare_actions(self.context['decision_id'], [{'id':'hold','kind':'hold'},action])
        branch = result['actions'][1]
        self.assertEqual(branch['status'],'mechanical_comparison')
        self.assertTrue(branch['accounting']['valid'])
        self.assertIn('bx',branch['accounting']['after']['teams']['a']['player_ids'])
        self.assertNotIn('ax',branch['accounting']['after']['teams']['a']['player_ids'])
        self.assertTrue({'ar','bx','ax'} <= set(result['focused_player_ids']))
        self.assertEqual(branch['accounting']['steps'][0]['comparison']['proposal']['decision_at'],self.context['evidence_as_of'])

    def test_direct_roster_plan_resolves_late_member_full_index_and_retains_unknown_forecast(self):
        result = self.service.roster_plan(self.context['decision_id'],{'steps':[
            {'kind':'add_drop','player_id':'zzz','drop_id':'aw'}]})
        self.assertTrue(result['valid'])
        self.assertIn('zzz',result['after']['teams']['a']['player_ids'])
        self.assertTrue({'zzz','aw'} <= set(result['focused_player_ids']))
        self.assertIsNone(result['steps'][0]['comparison']['result'])
        self.assertEqual(result['evidence_as_of'],self.context['evidence_as_of'])

    def test_unknown_retention_is_blocked_not_placeholder_or_automatic_dynasty_rule(self):
        plan = {'steps':[{'kind':'retain','season':2027,'teams':
            {tid:team['player_ids'] for tid,team in self.data['teams'].items()}}]}
        result = self.service.compare_actions(self.context['decision_id'],
            [{'id':'hold','kind':'hold'},{'id':'future','kind':'roster_plan','plan':plan}])
        future = result['actions'][1]
        self.assertEqual(future['status'],'blocked')
        self.assertEqual(future['gaps'][0]['code'],'exact_retention_rules_required')
        self.assertTrue(set(self.data['players']) <= set(result['focused_player_ids']))

    def test_comparison_and_discovery_open_one_fixed_read_view(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        original = self.service.store.read_view
        calls, connections = [], []
        @contextmanager
        def tracked(cutoff=None):
            calls.append(cutoff)
            with original(cutoff) as view:
                with self.service.store._read_db() as db:
                    connections.append(id(db))
                yield view
        with patch.object(self.service.store,'read_view',side_effect=tracked):
            self.service.discover_candidates(self.context['decision_id'],limit=1)
        self.assertTrue(calls)
        self.assertTrue(all(cutoff == self.context['evidence_as_of'] for cutoff in calls))
        self.assertEqual(len(set(connections)), 1)
        calls.clear()
        connections.clear()
        with patch.object(self.service.store,'read_view',side_effect=tracked):
            self.service.compare_actions(self.context['decision_id'],[{'id':'hold','kind':'hold'},
                {'id':'plan','kind':'roster_plan','plan':{'steps':[{'kind':'hold'}]}}])
        self.assertTrue(calls)
        self.assertTrue(all(cutoff == self.context['evidence_as_of'] for cutoff in calls))
        self.assertEqual(len(set(connections)), 1)


if __name__ == '__main__':
    unittest.main()
