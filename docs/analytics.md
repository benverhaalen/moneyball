# Research and conditional decision experiments

The `lab` commands add a versioned research warehouse, professional forecast adapters, historical measurement tools, legal-lineup optimization, and complete-league draft/move experiments. They produce inspectable inputs and conditional comparisons. **A simulated championship percentage is not a calibrated probability or a demonstrated edge.** More simulation draws reduce sampling error; they do not repair missing data, incorrect mechanics, or a weak opponent model.

All player-level data, hypotheses, private league evidence, and experiment results live under gitignored `.moneyball/`. This document describes the software and its limits. The retained research map and source-access log are local artifacts, not dependencies that need to be committed.

## Start with current rules and the data inventory

Run commands from the repository root. Output is JSON; errors are JSON on stderr with a nonzero exit code.

```sh
python3 -m moneyball context --fresh
python3 -m moneyball view settings
python3 -m moneyball view drafts
python3 -m moneyball lab init
python3 -m moneyball lab status
python3 -m moneyball lab sources
python3 -m moneyball lab alerts --limit 10
```

`context --fresh` refreshes the league evidence. It does not refresh professional forecasts. `lab sources` reports acquisition and source-specific permission/refresh metadata; `lab status` lists published partitions and quarantine counts. Always inspect source timestamps and failures before using a derived artifact.

The current draft adapter is deliberately specific: twelve teams, a 28-round snake without third-round reversal, and the verified ten-slot starting lineup. It derives the manager's draft slot from observed ownership, checks contiguous observed picks, and refuses unsupported traded startup picks or changed mechanics. The exact scoring dictionary is hashed and passed through every forecast partition. A label such as “dynasty,” “PPR,” or “superflex” is insufficient configuration.

## Acquire and inspect evidence

```sh
# Historical event statistics, rosters, identifiers and schedules.
python3 -m moneyball lab ingest --seasons 2018 2019 2020 2021 2022 2023 2024 2025 2026

# Provider observations: event forecasts, tagged ADP and market values.
python3 -m moneyball lab providers --season 2026 --weeks 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18

# Optional larger observations. Narrow seasons/datasets for a first run.
python3 -m moneyball lab advanced --datasets weekly_rosters depth_charts injuries --seasons 2025 2026
python3 -m moneyball lab advanced --datasets pbp --seasons 2025
python3 -m moneyball lab ingest --datasets snap_counts --seasons 2025
python3 -m moneyball lab opportunities --seasons 2025
python3 -m moneyball lab depth --season 2026 --positions QB --limit 20

# A bounded local read; season labels do not imply historical knowability.
python3 -m moneyball lab query --dataset projections --source sleeper_rotowire --positions QB --seasons 2026 --limit 8
python3 -m moneyball lab query --dataset consensus_values --seasons 2026 --limit 8
python3 -m moneyball lab query --dataset weekly --seasons 2025 --cutoff 2026-09-07T12:00:00Z --limit 8
```

Use `--offline` to reuse already acquired bytes without HTTP. Missing cached objects are reported as failures. Re-running ingestion resumes from cached/unchanged partitions. `--fresh` requests eligible network refreshes; it does not override provider rights restrictions or the FantasyCalc minimum cache period.

`advanced` supports play-by-play, weekly rosters, depth charts, injury reports, participation, FTN charting, and NGS datasets. `snap_counts` belongs to `ingest`. NGS currently uses the available base-R interpreter to decode the official `.rds` release; the rest of the substrate uses Python's standard library. No model-based EPA/WP columns or betting lines enter the cleaned play-by-play feature table. Full source bytes remain in the raw layer for audit. `opportunities` counts observed events; it does not establish that opportunity metrics predict future production.

### Source roles and access boundaries

