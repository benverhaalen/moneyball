"""Measured forecasts and honest evaluation for the local Moneyball substrate.

No network calls or third-party dependencies.  Callers supply warehouse rows.
Bulk historical files can support exploratory modeling, never retroactively
become point-in-time forecasts.  A missing statistics row is not an absence.

Methods: Buhlmann (1967) linear credibility; Brown (2008) temporal field tests;
Hilden et al. (2023) separated-window reliability; Gneiting/Raftery (2007)
proper scores.  See .moneyball/research/tournament-forecasting-notes.md.
"""
from collections import defaultdict
import copy
from datetime import datetime, timezone
import math
import random
import statistics
import time

from .store import digest

SCHEMA_VERSION = 1
POSITIONS = {"QB", "RB", "WR", "TE"}
# Modern nflverse stats_player_week_2025.csv header was read on 2026-09-07.
# Values are alternative *whole* mappings, never summed across alternatives.
NFLVERSE_FIELDS = {
    "pass_yd": [("passing_yards",)], "pass_td": [("passing_tds",)],
    "pass_int": [("passing_interceptions",), ("interceptions",)],
    "pass_2pt": [("passing_2pt_conversions",)],
    "rush_yd": [("rushing_yards",)], "rush_td": [("rushing_tds",)],
    "rush_2pt": [("rushing_2pt_conversions",)],
    "rec": [("receptions",)], "rec_yd": [("receiving_yards",)],
    "rec_td": [("receiving_tds",)], "rec_2pt": [("receiving_2pt_conversions",)],
    "fum_lost": [("fumbles_lost_total",),
                 ("sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost")],
    "fum": [("fumbles_total",), ("sack_fumbles", "rushing_fumbles", "receiving_fumbles")],
    "fum_rec_td": [("fumble_recovery_tds",)],
    "st_td": [("special_teams_tds",)],
    "fgm_0_19": [("fg_made_0_19",)], "fgm_20_29": [("fg_made_20_29",)],
    "fgm_30_39": [("fg_made_30_39",)], "fgm_40_49": [("fg_made_40_49",)],
    "fgm_50_59": [("fg_made_50_59",)], "fgm_60p": [("fg_made_60_",)],
    "fgmiss": [("fg_missed",)], "xpm": [("pat_made",)], "xpmiss": [("pat_missed",)],
}
CORE_OFFENSE = {"pass_yd", "pass_td", "pass_int", "pass_2pt", "rush_yd",
                "rush_td", "rush_2pt", "rec", "rec_yd", "rec_td", "rec_2pt", "fum_lost", "fum"}
# ST forced-fumble/recovery classification needs play-level evidence; do not
# silently map all fumble recoveries to a special-teams scoring key.
OFFENSE_APPLICABLE = CORE_OFFENSE | {"st_td", "st_ff", "st_fum_rec", "fum_rec_td"}
METRICS = {
    "targets": (("targets", "rec_tgt"), None),
    "carries": (("carries", "rush_att"), None),
    "pass_attempts": (("attempts", "pass_att"), None),
    "receptions": (("receptions", "rec"), None),
    "receiving_yards_per_target": (("receiving_yards", "rec_yd"), ("targets", "rec_tgt")),
    "rushing_yards_per_carry": (("rushing_yards", "rush_yd"), ("carries", "rush_att")),
    "completion_rate": (("completions", "pass_cmp"), ("attempts", "pass_att")),
    "passing_yards_per_attempt": (("passing_yards", "pass_yd"), ("attempts", "pass_att")),
    "passing_td_rate": (("passing_tds", "pass_td"), ("attempts", "pass_att")),
    "target_share": (("target_share",), None),
    "offense_snap_share": (("offense_pct", "offense_snap_share"), None),
}


class ModelingError(ValueError):
    """Invalid, unregistered, or insufficiently evidenced model request."""


def _num(x):
    if x is None or x == "" or isinstance(x, bool):
        return None
    try:
        result = float(x)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _mean(xs, default=None):
    return statistics.fmean(xs) if xs else default


def _var(xs):
    return statistics.variance(xs) if len(xs) > 1 else 0.0


def _id(row):
    value = row.get("player_id", row.get("id", row.get("gsis_id")))
    if value is None or str(value) == "":
        raise ModelingError("Every modeling row must carry a stable player_id")
    return str(value)


def _position(row):
    return row.get("position") or (row.get("player") or {}).get("position") or "UNKNOWN"


def _stats(row):
    return row.get("stats") if isinstance(row.get("stats"), dict) else row


def _stamp(value):
    if value is None:
        return None
    n = _num(value)
    if n is not None:
        return n / 1000 if n > 100_000_000_000 else n
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None  # no silently guessed timezone
        return d.timestamp()
    except ValueError:
        return None


def _first(stats, keys):
    for key in keys:
        value = _num(stats.get(key))
        if value is not None:
            return value
    return None


def score_stats(stats, scoring, *, source_schema="auto", applicable_keys=None,
                sparse_zero=False, strict=False):
    """Apply supplied league coefficients, retaining a component coverage audit.

    `sparse_zero=True` is an explicit caller assertion about a sparse source.
    It is never inferred from missing fields. Native Sleeper keys win; aggregate
    nflverse fumbles_lost_total wins over its components to avoid double counts.
    Scoring bonuses/ranges are supported only when the source supplies the
    corresponding event-count key; this function does not invent those counts.
    """
    if not isinstance(stats, dict) or not isinstance(scoring, dict):
        raise ModelingError("stats and scoring must be dictionaries")
    if source_schema not in {"auto", "sleeper", "nflverse"}:
        raise ModelingError("Unknown scoring schema")
    schema = source_schema
    if schema == "auto":
        schema = "nflverse" if any(k in stats for k in ("passing_yards", "receptions", "fumbles_lost_total")) else "sleeper"
    keys = set(scoring) if applicable_keys is None else set(scoring) & set(applicable_keys)
    contributions, used, missing, missing_fields = {}, {}, [], {}
    for key in sorted(keys):
        coefficient = _num(scoring[key])
        if coefficient is None:
            raise ModelingError(f"Nonfinite scoring coefficient: {key}")
        if coefficient == 0:
            continue
        fields = (key,) if _num(stats.get(key)) is not None else None
        if fields is None and schema == "nflverse":
            for alternative in NFLVERSE_FIELDS.get(key, []):
                if all(_num(stats.get(f)) is not None for f in alternative):
                    fields = alternative
                    break
        if fields is None:
            if sparse_zero and schema == "sleeper":
                contributions[key], used[key] = 0.0, []
            else:
                missing.append(key)
                missing_fields[key] = [list(x) for x in NFLVERSE_FIELDS.get(key, [])]
            continue
        contributions[key] = coefficient * sum(float(stats[f]) for f in fields)
        used[key] = list(fields)
    result = {"points": sum(contributions.values()), "components": contributions,
              "source_fields": used, "missing_scoring_keys": missing,
              "missing_source_fields": missing_fields, "complete": not missing,
              "source_schema": schema, "sparse_zero_asserted": bool(sparse_zero),
              "scoring_hash": digest(scoring), "applicable_keys": sorted(keys)}
    if strict and missing:
        raise ModelingError("Incomplete scoring coverage: " + ", ".join(missing))
    return result


def _score(row, scoring):
    return score_stats(_stats(row), scoring, applicable_keys=OFFENSE_APPLICABLE,
                       sparse_zero=bool(row.get("sparse_zero_verified")))


def _regular(row):
    return str(row.get("season_type", row.get("game_type", "REG"))).upper() in {"REG", "REGULAR"}


def _availability(row):
    # Requires an explicit at-risk denominator produced by a roster/schedule join.
    if row.get("at_risk") is not True:
        return None
    for key in ("available", "active"):
        if isinstance(row.get(key), bool):
            return int(row[key])
        if row.get(key) in (0, 1, "0", "1"):
            return int(row[key])
    return None


def _group(row):
    # This is a measurable cohort hypothesis, not a positional value prior.
    legal = [p for p in row.get("fantasy_positions", []) if p in POSITIONS]
    position = _position(row)
    return str(row.get("cohort") or (legal[0] if position not in POSITIONS and legal else position))


