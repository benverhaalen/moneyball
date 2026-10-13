"""Engineering acceptance, deliberately not a player-forecast benchmark."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from moneyball.league import workbench as wb
from moneyball.league.assessment_demo import seed, inputs, run
from moneyball.league.service import Service
from moneyball.league.store import DataError
from moneyball.league.workbench_math import calculate, score_projection


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Service(self.temp.name)
        self.league = seed(self.s)
        self.report, self.spec = inputs(self.league)
        self.saved = wb.save(self.s,'demo','case',self.report)

    def test_complete_trade_changes_direction_and_keeps_both_squads(self):
        x = wb.evaluate(self.s,'demo','case',self.spec)
        deltas=[]
        for world in x['worlds']:
            trade=world['actions']['swap_runner_for_receiver']
            self.assertTrue(trade['valid'])
            teams=trade['forecast_comparisons'][0]['teams']
            self.assertEqual(set(teams),{'a','b'})
            deltas.append(teams['a']['delta_points'])
        self.assertEqual(deltas,[-1,9,-8])
        self.assertIsNone(x['championship_probability_delta'])
        self.assertEqual(len(x['decision_matrix']),6)
        self.assertEqual(self.s.store.get('workbench_run','demo:case')['data']['result']['binding'],wb.binding(self.league))

    def test_pruning_propagates_without_rewriting_evidence(self):
        self.report['claims'][1]['depends_on']=['bx']
        wb.save(self.s,'demo','case',self.report)
        before=self.s.store.get('workbench_assessment','demo:case')['sha256']
        x=wb.evaluate(self.s,'demo','case',self.spec,disabled_claims=['bx'])
        self.assertFalse(x['claim_status']['ar']['usable_as_driver'])
        self.assertIsNone(x['worlds'][0]['player_projections']['ar']['mean'])
        self.assertEqual(before,self.s.store.get('workbench_assessment','demo:case')['sha256'])
        self.assertTrue(wb.read(self.s,'demo','case')['claim_status']['ar']['usable_as_driver'])

    def test_expiration_and_exploration_are_visible(self):
        self.report['sources'][0]['observed_at']=time.time()-100
        self.report['sources'][0]['expires_at']=time.time()-1
        wb.save(self.s,'demo','case',self.report)
        view=wb.read(self.s,'demo','case')
        self.assertFalse(view['claim_status']['ar']['usable_as_driver'])
        self.assertEqual(len(view['shared_origin_groups']['single-invented-input']),8)
        self.report['sources'][0].pop('expires_at')
        self.report['claims'][0]['use']='explore'
        wb.save(self.s,'demo','case',self.report)
        self.assertFalse(wb.read(self.s,'demo','case')['claim_status']['aq']['usable_as_driver'])

    def test_rules_holdings_team_and_season_bindings(self):
        for mutation in ('rules','roster','team','season'):
            with self.subTest(mutation=mutation):
                league=deepcopy(self.league)
                if mutation=='rules': league['rules']['scoring']['weights']['rush_yd']=.2
                if mutation=='roster': league['teams']['a']['player_ids'].remove('ax')
                if mutation=='team': league['own_team_id']='b'
                if mutation=='season': league['season']+=1
                self.s._publish('demo',league,[],[])
                self.assertTrue(wb.read(self.s,'demo','case')['binding_changes'])
                with self.assertRaisesRegex(DataError,'stale'): wb.evaluate(self.s,'demo','case',self.spec)
                self.s._publish('demo',self.league,[],[])

    def test_retention_and_bestball_limitations(self):
        for key,value in [('format','redraft'),('format','keeper'),('best_ball',True),('best_ball',None)]:
            league=deepcopy(self.league)
            if key=='format': league[key]=value
            else: league['rules'][key]=value
            self.s._publish('demo',league,[],[])
            self.report['binding']=wb.binding(league)
            wb.save(self.s,'demo','case',self.report)
            with self.assertRaises(DataError): wb.evaluate(self.s,'demo','case',self.spec)

    def test_no_backdating_new_world_assumptions(self):
        with self.assertRaisesRegex(DataError,'backdated'):
            wb.evaluate(self.s,'demo','case',self.spec,as_of=time.time())
        first=wb.read(self.s,'demo','case')
        cutoff=time.time()
        self.report['objective']='Revised objective'
        wb.save(self.s,'demo','case',self.report)
        self.assertEqual(wb.read(self.s,'demo','case',as_of=cutoff)['receipt'],first['receipt'])
        with self.assertRaises(DataError): wb.read(self.s,'demo','case',as_of=time.time()+3600)

    def test_invalid_claim_graph_and_identity(self):
        cases=[]
        bad=deepcopy(self.report);bad['claims'][0]['depends_on']=['aq'];cases.append(bad)
        bad=deepcopy(self.report);bad['claims'][0]['player_id']='another-provider:aq';cases.append(bad)
        bad=deepcopy(self.report);bad['claims'][0]['depends_on']=['absent'];cases.append(bad)
        bad=deepcopy(self.report);bad['claims'][0]['kind']='observation';bad['claims'][0]['source_ids']=[];cases.append(bad)
        bad=deepcopy(self.report);bad['sources'][0]['observed_at']=time.time()+3600;cases.append(bad)
        bad=deepcopy(self.report);bad['claims']=[None];cases.append(bad)
        for bad in cases:
            with self.assertRaises(DataError): wb.save(self.s,'demo','invalid',bad)

    def test_missing_components_propagate_and_ownership_errors_remain(self):
        self.spec['worlds'][0]['projections'][0]['stats'].pop('pass_yd')
        self.spec['actions']['invalid']={'transfers':[{'player_id':'ar','from_team':'b','to_team':'a'}]}
        x=wb.evaluate(self.s,'demo','case',self.spec)
        self.assertEqual(x['worlds'][0]['player_projections']['aq']['status'],'missing_components')
        self.assertEqual(x['worlds'][0]['team_baselines']['a']['status'],'incomplete')
        self.assertFalse(x['worlds'][0]['actions']['invalid']['valid'])

    def test_zero_availability_and_dated_out_status_consistent_with_trade(self):
        self.league['players']['ar']['injury_status'] = {'status':'OUT', 'available_at':time.time()-10, 'week':5, 'season':2026}
        self.s._publish('demo',self.league,[],[])
        x=wb.evaluate(self.s,'demo','case',self.spec)
        self.assertNotIn('ar',x['worlds'][0]['team_baselines']['a']['lineup']['selected_player_ids'])
        self.assertIn('ar',x['worlds'][1]['team_baselines']['a']['lineup']['selected_player_ids'])
        self.spec['worlds'][1]['projections'][0]['availability']=0
        x=wb.evaluate(self.s,'demo','case',self.spec)
        lineup=x['worlds'][1]['team_baselines']['a']['lineup']
        self.assertNotIn('aq',lineup['selected_player_ids'])
        self.assertFalse(lineup['complete'])

    def test_allocation_conservation(self):
        rows=self.spec['worlds'][0]['projections']
        for row in rows[:2]:
            row.pop('stats');row['nfl_team']='SYN'
            row['opportunity']={'rushing':{'team_attempts':25,'carry_share':.7,'yards_per_carry':4,'td_per_carry':.03}}
            row['zero_components']=['rec_yd','pass_yd']
        with self.assertRaisesRegex(DataError,'exceed'): wb.evaluate(self.s,'demo','case',self.spec)

    def test_context_roundtrip_and_cli(self):
        packet=wb.context(self.s,'demo',['ar','bx'])
        self.assertEqual(set(packet['teams']),{'a','b'})
        self.assertEqual(len(packet['required_facets']),8)
        self.assertEqual(len(packet['reference_catalog']['references']),34)
        result=subprocess.run([sys.executable,'-m','moneyball','--home',self.temp.name,'assess','read','demo','case'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['binding_current'])

    def test_compatibility_is_same_implementation(self):
        from art_of_the_deal.store import Store as OldStore, DataError as OldError
        from moneyball.league.store import Store
        self.assertIs(OldStore,Store)
        self.assertIs(OldError,DataError)


class MathTests(unittest.TestCase):
    def test_calculators(self):
        self.assertAlmostEqual(calculate({'kind':'shrinkage','estimate':.5,'prior':.2,'sample_size':10,'prior_strength':20})['estimate'],.3)
        mixture=calculate({'kind':'availability_mixture','availability':.5,'conditional_mean':10,'conditional_variance':4})
        self.assertEqual((mixture['mean'],mixture['variance']),(5,27))
        x=calculate({'kind':'role_transition','initial':{'reserve':1,'starter':0},'steps':2,
            'transitions':{'reserve':{'reserve':.5,'starter':.5},'starter':{'reserve':0,'starter':1}}})
        self.assertEqual(x['state_path'][-1]['starter'],.75)

    def test_opportunity_scoring_and_negative_interceptions(self):
        x=score_projection({'opportunity':{'passing':{'team_attempts':30,'attempt_share':1,'yards_per_attempt':7,'td_per_attempt':.05,'int_per_attempt':.03}},'availability':.8},
            {'weights':{'pass_yd':.04,'pass_td':4,'pass_int':-2}})
        self.assertAlmostEqual(x['mean'],10.08)
        self.assertIsNone(x['variance'])
        bad=score_projection({'stats':{'pass_yd':100},'availability':1},{'weights':{'pass_yd':.04,'pass_int':-2}})
        self.assertEqual(bad['missing'],['pass_int'])
        self.assertEqual(score_projection({}, {'nonlinear':['bonus']})['status'],'unsupported_scoring')

    def test_malformed_and_nonfinite_fail_cleanly(self):
        for payload in (None,{'kind':'shrinkage','estimate':float('nan')},{'kind':'opportunity','receiving':None},
                        {'kind':'role_transition','initial':{'a':1},'steps':1,'transitions':None}):
            with self.assertRaises(DataError): calculate(payload)
        with self.assertRaises(DataError):
            score_projection({'stats':{},'zero_components':['x','x'],'availability':1},{'weights':{'x':1}})


@unittest.skipUnless(importlib.util.find_spec('mcp'),'MCP extra required')
class AssessmentMCP(unittest.IsolatedAsyncioTestCase):
    async def test_normal_stdio_journey(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryFile(mode='w+') as log:
            s=Service(home); league=seed(s); report,spec=inputs(league)
            params=StdioServerParameters(command=sys.executable,args=['-m','moneyball.league.server'],env={**os.environ,'MONEYBALL_HOME':home})
            async with stdio_client(params,errlog=log) as (reader,writer):
                async with ClientSession(reader,writer) as client:
                    await client.initialize()
                    async def call(name,args):
                        r=await client.call_tool(name,args)
                        self.assertFalse(r.isError,str(r.content))
                        return json.loads(r.content[0].text)
                    packet=await call('player_assessment_context',{'alias':'demo','player_ids':['ar','bx']})
                    report['binding']=packet['binding']
                    await call('save_player_assessment',{'alias':'demo','assessment_id':'case','report':report})
                    result=await call('compare_assessment_worlds',{'alias':'demo','assessment_id':'case','specification':spec})
                    self.assertEqual(result['worlds'][1]['actions']['swap_runner_for_receiver']['forecast_comparisons'][0]['teams']['a']['delta_points'],9)
                    pruned=await call('read_player_assessment',{'alias':'demo','assessment_id':'case','disabled_mechanisms':['volume_share']})
                    self.assertFalse(pruned['claim_status']['bx']['usable_as_driver'])
                    resource=await client.read_resource('moneyball://research/player-assessment')
                    self.assertIn('Use deep session reasoning first',resource.contents[0].text)
                    references=await client.read_resource('moneyball://research/repositories')
                    self.assertEqual(len(json.loads(references.contents[0].text)['references']),34)

if __name__=='__main__': unittest.main()