| Source | Role in this project | Refresh and interpretation |
|---|---|---|
| Sleeper documented `/v1` API | League configuration, rosters, draft state, player IDs/eligibility | Public, noncommercial read access documented by [Sleeper](https://docs.sleeper.com/). League polling and daily player-directory limits remain separate from projection access. |
| Previously acquired Sleeper responses labeled `company=rotowire` | Native projected events and separately tagged ADP | Undocumented projection endpoint is **cached-only by default**, pending written permission under [Sleeper's terms](https://sleeper.com/terms). Existing observations may support private analysis; `--fresh` does not enable recurring collection. |
| [FantasyPros](https://www.fantasypros.com/nfl/projections/) public pages | A second professional consensus forecast source where readable | Parse only accessible rows. Registration gates and paid APIs are not bypassed. Acquired annual totals cannot be treated as weekly forecasts; player overlap alone is not common weekly support. [Terms](https://www.fantasypros.com/about/legal/). |
| [FantasyCalc](https://fantasycalc.com/) | Market-value observations, never point forecasts | Use documented endpoints; cache at least one hour, preferably daily. Personal noncommercial support, visible attribution and no material substitute, subject to [official API documentation](https://fantasycalc.com/api-docs) and [terms](https://fantasycalc.com/terms-of-usage). |
| [DynastyProcess data](https://github.com/dynastyprocess/data) | IDs and expert-rank-derived values, separated by format | Current files and pinned historical blobs are distinct observations. Repository licensing does not establish rights to every upstream input. Unsigned commit dates do not alone prove when historical information was public. |
| [nflverse](https://github.com/nflverse/nflverse-data) / [nflreadr loaders](https://github.com/nflverse/nflreadr/tree/main/R) | Raw outcomes, rosters, schedules and optional advanced observations | Current mutable files are acquired now, including files named for old seasons. Record dataset-specific licensing and source changes. Some third-party datasets have different attribution/share-alike requirements. |

KeepTheCut automation is not enabled. Consumer subscriptions do not establish bulk/API rights, and no paid provider account, key, or purchase is required by the implemented path. The private provider dossier records investigated alternatives, access failures, prices observed during research, and unresolved permissions. Check it and current official terms before expanding acquisition.

## Storage and point-in-time rules

The existing league cache is `.moneyball/data.sqlite3`. The separate research store is:

```text
.moneyball/lab/
  warehouse.sqlite3          source registry, receipts, immutable batches, lineage
  raw/sha256/<hash>.gz       content-addressed original acquired bytes
  derived/                  JSON/CSV models, projections, diagnostics and experiments
  alerts.jsonl              visible acquisition/validation problems
  research-events.jsonl     append-only hypothesis/experiment decisions
  last-refresh.json         complete per-source refresh receipt
```

Each cleaned row carries `_provenance`: source, raw receipt, batch, partition, observed time, available time, publication evidence and content hash. Cleaned partitions publish atomically after validation; malformed/duplicate/invalid data is quarantined with raw bytes retained. A failed partition preserves the prior valid partition. An unchanged cleaned batch is reused, while network receipts still document checks. This prevents a partial download from becoming a new complete dataset.

Three times answer different questions:

| Time | Meaning | Permitted use |
|---|---|---|
| Event time | When the game, transaction or measured event happened | Outcome windows and ordering. It does not establish when the datum was knowable. |
| Observed time | When this system acquired the bytes | Conservative availability default for current/mutable sources. |
| Verified publication time | When these exact bytes were demonstrably public | Earlier feature availability only with explicit verified evidence tied to the raw content hash. |

HTTP `Last-Modified`, a row's update timestamp, a season label, or a Git commit date does not automatically justify backdating. A historical forecast fetched today remains unavailable at an earlier cutoff unless the archival evidence proves otherwise. The selected player-directory copy is explicitly marked as a reserialized local subset, not original wire bytes; its original cache retrieval time is retained. An older cutoff gets an archived metadata revision or missing metadata, never today's age/injury/ID corrections silently applied to the past.

`Warehouse.query(..., cutoff=...)` selects the latest eligible revision of each source partition and checks provenance. `system_asof` additionally asks what this system had actually acquired by a given time. The `features`, `outcomes` and `exploratory` purpose labels do not bypass an explicit cutoff. `record_artifact(..., purpose='features')` rejects inputs available after its declared cutoff. Use timezone-aware ISO timestamps such as `2026-09-07T12:00:00Z`.

The CLI currently accepts `--cutoff` only for `lab query`. Current-decision commands reject it rather than pretend to replay a historical information set. A successful current model build does not create a retrospective forecast archive.

## Build forecasts and measurements

```sh
python3 -m moneyball lab build --season 2026
python3 -m moneyball lab mispricing --season 2026
```

`build` transforms locally stored evidence without HTTP. It copies the observed league configuration into lineage, fits the historical model using earlier seasons, scores forecast events using the exact league dictionary, and saves a build receipt and input-batch IDs. Artifact names and dimensions are returned in JSON. Typical outputs include current model/projection JSON, reliability curves/CSV, role-change measurements and a correlation matrix. Inspect each artifact's definitions and limitations; existence is not validation.

Production and demand stay separate:

- Professional weekly event forecasts supply expected points after league rescoring. Published provider fantasy-point totals are not substituted for the scoring adapter. Season totals and `gp` are not divided into fabricated weekly forecasts.
- A player's legal eligibility comes from fantasy positions, not just the NFL roster label. Team aliases such as `LAR`/`LA` are canonicalized before schedule joins.
- Missing weekly forecasts remain missing. The current draft heuristic averages known forecast contributions over scheduled playing weeks, explicitly treating unknown contributions as zero for that heuristic. Strict simulation can reject missing weeks; `zero` and `carry` are sensitivity assumptions, never observed forecasts.
- `adp_dynasty_2qb` is a tagged demand scenario input. Two-QB demand is not automatically identical to this superflex league's demand or exact scoring. One-QB ADP, expert ranks and market values remain separate formats/units. ADP does not estimate production or establish these eleven managers' preferences.
- Historical output residuals provide an uncalibrated distribution around professional means. Availability with no evidenced denominator remains unknown. The code does not apply an invented second injury haircut to an already unconditional professional center.

`mispricing` exports source disagreements and leaves unestablished championship value/cause fields empty. Rank or value disagreement does not establish a price error. A trade claim also needs an executable counterparty package and a full-league before/after evaluation.

### Programmatic research methods

Some deeper measurements are library functions rather than CLI actions. They can be composed against warehouse queries without new network access:

| Module/API | Current output | What it does not prove |
|---|---|---|
| `modeling.score_stats`, `fit_model`, `project` | Exact event scoring; measured linear-credibility fallback; professional-centered scenarios | Calibrated future distributions or incremental value over professional forecasts |
| `measure_reliability`, `measure_role_changes`, `measure_variance_bins` | Window/cohort diagnostics, role-change contrasts and variance estimates | A universal stabilization threshold, causal mechanism or validated trading signal |
| `build_participation_panel` | Explicit roster/schedule/stat/snap denominator with unknown/absence distinctions | Injury diagnosis; a missing statistics row alone is not inactivity |
| `register_evaluation`, `evaluate_model` | Prespecified train/holdout comparison using declared information availability | Historical live performance when original feature vintages are missing |
| `register_source_ablation`, `evaluate_source_ablation` | Incremental source comparison on matching player/season/week support | Independent evidence from overlapping annual and weekly data, or a quality ranking before outcomes arrive |
| `horizon.fit_horizon`, `transition_for_player` | Exploratory annual output transitions, partially pooled by measured cohorts | Medical career survival, calibrated rookie breakout odds, or an optimal multiyear roster policy |

The horizon model requires an explicit complete-statistics-snapshot contract and completed schedule coverage before it codes an absent next-year event total as zero recorded output. Zero recorded output is not retirement. Positive-base ratios omit zero-base breakout cases; novice transfer, cohort choice, upper-tail treatment and professional-baseline measurement differences remain assumptions to test. Do not call a frozen-roster projection a trading/development policy.

## Compare decisions with the whole league present

```sh
# Small first experiment; then compare prespecified demand/rival assumptions.
python3 -m moneyball lab draft --draft-draws 4 --draws 100 --years 1 --seed 17 --noise 8 --rivals adp
python3 -m moneyball lab draft --draft-draws 4 --draws 100 --years 1 --seed 17 --noise 8 --rivals lineup
python3 -m moneyball lab draft --draft-draws 4 --draws 100 --years 1 --seed 17 --noise 20 --rivals mixed

# Multiyear sensitivity, with previously built support/transition artifacts.
python3 -m moneyball lab draft --draft-draws 4 --draws 100 --years 3 --horizon transition --distribution entropy --rivals lineup --missing-forecast zero

# Optional actual available Sleeper player IDs; placeholders are not valid IDs.
python3 -m moneyball lab draft --candidates PLAYER_ID_A PLAYER_ID_B --draft-draws 4 --draws 100
```

Online `draft` and `move` refresh the live league snapshot before evaluation; `--offline` explicitly uses the stored snapshot. The professional projection can still be older and its timestamp/age is reported separately. A draft experiment carries forward every manager's actual observed picks, then completes all twelve rosters. Changing our choice changes later available players and rival holdings. Replaying the old opponent transcript unchanged would not be a valid counterfactual.

Policies include a format-ADP reference, immediate marginal-lineup selection, bounded `lookahead`, and explicit QB-count/early-QB hypotheses. The QB policies are experiments, not endorsed positional rules. Lookahead compares at most24 current choices, simulates intervening rival choices, and evaluates a greedy legal lineup after the next own turn. Four independent planning demand scenarios are shared across candidate branches. They do not expose the simulator's actual private future preference shocks. Observed holdings are used; the ADP prior is not yet updated from revealed picks.

Forced candidates are selected at the next unobserved own pick if available; the rest uses lookahead. Availability frequency is reported. `first_pick_frequencies` refers to the original first pick, while `next_unobserved_pick_frequencies` describes the current decision after a prefix exists.

Completed rosters feed paired Monte Carlo evaluation under identical random trajectories within the comparison. Pre-outcome expected values choose legal, unique lineups; realized scores do not choose the lineup. Pairwise results then generate standings and a bracket winner. Title probabilities across all managers sum to one in each season. A bilateral trade can help both participants by harming other teams, so “total league title equity” cannot be increased above one.

The output distinguishes simulation standard error from model confidence. The regular-season schedule fallback, bracket mapping, missing-week policy, correlation construction, roster cuts/taxi heuristic and future-state assumptions are reported. Unverified rule enums remain explicit assumptions. More completed drafts and score draws do not resolve those assumptions.

`--horizon frozen` repeats the current production baseline; `--horizon transition` requires `derived/horizon-production.json` and applies sampled empirical annual multipliers to the entire score process. Holdings and current transition cohorts remain fixed, zero production is absorbing in this multiplicative approximation, and the current bye calendar is repeated. Future lineups use expected decline rather than seeing realized annual shocks. These are sensitivity scenarios with no future intake, trades or adaptive replacement policy. The finite objective reports expected championship count and probability of at least one title; it assumes no invented discount rate.

`--distribution residual` shifts measured residuals around the professional center. `--distribution entropy` instead requires `derived/marginal-support.json` and reweights historical score support to match the requested mean. The constrained reweighting uses [Hainmueller's entropy-balancing mathematics (2012)](https://www.mit.edu/~jhainm/Paper/eb.pdf); his causal-inference evidence does not validate football distributions. Observed support excludes unseen extremes, cohort indexing remains a transfer assumption, and Gaussian copula loadings do not guarantee matching Pearson correlations. Both optional artifacts are checked for scoring and training-season compatibility; a missing artifact fails loudly. `--missing-forecast reject`, `zero`, and `carry` expose the missing-week decision rather than hiding it.

### Hypothetical roster moves

Save a local JSON input, using actual team/player IDs:

```json
{"transfers":[
  {"from_team":"1","to_team":"2","players":["PLAYER_A"]},
  {"from_team":"2","to_team":"1","players":["PLAYER_B"]}
]}
```

```sh
python3 -m moneyball lab move --input .moneyball/proposed-move.json --years 1 --draws 500
```

A free-agent alternative uses `{"team":"1","add":["PLAYER_C"],"drop":["PLAYER_A"]}`. Predraft empty rosters require `lab draft` or an explicitly supplied hypothetical `--state` containing the full league. Ownership and ordinary-capacity checks reject invalid changes. IR/taxi eligibility, lock constraints and excess holdings must be resolved before interpreting operational legality.

This is a **local what-if transformation**. No Sleeper action, message or offer is sent. Future-pick packages are refused until draft-rights and future policy states are implemented; the current model does not assign invented pick prices. Acceptance probability, review timing and veto rules are not modeled by a player transfer alone.

## Research discipline and external methods

The retained private map records exact reading status and searches for each structural analogy. The sources below were read in that research; they motivate methods rather than validate this implementation or imply that their empirical effect sizes transfer.

| Structural problem | Verified source and transferable method | Cheap rejection test |
|---|---|---|
| Assign scarce flexible slots | Van Mieghem, [*Investment Strategies for Flexible Resources* (1998)](https://www.kellogg.northwestern.edu/faculty/VanMieghem/htm/Flex_MS.pdf): allocate capacity after uncertainty under legal substitution | Compare assignment decisions against a prespecified additive reference; exact assignment arithmetic alone does not establish a useful strategic gain. |
| Pool short noisy histories | Bühlmann, [*Experience Rating and Credibility* (1967)](https://www.casact.org/sites/default/files/database/astin_vol4no3_199.pdf): measured within/between variation determines linear credibility | On untouched future windows, compare unpooled, cohort and credibility forecasts; discard complexity that loses. |
| Buy now or risk a next-turn stockout | Budish and Cantillon, [*The Multi-unit Assignment Problem* (2012)](https://ericbudish.org/wp-content/uploads/2022/03/multi_unit_assignment_problem.pdf): distinguish preference from run-out time | Freeze next-turn survival predictions and score actual outcomes; do not claim a large-market guarantee in a twelve-manager draft. |
| Extend a decision horizon cheaply | Bertsekas, [2015 dynamic-programming lecture9](https://www.mit.edu/~dimitrib/DP_Slides_2015.pdf): limited lookahead with a base policy | Compare independent evaluation seeds and stronger rival policies; longer lookahead can lose with a poor terminal heuristic. |
| Avoid selecting favorable model error | Smith and Winkler, [*The Optimizer's Curse* (2006)](https://gwern.net/doc/statistics/decision/2006-smith.pdf); Cawley and Talbot, [model-selection bias (2010)](https://www.jmlr.org/papers/volume11/cawley10a/cawley10a.pdf) | Register candidates, assumptions and holdouts before results; include every tried variant in the comparison family. |
| Score distributions honestly | Gneiting and Raftery, [*Strictly Proper Scoring Rules, Prediction, and Estimation* (2007)](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf): Brier score and CRPS | Preserve forecast vintages and compare proper scores on aligned future outcomes; annual rank overlap cannot substitute for weekly forecast testing. |
| Convert pairwise games into a terminal winner | Bettisworth, Jordan and Stamatakis, [*Phylourny* (2023)](https://link.springer.com/article/10.1007/s11222-023-10246-y): elimination-tree probability propagation | Check symmetry, deterministic dominance and title-probability conservation before estimating a player effect. |

`lab init` creates a hypothesis ledger with mechanic, falsification criterion, comparison family and explicit planning judgments. Its priority scores are ordinal effort judgments, not measured effect sizes. Keep preregistration and results separate; changing a hypothesis after seeing a result requires a new logged decision and fresh evaluation evidence. A local hash chain detects edits relative to its retained history; it is not a signed external attestation.

The highest-value next measurements are prospectively archived forecasts, manager demand/survival predictions, explicit participation denominators, and matched source comparisons. Test data additions by ablation. If outcomes or trusted vintages do not exist, state that the test is blocked; do not upgrade a current-data fit into a historical result.

## Refresh jobs, operation and tests

### Live draft evaluation

```sh
python3 -m moneyball lab live --watch-live --budget-seconds 40 --candidate-limit 6 --live-inner-draws 8 --years 3 --distribution entropy --horizon transition
```

Open `http://127.0.0.1:8765`. A separate thread polls documented draft-pick endpoints every five seconds. Availability and opponent roster needs update immediately; forward availability estimates appear before the slower championship comparison. New picks invalidate prior recommendations and cancel the obsolete calculation. The board suppresses actionable choices on a stale feed, outside the manager's turn, or outside the platform's drafting state. The process and Mac must remain running. It never sends a selection or offer.

`--candidates ID ID ...` supplies up to twelve explicit alternatives. Otherwise a transparent shortlist combines format-matched demand and legal-lineup contribution; excluded players are not proved inferior. `--max-blocks`, `--live-inner-draws`, and `--budget-seconds` control computation. The budget is checked between balanced paired blocks, so a block can overrun it. Omitting `--watch-live` runs once and rechecks the draft before returning.

`moneyball/opponents.py` conditions choices on availability and pre-choice rosters, maintaining a partially pooled mixture of unvalidated behavioral hypotheses. Four early opponent choices are four observations, not the size of the available player pool. Simulated choices never update the fitted model. Full next-choice forecasts can be archived, but a later locally discovered pick does not itself prove the prediction preceded public availability; prospective scoring requires independently checked ordering.

`moneyball/uncertainty.py` supplies simultaneous confidence sequences over a predeclared candidate family. One complete draft with multiple inner season draws contributes one independent outer block. Increasing inner draws does not create more independent opponent histories. The displayed bounds measure Monte Carlo integration error under a fixed model, not professional-forecast calibration or the truth of manager preferences. A provisional minimax-regret choice can be shown at the deadline; small samples generally remain statistically unresolved.

Private live artifacts are in `.moneyball/lab/live/`: `latest.json`, immutable observations, forecast receipts and decision results. Each decision retains its prefix, samples, seed, input batches and model fingerprints. The extended method, sample-size and market-data explanation is `.moneyball/research/live-draft-and-uncertainty.md`.

`moneyball/market_distribution.py` provides an optional inspected-schema adapter for two-sided sportsbook quotes. It makes de-vig, monotonicity and tail-support assumptions explicit and returns distribution/mean identification bounds, not fabricated confidence intervals. Actual player props still require an authorized feed. The first retrospective game-total coverage measurement is separate from the fantasy simulator and has not been promoted to a forecasting input.

```sh
python3 -m moneyball lab refresh --season 2026
python3 -m moneyball lab schedule --schedule-action status
python3 -m moneyball lab schedule --schedule-action install --interval 3600
python3 -m moneyball lab schedule --schedule-action remove
python3 -m unittest discover -s tests -v
```

The research job is separate from the documented Sleeper league watcher. It refreshes current-season historical files and selected provider partitions; it does not automatically rebuild models or perform actions. Its minimum interval is15minutes, and source-specific cache/permission gates still apply. It runs only while the Mac is awake and the user is logged in. Check `last-refresh.json`, alerts and launchd logs for partial failure. No background LLM or browser is required.

Tests cover content integrity, revisions/cutoffs, quarantine/partial publication, source parsing/scoring, eligibility, team aliases, hidden future manager preferences, observed draft prefixes and simulator identities. The lab integration fixtures are entirely synthetic. These establish software properties; they do not establish calibrated championship predictions, successful trades, a universal stabilization threshold, or a completed optimal multiyear strategy.

Private supporting artifacts are under `.moneyball/research/`: `dynasty-research-map.md`, `strategy-audit.md`, `provider-dossier.md`, `provider-research-log.md`, and `independent-code-review.md`. Extend those alongside new acquisition receipts and preregistered tests, keeping player-specific results out of committed documentation.
