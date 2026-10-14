"""Read the user's Cursor plan usage.

Cursor has no public usage API for individual plans. Community trackers (MIT-licensed
Tendo33/cursor-usage-tracker, Dwtexe/cursor-stats) read Cursor's own login token from its
local `state.vscdb` and call the same endpoint cursor.com/dashboard uses. We do the same, read-only.
These endpoints are unofficial and can change; every field is parsed defensively and the gauge
falls back to a manual reading (`tokengate gauge --set 62`).

Order of sources:
  1. CURSOR_SESSION_TOKEN env var (the `WorkosCursorSessionToken` cookie value from your browser)
  2. Cursor's local state.vscdb (`cursorAuth/accessToken`)
  3. manual reading stored by `tokengate gauge --set PERCENT`
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://cursor.com/api/usage-summary"


@dataclass
class CursorUsage:
    percent_used: float | None        # 0-100 of included plan usage
    used: float | None = None         # in `unit`
    limit: float | None = None
    unit: str = ""                    # "USD" or "requests"
    cycle_start: datetime | None = None
    cycle_end: datetime | None = None
    plan: str = ""
    source: str = ""                  # "api" | "manual" | "unavailable"
    error: str = ""


def _db_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "")) / "Cursor"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Cursor"
    return Path(os.environ.get("CURSOR_STATE_DB", base / "User" / "globalStorage" / "state.vscdb"))


def _read_access_token() -> str | None:
    path = _db_path()
    if not path.exists():
        return None
    try:  # read-only, never lock Cursor's DB
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        row = con.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()
        con.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def _user_id_from_jwt(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
    except (IndexError, ValueError):
        return None
    return sub.split("|")[-1] or None  # "auth0|user_abc" -> "user_abc"


def session_cookie() -> str | None:
    env = os.environ.get("CURSOR_SESSION_TOKEN")
    if env:
        return env
    token = _read_access_token()
    if not token:
        return None
    uid = _user_id_from_jwt(token)
    return f"{uid}%3A%3A{token}" if uid else None


def _dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def parse_summary(data: dict) -> CursorUsage:
    """Pull the best available numbers out of a usage-summary payload."""
    u = CursorUsage(percent_used=None, source="api", plan=str(data.get("membershipType", "")),
                    cycle_start=_dt(data.get("billingCycleStart")), cycle_end=_dt(data.get("billingCycleEnd")))
    if data.get("isUnlimited"):
        u.percent_used = 0.0
        u.plan += " (unlimited)"
        return u
    indiv = data.get("individualUsage") or {}
    for bucket_name in ("plan", "overall"):
        b = indiv.get(bucket_name) or {}
        if not b.get("enabled", True):
            continue
        used, limit, remaining = b.get("used"), b.get("limit"), b.get("remaining")
        if limit is None and used is not None and remaining is not None:
            limit = used + remaining
        if b.get("totalPercentUsed") is not None:
            u.percent_used = float(b["totalPercentUsed"])
        elif used is not None and limit:
            u.percent_used = 100.0 * used / limit
        if u.percent_used is not None:
            if used is not None and limit:
                u.used, u.limit, u.unit = used / 100, limit / 100, "USD"  # API reports cents
            return u
    u.error = "usage-summary had no individual plan numbers (team/enterprise seat?)"
    return u


def fetch(timeout: int = 10) -> CursorUsage:
    cookie = session_cookie()
    if not cookie:
        return CursorUsage(None, source="unavailable",
                           error="no Cursor login found (set CURSOR_SESSION_TOKEN or use --set)")
    req = urllib.request.Request(USAGE_URL, headers={
        "Cookie": f"WorkosCursorSessionToken={cookie}",
        "Accept": "application/json",
        "User-Agent": "tokengate-gauge",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_summary(json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        return CursorUsage(None, source="unavailable", error=f"cursor.com returned {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return CursorUsage(None, source="unavailable", error=f"cursor.com unreachable: {e}")
