import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball import live, drafting
from moneyball.store import Store
from moneyball.warehouse import Warehouse


def fixture():
    return [{'id':str(i),'name':'Player '+str(i),'position':('QB','RB','WR','TE')[i%4],
        'mean':max(0,25-i/15),'sd':0,'adp':i+1,'years_exp':2,
        'weekly_means':{str(w):max(0,25-i/15) for w in range(1,19)}} for i in range(350)]


def prefix(n=4):
    return [{'pick_no':i+1,'draft_slot':drafting.snake_owner(i+1),'player_id':str(i)} for i in range(n)]


class LiveTests(unittest.TestCase):
    def test_state_updates_exact_availability_and_real_sample_count(self):
        p=fixture(); before=live.state_from_picks(p,prefix())
        self.assertTrue(before['on_clock']); self.assertEqual(before['opponent_observations'],4)
        self.assertEqual(max(before['observations_per_opponent'].values()),1)
        self.assertNotIn('0',before['available'])
        after=live.state_from_picks(p,prefix(5))
        self.assertEqual(after['next_own_pick'],20)
        self.assertEqual(after['picks_before_own_turn'],14)
        self.assertEqual(after['opponent_observations'],4)
        self.assertNotEqual(before['prefix_hash'],after['prefix_hash'])

    def test_invalid_prefix_and_candidate_never_silently_fixed(self):
        p=fixture()
        for bad in (prefix()[1:],prefix()+prefix()[:1],prefix()+[{'pick_no':5,'player_id':'0'}],
                    [{'pick_no':1,'draft_slot':2,'player_id':'0'}]):
            with self.assertRaises(ValueError): live.state_from_picks(p,bad)
        with self.assertRaises(ValueError): live.shortlist(p,prefix(),requested=['0'])

    def test_selector_observed_prefix_is_immutable_and_checks_unavailable(self):
        p=fixture(); history=prefix(); original=copy.deepcopy(history)
        selected=[]
        def choose(team,pick,existing,available,rosters):
            selected.append(pick); return min(available,key=int)
        d=drafting.draft_once(p,observed_picks=history,forced_first='6',rival_selector=choose)
        self.assertEqual(history,original); self.assertEqual(d['trace'][:4],history)
        self.assertEqual(d['trace'][4]['player_id'],'6')
        self.assertTrue(all(n>5 for n in selected))
        with self.assertRaisesRegex(ValueError,'unavailable'):
            drafting.draft_once(p,observed_picks=history,rival_selector=lambda *a:'0')

    def test_small_live_run_retains_outer_vectors_and_refuses_certification(self):
        p=fixture(); projection={'players':p,'created_at':time.time()}
        result=live.evaluate(projection,prefix(),candidates=['4','5'],max_blocks=1,
            inner_draws=2,budget_seconds=30,modes=('adp',),years=1)
        self.assertEqual(result['completed_outer_blocks_per_mode'],1)
        self.assertIsNone(result['recommendation'])
        self.assertEqual(len(result['samples_by_mode']['adp']['4']),1)
        self.assertEqual(result['scenario_reports']['adp']['race']['n_independent_units'],1)
        self.assertEqual(result['scenario_reports']['adp']['rows'][0]['paired_ci']['comparisons'],1)
        self.assertEqual(result['fast_availability']['4']['target_pick'],20)

    def test_cancelled_generation_never_gets_a_choice(self):
        r=live.evaluate({'players':fixture(),'created_at':time.time()},prefix(),
            candidates=['4','5'],cancelled=lambda:True,max_blocks=1)
        self.assertEqual(r['stage'],'cancelled_new_state')
        self.assertIsNone(r['recommendation'])

    def test_final_turn_has_no_survival_prediction(self):
        r=live.evaluate({'players':fixture(),'created_at':time.time()},prefix(331),
            candidates=['331','332'],max_blocks=1,inner_draws=2,modes=('adp',))
        self.assertEqual(r['fast_availability']['status'],'no_following_own_pick')
        self.assertIsNone(r['scenario_reports']['adp']['rows'][0]['conditional_survival_to_following_own_pick'])

    def test_feed_stale_and_wrong_prefix_are_not_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)); w=Warehouse(store.root/'lab')
            feed=live.LiveFeed(store,w,{'picks':prefix()})
            key=live.prefix_key(prefix())
            feed.current.update(platform_status='drafting',last_checked_at=time.time())
            r={'stage':'state_updated','on_clock':True,'recommendation':'4','provisional_choice':'4'}
            self.assertTrue(feed.publish(key,r)); self.assertTrue(feed.snapshot()['actionable'])
            feed.current['last_checked_at']=time.time()-20
            self.assertFalse(feed.snapshot()['actionable'])
            self.assertIsNone(feed.snapshot()['result']['provisional_choice'])
            feed.current.update(last_checked_at=time.time(),platform_status='pre_draft')
            self.assertFalse(feed.snapshot()['actionable'])
            feed.current['prefix_hash']='changed'
            self.assertFalse(feed.publish(key,r))


if __name__=='__main__': unittest.main()
