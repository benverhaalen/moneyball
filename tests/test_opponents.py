import copy
import math
import unittest

from moneyball.opponents import ChoiceEngine, EXPERTS, fit, make_selector, predict, simulate_until
from moneyball.simulation import select_lineup


def pool(n=60):
    return [{'id':str(i), 'position':('QB','RB','WR','TE')[i%4],
             'fantasy_positions':[('QB','RB','WR','TE')[i%4]],
             'adp':i+1, 'mean':22-i/4, 'age':22+i%12} for i in range(n)]


def prefix(n):
    return [{'pick_no':i+1, 'player_id':str(i)} for i in range(n)]


class OpponentsTests(unittest.TestCase):
    def test_empty_model_has_zero_real_information(self):
        model=fit(pool())
        self.assertEqual(model['sample_size']['opponent_decisions'],0)
        self.assertEqual(model['score_summary']['mean_negative_log_score'],None)
        for expert, prior in zip(EXPERTS,model['settings']['prior']):
            self.assertAlmostEqual(model['managers']['1']['predictive_weights'][expert],prior)

    def test_all_available_normalized_and_positive(self):
        players=pool(); model=fit(players,prefix(8))
        for mode in ('posterior','adp','lineup'):
            probs=predict(players,model,9,mode=mode)
            self.assertEqual(set(probs),set(model['available']))
            self.assertAlmostEqual(sum(probs.values()),1)
            self.assertGreater(min(probs.values()),0)

    def test_prefix_rejects_gap_duplicates_and_trades(self):
        for picks in ([{'pick_no':2,'player_id':'1'}],prefix(2)+[{'pick_no':2,'player_id':'2'}],
                      [{'pick_no':1,'player_id':'1','draft_slot':2}],
                      [{'pick_no':1,'player_id':'0'},{'pick_no':2,'player_id':'0'}]):
            with self.assertRaises(ValueError): fit(pool(),picks)

    def test_prequential_scores_never_change_with_later_observations(self):
        small=fit(pool(),prefix(3)); larger=fit(pool(),prefix(20))
        self.assertEqual(small['prequential'],larger['prequential'][:3])
        first=small['prequential'][0]
        probs=predict(pool(),fit(pool()),1)
        self.assertAlmostEqual(first['negative_log_score'],-math.log(probs['0']))
        self.assertEqual(first['manager_n_before'],0)

    def test_own_choices_change_availability_without_training(self):
        model=fit(pool(),prefix(5),our_slot=5)
        self.assertEqual(model['sample_size']['actual_picks_total'],5)
        self.assertEqual(model['sample_size']['opponent_decisions'],4)
        self.assertEqual(model['managers']['5']['observed_picks'],0)
        self.assertEqual(model['managers']['5']['log_likelihood'],[0]*5)
        self.assertEqual(model['rosters']['5'],['4'])
        self.assertNotIn('4',model['available'])

    def test_one_pick_cannot_create_personal_certainty(self):
        model=fit(pool(),[{'pick_no':1,'player_id':'40'}])
        manager=model['managers']['1']
        self.assertAlmostEqual(manager['local_weight'],1/9)
        self.assertLess(max(manager['predictive_weights'].values()),0.45)
        self.assertEqual(manager['observed_picks'],1)

    def test_production_is_not_adp(self):
        players=pool(); changed=copy.deepcopy(players)
        for p in changed:p['adp']=100-p['adp']
        a=ChoiceEngine(players);b=ChoiceEngine(changed)
        self.assertEqual(a.values,b.values)
        self.assertEqual(a._thresholds(('0','2')),b._thresholds(('0','2')))

    def test_fast_entry_gains_match_exact_legal_lineup(self):
        players=pool(); engine=ChoiceEngine(players)
        for roster in ([],[str(i) for i in range(7)],[str(i) for i in range(20)]):
            thresholds=engine._thresholds(tuple(roster))
            before=sum(engine.values[p] for p in select_lineup(roster,engine.pool,engine.values))
            for pid in engine.pool:
                if pid in roster:continue
                exact=sum(engine.values[p] for p in select_lineup(roster+[pid],engine.pool,engine.values))-before
                fast=max(0,engine.values[pid]-thresholds[engine.eligibility[pid]])
                self.assertAlmostEqual(fast,exact,places=8)

    def test_dual_eligibility_entry_thresholds_match_exact_assignment(self):
        players=pool(24)
        players[3]['fantasy_positions']=['WR','TE']
        players[7]['fantasy_positions']=['RB','WR']
        engine=ChoiceEngine(players)
        for roster in (['1','3','4','6','8','9','10','14','18','22'],
                       ['0','2','3','4','5','6','7','10','12','18','22']):
            thresholds=engine._thresholds(tuple(roster))
            before=sum(engine.values[p] for p in select_lineup(roster,engine.pool,engine.values))
            for pid in engine.pool:
                if pid in roster:continue
                exact=sum(engine.values[p] for p in select_lineup(roster+[pid],engine.pool,engine.values))-before
                fast=max(0,engine.values[pid]-thresholds[engine.eligibility[pid]])
                self.assertAlmostEqual(fast,exact,places=8)

    def test_forward_only_uses_prefix_and_does_not_mutate(self):
        players=pool(); model=fit(players,prefix(4)); saved=copy.deepcopy(model)
        result=simulate_until(players,prefix(4),['4','5','6'],draws=8,seed=11,model=model,first_pick='4')
        self.assertEqual(result['target_pick'],20)
        self.assertEqual(result['intervening_picks'],14)
        self.assertEqual(result['candidates'][0]['survival_probability'],0)
        self.assertEqual(model,saved)
        trace=result['example_trace']
        self.assertEqual(len(set(p['player_id'] for p in trace)),14)
        self.assertFalse(set(str(i) for i in range(5)).intersection(p['player_id'] for p in trace))
        self.assertEqual(result,simulate_until(players,prefix(4),['4','5','6'],draws=8,seed=11,model=model,first_pick='4'))

    def test_selector_samples_persistent_expert_and_does_not_train(self):
        players=pool();model=fit(players,prefix(4));saved=copy.deepcopy(model)
        a=make_selector(players,model,99);b=make_selector(players,model,99)
        self.assertEqual(a.manager_types,b.manager_types)
        self.assertEqual(a(6,6,[],model['available']),b(6,6,[],model['available']))
        self.assertEqual(model,saved)

    def test_next_pick_receipt_and_immutable_archive(self):
        from moneyball.opponents import next_forecast,archive_forecast
        import tempfile
        players=pool();model=fit(players,prefix(3))
        receipt=next_forecast(players,model,created_at=100,feature_available_at=90)
        self.assertEqual(receipt['target_pick_no'],4)
        self.assertEqual(receipt['target_draft_slot'],4)
        self.assertEqual(len(receipt['probabilities']),57)
        with tempfile.TemporaryDirectory() as directory:
            a=archive_forecast(directory,receipt)
            b=archive_forecast(directory,{**receipt,'created_at':120})
            self.assertEqual(a,b)
            self.assertEqual(b['forecast']['created_at'],100)
        self.assertIsNone(next_forecast(players,fit(players,prefix(4)),created_at=100))
        with self.assertRaises(ValueError):
            next_forecast(players,model,created_at=100,feature_available_at=101)

    def test_stale_feature_snapshot_rejected(self):
        players=pool(); model=fit(players); players[0]['adp']=900
        with self.assertRaises(ValueError):predict(players,model,1)


if __name__=='__main__':unittest.main()
