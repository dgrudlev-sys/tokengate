"""graphify (PyPI `graphifyy`, MIT) as the graph engine.

graphify parses 20+ languages with tree-sitter and records call edges, so it answers
"who calls X / what does X call / what breaks if X changes" at symbol level, which the
built-in import graph can't. We drive its public CLI (stable) rather than private modules.
Output lives in .tokengate/graphify-out/graph.json. If graphify isn't installed every
function returns None and callers fall back to tokengate's own graph.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from .config import Config

TIMEOUT = 120


def available() -> bool:
    return importlib.util.find_spec("graphify") is not None


def graph_path(cfg: Config) -> Path:
    return cfg.state_dir / "graphify-out" / "graph.json"


def _run(cfg: Config, args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "graphify", *args], cwd=cfg.root,
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or p.stderr).strip()


def write_ignore(cfg: Config) -> None:
    """graphify honours .graphifyignore; mirror tokengate's ignore list into it."""
    path = cfg.root / ".graphifyignore"
    existing = path.read_text("utf-8").splitlines() if path.exists() else []
    wanted = [f"{d}/" for d in cfg.ignore]
    missing = [w for w in wanted if w not in existing]
    if missing:
        path.write_text("\n".join(existing + missing) + "\n", "utf-8")


def build(cfg: Config) -> str | None:
    """Incremental AST extraction (no LLM, no network). ~1-3 s on a small repo."""
    if not available():
        return None
    write_ignore(cfg)
    code, out = _run(cfg, ["extract", str(cfg.root), "--code-only", "--no-label",
                           "--out", str(cfg.state_dir)], timeout=TIMEOUT)
    summary = next((l for l in out.splitlines() if "nodes" in l and "edges" in l), out[-300:])
    return summary if code == 0 else f"graphify extract failed: {summary}"


def _query(cfg: Config, args: list[str]) -> str | None:
    if not (available() and graph_path(cfg).exists()):
        return None
    code, out = _run(cfg, [*args, "--graph", str(graph_path(cfg))])
    # exit 3 = ambiguous (candidates listed), 4 = no match: both are useful answers
    return _compact(out) if code in (0, 3, 4) else None


_NOISE = re.compile(r"^\s*(ID|Type|Community|Degree|Relations|Depth):")


def _compact(text: str) -> str:
    """Drop fields an agent never needs; every line here is paid for in Cursor tokens."""
    lines = [l for l in text.splitlines() if not _NOISE.match(l)]
    return re.sub(r" \[(EXTRACTED|INFERRED|AMBIGUOUS)\]", "", "\n".join(lines))


def explain(cfg: Config, symbol: str) -> str | None:
    """Node plus every caller / callee / import with file:line."""
    return _query(cfg, ["explain", symbol])


def affected(cfg: Config, symbol: str, depth: int = 2) -> str | None:
    """Reverse traversal: what breaks if `symbol` (or file) changes."""
    return _query(cfg, ["affected", symbol, "--depth", str(depth)])


def path(cfg: Config, a: str, b: str) -> str | None:
    return _query(cfg, ["path", a, b])


def hubs(cfg: Config, top: int = 10) -> str | None:
    return _query(cfg, ["god-nodes", "--top", str(top)])


def query(cfg: Config, question: str, budget: int = 800) -> str | None:
    return _query(cfg, ["query", question, "--budget", str(budget)])
