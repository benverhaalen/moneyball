"""Read-only before/after evidence for future roster actions or manual changes."""
import time
from .sleeper import Client, DataError
from .store import digest

FIELDS = ("players", "starters", "reserve", "taxi")


def team_state(store, config):
    c = Client(store, force=True)
    rosters = c.get(f"league/{config['league_id']}/rosters", 0)
    mine = [r for r in rosters if r.get("owner_id") == config["user_id"] or config["user_id"] in (r.get("co_owners") or [])]
    if len(mine) != 1:
        raise DataError("Cannot uniquely identify owned roster")
    r = mine[0]
    return {"league_id": config["league_id"], "roster_id": r["roster_id"],
            **{k: (r.get(k) or []) if k == "starters" else sorted(r.get(k) or []) for k in FIELDS}}


def check_team(store, config, expected):
    required = {"league_id", "roster_id", *FIELDS}
    if set(expected) != required or expected["league_id"] != config["league_id"]:
        raise DataError("Expected state must contain league_id, roster_id, players, starters, reserve, taxi for this league")
    if any(not isinstance(expected[k], list) or any(not isinstance(p, str) for p in expected[k]) for k in FIELDS):
        raise DataError("Expected player fields must be arrays of string IDs")
    expected = {**expected, **{k: expected[k] if k == "starters" else sorted(expected[k]) for k in FIELDS}}
    actual = team_state(store, config)
    differences = {k: {"expected": expected[k], "actual": actual[k]} for k in required if expected[k] != actual[k]}
    receipt = {"ok": not differences, "checked_at": time.time(), "expected_hash": digest(expected),
               "observed_hash": digest(actual), "differences": differences, "observed": actual,
               "verification": "fresh public API read-back; starters compared in slot order; no mutation performed"}
    # Keep these separate from data-sync receipts.
    import uuid
    path = store.root / f"team-check-{uuid.uuid4().hex}.json"
    from .store import encode
    path.write_text(encode(receipt) + "\n")
    path.chmod(0o600)
    return {**receipt, "receipt": str(path)}
