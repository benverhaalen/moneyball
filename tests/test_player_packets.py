import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from moneyball.player_packets import assemble_packets, build, time_check
from moneyball.store import digest


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=datetime(2026,9,8,tzinfo=timezone.utc).timestamp()
        self.player={'player_id':'1','name':'Player One','position':'QB','fantasy_positions':['QB'],
            'team':'BUF','gsis_id':'gsis1','cohort_rank':1,'adp':5,'observed_name_aliases':['Player One'],
            'adp_provenance':{'available_at':100,'observed_at':100},'identity_provenance':None}
        self.cohort={'players':[self.player],'season':2026,'built_at':100,'information_cutoff':100}
        self.universe=copy.deepcopy(self.cohort)
        self.league={'season':'2026','scoring_settings':{'pass_yd':.04,'fum_lost':-2}}
        self.meta={'players':{},'observed_at':100}
        self.sources={}

    def assemble(self,forecasts=()):
        return assemble_packets(self.cohort,self.universe,self.sources,list(forecasts),self.meta,
                                self.league,cutoff=self.cutoff,expected_count=1)

    def test_missing_is_not_zero_or_completed_review(self):
        p=self.assemble()[0][0]
        self.assertEqual(p['professional_forecasts']['season'],[])
        self.assertIsNone(p['contract_context'])
        self.assertIsNone(p['calibrated_ranking'])
        self.assertFalse(p['in_depth_review_completed'])
        self.assertEqual(p['identity']['adp'],5)
        self.assertIsNone(p['decision_outputs']['marginal_title_effect'])

    def test_exact_count_duplicate_and_identity_change_fail(self):
        with self.assertRaises(ValueError):
            assemble_packets(self.cohort,self.universe,{},[],self.meta,self.league,cutoff=self.cutoff)
        self.universe['players'][0]['gsis_id']='other'
        with self.assertRaises(ValueError):self.assemble()
        self.universe=copy.deepcopy(self.cohort);self.cohort['players'].append(self.player)
        with self.assertRaises(ValueError):self.assemble()

    def test_future_source_is_rejected_future_game_date_is_not(self):
        self.sources={'news':[{'known_at':self.cutoff+1,'players':[]}]}
        with self.assertRaises(ValueError):self.assemble()
        time_check({'start_time':'2027-01-01','contract_expiration_year':2028},self.cutoff)

    def test_scoring_mismatch_quarantined_and_provenance_preserved(self):
        r={'player_id':'1','source_id':'pro','season':2026,'week':None,'scoring_hash':'wrong',
            'stats':{'pass_yd':0,'fum_lost':None},'_provenance':{'available_at':100,'observed_at':100}}
        p=self.assemble([r])[0][0]
        self.assertEqual(p['professional_forecasts']['season'],[])
        self.assertIn('different_scoring_hash',p['quarantined_evidence'][0]['reasons'])
        r['scoring_hash']=digest(self.league['scoring_settings']);p=self.assemble([r])[0][0]
        self.assertEqual(p['professional_forecasts']['season'][0],r)
        self.assertIsNone(p['professional_forecasts']['season'][0]['stats']['fum_lost'])

    def test_provider_median_and_redraft_scope_stay_references(self):
        r={'player_id':'1','source_player_id':'other','source_id':'DS','known_at':100,'season':2026,
            'identity_match':'observed','position':'QB','format':{'type':'redraft','superflex':False},
            'conditioning':'median; floor excludes injury','raw_reference_metrics':{'fantasy_points':'200'}}
        self.sources={'rank_preview':[r]};p=self.assemble()[0][0]
        self.assertEqual(p['provider_references']['rank_preview'],[r])
        self.assertEqual(p['professional_forecasts']['season'],[])

    def test_wrong_market_target_horizon_is_quarantined(self):
        self.sources={'market_ladders':{'ladders':[{'player_id':'1','season':2026,'horizon':'single_week_or_game',
            'kind':'exchange','statistic':'fantasy_pts','observed_at':100}]}}
        p=self.assemble()[0][0]
        self.assertEqual(p['market_component_ladders'],[])
        self.assertEqual(len(p['quarantined_evidence']),1)

    def test_pp_duplicate_identity_retained_only_in_quarantine(self):
        row={'player_name':'Player One','position':'QB','team':'BUF','requires_identity_review':True,
            'identity_warnings':['duplicate source ID'],'duration':'Full','stat_type':'Fantasy Score',
            'status':'pre_game','is_live':False,'in_game':False,'threshold':17.5,'observed_at':100,
            'game_start_time':'2026-09-10T00:20:00+00:00'}
        self.sources={'fantasy_lines':{'rows':[row]}};p=self.assemble()[0][0]
        self.assertEqual(p['weekly_fantasy_score_quotes'],[])
        self.assertEqual(p['quarantined_evidence'][0]['source_record']['threshold'],17.5)

    def test_pp_january_rollover_and_corroborated_source_team_alias(self):
        self.player['team']='JAX';self.universe=copy.deepcopy(self.cohort)
        row={'player_name':'Player One','position':'QB','team':'JAC','duration':'Full',
            'stat_type':'Fantasy Score','status':'pre_game','threshold':17.5,'observed_at':100,
            'game_start_time':'2027-01-03T18:00:00+00:00',
            'raw_player':{'attributes':{'market':'Jacksonville','team_name':'Jaguars'}}}
        self.sources={'fantasy_lines':{'rows':[row]}};p=self.assemble()[0][0]
        self.assertEqual(len(p['weekly_fantasy_score_quotes']),1)
        self.assertEqual(p['weekly_fantasy_score_quotes'][0]['packet_calendar_season'],2026)
        self.assertFalse(p['weekly_fantasy_score_quotes'][0]['league_week_eligibility_verified'])
        row['game_start_time']='2027-09-10T18:00:00+00:00';p=self.assemble()[0][0]
        self.assertEqual(p['weekly_fantasy_score_quotes'],[])
        self.assertIn('different_or_unknown_fantasy_quote_season',p['quarantined_evidence'][0]['reasons'])
        row['game_start_time']='2026-09-10T18:00:00+00:00';row['raw_player']={}
        p=self.assemble()[0][0]
        self.assertIn('source_team_disagrees',p['quarantined_evidence'][0]['reasons'])

    def test_wrong_gsis_contract_record_never_attached(self):
        self.sources={'contracts':{'players':[{'player_id':'1','position':'QB','gsis_id':'other'}]}}
        p=self.assemble()[0][0]
        self.assertIsNone(p['contract_context'])
        self.assertIn('source_gsis_disagrees',p['quarantined_evidence'][0]['reasons'])

    def test_sensitivity_remains_conditional_with_stable_reference(self):
        record={'name':'Player One','position':'QB','championship_odds':None,
            'professional_component_reference':{'complete_league_projection':False,'subtotal':120},
            'scenarios':[{'scenario_id':'wide','source_id':'book','action_condition':'conditional on action',
                'core_scoring':{'subtotal':125,'missing_core_components':['fum_lost']}}]}
        body={'created_at':100,'information_cutoff':100,'target':'NFL2026 full regular season; includes week18',
            'players':{'1':record},'marginals':[{'large_models':'not copied'}],
            'failures':[{'player_id':'1','reason':'unresolved market scope'}]}
        self.sources={'market_sensitivity':body};p=self.assemble()[0][0]
        self.assertEqual(p['market_sensitivity']['record'],record)
        self.assertEqual(p['market_sensitivity']['json_pointer'],'/players/1')
        self.assertNotIn('marginals',p['market_sensitivity'])
        self.assertEqual(p['coverage']['market_sensitivity_scenarios'],1)
        self.assertIsNone(p['decision_outputs']['marginal_title_effect'])
        body['created_at']=self.cutoff+1
        with self.assertRaises(ValueError):self.assemble()
        body['created_at']=100;body['target']='NFL2027 full regular season; includes week18'
        with self.assertRaises(ValueError):self.assemble()

    def test_snapshot_publish_exact_400_and_failed_rebuild_preserves_index(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'draft').mkdir()
            players=[]
            for i in range(400):
                p=copy.deepcopy(self.player);p.update(player_id=str(i),name='Player '+str(i),cohort_rank=i+1,
                    observed_name_aliases=['Player '+str(i)],gsis_id='gsis'+str(i));players.append(p)
            cohort={**self.cohort,'players':players}
            for filename in ('top400-cohort.json','player-universe.json'):
                (root/'draft'/filename).write_text(json.dumps(cohort))
            class FakeStore:
                def __init__(inner,*args):pass
                def resolve_alias(inner, alias=None):return 'synthetic-league'
                def latest(inner,*args):return {'created':100,'hash':'leaguehash','data':{'league':self.league}}
                def cached(inner,*args):return {'fetched':100,'hash':'metahash','data':{}}
            class FakeWarehouse:
                def __init__(inner,*args):pass
                def query(inner,*args,**kwargs):return []
            with patch('moneyball.player_packets.Store',FakeStore),patch('moneyball.player_packets.Warehouse',FakeWarehouse):
                result=build(root,as_of=self.cutoff)
                index_path=root/'draft/player-packets/index.json';first=index_path.read_bytes();index=json.loads(first)
                self.assertEqual(len(index['players']),400)
                self.assertEqual(len({p['player_id'] for p in index['players']}),400)
                for p in index['players']:self.assertTrue((index_path.parent/p['path']).exists())
                self.assertEqual(build(root,as_of=self.cutoff),result)
                self.assertEqual(index_path.read_bytes(),first)
                one=index_path.parent/index['players'][0]['path'];saved=one.read_bytes()
                one.write_text('corrupt')
                with self.assertRaisesRegex(ValueError,'corrupt'):build(root,as_of=self.cutoff)
                self.assertEqual(index_path.read_bytes(),first)
                one.write_bytes(saved)
                (root/'draft/top400-cohort.json').write_text(json.dumps({**cohort,'players':players[:-1]}))
                with self.assertRaises(ValueError):build(root,as_of=self.cutoff)
                self.assertEqual(index_path.read_bytes(),first)
                self.assertEqual(result['player_count'],400)
