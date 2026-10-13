"""MCP server that Cursor talks to. Every tool here costs Cursor only the (short) result text."""

from __future__ import annotations

import json
import time

from . import config, gauge, local_tools
from .cache import Store
from .distribute import fan_out, to_text
from .graph import Graph, estimate_tokens
from .router import Gate

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """tokengate saves the user's paid Cursor tokens. Order of preference:
1. Read code via repo_map / graph_file / graph_symbol / context_pack / search instead of opening whole files.
2. Hand writing work (edits, tests, docs, reviews, explanations, summaries) to `delegate` first.
   Only do it yourself if delegate returns status=escalate.
3. For the same task over many files use `distribute`.
4. After any change, `run_checks` to verify locally instead of reasoning about it.
5. Call `fuel_gauge` at the start of a session. Lines starting with [gauge WARN/RESERVE/EMPTY]
   mean the user's Cursor quota is running low: delegate everything you can and keep replies minimal."""

cfg = config.load()
store = Store(cfg.state_dir, cfg.cache_ttl_days)
graph = Graph(cfg).load()
gate = Gate(cfg, graph, store)
_last_build = 0.0

mcp = _Server("tokengate", instructions=INSTRUCTIONS)


def _fresh() -> Graph:
    """Incremental re-index (only changed files are re-parsed), at most every 5s."""
    global _last_build
    if time.time() - _last_build > 5:
        graph.build()
        _last_build = time.time()
    return graph


def _g(text: str) -> str:
    """Prefix a one-line fuel warning when Cursor usage is getting tight."""
    return gauge.banner(cfg, store) + text


def _saved(tier: str, avoided: int, task: str) -> None:
    store.log(tier, avoided=avoided, task=task)


@mcp.tool()
def repo_map() -> str:
    """Compact map of the whole repo (files + exported symbols + token sizes). Call this before exploring."""
    return _g(_fresh().overview())


@mcp.tool()
def graph_file(path: str, depth: int = 1) -> str:
    """Outline of one file (signatures with line numbers) plus what it imports and what imports it.
    Use instead of reading the file; read only the line ranges you then need."""
    g = _fresh()
    p = g.resolve_file(path)
    if not p:
        return f"no file matching {path!r}"
    nb = g.neighbors(p, depth)
    text = g.outline(p) + f"\nimported_by: {', '.join(nb['imported_by']) or '-'}"
    _saved("graph", max(0, g.files[p].tokens - estimate_tokens(text)), f"graph_file {p}")
    return _g(text)


@mcp.tool()
def graph_symbol(name: str) -> str:
    """Where a function/class/type/const/table is defined, with its signature."""
    hits = _fresh().find_symbol(name)[:25]
    return "\n".join(f"{h['file']}:{h['line']} [{h['kind']}] {h['signature']}" for h in hits) or "not found"


@mcp.tool()
def graph_impact(path: str) -> str:
    """Every file that (transitively) depends on `path`: the blast radius of changing it."""
    g = _fresh()
    p = g.resolve_file(path)
    if not p:
        return f"no file matching {path!r}"
    deps = g.impact(p)
    return f"{len(deps)} dependents of {p}:\n" + "\n".join(deps)


@mcp.tool()
def context_pack(task: str, files: list[str] | None = None, budget_tokens: int = 0) -> str:
    """Smallest useful context for a task (outlines of the most relevant files)."""
    return _g(gate.context_pack(task, files, budget_tokens or None))


@mcp.tool()
def search(pattern: str, glob: str | None = None) -> str:
    """Regex search across the project (ripgrep), capped output."""
    return local_tools.search(cfg, pattern, glob)


@mcp.tool()
def run_checks(only: list[str] | None = None) -> str:
    """Run local typecheck / lint / tests and return only failures. Free, no model tokens."""
    return json.dumps(local_tools.run_checks(cfg, only), indent=2)


@mcp.tool()
def delegate(task: str, files: list[str] | None = None, kind: str = "general",
             apply: bool = True, verify: bool = True) -> str:
    """Send work to free models (local Ollama, then NVIDIA NIM) before using Cursor's own tokens.

    kind: edit | tests | docs | review | explain | summarize | general.
    For edit/tests/docs the change is written to disk (all-or-nothing) and rolled back
    if local checks get worse. status=escalate means: do it yourself, using the context pack returned.
    """
    _fresh()
    return _g(gate.delegate(task, files, kind, apply=apply, verify=verify).render())


@mcp.tool()
def distribute(task: str, pattern: str, kind: str = "summarize") -> str:
    """Run the same task over every file matching a glob (e.g. 'src/screens/**/*.tsx'),
    spread in parallel over all free models. Returns a short summary + path to the full report."""
    _fresh()
    return to_text(fan_out(gate, task, pattern, kind))


@mcp.tool()
def savings(days: int = 30) -> str:
    """Estimated Cursor tokens avoided, per tier, and how often work escalated to Cursor."""
    return json.dumps(store.stats(days), indent=2)


@mcp.tool()
def fuel_gauge() -> str:
    """How much of the user's paid Cursor usage is left this billing cycle, the burn-rate
    forecast, and free-provider activity. Level ok | warn | reserve | empty."""
    r = gauge.read(cfg, store)
    free = ", ".join(f"{p['name']} {p['requests_24h']}" + (f"/{p['rpd_limit']}" if p["rpd_limit"] else "")
                     for p in r.free["providers"] if p["available"])
    return (f"level={r.level} {r.message}\n"
            f"free providers, requests in 24h: {free or 'none configured'}\n"
            f"cursor tokens avoided (30d, est.): {r.savings['total_cursor_tokens_avoided']}")


def main() -> None:
    _fresh()
    mcp.run()


if __name__ == "__main__":
    main()
