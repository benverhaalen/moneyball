"""Local MCP surface. All recommendations remain in the calling session."""
import argparse
import json
from mcp.server.fastmcp import FastMCP
from .service import Service
from .sources import registry
from .league_research import skill_text

mcp = FastMCP("Moneyball", instructions=(
    "Read-only fantasy league evidence and trade accounting. Start with onboarding_status and list_leagues. For a new Sleeper user, discover_sleeper_leagues and connect only selected leagues with the returned user_id. ESPN accepts a league URL or ID; private credentials stay in the local environment. If team selection is pending, show observed teams and use select_team before personalized reasoning. Then league_context and league_strategy. "
    "Use the league's verified retention, scoring, slots and playoff structure. Do not rederive cached strategy every trade. "
    "Refresh dynamic state before actionable advice; forecast refresh is separate and period-specific. "
    "trade_packet validates and compares complete legal rosters but produces no calibrated championship odds or recommendation. "
    "Reason in this session about both teams, holding versus trading, roster coverage and decision-sensitive uncertainty. "
    "Missing data is never zero, a prop threshold is not a mean, and archived dossiers are not current injury reports. "
    "All writes here are to local evidence storage; there is no trade submission or messaging tool. "
    "After connect_league, inspect research status: if pending, get league_research_plan, perform the packaged research workflow using host browsing, and save_league_research. "
    "Do not claim a connection or rule compiler performed new research. Reuse saved policies in draft, lineup, waiver and trade reasoning.\n\n" + skill_text()), host="127.0.0.1")

def service():
    return Service()

@mcp.tool()
def list_leagues() -> dict:
    """List explicitly connected leagues and last successful local snapshot times."""
    return {"leagues":service().leagues()}

@mcp.tool()
def discover_sleeper_leagues(username: str, season: int) -> dict:
    """Find Sleeper memberships by public username/user ID. No password needed; choose leagues explicitly before connect_league."""
    return service().discover_sleeper(username, season)

@mcp.tool()
def select_team(alias: str, team_id: str) -> dict:
    """Bind the user's explicitly chosen observed team. Changes local config and research binding, never the league itself."""
    return service().select_team(alias, team_id)

@mcp.tool()
def onboarding_status(alias: str | None=None) -> dict:
    """Local readiness, missing team/research/forecasts and private ESPN setup guidance. No secrets or network authentication test."""
    return service().onboarding_status(alias)

@mcp.tool()
def draft_context(alias: str, fresh: bool=True, draft_id: str | None=None, player_ids: list[str] | None=None, expected_board_hash: str | None=None, max_age_seconds: int=20, as_of: str | None=None) -> dict:
    """Read the connected draft board, picks and exact-rule strategy. Sleeper current reads; ESPN draft history cannot establish a live clock. Never selects players. Preserve expected_board_hash to detect board movement."""
    from .draft import context
    return context(service(),alias,fresh=fresh,draft_id=draft_id,player_ids=player_ids,expected_board_hash=expected_board_hash,max_age_seconds=max_age_seconds,as_of=as_of)

@mcp.tool()
def lineup_packet(alias: str, evaluation_week: int | None=None, fresh: bool=True, unavailable: list[str] | None=None, as_of: str | None=None) -> dict:
    """Legal lineup counterfactual using dated, rule-matched forecasts and saved league strategy. No lineup submission; incomplete data remains explicit."""
    from .lineups import packet
    return packet(service(),alias,evaluation_week=evaluation_week,fresh=fresh,unavailable=unavailable,as_of=as_of)

@mcp.tool()
def pipeline_catalog() -> dict:
    """Inspect existing NFL data pipelines, local coverage, provenance and availability limits before choosing explicit datasets/seasons."""
    from .pipelines import catalog
    return catalog(service())

@mcp.tool()
def sync_pipeline(datasets: list[str], seasons: list[int], force: bool=False, offline: bool=False) -> dict:
    """Ingest selected bounded datasets/seasons into the private warehouse. Network reads may be substantial; failed refreshes preserve valid partitions."""
    from .pipelines import sync
    return sync(service(),datasets,seasons,force=force,offline=offline)

@mcp.tool()
def query_pipeline(dataset: str, seasons: list[int], alias: str | None=None, player_ids: list[str] | None=None, source: str | None=None, as_of: str | None=None, limit: int=20, positions: list[str] | None=None) -> dict:
    """Bounded locally knowable NFL evidence. Focus connected players by exact recorded crosswalks; no fuzzy identity matching or missing-as-zero."""
    from .pipelines import query
    return query(service(),dataset,seasons=seasons,alias=alias,player_ids=player_ids,source=source,as_of=as_of,limit=limit,positions=positions)