def _ensure_unique(rows):
    seen = set()
    for row in rows:
        key = (_id(row), int(row["season"]), int(row["week"]))
        if key in seen:
            raise ModelingError(f"Duplicate player-season-week row: {key}; select one source/vintage first")
        seen.add(key)


def temporal_audit(rows, cutoff, *, require_vintage=True):
    """Check knowability, not just event date. Missing evidence is a failure."""
    t = _stamp(cutoff)
    if t is None:
        raise ModelingError("A timezone-aware cutoff or epoch timestamp is required")
    counts = defaultdict(int)
    examples = []
    for row in rows:
        p = row.get("_provenance", {})
        available = _stamp(p.get("available_at", row.get("available_at")))
        observed = _stamp(p.get("observed_at", row.get("observed_at")))
        reasons = []
        if available is None:
            reasons.append("missing_available_at")
        elif available > t:
            reasons.append("not_available_at_cutoff")
        # A body actually saved before the cutoff is a real forward vintage.
        # Historical publication claims earlier than first observation still
        # need independent verification; a current mutable file cannot backdate.
        observed_by_cutoff = observed is not None and observed <= t
        if require_vintage and not observed_by_cutoff and p.get("vintage_verified", row.get("vintage_verified")) is not True:
            reasons.append("unverified_historical_vintage")
        for reason in reasons:
            counts[reason] += 1
        if reasons and len(examples) < 5:
            examples.append({"player_id": _id(row), "reasons": reasons})
    return {"passed": not counts, "cutoff": t, "row_count": len(rows),
            "failure_counts": dict(counts), "examples": examples,
            "rule": "available_at <= cutoff and (body observed by cutoff or independently verified historical vintage)"}


def _residual_factors(scored):
    """Positive common-factor approximation; signed diagnostics remain visible."""
    seasons = defaultdict(list)
    for row, points in scored:
        seasons[(_id(row), int(row["season"]))].append(points)
    means = {key: _mean(v) for key, v in seasons.items()}
    games = defaultdict(list)
    residuals = defaultdict(list)
    for row, points in scored:
        e = points - means[(_id(row), int(row["season"]))]
        residuals[_group(row)].append(e)
        game = row.get("game_id")
        team = row.get("team", row.get("recent_team"))
        if game and team:
            games[(int(row["season"]), int(row["week"]), str(game))].append((str(team), _position(row), e))
    same, opposing, pairs = [], [], defaultdict(list)
    pair_moments = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0, 0.0])
    for values in games.values():
        for i, (team, pos, e) in enumerate(values):
            for team2, pos2, e2 in values[i + 1:]:
                key = ("same_team" if team == team2 else "opponents") + ":" + "/".join(sorted([pos, pos2]))
                pairs[key].append(e * e2)
                x, y = (e, e2) if pos <= pos2 else (e2, e)
                m = pair_moments[key]
                for k, v in enumerate([1, x, y, x*x, y*y, x*y]):
                    m[k] += v
                (same if team == team2 else opposing).append(e * e2)
    game_cov = max(0.0, _mean(opposing, 0.0))
    team_cov = max(0.0, _mean(same, 0.0) - game_cov)
    correlations = {}
    for key, (n, sx, sy, sxx, syy, sxy) in pair_moments.items():
        xx, yy, xy = sxx - sx*sx/n, syy-sy*sy/n, sxy-sx*sy/n
        correlations[key] = {"pairs": n, "correlation": xy / math.sqrt(xx*yy) if xx > 0 and yy > 0 else None,
                             "covariance": xy / (n-1) if n > 1 else None}
    return {"method": "positive game/team factors fitted to centered cross-products",
            "game_variance": game_cov, "team_variance": team_cov,
            "same_team_pairs": len(same), "opposing_pairs": len(opposing),
            "pair_covariances": {k: {"covariance": _mean(v), "pairs": len(v)} for k, v in sorted(pairs.items())},
            "empirical_pair_correlations": correlations,
            "limitations": ["Player-season means use final training-season observations; exploratory residual fit.",
                             "Negative and position-specific correlations are diagnostics, not reproduced by positive factors.",
                             "Estimated factor variance is capped per player; this is an approximation, not fitted full covariance."]}, residuals


def fit_model(rows, scoring, *, train_seasons, cutoff=None, forecast_rows=None, registry=None):
    """Fit JSON-safe empirical-Bayes active-output and explicit availability models.

    Cohort hypotheses and variance components are learned only from selected
    train seasons. Outcomes lacking required scoring fields are rejected, while
    unresolved special-teams components are made visible in `scoring_audit`.
    """
    train_seasons = sorted(set(int(s) for s in train_seasons))
    if not train_seasons:
        raise ModelingError("Explicit nonempty train_seasons required")
    selected = [r for r in rows if int(r["season"]) in train_seasons and _regular(r) and _position(r) in POSITIONS]
    if not selected:
        raise ModelingError("No eligible observations in train seasons")
    _ensure_unique(selected)
    by_player, by_group, scored, missing = defaultdict(list), defaultdict(list), [], defaultdict(int)
    availability_group, availability_player = defaultdict(list), defaultdict(list)
    availability_player_group = {}
    for row in selected:
        a = _availability(row)
        if a is not None:
            availability_group[_group(row)].append(a)
            availability_player[_id(row)].append(a)
            availability_player_group[_id(row)] = _group(row)
        if a == 0:
            continue  # evidenced at-risk absence need not invent a statistics row
        audit = _score(row, scoring)
        missing_core = CORE_OFFENSE & set(audit["missing_scoring_keys"])
        if missing_core:
            raise ModelingError("Missing core scoring fields for " + _id(row) + ": " + ",".join(sorted(missing_core)))
        for key in audit["missing_scoring_keys"]:
            missing[key] += 1
        value = audit["points"]
        by_player[_id(row)].append((row, value))
        by_group[_group(row)].append((row, value))
        scored.append((row, value))
    if not scored:
        raise ModelingError("No active output observations")
    factors, residuals = _residual_factors(scored)
    groups = {}
    for group, records in by_group.items():
        units = defaultdict(list)
        for row, value in records:
            units[(_id(row), int(row["season"]))].append(value)
        unit_means = [_mean(v) for v in units.values()]
        dof = sum(max(0, len(v) - 1) for v in units.values())
        within = sum(_var(v) * (len(v) - 1) for v in units.values() if len(v) > 1) / dof if dof else _var([v for _, v in records])
        tau = max(0.0, _var(unit_means) - _mean([within / len(v) for v in units.values()], 0))
        av = availability_group[group]
        groups[group] = {"mean": _mean(unit_means), "within_variance": within,
                         "between_variance": tau, "units": len(units), "observations": len(records),
                         "residuals": residuals[group], "availability": _mean(av), "availability_n": len(av)}
    players = {}
    for pid, records in by_player.items():
        records.sort(key=lambda r: (int(r[0]["season"]), int(r[0]["week"])))
        latest_season = max(int(row["season"]) for row, _ in records)
        recent = [(row, value) for row, value in records if int(row["season"]) == latest_season]
        last = recent[-1][0]
        group = _group(last)
        g = groups[group]
        values = [value for _, value in recent]
        n = len(values)
        denominator = g["between_variance"] + g["within_variance"] / n
        z = g["between_variance"] / denominator if denominator else 0.0
        mean = z * _mean(values) + (1 - z) * g["mean"]
        # A group-sized empirical credibility exposure, not an invented 80% health prior.
        av = availability_player[pid]
        availability = None
        if g["availability"] is not None:
            av_units = [len(v) for p, v in availability_player.items() if p in by_player and _group(by_player[p][-1][0]) == group]
            prior_n = statistics.median(av_units) if av_units else 0
            availability = (sum(av) + prior_n * g["availability"]) / (len(av) + prior_n) if len(av) + prior_n else None
        player_residuals = [value - _mean(values) for value in values]
        players[pid] = {"player_id": pid, "position": _position(last), "group": group,
                        "team": last.get("team", last.get("recent_team")), "mean": mean,
                        "raw_mean": _mean(values), "cohort_mean": g["mean"], "credibility": z,
                        "sd": math.sqrt(max(0, g["within_variance"])),
                        "mean_parameter_variance": (1 - z) * g["between_variance"],
                        "observations": n, "last_observed_season": latest_season,
                        "availability": availability, "availability_n": len(av),
                        "residuals": player_residuals}
    train_audit = temporal_audit(selected, cutoff) if cutoff is not None else {"passed": False, "reason": "no temporal cutoff supplied"}
    participation = {}
    for pid, values in availability_player.items():
        if not values:
            continue
        group = availability_player_group[pid]
        group_values = availability_group[group]
        prior_n = statistics.median([len(v) for p, v in availability_player.items() if availability_player_group.get(p) == group])
        participation[pid] = {"group": group, "n": len(values), "raw_probability": _mean(values),
                              "probability": (sum(values)+prior_n*_mean(group_values))/(len(values)+prior_n),
                              "prior_exposure": prior_n}
    model = {"schema_version": SCHEMA_VERSION, "method": "empirical linear credibility",
             "created_at": time.time(), "scoring": dict(scoring), "scoring_hash": digest(scoring),
             "training": {"seasons": train_seasons, "rows": len(selected), "active_rows": len(scored),
                          "temporal_audit": train_audit, "data_hash": digest(selected)},
             "groups": groups, "players": players, "factors": factors,
             "variance_bins": measure_variance_bins(selected, scoring),
             "participation_history": participation,
             "participation_target": sorted({r["availability_definition"] for r in selected if r.get("availability_definition")}),
             "scoring_audit": {"unmapped_applicable_keys": dict(missing), "complete": not missing},
             "calibration_status": "unvalidated_historical_fallback",
             "limitations": ["No historical PIT validation implied by fitting final historical outcomes.",
                              "Active-output means are not unconditional weekly means.",
                              "Missing weekly rows are not encoded as absence; availability requires explicit at-risk rows.",
                              "Position/cohort grouping is a testable predictive hypothesis, not a dynasty price prior.",
                              "Residual distribution around professional means is uncalibrated without PIT forecast-error records."]}
    if forecast_rows:
        model["forecast_rows_supplied"] = len(forecast_rows)
        model["limitations"].append("Supplied forecasts do not train residual corrections without registered PIT evaluation.")
    if registry:
        model["registry_id"] = registry.get("id")
    model["id"] = digest(model)
    return model


