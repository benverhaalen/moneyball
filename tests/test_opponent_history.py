import unittest
from moneyball.opponent_history import summarize_history

class OpponentHistoryTests(unittest.TestCase):
    def fixture(self):
        l={'league_id':'l','settings':{'type':2},'scoring_settings':{'rec':1},'roster_positions':['QB','SUPER_FLEX']}
        c={'league':l,'my_roster_id':1,'users':[],'rosters':[{'roster_id':1,'owner_id':'me'},{'roster_id':2,'owner_id':'them'}]}
        d={'info':{'draft_id':'d','league_id':'l','status':'complete','settings':{'teams':12,'rounds':28},'draft_order':{'them':1,'me':2}},
           'picks':[{'pick_no':1,'draft_slot':1,'player_id':'a','picked_by':'them','round':1,'metadata':{'position':'QB'}},
                    {'pick_no':2,'draft_slot':2,'player_id':'b','picked_by':'me','round':1,'metadata':{'position':'WR'}}], 'traded_picks':[]}
        return c,l,d
    def test_count_beneficiary_and_do_not_promote_to_training(self):
        c,l,d=self.fixture();r=summarize_history(c,[l],[d]);self.assertEqual(r['attributed_nonkeeper_selections'],1)
        self.assertEqual(r['coarse_format_matched_selections'],1);self.assertEqual(r['prospectively_valid_behavior_training_rows'],0)
        self.assertFalse(r['selections'][0]['manual_human_action_verified'])
    def test_keeper_not_a_new_choice(self):
        c,l,d=self.fixture();d['picks'][0]['is_keeper']=True
        self.assertEqual(summarize_history(c,[l],[d])['attributed_nonkeeper_selections'],0)
    def test_conflicting_beneficiary_quarantined(self):
        c,l,d=self.fixture();d['picks'][0]['picked_by']='me'
        r=summarize_history(c,[l],[d]);self.assertEqual(r['attributed_nonkeeper_selections'],0);self.assertEqual(len(r['excluded']),1)
    def test_gap_rejected(self):
        c,l,d=self.fixture();d['picks'][1]['pick_no']=3
        with self.assertRaises(ValueError):summarize_history(c,[l],[d])
    def test_redraft_code_is_not_dynasty_training(self):
        c,l,d=self.fixture();l={**l,'settings':{'type':0}}
        r=summarize_history(c,[l],[d]);self.assertEqual(r['coarse_format_matched_selections'],0)
    def test_unique_slot_fallback(self):
        c,l,d=self.fixture();d['picks'][0]['picked_by']=''
        r=summarize_history(c,[l],[d]);self.assertEqual(r['attributed_nonkeeper_selections'],1)
        self.assertEqual(r['selections'][0]['attribution_method'],'draft_order_slot_without_trades')

if __name__=='__main__':unittest.main()
