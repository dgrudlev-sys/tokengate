"""Tier 0: deterministic local tools that cost zero model tokens."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config

MAX_OUT = 4000  # characters returned to Cursor


def _truncate(text: str, limit: int = MAX_OUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


def search(cfg: Config, pattern: str, glob: str | None = None, max_hits: int = 60) -> str:
    """Regex search returning `file:line: text` rows, capped. Uses ripgrep when present."""
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "-S", "--max-columns", "200", "-m", "20"]
        for ig in cfg.ignore:
            cmd += ["-g", f"!{ig}"]
        if glob:
            cmd += ["-g", glob]
        cmd += ["-e", pattern, "."]
        out = subprocess.run(cmd, cwd=cfg.root, capture_output=True, text=True, timeout=30).stdout
        rows = out.splitlines()
    else:
        from .graph import Graph
        rx = re.compile(pattern, re.I)
        rows = []
        for p in Graph(cfg)._walk():
            if glob and not p.match(glob):
                continue
            for i, line in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    rows.append(f"{p.relative_to(cfg.root).as_posix()}:{i}:{line.strip()[:200]}")
    extra = len(rows) - max_hits
    return "\n".join(rows[:max_hits]) + (f"\n... {extra} more hits" if extra > 0 else "") or "no matches"


def _node_bin(root: Path, name: str) -> list[str] | None:
    if os.name == "nt":
        name += ".cmd"
    for d in [root, *root.parents]:
        b = d / "node_modules" / ".bin" / name
        if b.exists():
            return [str(b)]
    return None


def detect_checks(cfg: Config) -> dict[str, list[str]]:
    """Which checkers this project can run, without asking a model."""
    root = cfg.root
    checks: dict[str, list[str]] = {}
    if (root / "tsconfig.json").exists() and (tsc := _node_bin(root, "tsc")):
        checks["typecheck"] = tsc + ["--noEmit", "--pretty", "false"]
    if (eslint := _node_bin(root, "eslint")) and any(root.glob(".eslintrc*")) | any(root.glob("eslint.config.*")):
        checks["lint"] = eslint + [".", "--quiet", "-f", "unix"]
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or any(root.glob("*.py")):
        if shutil.which("ruff"):
            checks["lint_py"] = ["ruff", "check", "--output-format", "concise", "."]
        if shutil.which("pytest") and (any(root.glob("tests")) or any(root.glob("test_*.py"))):
            checks["test_py"] = ["pytest", "-q", "-x", "--no-header"]
    return checks


def run_checks(cfg: Config, only: list[str] | None = None, timeout: int = 300) -> dict:
    """Run available checks; return only the failing output, truncated."""
    results = {}
    for name, cmd in detect_checks(cfg).items():
        if only and name not in only:
            continue
        try:
            p = subprocess.run(cmd, cwd=cfg.root, capture_output=True, text=True, timeout=timeout)
            out = (p.stdout + p.stderr).strip()
            results[name] = {"ok": p.returncode == 0, "errors": count_errors(out) if p.returncode else 0,
                             "output": "" if p.returncode == 0 else _truncate(out)}
        except (subprocess.TimeoutExpired, OSError) as e:
            results[name] = {"ok": False, "errors": -1, "output": str(e)}
    return results or {"note": "no local checkers detected (install typescript / eslint / ruff / pytest)"}


def count_errors(output: str) -> int:
    return len(re.findall(r"error|Error|\bE\d{3}\b|FAILED", output)) or (1 if output else 0)