def _lookup(model, player):
    for key in (_id(player), player.get("gsis_id"), player.get("nflverse_id")):
        if key is not None and str(key) in model["players"]:
            return model["players"][str(key)]
    return None


def _skew(values):
    if len(values) < 3:
        return None
    mean = _mean(values)
    variance = _mean([(v - mean) ** 2 for v in values])
    return _mean([(v - mean) ** 3 for v in values]) / variance ** 1.5 if variance else 0.0


def residual_distribution(model, player):
    """Empirical cohort output uncertainty, never a fabricated personal floor.

    Historical observed-output residuals are NOT professional forecast errors.
    Using the cohort pool avoids false certainty from old zero-output/changed-
    role records without presenting an invented variance shrinkage parameter.
    A calibrated provider-error model can replace this explicit proxy later.
    """
    fitted = _lookup(model, player)
    group = _group(player)
    if group not in model["groups"]:
        group = (fitted or {}).get("group")
    cohort = model["groups"].get(group, {})
    values = cohort.get("residuals") or []
    if not values:
        return {"residuals": [], "standardized_residuals": [], "sd": None,
                "status": "unmodeled_uncertainty", "group": group}
    center = _mean(values)
    centered = [x-center for x in values]
    sd = statistics.pstdev(centered)
    own = (fitted or {}).get("residuals") or []
    return {"residuals": centered, "standardized_residuals": [x/sd for x in centered] if sd else [0.0 for x in centered],
            "sd": sd, "skewness": _skew(centered), "group": group, "n": len(centered),
            "historical_individual_sd": statistics.pstdev(own) if own else None,
            "status": "uncalibrated_cohort_output_residual_proxy",
            "method": "Measured cohort pool; no invented individual variance floor or inferred provider-error calibration"}


def measure_variance_bins(rows, scoring, *, bins=4):
    """Descriptive heteroskedasticity conditional on observed seasonal output.

    Bins are fit from training-player-season observed-game means. They are not
    provider forecasts and do not establish forecast error calibration.
    """
    if bins < 1:
        raise ModelingError("At least one variance bin required")
    units = defaultdict(list)
    for row in rows:
        if _regular(row) and _position(row) in POSITIONS and _availability(row) != 0:
            units[(_group(row), _id(row), int(row["season"]))].append(_score(row, scoring)["points"])
    by_group = defaultdict(list)
    omitted = 0
    for (group, pid, year), values in units.items():
        if len(values) < 2:
            omitted += 1
            continue
        by_group[group].append((_mean(values), values))
    groups = {}
    for group, records in sorted(by_group.items()):
        means = sorted(mean for mean, values in records)
        edges = []
        for i in range(1, bins):
            index = (len(means)-1)*i/bins
            lo = int(index)
            edges.append(means[lo]+(index-lo)*(means[min(lo+1,len(means)-1)]-means[lo]))
        buckets = defaultdict(list)
        for mean, values in records:
            buckets[sum(mean > edge for edge in edges)].append((mean, values))
        output = {}
        for bucket, members in sorted(buckets.items()):
            residuals = [value-mean for mean, values in members for value in values]
            points = [value for mean, values in members for value in values]
            # Each player-season contributes weeks to its conditional pool;
            # this distribution deliberately weights observed exposure.
            output[str(bucket)] = {"units": len(members), "observations": len(residuals),
                "mean_low": min(mean for mean, values in members), "mean_high": max(mean for mean, values in members),
                "mean_of_unit_means": _mean([mean for mean, values in members]),
                "residual_sd": statistics.pstdev(residuals), "residuals": residuals,
                "observed_score_min": min(points), "observed_score_max": max(points),
                "observed_negative_fraction": sum(x < 0 for x in points)/len(points)}
        groups[group] = {"edges": edges, "bins": output, "units": len(records)}
    return {"groups": groups, "bin_count": bins, "single_observation_units_excluded": omitted,
            "evidence_status": "exploratory_conditional_output_variance_not_forecast_error_calibration",
            "limitations": ["Observed season means include end-of-season information; use only completed training seasons to construct bins.",
                            "Conditioning these bins on professional expected means is an unvalidated measurement transfer.",
                            "Center-shifted residuals can violate realistic lower-tail support. No clipping is applied because it would silently increase expected points.",
                            "Positive common Gaussian factors can add further lower-tail violations; a copula with empirical marginals is a future calibrated replacement."]}


def conditional_residual_distribution(model, player, *, mean=None):
    """Minimum variance proxy that preserves the professional center explicitly."""
    center = _num(player.get("mean") if mean is None else mean)
    group = _group(player)
    diagnostic = (model.get("variance_bins") or {}).get("groups", {}).get(group)
    if center is None or not diagnostic:
        result = residual_distribution(model, player)
        result["fallback_reason"] = "No measured conditional variance bins or center"
        return result
    bucket = str(sum(center > edge for edge in diagnostic["edges"]))
    selected = diagnostic["bins"].get(bucket)
    if not selected:
        return {**residual_distribution(model, player), "fallback_reason": "Empty conditional bin"}
    values = selected["residuals"]
    empirical_center = _mean(values)
    residuals = [x-empirical_center for x in values]
    sd = statistics.pstdev(residuals)
    return {"residuals": residuals, "standardized_residuals": [x/sd for x in residuals] if sd else [0.0],
            "sd": sd, "skewness": _skew(residuals), "group": group, "bin": int(bucket), "n": len(residuals),
            "status": "uncalibrated_output_level_conditional_residual_proxy", "professional_center": center,
            "negative_score_fraction_after_shift": sum(center+x < 0 for x in residuals)/len(residuals),
            "below_minus_two_fraction_after_shift": sum(center+x < -2 for x in residuals)/len(residuals),
            "observed_negative_score_fraction": selected["observed_negative_fraction"],
            "method": "Measured position + observed-season-mean quartile residual pool, indexed by professional expected mean; no clipping or mean replacement",
            "limitations": model["variance_bins"]["limitations"]}


