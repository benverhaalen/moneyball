import copy
import unittest
from moneyball.draft_universe import make_universe


def fixture():
    p={'available_at':10,'observed_at':10}
    adp=[{'player_id':'1','name':'Example Player','position':'WR','fantasy_positions':['WR'],
          'adp':15,'adp_type':'adp_dynasty_2qb','week':None,'_provenance':p}]
    meta={'1':{'full_name':'Example Player','birth_date':'2000-01-01','position':'WR',
               'fantasy_positions':['WR'],'gsis_id':None}}
    nfl=[{'display_name':'Example Player','birth_date':'2000-01-01','position':'WR',
          'gsis_id':' 00-000001 ','identity_verified':True,'_provenance':p}]
    return adp,meta,nfl


class UniverseTests(unittest.TestCase):
    def test_no_projection_needed_and_whitespace_id_normalized(self):
        rows=make_universe(*fixture(),metadata_observed_at=10,as_of=20)
        self.assertEqual(rows[0]['gsis_id'],'00-000001')
        self.assertEqual(rows[0]['cohort_rank'],1)

    def test_name_alone_does_not_join_but_player_is_retained(self):
        adp,meta,nfl=fixture();meta['1']['birth_date']=None
        rows=make_universe(adp,meta,nfl,metadata_observed_at=10,as_of=20)
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['gsis_id'])

    def test_conflicting_evidence_rejected(self):
        adp,meta,nfl=fixture();meta['1']['gsis_id']='other'
        nfl.append({**nfl[0],'gsis_id':'other','display_name':'Different','birth_date':'1990-01-01'})
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            make_universe(adp,meta,nfl,metadata_observed_at=10,as_of=20)

    def test_source_after_cutoff_and_duplicate_adp_rejected(self):
        with self.assertRaises(ValueError):make_universe(*fixture(),metadata_observed_at=30,as_of=20)
        a,m,n=fixture();a.append(copy.deepcopy(a[0]))
        with self.assertRaises(ValueError):make_universe(a,m,n,metadata_observed_at=10,as_of=20)

    def test_position_exclusion_and_double_eligible_fullback(self):
        a,m,n=fixture();m['1'].update(position='FB',fantasy_positions=['RB'])
        rows=make_universe(a,m,n,metadata_observed_at=10,as_of=20)
        self.assertEqual(rows[0]['position'],'RB')
        m['1'].update(position='K',fantasy_positions=['K'])
        self.assertEqual(make_universe(a,m,n,metadata_observed_at=10,as_of=20),[])
