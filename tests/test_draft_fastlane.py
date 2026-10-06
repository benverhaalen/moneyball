import copy
from datetime import datetime
import json
import unittest

from moneyball import draft_fastlane as fast
from moneyball.draft_room import POLICY, REASONS, board_state
from moneyball.mock_lab import snake_slot
from moneyball.store import digest

MOCK = '9000000000000000002'
NOW = datetime.fromisoformat('2026-09-08T21:04:20+00:00').timestamp()
SOURCE = {'path': '/local/review.md', 'sha256': 'a'*64}


def board(taken, *, at=NOW):
    data = {'league': {'scoring_settings': {'rec': 1}, 'roster_positions': ['QB', 'WR', 'FLEX', 'BN'],
                       'settings': {}}, 'my_roster_id': 7, 'drafts': [{
        'info': {'draft_id': MOCK, 'status': 'drafting', 'type': 'snake',
                 'settings': {'teams': 12, 'rounds': 28}, 'slot_to_roster_id': {'5': 7}},
        'traded_picks': [], 'picks': [{'pick_no': i, 'player_id': p, 'draft_slot': snake_slot(i)}
                                    for i, p in enumerate(taken, 1)]}]}
    return board_state(data, checked_at=at)


def rendered(b):
    return {'source': 'sleeper_rendered_ui', 'observed_at': NOW,
            'draft_id': b['draft_id'], 'taken_player_ids': b['taken'],
            'own_roster': b['own_roster'], 'current_pick': b['picks_made']+1,
            'on_clock': b['on_clock'], 'auto_pick': False}


def fixture():
    names = {'11564': 'Drake Maye', '7564': "Ja'Marr Chase", '6797': 'Justin Herbert',
             '6904': 'Jalen Hurts', '6770': 'Joe Burrow', '9493': 'Puka Nacua'}
    cards = {p: {'player_id': p, 'name': n, 'position': 'QB', 'identity': {'team': 'NE'},
                 'full_review': 'Observed evidence with future uncertainty.'} for p, n in names.items()}
    deck = {'policy': copy.deepcopy(POLICY), 'cards': cards}
    prior = board([str(1000+i) for i in range(1, 6)])
    fresh = board([str(1000+i) for i in range(1, 20)])
    def branch(pid, alt, key, text):
        return {'branch_id': key, 'player_id': pid, 'alternative_id': alt,
            'exact_user_text': text, 'confidence': 'Moderate conditional preference; future path uncertain.',
            'precedence_conditions': 'This authored order applies only while the declared continuation is feasible.',
            'live_mentions_complete': True, 'live_mentions': [pid],
            'requires_all_available': [], 'requires_any_available': [
                {'group_id': 'later-qb', 'player_ids': ['6797', '6904', '6770'], 'min_count': 1}],
            'reasons': {k: 'Explicit authored reasoning explains the roster consequence and its uncertainty.' for k in REASONS},
            'source_refs': [SOURCE]}
    raw = {'schema_version': 1, 'target_pick': 20, 'news_revision': 'reviewed-through-2026-09-08T21:00Z',
           'ordering_rationale': 'Maye first under these conditions; Chase is the authored fallback.',
           'analyzed_player_ids': list(cards), 'watched_player_ids': list(cards), 'branches': [
               branch('11564', '7564', 'maye', 'Drake Maye. The declared later-quarterback group must remain feasible.'),
               branch('7564', '11564', 'chase', "Ja'Marr Chase. Retain at least one acceptable later quarterback path.")]}
    compiled = fast.compile_plan(raw, deck, prior, prepared_at=NOW-120)
    return raw, compiled, deck, prior, fresh


def run(compiled, deck, b, **changes):
    options = {'now': NOW, 'current_news_revision': compiled['authored']['news_revision'],
               'current_source_hashes': {SOURCE['path']: SOURCE['sha256']},
               'visible_slate_ids': [p for p in deck['cards'] if p not in b['taken']],
               'slate_scope': 'complete_top_visible_slate'}
    ui = changes.pop('ui', rendered(b)); options.update(changes)
    return fast.evaluate(compiled, deck, b, ui, **options)


def snipe(b, *ids):
    taken = list(b['taken'])
    # Intervening opponent picks only; our exact prior roster stays fixed.
    for i, pid in enumerate(ids, 6): taken[i-1] = pid
    return board(taken)