def bind_variance_dependency(model, rows, *, cutoff, input_batches=None):
    """Bind persisted variance state to the exact fitted training observations.

    Reuse bins already produced by fit_model. Older models missing this state
    compute it once from the identical training rows; no other model is refit.
    The returned composite ID covers parameters, bins and their provenance.
    """
    cut = _num(cutoff)
    if cut is None:
        raise ModelingError("Variance dependency requires a finite observation cutoff")
    if model.get("id") != digest({k:v for k,v in model.items() if k != "id"}):
        raise ModelingError("Model identity does not match its saved parameters")
    if model.get("scoring_hash") != digest(model["scoring"]):
        raise ModelingError("Variance dependency scoring does not match model")
    years = sorted(set(int(y) for y in model["training"]["seasons"]))
    selected = [r for r in rows if int(r["season"]) in years and _regular(r) and _position(r) in POSITIONS]
    _ensure_unique(selected)
    if len(selected) != model["training"]["rows"] or digest(selected) != model["training"]["data_hash"]:
        raise ModelingError("Variance dependency must use exactly the fitted training rows and provenance")
    for row in selected:
        known = _num((row.get("_provenance") or {}).get("available_at"))
        if known is not None and known > cut:
            raise ModelingError("Variance training record was unavailable at its cutoff")
    observed_batches = sorted({r["_provenance"]["batch_id"] for r in selected
                               if (r.get("_provenance") or {}).get("batch_id") is not None})
    batches = observed_batches if input_batches is None else sorted(set(input_batches))
    if batches != observed_batches:
        raise ModelingError("Variance input batches do not match fitted training row provenance")
    result = copy.deepcopy(model)
    bins = result.get("variance_bins")
    if bins is None:
        bins = measure_variance_bins(selected, result["scoring"])
        result["variance_bins"] = bins
    result["variance_dependency"] = {
        "schema_version": 1, "scoring_hash": result["scoring_hash"],
        "training_seasons": years, "training_rows": len(selected),
        "training_data_hash": digest(selected), "cutoff": cut,
        "input_batches": batches, "bins_hash": digest(bins),
        "training_query_purposes": sorted({(r.get("_provenance") or {}).get("purpose", "not_recorded") for r in selected}),
        "evidence_status": "reproducible_training_dependency_not_forecast_calibration",
    }
    result["base_model_id"] = result.get("base_model_id", result["id"])
    result.pop("id")
    result["id"] = digest(result)
    return result


def validate_variance_dependency(model, *, season, cutoff):
    """Refuse incomplete, altered, wrong-scoring or temporally leaking state."""
    metadata = model.get("variance_dependency")
    bins = model.get("variance_bins")
    if not isinstance(metadata, dict) or not isinstance(bins, dict):
        raise ModelingError("Persisted variance dependency is missing; bind exact training data before rebuilding projection")
    if model.get("scoring_hash") != digest(model["scoring"]) or metadata.get("scoring_hash") != model["scoring_hash"]:
        raise ModelingError("Variance dependency scoring mismatch")
    years = sorted(set(int(y) for y in model["training"]["seasons"]))
    if not years or metadata.get("training_seasons") != years or max(years) >= int(season):
        raise ModelingError("Variance training seasons overlap the target year or differ from fitted model")
    if metadata.get("training_rows") != model["training"]["rows"] or metadata.get("training_data_hash") != model["training"]["data_hash"]:
        raise ModelingError("Variance training lineage differs from fitted model")
    cut, trained = _num(cutoff), _num(metadata.get("cutoff"))
    if cut is None or trained is None or trained > cut:
        raise ModelingError("Variance dependency was unavailable at the projection cutoff")
    if metadata.get("bins_hash") != digest(bins):
        raise ModelingError("Persisted variance bins do not match their content hash")
    if model.get("id") != digest({k:v for k,v in model.items() if k != "id"}):
        raise ModelingError("Composite model identity does not match saved variance dependency")
    return metadata


def projection_component_metadata(forecast):
    """Preserve provider audits; legacy absence is unknown, never completeness.

    This does not infer zeros, event applicability, or missing event means.
    It is safe for old warehouse records that predate provider completeness.
    """
    source = forecast or {}
    result = {key: source.get(key) for key in (
        "missing_scoring_fields", "absent_scoring_fields", "null_scoring_fields",
        "explicit_zero_scoring_fields", "scoring_field_status", "completeness_scope",
        "missingness_caveat")}
    status = source.get("projection_completeness")
    if status not in ("partial_sparse_zero_unverified", "complete_for_requested_scoring_keys"):
        status = "unknown"
    result["projection_completeness"] = status
    result["mean_interpretation"] = (
        "observed_component_subtotal" if status == "partial_sparse_zero_unverified" else
        "score_of_provided_component_means" if status == "complete_for_requested_scoring_keys" else
        "unknown_component_completeness")
    if status == "unknown":
        result["missingness_caveat"] = "Provider component-completeness metadata unavailable; no completeness or zero-imputation claim is made."
    return result


