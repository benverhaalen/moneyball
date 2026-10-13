# From evidence to a dynasty decision

Moneyball lets a coding agent reason deeply about players, preserve those judgments and compute their consequences for real legal rosters. It does not require experiments before a conditional judgment is useful. It does require that evidence, interpretation and numerical assumptions remain distinguishable.

## Try the whole journey

```sh
moneyball assess demo
moneyball skill --reference player-assessment
moneyball assess references
```

The offline demo uses eight invented players and three worlds. A trade costs Harbor one current-week point, gains nine if a receiver expands his role next year, and loses eight if he does not. Summit's roster is also recomputed. Removing the receiver's supporting claim creates a visible forecast gap, without rewriting the assessment. These exact results are regression-tested; they demonstrate accounting, not football forecasting.

For a connected league:

```sh
moneyball --home /private/moneyball context ALIAS --fresh
moneyball --home /private/moneyball assess context ALIAS --ids PLAYER1 PLAYER2
moneyball --home /private/moneyball assess save ALIAS RESEARCH_ID assessment.json
moneyball --home /private/moneyball assess compare ALIAS RESEARCH_ID worlds.json
moneyball --home /private/moneyball assess read ALIAS RESEARCH_ID --disable-claims role-expansion
```

Refresh forecasts separately for the actual decision period. Context uses the existing evidence service and optional named pipeline datasets; it does not silently scrape every source. The demo emits complete `example_assessment` and `example_specification` objects as starting templates. All personal evidence remains in the configured private store.

MCP tools are `player_assessment_context`, `save_player_assessment`, `read_player_assessment`, `compare_assessment_worlds`, and `assessment_calculator`. Read `moneyball://research/player-assessment` for the host reasoning protocol, and `moneyball://research/repositories` for the retained repository catalog.

## What an assessment preserves

Eight facets cover ability, opportunity, availability, environment, development, contracts, market and roster fit. A missing facet remains visibly empty. Claims include:

```json
{
  "id": "role-expansion",
  "player_id": "PLAYER1",
  "facet": "opportunity",
  "kind": "forecast",
  "use": "conditional",
  "confidence": "low",
  "text": "A larger receiving role is plausible if the incumbent departs.",
  "reasoning": "Execution evidence supports this job, but the vacancy is unresolved.",
  "countercase": "The team could retain the incumbent or acquire someone else.",
  "reversal_condition": "A new competing starter arrives or deployment contracts.",
  "horizon": "Next season",
  "source_ids": ["dated-report"],
  "depends_on": ["execution-evidence"],
  "mechanism_ids": ["volume_share"]
}
```

Sources record URL, observation time, scope, optional expiry and origin group. Dependencies form an acyclic graph. `rely` and `conditional` permit a selected claim to drive arithmetic; `explore` and `set_aside` preserve information without doing so. Confidence is separate from use. No label certifies truth or supplies an invented probability.

Repeated source origins produce dependence warnings, not automatic rejection. Expired or disabled claims propagate through dependencies to affected projection gaps. Revisions are immutable; historical reads use local acquisition time. Rule, season, own-team or ownership changes report the changed bindings and require a reviewed assessment before comparison. Context is bounded to twelve players per packet; an assembled assessment may cover up to 600 players and 5,000 claims.

## Build scenarios from mechanisms

Each world names a season, representative week, condition and player projections. Supply either component statistics or team opportunity inputs, plus availability and claim drivers. Every nonzero scoring component must be supplied or explicitly listed as zero. Missing interceptions, fumbles or bonuses do not disappear from the score.

The implemented calculators provide:

| Mechanism | Computation | What remains judgment |
| --- | --- | --- |
| Team volume and allocation | Dropbacks × route share × targets per route × conversion; analogous rush/pass branches | Future volume, role, efficiency and joint consistency |
| Shrinkage | Sample-weighted estimate and explicit prior strength | Appropriate prior, exposure unit and calibrated strength |
| Role persistence | Propagate a supplied finite-state transition matrix | Transitions, time step, dwell behavior and covariate changes |
| Availability mixture | Zero-if-absent mean and variance | Absence probability, partial return, replacement and dependence |

Teammates with explicit NFL-team identities must agree on totals and cannot allocate more attempts/targets than the team total. This check cannot establish that those totals or shares are accurate. Alternative correlated worlds can represent a departure simultaneously changing volume, efficiency and competition.

The repository catalog retains 34 references with their specific jobs, limitations and rights. A faulty benchmark does not erase a useful decomposition; a useful decomposition does not validate the benchmark. Integration status distinguishes existing pipelines, small calculators, reasoning protocols and reference-only leads. Fitted LaMa models, tracking models and third-party injury predictors are **not** installed or reproduced by adding them to the catalog. No restricted implementation or data is bundled.

## Decision and limits

A specification includes `hold`, named trade proposals and an explicit future-management assumption. Moneyball recomputes both teams, legal slot assignments, capacity and drops in each world. A compact decision matrix accompanies the detailed results. The agent then explains which action serves the championship objective, the sacrifice this year, the future benefit required, and the price/condition at which holding wins.

Worlds are unweighted stress cases, not independent samples or inferred probabilities. Do not average them into an expected outcome. Per-player variance remains conditional; no independent-player assumption is silently used to sum team variance. Projection means are representative-week lineup proxies, not total player values or championship odds.

Future scenarios presently freeze roster, reserve/taxi membership and eligibility unless the specified trade/drop changes them. They do not model adaptive waiver policies, rookie intake, aging, retirement or future promotion costs automatically. Future retained rosters require dynasty; redraft and keeper horizons beyond the current season are rejected until retention and costs are explicit. Best-ball and nonlinear scoring need richer outcome models. Current status is not carried into another period as a permanent assumption.

New scenario inputs are recorded now and cannot be backdated into a historical evaluation. Saved assessments support as-of reads. Provider or host publication claims do not override local acquisition time. The separate experimental championship simulator remains available through the legacy analytics tools; its outputs are not presented as calibrated probabilities by this workflow.

## Resume and portfolio evidence

The meaningful engineering demonstration is a complete CLI/MCP path from exact league context to a versioned claim graph, source-dependency pruning, and bilateral roster comparisons across explicit future conditions. Tests cover temporal binding, missing components, shared allocation, scenario reversals, invalid trades and installed-package access. A useful portfolio claim describes those implemented capabilities. Winning championships, finding undervalued players, predicting injuries and outperforming consensus require separate outcome evidence.
