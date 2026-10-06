import unittest
from art_of_the_deal.sleeper import normalize,scoring_rules
from art_of_the_deal.store import DataError

class SleeperRulesTests(unittest.TestCase):
    def test_thresholds_do_not_apply_to_component_means(self):
        rules=scoring_rules({"pass_yd":.04,"bonus_pass_yd_300":3,"bonus_rec_te":1,"rec":1,"pass_cmp":0})
        self.assertEqual(rules["weights"],{"pass_yd":.04,"rec":1})
        self.assertEqual(len(rules["nonlinear"]),2)
        self.assertEqual(rules["raw"]["pass_cmp"],0)

    def test_predraft_null_roster_coownership_and_pinned_january_season(self):
        raw={"league":{"league_id":"1","season":"2026","name":"January","total_rosters":1,
                       "roster_positions":["QB","BN"],"scoring_settings":{"pass_td":4},
                       "settings":{"type":2,"best_ball":0,"reserve_slots":0,"taxi_slots":0}},
             "week":17,"rosters":[{"roster_id":1,"owner_id":"primary","co_owners":["coowner"],"players":None,"starters":None}],
             "users":[],"traded_picks":[]}
        d=normalize(raw,{},user_id="coowner")
        self.assertEqual(d["own_team_id"],"1")
        self.assertEqual(d["season"],2026)
        self.assertEqual(d["teams"]["1"]["player_ids"],[])

    def test_invalid_rule_does_not_silently_publish(self):
        with self.assertRaises(DataError): scoring_rules({"pass_yd":True})

if __name__=="__main__": unittest.main()
