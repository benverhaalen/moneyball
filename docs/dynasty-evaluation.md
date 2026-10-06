# Dynasty evaluation: operating contract

The evaluator's objective is championship opportunity through time, not an additive player-value score. Current and future legal lineups, available recourse, replacement needs and the strength of the field determine that opportunity. The player's observed age, position, market rank or mean production is an input, never the objective.

## Current capabilities

`moneyball.simulation.simulate` and the optional NumPy implementation return, for each action and team:

- `championship_by_year`: a list of summaries, index zero = current season.
- `paired_delta_championship_by_year`: changes versus the first named state, using paired random trajectories.
- `expected_titles`: expected championship count over the full horizon.
- `at_least_one_title`: probability of one or more titles, measured on joint trajectories.
- Existing current qualification and title summaries.

Each summary uses `estimate`, `monte_carlo_se`, `draws` and `interval_meaning`. The sampling error excludes model uncertainty. Annual championship probabilities sum to expected title count without any year-independence assumption; they do not identify the probability of at least one title.

The current generative model freezes holdings and labels its assumptions. It does not implement future rookie intake, trade execution, learned role changes or optimal future management. Annual production multipliers are sensitivity inputs, not an individualized aging model. New annual summaries do not improve those underlying assumptions.

Conditional variance bins now live inside the saved composite model with scoring, training-data, cutoff and input-batch provenance. Projection rebuilding rejects a missing, altered, differently scored or temporally leaking dependency. A separate untracked variance file is not a runtime source. This repairs reproducibility; it does not establish distributional calibration.

## Compare explicit scenarios without predicting opponent choices

```sh
python3 -m moneyball.dynasty_decision /absolute/path/to/model-reports.json --team 7 --horizons 1,3
```

Input:

```json
{
  "models": {
    "first_assumption_case": "replace this string with a full simulate() report",
    "second_assumption_case": "replace this string with another full report"
  }
}
```

Every report must evaluate the same named actions and baseline across the same number of years. Each team/action needs annual title summaries. Aggregate-only older reports are rejected: annual timing cannot be recovered from them.

Output:

- Each model's annual title probabilities, cumulative expected titles and change versus baseline.
- Range of those changes and whether their direction reverses across supplied models.
- Regret measured against the best evaluated action in the same model and horizon.
- Actions nondominated across model/horizon **point estimates**.
- The source reports' assumptions and the limits of interpretation.
- Original per-action annual/paired Monte Carlo summaries, draw count and seed in `model_evidence`; no new confidence interval is manufactured.

Scenarios are not probability weights. Duplicating a scenario does not change its influence on the frontier/ranges. This tool does not declare statistical dominance or recommend a single scalar tradeoff between current and later titles. A limited scenario set can omit the case that matters; actively search for reversals.

## Information and action contract

1. Refresh actual league rules, ownership and draft board before an actionable decision.
2. Preserve professional forecast components and their known-at dates. An absent component is not zero. In normalized providers, a partial numeric `mean` is explicitly an observed-component subtotal.
3. Evaluate actual before/after holdings jointly; include the acquisition or release forgone.
4. For incomplete drafts, provide explicit feasible completion cases and break-even requirements. Do not pretend a future roster is already owned or infer personal opponent policies by default.
5. Allow lineup changes only using information available before the relevant outcomes. Never use ex-post best-ball selections as ordinary managed-lineup performance.
6. Evaluate future replacement and management assumptions explicitly. A player cannot be replenished by an invented free trade or guaranteed successful rookie.
7. Report data uncertainty, model sensitivity and Monte Carlo sampling error separately.

The local detailed synthesis is `.moneyball/research/dynasty-team-evaluation-framework.md`, with companion primary-source research on capacity, aging/context and projection composition. These private artifacts stay in the gitignored data directory.

A private forecast dependency map can expand the framework beyond future service to scoring, personnel, observation timing, acquisition access, every squad's future choices and competition. Such maps are optional user research, not files shipped by this project. Unavailable or restricted sources remain candidates; an inventory alone demonstrates no predictive edge.

Use the [annual futures-board exporter](futures-board.md) to view all twelve squads by explicitly labeled season from supplied simulation reports. It checks annual probability and delta conservation, retains paired Monte Carlo summaries and shows unweighted model ranges. It requires source assumptions and an explicit future-policy description; it generates no new forecasts. The local example is synthetic. Current frozen-holdings results must not be presented as calibrated adaptive 2028 dynasty futures.

## Validation

```sh
python3 -m unittest tests.test_dynasty_decision tests.test_simulation tests.test_simulation_fast tests.test_providers
```

Tests cover annual championship conservation, annual/count reconciliation, paired identity, timing swaps, source completeness and the actual simulator/frontier interface. They establish software properties. They do not establish football forecast calibration or a repeatable championship edge.
