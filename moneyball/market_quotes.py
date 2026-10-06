"""Normalize observed exchange facts without conflating price and probability.

All input markets and fields were inspected in actual September 2026 responses.
Binary stat thresholds, fractional-payout contests and other events remain
different targets. No fabricated median, tail, injury probability or source CI.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .warehouse import Warehouse, canonical, leakage_check, timestamp
from .draft_universe import clean_id

STAT_PHRASES = {
    'passing yards':'pass_yd', 'passing touchdowns':'pass_td', 'passing tds':'pass_td',
    'rushing yards':'rush_yd', 'rushing touchdowns':'rush_td', 'rushing tds':'rush_td',
    'receiving yards':'rec_yd', 'receiving touchdowns':'rec_td', 'receiving tds':'rec_td',
    'receptions':'rec', 'passing interceptions':'pass_int', 'interceptions thrown':'pass_int',
    'rushing and receiving yards':'rush_rec_yd', 'rushing + receiving yards':'rush_rec_yd',
    'rushing and receiving touchdowns':'rush_rec_td', 'rushing + receiving touchdowns':'rush_rec_td',
}
STAT_PATTERN = '|'.join(re.escape(s) for s in sorted(STAT_PHRASES,key=len,reverse=True))
THRESHOLD = re.compile(r'(?:(over|at least)\s+)?([\d,]+(?:\.\d+)?)\s*(\+|or more)?\s+('+STAT_PATTERN+r')\b',re.I)


def number(value):
    if value is None or value == '':return None
    if isinstance(value,bool):raise ValueError('Boolean is not a market number')
    x=float(value)
    if not math.isfinite(x):raise ValueError('Nonfinite market number')
    return x


def name_key(value):
    return re.sub('[^a-z0-9]','',str(value).lower())


class PlayerIndex:
    """Unique spelling/punctuation match only; nicknames require explicit review."""
    def __init__(self, players, aliases=()):
        self.players={str(p['player_id']):p for p in players}
        if len(self.players)!=len(players):raise ValueError('Duplicate roster identity')
        self.names=defaultdict(set)
        self.alias_evidence=defaultdict(list)
        for p in players:self.names[name_key(p['name'])].add(str(p['player_id']))
        for alias in aliases:
            pid=str(alias['player_id'])
            if pid not in self.players or not alias.get('provenance'):
                raise ValueError('Alias needs known player and source provenance')
            key=name_key(alias['name']);self.names[key].add(pid);self.alias_evidence[key].append(alias)

    def match(self,name):
        ids=self.names.get(name_key(name),set())
        if len(ids)==1:return next(iter(ids)), 'unique_name_or_id_crosswalk_alias_ignoring_punctuation_case'
        return None, 'ambiguous_name' if ids else 'unmatched_name'


def quote_band(bid,ask):
    """Observed pre-fee BBO; boundary sentinels are not actual certainty."""
    bid,ask=number(bid),number(ask)
    if any(x is not None and not 0<=x<=1 for x in (bid,ask)):
        raise ValueError('Exchange price outside unit payout range')
    flags=[]
    if bid is not None and bid in (0,1):flags.append('boundary_bid_not_used_as_probability');bid=None
    if ask is not None and ask in (0,1):flags.append('boundary_ask_not_used_as_probability');ask=None
    if bid is None:flags.append('missing_positive_bid')
    if ask is None:flags.append('missing_interior_ask')
    crossed=bid is not None and ask is not None and bid>ask
    if crossed:flags.append('crossed_quote')
    width=ask-bid if bid is not None and ask is not None else None
    if width is not None and width>.15:flags.append('wide_spread_over_15pp_diagnostic')
    return {'yes_bid':bid,'yes_ask':ask,'spread':width,
            'midpoint':(bid+ask)/2 if width is not None and not crossed else None,
            'two_sided':width is not None and not crossed,'flags':flags,
            'interpretation':'Observed pre-fee price interval, not a confidence interval or calibrated probability. Missing side remains missing.'}


def classify(title,question,rules,source_player=None):
    text=' '.join(str(x or '') for x in (title,question,rules))
    lower=text.lower()
    explicit_2026=bool(re.search(r'2026(?:\s*[-–/]\s*(?:27|2027))?',text))
    season=2026 if explicit_2026 else None
    stat=None;threshold=None;comparison=None;minimum=None
    player=source_player
    if not player:
        patterns=[r'Will (.+?) (?:have|record|finish|win|be|average)\b',
                  r'if (.+?) (?:records|wins|finishes|averages)\b']
        for pattern in patterns:
            m=re.search(pattern,question or '',re.I) or re.search(pattern,rules or '',re.I)
            if m:
                candidate=m.group(1).strip()
                if len(candidate)<=80 and len(candidate.split())<=7:
                    player=candidate;break
    kind='other_context'
    horizon='unresolved'
    if re.search(r'week\s*15\s*(?:through|[-–])\s*(?:week\s*)?17',lower):
        horizon='nfl_weeks_15_17'
    elif re.search(r'\bweek\s*\d+\b|\bin a game\b|\bsingle.game\b',lower):
        horizon='single_week_or_game'
    elif 'regular season' in lower or 'regular-season' in lower:
        horizon='nfl_regular_season'
    if 'fantasy' in lower or 'full-ppr' in lower:
        kind='fantasy_scoring_context'
    elif re.search(r'\bmvp\b|player of the year|rookie of the year|comeback',lower):
        kind='award'
    elif re.search(r'\bleader\b|\bmost\b|top \d+|finishes? (?:in the )?top|\bfirst\b.{0,50}\breach\b',lower):
        kind='rank_or_leader'
    else:
        # An integer-valued "over 10" differs from "10+". Resolution text wins
        # over abbreviated display labels; half-integer cases coincide.
        match=THRESHOLD.search(rules or '') or THRESHOLD.search(question or '') or THRESHOLD.search(title or '')
        if match:
            modifier,value,plus,phrase=match.groups()
            threshold=float(value.replace(',',''));stat=STAT_PHRASES[phrase.lower()]
            comparison='>' if modifier and modifier.lower()=='over' else '>='
            minimum=math.floor(threshold)+1 if comparison=='>' else math.ceil(threshold)
            kind='player_stat_threshold' if player else 'unresolved_subject_threshold'
    return {'kind':kind,'season':season,'horizon':horizon,'player_name':player,
            'statistic':stat,'source_threshold':threshold,'comparison':comparison,
            'minimum_integer_outcome':minimum,
            'cdf_at_integer':minimum-1 if minimum is not None else None}


def payout_semantics(rules,kind,source):
    lower=rules.lower()
    fractional=bool(re.search(r'dead.heat|divided by|\$?1\s*/\s*n|1/n|rounded down',lower))
    if fractional:payout='fractional_tie_payout'
    elif kind=='player_stat_threshold':payout='binary_threshold_subject_to_rules'
    else:payout='requires_target_specific_review'
    if re.search(r'does not play.*resolve.*no',lower,re.S):participation='nonparticipation_resolves_no'
    elif re.search(r'void|refund|must play|must participate',lower):participation='explicit_action_condition_requires_review'
    else:participation='not_explicit_in_acquired_rule_text'
    return {'payout_type':payout,'participation':participation,
        'cancel_50_50':'50-50' in lower or '50/50' in lower,
        'source_scoring_reference':('Sleeper PPR; equivalence to league scoring NOT established' if 'sleeper' in lower else
                                    'ESPN PPR; equivalence to league scoring NOT established' if 'espn' in lower and 'fantasy' in lower else None),
        'fee_adjustment_applied':False,
        'interpretation':'Rank/award quote may price fractional settlement rather than event probability. Rule conditions remain attached.'}


def normalize_record(source,market,receipt,index,*,event=None,series=None,as_of=None):
    observed=timestamp(receipt['observed_at']);cutoff=observed if as_of is None else timestamp(as_of)
    if observed>cutoff:raise ValueError('Market response was observed after cutoff')
    if receipt.get('source_id')!=source:raise ValueError('Raw receipt source mismatch')
    if not receipt.get('sha256') or not receipt.get('url'):raise ValueError('Missing raw provenance')
    event=event or {};series=series or {}
    if source=='kalshi_public':
        mid=market['ticker'];title=market.get('title','');question=title
        rules='\n'.join(str(market.get(k) or '') for k in ('rules_primary','rules_secondary','early_close_condition'))
        subject=market.get('yes_sub_title')
        # Some subtitle fields contain a whole proposition rather than a name.
        if subject and (':' in subject or re.search(r'\d|\+',subject)):subject=None
        band=quote_band(market.get('yes_bid_dollars'),market.get('yes_ask_dollars'))
        volume=number(market.get('volume_fp'));liquidity=number(market.get('liquidity_dollars'))
        # Do not invent a human-facing route from a ticker; the acquired API
        # query is a verified source URL and includes this exact contract.
        url=receipt['url']
        event_id=market.get('event_ticker');updated=market.get('updated_time')
        sizes={k:number(market.get(k)) for k in ('yes_bid_size_fp','yes_ask_size_fp')}
    elif source=='polymarket_global_public':
        mid=str(market['id']);title=event.get('title','');question=market.get('question','')
        rules=market.get('description') or '';subject=None
        band=quote_band(market.get('bestBid'),market.get('bestAsk'))
        volume=number(market.get('volume'));liquidity=number(market.get('liquidity'))
        url='https://polymarket.com/event/'+event['slug']+'/'+market['slug']
        event_id=str(event['id']);updated=market.get('updatedAt');sizes={}
    elif source=='polymarket_us_public':
        mid=str(market['id']);title=event.get('title','');question=market.get('title') or market.get('question','')
        rules=market.get('description') or '';subject=(market.get('metadata') or {}).get('playerName')
        bid=market.get('bestBidQuote') or {};ask=market.get('bestAskQuote') or {}
        if any(q.get('currency') not in (None,'USD') for q in (bid,ask)):raise ValueError('Non-USD price in US API')
        band=quote_band(bid.get('value'),ask.get('value'))
        volume=number(market.get('volume'));liquidity=number(market.get('liquidity'))
        url='https://polymarket.us/event/'+event['slug'];event_id=str(event['id'])
        updated=market.get('updatedAt');sizes={}
    else:raise ValueError('Unsupported exchange source')
    target=classify(title,question,rules,subject)
    pid,status=index.match(target['player_name']) if target['player_name'] else (None,'no_resolved_player_subject')
    if updated and timestamp(updated)>observed+1:band['flags'].append('provider_update_later_than_observation')
    if volume is None:band['flags'].append('trade_volume_unavailable')
    elif volume==0:band['flags'].append('no_reported_traded_volume')
    if target['season'] is None:band['flags'].append('season_not_established_from_text')
    settlement=payout_semantics(rules,target['kind'],source)
    eligible=(target['kind']=='player_stat_threshold' and target['season']==2026 and
              target['horizon']=='nfl_regular_season' and pid is not None and band['two_sided'] and
              settlement['payout_type']=='binary_threshold_subject_to_rules' and
              'provider_update_later_than_observation' not in band['flags'])
    row={'schema_version':1,'source_id':source,'market_id':mid,'event_id':event_id,
         'event_title':title,'question':question,'series_ticker':series.get('ticker'),
         'market_url':url,'api_url':receipt['url'],'market_slug':market.get('slug'),
         'raw_id':receipt['id'],'raw_sha256':receipt['sha256'],
         'observed_at':observed,'available_at':observed,'provider_updated_at':updated,
         'quote_timestamp':None,'quote_timestamp_note':'Snapshot observation time known; metadata update is not assumed to be a price tick timestamp.',
         'player_id':pid,'identity_match':status,'target':target,'quote':band,'settlement':settlement,
         'rules':rules,'volume':volume,'liquidity_reported':liquidity,'quote_sizes':sizes,
         'threshold_analysis_eligible':eligible,
         'eligibility_interpretation':'Eligible to inspect threshold sensitivity only; not a licence, reliability endorsement or calibrated probability.'}
    return row


def normalize_run(run,index,*,as_of=None):
    output=[];seen=set()
    for platform, records in run['results'].items():
        for record in records:
            source=record['source_id'];event=record.get('event')
            markets=[record['market']] if platform=='kalshi' else event.get('markets',[])
            for market in markets:
                key=(source,str(market.get('ticker') or market.get('id')))
                if key in seen:raise ValueError('Duplicate market identity in acquisition run')
                seen.add(key)
                output.append(normalize_record(source,market,record['receipt'],index,
                    event=event,series=record.get('series'),as_of=as_of))
    return output


def id_aliases(players, crosswalk, rosters, *, as_of):
    """Generate only observed full-name aliases linked by an exact GSIS ID."""
    leakage_check(list(crosswalk)+list(rosters),as_of,raise_on_error=True)
    ids={}
    for p in players:
        gid=clean_id(p.get('gsis_id'))
        if not gid:continue
        if gid in ids:raise ValueError('Ambiguous GSIS ID among candidate players')
        ids[gid]=str(p['player_id'])
    result=[];seen=set()
    for r in list(crosswalk)+list(rosters):
        gid=clean_id(r.get('gsis_id') or r.get('player_id'))
        if gid not in ids or not r.get('identity_verified'):continue
        pid=ids[gid]
        if r.get('sleeper_id') and str(r['sleeper_id'])!=pid:
            raise ValueError('GSIS and Sleeper source identities disagree')
        names=[r.get('full_name'),r.get('display_name')]
        for first in ('first_name','football_name','common_first_name'):
            if r.get(first) and r.get('last_name'):
                names.append(r[first]+' '+r['last_name'])
        for name in names:
            if not name or (pid,name) in seen:continue
            seen.add((pid,name))
            result.append({'player_id':pid,'gsis_id':gid,'name':name,'provenance':r['_provenance'],
                           'method':'Observed full-name fields joined by exact verified GSIS ID; no inferred nickname or suffix.'})
    return result


def build(run_path,*,data_root='.moneyball'):
    data_root=Path(data_root);run_path=Path(run_path)
    run=json.loads(run_path.read_text())
    cohort=json.loads((data_root/'draft'/'top400-cohort.json').read_text())
    universe=json.loads((data_root/'draft'/'player-universe.json').read_text())
    players=universe['players']
    w=Warehouse(data_root/'lab');cutoff=run['completed_at']
    nfl_players=w.query('players',cutoff=cutoff)
    nfl_rosters=w.query('rosters',cutoff=cutoff,seasons=[2026])
    player_gsis={clean_id(p.get('gsis_id')):p for p in players if p.get('gsis_id')}
    quarantine=[];verified_rosters=[]
    for r in nfl_rosters:
        p=player_gsis.get(clean_id(r.get('gsis_id') or r.get('player_id')))
        if p and r.get('sleeper_id') and clean_id(r['sleeper_id'])!=p['player_id']:
            quarantine.append({'reason':'Source roster Sleeper ID conflicts with independently reconciled name/DOB/position identity; excluded from aliases.',
                'candidate_id':p['player_id'],'name':p['name'],'source_row':r})
        else:verified_rosters.append(r)
    aliases=id_aliases(players,nfl_players,verified_rosters,as_of=cutoff)
    for p in players:
        if timestamp(p['metadata_observed_at'])>cutoff or timestamp(p['adp_as_of'])>cutoff:
            raise ValueError('Draft universe inputs were first observed after market cutoff')
        for name in p.get('observed_name_aliases',[]):
            if p.get('identity_provenance'):
                if timestamp(p['identity_provenance']['available_at'])>cutoff:
                    raise ValueError('Identity alias observed after cutoff')
                aliases.append({'player_id':p['player_id'],'name':name,'provenance':p['identity_provenance']})
    index=PlayerIndex(players,aliases);rows=normalize_run(run,index,as_of=cutoff)
    directory=data_root/'markets'/'normalized';directory.mkdir(parents=True,exist_ok=True)
    (directory/(run_path.stem+'-identity-quarantine.json')).write_text(canonical(quarantine)+'\n')
    (directory/(run_path.stem+'-identity-aliases.json')).write_text(canonical(aliases)+'\n')
    path=directory/(run_path.stem+'-quotes.jsonl')
    with path.open('w') as f:
        for row in rows:f.write(canonical(row)+'\n')
    ids={p['player_id'] for p in cohort['players']};matched=defaultdict(list)
    for row in rows:
        if row['player_id'] in ids:matched[row['player_id']].append(row)
    coverage=[]
    for p in cohort['players']:
        ms=matched[p['player_id']]
        coverage.append({**p,'market_records':len(ms),'sources':dict(Counter(m['source_id'] for m in ms)),
            'kinds':dict(Counter(m['target']['kind'] for m in ms)),
            'usable_thresholds':sum(m['threshold_analysis_eligible'] for m in ms),
            'threshold_stats':dict(Counter(m['target']['statistic'] for m in ms if m['threshold_analysis_eligible'])),
            'market_distribution_complete':False})
    summary={'built_at':time.time(),'run_path':str(run_path),'run_sha256':hashlib.sha256(run_path.read_bytes()).hexdigest(),
        'quote_path':str(path),'records':len(rows),'sources':dict(Counter(m['source_id'] for m in rows)),
        'cohort_sha256':hashlib.sha256((data_root/'draft'/'top400-cohort.json').read_bytes()).hexdigest(),
        'kinds':dict(Counter(m['target']['kind'] for m in rows)),
        'eligible_thresholds':sum(m['threshold_analysis_eligible'] for m in rows),
        'eligible_players':len({m['player_id'] for m in rows if m['threshold_analysis_eligible']}),
        'quarantined_identity_rows':len(quarantine),
        'top400_any_market':sum(p['market_records']>0 for p in coverage),
        'top400_threshold_coverage':sum(p['usable_thresholds']>0 for p in coverage),
        'unmatched_subjects':dict(Counter(m['target']['player_name'] for m in rows if m['target']['player_name'] and not m['player_id'])),
        'flags':dict(Counter(flag for m in rows for flag in m['quote']['flags'])),
        'top400':coverage,
        'interpretation':'Coverage and raw-price normalization only. No complete player distributions or final dossiers yet.'}
    summary_path=directory/(run_path.stem+'-coverage.json');summary_path.write_text(canonical(summary)+'\n')
    return {k:v for k,v in summary.items() if k not in ('top400','unmatched_subjects')}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);p.add_argument('--data-root',default='.moneyball')
    args=p.parse_args()
    try:print(canonical(build(args.run,data_root=args.data_root)))
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:p.exit(2,canonical({'error':str(exc)})+'\n')


if __name__=='__main__':main()
