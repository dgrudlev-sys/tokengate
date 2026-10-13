"""Answer cache + savings ledger (SQLite, stdlib only)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, state_dir: Path, ttl_days: int = 30):
        self.db = sqlite3.connect(state_dir / "cache.sqlite", check_same_thread=False)
        self.ttl = ttl_days * 86400
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS answers(key TEXT PRIMARY KEY, created REAL, provider TEXT, model TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS ledger(ts REAL, tier TEXT, provider TEXT, model TEXT,
                                              tokens_in INT, tokens_out INT, cursor_tokens_avoided INT, task TEXT);
            """
        )

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT created, provider, model, body FROM answers WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[0] > self.ttl:
            return None
        return {"provider": row[1], "model": row[2], "text": row[3]}

    def put(self, key: str, provider: str, model: str, body: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?)", (key, time.time(), provider, model, body))
        self.db.commit()

    def log(self, tier: str, provider: str = "", model: str = "", tokens_in: int = 0,
            tokens_out: int = 0, avoided: int = 0, task: str = "") -> None:
        self.db.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?,?,?)",
                        (time.time(), tier, provider, model, tokens_in, tokens_out, avoided, task[:200]))
        self.db.commit()

    def requests_today(self, provider: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM ledger WHERE provider=? AND tier!='cache' AND ts>?",
                               (provider, time.time() - 86400)).fetchone()[0]

    def stats(self, days: int = 30) -> dict:
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT tier, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cursor_tokens_avoided) "
            "FROM ledger WHERE ts>? GROUP BY tier ORDER BY 2 DESC", (since,)).fetchall()
        tiers = {r[0]: {"calls": r[1], "tokens_in": r[2] or 0, "tokens_out": r[3] or 0, "cursor_tokens_avoided": r[4] or 0}
                 for r in rows}
        return {"days": days, "tiers": tiers,
                "total_cursor_tokens_avoided": sum(t["cursor_tokens_avoided"] for t in tiers.values()),
                "escalations_to_cursor": tiers.get("escalate", {}).get("calls", 0)}

    def dump(self) -> str:
        return json.dumps(self.stats(), indent=2)
