"""Audit completed individual research manifests without confusing packets with reviews.

This checks provenance integrity, not the truth or calibration of an analyst's
conclusions. It deliberately ignores unmanifested drafts and archived revisions.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .warehouse import canonical


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_path(root, value, relative_to=None):
    path = Path(value)
    if not path.is_absolute():
        path = (root.parent if path.parts[0] == root.name else relative_to or root) / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('Research reference leaves the data store: ' + str(path))
    return path


def check_hash(path, expected, label):
    if not expected or digest(path) != expected:
        raise ValueError(label + ' content hash mismatch: ' + str(path))


def audit(root):
    root = Path(root).resolve()
    packet_dir = root / 'draft/player-packets'
    directory = root / 'research/player-reviews'
    index = json.loads((packet_dir / 'index.json').read_text())
    entries = {r['player_id']: r for r in index['players']}
    completed = {}
    checked_sources = set()
    checked_receipts = set()

    def receipts(node):
        if isinstance(node, dict):
            if node.get('raw_path'):
                path = local_path(root, node['raw_path'], directory)
                expected = node.get('raw_sha256') or node.get('sha256') or node.get('source_sha256')
                check_hash(path, expected, 'Source receipt')
                checked_sources.add(str(path))
            if node.get('receipt_path'):
                path = local_path(root, node['receipt_path'], directory)
                # Some provenance records identify their own receipt file.
                # Follow each file once; still validate every inline raw hash.
                if path not in checked_receipts:
                    checked_receipts.add(path)
                    rec = json.loads(path.read_text())
                    receipts(rec)
            for value in node.values():
                if isinstance(value, (list, dict)):
                    receipts(value)
        elif isinstance(node, list):
            for item in node:
                receipts(item)

    for manifest in sorted(directory.glob('batch*.json')):
        document = json.loads(manifest.read_text())
        if not isinstance(document, dict):
            continue
        rows = document.get('reviews', document.get('players', []))
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get('review_sha256'):
                continue
            pid = row['player_id']
            if pid not in entries:
                raise ValueError('Reviewed player is outside the frozen cohort: ' + pid)
            if pid in completed:
                raise ValueError('Duplicate current completed review: ' + pid)
            entry = entries[pid]
            review = local_path(root, row.get('review_path') or row['review'], directory)
            check_hash(review, row['review_sha256'], 'Review')
            evidence_path = row.get('evidence_path') or row.get('evidence') or str(directory / (pid + '.reviewed.json'))
            evidence_path = local_path(root, evidence_path, directory)
            if row.get('evidence_sha256'):
                check_hash(evidence_path, row['evidence_sha256'], 'Evidence')
            evidence = json.loads(evidence_path.read_text())
            if evidence['player_id'] != pid or evidence['cohort_rank'] != entry['cohort_rank']:
                raise ValueError('Review identity or cohort rank mismatch: ' + pid)
            if evidence.get('review_sha256'):
                check_hash(review, evidence['review_sha256'], 'Evidence review')
            packet = evidence.get('packet') or {}
            packet_hash = packet.get('sha256') or evidence.get('packet_sha256')
            if packet_hash != entry['sha256']:
                raise ValueError('Review uses a different packet version: ' + pid)
            check_hash(packet_dir / entry['path'], packet_hash, 'Packet')
            available = evidence.get('available_at', evidence.get('known_at'))
            if available is None:
                raise ValueError('Review has no knowability timestamp: ' + pid)
            receipts(evidence)
            completed[pid] = {'player_id': pid, 'name': entry['name'],
                              'cohort_rank': entry['cohort_rank'],
                              'review_path': str(review), 'review_sha256': row['review_sha256'],
                              'evidence_path': str(evidence_path), 'evidence_sha256': digest(evidence_path),
                              'available_at': available, 'manifest': str(manifest),
                              'status': 'Individual research pass; open model inputs remain.'}
    return {'schema_version': 1, 'packet_build': index['build_id'],
            'packet_count': len(entries), 'audited_individual_reviews': len(completed),
            'source_files_hash_checked': len(checked_sources),
            'players': sorted(completed.values(), key=lambda p: p['cohort_rank']),
            'remaining_cohort_ranks': sorted(e['cohort_rank'] for pid, e in entries.items() if pid not in completed),
            'calibrated_championship_model': False,
            'interpretation': 'Integrity and explicit completion evidence only. This does not validate factual judgments, source independence, medical probabilities, or championship effects.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.moneyball')
    parser.add_argument('--output')
    args = parser.parse_args()
    result = audit(args.root)
    if args.output:
        Path(args.output).write_text(canonical(result) + '\n')
        result = {k: v for k, v in result.items() if k not in ('players', 'remaining_cohort_ranks')}
        result['output'] = args.output
    print(canonical(result))
