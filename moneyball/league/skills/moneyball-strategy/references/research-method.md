# Moneyball's research method, generalized to a connected league

This transfers the process documented in Moneyball's `docs/analytics.md` and retained research maps, and Art of the Deal's `docs/strategy-research-process.md`. The original league-specific research is not bundled or treated as a new user's evidence.

## Frame the actual problem before selecting an analogy

Freeze observed scoring (including unsupported/nonlinear events), indexed slots, capacities, timing/locks, waiver mechanics, draft format, regular season and playoff structure, retention rights and team identity. Labels do not supply missing rules. The objective is competitive benefit over the actual ownership horizon; summed projected roster points are an intermediate quantity.

Translate bottlenecks out of fantasy vocabulary: heterogeneous resources assigned to scarce service slots; noisy short measurements; sequential allocation where alternatives disappear; delayed information and expiring choices; bilateral exchange with private preferences; finite-horizon competition. Search these mechanisms and direct football methods. Include an older, neglected or structurally distant approach when it could materially change the decision. Search breadth follows unresolved questions, not a source count.

## Inspect, transfer, challenge

Give each reference a job. Read relevant primary material; record full text, excerpt or abstract access, exact inspected scope, actual check time and limitations. A search hit or familiar name is a lead. The installed reference library documents prior reading, not proof that you inspected the original today.

For a candidate, record the structural match, exact activating rule paths, assumptions, deliberate differences, football inputs still needed, failure conditions and a test that distinguishes it from a simpler alternative. Preserve counterevidence and useful rejected candidates with revisit conditions. Synthesize complementary mechanisms without averaging incompatible objectives.

The original research used these starting points; choose and extend them for the actual league:

| Structural problem | Source lead and transferable property | Boundary / discriminating test |
|---|---|---|
| Flexible scoring capacity | Van Mieghem, [flexible resources](https://www.kellogg.northwestern.edu/faculty/VanMieghem/htm/Flex_MS.pdf); Kuhn, [assignment](https://www.math.utoronto.ca/mccann/1855/KuhnNRL55.pdf) | Legal deployment and actual displaced service; compare joint assignment with an additive baseline and named absence states. |
| Sparse noisy measurements | Bühlmann, [credibility](https://www.casact.org/sites/default/files/database/astin_vol4no3_199.pdf) | Cohort pooling is not a universal weight or full outcome distribution; compare raw, cohort and pooled forecasts in untouched future windows, including role changes. |
| Pick now or risk disappearance | Budish–Cantillon, [course allocation](https://ericbudish.org/wp-content/uploads/2022/03/multi_unit_assignment_problem.pdf) | Separate utility from run-out time. Large-market guarantees do not transfer to a small draft; compare candidate-plus-next-turn completions using frozen demand scenarios. |
| Information arriving before a deadline | Flight et al., [value of sample information](https://eprints.whiterose.ac.uk/id/eprint/181245/1/0272989x211045036.pdf) | Charge missed service and lost alternatives. Move information across the deadline to test whether waiting still helps. |
| Future policies and terminal value | Bertsekas, [dynamic programming](https://www.mit.edu/~dimitrib/DP_Slides_2015.pdf) | Longer lookahead can lose under a poor terminal policy. Compare independent evaluation worlds and coherent management on both branches. |
| Optimizing noisy estimates | Cawley–Talbot, [selection bias](https://www.jmlr.org/papers/volume11/cawley10a/cawley10a.pdf) | Candidate selection belongs inside evaluation. Retain attempted variants and untouched evaluation periods. |
| Honest forecast assessment | Gneiting–Raftery, [proper scoring](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf) | Archive forecast vintages and align targets. Rank overlap or simulation sample count does not validate weekly distributions. |

Original negative transfers matter too: financial mean–variance diversification, Kelly stake sizing, and replicating option prices assume objectives or markets that may fail for indivisible, illiquid rosters and finite tournaments. Do not turn them into fantasy rules merely because their vocabulary sounds relevant. Conventional rankings may provide attributed forecasts or evidence about market demand; they do not establish the strategic mechanism.

## Save a usable report

The original map also explored illiquid barter and negotiation menus, strategic queues with continuation value, relative-risk tournaments, delayed adoption with exclusive access, useful lifetime and survivor bias. Treat these as conditional discovery directions: queue strategy needs the real reset/processing rules; a bilateral menu needs differing manager preferences; survivor-conditioned histories cannot supply a universal aging curve. Preserve future capability versus current assignment, replenishment constraints, and coherent production processes rather than attaching an age or bench premium.

For live decisions, transfer the control loop: observe actual picks, update the available set, apply only the next decision, and replan. An unexpected pick changes the state rather than proving a manager model; do not train preferences on simulated choices. Suppress recommendations from obsolete prefixes. Compare disputed opponent/continuation models through explicit failure scenarios rather than hiding them in an average. Candidate-set regret is conditional on the scenarios inspected, not a universal optimum.

Distinguish predictive uncertainty, parameter/model uncertainty and Monte Carlo integration error. More inner simulations reduce numerical error; they do not create more independent draft observations or validate the model. Separate event time, acquisition time and content-bound verified publication time. Preserve exact-scoring hashes and prospectively logged forecast vintages.

Use the `research_key` from the current plan. A report contains:

- `summary`: concise stable strategy and its limits.
- `sources`: objects with unique `id`, `title`, public `url`, `access` (`full_text`, `excerpt`, `abstract`), `inspected_scope`, and timezone-aware `checked_at` or Unix timestamp. Uninspected leads belong in gaps, not supporting evidence.
- `mechanisms`: unique `id`, `domain`, `structural_match`, `transfer`, `rule_paths` (JSON pointers into `league_inputs`), `source_ids`, lists of `assumptions`, `failure_conditions`, `evidence_needed`, `status` (`candidate`, `adopted`, `deferred`, `contradicted`), and `test` with `comparison` and `observable_outcome`. The server attaches observed rule values itself.
- `policies`: exactly `draft`, `lineup`, `waiver`, `trade`; each has `reasoning`, `mechanism_ids`, `evidence_needed`, and `reversal_conditions`. Empty mechanism IDs can explicitly represent an unresolved operation; do not fabricate a transfer to fill it.
- `rejected_transfers`: objects with `mechanism`, `reason`, `revisit_when`; may be empty when none were investigated and rejected.
- `research_gaps`: unresolved questions and unavailable evidence; may be empty only when appropriate. Missing rule fields are attached separately by the server and remain unresolved.

Store one league's research with its rules and identity binding. Reuse it across routine changes. Check applicability again if evidence contradicts a policy; changed rules/method invalidate the binding, while a new roster/news item normally calls for refreshed decision evidence. A saved report records the agent's reasoning, not independently verified reading or predictive merit.

## Evaluate the recommendation, not the prose

Freeze the action, information cutoff, alternative, assumptions and reversal conditions before outcomes. Use a coherent source/vintage/horizon/scoring cohort. Historical replay may use only evidence available then; importing an old forecast today does not backdate its availability. Tests should expose wrong-rule activation, a plausible case where the method should not apply, and a failure that could reverse an action. Distinguish mechanical correctness, forecast accuracy, acquisition feasibility and realized luck. Where outcome evidence is absent, preserve a hypothesis and a blocked test rather than manufacturing a result.
