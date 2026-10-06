"""Exploratory annual production transitions, not medical career survival.

Empirical distributions are partially pooled using linear credibility variance
components. No age, position, or rookie value premium is supplied as a prior.
The numeric output is a scenario input; prospective policy validation is absent.
"""
from collections import defaultdict
from datetime import date
import math
import random
import statistics
import time

from .modeling import (ModelingError, POSITIONS, CORE_OFFENSE, _id, _num, _regular,
                       _score, _position, temporal_audit)
from .store import digest


def _mean(values):
    return statistics.mean(values) if values else None


def _quantile(values, fraction):
    values = sorted(values)
    index = (len(values)-1)*fraction
    lo = int(index)
    hi = min(lo+1, len(values)-1)
    return values[lo] + (index-lo)*(values[hi]-values[lo])


def _age(row, season):
    born = row.get("birth_date")
    if born:
        try:
            birth = date.fromisoformat(str(born)[:10])
            return (date(int(season), 9, 1)-birth).days/365.2425
        except ValueError:
            pass
    return _num(row.get("age"))


def _age_band(age):
    return "unknown" if age is None else str(5*math.floor(age/5)) + "-" + str(5*math.floor(age/5)+4)


def _output_bin(value, edges):
    return sum(value > edge for edge in edges)


def _cohort_key(position, age_band, output_bin):
    return f"{position}|age={age_band}|output={output_bin}"


