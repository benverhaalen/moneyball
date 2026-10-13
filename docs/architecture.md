# Architecture and reliability

Moneyball is one package and one default workflow. Connected application code lives in `moneyball.league`. The `art_of_the_deal` package contains compatibility forwarding modules only; it has no separate service or business logic. Root-level `moneyball` modules contain optional analytics and earlier draft experiments.

```text
Coding agent / JSON CLI
  -> local MCP service
     -> Sleeper or ESPN adapters -> normalized league snapshots
     -> exact-rule compiler -> research request -> host research -> saved policies
     -> dated player claims -> explicit future worlds -> roster comparisons
     -> draft / lineup / trade / waiver comparison packets
     -> bounded data-pipeline bridge -> research warehouse
```

The host owns browsing, reasoning and recommendations. The service does deterministic retrieval, storage, validation and comparison. No model API key is required by the server. The portable skill supplies a repeatable workflow across agent clients rather than embedding a model provider.

The connected evidence store retains versioned snapshots, raw content hashes, acquisition times, reports and decision packets. `MONEYBALL_HOME` chooses its location (`ART_OF_DEAL_HOME` is a fallback). The normal pipeline warehouse sits below the same root in `lab/`. Older snapshot archives remain available through `moneyball legacy`; existing databases with different schemas are not silently merged. Choose a private writable directory and back it up if you need history.

Historical queries use local availability, not just a row's football season or a provider's date claim. A current download of an old file cannot establish what was knowable before it was acquired. Snapshot failures preserve the previous successful version and report the failure. Public endpoint reads are not a transactional snapshot across an entire platform.

Research keys include exact rules, season, platform/league, own team, team count and method version. Changes invalidate the current report; roster-only changes do not automatically require repeating structural research. Saved source-reading claims are authored by the host. Validation does not independently prove that a source was read or that a transferred mechanism works.

Forecasts must match their period, scoring provenance, source cohort and availability cutoff. An unavailable value is not zero. A prop threshold is not an expected point total. Annual forecasts do not become weekly forecasts. Lineup mechanics maximize supplied values under supported constraints; this is distinct from modeling realized best-ball scores or calibrated championship probabilities.

The package reads platform state and writes local evidence. It does not submit trades, picks, waivers, lineup changes or messages. macOS launchd scheduling in the legacy CLI is local to a logged-in machine, not an always-on hosted service. Advanced mock and simulation paths remain experimental and have narrower format support than connection.

Player assessments bind to exact rules, ownership, season and selected team. Source/claim dependencies preserve why a forecast was used; expiry and optional pruning propagate to missing projections. The host chooses the trust posture; schema validation does not certify it. Scenario comparisons require explicit hold, complete scoring components and supplied availability. They save immutable run receipts and report all affected squads. Representative-week means are not a dynamic championship model. See [player assessment](player-assessment.md).
