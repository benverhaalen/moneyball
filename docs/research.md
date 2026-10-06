# Research and reference decisions

Moneyball uses reference-driven development to adapt mechanisms already useful elsewhere, then checks whether their conditions hold in a particular league. Popularity, a familiar label or a good story is insufficient evidence of fit.

## The reusable research operation

Connection compiles observed rules and prepares a research request. The host agent reads the packaged method, investigates primary sources, maps relevant mechanisms to exact rule paths and saves policies for draft, lineup, waiver and trade. The report includes source access, assumptions, evidence gaps, failure conditions, rejected transfers and discriminating tests. Its key binds the report to the league, team, season and method.

Examples of useful research questions:

| Fantasy decision | Structural problem | Candidate transfer and test |
| --- | --- | --- |
| FLEX and positional depth | Constrained assignment and substitutability | Compare complete legal lineups; test whether a nominal upgrade displaces useful existing service |
| Injured or developmental bench player | Real options and uncertain future service | Compare keeping the option with a named available replacement under explicit absence/role branches |
| Waiver budget and timing | Auctions and scarce replenishable resources | Check actual acquisition rules; compare a bid's opportunity cost and later feasible acquisitions |
| Trade acceptance | Bargaining with outside options | Evaluate each manager's whole roster and named hold/waiver alternatives; find which assumption changes both sides' incentives |
| Draft timing | Sequential allocation under uncertain competition | Separate player usefulness from availability at the next pick; test against changed boards and opponent-model counterexamples |

These are hypotheses, not proof that financial, economic or optimization models transfer unchanged. Strategies should expose the observation that would falsify them and the conditions that trigger new research. Prospectively recorded decisions and outcomes are needed to evaluate actual usefulness.

## Existing work with a specific job

References inspected during public packaging, October 2026:

- [Sleeper's API documentation](https://docs.sleeper.com/) supplies the public read contract: username/user identity, season-specific league discovery and draft endpoints. This informs explicit discovery and connection, separate from authenticated actions.
- [joscaz/sleeper-mcp](https://github.com/joscaz/sleeper-mcp) provides a useful distribution and interaction reference: stdio configuration and focused named-player queries. Moneyball adopts a portable MCP entry point while retaining its own evidence and rule-bound research contracts; it does not inherit the reference's optional account-write features.
- [cwendt94/espn-api](https://github.com/cwendt94/espn-api) provides observed ESPN identifiers and request conventions. Moneyball retains a conservative normalizer and separate private-session setup. Adapted details and the upstream MIT notice appear in THIRD_PARTY_NOTICES.md.
- [nflreadpy](https://github.com/nflverse/nflreadpy) provides a maintained data-loading reference. The useful separation is dataset acquisition from downstream analysis. Moneyball's existing warehouse additionally records acquisition and point-in-time lineage; accessible datasets are not automatically forecasts.

Moneyball's own earlier research supplied snapshot caching, compact outputs, acquisition-time lineage, rule fingerprints and conditional simulation. Art of the Deal supplied full-roster substitutions, bilateral incentives, exact player identities, outside options and dated evidence. The public release packages those mechanisms rather than rebuilding two parallel products.

## What a polished release establishes

The release should make a new user's path concrete: install, run an offline example, configure an agent, discover/connect selected leagues, identify their teams, complete rule-specific research, then ask for a decision with explicit freshness and evidence gaps. Distribution checks exercise installed resources and entry points outside the checkout. Synthetic tests cover mechanics and failure paths; they do not establish a live account connection or predictive advantage.

Useful future evidence includes reproducible onboarding completion, source refresh reliability, legal and timely draft decisions, measured usefulness of waiver/trade alternatives and prospective comparison with simpler baselines. Retain failed and uncertain cases. More simulation draws or passing report-schema checks cannot substitute for those evaluations.
