import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.dossier_scouts import (Scouts, SourceAudits, ROUTES, primary_url, tiny_context,
                                     validate_output, verify_cards, source_rows, partition_output, normalize_text)
from moneyball.dossier_swarm import sha, write_json


def output(pid='p'):
    return {'player_id': pid, 'cards': [
        {'source_url': 'https://www.newyorkjets.com/news/synthetic', 'title': 'Synthetic source',
         'published_at': '2026-09-07', 'event_at': '2026-09-07',
         'claim': 'The coach said Synthetic Player returned to practice.',
         'excerpt': 'Synthetic Player returned to practice.', 'attribution': 'Coach statement',
         'read_firsthand': True, 'fact_type': 'practice_participation'}],
        'searches': [{'route': route, 'query': route + ' synthetic search', 'outcome': 'Source found'} for route in ROUTES],
        'gaps': []}


class ScoutEvidenceTests(unittest.TestCase):
    def test_primary_domain_check_rejects_suffix_tricks_credentials_and_http(self):
        self.assertTrue(primary_url('https://www.nfl.com/news/example'))
        self.assertTrue(primary_url('https://www.chargers.com/news/example'))
        for url in ('http://www.nfl.com/', 'https://nfl.com.evil.example/',
                    'https://user:secret@nfl.com/', 'https://news.example/'):
            self.assertFalse(primary_url(url))

    def test_context_contains_no_forecasts_prices_or_cohort_ranking(self):
        packet = {'draft_season': 2026, 'identity': {'player_id': 'p', 'name': 'Synthetic Player',
                  'position': 'QB', 'adp': 3, 'cohort_rank': 1}, 'professional_forecasts': {'clay': 'secret forecast'},
                  'historical_player_context': {'current_team_context': {'other_offensive_roster_members': [
                      {'name': 'Other QB', 'position': 'QB', 'status': 'ACT'},
                      {'name': 'Other WR', 'position': 'WR', 'status': 'ACT'}]}}}
        context = tiny_context(packet, 1)
        text = json.dumps(context)
        self.assertNotIn('secret forecast', text)
        self.assertNotIn('cohort_rank', text)
        self.assertNotIn('"adp"', text)
        self.assertEqual([p['name'] for p in context['same_position_peers']], ['Other QB'])

    def test_quotes_are_limited_per_source_across_cards(self):
        value = output(); value['cards'][0]['excerpt'] = 'word ' * 11
        value['cards'].append(dict(value['cards'][0]))
        with self.assertRaisesRegex(ValueError, 'quoted words'):
            validate_output(value, 'p')

    def test_four_routes_are_required_for_critical_gaps(self):
        value = output(); value['gaps'] = [{'critical': True, 'question': 'Current availability?',
                                          'routes_attempted': [ROUTES[0]], 'result': 'Unresolved'}]
        with self.assertRaisesRegex(ValueError, 'four attempted'):
            validate_output(value, 'p')
        value['gaps'][0]['routes_attempted'] = list(ROUTES)
        validate_output(value, 'p')
        value['searches'].pop()
        with self.assertRaisesRegex(ValueError, 'Four distinct'):
            validate_output(value, 'p')

    def test_source_rows_do_not_repeat_identical_urls(self):
        cards = output()['cards']; cards.append(dict(cards[0]))
        self.assertEqual(len(source_rows(cards)), 1)

    def test_source_scout_cannot_return_fantasy_strategy_as_a_fact(self):
        value = output(); value['cards'][0]['claim'] = 'His ADP makes him a fantasy bargain.'
        with self.assertRaisesRegex(ValueError, 'fantasy analysis'):
            validate_output(value, 'p')

    def test_salvage_keeps_cards_but_does_not_call_incomplete_gap_unobtainable(self):
        value = output(); value['gaps'] = [{'question': 'Current role?', 'critical': True,
            'routes_attempted': [ROUTES[0]], 'result': 'Unobtainable'}]
        partition = partition_output(value, 'p')
        self.assertEqual(len(partition['cards']), 1)
        self.assertEqual(partition['gaps'][0]['search_status'], 'insufficient_search')
        self.assertTrue(partition['gaps'][0]['result'].startswith('Unknown:'))
        self.assertEqual(partition['gaps'][0]['reported_result'], 'Unobtainable')

    def test_salvage_excludes_only_quote_overage_without_repeating_it(self):
        value = output(); value['cards'][0]['excerpt'] = 'word ' * 11
        value['cards'].append(dict(value['cards'][0]))
        partition = partition_output(value, 'p')
        self.assertEqual(len(partition['cards']), 1)
        self.assertEqual(len(partition['quarantined_cards']), 1)
        self.assertNotIn('excerpt', partition['quarantined_cards'][0])

    def test_typography_equivalence_preserves_numbers_negation_and_other_punctuation(self):
        self.assertEqual(normalize_text('Smith’s “limited”—role'), normalize_text('Smith\'s "limited"-role'))
        self.assertNotEqual(normalize_text('He did not practice.'), normalize_text('He did practice.'))
        self.assertNotEqual(normalize_text('5 games'), normalize_text('6 games'))
        self.assertNotEqual(normalize_text('No, practice.'), normalize_text('No practice.'))
        self.assertEqual(normalize_text('Geno Smith\n, as he went'), normalize_text('Geno Smith, as he went'))
        self.assertEqual(normalize_text("Ricky Pearsall\n's surgery"), normalize_text('Ricky Pearsall’s surgery'))
        self.assertNotEqual(normalize_text('1 .5 games'), normalize_text('1.5 games'))
        self.assertNotEqual(normalize_text('"We have three receivers," he said.'), normalize_text('We have three receivers, he said.'))


