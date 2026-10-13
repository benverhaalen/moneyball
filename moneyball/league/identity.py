"""Dated provider assertions and contradiction checks, never identity proof.

DynastyProcess rows are read from the native warehouse, not acquired on this
latency-sensitive path. Exact keys select assertions; names/DOB can reject a
target but never find one. The source's private builder cannot be certified.
"""
from copy import deepcopy
from datetime import datetime
import re
import unicodedata

from .store import DataError, digest, timestamp

ATHLETE = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/"
METHOD = "provider-assertions-v1"
BLOCKED = {"unresolved", "ambiguous", "conflict", "contradicted", "target_unresolved"}


def _name(value):
    # Formatting only, not a fuzzy join or alias-resolution rule.
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold())


def _dob(value, formats=("%Y-%m-%d",)):
    for fmt in formats:
        try:
            return datetime.strptime(str(value), fmt).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def assertions(service, platform, pid, player, cutoff=None):
    """Keep every exact matching source row; conflicting duplicates survive."""
    from .pipelines import warehouse
    cutoff = timestamp(cutoff)
    native = {"name": player.get("name"), "birth_date": player.get("birth_date"),
              "external_ids": deepcopy(player.get("external_ids") or {})}
    eid = native["external_ids"].get("espn_id")
    if not eid and platform == "espn":
        eid = pid.removeprefix("espn:")
    eid = str(eid) if eid is not None else None
    rows = []
    w = warehouse(service)
    if platform == "sleeper":
        rows = w.query("fantasy_ids", cutoff=cutoff, system_asof=cutoff,
                       source_id="dynastyprocess_ids", identity_filters={"sleeper_id": [pid]})
    source_rows = [{"espn_id": r.get("espn_id"), "gsis_id": r.get("gsis_id"),
                    "name": r.get("name"), "birth_date": r.get("birth_date"),
                    "mfl_id": r.get("mfl_id"), "source_row_number": r.get("source_row_number"),
                    "provenance": r["_provenance"]} for r in rows]
    # Missing a target ID does not erase contradictions in other identity fields.
    # Nonmissing partial assertions may agree; missing is not a second value.
    values = {field: {r.get(field) for r in rows if r.get(field)}
              for field in ("espn_id", "gsis_id", "birth_date")}
    names = {_name(r.get("name")) for r in rows if _name(r.get("name"))}
    status, reasons = "unresolved", []
    kind = "native_assertion" if eid else "third_party_assertion"
    if any(len(v) > 1 for v in values.values()) or len(names) > 1:
        status, reasons = "ambiguous", ["Exact source key has differing provider assertions; no winner selected"]
    elif values["espn_id"]:
        proposed = next(iter(values["espn_id"]))
        gsis = next(iter(values["gsis_id"]), None)
        native_gsis = native["external_ids"].get("gsis_id")
        source_dob = next(iter(values["birth_date"]), None)
        if ((eid and eid != proposed) or (native_gsis and gsis and native_gsis != gsis)
                or (names and _name(native.get("name")) and _name(native["name"]) not in names)
                or (_dob(native.get("birth_date")) and _dob(source_dob)
                    and _dob(native["birth_date"]) != _dob(source_dob))):
            status, reasons = "conflict", ["Native and recorded third-party assertions disagree; no source preference"]
        else:
            eid = eid or proposed
            status = "assertion"
    elif eid:
        status = "assertion"
    if eid is not None and not re.fullmatch(r"[0-9]+", eid):
        status, reasons = "unresolved", ["No exact decimal ESPN assertion"]
    nflverse = []
    gsis = native["external_ids"].get("gsis_id")
    if gsis:
        matches = w.query("players", cutoff=cutoff, system_asof=cutoff,
                          source_id="nflverse_players", identity_filters={"gsis_id": [gsis]})
        nflverse = [{"espn_id": r.get("espn_id"), "gsis_id": r.get("gsis_id"),
                     "provenance": r["_provenance"]} for r in matches]
        if any(r["espn_id"] and eid and str(r["espn_id"]) != eid for r in nflverse):
            status, reasons = "conflict", ["Exact GSIS path disagrees with selected ESPN assertion"]
    inputs = {"method": METHOD, "platform": platform, "player_id": pid,
              "native": native, "crosswalk_rows": source_rows, "nflverse_rows": nflverse}
    return {**inputs, "inputs_hash": digest(inputs), "espn_id": eid, "status": status,
            "assertion_kind": kind, "reasons": reasons, "identity_verified": False,
            "limits": ["Source agreement is not independent identity proof", "No name fallback or current-team tie-break",
                       "Source snapshots bind local knowability, not original historical publication"]}