def fit_horizon(weekly_rows, roster_rows, schedules, scoring, *,
                train_base_seasons=range(2018, 2025), cutoff=None,
                complete_stats_snapshot=False, upper_quantile=0.99,
                expected_season_game_counts=None):
    """Fit annual total-output ratios with an explicit full-snapshot contract.

    Each entrant must have observed roster membership in the base season.
    The next season is included only if every scheduled regular NFL game is
    completed and represented in the supplied statistics table. Under the
    explicit complete-snapshot contract, no next-year events means zero recorded
    output, not retirement. Positive base production is required for a ratio.
    """
    if not complete_stats_snapshot:
        raise ModelingError("Annual zero-output states require an explicit complete statistics snapshot assertion")
    if not 0.5 <= upper_quantile <= 1:
        raise ModelingError("upper_quantile must lie between 0.5 and 1")
    weekly_rows, roster_rows, schedules = list(weekly_rows), list(roster_rows), list(schedules)
    base_seasons = sorted(set(map(int, train_base_seasons)))
    if not base_seasons:
        raise ModelingError("Explicit base seasons required")
    required_seasons = set(base_seasons) | {y+1 for y in base_seasons}
    # Counts independently checked in the full nflverse schedules snapshot.
    # The 2022 snapshot has 271 completed REG games; BUF/CIN have 16 each.
    expected_counts = ({2018: 256, 2019: 256, 2020: 256, 2021: 272,
                        2022: 271, 2023: 272, 2024: 272, 2025: 272}
                       if expected_season_game_counts is None else
                       {int(k): int(v) for k, v in expected_season_game_counts.items()})
    schedule_by_year = defaultdict(list)
    team_games = defaultdict(int)
    for row in schedules:
        if _regular(row) and int(row["season"]) in required_seasons:
            schedule_by_year[int(row["season"])].append(row)
            if row.get("home_score") is not None and row.get("away_score") is not None:
                for team in (row["home_team"], row["away_team"]):
                    team_games[(int(row["season"]), team)] += 1
    observed_games = defaultdict(set)
    totals = defaultdict(float)
    stat_meta, seen = {}, set()
    missing_scoring = defaultdict(int)
    for row in weekly_rows:
        if not _regular(row) or int(row["season"]) not in required_seasons:
            continue
        year = int(row["season"])
        if row.get("game_id"):
            observed_games[year].add(row["game_id"])
        if _position(row) not in POSITIONS or not row.get("identity_verified", True):
            continue
        key = (_id(row), year)
        event_key = (*key, int(row["week"]))
        if event_key in seen:
            raise ModelingError("Duplicate player-season-week statistics")
        seen.add(event_key)
        value = _score(row, scoring)
        for missing in value["missing_scoring_keys"]:
            missing_scoring[missing] += 1
        if set(value["missing_scoring_keys"]) & CORE_OFFENSE:
            raise ModelingError("Missing core scoring fields in horizon fit")
        totals[key] += value["points"]
        stat_meta[key] = row
    completeness = {}
    year_game_denominator = {}
    for year in sorted(required_seasons):
        records = schedule_by_year.get(year, [])
        completed = [r for r in records if r.get("home_score") is not None and r.get("away_score") is not None]
        missing = sorted({r["game_id"] for r in completed} - observed_games[year])
        teams = {team for y, team in team_games if y == year}
        # A historically canceled game can remain on the schedule with no scores.
        # Permit only explicitly labeled cancellation; never assume a missing
        # future result is canceled or use incomplete seasons as zero outcomes.
        unresolved = [r["game_id"] for r in records if r not in completed and
                      str(r.get("game_status", r.get("status", ""))).lower() not in {"cancelled", "canceled"}]
        valid = (bool(records) and len(teams) == 32 and not missing and not unresolved
                 and len(completed) == expected_counts.get(year))
        completeness[year] = {"usable": valid, "completed_games": len(completed),
                              "expected_completed_games": expected_counts.get(year),
                              "teams": len(teams), "missing_stats_games": missing,
                              "unresolved_schedule_games": unresolved}
        if completed:
            year_game_denominator[year] = 2*len(completed)/len(teams)
    # Stable exact identity, roster membership and age; no fuzzy name matching.
    membership = defaultdict(list)
    for row in roster_rows:
        if (_regular(row) and _position(row) in POSITIONS and row.get("identity_verified", True)
                and int(row["season"]) in required_seasons):
            membership[(_id(row), int(row["season"]))].append(row)
    transitions, exclusions = [], defaultdict(int)
    for (pid, year), members in sorted(membership.items()):
        if year not in base_seasons:
            continue
        if not completeness[year]["usable"] or not completeness[year+1]["usable"]:
            exclusions["incomplete_season_pair"] += 1
            continue
        meta = min(members, key=lambda r: int(r.get("week") or 0))
        base_total = totals.get((pid, year), 0.0)
        if base_total <= 0:
            exclusions["nonpositive_base_no_identifiable_ratio"] += 1
            continue
        # Most players have one team. For a multi-team player, exposure is the
        # season-wide mean completed games/team, not number of roster rows.
        base_teams = {r.get("team") for r in members if (year, r.get("team")) in team_games}
        base_den = team_games[(year, next(iter(base_teams)))] if len(base_teams) == 1 else year_game_denominator[year]
        next_members = membership.get((pid, year+1), [])
        next_teams = {r.get("team") for r in next_members if (year+1, r.get("team")) in team_games}
        next_den = team_games[(year+1, next(iter(next_teams)))] if len(next_teams) == 1 else year_game_denominator[year+1]
        next_total = totals.get((pid, year+1), 0.0)
        # Negative annual totals cannot be represented as nonnegative production
        # multipliers; keep count and conservatively collapse them to zero.
        if next_total < 0:
            exclusions["negative_next_total_collapsed_to_zero"] += 1
        base_mean, next_mean = base_total/base_den, max(0.0, next_total)/next_den
        age = _age(meta, year)
        exp = _num(meta.get("years_exp"))
        transitions.append({"player_id": pid, "season": year, "position": _position(meta),
                            "age": age, "age_band": _age_band(age), "years_exp": exp,
                            "baseline_mean": base_mean, "next_mean": next_mean,
                            "base_denominator": base_den, "next_denominator": next_den,
                            "next_roster_membership_observed": bool(next_members),
                            "next_stat_record_observed": (pid, year+1) in totals,
                            "raw_ratio": next_mean/base_mean})
    if not transitions:
        raise ModelingError("No complete-season positive-base transitions available")
    position_rows = defaultdict(list)
    for row in transitions:
        position_rows[row["position"]].append(row)
    positions, cells = {}, {}
    for position, records in sorted(position_rows.items()):
        levels = [r["baseline_mean"] for r in records]
        edges = [_quantile(levels, q) for q in (.25, .5, .75)]
        ratios = [r["raw_ratio"] for r in records]
        ceiling = _quantile(ratios, upper_quantile)
        grouped = defaultdict(list)
        for row in records:
            row["ratio"] = min(row["raw_ratio"], ceiling)
            row["output_bin"] = _output_bin(row["baseline_mean"], edges)
            grouped[_cohort_key(position, row["age_band"], row["output_bin"])].append(row["ratio"])
        # Method-of-moments credibility. Ratios from repeating player seasons
        # are dependent; this is a transparent exploratory regularizer, not a
        # correctly specified posterior or a calibrated career distribution.
        pool = [r["ratio"] for r in records]
        output_pools = {}
        for output_bin in sorted({r["output_bin"] for r in records}):
            output_records = [r for r in records if r["output_bin"] == output_bin]
            output_samples = [r["ratio"] for r in output_records]
            selected_groups = {key: values for key, values in grouped.items() if key.endswith(f"output={output_bin}")}
            groups = list(selected_groups.values())
            denominator = sum(max(0, len(values)-1) for values in groups)
            within = sum(sum((v-_mean(values))**2 for v in values) for values in groups)/denominator if denominator else 0.0
            group_means = [_mean(values) for values in groups]
            between = max(0.0, (statistics.variance(group_means) if len(groups)>1 else 0.0)
                          - _mean([within/len(values) for values in groups]))
            output_pools[str(output_bin)] = {"n": len(output_samples), "samples": output_samples,
                "ratio_mean": _mean(output_samples), "zero_fraction": sum(r == 0 for r in output_samples)/len(output_samples),
                "within_variance": within, "between_variance": between,
                "novice_samples": [r["ratio"] for r in output_records if r["years_exp"] is not None and r["years_exp"] <= 1]}
            for key, values in selected_groups.items():
                z = between/(between+within/len(values)) if between+within/len(values) else 0.0
                cells[key] = {"n": len(values), "samples": values, "credibility": z,
                              "sample_mean": _mean(values), "pooled_mean": z*_mean(values)+(1-z)*_mean(output_samples),
                              "shrinkage_parent": f"{position}|output={output_bin}"}
        novice = [r["ratio"] for r in records if r["years_exp"] is not None and r["years_exp"] <= 1]
        positions[position] = {"n": len(records), "samples": pool, "novice_samples": novice,
                               "output_edges": edges, "output_pools": output_pools,
                               "upper_quantile": upper_quantile, "upper_ratio": ceiling,
                               "truncated_count": sum(r > ceiling for r in ratios),
                               "raw_ratio_mean": _mean(ratios), "ratio_mean": _mean(pool),
                               "zero_fraction": sum(r == 0 for r in pool)/len(pool),
                               "tail_sensitivity": {str(q): _mean([min(r, _quantile(ratios, q)) for r in ratios]) for q in (.95, .99, 1.0)}}
    fitted_rows = [r for r in weekly_rows if int(r["season"]) in required_seasons]
    audit = temporal_audit(fitted_rows+roster_rows, cutoff) if cutoff is not None else {"passed": False, "reason": "no_cutoff"}
    result = {"schema_version": 1, "created_at": time.time(), "scoring_hash": digest(scoring),
              "base_seasons": base_seasons, "last_outcome_season": max(base_seasons)+1,
              "positions": positions, "cohorts": cells, "transitions": transitions,
              "transition_count": len(transitions), "completeness": completeness,
              "exclusions": dict(exclusions), "missing_scoring_keys": dict(missing_scoring),
              "training_temporal_audit": audit, "upper_quantile": upper_quantile,
              "target": "next annual recorded offensive points per completed team game / prior annual recorded offensive points per completed team game",
              "evidence_status": "exploratory_production_transition_sensitivity_not_validated_titles_or_medical_survival",
              "limitations": ["Risk set begins with observed NFL roster membership and positive base output; undrafted/nonproductive entrants have no identifiable multiplicative ratio.",
                              "Zero means zero recorded scored output in a complete statistics snapshot, not adjudicated retirement or injury.",
                              "Age bands and output quartiles are registered modeling choices, not evidence that age or any position is mispriced.",
                              "Repeated player-season ratios violate independence; credibility is a regularizer, not calibrated posterior uncertainty.",
                              "Upper-tail winsorization is an explicit robustness assumption; raw, 95th and 99th percentile mean sensitivities are reported.",
                              "Missing rare special-teams scoring fields are audited; these transitions are exact only for available mapped components.",
                              "Historical bulk tables first observed now cannot establish historical point-in-time policy validation.",
                              "Applying realized-output ratios around a professional forecast assumes conditional transfer across baseline measurement types; validate prospectively."]}
    result["limitations"].append("Broad position ratio pooling failed a tail diagnostic and is retained only as a stress case; the operational parent is position plus baseline-output quartile, since tiny denominators otherwise inflate established-player growth.")
    result["id"] = digest({k: v for k, v in result.items() if k != "created_at"})
    return result


