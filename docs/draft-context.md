# Drafting from a changing board with bounded context

The goal is a reasoned dynasty decision in time, including when the board departs from expectations. A list of 24 candidates is a temporary reading window, not a strategy, a complete search, or 24 scripted decisions. The 4.7 ms fastlane measurement times validation of previously authored choices; it does not measure reasoning quality, board capture, an LLM response or a full pick.

We cannot guarantee zero compaction or a correct choice. We can stop making either the complete draft history or an uninterrupted LLM session necessary for recovery. The full-draft timing claim remains unproven: B failed after 21 manual picks; B2 after 26. Both retained source evidence and deliberate decisions for critique.

## Four distinct responsibilities

1. **Observe independently.** A read-only observer captures the actual full board, all squads, our roster, clock and available rows. It writes durable state and changes without sending a full browser dump into the LLM. It neither selects players nor quietly keeps a stale recommendation active. An independent display is needed because a message in this conversation alone cannot guarantee visibility during a model interruption.
2. **Maintain the team's decision state.** After every own pick, save the current strategy, holdings, current starter/coverage obligations, future promotion/replacement obligations, important common failure scenarios, unresolved comparisons and user preferences. These are explicitly authored judgments bound to observed facts. Do not regenerate them by asking an LLM to summarize a long conversation.
3. **Search the available universe.** Reconcile all 400 acquired dossiers against actual picks. The board's visible list, authored watches and an independent challenger each nominate candidates. Players without ADP or projections remain in the searchable universe; unknown players observed on the platform get explicit evidence-gap flags. Search order is not draft order.
4. **Compare a few actual alternatives.** Give a fresh decision worker the durable state, board changes, a small set of full relevant reviews and precise source handles. It should resolve the comparison that could change this pick, retaining the best argument against the favorite. An output must state the current effect, future effect, capacity cost, acquisition condition, confidence and reversal trigger. Publish up to three choices; the manager selects in the real draft.

Fresh workers must receive a compact prompt, not fork the entire conversation. Browser setup, package documentation, debugging, complete event history and 400 full dossiers remain outside the clocked worker. A new worker can begin between turns while the observer and existing reviewed choices persist. This is a lifecycle to test, not a claim that orchestration latency disappears.

## What survives compression

Preserve the claim, its conditions, contrary evidence, source pointer and uncertainty together. For example, preserve “future opportunity could open if the incumbent leaves; this player still must win the role; new competition would reverse the case,” rather than compressing it to “young, high upside.”

The persistent ledger includes:

| State | Why it must survive |
|---|---|
| Exact rules and multi-year objective | Prevent a familiar redraft heuristic replacing our task. |
| Actual holdings and roster obligations | Another reserve is useful only through the legal deployment/availability scenarios it improves. |
| Future promotion and replacement demands | Several individually attractive prospects can require the same future slot or acquisition budget. |
| Failure exposures | Sharing a team is not itself the risk; the shared cause and conditional consequences matter. |
| Open A-now/B-later versus B-now/A-later comparisons | Prevent claiming a better total from identical completed pairs or assuming one later target is guaranteed. |
| Best counterargument and reversal condition | Keep a rejected alternative recoverable when the board or information changes. |
| Consider/defer/reject-for-this-roster records | Distinguish a reasoned exclusion from something we simply have not examined. |
| Evidence and news revisions | A valid old interpretation may become invalid after a correction or role report. |

The code preserves full reviews for focused players and addressable verbatim paragraphs for targeted follow-up. It does **not** certify that a review contains every source nuance or that an analyst used its sources correctly. Detailed forecast components and primary-source packets remain accessible. The worker must request the missing passage if it can change the choice. A stored URL is not evidence that it was read.

`draft_context.pack` has a 42,000-character default working-packet ceiling, measured as characters, not model tokens. This is an operational starting point, not a model-context limit or measured optimum. It refuses an oversized packet instead of cutting off the final caveat. Focus can be narrowed or the ledger deliberately edited with a retained audit history. An ever-growing list of “important” notes is not a solution.

## How the search changes in an unpredictable draft

Every update removes actual picks from the full acquired universe. Every own pick reopens roster-sensitive exclusions. Relevant source changes reopen prior judgments. A snipe invalidates a comparison's dependency, not the player's entire research history. The current code conservatively reopens all comparisons after an own-roster change; more selective dependency tracking needs evidence that it does not miss interactions.

