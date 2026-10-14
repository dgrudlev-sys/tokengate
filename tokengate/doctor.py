"""`tokengate doctor`: one screen, every check, and the fix for each failure."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from . import gauge, gfy, keys, setup_wizard
from .cache import Store
from .config import Config

OK, WARN, BAD = "✓", "!", "✗"


def checks(cfg: Config, live: bool = True) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    add = lambda s, what, fix="": out.append((s, what, fix))

    add(OK if sys.version_info >= (3, 10) else BAD, f"Python {sys.version.split()[0]}", "needs 3.10+")
    add(OK if importlib.util.find_spec("mcp") else BAD, "MCP package", 'pip install "mcp>=1.2"')
    if gfy.available():
        add(OK if gfy.graph_path(cfg).exists() else WARN, "graphify call graph",
            "" if gfy.graph_path(cfg).exists() else "tokengate init (or: tokengate index)")
    else:
        add(WARN, "graphify not installed (using built-in graph)", "pip install graphifyy")
    add(OK if shutil.which("rg") else WARN, "ripgrep", "optional: faster search (winget install BurntSushi.ripgrep.MSVC)")

    root = cfg.root
    add(OK if (root / "tokengate.toml").exists() else WARN, f"project config in {root}", "tokengate init")
    mcp_json = root / ".cursor" / "mcp.json"
    try:
        server = json.loads(mcp_json.read_text("utf-8"))["mcpServers"]["tokengate"]
        cmd = server["command"]
        found = Path(cmd).exists() or shutil.which(cmd)
        add(OK if found else BAD, "Cursor MCP config", "" if found else f"'{cmd}' not found: re-run tokengate init on this machine")
    except (OSError, KeyError, ValueError):
        add(BAD, "Cursor MCP config (.cursor/mcp.json)", "tokengate init")
    hooks = root / ".cursor" / "hooks.json"
    add(OK if hooks.exists() and "tokengate.prompt_gate" in hooks.read_text("utf-8") else WARN,
        f"prompt gate hook (mode: {cfg.prompt_gate})", "tokengate init")
    add(OK if (root / ".cursor" / "rules" / "tokengate.mdc").exists() else WARN, "Cursor rule", "tokengate init")

    working = 0
    for label, status, detail in setup_wizard.status_rows(cfg, live=live):
        good = status in ("OK", "key saved")
        working += good
        mark = OK if good else (BAD if status == "KEY REJECTED" else "·")
        add(mark, f"{label}: {status}" + (f" ({detail})" if detail and good else ""),
            "" if good else detail)
    add(OK if working >= 2 else (WARN if working == 1 else BAD),
        f"{working} free provider(s) ready", "" if working >= 2 else "tokengate setup  (Groq + OpenRouter take ~4 min)")

    r = gauge.read(cfg, Store(cfg.state_dir))
    if r.percent_used is None:
        add(WARN, "Cursor usage: unknown", "log in to Cursor on this machine, or: tokengate gauge --set <percent>")
    else:
        add(OK if r.level == "ok" else WARN, f"Cursor usage: {r.message}")
    add(OK, f"key store: {keys.location()}")
    return out


def report(cfg: Config, live: bool = True) -> str:
    rows = checks(cfg, live)
    lines = [f" {s} {what}" + (f"\n     fix: {fix}" if fix and s != OK else "") for s, what, fix in rows]
    bad = sum(1 for s, *_ in rows if s == BAD)
    lines.append(f"\n{'All good.' if not bad else f'{bad} problem(s) to fix.'}")
    return "\n".join(lines)
