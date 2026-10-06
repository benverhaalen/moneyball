"""Audited extended public data acquisition and directly counted opportunities.

Only raw observations are imported. No EPA, model-based expected points, ADP,
scoring weights, or historical publication vintages are invented. NGS currently
requires the existing base-R interpreter because maintained CSV URLs are absent.
"""
from collections import defaultdict
import csv
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .sources import HttpFetcher, FetchError, RELEASE, LOADER, _value
from .warehouse import Warehouse, WarehouseError, ValidationError, canonical, sha256, timestamp, leakage_check


SPECS = {
    'pbp': {'path':'pbp/play_by_play_{season}.csv.gz','loader':'load_pbp.R','yearly':True,'format':'csv.gz',
        'required':('game_id','play_id','season','week'),'key':('game_id','play_id'),
        'origin':'NFL play-by-play processed by nflfastR via nflverse', 'history':'1999 onward',
        'caveat':'Full raw contains model outputs; cleaned table excludes EPA/WP/expected models and betting lines.'},
    'weekly_rosters': {'path':'weekly_rosters/roster_weekly_{season}.csv.gz','loader':'load_rosters_weekly.R','yearly':True,'format':'csv.gz',
        'required':('player_id','season','week','team'),'key':('season','week','team','player_id','game_type'),
        'origin':'nflverse weekly roster snapshots','history':'2002 onward',
        'caveat':'Membership/status is not medical availability. Missing or unresolved IDs are explicit.'},
    'depth_charts': {'path':'depth_charts/depth_charts_{season}.csv.gz','loader':'load_depth_charts.R','yearly':True,'format':'csv.gz',
        'required':('record_id','season','team'),'key':('record_id',),
        'origin':'nflverse depth charts; provider/schema changed after2024','history':'2001 onward; timestamps since2025',
        'caveat':'Source dt is a snapshot timestamp, not independently verified public publication time. Position labels are not fantasy eligibility.'},
    'injuries': {'path':'injuries/injuries_{season}.csv.gz','loader':'load_injuries.R','yearly':True,'format':'csv.gz',
        'required':('record_id','season','week','team'),'key':('record_id',),
        'origin':'nflverse collected NFL injury/practice report data','history':'2009 onward; observed2025/2026assets conflict with stale schedule documentation',
        'caveat':'Absence from injury report does not prove healthy or active. Current coverage can be only a few reporting teams.'},
    'participation': {'path':'pbp_participation/pbp_participation_{season}.csv.gz','loader':'load_participation.R','yearly':True,'format':'csv.gz',
        'required':('game_id','play_id'),'key':('game_id','play_id'),
        'origin':'NFL NextGenStats via nflverse through2022; FTN Data via nflverse since2023',
        'history':'2016 onward; FTN season supplied only after postseason completes',
        'license':'CC-BY-SA4.0 explicitly stated in loader; attribution NFL NextGenStats via nflverse or FTN Data via nflverse',
        'caveat':'2023+ full-season participation is unavailable during that same season. No historical feature backdating.'},
    'ftn_charting': {'path':'ftn_charting/ftn_charting_{season}.csv','loader':'load_ftn_charting.R','yearly':True,'format':'csv',
        'required':('ftn_play_id','season','week'),'key':('ftn_play_id',),
        'origin':'FTN Data via nflverse; published subset of manual play charting',
        'history':'2022 onward; loader describes charting within 48 hours of each game',
        'license':'CC-BY-SA4.0 explicitly stated in loader; attribution FTN Data via nflverse',
        'caveat':'Subjective charting labels are measured judgments, not verified physical truth. date_pulled is not public availability evidence.'},
}
for _kind in ('passing','receiving','rushing'):
    SPECS['ngs_'+_kind] = {'path':'nextgen_stats/ngs_'+_kind+'.rds','loader':'load_nextgen_stats.R',
        'yearly':False,'format':'rds','required':('player_id','season','week'),'key':('season','season_type','week','player_id'),
        'origin':'NFL Next Gen Stats via nflverse','history':'2016 onward; actual coverage measured during ingestion',
        'caveat':'Minimum attempt thresholds cause nonrandom coverage. week0 is a season summary, never a weekly observation.'}

