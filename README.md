# Moneyball

League-aware fantasy football strategy for your coding agent. Connect your Sleeper or ESPN leagues, keep their rules and rosters in a local evidence store, and ask for draft, lineup, waiver and trade advice grounded in the league you actually play.

Moneyball brings its data pipelines and draft research together with **Art of the Deal's trade and waiver evaluation** in one public project. Your agent does the research and reasoning; the package retrieves evidence, preserves provenance, checks rule bindings and compares legal roster alternatives.

> “It's Tuesday night. Should I add anyone? Compare the best available additions with holding my roster, include the required drop, and explain which assumption would change your answer.”

## Install and try it

Python 3.11 or later:

Check `python3 --version` first. If that command selects an older system Python, use your installed Python 3.11 or later executable to create the virtual environment.

```sh
git clone https://github.com/benverhaalen/moneyball.git
cd moneyball
python3 -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -e '.[agent]'
moneyball-agent demo
moneyball-agent --help
```

The demo uses synthetic data, an isolated temporary store and no account or network. Add `analytics` to the extra list when using the experimental NumPy/SciPy lab: `'.[agent,analytics]'`. Some advanced data imports require R; the dataset catalog explains which ones.

## Use it in your agent

Add the local stdio server to your coding agent's MCP configuration. Replace the executable and data paths with absolute paths on your machine; on Windows use `.venv\\Scripts\\moneyball-mcp.exe`. Your client's configuration location varies.

```json
{
  "mcpServers": {
    "moneyball": {
      "command": "/absolute/path/to/moneyball/.venv/bin/moneyball-mcp",
      "env": {"ART_OF_DEAL_HOME": "/absolute/path/to/private/moneyball-data"}
    }
  }
}
```

Then ask your agent:

> “Connect my Sleeper account by username, show my leagues for this season, and connect the ones I choose. Identify my team in each league. Complete Moneyball's strategy research for their exact rules, and tell me which evidence is still missing.”

Sleeper's public read API needs a username, **no password or API token**. Discovery identifies leagues; you choose which to connect. ESPN connections use a league ID or supported league URL, season and team selection. Public leagues need no credentials; private leagues need your ESPN session configured outside chat and the repository. There is no ESPN OAuth flow or automatic account-wide league discovery. See [account setup and the decision workflow](docs/agent-strategy.md).

The server includes the strategy skill in its instructions and exposes these resources:

- `moneyball://skills/strategy` — the complete host workflow.
- `moneyball://research/method` — cross-domain strategy discovery and evaluation.
- `moneyball://research/trades` — Art of the Deal's trade method.

A connection saves a rule-bound research request. Your agent investigates and saves the research; connection itself does not run a model. Agents without MCP can use `moneyball-agent skill` and the JSON CLI. `moneyball agent ...` is an alias for the same CLI.

## What you can do

| Journey | Evidence and mechanics | Boundaries |
| --- | --- | --- |
| Connect leagues | Sleeper discovery; explicit Sleeper/ESPN connections and own-team selection | No account-wide ESPN discovery; credentials stay local |
| Draft live | Connected Sleeper draft board, picks, ownership and change/freshness checks | ESPN draft history, without a live clock guarantee; no pick submission |
| Set a lineup | Rule-compatible assignments from dated, matching-period forecasts | Conditional on supplied forecasts; check player locks and late news |
| Work waivers | Available-player evidence, add/drop and ordered claim alternatives, holding baseline | Provider eligibility and clearing-time gaps remain explicit; no claim submission |
| Curate trades | Complete legal roster substitutions, bilateral effects, drops, pick evidence and outside options | Agent explains who benefits, each side's tradeoffs and why a balanced proposal is worth considering; no calibrated title odds |
| Research strategy | Reusable policies for draft, lineup, waiver and trade bound to rules/team/season | Report validation checks structure and binding, not predictive edge |
| Inspect data | Dataset catalog, bounded ingestion/query, provenance and historical-availability filters | Dataset-specific coverage/rights; raw history is not a forecast |

A useful answer identifies the league and decision deadline, refreshes relevant evidence, compares feasible alternatives, explains the decisive assumptions and shows what would reverse the recommendation. Missing projections or unclear rules remain visible instead of becoming zeroes.

## Research rather than slogans

Moneyball's research method maps league decisions to underlying problems such as scarce-slot allocation, substitutability, uncertain future service, auction incentives and the value of waiting. It looks for mechanisms in other domains, checks their assumptions against observed league rules, and retains counterexamples and rejected transfers. Art of the Deal evaluates the whole post-trade roster for both managers, including capacity costs and feasible alternatives.

Read [the research method and reference decisions](docs/research.md), [the portable strategy workflow](docs/agent-strategy.md), and [architecture and reliability](docs/architecture.md). These methods are research hypotheses and conditional decision tools, not a claim of demonstrated competitive advantage.

## Data and advanced tools

The local service stores private league evidence on your machine. The older `moneyball` CLI also provides a Sleeper snapshot substrate and experimental `lab` tooling. These stores remain separate unless explicitly configured; there is no automatic migration of existing caches. [Data pipelines](docs/data-pipelines.md) explains access, provenance and reuse.

The NFL has released **selected tracking datasets** for the Big Data Bowl. That is different from opening all Next Gen Stats tracking data. Public nflverse datasets include play-by-play and other observations with source-specific coverage and licenses. See [data sources and limits](docs/data-pipelines.md).

Advanced guides: [analytics](docs/analytics.md), [draft copilot](docs/draft-copilot.md), [draft context](docs/draft-context.md), [fast local comparisons](docs/draft-fastlane.md), [player dossiers](docs/draft-dossiers.md), [dynasty evaluation](docs/dynasty-evaluation.md), and [mock experiments](docs/mock-lab.md). The research lab's fixed-format draft experiments are distinct from connected league context; experimental simulation percentages are conditional model outputs.

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, packaging and synthetic fixtures, and [SECURITY.md](SECURITY.md) for safe reporting. Code is MIT licensed; upstream data and service access have separate terms, attribution and licensing. See [third-party notices](THIRD_PARTY_NOTICES.md).
