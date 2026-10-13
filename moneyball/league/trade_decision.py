"""Decision obligations carried with evidence, without manufacturing a grade."""


def frame(league, mechanics):
    cohorts = []
    for c in mechanics.get("forecast_comparisons", []):
        cohorts.append({"source": c["source"], "period": c["period"],
                        "conditioning": c["conditioning"], "status": c["status"],
                        "team_changes": [{"team_id": tid,
                                          "team_name": league["teams"][tid]["name"],
                                          "delta_provider_baseline_points": state["delta_points"]}
                                         for tid, state in sorted(c["teams"].items())]})
    return {
        "own_team_id": league.get("own_team_id"),
        "cohort_diagnostics": cohorts,
        "recommendation": None,
        "who_benefits": None,
        "instruction": "The host must turn this packet into a decision: recommend accept, decline, counter, or wait for a named fact; explain who benefits and the tradeoffs for each manager.",
        "answer_contract": {
            "recommendation": "Action for this user's team, confidence stated separately, and the deciding reason under this league's rules and service horizon.",
            "who_benefits": "Assess you, them, both, neither, roughly even, or uncertain. State the criterion and horizon; distinguish relative gain from whether accepting improves your own position.",
            "tradeoffs": "Name each team's actual starter substitutions, lost and gained coverage, required drops, roster flexibility, acquisition cost and retained rights. Compare with holding and verified alternatives.",
            "roughly_even": "Explain which small advantage or preference tips the recommendation: usable weeks, coverage, timing, flexibility or a verified outside option. If plausible uncertainty reverses the ordering, say so and prefer hold/wait or a justified counter; do not manufacture a decisive winner.",
            "conditional_recommendation": "State the pivotal projection as an if/otherwise decision: if X provides more usable service than Y over the named horizon after actual substitutions and costs, choose the supported action; otherwise choose the alternative. Identify whose projection it is, the evidence and assumptions, and which other conditions must hold. X outperforming Y alone need not make the whole package worthwhile. Give a break-even threshold only when coherent inputs support calculating it; otherwise give the qualitative switching condition and missing evidence. Separate the current evidence-based recommendation from the user's alternative belief and explain how adopting that belief changes the decision.",
            "countercase": "Give the strongest reason against your recommendation and the named fact, threshold or preference that would change it.",
        },
        "limits": [
            "A trade can benefit both managers; a larger baseline point gain does not establish an overall winner.",
            "No fixed point tolerance establishes that a trade is even. Compare the difference with uncertainty and decision-relevant costs.",
            "Missing or incompatible forecasts withhold a numerical comparison; they do not imply equality.",
            "Baseline points are conditional diagnostics, not a trade grade, manager acceptance probability or championship estimate.",
        ],
    }
