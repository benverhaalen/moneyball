# Drafting together in under a minute

The target is a well-reasoned recommendation within 60 seconds, with the manager making every real selection. Retrieval speed is measured separately from decision quality and total response time. Neither fast code nor a polished explanation establishes that the selection is good.

## What stays fixed

Use the actual the example league configuration and current holdings. Seek championship opportunities this season and over subsequent seasons. ADP describes acquisition timing; professional projected points describe an intermediate contribution. Neither is our dynasty objective. Missing future probabilities remain unknown. A specific prospect pathway may justify a pick, but an incumbent leaving does not guarantee the prospect wins the role. A hoped-for trade is not an assured repair.

The user wants a broad scan of 12–24 available players, followed by about 3–5 ranked, explained alternatives. If a choice is clearly preferable across the reasonable cases we considered, say so. Otherwise describe the conditions under which each choice is best. Do not conceal disagreement inside an arbitrary weighted score or an uncalibrated title percentage.

## The LLM's operating cycle

**After our pick, before the next turn:** verify the public draft receipt. Update a short strategy ledger: actual holdings, current starting-slot coverage, credible future contribution paths, concentrated failure risks, and unresolved acquisitions. Decide which categories of contribution would improve this particular roster. Categories describe functions—usable starter, future successor, protection against a specific failure, cheap developmental option—not mandatory positional or age quotas.

Scan the next 12–24 candidates using observed Sleeper dynasty-superflex ADP, plus explicitly watched candidates outside that window. Remove drafted players. Do not use a points-only filter that hides young uncertain players or a youth-only filter that hides useful current contributors. Retrieve full dossiers only for the serious alternatives. Check the best contrary case and whether evidence is already represented in the professional forecast.

Prepare ordered branches before the clock: A if available under stated conditions; B if A disappears; C if an important continuation disappears. Each branch states current roster effect, future pathway, risk/capacity cost, acquisition timing, strongest alternative, later acquisitions needed, and a reversal trigger. Judge these branches together; a substitute at the same position is not automatically the right fallback.

**As the preceding picks occur:** cheap public-board updates remove choices. Reconsider strategic direction only when an observation changes our case—for example, loss of both targeted current contributors, loss of a required successor, or a roster change. A positional run is observed depletion, not proof that a particular remaining player is valuable or will be selected next.

**At our turn, target budget:**

| Time | Work |
|---|---|
| 0–5 seconds | Verify fresh board, ownership, actual turn and available candidates. |
| 5–15 seconds | Revalidate prepared branches and identify the one changed fact that matters. |
| 15–35 seconds | Resolve the serious comparison; consult a targeted dossier passage if necessary. |
| 35–45 seconds | Present a ranked shortlist with conditions, risks and take-now/wait reasoning. |
| 45–60 seconds | the manager chooses and presses the button. Verify the resulting public pick; update the ledger. |

These are targets, not measured performance promises. Do not start fresh broad web research or a swarm debate on the clock. New medical/roster news is an exception: surface the uncertainty and use a prepared fallback if the fact cannot be resolved in time. Do not auto-select in the real draft.

## What the manager sees

Lead with the recommendation and its confidence, followed by at most a few alternatives:

| Order | Candidate | Why this choice fits us | Better if… | Cost and waiting risk |
|---|---|---|---|---|
| 1 | A | Best supported route for this roster | Named assumptions remain true | What taking A now prevents; what waiting could lose |
| 2 | B | More immediate contribution | Our other holdings cover the later succession need | Stronger now, but additional future replacement required |
| 3 | C | More future upside | The player earns the identified opportunity | More uncertain current usefulness and greater carrying cost |

This is an illustrative format, not three real player recommendations. Explain role uncertainty, injury/absence exposure, weekly variance and future-employment uncertainty separately when they change the choice. Let the manager's risk preference and football judgment choose among a genuine tradeoff; do not pretend those preferences were inferred numerically.

## Avoiding unnecessary reaches

