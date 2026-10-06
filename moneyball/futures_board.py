"""Export all twelve teams' annual futures from supplied simulation reports.

This module validates and arranges results. It generates no player forecasts,
simulated outcomes, model weights, or empirical confidence intervals.
"""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import tempfile


POLICIES = ("frozen_holdings", "adaptive", "other", "unknown")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be an explicit nonempty statement")
    return value


def _number(value, label, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + " must be finite and numeric")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(label + " outside its permitted range")
    return value


def _summary(value, label, low=None, high=None):
    if not isinstance(value, dict):
        raise ValueError(label + " must contain a simulation summary")
    estimate = _number(value.get("estimate"), label + " estimate", low, high)
    if value.get("monte_carlo_se") is not None:
        _number(value["monte_carlo_se"], label + " Monte Carlo SE", 0)
        _text(value.get("interval_meaning"), label + " uncertainty meaning")
    if "draws" in value:
        n = value["draws"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(label + " draws must be a positive integer")
    return estimate


def _team_map(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must map team IDs to results")
    result = {}
    for raw_id, row in value.items():
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise ValueError(label + " has an invalid team ID")
        team = str(raw_id)
        if not team or team != team.strip():
            raise ValueError(label + " has an empty or padded team ID")
        if team in result:
            raise ValueError(label + " has duplicate team IDs after normalization")
        if not isinstance(row, dict):
            raise ValueError(label + " team result must be an object")
        result[team] = row
    if len(result) != 12:
        raise ValueError(label + " must contain exactly twelve teams")
    return result


def _close(actual, expected, tolerance, label):
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=tolerance):
        raise ValueError(label + f" does not reconcile: {actual!r} versus {expected!r}")


def export_board(payload, *, season_labels, model_descriptors, tolerance=1e-8):
    """Validate {models: {name: simulate(...)}} and return annual league boards.

    Explicit calendar labels and a descriptor for every model are required.
    A descriptor contains future_policy_mode and horizon_semantics. Source
    reports must also retain their assumptions, including future_policy.
    Missing paired annual summaries remain missing: only point differences
    are calculated, without inventing a paired standard error.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict) or not payload["models"]:
        raise ValueError("Input must contain a nonempty models mapping")
    _number(tolerance, "Tolerance", 0)
    if not isinstance(season_labels, (list, tuple)) or not season_labels:
        raise ValueError("Explicit annual season_labels are required")
    if any(isinstance(y, bool) or not isinstance(y, int) or y < 1 for y in season_labels):
        raise ValueError("Season labels must be positive integer NFL seasons")
    if list(season_labels) != list(range(season_labels[0], season_labels[0] + len(season_labels))):
        raise ValueError("Season labels must be unique consecutive annual seasons")
    models = payload["models"]
    if not isinstance(model_descriptors, dict) or set(model_descriptors) != set(models):
        raise ValueError("Explicit model_descriptors must cover exactly the supplied models")
    years = len(season_labels)
    common_teams, common_actions, baseline = None, None, None
    output_models = {}
    for model_name, report in models.items():
        _text(model_name, "Model name")
        descriptor = model_descriptors[model_name]
        if not isinstance(descriptor, dict) or descriptor.get("future_policy_mode") not in POLICIES:
            raise ValueError("Each descriptor needs an explicit future_policy_mode")
        _text(descriptor.get("horizon_semantics"), "Horizon semantics")
        if not isinstance(report, dict):
            raise ValueError("Each model must be a simulation report")
        if isinstance(report.get("years"), bool) or not isinstance(report.get("years"), int) or report.get("years") != years:
            raise ValueError("Report years must match all explicit season labels")
        assumptions = report.get("assumptions")
        if not isinstance(assumptions, dict) or not assumptions:
            raise ValueError("Model assumptions must be supplied explicitly")
        policy_statement = _text(assumptions.get("future_policy"), "Source future_policy")
        if descriptor["future_policy_mode"] == "adaptive" and policy_statement.lower().startswith("frozen holdings"):
            raise ValueError("Adaptive descriptor contradicts the source frozen holdings policy")
        cases = report.get("scenarios")
        if not isinstance(cases, dict) or not cases:
            raise ValueError("Each model needs evaluated action scenarios")
        for action in cases:
            _text(action, "Action name")
        _text(report.get("baseline"), "Baseline")
        if report["baseline"] not in cases:
            raise ValueError("Baseline must be an evaluated action")
        if common_actions is None:
            common_actions, baseline = set(cases), report["baseline"]
        if set(cases) != common_actions or report["baseline"] != baseline:
            raise ValueError("All models must share the same actions and baseline")
        normalized = {}
        for action, teams in cases.items():
            rows = _team_map(teams, model_name + "/" + action)
            if common_teams is None:
                common_teams = set(rows)
            if set(rows) != common_teams:
                raise ValueError("Every model/action must contain the same twelve team IDs")
            normalized[action] = rows
        team_order = sorted(common_teams, key=lambda t: (not t.isdecimal(), int(t) if t.isdecimal() else t))
        annual_boards = {str(y): {} for y in season_labels}
        values, paired_values = {}, {}
        for action, teams in normalized.items():
            values[action], paired_values[action] = {}, {}
            paired_present = {"paired_delta_championship_by_year" in r for r in teams.values()}
            if len(paired_present) != 1:
                raise ValueError("Paired annual components must cover all teams or none within an action")
            for team, row in teams.items():
                label = model_name + "/" + action + "/" + team
                annual = row.get("championship_by_year")
                if not isinstance(annual, list) or len(annual) != years:
                    raise ValueError(label + " missing complete annual championship components")
                values[action][team] = [_summary(x, label + " annual", 0, 1) for x in annual]
                if "expected_titles" in row:
                    total = _summary(row["expected_titles"], label + " expected titles", 0, years)
                    _close(sum(values[action][team]), total, tolerance, label + " annual title sum")
                if "current_championship" in row:
                    current = _summary(row["current_championship"], label + " current championship", 0, 1)
                    _close(current, values[action][team][0], tolerance, label + " current/first year")
                if "paired_delta_championship_by_year" in row:
                    paired = row["paired_delta_championship_by_year"]
                    if not isinstance(paired, list) or len(paired) != years:
                        raise ValueError(label + " missing complete paired annual components")
                    paired_values[action][team] = [_summary(x, label + " paired annual delta", -1, 1) for x in paired]
                    if "paired_delta_expected_titles" in row:
                        delta_total = _summary(row["paired_delta_expected_titles"], label + " paired title count", -years, years)
                        _close(sum(paired_values[action][team]), delta_total, tolerance, label + " paired annual sum")
                elif "paired_delta_expected_titles" in row:
                    raise ValueError(label + " aggregate paired delta lacks annual components")
            for i in range(years):
                _close(sum(values[action][t][i] for t in teams), 1, tolerance, label + " annual championship conservation")
                if paired_values[action]:
                    _close(sum(paired_values[action][t][i] for t in teams), 0, tolerance, label + " paired delta conservation")
        for action, teams in normalized.items():
            for i, season in enumerate(season_labels):
                annual_rows = []
                point_deltas = []
                for team in team_order:
                    row = teams[team]
                    point_delta = values[action][team][i] - values[baseline][team][i]
                    point_deltas.append(point_delta)
                    paired = row.get("paired_delta_championship_by_year")
                    if paired is not None:
                        _close(paired_values[action][team][i], point_delta, tolerance,
                               model_name + "/" + action + "/" + team + " paired delta versus baseline")
                    annual_rows.append({
                        "team_id": team,
                        "championship": copy.deepcopy(row["championship_by_year"][i]),
                        "delta_from_baseline": point_delta,
                        "paired_delta_championship": copy.deepcopy(paired[i]) if paired is not None else None,
                        "delta_uncertainty_status": "supplied_paired_sampling_summary" if paired is not None else
                            "point_difference_only_no_paired_uncertainty_supplied",
                    })
                _close(sum(point_deltas), 0, tolerance, model_name + "/" + action + " point delta conservation")
                annual_boards[str(season)][action] = annual_rows
        output_models[model_name] = {
            "descriptor": copy.deepcopy(descriptor),
            "assumptions": copy.deepcopy(assumptions),
            "source_run_metadata": {k: copy.deepcopy(report.get(k)) for k in ("draws", "seed", "confidence")},
            "annual_boards": annual_boards,
            "source_aggregate_summaries": {action: {team: copy.deepcopy({k: v for k, v in row.items()
                if k not in ("championship_by_year", "paired_delta_championship_by_year")})
                for team, row in teams.items()} for action, teams in normalized.items()},
        }
    ranges = {}
    for season in map(str, season_labels):
        ranges[season] = {}
        for action in sorted(common_actions):
            rows = []
            for index, team in enumerate(team_order):
                estimates = [m["annual_boards"][season][action][index] for m in output_models.values()]
                p = [r["championship"]["estimate"] for r in estimates]
                d = [r["delta_from_baseline"] for r in estimates]
                rows.append({"team_id": team, "championship_probability_range": [min(p), max(p)],
                             "delta_point_estimate_range": [min(d), max(d)],
                             "delta_direction_changes": min(d) < -tolerance and max(d) > tolerance})
            ranges[season][action] = rows
    return {
        "schema_version": 1,
        "input_kind": "synthetic_software_fixture" if payload.get("synthetic_software_fixture_only") is True else
            "supplied_conditional_simulation_reports",
        "season_labels": list(season_labels), "team_ids": team_order,
        "baseline": baseline, "actions": sorted(common_actions),
        "models": output_models, "across_model_ranges": ranges,
        "validation": {"team_count": 12, "absolute_tolerance": tolerance,
                       "annual_probability_sum": 1, "annual_delta_sum": 0},
        "interpretation": {
            "scope": "Result exporter only; no new forecasts, simulations, transactions or opponent-choice predictions",
            "ranges": "Min/max across supplied model point estimates, not confidence or predictive intervals; no scenario weights",
            "uncertainty": "Supplied summaries retain their original meaning. Monte Carlo sampling error does not cover forecast or policy misspecification; absent uncertainty is not zero uncertainty.",
            "future_policy": "Explicit descriptor and source assumptions retained per model; adaptive status is a supplied claim, not certified by this exporter",
            "horizon": "Calendar labels supplied explicitly; annual probabilities are marginal title probabilities, not probability of at least one title",
        },
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key: " + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=_unique_object)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--seasons", required=True, help="Explicit consecutive seasons, e.g. 2026,2027,2028")
    parser.add_argument("--descriptors", type=Path, help="JSON mapping from model names to explicit descriptors")
    parser.add_argument("--future-policy", choices=POLICIES, help="Explicit mode applied to every supplied model")
    parser.add_argument("--horizon-semantics", help="Explicit horizon statement applied to every supplied model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = read_json(args.input)
        seasons = [int(x) for x in args.seasons.split(",")]
        if args.descriptors:
            if args.future_policy or args.horizon_semantics:
                raise ValueError("Use descriptors or uniform policy/horizon flags, not both")
            descriptors = read_json(args.descriptors)
        else:
            if not args.future_policy or not args.horizon_semantics:
                raise ValueError("Supply descriptors or both future-policy and horizon-semantics")
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
                raise ValueError("Input must contain models")
            descriptors = {name: {"future_policy_mode": args.future_policy,
                                 "horizon_semantics": args.horizon_semantics} for name in payload["models"]}
        result = export_board(payload, season_labels=seasons, model_descriptors=descriptors)
        if args.output:
            _write_json(args.output, result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, json.dumps({"error": str(exc)}) + "\n")


if __name__ == "__main__":
    main()