def transition_for_player(model, player, *, baseline_mean=None, season=None, draws=1024, seed=0):
    """Return a reproducible finite mixture sample and its exact mixture mean.

    Call again with updated age and output after each annual transition to obtain
    a coarse Markov scenario. Reusing this result freezes the current cohort and
    must be disclosed by the simulator. This function never simulates injury.
    """
    if draws < 2:
        raise ModelingError("At least two transition draws required")
    legal = [p for p in player.get("fantasy_positions", []) if p in POSITIONS]
    position = _position(player)
    if position not in model["positions"]:
        position = legal[0] if legal else position
    pool = model["positions"].get(position)
    if not pool:
        return {"status": "unsupported_position", "year_transition_samples": []}
    season = int(season or model["last_outcome_season"]+1)
    if season <= model["last_outcome_season"]:
        raise ModelingError("Transition forecast season must follow all fitted outcomes")
    mean = _num(baseline_mean if baseline_mean is not None else player.get("mean"))
    if mean is None or mean <= 0:
        return {"status": "unsupported_nonpositive_baseline", "year_transition_samples": [],
                "reason": "Multiplicative model cannot create a role from a zero base forecast"}
    pid = str(player.get("gsis_id") or player.get("nflverse_id") or _id(player))
    has_history = any(r["player_id"] == pid for r in model["transitions"])
    age = _age(player, season)
    output_bin = _output_bin(mean, pool["output_edges"])
    key = _cohort_key(position, _age_band(age), output_bin)
    cell = model["cohorts"].get(key)
    parent = pool["output_pools"][str(output_bin)]
    unknown = not has_history
    if unknown:
        specific = parent["novice_samples"] or parent["samples"]
        z = 1.0
        method = "novice_positive_production_cohort_transfer_to_professional_baseline"
    else:
        specific = (cell or {}).get("samples") or parent["samples"]
        z = (cell or {}).get("credibility", 0.0)
        method = "age_and_output_cohort_distribution_partially_pooled_to_position"
    rng = random.Random(seed)
    samples = [rng.choice(specific if rng.random() < z else parent["samples"]) for _ in range(draws)]
    exact_mean = z*_mean(specific)+(1-z)*_mean(parent["samples"])
    zero_probability = z*sum(x == 0 for x in specific)/len(specific)+(1-z)*parent["zero_fraction"]
    return {"status": "exploratory_sensitivity", "year_transition_samples": samples,
            "annual_expected_multiplier": exact_mean, "zero_output_probability": zero_probability,
            "sample_mean": _mean(samples), "cohort_key": key, "cohort_n": len(specific),
            "position_n": pool["n"], "credibility": z, "method": method,
            "shrinkage_parent": f"{position}|output={output_bin}", "shrinkage_parent_n": parent["n"],
            "unknown_history_transfer": unknown, "baseline_mean": mean, "age": age,
            "winsor_upper_ratio": pool["upper_ratio"], "horizon_model_id": model["id"],
            "application": "Multiply the entire annual score process including noise by the annual draw; zero draw means every output zero. Do not add a separate injury/availability haircut.",
            "limitations": ["Using a professional expected baseline to index cohorts fitted from realized historical baselines is an unvalidated measurement transfer.",
                            "Conditional cohort transition draws assume the state summary captures dependence; latent persistent frailty and future interventions are omitted.",
                            "Unknown-history novice samples exclude novice seasons with no positive output; this is a selected cohort, not a calibrated rookie breakout probability."]}