@mcp.tool()
def connect_league(alias: str, platform: str, league_id: str, season: int, own_team_id: str | None=None, user_id: str | None=None, auth_mode: str="none") -> dict:
    """Connect a selected Sleeper/ESPN league locally. ESPN Firefox auth requires the user's authorization; never request cookies in chat. No league changes."""
    return service().connect(alias,platform,league_id,season,own_team_id,user_id,auth_mode)

@mcp.tool()
def league_context(alias: str, fresh: bool=False, as_of: str | None=None) -> dict:
    """Compact roster/all-team summary and freshness. fresh=true before actionable advice; historical as_of cannot refresh."""
    return service().context(alias,fresh,as_of)

@mcp.tool()
def league_details(alias: str, section: str, team_id: str | None=None, as_of: str | None=None) -> dict:
    """Read rules, teams, schedule, picks, transactions or completeness; use team_id for focused roster detail."""
    return {"section":section,"data":service().details(alias,section,team_id,as_of)}

@mcp.tool()
def league_strategy(alias: str, expanded: bool=False, as_of: str | None=None) -> dict:
    """Reuse compiled exact-rule strategy. Expanded includes mechanics, citations, assumptions and falsification tests."""
    return service().strategy(alias,expanded,as_of)

@mcp.tool()
def league_research_plan(alias: str, as_of: str | None=None) -> dict:
    """Get exact league inputs and the pending RDD research workflow. Host agent performs the research; a compiled profile is not completed discovery."""
    return service().research_plan(alias,as_of)

@mcp.tool()
def save_league_research(alias: str, report: dict) -> dict:
    """Save host-authored league research bound to the current research_key. Requires inspected sources, conditional mechanisms/tests and policies for draft/lineup/waiver/trade. Validates structure, not research quality."""
    return service().save_research(alias,report)

@mcp.resource("moneyball://skills/strategy")
def strategy_skill() -> str:
    """Portable research and decision skill included in the installed package."""
    return skill_text()

@mcp.resource("moneyball://research/method")
def research_method() -> str:
    return skill_text("research-method")

@mcp.resource("moneyball://research/trades")
def trade_method() -> str:
    return skill_text("trade-evaluation")

@mcp.tool()
def search_players(alias: str, query: str, limit: int=10) -> dict:
    """Find namespaced roster identities and ownership; never infer a matching player solely from position."""
    return {"players":service().search(alias,query,limit)}

@mcp.tool()
def player_evidence(alias: str, player_ids: list[str], question: str | None=None, expanded: bool=False, as_of: str | None=None) -> dict:
    """Dated player evidence and targeted research triggers. Read full historical dossier only when needed."""
    return {"players":service().evidence(alias,player_ids,question,expanded,as_of)}

@mcp.tool()
def refresh_forecasts(alias: str, weeks: list[int] | None=None, force: bool=False) -> dict:
    """Fetch observed ESPN forecasts for specified weeks. Does not turn past/full-season rows into future or ROS forecasts."""
    return service().refresh_forecasts(alias,weeks,force)

@mcp.tool()
def import_forecasts(alias: str, source: str, rows: list[dict]) -> dict:
    """Save authorized provider exports locally with actual import time. Requires exact IDs, source URL, season, period, conditioning and scoring provenance."""
    return service().import_forecasts(alias,source,rows)

@mcp.tool()
def trade_packet(alias: str, proposal: dict, fresh: bool=True, unavailable: list[str] | None=None, as_of: str | None=None, expanded: bool=False) -> dict:
    """Build both teams' roster counterfactuals and decision support; host reasoning explains who benefits, tradeoffs and whether to accept, hold or counter. Never sends offers. proposal: transfers[{player_id,from_team,to_team}], optional drops{team_id:[ids]}, pick_transfers, evaluation_week. Explicit unavailable IDs are sensitivity scenarios. Refresh matching forecasts separately."""
    return service().trade_packet(alias,proposal,fresh,unavailable,as_of,expanded)

@mcp.tool()
def save_player_dossier(player_ref: str, dossier: dict) -> dict:
    """Preserve calling-session evidence locally. Requires claims, sources, evidence_checked_at and reversal_conditions. Not independently certified by server."""
    return service().save_dossier(player_ref,dossier)

@mcp.tool()
def source_registry(source_id: str | None=None) -> dict:
    """Dated source access, horizon, terms and limits. Does not claim a universal best projection or injury provider."""
    return registry(source_id)

