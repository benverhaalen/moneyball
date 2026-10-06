"""Fail-closed interpretation of a *full*, observed native Sleeper mock AX tree.

This module does not read or operate a browser. A driver supplies freshly read
accessibility text, acts only on a returned observed control, and reads back.
In particular, HTTP fetched_at is not accepted as evidence of a current board.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re


class GuardError(ValueError):
    pass


@dataclass(frozen=True)
class MockSnapshot:
    draft_id: str
    own_slot: int
    own_drafted_count: int
    auto_pick_on: bool
    auto_pick_control: int | None
    completed: bool
    pick_no: int | None
    drafting_slot: int | None
    seconds_remaining: int | None
    action: str


def inspect_snapshot(text: str, *, expected_mock_id: str, teams: int = 12,
                     own_slot: int = 5, rounds: int = 28) -> MockSnapshot:
    """Inspect a full observed snapshot; never turn absence in a diff into OFF.

    The expected ID must come from independently verified league_mock metadata.
    This text parser cannot itself verify that a supplied ID is a private mock.
    """
    if not expected_mock_id.isdigit() or teams < 2 or not 1 <= own_slot <= teams or rounds < 1:
        raise GuardError("invalid explicitly supplied mock configuration")
    if not re.search(r"^\s*\d+ standard window", text, re.M) or "Your team's actions" not in text:
        raise GuardError("a full current mock window snapshot is required, not a diff or menu")
    ids = set(re.findall(r"sleeper\.com/(?:beta/)?draft/nfl/(\d+)", text))
    if ids != {expected_mock_id}:
        raise GuardError("wrong or ambiguous draft ID; no browser action authorized")
    if not re.search(r"\bMock Draft\b", text):
        raise GuardError("the observed window does not identify itself as a mock")

    counts = {int(x) for x in re.findall(r"^\s*\d+ text ALL\s+(\d+)\s*/\s*\d+\s*$", text, re.M)}
    if len(counts) != 1 or next(iter(counts)) > rounds:
        raise GuardError("missing or conflicting own-team holding count")
    own_count = next(iter(counts))
    controls = re.findall(r"^\s*(\d+) switch Description: Auto-pick, Value: (on|off)\s*$", text, re.M)
    if len(controls) > 1:
        raise GuardError("ambiguous Auto-Pick control")
    auto_text = "You’re on Auto-Pick" in text or "You're on Auto-Pick" in text
    auto_on = bool(controls and controls[0][1] == "on")
    if auto_text and not auto_on:
        raise GuardError("automatic-mode warning lacks an unambiguous ON control")
    auto_control = int(controls[0][0]) if controls else None
    completed = "Draft Completed" in text or "Draft completed" in text

    clocks = re.findall(r"^\s*\d+ pop up button (\d+)\.(\d+) ON THE CLOCK (\d+):(\d{2})\s*$", text, re.M)
    if len(clocks) > 1 or (completed and clocks):
        raise GuardError("conflicting live draft clocks")
    pick_no = drafting_slot = remaining = None
    if clocks:
        rnd, within, minutes, seconds = map(int, clocks[0])
        if not 1 <= rnd <= rounds or not 1 <= within <= teams or seconds >= 60:
            raise GuardError("malformed clock or snake pick")
        pick_no = (rnd - 1) * teams + within
        drafting_slot = within if rnd % 2 else teams + 1 - within
        remaining = 60 * minutes + seconds
    # Finished status wins: a completed mock can retain the previous AUTO label.
    if completed:
        action = "no_action_completed"
    elif auto_on:
        action = "disable_auto_pick_then_read_back"
    elif not clocks:
        action = "read_again_no_live_clock"
    elif drafting_slot != own_slot:
        action = "observe_intervening_picks"
    elif remaining == 0:
        action = "read_again_timeout_boundary"
    else:
        action = "manual_turn_requires_matching_prepared_choice"
    return MockSnapshot(expected_mock_id, own_slot, own_count, auto_on,
                        auto_control, completed, pick_no, drafting_slot,
                        remaining, action)


def require_prepared_turn(snapshot: MockSnapshot, *, expected_pick: int,
                          expected_own_count: int, minimum_seconds: int = 5) -> None:
    """Reject stale instructions, new holdings, automatic mode, and zero clocks.

    Passing checks only authorizes consulting the existing prepared choice. It
    does not choose a player, establish availability, or endorse its reasoning.
    """
    if minimum_seconds < 1:
        raise GuardError("a positive execution-time margin is required")
    if snapshot.action != "manual_turn_requires_matching_prepared_choice":
        raise GuardError("not an observed manual own-team turn")
    if snapshot.pick_no != expected_pick:
        raise GuardError("prepared instruction belongs to a different turn")
    if snapshot.own_drafted_count != expected_own_count:
        raise GuardError("own holdings changed; prepared strategy must be reconsidered")
    if snapshot.seconds_remaining is None or snapshot.seconds_remaining < minimum_seconds:
        raise GuardError("insufficient observed time for guarded execution")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("--mock-id", required=True)
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--slot", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=28)
    args = ap.parse_args()
    value = inspect_snapshot(args.snapshot.read_text(), expected_mock_id=args.mock_id,
                             teams=args.teams, own_slot=args.slot, rounds=args.rounds)
    print(json.dumps(asdict(value), sort_keys=True))


if __name__ == "__main__":
    main()
