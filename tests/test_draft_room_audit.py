"""Independent adversarial checks for the prepared-choice safety boundary."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from moneyball.draft_room import POLICY, REASONS, board_state, check, compare, prepare, verify_current_references
from moneyball.store import digest


def deck_fixture():
    cards={pid:{'player_id':pid,'name':'Synthetic '+pid,'position':'QB','identity':{'adp':adp,'team':'SYN'},'forecasts':[],'review':None,'review_excerpts':{},'full_review':'','packet_path':'synthetic','calibrated_title_delta':None} for pid,adp in [('a',1),('b',999),('c',5),('d',6)]}
    return {'policy':copy.deepcopy(POLICY),'cards':cards,'content_hash':digest(cards)}


def board_fixture():
    return {'draft_id':'synthetic-draft','own_roster':[],'rules_hash':'rules','next_own_pick':5,'following_own_pick':20,'prefix_hash':'synthetic-prefix','taken':[],'on_clock':True,'checked_at':100,'starters':['QB','SUPER_FLEX']}


def plan_fixture():
    choice={'player_id':'a','alternative_id':'b','requires_available':['c']}
    choice.update({key:'Explicit conditional analyst reason with observable reversal evidence.' for key in REASONS})
    return {'target_pick':5,'choices':[choice]}


class IndependentDraftRoomAudit(unittest.TestCase):
    def setUp(self):
        self.deck=deck_fixture();self.board=board_fixture();self.plan=prepare(plan_fixture(),self.deck,self.board)

    def test_elapsed_or_future_board_timestamp_cannot_be_ready(self):
        for checked_at in (None,80,101):
            board=copy.deepcopy(self.board);board['checked_at']=checked_at
            self.assertFalse(check(self.plan,self.deck,board,now=100)['ready_to_execute'])

    def test_changed_rules_or_existing_own_roster_invalidates(self):
        for key,value in [('rules_hash','new'),('own_roster',['d'])]:
            board=copy.deepcopy(self.board);board[key]=value
            self.assertFalse(check(self.plan,self.deck,board,now=100)['ready_to_execute'])

    def test_taken_declared_continuation_blocks_prepared_choice(self):
        board=copy.deepcopy(self.board);board['taken']=['c']
        self.assertFalse(check(self.plan,self.deck,board,now=100)['ready_to_execute'])

    def test_modified_evidence_or_policy_invalidates(self):
        for field in ('cards','policy'):
            deck=copy.deepcopy(self.deck)
            if field=='cards':deck['cards']['a']['full_review']='New factual evidence'
            else:deck['policy']['version']=99
            self.assertFalse(check(self.plan,deck,self.board,now=100)['ready_to_execute'])

    def test_mutated_prepared_candidate_is_rejected_not_silently_approved(self):
        self.plan['choices'][0]['player_id']='d'
        try: result=check(self.plan,self.deck,self.board,now=100)
        except ValueError:return
        self.assertFalse(result['ready_to_execute'],'Mutated unbound candidate passed the stale-choice gate')

    def test_preparation_does_not_alias_live_roster(self):
        self.board['own_roster'].append('d')
        self.assertEqual(self.plan['own_roster'],[],'Prepared snapshot aliases mutable live roster')
        self.assertFalse(check(self.plan,self.deck,self.board,now=100)['ready_to_execute'])

    def test_preparation_does_not_alias_mutable_input_choices(self):
        original=plan_fixture();bound=prepare(original,self.deck,self.board)
        original['choices'][0]['player_id']='d'
        self.assertEqual(bound['choices'][0]['player_id'],'a')

    def test_rank_extremes_do_not_create_ranking_or_title_probabilities(self):
        result=compare(self.deck,self.board,['b','a'])
        self.assertEqual([c['player_id']for c in result['candidates']],['b','a'])
        self.assertIsNone(result['recommendation'])
        for c in result['candidates']:
            self.assertIsNone(c['calibrated_title_delta'])
            self.assertTrue(c['acquisition_only']['not_a_survival_probability'])

    def test_real_snake_position_arithmetic_from_contiguous_synthetic_prefix(self):
        info={'status':'drafting','type':'snake','draft_id':'synthetic','settings':{'teams':12,'rounds':28},'slot_to_roster_id':{str(i):i for i in range(1,13)}}
        data={'league':{'roster_positions':['QB','SUPER_FLEX','BN'],'settings':{},'scoring_settings':{}},'my_roster_id':5,'drafts':[{'info':info,'picks':[{'pick_no':i,'player_id':'synthetic-'+str(i),'draft_slot':i}for i in range(1,5)],'traded_picks':[]}]}
        state=board_state(data,checked_at=100)
        self.assertEqual((state['next_own_pick'],state['following_own_pick']),(5,20))
        self.assertTrue(state['on_clock'])
        data['drafts'][0]['picks'].append({'pick_no':5,'player_id':'own-1','draft_slot':5})
        state=board_state(data,checked_at=100)
        self.assertEqual((state['next_own_pick'],state['following_own_pick']),(20,29))
        self.assertFalse(state['on_clock'])

    def test_invalid_team_count_fails_as_clear_validation_error(self):
        data={'league':{'roster_positions':['QB'],'settings':{},'scoring_settings':{}},'my_roster_id':5,'drafts':[{'info':{'status':'drafting','type':'snake','draft_id':'synthetic','settings':{'teams':0,'rounds':28},'slot_to_roster_id':{'5':5}},'picks':[],'traded_picks':[]}]}
        with self.assertRaises(ValueError):board_state(data,checked_at=100)

    def test_target_loss_selects_pre_reasoned_fallback_without_new_inference(self):
        before=copy.deepcopy(self.board);before['on_clock']=False
        inputs=plan_fixture()
        backup=copy.deepcopy(inputs['choices'][0])
        backup.update(player_id='b', alternative_id='d', requires_available=[])
        backup['why_over_alternative']='Prepared fallback: this specific roster path was evaluated between turns.'
        inputs['choices'].append(backup)
        prepared=prepare(inputs,self.deck,before)
        self.assertFalse(check(prepared,self.deck,before,now=100)['ready_to_execute'])
        after=copy.deepcopy(self.board);after['taken']=['a'];after['prefix_hash']='new-prefix'
        result=check(prepared,self.deck,after,now=100)
        self.assertTrue(result['ready_to_execute'])
        self.assertEqual(result['first_viable_prepared_choice'],backup)
        self.assertIsNone(compare(self.deck,after,['b','d'])['recommendation'])

    def test_lost_continuation_invalidates_that_choice_but_not_independent_fallback(self):
        inputs=plan_fixture();backup=copy.deepcopy(inputs['choices'][0])
        backup.update(player_id='b',alternative_id='d',requires_available=[])
        inputs['choices'].append(backup);prepared=prepare(inputs,self.deck,self.board)
        after=copy.deepcopy(self.board);after['taken']=['c']
        result=check(prepared,self.deck,after,now=100)
        self.assertTrue(result['ready_to_execute'])
        self.assertEqual(result['first_viable_prepared_choice']['player_id'],'b')

    def test_all_prepared_choices_taken_does_not_invent_third_choice(self):
        inputs=plan_fixture();backup=copy.deepcopy(inputs['choices'][0])
        backup.update(player_id='b',alternative_id='d',requires_available=[])
        inputs['choices'].append(backup);prepared=prepare(inputs,self.deck,self.board)
        after=copy.deepcopy(self.board);after['taken']=['a','b']
        result=check(prepared,self.deck,after,now=100)
        self.assertFalse(result['ready_to_execute'])
        self.assertIsNone(result['first_viable_prepared_choice'])

    def test_revised_earlier_picks_cannot_reuse_prepared_strategy(self):
        before=copy.deepcopy(self.board);before['taken']=['d']
        prepared=prepare(plan_fixture(),self.deck,before)
        after=copy.deepcopy(before);after['taken']=['different-early-player']
        result=check(prepared,self.deck,after,now=100)
        self.assertFalse(result['ready_to_execute'])
        self.assertIn('draft_prefix_revised',result['problems'])

    def reference_fixture(self, directory):
        root=Path(directory);packets=root/'draft/player-packets';reviews=root/'research/player-reviews'
        packets.mkdir(parents=True);reviews.mkdir(parents=True)
        packet=packets/'a.json';packet.write_text('{"synthetic":true}')
        review=reviews/'a.md';review.write_text('Synthetic current individual case')
        evidence=reviews/'a.reviewed.json';evidence.write_text('{"player_id":"a","synthetic":true}')
        sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        (packets/'index.json').write_text(json.dumps({'players':[{'player_id':'a','sha256':sha(packet)}]}))
        (reviews/'batch-synthetic.json').write_text(json.dumps({'reviews':[{'player_id':'a','review_sha256':sha(review)}]}))
        deck={'cards':{'a':{'packet_sha256':sha(packet),'packet_path':str(packet),'review':{'review_sha256':sha(review),'review_path':str(review),'evidence_path':str(evidence),'evidence_sha256':sha(evidence)}}}}
        return root,deck,packet,review,evidence

    def test_latest_reference_validation_detects_disk_edits_not_just_cached_deck(self):
        for component in ('packet','review','evidence'):
            with self.subTest(component=component),tempfile.TemporaryDirectory() as directory:
                root,deck,packet,review,evidence=self.reference_fixture(directory)
                verify_current_references(root,deck,['a'])
                {'packet':packet,'review':review,'evidence':evidence}[component].write_text('Revised after compilation')
                with self.assertRaises(ValueError):verify_current_references(root,deck,['a'])

    def test_latest_reference_validation_detects_new_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root,deck,*_=self.reference_fixture(directory)
            deck['cards']['a']['review']=None
            with self.assertRaisesRegex(ValueError,'publication changed'):
                verify_current_references(root,deck,['a'])

if __name__=='__main__':unittest.main()
