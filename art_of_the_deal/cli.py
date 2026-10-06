"""Inspectable JSON CLI sharing the MCP's exact application operations."""
import argparse
import json
from pathlib import Path
import sys
from .service import Service
from .sources import registry
from .store import DataError

def parser():
    p=argparse.ArgumentParser(description="Moneyball: connected fantasy league evidence and strategy")
    p.add_argument("--home",help="Private data directory, defaults to ART_OF_DEAL_HOME")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("leagues")
    c=sub.add_parser("discover", help="Discover Sleeper leagues by public username")
    c.add_argument("username"); c.add_argument("season", type=int)
    c=sub.add_parser("select-team"); c.add_argument("alias"); c.add_argument("team_id")
    c=sub.add_parser("doctor", help="Safe local onboarding diagnostics"); c.add_argument("alias", nargs="?")
    sub.add_parser("demo", help="Run an isolated synthetic example without an account")
    c=sub.add_parser("draft", help="Read a connected draft board; no selections")
    c.add_argument("alias"); c.add_argument("--cached", action="store_true"); c.add_argument("--draft-id"); c.add_argument("--ids", nargs="+"); c.add_argument("--expected-board-hash"); c.add_argument("--max-age-seconds", type=int, default=20); c.add_argument("--as-of")
    c=sub.add_parser("lineup"); c.add_argument("alias"); c.add_argument("--week", type=int); c.add_argument("--cached", action="store_true"); c.add_argument("--unavailable", nargs="*"); c.add_argument("--as-of")
    sub.add_parser("pipeline-catalog")
    c=sub.add_parser("pipeline-sync"); c.add_argument("datasets", nargs="+"); c.add_argument("--seasons", nargs="+", type=int, required=True); c.add_argument("--force", action="store_true"); c.add_argument("--offline", action="store_true")
    c=sub.add_parser("pipeline-query"); c.add_argument("dataset"); c.add_argument("--seasons", nargs="+", type=int, required=True); c.add_argument("--alias"); c.add_argument("--ids", nargs="+"); c.add_argument("--source"); c.add_argument("--as-of"); c.add_argument("--limit", type=int, default=20); c.add_argument("--positions", nargs="+")
    c=sub.add_parser("skill", help="Print the packaged Moneyball strategy skill")
    c.add_argument("--reference", choices=["research-method", "trade-evaluation"])
    c=sub.add_parser("research"); c.add_argument("alias"); c.add_argument("--as-of")
    c=sub.add_parser("research-save"); c.add_argument("alias"); c.add_argument("file")
    c=sub.add_parser("connect")
    c.add_argument("alias"); c.add_argument("platform",choices=["espn","sleeper"]); c.add_argument("league_id"); c.add_argument("season",type=int)
    c.add_argument("--team"); c.add_argument("--user-id"); c.add_argument("--auth",choices=["none","environment","firefox"],default="none")
    c=sub.add_parser("context"); c.add_argument("alias"); c.add_argument("--fresh",action="store_true"); c.add_argument("--as-of")
    c=sub.add_parser("strategy"); c.add_argument("alias"); c.add_argument("--expanded",action="store_true"); c.add_argument("--as-of")
    c=sub.add_parser("view"); c.add_argument("alias"); c.add_argument("section",choices=["rules","teams","schedule","picks","transactions","completeness"]); c.add_argument("--team"); c.add_argument("--as-of")
    c=sub.add_parser("search"); c.add_argument("alias"); c.add_argument("query")
    c=sub.add_parser("evidence"); c.add_argument("alias"); c.add_argument("ids",nargs="+"); c.add_argument("--expanded",action="store_true"); c.add_argument("--as-of")
    c=sub.add_parser("forecasts"); c.add_argument("alias"); c.add_argument("--weeks",nargs="+",type=int); c.add_argument("--force",action="store_true")
    c=sub.add_parser("trade"); c.add_argument("alias"); c.add_argument("proposal",help="JSON file or - for stdin"); c.add_argument("--week",type=int); c.add_argument("--cached",action="store_true"); c.add_argument("--expanded",action="store_true"); c.add_argument("--unavailable",nargs="*"); c.add_argument("--as-of")
    c=sub.add_parser("sources"); c.add_argument("source",nargs="?")
    c=sub.add_parser("pool"); c.add_argument("alias"); c.add_argument("--positions",nargs="+",default=["QB","RB","WR","TE"]); c.add_argument("--limit",type=int,default=10); c.add_argument("--fresh",action="store_true"); c.add_argument("--query")
    c=sub.add_parser("pickup"); c.add_argument("alias"); c.add_argument("player_id"); c.add_argument("--drop"); c.add_argument("--week",type=int); c.add_argument("--cached",action="store_true"); c.add_argument("--expanded",action="store_true"); c.add_argument("--unavailable",nargs="*"); c.add_argument("--as-of")
    c=sub.add_parser("claims"); c.add_argument("alias"); c.add_argument("file",help="JSON array of ordered alternatives: file path or - for stdin"); c.add_argument("--week",type=int); c.add_argument("--cached",action="store_true"); c.add_argument("--expanded",action="store_true"); c.add_argument("--as-of")
    c=sub.add_parser("markets"); c.add_argument("series",nargs="?"); c.add_argument("--limit",type=int,default=10)
    c=sub.add_parser("import-forecasts"); c.add_argument("alias"); c.add_argument("source"); c.add_argument("file")
    c=sub.add_parser("import-dossiers"); c.add_argument("deck")
    c=sub.add_parser("events"); c.add_argument("--limit",type=int,default=10)
    return p

