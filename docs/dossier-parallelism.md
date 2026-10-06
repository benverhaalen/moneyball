# Parallel dossier research

The queue accelerates research for the frozen top-400 cohort. It does not turn a
model-written draft into a verified recommendation, or count source packets as
completed individual research. The authoritative completion inventory remains:

```sh
python3 -m moneyball.review_audit --output .moneyball/research/player-reviews/audited-index.json
python3 -m moneyball.player_research status
```

## What the pilot established

The local Claude CLI is callable using existing authentication. Actual probes
returned `claude-sonnet-5` and `claude-fable-5-1`. The controller uses print mode,
structured JSON, and streaming transcripts. It disables browser integration,
customizations and MCP servers. Workers receive only public player evidence in
their own directories and Read/WebSearch/WebFetch tools; they cannot modify the
repository or league. These options were checked against the installed CLI help,
the [official CLI reference](https://code.claude.com/docs/en/cli-usage), and the
[headless usage documentation](https://code.claude.com/docs/en/headless).

The first four Sonnet dossier drafts were rejected. Problems included an
unreadable single-line evidence archive, unsupported quotes, applying a provider's
scoring total instead of the example league scoring, treating an extension's unguaranteed
year as a void year, mixing independently produced weekly and season forecasts,
and deriving roster value from ADP. A second Sonnet reviewer caught several
errors but also endorsed some incorrect interpretations. Therefore, a positive
model review alone is insufficient evidence of quality.

The working division is narrow Sonnet source collection, a stronger dynasty
synthesis pass, deterministic verification, and independent spot review. This is
being evaluated; no throughput or accuracy advantage is claimed before accepted
outputs exist. Rejected drafts and their denominators remain recorded.

## Queue and evidence layout

All runtime data is gitignored under `.moneyball/swarm/`:

- `queue.sqlite`: per-player state, packet hash, attempt count and controller owner.
- `jobs/<player_id>/`: immutable source packet, readable sections and compact brief.
- `jobs/<player_id>/attempt-N/`: prompts, exact CLI events/results, sources, draft,
  critic response and a hash binding of the actual reviewed files.
- `source-cache/`: content-addressed raw responses and URL cache receipts.
- `quality-trial-*`: isolated experiments, excluded from completed reviews.

The original packet remains unchanged. The compact view removes repeated
provenance scaffolding while retaining observed values, sample sizes and missing
data caveats; full sections remain readable. A failed read is not missing data.
Newly fetched evidence has its actual observation timestamp and cannot be used
retroactively in historical evaluation.

```sh
python3 -m moneyball.dossier_swarm seed 181 400
python3 -m moneyball.dossier_swarm status
python3 -m moneyball.dossier_swarm recover
```

`run --workers N --limit M` launches bounded parallel jobs. The initial author
workflow remains available for experiments; the rejected pilot does not justify
running it blindly across the cohort. `retry PLAYER_ID` preserves prior attempts
and supplies prior critique to the next attempt. `recover` only releases jobs
whose owning controller demonstrably no longer exists.

## Publication gate

`publish PLAYER_ID` is separate from generation. It requires a successful
independent review, exact player/packet identity, no unresolved critic issues,
matching source receipts, source-text and raw-response hashes, and a binding
between the final draft and the files the critic actually reviewed. It also
recomputes each acquired provider's observed core scoring subtotal from the
supplied components. Missing fields remain unknown.

Publication stages complete files, installs without overwriting manual work, and
writes the completion manifest last. Repeating an interrupted publication only
resumes byte-identical files owned by that attempt. File presence alone does not
establish completion. These checks detect provenance and arithmetic failures;
they do not prove causal claims, medical forecasts or strategic conclusions.

## Transport and cost records

Successful public URLs are cached for an hour under a cross-process URL lock.
Different raw response versions are retained by content hash. HTTP rate-limit and
temporary server responses receive bounded retries. CLI transient failures also
receive bounded backoff; authentication/billing problems are not solved by
silently changing credentials or buying capacity.

CLI results record model identity, token usage and the CLI's list-price estimate.
That estimate is not a verified subscription charge. No purchase or credential
export is part of this pipeline. There is no claim that 100 simultaneous workers
are better than 12: scale depends on accepted output, source access and rate limits.

## Evaluation rule

Measure accepted, independently checked dossiers per elapsed hour, including
failed drafts, corrections and review time in the denominator. Count material
errors found after model review separately. Source-only extraction throughput
must not be presented as completed-dossier throughput. A calibrated championship
model and empirically calibrated market confidence intervals remain separate,
unfinished research requirements.
