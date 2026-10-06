"""Strict offline readers for observed public 2026 professional source tables.

These preserve source semantics. They do not generate projections, infer injuries,
turn ranks into points, or fetch gated data. Network acquisition receipts are inputs.
"""
import html
import json
import math
import re
from datetime import datetime
from html.parser import HTMLParser


def plain(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', value)).split())


def attrs(tag):
    class FirstTag(HTMLParser):
        def handle_starttag(self, name, attributes):
            if not hasattr(self, 'found'):
                self.found = dict(attributes)
    p = FirstTag()
    p.feed(tag)
    return getattr(p, 'found', {})


def number(value, *, maximum=20000):
    n = float(str(value).replace(',', ''))
    if not math.isfinite(n) or not 0 <= n <= maximum:
        raise ValueError('Invalid nonnegative source number')
    return n


def provenance(receipt):
    dt = datetime.fromisoformat(receipt['known_at'].replace('Z', '+00:00'))
    if dt.utcoffset() is None or receipt.get('status') != 200 or not receipt.get('sha256'):
        raise ValueError('Need successful dated raw receipt with hash')
    return {'source_url': receipt['url'], 'known_at': receipt['known_at'],
            'raw_sha256': receipt['sha256'], 'historical_vintage_verified': False}


FFT_FIELDS = {
    'QB': [('Cmp', 'pass_cmp'), ('Att', 'pass_att'), ('Yds', 'pass_yd'), ('TD', 'pass_td'),
           ('INT', 'pass_int'), ('Att', 'rush_att'), ('Yds', 'rush_yd'), ('TD', 'rush_td')],
    'RB': [('Att', 'rush_att'), ('Yds', 'rush_yd'), ('TD', 'rush_td'),
           ('Rec', 'rec'), ('Yds', 'rec_yd'), ('TD', 'rec_td')],
    'WR': [('Rec', 'rec'), ('Yds', 'rec_yd'), ('TD', 'rec_td'),
           ('Att', 'rush_att'), ('Yds', 'rush_yd'), ('TD', 'rush_td')],
    'TE': [('Rec', 'rec'), ('Yds', 'rec_yd'), ('TD', 'rec_td')],
}


def fftoday(body, position, receipt, season=2026):
    """Parse one visible page; stat groups and row widths must match exactly."""
    p = provenance(receipt)
    if position not in FFT_FIELDS or not re.search(r'Projections:\s*' + str(season), body):
        raise ValueError('Wrong position, season or non-projection page')
    update = re.search(r'Regular Season, Updated:\s*(\d+/\d+/\d+)', body)
    if not update:
        raise ValueError('Source update date missing')
    published = datetime.strptime(update.group(1), '%m/%d/%Y').date().isoformat()
    if int(published[:4]) != season:
        raise ValueError('Wrong publication year')
    header = re.search(r'<tr class=[\'"]tableclmhdr[\'"]>(.*?)</tr>', body, re.S | re.I)
    if not header:
        raise ValueError('Missing FFToday column header')
    labels = [plain(c) for c in re.findall(r'<td\b[^>]*>(.*?)</td>', header[1], re.S | re.I)]
    if labels[4:] != [x[0] for x in FFT_FIELDS[position]] + ['FPts']:
        raise ValueError('FFToday component columns changed')
    rows, seen = [], set()
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', body, re.S | re.I):
        match = re.search(r'<a\s+href=[\'"]/stats/players/(\d+)/[^\'"]+[\'"][^>]*>(.*?)</a>', row, re.S | re.I)
        if not match:
            continue
        sid, name = match[1], plain(match[2])
        cells = [plain(c) for c in re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.S | re.I)]
        if sid in seen or len(cells) != len(labels) or cells[1] != name:
            raise ValueError('Duplicate player, nested/shifted table or malformed row')
        seen.add(sid)
        stats = {field: number(value) for (_, field), value in zip(FFT_FIELDS[position], cells[4:-1])}
        if stats.get('pass_cmp', 0) > stats.get('pass_att', 0):
            raise ValueError('Completions exceed attempts')
        rows.append({**p, 'source_id': 'fftoday', 'source_player_id': sid, 'name': name,
                     'position': position, 'team': cells[2], 'bye': int(number(cells[3], maximum=18)),
                     'season': season, 'source_stated_updated': published,
                     'kind': 'statistical_projection_components', 'stats': stats,
                     'source_fantasy_points_reference': number(cells[-1]),
                     'conditioning': 'Full regular-season table; individual games and injury treatment not specified here.',
                     'omitted_components': 'Unknown, never implicit zeros; TE table omits rushing and all tables omit fumbles/two-point events.',
                     'uncertainty': None})
    if not rows:
        raise ValueError('No FFToday player rows')
    return rows


def assigned_json(body, variable):
    m = re.search(r'var\s+' + re.escape(variable) + r'\s*=\s*', body)
    if not m:
        raise ValueError('Expected public page payload absent')
    return json.JSONDecoder().raw_decode(body[m.end():])[0]