# Raw-only columns sufficient for observed opportunity and scoring event work.
# Full source remains in immutable raw bytes for later questions.
PBP_COLUMNS = tuple(('play_id game_id season season_type week game_date home_team away_team posteam defteam '
    'yardline_100 down ydstogo qtr game_seconds_remaining score_differential drive play_type play_deleted '
    'qb_dropback qb_kneel qb_spike qb_scramble pass_attempt rush_attempt sack complete_pass incomplete_pass '
    'interception touchdown pass_touchdown rush_touchdown return_touchdown two_point_attempt two_point_conv_result '
    'passer_player_id receiver_player_id rusher_player_id td_player_id passing_yards receiving_yards rushing_yards '
    'air_yards yards_after_catch yards_gained penalty fumble_lost fumbled_1_player_id fumbled_2_player_id '
    'fumble_recovery_1_player_id fumble_recovery_1_team fumble_recovery_2_player_id fumble_recovery_2_team '
    'punt_returner_player_id kickoff_returner_player_id return_yards lateral_receiver_player_id lateral_rusher_player_id '
    'lateral_receiving_yards lateral_rushing_yards special_teams_play').split())


def register_advanced(warehouse):
    cadences={'pbp':'nightly after games, according to nflverse availability documentation',
        'weekly_rosters':'daily at 07:00 UTC, according to nflverse availability documentation',
        'participation':'2023 onward: full season published only after postseason completes',
        'ftn_charting':'loader describes charting within 48 hours after games',
        'injuries':'mirror cadence not verified; can lag official team reports',
        'depth_charts':'timestamped snapshots; actual dt coverage recorded, update cadence not asserted'}
    for dataset, spec in SPECS.items():
        warehouse.register_source('nflverse_'+dataset, {
            'dataset':dataset,'origin':spec['origin'],'url_template':RELEASE+spec['path'],
            'access':'free public GitHub release; no credentials or paid subscription',
            'cadence':cadences.get(dataset,'source-dependent; actual snapshot and release coverage recorded'),
            'history':spec['history'],'quality':[spec['caveat'],'mutable files; historical publication vintage unknown'],
            'license_status':spec.get('license','nflverse-data LICENSE.md declares CC BY4.0; upstream third-party rights not separately audited'),
            'terms_urls':['https://raw.githubusercontent.com/nflverse/nflverse-data/main/LICENSE.md',LOADER+spec['loader']],
            'verification_status':'actual loader source and release asset inventory read2026-09-07',
            'verification_url':LOADER+spec['loader'],
            'historical_pit_status':'available_at is first observed retrieval unless independently verified archive evidence supplied',
            'transform_version':'advanced-csv-v1'})
    warehouse.register_source('nfl_official_injuries_manual',{
        'dataset':'official_injury_reports','origin':'NFL and official club report pages',
        'access':'manual corroboration only; no automated recurring collection enabled',
        'verification_status':'official league and Patriots pages read 2026-09-07',
        'verification_urls':['https://www.nfl.com/injuries/','https://www.patriots.com/team/injury-report/'],
        'license_status':'NFL.com terms section1.3 prohibits systematic retrieval without prior written consent',
        'terms_urls':['https://www.nfl.com/legal/terms/'],
        'cadence':'practice-day updates; pages can disagree or lag one another',
        'history':'not acquired as structured data',
        'quality':['No absence-to-healthy inference','nflverse injury mirror lags some observed official club changes'],
        'historical_pit_status':'not acquired; page timestamps are not an immutable historical archive'})


def _csv_records(body, file_format, *, columns=None):
    """Stream decompression and row conversion to avoid retaining all PBP fields."""
    if file_format == 'rds':
        interpreter = shutil.which('Rscript')
        if not interpreter:
            raise FetchError('Maintained NGS source is RDS; base Rscript interpreter unavailable')
        with tempfile.TemporaryDirectory(prefix='moneyball-rds-') as directory:
            src, dst = Path(directory)/'source.rds', Path(directory)/'source.csv'
            src.write_bytes(body)
            expression = 'a <- commandArgs(TRUE); x <- readRDS(a[1]); stopifnot(is.data.frame(x)); write.csv(x, a[2], row.names=FALSE, na="")'
            result = subprocess.run([interpreter,'--vanilla','-e',expression,str(src),str(dst)],capture_output=True,timeout=60)
            if result.returncode:
                raise ValidationError('Base R failed to decode source RDS: '+result.stderr.decode(errors='replace')[:300])
            with dst.open(newline='',encoding='utf-8-sig') as f:
                yield from _read_csv(f, columns=columns)
        return
    buffer = io.BytesIO(body)
    binary = gzip.GzipFile(fileobj=buffer) if body[:2] == b'\x1f\x8b' else buffer
    with io.TextIOWrapper(binary,encoding='utf-8-sig',newline='') as f:
        yield from _read_csv(f,columns=columns)


