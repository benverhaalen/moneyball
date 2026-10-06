# Architecture and reliability

The public home is Moneyball. `art_of_the_deal` remains the Python namespace for the combined league service so existing imports and command aliases continue working. It is not a separate installation requirement.

```text
Coding agent / JSON CLI
  -> local MCP service
     -> Sleeper or ESPN adapters -> normalized league snapshots
     -> exact-rule compiler -> research request -> host research -> saved policies
     -> draft / lineup / trade / waiver comparison packets
     -> bounded data-pipeline bridge -> research warehouse
```

The host owns browsing, reasoning and recommendations. The service does deterministic retrieval, storage, validation and comparison. No model API key is required by the server. The portable skill supplies a repeatable workflow across agent clients rather than embedding a model provider.

The connected evidence store retains versioned snapshots, raw content hashes, acquisition times, reports and decision packets. `ART_OF_DEAL_HOME` chooses its location. Older Moneyball snapshots and analytics use their own configured store; existing stores are not silently imported. Choose a private writable directory and back it up if you need history.

Historical queries use local availability, not just a row's football season or a provider's date claim. A current download of an old file cannot establish what was knowable before it was acquired. Snapshot failures preserve the previous successful version and report the failure. Public endpoint reads are not a transactional snapshot across an entire platform.

Research keys include exact rules, season, platform/league, own team, team count and method version. Changes invalidate the current report; roster-only changes do not automatically require repeating structural research. Saved source-reading claims are authored by the host. Validation does not independently prove that a source was read or that a transferred mechanism works.

Forecasts must match their period, scoring provenance, source cohort and availability cutoff. An unavailable value is not zero. A prop threshold is not an expected point total. Annual forecasts do not become weekly forecasts. Lineup mechanics maximize supplied values under supported constraints; this is distinct from modeling realized best-ball scores or calibrated championship probabilities.

The package reads platform state and writes local evidence. It does not submit trades, picks, waivers, lineup changes or messages. macOS launchd scheduling in the legacy CLI is local to a logged-in machine, not an always-on hosted service. Advanced mock and simulation paths remain experimental and have narrower format support than connection.
