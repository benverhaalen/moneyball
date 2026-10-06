import unittest
from moneyball.market_ladders import sportsbook_ladders
from moneyball.market_quotes import PlayerIndex


class LaddersTests(unittest.TestCase):
    def rows(self):
        base={'book':'Test','subject':'Example Player','subject_type':'player','season':2026,
              'period':'regular_season','known_at':10,'observed_at':10,'stat':'pass_yd',
              'source_sha256':'abc','source_url':'https://example.test','action_rules':{'action':'one game'},
              'threshold':3499.5,'paired':True,'threshold_kind':'main_over_under',
              'comparator':'over_under','over_odds':-110,'under_odds':-110}
        return [base,{**base,'threshold':4000,'paired':False,'threshold_kind':'alternate_milestone',
                      'comparator':'ge','yes_odds':200}]

    def index(self):return PlayerIndex([{'player_id':'1','name':'Example Player'}])

    def test_alt_is_distinct_from_actual_paired_evidence(self):
        result,_,_=sportsbook_ladders(self.rows(),self.index(),as_of=20)
        r=result[0]
        self.assertEqual(r['paired_only']['n_thresholds'],1)
        self.assertEqual(r['with_anchored_alternates']['n_thresholds'],2)
        self.assertIsNone(r['with_anchored_alternates']['mean'])

    def test_no_same_player_anchor_leaves_alt_unusable(self):
        result,_,_=sportsbook_ladders(self.rows()[1:],self.index(),as_of=20)
        self.assertEqual(result[0]['with_anchored_alternates']['n_thresholds'],0)
        self.assertTrue(result[0]['unusable_or_failed_methods'])

    def test_different_action_rules_prevent_transfer(self):
        rows=self.rows();rows[1]['action_rules']={'action':'ten games'}
        result,_,_=sportsbook_ladders(rows,self.index(),as_of=20)
        self.assertEqual(result[0]['with_anchored_alternates']['n_thresholds'],1)

    def test_future_observation_rejected(self):
        with self.assertRaises(ValueError):sportsbook_ladders(self.rows(),self.index(),as_of=5)

    def test_pair_subject_not_treated_as_individual(self):
        rows=self.rows();rows[0]['subject_type']='pair'
        r,u,e=sportsbook_ladders(rows,self.index(),as_of=20)
        self.assertEqual(len(e),1);self.assertEqual(r[0]['paired_lines'],0)
