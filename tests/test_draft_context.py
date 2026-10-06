import copy
import unittest

from moneyball import draft_context as context
from moneyball.draft_room import POLICY
from moneyball.store import digest


def fixture():
    cards = {str(i): {'player_id':str(i), 'name':'Player '+str(i), 'position':pos,
             'identity': {'team':'A', 'adp':None if i == 9 else i},
             'full_review':'Case '+str(i)+'\n\nContrary evidence and future uncertainty must survive.\n\nReversal condition.',
             'packet_path':'/external/'+str(i)+'.json', 'review':{'review_path':'/external/'+str(i)+'.md'}}
             for i, pos in enumerate(['QB','RB','WR','TE','WR','QB','WR','RB','WR'],1)}
    deck = {'cards':cards,'policy':copy.deepcopy(POLICY),'content_hash':'fixture-deck'}
    board = {'draft_id':'mock','own_slot':5,'rules_hash':'exact-rules','taken':['1'],
             'picks_made':1,'own_roster':[],'next_own_pick':5,'following_own_pick':20,'checked_at':100}
    authored = {'schema_version':1,'horizon_policy':'Current and future feasible championships; no assumed numerical discount.',
                'user_preferences':[],'roster_obligations':[], 'failure_exposures':[], 'unresolved_questions':[],
                'news_revision':'observed-v1','watched_player_ids':['9'],
                'screenings':{'2':{'status':'defer','reason':'Conditional role needs checking.',
                  'reopen_if':'Different roster need or a changed role.','roster_sensitive':True,'card_hash':digest(cards['2'])}},
                'open_comparisons':[{'comparison_id':'two-v-three','player_ids':['2','3'],
                   'requires_available':['2'],
                   **{key:'Explicit uncertain analyst reasoning for '+key for key in context.COMPARISON_FIELDS},
                   'evidence_spans':[{'player_id':'2','paragraph_id':'1',
                                      'sha256':digest(cards['2']['full_review'].split('\n\n')[1])}]}]}
    return deck,board,authored


