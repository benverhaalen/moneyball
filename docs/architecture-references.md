# Architectural references and what Moneyball takes from them

Research and implementation review, 2026-10-13. This supplements [the architecture plan](architecture-plan.md). References below were inspected in primary documentation or pinned source; they are not claims of exhaustive coverage or evidence that Moneyball predicts better than another service. Upstream source, licenses, failure reports and acquisition receipts are retained privately for reproducibility. Public links identify the inspected revisions.

## The general project

Moneyball is a **decision workbench over changing, heterogeneous evidence**. A user brings a question; the application resolves the relevant system, assembles dated evidence, checks feasible alternatives, and preserves what informed the human/agent judgment. The host conversation owns that judgment. Fantasy football supplies the rules and calculations, but the architectural problem also appears in financial terminals, clinical decision support, connected-device controllers and reproducible analytical pipelines.

Generalize the contracts, rather than erase domain mechanics:

| Contract | General responsibility | Moneyball specialization |
|---|---|---|
| System binding | Exact identity, rules and decision horizon | Platform, league, season, own team, scoring and retention |
| Evidence resource | Origin, version, observation time, target and access scope | Rosters, forecasts, reports, player claims and markets |
| Preparation | Select what this question needs and disclose missing coverage | Trade, waiver, lineup, draft, rebuild or research context |
| Feasible action | Complete before/after state and constraints | Ownership, slots, cuts, priority, budget and keeper rights |
| Conditional calculation | Effects under explicit inputs and assumptions | Lineup changes, role worlds and joint weekly outcomes |
| Decision receipt | Chosen alternative, countercase, reversals and consulted evidence | Host judgment, recheck and compact continuation capsule |

These are separations inside one Python application, not a proposal for six services. Deterministic accounting belongs in code; unresolved player development and preferences belong in qualified evidence and host reasoning. A language model may interpret a report, but cannot turn missing eligibility or an unknown keeper price into a legal action by sounding confident.

## Inside sports

### StatMuse Fantasy Football Manager: host-led operation

