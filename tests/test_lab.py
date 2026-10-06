import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from moneyball import cli, lab, modeling
from moneyball.store import Store, digest
from moneyball.warehouse import Warehouse
from moneyball.providers import normalize_sleeper


class LabIntegrationTests(unittest.TestCase):
    """Synthetic, timestamped evidence; never private league/data fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.w = Warehouse(self.store.root/'lab')
        self.scoring = {'rec':1.0,'rec_yd':0.1,'pass_int':-1.0,'st_fum_rec':1.0}
        self.training_rows = [
            {'player_id':'history_a','position':'WR','season':2025,'week':week,
             'team':'LA','game_id':f'g{week}',
             'stats':{'rec':1,'rec_yd':10*week,'pass_int':0,'st_fum_rec':0}}
            for week in range(1,5)
        ]
        self.model = modeling.fit_model(self.training_rows,self.scoring,train_seasons=[2025])
        self.model = modeling.bind_variance_dependency(self.model,self.training_rows,cutoff=100)
        self.cache_metadata(100,age=22,gsis_id='history_a')
        self.season_row = {'player_id':'a','source_id':'sleeper_rotowire',
            'season':2026,'week':None,'position':'WR','fantasy_positions':['WR'],
            'name':'Synthetic Receiver','team':'LAR','mean':9999,
            'stats':{'gp':18},'scoring_hash':digest(self.scoring)}
        self.publish('projections',[self.season_row],partition='season')
        for week in range(1,19):
            if week != 4:  # Actual fixture bye, not a missing active forecast.
                self.publish('projections',[{**self.season_row,'week':week,
                    'mean':1000 if week==18 else 10}],partition=str(week))
        self.publish('adp',[{'player_id':'a','season':2026,'week':None,
            'adp_type':'adp_dynasty_2qb','adp':42}],partition='2qb')
        self.publish('adp',[{'player_id':'a','season':2026,'week':None,
            'adp_type':'adp_dynasty_ppr','adp':7}],partition='1qb')
        self.publish('schedules',[{'game_id':f'2026_{week}_LA_SEA','season':2026,
            'week':week,'game_type':'REG','home_team':'LA','away_team':'SEA'}
            for week in range(1,19) if week!=4],source='schedule',partition='2026',keys=('game_id',))

    def publish(self,dataset,rows,*,observed=100,partition='all',source='sleeper_rotowire',keys=('player_id',)):
        raw = self.w.put_raw(source,'https://example.test/'+dataset+'/'+partition,
                             json.dumps(rows).encode(),observed_at=observed)
        return self.w.publish(source,dataset,rows,raw_id=raw['id'],key_fields=keys,partition=partition)

    def cache_metadata(self,observed,*,age,gsis_id):
        with patch('moneyball.store.time.time',return_value=observed):
            self.store.cache('players/nfl',{'a':{'full_name':'Synthetic Receiver',
                'position':'WR','team':'LAR','age':age,'years_exp':2,'gsis_id':gsis_id}})

    def projection(self,cutoff=150):
        return lab.make_projection(self.w,self.store,self.model,season=2026,cutoff=cutoff,draws=8)

    def test_warehouse_to_projection_preserves_week_centers_and_team_alias(self):
        result = self.projection()
        player = result['players'][0]
        self.assertEqual(player['team'],'LA')
        self.assertEqual(player['source_team'],'LAR')
        self.assertEqual(player['weekly_means']['1'],10)
        self.assertEqual(player['weekly_means']['18'],1000)
        self.assertEqual(player['mean'],10)  # No season/gp division or week18 contamination.
        self.assertEqual(player['observed_week_mean'],10)
        self.assertEqual(player['missing_forecast_weeks'],[4])
        self.assertEqual(player['missing_nonbye_forecast_weeks'],[])
        self.assertEqual(player['adp'],42)  # Never substitute1QB demand field.
        self.assertEqual(player['historical_observations'],4)
        self.assertEqual(result['scoring_hash'],digest(self.scoring))
        self.assertTrue(result['input_batches'])

    def test_provider_component_audit_survives_week17_into_final_projection(self):
        before = self.projection()['players'][0]
        rows, _, _ = normalize_sleeper([{
            'player_id':'a','season':'2026','week':17,'company':'rotowire',
            'team':'LAR','player':{'first_name':'Synthetic','last_name':'Receiver',
                                  'position':'WR','fantasy_positions':['WR']},
            'stats':{'rec':4,'rec_yd':60,'pass_int':None}}],2026,17,self.scoring)
        self.publish('projections',rows,observed=200,partition='17')
        projected = modeling.project(self.model,[{'player_id':'a','position':'WR'}],
            season=2026,week=17,draws=8,forecast_rows=rows)['players'][0]
        self.assertEqual(projected['mean'],10)
        self.assertEqual(projected['null_scoring_fields'],['pass_int'])
        self.assertEqual(projected['absent_scoring_fields'],['st_fum_rec'])
        self.assertIn('observed_component_subtotal',projected['mean_definition'])
        player = self.projection(250)['players'][0]
        audit = player['weekly_component_metadata']['17']
        self.assertEqual(audit['null_scoring_fields'],['pass_int'])
        self.assertEqual(audit['absent_scoring_fields'],['st_fum_rec'])
        self.assertEqual(audit['missing_scoring_fields'],['pass_int','st_fum_rec'])
        self.assertEqual(player['projection_completeness'],'partial_sparse_zero_unverified')
        self.assertEqual(player['component_partial_weeks'],[17])
        self.assertIn(1,player['component_completeness_unknown_weeks'])
        self.assertNotIn(4,player['component_completeness_unknown_weeks'])  # The actual bye.
        self.assertEqual(player['known_missing_scoring_fields'],['pass_int','st_fum_rec'])
        self.assertIsNone(player['missing_scoring_fields'])  # Other weeks' audits are unknown.
        self.assertEqual(player['mean_interpretation'],'observed_component_subtotal')
        self.assertEqual(player['weekly_means'],before['weekly_means'])
        self.assertEqual(player['mean'],before['mean'])  # Metadata correction does not change forecasts.
        self.assertEqual(self.projection(150)['players'][0]['projection_completeness'],'unknown')

    def test_legacy_missing_component_metadata_is_unknown_not_complete(self):
        result = self.projection()
        player = result['players'][0]
        self.assertEqual(player['projection_completeness'],'unknown')
        self.assertEqual(player['mean_interpretation'],'unknown_component_completeness')
        self.assertEqual(player['weekly_component_metadata']['1']['projection_completeness'],'unknown')
        self.assertIsNone(player['weekly_component_metadata']['1']['missing_scoring_fields'])
        self.assertIsNone(player['missing_scoring_fields'])
        self.assertEqual(result['component_completeness_counts'],{'unknown':1})
        # Older provider rows sometimes had a misleading empty missing list.
        # That alone is not a modern, explicit completeness audit.
        legacy = {**self.season_row,'week':1,'mean':10,'missing_scoring_fields':[]}
        projected = modeling.project(self.model,[{'player_id':'a','position':'WR'}],
            season=2026,week=1,draws=8,forecast_rows=[legacy])['players'][0]
        self.assertEqual(projected['projection_completeness'],'unknown')
        self.assertIn('completeness_unknown',projected['mean_definition'])

    def test_saved_composite_model_roundtrip_reconstructs_uncertainty(self):
        original = self.projection()
        path = Path(self.tmp.name)/'model-current.json'
        lab.write_json(path,self.model)
        loaded = lab.read_json(path)
        self.assertEqual(loaded['variance_dependency']['training_data_hash'],loaded['training']['data_hash'])
        self.assertEqual(loaded['variance_dependency']['scoring_hash'],digest(self.scoring))
        self.assertEqual(loaded['variance_dependency']['cutoff'],100)
        self.assertEqual(loaded['variance_dependency']['input_batches'],[])
        self.assertEqual(loaded['id'],digest({k:v for k,v in loaded.items() if k!='id'}))
        with patch('moneyball.modeling.measure_variance_bins',side_effect=AssertionError('No variance refit during reconstruction')):
            rebuilt = lab.make_projection(self.w,self.store,loaded,season=2026,cutoff=150,draws=8)
        self.assertEqual(rebuilt['players'],original['players'])
        self.assertEqual(rebuilt['model_id'],original['model_id'])

    def test_projection_rejects_wrong_scoring_target_year_and_missing_variance(self):
        bad = copy.deepcopy(self.model)
        bad['variance_dependency']['scoring_hash'] = 'different-scoring'
        with self.assertRaisesRegex(modeling.ModelingError,'scoring mismatch'):
            lab.make_projection(self.w,self.store,bad,season=2026,cutoff=150,draws=8)
        with self.assertRaisesRegex(modeling.ModelingError,'overlap the target year'):
            lab.make_projection(self.w,self.store,self.model,season=2025,cutoff=150,draws=8)
        missing = copy.deepcopy(self.model)
        missing.pop('variance_dependency')
        with self.assertRaisesRegex(modeling.ModelingError,'Persisted variance dependency is missing'):
            lab.make_projection(self.w,self.store,missing,season=2026,cutoff=150,draws=8)

    def test_variance_content_tampering_and_future_cutoff_rejected(self):
        bad = copy.deepcopy(self.model)
        bad['variance_bins']['groups']['WR']['bins']['0']['residuals'][0] += 1
        with self.assertRaisesRegex(modeling.ModelingError,'content hash'):
            lab.make_projection(self.w,self.store,bad,season=2026,cutoff=150,draws=8)
        with self.assertRaisesRegex(modeling.ModelingError,'unavailable at the projection cutoff'):
            lab.make_projection(self.w,self.store,self.model,season=2026,cutoff=99,draws=8)

    def test_variance_binding_requires_exact_training_rows_and_reuses_existing_bins(self):
        fitted = modeling.fit_model(self.training_rows,self.scoring,train_seasons=[2025])
        changed = copy.deepcopy(self.training_rows)
        changed[0]['stats']['rec_yd'] += 1
        with self.assertRaisesRegex(modeling.ModelingError,'exactly the fitted training rows'):
            modeling.bind_variance_dependency(fitted,changed,cutoff=100)
        with patch('moneyball.modeling.measure_variance_bins',side_effect=AssertionError('Existing fitted bins must be reused')):
            bound = modeling.bind_variance_dependency(fitted,self.training_rows,cutoff=100)
        self.assertEqual(bound['variance_bins'],fitted['variance_bins'])
        self.assertNotEqual(bound['id'],fitted['id'])

    def test_cutoff_excludes_new_forecast_and_metadata_revisions(self):
        original = self.projection()
        self.cache_metadata(300,age=44,gsis_id='different_history')
        self.publish('projections',[{**self.season_row,'week':1,'mean':90}],
                     observed=300,partition='1')
        historical = self.projection(150)
        player = historical['players'][0]
        self.assertEqual(player['age'],22)
        self.assertEqual(player['historical_observations'],4)
        self.assertEqual(player['weekly_means']['1'],10)
        self.assertEqual(historical['input_batches'],original['input_batches'])
        current = self.projection(350)['players'][0]
        self.assertEqual(current['age'],44)
        self.assertEqual(current['weekly_means']['1'],90)
        self.assertEqual(current['mean'],15)

    def test_unarchived_current_metadata_is_not_backfilled_into_past(self):
        self.cache_metadata(300,age=44,gsis_id='history_a')
        player = self.projection(150)['players'][0]
        self.assertIsNone(player['age'])
        self.assertIsNone(player['years_exp'])
        self.assertEqual(player['historical_observations'],0)
        with self.assertRaisesRegex(ValueError,'Variance dependency was unavailable'):
            self.projection(99)

    def test_any_scoring_mismatch_including_late_week_is_rejected(self):
        self.publish('projections',[{**self.season_row,'week':17,'mean':10,
            'scoring_hash':'different-profile'}],observed=200,partition='17')
        self.projection(150)
        with self.assertRaisesRegex(ValueError,'different scoring profile'):
            self.projection(250)

    def test_missing_active_week_stays_missing_and_changes_heuristic(self):
        raw = self.w.put_raw('sleeper_rotowire','https://example.test/empty',b'[]',observed_at=200)
        self.w.publish('sleeper_rotowire','projections',[],raw_id=raw['id'],
                       key_fields=('player_id',),partition='17',allow_empty=True)
        player = self.projection(250)['players'][0]
        self.assertNotIn('17',player['weekly_means'])
        self.assertEqual(player['missing_nonbye_forecast_weeks'],[17])
        self.assertAlmostEqual(player['mean'],150/16)
        self.assertIn('assumed zero',player['mean_definition'])

    def test_dual_fantasy_eligibility_survives_nonstandard_nfl_position(self):
        self.publish('projections',[{**self.season_row,'position':'DB',
            'fantasy_positions':['WR','RB']}],observed=200,partition='season')
        player = self.projection(250)['players'][0]
        self.assertEqual(player['position'],'WR')
        self.assertEqual(player['fantasy_positions'],['WR','RB'])

    def test_cli_rejects_retrospective_cutoff_for_current_draft(self):
        stderr = io.StringIO()
        argv = ['moneyball','--data-dir',str(self.store.root),'lab','draft',
                '--offline','--cutoff','2026-09-01T00:00:00Z']
        with patch.object(sys,'argv',argv),contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code,1)
        self.assertIn('supported by lab query only',json.loads(stderr.getvalue())['error'])

    def test_refresh_passes_entire_exact_scoring_profile(self):
        self.store.record('test',{'league':{'scoring_settings':self.scoring}}, {})
        with patch('moneyball.lab.ingest',return_value={'ok':True}) as history, \
             patch('moneyball.providers.ingest_providers',return_value={'status':'ok'}) as providers:
            result = lab.refresh(self.w,self.store,'test',season=2026,force=True,offline=True)
        history.assert_called_once()
        providers.assert_called_once()
        self.assertEqual(providers.call_args.kwargs['scoring'],self.scoring)
        self.assertTrue(providers.call_args.kwargs['offline'])
        self.assertTrue(providers.call_args.kwargs['force'])
        self.assertEqual(providers.call_args.kwargs['weeks'],(1,15,16,17))
        self.assertTrue(Path(result['path']).is_file())


if __name__ == '__main__':
    unittest.main()
