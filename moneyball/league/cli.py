"""Inspectable JSON CLI sharing the MCP's exact application operations."""
import argparse
import json
from pathlib import Path
import sys
from .service import Service
from .sources import registry
from .store import DataError

def parser():
    p=argparse.ArgumentParser(description="Moneyball: connected fantasy league evidence and strategy", epilog="Older snapshot and experimental commands: moneyball legacy --help")
    p.add_argument("--home",help="Private data directory, defaults to MONEYBALL_HOME; ART_OF_DEAL_HOME remains supported")
    sub=p.add_subparsers(dest="command",required=True)
    c = sub.add_parser("news-leads", help="Dated unverified discovery links; no article text or player truth claims")
    c.add_argument("alias"); c.add_argument("--ids", nargs="+", required=True)
    c.add_argument("--refresh", action="store_true"); c.add_argument("--as-of")
    c.add_argument("--limit", type=int, default=3)
    c = sub.add_parser("decision", help="Prepare, compare, preserve and resume whole-question evidence")
    modes = c.add_subparsers(dest="decision_action", required=True)
    modes.add_parser("demo", help="Five isolated decision journeys with synthetic league evidence")
    c = modes.add_parser("prepare"); c.add_argument("file", help="Decision specification JSON or -")
    c = modes.add_parser("evidence"); c.add_argument("decision_id"); c.add_argument("--ids", nargs="+"); c.add_argument("--detail", choices=["decisive", "expanded", "raw"], default="decisive")
    c = modes.add_parser("compare"); c.add_argument("decision_id"); c.add_argument("file", help="JSON with actions and optional worlds")
    c = modes.add_parser("record"); c.add_argument("decision_id"); c.add_argument("file")
    c = modes.add_parser("read"); c.add_argument("decision_id"); c.add_argument("--comparison-id")
    c = modes.add_parser("resume"); c.add_argument("decision_id"); c.add_argument("--cached", action="store_true")
    c = modes.add_parser("candidates"); c.add_argument("decision_id"); c.add_argument("--limit", type=int, default=20); c.add_argument("--cursor"); c.add_argument("--kinds", nargs="+"); c.add_argument("--ids", nargs="+"); c.add_argument("--package-shapes", help="JSON file of [give_count, receive_count] pairs"); c.add_argument("--detail", choices=["compact", "full"], default="compact")
    c = modes.add_parser("candidate-page"); c.add_argument("candidate_page_id")
    c = modes.add_parser("roster-plan"); c.add_argument("decision_id"); c.add_argument("file")
    c = sub.add_parser("draft-preparation", help="Save/recover host-authored draft alternatives; no picks")
    dm = c.add_subparsers(dest="preparation_action", required=True)
    c = dm.add_parser("save"); c.add_argument("decision_id"); c.add_argument("file"); c.add_argument("--max-age-seconds", type=float, default=300)
    c = dm.add_parser("lookup"); c.add_argument("preparation_id"); c.add_argument("file"); c.add_argument("--max-age-seconds", type=float, default=20); c.add_argument("--deadline")
    c = sub.add_parser("maintenance", help="Opt-in one-shot shared refresh jobs; no scheduler installation")
    mm = c.add_subparsers(dest="maintenance_action", required=True)
    c = mm.add_parser("configure"); c.add_argument("job_id"); c.add_argument("file"); c.add_argument("--cadence-seconds", type=float, required=True); c.add_argument("--disabled", action="store_true")
    c = mm.add_parser("status"); c.add_argument("--job-id")
    c = mm.add_parser("run"); c.add_argument("--jobs", nargs="+"); c.add_argument("--deadline"); c.add_argument("--timeout-seconds", type=float, default=30); c.add_argument("--force", action="store_true")
    c = sub.add_parser("experiment", help="Optional exact-league supplied-data calculations"); c.add_argument("file")
    a = sub.add_parser("assess", help="Player evidence, host judgments and current/future roster alternatives")
    modes = a.add_subparsers(dest="assessment_action", required=True)
    for name in ("references", "demo"):
        modes.add_parser(name)
    c = modes.add_parser("calculate"); c.add_argument("file")
    c = modes.add_parser("context"); c.add_argument("alias"); c.add_argument("--ids", nargs="+", required=True)
    c.add_argument("--fresh", action="store_true"); c.add_argument("--as-of"); c.add_argument("--datasets", nargs="+")
    c = modes.add_parser("save"); c.add_argument("alias"); c.add_argument("assessment_id"); c.add_argument("file")
    for name in ("read", "compare"):
        c = modes.add_parser(name); c.add_argument("alias"); c.add_argument("assessment_id")
        if name == "compare": c.add_argument("file")
        c.add_argument("--as-of"); c.add_argument("--disable-claims", nargs="+"); c.add_argument("--disable-mechanisms", nargs="+")
    sub.add_parser("leagues")
    c=sub.add_parser("discover", help="Discover Sleeper leagues by public username")
    c.add_argument("username"); c.add_argument("season", type=int)
    c=sub.add_parser("select-team"); c.add_argument("alias"); c.add_argument("team_id")
    c=sub.add_parser("doctor", help="Safe local onboarding diagnostics"); c.add_argument("alias", nargs="?")
    sub.add_parser("demo", help="Run an isolated synthetic example without an account")
    c=sub.add_parser("draft", help="Read a connected draft board; no selections")
    c.add_argument("alias"); c.add_argument("--cached", action="store_true"); c.add_argument("--draft-id"); c.add_argument("--ids", nargs="+"); c.add_argument("--expected-board-hash"); c.add_argument("--max-age-seconds", type=int, default=20); c.add_argument("--as-of")
    c.add_argument("--deadline", help="Absolute UTC timestamp or timezone-aware ISO time")
    c=sub.add_parser("lineup"); c.add_argument("alias"); c.add_argument("--week", type=int); c.add_argument("--cached", action="store_true"); c.add_argument("--unavailable", nargs="*"); c.add_argument("--as-of")
    sub.add_parser("pipeline-catalog")
    c=sub.add_parser("pipeline-sync"); c.add_argument("datasets", nargs="+"); c.add_argument("--seasons", nargs="+", type=int, required=True); c.add_argument("--force", action="store_true"); c.add_argument("--offline", action="store_true")
    c=sub.add_parser("pipeline-query"); c.add_argument("dataset"); c.add_argument("--seasons", nargs="+", type=int, required=True); c.add_argument("--alias"); c.add_argument("--ids", nargs="+"); c.add_argument("--source"); c.add_argument("--as-of"); c.add_argument("--limit", type=int, default=20); c.add_argument("--positions", nargs="+"); c.add_argument("--cursor")
    c=sub.add_parser("skill", help="Print the packaged Moneyball strategy skill")
    c.add_argument("--reference", choices=["research-method", "trade-evaluation", "player-assessment"])
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
    c=sub.add_parser("pool"); c.add_argument("alias"); c.add_argument("--positions",nargs="+",default=["QB","RB","WR","TE"]); c.add_argument("--limit",type=int,default=10); c.add_argument("--fresh",action="store_true"); c.add_argument("--query"); c.add_argument("--cursor"); c.add_argument("--as-of")
    c=sub.add_parser("pickup"); c.add_argument("alias"); c.add_argument("player_id"); c.add_argument("--drop"); c.add_argument("--week",type=int); c.add_argument("--cached",action="store_true"); c.add_argument("--expanded",action="store_true"); c.add_argument("--unavailable",nargs="*"); c.add_argument("--as-of")
    c=sub.add_parser("claims"); c.add_argument("alias"); c.add_argument("file",help="JSON array of ordered alternatives: file path or - for stdin"); c.add_argument("--week",type=int); c.add_argument("--cached",action="store_true"); c.add_argument("--expanded",action="store_true"); c.add_argument("--as-of")
    c=sub.add_parser("markets"); c.add_argument("series",nargs="?"); c.add_argument("--limit",type=int,default=10)
    c=sub.add_parser("import-forecasts"); c.add_argument("alias"); c.add_argument("source"); c.add_argument("file")
    sub.add_parser("firecrawl-status", help="Private environment readiness; never prints the API key")
    c=sub.add_parser("firecrawl-discover"); c.add_argument("query"); c.add_argument("--limit", type=int, default=10)
    c=sub.add_parser("firecrawl-sync", help="One explicitly bounded Alexandria execution")
    c.add_argument("alias"); c.add_argument("provider"); c.add_argument("capability"); c.add_argument("options", help="JSON file or -")
    c.add_argument("--max-credits", type=float, required=True); c.add_argument("--player-map"); c.add_argument("--component-map")
    c=sub.add_parser("firecrawl-feeds"); c.add_argument("alias"); c.add_argument("--limit", type=int, default=10); c.add_argument("--as-of")
    c=sub.add_parser("firecrawl-qualify"); c.add_argument("alias"); c.add_argument("feed_key"); c.add_argument("--player-map"); c.add_argument("--component-map")
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
    if a.command == "assess" and a.assessment_action == "demo":
        from .assessment_demo import run
        print(json.dumps(run(), ensure_ascii=False, allow_nan=False))
        return
    if a.command == "decision" and a.decision_action == "demo":
        from .decision_demo import run
        print(json.dumps(run(), ensure_ascii=False, allow_nan=False))
        return
    s=Service(a.home)
    try:
        if a.command == "decision":
            mode = a.decision_action
            if mode == "prepare": out = s.prepare_decision(load(a.file))
            elif mode == "evidence": out = s.decision_evidence(a.decision_id, a.ids, a.detail)
            elif mode == "compare":
                request = load(a.file)
                out = s.compare_actions(a.decision_id, request["actions"], request.get("worlds"))
            elif mode == "read": out = s.read_decision(a.decision_id, a.comparison_id)
            elif mode == "record": out = s.record_decision(a.decision_id, load(a.file))
            elif mode == "candidates": out = s.discover_candidates(a.decision_id, limit=a.limit, cursor=a.cursor, kinds=a.kinds, focus_player_ids=a.ids, package_shapes=load(a.package_shapes) if a.package_shapes else None, detail=a.detail)
            elif mode == "candidate-page": out = s.read_candidate_page(a.candidate_page_id)
            elif mode == "roster-plan": out = s.roster_plan(a.decision_id, load(a.file))
            else: out = s.resume_decision(a.decision_id, refresh=not a.cached)
        elif a.command == "draft-preparation":
            request = load(a.file)
            if a.preparation_action == "save": out = s.save_draft_preparation(a.decision_id, request["branches"], request["board"], a.max_age_seconds)
            else: out = s.prepared_draft_options(a.preparation_id, request["board"], request.get("condition_values"), a.max_age_seconds, a.deadline)
        elif a.command == "maintenance":
            if a.maintenance_action == "configure": out = s.configure_maintenance(a.job_id, load(a.file), a.cadence_seconds, not a.disabled)
            elif a.maintenance_action == "status": out = s.maintenance_status(a.job_id)
            else: out = s.run_maintenance(a.jobs, a.deadline, a.timeout_seconds, a.force)
        elif a.command == "experiment": out = s.run_experiment(load(a.file))
        elif a.command == "assess":
            from . import workbench
            from .workbench_math import calculate
            mode = a.assessment_action
            if mode == "references": out = workbench.catalog()
            elif mode == "calculate": out = calculate(load(a.file))
            elif mode == "context": out = workbench.context(s, a.alias, a.ids, fresh=a.fresh, as_of=a.as_of, datasets=a.datasets)
            elif mode == "save": out = workbench.save(s, a.alias, a.assessment_id, load(a.file))
            else:
                kwargs = dict(as_of=a.as_of, disabled_claims=a.disable_claims, disabled_mechanisms=a.disable_mechanisms)
                out = (workbench.read(s, a.alias, a.assessment_id, **kwargs) if mode == "read" else
                       workbench.evaluate(s, a.alias, a.assessment_id, load(a.file), **kwargs))
        elif a.command=="news-leads": out=s.news_leads(a.alias,a.ids,a.refresh,a.as_of,a.limit)
        elif a.command=="leagues": out=s.leagues()
        elif a.command=="discover": out=s.discover_sleeper(a.username,a.season)
        elif a.command=="select-team": out=s.select_team(a.alias,a.team_id)
        elif a.command=="doctor": out=s.onboarding_status(a.alias)
        elif a.command=="draft":
            from .draft import context
            out=context(s,a.alias,fresh=not a.cached and a.as_of is None,draft_id=a.draft_id,player_ids=a.ids,expected_board_hash=a.expected_board_hash,max_age_seconds=a.max_age_seconds,as_of=a.as_of,deadline=a.deadline)
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
            out=query(s,a.dataset,seasons=a.seasons,alias=a.alias,player_ids=a.ids,source=a.source,as_of=a.as_of,limit=a.limit,positions=a.positions,cursor=a.cursor)
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
            out=acquisition_context(s,a.alias,a.positions,a.limit,a.fresh,a.query,cursor=a.cursor,as_of=a.as_of)
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
        elif a.command.startswith("firecrawl-"):
            from . import firecrawl
            if a.command=="firecrawl-status": out=firecrawl.status(s)
            elif a.command=="firecrawl-discover": out=firecrawl.discover(s,a.query,a.limit)
            elif a.command=="firecrawl-feeds": out=firecrawl.query(s,a.alias,a.limit,a.as_of)
            elif a.command=="firecrawl-qualify": out=firecrawl.qualify_saved(s,a.alias,a.feed_key,
                load(a.player_map) if a.player_map else None,load(a.component_map) if a.component_map else None)
            else: out=firecrawl.sync(s,a.alias,a.provider,a.capability,load(a.options),a.max_credits,
                                    load(a.player_map) if a.player_map else None,load(a.component_map) if a.component_map else None)
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