class ContextTests(unittest.TestCase):
    def test_recovery_does_not_need_chat_or_prior_log(self):
        d,b,a = fixture(); ledger=context.checkpoint(a,d,b)
        fresh={**b,'taken':['1','4'],'picks_made':2}
        r=context.recover(ledger,d,fresh,news_revision='observed-v1')
        self.assertEqual(r['new_picks'],['4'])
        self.assertFalse(r['state_requires_reconsideration'])
        self.assertEqual(r['comparisons'][0]['best_counterargument'],a['open_comparisons'][0]['best_counterargument'])

    def test_snipe_revokes_comparison_but_not_unrelated_evidence(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        r=context.recover(ledger,d,{**b,'taken':['1','2'],'picks_made':2},news_revision='observed-v1')
        self.assertEqual(r['comparisons'][0]['lost_dependencies'],['2'])
        self.assertIn('required_player_drafted',r['comparisons'][0]['requires_reconsideration'])
        self.assertEqual(r['changed_evidence'],[])

    def test_sniped_counterchoice_invalidates_without_duplicate_manual_declaration(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        r=context.recover(ledger,d,{**b,'taken':['1','3'],'picks_made':2},news_revision='observed-v1')
        self.assertEqual(r['comparisons'][0]['lost_dependencies'],['3'])

    def test_new_own_pick_reopens_roster_sensitive_rejections(self):
        d,b,a=fixture(); a['screenings']['2']['status']='reject_for_this_roster'
        ledger=context.checkpoint(a,d,b)
        fresh={**b,'taken':['1','4'],'own_roster':['4'],'picks_made':2}
        r=context.recover(ledger,d,fresh,news_revision='observed-v1')
        self.assertTrue(r['screenings']['2']['needs_rescreening'])
        self.assertTrue(r['state_requires_reconsideration'])

    def test_unknown_adp_and_off_screen_players_remain_in_search(self):
        d,b,a=fixture(); idx=context.index_deck(d); ledger=context.checkpoint(a,d,b)
        r=context.recover(ledger,d,b,news_revision='observed-v1')
        f=context.frontier(idx,b,r,visible=['3','900'],watched=['9'])
        self.assertIn('9',f['unscreened_ids_by_position']['WR'])
        self.assertIn('8',f['available_ids'])
        self.assertEqual(f['unknown_observed_ids'],['900'])
        self.assertEqual(f['available_count'],8)
        self.assertFalse(f['screening_complete_for_acquired_universe'])

    def test_changed_news_reopens_screenings_without_pretending_auto_understanding(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        r=context.recover(ledger,d,b,news_revision='new-story')
        self.assertTrue(r['news_changed'])
        self.assertTrue(r['screenings']['2']['needs_rescreening'])

    def test_changed_card_invalidates_affected_interpretation(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        d['cards']['2']['full_review']+=' A source correction.'
        r=context.recover(ledger,d,b,news_revision='observed-v1')
        self.assertEqual(r['changed_evidence'],['2'])
        self.assertIn('evidence_changed',r['comparisons'][0]['requires_reconsideration'])

    def test_no_silent_cutting_of_contrary_evidence(self):
        d,b,a=fixture(); idx=context.index_deck(d); ledger=context.checkpoint(a,d,b)
        p=context.pack(idx,ledger,d,b,news_revision='observed-v1',focus_ids=['2','3'])
        self.assertTrue(p['within_budget'])
        self.assertEqual(p['context']['cases'][0]['full_review'],d['cards']['2']['full_review'])
        self.assertFalse(p['context']['ready_to_execute'])
        small=context.pack(idx,ledger,d,b,news_revision='observed-v1',focus_ids=['2','3'],max_characters=100)
        self.assertFalse(small['within_budget']); self.assertIsNone(small['context'])

    def test_cold_worker_keeps_rejection_reason_and_unresolved_challenger(self):
        d,b,a=fixture(); a['screenings']['2']['status']='reject_for_this_roster'
        p=context.pack(context.index_deck(d),context.checkpoint(a,d,b),d,b,
                       news_revision='observed-v1',focus_ids=['2','3'],visible=['4'])
        receipt=p['context']['relevant_screening_receipts']['2']
        self.assertEqual(receipt['reason'],a['screenings']['2']['reason'])
        self.assertEqual(receipt['reopen_if'],a['screenings']['2']['reopen_if'])
        self.assertIn('4',p['context']['discovery']['attention_outside_focus_ids'])
        self.assertTrue(p['context']['discovery']['search_incomplete'])

    def test_context_envelope_detects_tamper_staleness_and_new_board(self):
        d,b,a=fixture(); idx=context.index_deck(d); ledger=context.checkpoint(a,d,b)
        p=context.pack(idx,ledger,d,b,news_revision='observed-v1',focus_ids=['2','3'],generated_at=101)
        self.assertTrue(context.verify_pack(p,idx,ledger,d,b,news_revision='observed-v1',now=102)['valid_context'])
        self.assertFalse(context.verify_pack(p,idx,ledger,d,b,news_revision='observed-v1',now=117)['valid_context'])
        revised={**b,'taken':['1','4'],'picks_made':2}
        self.assertFalse(context.verify_pack(p,idx,ledger,d,revised,news_revision='observed-v1',now=102)['valid_context'])
        p['context']['relevant_screening_receipts']['2']['reason']='Changed without evidence.'
        self.assertFalse(context.verify_pack(p,idx,ledger,d,b,news_revision='observed-v1',now=102)['valid_context'])

    def test_tampered_ledger_wrong_room_and_revised_prefix_stop(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        changed=copy.deepcopy(ledger); changed['authored']['horizon_policy']='Win now only.'
        with self.assertRaises(ValueError): context.recover(changed,d,b,news_revision='observed-v1')
        for fresh in [{**b,'draft_id':'real'}, {**b,'taken':['4']}]:
            with self.assertRaises(ValueError): context.recover(ledger,d,fresh,news_revision='observed-v1')

    def test_strategy_binding_and_evidence_span_validation(self):
        d,b,a=fixture(); ledger=context.checkpoint(a,d,b)
        d['policy']['rules'].append('Changed strategy')
        with self.assertRaises(ValueError): context.recover(ledger,d,b,news_revision='observed-v1')
        d,b,a=fixture(); a['open_comparisons'][0]['evidence_spans'][0]['sha256']='bad'
        with self.assertRaises(ValueError): context.checkpoint(a,d,b)

    def test_comparison_requires_counterargument_and_explicit_missingness(self):
        d,b,a=fixture(); del a['open_comparisons'][0]['best_counterargument']
        with self.assertRaises(ValueError): context.checkpoint(a,d,b)
        d,b,a=fixture(); del a['screenings']['2']['reopen_if']
        with self.assertRaises(ValueError): context.checkpoint(a,d,b)

    def test_already_taken_cannot_be_focus(self):
        d,b,a=fixture()
        with self.assertRaises(ValueError):
            context.pack(context.index_deck(d),context.checkpoint(a,d,b),d,b,
                         news_revision='observed-v1',focus_ids=['1'])

    def test_oversize_ledger_remains_visible_failure_after_many_rounds(self):
        d,b,a=fixture(); a['roster_obligations']=['x'*45000]
        p=context.pack(context.index_deck(d),context.checkpoint(a,d,b),d,b,
                       news_revision='observed-v1',focus_ids=['9'])
        self.assertFalse(p['within_budget'])
        self.assertGreater(p['characters'],45000)
        self.assertTrue(p['state_alone_exceeds_budget'])


if __name__ == '__main__':
    unittest.main()