def project(model, players, *, season, week, draws=512, seed=0, forecast_rows=None,
            availability_overrides=None):
    """Joint scenario draws with professional weekly centers when supplied.

    By default forecast availability semantics are unknown: we preserve the
    provider weekly center and apply no second availability haircut. An explicit
    override p is a scenario assumption; provider center is divided by p before
    mixing, preserving its unconditional mean. It is reported, never hidden.
    """
    if draws < 2:
        raise ModelingError("At least two draws are required")
    if max(model["training"]["seasons"]) >= int(season):
        raise ModelingError("This annual model may project only seasons after all fitted training seasons")
    rng = random.Random(seed)
    forecasts = {}
    for f in forecast_rows or []:
        if int(f["season"]) == int(season) and f.get("week") is not None and int(f["week"]) == int(week):
            if _id(f) in forecasts:
                raise ModelingError("Choose a projection provider/vintage before calling project")
            forecasts[_id(f)] = f
    by_team, by_game = {}, {}
    answer, joint = [], {}
    overrides = availability_overrides or {}
    for player in players:
        pid = _id(player)
        fitted = _lookup(model, player)
        position = _position(player)
        group = (fitted or {}).get("group", _group(player))
        cohort = model["groups"].get(group)
        f = forecasts.get(pid)
        if cohort is None and fitted is None and f is None:
            answer.append({"id": pid, "player_id": pid, "position": position,
                           "status": "unmodeled", "reason": "No measured cohort, history or professional projection"})
            continue
        center = _num(f.get("mean")) if f else None
        if f and f.get("scoring_hash") not in (None, model["scoring_hash"]):
            raise ModelingError("Projection scoring hash differs from model scoring")
        if f and center is None:
            audit = score_stats(_stats(f), model["scoring"], source_schema="sleeper", sparse_zero=True)
            center = audit["points"]
        provider = center is not None
        if center is None:
            center = (fitted or cohort)["mean"]
        uncertainty = residual_distribution(model, player)
        residuals = uncertainty.get("residuals", [])
        if not residuals:
            residuals = [0.0]
        residual_mean = _mean(residuals)
        residuals = [e - residual_mean for e in residuals]
        empirical_variance = _mean([e * e for e in residuals], 0.0)
        sd = math.sqrt(empirical_variance)
        availability = (fitted or {}).get("availability") if not provider else None
        av_source = "explicit_at_risk_history" if availability is not None else "unknown"
        if pid in overrides:
            availability = _num(overrides[pid])
            if availability is None or not 0 <= availability <= 1:
                raise ModelingError("Availability override must be in [0,1]")
            av_source = "explicit_scenario_override"
        active_center = center
        if provider and availability is not None:
            if availability == 0 and center != 0:
                raise ModelingError("Nonzero provider mean incompatible with zero-availability scenario")
            active_center = center / availability if availability else 0.0
        team = str(player.get("team") or (f or {}).get("team") or (fitted or {}).get("team") or pid)
        game = str(player.get("game_id") or (f or {}).get("game_id") or "unknown:" + team)
        if team not in by_team:
            by_team[team] = [rng.gauss(0, 1) for _ in range(draws)]
        if game not in by_game:
            by_game[game] = [rng.gauss(0, 1) for _ in range(draws)]
        gv = float(model.get("factors", {}).get("game_variance", 0))
        tv = float(model.get("factors", {}).get("team_variance", 0))
        total = gv + tv
        if total > empirical_variance and total:
            scale = empirical_variance / total
            gv, tv = gv * scale, tv * scale
        idio_scale = math.sqrt(max(0, 1 - (gv + tv) / empirical_variance)) if empirical_variance else 0.0
        values = []
        for k in range(draws):
            if availability is not None and rng.random() >= availability:
                values.append(0.0)
            else:
                values.append(active_center + math.sqrt(gv) * by_game[game][k] +
                              math.sqrt(tv) * by_team[team][k] + idio_scale * rng.choice(residuals))
        joint[pid] = values
        components = projection_component_metadata(f)
        if provider:
            point_kind = ("observed_component_subtotal" if components["projection_completeness"] == "partial_sparse_zero_unverified" else
                          "expected_points" if components["projection_completeness"] == "complete_for_requested_scoring_keys" else
                          "point_center_component_completeness_unknown")
            mean_definition = ("provider_weekly_" + point_kind + "_availability_semantics_unverified"
                               if availability is None else "unconditional_weekly_" + point_kind)
        else:
            mean_definition = "unconditional_weekly_expected_points" if availability is not None else "conditional_on_observed_active_output"
        item = {"id": pid, "player_id": pid, "position": position,
                "fantasy_positions": player.get("fantasy_positions") or [position], "team": team,
                "name": player.get("name", player.get("full_name", (f or {}).get("name"))),
                "age": player.get("age"), "years_exp": player.get("years_exp"),
                "mean": center if provider or availability is None else availability * center,
                "active_mean": active_center, "sd": statistics.pstdev(values),
                "active_residual_sd": sd, "availability": availability,
                "standardized_residuals": uncertainty.get("standardized_residuals", []),
                "uncertainty_method": uncertainty.get("method", uncertainty["status"]),
                "availability_source": av_source,
                "mean_definition": mean_definition,
                **components,
                "samples": values, "sample_mean": _mean(values), "skewness": _skew(values),
                "projection_source": (f or {}).get("source_id", "raw_history_credibility_fallback"),
                "calibration_status": "uncalibrated_historical_residual_proxy" if provider else model["calibration_status"],
                "factor_loadings": {"game": math.sqrt(gv), "team": math.sqrt(tv)},
                "historical_observations": (fitted or {}).get("observations", 0),
                "professional_center_preserved": provider, "season": int(season), "week": int(week)}
        answer.append(item)
    return {"schema_version": SCHEMA_VERSION, "model_id": model["id"], "season": int(season),
            "week": int(week), "players": answer, "joint_samples_by_id": joint,
            "sample_count": draws, "seed": seed, "factor_metadata": model.get("factors", {}),
            "limitations": ["Joint draws are approximate and not calibrated forecasts.",
                             "Provider mean preserved; unknown availability is null, never an assumed health probability of one.",
                             "Provider centers may be observed-component subtotals; completeness metadata is preserved and absent metadata remains unknown. No missing event is imputed here.",
                             "Without an override professional projections receive no second injury haircut.",
                             "Game factors require matching game IDs; unknown games share only team factors.",
                             "Current player-role history is not silently mapped to a future career survival model."]}


def brier_loss(probability, outcome):
    p = _num(probability)
    if p is None or not 0 <= p <= 1 or outcome not in (0, 1, False, True):
        raise ModelingError("Brier loss requires p in [0,1] and a binary outcome")
    return (p - int(outcome)) ** 2


def crps_loss(samples, outcome):
    """CRPS of the issued empirical distribution; lower is better, O(n log n)."""
    values = sorted(float(v) for v in samples)
    y = _num(outcome)
    if not values or y is None or any(not math.isfinite(v) for v in values):
        raise ModelingError("CRPS requires finite samples and outcome")
    n = len(values)
    half_pair_distance = sum((2 * i - n + 1) * v for i, v in enumerate(values)) / (n * n)
    return _mean([abs(v - y) for v in values]) - half_pair_distance


def _corr(xs, ys):
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = _mean(xs), _mean(ys)
    xx = sum((x - mx) ** 2 for x in xs)
    yy = sum((y - my) ** 2 for y in ys)
    if not xx or not yy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)


def _metric(row, metric, scoring):
    if metric == "league_points":
        return _score(row, scoring)["points"]
    if metric not in METRICS:
        raise ModelingError("Unregistered metric: " + str(metric))
    numerator, denominator = METRICS[metric]
    value = _first(_stats(row), numerator)
    if denominator:
        den = _first(_stats(row), denominator)
        return value / den if value is not None and den is not None and den > 0 else None
    return value


def measure_reliability(rows, scoring, *, metrics=None, windows=(2, 4, 6), gap=1):
    """Direct descriptive stability curves, not universal stabilization thresholds.

    Separate positions before correlating; pooling QB/WR/RB averages can create
    spurious reliability from role differences. Window sizes are registered in
    output. Missing weeks are not inserted as zero, and no CI treats rows iid.
    """
    metrics = list(metrics or ["league_points", "targets", "carries", "pass_attempts",
                               "receiving_yards_per_target", "rushing_yards_per_carry",
                               "completion_rate", "passing_yards_per_attempt", "passing_td_rate"])
    if gap < 0 or any(int(k) < 1 for k in windows):
        raise ModelingError("Invalid reliability windows")
    selected = [r for r in rows if _regular(r) and _position(r) in POSITIONS]
    _ensure_unique(selected)
    output = []
    for metric in metrics:
        units = defaultdict(dict)
        for row in selected:
            value = _metric(row, metric, scoring)
            if value is not None:
                units[(_position(row), _id(row), int(row["season"]))][int(row["week"])] = value
        for position in sorted({k[0] for k in units}):
            subgroup = {key: val for key, val in units.items() if key[0] == position}
            for length in windows:
                for kind, separation in (("adjacent", 0), ("separated", gap)):
                    xs, ys = [], []
                    for values in subgroup.values():
                        start = min(values)
                        left = range(start, start + length)
                        right = range(start + length + separation, start + 2 * length + separation)
                        if all(w in values for w in list(left) + list(right)):
                            xs.append(_mean([values[w] for w in left]))
                            ys.append(_mean([values[w] for w in right]))
                    output.append({"metric": metric, "position": position, "kind": kind,
                                   "window_games": int(length), "gap_weeks": separation,
                                   "units": len(xs), "correlation": _corr(xs, ys),
                                   "mean_squared_difference": _mean([(x-y)**2 for x,y in zip(xs,ys)])})
            annual = {key: _mean(list(values.values())) for key, values in subgroup.items()}
            pairs = [(value, annual[(pos, pid, year + 1)]) for (pos, pid, year), value in annual.items() if (pos, pid, year + 1) in annual]
            output.append({"metric": metric, "position": position, "kind": "year_over_year_observed_survivors",
                           "units": len(pairs), "correlation": _corr([x for x,_ in pairs], [y for _,y in pairs]),
                           "eligible_player_seasons": len(annual),
                           "selection_warning": "Requires observations both years; not a full-entry-cohort survival estimate"})
    return {"curves": output, "metrics": metrics, "windows": list(windows), "gap": gap,
            "comparison_count": len(output), "evidence_status": "exploratory_historical_association",
            "population": "Observed offensive player-weeks; no missing-week imputation",
            "limitations": ["No game-count stabilization threshold established.",
                             "Consecutive observed windows select for continuous participation; exclusions are material.",
                             "Year-over-year correlations condition on observed survivors.",
                             "Ratios are averaged by observed week, not estimated as pooled success probabilities."]}


