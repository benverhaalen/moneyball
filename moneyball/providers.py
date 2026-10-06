"""Read-only public forecasts and market observations, with raw-source receipts.

Projection means use league event scoring. Rankings, trade values and ADP remain
separate datasets. Current responses are knowable when observed, including when
their URL contains an earlier season or their metadata contains an old date.
No KTC scraper, paid API key, browser session, or account creation is used.
"""
import csv
import gzip
import hashlib
import io
import json
import math
import re
from html.parser import HTMLParser

from .sources import HttpFetcher


EXPERIMENTAL_REFERENCE_SCORING = {
    "pass_yd": .04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2,
    "rush_yd": .1, "rush_td": 6, "rush_2pt": 2,
    "rec": 1, "rec_yd": .1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2,
}
RATRACE_SCORING = EXPERIMENTAL_REFERENCE_SCORING  # Historical artifact compatibility.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
PERFORMANCE_KEYS = set(EXPERIMENTAL_REFERENCE_SCORING) | {"pass_att", "rush_att", "rec_tgt"}
SLEEPER_BASE = "https://api.sleeper.com/projections/nfl"
SLEEPER_QUERY = "season_type=regular&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=pts_ppr"
DP_BASE = "https://raw.githubusercontent.com/dynastyprocess/data"
FC_URL = "https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2&numTeams=12&ppr=1"
ARCHIVED_DP = {
    2024: ("ce5e9ba0214b956ff2fcad051105d7564ff0728b", "2024-08-30T02:53:34Z"),
    2025: ("10dde2b393efb289719fb4304f69c258b5419ba4", "2025-08-29T03:15:22Z"),
}


class ProviderError(ValueError):
    """Loud failure for a changed schema or unsafe identity join."""


def _number(value, *, nullable=False):
    if value in (None, "", "NA", "N/A", "-"):
        if nullable:
            return None
        raise ProviderError("Missing numeric provider field")
    if isinstance(value, bool):
        raise ProviderError("Boolean in numeric provider field")
    try:
        result = float(str(value).replace(",", ""))
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"Invalid numeric provider field: {value!r}") from exc
    if not math.isfinite(result):
        raise ProviderError("Non-finite provider value")
    return result