@mcp.tool()
def saved_packet(alias: str, as_of: str | None=None, kind: str="trade_packet") -> dict:
    """Read a previously saved full trade packet and receipt without recomputing it."""
    if kind not in ("trade_packet","pickup_packet","claim_packet"):
        raise ValueError("kind must be trade_packet, pickup_packet or claim_packet")
    return service().store.get(kind,alias,as_of)

@mcp.tool()
def market_evidence(records: list[dict], requested_scope_ids: list[str], as_of: str | None=None) -> dict:
    """Filter normalized short-term market evidence by scope/time. No automatic odds-to-mean conversion, pooling of books, or ROS inference."""
    from .markets import evidence_bundle
    from .store import timestamp
    return evidence_bundle(records,requested_scope_ids=requested_scope_ids,as_of=timestamp(as_of))

@mcp.tool()
def market_snapshot(series_ticker: str, limit: int=10) -> dict:
    """Read short-lived official public Kalshi NFL market evidence. No orders or automatic point forecasts."""
    from .markets import fetch_kalshi
    from .http import Client
    s=service()
    result=fetch_kalshi(Client(s.store),series_ticker,limit)
    s.store.put("market_snapshot",series_ticker,result)
    return {k:v for k,v in result.items() if k!="raw"}

@mcp.tool()
def market_sources() -> dict:
    """List verified callable public NFL market series; a contract's terms determine its scope."""
    from .markets import market_sources as known
    return {"series":known()}

@mcp.tool()
def acquisition_context(alias: str, positions: list[str] | None=None, limit: int=10, fresh: bool=False, query: str | None=None) -> dict:
    """Current acquisition candidates, priority and teams ahead. Uses CURRENT scoring period for status; rankings/ownership sort are discovery aids, not recommendations."""
    from .operations import acquisition_context as op
    return op(service(),alias,positions,limit,fresh,query)

@mcp.tool()
def pickup_packet(alias: str, player_id: str, drop_player_id: str | None=None, evaluation_week: int | None=None, fresh: bool=True, unavailable: list[str] | None=None, expanded: bool=False, as_of: str | None=None) -> dict:
    """Compare hold versus a named waiver/free-agent acquisition and explicit drop. Conditional roster effect only; never submits a claim. Refresh owned-player forecasts for evaluation_week first."""
    from .operations import pickup_packet as op
    return op(service(),alias,player_id,drop_player_id,evaluation_week,fresh,unavailable,expanded,as_of)

@mcp.tool()
def claim_scenarios(alias: str, claims: list[dict], evaluation_week: int | None=None, fresh: bool=True, unavailable: list[str] | None=None, expanded: bool=False, as_of: str | None=None) -> dict:
    """Compare ordered acquisition alternatives. Each claim has player_id, optional drop_player_id, priority_before/after_if_success or faab_bid/budget_before. Independent conditional successes, not predicted outcomes or a platform queue emulator. Include the lose-all/hold branch in session reasoning."""
    from .operations import claim_scenarios as op
    return op(service(),alias,claims,evaluation_week,fresh,unavailable,expanded,as_of)

@mcp.resource("art-of-the-deal://sources")
def sources_resource() -> str:
    return json.dumps(registry())

@mcp.prompt()
def evaluate_trade(alias: str, offer: str) -> str:
    return (f"Evaluate this unacted offer in {alias}: {offer}. Use the packaged Moneyball strategy and Art of the Deal trade method. "
            "Freeze current league data, load saved strategy, resolve exact ownership, and choose explicit future week(s). "
            "Compare actual starter changes on both sides, bench/IR/drop consequences and named replacement options. "
            "Consult player_evidence and source_registry only for uncertainty that can reverse the recommendation. "
            "Use dated props for short-term thresholds only. Separate redraft service from keeper costs or dynasty carryover. "
            "Explain who benefits on each relevant horizon, what each side gives up, and whether accept, hold, decline or counter best serves the user's exact team. An even-looking package still needs a recommendation and its reasons. State the pivotal projection as an if/otherwise decision, distinguishing the current forecast from the user's alternative belief; include the full package costs and a supported break-even when calculable. Return confidence, substitutions, strongest countercase and what changes the answer. "
            "Do not send offers, treat point deltas as title odds, or import position/age heuristics.")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--transport",choices=["stdio","streamable-http"],default="stdio")
    args=parser.parse_args()
    # HTTP is loopback only. Public hosting requires identity, tenant isolation and source rights.
    mcp.run(transport=args.transport)

if __name__ == "__main__":
    main()
