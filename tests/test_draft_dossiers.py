import copy
import json
from pathlib import Path
import tempfile
import unittest

from moneyball.draft_dossiers import make_cards, expected_opportunity_index, write_index, lookup, compact, clay_index, render_card
from moneyball.store import digest
from moneyball.warehouse import LeakageError

SCORING={'pass_yd':.04,'pass_td':4,'pass_int':-1,'rush_yd':.1,'rush_td':6,'rec':1,'rec_yd':.1,'rec_td':6,'fum_lost':-2}
P={'source_id':'provider','available_at':10,'observed_at':10,'batch_id':1}


def inputs():
    b={'season':2026,'cutoff':10,'created_at':11,'model_id':'fixture','scoring_hash':digest(SCORING),
       'players':[{'id':'a','name':'Test Player','position':'WR','fantasy_positions':['WR'],'team':'GB','gsis_id':'g',
          'age':30,'years_exp':8,'adp':20,'demand_source':'adp_dynasty_2qb','availability':1,'availability_source':'assumed',
          'missing_nonbye_forecast_weeks':[7]}]}
    f=[{'player_id':'a','source_id':'provider','season':2026,'week':None,'stats':{'rec':50},'mean':50,
        'scoring_hash':digest(SCORING),'_provenance':P}]
    m=[{'player_id':'a','injury_status':None,'status':'Active','_provenance':P}]
    h=[{'player_id':'g','season':2025,'season_type':'REG','week':1,'targets':10,'receptions':5,'_provenance':P}]
    return b,f,m,h