def register_evaluation(*, train_seasons, holdout_seasons, models=("cohort", "unpooled", "credibility"),
                        cutoff, created_at=None, label="temporal_holdout", feature_sets=None):
    """Create an immutable specification to save before inspecting evaluation."""
    train, holdout = sorted(set(map(int, train_seasons))), sorted(set(map(int, holdout_seasons)))
    if not train or not holdout or set(train) & set(holdout) or max(train) >= min(holdout):
        raise ModelingError("Training seasons must precede disjoint holdout seasons")
    allowed = {"cohort", "unpooled", "credibility", "provider"}
    if not models or not set(models) <= allowed or len(models) != len(set(models)):
        raise ModelingError("Unknown or repeated evaluation model")
    ts = _stamp(cutoff)
    if ts is None:
        raise ModelingError("Registered temporal cutoff required")
    spec = {"schema_version": 1, "label": label, "created_at": _stamp(created_at) if created_at is not None else time.time(),
            "cutoff": ts, "train_seasons": train, "holdout_seasons": holdout,
            "models": list(models), "metrics": ["CRPS_loss", "mean_squared_error", "availability_Brier_loss"],
            "feature_sets": feature_sets or ["player_history", "cohort_history"],
            "comparison_count": len(models), "selection_rule": "Report all registered models; no claim of confirmatory superiority from exploratory replays"}
    spec["id"] = digest(spec)
    return spec


def _event_cutoff_issues(rows, cutoff):
    """A file saved before a cutoff is not a forecast of an already-ended game."""
    issues = set()
    for row in rows:
        event = _stamp(row.get("kickoff_at", row.get("event_at")))
        if event is not None:
            if cutoff >= event:
                issues.add("registered_cutoff_not_before_target_event")
        else:
            # Before Jan 1 of a season label is conservatively before its regular
            # season. Later cutoffs need a real schedule/event timestamp join.
            conservative_start = datetime(int(row["season"]), 1, 1, tzinfo=timezone.utc).timestamp()
            if cutoff >= conservative_start:
                issues.add("target_event_timestamp_missing_for_within_season_cutoff")
    return sorted(issues)


def evaluate_model(model, rows, scoring, *, registry, forecast_rows=None, draws=128,
                   seed=0, require_validation=False):
    """Run every registered comparison; PIT leakage gates the evidence label.

    Final held-out outcomes may be retrieved after the event. Training features
    and archived forecasts must have been available at the registered cutoff.
    Registering an already resolved historical test now is exploratory even if
    the supplied features are truly archived.
    """
    if not registry or not registry.get("id"):
        raise ModelingError("Registered evaluation specification required")
    spec = {k: v for k,v in registry.items() if k != "id"}
    if digest(spec) != registry["id"]:
        raise ModelingError("Evaluation registry has changed since registration")
    if model["scoring_hash"] != digest(scoring):
        raise ModelingError("Scoring differs from fitted model")
    if model["training"]["seasons"] != registry["train_seasons"]:
        raise ModelingError("Model train seasons differ from preregistration")
    selected = [r for r in rows if int(r["season"]) in registry["holdout_seasons"] and _regular(r) and _position(r) in POSITIONS]
    _ensure_unique(selected)
    if not selected:
        raise ModelingError("No held-out rows")
    train_audit = model["training"].get("temporal_audit", {})
    issues = []
    issues.extend(_event_cutoff_issues(selected, registry["cutoff"]))
    if not train_audit.get("passed") or train_audit.get("cutoff", float("inf")) > registry["cutoff"]:
        issues.append("training_features_not_verified_at_registered_cutoff")
    if registry["created_at"] is None or registry["created_at"] > registry["cutoff"]:
        issues.append("registered_after_historical_cutoff_not_prospective")
    forecasts = {}
    for f in forecast_rows or []:
        if f.get("week") is not None:
            key = (_id(f), int(f["season"]), int(f["week"]))
            if key in forecasts:
                raise ModelingError("Duplicate forecasts: specify source/vintage for ablation")
            forecasts[key] = f
    forecast_audit = temporal_audit(list(forecasts.values()), registry["cutoff"]) if forecasts else None
    if "provider" in registry["models"] and not forecasts:
        issues.append("provider_ablation_missing_forecasts")
    if forecast_audit and not forecast_audit["passed"]:
        issues.append("provider_forecast_vintages_not_verified_at_cutoff")
    if require_validation and issues:
        raise ModelingError("Validation blocked: " + "; ".join(issues))
    rng = random.Random(seed)
    results = {}
    for name in registry["models"]:
        loss, squared, avloss, blocks, missing = [], [], [], defaultdict(list), 0
        for row in selected:
            fitted = _lookup(model, row)
            cohort = model["groups"].get((fitted or {}).get("group", _group(row)))
            if cohort is None:
                missing += 1
                continue
            mean = cohort["mean"] if name == "cohort" else (fitted or cohort).get("raw_mean", cohort["mean"]) if name == "unpooled" else (fitted or cohort)["mean"]
            if name == "provider":
                f = forecasts.get((_id(row), int(row["season"]), int(row["week"])))
                if f is None:
                    missing += 1
                    continue
                if f.get("scoring_hash") not in (None, model["scoring_hash"]):
                    raise ModelingError("Provider evaluation scoring hash differs from model")
                mean = _num(f.get("mean"))
                if mean is None:
                    mean = score_stats(_stats(f), scoring, source_schema="sleeper", sparse_zero=True)["points"]
            residuals = cohort.get("residuals") or [0]
            samples = [mean + rng.choice(residuals) for _ in range(draws)]
            observed_av = _availability(row)
            if observed_av is not None and cohort.get("availability") is not None:
                participation = model.get("participation_history", {}).get(_id(row), {})
                p = cohort["availability"] if name == "cohort" else participation.get("raw_probability" if name == "unpooled" else "probability", cohort["availability"])
                if name == "provider":
                    p = _num(f.get("availability"))
                if p is not None:
                    avloss.append(brier_loss(p, observed_av))
            if observed_av == 0:
                # Active-production forecasts are not judged against inactive zero.
                continue
            outcome = _score(row, scoring)["points"]
            l = crps_loss(samples, outcome)
            loss.append(l)
            squared.append((mean-outcome)**2)
            blocks[(int(row["season"]), int(row["week"]))].append(l)
        block_means = [_mean(v) for v in blocks.values()]
        results[name] = {"scored_rows": len(loss), "missing_predictions": missing,
                         "CRPS_loss": _mean(loss), "mean_squared_error": _mean(squared),
                         "availability_Brier_loss": _mean(avloss), "availability_rows": len(avloss),
                         "NFL_week_blocks": len(block_means),
                         "week_block_mean_loss_sd": math.sqrt(_var(block_means)),
                         "scoring_target": "active_observed_weekly_output; availability separate"}
    return {"registry": registry, "model_id": model["id"], "evaluated_at": time.time(),
            "holdout_rows": len(selected), "results": results, "comparison_count": len(results),
            "evidence_status": "prospectively_registered_temporal_validation" if not issues else "exploratory_retrospective_not_PIT_validation",
            "validation_gate": {"passed": not issues, "issues": issues,
                                "training": train_audit, "forecasts": forecast_audit},
            "participation_target": model.get("participation_target", []),
            "limitations": ["No independent-row confidence intervals reported.",
                             "Provider comparisons can have different coverage; compare common support before ranking.",
                             "A good forecast score does not alone establish a championship policy edge.",
                             "Raw-history distributions are fallback baselines, not replacements for verified professional forecasts."]}


