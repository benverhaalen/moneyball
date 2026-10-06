# Annual futures board

`moneyball.futures_board` exports all twelve teams' annual championship probabilities from existing `moneyball.simulation.simulate(...)` reports. It accepts the same `{"models": {name: report}}` container used by `moneyball.dynasty_decision`.

It does not generate forecasts, rerun simulations, implement future trades/rookie intake, or estimate empirical confidence. Its job is to preserve supplied results, check their accounting, and make their model assumptions visible.

## Run the existing synthetic example

An optional local fixture `.moneyball/research/dynasty-decision-example.json` contains a synthetic twelve-team simulation, three annual indices and two actions (`hold` and `future`). Its numbers are deliberately artificial software checks, **not real league championship forecasts**. Calendar labels below are supplied for demonstration.

From the repository root:

```sh
python3 -m moneyball.futures_board \
  .moneyball/research/dynasty-decision-example.json \
  --seasons 2026,2027,2028 \
  --future-policy frozen_holdings \
  --horizon-semantics 'Synthetic software fixture: annual indices labeled 2026–2028 for demonstration; frozen holdings and supplied scenario means, not real the example league forecasts.' \
  --output .moneyball/research/example-futures-board.json
```

The command writes the same board as JSON to stdout and, when requested, atomically to `--output`. The output retains `input_kind: synthetic_software_fixture`. Local `.moneyball/` artifacts are gitignored; the synthetic input may therefore be absent in a fresh checkout. `tests/test_futures_board.py` contains an independent small synthetic simulation contract test; this documentation does not imply the local example is a distributed real-data fixture.

## Explicit model descriptions

Calendar seasons and horizon semantics are mandatory. Reports contain annual indices, so the exporter cannot infer that index zero means 2026.

Each model also needs an explicit `future_policy_mode`:

- `frozen_holdings`: the provided model holds existing assets without future roster adaptation.
- `adaptive`: the caller explicitly states that a future action policy was evaluated. This exporter does not certify its implementation.
- `other`: a separately described policy.
- `unknown`: the caller explicitly acknowledges an unresolved policy. It is never inferred as adaptive or calibrated.

The source report must retain a nonempty `assumptions` object and its `future_policy` statement. An `adaptive` descriptor cannot overwrite the current simulator's explicit `frozen holdings` statement. Other free-text contradictions cannot be automatically certified; reviewers must inspect the retained descriptor and assumptions.

For multiple models with different meanings, supply `--descriptors path.json` instead of the uniform policy/horizon flags. The descriptor JSON maps **every input model name** to an object like the following, using the existing synthetic model's actual name:

```json
{
  "fixture": {
    "future_policy_mode": "frozen_holdings",
    "horizon_semantics": "Synthetic three-year software scenario, not real forecasts."
  }
}
```

Python interface:

```python
from moneyball.futures_board import export_board, read_json

payload = read_json(".moneyball/research/dynasty-decision-example.json")
board = export_board(
    payload,
    season_labels=[2026, 2027, 2028],
    model_descriptors={
        "fixture": {
            "future_policy_mode": "frozen_holdings",
            "horizon_semantics": "Synthetic software example; no real player forecasts.",
        }
    },
)
```

This preserves the input; nested output summaries are copied. `read_json` also rejects duplicate JSON object keys rather than silently overwriting a team or model.

## Output contract

`models[name].annual_boards[season][action]` contains twelve rows in consistent team-ID order. Each row retains:

- `championship`: the complete supplied annual summary, including any `monte_carlo_se`, `draws` and `interval_meaning`.
- `delta_from_baseline`: the difference between supplied annual point estimates.
- `paired_delta_championship`: the supplied paired annual summary, or `null` when none was supplied.
- `delta_uncertainty_status`: whether a paired sampling summary was provided or only a point difference is available.

Source aggregate summaries, run metadata, assumptions and explicit descriptors remain available per model. The exporter does not reconstruct probability of at least one title from annual marginals. If the simulator supplied that aggregate, it is retained unchanged in `source_aggregate_summaries`.

`across_model_ranges[season][action]` reports minimum and maximum supplied point estimates and point deltas for each team. These are **unweighted scenario ranges, not confidence intervals or predictive intervals**. Duplicating a model cannot change a range. A single-model range has equal endpoints; that does not imply zero uncertainty. Per-team range endpoints need not sum to one because their extrema can come from different models.

No paired standard error is calculated from marginal standard errors. Without paired annual summaries, the exporter reports point differences only. A baseline-only report is valid and does not require paired components.

## Validation and failure behavior

The default absolute floating tolerance is `1e-8`, with no relative tolerance. The Python API accepts an explicit finite, nonnegative tolerance when source precision requires it; the output records the chosen value.

The exporter requires:

1. The same twelve unique team IDs, actions, baseline and full annual horizon in every model.
2. Every annual championship estimate finite and between zero and one; each model/action/year sums to one.
3. Any paired annual estimates finite and between minus one and one; they sum to zero and match the action-minus-baseline point differences.
4. Paired annual components for either all twelve teams or none within an action. Aggregate-only paired deltas are insufficient.
5. Annual sums matching supplied expected-title totals; first-year probabilities matching supplied current-championship estimates; paired annual sums matching supplied paired expected-title deltas.
6. Explicit horizon/model-policy descriptions. Present Monte Carlo SEs require their source uncertainty-meaning statement; missing uncertainty stays unknown.

Errors fail loudly; CLI failures return exit status 2 and a JSON error on stderr. Missing annual results are never fabricated from total expected titles. Matching action names and team IDs cannot prove that upstream models used identical roster states; the caller is responsible for that experimental contract and should retain upstream state receipts.

Run the material checks with:

```sh
python3 -m unittest tests.test_futures_board
```

The module tests actual synthetic `simulate()` output, annual and paired conservation, missing components, duplicate/mismatched IDs, explicit policy semantics, immutable inputs, scenario-range interpretation and CLI round trips.
