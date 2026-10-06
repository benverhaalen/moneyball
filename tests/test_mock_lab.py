import copy
from datetime import datetime, timedelta, timezone
import unittest

from moneyball.mock_lab import (MockLabBlocked, REAL_DRAFT_ID, audit_run,
                               require_ready, snake_slot, verify_pick,
                               verify_receipt, verify_shortlist)

MOCK = '9000000000000000002'
NOW = '2026-09-08T21:04:20Z'


def iso(seconds):
    return (datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def cell(pick, pid=None, clock=None):
    label = f'{(pick-1)//12+1}.{(pick-1)%12+1}'
    return {'id': f'draft-cell-{pick}', 'cls': 'cell drafted' if pid else 'cell false current-pick',
            'text': label + (f'\nP. {pid}\nWR - NE' if pid else '\n'+(clock or '02:00')),
            'images': ['https://sleepercdn.com/images/v2/ui/icon_arrow_right_dark_2.png'] +
                      ([f'https://sleepercdn.com/content/nfl/players/thumb/{pid}.jpg'] if pid else [])}


def ui(pick=5, at=NOW, clock='01:20'):
    cells = [cell(p, str(1000+p)) for p in range(1, pick)] + [cell(pick, clock=clock)]
    columns = []
    for p in range(1, 337):
        if snake_slot(p) == 5:
            columns.append(next((c['text'] for c in cells if c['id'] == f'draft-cell-{p}'),
                                f'{(p-1)//12+1}.{(p-1)%12+1}'))
    return {'url': f'https://sleeper.com/draft/nfl/{MOCK}?ftue=commish',
            'observed_at': at, 'ownAutoPick': False, 'header': '12 Teams\n28 Rounds',
            'own': 'fixture_manager\nfixture_manager\n'+'\n'.join(columns), 'cells': cells,
            'candidates': [{'name': 'Drake Maye', 'position': 'QB\nNE\nQUES',
                            'enabled': True, 'draft_button_present': True}]}


def args(pick=5):
    return {'allowed_mock_id': MOCK, 'expected_pick': pick,
            'expected_board': [{'pick': p, 'player_id': str(1000+p)} for p in range(1,pick)],
            'expected_own_roster': [str(1000+p) for p in range(1,pick) if snake_slot(p) == 5],
            'candidate': {'player_id': '11564', 'name': 'Drake Maye'}, 'now': NOW,
            'player_catalog': [{'player_id': '11564', 'name': 'Drake Maye', 'position': 'QB', 'team': 'NE'}]}


class MockGuardTests(unittest.TestCase):
    def test_recorded_run_b_first_own_turn_shape_requires_real_button_observation(self):
        # Immutable relevant fields from B's actual21:03:43.758Z observation.
        # Other candidate rows are intentionally omitted; they do not affect this
        # selection. The old collector omitted the button-presence measurement.
        x=ui(at='2026-09-08T21:03:43.758Z',clock='01:57')
        observed=[('4984','J. Allen','QB - BUF'),('9221','J. Gibbs','RB - DET'),
                  ('7564','J. Chase','WR - CIN'),('9509','B. Robinson','RB - ATL')]
        x['cells']=[]
        for pick,(pid,name,position) in enumerate(observed,1):
            c=cell(pick,pid);c['text']=f'1.{pick}\n{name}\n{position}';x['cells'].append(c)
        x['cells'].append(cell(5,clock='01:57'))
        x['candidates']=[{'name':'Drake Maye','position':'QB\nNE','adp':'5.3','enabled':True}]
        a=args();a['now']=x['observed_at']
        a['expected_board']=[{'pick':i,'player_id':r[0]} for i,r in enumerate(observed,1)]
        self.assertEqual(verify_pick(x, **a)['blockers'],['candidate_button_not_observed_enabled'])
        # Test-only augmentation represents the measurement required in a new
        # collector; the historical observation is not retrospectively approved.
        x['candidates'][0]['draft_button_present']=True
        self.assertTrue(verify_pick(x, **a)['ready'])

    def test_actual_column_order_is_irrelevant_and_composite_label_is_honest(self):
        x = ui(20); x['cells'].reverse()
        r = verify_pick(x, **args(20))
        self.assertTrue(r['ready'], r)
        self.assertEqual(r['identity_evidence'], 'unique_name_position_team_join')
        self.assertEqual(r['actual_own_roster'], ['1005'])
        self.assertIs(require_ready(r), r)

    def test_global_current_clock_need_not_be_duplicated_in_own_column(self):
        x=ui(20); x['own']=x['own'].replace('2.8\n01:20','2.8')
        self.assertTrue(verify_pick(x, **args(20))['ready'])
        x['own']=x['own'].replace('2.8\n','2.8\nUnexpected Player\n')
        self.assertFalse(verify_pick(x, **args(20))['ready'])

    def test_paused_is_hard_stop_even_fresh_right_room_turn_and_button(self):
        x = ui(clock='Paused'); r = verify_pick(x, **args())
        self.assertFalse(r['ready']); self.assertIn('paused', r['blockers'])
        with self.assertRaises(MockLabBlocked): require_ready(r)

    def test_exact_url_and_real_room_rejection(self):
        for url in (f'https://sleeper.com/draft/nfl/{MOCK}9',
                    f'https://sleeper.com/draft/nfl/{REAL_DRAFT_ID}?next=/{MOCK}',
                    f'https://sleeper.com.evil.test/draft/nfl/{MOCK}',
                    f'https://sleeper.com/draft/nfl/{MOCK}/extra',
                    f'https://sleeper.com/other/{MOCK}',
                    f'https://user@sleeper.com/draft/nfl/{MOCK}'):
            x = ui(); x['url'] = url
            self.assertIn('wrong_or_forbidden_room', verify_pick(x, **args())['blockers'])
        a = args(); a['allowed_mock_id'] = REAL_DRAFT_ID
        self.assertFalse(verify_pick(ui(), **a)['ready'])

    def test_time_rejects_stale_future_nonfinite_naive_missing(self):
        for timestamp in ('2026-09-08T21:04:04Z', '2026-09-08T21:04:21Z',
                          float('nan'), float('inf'), None, '2026-09-08T21:04:20'):
            x = ui(at=timestamp)
            self.assertFalse(verify_pick(x, **args())['ready'], timestamp)
        a = args(); a['now'] = float('nan')
        self.assertFalse(verify_pick(ui(), **a)['ready'])
        self.assertTrue(verify_pick(ui(at='2026-09-08T21:04:05Z'), **args())['ready'])
        x=ui();x['unrelated_numeric_field']=float('inf')
        self.assertIn('non_json_or_nonfinite_observation', verify_pick(x, **args())['blockers'])

    def test_clock_format_positive_bounded_and_not_expired(self):
        for clock in ('00:00','1:20','02:60','03:00','missing','01:20\n01:19'):
            self.assertFalse(verify_pick(ui(clock=clock), **args())['ready'], clock)
        self.assertFalse(verify_pick(ui(at='2026-09-08T21:04:10Z', clock='00:10'), **args())['ready'])

    def test_autopick_and_button_are_explicit_bools(self):
        for value in (True, None, 0, 'false'):
            x=ui(); x['ownAutoPick']=value
            self.assertFalse(verify_pick(x, **args())['ready'])
        for key in ('enabled','draft_button_present'):
            x=ui(); x['candidates'][0].pop(key)
            self.assertIn('candidate_button_not_observed_enabled', verify_pick(x, **args())['blockers'])

    def test_missing_catalog_collision_wrong_team_replaced_candidate(self):
        a=args(); a.pop('player_catalog')
        self.assertFalse(verify_pick(ui(), **a)['ready'])
        a=args(); a['player_catalog'] *= 2
        self.assertFalse(verify_pick(ui(), **a)['ready'])
        a=args(); a['player_catalog'][0]['team']='NYJ'
        self.assertFalse(verify_pick(ui(), **a)['ready'])
        x=ui(); x['candidates'][0]['position']='QB'
        self.assertFalse(verify_pick(x, **args())['ready'])
        x=ui(); x['candidates'][0]['player_id']='9999'
        self.assertIn('candidate_identity_replaced', verify_pick(x, **args())['blockers'])
        x=ui(); x['candidates'].append(copy.deepcopy(x['candidates'][0]))
        self.assertFalse(verify_pick(x, **args())['ready'])

    def test_actual_image_id_route_and_conflict(self):
        x=ui(); x['candidates'][0]['images']=['https://sleepercdn.com/content/nfl/players/11564.jpg']
        a=args(); a.pop('player_catalog')
        self.assertTrue(verify_pick(x, **a)['ready'])
        x['candidates'][0]['player_id']='9999'
        self.assertFalse(verify_pick(x, **a)['ready'])

    def test_incomplete_changed_board_or_missing_drafted_identity(self):
        x=ui(); x['cells'].pop(0)
        self.assertFalse(verify_pick(x, **args())['ready'])
        x=ui(); x['cells'][0]['images']=[]
        self.assertIn('drafted_missing_player_identity', verify_pick(x, **args())['blockers'])
        a=args(); a['expected_board'][0]['player_id']='8888'
        self.assertIn('board_prefix_changed_or_incomplete', verify_pick(ui(), **a)['blockers'])
        a=args(); a['expected_board']=[]
        self.assertIn('invalid_expected_full_prefix', verify_pick(ui(), **a)['blockers'])

    def test_wrong_own_roster_or_column_or_current_pick(self):
        x=ui(20); x['own']=x['own'].replace('P. 1005','Wrong Player')
        self.assertIn('own_text_cell_mismatch', verify_pick(x, **args(20))['blockers'])
        a=args(20); a['expected_own_roster']=[]
        self.assertFalse(verify_pick(ui(20), **a)['ready'])
        x=ui(); x['own']=None
        self.assertIn('missing_or_wrong_own_column', verify_pick(x, **args())['blockers'])
        a=args(); a['expected_pick']=6
        self.assertFalse(verify_pick(ui(), **a)['ready'])
        x=ui(); x['cells'][-1]['id']='draft-cell-6'
        self.assertFalse(verify_pick(x, **args())['ready'])

    def test_public_api_does_not_establish_readiness(self):
        x=ui();x['source']='public_api'
        self.assertIn('not_ui_evidence', verify_pick(x, **args())['blockers'])

    def test_every_communicated_alternative_checked(self):
        self.assertFalse(verify_shortlist(['11564','1004'], ['1004'])['ready'])
        self.assertFalse(verify_shortlist(['11564','11564'], [])['ready'])
        self.assertFalse(verify_shortlist(['11564',None], [])['ready'])
        self.assertTrue(verify_shortlist(['11564','9493'], ['1004'])['ready'])

    def test_receipt_checks_actual_image_not_intended_name(self):
        x=ui(6); receipt={'pick':5,'intended_id':'1005','readback':x}
        a={k:v for k,v in args().items() if k not in ('candidate','player_catalog')}
        a['expected_player_id']='1005'
        self.assertTrue(verify_receipt(receipt, **a)['verified'])
        receipt['intended_id']='11564';a['expected_player_id']='11564'
        self.assertFalse(verify_receipt(receipt, **a)['verified'])
        receipt['intended_id']='1005';a['expected_player_id']='1005'
        x['cells'][4]['images']=[];x['cells'][4]['player_id']='1005'
        self.assertFalse(verify_receipt(receipt, **a)['verified'])


class TimingAuditTests(unittest.TestCase):
    def test_earliest_observation_not_fresh_reread_and_failures_kept(self):
        events=[{'type':'observation',**ui(4,iso(0))},
                {'type':'observation',**ui(5,iso(10))},
                {'type':'observation',**ui(5,iso(70))},
                {'type':'recommendation','pick':5,'recorded_at':iso(71),
                 'observed_at':iso(70),'exact_user_facing_text':'Recommendation'},
                {'type':'failure','pick':5,'recorded_at':iso(72),'reason':'element absent'},
                {'type':'observation',**ui(20,iso(100),clock='Paused')},
                {'type':'aborted','pick':20,'recorded_at':iso(110)}]
        report=audit_run(events,allowed_mock_id=MOCK,now=iso(120))
        self.assertEqual(report['evaluated_pick_count'],2)
        self.assertEqual(report['picks'][0]['recommendation_latency_bounds']['lower_seconds'],61)
        self.assertEqual(report['picks'][0]['recommendation_latency_bounds']['upper_seconds'],71)
        self.assertEqual(report['timing_counts']['definitely_late'],1)
        self.assertEqual(report['timing_counts']['missing_recommendation'],1)
        self.assertEqual(report['picks'][1]['paused_observation_count'],1)

    def test_unknown_onset_is_not_precise_zero_or_on_time(self):
        events=[{'type':'recommendation','pick':5,'recorded_at':iso(10),
                 'observed_at':iso(9.999),'exact_user_facing_text':'Pick X'},
                {'type':'failure','pick':20,'recorded_at':iso(30)}]
        report=audit_run(events,allowed_mock_id=MOCK,now=iso(40))
        row=report['picks'][0]
        self.assertEqual(row['recommendation_latency_bounds'],
                         {'lower_seconds':0.0,'upper_seconds':None,
                          'interpretation':'Elapsed from unknown true onset; null upper means unbounded from recorded evidence.'})
        self.assertEqual(row['timing_status'],'unresolved_onset')
        self.assertFalse(row['pre_click_delivery_verified'])
        self.assertEqual(report['evaluated_pick_count'],2)

    def test_completed_own_picks_with_missing_action_logs_stay_in_denominator(self):
        report=audit_run([{'type':'observation',**ui(30,iso(50))}],
                         allowed_mock_id=MOCK,now=iso(60))
        self.assertEqual([x['pick'] for x in report['picks']],[5,20,29])
        self.assertEqual(report['timing_counts']['missing_recommendation'],3)
        self.assertTrue(all(x['observed_completed_own_pick'] for x in report['picks']))

    def test_run_abort_and_reported_autopicks_are_not_silently_excluded(self):
        report=audit_run([{'type':'aborted','mock_id':MOCK,'recorded_at':iso(30),
                          'automatic_picks_observed':[{'pick':20,'player_id':'10020'}]}],
                         allowed_mock_id=MOCK,now=iso(40))
        self.assertEqual(report['evaluated_pick_count'],1)
        self.assertEqual(report['timing_counts']['missing_recommendation'],1)
        self.assertEqual(report['picks'][0]['reported_automatic_events'][0]['player_id'],'10020')
        self.assertEqual(len(report['run_events']),1)
        self.assertEqual(report['issues'],[])

    def test_receipt_mismatch_remains_in_denominator(self):
        receipt={'type':'receipt','pick':5,'intended_id':'11564','readback':ui(6,iso(30))}
        report=audit_run([receipt],allowed_mock_id=MOCK,now=iso(40))
        self.assertEqual(report['evaluated_pick_count'],1)
        self.assertFalse(report['picks'][0]['receipts'][0]['actual_id_matches'])

    def test_click_timestamp_and_onset_interval_allow_conservative_pass(self):
        events=[{'type':'observation',**ui(4,iso(10))},
                {'type':'observation',**ui(5,iso(20))},
                {'type':'recommendation','pick':5,'recorded_at':iso(40),'exact_user_facing_text':'Pick X'},
                {'type':'click_attempt','pick':5,'recorded_at':iso(42)}]
        row=audit_run(events,allowed_mock_id=MOCK,now=iso(50))['picks'][0]
        self.assertEqual(row['timing_status'],'guaranteed_within_target')
        self.assertEqual(row['recommendation_latency_bounds']['upper_seconds'],30)
        self.assertTrue(row['pre_click_delivery_verified'])
        self.assertIsNone(row['distinct_actual_attempt_count'])


if __name__ == '__main__': unittest.main()
