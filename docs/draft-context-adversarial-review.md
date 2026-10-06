# Adversarial review: bounded live-draft context

Historical offline review from September 8, 2026. The private example deck is not shipped. Findings below describe that review snapshot and must be rechecked against current code; they are not a current release verdict. This review covers `docs/live-draft-driver.md`, `moneyball/draft_fastlane.py`, `moneyball/draft_room.py`, their tests, the frozen 400-card deck, and the player-packet/review schemas. It does not operate a browser, change a roster, rank players, or claim an optimal draft policy.

## Verdict

The architecture has a sound separation between immutable evidence, authored judgment, and fresh board verification. It correctly refuses to let the fast evaluator invent a ranking, rewrite prose, click a real pick, accept a revised draft prefix, or treat an API timestamp as rendered-board freshness.

Its candidate-boundary guard is the main failure. `draft_fastlane.evaluate` blocks on every visible ID outside `analyzed_player_ids` (`draft_fastlane.py:217–220`). Yet compilation defines “analyzed” only as a distinct ID present in the deck (`:56–63`); it requires no per-player analysis receipt. An author can therefore make the blocker disappear by declaring all 400 IDs analyzed. Compilation then hashes every declared card (`:138`), so any unrelated card revision can invalidate every branch through `draft_room.check`. The guard is simultaneously overbroad, bypassable, and an incentive for context bloat.

A durable live system should keep all 400 dossiers externally addressable while carrying only a small decision frontier in working context. Novel visible players need cheap, explicit triage before full reasoning. Branches should bind to the evidence and assumptions they actually use, not every player ever scanned.

## What already works

- Board and UI checks are independent enough to catch wrong room, wrong turn, Auto-Pick, noncontiguous prefixes, revised prefixes, stale observations, and roster/rules changes.
- Prepared output is verbatim authored judgment. The evaluator neither scores players nor silently repairs a stale recommendation.
- Named availability dependencies distinguish “all required,” “at least N of a group,” and “must already be drafted.” This is more expressive than one brittle shortlist.
- The deck is a useful external retrieval store. It preserves full reviews, packet hashes, provider components, source conditioning, coverage, contracts, and explicit null title deltas for all 400 cards.
- The documentation already says a captured slate is not proof of analysis and that missing data must not become zero. The implementation needs stronger data structures to make those statements survive handoff and compaction.

## Failure modes

### 1. The novel-player guard has the wrong unit of review

The evaluator compares the visible slate as a set against every ID declared analyzed. One newly visible player blocks all options, whether the player moved into the view because of a filter, weak late-round ADP, a status-driven platform reorder, or genuine decision relevance. Conversely, marking all 400 analyzed prevents the guard from ever detecting novelty even if only 24 were actually read.

The implementation also discards visible order. A material reorder among already analyzed IDs does not trigger anything because only set membership is checked. `slate_scope` is a caller assertion with two accepted strings; the plan does not bind the slider/filter/sort identity, window size, visible rank, or capture provenance. Membership is therefore over-sensitive while order and UI selection state are under-sensitive.

`watched_player_ids` does not repair this. Compilation requires watched IDs to be a subset of analyzed IDs (`draft_fastlane.py:57–59`), then the evaluator never consults the watched set. A watched player outside the visible window creates no trigger, and adding many watched IDs to analyzed merely expands the hash/invalidation surface.

The comment in `_branch_failures` correctly revokes prose that names a drafted player, but `live_mentions_complete` is only an author attestation. Compilation does not parse or structurally render `exact_user_text`. A branch can mention an alternative as still available, omit it from `live_mentions`, and remain eligible after that alternative is drafted.

### 2. Bounded context is documented but not represented

`draft_room.compare` caps a scan at 24 and full review at eight. `draft_fastlane.compile_plan` places no cap on `analyzed_player_ids` and allows up to 72 fully authored branches. The current benchmark's 24-branch authored plan is about 55 KB and its compiled plan about 93 KB. The frozen deck is 7.1 MB and contains about 2.0 million characters of full-review prose. Loading all of that into a fresh evaluator would defeat the intended context boundary even though local evaluation itself is fast.

The benchmark also demonstrates that the broad compact output is not especially compact: a 24-card scan serializes to about 68 KB. It contains no dossier excerpts at that width. Eight cards with navigation excerpts serialize to about 42 KB, and eight full reviews to about 72 KB. These are manageable retrieval artifacts on disk, but poor default handoff payloads.

