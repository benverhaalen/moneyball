"""Compact JSON CLI for humans, agents, cron and launchd."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from .store import Store, digest, encode
from .sleeper import Client, DataError, context, sync
from .verification import team_state, check_team
from .warehouse import WarehouseError

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1] / ".moneyball"
DEFAULT_ROOT = (_CHECKOUT_ROOT if (_CHECKOUT_ROOT / "config.json").exists()
                else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "moneyball")


def emit(value):
    print(encode(value), flush=True)


def read_config(root):
    path = root / "config.json"
    return json.loads(path.read_text()) if path.exists() else {"leagues": {}}


def save_config(root, config):
    fd, temp = tempfile.mkstemp(dir=root, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(encode(config) + "\n")
        os.replace(temp, root / "config.json")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def league_config(root, alias):
    alias = Store(root).resolve_alias(alias)
    config = read_config(root)["leagues"].get(alias)
    if not config:
        raise DataError(f"Unknown league {alias!r}; run configure first")
    return config


def ensure_players(store, offline=False):
    # Search the small indexed table; do not parse the multi-MB catalog on every query.
    with store.connect() as db:
        row = db.execute("SELECT fetched,hash FROM responses WHERE path='players/nfl'").fetchone()
    if row and (offline or time.time() - row[0] < 86400):
        return {"url": "https://api.sleeper.app/v1/players/nfl", "fetched_at": row[0],
                "age_seconds": round(time.time()-row[0], 3), "cache_hit": True, "sha256": row[1]}
    client = Client(store, offline=offline)
    client.get("players/nfl", 86400)
    return client.evidence["players/nfl"]


def configure(args, store):
    if args.alias is not None and not re.fullmatch(r"[a-z0-9_-]+", args.alias):
        raise DataError("Alias must use lowercase letters, numbers, underscores or hyphens")
    client = Client(store, force=True)
    user = client.get(f"user/{args.username}")
    leagues = client.get(f"user/{user['user_id']}/leagues/nfl/{args.season}")
    if args.league_id:
        match = re.fullmatch(r"(?:https://sleeper\.(?:com|app)/leagues/)?(\d+)(?:/[^?]*)?", args.league_id)
        if not match:
            raise DataError("Supply a numeric league ID or Sleeper /leagues/ URL")
        found = [l for l in leagues if l["league_id"] == match[1]]
    else:
        normalize = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        found = [l for l in leagues if args.name is None or normalize(args.name) in normalize(l["name"])]
    if len(found) != 1:
        raise DataError("Need exactly one league match; use --league-id. Matches: " + encode([
            {"id": l["league_id"], "name": l["name"]} for l in found]))
    league = found[0]
    args.alias = args.alias or "sleeper-" + league["league_id"]
    item = {"platform": "sleeper", "league_id": league["league_id"], "user_id": user["user_id"], "season": args.season, "name": league["name"].strip()}
    rosters = client.get(f"league/{item['league_id']}/rosters")
    if not any(r.get("owner_id") == item["user_id"] or item["user_id"] in (r.get("co_owners") or []) for r in rosters):
        raise DataError("User does not own/co-own a roster in this league")
    config = read_config(store.root)
    config["leagues"][args.alias] = item
    save_config(store.root, config)
    return {"ok": True, "alias": args.alias, **item}


def short_receipt(receipt):
    return {k: v for k, v in receipt.items() if k != "sources"}


def run_sync(args, store):
    try:
        import fcntl
    except ImportError as exc:
        raise DataError("Legacy synchronized writes require Unix file locking; use moneyball agent on this platform") from exc
    config = league_config(store.root, args.league)
    # Prevent overlapping scheduled/manual snapshot publication and history races.
    with (store.root / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client = Client(store, force=args.fresh)
        return sync(client, config, args.league, full=args.full, week=args.week)


def schedule(args, store):
    if sys.platform != "darwin":
        raise DataError("Legacy launchd scheduling requires macOS")
    label = f"com.moneyball.sync.{args.league}"
    path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    if args.action == "status":
        result = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True)
        return {"installed": path.exists(), "loaded": result.returncode == 0, "details": result.stdout.strip() or result.stderr.strip()}
    if args.action == "remove":
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        path.unlink(missing_ok=True)
        return {"removed": label}
    league_config(store.root, args.league)
    project = str(Path(__file__).resolve().parents[1])
    # One short process every five minutes; full history refreshed once daily.
    plist = {"Label": label, "ProgramArguments": [sys.executable, "-m", "moneyball", "--data-dir", str(store.root), "auto-sync", "--league", args.league],
        "WorkingDirectory": project, "StartInterval": args.interval, "RunAtLoad": True,
        "StandardOutPath": str(store.root / f"{args.league}.out.log"), "StandardErrorPath": str(store.root / f"{args.league}.err.log"),
        "ProcessType": "Background", "Umask": 0o077}
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    path.write_bytes(plistlib.dumps(plist))
    result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if result.returncode:
        raise DataError(f"Schedule written but not loaded: {result.stderr.strip()}")
    return {"installed": True, "label": label, "interval_seconds": args.interval, "plist": str(path), "runs_while": "Mac is awake and user is logged in"}


def dispatch(args, store):
    if args.command == 'lab':
        from .lab import dispatch as lab_dispatch
        if args.action in ('draft','move','live') and not args.offline and args.cutoff is None:
            args.league = store.resolve_alias(args.league)
            args.fresh = True
            run_sync(args,store)
        return lab_dispatch(args,store)
    if args.command in ("team-state", "team-check"):
        config = league_config(store.root, args.league)
        if args.command == "team-state":
            return team_state(store, config)
        result = check_team(store, config, json.loads(args.expected.read_text()))
        if not result["ok"]:
            emit(result)
            raise SystemExit(2)
        return result
    if args.command == "discover":
        c = Client(store)
        u = c.get(f"user/{args.username}", 86400)
        return {"user_id": u["user_id"], "leagues": [{k: l.get(k) for k in ("league_id", "name", "season", "status")} for l in c.get(f"user/{u['user_id']}/leagues/nfl/{args.season}", 300)]}
    if args.command == "configure":
        return configure(args, store)
    if args.command in ("sync", "auto-sync"):
        if args.command == "auto-sync":
            marker = store.root / f"{args.league}.backfill"
            args.full = not marker.exists() or time.time() - marker.stat().st_mtime > 86400
        result = run_sync(args, store)
        if args.command == "auto-sync":
            ensure_players(store)
            if args.full:
                marker.touch()
        return short_receipt(result)
    if args.command == "context":
        if args.fresh:
            run_sync(args, store)
        return context(store, args.league, league_config(store.root, args.league))
    if args.command == "view":
        snap = store.latest(args.league)
        if not snap:
            raise DataError("No snapshot; run sync")
        data = snap["data"]
        if args.section == "settings":
            data = {k: data["league"][k] for k in ("settings", "scoring_settings", "roster_positions")}
        elif args.section != "all":
            data = data[args.section]
        if args.week is not None:
            if args.section not in ("matchups", "transactions"):
                raise DataError("--week applies to matchups or transactions")
            if str(args.week) not in data:
                raise DataError("Week not cached; run sync --full")
            data = data[str(args.week)]
        if args.section == "rosters" and args.mine:
            data = [r for r in data if r["roster_id"] == snap["data"]["my_roster_id"]]
        return {"snapshot_created": snap["created"], "last_sync": store.last_run(args.league)["checked_at"], "data": data}
    if args.command == "players":
        source = ensure_players(store, args.offline)
        where, params = ["(name LIKE ? OR id=?)"], [f"%{args.query}%", args.query]
        if args.position:
            where.append("position=?")
            params.append(args.position.upper())
        owned = set()
        if args.available:
            snap = store.latest(args.league)
            if not snap:
                raise DataError("Run sync before filtering availability")
            for r in snap["data"]["rosters"]:
                owned.update(r.get("players") or [])
            for d in snap["data"]["drafts"]:
                owned.update(p["player_id"] for p in d["picks"])
        with store.connect() as db:
            rows = db.execute("SELECT id,name,position,team,body FROM players WHERE " + " AND ".join(where) + " ORDER BY name", params).fetchall()
        found = []
        for pid, name, pos, team, body in rows:
            p = json.loads(body)
            if pid in owned or (args.available and not p.get("active")):
                continue
            found.append({"id": pid, "name": name, "position": pos, "team": team, "age": p.get("age"), "injury_status": p.get("injury_status"), "status": p.get("status")})
            if len(found) >= args.limit:
                break
        return {"players": found, "source": source, "availability_snapshot_at": store.last_run(args.league)["checked_at"] if args.available else None}
    if args.command == "receipt":
        return store.last_run(args.league)
    if args.command == "verify":
        snap = store.latest(args.league)
        if not snap or digest(snap["data"]) != snap["hash"]:
            raise DataError("Snapshot missing or hash mismatch")
        run = store.last_run(args.league)
        if not run or run["snapshot_hash"] != snap["hash"]:
            raise DataError("Receipt does not match latest snapshot")
        return {"ok": True, "snapshot_hash": snap["hash"], "verification": "local content integrity only; not proof of current remote state", "last_sync": run["checked_at"]}
    if args.command == "changes":
        with store.connect() as db:
            rows = db.execute("SELECT id,created,body FROM runs WHERE alias=? ORDER BY id DESC LIMIT ?", (args.league,args.limit)).fetchall()
        return [{"run_id": row[0], "created": row[1], **short_receipt(json.loads(row[2]))} for row in rows]
    if args.command == "schedule":
        return schedule(args, store)
    if args.command == "watch":
        args.fresh = True
        config = league_config(store.root, args.league)
        previous = None
        for _ in range(args.count) if args.count else iter(int, 1):
            client = Client(store, force=True)
            drafts = client.get(f"league/{config['league_id']}/drafts")
            paths = {f"draft/{d['draft_id']}{suffix}": 0 for d in drafts for suffix in ("", "/picks", "/traded_picks") if d.get("status") != "complete"}
            if not paths:
                result = run_sync(args, store)
                emit({"event": "no_active_draft", **short_receipt(result)})
                break
            data = client.many(paths)
            sha = digest(data)
            if sha != previous:
                result = run_sync(args, store)
                emit({"event": "draft_changed", "checked_at": time.time(), "draft_hash": sha, **short_receipt(result)})
                previous = sha
            else:
                emit({"event": "unchanged", "checked_at": time.time()})
            time.sleep(args.interval)
        return None
    raise DataError("Unknown command")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        from art_of_the_deal.cli import main as agent_main
        return agent_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("MONEYBALL_DATA_DIR", DEFAULT_ROOT)))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser('lab',help='Versioned research, projections and conditional championship experiments')
    p.add_argument('action',choices=('init','status','sources','alerts','query','ingest','advanced','opportunities','features','incremental','depth','providers','build','mispricing','draft','live','move','refresh','schedule'))
    p.add_argument('--league',default=None)
    p.add_argument('--season',type=int,default=2026)
    p.add_argument('--seasons',type=int,nargs='+')
    p.add_argument('--datasets',nargs='+')
    p.add_argument('--dataset')
    p.add_argument('--source')
    p.add_argument('--positions',nargs='+')
    p.add_argument('--weeks',type=int,nargs='+')
    p.add_argument('--cutoff')
    p.add_argument('--limit',type=positive,default=10)
    p.add_argument('--fresh',action='store_true')
    p.add_argument('--offline',action='store_true')
    p.add_argument('--input',type=Path)
    p.add_argument('--state',type=Path)
    p.add_argument('--draws',type=positive,default=100)
    p.add_argument('--draft-draws',type=positive,default=12)
    p.add_argument('--years',type=positive,default=1)
    p.add_argument('--seed',type=int,default=1)
    p.add_argument('--noise',type=float,default=8)
    p.add_argument('--rivals',choices=('adp','lineup','mixed'),default='adp')
    p.add_argument('--distribution',choices=('residual','entropy'),default='residual')
    p.add_argument('--horizon',choices=('frozen','transition'),default='frozen')
    p.add_argument('--missing-forecast',choices=('reject','zero','carry'),default='zero')
    p.add_argument('--candidates',nargs='+')
    p.add_argument('--watch-live',action='store_true',help='Poll picks independently and serve a read-only localhost draft board')
    p.add_argument('--port',type=positive,default=8765)
    p.add_argument('--budget-seconds',type=positive,default=40)
    p.add_argument('--max-blocks',type=positive,default=64)
    p.add_argument('--candidate-limit',type=positive,default=6)
    p.add_argument('--live-inner-draws',type=positive,default=8)
    p.add_argument('--schedule-action',choices=('install','status','remove'),default='status')
    p.add_argument('--interval',type=positive,default=3600)
    p.set_defaults(full=False,week=None)
    for name in ("discover", "configure"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--season", default="2026")
        if name == "configure":
            p.add_argument("--alias", default=None)
            p.add_argument("--name", default=None)
            p.add_argument("--league-id")
    for name in ("sync", "auto-sync", "context", "view", "players", "receipt", "verify", "changes", "watch", "schedule", "team-state", "team-check"):
        p = sub.add_parser(name)
        p.add_argument("--league", default=None)
        p.set_defaults(fresh=False, full=False, week=None)
        if name == "team-check":
            p.add_argument("expected", type=Path)
        if name in ("sync", "context", "auto-sync", "watch"):
            p.add_argument("--fresh", action="store_true")
            p.add_argument("--full", action="store_true")
            p.add_argument("--week", type=int, choices=range(1,19))
        if name == "view":
            p.add_argument("section", choices=("all", "settings", "league", "rosters", "users", "drafts", "matchups", "transactions", "traded_picks", "winners_bracket", "losers_bracket", "state"))
            p.add_argument("--week", type=int, choices=range(1,19))
            p.add_argument("--mine", action="store_true")
        if name == "players":
            p.add_argument("query", nargs="?", default="")
            p.add_argument("--position")
            p.add_argument("--available", action="store_true")
            p.add_argument("--offline", action="store_true")
            p.add_argument("--limit", type=positive, default=20)
        if name == "changes":
            p.add_argument("--limit", type=positive, default=5)
        if name == "watch":
            p.add_argument("--interval", type=positive, default=5)
            p.add_argument("--count", type=positive, default=0, help="Stop after N polls; default runs until interrupted")
        if name == "schedule":
            p.add_argument("action", choices=("install", "status", "remove"))
            p.add_argument("--interval", type=positive, default=300)
    args = parser.parse_args()
    try:
        if args.command == "watch" and args.interval < 5:
            raise DataError("Draft polling minimum is five seconds")
        if args.command == "schedule" and args.interval < 60:
            raise DataError("Scheduled sync minimum is 60 seconds; use watch during a draft")
        store = Store(args.data_dir)
        if (hasattr(args, "league") and args.command != "lab"
                and (args.command != "players" or args.available)):
            args.league = store.resolve_alias(args.league)
        result = dispatch(args, store)
        if result is not None:
            emit(result)
    except (DataError, OSError, ValueError, KeyError, sqlite3.Error, WarehouseError) as exc:
        print(encode({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
