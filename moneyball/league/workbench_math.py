"""Small, inspectable scenario mechanisms. Parameters are supplied, never fitted.

These calculators do not turn an author's assumptions into measured probabilities.
Their outputs are conditional on those assumptions and retain component gaps.
"""
import math

from .store import DataError


def number(value, label, lo=None, hi=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DataError(f"{label} must be a finite number")
    if lo is not None and value < lo or hi is not None and value > hi:
        raise DataError(f"{label} outside allowed range")
    return float(value)


def distribution(values, label):
    if not isinstance(values, dict) or not values:
        raise DataError(f"{label} requires a nonempty state-to-probability object")
    out = {k: number(v, label, 0, 1) for k, v in values.items()}
    if not math.isclose(sum(out.values()), 1, abs_tol=1e-9):
        raise DataError(f"{label} must sum to one; no automatic normalization")
    return out


def calculate(request):
    """Explicit priors and transitions, useful before empirical model fitting."""
    if not isinstance(request, dict):
        raise DataError("Calculator request must be an object")
    kind = request.get("kind")
    if kind == "shrinkage":
        observed = number(request.get("estimate"), "estimate")
        prior = number(request.get("prior"), "prior")
        n = number(request.get("sample_size"), "sample_size", 0)
        k = number(request.get("prior_strength"), "prior_strength", 0)
        if n + k == 0:
            raise DataError("Sample and prior strength cannot both be zero")
        weight = n / (n + k)
        result = {"estimate": weight * observed + (1 - weight) * prior, "data_weight": weight}
    elif kind == "role_transition":
        state = distribution(request.get("initial"), "initial")
        steps = request.get("steps")
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
            raise DataError("steps must be an integer from 1 to 100")
        matrix = request.get("transitions", {})
        if not isinstance(matrix, dict) or set(matrix) != set(state):
            raise DataError("Every role needs a transition row, including exit/nonstarter states")
        matrix = {k: distribution(v, k) for k, v in matrix.items()}
        if any(set(row) != set(state) for row in matrix.values()):
            raise DataError("Transition rows must cover identical states")
        history = [state]
        for _ in range(steps):
            state = {end: sum(state[start] * matrix[start][end] for start in state) for end in state}
            history.append(state)
        result = {"state_path": history, "model": "supplied homogeneous Markov transitions; not a fitted HMM/HSMM"}
    elif kind == "availability_mixture":
        p = number(request.get("availability"), "availability", 0, 1)
        mean = number(request.get("conditional_mean"), "conditional_mean")
        variance = number(request.get("conditional_variance"), "conditional_variance", 0)
        result = {"mean": p * mean, "variance": p * variance + p * (1 - p) * mean ** 2,
                  "assumption": "Zero scoring if absent; no replacement or cross-player independence inferred"}
    elif kind == "opportunity":
        result = opportunity(request)
    else:
        raise DataError("Unknown calculator; use shrinkage, role_transition, availability_mixture, opportunity")
    return {**result, "assurance": "Conditional arithmetic only; input calibration and causal validity are not established"}


def opportunity(data):
    """Team totals -> allocation -> conversion. Incomplete branches stay absent."""
    if not isinstance(data, dict) or any(not isinstance(data[k], dict) for k in ("receiving", "rushing", "passing") if k in data):
        raise DataError("Opportunity branches must be objects")
    stats, trace = {}, {}
    if "receiving" in data:
        r = data["receiving"]
        dropbacks = number(r.get("team_dropbacks"), "team_dropbacks", 0)
        routes = dropbacks * number(r.get("route_share"), "route_share", 0, 1)
        targets = routes * number(r.get("targets_per_route"), "targets_per_route", 0, 1)
        catches = targets * number(r.get("catch_rate"), "catch_rate", 0, 1)
        stats.update(rec=catches, rec_yd=catches * number(r.get("yards_per_catch"), "yards_per_catch", 0),
                     rec_td=targets * number(r.get("td_per_target"), "td_per_target", 0, 1))
        trace.update(routes=routes, targets=targets)
    if "rushing" in data:
        r = data["rushing"]
        attempts = number(r.get("team_attempts"), "team_attempts", 0) * number(r.get("carry_share"), "carry_share", 0, 1)
        stats.update(rush_att=attempts, rush_yd=attempts * number(r.get("yards_per_carry"), "yards_per_carry", 0),
                     rush_td=attempts * number(r.get("td_per_carry"), "td_per_carry", 0, 1))
        trace["carries"] = attempts
    if "passing" in data:
        r = data["passing"]
        attempts = number(r.get("team_attempts"), "team_attempts", 0) * number(r.get("attempt_share"), "attempt_share", 0, 1)
        stats.update(pass_att=attempts, pass_yd=attempts * number(r.get("yards_per_attempt"), "yards_per_attempt", 0),
                     pass_td=attempts * number(r.get("td_per_attempt"), "td_per_attempt", 0, 1),
                     pass_int=attempts * number(r.get("int_per_attempt"), "int_per_attempt", 0, 1))
        trace["pass_attempts"] = attempts
    if not stats:
        raise DataError("Opportunity calculation needs receiving, rushing or passing inputs")
    return {"stats": stats, "trace": trace}


def score_projection(row, scoring):
    if scoring.get("unsupported") or scoring.get("nonlinear"):
        return {"status": "unsupported_scoring", "mean": None}
    weights = scoring.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise DataError("Explicit scoring weights are required")
    if ("stats" in row) == ("opportunity" in row):
        raise DataError("Supply exactly one of stats or opportunity per projection")
    derived = opportunity(row["opportunity"]) if "opportunity" in row else {"stats": row["stats"], "trace": {}}
    if not isinstance(derived["stats"], dict):
        raise DataError("stats must be an object")
    stats = {k: number(v, k) for k, v in derived["stats"].items()}
    zero = row.get("zero_components", [])
    if not isinstance(zero, list) or any(not isinstance(k, str) for k in zero) or len(zero) != len(set(zero)) or set(zero) & set(stats):
        raise DataError("zero_components must be explicit unique-to-zero component names")
    stats.update({k: 0.0 for k in zero})
    weights = {k: number(v, k) for k, v in weights.items()}
    missing = sorted(k for k, v in weights.items() if v != 0 and k not in stats)
    if missing:
        return {"status": "missing_components", "missing": missing, "mean": None, **derived}
    p = number(row.get("availability"), "availability", 0, 1)
    conditional = sum(v * stats.get(k, 0) for k, v in weights.items())
    result = {"status": "complete_conditional", "mean": conditional * p, "conditional_mean": conditional,
              "availability": p, "stats": stats, "trace": derived["trace"],
              "variance": None, "variance_scope": "Unknown unless conditional_variance supplied; never summed across teammates"}
    if "conditional_variance" in row:
        variance = number(row["conditional_variance"], "conditional_variance", 0)
        result["variance"] = p * variance + p * (1 - p) * conditional ** 2
    return result
