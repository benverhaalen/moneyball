import copy
import unittest

from moneyball.contract_context import normalize_contracts


def inputs():
    rows=[{'player':'Test Player','position':'QB','team':'Bills','is_active':True,
        'year_signed':2025,'years':6,'value':330,'apy':55,'guaranteed':147,'otc_id':1,'gsis_id':'g',
        'player_page':'https://example.test/player/1','season_history':[
            {'year':'2027','team':'Bills','base_salary':14,'guaranteed_salary':52.5,'cap_number':56.148},
            {'year':'2033','team':'Bills','base_salary':85,'cash_paid':0,'cap_number':0},
            {'year':'Total','base_salary':343.08}],
        'contract_history':[{'year_signed':2025,'contract_type':'Extension','status':'Active','yrs':6}]}]
    universe={'information_cutoff':10,'built_at':11,'players':[{'player_id':'p','name':'Test Player',
        'position':'QB','team':'BUF','gsis_id':' g ','external_ids':{'otc_id':'1'},
        'identity_provenance':{'source_id':'nflverse'},'identity_match':['exact_id']}]}
    cohort={'created_at':12,'players':[{'player_id':'p'}]}
    receipt={'observed_at':15,'available_at':15,'sha256':'hash'}
    return rows,universe,cohort,receipt


class ContractContextTests(unittest.TestCase):
    def test_does_not_mistake_void_row_or_signing_year_for_expiry(self):
        result=normalize_contracts(*inputs(),cutoff=20)['players'][0]
        self.assertIsNone(result['contract_expiration_year'])
        self.assertIsNone(result['free_agency_year'])
        self.assertIsNone(result['remaining_guaranteed_cash'])
        self.assertIsNone(result['release_trade_dead_cap_scenarios'])
        self.assertEqual(result['annual_rows_2026_2028']['2027'][0]['guaranteed_salary'],52.5)
        self.assertEqual(result['money_unit'],'USD millions; ratios are proportions')

    def test_repeated_nested_history_is_not_double_counted(self):
        a=list(inputs());old=copy.deepcopy(a[0][0]);old['is_active']=False;old['year_signed']=2021
        a[0].append(old)
        result=normalize_contracts(*a,cutoff=20)['players'][0]
        self.assertEqual(len(result['annual_source_rows']),3)
        self.assertEqual(len(result['all_contract_summary_records']),2)
        self.assertEqual(result['reported_active_contract_count'],1)

    def test_multiple_active_records_preserved(self):
        a=list(inputs());other=copy.deepcopy(a[0][0]);other['year_signed']=2026;a[0].append(other)
        result=normalize_contracts(*a,cutoff=20)
        self.assertEqual(result['coverage']['multiple_active_contract_records'],1)
        self.assertEqual(result['players'][0]['reported_active_contract_count'],2)

    def test_exact_id_whitespace_normalized(self):
        a=list(inputs());a[1]['players'][0]['external_ids']={}
        r=normalize_contracts(*a,cutoff=20)
        self.assertEqual(r['crosswalk'][0]['gsis_id'],'g')
        self.assertEqual(r['crosswalk'][0]['otc_id'],'1')

    def test_names_only_produce_unmatched_candidates(self):
        a=list(inputs());a[1]['players'][0].update(gsis_id=None,external_ids={})
        r=normalize_contracts(*a,cutoff=20)
        self.assertEqual(r['coverage']['unmatched'],1)
        self.assertEqual(r['players'],[])
        self.assertEqual(r['unmatched'][0]['candidate_identities_not_joined'][0]['otc_id'],'1')

    def test_conflicting_source_identity_fails(self):
        a=list(inputs());a[0][0]['gsis_id']='other'
        with self.assertRaises(ValueError):normalize_contracts(*a,cutoff=20)

    def test_gsis_collision_requires_unique_observed_position(self):
        a=list(inputs());a[1]['players'][0]['external_ids']={}
        other=copy.deepcopy(a[0][0]);other.update(otc_id=2,position='WR',is_active=False)
        a[0].append(other)
        r=normalize_contracts(*a,cutoff=20)['crosswalk'][0]
        self.assertEqual(r['otc_id'],'1')
        self.assertEqual(r['discarded_otc_identity_conflicts'][0]['otc_id'],'2')
        a[0][1]['position']='QB'
        with self.assertRaises(ValueError):normalize_contracts(*a,cutoff=20)

    def test_asof_and_nonfinite_fail(self):
        with self.assertRaises(ValueError):normalize_contracts(*inputs(),cutoff=14)
        a=list(inputs());a[0][0]['value']=float('nan')
        with self.assertRaises(ValueError):normalize_contracts(*a,cutoff=20)

    def test_missing_contract_status_is_not_false(self):
        a=list(inputs());a[0][0]['is_active']=None
        with self.assertRaises(ValueError):normalize_contracts(*a,cutoff=20)


if __name__=='__main__':unittest.main()