def _scoring_hash(scoring):
    return hashlib.sha256(json.dumps(scoring, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def score_stats(stats, scoring=None):
    """Score only observed numeric components; this may be a subtotal.

    Absence or null is not a zero forecast. Normalizers must attach the
    completeness audit below before this compatibility number is published.
    """
    scoring = EXPERIMENTAL_REFERENCE_SCORING if scoring is None else scoring
    return sum(_number(stats[key]) * _number(value) for key, value in scoring.items()
               if _number(value) != 0 and key in stats and stats[key] is not None)


def _projection_score_metadata(stats, scoring):
    """Audit requested nonzero keys without inferring applicability or sparsity.

    A caller supplying the full league profile will also see unprovided K/DST
    keys. They must resolve applicability separately; native player position
    does not prove a scored event impossible (including unusual trick plays).
    """
    fields = {}
    for key, weight in sorted(scoring.items()):
        if _number(weight) == 0:
            continue
        if key not in stats:
            fields[key] = "absent"
        elif stats[key] is None:
            fields[key] = "source_null"
        else:
            fields[key] = "explicit_zero" if _number(stats[key]) == 0 else "source_forecast"
    absent = [key for key, status in fields.items() if status == "absent"]
    nulls = [key for key, status in fields.items() if status == "source_null"]
    missing = sorted(absent + nulls)
    return {
        "missing_scoring_fields": missing,
        "absent_scoring_fields": absent,
        "null_scoring_fields": nulls,
        "explicit_zero_scoring_fields": [key for key, status in fields.items() if status == "explicit_zero"],
        "scoring_field_status": fields,
        "projection_completeness": "partial_sparse_zero_unverified" if missing else "complete_for_requested_scoring_keys",
        "mean_interpretation": "observed_component_subtotal" if missing else "score_of_provided_component_means",
        "completeness_scope": "All supplied nonzero scoring keys; individual event applicability is not inferred.",
        "missingness_caveat": "Absent/null is unknown, not imputed zero. Source sparse-zero semantics are unverified. Existing numeric mean remains a compatibility subtotal when partial.",
    }


def normalize_sleeper(payload, season, week=None, scoring=None):
    """Return (projections, ADP, coverage). Reject ADP-only rows as forecasts."""
    if not isinstance(payload, list):
        raise ProviderError("Sleeper projection response is not a list")
    scoring = EXPERIMENTAL_REFERENCE_SCORING if scoring is None else scoring
    projections, adp, seen = [], [], set()
    excluded, no_forecast = 0, 0
    for obj in payload:
        if not isinstance(obj, dict) or not obj.get("player_id"):
            raise ProviderError("Sleeper row lacks player_id")
        if str(obj.get("season")) != str(season) or obj.get("week") != week:
            raise ProviderError("Sleeper response season/week disagrees with requested partition")
        pid = str(obj["player_id"])
        if pid in seen:
            raise ProviderError(f"Duplicate Sleeper projection player_id {pid}")
        seen.add(pid)
        player = obj.get("player") or {}
        positions = player.get("fantasy_positions") or [player.get("position")]
        eligible = sorted(set(positions) & SKILL_POSITIONS)
        if not eligible:
            excluded += 1
            continue
        stats = obj.get("stats") or {}
        if not isinstance(stats, dict):
            raise ProviderError("Sleeper stats is not an object")
        base = {
            "source_id": "sleeper_rotowire", "player_id": pid,
            "source_player_id": pid, "name": " ".join(filter(None, [player.get("first_name"), player.get("last_name")])),
            "position": player.get("position"), "fantasy_positions": eligible,
            "team": obj.get("team"), "season": int(season), "week": week,
            "provider": obj.get("company"), "provider_updated_at": obj.get("updated_at"),
            "provider_last_modified": obj.get("last_modified"),
            "timestamp_caveat": "Provider update timestamp is not verified publication; observed_at controls availability.",
        }
        for field, value in stats.items():
            if not field.startswith("adp_"):
                continue
            value = _number(value, nullable=True)
            # The source uses 999/1000 for unranked. Do not turn sentinel into demand.
            if value is None or not 0 < value < 999:
                continue
            adp.append({**base, "adp": value, "adp_type": field,
                        "format": field.removeprefix("adp_"),
                        "format_caveat": "Native source field; 2qb, dynasty_2qb and dynasty_ppr are separate populations. Exact roster/scoring cohort and sample size unverified."})
        if not any(key in stats and stats[key] is not None for key in PERFORMANCE_KEYS):
            no_forecast += 1
            continue
        numeric_stats = {key: _number(value) for key, value in stats.items() if value is not None}
        projections.append({**base, "projection_type": "season" if week is None else "weekly",
                            "date": obj.get("date"), "opponent": obj.get("opponent"),
                            "game_id": obj.get("game_id"), "stats": numeric_stats,
                            "mean": score_stats(numeric_stats, scoring),
                            "scoring_hash": _scoring_hash(scoring),
                            **_projection_score_metadata(stats, scoring),
                            "uncertainty_status": "Observed component means only; omissions remain unknown and no calibrated uncertainty is supplied.",
                            "season_denominator": None})
    if not projections:
        raise ProviderError("Sleeper supplied no usable performance projections (ADP placeholders do not count)")
    return projections, adp, {"raw_rows": len(payload), "projection_rows": len(projections),
                              "adp_rows": len(adp), "excluded_positions": excluded,
                              "no_performance_projection": no_forecast}


def _csv_rows(body):
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


class IdentityCrosswalk:
    """Exact external IDs first; exact name + position fallback, never fuzzy."""
    def __init__(self, rows):
        self.by_fp, self.by_name = {}, {}
        for row in rows:
            pid = row.get("sleeper_id")
            if not pid or pid == "NA":
                continue
            entry = {"player_id": str(pid), "name": row.get("name"),
                     "position": row.get("position"), "team": row.get("team")}
            fp = row.get("fantasypros_id")
            if fp and fp != "NA":
                self.by_fp.setdefault(str(fp), {})[str(pid)] = entry
            if entry["name"]:
                self.by_name.setdefault((entry["name"], entry["position"]), {})[str(pid)] = entry

    def resolve(self, fp_id, name, position, team=None):
        candidates = self.by_fp.get(str(fp_id), {})
        method = "external_id"
        if not candidates:
            candidates = self.by_name.get((name, position), {})
            method = "exact_name_position"
        if len(candidates) > 1:
            # Team is a live, mutable field, so it cannot safely settle old collisions.
            raise ProviderError(f"Ambiguous identity: fp={fp_id}, name={name!r}, position={position}; Sleeper IDs={sorted(candidates)}")
        if not candidates:
            return None, "unmatched"
        candidate = next(iter(candidates.values()))
        if candidate["position"] in SKILL_POSITIONS and position in SKILL_POSITIONS and candidate["position"] != position:
            # Position changes (e.g. WR to TE) are real. Current crosswalk metadata
            # cannot invalidate an older row's position; expose the discrepancy.
            method += "_current_position_differs"
        return candidate["player_id"], method


def normalize_fantasycalc(payload, season):
    if not isinstance(payload, list) or not payload:
        raise ProviderError("FantasyCalc response is not a nonempty list")
    rows, unmatched, seen = [], [], set()
    for obj in payload:
        player = obj.get("player") or {}
        source_id = player.get("id")
        if source_id is None or "value" not in obj:
            raise ProviderError("FantasyCalc player/value schema changed")
        if str(source_id) in seen:
            raise ProviderError("Duplicate FantasyCalc player ID")
        seen.add(str(source_id))
        row = {"source_id": "fantasycalc", "source_player_id": str(source_id),
               "player_id": str(player["sleeperId"]) if player.get("sleeperId") else None,
               "name": player.get("name"), "position": player.get("position"),
               "team": player.get("maybeTeam"), "season": int(season), "week": None,
               "value_type": "trade_market", "format": "dynasty_2qb_12team_ppr",
               "value": _number(obj["value"]), "rank": _number(obj.get("overallRank"), nullable=True),
               "position_rank": _number(obj.get("positionRank"), nullable=True),
               "trend_30_day": _number(obj.get("trend30Day"), nullable=True),
               "interpretation": "Estimated market exchange value, not point projection or championship probability."}
        (rows if row["player_id"] else unmatched).append(row)
    return rows, unmatched


def normalize_dynastyprocess(body, season, crosswalk):
    records = _csv_rows(body)
    if not records or not {"player", "pos", "ecr_1qb", "ecr_2qb", "value_1qb", "value_2qb", "scrape_date", "fp_id"} <= records[0].keys():
        raise ProviderError("DynastyProcess CSV schema changed")
    rows, unmatched, seen = [], [], set()
    for obj in records:
        # Future pick labels intentionally have no player crosswalk; keep separately.
        pid, method = crosswalk.resolve(obj["fp_id"], obj["player"], obj["pos"], obj.get("team"))
        source_pid = obj["fp_id"] if obj["fp_id"] not in ("", "NA") else obj["player"]
        for fmt in ("1qb", "2qb"):
            key = (source_pid, fmt)
            if key in seen:
                raise ProviderError(f"Duplicate DynastyProcess key {key}")
            seen.add(key)
            value = _number(obj["value_" + fmt], nullable=True)
            rank = _number(obj["ecr_" + fmt], nullable=True)
            if value is None and rank is None:
                continue
            row = {"source_id": "dynastyprocess", "source_player_id": source_pid,
                   "player_id": pid, "name": obj["player"], "position": obj["pos"], "team": obj.get("team"),
                   "season": int(season), "week": None, "format": "dynasty_" + fmt,
                   "value_type": "expert_rank_derived", "value": value, "rank": rank,
                   "provider_scrape_date": obj["scrape_date"], "identity_method": method,
                   "interpretation": "FantasyPros expert-rank-derived market measure; no independent performance forecast."}
            (rows if pid else unmatched).append(row)
    return rows, unmatched


class _ProjectionTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.depth = 0
        self.rows = []
        self.row = None
        self.cell = None
        self.updated = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "time" and attrs.get("datetime") and self.updated is None:
            self.updated = attrs["datetime"]
        if tag == "table":
            if attrs.get("id") == "data":
                self.in_table = True
            if self.in_table:
                self.depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.row = {"cells": [], "fp_id": None, "name": None}
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"text": "", "span": int(attrs.get("colspan", 1)), "tag": tag}
        elif tag == "a" and self.row is not None and "fp-player-link" in attrs.get("class", ""):
            m = re.search(r"(?:^|\s)fp-id-(\d+)(?:\s|$)", attrs.get("class", ""))
            if m:
                self.row["fp_id"] = m.group(1)
            self.row["name"] = attrs.get("fp-player-name")

    def handle_data(self, data):
        if self.in_table and self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = " ".join(self.cell["text"].split())
            self.row["cells"].append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_table = False


FP_STATS = {
    ("PASSING", "ATT"): "pass_att", ("PASSING", "CMP"): "pass_cmp",
    ("PASSING", "YDS"): "pass_yd", ("PASSING", "TDS"): "pass_td", ("PASSING", "INTS"): "pass_int",
    ("RUSHING", "ATT"): "rush_att", ("RUSHING", "YDS"): "rush_yd", ("RUSHING", "TDS"): "rush_td",
    ("RECEIVING", "REC"): "rec", ("RECEIVING", "YDS"): "rec_yd", ("RECEIVING", "TDS"): "rec_td",
    ("MISC", "FL"): "fum_lost", ("MISC", "FPTS"): None,
}


def normalize_fantasypros(body, position, season, week, crosswalk, scoring=None):
    parser = _ProjectionTable()
    parser.feed(body.decode("utf-8"))
    if len(parser.rows) < 3:
        raise ProviderError("FantasyPros public performance table unavailable")
    groups = [cell["text"].upper() for cell in parser.rows[0]["cells"] for _ in range(cell["span"])]
    headers = [cell["text"].upper() for cell in parser.rows[1]["cells"]]
    if len(groups) != len(headers) or headers[0] != "PLAYER":
        raise ProviderError("FantasyPros grouped table header changed")
    fields = []
    for group, header in zip(groups[1:], headers[1:]):
        if (group, header) not in FP_STATS:
            raise ProviderError(f"Unknown FantasyPros column {group}/{header}")
        fields.append(FP_STATS[group, header])
    scoring = EXPERIMENTAL_REFERENCE_SCORING if scoring is None else scoring
    rows, unmatched, seen = [], [], set()
    for obj in parser.rows[2:]:
        if not obj["fp_id"] or not obj["name"]:
            raise ProviderError("FantasyPros row missing identity")
        if obj["fp_id"] in seen:
            raise ProviderError("Duplicate FantasyPros player ID")
        seen.add(obj["fp_id"])
        cells = obj["cells"]
        if len(cells) != len(headers):
            raise ProviderError("FantasyPros data/header column count mismatch")
        source_stats = {field: _number(cell["text"], nullable=True) for field, cell in zip(fields, cells[1:]) if field}
        stats = {field: value for field, value in source_stats.items() if value is not None}
        if not any(key in stats for key in PERFORMANCE_KEYS):
            raise ProviderError("FantasyPros row has no numeric performance components")
        team = cells[0]["text"].removeprefix(obj["name"]).strip() or None
        pid, method = crosswalk.resolve(obj["fp_id"], obj["name"], position, team)
        row = {"source_id": "fantasypros", "source_player_id": obj["fp_id"], "player_id": pid,
               "name": obj["name"], "position": position, "fantasy_positions": [position], "team": team,
               "season": int(season), "week": week, "provider": "FantasyPros consensus",
               "projection_type": "season" if week is None else "weekly", "stats": stats,
               "mean": score_stats(stats, scoring), "scoring_hash": _scoring_hash(scoring),
               **_projection_score_metadata(source_stats, scoring), "identity_method": method,
               "provider_updated_at": parser.updated, "season_denominator": None,
               "timestamp_caveat": "Page update date has no verified timezone/vintage; observed_at controls availability.",
               "uncertainty_status": "Consensus mean only, event omissions and source dependence remain; FPTS ignored."}
        (rows if pid else unmatched).append(row)
    if not rows:
        raise ProviderError("No FantasyPros players matched the identity crosswalk")
    return rows, unmatched


SOURCE_METADATA = {
    "sleeper_rotowire": {"url": SLEEPER_BASE, "kind": "professional_projection_and_adp", "attribution": "Sleeper; response company=rotowire", "access": "Previously observed public unauthenticated non-v1 endpoint; further network refresh disabled", "refresh_policy": "cached_only_pending_written_permission", "license": "August 27, 2026 terms prohibit automated collection without written consent; documented v1 permission does not clearly cover projections", "terms_url": "https://sleeper.com/terms", "pit": "Current observed time only; old season is not old publication"},
    "fantasycalc": {"url": FC_URL, "kind": "market_value", "access": "Officially documented public /values/current", "refresh_policy": "cache_at_least_1_hour_prefer_daily", "license": "Personal noncommercial support work allowed; no material substitute; visible linked attribution required; only documented endpoints allowed", "terms_url": "https://fantasycalc.com/terms-of-usage", "api_docs": "https://fantasycalc.com/api-docs", "pit": "Observed time only; not a professional point forecast"},
    "dynastyprocess": {"url": DP_BASE, "kind": "expert_rank_derived_value", "refresh_policy": "weekly_current_immutable_archive_cache_forever", "license": "Repository GPL-3.0; underlying FantasyPros data rights remain distinct", "terms_url": "https://github.com/dynastyprocess/data/blob/master/LICENSE", "pit": "Current observed time; pinned historical commits retained but unsigned commit dates alone do not prove historical public availability"},
    "dynastyprocess_ids": {"url": DP_BASE + "/master/files/db_playerids.csv", "kind": "identity_crosswalk", "license": "Repository GPL-3.0", "pit": "Current ID-only crosswalk; do not use mutable age/team fields as historical features"},
    "fantasypros": {"url": "https://www.fantasypros.com/nfl/projections/", "kind": "professional_consensus_projection", "refresh_policy": "personal_public_page_cached_1_hour_no_registration_gate_bypass", "license": "Public page single copy for personal use; no redistribution. API access has separate free prototype/paid production requirements", "terms_url": "https://www.fantasypros.com/about/legal/", "pit": "Observed time only; historical page update may be after kickoff"},
}


def ingest_providers(warehouse, season=2026, weeks=(1,), force=False, offline=False,
                     scoring=None, include_fantasypros=True, archive_years=(2024, 2025),
                     sleeper_cached_only=True):
    """Fetch/reuse raw observations and publish independently validated partitions.

    Returns all successes and failures; it never quietly substitutes a different
    source. Re-run the same observation without duplicating model rows. Pass the
    fresh league scoring dict; omitted scoring uses an experimental reference profile, not the connected league.
    """
    scoring = EXPERIMENTAL_REFERENCE_SCORING if scoring is None else scoring
    weeks = tuple(dict.fromkeys(int(week) for week in weeks))
    if any(week < 1 or week > 18 for week in weeks):
        raise ProviderError("NFL regular-season projection weeks must be 1..18")
    fetcher = HttpFetcher(warehouse)
    result = {"season": int(season), "weeks": list(weeks), "scoring_hash": _scoring_hash(scoring),
              "sources": [], "failures": [], "warnings": [], "coverage": {}}
    for source_id, metadata in SOURCE_METADATA.items():
        warehouse.register_source(source_id, metadata)

    def fetch(source_id, url):
        immutable = bool(re.search(r'/[0-9a-f]{40}/',url))
        ttl = 365*86400 if immutable else 86400 if source_id=='dynastyprocess' else 3600
        raw = fetcher.fetch(source_id, url, force=force and source_id != "fantasycalc",
                            offline=offline or source_id == "sleeper_rotowire" and sleeper_cached_only,
                            ttl=ttl)
        body = warehouse.raw_bytes(raw["id"])
        headers = (raw.get("http_meta") or {}).get("headers") or {}
        if headers.get("content-encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return raw, body

    def publish(source_id, dataset, rows, raw, partition, keys, evidence=None):
        if not rows:
            return
        receipt = warehouse.publish(source_id, dataset, rows, raw_id=raw["id"],
                                    key_fields=tuple(key for key in keys if key != "week" or any(row.get("week") is not None for row in rows)), partition=partition,
                                    observed_at=raw["observed_at"],
                                    publication_evidence=evidence,
                                    required_fields=("source_id", "source_player_id"))
        result["sources"].append({"source_id": source_id, "dataset": dataset, "partition": partition,
                                  "raw_id": raw["id"], **receipt})

    for week in (None,) + weeks:
        partition = f"{season}/" + ("season" if week is None else f"week{week}")
        url = f"{SLEEPER_BASE}/{season}" + ("" if week is None else f"/{week}") + "?" + SLEEPER_QUERY
        try:
            raw, body = fetch("sleeper_rotowire", url)
            projections, adp, coverage = normalize_sleeper(json.loads(body), season, week, scoring)
            publish("sleeper_rotowire", "projections", projections, raw, partition,
                    ("source_id", "player_id", "season", "week", "projection_type"))
            publish("sleeper_rotowire", "adp", adp, raw, partition,
                    ("source_id", "player_id", "season", "week", "adp_type"))
            result["coverage"]["sleeper/" + partition] = coverage
        except Exception as exc:
            result["failures"].append({"source_id": "sleeper_rotowire", "partition": partition, "error": str(exc)})

    try:
        raw, body = fetch("fantasycalc", FC_URL)
        rows, unmatched = normalize_fantasycalc(json.loads(body), season)
        keys = ("source_id", "source_player_id", "season", "format", "value_type")
        publish("fantasycalc", "consensus_values", rows, raw, str(season), keys)
        publish("fantasycalc", "provider_unmatched", unmatched, raw, str(season), keys)
        result["coverage"]["fantasycalc"] = {"matched": len(rows), "unmatched": len(unmatched)}
    except Exception as exc:
        result["failures"].append({"source_id": "fantasycalc", "error": str(exc)})

    crosswalk = None
    try:
        raw, body = fetch("dynastyprocess_ids", DP_BASE + "/master/files/db_playerids.csv")
        records = _csv_rows(body)
        if not records or not {"sleeper_id", "fantasypros_id", "name", "position"} <= records[0].keys():
            raise ProviderError("DynastyProcess crosswalk schema changed")
        crosswalk = IdentityCrosswalk(records)
        result["coverage"]["crosswalk"] = {"rows": len(records), "raw_id": raw["id"]}
    except Exception as exc:
        result["failures"].append({"source_id": "dynastyprocess_ids", "error": str(exc)})

    if crosswalk is not None:
        snapshots = [(season, "master", None)]
        snapshots += [(year, ARCHIVED_DP[year][0], ARCHIVED_DP[year][1]) for year in archive_years if year in ARCHIVED_DP and year != season]
        for year, version, commit_date in snapshots:
            try:
                url = DP_BASE + f"/{version}/files/values.csv"
                raw, body = fetch("dynastyprocess", url)
                rows, unmatched = normalize_dynastyprocess(body, year, crosswalk)
                evidence = None
                if commit_date:
                    evidence = {"verified": False, "kind": "git_commit", "commit": version,
                                "commit_date": commit_date, "url": url,
                                "reason": "Unsigned git metadata proves content identity, not independent public posting time; availability stays observed."}
                    for row in rows + unmatched:
                        row["archive_commit"] = version
                        row["archive_commit_date"] = commit_date
                keys = ("source_id", "source_player_id", "season", "format", "value_type")
                publish("dynastyprocess", "consensus_values", rows, raw, str(year), keys, evidence)
                publish("dynastyprocess", "provider_unmatched", unmatched, raw, str(year), keys, evidence)
                result["coverage"][f"dynastyprocess/{year}"] = {"matched": len(rows), "unmatched": len(unmatched)}
            except Exception as exc:
                result["failures"].append({"source_id": "dynastyprocess", "partition": str(year), "error": str(exc)})

        if include_fantasypros:
            for position in sorted(SKILL_POSITIONS):
                try:
                    url = f"https://www.fantasypros.com/nfl/projections/{position.lower()}.php?week=draft&year={season}&scoring=PPR"
                    raw, body = fetch("fantasypros", url)
                    rows, unmatched = normalize_fantasypros(body, position, season, None, crosswalk, scoring)
                    keys = ("source_id", "source_player_id", "season", "week", "projection_type")
                    publish("fantasypros", "projections", rows, raw, f"{season}/{position}", keys)
                    publish("fantasypros", "provider_unmatched", unmatched, raw, f"{season}/{position}", keys)
                    result["coverage"]["fantasypros/" + position] = {"matched": len(rows), "unmatched": len(unmatched)}
                except Exception as exc:
                    result["failures"].append({"source_id": "fantasypros", "partition": position, "error": str(exc)})

    result["warnings"].extend([
        "Undocumented Sleeper projections refresh is cached-only by default pending written permission; documented v1 league API has a separate noncommercial invitation.",
        "ADP and market values are observations/hypotheses, never optimized performance targets.",
        "Sleeper season gp=18 was observed; no division by gp or fabricated per-week distribution is performed.",
        "Projection missingness is not zero ability. Historical current endpoints are not point-in-time forecast archives.",
        "FantasyPros omits some scored event categories; its mean is a subtotal with explicit missing_scoring_fields.",
        "Providers may share inputs (Sleeper identifies Rotowire; FantasyPros contributors not proven independent).",
    ])
    result["status"] = "partial" if result["failures"] else "ok"
    return result