class DossierTests(unittest.TestCase):
    def cards(self, **kw):
        return make_cards(*inputs(), as_of=20, scoring=SCORING, **kw)

    def test_unknown_future_and_health_not_guessed(self):
        c=self.cards()[0]
        self.assertIsNone(c['availability']['future_injury_probability'])
        self.assertIsNone(c['future_paths']['2028']['production_distribution'])
        self.assertIsNone(c['live_decision']['annual_title_deltas'])
        self.assertEqual(c['history']['observed_rows'],1)
        self.assertIsNone(c['history']['totals']['carries'])
        self.assertIn('not dynasty utility',c['acquisition_reference']['role'])
        self.assertEqual(c['identity_provenance']['source'],'projection_bundle')
        self.assertEqual(c['current_metadata_provenance']['source_id'],'provider')

    def test_asof_leak_rejected(self):
        a=list(inputs());a[1][0]['_provenance']={**P,'available_at':21}
        with self.assertRaises(LeakageError):
            make_cards(*a,as_of=20,scoring=SCORING)

    def test_future_artifact_rejected(self):
        a=list(inputs());a[0]['created_at']=21
        with self.assertRaises(ValueError):make_cards(*a,as_of=20,scoring=SCORING)

    def test_wrong_scoring_and_forecast_season(self):
        a=list(inputs());a[1][0]['scoring_hash']='wrong'
        with self.assertRaises(ValueError):make_cards(*a,as_of=20,scoring=SCORING)
        a=list(inputs());a[1][0]['season']=2025
        with self.assertRaises(ValueError):make_cards(*a,as_of=20,scoring=SCORING)

    def test_duplicate_ids_and_forecasts_and_history_fail(self):
        for component in (0,1,3):
            a=list(inputs())
            if component==0:a[0]['players']*=2
            else:a[component]*=2
            with self.assertRaises(ValueError):make_cards(*a,as_of=20,scoring=SCORING)

    def test_evidence_requires_date_counterevidence_and_overlap(self):
        e={'a':[{'target':'role2028','source_url':'https://example.test','known_at':10,'claim':'claim','counterevidence':'alternative','baseline_overlap':'unknown'}]}
        self.assertEqual(len(self.cards(evidence=e)[0]['case_evidence']),1)
        for key in ('known_at','counterevidence','baseline_overlap'):
            bad=copy.deepcopy(e);bad['a'][0].pop(key)
            with self.assertRaises(ValueError):self.cards(evidence=bad)
        with self.assertRaises(ValueError):self.cards(evidence={'unknown':[]})

    def test_storage_lookup_and_compact_preserve_inputs(self):
        cards=self.cards();old=copy.deepcopy(cards)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'cards.sqlite3';write_index(p,cards,{'test':True})
            self.assertEqual(lookup(p,'test player'),cards[0])
            self.assertEqual(lookup(p,'a'),cards[0])
            with self.assertRaises(ValueError):lookup(p,'not there')
        compact(cards[0]);self.assertEqual(cards,old)
        text=render_card(cards[0])
        self.assertIn('not yet estimated',text)
        self.assertIn('not a completed league recommendation',text)

    def test_clay_semantics_exact_identity_and_no_rank_value(self):
        b,*_=inputs()
        raw={'season':2026,'player_name':'Test Player','position':'WR','source_team_code':'GB','source_page':1,
             'components':{'games':12,'rec':40,'rec_yd':500,'rec_td':2},'source_ppr_points':9999}
        payload={'source_url':'https://example.test','known_at':12,'source_conditioning':{'injury':'separate'},
                 'rows':[raw,{**raw,'player_name':'Other Name'}]}
        idx,miss=clay_index(payload,b['players'],as_of=20,scoring=SCORING,season=2026)
        self.assertEqual(len(miss),1)
        self.assertEqual(idx['a']['stats']['games'],12)
        self.assertEqual(idx['a']['league_observed_component_subtotal'],102)
        self.assertIn('fum_lost',idx['a']['missing_scoring_keys'])
        c=self.cards(clay=payload)[0]
        self.assertEqual(c['current_production']['independent_semantics_crosscheck']['source_conditioning'],{'injury':'separate'})
        payload['rows'][0]['source_team_code']='SF'
        self.assertFalse(clay_index(payload,b['players'],as_of=20,scoring=SCORING,season=2026)[0])
        payload['known_at']=21
        with self.assertRaises(ValueError):self.cards(clay=payload)

    def test_opportunity_rescores_interceptions_and_excludes_postseason_unidentified(self):
        raw={'season':'2025','player_id':'g','game_id':'2025_01_A_B','posteam':'GB','week':'1',
             'pass_yards_gained_exp':'100','pass_yards_gained':'200','pass_interception_exp':'1','pass_interception':'2',
             'pass_fantasy_points_exp':'-999','total_fantasy_points_exp':'-999'}
        wrapper={'source_id':'x','source_url':'https://example.test','observed_at':12,'available_at':12,'season':2025,
                 'interpretation':'retrospective','rows':[raw,{**raw,'week':'19','game_id':'other'},{**raw,'player_id':'NA'}]}
        idx=expected_opportunity_index(wrapper,as_of=20,scoring=SCORING,draft_season=2026)
        self.assertEqual(set(idx),{'g'})
        self.assertEqual(idx['g']['league_expected_component_subtotal'],3)
        self.assertEqual(idx['g']['actual_same_component_subtotal'],6)
        self.assertIn('fum_lost',idx['g']['unsupported_scoring_keys'])
        self.assertEqual(idx['g']['observed_rows'],1)
        self.assertIn('rec',idx['g']['missing_component_rows'])
        self.assertEqual(self.cards(opportunity=wrapper)[0]['expected_opportunity']['status'],'retrospective_diagnostic_not_forecast')
        wrapper['available_at']=21
        with self.assertRaises(ValueError):self.cards(opportunity=wrapper)

    def test_absent_opportunity_is_not_zero(self):
        wrapper={'source_id':'x','source_url':'https://example.test','observed_at':12,'available_at':12,'season':2025,
                 'interpretation':'retrospective','rows':[{'season':'2025','week':'1','player_id':'g','game_id':'game','posteam':'GB'}]}
        c=self.cards(opportunity=wrapper)[0]
        self.assertEqual(c['expected_opportunity']['status'],'no_matched_opportunity_components')
        self.assertIsNone(c['expected_opportunity']['league_expected_component_subtotal'])
        self.assertIsNone(c['expected_opportunity']['actual_minus_expected_same_components'])
        self.assertNotIn('subtotal under configured league weights: 0.00',render_card(c))


if __name__=='__main__':unittest.main()
