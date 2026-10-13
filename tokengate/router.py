"""The escalation ladder.

    0. local tools   graph lookups, search, type/lint checks       (free)
    1. cache         identical task + identical context             (free)
    2. local LLM     Ollama, if running                             (free)
    3. NVIDIA NIM    free developer credits, several models         (free quota)
    4. ESCALATE      hand Cursor a minimal context pack             (paid, last)

A model may answer "ESCALATE: <reason>" when it cannot do the job; edits are
only accepted when every SEARCH block applies and local checks don't regress.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import local_tools
from .cache import Store
from .config import Config
from .graph import Graph, estimate_tokens
from .providers import Client, ProviderError

SYSTEM = """You are a senior engineer working inside an existing codebase.
Be concise. Never invent APIs that are not shown in the context.
If you cannot complete the task correctly with the given context, reply with exactly one line:
ESCALATE: <short reason>"""

EDIT_RULES = """Return ONLY edit blocks in this exact format, one per change (the SEARCH text must match the file exactly, including indentation):

path/to/file.ext
<<<<<<< SEARCH
exact existing lines
=======
replacement lines
>>>>>>> REPLACE

To create a new file, use an empty SEARCH section. No explanations outside the blocks."""

KIND_HINTS = {
    "explain": "Explain clearly and briefly. Reference files and line numbers.",
    "review": "Review for bugs, edge cases and risky changes. List findings as `file:line - issue - fix`. Say 'no issues' if none.",
    "tests": "Write tests matching the project's existing test style. " + EDIT_RULES,
    "edit": EDIT_RULES,
    "docs": "Write documentation / docstrings / comments. " + EDIT_RULES,
    "summarize": "Summarize purpose, public API, dependencies and gotchas in <= 12 bullet points.",
    "general": "",
}
EDIT_KINDS = {"edit", "tests", "docs"}

BLOCK = re.compile(
    r"^(?P<path>[^\n`<>=]+?)\s*\n(?:```[^\n]*\n)?<<<<<<< SEARCH\n(?P<search>[\s\S]*?)\n?=======\n(?P<replace>[\s\S]*?)\n?>>>>>>> REPLACE",
    re.M,
)


@dataclass
class Result:
    status: str                    # "done" | "escalate"
    tier: str
    text: str = ""
    provider: str = ""
    model: str = ""
    files_changed: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    context_pack: str = ""

    def render(self) -> str:
        head = f"[tokengate] status={self.status} tier={self.tier}" + (f" via {self.provider}/{self.model}" if self.model else "")
        parts = [head]
        if self.files_changed:
            parts.append("files changed (already written to disk, review with git diff): " + ", ".join(self.files_changed))
        if self.text:
            parts.append(self.text)
        if self.attempts:
            parts.append("attempts: " + " | ".join(self.attempts))
        if self.context_pack:
            parts.append("Cheap models could not finish this. Minimal context for you:\n" + self.context_pack)
        return "\n\n".join(parts)


class Gate:
    def __init__(self, cfg: Config, graph: Graph | None = None, store: Store | None = None):
        self.cfg = cfg
        self.graph = graph or Graph(cfg).load()
        self.store = store or Store(cfg.state_dir, cfg.cache_ttl_days)

    # ------------------------------------------------------------ tier 0
    def context_pack(self, task: str, files: list[str] | None = None, budget: int | None = None) -> str:
        """Smallest useful context for a task: outlines first, full text only for top files."""
        budget = budget or self.cfg.pack_budget_tokens
        chosen = [p for f in (files or []) if (p := self.graph.resolve_file(f))]
        if not chosen:
            chosen = [p for p, _ in self.graph.rank(task, limit=6)]
        out, used = [], 0
        for p in chosen:
            o = self.graph.outline(p)
            if used + estimate_tokens(o) > budget:
                break
            out.append(o)
            used += estimate_tokens(o)
        if chosen:
            nb = self.graph.neighbors(chosen[0])
            line = f"\nrelated: imports={nb['imports'][:6]} imported_by={nb['imported_by'][:6]}"
            if used + estimate_tokens(line) <= budget:
                out.append(line)
        return "\n".join(out) or "no relevant files found; try graph_symbol or search"

    def _full_context(self, task: str, files: list[str] | None) -> tuple[str, list[str], int]:
        """Context sent to free models: full text of target files + outlines of neighbours."""
        limit = self.cfg.max_context_tokens
        targets = [p for f in (files or []) if (p := self.graph.resolve_file(f))]
        if not targets:
            targets = [p for p, _ in self.graph.rank(task, limit=3)]
        parts, used, raw_tokens = [], 0, 0
        for p in targets:
            text = (self.cfg.root / p).read_text("utf-8", errors="replace")
            raw_tokens += estimate_tokens(text)
            if used + estimate_tokens(text) > limit:
                text = self.graph.outline(p)
            parts.append(f"### {p}\n```\n{text}\n```")
            used += estimate_tokens(text)
        for p in {n for t in targets for n in self.graph.files[t].imports} - set(targets):
            o = self.graph.outline(p)
            if used + estimate_tokens(o) > limit:
                break
            parts.append(o)
            used += estimate_tokens(o)
            raw_tokens += self.graph.files[p].tokens
        return "\n\n".join(parts), targets, raw_tokens

    # ------------------------------------------------------- tiers 1-4
    def delegate(self, task: str, files: list[str] | None = None, kind: str = "general",
                 apply: bool = True, verify: bool = True, max_tokens: int = 2048) -> Result:
        kind = kind if kind in KIND_HINTS else "general"
        context, targets, cursor_would_read = self._full_context(task, files)
        prompt = f"Task ({kind}): {task}\n\n{KIND_HINTS[kind]}\n\nContext:\n{context}"
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        key = Store.key(kind, task, hashlib.sha1(context.encode()).hexdigest())

        cached = self.store.get(key)
        if cached:
            res = self._accept(cached["text"], kind, apply, verify, "cache", cached["provider"], cached["model"])
            if res:
                self.store.log("cache", cached["provider"], cached["model"], avoided=cursor_would_read, task=task)
                return res

        attempts = []
        for prov in self.cfg.providers:
            client = Client(prov)
            if not client.usable:
                attempts.append(f"{prov.name}: unavailable")
                continue
            for model in client.models():
                try:
                    comp = client.chat(model, messages, max_tokens=max_tokens)
                except ProviderError as e:
                    attempts.append(f"{prov.name}/{model}: {str(e)[:80]}")
                    if not client.usable:
                        break
                    continue
                text = comp.text.strip()
                if not text or text.upper().startswith("ESCALATE"):
                    attempts.append(f"{prov.name}/{model}: {text[:80] or 'empty'}")
                    continue
                tier = "local_llm" if prov.name == "ollama" else prov.name
                res = self._accept(text, kind, apply, verify, tier, prov.name, model)
                if res is None:
                    attempts.append(f"{prov.name}/{model}: rejected (edit did not apply or broke checks)")
                    continue
                self.store.put(key, prov.name, model, text)
                self.store.log(tier, prov.name, model, comp.tokens_in, comp.tokens_out,
                               avoided=cursor_would_read + comp.tokens_out, task=task)
                return res

        self.store.log("escalate", task=task)
        return Result("escalate", "cursor", attempts=attempts,
                      context_pack=self.context_pack(task, targets or files))

    # ------------------------------------------------------- validation
    def _accept(self, text: str, kind: str, apply: bool, verify: bool,
                tier: str, provider: str, model: str) -> Result | None:
        if kind not in EDIT_KINDS:
            return Result("done", tier, text=text, provider=provider, model=model)
        edits = parse_edits(text)
        if not edits:
            return None
        if not apply:
            return Result("done", tier, text=text, provider=provider, model=model)
        before = self._error_count() if verify else 0
        backup = apply_edits(self.cfg.root, edits)
        if backup is None:
            return None
        if verify and self._error_count() > before:
            restore(self.cfg.root, backup)
            return None
        changed = sorted(backup)
        self.graph.build()
        return Result("done", tier, text=f"{len(edits)} edit block(s) applied.", provider=provider,
                      model=model, files_changed=changed)

    def _error_count(self) -> int:
        res = local_tools.run_checks(self.cfg, only=["typecheck", "lint_py"])
        return sum(max(0, r.get("errors", 0)) for r in res.values() if isinstance(r, dict))


# ------------------------------------------------------------- edit helpers
def parse_edits(text: str) -> list[tuple[str, str, str]]:
    return [(m["path"].strip().strip("`*# "), m["search"], m["replace"]) for m in BLOCK.finditer(text)]


def apply_edits(root: Path, edits: list[tuple[str, str, str]]) -> dict[str, str | None] | None:
    """Apply all edits or none. Returns {path: original_text_or_None} for rollback."""
    root = root.resolve()
    staged: dict[str, str] = {}
    backup: dict[str, str | None] = {}
    for rel, search, replace in edits:
        path = (root / rel).resolve()
        if root not in path.parents:
            return None  # refuse writes outside the project
        key = path.relative_to(root).as_posix()
        if key not in staged:
            exists = path.exists()
            staged[key] = path.read_text("utf-8") if exists else ""
            backup[key] = staged[key] if exists else None
        cur = staged[key]
        if not search.strip():
            staged[key] = cur + ("\n" if cur and not cur.endswith("\n") else "") + replace + "\n"
        elif cur.count(search) == 1:
            staged[key] = cur.replace(search, replace, 1)
        else:
            return None
    for key, text in staged.items():
        p = root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")
    return backup


def restore(root: Path, backup: dict[str, str | None]) -> None:
    for key, text in backup.items():
        p = root / key
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(text, "utf-8")
