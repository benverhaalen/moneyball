import itertools
import unittest
from moneyball.simulation import select_lineup, simulate, apply_move, RATRACE_STARTERS, ELIGIBILITY
from moneyball.drafting import snake_owner, draft_once
from moneyball.simulation import _weekly_mean
from moneyball.simulation import _draw_player
import random
from moneyball.identity import canonical_team


class SimulationTests(unittest.TestCase):
    def test_lineup_eligibility_and_dual_position(self):
        players = {str(i):{'position':p} for i,p in enumerate(['QB','QB','RB','RB','RB','WR','WR','WR','WR','TE','TE'])}
        values = {p:i+1 for i,p in enumerate(players)}
        chosen = select_lineup(players,players,values)
        self.assertEqual(len(chosen),10)
        self.assertEqual(len(set(chosen)),10)
        # Force exact DP by declaring an otherwise equivalent single position
        # with additional eligibility; it must never reuse an athlete.
        players['0']['fantasy_positions'] = ['QB','WR']
        chosen = select_lineup(players,players,values)
        self.assertEqual(len(chosen),len(set(chosen)))
        self.assertEqual(len(chosen),10)

    def test_flex_does_not_take_qb_and_missing_slot_is_not_filled_illegally(self):
        players = {str(i):{'position':'QB'} for i in range(8)}
        self.assertEqual(len(select_lineup(players,players,{p:100 for p in players})),2)

    def test_known_dominance_and_paired_identity(self):
        players, state = [], {}
        pos = ['QB','QB','RB','RB','WR','WR','WR','TE','RB','WR']
        for t in range(1,13):
            state[str(t)] = []
            for i,p in enumerate(pos):
                pid = f'{t}:{i}'
                state[str(t)].append(pid)
                players.append({'id':pid,'position':p,'mean':100 if t==1 else 1,'sd':0})
        result = simulate({'baseline':state,'same':state},{'players':players},draws=4,years=3,allow_assumptions=True)
        self.assertEqual(result['scenarios']['baseline']['1']['expected_titles']['estimate'],3)
        for team,r in result['scenarios']['same'].items():
            self.assertEqual(r['paired_delta_expected_titles']['estimate'],0)
        self.assertEqual(sum(x['current_championship']['estimate'] for x in result['scenarios']['baseline'].values()),1)

    def test_assumptions_are_explicit(self):
        with self.assertRaises(ValueError):
            simulate({},[],draws=3)

    def test_season_timing_conservation_and_paired_deltas(self):
        players, state = [], {}
        positions = ['QB','QB','RB','RB','WR','WR','WR','TE','RB','WR']
        for team in range(1,13):
            state[str(team)] = []
            for i,position in enumerate(positions):
                pid = f'{team}:{i}'
                state[str(team)].append(pid)
                players.append({'id':pid,'position':position,'mean':100 if team<=3 else 1,
                    'sd':0,'year_mean_factors':[int(team==y+1) for y in range(3)] if team<=3 else [1,1,1]})
        swapped = {t:list(roster) for t,roster in state.items()}
        swapped['1'],swapped['2'] = swapped['2'],swapped['1']
        result = simulate({'baseline':state,'same':state,'swap':swapped}, {'players':players},
            draws=8,years=3,allow_assumptions=True)
        for rows in result['scenarios'].values():
            for year in range(3):
                self.assertAlmostEqual(sum(r['championship_by_year'][year]['estimate'] for r in rows.values()),1)
                self.assertAlmostEqual(sum(r['paired_delta_championship_by_year'][year]['estimate'] for r in rows.values()),0)
            for r in rows.values():
                self.assertEqual(r['championship_by_year'][0],r['current_championship'])
                self.assertAlmostEqual(sum(y['estimate'] for y in r['championship_by_year']),r['expected_titles']['estimate'])
                self.assertAlmostEqual(sum(y['estimate'] for y in r['paired_delta_championship_by_year']),r['paired_delta_expected_titles']['estimate'])
        for r in result['scenarios']['same'].values():
            self.assertTrue(all(y['estimate']==0 and y['monte_carlo_se']==0 for y in r['paired_delta_championship_by_year']))
        first = result['scenarios']['baseline']['1']
        self.assertEqual([y['estimate'] for y in first['championship_by_year']],[1,0,0])
        self.assertEqual([y['estimate'] for y in result['scenarios']['swap']['1']['paired_delta_championship_by_year']],[-1,1,0])
        self.assertIn('frozen holdings',result['assumptions']['future_policy'])

    def test_observed_team_alias_does_not_create_seventeen_byes(self):
        self.assertEqual(canonical_team('LAR'),canonical_team('LA'))
        self.assertIsNone(canonical_team('4984'))

    def test_missing_professional_week_never_silently_becomes_a_start(self):
        player = {'id':'partial_qb','mean':15,'observed_week_mean':15,'weekly_means':{'1':15,'2':15}}
        with self.assertRaises(ValueError):
            _weekly_mean(player,15,0)
        self.assertEqual(_weekly_mean(player,15,0,'zero'),0)
        self.assertEqual(_weekly_mean(player,15,0,'carry'),15)

    def test_zero_future_production_scales_noise_too(self):
        player = {'sd':20,'standardized_residuals':[1.0],'team_loading':.2}
        self.assertEqual(_draw_player(player,15,random.Random(1),2,0,0),0)
        self.assertEqual(_draw_player(player,15,random.Random(1),2,0,0,{'values':[20],'cdf':[1.0]}),0)

    def test_move_ownership_and_no_mutation(self):
        state = {'1':['a'],'2':['b']}
        result = apply_move(state,{'transfers':[{'from_team':'1','to_team':'2','players':['a']},{'from_team':'2','to_team':'1','players':['b']}]})
        self.assertEqual(result['state'],{'1':['b'],'2':['a']})
        self.assertEqual(state,{'1':['a'],'2':['b']})
        with self.assertRaises(ValueError):
            apply_move(state,{'team':'1','add':['b']})
        with self.assertRaises(ValueError):
            apply_move(state,{'picks':['2027first'],'team':'1'})

    def test_snake_and_capacity(self):
        self.assertEqual([p for p in range(1,73) if snake_owner(p)==5],[5,20,29,44,53,68])
        players = [{'id':str(i),'position':['QB','RB','WR','TE'][i%4], 'mean':30-i/25,'adp':i+1,'years_exp':0 if i%3==0 else 2} for i in range(360)]
        result = draft_once(players,policy='two_qb_early',seed=2)
        own = [x['player_id'] for x in result['trace'] if x['draft_slot']==5]
        lookup = {p['id']:p for p in players}
        self.assertEqual([lookup[p]['position'] for p in own[:2]],['QB','QB'])
        self.assertEqual(len({x['player_id'] for x in result['trace']}),336)
        self.assertTrue(all(len(r)<=25 for r in result['rosters'].values()))
        self.assertTrue(all(lookup[p]['years_exp']==0 for r in result['taxi'].values() for p in r))
        prefix = result['trace'][:5]
        forced = next(p['id'] for p in players if p['id'] not in {x['player_id'] for x in prefix} and p['adp']>300)
        d = draft_once(players,observed_picks=prefix,forced_first=forced,seed=2)
        self.assertTrue(d['forced_first_available'])
        self.assertEqual(d['trace'][19]['player_id'],forced)
        with self.assertRaises(ValueError):
            draft_once(players,observed_picks=prefix+[prefix[0]])


if __name__ == '__main__':
    unittest.main()
