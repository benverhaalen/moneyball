# A read-only draft observer independent of the chat

`moneyball.draft_advisor_watch` runs as a normal bounded local process. Every two seconds by default it captures the existing Sleeper page, saves the complete observed board, checks current prepared branches, and atomically updates `decision.json` and `readable.md`. It continues when a new player needs comparison or when the chat stops producing messages. It never chooses a fallback, clicks a player, starts/resumes/pauses a draft, opens a tab, navigates, or closes a browser.

This is observation and conditional-advice infrastructure. It still needs valid authored branches from [draft-fastlane.md](draft-fastlane.md). It does not solve an unreviewed decision while the model is absent. A newly visible unanalyzed player becomes `needs_comparison`, with the current board preserved for a fresh reasoning worker.

## Observation-only mode

Use explicit `--observe-only` when a driver has no fastlane-compatible plan yet:

```sh
python3 -m moneyball.draft_advisor_watch --observe-only \
  --setup setup.json --catalog catalog.json --output observer-output \
  --max-duration 1800 --interval 2
```

Only setup and catalog inputs are required or read. No placeholder plan, deck or news registry is needed. The mode preserves complete raw frames, all twelve squads, own roster, board/slate differences and earliest captured own turns. It never calls the advice evaluator. Even on a fresh valid own turn, `mode` is `observe_only`, `advice_evaluated` is false, `ready_to_recommend` is false and `options` is empty. A normal capture has status `observation_only`; failures retain that scope while reporting `capture_failed`. Observation expiry only describes data freshness, never selection permission. This mode also supports `--once --raw-ui-fixture ...` for offline checks.

## Existing-page transport

