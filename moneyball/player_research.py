"""Fast, source-aware draft research lookup; no online requests or rankings.

Examples: python3 -m moneyball.player_research search 'Allen'
          python3 -m moneyball.player_research player 4984
          python3 -m moneyball.player_research status
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .warehouse import canonical


def verified_packet(directory, entry):
    path = directory / entry['path']
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != entry['sha256']:
        raise ValueError('Player packet content hash mismatch: ' + entry['player_id'])
    packet = json.loads(body)
    if packet['player_id'] != entry['player_id']:
        raise ValueError('Packet identity mismatch')
    return packet


def reviews(root, ids):
    directory = root / 'research/player-reviews'
    return {pid: {'path': str(directory / (pid + '.md')),
                  'sha256': hashlib.sha256((directory / (pid + '.md')).read_bytes()).hexdigest(),
                  'status': 'Individual narrative exists; consult its explicit completion status and open inputs.'}
            for pid in ids if (directory / (pid + '.md')).exists()}


def compact_market(record):
    book = record.get('orderbook')
    q = book['top'] if book else record.get('quote')
    return {'source_id': record['source_id'], 'market_id': record['market_id'],
            'question': record.get('question'), 'target': record['target'],
            'quote': q, 'sizes': ({'yes_bid': book.get('best_bid_size'), 'yes_ask': book.get('best_ask_size')}
                                  if book else record.get('quote_sizes')),
            'depth_100_share_sensitivity': book.get('size_sensitivity', {}).get('100') if book else None,
            'observed_at': book['observed_at'] if book else record['observed_at'],
            'source_url': book.get('url') if book else record['market_url'],
            'raw_id': book['raw_id'] if book else record['raw_id'],
            'full_rules_lookup': {'array': 'records', 'market_id': record['market_id']},
            'confidence_interval': None}


def lookup(root, command, query=None):
    root = Path(root)
    directory = root / 'draft/player-packets'
    index = json.loads((directory/'index.json').read_text())
    ids = {p['player_id'] for p in index['players']}
    review_index = reviews(root, ids)
    if command == 'status':
        from .review_audit import audit
        audited = audit(root)
        return {'packet_build': index['build_id'], 'packet_information_cutoff': index['information_cutoff'],
                'research_packets': index['player_count'], 'individual_narratives_present': len(review_index),
                'audited_individual_research_passes': audited['audited_individual_reviews'],
                'source_files_hash_checked': audited['source_files_hash_checked'],
                'review_audit_scope': audited['interpretation'],
                'goal_complete': False, 'coverage': index['coverage_players'],
                'calibrated_title_model_from_these_packets': False}
    matches = [p for p in index['players'] if p['player_id'] == query or query.casefold() in p['name'].casefold()]
    if command == 'search':
        return {'query': query, 'matches': [{k: p[k] for k in ('player_id', 'name', 'position', 'cohort_rank')}
                                          for p in sorted(matches, key=lambda p: p['cohort_rank'])]}
    if len(matches) != 1:
        raise ValueError('Need a unique name or exact player ID; use search first')
    entry = matches[0]
    packet = verified_packet(directory, entry)
    forecasts = packet['professional_forecasts']
    clay = forecasts.get('clay')
    contract = packet.get('contract_context') or {}
    h = packet.get('historical_player_context') or {}
    current = h.get('current_team_context') or {}
    fantasy_path = root/'research/fantasy-exchanges/normalized.json'
    fantasy = json.loads(fantasy_path.read_text()) if fantasy_path.exists() else None
    markets = [compact_market(r) for r in (fantasy['records'] if fantasy else []) if r.get('player_id') == entry['player_id']]
    # Default output stays small; full contracts, market curves and histories are
    # reached through the verified packet rather than dumped into model context.
    by_kind = Counter(r['target']['kind'] for r in markets)
    featured = [r for r in markets if r['target']['kind'] in
                ('fantasy_season_points_per_game_threshold', 'fantasy_season_total_threshold')]
    return {'player_id': entry['player_id'], 'name': packet['name'], 'identity': packet['identity'],
            'packet': {'path': str(directory/entry['path']), 'sha256': entry['sha256'],
                       'information_cutoff': packet['information_cutoff']},
            'individual_review': review_index.get(entry['player_id']),
            'professional_components': {'fftoday': [{'stats': r['stats'], 'known_at': r['known_at'],
                'source_url': r['source_url'], 'conditioning': r['conditioning']} for r in forecasts['fftoday']],
                'clay': None if not clay else {k: clay.get(k) for k in ('stats', 'known_at', 'source_url', 'source_conditioning')},
                'warehouse_season_forecast_count': len(forecasts.get('season', [])),
                'warehouse_weekly_forecast_count': len(forecasts.get('weekly', []))},
            'current_status': packet['observed_player_metadata'],
            'news': [{'headline': r['headline'], 'url': r['article_url'], 'source_published_at': r['source_published_at'],
                      'known_at': r['known_at']} for r in packet['news']['items'][:6]],
            'same_position_roster_observations': [r for r in current.get('other_offensive_roster_members', []) if r['same_position']],
            'contract_context': {'annual_rows_2026_2028': contract.get('annual_rows_2026_2028'),
                                 'money_unit': contract.get('money_unit'),
                                 'expiration': contract.get('contract_expiration_year'),
                                 'caveat': 'Economic rows and roster labels are not calibrated future role/health probabilities.'},
            'fantasy_exchange_overlay': {'source_path': str(fantasy_path) if fantasy else None,
                'source_sha256': hashlib.sha256(fantasy_path.read_bytes()).hexdigest() if fantasy else None,
                'built_at': fantasy['built_at'] if fantasy else None, 'counts_by_target': dict(by_kind),
                'featured_thresholds': featured,
                'espn_scoring_source': 'https://support.espn.com/hc/en-us/articles/360003914032-Scoring-Formats',
                'note': 'Overlay can be newer than packet. Keep each observation cutoff. Provider scoring must be compared against exact configured league scoring; interception penalties can differ. Lines are not means.'},
            'coverage': packet['coverage'], 'quarantined_evidence': packet['quarantined_evidence'],
            'confidence_in_calibrated_dynasty_value': None,
            'draft_instruction': 'Use the individual review and feasible roster continuations; ADP is an acquisition reference, not this tool\'s value estimate.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.moneyball')
    parser.add_argument('command', choices=['search', 'player', 'status'])
    parser.add_argument('query', nargs='?')
    args = parser.parse_args()
    if args.command != 'status' and not args.query:
        parser.error('Search or player requires a query')
    print(canonical(lookup(args.root, args.command, args.query)))