def measure_role_changes(rows, scoring, *, window=3):
    """Exploratory before/after flags, including explicitly identified injury gaps."""
    grouped = defaultdict(list)
    for row in rows:
        if _regular(row) and _position(row) in POSITIONS:
            grouped[(_id(row), int(row["season"]))].append(row)
    changes, recovery = [], []
    for (pid, year), history in grouped.items():
        history.sort(key=lambda r: int(r["week"]))
        for i in range(window, len(history) - window + 1):
            before, after = history[i-window:i], history[i:i+window]
            if int(after[-1]["week"]) - int(before[0]["week"]) != 2 * window - 1:
                continue
            a = [_first(_stats(r), ("offense_pct", "offense_snap_share")) for r in before]
            b = [_first(_stats(r), ("offense_pct", "offense_snap_share")) for r in after]
            if None not in a + b:
                changes.append({"player_id": pid, "season": year, "after_week": int(after[-1]["week"]),
                                "pre_snap_share": _mean(a), "post_snap_share": _mean(b),
                                "difference": _mean(b)-_mean(a), "window": window,
                                "known_only_after_week": int(after[-1]["week"])})
        for i, row in enumerate(history):
            if i == 0 or _availability(row) != 1 or _availability(history[i-1]) != 0:
                continue
            if str(history[i-1].get("absence_reason", "")).lower() not in {"injury", "injured"}:
                continue
            pre = [r for r in history[:i] if _availability(r) == 1][-window:]
            post = [r for r in history[i:] if _availability(r) == 1][:window]
            if len(pre) == window and len(post) == window:
                recovery.append({"player_id": pid, "season": year, "return_week": int(row["week"]),
                                 "pre_points": _mean([_score(r, scoring)["points"] for r in pre]),
                                 "post_points": _mean([_score(r, scoring)["points"] for r in post]),
                                 "known_only_after_week": int(post[-1]["week"]),
                                 "window": window})
    return {"role_changes": changes, "recovery_windows": recovery, "window": window,
            "evidence_status": "exploratory_descriptive_not_causal",
            "comparison_count": len(changes) + len(recovery),
            "limitations": ["Flags require future post-window data; only known_after_week may be used as a feature.",
                             "No statistical change-point significance or injury recovery effect is claimed.",
                             "Injury analysis requires explicit at-risk, availability and absence_reason evidence; gaps alone do not qualify."]}


def estimate_horizon_transitions(rows, scoring):
    """Annual observed-output retention, with an honest full-risk-set requirement.

    Stats-only next-year absence is observational retention, NOT death, retirement,
    medical survival or probability of being rosterable. Fully observed roster
    risk sets would be needed to upgrade this to useful-service transitions.
    """
    annual = defaultdict(list)
    meta = {}
    for row in rows:
        if _regular(row) and _position(row) in POSITIONS:
            key = (_id(row), int(row["season"]))
            annual[key].append(_score(row, scoring)["points"])
            meta[key] = row
    seasons = sorted({year for _, year in annual})
    groups = defaultdict(list)
    for (pid, year), values in annual.items():
        if year + 1 not in seasons:
            continue
        position = _position(meta[(pid, year)])
        next_values = annual.get((pid, year + 1))
        base, following = sum(values), sum(next_values or [])
        groups[position].append({"observed_next_year": int(next_values is not None),
                                 "next_points_including_unobserved_zero": following,
                                 "prior_points": base,
                                 "season_total_ratio": following / base if base > 0 else None})
    return {"groups": {g: {"player_seasons": len(v),
                           "next_year_observed_fraction": _mean([x["observed_next_year"] for x in v]),
                           "pooled_next_to_prior_points_ratio": sum(x["next_points_including_unobserved_zero"] for x in v) / sum(x["prior_points"] for x in v) if sum(x["prior_points"] for x in v) > 0 else None,
                           "ratio_distribution": [x["season_total_ratio"] for x in v if x["season_total_ratio"] is not None]}
                       for g, v in sorted(groups.items())},
            "seasons": seasons, "evidence_status": "exploratory_observation_retention_not_survival",
            "usable_as_individual_survival": False,
            "limitations": ["Population enters after an observed statistics row, not at original NFL entry.",
                             "No later statistics can mean no play, missing records, position change or exit; not verified retirement.",
                             "Do not exponentiate these pooled annual fractions into independent career survival.",
                             "Aggregate position transitions are descriptive and do not prove age or youth price advantages."]}


def build_participation_panel(weekly_rows, weekly_rosters, schedules, snap_rows, *,
                              crosswalk_rows=(), injury_rows=(), complete_snapshot=False):
    """Join a measured participation target; never label it medical availability.

    A zero means no offensive participation *recorded in the supplied complete
    statistics/snap snapshots* for a weekly-listed member of a team that played.
    The explicit completeness assertion is necessary for negative observations.
    Games lacking either team-level source coverage remain unknown and excluded.
    """
    weekly_rosters = list(weekly_rosters)
    games = {}
    for r in schedules:
        if not _regular(r) or r.get("home_score") is None or r.get("away_score") is None:
            continue
        for team in (r.get("home_team"), r.get("away_team")):
            games[(int(r["season"]), int(r["week"]), team)] = r.get("game_id")
    pfr_map = {}
    for r in list(crosswalk_rows) + list(weekly_rosters):
        if r.get("pfr_id") and r.get("gsis_id") and r.get("identity_verified", True):
            key = (int(r["season"]), str(r["pfr_id"]))
            old = pfr_map.get(key)
            if old is not None and old != str(r["gsis_id"]):
                raise ModelingError("Ambiguous exact PFR/GSIS crosswalk")
            pfr_map[key] = str(r["gsis_id"])
    stats_by_key, stats_coverage = {}, set()
    audit = defaultdict(int)
    for r in weekly_rows:
        if not _regular(r):
            continue
        team_key = (int(r["season"]), int(r["week"]), r.get("team", r.get("recent_team")))
        stats_coverage.add(team_key)
        if r.get("identity_verified", True) and _position(r) in POSITIONS:
            key = (_id(r), *team_key[:2])
            if key in stats_by_key:
                raise ModelingError("Duplicate player-week statistics in participation panel")
            stats_by_key[key] = r
    snaps_by_key, snap_coverage = {}, set()
    for r in snap_rows:
        if not _regular(r):
            continue
        team_key = (int(r["season"]), int(r["week"]), r.get("team"))
        if (_num(r.get("offense_snaps")) or 0) > 0:
            snap_coverage.add(team_key)
        pid = r.get("gsis_id") or pfr_map.get((int(r["season"]), str(r.get("pfr_player_id"))))
        if pid:
            key = (str(pid), int(r["season"]), int(r["week"]))
            if key in snaps_by_key:
                audit["duplicate_snap_rows"] += 1
                continue
            snaps_by_key[key] = r
        else:
            audit["unmapped_snap_rows"] += 1
            audit["unmapped_snap_rows_offense" if _position(r) in POSITIONS else "unmapped_snap_rows_other_positions"] += 1
    injury = defaultdict(set)
    for r in injury_rows:
        if r.get("identity_verified", True) and r.get("player_id", r.get("gsis_id")):
            injury[(_id(r), int(r["season"]), int(r["week"]))].add(str(r.get("report_status", "")))
    membership = defaultdict(list)
    for r in weekly_rosters:
        if _regular(r) and _position(r) in POSITIONS and r.get("identity_verified", True):
            membership[(_id(r), int(r["season"]), int(r["week"]))].append(r)
    panel = []
    for key, members in sorted(membership.items()):
        candidates = [r for r in members if (key[1], key[2], r.get("team")) in games]
        if not candidates:
            audit["bye_or_uncompleted_game"] += 1
            continue
        observed = stats_by_key.get(key)
        teams = {r.get("team") for r in candidates}
        if len(teams) > 1:
            observed_team = (observed or {}).get("team")
            candidates = [r for r in candidates if r.get("team") == observed_team]
            if not candidates:
                audit["ambiguous_multi_team_membership"] += 1
                continue
        member = candidates[0]
        tk = (key[1], key[2], member.get("team"))
        snaps = snaps_by_key.get(key)
        opportunities = sum(_first(_stats(observed or {}), names) or 0 for names in
                            [("attempts", "pass_att"), ("carries", "rush_att"), ("targets", "rec_tgt")])
        positive = bool((snaps and (_num(snaps.get("offense_snaps")) or 0) > 0) or opportunities > 0)
        source_covered = tk in stats_coverage and tk in snap_coverage
        if not positive and (not complete_snapshot or not source_covered):
            audit["unknown_negative_excluded"] += 1
            continue
        result = dict(observed or member)
        result.update(player_id=key[0], gsis_id=key[0], season=key[1], week=key[2],
                      team=tk[2], game_id=games[tk], at_risk=True, active=positive,
                      availability_definition="recorded_offensive_participation_among_weekly_listed_roster_members",
                      medical_availability=False, roster_status=member.get("status"),
                      complete_snapshot_asserted=bool(complete_snapshot))
        result["stats"] = dict(_stats(observed)) if observed else {field: 0.0 for field in CORE_OFFENSE}
        result["stats_origin"] = "recorded_statistics" if observed else "no_recorded_scoring_events_in_complete_snapshot"
        if snaps:
            result["stats"].update(offense_snaps=snaps.get("offense_snaps"), offense_pct=snaps.get("offense_pct"))
        result["report_statuses"] = sorted(injury.get(key, []))
        if not positive and "Out" in injury.get(key, set()):
            result["absence_reason"] = "injury"
            result["absence_reason_evidence"] = "reported_Out_and_no_recorded_offensive_participation; not causal medical adjudication"
        panel.append(result)
    counts = defaultdict(lambda: [0, 0])
    for row in panel:
        counts[_position(row)][0] += 1
        counts[_position(row)][1] += int(row["active"])
    return {"rows": panel, "diagnostics": {"rows": len(panel), "excluded_or_unresolved": dict(audit),
            "groups": {p: {"weekly_memberships": n, "recorded_participations": a,
                              "fraction": a/n if n else None} for p, (n, a) in sorted(counts.items())},
            "target": "recorded_offensive_participation; NOT health, game-day active status, or starter availability",
            "complete_snapshot_asserted": bool(complete_snapshot),
            "evidence_status": "exploratory_retrospective_membership_panel",
            "limitations": ["No recorded participation includes healthy reserves, practice squads, injury and exit; do not apply this rate as an injury haircut.",
                            "A complete source snapshot is a caller assertion; team coverage checks do not prove every individual record is correct.",
                            "No source-vintage upgrade is implied by joining final historical tables."]}}


