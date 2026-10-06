# Preparing player evidence before the dynasty draft

The dossier command prepares local, inspectable evidence for a candidate. It does not produce a dynasty ranking or replace a team-and-board-specific decision. User-authored research files are optional private inputs and are not shipped with a fresh checkout.

```sh
python3 -m moneyball context --fresh
python3 -m moneyball.draft_dossiers build
python3 -m moneyball.draft_dossiers show "Josh Allen"
python3 -m moneyball.draft_dossiers show "Josh Allen" --full
python3 -m moneyball.draft_dossiers show "Josh Allen" --markdown-file .moneyball/draft/examples/josh-allen.md
```

All stdout is JSON. `show` accepts one exact full name (case insensitive) or Sleeper ID. It reads an indexed SQLite record without parsing the complete player directory or projection bundle. `--full` includes weekly component forecasts and their provenance. `--markdown-file` additionally writes a readable evidence card.

## Inputs and interpretation

`build` reads the local league snapshot, the existing versioned warehouse, and `.moneyball/lab/derived/projection.json`. It makes no network requests and does **not** refresh professional forecasts. The build receipt distinguishes the dossier cutoff, projection cutoff, last league check, and unchanged league snapshot's original observation time. A new build does not make old inputs new.

Optional user-acquired inputs are `.moneyball/research/draft-source-samples/ffopportunity-weekly-2025.json` and `clay-2026-components.json` (the filenames follow the configured season). These research acquisitions are not yet recurring production ingestion jobs. Retain their source receipts and acquisition code.

The resulting `.moneyball/draft/dossiers.sqlite3` contains:

- Current professional component forecasts, with provider and exposure assumptions separate.
- Dated reported health/depth-chart observations, never automatically converted into risk probabilities.
- Prior-season observed offensive statistics; absent observations are not inferred injuries.
- Available ffopportunity diagnostics, re-scored using the actual league's weights. Expected and actual subtotals use identical supported components. Expected lost fumbles and other unsupported events remain absent. These describe opportunities that already happened, not next year's opportunity or a forecast adjustment.
- Mike Clay's published components where exact name, position and explicitly normalized team identify one candidate. Source rankings and source PPR totals are not used to value players. Games and injury conventions remain visible. Unmatched rows are saved, not fuzzily assigned.
- Unestimated individual future paths and injury probabilities as nulls; evidence gaps and the ten-question draft checklist.
- A place for reviewed mechanisms and a separate, initially empty live decision overlay.

The projected candidate universe is a coverage choice, not every ownable NFL player. Players absent from it require explicit expansion of the projection/crosswalk inputs. Missing history must not become a rookie penalty. Multiple provider names do not establish source independence.

## Add reviewed mechanisms without inventing adjustments

Supply a JSON object keyed by Sleeper ID. Each value is a list of evidence records:

```json
{
  "4984": [
    {
      "target": "future NFL assignment",
      "source_url": "https://example.org/the-actual-source-read",
      "known_at": "2026-09-08T05:00:00+00:00",
      "claim": "Replace with a specific sourced observation and its proposed mechanism.",
      "counterevidence": "Replace with the strongest competing mechanism or source limitation.",
      "baseline_overlap": "State whether the current professional projection probably already includes this fact."
    }
  ]
}
```

The example above illustrates schema only; it is not evidence and should not be imported. Use first acquisition time for `known_at` unless a historical vintage has actually been established. A source's old article date does not prove that today's displayed page was available unchanged then.

```sh
python3 -m moneyball.draft_dossiers build --evidence .moneyball/research/draft-source-samples/worked-player-evidence.json
```

Pass the complete evidence file on subsequent builds; omitting it intentionally builds without case evidence. The index is an atomically replaced derived artifact, not the permanent evidence store. Raw acquisitions and curated input files remain the records to preserve. Reviews are retained as evidence; the command does not convert prose into probabilities or add an arbitrary bonus to a professional forecast.

## Decision boundary

Before a pick, refresh the board and squads, retrieve the shortlist's dossiers, and compare feasible roster continuations under common scenarios. Report title-probability changes by year only when a corresponding simulator and inputs support them. A conditional scenario result is not a calibrated forecast. The earlier current-points startup heuristic is not the dynasty draft policy.

This index does not yet supply individualized future probabilities, priced injury forecasts, opponent-roster comparisons, or a completed live recommendation screen. It prepares the evidence those functions require. Do not call it an automatic draft adviser.

## Verification

```sh
python3 -m unittest discover -s tests
```

Tests cover scoring and season mismatch, source/artifact knowability, duplicate identity and forecast rows, safe exact joins, actual/expected component alignment, absent-versus-zero opportunity data, source provenance, evidence requirements, read-only lookup and rendering. They verify transformations, not forecasting superiority. The source audits document provider validation separately.
