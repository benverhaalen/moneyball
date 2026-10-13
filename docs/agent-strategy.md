# Connected league research and decision methods

Moneyball provides one connected workflow for league research, player assessments and roster decisions. Previous command names remain compatibility aliases; see [migration](migration.md).

Install this checkout with Python 3.11 or later:

```sh
python -m pip install '.[agent]'
moneyball skill
moneyball --help
```

`moneyball-agent ...` and `moneyball agent ...` are compatibility aliases. `moneyball-mcp` starts the local stdio server. An MCP client's executable must resolve in its configured environment. Install the `analytics` extra when using the NumPy/SciPy research paths. No model API key is required by this package; the user's coding agent performs research and recommendations.

CLI and MCP share the `MONEYBALL_HOME` evidence root (CLI override: `--home`). New installations default to `~/.local/share/moneyball`; an existing old default store is reused. `ART_OF_DEAL_HOME` remains a fallback. The pipeline warehouse normally lives under the same root. Older snapshots are accessed explicitly through `moneyball legacy --data-dir PATH`; no private databases are silently merged.

## Account and team onboarding

Sleeper requires no password for public league reads. Discover a user's leagues for an explicit season, then connect each selected league under a useful alias. Copy the returned numeric IDs; the placeholders below are not runnable account identifiers.

```sh
moneyball discover YOUR_SLEEPER_USERNAME 2026
moneyball connect home sleeper LEAGUE_ID 2026 --user-id USER_ID
moneyball context home --fresh
moneyball doctor home
```

Repeat connection for the leagues you choose; aliases keep their evidence and strategy distinct. MCP equivalents are `discover_sleeper_leagues`, `connect_league` and `onboarding_status`. If own-team identity is ambiguous or absent, context reports `team_selection_required` and lists observed teams. Use `moneyball select-team home TEAM_ID` or `select_team` rather than assuming the first roster is yours. Selection changes the research binding.

For ESPN, supply a numeric league ID or HTTPS ESPN URL containing one `leagueId`, season and your team ID:

```sh
moneyball connect redraft espn LEAGUE_ID 2026 --team TEAM_ID
# Private leagues, after credentials are configured locally:
moneyball connect redraft espn LEAGUE_ID 2026 --team TEAM_ID --auth environment
```

Configure `ESPN_SWID` and `ESPN_S2` in the process environment or your client's local secret configuration. Do not paste their values into chat, commit them, or put them in issue reports. They come from your own authorized ESPN session; they are not a Moneyball API key. `environment` authentication fails visibly when either is absent. Credential expiration requires renewing the provider session. Public leagues can use `--auth none`, the default. The optional Firefox mode is opt-in and requires authorization for its selected profile. No OAuth flow or automatic discovery of every ESPN account league is implemented.

`doctor` reports local configuration, snapshot freshness and onboarding gaps without network access or echoing credentials. A healthy local configuration does not prove that credentials still work remotely; refresh the actual league to verify access.

## From a Tuesday question to a decision

The shared decision lifecycle is `prepare_decision → decision_evidence → compare_actions → record_decision → resume_decision`. Preparation returns exact rules, all-team membership, an evidence cutoff, source readiness and canonical action examples. Every comparison includes an explicit hold. Recording requires a countercase and reversal conditions; supplied citations must refer to evidence actually consumed. A receipt checks dependency consistency, not whether the host's judgment is true.

MCP comparisons and candidate discovery use compact responses by default. Exact membership, capacity, selected forecast lineups and qualifications remain available; repeated strategy copies and detailed eligibility inventories have explicit expansion paths. `read_decision(decision_id, comparison_id)` recovers the full original comparison. Candidate search returns a `candidate_page_id`; `read_candidate_page` retrieves its full calculations. Neither expansion refreshes or certifies old evidence. Service callers can request full detail.

Saved decisions recover their original actions, alternatives, countercases and reversals. Resume prepares a new revision and labels old reasoning as requiring recheck. A roster, forecast or consumed draft-board change can invalidate a comparison even if the league rules are unchanged.

The agent refreshes league state, checks own-team identity and the saved strategy binding, gets focused acquisition context, and compares candidate pickups against holding with any required drops. It separately acquires matching-week forecasts and current player evidence. Sleeper league discovery does not supply a forecast feed: authorized dated exports can be imported; ESPN forecasts have their own refresh tool. Missing or incomplete support prevents a complete mechanical comparison.

Lineup, pickup, ordered claim and trade packets carry exact rules, evidence times, conditional comparisons and the saved policies. The final answer should name the decisive assumptions, opportunity costs, deadline/eligibility gaps and reversal conditions. Holding is an actual alternative. Waiver priority, FAAB and clearing rules must come from observed settings; a budget field alone cannot identify the acquisition system.

