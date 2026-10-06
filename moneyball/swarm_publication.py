"""Fail-closed publication of separately authored and reviewed dossiers.

These checks establish provenance, arithmetic, and transaction completeness.
They do not establish that model-written factual or strategic claims are true.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from .providers import EXPERIMENTAL_REFERENCE_SCORING, score_stats


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def component_expectations(context):
    """Compute observed core subtotals only, without making absent fields zero."""
    scoring = context.get('scoring_settings')
    if scoring is None:
        scoring = (context.get('league_configuration') or {}).get('scoring_settings')
    scoring = EXPERIMENTAL_REFERENCE_SCORING if scoring is None else scoring
    result = {}
    for family, rows in context.get('professional_components', {}).items():
        if not isinstance(rows, list):
            raise ValueError('Professional component family must be a list')
        for index, row in enumerate(rows):
            stats = row.get('stats') or {}
            present = [k for k in scoring if k in stats and stats[k] is not None]
            if not present:
                continue
            key = family + ':' + str(index)
            if row.get('source_key', key) != key:
                raise ValueError('Professional source key does not match its context row')
            subtotal = score_stats(stats, scoring)
            supplied = row.get('league_observed_component_subtotal', row.get('ratrace_observed_component_subtotal'))
            if supplied is not None and (not _finite(supplied) or abs(supplied - subtotal) > .051):
                raise ValueError('Context component subtotal disagrees with provided fields: ' + key)
            result[key] = {'source_key': key, 'source_id': row.get('source_id'),
                           'observed_core_subtotal': subtotal,
                           'observed_scoring_fields': present,
                           'missing_scoring_fields': [k for k in scoring if k not in present],
                           'scope': 'Observed configured components, not full scoring or championship utility.'}
    return result


def validate_components(value, context):
    expected = component_expectations(context)
    checks = value.get('component_checks', [])
    if not isinstance(checks, list):
        raise ValueError('Component checks must be a list')
    actual = {}
    for check in checks:
        key = check.get('source_key')
        if key in actual or key not in expected:
            raise ValueError('Duplicate or unknown component check: ' + str(key))
        actual[key] = check
    if actual.keys() != expected.keys():
        raise ValueError('Missing acquired component checks: ' + ', '.join(sorted(expected.keys() - actual.keys())))
    for key, row in expected.items():
        check = actual[key]
        number = check.get('observed_core_subtotal')
        if not _finite(number) or abs(number - row['observed_core_subtotal']) > .051:
            raise ValueError('Incorrect observed core scoring arithmetic: ' + key)
        if not isinstance(check.get('source_conditioning'), str) or not check['source_conditioning'].strip():
            raise ValueError('Missing source conditioning: ' + key)
        row['author_source_conditioning'] = check['source_conditioning']
    return list(expected.values())


def validate_binding(directory, receipts):
    path = directory / 'review-binding.json'
    binding = json.loads(path.read_text())
    files = binding.get('sha256')
    required = {'draft.json', 'critic.json', 'source-receipts.json', 'packet.json',
                'context.json', 'rules.md'}
    required.update(r['local_text'] for r in receipts if r.get('status') == 200 and not r.get('archive_error'))
    if (directory / 'brief.json').exists():
        required.add('brief.json')
    required.update(str(p.relative_to(directory)) for p in (directory / 'evidence').glob('*.json'))
    if binding.get('schema_version') != 1 or not isinstance(files, dict) or not required.issubset(files):
        raise ValueError('Incomplete review binding')
    if not _finite(binding.get('bound_at')) or binding['bound_at'] <= 0:
        raise ValueError('Review binding has no valid observation time')
    for name, expected in files.items():
        if name not in required and not re.fullmatch(r'source-\d+\.txt', name):
            raise ValueError('Unexpected review-bound path')
        resolved = (directory / name).resolve()
        if not resolved.is_relative_to(directory.resolve()):
            raise ValueError('Review-bound path escapes attempt directory')
        if not isinstance(expected, str) or digest(resolved.read_bytes()) != expected:
            raise ValueError('Reviewed input changed: ' + name)
    return binding, digest(path.read_bytes())


def validate_sources(value, critic, receipts, directory, cache):
    """Validate actual archived bytes and their relationship to reviewed URLs."""
    if critic.get('player_id') != value['player_id']:
        raise ValueError('Critic identity mismatch')
    if critic.get('approved') is not True or critic.get('issues') != []:
        raise ValueError('Review gate failed: outstanding or unspecified issues')
    checks = critic.get('checks_performed')
    verified = critic.get('verified_source_urls')
    if not isinstance(checks, list) or not checks or any(not isinstance(c, str) or not c.strip() for c in checks):
        raise ValueError('Critic supplied no concrete checks')
    if not isinstance(verified, list) or not verified or len(set(verified)) != len(verified):
        raise ValueError('Critic supplied missing or duplicate verified URLs')
    declared = {}
    for source in value['sources']:
        url = source.get('url')
        if not isinstance(url, str) or url in declared:
            raise ValueError('Missing or duplicate author source URL')
        declared[url] = source
    by_url = {}
    for receipt in receipts:
        url = receipt.get('url')
        if not isinstance(url, str) or url in by_url:
            raise ValueError('Missing or duplicate source receipt URL')
        by_url[url] = receipt
    if by_url.keys() != declared.keys():
        raise ValueError('Source receipts do not match author source list')
    successful = set()
    primary = set()
    for url, receipt in by_url.items():
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Non-public source URL in publication')
        if receipt.get('archive_error') or receipt.get('status') != 200:
            continue
        raw = Path(receipt['raw_path']).resolve()
        if not raw.is_relative_to(cache.resolve()):
            raise ValueError('Source raw path escapes archive cache')
        if digest(raw.read_bytes()) != receipt.get('raw_sha256'):
            raise ValueError('Archived source bytes changed')
        name = receipt.get('local_text', '')
        if not re.fullmatch(r'source-\d+\.txt', name) or not (directory / name).is_file():
            raise ValueError('Missing archived source text')
        if not _finite(receipt.get('observed_at')) or receipt['observed_at'] <= 0:
            raise ValueError('Source lacks observation time')
        source = declared[url]
        if receipt.get('read_firsthand') is not True or source.get('read_firsthand') is not True:
            continue
        successful.add(url)
        if receipt.get('primary') is True and source.get('primary') is True:
            primary.add(url)
    if not set(verified).issubset(successful):
        failures = []
        for url in sorted(set(verified) - successful):
            source, receipt = declared.get(url), by_url.get(url)
            reasons = []
            if source is None:
                reasons.append('absent from author source list')
            elif source.get('read_firsthand') is not True:
                reasons.append('author read_firsthand is not true')
            if receipt is None:
                reasons.append('no source receipt')
            else:
                if receipt.get('archive_error'):
                    reasons.append('archive_error=' + str(receipt['archive_error']))
                if receipt.get('status') != 200:
                    reasons.append('HTTP status=' + str(receipt.get('status')))
                if receipt.get('read_firsthand') is not True:
                    reasons.append('receipt read_firsthand is not true')
            failures.append(url + ' [' + '; '.join(reasons) + ']')
        raise ValueError('Critic verified URLs without successful firsthand source receipts: ' +
                         ' | '.join(failures))
    if len(primary.intersection(verified)) < 2:
        raise ValueError('Fewer than two independently checked primary sources; manual review required')
    # Claims in prose must not silently acquire an unarchived external citation.
    cited = {url.rstrip('.,;:') for url in re.findall(r'https://[^\s<>\[\]()"]+', value['dossier_markdown'])}
    if not cited.issubset(set(verified)):
        raise ValueError('Dossier cites URLs not independently verified: ' + ', '.join(sorted(cited - set(verified))))


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data); f.flush(); os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def publish_review(swarm, pid):
    """Manifest is the completion marker; resume byte-identical owned partial work.

    A queue transaction serializes publishers. Exclusive hard-link installation
    never overwrites a manual file appearing concurrently. Every staged file is
    complete before final installation, and the manifest is installed last.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]+', pid):
        raise ValueError('Invalid publication player ID')
    with swarm.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE player_id=?', (pid,)).fetchone()
        if not row or row['state'] not in ('reviewed', 'published'):
            raise ValueError('Independent review has not passed')
        directory = swarm.base / 'jobs' / pid / ('attempt-' + str(row['attempts']))
        value = json.loads((directory / 'draft.json').read_text())
        critic = json.loads((directory / 'critic.json').read_text())
        swarm.check_author(value, pid)
        if critic.get('approved') is not True:
            raise ValueError('Review gate failed')
        if digest((directory / 'packet.json').read_bytes()) != row['packet_sha']:
            raise ValueError('Packet changed')
        context = json.loads((directory / 'context.json').read_text())
        entry = context.get('entry', {})
        if entry.get('player_id') != pid or entry.get('cohort_rank') != row['rank'] or entry.get('sha256') != row['packet_sha']:
            raise ValueError('Reviewed context identity/version differs from queue')
        components = validate_components(value, context)
        receipts = json.loads((directory / 'source-receipts.json').read_text())
        validate_sources(value, critic, receipts, directory, swarm.base / 'source-cache')
        binding, binding_sha = validate_binding(directory, receipts)
        if any(binding['bound_at'] < r['observed_at'] for r in receipts if r.get('status') == 200):
            raise ValueError('Review binding predates source observation')

        target = swarm.root / 'research/player-reviews'
        target.mkdir(parents=True, exist_ok=True)
        paths = {'review.md': target / (pid + '.md'),
                 'evidence.json': target / (pid + '.reviewed.json'),
                 'manifest.json': target / ('batch-swarm-' + pid + '.json')}
        stage = directory / 'publication'
        plan_path = stage / 'plan.json'
        if plan_path.exists():
            plan = json.loads(plan_path.read_text())
            if plan.get('review_binding_sha256') != binding_sha or plan.get('player_id') != pid:
                raise ValueError('Staged publication does not match reviewed inputs')
            if set(plan.get('files', {})) != set(paths):
                raise ValueError('Incomplete publication plan')
            for name, expected in plan['files'].items():
                if digest((stage / name).read_bytes()) != expected:
                    raise ValueError('Staged publication bytes changed')
        else:
            if any(path.exists() for path in paths.values()):
                raise ValueError('Refusing to overwrite existing review without owned publication plan')
            timestamp = time.time()
            review_data = (value['dossier_markdown'] + '\n').encode()
            evidence = {'schema_version': 1, 'player_id': pid, 'cohort_rank': row['rank'],
                        'available_at': timestamp, 'known_at': timestamp,
                        'packet_sha256': row['packet_sha'], 'review_sha256': digest(review_data),
                        'sources': receipts, 'independent_review': critic,
                        'component_arithmetic_checks': components,
                        'searches': value['searches'], 'unresolved': value.get('unresolved', []),
                        'falsification_tests': value['falsification_tests'],
                        'review_binding': binding, 'review_binding_sha256': binding_sha,
                        'model_provenance': str(directory), 'individual_review_written': True,
                        'calibrated_forecast_completed': False, 'individual_championship_delta_estimated': False,
                        'scope': 'Source research with model audit and deterministic integrity/arithmetic checks. Not calibrated title odds.'}
            evidence_data = encoded(evidence)
            manifest_data = encoded({'reviews': [{'player_id': pid, 'review_path': str(paths['review.md']),
                                                  'review_sha256': digest(review_data),
                                                  'evidence_path': str(paths['evidence.json']),
                                                  'evidence_sha256': digest(evidence_data)}]})
            payloads = {'review.md': review_data, 'evidence.json': evidence_data, 'manifest.json': manifest_data}
            for name, data in payloads.items():
                _atomic_bytes(stage / name, data)
            plan = {'schema_version': 1, 'player_id': pid, 'review_binding_sha256': binding_sha,
                    'created_at': timestamp, 'files': {name: digest(data) for name, data in payloads.items()}}
            _atomic_bytes(plan_path, encoded(plan))
        # Check every destination before installing any further file on retry.
        for name, path in paths.items():
            if path.exists() and digest(path.read_bytes()) != plan['files'][name]:
                raise ValueError('Refusing to overwrite changed publication file: ' + path.name)
        resumed = any(path.exists() for path in paths.values())
        for name, path in paths.items():
            try:
                os.link(stage / name, path)
            except FileExistsError:
                if digest(path.read_bytes()) != plan['files'][name]:
                    raise ValueError('Publication destination appeared with different content')
        db.execute("UPDATE jobs SET state='published',updated=?,error=NULL,owner=NULL WHERE player_id=?",
                   (time.time(), pid))
        return {'player_id': pid, 'review_path': str(paths['review.md']), 'resumed_or_already_published': resumed,
                'completion_manifest': str(paths['manifest.json'])}