class ScoutQueueTests(unittest.TestCase):
    def test_shared_pause_blocks_source_claim_without_consuming_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            scout = Scouts(directory)
            with scout.db() as db:
                db.execute("INSERT INTO source_jobs VALUES('p',181,'hash','pending',0,NULL,?,NULL)", (time.time(),))
            (Path(directory) / 'swarm/claude-pause.json').write_text('malformed but still paused')
            self.assertIsNone(scout.claim())
            self.assertTrue(scout.stop.is_set())
            with scout.db() as db:
                self.assertEqual(tuple(db.execute('SELECT state,attempts FROM source_jobs').fetchone()), ('pending', 0))

    def test_runtime_stop_also_stops_source_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            scout = Scouts(directory)
            scout.runtime.stop.set()
            self.assertIsNone(scout.claim())
            self.assertTrue(scout.stop.is_set())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name) / '.moneyball'
        directory = self.root / 'draft/player-packets'; directory.mkdir(parents=True)
        packet = {'draft_season': 2026, 'identity': {'player_id': 'p', 'name': 'Synthetic Player',
                  'position': 'QB', 'team': 'NYJ', 'observed_name_aliases': []}}
        raw = json.dumps(packet).encode(); (directory / 'p.json').write_bytes(raw)
        write_json(directory / 'index.json', {'information_cutoff': 1, 'players': [
            {'player_id': 'p', 'name': 'Synthetic Player', 'cohort_rank': 181, 'path': 'p.json', 'sha256': sha(raw)}]})
        self.scouts = Scouts(self.root)

    def tearDown(self): self.temp.cleanup()

    def seed(self): self.scouts.seed(181, 181)

    def receipts(self, directory, sources):
        cache = self.scouts.runtime.base / 'source-cache'; cache.mkdir(exist_ok=True)
        raw = b'<p>Coach: Synthetic Player returned to practice.</p>'
        path = cache / (sha(raw) + '.raw'); path.write_bytes(raw)
        (directory / 'source-0.txt').write_text('Coach: Synthetic Player returned to practice.')
        receipt = {**sources[0], 'status': 200, 'observed_at': time.time(), 'raw_path': str(path),
                   'raw_sha256': sha(raw), 'local_text': 'source-0.txt', 'final_url': sources[0]['url']}
        write_json(directory / 'source-receipts.json', [receipt])
        return [receipt]

    def test_seed_idempotence_version_change_and_separate_queue(self):
        self.assertEqual(self.scouts.seed(181, 181)['new_jobs'], 1)
        self.assertEqual(self.scouts.seed(181, 181)['new_jobs'], 0)
        self.assertEqual(self.scouts.runtime.status()['states'], {})
        with self.assertRaisesRegex(ValueError, '181–400'):
            self.scouts.seed(180, 184)
        p = self.root / 'draft/player-packets/index.json'; data = json.loads(p.read_text())
        data['players'][0]['sha256'] = 'changed'; write_json(p, data)
        with self.assertRaisesRegex(ValueError, 'version changed'):
            self.scouts.seed(181, 181)

    def test_claim_is_exclusive_and_recovery_reuses_attempt_number(self):
        self.seed(); first = self.scouts.claim()
        self.assertEqual(first['attempts'], 1)
        self.assertIsNone(Scouts(self.root).claim())
        self.assertEqual(self.scouts.recover()['recovered'], [])
        with patch('moneyball.dossier_scouts.os.kill', side_effect=ProcessLookupError):
            self.assertEqual(self.scouts.recover()['recovered'], ['p'])
        self.assertEqual(self.scouts.claim()['attempts'], 1)

    def test_valid_literal_excerpt_does_not_mark_fact_or_dossier_approved(self):
        self.seed(); row = self.scouts.claim()
        with patch.object(self.scouts.runtime, 'invoke', return_value=output()), \
             patch.object(self.scouts.runtime, 'archive', side_effect=self.receipts):
            result = self.scouts.work(row)
        self.assertEqual(result['state'], 'excerpt_verified')
        report = json.loads((self.scouts.base / 'jobs/p/attempt-1/verified-facts.json').read_text())
        self.assertFalse(report['dossier_completed'])
        self.assertFalse(report['semantic_claims_verified'])
        self.assertFalse(report['cards'][0]['semantic_claim_verified'])
        self.assertFalse((self.root / 'research/player-reviews/p.md').exists())
        self.assertEqual(self.scouts.status()['dossiers_completed'], 0)

    def test_wrong_quote_is_mismatch_not_verified(self):
        self.seed(); row = self.scouts.claim(); value = output()
        value['cards'][0]['excerpt'] = 'Synthetic Player is guaranteed a starting role.'
        with patch.object(self.scouts.runtime, 'invoke', return_value=value), \
             patch.object(self.scouts.runtime, 'archive', side_effect=self.receipts):
            result = self.scouts.work(row)
        self.assertEqual(result['state'], 'mismatch')
        self.assertEqual(result['verified_excerpts'], 0)

    def test_resume_reuses_persisted_facts_and_receipts_without_model_or_network(self):
        self.seed(); row = self.scouts.claim(); directory = self.scouts.base / 'jobs/p/attempt-1'
        directory.mkdir(); (directory / 'context.json').write_bytes((directory.parent / 'context.json').read_bytes())
        write_json(directory / 'facts.json', output()); self.receipts(directory, source_rows(output()['cards']))
        self.scouts.state('p', 'acquired', running=True)
        with patch('moneyball.dossier_scouts.os.kill', side_effect=ProcessLookupError):
            self.scouts.recover()
        resumed = self.scouts.claim()
        with patch.object(self.scouts.runtime, 'invoke', side_effect=AssertionError('No model retry')), \
             patch.object(self.scouts.runtime, 'archive', side_effect=AssertionError('No network retry')):
            self.assertEqual(self.scouts.work(resumed)['state'], 'excerpt_verified')

    def test_wrong_player_and_missing_four_routes_fail_loudly(self):
        self.seed(); row = self.scouts.claim()
        with patch.object(self.scouts.runtime, 'invoke', return_value=output('other')):
            self.assertEqual(self.scouts.work(row)['state'], 'error')
        self.assertIn('identity', self.scouts.status()['problems'][0]['error'])

    def test_failed_source_acquisition_is_retained_as_mismatch(self):
        self.seed(); row = self.scouts.claim()
        with patch.object(self.scouts.runtime, 'invoke', return_value=output()), \
             patch.object(self.scouts.runtime, 'archive', return_value=[{'url': output()['cards'][0]['source_url'],
                         'archive_error': 'HTTP403', 'observed_at': time.time()}]):
            self.assertEqual(self.scouts.work(row)['state'], 'mismatch')

    def test_future_reported_date_and_missing_player_are_not_verified(self):
        directory = self.scouts.base / 'test'; directory.mkdir()
        cards = output()['cards']; receipts = self.receipts(directory, source_rows(cards))
        context = {'identity': {'name': 'Different Player'}, 'as_of_utc': '2026-09-06T00:00:00+00:00'}
        checked = verify_cards(cards, receipts, directory, self.scouts.runtime.base / 'source-cache', context)
        self.assertEqual(checked[0]['verification_status'], 'mismatch')
        self.assertTrue(any('identity' in e for e in checked[0]['verification_errors']))
        self.assertTrue(any('decision date' in e for e in checked[0]['verification_errors']))

    def test_successful_scout_is_not_automatically_retried(self):
        self.seed(); self.scouts.state('p', 'excerpt_verified')
        with self.assertRaisesRegex(ValueError, 'unsuccessful'):
            self.scouts.retry('p')


