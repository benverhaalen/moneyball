"""Use explicitly configured ESPN credentials only in memory for ESPN requests.

Firefox integration is opt-in. It reads only the two ESPN authentication cookie
names from a selected normal Firefox profile, without exporting the cookie DB.
"""
import configparser
import os
from pathlib import Path
import sqlite3
import time
from .store import DataError


def espn_headers(mode="environment", profile=None):
    if mode == "none":
        return {}
    if mode == "environment":
        values = {"SWID": os.environ.get("ESPN_SWID"), "espn_s2": os.environ.get("ESPN_S2")}
        if not all(values.values()):
            raise DataError("ESPN_SWID and ESPN_S2 are not configured; supply credentials outside the repository or use explicitly authorized Firefox access")
    elif mode == "firefox":
        root = Path.home() / "Library/Application Support/Firefox"
        if profile:
            paths = [Path(profile).expanduser()]
        else:
            ini = configparser.ConfigParser()
            ini.read(root / "profiles.ini")
            paths = []
            for section in ini.sections():
                if section.startswith("Profile") and ini.has_option(section, "Path"):
                    p = Path(ini.get(section, "Path"))
                    paths.append(root / p if ini.get(section, "IsRelative", fallback="1") == "1" else p)
        matches = []
        for path in paths:
            dbpath = path / "cookies.sqlite"
            if not dbpath.is_file():
                continue
            try:
                # Firefox may hold an exclusive DB lock. Read its durable main-file
                # snapshot without taking a lock; never copy the cookie database.
                # This can be older than the open browser. The ESPN request must
                # authenticate successfully; otherwise require a refreshed session.
                with sqlite3.connect(dbpath.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=3) as db:
                    rows = db.execute("SELECT name,value,expiry FROM moz_cookies WHERE host IN ('.espn.com','espn.com') AND name IN ('SWID','espn_s2') AND originAttributes='' ORDER BY expiry DESC").fetchall()
                found = {}
                for name, value, expiry in rows:
                    expiry = expiry / 1000 if expiry > 100_000_000_000 else expiry
                    if name not in found and (expiry == 0 or expiry > time.time()):
                        found[name] = value
                if len(found) == 2:
                    matches.append(found)
            except sqlite3.Error:
                continue
        if len(matches) != 1:
            raise DataError(f"Expected one authorized Firefox ESPN session; found {len(matches)}. Sign in or explicitly select its profile. No cookies were exported.")
        values = matches[0]
    else:
        raise DataError("Unsupported ESPN auth mode")
    if any(any(c in str(v) for c in "\r\n;") for v in values.values()):
        raise DataError("Invalid ESPN authentication value")
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in values.items())}