def register_source_ablation(source_ids, *, cutoff, created_at=None):
    """Freeze source-alone and equal-weight additions before scoring outcomes."""
    sources = list(source_ids)
    if not sources or len(sources) != len(set(sources)):
        raise ModelingError("A unique nonempty ordered source list is required")
    ts = _stamp(cutoff)
    if ts is None:
        raise ModelingError("Timezone-aware cutoff required")
    comparisons = [{"label": s, "sources": [s]} for s in sources]
    comparisons += [{"label": "+".join([sources[0], s]), "sources": [sources[0], s]} for s in sources[1:]]
    spec = {"kind": "source_ablation", "created_at": _stamp(created_at) if created_at is not None else time.time(),
            "cutoff": ts, "source_ids": sources, "comparisons": comparisons,
            "comparison_count": len(comparisons), "baseline": sources[0],
            "target": "common-support observed player-week points; equal weights fixed before outcomes",
            "metrics": ["CRPS_loss", "mean_squared_error"]}
    spec["id"] = digest(spec)
    return spec


def evaluate_source_ablation(model, forecasts, outcomes, *, registry, id_map=None, draws=128, seed=0,
                             require_validation=False):
    """Score marginal source additions only on identical resolved event sets.

    This can safely run before a season: it returns blocked_no_resolved_common_
    outcomes instead of inventing a forecast-quality result. Source center
    uncertainty uses one common historical residual proxy for fair comparisons.
    """
    if registry.get("kind") != "source_ablation" or digest({k:v for k,v in registry.items() if k != "id"}) != registry.get("id"):
        raise ModelingError("Unchanged registered source-ablation specification required")
    if draws < 2:
        raise ModelingError("At least two distribution draws required")
    lookup = {s: {} for s in registry["source_ids"]}
    for f in forecasts:
        source = f.get("source_id")
        if source not in lookup or f.get("week") is None:
            continue
        key = (_id(f), int(f["season"]), int(f["week"]))
        if key in lookup[source]:
            raise ModelingError("Choose one forecast vintage per source-event")
        if f.get("scoring_hash") not in (None, model["scoring_hash"]):
            raise ModelingError("Ablation forecast scoring mismatch")
        lookup[source][key] = f
    resolved = {}
    id_map = id_map or {}
    for row in outcomes:
        if not _regular(row) or _position(row) not in POSITIONS:
            continue
        pid = id_map.get(_id(row), _id(row))
        key = (str(pid), int(row["season"]), int(row["week"]))
        if key in resolved:
            raise ModelingError("Multiple outcome rows for a source-ablation event")
        resolved[key] = row
    common_forecasts = set(next(iter(lookup.values())))
    for source in lookup.values():
        common_forecasts &= set(source)
    common = set(resolved)
    for source in lookup.values():
        common &= set(source)
    common = sorted(common)
    evidence = {"registry": registry, "model_id": model["id"], "evaluated_at": time.time(),
                "comparison_count": registry["comparison_count"],
                "forecast_rows_by_source": {s:len(v) for s,v in lookup.items()},
                "common_forecast_player_weeks": len(common_forecasts),
                "resolved_outcome_rows": len(resolved), "common_support_rows": len(common),
                "results": {}}
    if not common:
        block_issues = ["No common resolved player-week events"]
        if not common_forecasts:
            block_issues.append("No aligned weekly forecast common support; annual player overlap does not qualify")
        evidence.update(evidence_status="blocked_no_resolved_common_outcomes",
                        validation_gate={"passed": False, "issues": block_issues})
        if require_validation:
            raise ModelingError("No resolved common-support events for source validation")
        return evidence
    issues = []
    issues.extend(_event_cutoff_issues([resolved[k] for k in common], registry["cutoff"]))
    if max(model["training"]["seasons"]) >= min(k[1] for k in common):
        issues.append("annual_residual_fit_overlaps_or_follows_target_season")
    if registry["created_at"] is None or registry["created_at"] > registry["cutoff"]:
        issues.append("registration_after_claimed_cutoff")
    chosen = [lookup[s][k] for s in lookup for k in common]
    audit = temporal_audit(chosen, registry["cutoff"])
    if not audit["passed"]:
        issues.append("source_knowability_or_vintage_failed")
    if not model["training"].get("temporal_audit", {}).get("passed"):
        issues.append("historical_residual_fit_not_verified_PIT")
    if require_validation and issues:
        raise ModelingError("Source validation blocked: " + "; ".join(issues))
    rng = random.Random(seed)
    losses, squared, weeks = defaultdict(list), defaultdict(list), defaultdict(lambda: defaultdict(list))
    for key in common:
        row = resolved[key]
        g = model["groups"].get(_group(row))
        if not g:
            raise ModelingError("No empirical residual cohort for common-support event")
        residuals = [rng.choice(g.get("residuals") or [0]) for _ in range(draws)]
        y = _score(row, model["scoring"])["points"]
        means = {}
        for source in lookup:
            f = lookup[source][key]
            value = _num(f.get("mean"))
            if value is None:
                value = score_stats(_stats(f), model["scoring"], source_schema="sleeper", sparse_zero=True)["points"]
            means[source] = value
        for comparison in registry["comparisons"]:
            label = comparison["label"]
            center = _mean([means[s] for s in comparison["sources"]])
            loss = crps_loss([center+e for e in residuals], y)
            losses[label].append(loss)
            squared[label].append((center-y)**2)
            weeks[label][key[1:]].append(loss)
    baseline = losses[registry["baseline"]]
    for label, loss in losses.items():
        evidence["results"][label] = {"n": len(loss), "CRPS_loss": _mean(loss),
                                       "mean_squared_error": _mean(squared[label]),
                                       "paired_delta_CRPS_vs_baseline": _mean([a-b for a,b in zip(loss,baseline)]),
                                       "NFL_week_blocks": len(weeks[label])}
    evidence.update(evidence_status="prospectively_registered_temporal_validation" if not issues else "exploratory_retrospective_not_PIT_validation",
                    validation_gate={"passed": not issues, "issues": issues, "source_audit": audit},
                    limitations=["All comparisons use the intersection of resolved source coverage.",
                                 "Smaller lower-is-better CRPS delta favors the addition; no independent-row significance is claimed.",
                                 "Shared historical residual proxy has not been calibrated as provider forecast errors.",
                                 "No future champion or synthetic outcome is used to score a real forecast."])
    return evidence
