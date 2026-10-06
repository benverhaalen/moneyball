# Conditional recommendations without another reasoning round trip

`moneyball.draft_fastlane` checks already-authored recommendations against a fresh board. It does not discover a strategy, estimate player utility, infer new relative preferences, call an LLM, read a browser, or send a Sleeper write. The user still makes the real selection. Mock drivers retain their existing immediate pre-click and receipt checks.

Author the likely branches **between turns**. At the turn, a local evaluation can return up to three surviving options in the author's declared order, each with its exact text, confidence, seven reasons, and source references. When a hard state or evidence guard fails, no option text is returned. When the **only** blocker is an unreviewed newly visible candidate, a separate provisional display can preserve otherwise-valid authored branches while the recommendation gate remains closed.

## Library contract

```python
from moneyball.draft_fastlane import compile_plan, evaluate

# Once, after analyzing the candidate slate and continuation alternatives:
plan = compile_plan(authored, deck, prepared_board, prepared_at=preparation_epoch)

# Repeatedly, using fresh observed input, without another LLM/network call here:
result = evaluate(
    plan, current_deck, current_board, rendered_ui,
    now=current_epoch,
    current_news_revision=news_revision,
    current_source_hashes=source_hash_registry,
    visible_slate_ids=all_current_top_visible_ids,
    slate_scope="complete_top_visible_slate",
    unreviewed_fact_ids=[],
)
if result["ready_to_recommend"]:
    texts = [option["exact_user_text"] for option in result["options"]]
else:
    reasons_to_review = result["problems"]
    # Optional separate display only; never route these into recommendation or
    # selection handling. Show each display_label alongside its authored text.
    provisional_branches = result["provisional_options"]
```

`deck` is the existing `draft_room` deck, with `policy` and `cards`. `prepared_board` and `current_board` use the existing `draft_room.board_state` shape. Preparation targets its **next own pick**, binds its exact own roster, own slot, draft/rules/policy, ordered taken prefix, and hashes every analyzed card. A later board may extend that prefix as other teams select; it may not rewrite it. An own selection requires a new plan for the next turn.

`rendered_ui` uses the existing `draft_room.live_check` contract:

```json
{
  "source": "sleeper_rendered_ui",
  "observed_at": 1788901460.0,
  "draft_id": "explicitly-observed-draft-id",
  "taken_player_ids": ["complete", "ordered", "observed", "prefix"],
  "own_roster": ["exact", "observed", "own", "ids"],
  "current_pick": 20,
  "on_clock": true,
  "auto_pick": false
}
```

The words in the example ID lists are schema placeholders, not valid player identities. Real inputs use canonical Sleeper ID strings. Both board and rendered timestamps must be finite epoch seconds, no more than 15 seconds old and not in the future. Normalized UI values must come from an actual current rendered observation, not be copied from the public API. The checker cannot authenticate assertions about how an input was collected.

For private mock practice, add `private_mock_id`, `mock_ui`, and `player_catalog` to `evaluate`. These reuse `mock_lab.verify_pick` for the **first option's** exact allowed room, active clock, board/own-column identities, candidate identity and enabled draft button. That raw-UI schema includes timezone-aware ISO timestamps and supports the existing unique name/position/team catalog join. Other options still require their own immediate control checks if chosen. The real draft is rejected by the private mock guard. No click permission is returned in either mode: `ready_to_execute` and `writes_to_sleeper` remain false.

### Provisional display is a separate output contract

`provisional_options` is normally empty. It may contain up to three surviving authored branches only when `problems == ["novel_slider_needs_review"]` after **all** applicable board, rendered-UI, source, news, plan-integrity and branch-dependency checks. In this case `ready_to_recommend` remains false, `needs_reasoning` remains true, and `options` remains empty. A changed source, stale board, wrong room, Auto-Pick, invalid prefix, unreviewed fact, failed continuation with no valid fallback, or any other additional blocker suppresses provisional output too.

Each provisional entry retains `branch_id`, `player_id`, `exact_user_text`, `confidence`, `reasons`, `source_refs`, `live_mentions`, and `precedence_conditions`. It uses **`author_rank`**, not the ready option's `rank`, and adds:

```json
{
  "comparison_status": "incomplete_visible_slate_review",
  "is_recommendation": false,
  "display_label": "Provisional authored branch; the visible-slate comparison is incomplete. This is not a best-player claim, recommendation, or permission to select."
}
```