The installed agent-browser 0.27.0 CLI is unsuitable for the strict no-create failure path: its [main dispatcher](https://github.com/vercel-labs/agent-browser/blob/v0.27.0/cli/src/main.rs) starts missing daemons, and its [action dispatcher](https://github.com/vercel-labs/agent-browser/blob/v0.27.0/cli/src/native/actions.rs) can relaunch a browser or create a page before `evaluate`. Checking that a session exists first does not remove that race.

Therefore the observer uses only [CDP `Runtime.evaluate`](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate) on one explicitly supplied **existing localhost page endpoint**. The expression is fixed, derived from the recorded A2/B2 legacy DOM collectors. It reads cells, player identities, the clock, Auto-Pick, our column and candidate rows; there is no user-supplied JavaScript. A dead endpoint fails rather than launching anything. A disposable helper process has a 4.5-second hard timeout and the evaluate request a 3-second timeout. Closing its debugging connection does not close the page.

Live transport uses the already-installed `websocket-client` 1.9.0 package for the protocol and timeout handling. No package was installed for this work. Offline fixture mode, normalization, caching and publication use the standard library. Other environments need that optional package for live capture; missing it produces a capture failure, with no alternate transport or launch fallback.

**One-time binding belongs to the setup owner, not the observer:** after establishing the explicitly named isolated headless session, `agent-browser --session NAME --json get cdp-url` returns `data.cdpUrl`. Read the returned localhost host/port's `/json/list`, require exactly one page whose URL has the expected Sleeper origin and exact draft pathname, and retain that page's `webSocketDebuggerUrl`. This enumerates existing targets; it does not create one. Do not print other target records, save browser auth state, or read cookies/tokens. Because the CLI itself can launch, obtain the endpoint during the already-authorized setup operation, not as an observer recovery step. A fresh target still needs an actual capture test; an endpoint string alone is not validation.

## Inputs

Supply five current files:

- `setup.json`: the explicit binding below, with the complete observed draft/settings and actual league rules embedded.
- `compiled-plan.json`: a compiled fastlane plan for the next own turn; the worker may replace it atomically after reasoning.
- `deck.json`: the current dossier deck.
- `catalog.json`: the current identity list `[{player_id,name,position,team}, ...]`. It should cover the observed universe, including players beyond the 400 dossiers. Unknown identity remains unknown.
- `registry.json`: `{"observed_at":"timezone-aware ISO timestamp", "news_revision":"reviewed-revision", "sources":{"source-path-or-URL":"current-sha256"}, "unreviewed_fact_ids":[]}`.

```json
{
  "schema_version": 1,
  "session_name": "explicitly-assigned-headless-session",
  "headless": true,
  "mode": "private_mock",
  "own_slot": 5,
  "setup_observed_at": "2026-09-08T21:00:00Z",
  "registry_max_age_seconds": 300,
  "page_websocket_url": "ws://127.0.0.1:PORT/devtools/page/EXACT_EXISTING_TARGET",
  "draft": {"embed": "the complete actual observed setup-api draft object"},
  "league": {"embed": "the actual scoring_settings, roster_positions and settings object"}
}
```

The embedded-object and endpoint placeholders must be replaced, not supplied literally. This experimental browser observer targets slot 5 in a 12-team snake, 28 rounds and 120-second clock; reversal and traded picks are rejected. Bind the setup to the exact observed draft, league and own roster using the current room-binding contract. `real_advisory` remains read-only. Private mode additionally requires private mock metadata. These constraints are separate from the connected agent draft-context tool.

The registry timestamp is the time its **freshness check** occurred, not the publication date of a forecast. A missing, future, or more-than-300-seconds-old check blocks advice; a positive finite setup override changes that TTL. The observer never renews the timestamp. The registry publisher must perform a real check, not copy the old plan's hash/revision and stamp it anew. TTL does not prove that a news source covers every relevant event.

Input files are cached by inode, size, modification and change timestamps. Changed files are reloaded and hashed; fastlane then verifies the bound cards and reference hashes. The registry is the caller-maintained source-revision record, not an automatic news ingester. Its age and input-file receipts stay visible.

## Run and output

```sh
python3 -m moneyball.draft_advisor_watch \
  --setup setup.json --plan compiled-plan.json --deck deck.json \
  --catalog catalog.json --registry registry.json \
  --output observer-output --max-duration 1800 --interval 2
```

Use one observer per output directory. The process does not need a chat or an LLM after launch. It does not start another platform mock. At its configured end or a handled interruption it publishes `observer_stopped` with no advice and retains the last board for recovery.

For a one-shot offline check, add `--once --raw-ui-fixture captured-frame.json`. Old observations remain old and must fail freshness checks. Do not retimestamp a historical frame to call it live evidence. A fixture needs the new collector's top-prefix coverage metadata; the old helpers did not record all those fields.

The collector keeps every drafted/current cell and the first 24 rendered candidate rows, plus any rendered watched rows. The 24 rows are a **review boundary**, not a promise that the relevant decision always fits 24 players. Full board state remains available to a fresh worker. Watched-name matching only selects rows to collect; it does not identify players. Identity requires an actual player ID/image or a unique exact name/position/team match, with duplicate DOM rows and missing teams rejected. Unknown players can trigger comparison without being ranked.

`decision.json` is the authoritative result. It contains:

- `status`: `advice_ready`, `needs_comparison`, `not_ready`, `capture_failed`, or `observer_stopped`.
- `checked_at`, `valid_until`, registry age/TTL and input hashes. Ready advice expires within four seconds and earlier if its observed clock or freshness limit expires.
- `raw_frame_path` and its hash. Raw frames are saved before the result pointer is published.
- `normalized`: complete contiguous prefix, all twelve squads, exact own roster, current pick/clock, candidate identity evidence and blockers.
- `diff`: added picks, a revised-prefix flag, and new/lost candidate IDs.
- `first_own_turn_observations`: earliest captured active own-turn timestamps, not invented exact turn-onset times.
- `result`: the full fastlane output. New `provisional_options`, if present, remain explicitly non-recommendations and are not printed as advice in `readable.md`. Their candidate controls are checked too.

Readers must inspect readiness **and expiry**. A killed observer cannot erase an already-written file, so a timestamp past `valid_until` is never current advice. `readable.md` is a convenience view; each file is atomically replaced, but the files are not a multi-file database transaction. Use the final `decision.json` pointer/hash for consistent machine consumption. No cached advice survives a caught capture error as ready. A retained earlier frame is explicitly marked non-current.

## Verification and limits

Synthetic tests cover complete board normalization, identity collisions, missing rows, exact room/roster, stale/paused/Auto-Pick state, changed picks, novel candidates, source-registry TTL, capture failures and atomic publication. Observation-only mode must never advise or load advisory inputs.

These tests do not establish a live browser-to-recommendation pass. A separately authorized mock run must verify the real collector, endpoint binding and complete observation-to-readable-advice flow. Retrieval benchmarks are distinct from end-to-end decision quality.
