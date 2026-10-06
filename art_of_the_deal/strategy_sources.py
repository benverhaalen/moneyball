"""Verified reference jobs; analogy fit is separate from evidence of an edge."""
ASSIGNMENT = {"title":"Kuhn (1955), The Hungarian Method for the Assignment Problem", "url":"https://www.math.utoronto.ca/mccann/1855/KuhnNRL55.pdf", "job":"One asset per eligible slot, joint optimum. Implementation uses small-state dynamic programming for the same assignment problem, not Kuhn's algorithm."}
CAPACITY = {"title":"Van Mieghem (1998), Investment Strategies for Flexible Resources", "url":"https://www.kellogg.northwestern.edu/faculty/VanMieghem/htm/Flex_MS.pdf", "job":"Capacity chosen before uncertain demand; compare contingent service. No imported prices, failure rates or value coefficients."}
TIMING = {"title":"Flight et al. (2022), Expected Value of Sample Information", "url":"https://eprints.whiterose.ac.uk/id/eprint/181245/1/0272989x211045036.pdf", "job":"New information matters only if a valuable action remains available; platform rules determine actual timing."}
HORIZON = {"title":"Bertsekas, MIT Dynamic Programming slides (2015), lecture 9", "url":"https://www.mit.edu/~dimitrib/DP_Slides_2015.pdf", "job":"Explicit horizon and continuation policy; longer lookahead is not automatically better with a poor terminal approximation."}
TOURNAMENT = {"title":"Bettisworth, Jordan and Stamatakis (2023), Phylourny", "url":"https://link.springer.com/article/10.1007/s11222-023-10246-y", "job":"Fixed-tree win recursion conditional on supplied pairwise probabilities; does not validate fantasy probabilities."}
METHODS = {
    "maximum_weight_legal_slot_assignment":[ASSIGNMENT],
    "joint_slot_opportunity_cost":[ASSIGNMENT],
    "observed_replacement_path":[CAPACITY,ASSIGNMENT],
    "rule_conditioned_lineup_timing":[TIMING],
    "capacity_constrained_holdings_conservation":[CAPACITY],
    "rule_exact_event_rescoring":[],
    "finite_postseason_service_window":[HORIZON,TOURNAMENT],
    "transaction_timeline_feasibility":[TIMING],
    "feasible_replacement_branch":[CAPACITY,TIMING],
    "format_conditioned_asset_horizon":[HORIZON],
}

def method(handle):
    refs = METHODS.get(handle,[])
    return {"handle":handle,"sources":refs,
            "verification":"Primary text read in preserved research review; key assignment/capacity/horizon/tournament sources reopened 2026-09-14" if refs else "League arithmetic; no external empirical claim",
            "confidence":{"structural_use":"high when the stated league preconditions hold","league_edge_magnitude":"unmeasured"},
            "transfer_limit":"A reference supports the method or caution, not player inputs, manager behavior, calibrated championship odds, or guaranteed profitable trades."}
