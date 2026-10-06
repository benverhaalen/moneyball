"""Exact, forecast-conditional lineup coverage diagnostics.

This module deliberately does not estimate injuries, role changes, or title
probabilities.  Callers supply a roster prefix, eligibility, one coherent value
map, and explicit unavailable-player scenarios.  Missing values are errors so
that an absent provider row cannot silently become a zero projection.
"""

from math import isfinite

from .simulation import ELIGIBILITY, EXPERIMENTAL_STARTERS, positions


def _assignment_key(assignment):
    """Return a stable tie-break key; unfilled slots sort after player IDs."""
    return tuple("\uffff" if player_id is None else player_id for player_id in assignment)


def exact_assignment(roster, players, values, slots=EXPERIMENTAL_STARTERS):
    """Maximize filled slots, then supplied value, under exact slot eligibility.

    The result identifies the player assigned to every individual slot.  Values
    may be negative because a legal mandatory slot must still be filled.  Every
    rostered player with recognized fantasy eligibility must have a finite value;
    callers must explicitly exclude unavailable or unmodeled players first.
    """
    roster_ids = sorted({str(player_id) for player_id in roster})
    missing_players = [player_id for player_id in roster_ids if player_id not in players]
    if missing_players:
        raise ValueError("Missing player records: " + repr(missing_players))

    eligible_ids = [player_id for player_id in roster_ids if positions(players[player_id])]
    missing_values = [player_id for player_id in eligible_ids if player_id not in values]
    if missing_values:
        raise ValueError("Missing values: " + repr(missing_values))
    nonfinite = [player_id for player_id in eligible_ids if not isfinite(float(values[player_id]))]
    if nonfinite:
        raise ValueError("Non-finite values: " + repr(nonfinite))

    empty = (None,) * len(slots)
    # mask -> (score, assignment indexed by the original slot tuple)
    states = {0: (0.0, empty)}
    for player_id in eligible_ids:
        legal_positions = positions(players[player_id])
        updated = dict(states)
        for mask, (score, assignment) in states.items():
            for slot_index, slot in enumerate(slots):
                if mask & (1 << slot_index) or not legal_positions & ELIGIBILITY[slot]:
                    continue
                new_mask = mask | (1 << slot_index)
                placed = list(assignment)
                placed[slot_index] = player_id
                candidate = (score + float(values[player_id]), tuple(placed))
                incumbent = updated.get(new_mask)
                if (
                    incumbent is None
                    or candidate[0] > incumbent[0]
                    or (
                        candidate[0] == incumbent[0]
                        and _assignment_key(candidate[1]) < _assignment_key(incumbent[1])
                    )
                ):
                    updated[new_mask] = candidate
        states = updated

    most_filled = max(mask.bit_count() for mask in states)
    best_score = max(score for mask, (score, _) in states.items() if mask.bit_count() == most_filled)
    finalists = [
        (mask, score, assignment)
        for mask, (score, assignment) in states.items()
        if mask.bit_count() == most_filled and score == best_score
    ]
    best_mask, score, assignment = min(finalists, key=lambda row: _assignment_key(row[2]))
    assignments = [
        {
            "slot_index": index,
            "slot": slot,
            "player_id": assignment[index],
            "value": None if assignment[index] is None else float(values[assignment[index]]),
        }
        for index, slot in enumerate(slots)
    ]
    return {
        "complete": best_mask.bit_count() == len(slots),
        "filled_slots": best_mask.bit_count(),
        "score": score,
        "assignments": assignments,
        "unfilled_slots": [row["slot_index"] for row in assignments if row["player_id"] is None],
    }


def stress_scenarios(roster, players, values, scenarios, *, unavailable=(), slots=EXPERIMENTAL_STARTERS):
    """Assign one roster under named, explicit sets of unavailable player IDs."""
    roster_ids = {str(player_id) for player_id in roster}
    baseline_unavailable = {str(player_id) for player_id in unavailable}
    unknown = baseline_unavailable - roster_ids
    if unknown:
        raise ValueError("Unavailable players outside roster: " + repr(sorted(unknown)))

    result = {}
    for name, absent in scenarios.items():
        absent_ids = baseline_unavailable | {str(player_id) for player_id in absent}
        outside = absent_ids - roster_ids
        if outside:
            raise ValueError("Scenario players outside roster: " + repr(sorted(outside)))
        active = sorted(roster_ids - absent_ids)
        result[name] = {
            "absent": sorted(absent_ids),
            **exact_assignment(active, players, values, slots),
        }
    return result


def compare_candidate_branches(
    prefix,
    candidates,
    players,
    values,
    scenarios,
    *,
    unavailable=(),
    slots=EXPERIMENTAL_STARTERS,
):
    """Compare candidates using only an explicitly supplied draft prefix.

    This function cannot look ahead: later holdings are absent unless the caller
    puts them in ``prefix``.  Candidate IDs must also be absent from that prefix.
    """
    prefix_ids = [str(player_id) for player_id in prefix]
    if len(set(prefix_ids)) != len(prefix_ids):
        raise ValueError("Duplicate player in prefix")
    candidate_ids = [str(player_id) for player_id in candidates]
    overlap = set(prefix_ids) & set(candidate_ids)
    if overlap:
        raise ValueError("Candidate already in prefix: " + repr(sorted(overlap)))
    return {
        player_id: stress_scenarios(
            prefix_ids + [player_id],
            players,
            values,
            scenarios,
            unavailable=unavailable,
            slots=slots,
        )
        for player_id in candidate_ids
    }