For trades, use the packaged trade reference. Compare complete post-trade rosters for both managers, including displaced starters, drops, reserve/taxi restrictions, future picks and feasible outside options. Curate a small set of defensible deals and explain why the other manager could prefer one. State who benefits, who gives up more under the modeled assumptions, and each side's tradeoffs. Both managers can benefit; relative gain does not automatically decide whether you should accept. Prefer meaningfully balanced proposals when supported, with the reason for recommending each and a condition that would reverse it. No universal near-even threshold or automatic winner is computed. No packet sends an offer.

```sh
moneyball draft home
moneyball lineup home --week 5
moneyball pool home --fresh --positions RB WR --limit 10
moneyball pickup home PLAYER_ID --drop DROP_ID --week 5
```

Copy exact IDs from observed tool results. Forecast refresh/import is separate; these commands do not magically supply missing forecasts. Historical `--as-of` reads use retained versions; draft historical reads require `--cached`.

## Automatic preparation, host-executed research

After a successful `connect_league` or `moneyball connect`, the service compiles exact-rule mechanics and persists a research request. Its response includes `research.status=pending_agent_research` and the next tool. The MCP server includes the portable skill in its instructions and exposes it as a resource, so the calling agent is instructed to complete the research without a separate user request.

The host reads `league_research_plan`, investigates primary references with its browsing tools, and saves a report through `save_league_research`. CLI equivalents are `research ALIAS` and `research-save ALIAS REPORT.json`. A CLI-only caller must execute these steps; the server does not spawn a model or browser. If the host cannot research, the request remains visibly pending.

The installed method and trade references are available through `moneyball skill --reference research-method`, `--reference trade-evaluation`, and MCP resources `moneyball://research/method` and `moneyball://research/trades`. The skill directory is bundled in the wheel and can be copied into a coding agent's supported skill directory; no global skill installation is performed by connection.

The research method preserves Moneyball's structural discovery, primary-source access records, assumption checks, rejected analogies, comparison tests and prospective evaluation. The trade method preserves Art of the Deal's complete legal substitutions, bilateral incentives, named outside options, timing, capacity costs, equal future-management standards and reversal conditions.

Saved policies cover draft, lineup, waiver and trade. They are returned by `league_strategy`, so existing trade, pickup and claim packets receive them through the same service. Expanded strategy includes the complete report. Connected draft context carries those same policies with observed Sleeper picks and ownership. ESPN draft context is conservative history without supported live-clock or current-picker inference. The experimental browser observer and simulation tools remain separate, with narrower format support. No tool submits a selection.

## Binding and limits

For a draft, the host can save ordered, fully reasoned alternatives through `save_draft_preparation`. `prepared_draft_options` checks a separately observed current board and removes players taken by rivals. An own pick, changed rules, changed evidence or a rewritten board invalidates the preparation. Unknown conditions remain conditional. This supports reasoning between picks; it does not establish sub-minute host latency or select a player automatically.

Optional `run_league_experiment` results can support a compared action through its `experiment_ids` list. Run the calculation before preparing (or resume afterward), so its saved request and result are knowable at the decision cutoff. The comparison checks the exact saved league/rules/roster binding and includes both records in its consumed manifest. Changed results invalidate the final receipt. Compact packets retain qualifications and bounded output previews; expand the original before relying on omitted results. These are host-selected supporting analyses, not automatic scores, rankings or calibrated title odds.

Maintenance jobs are opt-in persisted configurations, with shared league/forecast/source acquisition paths, leases and separate attempt/success histories. `run_maintenance` executes one bounded pass; preparation can name configured jobs through `maintenance_job_ids`. An external scheduler may invoke `moneyball maintenance run`; this package installs no scheduler or background daemon. Late source acquisition may finish after the caller deadline, and failed attempts never renew old successful evidence dates.

Research binds to the exact rules fingerprint, method version, platform/league, season, own team and team count. Roster/projection/standings-only refreshes preserve it. Changed binding makes the current research pending and rejects an old-key submission. Older reports remain historical evidence and cannot appear in an as-of query before their local save time.

Reports require inspected-source metadata, conditional mechanisms, exact rule paths, assumptions, evidence needs, failure conditions, discriminating tests, policies for all four operations, and explicit research gaps. The service derives rule values itself. These checks establish input binding and report structure, not truth of source reading, practical strategy quality, automatic host compliance or a measured predictive advantage.

Tests use synthetic data and a mocked provider fetch for connection, plus an actual MCP stdio session for saving research and using it in a trade packet. They do not constitute a live account onboarding or independent model-research evaluation.

Fresh draft-board reads require each HTTP attempt to start after its caller began that check. This deliberately bypasses ordinary cache reuse and does not reuse an older in-flight response. Source cache headers and board consistency checks still matter: local request timing cannot prove the provider has published the latest pick. If the strict request exceeds the deadline, retained evidence stays dated and the board read remains partial.
