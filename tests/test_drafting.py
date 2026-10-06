import copy
import unittest
from unittest.mock import patch

from moneyball import drafting


def opportunity_pool():
    """A high-mean later demand and slightly lower-mean imminent stockout."""
    players = [
        {'id':'urgent','position':'WR','mean':20,'adp':5},
        {'id':'wait','position':'QB','mean':22,'adp':31},
    ]
    for i in range(358):
        rank = i+1 if i<4 else i+2
        if rank >= 31:
            rank += 1
        players.append({'id':f'p{i}','position':('QB','RB','WR','TE')[i%4],
                        'mean':1,'adp':rank,'years_exp':2})
    return players


PREFIX = [{'pick_no':i+1,'draft_slot':i+1,'player_id':f'p{i}'} for i in range(4)]


class DraftingTests(unittest.TestCase):
    def test_lookahead_preserves_later_opportunity(self):
        players = opportunity_pool()
        greedy = drafting.draft_once(players,policy='flexible',rounds=2,
                                      demand_noise=0,observed_picks=PREFIX)
        look = drafting.draft_once(players,policy='lookahead',rounds=2,
                                    demand_noise=0,observed_picks=PREFIX)
        self.assertEqual(greedy['trace'][4]['player_id'],'wait')
        self.assertEqual(look['trace'][4]['player_id'],'urgent')
        self.assertEqual(look['trace'][19]['player_id'],'wait')
        self.assertEqual(len({p['player_id'] for p in look['trace']}),24)

    def test_intervening_lineup_rivals_change_waiting_value(self):
        look = drafting.draft_once(opportunity_pool(),policy='lookahead',rounds=2,
                                    demand_noise=0,observed_picks=PREFIX,rivals='lineup')
        # Adaptive rivals would take the larger forecast immediately. Waiting
        # for it is no longer the model's best two-turn decision.
        self.assertEqual(look['trace'][4]['player_id'],'wait')

    def test_actual_future_private_preferences_are_hidden(self):
        original = drafting._sample_demand
        def private_override(pool, noise, seed):
            sampled = original(pool,noise,seed)
            if seed == 100:  # Actual completion only, never planning draws.
                for team in range(1,13):
                    if team != 5:
                        sampled[team]['wait'] = -10000
            return sampled
        kwargs = dict(policy='lookahead',rounds=2,demand_noise=0,
                      observed_picks=PREFIX,seed=100,planning_seed=987)
        ordinary = drafting.draft_once(opportunity_pool(),**kwargs)
        with patch('moneyball.drafting._sample_demand',side_effect=private_override):
            surprising = drafting.draft_once(opportunity_pool(),**kwargs)
        self.assertEqual(ordinary['trace'][4]['player_id'],'urgent')
        self.assertEqual(surprising['trace'][4]['player_id'],'urgent')
        self.assertNotEqual(ordinary['trace'][5]['player_id'],'wait')
        self.assertEqual(surprising['trace'][5]['player_id'],'wait')

    def test_fantasy_eligible_nonstandard_nfl_position_is_draftable(self):
        players = opportunity_pool()
        players[0]['position'] = 'DB'
        players[0]['fantasy_positions'] = ['WR']
        players[1]['position'] = 'FB'
        players[1]['fantasy_positions'] = ['RB']
        result = drafting.draft_once(players,policy='lookahead',rounds=2,
                                      demand_noise=0,observed_picks=PREFIX)
        own = [p['player_id'] for p in result['trace'] if p['draft_slot']==5]
        self.assertEqual(own,['urgent','wait'])

    def test_forced_next_turn_and_rollout_do_not_mutate_inputs(self):
        players = opportunity_pool()
        before = copy.deepcopy(players)
        first = drafting.draft_once(players,policy='lookahead',rounds=3,
                                     demand_noise=0,observed_picks=PREFIX)
        prefix = first['trace'][:5]
        result = drafting.draft_once(players,policy='lookahead',rounds=3,
                                      demand_noise=0,observed_picks=prefix,forced_first='p350')
        self.assertEqual(result['candidate_decision_pick'],20)
        self.assertEqual(result['trace'][19]['player_id'],'p350')
        self.assertEqual(result['decision_player_id'],'p350')
        self.assertTrue(result['forced_first_available'])
        self.assertEqual(players,before)
        self.assertEqual(result['trace'][:5],prefix)

    def test_impossible_prefix_and_zero_planning_draws_rejected(self):
        result = drafting.draft_once(opportunity_pool(),rounds=2)
        with self.assertRaises(ValueError):
            drafting.draft_once(opportunity_pool(),rounds=1,observed_picks=result['trace'])
        with self.assertRaises(ValueError):
            drafting.draft_once(opportunity_pool(),policy='lookahead',planning_draws=0)


if __name__ == '__main__':
    unittest.main()