Render that label with any displayed authored text. Do not silently merge these records into `options`, describe their first item as the best player, or use them to execute a selection. The ordering is preserved author judgment within the old analyzed set; the unseen challenger has not been compared. Stale branch text is never rewritten or resurrected. An invalid branch may be rejected while a separately authored valid fallback appears provisionally.

In private mock mode, provisional display checks **every** displayed candidate through the raw-UI guard and exposes `mock_provisional_control_checks` as `{player_id, control}` records. Any failed candidate check suppresses all provisional output. The ordinary ready path retains its existing first-option check and `mock_selected_control_check`. These are display/validation boundaries, not click permissions. Without private mock inputs, the existing normalized rendered-UI contract applies; neither mode authenticates a caller's assertion that a capture or news sweep really occurred.

## Authored schema

The following is a structural example, not a player ranking or draft recommendation. IDs, sources, text and reasoning must be replaced with actual analyzed material.

```json
{
  "schema_version": 1,
  "target_pick": 20,
  "news_revision": "last-reviewed-news-revision",
  "ordering_rationale": "Explain why the following branch order is valid under its declared conditions.",
  "analyzed_player_ids": ["101", "102", "103", "104", "105"],
  "watched_player_ids": ["101", "102", "103", "104", "105"],
  "branches": [
    {
      "branch_id": "candidate-a-with-later-role-options",
      "player_id": "101",
      "alternative_id": "102",
      "exact_user_text": "Exact previously written recommendation for A. At least one member of the analyzed later-role group remains available.",
      "confidence": "Author's explicit confidence and its limitations.",
      "precedence_conditions": "This preference over subsequent branches requires the declared continuation and unchanged roster/evidence.",
      "live_mentions_complete": true,
      "live_mentions": ["101"],
      "requires_all_available": [],
      "requires_any_available": [
        {"group_id": "acceptable-later-role", "player_ids": ["103", "104", "105"], "min_count": 1}
      ],
      "requires_drafted": [],
      "reasons": {
        "current_roster_effect": "State the specific effect on this actual current roster.",
        "future_pathway": "State the future role mechanism and what remains unknown.",
        "continuation": "State the complete plausible roster continuation after this pick.",
        "risk_and_capacity": "State the opportunity cost and overlapping roster capacity risks.",
        "acquisition_timing": "State what availability assumption makes this order useful.",
        "why_over_alternative": "Compare with the strongest explicitly analyzed competing completion.",
        "reversal_trigger": "State an observation that would overturn the preference."
      },
      "source_refs": [
        {"path": "actual-review-path", "sha256": "replace-with-actual-64-character-lowercase-sha256"}
      ]
    }
  ]
}
```

Rules for authors:

- Every branch candidate, comparative alternative and availability dependency must belong to `analyzed_player_ids`. Watched IDs are an explicit subset; all analyzed-card hashes are checked, so a changed continuation dossier cannot escape because it is not the current first choice.
- `live_mentions` exhaustively identifies **draft targets and actionable alternatives** named anywhere in the branch's recommendation/reasons. If any one is drafted, the whole branch becomes invalid. No text is rewritten to delete names. Factual references to already-owned players or NFL teammates are not live availability dependencies. The declaration is a semantic obligation for the author/reviewer; the module does not pretend a name scanner understands prose.
- A later-role group with `min_count: 1` survives the loss of one or two of three acceptable targets. Its text should describe that group without asserting that every individual member remains available. If it explicitly names a live target, that target belongs in `live_mentions` and its loss invalidates the whole text even when the group count still passes.
- Write a separate fallback branch when different wording is needed. Several branches may refer to the same chosen player; the first valid one wins. At most three distinct players are returned. Candidate and branch order are supplied by the author, never calculated from projections, ADP, age or a position rule.
- `requires_drafted` expresses an explicit order condition. It means taken on the current board, not injured, unsigned, unavailable in the NFL, or forecast to disappear later.
- `alternative_id` records the analyzed comparative candidate for existing preparation validation. It is not automatically a live dependency: a fallback may remain valid after that competing candidate is drafted. Any prose still presenting that candidate as available must declare it in `live_mentions`.
- Optional `comparison_bundles` has `chosen_then_later`, `alternative_then_later`, and `claims_bundle_total_advantage`. Identical completed membership produces a warning: A-then-B versus B-then-A cannot differ because of that same bundle's total. An explicit claim of such an advantage is rejected. Acquisition order, survival and different future conditions may still differ; the module does not estimate them.

