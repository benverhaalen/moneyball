"""Preregistered, append-only research decisions. Judgments are never effect estimates."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import time


HYPOTHESES = [
    ("draft_qb", "Two early QB selections improve title probability after charging the skipped alternatives", "startup 5/20/29; QB+SF", 5, 3, 2,
     "Paired championship delta vs adaptive and WR-first is positive across preregistered demand/continuation scenarios; simulation robustness is not empirical confirmation"),
    ("draft_timing", "Two-turn completion beats greedy current contribution", "14 then 8 intervening picks", 5, 4, 2,
     "Positive paired title delta in independent evaluation seeds and survival predictions beat constant baseline prospectively"),
    ("capacity", "Legal substitution changes acquisition ordering enough to improve title probability", "10 starters, 15 bench, restricted taxi/IR", 5, 5, 1,
     "Assignment beats prespecified additive policy on held-out decisions; exact code checks alone do not confirm advantage"),
    ("professional_center", "A rented forecast plus fitted uncertainty improves decision inputs", "exact event scoring", 5, 4, 2,
     "Held-out CRPS beats professional-center/simple-residual baseline with paired uncertainty; only trusted-vintage tests eligible"),
    ("volume_persistence", "Opportunity features add predictive information beyond existing professional/lagged-output inputs", "PPR and linear yard/event coefficients", 4, 3, 2,
     "Out-of-time incremental CRPS improvement has positive family-adjusted evidence; no universal 3-5-game threshold assumed"),
    ("efficiency", "Efficiency retains zero incremental predictive information", "scoring weights event counts, not an imposed volume-only model", 4, 2, 2,
     "Refute zero-information claim if efficiency improves prespecified held-out forecast score; absence of significance is not proof of zero"),
    ("qb_inventory", "A third or fourth QB improves multi-year title odds net of displacement and trade uncertainty", "SF and deep bench", 4, 2, 2,
     "Positive paired title delta vs two-QB construction without assuming future buyers or scarcity rent"),
    ("taxi", "Three eligible taxi holdings improve future title odds after counting acquisition and slot costs", "taxi_slots=3; exact eligibility/deadline required", 3, 4, 2,
     "Positive delta after pricing all displaced assets; reject free-option assertion when opportunities or legal slots bind"),
    ("age_horizon", "Age/cohort transitions improve future service forecasts and draft decisions", "persistent holdings", 4, 3, 3,
     "Held-out cohort probability/CRPS improves beyond role/experience; policy survives terminal-value sensitivity"),
    ("correlation", "Joint residual dependence changes title decisions beneficially", "ten players, shared NFL games", 3, 3, 3,
     "Joint model improves held-out matchup proper score; positive robust title delta vs independence for action change"),
    ("role_change", "A changepoint signal adds information before output alone does", "externally allocated opportunities", 3, 3, 3,
     "Predeclared changepoint specification improves next-window CRPS over unflagged baseline, with source-vintage eligibility"),
    ("injury_recovery", "Post-return production differs predictably from prior role after available covariates", "IR restrictions and active lineup", 3, 3, 4,
     "PIT injury/return histories improve held-out conditional role forecasts, not simply total-points comparisons"),
    ("late_season", "NFL competitive state predicts opportunity shifts in league playoff weeks", "playoffs start15", 3, 2, 4,
     "Held-out weeks15-17 conditional proper-score improvement; no retrospective labels as input"),
    ("barter", "Private-preference package menus increase feasible title-improving trades", "11 counterparties, pick trading, review2days", 3, 3, 3,
     "Prospective recorded interest/acceptance improves over simple menu baseline and full league title delta remains positive"),
    ("waivers", "Routine free-agent optimization is low marginal value relative to startup choices", "336 startup selections, deep rosters", 2, 3, 3,
     "Compare available-pool incremental title changes with draft alternatives; include occasional role shocks and do not assume zero value"),
    ("information", "Additional source improves decisions beyond correlated existing information", "120-second picks and finite action windows", 3, 3, 3,
     "Ablation worsens untouched proper scores or decision utility; redundant or late information fails"),
]


def append_event(root, event):
    """Serialize local appenders and chain event hashes to make edits detectable."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "research-events.jsonl"
    with path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        lines = [line for line in f if line.strip()]
        previous = json.loads(lines[-1])["event_hash"] if lines else None
        record = {"recorded_at": time.time(), "previous_hash": previous, **event}
        record["event_hash"] = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        f.seek(0, 2)
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
    return record


def initialize(root):
    root = Path(root)
    path = root / "research-events.jsonl"
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") == "preregister":
                seen.add(r["hypothesis_id"])
    result = []
    for hid, claim, mechanic, magnitude, confidence, difficulty, criterion in HYPOTHESES:
        row = {"hypothesis_id": hid, "claim": claim, "mechanic": mechanic,
               "status": "blocked_on_data", "ordinal_magnitude": magnitude,
               "ordinal_confidence": confidence, "ordinal_data_difficulty": difficulty,
               "priority_score": magnitude * confidence / difficulty,
               "ranking_basis": "explicit planning judgments on 1-5 scale, not measured effects or statistical confidence",
               "success_or_refutation_criterion": criterion,
               "comparison_family": "startup-v1" if hid.startswith("draft") or hid in ("qb_inventory", "capacity") else "forecast-v1",
               "measured_championship_delta": None}
        if hid not in seen:
            append_event(root, {"kind": "preregister", **row})
        result.append(row)
    return sorted(result, key=lambda x: (-x["priority_score"], x["hypothesis_id"]))