The plan lacks a durable strategic ledger. There is no structured field for legal service gaps, ordinary/taxi/IR commitments, open replenishment obligations, risk clusters, rejected assumptions, last processed prefix, or unresolved evidence. Those facts can exist in free-text reason fields, but compaction can preserve their words without preserving their operational status.

### 3. Compression loses missingness before it loses numbers

The deck card retains forecast `stats`, `unreported_scoring_fields`, source URL, conditioning, and timestamp. `draft_room.compare` reduces this to source key, provider, `observed_core_subtotal`, conditioning, and timestamp (`draft_room.py:210–211`). It drops the component vector and the explicit missing-field list while retaining the subtotal.

This is material rather than hypothetical. Thirty-eight deck cards have no professional forecast. All 362 cards with a forecast have at least one row whose component audit reports an unreported scoring field. A fresh evaluator can see a precise subtotal after the fact that qualifies it has been removed.

Coverage counts have a similar ambiguity. “Has news,” “has forecasts,” or “review completed” does not mean the decisive question was answered. `compile_plan` accepts a card with no published review because it checks only deck membership. The seven reason fields are validated by a five-word minimum, not by typed evidence status. The architecture distinguishes zero from missing in source storage, but not in the authored-analysis receipt.

### 4. Correlation and functional substitutability remain prose

`requires_any_available` counts surviving player IDs. It does not know whether every member depends on the same NFL vacancy, shares one passing game, covers the same roster function, consumes the same taxi place, or is clustered in one bye/injury path. Three names can satisfy `min_count: 1` while providing little genuine continuation diversity.

The deck contains team and provider identity, but the branch schema has no structured risk clusters or evidence-dependency clusters. Repeated reports derived from one upstream item can look like multiple source references. Multiple provider totals can share exposure assumptions. The mandatory `risk_and_capacity` sentence can describe these concerns, but evaluation cannot detect when later picks make the sentence false.

`comparison_bundles` validates only ID membership and catches identical sets with a false membership-advantage claim. It does not validate acquisition order, capacity placement, legal lineup service, availability, or whether a named later player remains feasible. Those dependencies must be duplicated manually in other fields or remain unchecked prose.

### 5. Fresh board checks do not make old football assumptions fresh

The board and rendered UI expire after 15 seconds. The authored plan itself has no maximum age. `news_revision` is one opaque global string supplied again by the caller; unchanged text does not prove that a status sweep occurred. `unreviewed_fact_ids` is also caller-supplied. Card hashes detect changes already published into the deck, but cannot detect new evidence that was never ingested.

`precedence_conditions` is required prose and is never evaluated. A branch can say “use only if two starting QBs remain available,” lose that condition, and still pass unless the author separately encoded it as a player-ID dependency. Roster equality similarly verifies held IDs, not intended ordinary/taxi/IR placement or future capacity obligations. A fresh UI can therefore validate an old strategic state.

### 6. Fixed ADP windows are weakest where the late draft is least orderly

ADP is valuable acquisition timing, but a fixed 24-name window is not a stable challenger set late in a 28-round startup. Platform order can flatten, filters can expose a distant player, rookies can lack strong ADP, and a team-specific service need can make an outside-window position relevant. The current alternatives are either to ignore the outside player before compilation or let any newly visible ID stop the clocked recommendation. Neither response gives a nuanced bounded search.

An unknown player outside the 400-card cohort is handled safely as novelty, but there is no explicit route from unknown identity to a minimal brief and back into the same turn. Under time pressure, that tends toward either an unhelpful hard stop or an unjustified decision to expand the permanent analyzed set.

## Minimal modifications

These changes preserve the current separation between reasoning and evaluation.

1. Replace `analyzed_player_ids` with per-player coverage receipts. Each receipt should include `player_id`, `review_depth` (`identity`, `brief`, or `full`), card/review hash, sections read, evidence cutoff, explicit missingness acknowledgment, and trigger tags. A branch may reference only a player with at least a current brief receipt. Merely existing in the 400-card deck must not count as analyzed.

2. Split novelty into discovery and material escalation. Every newly visible ID receives a bounded identity/position/ADP/status/coverage/missingness brief. Promote it to full comparison when it hits a declared trigger: uncovered roster function, watched-player rule, meaningful visible-order move, material status change, or proximity to the live acquisition window. If the brief cannot be retrieved, record `search_incomplete` and withhold strong comparative language. This is a search policy, not proof that deferred players are inferior.

