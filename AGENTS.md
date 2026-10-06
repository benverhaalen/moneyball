# Working on Moneyball

Moneyball combines league evidence, portable agent strategy research, draft support and Art of the Deal trade/waiver accounting. Sleeper and ESPN are the supported league platforms. See README.md and docs/agent-strategy.md for the public user journey.

Read exact league rules before proposing a strategy. Refresh dynamic state before actionable advice, inspect forecast period/scoring/availability separately, and keep missing evidence explicit. Prefer compact queries over entire player catalogs. The host agent performs research and recommendations; the local service provides evidence and mechanical comparisons.

Keep private stores, browser state, credentials, player evidence and experiment outputs out of commits. Use synthetic fixtures for tests. Never paste authentication cookies into chat or logs. Connection and data-import tools write local evidence; this package does not submit trades, waivers, draft picks or messages.

Preserve historical knowability: an old season label is not evidence that data was available at that time. Failed refreshes must preserve the last successful snapshot without relabeling its freshness. Compare identities within their provider namespace. Research reports bind to exact rules, season, own team and method version.

Run both unittest suites for material changes; use CONTRIBUTING.md for commands and installed-package checks. Keep optional analytics separate from the lightweight retrieval path. Test rule changes, January rollover, co-ownership, missing team identity, stale state, partial failures and incomplete forecasts.
