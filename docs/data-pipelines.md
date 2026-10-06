# Data pipelines and source boundaries

The connected agent can inspect the dataset catalog, request bounded imports and query selected seasons or focused player identities. Acquisition is explicit; connecting a league does not silently download every historical dataset. The bridge uses the existing Moneyball warehouse, preserving raw hashes, source metadata, partition receipts, quarantine and historical-availability checks.

The connected service defaults its lab warehouse beneath its private evidence directory. `MONEYBALL_LAB_DIR` explicitly points the bridge at an existing lab store when reusing one. This does not migrate league snapshots or authorize additional provider access. The older lab commands remain documented in [analytics](analytics.md).

```sh
moneyball-agent pipeline-catalog
moneyball-agent pipeline-sync weekly --seasons 2025
moneyball-agent pipeline-query weekly --seasons 2025 --positions QB --limit 8
# Focus a read on exact IDs known to a connected league:
moneyball-agent pipeline-query weekly --seasons 2025 --alias home --ids PLAYER_ID --limit 8
```

MCP equivalents are `pipeline_catalog`, `sync_pipeline` and `query_pipeline`; inspect the client's tool schema for parameters. The catalog is the current source of truth for supported dataset names. A focused player query requires a connected alias and recorded compatible external IDs.

Use small dataset/season selections. Play-by-play can be large. The catalog distinguishes acquired partitions from merely available adapters and reports decoder requirements. NGS `.rds` decoding requires R. Offline ingestion reuses acquired bytes and reports missing objects rather than pretending to refresh them.

Player joins use recorded provider namespaces and identifier crosswalks. Equal numeric strings in ESPN and Sleeper are not evidence of the same player. Names alone are insufficient. A focused query may return an identity-crosswalk gap rather than guessing.

## NFL data availability

The [NFL's Big Data Bowl repository](https://github.com/nfl-football-ops/Big-Data-Bowl) makes selected tracking data available for research competitions. It does not establish unrestricted access to all league-wide Next Gen Stats tracking feeds. [nflverse's availability schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html) describes public datasets and their update differences: play-by-play, player statistics and other observations have different upstream sources and refresh schedules.

NGS public summaries, participation data and FTN charting are distinct from complete raw tracking. Coverage varies by season and dataset. Do not infer that a published summary permits redistributing an upstream proprietary feed. Read dataset-specific licenses and source notices before sharing derived data.

## Evidence roles

| Evidence | Appropriate role | Common invalid inference |
| --- | --- | --- |
| League API state | Rules, rosters, picks, schedule and ownership | Public access means permission to execute a trade |
| Historical football outcomes | Observed usage and realized events | An old season filename proves historical availability |
| Professional forecasts | Conditional future inputs for their stated period/scoring | Annual totals are weekly points; missing players have zero value |
| Market values or ranks | Observed preferences and acquisition context | Rank is expected points or a universally fair trade price |
| Public tracking summaries | Source-specific advanced observations | All underlying NFL tracking data is open |

The source registry preserves horizon, access status, licensing and refresh caveats. Some legacy providers are cached-only or permission-dependent. No paid subscription is required for the offline demo, and a subscription alone does not establish bulk or redistribution rights. Source failures and missing future projections should appear in the final advice.