3. Bind the slate receipt to an ordered capture: slider/filter/sort identity, visible rank, window bounds, player IDs in order, observed time, and capture hash. Detect both new membership and material movement. Do not interpret a search-result view as the default ADP frontier.

4. Hash each branch's dependency closure, not all scanned cards. Keep one deck-manifest hash as provenance, while branch validity depends on selected player, named alternative, continuation group, watched triggers, and the exact evidence passages used. An unrelated card revision should prompt frontier refresh without revoking a logically independent branch.

5. Carry missingness in every compact forecast object: reported components, `unreported_scoring_fields`, games/exposure meaning, horizon including Week 18, and whether provider rows are comparable. Require the branch receipt to state when the comparison has no coherent same-provider inputs. Do not present `observed_core_subtotal` alone.

6. Make invalidators typed. Preserve prose for explanation, but represent minimum legal service, ordinary/taxi/IR capacity, continuation function, required roster role, and risk cluster separately. `requires_any_available` groups should declare the function they substitute for and their shared exposure clusters.

7. Replace the global opaque news token with scoped freshness receipts. At minimum, store per-player status/news revision, last checked time, check source, and `known_absent` versus `not_checked`. A branch should expire when one of its load-bearing status receipts ages out even if the board remains fresh.

8. Create a compact turn-handoff packet. It should contain the exact league/rules and prefix hashes; held IDs with intended capacity placement; legal service gaps; open replenishment duties; exposure clusters; up to three live branches with typed invalidators; a small ordered challenger frontier; watched trigger IDs; unresolved inputs; and paths/hashes to external evidence. Include rejected branches and why they failed so a fresh evaluator does not reconstruct or accidentally revive them. Full dossiers stay on disk and are retrieved by player and section.

The evaluator can remain deterministic: it validates the packet, returns a bounded triage request or still-valid authored options, and never invents a new ordering. A fresh reasoning process handles only the promoted challengers and writes the next immutable decision receipt.

## Falsification scenarios

The revised design should be rejected or refined if it cannot handle these cases without silently widening its claims:

| Scenario | Current failure | Required observation |
|---|---|---|
| One low-ADP ID enters a filtered visible slate | Global `novel_slider_needs_review` block | Compact brief is requested; full reasoning occurs only under a declared trigger, with deferred status recorded |
| Author declares all 400 IDs analyzed without reading them | Novel guard is bypassed | Compilation rejects missing per-player coverage receipts |
| Same 24 IDs reorder sharply | No novelty because membership is unchanged | Ordered-slate receipt records the move and acquisition-timing trigger |
| A candidate has no provider row; another has a numeric subtotal | Missing forecast can disappear behind compact output | Comparison says unresolved and retains missing fields; it does not order zero against a subtotal |
| Three continuation names share one NFL vacancy or team offense | `min_count` still passes | Shared exposure cluster is visible and cannot be called three independent paths |
| A source repeats one upstream report under several URLs | Multiple hashes appear diverse | Evidence-dependency cluster records the common origin |
| Candidate status changes after deck build while `news_revision` is reused | Fresh board can still return ready | Load-bearing status receipt expires or changes and blocks that branch |
| An earlier rookie consumes the last intended taxi place | Held IDs match, free-text plan may remain ready | Structured capacity ledger invalidates the taxi-dependent branch |
| A prose condition becomes false but named players remain available | `precedence_conditions` is not evaluated | Equivalent typed invalidator rejects the branch |
| An unrelated player's dossier changes | Hashing every analyzed card revokes the plan | Branch stays valid; frontier manifest records that a refresh may be due |
| Context is compacted immediately before the turn | Free prose may lose open duties or rejected logic | Fresh evaluator reconstructs exact state from the handoff packet and retrieves only promoted dossiers |
| A player outside the 400-card cohort appears late | No card exists for reasoned comparison | Identity/coverage brief is attempted; unresolved coverage is explicit and no optimality claim is made |
| Exact user text names an unavailable alternative omitted from `live_mentions` | Author attestation can miss it | Structured rendering or a text-to-ID verification receipt prevents stale availability prose |

Success means the system can state what it searched, what it did not search, why a challenger was escalated, which evidence remains missing, and which prepared branch remains valid. It still cannot certify that the chosen player is globally optimal; the design should never imply otherwise.

## Audit of the current `draft_context` implementation

