"""Gas gauge: how much paid Cursor usage is left, how fast it's burning, and free-tier activity.

Levels drive "reserve mode": when Cursor usage is high, every MCP tool response carries a
one-line warning and the Cursor rule tells the agent to delegate everything it can.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import config, cursor_usage
from .cache import Store
from .config import Config

REFRESH_SECONDS = 300  # don't hit cursor.com more than every 5 minutes
_last_fetch = 0.0


@dataclass
class Reading:
    level: str                          # ok | warn | reserve | empty | unknown
    percent_used: float | None
    source: str
    plan: str = ""
    used: float | None = None
    limit: float | None = None
    unit: str = ""
    cycle_start: str | None = None
    cycle_end: str | None = None
    cycle_elapsed_pct: float | None = None
    projected_pct_at_cycle_end: float | None = None
    runs_out_on: str | None = None
    message: str = ""
    error: str = ""
    free: dict = field(default_factory=dict)
    savings: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    checked_at: str = ""


def _ensure_table(store: Store) -> None:
    store.db.execute("CREATE TABLE IF NOT EXISTS gauge(ts REAL, percent REAL, source TEXT, payload TEXT)")
    store.db.commit()


def set_manual(store: Store, percent: float, cycle_end: str | None = None) -> None:
    """Record a reading copied from cursor.com/dashboard."""
    _ensure_table(store)
    prev = _latest(store)
    extra = dict(prev[3]) if prev else {}  # keep plan / billing cycle from the last API reading
    if cycle_end:
        extra["cycle_end"] = datetime.fromisoformat(cycle_end).replace(tzinfo=timezone.utc).isoformat()
        extra.setdefault("cycle_start", (datetime.fromisoformat(extra["cycle_end"]) - timedelta(days=30)).isoformat())
    extra.update(used=None, limit=None)
    payload = json.dumps(extra)
    store.db.execute("INSERT INTO gauge VALUES(?,?,?,?)", (time.time(), percent, "manual", payload))
    store.db.commit()


def _latest(store: Store) -> tuple[float, float, str, dict] | None:
    _ensure_table(store)
    row = store.db.execute("SELECT ts, percent, source, payload FROM gauge ORDER BY ts DESC LIMIT 1").fetchone()
    return (row[0], row[1], row[2], json.loads(row[3] or "{}")) if row else None


def _history(store: Store, days: int = 31) -> list[list]:
    rows = store.db.execute("SELECT ts, percent FROM gauge WHERE ts>? ORDER BY ts",
                            (time.time() - days * 86400,)).fetchall()
    return [[round(ts), round(p, 1)] for ts, p in rows]


def _free_activity(store: Store, cfg: Config) -> dict:
    """Per free provider: limits vs what tokengate used (no provider exposes remaining quota)."""
    now = time.time()
    q = ("SELECT COUNT(*), COALESCE(SUM(tokens_in),0)+COALESCE(SUM(tokens_out),0) FROM ledger "
         "WHERE provider=? AND tier!='cache' AND ts>?")
    rows = []
    for p in cfg.providers:
        minute = store.db.execute(q, (p.name, now - 60)).fetchone()
        day = store.db.execute(q, (p.name, now - 86400)).fetchone()
        month = store.db.execute(q, (p.name, now - 30 * 86400)).fetchone()
        rows.append({"name": p.name, "available": p.available,
                     "may_train": p.may_train and not config._training_allowed(p.name),
                     "rpm_limit": p.rpm, "rpd_limit": p.rpd, "requests_last_minute": minute[0],
                     "requests_24h": day[0], "tokens_24h": day[1], "requests_30d": month[0], "tokens_30d": month[1]})
    return {"providers": rows,
            "requests_24h": sum(r["requests_24h"] for r in rows),
            "tokens_30d": sum(r["tokens_30d"] for r in rows)}


def read(cfg: Config, store: Store, refresh: bool = False) -> Reading:
    _ensure_table(store)
    last = _latest(store)
    global _last_fetch
    stale = (not last or time.time() - last[0] > REFRESH_SECONDS) and time.time() - _last_fetch > REFRESH_SECONDS
    usage = None
    if refresh or stale:
        _last_fetch = time.time()
        usage = cursor_usage.fetch()
        if usage.percent_used is not None:
            store.db.execute("INSERT INTO gauge VALUES(?,?,?,?)", (time.time(), usage.percent_used, "api", json.dumps({
                "plan": usage.plan, "used": usage.used, "limit": usage.limit, "unit": usage.unit,
                "cycle_start": usage.cycle_start.isoformat() if usage.cycle_start else None,
                "cycle_end": usage.cycle_end.isoformat() if usage.cycle_end else None})))
            store.db.commit()
            last = _latest(store)

    r = Reading(level="unknown", percent_used=None, source="unavailable",
                error=usage.error if usage and usage.percent_used is None else "")
    if last:
        ts, pct, source, extra = last
        r.percent_used, r.source = round(pct, 1), source
        r.plan, r.used, r.limit, r.unit = extra.get("plan", ""), extra.get("used"), extra.get("limit"), extra.get("unit", "")
        r.cycle_start, r.cycle_end = extra.get("cycle_start"), extra.get("cycle_end")
        r.checked_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="minutes")
        _project(r)
        r.level = _level(cfg, r)
    r.message = _message(r)
    r.free = _free_activity(store, cfg)
    r.savings = store.stats(30)
    r.history = _history(store)
    return r


def _project(r: Reading) -> None:
    """Linear burn-rate forecast over the billing cycle."""
    if not (r.cycle_end and r.percent_used is not None):
        return
    end = datetime.fromisoformat(r.cycle_end)
    start = datetime.fromisoformat(r.cycle_start) if r.cycle_start else end - timedelta(days=30)
    now = datetime.now(timezone.utc)
    total = (end - start).total_seconds()
    elapsed = min(max((now - start).total_seconds(), 1), total)
    r.cycle_elapsed_pct = round(100 * elapsed / total, 1)
    rate = r.percent_used / elapsed  # percent per second
    r.projected_pct_at_cycle_end = round(rate * total, 1)
    if rate > 0 and r.percent_used < 100:
        out = start + timedelta(seconds=100 / rate)
        if out < end:
            r.runs_out_on = out.date().isoformat()


def _level(cfg: Config, r: Reading) -> str:
    p = r.percent_used or 0
    if p >= 100:
        return "empty"
    if p >= cfg.gauge_reserve_pct or (r.runs_out_on and (r.projected_pct_at_cycle_end or 0) >= 130):
        return "reserve"
    if p >= cfg.gauge_warn_pct or (r.projected_pct_at_cycle_end or 0) > 100:
        return "warn"
    return "ok"


def _message(r: Reading) -> str:
    if r.percent_used is None:
        return "Cursor usage unknown. Run `tokengate gauge --set <percent>` from cursor.com/dashboard."
    base = f"Cursor {r.percent_used:.0f}% used"
    if r.cycle_elapsed_pct is not None:
        base += f" with {r.cycle_elapsed_pct:.0f}% of the cycle gone"
    if r.runs_out_on:
        base += f"; at this pace it runs out {r.runs_out_on}"
    return base + {
        "ok": ".",
        "warn": ". Prefer tokengate tools.",
        "reserve": ". RESERVE MODE: delegate everything possible, minimal replies.",
        "empty": ". Included usage exhausted: delegate everything, ask before any large edit.",
        "unknown": ".",
    }[r.level]


def banner(cfg: Config, store: Store) -> str:
    """Short line prepended to MCP tool output when usage is getting tight (empty otherwise)."""
    try:
        r = read(cfg, store)
    except Exception:  # the gauge must never break the tools
        return ""
    if r.level not in ("warn", "reserve", "empty"):
        return ""
    left = f"{max(0, 100 - (r.percent_used or 0)):.0f}% left" + (f", out ~{r.runs_out_on}" if r.runs_out_on else "")
    advice = "prefer tokengate" if r.level == "warn" else "delegate everything, minimal replies"
    return f"[gauge {r.level.upper()}: {left}; {advice}]\n"


def bar(r: Reading, width: int = 30) -> str:
    """Terminal fuel gauge: remaining fuel, E on the left."""
    if r.percent_used is None:
        return "E [" + "?" * width + "] F   " + r.message
    left = max(0.0, 100 - r.percent_used)
    filled = round(width * left / 100)
    color = {"ok": "\033[32m", "warn": "\033[33m", "reserve": "\033[31m", "empty": "\033[31m"}.get(r.level, "")
    return f"E [{color}{'█' * filled}\033[0m{'·' * (width - filled)}] F  {left:.0f}% left   {r.message}"


def to_json(r: Reading) -> str:
    return json.dumps(asdict(r), indent=2, default=str)
