import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from moneyball import draft_advisor_watch as advisor
from moneyball.draft_fastlane import compile_plan
from moneyball.draft_room import write_json
from tests.test_draft_fastlane import fixture, SOURCE, NOW, MOCK
from tests.test_mock_lab import ui as old_ui, cell


def scene():
    authored, _, deck, _, _ = fixture()
    setup = {'schema_version':1,'session_name':'assigned-private-mock','headless':True,
        'mode':'private_mock','own_slot':5,'setup_observed_at':'2026-09-08T21:00:00Z',
        'page_websocket_url':'ws://127.0.0.1:9222/devtools/page/EXPLICIT-TARGET',
        'league':{'scoring_settings':{'rec':1},'roster_positions':['QB','WR','FLEX','BN'],'settings':{}},
        'draft':{'draft_id':MOCK,'league_id':None,'type':'snake','sport':'nfl','status':'pre_draft',
                 'metadata':{'type':'league_mock'},'settings':{'teams':12,'rounds':28,'pick_timer':120},
                 'slot_to_roster_id':{'5':7}}}
    catalog = [{'player_id':pid,'name':c['name'],'position':c['position'],'team':c['identity']['team']}
               for pid,c in deck['cards'].items()]
    def frame(pick):
        value = old_ui(pick); value['source']='ui'
        value['candidates'] = [{'name':c['name'],'position':c['position'],'team':c['team'],
                               'row_index':i,'composite_row_count':1,'enabled':True,'draft_button_present':True}
                              for i,c in enumerate(catalog)]
        value['slate_capture'] = {'top_limit':24,'rendered_count':len(catalog),
                                  'captured_top_count':len(catalog),'complete_top_prefix':True}
        return value
    prior = advisor.normalize(frame(6),setup,catalog,now=NOW)
    assert prior['problems']==[], prior
    plan = compile_plan(authored,deck,prior['board'],prepared_at=NOW-120)
    registry = {'observed_at':'2026-09-08T21:04:00Z','news_revision':authored['news_revision'],
                'sources':{SOURCE['path']:SOURCE['sha256']},'unreviewed_fact_ids':[]}
    return setup,plan,deck,catalog,registry,frame(20)


def put_inputs(path, values):
    names = ['setup','plan','deck','catalog','registry']
    for name,value in zip(names,values): write_json(path/(name+'.json'),value)
    return {name+'_path':path/(name+'.json') for name in names}


