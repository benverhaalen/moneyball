"""Adversarial publication tests; all evidence and model output are synthetic."""
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from moneyball.dossier_swarm import Swarm, sha, write_json
from moneyball.swarm_publication import component_expectations, validate_components


class ComponentAuditTests(unittest.TestCase):
    def setUp(self):
        self.context = {'professional_components': {
            'clay': [{'source_id': 'espn_mike_clay_pdf', 'stats': {
                'rush_att': 151, 'rush_yd': 659, 'rush_td': 3,
                'rec_tgt': 60, 'rec': 47, 'rec_yd': 344, 'rec_td': 2}}],
            'fftoday': [{'source_id': 'fftoday', 'stats': {
                'rush_yd': 602, 'rush_td': 3, 'rec': 37, 'rec_yd': 268, 'rec_td': 1}}]}}

    def checks(self):
        return {'component_checks': [
            {'source_key': key, 'observed_core_subtotal': row['observed_core_subtotal'],
             'source_conditioning': 'Displayed observed components; injury conditioning unspecified.'}
            for key, row in component_expectations(self.context).items()]}

    def test_correct_known_arithmetic_without_fabricating_missing_fumbles(self):
        rows = validate_components(self.checks(), self.context)
        self.assertAlmostEqual(rows[0]['observed_core_subtotal'], 177.3)
        self.assertAlmostEqual(rows[1]['observed_core_subtotal'], 148.0)
        self.assertIn('fum_lost', rows[0]['missing_scoring_fields'])
        self.assertNotIn('rush_att', rows[0]['observed_scoring_fields'])

    def test_pilot_wrong_152_point_claim_is_rejected(self):
        value = self.checks(); value['component_checks'][0]['observed_core_subtotal'] = 152
        with self.assertRaisesRegex(ValueError, 'scoring arithmetic'):
            validate_components(value, self.context)

    def test_omitted_acquired_provider_is_rejected(self):
        value = self.checks(); value['component_checks'].pop()
        with self.assertRaisesRegex(ValueError, 'Missing acquired'):
            validate_components(value, self.context)

    def test_no_phantom_check_for_absent_statistical_forecasts(self):
        context = {'professional_components': {'clay': [], 'season': [{'stats': {'adp_ppr': 150}}]}}
        self.assertEqual(validate_components({}, context), [])

    def test_duplicate_rows_nonfinite_numbers_and_bad_context_total_fail(self):
        value = self.checks(); value['component_checks'].append(value['component_checks'][0])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            validate_components(value, self.context)
        for number in (float('nan'), float('inf'), True):
            value = self.checks(); value['component_checks'][0]['observed_core_subtotal'] = number
            with self.assertRaisesRegex(ValueError, 'scoring arithmetic'):
                validate_components(value, self.context)
        self.context['professional_components']['clay'][0]['league_observed_component_subtotal'] = 152
        with self.assertRaisesRegex(ValueError, 'Context component subtotal'):
            component_expectations(self.context)

    def test_source_keys_disambiguate_multiple_rows_of_same_provider(self):
        self.context['professional_components']['clay'].append({'source_id': 'espn_mike_clay_pdf', 'stats': {'rec': 20}})
        rows = validate_components(self.checks(), self.context)
        self.assertEqual([r['source_key'] for r in rows], ['clay:0', 'clay:1', 'fftoday:0'])


class PublicationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / '.moneyball'
        packet_dir = self.root / 'draft/player-packets'; packet_dir.mkdir(parents=True)
        packet = {'player_id': 'p', 'professional_forecasts': {'clay': [{'source_id': 'clay', 'stats': {'rec': 20}}]}}
        raw = json.dumps(packet).encode(); (packet_dir / 'p.json').write_bytes(raw)
        write_json(packet_dir / 'index.json', {'information_cutoff': 1, 'players': [
            {'player_id': 'p', 'cohort_rank': 177, 'name': 'Synthetic Player', 'path': 'p.json', 'sha256': sha(raw)}]})
        target = self.root / 'research/player-reviews'; target.mkdir(parents=True)
        (target / 'README.md').write_text('Synthetic rules')
        self.swarm = Swarm(self.root); self.swarm.seed(177, 177); self.swarm.claim()
        self.swarm.state('p', 'reviewed')
        job = self.swarm.base / 'jobs/p'; self.attempt = job / 'attempt-1'; self.attempt.mkdir()
        for name in ('packet.json', 'context.json', 'rules.md'):
            shutil.copyfile(job / name, self.attempt / name)
        self.sources = [{'url': 'https://example.org/source-' + str(i), 'primary': True,
                         'read_firsthand': True, 'title': 'Synthetic evidence',
                         'published_at': '2026-09-08', 'claim_supported': 'Synthetic role observation.'}
                        for i in range(2)]
        self.draft = {'player_id': 'p', 'dossier_markdown': 'synthetic analysis ' * 300,
                      'sources': self.sources, 'searches': ['synthetic query'],
                      'falsification_tests': ['Observe a changed assignment'], 'unresolved': [],
                      'packet_sections_read': ['professional_forecasts'],
                      'component_checks': [{'source_key': 'clay:0', 'observed_core_subtotal': 20,
                                            'source_conditioning': 'Observed core subtotal; no availability distribution.'}]}
        self.critic = {'player_id': 'p', 'approved': True, 'issues': [],
                       'checks_performed': ['Compared source observation with narrative'],
                       'verified_source_urls': [s['url'] for s in self.sources]}
        cache = self.swarm.base / 'source-cache'; cache.mkdir()
        receipts = []
        for i, source in enumerate(self.sources):
            raw = ('<p>Synthetic source ' + str(i) + '</p>').encode()
            raw_path = cache / (sha(raw) + '.raw'); raw_path.write_bytes(raw)
            (self.attempt / ('source-' + str(i) + '.txt')).write_text('Synthetic source ' + str(i))
            receipts.append({**source, 'status': 200, 'observed_at': time.time(),
                             'raw_sha256': sha(raw), 'raw_path': str(raw_path),
                             'local_text': 'source-' + str(i) + '.txt'})
        write_json(self.attempt / 'source-receipts.json', receipts)
        self.save_outputs(); self.bind()

    def tearDown(self):
        self.temp.cleanup()

    def save_outputs(self):
        write_json(self.attempt / 'draft.json', self.draft)
        write_json(self.attempt / 'critic.json', self.critic)

    def bind(self):
        files = ['draft.json', 'critic.json', 'source-receipts.json', 'packet.json', 'context.json', 'rules.md',
                 'source-0.txt', 'source-1.txt']
        write_json(self.attempt / 'review-binding.json', {'schema_version': 1, 'bound_at': time.time(),
                   'sha256': {name: sha((self.attempt / name).read_bytes()) for name in files}})

    def assert_no_publication(self):
        self.assertFalse((self.root / 'research/player-reviews/p.md').exists())
        self.assertFalse((self.root / 'research/player-reviews/batch-swarm-p.json').exists())

    def test_success_is_idempotent_and_marks_uncalibrated_scope(self):
        result = self.swarm.publish('p')
        before = (self.root / 'research/player-reviews/p.reviewed.json').read_bytes()
        self.assertTrue(Path(result['completion_manifest']).is_file())
        self.assertFalse(json.loads(before)['calibrated_forecast_completed'])
        self.assertTrue(self.swarm.publish('p')['resumed_or_already_published'])
        self.assertEqual(before, (self.root / 'research/player-reviews/p.reviewed.json').read_bytes())

    def test_draft_edited_after_review_cannot_publish(self):
        self.draft['dossier_markdown'] += ' An unsupported new claim.'; self.save_outputs()
        with self.assertRaisesRegex(ValueError, 'Reviewed input changed: draft'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_critic_approval_requires_matching_identity_and_zero_issues(self):
        self.critic['player_id'] = 'other'; self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'Critic identity'):
            self.swarm.publish('p')
        self.critic['player_id'] = 'p'; self.critic['issues'] = ['Unsupported medical claim']; self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'outstanding'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_critic_cannot_claim_unarchived_verified_source(self):
        self.critic['verified_source_urls'].append('https://example.org/unseen'); self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'without successful firsthand'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_source_failure_identifies_exact_url_and_unread_marker(self):
        self.draft['sources'][1]['read_firsthand'] = False
        self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'https://example.org/source-1.*author read_firsthand is not true'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_unknown_publication_date_is_null_and_not_invented_from_observation(self):
        from moneyball.dossier_swarm import SOURCE
        self.assertIn('null', SOURCE['properties']['published_at']['type'])
        self.draft['sources'][0]['published_at'] = None
        receipts = json.loads((self.attempt / 'source-receipts.json').read_text())
        receipts[0]['published_at'] = None
        write_json(self.attempt / 'source-receipts.json', receipts)
        self.save_outputs(); self.bind()
        self.assertTrue(Path(self.swarm.publish('p')['completion_manifest']).exists())

    def test_missing_critic_source_diagnostic_names_absent_receipt(self):
        self.critic['verified_source_urls'].append('https://example.org/new-rules')
        self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'https://example.org/new-rules.*absent from author source list; no source receipt'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_no_primary_source_substitution_with_secondary_only_evidence(self):
        path = self.attempt / 'source-receipts.json'; receipts = json.loads(path.read_text())
        receipts[1]['primary'] = False; write_json(path, receipts); self.bind()
        with self.assertRaisesRegex(ValueError, 'Fewer than two'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_corrupted_raw_source_is_rejected_before_any_output(self):
        receipt = json.loads((self.attempt / 'source-receipts.json').read_text())[0]
        Path(receipt['raw_path']).write_text('different content')
        with self.assertRaisesRegex(ValueError, 'Archived source bytes changed'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_extracted_source_text_is_bound_to_review(self):
        (self.attempt / 'source-0.txt').write_text('injected statement after review')
        with self.assertRaisesRegex(ValueError, 'Reviewed input changed: source-0'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_missing_binding_text_entry_cannot_be_ignored(self):
        p = self.attempt / 'review-binding.json'; binding = json.loads(p.read_text())
        binding['sha256'].pop('source-0.txt'); write_json(p, binding)
        with self.assertRaisesRegex(ValueError, 'Incomplete review binding'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_readable_evidence_and_brief_are_also_required_and_bound(self):
        write_json(self.attempt / 'brief.json', {'warning': 'Synthetic summary'})
        write_json(self.attempt / 'evidence/professional_forecasts.json', {'clay': 'Synthetic components'})
        with self.assertRaisesRegex(ValueError, 'Incomplete review binding'):
            self.swarm.publish('p')
        path = self.attempt / 'review-binding.json'; binding = json.loads(path.read_text())
        for name in ('brief.json', 'evidence/professional_forecasts.json'):
            binding['sha256'][name] = sha((self.attempt / name).read_bytes())
        write_json(path, binding)
        self.assertTrue(Path(self.swarm.publish('p')['completion_manifest']).exists())

    def test_readable_evidence_cannot_escape_via_symlink(self):
        private = Path(self.temp.name) / 'not-part-of-evidence.json'; private.write_text('private')
        (self.attempt / 'evidence').mkdir()
        (self.attempt / 'evidence/identity.json').symlink_to(private)
        path = self.attempt / 'review-binding.json'; binding = json.loads(path.read_text())
        binding['sha256']['evidence/identity.json'] = sha(private.read_bytes()); write_json(path, binding)
        with self.assertRaisesRegex(ValueError, 'escapes attempt'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_missing_component_math_fails_even_when_critic_approves(self):
        self.draft['component_checks'] = []; self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'Missing acquired'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_unverified_inline_citation_is_rejected(self):
        self.draft['dossier_markdown'] += ' [Unsupported](https://example.org/never-read).'
        self.save_outputs(); self.bind()
        with self.assertRaisesRegex(ValueError, 'not independently verified'):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_missing_receipts_cannot_leave_an_orphan_review(self):
        (self.attempt / 'source-receipts.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.swarm.publish('p')
        self.assert_no_publication()

    def test_existing_manual_work_is_not_overwritten(self):
        p = self.root / 'research/player-reviews/p.md'; p.write_text('manual work')
        with self.assertRaisesRegex(ValueError, 'overwrite existing'):
            self.swarm.publish('p')
        self.assertEqual(p.read_text(), 'manual work')

    def test_interrupted_install_has_no_manifest_and_resumes_identically(self):
        import moneyball.swarm_publication as publication
        original = publication.os.link
        count = 0
        def interrupted(source, destination):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('synthetic interruption before completion marker')
            return original(source, destination)
        with patch.object(publication.os, 'link', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'synthetic interruption'):
                self.swarm.publish('p')
        target = self.root / 'research/player-reviews'
        self.assertFalse((target / 'batch-swarm-p.json').exists())
        self.assertEqual(self.swarm.status()['states'], {'reviewed': 1})
        partial = (target / 'p.reviewed.json').read_bytes()
        self.assertTrue(self.swarm.publish('p')['resumed_or_already_published'])
        self.assertEqual(partial, (target / 'p.reviewed.json').read_bytes())

    def test_changed_partial_publication_is_not_overwritten_on_retry(self):
        import moneyball.swarm_publication as publication
        original = publication.os.link
        def interrupted(source, destination):
            if str(destination).endswith('.reviewed.json'):
                raise OSError('synthetic interruption')
            return original(source, destination)
        with patch.object(publication.os, 'link', side_effect=interrupted):
            with self.assertRaises(OSError): self.swarm.publish('p')
        p = self.root / 'research/player-reviews/p.md'; p.unlink(); p.write_text('new manual work')
        with self.assertRaisesRegex(ValueError, 'changed publication file'):
            self.swarm.publish('p')
        self.assertEqual(p.read_text(), 'new manual work')


if __name__ == '__main__':
    unittest.main()
