"""The `code` operations, shared by the MCP server and the CLI.

graphify answers symbol-level questions (calls, callers, impact, paths); the built-in graph
gives compact file outlines, the repo map and context packs, and is the fallback.
"""

from __future__ import annotations

from . import gfy, local_tools
from .cache import Store
from .graph import Graph, estimate_tokens
from .router import Gate


class Explorer:
    def __init__(self, gate: Gate):
        self.gate, self.cfg, self.graph = gate, gate.cfg, gate.graph
        self.store: Store = gate.store
        self.ops = {
            "map": lambda q, q2: self.graph.overview(),
            "file": lambda q, q2: self._file(q),
            "find": lambda q, q2: self._find(q),
            "explain": lambda q, q2: gfy.explain(self.cfg, q) or self._find(q),
            "impact": lambda q, q2: self._impact(q),
            "path": lambda q, q2: gfy.path(self.cfg, q, q2) or "path needs graphify (pip install graphifyy)",
            "hubs": lambda q, q2: gfy.hubs(self.cfg) or "hubs needs graphify (pip install graphifyy)",
            "pack": lambda q, q2: self.gate.context_pack(q, [f.strip() for f in q2.split(",") if f.strip()] or None),
            "search": lambda q, q2: local_tools.search(self.cfg, q, q2 or None),
        }

    def run(self, op: str, q: str = "", q2: str = "") -> str:
        fn = self.ops.get(op)
        return fn(q, q2) if fn else f"unknown op {op!r}; use one of {', '.join(self.ops)}"

    def _file(self, q: str) -> str:
        g: Graph = self.graph
        p = g.resolve_file(q)
        if not p:
            return f"no file matching {q!r}"
        nb = g.neighbors(p)
        text = g.outline(p) + f"\nimported_by: {', '.join(nb['imported_by']) or '-'}"
        self.store.log("graph", avoided=max(0, g.files[p].tokens - estimate_tokens(text)), task=f"file {p}")
        return text

    def _find(self, q: str) -> str:
        hits = self.graph.find_symbol(q)[:20]
        return "\n".join(f"{h['file']}:{h['line']} [{h['kind']}] {h['signature']}" for h in hits) or "not found"

    def _impact(self, q: str) -> str:
        out = gfy.affected(self.cfg, q)
        if out is not None and "No node" not in out[:80]:
            return out
        p = self.graph.resolve_file(q)
        return f"dependents of {p}:\n" + "\n".join(self.graph.impact(p)) if p else (out or f"no match for {q!r}")