def target_check(payload, eid, native):
    athlete = payload.get("athlete") if isinstance(payload, dict) else None
    if not isinstance(athlete, dict) or str(athlete.get("id")) != eid or not isinstance(athlete.get("displayName"), str):
        raise DataError("Athlete endpoint lacks exact requested identity metadata")
    target = {"espn_id": eid, "name": athlete["displayName"],
              "birth_date": _dob(athlete.get("displayDOB"), ("%d/%m/%Y", "%Y-%m-%d"))}
    reasons = []
    if _name(native.get("name")) and _name(native["name"]) != _name(target["name"]):
        reasons.append("Target name differs from native catalog; unreviewed aliases are quarantined")
    born = _dob(native.get("birth_date"))
    if born and target["birth_date"] and born != target["birth_date"]:
        reasons.append("Target DOB contradicts native catalog")
    status = "contradicted" if reasons else "metadata_consistent_assertion"
    if not _name(native.get("name")):
        status, reasons = "target_unresolved", ["Native comparison name unavailable"]
    gaps = [] if born and target["birth_date"] else ["DOB comparison unavailable; matching name is weak evidence"]
    return target, status, reasons, gaps


def qualify(service, platform, pid, player, client, *, as_of=None, refresh=False, native_check=None):
    """Return a retained or newly fenced binding; historical reads never GET."""
    key = platform + ":" + pid
    candidate = assertions(service, platform, pid, player, as_of)
    prior = service.store.get("player_identity_binding", key, as_of, required=False)
    if as_of is not None:
        if prior and prior["data"].get("inputs_hash") == candidate["inputs_hash"]:
            return prior
        return {"data": {**candidate, "status": candidate["status"] if candidate["status"] in BLOCKED else "target_unresolved",
                         "reasons": candidate["reasons"] + ["No retained target qualification for these assertions at cutoff"]},
                "version_id": None}
    starting = service.store.get("player_identity_binding", key, required=False)
    if candidate["status"] not in BLOCKED:
        client.force = refresh
        payload, receipt = client.get(ATHLETE + candidate["espn_id"], ttl=86400, namespace="public:espn-athlete")
        target, status, reasons, gaps = target_check(payload, candidate["espn_id"], candidate["native"])
        candidate.update(target=target, status=status, reasons=reasons, qualification_gaps=gaps,
                         target_receipt=receipt, target_url=ATHLETE + candidate["espn_id"])
    candidate["qualification_token"] = digest({k: v for k, v in candidate.items() if k != "target_receipt"})
    # GET may itself append transport evidence. Fence after GET, then compare the
    # binding observed before it. Never retry a stale candidate after a conflict.
    epoch = service.store.version_epoch()
    if native_check is not None and not native_check():
        raise DataError("Native catalog changed during request; repeat binding")
    latest = service.store.get("player_identity_binding", key, required=False)
    if (latest and latest["data"].get("inputs_hash") == candidate["inputs_hash"]
            and latest["data"].get("target_receipt", {}).get("checked_at", 0)
            > candidate.get("target_receipt", {}).get("checked_at", 0)
            and candidate.get("target_receipt")):
        # A stale positive transport result cannot undo a newer contradiction.
        return latest
    if latest and latest["data"].get("qualification_token") == candidate["qualification_token"]:
        # Preserve original target observation dates, including on cache hits.
        if latest["data"].get("target_receipt", {}).get("checked_at", 0) >= candidate.get("target_receipt", {}).get("checked_at", 0):
            return latest
    if (latest or {}).get("version_id") != (starting or {}).get("version_id"):
        raise DataError("Identity qualification changed during request; repeat binding")
    if assertions(service, platform, pid, player)["inputs_hash"] != candidate["inputs_hash"]:
        raise DataError("Crosswalk assertions changed during request; repeat binding")
    record = service.store.put_checked("player_identity_binding", key, candidate, epoch,
                                      {"role": "unverified_provider_assertion", "method": METHOD})
    return {**record, "data": candidate}
