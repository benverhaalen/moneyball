import gzip
import tempfile
import unittest

from moneyball.advanced import (_csv_records, _normalize, derive_opportunities, ingest_advanced,
                               PBP_COLUMNS, latest_depth_chart, materialize_opportunities)
from moneyball.sources import HttpFetcher
from moneyball.warehouse import Warehouse


class AdvancedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=Warehouse(self.tmp.name)

    def test_pbp_keeps_measured_fields_excludes_model_weights(self):
        body=gzip.compress(b'play_id,game_id,season,week,passing_yards,epa,wp,fantasy_points\n1,g,2025,1,12,99,.9,100\n')
        row=list(_csv_records(body,'csv.gz',columns=PBP_COLUMNS))[0]
        self.assertEqual(row['passing_yards'],12)
        self.assertNotIn('epa',row);self.assertNotIn('wp',row);self.assertNotIn('fantasy_points',row)

    def test_ngs_summary_and_missing_roster_identity_explicit(self):
        ngs,_=_normalize('ngs_passing',[{'player_gsis_id':'00-a','season':2025,'week':0}])
        self.assertEqual(ngs[0]['observation_type'],'season_summary')
        rosters,_=_normalize('weekly_rosters',[{'season':2025,'week':1,'team':'GB','full_name':'A','gsis_id':None}])
        self.assertFalse(rosters[0]['identity_verified'])
        self.assertTrue(rosters[0]['player_id'].startswith('unresolved:'))
        self.assertNotIn('active',rosters[0])

    def test_depth_timestamp_not_publication_backdating(self):
        body=b'dt,team,gsis_id,pos_abb\n2020-01-01T00:00:00Z,GB,00-a,QB\n'
        f=HttpFetcher(self.w,transport=lambda u,h:(200,{},body),clock=lambda:1_700_000_000,min_interval=0)
        result=ingest_advanced(self.w,datasets=['depth_charts'],seasons=[2020],fetcher=f)
        self.assertTrue(result['ok'])
        self.assertEqual(self.w.query('depth_charts',cutoff='2020-02-01T00:00:00Z'),[])
        r=self.w.query('depth_charts',cutoff=1_700_000_001)[0]
        self.assertEqual(r['_provenance']['available_at'],1_700_000_000)
        self.assertFalse(r['_provenance']['vintage_verified'])

    def test_gzip_absent_falls_back_to_documented_csv_and_resumes(self):
        calls=[]
        def transport(url,headers):
            calls.append(url)
            if url.endswith('.gz'):return 404,{},b''
            return 200,{},b'season,week,team,game_type,position,gsis_id\n2023,1,GB,REG,QB,00-a\n'
        f=HttpFetcher(self.w,transport=transport,clock=lambda:100,min_interval=0)
        r=ingest_advanced(self.w,datasets=['weekly_rosters'],seasons=[2023],fetcher=f)
        self.assertTrue(r['ok']);self.assertEqual(len(calls),2)
        again=ingest_advanced(self.w,datasets=['weekly_rosters'],seasons=[2023],fetcher=f,offline=True)
        self.assertTrue(again['ok']);self.assertTrue(again['receipts'][0]['unchanged'])
        self.assertEqual(len(calls),2)

    def test_bad_parse_quarantined_no_partial_rows(self):
        f=HttpFetcher(self.w,transport=lambda u,h:(200,{},b'a,a\n1,2\n'),clock=lambda:100,min_interval=0)
        r=ingest_advanced(self.w,datasets=['injuries'],seasons=[2025],fetcher=f)
        self.assertFalse(r['ok']);self.assertEqual(self.w.query('injuries',200),[])
        self.assertEqual(self.w.batches()[-1]['status'],'quarantined')

    def test_opportunity_counts_null_denominator_and_excluded_plays(self):
        base={'season':2025,'week':1,'game_id':'g','posteam':'GB','yardline_100':10,'play_type':'pass',
              'pass_attempt':1,'qb_dropback':1,'passer_player_id':'q','receiver_player_id':'w','air_yards':8,
              '_provenance':{'batch_id':7}}
        rows=[base,{**base,'receiver_player_id':None},
              {**base,'play_type':'no_play'}, {**base,'two_point_attempt':1}, {**base,'play_deleted':1},
              {**base,'play_type':'run','pass_attempt':0,'qb_dropback':0,'rush_attempt':1,'rusher_player_id':'r'},
              {**base,'play_type':'run','pass_attempt':0,'qb_dropback':0,'rush_attempt':1,'rusher_player_id':'q','qb_kneel':1}]
        d=derive_opportunities(rows);by={r['player_id']:r for r in d['rows']}
        self.assertEqual(by['w']['targets'],1);self.assertEqual(by['w']['red_zone_targets'],1)
        self.assertEqual(by['w']['target_share_of_eligible_pass_attempts'],.5)
        self.assertEqual(by['w']['team_unattributed_pass_attempts'],1)
        self.assertEqual(by['r']['rush_share_excluding_kneels'],1)
        self.assertEqual(by['q']['kneels'],1);self.assertEqual(by['q']['dropbacks'],2)
        self.assertEqual(d['input_batches'],[7])
        rush=derive_opportunities([{**base,'pass_attempt':0,'qb_dropback':0,'rush_attempt':1,'rusher_player_id':'r'}])['rows'][0]
        self.assertIsNone(rush['target_share_of_eligible_pass_attempts'])

    def test_latest_depth_is_per_team_and_respects_batch_availability(self):
        body=(b'dt,team,gsis_id,pos_abb\n'
              b'2020-01-01T00:00:00Z,GB,old,QB\n'
              b'2020-01-02T00:00:00Z,GB,new,QB\n'
              b'2020-01-01T00:00:00Z,KC,other,QB\n')
        f=HttpFetcher(self.w,transport=lambda u,h:(200,{},body),clock=lambda:1_700_000_000,min_interval=0)
        ingest_advanced(self.w,datasets=['depth_charts'],seasons=[2020],fetcher=f)
        self.assertEqual(latest_depth_chart(self.w,season=2020,cutoff=1_600_000_000),[])
        rows=latest_depth_chart(self.w,season=2020,cutoff=1_700_000_001)
        self.assertEqual({r['player_id'] for r in rows},{'new','other'})
        self.assertTrue(all(r['_provenance']['available_at']==1_700_000_000 for r in rows))

    def test_opportunity_artifact_lineage_cannot_use_future_inputs(self):
        body=(b'play_id,game_id,season,week,posteam,play_type,rush_attempt,rusher_player_id\n'
              b'1,g,2025,1,GB,run,1,00-r\n')
        f=HttpFetcher(self.w,transport=lambda u,h:(200,{},body),clock=lambda:100,min_interval=0)
        ingest_advanced(self.w,datasets=['pbp'],seasons=[2025],fetcher=f)
        unavailable=materialize_opportunities(self.w,seasons=[2025],cutoff=99,purpose='features')
        self.assertEqual(unavailable['receipts'][0]['status'],'no_eligible_input')
        result=materialize_opportunities(self.w,seasons=[2025],cutoff=101,purpose='features')
        receipt=result['receipts'][0]
        self.assertEqual(receipt['row_count'],1)
        self.assertEqual(receipt['input_batches'],[self.w.batches('pbp')[0]['batch_id']])
        self.assertEqual(receipt['cutoff'],101)


if __name__=='__main__':unittest.main()