This audit covers the implementation present on September 8, 2026. It treats the context pack as a reasoning handoff, not an executable pick. The module correctly keeps the 400 indexed players unscreened until an explicit screening exists, labels the full numerical packet as unloaded, rejects silent truncation, and binds ledger recovery to the draft, rules, strategy, prefix, referenced card hashes and a caller-supplied news revision. The checkpoint CLI now archives each immutable hash before replacing the latest pointer. The compact forecast in `draft_room.compare` also now retains unreported scoring fields, exposure, source URL and component-vector location. Those fixes remove two failure modes described earlier; they do not certify that a source or dossier was analyzed.

The remaining findings are ordered by risk to a cold-worker handoff.

### P1: a compared candidate can be drafted without invalidating the comparison

`checkpoint` permits `requires_available` to be any subset of `player_ids` (`draft_context.py:111–118`), while `recover` checks new picks only against that subset (`draft_context.py:171–179`). The test fixture demonstrates the unsafe shape: comparison `['2', '3']` declares only `['2']` required. If another team drafts player `3`, the comparison retains no reconsideration flag even though one of its two competing choices has disappeared.

Make availability semantics explicit. A minimal safe version should either require every currently available competing choice in `requires_available`, or invalidate any newly drafted `player_id` that is not already owned. A more expressive schema can separate `available_candidate_ids` from contextual or already-held player IDs. Add the symmetric test in which player `3`, rather than the declared player `2`, is sniped.

### P1: the worker pack drops the reasoned screening record

The durable ledger stores `consider`, `defer`, and `reject_for_this_roster` with `reason` and `reopen_if`, but `pack` removes all recovery screenings (`draft_context.py:251–255`). `full_frontier` contains counts and attention flags, not those records. A cold worker therefore cannot distinguish why a focused, visible, watched, or independently nominated player was deferred or rejected. That defeats the documented goal of preventing accidental revival after compaction.

Include bounded screening receipts in `context` for the focus set and every attention request: status, reason, reopening condition, roster sensitivity, changed-evidence state and card hash. Keep the other hundreds external behind a ledger locator. A test should recover a rejected visible player and assert that its reason survives in the working context, along with whether it now needs rescreening.

### P1: the emitted working pack has no verifiable snapshot identity

The ledger and index are hashed, but the return from `pack` has no generated time, ledger hash, index hash, deck hash, board-prefix binding or pack content hash (`draft_context.py:246–267`). `ready_to_execute: false` prevents it from being mistaken for a click authorization, but it does not let a later worker detect that `working-pack.json` was modified or belongs to an older board after the file was emitted.

Add an envelope containing at least `generated_at`, `ledger_hash`, `index_hash`, `deck_hash`, `board_prefix_hash`, and a `content_hash` over the complete pack. Verify it at the next consumer boundary, and require a fresh reconciliation before authored choices are passed to the fastlane evaluator. Test one-byte pack tampering and reuse after the board prefix advances.

### P2: attention can be acknowledged without entering the bounded comparison

Known visible, watched and challenger IDs become `attention_requests`, but `pack` can still succeed when every such ID is absent from `focus_ids` and has no current screening (`draft_context.py:192–217`, `220–267`). The request is visible, and the pack remains correctly non-executable, but nothing machine-readable says that the handoff is incomplete for comparative language. A cold worker can read the favorite cases and overlook an unresolved challenger in a nearby metadata list.

Return `unresolved_attention_ids` and `comparison_complete_for_attention: false` until every attention ID is either focused, supported by a current screening receipt, or explicitly deferred with a reason and reopening condition. This should gate claims such as “best available” or “strongest alternative”; it need not force all 400 candidates into the packet.

### P2: budget failure lacks a diagnostic for an oversized durable state

Failing whole is safer than truncating, and the character unit is stated accurately. The suggested action always begins with “Narrow focus,” however, even when the authored state alone exceeds the ceiling, as the existing oversized-ledger test does (`draft_context.py:261–265`; `test_draft_context.py:113–118`). In that case reducing one focus case cannot solve the problem.

Report a character breakdown for durable state, recovery, discovery and each case, plus a `minimum_state_characters` measurement. When the minimum state exceeds the ceiling, direct the author to rewrite or externalize a specific state section while retaining the archived predecessor. This makes the budget a debuggable boundary rather than pressure to delete caveats blindly.

Two limits remain explicit rather than defects: the global caller-supplied news revision cannot prove that fresh information was ingested, and one evidence span does not prove that both sides or the best counterargument were supported. The current scope text correctly assigns source completeness and interpretation to the analyst. Before live use, the handoff contract should preserve those limits and avoid treating a successfully built index or pack as proof of analysis, freshness, completeness, or optimality.