class FastlaneTests(unittest.TestCase):
    def test_ready_is_verbatim_author_order_not_a_click_permission(self):
        raw, p, d, _, b = fixture(); r = run(p, d, b)
        self.assertTrue(r['ready_to_recommend'], r)
        self.assertEqual([x['player_id'] for x in r['options']], ['11564', '7564'])
        self.assertEqual(r['options'][0]['exact_user_text'], raw['branches'][0]['exact_user_text'])
        self.assertEqual(r['prepared_at'], NOW-120)
        self.assertEqual(r['checked_at'], NOW)
        self.assertFalse(r['ready_to_execute']); self.assertFalse(r['writes_to_sleeper'])
        self.assertEqual(r['provisional_options'], [])

    def test_first_choice_sniped_uses_only_previously_written_fallback(self):
        _, p, d, _, b = fixture(); r = run(p, d, snipe(b, '11564'))
        self.assertTrue(r['ready_to_recommend'], r)
        self.assertEqual([x['player_id'] for x in r['options']], ['7564'])
        self.assertNotIn('Drake Maye', r['options'][0]['exact_user_text'])

    def test_live_named_alternative_invalidates_entire_text(self):
        raw, _, d, prior, b = fixture()
        raw['branches'][1]['exact_user_text'] += ' Drake Maye remains another available choice.'
        raw['branches'][1]['live_mentions'].append('11564')
        p = fast.compile_plan(raw, d, prior, prepared_at=NOW-120)
        r = run(p, d, snipe(b, '11564'))
        self.assertFalse(r['ready_to_recommend']); self.assertEqual(r['options'], [])
        self.assertIn('no_authored_branch_remains_valid', r['problems'])

    def test_one_of_three_later_targets_lost_does_not_revoke_group(self):
        _, p, d, _, b = fixture()
        for ids in [('6797',), ('6797', '6904')]:
            self.assertTrue(run(p, d, snipe(b, *ids))['ready_to_recommend'])

    def test_all_targets_lost_requires_reasoning(self):
        _, p, d, _, b = fixture(); r = run(p, d, snipe(b, '6797', '6904', '6770'))
        self.assertFalse(r['ready_to_recommend'])
        failure = r['rejected_branches'][0]['failures'][0]
        self.assertEqual(failure['code'], 'continuation_group_below_minimum')
        self.assertEqual(failure['remaining_ids'], [])

    def test_minimum_two_does_not_mean_all_three(self):
        raw, _, d, prior, b = fixture()
        for branch in raw['branches']: branch['requires_any_available'][0]['min_count'] = 2
        p = fast.compile_plan(raw, d, prior)
        self.assertTrue(run(p, d, snipe(b, '6797'))['ready_to_recommend'])
        self.assertFalse(run(p, d, snipe(b, '6797', '6904'))['ready_to_recommend'])

    def test_required_all_and_required_drafted_are_distinct(self):
        raw, _, d, prior, b = fixture()
        raw['branches'][0]['requires_all_available'] = ['6797']
        raw['branches'][1]['requires_drafted'] = ['6797']
        p = fast.compile_plan(raw, d, prior)
        self.assertEqual(run(p, d, b)['options'][0]['player_id'], '11564')
        self.assertEqual(run(p, d, snipe(b, '6797'))['options'][0]['player_id'], '7564')

    def test_unseen_slider_flags_coverage_not_superiority(self):
        raw, p, d, _, b = fixture(); r = run(p, d, b, visible_slate_ids=['11564','7564','9999'])
        self.assertEqual(r['problems'], ['novel_slider_needs_review'])
        self.assertEqual(r['novel_player_ids'], ['9999']); self.assertEqual(r['options'], [])
        self.assertFalse(r['ready_to_recommend']); self.assertTrue(r['needs_reasoning'])
        self.assertFalse(r['ready_to_execute']); self.assertFalse(r['writes_to_sleeper'])
        self.assertEqual([x['player_id'] for x in r['provisional_options']], ['11564', '7564'])
        for rank, (option, authored) in enumerate(zip(r['provisional_options'], raw['branches']), 1):
            self.assertEqual(option['author_rank'], rank)
            self.assertNotIn('rank', option)
            self.assertFalse(option['is_recommendation'])
            self.assertEqual(option['comparison_status'], 'incomplete_visible_slate_review')
            self.assertIn('not a best-player claim', option['display_label'])
            self.assertEqual(option['exact_user_text'], authored['exact_user_text'])
            self.assertEqual(option['reasons'], authored['reasons'])
            self.assertEqual(option['source_refs'], authored['source_refs'])
        # Display consumers cannot mutate the compiled authored judgment.
        r['provisional_options'][0]['reasons']['continuation'] = 'Changed display copy'
        self.assertEqual(p['authored']['branches'][0]['reasons'], raw['branches'][0]['reasons'])

    def test_novel_slate_does_not_resurrect_invalid_branch_or_rewrite_its_text(self):
        raw, _, d, prior, b = fixture()
        raw['branches'][0]['live_mentions'].append('6797')
        raw['branches'][0]['exact_user_text'] += ' Justin Herbert remains available.'
        p = fast.compile_plan(raw, d, prior, prepared_at=NOW-120)
        fresh = snipe(b, '6797')
        r = run(p, d, fresh, visible_slate_ids=['11564', '7564', '9999'])
        self.assertEqual([x['branch_id'] for x in r['provisional_options']], ['chase'])
        self.assertNotIn('Justin Herbert', json.dumps(r['provisional_options']))
        self.assertEqual(r['rejected_branches'][0]['branch_id'], 'maye')
        self.assertEqual(r['options'], []); self.assertFalse(r['ready_to_recommend'])
        depleted = snipe(b, '6797', '6904', '6770')
        r = run(p, d, depleted, visible_slate_ids=['11564', '7564', '9999'])
        self.assertIn('no_authored_branch_remains_valid', r['problems'])
        self.assertEqual(r['provisional_options'], [])

    def test_novel_slate_plus_any_hard_state_or_evidence_failure_exposes_no_text(self):
        _, plan, deck, _, fresh = fixture()
        cases = [
            ('news', {}, {'current_news_revision': 'new'}),
            ('facts', {}, {'unreviewed_fact_ids': ['injury-update']}),
            ('source', {}, {'current_source_hashes': {}}),
            ('old-board', {'checked_at': NOW-16}, {}),
            ('future-board', {'checked_at': NOW+1}, {}),
            ('roster', {'own_roster': ['1005', '6797']}, {}),
            ('slot', {'own_slot': 4}, {}),
            ('rules', {'rules_hash': 'changed'}, {}),
            ('turn', {'on_clock': False}, {}),
            ('prefix-count', {'picks_made': 18}, {}),
        ]
        for label, board_changes, kwargs in cases:
            with self.subTest(label=label):
                b = {**fresh, **board_changes}
                r = run(plan, deck, b, visible_slate_ids=['11564', '7564', '9999'], **kwargs)
                self.assertFalse(r['ready_to_recommend'], r)
                self.assertEqual(r['options'], [])
                self.assertEqual(r['provisional_options'], [], r)
        for label, field, value in [('stale-ui', 'observed_at', NOW-16),
                                    ('future-ui', 'observed_at', NOW+1),
                                    ('invalid-ui', 'observed_at', float('nan')),
                                    ('wrong-room', 'draft_id', '9000000000000000001'),
                                    ('automatic', 'auto_pick', True),
                                    ('wrong-turn', 'on_clock', False),
                                    ('ui-prefix', 'taken_player_ids', ['wrong'])]:
            with self.subTest(label=label):
                u = rendered(fresh); u[field] = value
                r = run(plan, deck, fresh, ui=u, visible_slate_ids=['11564', '7564', '9999'])
                self.assertEqual(r['provisional_options'], [], r)
        for change in ['card', 'policy', 'compiled-plan']:
            with self.subTest(change=change):
                p, d = copy.deepcopy(plan), copy.deepcopy(deck)
                if change == 'card': d['cards']['6797']['full_review'] = 'New dependency evidence'
                elif change == 'policy': d['policy']['version'] = 99
                else: p['authored']['branches'][0]['exact_user_text'] = 'Unbound modification'
                r = run(p, d, fresh, visible_slate_ids=['11564', '7564', '9999'])
                self.assertEqual(r['provisional_options'], [], r)
        revised = list(fresh['taken']); revised[0] = '9000'
        r = run(plan, deck, board(revised), visible_slate_ids=['11564', '7564', '9999'])
        self.assertIn('draft_prefix_revised', r['problems'])
        self.assertEqual(r['provisional_options'], [])
        r = run(plan, deck, snipe(fresh, '11564'), visible_slate_ids=['11564', '7564', '9999'])
        self.assertIn('visible_slate_contains_drafted_players', r['problems'])
        self.assertEqual(r['provisional_options'], [])

    def test_provisional_private_mock_checks_every_displayed_candidate(self):
        from tests.test_mock_lab import ui as mock_ui, args
        _, p, d, _, b = fixture(); raw_ui = mock_ui(20)
        raw_ui['candidates'].append({'name': "Ja'Marr Chase", 'position': 'QB', 'team': 'NE',
                                     'enabled': True, 'draft_button_present': True})
        catalog = args(20)['player_catalog'] + [
            {'player_id': '7564', 'name': "Ja'Marr Chase", 'position': 'QB', 'team': 'NE'}]
        opts = {'private_mock_id': MOCK, 'mock_ui': raw_ui, 'player_catalog': catalog,
                'visible_slate_ids': ['11564', '7564', '9999']}
        r = run(p, d, b, **opts)
        self.assertEqual(len(r['provisional_options']), 2, r)
        self.assertEqual([x['player_id'] for x in r['mock_provisional_control_checks']], ['11564', '7564'])
        self.assertTrue(all(x['control']['ready'] for x in r['mock_provisional_control_checks']))
        self.assertFalse(r['ready_to_execute']); self.assertEqual(r['options'], [])
        for issue in ['disabled-second', 'missing-second', 'paused', 'wrong-room', 'autopick', 'expired']:
            with self.subTest(issue=issue):
                bad = copy.deepcopy(raw_ui)
                if issue == 'disabled-second': bad['candidates'][1]['enabled'] = False
                elif issue == 'missing-second': bad['candidates'].pop()
                elif issue == 'paused': bad['cells'][-1]['text'] = '2.8\nPaused'
                elif issue == 'wrong-room': bad['url'] = bad['url'].split('?')[0] + '/wrong'
                elif issue == 'autopick': bad['ownAutoPick'] = True
                else: bad['cells'][-1]['text'] = '2.8\n00:00'
                r = run(p, d, b, **{**opts, 'mock_ui': bad})
                self.assertEqual(r['provisional_options'], [], r)
                self.assertEqual(r['options'], [])
                self.assertFalse(r['ready_to_recommend'])

    def test_stale_replaced_or_duplicate_slate_blocks(self):
        _, p, d, _, b = fixture()
        r = run(p, d, snipe(b, '11564'), visible_slate_ids=['11564','7564'])
        self.assertIn('visible_slate_contains_drafted_players', r['problems'])
        self.assertFalse(run(p, d, b, visible_slate_ids=['11564','11564'])['ready_to_recommend'])
        self.assertFalse(run(p, d, b, slate_scope='just_my_shortlist')['ready_to_recommend'])

    def test_news_source_evidence_policy_and_roster_changes_invalidate(self):
        _, p, d, _, b = fixture()
        for kwargs, code in [({'current_news_revision':'new'}, 'news_revision_changed'),
                             ({'unreviewed_fact_ids':['report7']}, 'unreviewed_facts'),
                             ({'current_source_hashes':{}}, 'source_reference_changed_or_unchecked:/local/review.md')]:
            self.assertIn(code, run(p, d, b, **kwargs)['problems'])
        changed = copy.deepcopy(d); changed['cards']['6797']['full_review'] = 'new injury evidence'
        self.assertIn('evidence_changed:6797', run(p, changed, b)['problems'])
        changed = copy.deepcopy(d); changed['policy']['version'] = 99
        self.assertIn('strategy_changed', run(p, changed, b)['problems'])
        changed = {**b, 'own_roster':['1005','6797']}
        self.assertIn('own_roster_changed', run(p, d, changed)['problems'])
        self.assertIn('own_slot_changed', run(p, d, {**b,'own_slot':4})['problems'])

    def test_owned_or_teammate_factual_mentions_are_not_live_dependencies(self):
        raw, _, d, prior, b = fixture()
        raw['branches'][0]['exact_user_text'] += ' Our previously selected Player 1005 remains on our roster.'
        p = fast.compile_plan(raw, d, prior)
        self.assertTrue(run(p, d, b)['ready_to_recommend'])

    def test_stale_future_nonfinite_and_wrong_room_ui(self):
        _, p, d, _, b = fixture()
        for stamp in [NOW-16, NOW+1, float('nan'), float('inf'), True, None]:
            u = rendered(b); u['observed_at'] = stamp
            self.assertFalse(run(p, d, b, ui=u)['ready_to_recommend'], stamp)
        u = rendered(b); u['draft_id'] = '9000000000000000001'
        self.assertIn('rendered_board_mismatch:draft_id', run(p, d, b, ui=u)['problems'])
        u = rendered(b); u['auto_pick'] = True
        self.assertIn('manual_pick_mode_not_confirmed', run(p, d, b, ui=u)['problems'])

    def test_prefix_revision_cannot_hide_behind_fresh_hash(self):
        _, p, d, _, b = fixture(); taken = list(b['taken']); taken[0] = '9000'
        r = run(p, d, board(taken)); self.assertIn('draft_prefix_revised', r['problems'])

    def test_missing_schema_and_undeclared_mentions_fail_compilation(self):
        raw, _, d, prior, _ = fixture()
        for key in ['live_mentions_complete','requires_any_available','source_refs','confidence']:
            x = copy.deepcopy(raw); del x['branches'][0][key]
            with self.assertRaises(ValueError): fast.compile_plan(x, d, prior)
        x = copy.deepcopy(raw); x['branches'][0]['live_mentions'] = ['7564']
        with self.assertRaises(ValueError): fast.compile_plan(x, d, prior)

    def test_plan_and_nested_guard_modifications_fail(self):
        _, p, d, _, b = fixture(); p['authored']['branches'][0]['exact_user_text'] = 'Changed'
        self.assertEqual(run(p, d, b)['problems'], ['compiled_plan_modified'])
        _, p, d, _, b = fixture(); p['guard_plan']['own_roster'] = []
        p['compiled_hash'] = fast._payload_hash(p)
        self.assertIn('prepared_plan_modified', run(p, d, b)['problems'])

    def test_identical_completed_bundles_warn_or_reject_false_advantage(self):
        raw, _, d, prior, _ = fixture()
        comparison = {'chosen_then_later':['11564','7564'], 'alternative_then_later':['7564','11564'],
                      'claims_bundle_total_advantage': False}
        raw['branches'][0]['comparison_bundles'] = comparison
        p = fast.compile_plan(raw, d, prior)
        self.assertEqual(p['warnings'][0]['code'], 'identical_bundle_membership')
        comparison['claims_bundle_total_advantage'] = True
        with self.assertRaises(ValueError): fast.compile_plan(raw, d, prior)

    def test_private_mock_reuses_actual_ui_guard_for_paused_and_wrong_room(self):
        from tests.test_mock_lab import ui as mock_ui, args
        _, p, d, _, b = fixture(); a = args(20)
        raw_ui = mock_ui(20)
        opts = {'private_mock_id': MOCK, 'mock_ui':raw_ui, 'player_catalog':a['player_catalog']}
        self.assertTrue(run(p, d, b, **opts)['ready_to_recommend'])
        opts['mock_ui'] = mock_ui(20, clock='Paused')
        self.assertIn('paused', run(p, d, b, **opts)['problems'])
        opts['mock_ui'] = mock_ui(20)
        opts['mock_ui']['url'] = opts['mock_ui']['url'].split('?')[0] + '/wrong'
        self.assertFalse(run(p, d, b, **opts)['ready_to_recommend'])

    def test_fallback_text_for_same_candidate_is_authored_not_rewritten(self):
        raw, _, d, prior, b = fixture()
        primary = raw['branches'][0]
        primary['exact_user_text'] += ' Justin Herbert is an available later target.'
        primary['live_mentions'].append('6797')
        fallback = copy.deepcopy(primary); fallback['branch_id'] = 'maye-fallback'
        fallback['exact_user_text'] = 'Drake Maye. Another member of the declared later group remains available.'
        fallback['live_mentions'] = ['11564']; fallback['requires_drafted'] = ['6797']
        raw['branches'].insert(1, fallback)
        p = fast.compile_plan(raw, d, prior); r = run(p, d, snipe(b, '6797'))
        self.assertEqual(r['options'][0]['branch_id'], 'maye-fallback')
        self.assertNotIn('Justin Herbert', json.dumps(r['options']))


if __name__ == '__main__': unittest.main()
