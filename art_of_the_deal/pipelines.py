"""Agent access to the existing observed-data warehouse, with bounded reads."""
import os
from pathlib import Path
import shutil
import time

from moneyball.warehouse import Warehouse
from moneyball.sources import DATASETS, ingest, registry as register_basic
from moneyball.advanced import SPECS, ingest_advanced, register_advanced
from .store import DataError, timestamp


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
        rows.append({"dataset": name, "source": "nflverse_" + name,
                     "access": "public", "url": spec["url"],
                     "acquired": (name, "nflverse_" + name) in published})
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
    return {"ok": all(r["ok"] for r in results), "results": results,
            "interpretation": "Only complete validated partitions are published. Failed refreshes preserve previously valid partitions and remain explicit failures."}


def query(service, dataset, *, seasons, alias=None, player_ids=None, source=None,
          as_of=None, limit=20, positions=None):
    years = _seasons(seasons)
    if not isinstance(dataset, str) or not dataset or len(dataset) > 100:
        raise DataError("Supply a dataset name from pipeline_catalog")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise DataError("Pipeline limit must be an integer from 1 to 100")
    cutoff = timestamp(as_of)
    if cutoff > time.time():
        raise DataError("Pipeline cutoff cannot be in the future")
    filters, identities = None, []
    if player_ids is not None:
        if not alias or not isinstance(player_ids, list) or not player_ids or len(player_ids) > 12:
            raise DataError("Focused pipeline queries need a league alias and one to twelve player IDs")
        d = service.snapshot(alias, as_of=as_of)["data"]
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
    rows = warehouse(service).query(dataset, cutoff=cutoff, source_id=source,
                                    seasons=years, positions=positions, system_asof=cutoff,
                                    identity_filters=filters, limit=limit + 1)
    return {"dataset": dataset, "rows": rows[:limit], "more_available": len(rows) > limit,
            "identities": identities, "evidence_as_of": cutoff,
            "scope": "Latest locally knowable validated partitions at this cutoff. Empty rows mean absent eligible evidence, never zero performance. Exact recorded IDs only; inspect each row's provenance."}
