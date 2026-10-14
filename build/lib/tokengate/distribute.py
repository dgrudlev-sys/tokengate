"""Fan a task out over many files across all free models in parallel (map -> optional reduce).

Example: "summarize each file", "find bugs in each screen", "add JSDoc to every exported function".
Results land in .tokengate/out/ so Cursor reads a short index, not every answer.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .providers import Client, ProviderError
from .router import SYSTEM, Gate

MAX_FILES = 200


def _slots(cfg: Config, store=None) -> list[tuple[Client, str]]:
    """Every (client, model) pair we can use, interleaved so load spreads across providers."""
    per_provider = []
    for p in cfg.providers:
        c = Client(p)
        if p.rpd and store is not None and store.requests_today(p.name) >= p.rpd:
            continue
        if c.usable:
            per_provider.append([(c, m) for m in c.models()])
    return [s for group in itertools.zip_longest(*per_provider) for s in group if s]


def _ask(slots: list[tuple[Client, str]], msgs: list[dict], start: int = 0, max_tokens: int = 1024):
    """Try slots round-robin from `start`; return the first usable completion or None."""
    for k in range(len(slots)):
        client, model = slots[(start + k) % len(slots)]
        if not client.usable or model not in client.models():
            continue
        try:
            comp = client.chat(model, msgs, max_tokens=max_tokens)
        except ProviderError:
            continue
        if comp.text.strip() and not comp.text.strip().upper().startswith("ESCALATE"):
            return comp
    return None


def fan_out(gate: Gate, task: str, pattern: str, kind: str = "summarize", reduce: bool = True,
            max_tokens: int = 1024) -> dict:
    cfg = gate.cfg
    files = sorted(p for p in gate.graph.files if fnmatch.fnmatch(p, pattern))[:MAX_FILES]
    if not files:
        return {"status": "no files matched", "pattern": pattern}
    slots = _slots(cfg, gate.store)
    if not slots:
        return {"status": "escalate", "reason": "no free providers available (set NVIDIA_API_KEY or start Ollama)"}

    run_dir = cfg.state_dir / "out" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    workers = sum({c.p.name: c.p.max_parallel for c, _ in slots}.values())

    def job(i: int, path: str) -> tuple[str, str, str]:
        if kind in ("edit", "tests", "docs"):
            res = gate.delegate(f"{task}\n(Apply only to {path}.)", [path], kind=kind)
            return path, res.status, res.render()
        text = (cfg.root / path).read_text("utf-8", errors="replace")
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Task: {task}\n\n### {path}\n```\n{text[: cfg.max_context_tokens * 4]}\n```"}]
        comp = _ask(slots, msgs, start=i, max_tokens=max_tokens)
        if comp is None:
            return path, "escalate", "all free models failed"
        gate.store.log("distribute", comp.provider, comp.model, comp.tokens_in, comp.tokens_out,
                       avoided=gate.graph.files[path].tokens + comp.tokens_out, task=task)
        return path, "done", f"_{comp.provider}/{comp.model}_\n\n{comp.text.strip()}"

    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(job, i, f) for i, f in enumerate(files)]
        for fut in as_completed(futs):
            path, status, text = fut.result()
            results[path] = (status, text)

    report = [f"# {task}\n"]
    for path in files:
        status, text = results[path]
        report.append(f"## {path} [{status}]\n\n{text}\n")
    (run_dir / "results.md").write_text("\n".join(report), "utf-8")

    summary = ""
    if reduce and kind not in ("edit", "tests", "docs"):
        done = "\n\n".join(f"{p}: {t[:1500]}" for p, (s, t) in results.items() if s == "done")
        comp = _ask(slots, [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
                    f"Combine these per-file results for the task '{task}' into one short, prioritised "
                    f"summary (max 25 lines):\n\n{done[: cfg.max_context_tokens * 4]}"}], max_tokens=max_tokens)
        summary = comp.text.strip() if comp else ""
        (run_dir / "summary.md").write_text(summary, "utf-8")

    stats = {"files": len(files), "done": sum(1 for s, _ in results.values() if s == "done"),
             "escalated": [p for p, (s, _) in results.items() if s != "done"],
             "report": str((run_dir / "results.md").relative_to(cfg.root))}
    return {"status": "done", **stats, "summary": summary}


def to_text(result: dict) -> str:
    summary = result.pop("summary", "")
    return json.dumps(result, indent=2) + (f"\n\n{summary}" if summary else "")