[The inspected skill](https://github.com/statmuse/fantasy-football-manager/blob/bd55f95b04ed2476d84bf2942191ad3c227e03af/SKILL.md) combines sports information with league-specific rules and keeps research and decisions in the user's assistant. Its multi-league workflow is a useful interaction reference: collect reusable player information once, then specialize the consequences to each league. It also separates platform IDs from its information provider's IDs.

**Adopt:** the ordinary-language entry point, exact league settings, shared player research and attributed explanations. **Change:** Moneyball exposes durable structured evidence and mechanical comparisons through a local service. **Exclude:** its default authorization for lineup/waiver changes and its mandatory StatMuse connection. Moneyball does not submit moves, and no single information vendor is required. The skill's fantasy heuristics are not architectural evidence or validated strategy.

**Check:** the same player report can support two league contexts, while scoring, roster constraints and retention produce distinct comparisons. A shared player fact must not silently inherit a personalized trade verdict.

### Sleeper MCP: small connectors with retained state

[The pinned storage implementation](https://github.com/vbudhram/sleeper-mcp/blob/5bc255961afc62174e75cba2f213f3e9243f5382/src/sleeper_mcp/storage.py) is a closer implementation reference than a generic autonomous-agent framework. It provides cache/snapshot machinery around a fantasy connector.

**Adopt:** compact provider access and persisted versions. **Change:** explicit historical knowability, immutable manifests, exact namespace identity and failure state. First/last-seen deduplication alone cannot reproduce every A→B→A transition. Provider caches are useful but do not establish that the draft board was observed after the latest pick.

**Check:** replay ownership changes including removal and return; a failed request must retain the previous successful timestamp.

### ffverse: reusable acquisition and separate simulation components

[ffscrapr's Sleeper adapter](https://github.com/ffverse/ffscrapr/blob/b6990181e125507a1b4642cea6bc9778d9acd6f3/R/sleeper_rosters.R) and [ESPN adapter](https://github.com/ffverse/ffscrapr/blob/b6990181e125507a1b4642cea6bc9778d9acd6f3/R/espn_rosters.R) are source references for equivalent operations with platform-specific details. [ffsimulator's projection stage](https://github.com/ffverse/ffsimulator/blob/9d8bc79354ed79bd9f9ac028c3f3442b271afea5/R/4_projections.R) helps separate acquisition, projection and competition calculation.

**Adopt:** modular source/transform/simulation boundaries. **Do not infer:** repeated simulated seasons represent persistent dynasty development, or that a source's licensing covers every upstream dataset. Keep supplied role transitions and future roster policies explicit. Existing native adapters remain the application owners; an R package is not required merely to reproduce its useful separation.

## Outside sports

### OpenBB: provider contracts instead of one universal forecast

[The pinned fetcher](https://github.com/OpenBB-finance/OpenBB/blob/7c9d4221bdf66965e3f6c7f4dc6ee848073e5ae2/openbb_platform/core/openbb_core/provider/abstract/fetcher.py) separates query transformation, extraction and result transformation. This is a strong fit for a service consuming heterogeneous professional inputs without owning every prediction model.

**Adopt:** normalized envelopes with provider, warnings and retained extras. **Change:** require forecast period, scoring basis, statistic and availability conditioning. A mean conditional on playing, an unconditional mean and a sportsbook threshold are different inputs even if each fits a numeric field. **Exclude:** importing the entire terminal or automatically averaging every provider.

**Check:** mismatched periods and conditional-versus-unconditional forecasts remain separate cohorts or explicit gaps, never a plausible-looking blended score.

### Home Assistant and TanStack Query: refresh is a state machine

[Home Assistant's coordinator](https://github.com/home-assistant/core/blob/8c014f3e4ce2d987a94d16db9e0d7c19dda89b00/homeassistant/helpers/update_coordinator.py) supplies resource-level coordination. [TanStack Query's inspected revision](https://github.com/TanStack/query/tree/0fbbc6213641ef2d7cec7d4bfd2f1044c561dde8) supplies concrete join/cancel/restart distinctions. Its migration history describes imperative refetch missing newer data when an older request was already underway.

**Adopt:** share redundant work; distinguish last successful evidence from the newest attempt. **Change:** expose failure and observation provenance instead of treating an awaited aggregate as universal success. A request finishing after a pick can still contain a board captured before the pick. Ordinary refreshes label joining existing work. Fresh draft reads require a local request-start barrier: older work can finish but cannot satisfy that caller. Provider caching can still return an older board, so this is not a guaranteed origin observation barrier.

**Check:** an older parked draft request cannot satisfy a later strict caller; strict timeout cannot certify its old snapshot. Ordinary callers still share a fetch, an old late response cannot overwrite a newer accepted result, and a failed refresh cannot make old data appear freshly observed. The shared quota policy must also cover distinct resources; deduplicating identical GETs does not enforce a provider-wide rate limit.

### DBOS: stale workers must lose publication authority

[The inspected DBOS revision](https://github.com/dbos-inc/dbos-transact-py/tree/61426cb95bcd749dd18137b4eb483d02b9975d7d) and [merged execution-token change](https://github.com/dbos-inc/dbos-transact-py/pull/862) implement ownership checks alongside checkpoint publication.

**Adopt:** compare the worker's ownership token and publish inside the same writer transaction. **Change:** a small SQLite lease and immutable evidence receipt are enough for the current GET lifecycle. **Defer:** full durable workflow replay. Its default long database waits are unsuitable for a pick deadline.

**Check:** replace or expire the owner while its network request is parked; releasing that request must not let it become the accepted latest evidence.

### Clinical decision support: prepare for the task, return attributed alternatives

[HL7 CDS Hooks 2.0](https://cds-hooks.hl7.org/2.0/) defines workflow-triggered context, prefetch and response cards. Its useful contribution is task-specific preparation at the moment of a decision, not medical authority.

**Adopt:** a small evidence packet with reasons, alternatives, source and expansion handles. **Change:** fantasy has explicit roster counterfactuals and user-selected horizons. The calling host owns the recommendation. **Exclude:** treating a missing report as a reassuring negative finding.

**Check:** partial news coverage produces a visible gap; decisive cards retain their qualifications and a route to the original evidence.

### DVC and SQLite: reproducibility is different from currency

[The inspected DVC revision](https://github.com/treeverse/dvc/tree/56e59829512ff134aa269099a2099587b810b4dd) distinguishes declared dependencies, definitions, outputs and reusable runs. [SQLite isolation](https://sqlite.org/isolation.html) explains why a coherent local view needs an explicit read transaction.

**Adopt:** immutable consumed-input manifests and dependency-aware invalidation. **Change:** host judgments are not deterministic build stages. Equal content can reuse arithmetic, but does not automatically renew a recommendation's currency. Historical replay uses its historical cutoff, while present advice requires current readiness. **Defer:** DVC's full remote-storage/execution machinery and finer semantic invalidation until actual consumed dependencies support it.

**Check:** changed forecasts invalidate a saved comparison even when the roster is unchanged; a concurrent publication between validation and receipt insertion prevents a falsely current receipt. A blocked writer must not backdate newly published evidence. These checks now exist in the local decision/store tests.

The completion audit found another boundary worth preserving: deriving an index from an already-known report is a write, even when the report keeps its original observation date. Dossier indexing now finishes before a decision opens its frozen read. Claim search, expansion and manifests share that read transaction. A concurrent migration test verifies that an index published later cannot enter the earlier snapshot merely because its claims carry older dates. This establishes local SQLite consistency; it cannot make separate remote services observe the same instant.

## Resulting polish

The useful combination is **provider contracts + coordinated refresh + task-bound evidence + complete action accounting + recoverable decision history**. None of the inspected projects supplies this entire fantasy-specific journey unchanged.

The implemented lifecycle is `prepare_decision → decision_evidence → compare_actions → record_decision → resume_decision`. It reuses existing league mechanics rather than replacing them with a second player database or a universal rank. Indexed claims retain qualifications and originals. Candidate discovery and optional calculations remain distinct from host recommendations. Persisted draft branches support between-pick preparation; opt-in maintenance uses the same acquisition operations as on-demand refresh.

Actual native Claude walkthroughs exposed faults that the synthetic SDK journey missed: an unsupported fixture provider fell through to the ESPN adapter, and repeated snapshots made candidate/comparison responses too large to inspect. The adapter now rejects unsupported providers before network access, preserving conditional context. Compact responses retain the full originals with expansion handles. These failures and repairs are retained in the private evidence corpus rather than counted as successful demonstrations.

After those repairs, one native Claude waiver walkthrough completed in 46.7 seconds: the host selected tools from an ordinary question, inspected legal alternatives and retained a conditional judgment with the cut, fallback, lost positional cover and missing evidence. This used invented data and a temporary MCP configuration. It establishes one supported host interaction, not a latency distribution, global tool activation, live recommendation quality or draft performance. The rebuilt installed wheel exercises five CLI entries and 49 SDK calls outside the checkout, using eight synthetic question lifecycles and draft/maintenance recovery. Trade discovery expands the retained candidate original before comparison; mixed questions retain unweighted hypotheses and block unsupported mechanics; draft comparisons consume an independently saved board. Those declared fixture judgments are not LLM strategy evaluations. Both regression suites pass: 587 core tests and 356 league-tool tests.

Saved optional analyses can now enter an action through `experiment_ids`. Preparation checks the exact league snapshot, knowability and qualified status; comparison exposes bounded outputs with complete originals available for expansion. Both the request and result enter the consumed-evidence manifest, so changing an analysis invalidates a saved judgment. Six additional actual SDK calls exercise this connection. A retained ffopportunity observation also passed through calculation, comparison and citation under invented yardage-only league rules. That demonstrates descriptive accounting and provenance; it supplies no evidence of forecast skill or calibrated title odds.

What remains deliberately unproven: forecast superiority, calibrated championship probabilities, automatic host selection in every chat, complete live-source coverage and sub-minute end-to-end LLM draft performance. Synthetic SDK journeys establish transport and accounting behavior; they do not establish those claims. The full implementation plan retains its open obligations.

## A sharper product boundary

For an ordinary question such as “find trades that improve my roster,” the default operation should be **prepare a decision**, rather than expose a catalog of raw sports endpoints. The underlying service remains useful independently of any particular model. Its contracts support this sequence:

1. Bind the league, own team, exact rules and requested horizon. Reuse rule-bound research; invalidate it when its binding changes.
2. Acquire the resources needed for this question. Show last successful observation, attempted refresh, source period and missing coverage separately.
3. Enumerate feasible alternatives, including holding, required cuts and both teams' resulting rosters. Candidate enumeration is not a value ranking.
4. Supply a compact evidence packet: claims, qualifications, counterevidence, calculations, material gaps and handles for originals. The host expands what could change the choice.
5. Let the host recommend and explain a branch. Recheck the evidence actually consumed, then save the rationale, reversals and a continuation capsule.

This sequence is already represented by the decision lifecycle above. It is not a promise that current live feeds cover every requested input. A trade question with incomplete future forecasts can still yield a conditional roster comparison; it cannot yield calibrated future title odds merely because the interface supports them.

The project has three distinct validity questions: **is the action legal; is the evidence fit for this decision now; is the recommendation analytically supported?** A successful HTTP response answers none of them alone. A source can be reachable while its period is wrong. A legal trade can rely on incomplete injury evidence. A complete evidence packet can still contain an unvalidated forecast. Keep these distinctions visible in both tools and explanations.

### Another direct sports reference: selective tools and bounded responses

[Sports Hub's pinned server](https://github.com/lacausecrypto/mcp-sports-hub/blob/95d6046c80d33f101b4f496c343510265e64ab4c/src/index.ts), MIT, registers selected providers through lazy imports. Its [response pipeline](https://github.com/lacausecrypto/mcp-sports-hub/blob/95d6046c80d33f101b4f496c343510265e64ab4c/src/shared/tool-pipeline.ts) centralizes field selection, response limits and empty-result warnings. The registry, package manifest, projection implementation, HTTP helper and pipeline test source were inspected; the upstream package was not run. Its README and code comments contain differing provider/tool counts, so those marketing totals are not used as architectural evidence.

**Useful transfer:** capabilities should be discoverable without making the host repeatedly process every upstream tool or enormous payload. Shared response shaping belongs at the transport boundary. **Important difference:** [key-name projection](https://github.com/lacausecrypto/mcp-sports-hub/blob/95d6046c80d33f101b4f496c343510265e64ab4c/src/shared/projection.ts) and shortened arrays can omit decision-critical qualifiers or candidates. Moneyball should preserve complete candidate originals and mandatory identity, period, provenance and caveat fields; compact previews must expose omitted coverage and expansion handles. An empty provider result means neither “no injury” nor “no valuable free agent.”

**Status:** inspected challenger/reference; no dependency added or upstream code copied. Reopen adoption if Moneyball adds enough independent providers that static registration becomes a demonstrated maintenance problem. The relevant discriminator is whether an ordinary agent can find the right operation while retaining decisive evidence, not tool count.

### Polish priorities, with explicit non-goals

| Priority | Useful improvement | What would establish success |
|---|---|---|
| 1 | Make question-level readiness the visible entry contract | A real trade or waiver question shows identity, legal alternatives, compatible forecast coverage and dated reports; partial inputs stay partial |
| 2 | Keep compact previews recoverable | Expanding a preview recovers the decisive source qualifier and the entire candidate list after a reset |
| 3 | Keep provider semantics distinct | A playing-conditional mean, unconditional mean and prop threshold cannot silently become interchangeable numbers |
| 4 | Invalidate only with an explicit dependency basis | Changed consumed evidence forces recheck; unchanged content does not conceal stale requested-period coverage |
| 5 | Add new analytics behind qualified contracts | An experimental result contributes an inspectable assumption and provenance trail without becoming an unsupported default prediction |

These are product priorities, not five newly completed features. Much of the structural machinery exists; comprehensive current evidence and numerical calibration remain separate obligations. Do not extract a generic framework, add a separate autonomous reasoning server or replace local SQLite merely to resemble an enterprise reference. Reconsider those choices only when a concrete user journey exposes a limitation.

A bounded Jev critique identified a concrete increment, confirmed by local inspection: preparation aggregated `gaps` into one overall `readiness`, and recording retained that list as `unresolved_gaps` (`moneyball/league/decision.py`). An additive `capability_readiness` now distinguishes snapshot accounting, weekly point calculations, current decision evidence, future roster accounting, future valuation, execution and championship probability. Only recognized typed gaps receive narrower scopes; unfamiliar codes and prose remain conservative. The complete gap ledger stays available. A complete calculator result establishes conditional arithmetic, never forecast quality or recommendation approval. Future valuation and championship probability remain explicitly unestimated.

Comparison receipts retain these disclosures for every alternative, including blocked and unchosen branches, and continuation capsules recover them. **Discriminators:** stale reporting cannot erase a supplied calculation or certify current advice; incomplete lineups and wrong forecast horizons cannot masquerade as weekly arithmetic; a blocked retention alternative remains recoverable even when the host chooses hold. These cases have local regression tests. The bounded Jev review inspected calculator contracts before integration and returned `completed_unverified`; it is not an independent passing audit of the new implementation.

The outside references support this boundary from different directions: [OpenBB's current architecture](https://docs.openbb.co/odp/python/concepts/architecture) separates provider fetchers from commands and shares one engine across interfaces; [CDS Hooks 2.0](https://cds-hooks.hl7.org/2.0/#update-stale-guidance) treats persisted advice as guidance that must be replaced when the relevant workflow changes. Neither supplies Moneyball's exact roster accounting, dynasty strategy or predictive validation. Their mechanisms complement rather than substitute for those domain obligations.

## Repeatable reference process

1. State the user journey and its failure independently of fantasy terminology.
2. Search both direct sports tools and structurally similar systems; give each reference one specific job.
3. Pin the source and inspect dependencies, defaults, relevant tests and failure history—not just the README.
4. Preserve originals, licenses and receipts. Label adoption, challenger, deferral and contradiction separately.
5. Implement the narrow useful mechanism in the existing application, retaining meaningful source distinctions.
6. Test a plausible wrong behavior. Use discoveries to seed the next investigation: refresh ordering led to worker fencing, then dependency manifests and atomic receipt publication.
7. Report separately what was inspected upstream, tested locally, integrated and demonstrated through the actual transport. Reopen a decision when its inputs or acceptance conditions change.

This follows reference-driven-development and its it-already-exists factory without adding an unrelated framework merely because it exists.

## Further generalization review: 2026-10-13

The repeatable core is **a question-scoped decision workbench**, not a fantasy ranking engine. Evidence resources are reusable across questions; qualification is specific to the league, horizon and people actually involved. Separate the resource's observation/version from the host's interpretation of it. Keep one application with provider adapters, deterministic constraints and calculations, recoverable compact packets, and decision receipts. This generalization supports more question types without pretending that the same forecasting method answers every one.

### Closer sports implementations

[gtonic/nfl_mcp's weekly briefing](https://github.com/gtonic/nfl_mcp/blob/5021452ca89fcc13fe42f0cb02b2eff0ab0c117d/nfl_mcp/briefing_tools.py), MIT, gathers league state and auxiliary inputs concurrently and exposes feed staleness and injury-source conflicts. Its [roster-needs module](https://github.com/gtonic/nfl_mcp/blob/5021452ca89fcc13fe42f0cb02b2eff0ab0c117d/nfl_mcp/roster_needs.py) shares starting-lineup/replacement calculations between waiver and trade tools. These are useful examples of task-level preparation and shared mechanics. **Boundary:** inspected source and selected test source, not an upstream runtime demonstration. Do not inherit arbitrary cache-age thresholds, forecast blend weights or probability calibration. Fractional flex approximations are not substitutes for exact eligibility. Current means and replacement levels also do not settle dynasty development or future title probability.

[gregbuehler/mcp-ff2026's repository](https://github.com/gregbuehler/mcp-ff2026/blob/27f446a75e188b99d33389e5e8f4e546a91b4ce0/src/mcp_ff2026/storage/database.py) provides a particularly close ingestion mechanism: `commit_sync` commits raw snapshots first, then projects them in a separate transaction; projection failure is recorded without discarding the captured response. Its [architecture document](https://github.com/gregbuehler/mcp-ff2026/blob/27f446a75e188b99d33389e5e8f4e546a91b4ce0/docs/architecture.md) describes an append-only league mirror and separate value concepts. **Transfer:** retain recoverable capture separately from successful interpretation, especially for schema drift. **Boundary:** relevant source inspected, upstream not run; no license was established from the inspected repository tree, so no code was copied. Documentation's broader capability claims are not verification of their implementation. This is an audit criterion for Moneyball ingestion, not a claim that every existing adapter lacks it.

### Outside sports: event time is not knowledge time

[Feast's point-in-time documentation](https://docs.feast.dev/getting-started/concepts/point-in-time-joins) describes feature retrieval relative to a historical decision and its TTL. But the [inspected query builder](https://github.com/feast-dev/feast/blob/2dfb9e6d1a37719fd11028d916094050f1cdd552/sdk/python/feast/infra/offline_stores/offline_utils.py) defaults `filter_by_created_timestamp` to false. Its [tests](https://github.com/feast-dev/feast/blob/2dfb9e6d1a37719fd11028d916094050f1cdd552/sdk/python/tests/unit/infra/offline_stores/test_filter_by_created_timestamp.py) distinguish event-time eligibility from filtering later-created revisions. **Transfer:** test both clocks explicitly; an old event date cannot establish historical availability. **Boundary:** documentation and source inspected, upstream tests not executed; Apache-2.0 license inspected, no dependency added. Moneyball also needs the time it locally observed information, not merely the provider's claimed publication date. This reinforces its existing frozen-read and knowability contracts rather than replacing them with a feature-store library.

[ALFRED's real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) and [vintage dates](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html) make revised knowledge a first-class query dimension. **Transfer:** an as-of question retrieves a vintage, not today's revised history labeled with an old season. **Boundary:** primary documentation read, no authenticated API request made. An economic publication calendar does not solve irregular sports reporting; it cannot reconstruct snapshots that were never captured. Retain event period, source issue time and local observation time separately.

[Wikidata's ranking documentation](https://www.wikidata.org/wiki/Help:Ranking) is a useful challenger for keeping source statements distinct from their interpretation. Preferred/normal/deprecated ranks serve a community knowledge base; they are not a user's private confidence scores, and historical correctness belongs in temporal qualifiers. **Transfer:** a decision receipt may record why the host preferred one claim while keeping counterevidence retrievable. **Reject:** a global truth rank that permanently hides inconvenient reporting. The bounded Jev review suggested a recency concern, but parent inspection found latest selection scoped per document key, not replacement of all unrelated sources by the newest document. No such broader bug was established. The review ended `completed_unverified`; official documentation was independently read, but upstream source/runtime was not verified.

### Concrete polish from the comparison

A real synthetic Service probe found a scope mismatch: preparation covered one player, then a trade introduced two others. Comparison extended its dependency manifest but did not recompute targeted source-readiness checks for the added players. This did not prove that a wrong recommendation had been made; it established an incomplete disclosure path.

Comparison now qualifies the union of original and action-specific players at the same frozen evidence cutoff. Work is grouped in bounded batches of twelve, identical scopes are reused within the comparison, and the operation makes no new network requests. Each branch retains its checks and merges new gaps into capability readiness. Saved receipts retain even unchosen alternatives' missing evidence. The original question packet remains unchanged.

Regression discriminators cover a twenty-five-player expansion without losing the final group, late local reporting excluded from a historical comparison, and missing reporting retained for an unchosen trade. A test initially attempted to save a live judgment after adding new consumed evidence; the existing freshness guard correctly rejected it. The historical test now declares its as-of intent explicitly. The guard was not weakened.

These changes establish coverage disclosure and frozen evidence behavior, not forecast accuracy, automatic acquisition of every new player's news or calibrated championship odds. Newly introduced people with missing evidence still need focused acquisition followed by a fresh preparation/comparison before actionable advice. Do not turn this review into a generic framework extraction, new service or one-agent-per-player architecture.

Verification after this increment: **587 core tests and 363 league-tool tests passed; wheel and sdist built successfully.** These are local synthetic regression and packaging checks, not upstream runtime or real-league forecasting evaluations.


### Discovery is a separate architectural role

The [inspected gtonic player-news client](https://github.com/gtonic/nfl_mcp/blob/5021452/nfl_mcp/news_sources.py), MIT, supplied a useful query mechanism: a bounded ESPN player-specific news endpoint. Actual public requests on 2026-10-13 confirmed JSON responses with item IDs, headlines, links, publication dates and modification dates. The native implementation uses Moneyball's existing cache and deadline controls rather than importing a second server or HTTP stack. No upstream implementation was copied.

The live response also falsified a tempting interpretation: generic fantasy articles can carry the queried player ID, and old notes remain in a newly fetched response. That ID is association, not entailment. Moneyball therefore exposes **discovery leads**, with no inferred forecast period and no verified player claims. Publication, modification and collection times remain distinct. The host must inspect originals before indexing permitted, dated observations. Tool output contains metadata and links; the private transport cache may contain raw upstream responses and does not confer article redistribution rights.

Focused live `prepare_decision` now attaches these bounded leads when exact IDs are available. `player_news_leads` and `moneyball news-leads` also expose the same operation independently. Historical preparation reads retained observations without current network calls. Waiver candidates can use an exact crosswalk from a saved pool bound to the same league, platform and season. Failed requests retain the last successful dates. Checked publication prevents a late older result from overwriting a newer collection; identical records do not create another version. Leads enter consumed-input manifests but **never clear reporting readiness** on their own.

A real connected-league preparation refreshed league state in 3.87 seconds but found no exact ESPN crosswalks for the three focused players. It correctly remained partial: reporting and compatible future forecasts were missing. This demonstrates honest failure handling in the ordinary service path, not complete news coverage, a latency distribution or advice quality. Separate direct endpoint probes established reachability; they cannot repair identity or source relevance. Preserve these distinctions when generalizing this pattern to contracts, markets or other sports.


### Identity is an evidence relationship, not a dictionary lookup

The next seeded investigation followed the real Sleeper discovery gap into the sources that generate cross-platform IDs. [nflreadr's loader](https://github.com/nflverse/nflreadr/blob/master/R/load_ffverse.R) points to DynastyProcess for fantasy IDs; [the pinned DynastyProcess export](https://github.com/dynastyprocess/data/blob/2bbc02be740f0972e4a13791cd9f8ffd1b78a9d2/files/db_playerids.csv) contains direct Sleeper/ESPN relationships. nflverse's player export supplies GSIS/ESPN relationships, but no Sleeper column. These are useful acquisition references, with different source provenance and licenses; no additional provider framework is needed merely to read them.

Inspecting production changed the proposed transfer. The [nflverse producer](https://github.com/nflverse/nflverse-players/blob/943a362bffed0068ab70c314c443bed6b3e0b838/R/players_espn.R) progressively joins normalized names with other attributes, ultimately permitting full-name-only matching. Its default fills missing IDs while retaining prior assignments. DynastyProcess's [public workflow](https://github.com/dynastyprocess/data/blob/7e2a4c1a84999c2cd14ad4502c1d12fa2dbde8e2/.github/workflows/weekly-playerids.yml) invokes a private builder whose current matching implementation could not be inspected. **An exact lookup in either output establishes a recorded assertion, not independently proven identity.** Uniqueness tests are necessary and insufficient.

The captured DynastyProcess export contained six duplicated Sleeper keys, one with conflicting ESPN targets. Comparing direct and GSIS-based paths revealed six disagreements. A second investigation inspected actual ESPN athlete responses: the Sleeper Conklin/Izzo external IDs resolved to the opposite person's ESPN record, while the DynastyProcess David Morgan target resolved to Kyle Carter. Other disagreements appeared to involve duplicate historical ESPN records for the same person. This bounded sample does not establish a catalog-wide error rate or a universal preferred source. It does falsify both last-row-wins and unconditional native-source-wins enrichment.

**Contract selected at the research checkpoint:** retain namespace-scoped, versioned identity assertions; collect every matching row; expose contradictions; never resolve ambiguity by source order or name-only fallback. Preserve native league ownership IDs, while separately qualifying external enrichment. Target metadata can detect a contradiction without becoming a fuzzy lookup mechanism. Store mapping provenance, observation time and any corroboration separately from news freshness. A current crosswalk cannot supply historical availability before it was observed. Alias identity and usefulness of a particular news endpoint require separate checks.

**Preimplementation status (superseded by the integration checkpoint below):** sources, generation paths and counterexamples inspected; source bytes and receipts retained privately. The parent verified all 32 layer-two capture hashes. The proposed crosswalk resolver and conflict quarantine are **not integrated**. Existing news discovery requires recorded exact IDs and labels its output untrusted; it does not currently check the target athlete for identity contradictions. Preserve that limit when using leads, and inspect originals before attaching a player claim. No imported crosswalk, copied GPL pipeline code or real-player fixture was added to the repository. DynastyProcess's repository license is GPL-3.0; nflverse-data declares CC BY 4.0; loader code licensing does not supersede data-source terms.

Discriminating checks before adoption: row-order changes cannot choose a conflicting target; identical duplicate assertions retain provenance; a native/GSIS agreement with contradictory target metadata cannot certify enrichment; missing native GSIS remains a weaker assertion rather than corroboration; failed refresh retains original dates; a historical cutoff excludes later mappings; duplicate ESPN aliases are not selected by numeric size. These are proposed resolver acceptance checks, distinct from the existing discovery regression tests.


The third seeded review reproduced the acceptance gap through a temporary synthetic `Service.news_leads` call: a league player with another athlete's decimal ESPN target received `discovered` leads under its league ID, while the output correctly remained labeled unverified. This is an acquisition-binding counterexample, not proof that the service certified a false report. It also identified a necessary cache dependency: revoking an identity qualification must invalidate attached leads even when the numeric ESPN ID stays unchanged. Target checks requiring DOB must consume a dated raw identity receipt, because the current normalized Sleeper player record does not retain DOB. These findings refine the proposed resolver contract; no production repair is claimed by this research review.


## Identity integration checkpoint

The selected contract is now integrated into the existing native warehouse and league service. An opt-in DynastyProcess import records every matching assertion, including duplicate rows, source names, birthdates and namespace IDs. Native IDs do not outrank contradictory imported assertions. Source conflicts, ambiguous rows and mismatched ESPN target metadata quarantine enrichment; no fuzzy lookup is performed. Target agreement is labeled **metadata-consistent assertion**, not identity proof. Current imports cannot backfill what was knowable historically.

News collections consume a dated qualification token. A revoked qualification invalidates attached leads even when the ESPN ID is unchanged. Delayed target or news responses cannot overwrite a newer contradiction. Historical reads use retained qualifications without fetching current metadata. Service-managed crosswalk refreshes enter decision dependency manifests. This establishes local publication checks; the separate warehouse and league store are not one atomic cross-database snapshot, and external warehouse writers remain outside that boundary.

An independent ingestion-to-news audit found two failures in the initial implementation: missing-ESPN rows could hide conflicting GSIS assertions, and normalization discarded conflicting birthdates. Both were repaired and preserved as regression cases. These failures show why multiple agreeing IDs and successful schema checks alone are insufficient.

A real native connected-league refresh and opt-in import subsequently discovered bounded leads for three focused players that the earlier preparation could not resolve. The observed league refresh took 4.00 seconds and focused discovery 2.15 seconds in one run. All three were metadata-consistent third-party assertions. Reporting and compatible future forecast gaps remained explicit: discovery is not verified article relevance, forecast accuracy or a latency distribution. Originals, acquisition receipts and synthetic audit failures remain private. No GPL pipeline code or real-player test fixture was copied; the source's GPL-3.0 registry and inaccessible private-builder caveat remain visible.

Final integration verification: 592 core and 389 connected-service tests pass. A fresh isolated installed wheel passes five CLI checks and 49 existing MCP SDK calls, plus a focused identity-qualified discovery call using cached synthetic evidence. These establish regression, packaging and transport behavior; predictive quality remains unproven.