Candidate discovery must use several routes, not only the next platform rows:

- The actual visible board, including an unexpected slider or previously unknown name.
- Players previously researched for a current service gap or coverage scenario.
- Future-role candidates whose necessary conditions and capacity costs are documented.
- A challenger from outside the favored route, selected to expose a missed tradeoff.
- Explicit pages of the remaining unscreened universe, including players with missing ADP or projections.

These nominations are human/LLM research tasks. The index cannot automatically identify the best upside player from a keyword such as “rookie.” The current implementation records the unscreened denominator rather than pretending that building 400 files means all 400 have been compared for this roster.

Later rounds need **broader discovery and narrower final comparisons**. ADP can still describe acquisition timing, but it must not filter away candidates. Before taking another ordinary reserve, ask which actual absence/role scenario brings him into our lineup and what is sacrificed by using the slot. Compare that against the strongest future option and a different coverage job. If those scenarios cannot be priced, state the conditional preference; do not manufacture title deltas.

## Time and interruption protocol

While other teams pick, update the state, scan the next candidate groups and author conditional choices. Do not plan a single predetermined sequence. Keep at least one acceptable route that does not require a particular later player surviving.

When our turn arrives, use the latest board and compare only the changes that can reorder our choices. Rough operating targets are: reconcile immediately, resolve the decisive challenger by about 30 seconds, publish by 45 seconds. These are deadlines for practice, not performance guarantees. Stop expanding research when it would displace a better-supported available decision without enough time to evaluate the newcomer.

If a late snipe destroys the favored route, a valid separately reasoned alternative can remain useful. If an unfamiliar slider appears, display the current reviewed preference as **provisional**, alongside the unresolved challenger; do not label it “best on the board.” The current fastlane intentionally withholds its `ready_to_recommend` result on an unreviewed visible candidate. The display should distinguish incomplete comparison from corrupted/stale board data rather than making both look like a crashed system.

If the evaluator stalls, the observer keeps time-stamped state and previously authored conditions visible. A fresh worker loads one recovery packet. It must reconcile changes and cannot simply reuse the old favorite. Real-draft selection remains with the manager. No system here should turn on Sleeper Auto-Pick to compensate for an interrupted model.

## Working implementation

- [draft_context.py](../moneyball/draft_context.py): complete-universe index, explicit reasoning checkpoints, changed-state recovery, discovery denominator and bounded full-case packets.
- [draft_fastlane.py](../moneyball/draft_fastlane.py): rapidly validates and returns eligible preauthored choices; does not supply their reasoning or rank unfamiliar players.
- [live-draft-driver.md](live-draft-driver.md): compact driver contract and clock discipline.
- [draft-context-adversarial-review.md](draft-context-adversarial-review.md): independent critique and remaining failure cases.

```sh
python3 -m moneyball.draft_context index --output .moneyball/draft/context/index.json
python3 -m moneyball.draft_context checkpoint --authored authored.json --board board.json --output ledger.json
python3 -m moneyball.draft_context pack --index .moneyball/draft/context/index.json --ledger ledger.json --board board.json --news-revision ACTUAL_CURRENT_REVISION --players ID_A ID_B ID_C --output working-pack.json
```

The CLI prints a compact receipt and writes the full artifact. Load `context` for the worker; `full_frontier` is a separate discovery artifact and need not be dumped wholesale into the prompt. Source refresh and current-news revision must come from actual observation, never a freshly assigned timestamp on old data.

## Tests that could disprove this design

Unit tests cover a sniped prerequisite, new own holdings, revised evidence/news, an unknown observed player, missing ADP, wrong room/rules/prefix, retained contrary evidence and budget overflow. These verify software behavior, not superior picks.

The next rehearsal must use shuffled late-round availability, a strong candidate outside the visible shortlist, simultaneous loss of several substitutes, a provider disagreement, a changed injury/role report and a cold worker replacement late in the draft. The evaluator must recover without old chat, keep the original strategy, surface the decisive contrary evidence and publish before the deadline. Independently compare its answer with a slower full-evidence review. Count omitted decisive facts and unjustified preferences as failures even if the player selected later performs well. Also retain unknown/missing-data cases in the denominator.

Only an uninterrupted unpaused full rehearsal can establish end-to-end timing for this architecture. Until then, rapid local retrieval and passing integrity tests are components, not proof of readiness.
