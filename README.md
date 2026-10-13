# Moneyball

Fantasy football evidence and roster comparisons for your AI assistant. Connect a Sleeper or ESPN league, then ask about trades, waivers, lineups, drafts, or a dynasty rebuild.

Moneyball supplies dated league data, reusable strategy research, player evidence, and rule-specific calculations. Your assistant researches uncertain claims and explains the recommendation. You make the moves on the fantasy platform.

> "Find trades that improve my roster. Include the required drops, show why the other team might accept, and compare each offer with keeping my players."

Redraft, keeper, and dynasty decisions use your league's scoring, roster slots, retention rules, and playoff format. Missing forecasts or unclear rules stay visible. Moneyball does not claim calibrated championship odds.

## Install and try a decision

Use Python 3.11 or later. Check `python3 --version` before creating the environment.

```sh
git clone https://github.com/benverhaalen/moneyball.git
cd moneyball
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[agent]'
moneyball decision demo
```

On Windows, activate the environment with `.venv\Scripts\activate`.

The demo runs synthetic decision examples in a temporary store. It needs no account or network. You can inspect alternatives, missing evidence, and saved decision context before connecting a league.

## Connect your assistant

Add the local stdio server to an MCP client such as Codex or Claude Code. Replace the executable and data paths with absolute paths on your machine.

```json
{
	"mcpServers": {
		"moneyball": {
			"command": "/absolute/path/to/moneyball/.venv/bin/moneyball-mcp",
			"env": {
				"MONEYBALL_HOME": "/absolute/path/to/private/moneyball-data"
			}
		}
	}
}
```

On Windows, use the executable at `.venv\Scripts\moneyball-mcp.exe`. Configuration locations depend on the client. A local stdio server does not provide a hosted ChatGPT connection.

Then ask your assistant:

> "Find my Sleeper leagues for this season using my username. Show me the leagues before connecting them, identify my team, and research strategy for their exact rules. Tell me which inputs are missing."

Choose the connection method that fits your league:

| League | Required information |
| --- | --- |
| Sleeper | Your username for discovery, followed by your selected leagues and teams. Public read access needs no password or token. |
| Public ESPN | A supported league URL or league ID, the season, and your team. |
| Private ESPN | The same league information, plus a locally configured ESPN session. Keep credentials outside chat and Git. |

ESPN account-wide discovery and OAuth are not implemented. Follow [the connection and strategy guide](docs/agent-strategy.md) for setup details. Without MCP, run `moneyball skill` for the host instructions and use `moneyball --help` for the JSON CLI.

## Ask about your league

Once connected, ask an ordinary question:

- "Compare this trade with holding. Recompute both lineups and include any cuts."
- "Find this week's waiver moves. Give me ordered claims, drops, and fallbacks."
- "Compare my lineup options using matching-week forecasts and current availability evidence."
- "At this draft pick, compare available players across positions. Show the current-year and future tradeoffs."
- "Help me rebuild. Separate evidence of player ability from evidence of future opportunity."

The assistant follows `prepare_decision → decision_evidence → compare_actions → record_decision → resume_decision`. Moneyball refreshes relevant resources, exposes dated evidence, and checks roster mechanics. Saved decisions retain the consulted inputs, counterarguments, and conditions that would change the answer.

The research workflow imports methods from structurally similar problems outside fantasy. It checks their assumptions against your rules and saves the findings for reuse. Connecting a league creates a research request; it does not automatically run a model or validate a strategy.

## Understand the limits

| Capability | Current boundary |
| --- | --- |
| League context | Sleeper and ESPN are supported. Other platforms require an adapter. |
| Trade and waiver comparisons | Complete roster changes and cuts are checked. Forecast coverage and platform eligibility can remain incomplete. |
| Live draft support | Sleeper board checks and saved decision branches are implemented. Sub-minute assistant performance is unproven. ESPN draft history has no live-clock guarantee. |
| News discovery | Player-specific leads have dated identity checks. A headline or link is not verified reporting. |
| Future scenarios | Supplied assumptions can change role and competition calculations. Development, injury risk, and title probabilities are not automatically calibrated. |
| Historical analysis | Queries preserve local knowability. An old season label does not prove that today's revised data existed then. |

Moneyball does not submit trades, waiver claims, lineup changes, draft picks, or messages. Treat conditional calculations as support for a decision, not as established forecast accuracy.

## Extend the evidence and methods

Connected league tools share one private evidence root, configured by `MONEYBALL_HOME`. Observed-data partitions live under its `lab/` directory. Older stores are preserved rather than silently merged.

Read the guide for the work you want to do:

| Task | Guide |
| --- | --- |
| Configure leagues and reuse strategy | [Agent strategy](docs/agent-strategy.md) |
| Add or inspect data sources, dates, and licensing | [Data pipelines](docs/data-pipelines.md) |
| Build player assessments and conditional future scenarios | [Player assessment](docs/player-assessment.md) |
| Investigate cross-domain strategy methods | [Research](docs/research.md) |
| Understand the service and its remaining milestones | [Architecture](docs/architecture.md), [architecture plan](docs/architecture-plan.md), and [reference decisions](docs/architecture-references.md) |
| Add optional forecast or news inputs | [Forecast challengers](docs/forecast-challengers.md) and [Firecrawl setup](docs/firecrawl-feed.md) |
| Use experimental models | [Analytics](docs/analytics.md) |
| Prepare draft evidence and practice | [Draft copilot](docs/draft-copilot.md), [dossiers](docs/draft-dossiers.md), and [mock experiments](docs/mock-lab.md) |
| Move an older installation | [Migration](docs/migration.md) |

Add the `analytics` extra when using the NumPy/SciPy lab: `python -m pip install -e '.[agent,analytics]'`. Some data imports require R. Check the dataset catalog before ingesting them. Existing league access works without a Firecrawl key.

The canonical connected service lives in `moneyball/league/`. Existing `art_of_the_deal` imports and commands remain compatibility aliases.

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for both test suites, packaging checks, and synthetic fixtures. See [SECURITY.md](SECURITY.md) for reporting security issues.

Code is MIT licensed. Upstream datasets and services have separate terms. Preserve their attribution and consult [third-party notices](THIRD_PARTY_NOTICES.md) before reuse. Keep league snapshots, browser state, credentials, player dossiers, and experiment transcripts out of commits.
