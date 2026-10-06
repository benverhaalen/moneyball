---
name: moneyball-strategy
description: Research and maintain exact-rule fantasy football strategy after connecting a Sleeper or ESPN league, and apply it to live drafts, lineups, waiver alternatives and curated trades.
---

Use Moneyball's connected league tools. The service retrieves evidence and performs accounting; you research and make the recommendation in this session. No tool sends an offer, claim, message or lineup change.

## After connection

Inspect `connect_league` or `league_context`'s `research` field. If `pending_agent_research`, call `league_research_plan`. Read [research method](references/research-method.md), available as `moneyball://research/method`, and [trade evaluation](references/trade-evaluation.md), available as `moneyball://research/trades`. Apply both when writing the league's initial policies. Complete the research using your browsing tools and save it with `save_league_research`. If tools or essential evidence are unavailable, disclose the gap and leave the research pending; do not ask the user to perform the research or describe compiled rules as completed discovery.

For CLI access use `moneyball agent` (also `moneyball-agent`). `research ALIAS` returns the plan, `research-save ALIAS REPORT.json` saves the report, and `skill --reference research-method` prints the method. These commands use the same service and private evidence store as MCP. Avoid defaulting to a personal league alias.

The plan supplies current league inputs, a binding `research_key`, and the existing rule-conditioned mechanisms. Discover missing methods rather than merely paraphrasing that library. Prioritize unresolved decisions, inspect structurally distant work, and reject seductive analogies when assumptions fail. Separate observations, transfers and empirical claims. No quota of domains or citations establishes useful research.

Save `summary`, `sources`, `mechanisms`, `policies`, `rejected_transfers`, and `research_gaps` with the plan's key. See the method reference for the report contract. Every policy covers one of `draft`, `lineup`, `waiver`, `trade`. Keep stable strategy free of standing player recommendations. State unknowns and conditional applications. Server validation is not independent confirmation of source reading or an edge.

## Reuse in decisions

Read `league_context(fresh=true)` and compact `league_strategy`. Saved strategy survives roster, standings and projection updates; changed rules, league season/team identity or method version require fresh research. Expand the saved report for a disputed mechanism, not every routine request. Refresh decision inputs separately and investigate only uncertainties that could change the action.

- **Draft:** call `draft_context` for the selected league. Reason about complete team construction, actual available players, observed turn timing and the cost of deferral. Separate own-team usefulness from opponent demand. Verify the observed pick prefix and freshness; suppress an obsolete board. Sleeper supplies checked board observations; ESPN supplies draft history with explicit live-clock limitations. Re-read after picks change; no background watcher or pick execution is supplied.
- **Lineup:** use `lineup_packet` to compare legal slot assignments with pre-lock information, eligibility and actual scoring. Verify its explicit lock-state gaps before recommending a change. Managed lineups cannot select with hindsight; best-ball expectations need joint outcomes.
- **Waivers:** use `acquisition_context`, then `pickup_packet` and `claim_scenarios` with named drops and evaluation weeks. Compare hold, success/fallback/failure, resource cost, future coverage, and doing nothing. Current acquisition status and future forecasts are separate. Explain bid/priority reasoning without inventing rival claim probabilities.
- **Trades:** read [trade evaluation](references/trade-evaluation.md), available as `moneyball://research/trades`. Curate legal packages around both teams' actual needs and outside options. Follow `trade_packet.decision_support`: give the user's action, who benefits, both managers' tradeoffs and why. For a relatively even deal, explain what tips the decision and what would reverse it; do not invent a decisive winner. The packet measures roster effects; you supply the supported recommendation.

Lead with the action and why it fits this league. Give confidence, the strongest countercase, executable versus conditional alternatives, relevant timestamps/gaps, and the fact or threshold that changes the answer. Keep extensive research behind an accessible explanation.

For trades, expose the pivotal projection as an if/otherwise recommendation. Explain how the advice changes if the user expects X to outperform Y over the relevant service horizon, after the full roster substitutions and costs. Distinguish that belief from the current evidence-based projection; calculate a break-even only when coherent inputs support it. Follow `decision_support.answer_contract.conditional_recommendation`.
