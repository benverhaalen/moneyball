"""Plausible bad bindings must never attach evidence to a different person."""
from copy import deepcopy
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.league.decision_demo import seed
from moneyball.league.identity import assertions, target_check
from moneyball.league.pipelines import warehouse, query, catalog
from moneyball.league.service import Service
from moneyball.league.store import DataError
from moneyball.sources import HttpFetcher, ingest


class BindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = Service(tmp.name)
        seed(self.service)
        self.league = deepcopy(self.service.snapshot('demo')['data'])
        self.league['platform'] = 'sleeper'
        self.league['players']['ar'].update(name='Synthetic Receiver', birth_date='2000-01-02', external_ids={'espn_id':'42'})
        self.service.store.put('league','demo',self.league)
        self.target_name = 'Synthetic Receiver'
        self.target_dob = '02/01/2000'
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        receipt = {'checked_at':time.time(), 'cache_hit':False}
        if '/athletes/' in url:
            return {'athlete':{'id':'42','displayName':self.target_name,'displayDOB':self.target_dob}}, receipt
        return {'feed':[{'id':1,'playerId':'42','headline':'Generic article unrelated to this person','published':'2026-10-01T00:00:00Z'}]}, receipt

    def crosswalk(self, rows):
        body = ('mfl_id,sleeper_id,espn_id,gsis_id\n'+'\n'.join(rows)+'\n').encode()
        w = warehouse(self.service)
        fetcher = HttpFetcher(w, transport=lambda *a:(200,{},body), min_interval=0)
        return ingest(w,datasets=['fantasy_ids'],seasons=[2026],fetcher=fetcher,force=True)

    def run_news(self, **kwargs):
        with patch('moneyball.league.news.Client.get',side_effect=self.get):
            return self.service.news_leads('demo',['ar'],**kwargs)['players'][0]

    def test_native_wrong_person_is_quarantined_even_with_agreeing_ids(self):
        self.league['players']['00133']=deepcopy(self.league['players']['ar'])
        self.league['players']['00133']['external_ids']['gsis_id']='00-0000001'
        self.service.store.put('league','demo',self.league)
        self.crosswalk(['1,00133,42,00-0000001'])
        self.target_name = 'Other Synthetic Receiver'
        with patch('moneyball.league.news.Client.get',side_effect=self.get):
            row=self.service.news_leads('demo',['00133'])['players'][0]
        self.assertEqual(row['status'],'identity_contradicted')
        self.assertNotIn('data',row)
        self.assertEqual(len(self.calls),1)
        self.assertFalse(row['identity_binding']['identity_verified'])

    def test_partial_row_without_espn_still_contradicts_other_ids(self):
        self.league['players']['00133']=deepcopy(self.league['players']['ar'])
        self.service.store.put('league','demo',self.league)
        self.crosswalk(['1,00133,42,00-0000001','2,00133,,00-0000002'])
        with patch('moneyball.league.news.Client.get') as get:
            row=self.service.news_leads('demo',['00133'])['players'][0]
            get.assert_not_called()
        self.assertEqual(row['status'],'identity_ambiguous')

    def test_source_duplicate_dob_conflict_is_not_lost_in_normalization(self):
        w=warehouse(self.service)
        body=b'mfl_id,sleeper_id,espn_id,gsis_id,birthdate,name\n1,00133,42,00-0000001,2000-01-02,Synthetic Receiver\n2,00133,42,00-0000001,1990-01-02,Synthetic Receiver\n'
        ingest(w,datasets=['fantasy_ids'],seasons=[2026],fetcher=HttpFetcher(w,transport=lambda *a:(200,{},body),min_interval=0),force=True)
        self.league['players']['00133']=deepcopy(self.league['players']['ar'])
        self.service.store.put('league','demo',self.league)
        with patch('moneyball.league.news.Client.get') as get:
            row=self.service.news_leads('demo',['00133'])['players'][0]
            get.assert_not_called()
        self.assertEqual(row['status'],'identity_ambiguous')

    def test_same_name_different_dob_is_not_identity(self):
        self.target_dob = '03/01/2000'
        row = self.run_news()
        self.assertEqual(row['status'],'identity_contradicted')
        self.assertNotIn('data',row)

    def test_same_id_revocation_detaches_old_leads_and_changes_manifest(self):
        first = self.run_news()
        self.assertEqual(first['status'],'discovered')
        self.target_name = 'Wrong Target'
        revoked = self.run_news(refresh=True)
        self.assertEqual(revoked['espn_id'],first['espn_id'])
        self.assertEqual(revoked['status'],'identity_contradicted')
        self.assertNotIn('data',revoked)
        self.assertNotEqual(revoked['identity_binding_version_id'],first['identity_binding_version_id'])
        with patch('moneyball.league.news.Client.get') as get:
            context = self.service.prepare_decision({'alias':'demo','intent':'research','question':'Check binding','player_ids':['ar'],'refresh':False})
            get.assert_not_called()
        self.assertTrue(any(m['kind']=='player_identity_binding' and m['version_id']==revoked['identity_binding_version_id'] for m in context['manifest']))
        self.assertNotIn('data',context['news_discovery']['players'][0])

    def test_recorded_current_decision_rejects_same_id_qualification_change(self):
        self.run_news()
        context=self.service.prepare_decision({'alias':'demo','intent':'research','question':'Assess roster','player_ids':['ar'],'refresh':False})
        comparison=self.service.compare_actions(context['decision_id'],[{'id':'hold','kind':'hold','week':5}])
        self.target_name='Wrong Target'
        self.run_news(refresh=True)
        with self.assertRaisesRegex(DataError,'Consumed evidence changed'):
            self.service.record_decision(context['decision_id'],{'comparison_id':comparison['comparison_id'],
                'choice':'hold','confidence':'conditional','strongest_countercase':'Other target',
                'reversal_conditions':['Identity qualification changed']})

    def test_historical_old_binding_retained_without_current_target_request(self):
        first = self.run_news()
        cutoff = time.time()
        self.target_name = 'Wrong Target'
        self.run_news(refresh=True)
        with patch('moneyball.league.news.Client.get') as get:
            old = self.service.news_leads('demo',['ar'],as_of=cutoff)['players'][0]
            get.assert_not_called()
        self.assertEqual(old['status'],'retained')
        self.assertEqual(old['version_id'],first['version_id'])
        self.assertEqual(old['identity_binding_version_id'],first['identity_binding_version_id'])

    def test_late_crosswalk_cannot_qualify_past_decision(self):
        p = self.league['players']['ar']; p['external_ids']={}
        self.league['players']['00133']=self.league['players'].pop('ar')
        self.service.store.put('league','demo',self.league)
        cutoff=time.time()
        self.crosswalk(['1,00133,42,'])
        before=assertions(self.service,'sleeper','00133',p,cutoff)
        now=assertions(self.service,'sleeper','00133',p)
        self.assertEqual(before['status'],'unresolved')
        self.assertEqual(now['espn_id'],'42')
        with patch('moneyball.league.news.Client.get') as get:
            old=self.service.news_leads('demo',['00133'],as_of=cutoff)['players'][0]
            get.assert_not_called()
        self.assertEqual(old['status'],'identity_unresolved')

    def test_duplicate_conflicts_are_order_independent_and_never_get(self):
        p=deepcopy(self.league['players']['ar']); p['external_ids']={}
        self.league['players']['00133']=p
        self.service.store.put('league','demo',self.league)
        for rows in [['1,00133,42,','2,00133,43,'],['2,00133,43,','1,00133,42,']]:
            self.assertTrue(self.crosswalk(rows)['ok'])
            with patch('moneyball.league.news.Client.get') as get:
                row=self.service.news_leads('demo',['00133'])['players'][0]
                get.assert_not_called()
            self.assertEqual(row['status'],'identity_ambiguous')
            self.assertEqual(len(row['identity_binding']['crosswalk_rows']),2)
            self.assertNotIn('data',row)

    def test_unique_crosswalk_fills_missing_native_id_but_not_proof(self):
        p=deepcopy(self.league['players']['ar']); p['external_ids']={}
        self.league['players']['00133']=p
        self.service.store.put('league','demo',self.league)
        self.crosswalk(['1,00133,42,','1,00133,42,'])
        with patch('moneyball.league.news.Client.get',side_effect=self.get):
            row=self.service.news_leads('demo',['00133'])['players'][0]
        self.assertEqual(row['status'],'discovered')
        self.assertEqual(row['identity_binding']['assertion_kind'],'third_party_assertion')
        self.assertFalse(row['identity_binding']['identity_verified'])
        self.assertEqual(len(row['identity_binding']['crosswalk_rows']),2)
        self.assertIn('unverified',row['data']['leads'][0]['association'])
        result=query(self.service,'fantasy_ids',seasons=[2026],source='dynastyprocess_ids')
        self.assertEqual(len(result['rows']),2)
        source=next(d for d in catalog(self.service)['datasets'] if d['dataset']=='fantasy_ids')
        self.assertEqual(source['source'],'dynastyprocess_ids')
        self.assertTrue(source['acquired'])

    def test_native_and_source_disagreement_is_not_resolved_by_priority(self):
        p=deepcopy(self.league['players']['ar'])
        self.league['players']['00133']=p
        self.service.store.put('league','demo',self.league)
        self.crosswalk(['1,00133,43,'])
        with patch('moneyball.league.news.Client.get') as get:
            row=self.service.news_leads('demo',['00133'])['players'][0]
            get.assert_not_called()
        self.assertEqual(row['status'],'identity_conflict')

    def test_pending_target_cannot_replace_concurrent_revocation(self):
        first=self.run_news()
        def race(url,**kwargs):
            old=self.service.store.get('player_identity_binding','sleeper:ar')['data']
            old.update(status='contradicted',qualification_token='revoked')
            self.service.store.put('player_identity_binding','sleeper:ar',old)
            return self.get(url,**kwargs)
        with patch('moneyball.league.news.Client.get',side_effect=race):
            row=self.service.news_leads('demo',['ar'],refresh=True)['players'][0]
        self.assertNotIn('data',row)
        current=self.service.store.get('player_identity_binding','sleeper:ar')
        self.assertEqual(current['data']['qualification_token'],'revoked')

    def test_pending_news_cannot_attach_after_concurrent_revocation(self):
        self.run_news()
        def race(url,**kwargs):
            if '/athletes/' not in url:
                old=self.service.store.get('player_identity_binding','sleeper:ar')['data']
                old.update(status='contradicted',qualification_token='revoked')
                self.service.store.put('player_identity_binding','sleeper:ar',old)
            return self.get(url,**kwargs)
        with patch('moneyball.league.news.Client.get',side_effect=race):
            row=self.service.news_leads('demo',['ar'])['players'][0]
        self.assertNotIn('data',row)
        self.assertEqual(row['status'],'identity_changed')

    def test_older_positive_target_cannot_undo_newer_contradiction(self):
        self.run_news()
        self.target_name='Wrong Target'
        revoked=self.run_news(refresh=True)
        self.target_name='Synthetic Receiver'
        def old(url,**kwargs):
            payload,receipt=self.get(url,**kwargs)
            receipt['checked_at']=revoked['identity_binding']['target_receipt']['checked_at']-100
            return payload,receipt
        with patch('moneyball.league.news.Client.get',side_effect=old):
            row=self.service.news_leads('demo',['ar'])['players'][0]
        self.assertEqual(row['status'],'identity_contradicted')
        self.assertEqual(row['identity_binding_version_id'],revoked['identity_binding_version_id'])
        self.assertNotIn('data',row)

    def test_target_schema_and_formatting_are_not_search(self):
        with self.assertRaises(DataError):
            target_check({'athlete':{'id':'43','displayName':'Synthetic Receiver'}},'42',self.league['players']['ar'])
        _,status,_,gaps=target_check({'athlete':{'id':'42','displayName':'Synthetic-Receiver'}},'42',self.league['players']['ar'])
        self.assertEqual(status,'metadata_consistent_assertion')
        self.assertTrue(gaps)


if __name__=='__main__':
    unittest.main()
