"""Agent access to the existing observed-data warehouse, with bounded reads."""
import os
from pathlib import Path
import shutil
import time

from moneyball.warehouse import Warehouse
from moneyball.sources import DATASETS, ingest, registry as register_basic
from moneyball.advanced import SPECS, ingest_advanced, register_advanced
from .store import DataError, timestamp, digest
from .pagination import encode_cursor, decode_cursor


def warehouse(service):
    # One explicit location makes old analytical stores reusable without copying.
    return Warehouse(Path(os.environ.get("MONEYBALL_LAB_DIR", str(service.store.root / "lab"))).expanduser())


def catalog(service):
    w = warehouse(service)
    register_basic(w)
    register_advanced(w)
    state = w.summary()
    published = {(r["dataset"], r["source_id"]) for r in state["published_partitions"]}
    rows = []
    for name, spec in DATASETS.items():
        source_id = spec.get("source_id", "nflverse_" + name)
        rows.append({"dataset": name, "source": source_id,
                     "access": "public", "url": spec["url"],
                     "acquired": (name, source_id) in published})
    for name, spec in SPECS.items():
        rows.append({"dataset": name, "source": "nflverse_" + name,
                     "access": "public", "history": spec["history"], "caveat": spec["caveat"],
                     "runtime": "Rscript" if spec["format"] == "rds" else "Python standard library",
                     "runtime_available": bool(shutil.which("Rscript")) if spec["format"] == "rds" else True,
                     "acquired": (name, "nflverse_" + name) in published})
    return {"warehouse": state, "datasets": rows,
            "registered_sources": w.source_registry(),
            "next_tools": ["sync_pipeline", "query_pipeline"],
            "scope": "Historical and current observations. Retrieval today does not prove past publication; these are not player forecasts. Commercial source imports remain explicit and subject to their rights."}


def _seasons(values):
    if not isinstance(values, list) or not values or len(values) > 8 or any(
            isinstance(v, bool) or not isinstance(v, int) or not 1920 <= v <= 2200 for v in values):
        raise DataError("Supply one to eight explicit season years")
    return sorted(set(values))


def sync(service, datasets, seasons, *, force=False, offline=False):
    years = _seasons(seasons)
    if not isinstance(datasets, list) or not datasets or len(datasets) > 8 or any(
            not isinstance(d, str) or d not in {*DATASETS, *SPECS} for d in datasets):
        raise DataError("Request one to eight named datasets from pipeline_catalog")
    w = warehouse(service)
    basic = list(dict.fromkeys(d for d in datasets if d in DATASETS))
    advanced = list(dict.fromkeys(d for d in datasets if d in SPECS))
    results = []
    if basic:
        results.append(ingest(w, datasets=basic, seasons=years, force=force, offline=offline))
    if advanced:
        results.append(ingest_advanced(w, datasets=advanced, seasons=years, force=force, offline=offline))
    # The connected service consumes native publication receipts. This marker
    # invalidates decision capsules after service-managed identity source refresh;
    # external warehouse writers remain outside the Store transaction boundary.
    if any(d in ("fantasy_ids", "players") for d in datasets):
        current = [r for r in w.summary()["published_partitions"] if r["dataset"] in ("fantasy_ids", "players")]
        previous = service.store.get("identity_source", "warehouse", required=False)
        data = {"partitions": current, "warehouse": str(w.root)}
        if not previous or previous["data"] != data:
            service.store.put("identity_source", "warehouse", data)
    return {"ok": all(r["ok"] for r in results), "results": results,
            "interpretation": "Only complete validated partitions are published. Failed refreshes preserve previously valid partitions and remain explicit failures."}


