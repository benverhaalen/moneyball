"""Compare conditional dynasty evaluations without predicting draft choices.

Input: {"models": {name: simulation.simulate(...) result, ...}}. Each model
must evaluate the same actions against the same baseline and expose annual
championship probabilities. Scenarios are stress cases, never probability
weights. This module performs decision accounting; it does not fit forecasts.
"""
import argparse
import copy
import json
import math
from pathlib import Path


def _probability(summary):
    if not isinstance(summary, dict):
        raise ValueError("Each annual result must be a simulation summary")
    value = summary.get("estimate")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Missing numeric annual championship probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Annual championship probability outside [0,1]")
    return value


def compare_models(payload, team, *, horizons=None, tolerance=1e-12):
    """Return horizon-specific regret and an unweighted model-mean frontier.

    Dominance here concerns supplied conditional point estimates only. Sampling
    error and model validity are deliberately not turned into confidence claims.
    Sum of annual title probabilities = expected title count by linearity;
    it is not probability of at least one title and needs no year independence.
    """
    if not isinstance(payload, dict):
        raise ValueError("Input must be an object containing models")
    models = payload.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("Supply a nonempty models mapping")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Tolerance must be finite and nonnegative")
    team = str(team)
    actions, years, baseline = None, None, None
    probabilities, assumptions, evidence = {}, {}, {}
    for model_name, report in models.items():
        if not isinstance(report, dict):
            raise ValueError("Each model must be a full simulation report object")
        cases = report.get("scenarios")
        if not isinstance(cases, dict) or not cases:
            raise ValueError("Each model must contain named action scenarios")
        if actions is None:
            actions = sorted(cases)
            baseline = report.get("baseline")
            years = report.get("years")
            if isinstance(years, bool) or not isinstance(years, int) or years < 1:
                raise ValueError("Each model needs a positive integer horizon")
            if baseline not in cases:
                raise ValueError("Baseline must be an evaluated action")
        if set(cases) != set(actions):
            raise ValueError("All models must evaluate the same actions")
        if report.get("years") != years or report.get("baseline") != baseline:
            raise ValueError("Models disagree on baseline or horizon")
        probabilities[model_name] = {}
        evidence[model_name] = {
            "draws": report.get("draws"), "seed": report.get("seed"),
            "confidence": report.get("confidence"), "action_summaries": {},
        }
        for action, teams in cases.items():
            if not isinstance(teams, dict):
                raise ValueError("Each action must map team IDs to results")
            if team not in teams:
                raise ValueError("Requested team absent from an action")
            if not isinstance(teams[team], dict):
                raise ValueError("Each team result must be an object")
            annual = teams[team].get("championship_by_year")
            if not isinstance(annual, list) or len(annual) != years:
                raise ValueError("Annual championship results required; cannot reconstruct them from total titles")
            values = [_probability(summary) for summary in annual]
            total = teams[team].get("expected_titles", {}).get("estimate")
            if total is not None and (not math.isfinite(total) or not math.isclose(sum(values), total, abs_tol=1e-8)):
                raise ValueError("Annual probabilities do not reconcile to expected title count")
            probabilities[model_name][action] = values
            evidence[model_name]["action_summaries"][action] = copy.deepcopy({
                key: teams[team].get(key) for key in (
                    "championship_by_year", "paired_delta_championship_by_year",
                    "expected_titles", "paired_delta_expected_titles",
                    "at_least_one_title", "current_championship")
            })
        assumptions[model_name] = report.get("assumptions", {})
    horizons = sorted(set(horizons or [h for h in (1, 3, 5) if h <= years] + [years]))
    if any(isinstance(h, bool) or not isinstance(h, int) or not 1 <= h <= years for h in horizons):
        raise ValueError("Requested horizons must be within the evaluated seasons")
    result, vectors = {}, {a: [] for a in actions}
    for horizon in horizons:
        counts = {m: {a: sum(probs[a][:horizon]) for a in actions}
                  for m, probs in probabilities.items()}
        best = {m: max(values.values()) for m, values in counts.items()}
        action_rows = {}
        for action in actions:
            by_model = {}
            for model_name in probabilities:
                count = counts[model_name][action]
                vectors[action].append(count)
                by_model[model_name] = {
                    "expected_titles": count,
                    "delta_expected_titles": count - counts[model_name][baseline],
                    "regret_expected_titles": best[model_name] - count,
                    "annual_title_probabilities": probabilities[model_name][action][:horizon],
                }
            deltas = [row["delta_expected_titles"] for row in by_model.values()]
            action_rows[action] = {
                "models": by_model,
                "delta_range": [min(deltas), max(deltas)],
                "worst_model_regret": max(row["regret_expected_titles"] for row in by_model.values()),
                "direction_changes_across_models": min(deltas) < -tolerance and max(deltas) > tolerance,
            }
        result[str(horizon)] = action_rows
    dominated_by = {}
    for action in actions:
        dominated_by[action] = [other for other in actions if other != action
            and all(b >= a - tolerance for a, b in zip(vectors[action], vectors[other]))
            and any(b > a + tolerance for a, b in zip(vectors[action], vectors[other]))]
    return {
        "input_kind": "synthetic_software_fixture" if payload.get("synthetic_software_fixture_only") is True else "supplied_conditional_model_evaluations",
        "team": team, "baseline": baseline, "evaluated_years": years,
        "horizons": result,
        "point_estimate_frontier": [a for a in actions if not dominated_by[a]],
        "point_estimate_dominated_by": dominated_by,
        "model_assumptions": assumptions,
        "model_evidence": evidence,
        "interpretation": {
            "objective": "Expected championship count at each requested horizon; annual probabilities retained",
            "scenario_weights": "None. Scenario frequency is not an estimated probability.",
            "frontier": "Nondominated across supplied model/horizon point estimates, not statistically proven dominance",
            "uncertainty": "No empirical confidence interval inferred. Original paired Monte Carlo summaries/standard errors and run metadata are retained in model_evidence; they do not cover model error.",
            "future_policy": "Inherited from each model. Frozen holdings cannot establish an adaptive dynasty strategy.",
            "scope": "Conditional action comparison only; no opponent move predictions, ADP utility or automatic transactions",
            "not_reported": "Probability of at least one title cannot be reconstructed from annual marginals alone",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--team", required=True)
    parser.add_argument("--horizons", help="Comma-separated evaluated horizons, e.g. 1,3,5")
    args = parser.parse_args()
    try:
        horizons = [int(x) for x in args.horizons.split(",")] if args.horizons else None
        output = compare_models(json.loads(args.input.read_text()), args.team, horizons=horizons)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, json.dumps({"error": str(exc)}) + "\n")
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