def _read_csv(f, *, columns=None):
    reader=csv.DictReader(f)
    if not reader.fieldnames or len(reader.fieldnames)<2 or len(set(reader.fieldnames))!=len(reader.fieldnames):
        raise ValidationError('CSV missing/duplicated header')
    chosen = set(columns) if columns else None
    for index,row in enumerate(reader):
        if None in row or any(v is None for v in row.values()):
            raise ValidationError(f'CSV record{index+2} differs from schema')
        yield {k:_value(k,v) for k,v in row.items() if chosen is None or k in chosen}


def _normalize(dataset, records, season=None):
    output, seen, duplicates = [], set(), 0
    for original in records:
        row=dict(original)
        if dataset in ('ftn_charting','participation'):
            for key,value in row.items():
                if value in ('TRUE','FALSE'):
                    row[key]=value=='TRUE'
        if dataset=='weekly_rosters':
            # normalize() accepts already converted values only for original CSV
            # strings, so preserve IDs explicitly here instead.
            row['player_id']=row.get('gsis_id')
            row['identity_verified']=bool(row['player_id'])
            if not row['player_id']:
                row['player_id']='unresolved:'+sha256(canonical([row.get(k) for k in ('season','week','team','full_name','esb_id','espn_id')]).encode())[:24]
            row['observation_type']='reported_weekly_roster_membership'
        elif dataset=='depth_charts':
            row['season']=row.get('season') or season
            row['team']=row.get('team') or row.get('club_code')
            row['player_id']=row.get('gsis_id')
            row['identity_verified']=bool(row['player_id'])
            row['position']=row.get('position') or row.get('pos_abb')
            row['snapshot_at']=row.get('dt')
            if row.get('dt'): row['event_at']=row['dt']
            row['record_id']=sha256(canonical(original).encode())
        elif dataset=='injuries':
            row['player_id']=row.get('gsis_id')
            row['identity_verified']=bool(row['player_id'])
            row['record_id']=sha256(canonical(original).encode())
            row['observation_type']='reported_injury_or_practice_status_not_complete_availability'
        elif dataset.startswith('ngs_'):
            row['player_id']=row.get('player_gsis_id') or row.get('gsis_id')
            row['position']=row.get('player_position')
            row['team']=row.get('team_abbr')
            row['stat_type']=dataset.removeprefix('ngs_')
            row['observation_type']='season_summary' if row.get('week')==0 else 'thresholded_weekly_tracking_summary'
        elif dataset=='participation':
            row['game_id']=row.get('nflverse_game_id') or row.get('game_id')
            row['season']=season
        elif dataset=='ftn_charting':
            row['game_id']=row.get('nflverse_game_id')
            row['play_id']=row.get('nflverse_play_id')
        elif dataset=='pbp' and row.get('game_date'):
            row['event_at']=str(row['game_date'])+'T00:00:00Z'
            row['event_time_precision']='calendar_day_not_kickoff'
        digest=sha256(canonical(row).encode())
        if digest in seen:
            duplicates+=1
            continue
        seen.add(digest);output.append(row)
    return output,duplicates


