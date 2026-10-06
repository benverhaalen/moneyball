import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from moneyball.features import DEFINITIONS
from moneyball.incremental import (bootstrap_crps,residual_distribution,fit_ridge,predict_ridge,
                                  make_lagged_rows,evaluate_incremental,run_incremental)
from moneyball.warehouse import Warehouse,ValidationError


class IncrementalTests(unittest.TestCase):
    def sample(self):
        rng=random.Random(42);rows=[]
        for season in [2018,2023]:
            for player in range(12):
                values=[rng.randint(0,4) for _ in range(16)]
                for week in range(1,17):
                    past=values[max(0,week-4):week-1]
                    points=4+3*(sum(past)/len(past) if past else 2)+rng.uniform(-.2,.2)
                    metrics={k:None for k in DEFINITIONS};metrics.update(targets=8+player%3,red_zone_targets=values[week-1])
                    rows.append({'player_id':'p'+str(player),'season':season,'week':week,'position':'WR','season_type':'REG',
                        'stats':{'receiving_yards':points*10},'metrics':metrics,'_provenance':{'available_at':100}})
        return {'rows':rows,'definitions':DEFINITIONS,'coverage':{'seasons':[2018,2023]},'input_batches':[],'cutoff':200}

    def test_empirical_bootstrap_crps_matches_pairwise_definition(self):
        for values,center,observed in [([-1,1],0,0),([-2,0,1,7],2,3),([1,1],10,12)]:
            expected=sum(abs(center+x-observed) for x in values)/len(values)-sum(abs(a-b) for a in values for b in values)/(2*len(values)**2)
            self.assertAlmostEqual(bootstrap_crps(residual_distribution(values),center,observed),expected)

    def test_lags_ignore_current_values_and_do_not_bridge_missing_weeks(self):
        sample=self.sample();lagged,_=make_lagged_rows(sample,{'rec_yd':.1},train_seasons=[2018])
        before=next(r for r in lagged if r['player_id']=='p0' and r['season']==2023 and r['week']==10)
        for row in sample['rows']:
            if row['player_id']=='p0' and row['season']==2023 and row['week']==10:
                row['metrics']['red_zone_targets']=999
        after=next(r for r in make_lagged_rows(sample,{'rec_yd':.1},train_seasons=[2018])[0]
                   if r['player_id']=='p0' and r['season']==2023 and r['week']==10)
        self.assertEqual(before['candidate_lags'],after['candidate_lags'])
        sample['rows']=[r for r in sample['rows'] if not (r['player_id']=='p0' and r['season']==2023 and r['week']==8)]
        rows=make_lagged_rows(sample,{'rec_yd':.1},train_seasons=[2018])[0]
        weeks={r['week'] for r in rows if r['player_id']=='p0' and r['season']==2023}
        self.assertFalse(weeks & {1,2,3,9,10,11});self.assertIn(12,weeks)

    def test_actual_incremental_signal_and_identical_common_support(self):
        sample=self.sample()
        # One missing source observation removes affected lags from both models.
        next(r for r in sample['rows'] if r['player_id']=='p0' and r['season']==2023 and r['week']==8)['metrics']['red_zone_targets']=None
        result=evaluate_incremental(sample,{'rec_yd':.1,'st_ff':1},train_seasons=[2018],test_seasons=[2023])
        row=next(r for r in result['results'] if r['metric']=='red_zone_targets')
        self.assertGreater(row['mse_improvement'],0);self.assertGreater(row['crps_improvement'],0)
        self.assertLess(row['positions'][0]['test_rows'],row['positions'][0]['test_baseline_eligible'])
        self.assertEqual(row['rows'],row['test_rows'])
        self.assertEqual(result['lag_audit']['omitted_nonzero_scoring_keys'],{'st_ff':1})
        self.assertEqual(result['comparison_family_size'],32)
        self.assertEqual(next(r for r in result['results'] if r['metric']=='targets')['status'],'not_tested_already_in_baseline')
        self.assertTrue(result['failures']);self.assertNotIn('p_value',json.dumps(result))

    def test_forbidden_holdout_and_nonforward_splits_fail_before_evaluation(self):
        sample=self.sample()
        with self.assertRaises(ValidationError):evaluate_incremental(sample,{'rec_yd':.1},train_seasons=[2018],test_seasons=[2025])
        with self.assertRaises(ValidationError):evaluate_incremental(sample,{'rec_yd':.1},train_seasons=[2023],test_seasons=[2018])
        contaminated=copy.deepcopy(sample);contaminated['rows'][0]['season']=2025
        with self.assertRaises(ValidationError):evaluate_incremental(contaminated,{'rec_yd':.1},train_seasons=[2018],test_seasons=[2023])

    def test_ridge_scaling_is_fitted_only_on_training_data(self):
        model=fit_ridge([[0],[1],[2]],[0,2,4],penalty=1)
        frozen=copy.deepcopy(model);prediction=predict_ridge(model,[100])
        self.assertEqual(model,frozen);self.assertEqual(model['means'],[1])
        self.assertGreater(prediction,100)

    def test_registration_precedes_scoring_and_files_retain_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=Warehouse(tmp);sample=self.sample()
            def enrich(*args,**kwargs):
                event=json.loads((Path(tmp)/'research-events.jsonl').read_text().splitlines()[0])
                self.assertEqual(event['kind'],'preregister_incremental_features')
                self.assertIn('CRPS AND MSE',event['sign_rule'])
                return sample
            with patch('moneyball.incremental.enrich_features',side_effect=enrich):
                result=run_incremental(w,{'rec_yd':.1},train_seasons=[2018],test_seasons=[2023])
            saved=json.loads(Path(result['files']['json']).read_text())
            self.assertGreater(saved['failure_count'],0)
            self.assertIn('EXPLORATORY',saved['evidence_status'])
            self.assertTrue(Path(result['files']['csv']).exists())
            self.assertEqual(len((Path(tmp)/'research-events.jsonl').read_text().splitlines()),2)


if __name__=='__main__':unittest.main()
