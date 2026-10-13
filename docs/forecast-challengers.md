# Forecast challengers and Firecrawl Alexandria

Reviewed 2026-10-07. An optional CLI/MCP feed adapter is implemented and tested with synthetic responses; live authentication/execution and a measured fantasy advantage remain unverified. See [feed setup and supported qualification](firecrawl-feed.md). Moneyball's `source_registry` exposes the access status. Sleeper and ESPN remain the account and league-state adapters.

The goal is to improve a complete decision: refresh a connected league, identify a real roster choice, collect evidence capable of changing that choice, compare legal alternatives over the relevant ownership horizon, and explain the belief that would reverse the advice.

## Useful existing mechanisms

[Firecrawl's announcement](https://www.firecrawl.dev/blog/alexandria-fantasy-football) advertises six fantasy projection/ranking providers, news, injuries and market-derived projections through one connection. [Alexandria documentation](https://docs.firecrawl.dev/features/alexandria) describes free tool discovery, priced executions, response contracts and provider-specific terms. Provider pages explicitly label their displayed responses as samples. No sample values should enter current advice.

| Priority | Candidate | Missing decision input | Qualification before use |
| --- | --- | --- | --- |
| First | [FFToday](https://www.firecrawl.dev/alexandria/fftoday-com) weekly stat components | A forecast challenger that can be scored for the actual league | Match season/week and player identity; map supported components and expose missing scoring events. Ten presets are not arbitrary custom rules. |
| First | [Draft Sharks](https://www.firecrawl.dev/alexandria/draftsharks-com) weekly/ROS boards | Holding and dropping over future weeks; TE-premium and superflex context | Establish whether points are per-game, period totals or medians. Floor/ceiling labels need definitions before probability claims. Overall ranks are preferences, not point means. |
| First when status matters | [NFL reports](https://www.firecrawl.dev/alexandria/categories/sports) | Practice participation, transactions and current deployability | Require event date, season/week and primary-source link. A fresh retrieval of an old report does not confirm this week's status. |
| Challenger | [StartWho](https://www.firecrawl.dev/alexandria/startwho-com) | Market evidence that may disagree with a stale role assumption | Preserve the underlying book, market, line, odds and timestamp. A consensus prop line is not an expected-stat mean; pricing can condition on participation differently from fantasy scoring. |
| Later | [Fantasy Football Calculator](https://www.firecrawl.dev/alexandria/fantasyfootballcalculator-com) ADP/dynasty/rookie boards | Draft acquisition cost and observed preferences | Match season, format, league size and sample period; do not turn ADP into future points. |
| Conditional | CBS and FantasyData | Additional weekly/ROS comparisons or position coverage | Verify current access/terms and completeness. A gateway does not resolve the existing CBS automated-access restriction or authorize public redistribution. |

Existing Moneyball already supplies league rules/ownership, historical usage, player crosswalks, dated evidence and a forecast import ledger. Reuse those boundaries instead of replacing them with an external lineup tool. Historical opportunity models such as the registry's `ffopportunity` candidate can provide a different evidence family, but are not yet installed live and do not forecast future volume.

## Transfer into the existing service

Use the existing private forecast ledger and shared decision normalizer after provider-specific qualification; preserve the forecast author as `source`, with the gateway and capability in separate provenance. A Firecrawl-wide source label would incorrectly merge incompatible cohorts. Match external player IDs through recorded crosswalks; ambiguous matches stay unresolved.

Carry requested and returned season/week, explicit horizon and units, scoring assumptions, status conditioning, provider update time, first local observation time and source URL. Keep ranks and ADP out of point fields. The current import path validates identity/season/period and finite numbers, but does not itself establish provider units or prediction quality: a future adapter must enforce those semantics before calling it.

Use stat components with actual league weights where coverage permits. Unsupported events remain missing. Threshold bonuses need expected trigger frequencies or outcomes, not the bonus applied to a mean stat line. A provider's default PPR total must not receive the connected league's scoring hash merely because both use PPR.

Require coverage of the affected hold and proposed rosters from each evaluated cohort. Missing forecasts must not become zero or be filled opportunistically from a different source. Reject wrong-week, stale or ambiguous records with a visible reason. Preserve each accepted cohort for comparison before constructing any ensemble.

The account connection is optional. Initial discovery can inspect contracts and costs without fetching every table. Execution requires a configured key/connection and an appropriate credit budget; provider-term acceptance remains an explicit user action where required by Firecrawl. Public examples must be synthetic rather than redistributed provider tables.

## Where a defensible edge might come from

**Forecast combination:** research on [the forecast combination puzzle](https://arxiv.org/abs/1505.00475) distinguishes adapting to a changing best forecaster from combining forecasts for improvement, and examines the cost of estimating weights. Transfer this as a hypothesis: retain the native baseline, individual challengers and a simple combination as competing candidates. Do not learn elaborate weights from four games or treat six agreeing sites as six independent measurements. Check shared upstream inputs and correlated errors first.

**Value of information:** target retrieval at facts that could change the action. For a bye-week quarterback add, investigate the candidate's starting role and the proposed drop's future service. For an injured backfield, inspect current transactions and opportunity before refreshing every rank. The test is whether the new evidence changes a legal recommendation with a defensible cost, not how many sources were fetched.

**Inventory and option value:** a bench player grants contingent future starts. Compare the service lost, injury/bye coverage, replacements and reacquisition cost against immediate coverage gained. This is especially consequential for dynasty and superflex. A one-week zero does not price those rights.

**Detect changed opportunity:** compare recent touches/targets and participation with prior role assumptions, then seek an injury, transaction or deployment explanation. Keep variance and measurement gaps visible; a touchdown spike or one snap-share observation alone is not a durable role change. Routes and opportunity quality would improve this analysis where suitable sources are available; neither is supplied merely by a generic projection board.

These are proposed adaptations, not observed advantages. The benefit to test is faster recognition and better league-specific decisions than the default platform advice.

## Qualification and prospective evaluation

Before promoting a feed, exercise: an ambiguous player match, wrong week, preseason season-total returned for ROS, per-game points returned as a total, an undated update, missing bench players, correlated providers, and a newly changed injury/role. Each must remain unresolved or be rejected without fabricating values. Use synthetic public fixtures and private licensed live captures.

Save forecasts before kickoff with immutable local observation times and later attach realized outcomes. Provider revision histories and last week's projected-vs-actual endpoint are not proof that those forecasts were available before kickoff. Evaluate forecast errors by position and evidence conditions, then evaluate actual choices against the same-time native-baseline policy and hold policy using the same candidate pool, league rules and transaction costs. Report hindsight regret as a diagnostic, never as an executable policy. Assess calibration only for distributions or explicitly defined probabilities.

Track coverage, stale/mismatched records, ingestion failures, changed recommendations, credit cost and decision error. A better projection score alone does not establish more useful trades, waiver decisions or wins. Promote a source only when representative fresh decisions demonstrate a useful contribution; retain failed or conditional challengers with their scope and revisit trigger.