def ingest_advanced(warehouse=None, *, datasets=('weekly_rosters','depth_charts','injuries','ngs','pbp'),
                    seasons=(2024,2025,2026), force=False, offline=False, fetcher=None, ttl=86400):
    warehouse=warehouse or Warehouse();register_advanced(warehouse)
    fetcher=fetcher or HttpFetcher(warehouse)
    requested=sorted(set(int(y) for y in seasons))
    expanded=[]
    for name in datasets:
        expanded.extend(['ngs_passing','ngs_receiving','ngs_rushing'] if name=='ngs' else [name])
    receipts,failures=[],[]
    for dataset in dict.fromkeys(expanded):
        if dataset not in SPECS: raise ValueError('Unknown advanced dataset: '+dataset)
        spec=SPECS[dataset]
        for year in requested if spec['yearly'] else [None]:
            source_id='nflverse_'+dataset;url=RELEASE+spec['path'].format(season=year)
            partition=str(year) if year else 'all';raw=None
            try:
                try:
                    raw=fetcher.fetch(source_id,url,force=force,offline=offline,ttl=ttl)
                except FetchError as exc:
                    if url.endswith('.csv.gz') and ('HTTP 404:' in str(exc) or offline):
                        # CSV fallback is directly supported by the inspected loader.
                        url=url[:-3]
                        raw=fetcher.fetch(source_id,url,force=force,offline=offline,ttl=ttl)
                    else:
                        raise
                generator=_csv_records(warehouse.raw_bytes(raw['id']),spec['format'],columns=PBP_COLUMNS if dataset=='pbp' else None)
                rows,duplicates=_normalize(dataset,generator,year)
                ranges={'season':(1920,2200)}
                if dataset not in ('depth_charts','participation'):
                    ranges['week']=(0 if dataset.startswith('ngs_') else 1,30)
                batch=warehouse.publish(source_id,dataset,rows,raw_id=raw['id'],key_fields=spec['key'],
                    partition=partition,required_fields=spec['required'],event_field='event_at',ranges=ranges)
                coverage={'seasons':sorted(set(r.get('season') for r in rows if r.get('season') is not None)),
                    'weeks':sorted(set(r.get('week') for r in rows if r.get('week') is not None)),
                    'teams':sorted(set(str(r.get('team') or r.get('posteam')) for r in rows if r.get('team') or r.get('posteam'))),
                    'unique_players':len(set(r.get('player_id') for r in rows if r.get('player_id'))),
                    'unresolved_identities':sum(r.get('identity_verified') is False for r in rows),
                    'exact_duplicate_source_rows_collapsed':duplicates}
                receipts.append({**batch,'url':url,'raw_sha256':raw['sha256'],'raw_bytes':raw['byte_count'],
                    'cache_hit':raw['cache_hit'],'coverage':coverage,'source_caveat':spec['caveat']})
            except (WarehouseError,ValueError,UnicodeError,OSError,EOFError,subprocess.TimeoutExpired) as exc:
                failure={'dataset':dataset,'partition':partition,'url':url,'raw_id':raw['id'] if raw else None,'error':str(exc)}
                failures.append(failure)
                if raw and not str(exc).startswith('Batch '): warehouse.quarantine(source_id,dataset,raw_id=raw['id'],reason=str(exc),partition=partition)
                warehouse.alert('advanced_ingestion_failed',failure,severity='error')
    return {'ok':not failures,'partial':bool(failures and receipts),'checked_at':time.time(),'receipts':receipts,'failures':failures,
        'historical_pit_verified':False,'note':'Current mutable historical sources are outcomes/exploration before observed availability.'}


def derive_opportunities(plays):
    """Count measured opportunity, no fitted touchdown/yardage or xFP weights.

    No-play/deleted plays and two-point attempts are excluded. Red zone means
    pre-snap yardline_100 in [0,20]. Target shares divide by attributed targets
    plus explicitly counted unattributed eligible passing attempts; denominator
    is therefore stated and auditable rather than a hidden player-directory sum.
    """
    people={};team=defaultdict(lambda:defaultdict(float));batches=set()
    def player(pid,r):
        key=(str(pid),r['season'],r['week'],r['game_id'],r.get('posteam'))
        if key not in people:
            people[key]={'player_id':str(pid),'season':r['season'],'week':r['week'],'game_id':r['game_id'],
                'team':r.get('posteam'),'targets':0,'rush_attempts_excluding_kneels':0,'kneels':0,'dropbacks':0,
                'red_zone_targets':0,'red_zone_rush_attempts':0,'air_yards_sum':0.0,'air_yards_observed_targets':0}
        return people[key]
    for r in plays:
        if r.get('_provenance',{}).get('batch_id'):batches.add(r['_provenance']['batch_id'])
        if r.get('play_deleted')==1 or r.get('play_type')=='no_play' or r.get('two_point_attempt')==1:continue
        if not r.get('posteam'):continue
        group=(r['season'],r['week'],r['game_id'],r['posteam']);tot=team[group]
        red=isinstance(r.get('yardline_100'),(int,float)) and 0<=r['yardline_100']<=20
        passer,receiver,rusher=r.get('passer_player_id'),r.get('receiver_player_id'),r.get('rusher_player_id')
        if r.get('qb_dropback')==1:
            tot['dropbacks']+=1
            if passer:player(passer,r)['dropbacks']+=1
        if r.get('pass_attempt')==1 and r.get('sack')!=1 and r.get('qb_spike')!=1:
            # Throwaways lack receiver IDs and remain visible in denominator.
            tot['eligible_pass_attempts']+=1
            if receiver:
                p=player(receiver,r);p['targets']+=1;p['red_zone_targets']+=int(red);tot['attributed_targets']+=1
                if isinstance(r.get('air_yards'),(int,float)):
                    p['air_yards_sum']+=r['air_yards'];p['air_yards_observed_targets']+=1
            else:tot['unattributed_pass_attempts']+=1
        if r.get('rush_attempt')==1:
            if r.get('qb_kneel')==1:
                if rusher:player(rusher,r)['kneels']+=1
            else:
                tot['rush_attempts_excluding_kneels']+=1
                if rusher:
                    p=player(rusher,r);p['rush_attempts_excluding_kneels']+=1;p['red_zone_rush_attempts']+=int(red)
    out=[]
    for key,p in sorted(people.items(),key=lambda kv:str(kv[0])):
        totals=team[(p['season'],p['week'],p['game_id'],p['team'])]
        denom=totals.get('eligible_pass_attempts',0)
        p['team_eligible_pass_attempts']=int(denom)
        p['team_unattributed_pass_attempts']=int(totals.get('unattributed_pass_attempts',0))
        p['target_share_of_eligible_pass_attempts']=p['targets']/denom if denom else None
        rushes=totals.get('rush_attempts_excluding_kneels',0)
        p['rush_share_excluding_kneels']=p['rush_attempts_excluding_kneels']/rushes if rushes else None
        p['red_zone_definition']='pre-snap yardline_100 between0and20 inclusive'
        out.append(p)
    return {'rows':out,'input_batches':sorted(batches),'method':'direct event counting v1; no expected-score weights',
            'limits':['not an injury/availability model','targets may differ from official scoring on unusual plays',
                      'share denominator includes throwaways; not identical to attributed-target share',
                      'no-play/deleted/two-point events excluded; kneels separated','historical source-vintage restriction retained in lineage']}


