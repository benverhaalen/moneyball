import json
from pathlib import Path
import tempfile
import unittest

from moneyball.review_audit import audit, digest


class ReviewAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '.moneyball'
        self.packets = self.root / 'draft/player-packets'
        self.reviews = self.root / 'research/player-reviews'
        self.packets.mkdir(parents=True)
        self.reviews.mkdir(parents=True)
        self.packet = self.packets / '1.json'
        self.packet.write_text('{"player_id":"1"}')
        self.entry = {'player_id': '1', 'name': 'Player', 'cohort_rank': 1,
                      'path': '1.json', 'sha256': digest(self.packet)}
        (self.packets / 'index.json').write_text(json.dumps({'build_id': 'frozen', 'players': [self.entry]}))
        self.review = self.reviews / '1.md'
        self.review.write_text('A reviewed case with open model inputs.')

    def publish(self):
        source = self.reviews / 'source.html'
        source.write_text('Observed primary source')
        evidence = {'player_id': '1', 'cohort_rank': 1, 'available_at': 100,
                    'packet': {'sha256': self.entry['sha256']},
                    'primary_sources': [{'raw_path': str(source), 'raw_sha256': digest(source)}]}
        sidecar = self.reviews / '1.reviewed.json'
        sidecar.write_text(json.dumps(evidence))
        row = {'player_id': '1', 'review': str(self.review), 'review_sha256': digest(self.review),
               'evidence': str(sidecar), 'evidence_sha256': digest(sidecar)}
        (self.reviews / 'batch1-manifest.json').write_text(json.dumps({'players': [row]}))

    def test_unmanifested_narrative_is_not_completed(self):
        self.assertEqual(audit(self.root)['audited_individual_reviews'], 0)

    def test_published_provenance_checked_without_calibration_claim(self):
        self.publish()
        result = audit(self.root)
        self.assertEqual(result['audited_individual_reviews'], 1)
        self.assertEqual(result['source_files_hash_checked'], 1)
        self.assertFalse(result['calibrated_championship_model'])

    def test_changed_review_fails(self):
        self.publish()
        self.review.write_text('Unreviewed edit')
        with self.assertRaisesRegex(ValueError, 'Review content hash mismatch'):
            audit(self.root)

    def test_changed_source_receipt_fails(self):
        self.publish()
        (self.reviews / 'source.html').write_text('Revised in place')
        with self.assertRaisesRegex(ValueError, 'Source receipt content hash mismatch'):
            audit(self.root)

    def test_duplicate_completion_fails(self):
        self.publish()
        original = self.reviews / 'batch1-manifest.json'
        (self.reviews / 'batch2-manifest.json').write_bytes(original.read_bytes())
        with self.assertRaisesRegex(ValueError, 'Duplicate current completed review'):
            audit(self.root)

    def test_self_identifying_receipt_does_not_recurse(self):
        self.publish()
        sidecar = self.reviews / '1.reviewed.json'
        evidence = json.loads(sidecar.read_text())
        receipt = self.reviews / 'receipt.json'
        rec = dict(evidence['primary_sources'][0], receipt_path=str(receipt))
        receipt.write_text(json.dumps(rec))
        evidence['primary_sources'] = [{'receipt_path': str(receipt)}]
        sidecar.write_text(json.dumps(evidence))
        manifest = self.reviews / 'batch1-manifest.json'
        rows = json.loads(manifest.read_text())
        rows['players'][0]['evidence_sha256'] = digest(sidecar)
        manifest.write_text(json.dumps(rows))
        self.assertEqual(audit(self.root)['source_files_hash_checked'], 1)


if __name__ == '__main__':
    unittest.main()
