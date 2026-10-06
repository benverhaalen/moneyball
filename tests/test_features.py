import json
from pathlib import Path
import tempfile
import unittest

from moneyball.features import enrich_features, measure_features, DEFINITIONS
from moneyball.modeling import METRICS
from moneyball.warehouse import Warehouse, WarehouseError


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=Warehouse(self.tmp.name)

    def publish(self,dataset,rows,at=100,keys=('player_id',),partition='2025'):
        self.w.register_source('nflverse_'+dataset,{})
        raw=self.w.put_raw('nflverse_'+dataset,'https://example.invalid/'+dataset,json.dumps(rows).encode(),observed_at=at)
        return self.w.publish('nflverse_'+dataset,dataset,rows,raw_id=raw['id'],key_fields=keys,partition=partition)

    def base(self,**changes):
        row={'player_id':'00-a','position':'WR','season':2025,'week':1,'season_type':'REG','game_id':'g','team':'GB',
             'identity_verified':True,'stats':{'targets':0,'receptions':0,'receiving_yards':0,'receiving_tds':0,'target_share':0}}
        row.update(changes);return row

    def test_exact_crosswalk_and_maximum_source_availability(self):
        self.publish('weekly',[self.base()])
        self.publish('players',[{'player_id':'00-a','gsis_id':'00-a','pfr_id':'Good00'}],at=300,partition='all')
        self.publish('snap_counts',[{'pfr_player_id':'Good00','game_id':'g','season':2025,'offense_snaps':20,'offense_pct':.4},
            {'pfr_player_id':'Wrong00','game_id':'g','season':2025,'player':'Same Name','offense_snaps':99,'offense_pct':1}],
            at=250,keys=('pfr_player_id','game_id'))
        early=enrich_features(self.w,seasons=[2025],cutoff=275)['rows'][0]
        self.assertIsNone(early['metrics']['offense_snaps'])
        self.assertEqual(early['_provenance']['available_at'],100)
        row=enrich_features(self.w,seasons=[2025],cutoff=350)['rows'][0]
        self.assertEqual(row['metrics']['offense_snaps'],20)
        self.assertEqual(row['_provenance']['available_at'],300)
        self.assertEqual(len(row['_provenance']['input_batches']),3)
        self.assertIsNone(row['metrics']['receiving_td_rate'])
        self.assertEqual(row['metric_denominators']['receiving_td_rate'],0)
        self.assertEqual(row['metrics']['targets'],0)
        coverage=enrich_features(self.w,seasons=[2025],cutoff=350)['coverage']['metrics']['receiving_td_rate']
        self.assertEqual(coverage['observation_status_counts']['denominator_nonpositive'],1)

    def test_ngs_week_zero_never_used_and_team_conflict_stays_missing(self):
        self.publish('weekly',[self.base(position='QB')])
        self.publish('ngs_passing',[{'player_id':'00-a','season':2025,'week':0,'season_type':'REG','team':'GB','avg_time_to_throw':99},
            {'player_id':'00-a','season':2025,'week':1,'season_type':'REG','team':'GB','avg_time_to_throw':2.5,'expected_completion_percentage':60}],
            at=200,keys=('player_id','season','week'))
        r=enrich_features(self.w,seasons=[2025],cutoff=300)
        self.assertEqual(r['rows'][0]['metrics']['ngs_time_to_throw'],2.5)
        self.assertAlmostEqual(r['rows'][0]['metrics']['ngs_expected_completion_rate'],.6)
        self.assertEqual(r['coverage']['ngs_source_audit']['ngs_passing']['week_zero_excluded'],1)
        self.publish('ngs_passing',[{'player_id':'00-a','season':2025,'week':1,'season_type':'REG','team':'KC','avg_time_to_throw':3}],
            at=400,keys=('player_id','season','week'))
        row=enrich_features(self.w,seasons=[2025],cutoff=500)['rows'][0]
        self.assertIsNone(row['metrics']['ngs_time_to_throw'])
        self.assertEqual(row['feature_join_status']['ngs_passing'],'team_conflict')

    def test_ambiguous_crosswalk_does_not_choose_available_name(self):
        self.publish('weekly',[self.base()])
        self.publish('rosters',[{'player_id':'00-a','gsis_id':'00-a','pfr_id':'one','season':2025},
            {'player_id':'00-a','gsis_id':'00-a','pfr_id':'two','season':2025}],keys=('player_id','pfr_id'))
        self.publish('snap_counts',[{'pfr_player_id':'one','game_id':'g','season':2025,'offense_snaps':30}],keys=('pfr_player_id','game_id'))
        row=enrich_features(self.w,seasons=[2025],cutoff=200)['rows'][0]
        self.assertIsNone(row['metrics']['offense_snaps'])
        self.assertEqual(row['feature_join_status']['snap_counts'],'ambiguous_exact_crosswalk')

    def test_opportunity_artifact_integrity_lineage_and_no_pbp_query(self):
        self.publish('weekly',[self.base()])
        batch=self.publish('pbp',[{'play_id':'1','season':2025}],at=200,keys=('play_id',))
        self.w.record_artifact('opportunities_2025',{'rows':[{'player_id':'00-a','season':2025,'week':1,'game_id':'g','red_zone_targets':2}],
            'input_batches':[batch['batch_id']]},input_batches=[batch['batch_id']])
        original=self.w.query
        def guard(dataset,*a,**kw):
            self.assertNotEqual(dataset,'pbp')
            return original(dataset,*a,**kw)
        self.w.query=guard
        early=enrich_features(self.w,seasons=[2025],cutoff=150)['rows'][0]
        self.assertIsNone(early['metrics']['red_zone_targets'])
        row=enrich_features(self.w,seasons=[2025],cutoff=250)['rows'][0]
        self.assertEqual(row['metrics']['red_zone_targets'],2)
        self.assertEqual(row['_provenance']['available_at'],200)
        with self.w.connect() as db:db.execute("UPDATE artifacts SET body='{}' WHERE name='opportunities_2025'")
        with self.assertRaises(WarehouseError):enrich_features(self.w,seasons=[2025],cutoff=250)

    def test_candidate_reports_are_descriptive_and_model_registry_unchanged(self):
        rows=[]
        for player,base in [('00-a',1),('00-b',4),('00-c',7)]:
            for week in range(1,5):
                rows.append(self.base(player_id=player,week=week,game_id='g'+str(week),stats={'targets':base+week,'receptions':base,'target_share':.1*base}))
        self.publish('weekly',rows,keys=('player_id','week'))
        enriched=enrich_features(self.w,seasons=[2025],cutoff=200)
        before=dict(METRICS)
        result=measure_features(enriched,warehouse=self.w,windows=(1,),gap=1)
        self.assertEqual(METRICS,before);self.assertEqual(len(DEFINITIONS),32)
        self.assertEqual(result['evidence_status'],'exploratory_historical_association')
        self.assertEqual(set(result['metrics']),set(DEFINITIONS))
        self.assertTrue(any(r['metric']=='targets' and r['kind']=='separated' and r['units']==3 for r in result['curves']))
        self.assertTrue(all('window_games' not in r for r in result['curves']))
        for path in result['files'].values():self.assertTrue(Path(path).exists())
        self.assertIn('2025 already inspected',self.w.hypotheses()[0]['body']['holdout_status'])


if __name__=='__main__':unittest.main()
