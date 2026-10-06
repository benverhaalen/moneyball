"""Inspect saved NFL fantasy-score thresholds without inventing probabilities.

Offline normalization only. No entries, authentication, or automatic refresh.
"""
import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import json
import math
from pathlib import Path
import time

from .warehouse import canonical, timestamp

RULES_URL = 'https://www.prizepicks.com/playbook-article/how-to-play-prizepicks-nfl-fantasy-scoring-system'


def normalize_prizepicks(body, receipt, *, cutoff):
    """Preserve standard/alternate quotes and each observed settlement horizon."""
    receipt=copy.deepcopy(receipt)
    if isinstance(receipt.get('headers'),dict):
        receipt['headers']={k:v for k,v in receipt['headers'].items()
                            if k.lower() not in ('set-cookie','cookie','authorization')}
    cutoff = timestamp(cutoff)
    for key in ('observed_at', 'available_at'):
        if timestamp(receipt[key]) > cutoff:
            raise ValueError('Quote snapshot is later than evaluation cutoff')
    if receipt.get('http_status') != 200:
        raise ValueError('Quote source did not return success')
    if not isinstance(body.get('data'), list) or not isinstance(body.get('included'), list):
        raise ValueError('Missing JSON API collections')
    if body.get('meta', {}).get('total_pages') != 1:
        raise ValueError('Incomplete or unknown pagination; collect all pages before normalization')
    inc = {(x['type'], str(x['id'])):x for x in body['included']}
    if len(inc) != len(body['included']):
        raise ValueError('Duplicate included resource identity')
    ids = [str(x['id']) for x in body['data']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate projection identity')

    def related(row, key):
        ref = row['relationships'][key]['data']
        if not isinstance(ref, dict) or (ref['type'],str(ref['id'])) not in inc:
            raise ValueError('Unresolved projection relationship: '+key)
        return inc[ref['type'],str(ref['id'])]

    rows = []
    for row in body['data']:
        a = row['attributes']
        if a.get('stat_type') != 'Fantasy Score':
            continue
        player, league, duration, game = [related(row,key) for key in ('new_player','league','duration','game')]
        if league['attributes']['name'] != 'NFL':
            continue
        line = a.get('line_score')
        if isinstance(line,bool) or not isinstance(line,(int,float)) or not math.isfinite(line):
            raise ValueError('Invalid fantasy-score threshold')
        if a.get('updated_at') and timestamp(a['updated_at']) > cutoff:
            raise ValueError('Source update is later than evaluation cutoff')
        pa = player['attributes']
        if (a.get('game_id') and game['attributes'].get('external_game_id')
                and a['game_id'] != game['attributes']['external_game_id']):
            raise ValueError('Projection and related game identities disagree')
        timing_difference = timestamp(a['start_time'])-timestamp(game['attributes']['start_time'])
        rows.append({
            'source_id':'prizepicks_partner_nfl_fantasy_score',
            'source_url':receipt['source_url'],'source_sha256':receipt['sha256'],
            'observed_at':receipt['observed_at'],'available_at':receipt['available_at'],
            'projection_id':str(row['id']),'source_player_id':str(player['id']),
            'source_player_ppid':pa.get('ppid'),'player_name':pa['name'],
            'position':pa.get('position'),'team':pa.get('team'),
            'source_game_id':str(game['id']),'external_game_id':a.get('game_id'),
            'start_time':a['start_time'],'game_start_time':game['attributes']['start_time'],
            'projection_minus_game_start_seconds':timing_difference,
            'timing_warning':('Source projection/game times differ; not verified as a lock time.' if timing_difference else None),
            'duration':duration['attributes']['name'],
            'status':a.get('status'),'is_live':a.get('is_live'),'in_game':a.get('in_game'),
            'source_updated_at':a.get('updated_at'),'board_time':a.get('board_time'),
            'threshold':line,'stat_type':a['stat_type'],'odds_type':a.get('odds_type'),
            'allowed_wager_types':a.get('allowed_wager_types'),
            'is_promo':a.get('is_promo'),'adjusted_odds':a.get('adjusted_odds'),
            'offensive_position_eligible':pa.get('position') in ('QB','RB','WR','TE'),
            'price_over':None,'price_under':None,'fair_probability_over':None,
            'mean':None,'median':None,'variance':None,'confidence_interval':None,
            'interpretation':'Offered threshold, not an identified mean, median, or fair-probability quantile. Alternate lines are not independent forecasts.',
            'scoring_rules_url':RULES_URL,
            'league_directly_interchangeable':False,
            'scoring_difference':'PrizePicks lost fumble -1; compare this to the configured league lost-fumble penalty. DNP/reboot settlement differs from realized fantasy output.',
            'raw_projection':copy.deepcopy(row),'raw_player':copy.deepcopy(player),
            'raw_game':copy.deepcopy(game),'raw_duration':copy.deepcopy(duration)})
    if not rows:
        raise ValueError('No NFL Fantasy Score rows; empty board is not a zero forecast')
    name_ids=defaultdict(set)
    for row in rows:
        name_ids[(row['player_name'],row['position'],row['team'])].add(row['source_player_id'])
    for row in rows:
        warnings=[]
        if row['source_player_ppid'] and not row['source_player_ppid'].startswith('NFL_player_'):
            warnings.append('NFL quote carries a non-NFL source player PPID')
        if len(name_ids[(row['player_name'],row['position'],row['team'])])>1:
            warnings.append('Same observed name/position/team maps to multiple source player identities')
        row['identity_warnings']=warnings
        row['requires_identity_review']=bool(warnings)
    counts = Counter(r['odds_type'] for r in rows)
    return {'schema_version':1,'built_at':time.time(),'information_cutoff':cutoff,
        'source_receipt':receipt,'quote_count':len(rows),'unique_source_players':len({r['source_player_id'] for r in rows}),
        'unique_offensive_source_players':len({r['source_player_id'] for r in rows if r['offensive_position_eligible']}),
        'distinct_observed_name_position_team':len(name_ids),
        'source_player_identities_requiring_review':len({r['source_player_id'] for r in rows if r['requires_identity_review']}),
        'odds_type_counts':dict(counts),'quote_dates':[min(r['start_time'] for r in rows),max(r['start_time'] for r in rows)],
        'distinct_games':len({r['source_game_id'] for r in rows}),
        'projection_game_time_disagreements':sum(bool(r['timing_warning']) for r in rows),'rows':rows,
        'automatic_refresh_enabled':False,
        'access_rights':'Public unauthenticated response acquired. PrizePicks terms restrict automated monitoring/copying; no API licence or redistribution rights established.',
        'source_dependence':'The acquired GitHub mirror republishes PrizePicks; it is not an independent forecast.',
        'projection_adjustment':None,'championship_probability_adjustment':None}


def build(data_dir='.moneyball'):
    base=Path(data_dir)/'research'/'dossier400-fantasy-lines'
    receipt=json.loads((base/'prizepicks-partner.receipt.json').read_text())
    raw=(base/'prizepicks-partner.raw').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=receipt['sha256']:
        raise ValueError('Raw quote hash mismatch')
    result=normalize_prizepicks(json.loads(raw),receipt,cutoff=time.time())
    result['normalizer_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (base/'prizepicks-fantasy-score.json').write_text(canonical(result)+'\n')
    return {k:v for k,v in result.items() if k!='rows'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-dir',default='.moneyball')
    print(canonical(build(parser.parse_args().data_dir)))


if __name__=='__main__':main()