## What causes a return to reasoning

The visible slate must be the complete **top-visible candidate list** or the full available slate, not the previous shortlist. An available ID outside the analyzed set triggers `novel_slider_needs_review`. This detects a coverage gap; it does not claim the player is better. A stale visible list containing taken players suppresses both ready and provisional output. Use the actual capture boundary, rather than redefining the slate to hide novelty.

`analyzed_player_ids` is an **author declaration**, not machine proof of analysis. Compilation currently checks distinct deck membership and binds card hashes; it does not verify reading depth, comparison quality, or a per-player analysis receipt. Having 400 indexed dossiers does not mean 400 players have been analyzed for this roster. Declaring all 400 analyzed merely to suppress novelty would be a false declaration, not a completed comparison. This narrow provisional-display change does not implement the broader coverage-receipt and ordered-slate proposals in the [adversarial review](draft-context-adversarial-review.md).

Other blockers distinguish a depleted live target, a continuation group below its minimum, a rewritten board prefix, another own roster/turn, changed rules/policy/dossier/reference, new unreviewed facts, wrong room, automatic mode, or stale rendered input. Failed branches remain in `rejected_branches` with IDs and conditions, **without their stale recommendation text**. Unchanged prepared reasoning is reused; all 400 case papers need not be reread.

`current_source_hashes` maps every authored source path/URL to its current locally verified content hash. Missing entries block. Maintain that registry when local publications/source snapshots change; do not simply echo the plan's old hashes and call that a freshness check. Update `news_revision` whenever the monitored evidence inbox changes. Passing an old deck, old registry and old news revision cannot reveal an uncollected real-world update. Acquisition/monitoring remains the caller's responsibility and is outside the measured fast path.

## CLI and measured cost

```sh
python3 -m moneyball.draft_fastlane compile authored.json \
  --deck .moneyball/draft/room/deck.json --board preparation-board.json \
  --output compiled-plan.json

python3 -m moneyball.draft_fastlane evaluate compiled-plan.json \
  --deck .moneyball/draft/room/deck.json --board fresh-board.json \
  --ui fresh-rendered-ui.json --slate complete-visible-slate.json \
  --source-hashes current-source-hashes.json --news-revision reviewed-inbox-revision
```

Compilation verifies the production deck content hash; evaluation checks the bound analyzed cards instead of rehashing the entire 400-player deck. The CLI reports file-load and computation time together. Keep the deck in memory for repeated library calls.

Measured on this Mac, Python 3.11.7: 24 actual dossier cards totaling about 792KB, 24 synthetic authored branches, and 200 evaluations per condition:

| Condition | Median | 95th percentile | Maximum |
|---|---:|---:|---:|
| Own pick 332, 331 players taken | 4.68ms | 4.91ms | 5.58ms |
| Completed board, 336 taken; correctly blocked | 4.61ms | 5.09ms | 61.33ms |

The 7.1MB deck loaded in 36.11ms and library compilation took 10.89ms in that run. A separate CLI smoke test, including file reads and its full-deck integrity check, measured 231.91ms for compilation and 33.84ms for evaluation; evaluation correctly rejected the deliberately old fixture. Those CLI measurements exclude Python process startup. The completed-board outlier is retained, not discarded. Receipts and explicitly arbitrary benchmark fixtures are in `.moneyball/draft/fastlane-benchmark/`; they must never be used as strategy plans.

This removes another model call, repeated prose composition, repeated full dossier reading, and unnecessary full-deck rehashing from unchanged branches. It does **not** remove fresh browser observation, genuine new reasoning, current-news acquisition, user comprehension, clicking or receipt confirmation. These runtime measurements say nothing about decision quality or an end-to-end pick-time guarantee.

Validation command: `python3 -m unittest tests.test_draft_fastlane tests.test_draft_room tests.test_mock_lab tests.test_mock_ui_guard`. The initial integrated suite passed 57 tests, including realistic sniping, interchangeable substitutes, stale names, unseen candidates, changed evidence, exact roster/prefix checks, private paused/wrong-room UI, and the identical-bundle arithmetic trap.

The provisional-display regression cases add a sole-novelty positive case, verbatim author-order/copy checks, rejected-branch and depleted-continuation cases, combined novelty plus hard failures, and private mock checks for a disabled/missing second candidate, paused/expired clock, wrong room and Auto-Pick. They validate output separation and guards; they do not establish source freshness, analyst coverage, player quality, or draft-time resilience.