def draftsharks_adp(body, receipt):
    """Only the actual anonymous page's seeded sets; never calls another endpoint."""
    p = provenance(receipt)
    if not re.search(r'<title\b[^>]*>[^<]*2026', body, re.I):
        raise ValueError('Need explicit current 2026 source title')
    obj = assigned_json(body, 'vueAppData')
    descriptors = {r['key']: r for r in obj['availability']}
    players, rows = obj['seed']['players'], []
    for key, records in obj['seed']['adpSets'].items():
        d, seen = descriptors[key], set()
        for r in records:
            sid = str(r['id'])
            if sid in seen or sid not in players:
                raise ValueError('Duplicate/unidentified ADP player')
            seen.add(sid)
            meta = players[sid]
            rows.append({**p, 'source_id': 'draftsharks_adp', 'source_player_id': sid,
                         'name': f"{meta['fn']} {meta['ln']}", 'team': meta['tm'], 'position': meta['pos'],
                         'season': 2026, 'kind': 'observed_adp_reference', 'originating_platform': d['source'],
                         'format': {'scoring': d['scoring'], 'superflex': bool(d['superflex']),
                                    'teams': d['size'], 'type': d['type'] or 'redraft'},
                         'adp': number(r['pick']), 'position_adp': r['posAdp'],
                         'ds_rank_overlay_reference': r['dsRank'],
                         'sample_size': None, 'draft_window': None,
                         'interpretation': 'Platform acquisition reference; DS rank overlay is not a component forecast or dynasty value.'})
    if not rows:
        raise ValueError('No seeded ADP rows')
    return rows


def draftsharks_rankings(body, receipt, scope):
    """Keep the visible anonymous 25-row preview and its column definitions."""
    p = provenance(receipt)
    if not re.search(r'<title\b[^>]*>[^<]*2026', body, re.I):
        raise ValueError('Need explicit current 2026 source title')
    source_config = assigned_json(body, 'appData')
    rows = []
    definitions = {}
    for tag in re.findall(r'<th\b[^>]*>', body, re.I):
        a = attrs(tag)
        if a.get('data-attribute'):
            definitions[a['data-attribute']] = a.get('data-ds-tooltip')
    for row in re.findall(r'<tr class="player-row">(.*?)</tr>', body, re.S):
        mt = re.search(r'<player-name\b[^>]*>', row)
        if not mt:
            raise ValueError('Missing player identity')
        a = attrs(mt[0])
        rank = re.search(r'rank-index[^>]*>\s*<span>(\d+)</span>', row)
        team = re.search(r'player-details-group__team-name">([^<]+)', row)
        pos = re.search(r'pos-roster-spot="([^"]+)"', row)
        if not rank or not team or not pos:
            raise ValueError('Missing rank/team/position')
        values = {}
        for tag in re.findall(r'<td\b[^>]*>', row):
            at = attrs(tag)
            if at.get('data-attribute'):
                values[at['data-attribute']] = at.get('data-value')
        rows.append({**p, 'source_id': 'draftsharks_team', 'source_player_id': a['player-id'],
                     'name': f"{a['first-name']} {a['last-name']}", 'position': pos[1], 'team': team[1],
                     'season': 2026, 'kind': 'provider_rank_and_reference_metrics', 'scope': scope,
                     'rank': int(rank[1]), 'raw_reference_metrics': values,
                     'format': {'type': source_config['leagueType'],
                                'scoring': source_config['pprScoring'],
                                'superflex': source_config['superflex']},
                     'source_scoring_config': source_config['selectedScoringConfig'],
                     'source_field_definitions': definitions,
                     'provider': 'Draft Sharks Team; page reviewed by Jared Smola, not an individual Jody Smith ranking',
                     'conditioning': 'Selected redraft format; source labels DS Proj a median and floor as barring injury.',
                     'uncertainty': 'Floor/ceiling have no verified quantile or coverage here; injury percentage is an unvalidated vendor output, not an independent measured probability.'})
    if not rows or len({r['source_player_id'] for r in rows}) != len(rows):
        raise ValueError('Empty or duplicate DS ranking preview')
    return rows


def draftsharks_news(body, receipt):
    p = provenance(receipt)
    rows = []
    for article in re.findall(r'<article\b[^>]*>.*?</article>', body, re.S):
        sid = attrs(article)['data-id']
        h = re.search(r'<h2[^>]*>\s*<a href="([^"]+)">(.*?)</a>', article, re.S)
        timestamp = re.search(r'<time\b[^>]*\bdatetime="([^"]+)"', article)
        summary = re.search(r'<div class="article-summary">(.*?)</div>', article, re.S)
        named = re.findall(r'<p class="name" data-name="([^"]+)"', article)
        player_ids = re.findall(r'class="player-detail-card" data-pid="([^"]+)"', article)
        team = re.findall(r'data-team="([^"]+)"', article)
        if not h or not timestamp or not summary or len(named) != len(player_ids) or len(team) != len(named):
            raise ValueError('News listing schema changed')
        published = datetime.fromisoformat(timestamp[1].replace('Z', '+00:00'))
        acquired = datetime.fromisoformat(p['known_at'].replace('Z', '+00:00'))
        if published > acquired:
            raise ValueError('News publication is after acquisition')
        rows.append({**p, 'source_id': 'draftsharks_news', 'article_id': sid,
                     'article_url': 'https://www.draftsharks.com' + h[1], 'source_published_at': timestamp[1],
                     'headline': plain(h[2]), 'listing_summary': plain(summary[1]),
                     'players': [{'source_player_id': i, 'name': html.unescape(n), 'team': t}
                                 for i, n, t in zip(player_ids, named, team)],
                     'kind': 'dated_secondary_news_observation',
                     'status': 'Listing observed; original report and full story not independently corroborated by this ingestion.',
                     'model_adjustment': None})
    if not rows:
        raise ValueError('No news listings')
    return rows