Compare **A now + a plausible later B** against **B now + a plausible later A**, including the loss of A. Use ADP and a rough configurable buffer to describe possible availability, not a calibrated survival percentage. A three-round reach requires an explicit reason that waiting has a worse plausible cost than the alternative acquisition. Conversely, a large ADP discount does not make an unsuitable player valuable to us.

Name realistic substitutes for later acquisitions. If a branch requires a specific player, declare that dependency so losing him invalidates the branch. If several substitutes suffice, prepare separate branches or explain the acceptable substitute set; the initial implementation supports explicit per-branch dependencies, not an optimized assignment over all future rounds.

## Running artifacts

`python3 -m moneyball.draft_room build` compiles all 400 immutable research packets and currently published, integrity-checked dossiers into a local retrieval deck. Unfinished dossiers remain explicitly absent.

`python3 -m moneyball context --fresh` refreshes the public league snapshot. `python3 -m moneyball.draft_room compare ID ...` retrieves up to 24 available candidates. For groups over eight, prose stays out of the broad scan. Use `compare --full ID ...` for the finalists, capped at eight. It preserves provider subtotals separately and does not rank players.

`prepare plan.json` requires written dynasty reasons, a competing choice and declared availability dependencies, then records immutable input references. `check saved-plan.json --ui-observation current-ui.json` verifies the current turn, board freshness, unchanged own roster/rules/strategy, evidence revisions, draft-prefix corrections, and availability. It also requires an independently observed rendered Sleeper prefix, own roster, exact room, current turn and manual mode within 15 seconds. An API-only call cannot return an actionable recommendation: an actual mock showed a new HTTP response containing minutes-old selections. The UI receipt must be captured from the page, never populated from that same API response. It selects the first viable **prepared judgment**, not an algorithmically optimal player. Meaningless but well-formed prose can still pass; the LLM must defend the reasoning.

Current referenced packet/review/evidence files are checked against the compiled deck before live CLI use. New evidence not yet ingested is not magically detected. Rebuild between turns as research changes and perform an explicit final availability/news sweep before the draft.

All decisions and practice records belong in `.moneyball/draft/room/`, separate from immutable evidence and from real roster mutations. The implementation makes no authenticated Sleeper writes.

## Rehearsal acceptance criteria

Use a private Sleeper mock with the actual the example league configuration and observed platform ordering when authenticated access permits it. Separately label local synthetic replay tests. A mock's computer selections test the workflow under that ordering; they do not validate forecasts of the eleven humans.

Time the whole LLM response, not merely the lookup. Include ordinary picks, an obvious available choice, an ambiguous current-versus-future decision, a target disappearing immediately before our turn, two prepared candidates disappearing, and an invalidated later acquisition. Preserve every attempted case and timeout, not just the fastest examples.

Pass requires a legal available recommendation, current and future reasoning, explicit timing cost, no invented probability, correct reaction to changed dependencies, and output inside the budget. Review pick quality separately by checking whether the recommendation follows from its declared evidence and assumptions. Championship outcomes cannot validate this workflow before the draft.

As of the first local audit, compiling and integrity-checking the 400-player deck took about 1.4–2.1 seconds. A complete six-candidate CLI comparison took 134 milliseconds in one observed run. Synthetic cached operations were faster. These measurements show retrieval headroom. The first actual mock was aborted after a timeout and failed Auto-Pick monitoring: seven deliberate choices and fifteen automatic choices, with two paused cases excluded from timing. One prepared third-choice fallback took about 11 seconds; fresh comparisons took about 76–95 seconds in two recorded cases. No completed draft or general subminute operating reliability is established. [Exact postmortem](../.moneyball/research/mock-rehearsal/postmortem.md) and [parallel practice protocol](mock-lab.md) preserve the failures and next tests.

This operationalizes the existing [evidence and draft strategy](../.moneyball/research/draft-execution-and-player-dossiers.md). Underlying research, uncertainty and source limitations remain in the individual dossiers and research map.