def materialize_opportunities(warehouse=None, *, seasons, cutoff=None, purpose='exploratory'):
    """Persist one small directly counted artifact per season, with batch lineage."""
    warehouse=warehouse or Warehouse()
    if purpose=='features' and cutoff is None:
        raise ValidationError('Feature opportunities require an explicit information cutoff')
    receipts=[]
    for season in sorted(set(int(x) for x in seasons)):
        plays=warehouse.query('pbp',cutoff=cutoff,purpose=purpose,seasons=[season],source_id='nflverse_pbp')
        if not plays:
            receipts.append({'season':season,'status':'no_eligible_input','row_count':0})
            continue
        result=derive_opportunities(plays)
        receipt=warehouse.record_artifact('opportunities_'+str(season),result,
            input_batches=result['input_batches'],cutoff=cutoff,purpose=purpose,code_version='advanced-opportunity-v1',
            metadata={'season':season,'input_play_count':len(plays),'row_count':len(result['rows']),
                      'expected_score_weights':False})
        receipts.append({**receipt,'season':season,'input_play_count':len(plays),'row_count':len(result['rows'])})
    return {'receipts':receipts,'purpose':purpose,'cutoff':cutoff}


def latest_depth_chart(warehouse=None, *, season, cutoff=None, purpose='features'):
    """Latest observed timestamped snapshot per team; no historical dt backdating.

    Select rows in SQLite so the 2026 history does not become a half-million-row
    Python read. Teams may have different latest timestamps, shown on every row.
    Only the timestamped source schema is supported; old weekly schemas return
    no rows and should be queried explicitly via Warehouse.query instead.
    """
    if purpose not in ('features','outcomes','exploratory'):
        raise ValueError('Invalid query purpose')
    warehouse=warehouse or Warehouse();limit=timestamp(cutoff)
    candidates=[b for b in warehouse.batches('depth_charts',include_quarantined=False)
        if b['source_id']=='nflverse_depth_charts' and b['partition']==str(season) and b['available_at']<=limit]
    if not candidates:return []
    batch=max(candidates,key=lambda b:(b['available_at'],b['observed_at'],b['batch_id']))
    with warehouse.connect() as db:
        records=db.execute("""WITH dated AS (
              SELECT body,event_at,row_hash,json_extract(body,'$.team') AS team
              FROM records WHERE batch_id=? AND event_at IS NOT NULL),
            latest AS (SELECT team,MAX(event_at) AS snapshot FROM dated GROUP BY team)
            SELECT d.body,d.event_at,d.row_hash FROM dated d JOIN latest l
            ON d.team=l.team AND d.event_at=l.snapshot ORDER BY d.team,d.body""",[batch['batch_id']]).fetchall()
    provenance={k:batch[k] for k in ('source_id','dataset','batch_id','raw_id','partition','observed_at',
                 'available_at','published_at','publication_evidence','vintage_verified','content_sha256')}
    provenance.update(purpose=purpose,historical_vintage_unknown=not batch['vintage_verified'])
    rows=[]
    for r in records:
        if sha256(r['body'].encode())!=r['row_hash']:
            raise WarehouseError('Cleaned record hash mismatch in depth chart batch '+str(batch['batch_id']))
        row=json.loads(r['body']);row['_provenance']={**provenance,'event_at':r['event_at']};rows.append(row)
    leakage_check(rows,limit,raise_on_error=True)
    return rows
