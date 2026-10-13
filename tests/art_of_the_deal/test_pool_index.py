import tempfile
import unittest
from moneyball.league.operations import acquisition_context
from moneyball.league.service import Service
from moneyball.league.store import DataError
from test_service import league


class PoolIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.s=Service(self.temp.name)
        self.d,_=league(); self.d['platform']='sleeper'
        self.s.store.put('league','x',self.d)
        self.s.store.put('config','x',{'platform':'sleeper'})
        self.catalog={str(i):{'full_name':f'Candidate {i:03}', 'position':'RB','active':True} for i in range(75)}
        for tid,t in self.d['teams'].items():
            for pid in t['player_ids']:
                self.catalog[pid]={'full_name':'Owned','position':'RB','active':True}
        self.catalog['inactive']={'full_name':'Inactive','position':'RB','active':False}
        self.s.store.put('catalog','sleeper',self.catalog)

    def test_complete_membership_all_teams_and_bounded_pages(self):
        a=acquisition_context(self.s,'x',positions=['RB'],limit=50)
        index=self.s.store.get('pool_index','x')['data']
        self.assertEqual(len(index['players']),75)
        self.assertEqual(len(a['players']),50)
        self.assertFalse(a['coverage']['execution_eligibility_verified'])
        owned={p for t in self.d['teams'].values() for p in t['player_ids']}
        self.assertFalse(owned & {r['player_id'] for r in index['players']})
        self.assertNotIn('inactive',{r['player_id'] for r in index['players']})
        # A changed catalog after the first page must not enter its continuation.
        self.catalog['later']={'full_name':'AAA Later','position':'RB','active':True}
        self.s.store.put('catalog','sleeper',self.catalog)
        b=acquisition_context(self.s,'x',positions=['RB'],limit=50,cursor=a['next_cursor'])
        self.assertEqual(len(b['players']),25)
        self.assertFalse({r['player_id'] for r in a['players']} & {r['player_id'] for r in b['players']})
        self.assertNotIn('later',{r['player_id'] for r in b['players']})
        self.assertTrue(all(r['acquisition_status']=='UNOWNED_STATUS_UNVERIFIED' for r in b['players']))
        with self.assertRaises(DataError):
            acquisition_context(self.s,'x',positions=['WR'],cursor=a['next_cursor'])

    def test_name_search_is_applied_before_limit(self):
        packet=acquisition_context(self.s,'x',query='Candidate 074',limit=1)
        self.assertEqual(packet['players'][0]['player_id'],'74')