def load(path):
    return json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text())

def main(argv=None):
    a=parser().parse_args(argv)
    if a.command=="skill":
        from .league_research import skill_text
        print(skill_text(a.reference))
        return
    if a.command=="demo":
        from .demo import run
        print(json.dumps(run(), ensure_ascii=False, allow_nan=False))
        return
    s=Service(a.home)
    try:
        if a.command=="leagues": out=s.leagues()
        elif a.command=="discover": out=s.discover_sleeper(a.username,a.season)
        elif a.command=="select-team": out=s.select_team(a.alias,a.team_id)
        elif a.command=="doctor": out=s.onboarding_status(a.alias)
        elif a.command=="draft":
            from .draft import context
            out=context(s,a.alias,fresh=not a.cached and a.as_of is None,draft_id=a.draft_id,player_ids=a.ids,expected_board_hash=a.expected_board_hash,max_age_seconds=a.max_age_seconds,as_of=a.as_of)
        elif a.command=="lineup":
            from .lineups import packet
            out=packet(s,a.alias,evaluation_week=a.week,fresh=not a.cached and a.as_of is None,unavailable=a.unavailable,as_of=a.as_of)
        elif a.command=="pipeline-catalog":
            from .pipelines import catalog
            out=catalog(s)
        elif a.command=="pipeline-sync":
            from .pipelines import sync
            out=sync(s,a.datasets,a.seasons,force=a.force,offline=a.offline)
        elif a.command=="pipeline-query":
            from .pipelines import query
            out=query(s,a.dataset,seasons=a.seasons,alias=a.alias,player_ids=a.ids,source=a.source,as_of=a.as_of,limit=a.limit,positions=a.positions)
        elif a.command=="research": out=s.research_plan(a.alias,a.as_of)
        elif a.command=="research-save": out=s.save_research(a.alias,load(a.file))
        elif a.command=="connect": out=s.connect(a.alias,a.platform,a.league_id,a.season,a.team,a.user_id,a.auth)
        elif a.command=="context": out=s.context(a.alias,a.fresh,a.as_of)
        elif a.command=="strategy": out=s.strategy(a.alias,a.expanded,a.as_of)
        elif a.command=="view": out=s.details(a.alias,a.section,a.team,a.as_of)
        elif a.command=="search": out=s.search(a.alias,a.query)
        elif a.command=="evidence": out=s.evidence(a.alias,a.ids,expanded=a.expanded,as_of=a.as_of)
        elif a.command=="forecasts": out=s.refresh_forecasts(a.alias,a.weeks,a.force)
        elif a.command=="sources": out=registry(a.source)
        elif a.command=="pool":
            from .operations import acquisition_context
            out=acquisition_context(s,a.alias,a.positions,a.limit,a.fresh,a.query)
        elif a.command=="pickup":
            from .operations import pickup_packet
            out=pickup_packet(s,a.alias,a.player_id,a.drop,a.week,not a.cached and a.as_of is None,a.unavailable,a.expanded,a.as_of)
        elif a.command=="claims":
            from .operations import claim_scenarios
            out=claim_scenarios(s,a.alias,load(a.file),a.week,not a.cached and a.as_of is None,expanded=a.expanded,as_of=a.as_of)
        elif a.command=="markets":
            from .markets import market_sources,fetch_kalshi
            from .http import Client
            if a.series:
                result=fetch_kalshi(Client(s.store),a.series,a.limit)
                s.store.put("market_snapshot",a.series,result)
                out={k:v for k,v in result.items() if k!="raw"}
            else: out=market_sources()
        elif a.command=="import-forecasts": out=s.import_forecasts(a.alias,a.source,load(a.file))
        elif a.command=="import-dossiers":
            from .legacy import import_moneyball_deck
            out=import_moneyball_deck(s,a.deck)
        elif a.command=="events": out=s.store.events(a.limit)
        elif a.command=="trade":
            proposal=load(a.proposal)
            if a.week is not None: proposal["evaluation_week"]=a.week
            out=s.trade_packet(a.alias,proposal,not a.cached,a.unavailable,a.as_of,a.expanded)
        print(json.dumps(out,ensure_ascii=False,allow_nan=False))
    except (DataError,ValueError,OSError) as exc:
        print(json.dumps({"error":str(exc),"status":"failed_no_recommendation"}),file=sys.stderr)
        raise SystemExit(2)

if __name__=="__main__": main()