def query(service, dataset, *, seasons, alias=None, player_ids=None, source=None,
          as_of=None, limit=20, positions=None, cursor=None):
    years = _seasons(seasons)
    if not isinstance(dataset, str) or not dataset or len(dataset) > 100:
        raise DataError("Supply a dataset name from pipeline_catalog")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise DataError("Pipeline limit must be an integer from 1 to 100")
    if positions is not None and (not isinstance(positions, list) or len(positions) > 20 or any(not isinstance(p, str) or not p or len(p) > 20 for p in positions)):
        raise DataError("Positions must be a list of at most twenty position labels")
    binding = digest({"dataset": dataset, "seasons": years, "alias": alias, "player_ids": player_ids, "source": source, "positions": positions})
    offset, cutoff, batches = decode_cursor(cursor, binding) if cursor else (0, timestamp(as_of), None)
    w = warehouse(service)
    if batches is None:
        with w.connect() as db:
            batches = [r[0] for r in db.execute("SELECT id FROM batches WHERE dataset=? AND status='published' AND available_at<=? AND observed_at<=? ORDER BY id", (dataset, cutoff, cutoff))]
    if not isinstance(batches, list) or len(batches) > 10000 or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in batches):
        raise DataError("Invalid pipeline snapshot cursor")
    if cursor and as_of is not None and timestamp(as_of) != cutoff:
        raise DataError("Cursor cutoff differs from requested as_of")
    if cutoff > time.time():
        raise DataError("Pipeline cutoff cannot be in the future")
    filters, identities = None, []
    if player_ids is not None:
        if not alias or not isinstance(player_ids, list) or not player_ids or len(player_ids) > 12:
            raise DataError("Focused pipeline queries need a league alias and one to twelve player IDs")
        d = service.snapshot(alias, as_of=cutoff)["data"]
        filters = {}
        for pid in player_ids:
            pid = str(pid)
            if pid not in d["players"]:
                raise DataError("Unknown connected-league player ID: " + pid)
            p = d["players"][pid]
            # ESPN local ids are namespaced; use only recorded provider crosswalks.
            x = {k: str(v) for k, v in p.get("external_ids", {}).items() if v is not None and str(v)}
            if d["platform"] == "sleeper":
                x.setdefault("sleeper_id", pid)
            elif d["platform"] == "espn" and pid.startswith("espn:"):
                x.setdefault("espn_id", pid.split(":", 1)[1])
            identities.append({"league_player_id": pid, "name": p["name"], "external_ids": x})
            for field, value in x.items():
                if field in ("gsis_id", "sleeper_id", "espn_id", "pfr_id"):
                    filters.setdefault(field, []).append(value)
                    if field == "pfr_id":
                        filters.setdefault("pfr_player_id", []).append(value)
            if x.get("gsis_id"):
                for field in ("player_id", "passer_player_id", "receiver_player_id", "rusher_player_id", "td_player_id"):
                    filters.setdefault(field, []).append(x["gsis_id"])
        if not filters:
            return {"rows": [], "identities": identities, "status": "identity_crosswalk_required",
                    "reason": "No recorded external IDs; name similarity is not an identity join."}
    rows = w.query(dataset, cutoff=cutoff, source_id=source,
                                    seasons=None if dataset in DATASETS and not DATASETS[dataset]["yearly"] else years, positions=positions, system_asof=cutoff,
                                    identity_filters=filters, limit=limit + 1, order="latest", offset=offset, batch_ids=batches)
    return {"dataset": dataset, "rows": rows[:limit], "more_available": len(rows) > limit,
            "identities": identities, "evidence_as_of": cutoff,
            "next_cursor": encode_cursor(binding, offset + limit, cutoff, batches) if len(rows) > limit else None,
            "ordering": "season_desc_week_desc_event_desc_available_desc_stable_identity",
            "coverage": {"returned_rows": min(len(rows), limit), "more_available": len(rows) > limit,
                         "identity_gaps": [i["league_player_id"] for i in identities if not any(k in i["external_ids"] for k in ("gsis_id", "sleeper_id", "espn_id", "pfr_id"))],
                         "complete_population": False},
            "scope": "Latest locally knowable validated partitions at this cutoff. Empty rows mean absent eligible evidence, never zero performance. Exact recorded IDs only; inspect each row's provenance."}
