import copy
import unittest

from moneyball.rare_stats import (COMPONENTS, SPECIFICATIONS, aggregate_history,
    evaluate, fit_rate_model, impute_components, rate_for, select_development)
from moneyball.warehouse import LeakageError


def weekly(pid='a', season=2020, week=1, **extra):
    r = {'player_id':pid, 'season':season, 'week':week, 'season_type':'REG', 'position':'QB',
         'passing_interceptions':1,'attempts':50, 'rushing_fumbles_lost':0,'carries':5,
         'receiving_fumbles_lost':0,'receptions':1,'sack_fumbles_lost':0,'sacks_suffered':3,
         'fumbles_lost_total':0,'_provenance':{'observed_at':10,'available_at':10}}
    return {**r, **extra}


def panel():
    return aggregate_history([weekly(season=2019), weekly(season=2020,passing_interceptions=3),
        weekly(pid='b',season=2019,passing_interceptions=0,attempts=100),
        weekly(pid='b',season=2020,passing_interceptions=1,attempts=100)],cutoff=20)[0]


class RareStatsTests(unittest.TestCase):
    def test_no_absence_inferred_and_component_specific_missingness(self):
        p,a=aggregate_history([weekly(receptions=None),weekly(week=2)],cutoff=20)
        self.assertEqual(len(p),1)
        self.assertEqual(p[0]['observed_rows'],2)
        self.assertEqual(p[0]['components']['rec_fum_lost']['missing_rows'],1)
        self.assertEqual(p[0]['components']['pass_int']['exposure'],100)
        self.assertEqual(a['player_seasons'],1)

    def test_partial_component_season_not_used_as_complete_risk_set(self):
        p,_=aggregate_history([weekly(receptions=None),weekly(week=2),weekly(pid='b')],cutoff=20)
        m=fit_rate_model(p,'rec_fum_lost',before_season=2021,specification=SPECIFICATIONS[0])
        self.assertEqual(m['pools']['ALL']['exposure'],1)

    def test_invalid_and_duplicate_counts_fail(self):
        for r in (weekly(attempts=0),weekly(carries=-1),weekly(receptions=float('nan')),
                  weekly(passing_interceptions=.5),weekly(fumbles_lost_total=-1)):
            with self.assertRaises(ValueError):aggregate_history([r],cutoff=20)
        with self.assertRaises(ValueError):aggregate_history([weekly(),weekly()],cutoff=20)

    def test_unavailable_history_rejected(self):
        with self.assertRaises(LeakageError):aggregate_history([weekly()],cutoff=9)

    def test_postseason_excluded_and_fumble_residual_retained(self):
        p,a=aggregate_history([weekly(fumbles_lost_total=1),weekly(week=19,season_type='POST')],cutoff=20)
        self.assertEqual(a['unmodeled_other_fumbles_lost'],1)
        self.assertEqual(p[0]['observed_rows'],1)

    def test_future_outcomes_cannot_change_rate_fit(self):
        p=panel()
        spec=SPECIFICATIONS[2]
        a=fit_rate_model(p,'pass_int',before_season=2020,specification=spec)
        changed=copy.deepcopy(p)
        for r in changed:
            if r['season']==2020:r['components']['pass_int']['events']=40
        b=fit_rate_model(changed,'pass_int',before_season=2020,specification=spec)
        self.assertEqual(a,b)

    def test_credibility_respects_exposure_and_pool_fallback(self):
        spec=next(s for s in SPECIFICATIONS if s['name']=='all_k100')
        m=fit_rate_model(panel(),'pass_int',before_season=2020,specification=spec)
        rate=rate_for(m,'a','QB')
        # One interception in 50 personal attempts; pool 1 in 150 with explicit regularizer.
        self.assertAlmostEqual(rate['rate'],(1+100*1.5/151)/150)
        self.assertEqual(rate_for(m,'new','QB')['method'],'position_pool')
        self.assertEqual(rate_for(m,'new','WR')['method'],'all_position_fallback')
        self.assertIsNone(rate['rate_confidence_interval'])

    def test_raw_zero_is_a_rate_estimate_not_missing(self):
        m=fit_rate_model(panel(),'pass_int',before_season=2020,specification=SPECIFICATIONS[1])
        self.assertEqual(rate_for(m,'b','QB')['rate'],0)

    def test_risk_set_and_count_error_denominators(self):
        m=fit_rate_model(panel(),'pass_int',before_season=2020,specification=SPECIFICATIONS[2])
        r=evaluate(m,panel(),2020)
        self.assertEqual((r['players'],r['player_seasons'],r['exposures'],r['events']),(2,2,150,4))
        self.assertEqual(r['mse'],2.5)  # predictions 1,0 against observations 3,1

    def test_final_holdout_forbidden_in_development(self):
        p,_=aggregate_history([weekly(season=2025)],cutoff=20)
        with self.assertRaises(ValueError):select_development(p)
        self.assertEqual(len(SPECIFICATIONS)*len(COMPONENTS),76)

    def artifact(self):
        return {'status':'complete','forecast_season':2026,'models':{
            c:fit_rate_model(panel(),c,before_season=2021,specification=SPECIFICATIONS[0]) for c in COMPONENTS}}

    def test_imputation_preserves_forecast_zero_and_missing_exposures(self):
        r=impute_components(self.artifact(),'a','QB',{'pass_att':500,'rush_att':0}, {'pass_int':0})
        self.assertEqual(r['components']['pass_int']['mean'],0)
        self.assertEqual(r['components']['rush_fum_lost']['mean'],0)
        self.assertIsNone(r['components']['sack_fum_lost']['mean'])
        self.assertIsNone(r['offensive_fumbles_lost_subtotal'])
        self.assertIsNone(r['fum_lost'])

    def test_total_forecast_wins_and_median_rejected(self):
        r=impute_components(self.artifact(),'a','QB',{'pass_att':500,'rush_att':10,'rec':0,'pass_sack':30}, {'fum_lost':2})
        self.assertEqual(r['fum_lost'],2)
        self.assertGreater(r['offensive_fumbles_lost_subtotal'],0)
        with self.assertRaises(ValueError):impute_components(self.artifact(),'a','QB',{},exposure_kind='median')
        with self.assertRaises(ValueError):impute_components(self.artifact(),'a','QB',{'pass_att':float('inf')})


if __name__=='__main__':unittest.main()
