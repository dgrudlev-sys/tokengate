"""MCP server that Cursor talks to.

Deliberately only 5 tools with short descriptions: every tool schema is resent to Cursor's model
on every message, so fewer/leaner tools = fewer tokens per request.
"""

from __future__ import annotations

import json
import threading
import time

from . import config, gauge, gfy, local_tools
from .cache import Store
from .distribute import fan_out, to_text
from .explore import Explorer
from .graph import Graph
from .router import Gate

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = ("Saves the user's paid Cursor quota. Explore with `code` instead of reading files; "
                "hand writing work to `delegate` first; verify with `check`.")

cfg = config.load()
store = Store(cfg.state_dir, cfg.cache_ttl_days)
graph = Graph(cfg).load(build_if_missing=False)  # built lazily, never blocks startup
PROJECT = config.is_project(cfg.root)
gate = Gate(cfg, graph, store)
explorer = Explorer(gate)
_last_build = 0.0
_gfy_lock = threading.Lock()

mcp = _Server("tokengate", instructions=INSTRUCTIONS)


def _rebuild_gfy() -> None:
    if _gfy_lock.acquire(blocking=False):
        try:
            gfy.build(cfg)
        finally:
            _gfy_lock.release()


def _fresh() -> Graph:
    """Incremental re-index at most every 5 s; graphify refreshes in the background on change."""
    global _last_build
    if not PROJECT:
        return graph
    if time.time() - _last_build > 5:
        changed = graph.build()["reparsed"]
        _last_build = time.time()
        if gfy.available():
            if not gfy.graph_path(cfg).exists():
                _rebuild_gfy()
            elif changed:
                threading.Thread(target=_rebuild_gfy, daemon=True).start()
    return graph


NO_PROJECT = ("tokengate: no project folder detected (cwd: {root}). Open a project folder in the editor, "
              "or set TOKENGATE_ROOT in the MCP config.")


def _g(text: str) -> str:
    """Prefix a one-line fuel warning when Cursor usage is getting tight."""
    return gauge.banner(cfg, store) + text


@mcp.tool()
def code(op: str, q: str = "", q2: str = "") -> str:
    """Explore code without reading whole files. op:
    map (repo overview) | file q=path (outline+line numbers) | find q=symbol (definition)
    | explain q=symbol (callers/callees with lines) | impact q=symbol|file (what breaks)
    | path q=A q2=B (call chain) | hubs | pack q=task q2=files,csv (minimal context)
    | search q=regex q2=glob. Then read only the line ranges you need."""
    if not PROJECT:
        return NO_PROJECT.format(root=cfg.root)
    _fresh()
    return _g(explorer.run(op, q, q2))


@mcp.tool()
def delegate(task: str, files: list[str] | None = None, kind: str = "general") -> str:
    """Free models do the work first. kind: edit|tests|docs|review|explain|summarize|general.
    Edits are written to disk all-or-nothing and rolled back if checks regress.
    status=escalate -> do it yourself with the context returned."""
    _fresh()
    return _g(gate.delegate(task, files, kind).render())


@mcp.tool()
def distribute(task: str, glob: str, kind: str = "summarize") -> str:
    """Same task over every file matching glob, in parallel on free models. Returns summary + report path."""
    _fresh()
    return to_text(fan_out(gate, task, glob, kind))


@mcp.tool()
def check() -> str:
    """Run local typecheck/lint/tests; returns failures only."""
    return json.dumps(local_tools.run_checks(cfg), indent=1)


@mcp.tool()
def gauge_status() -> str:
    """Cursor quota left, burn-rate forecast, free-provider use, tokens saved."""
    r = gauge.read(cfg, store)
    free = ", ".join(f"{p['name']} {p['requests_24h']}" + (f"/{p['rpd_limit']}" if p["rpd_limit"] else "")
                     for p in r.free["providers"] if p["available"])
    return (f"level={r.level} {r.message}\n"
            f"free providers, requests in 24h: {free or 'none configured'}\n"
            f"cursor tokens avoided (30d, est.): {r.savings['total_cursor_tokens_avoided']}, "
            f"escalations: {r.savings['escalations_to_cursor']}")


def main() -> None:
    if PROJECT:  # warm the index in the background; the server answers Cursor immediately
        threading.Thread(target=_fresh, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