class SourceAuditTests(ScoutQueueTests):
    def test_shared_pause_blocks_audit_before_enqueue_or_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            auditor = SourceAudits(directory)
            (Path(directory) / 'swarm/claude-pause.json').write_text('{}')
            with patch.object(auditor, 'enqueue', side_effect=AssertionError('Must not enqueue while paused')):
                self.assertIsNone(auditor.claim_audit())
            self.assertTrue(auditor.stop.is_set())

    def ready(self, wrong_quote=False):
        self.seed(); value = output()
        if wrong_quote: value['cards'][0]['excerpt'] = 'Synthetic Player did not practice.'
        with patch.object(self.scouts.runtime, 'invoke', return_value=value), \
             patch.object(self.scouts.runtime, 'archive', side_effect=self.receipts):
            self.scouts.work(self.scouts.claim())
        self.auditor = SourceAudits(self.root)

    def mock_auditor(self, directory, name, prompt, schema, timeout):
        events = [{'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': str(directory / filename)}}]}}
            for filename in ('context.json', 'source-report.json', 'source-0.txt')]
        (directory / (name + '.events.jsonl')).write_text('\n'.join(json.dumps(e) for e in events))
        return {'player_id': 'p', 'checks_performed': ['Read dated source statement'],
                'verified_source_urls': [output()['cards'][0]['source_url']], 'unresolved': [],
                'cards': [{'card_index': 0, 'supported': True, 'issues': [], 'scope_notes': 'Dated coach statement only.'}]}

    def test_audit_queue_claim_exclusivity_and_idempotent_enqueue(self):
        self.ready(); self.assertEqual(self.auditor.enqueue(), 1); self.assertEqual(self.auditor.enqueue(), 0)
        row = self.auditor.claim_audit(); self.assertEqual(row['player_id'], 'p')
        self.assertIsNone(SourceAudits(self.root).claim_audit())
        with patch('moneyball.dossier_scouts.os.kill', side_effect=ProcessLookupError):
            self.assertEqual(self.auditor.recover_audits()['recovered_audit_ids'], [row['audit_id']])
        self.assertEqual(self.auditor.claim_audit()['attempts'], 1)

    def test_independent_source_support_retains_non_dossier_scope(self):
        self.ready(); row = self.auditor.claim_audit()
        with patch.object(self.auditor.runtime, 'invoke', side_effect=self.mock_auditor):
            result = self.auditor.audit_work(row)
        self.assertEqual(result['source_supported'], 1)
        latest = json.loads((self.auditor.base / 'audits/p/latest.json').read_text())
        report = json.loads(Path(latest['report_path']).read_text())
        self.assertFalse(report['dossier_completed'])
        self.assertEqual(report['cards'][0]['status'], 'source_supported')
        self.assertEqual(self.scouts.status()['states'], {'excerpt_verified': 1})

    def test_auditor_cannot_upgrade_a_failed_literal_quote(self):
        self.ready(wrong_quote=True); row = self.auditor.claim_audit()
        with patch.object(self.auditor.runtime, 'invoke', side_effect=self.mock_auditor):
            result = self.auditor.audit_work(row)
        self.assertEqual(result['source_supported'], 0)

    def test_auditor_must_cover_all_cards(self):
        self.ready(); row = self.auditor.claim_audit()
        def incomplete(*args, **kwargs):
            result = self.mock_auditor(*args, **kwargs); result['cards'] = []; return result
        with patch.object(self.auditor.runtime, 'invoke', side_effect=incomplete):
            result = self.auditor.audit_work(row)
        self.assertEqual(result['state'], 'error')
        self.assertIn('every card', result['error'])

    def test_support_requires_an_observed_read_of_archived_source(self):
        self.ready(); row = self.auditor.claim_audit()
        def did_not_read_source(directory, name, prompt, schema, timeout):
            result = self.mock_auditor(directory, name, prompt, schema, timeout)
            path = directory / (name + '.events.jsonl'); path.write_text('\n'.join(path.read_text().splitlines()[:2]))
            return result
        with patch.object(self.auditor.runtime, 'invoke', side_effect=did_not_read_source):
            result = self.auditor.audit_work(row)
        self.assertEqual(result['source_supported'], 0)

    def test_read_trace_tolerates_cli_system_message_strings(self):
        self.ready(); row = self.auditor.claim_audit()
        def mixed_messages(directory, name, prompt, schema, timeout):
            result = self.mock_auditor(directory, name, prompt, schema, timeout)
            path = directory / (name + '.events.jsonl')
            with path.open('a') as f:
                f.write('\n' + json.dumps({'type': 'system', 'message': 'Routine CLI notice'}))
            return result
        with patch.object(self.auditor.runtime, 'invoke', side_effect=mixed_messages):
            self.assertEqual(self.auditor.audit_work(row)['source_supported'], 1)


if __name__ == '__main__': unittest.main()