class AdvisorTests(unittest.TestCase):
    def test_observation_only_fresh_own_turn_never_produces_advice(self):
        setup,_,_,catalog,_,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'setup.json',setup);write_json(p/'catalog.json',catalog)
            watch=advisor.AdvisorWatch(setup_path=p/'setup.json',catalog_path=p/'catalog.json',
                output=p/'out',capture=lambda *_:frame,observe_only=True)
            result=watch.tick(now=NOW)
            self.assertEqual(result['mode'],'observe_only');self.assertEqual(result['status'],'observation_only')
            self.assertTrue(result['normalized']['board']['on_clock'])
            self.assertFalse(result['result']['ready_to_recommend']);self.assertEqual(result['result']['options'],[])
            self.assertEqual(result['first_own_turn_observations']['20'],frame['observed_at'])
            self.assertFalse(result['advice_evaluated'])

    def test_observation_only_failure_retains_scope_and_frame(self):
        setup,_,_,catalog,_,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'setup.json',setup);write_json(p/'catalog.json',catalog)
            watch=advisor.AdvisorWatch(setup_path=p/'setup.json',catalog_path=p/'catalog.json',
                output=p/'out',capture=lambda *_:frame,observe_only=True)
            first=watch.tick(now=NOW)
            def fail(*_):raise TimeoutError('capture unavailable')
            watch.capture=fail;result=watch.tick(now=NOW+1)
            self.assertEqual(result['status'],'capture_failed');self.assertEqual(result['mode'],'observe_only')
            self.assertFalse(result['advice_evaluated']);self.assertFalse(result['result']['ready_to_recommend'])
            self.assertEqual(result['raw_frame_path'],first['raw_frame_path'])

    def test_observation_only_never_loads_advisory_inputs_or_calls_evaluator(self):
        setup,_,_,catalog,_,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'setup.json',setup);write_json(p/'catalog.json',catalog)
            watch=advisor.AdvisorWatch(setup_path=p/'setup.json',catalog_path=p/'catalog.json',
                plan_path=p/'missing-plan',deck_path=p/'missing-deck',registry_path=p/'missing-registry',
                output=p/'out',capture=lambda *_:frame,observe_only=True)
            with patch.object(advisor.draft_fastlane,'evaluate',side_effect=AssertionError('must never execute')) as evaluate:
                result=watch.tick(now=NOW)
            self.assertEqual(result['status'],'observation_only');evaluate.assert_not_called()
            self.assertEqual(set(result['input_receipts']),{'setup','catalog'})

    def test_normalizes_actual_legacy_shape_and_all_squads(self):
        setup,_,_,catalog,_,frame=scene()
        value=advisor.normalize(frame,setup,catalog,now=NOW)
        self.assertEqual(value['problems'],[])
        self.assertEqual(value['board']['picks_made'],19)
        self.assertEqual(value['board']['own_roster'],['1005'])
        self.assertEqual(len(value['board']['squads']),12)
        self.assertEqual(value['current_pick'],20)
        self.assertTrue(all(r['identity_evidence']=='unique_name_position_team_join' for r in value['candidate_identities']))

    def test_paused_stale_wrong_room_and_autopick_are_not_ready(self):
        setup,_,_,catalog,_,frame=scene()
        for mutate,code in [
            (lambda x:x.update(url='https://sleeper.com/draft/nfl/'+MOCK+'/wrong'),'wrong_assigned_room'),
            (lambda x:x.update(ownAutoPick=True),'own_autopick_not_explicitly_off'),
            (lambda x:x.update(observed_at='2026-09-08T21:04:00Z'),'frame_not_fresh'),
            (lambda x:x.update(clock='Paused'),'paused')]:
            x=copy.deepcopy(frame);mutate(x)
            self.assertIn(code,advisor.normalize(x,setup,catalog,now=NOW)['problems'])

    def test_incomplete_prefix_and_missing_own_identity_block(self):
        setup,_,_,catalog,_,frame=scene()
        frame['cells']=[c for c in frame['cells'] if c['id']!='draft-cell-7']
        self.assertIn('drafted_prefix_incomplete',advisor.normalize(frame,setup,catalog,now=NOW)['problems'])
        setup,_,_,catalog,_,frame=scene();frame['own']='wrong team'
        self.assertIn('missing_or_wrong_own_column',advisor.normalize(frame,setup,catalog,now=NOW)['problems'])

    def test_catalog_collision_missing_team_and_incomplete_slate_block(self):
        setup,_,_,catalog,_,frame=scene();catalog.append(copy.deepcopy(catalog[0]))
        self.assertIn('candidate_identity_needs_review',advisor.normalize(frame,setup,catalog,now=NOW)['problems'])
        setup,_,_,catalog,_,frame=scene();del frame['candidates'][0]['team']
        self.assertIn('candidate_identity_needs_review',advisor.normalize(frame,setup,catalog,now=NOW)['problems'])
        setup,_,_,catalog,_,frame=scene();frame['candidates'].pop(0)
        self.assertIn('top_slate_rows_missing_or_duplicate',advisor.normalize(frame,setup,catalog,now=NOW)['problems'])

    def test_writes_full_frame_hashed_pointer_and_exact_prepared_advice(self):
        setup,plan,deck,catalog,registry,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);kwargs=put_inputs(p,[setup,plan,deck,catalog,registry])
            watch=advisor.AdvisorWatch(**kwargs,output=p/'out',capture=lambda *_:frame)
            result=watch.tick(now=NOW)
            self.assertEqual(result['status'],'advice_ready',result)
            self.assertGreater(result['valid_until'],NOW)
            self.assertEqual(json.loads(Path(result['raw_frame_path']).read_text()),frame)
            self.assertEqual(json.loads((p/'out/decision.json').read_text()),result)
            self.assertIn(plan['authored']['branches'][0]['exact_user_text'],(p/'out/readable.md').read_text())
            self.assertFalse(result['writes_to_sleeper'])
            self.assertFalse(result['result']['ready_to_execute'])

    def test_capture_failure_revokes_advice_and_retains_recovery_state(self):
        *inputs,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,inputs),output=p/'out',capture=lambda *_:frame)
            first=watch.tick(now=NOW)
            def fail(*_):raise TimeoutError('fixed capture timeout')
            watch.capture=fail;result=watch.tick(now=NOW+1)
            self.assertEqual(result['status'],'capture_failed')
            self.assertFalse(result['result']['ready_to_recommend'])
            self.assertEqual(result['raw_frame_path'],first['raw_frame_path'])
            self.assertFalse(result['retained_frame_is_current'])
            self.assertIn('No currently valid advice',(p/'out/readable.md').read_text())

    def test_sniped_candidate_uses_fallback_and_records_diff(self):
        setup,plan,deck,catalog,registry,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,[setup,plan,deck,catalog,registry]),output=p/'out',capture=lambda *_:frame)
            watch.tick(now=NOW)
            frame=copy.deepcopy(frame)
            frame['cells']=[cell(6,'11564') if c['id']=='draft-cell-6' else c for c in frame['cells']]
            frame['candidates']=[r for r in frame['candidates'] if r['name']!='Drake Maye']
            for i,r in enumerate(frame['candidates']):r['row_index']=i
            frame['slate_capture'].update(rendered_count=5,captured_top_count=5)
            result=watch.tick(now=NOW+1)
            self.assertEqual(result['status'],'advice_ready',result)
            self.assertEqual(result['result']['options'][0]['player_id'],'7564')
            self.assertNotIn('Drake Maye',(p/'out/readable.md').read_text())
            self.assertTrue(result['diff']['removed_or_revised_prefix'])

    def test_novel_slider_means_comparison_not_stopped_system(self):
        setup,plan,deck,catalog,registry,frame=scene()
        catalog.append({'player_id':'9999','name':'Newly Seen Player','position':'WR','team':'NE'})
        frame['candidates'].append({'row_index':6,'name':'Newly Seen Player','position':'WR','team':'NE',
                                    'enabled':True,'draft_button_present':True,'composite_row_count':1})
        frame['slate_capture'].update(rendered_count=7,captured_top_count=7)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,[setup,plan,deck,catalog,registry]),output=p/'out',capture=lambda *_:frame)
            result=watch.tick(now=NOW)
            self.assertEqual(result['status'],'needs_comparison')
            self.assertIn('novel_slider_needs_review',result['problems'])
            self.assertEqual(result['result']['options'],[])
            self.assertEqual(result['first_own_turn_observations']['20'],frame['observed_at'])
            self.assertEqual(watch.tick(now=NOW+1)['status'],'needs_comparison')

    def test_registry_reload_invalidates_prior_news_and_missing_controls(self):
        setup,plan,deck,catalog,registry,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,[setup,plan,deck,catalog,registry]),output=p/'out',capture=lambda *_:frame)
            self.assertEqual(watch.tick(now=NOW)['status'],'advice_ready')
            registry['news_revision']='new-unreviewed-report';write_json(p/'registry.json',registry)
            self.assertIn('news_revision_changed',watch.tick(now=NOW+1)['problems'])
            registry['news_revision']=plan['authored']['news_revision'];write_json(p/'registry.json',registry)
            frame['candidates'][1]['enabled']=False
            self.assertFalse(watch.tick(now=NOW+2)['result']['ready_to_recommend'])

    def test_registry_freshness_receipt_ttl_is_enforced_not_retimestamped(self):
        setup,plan,deck,catalog,registry,frame=scene()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,[setup,plan,deck,catalog,registry]),output=p/'out',capture=lambda *_:frame)
            for stamp,code in [(None,'registry_freshness_receipt_missing'),
                               ('2026-09-08T21:04:21Z','registry_freshness_receipt_in_future'),
                               ('2026-09-08T20:59:19Z','registry_freshness_receipt_stale')]:
                registry['observed_at']=stamp;write_json(p/'registry.json',registry)
                result=watch.tick(now=NOW)
                self.assertIn(code,result['problems']);self.assertEqual(result['valid_until'],NOW)
                self.assertFalse(result['result']['ready_to_recommend'])
                self.assertEqual(json.loads((p/'registry.json').read_text())['observed_at'],stamp)

    def test_loop_continues_after_failure_without_llm_and_stops_visibly(self):
        *inputs,frame=scene();calls=[];clock=[0.0]
        def capture(*_):
            calls.append(1)
            if len(calls)==1:raise TimeoutError('first observation unavailable')
            return frame
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);watch=advisor.AdvisorWatch(**put_inputs(p,inputs),output=p/'out',capture=capture)
            count=watch.run(max_duration=5,interval=2,monotonic=lambda:clock[0],sleep=lambda v:clock.__setitem__(0,clock[0]+v))
            self.assertEqual(count,3);self.assertEqual(len(calls),3)
            final=json.loads((p/'out/decision.json').read_text())
            self.assertEqual(final['status'],'observer_stopped')
            self.assertFalse(final['result']['ready_to_recommend'])
            self.assertIn('raw_frame_path',final)

    def test_atomic_text_never_exposes_partial_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'readable.md';p.write_text('old complete value')
            with patch.object(Path,'replace',side_effect=OSError('write interruption')):
                with self.assertRaises(OSError):advisor.atomic_text(p,'new complete value')
            self.assertEqual(p.read_text(),'old complete value')

    def test_live_transport_only_fixed_evaluate_and_no_remote_or_launch_fallback(self):
        setup,_,_,_,_,frame=scene();sent=[]
        class Wire:
            def send(self,s):sent.append(json.loads(s))
            def recv(self):return json.dumps({'id':1,'result':{'result':{'value':json.dumps(frame)}}})
            def settimeout(self,t):self.timeout=t
            def close(self):self.closed=True
        wire=Wire()
        value=advisor.capture_existing_page({'setup':setup,'watched_names':[]},connect=lambda *a,**k:wire)
        self.assertEqual(value,frame);self.assertEqual([s['method'] for s in sent],['Runtime.evaluate'])
        self.assertFalse(sent[0]['params']['userGesture']);self.assertTrue(wire.closed)
        setup['page_websocket_url']='ws://example.com:9222/devtools/page/OTHER'
        with self.assertRaises(ValueError):advisor.capture_existing_page({'setup':setup,'watched_names':[]},connect=lambda *_:self.fail())

    def test_capture_helper_hard_timeout_is_below_five_seconds(self):
        setup,*_=scene()
        with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('helper',4.5)) as call:
            with self.assertRaises(subprocess.TimeoutExpired):advisor.ExistingPageCapture()(setup,[])
        argv=call.call_args.args[0]
        self.assertEqual(argv[-1],'_capture');self.assertEqual(call.call_args.kwargs['timeout'],4.5)
        self.assertNotIn('agent-browser',argv)


if __name__=='__main__':unittest.main()
