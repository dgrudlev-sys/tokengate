"""Caveman-style reply compression + free-model translation back to plain English.

Output tokens are the expensive ones and replies stay in chat history, so terser replies save
twice. Code, paths, commands and error text are never compressed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config

RULES = {
    "lite": "Reply style: concise. No filler, pleasantries, hedging or restating the question.",
    "medium": ("Reply style (caveman-medium): short plain sentences; no filler, pleasantries, hedging or "
               "restating the question; facts first; lists and → over prose. Keep code, paths, commands "
               "and error text exact."),
    "full": ("Reply style (caveman-full): telegraphic. Drop articles, connectives, filler. Fragments OK; "
             "→ for causation; facts only. Keep code, paths, commands and error text exact."),
}

TRANSLATE = ("Rewrite this terse developer note as clear, friendly plain English for the person who asked. "
             "Keep every fact, file name, number and code snippet exactly; add nothing new.\n\n")
SQUEEZE = ("Compress this text for an AI coding assistant. Keep every error message, file path, line number, "
           "identifier and number exactly; drop repetition, timestamps and noise; deduplicate repeated "
           "stack frames. Output only the compressed text.\n\n")


def rule(level: str) -> str:
    return RULES.get(level, "")


def _free(cfg: Config, prompt: str, max_tokens: int = 1500) -> str | None:
    from .distribute import _ask, _slots
    slots = _slots(cfg)
    comp = _ask(slots, [{"role": "user", "content": prompt}], max_tokens=max_tokens) if slots else None
    return comp.text.strip() if comp else None


def translate(cfg: Config, text: str) -> str:
    return _free(cfg, TRANSLATE + text) or text


def squeeze(cfg: Config, text: str) -> str:
    return _free(cfg, SQUEEZE + text[: cfg.max_context_tokens * 4]) or text


def last_assistant_text(transcript: Path) -> str:
    """Last assistant message text from a Claude Code transcript (.jsonl)."""
    last = ""
    try:
        for line in transcript.read_text("utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message") or {}
            if (ev.get("type") or msg.get("role")) != "assistant":
                continue
            content = msg.get("content")
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"] \
                if isinstance(content, list) else [content or ""]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                last = text
    except OSError:
        pass
    return last


def stop_hook(payload: dict) -> None:
    """Claude Code Stop hook: save a plain-English version of the last reply."""
    from . import config
    cfg = config.load(Path(payload["cwd"]) if payload.get("cwd") else None)
    if cfg.caveman in ("off", "lite"):
        return
    text = last_assistant_text(Path(payload.get("transcript_path", "")))
    if text:
        (cfg.state_dir / "last_reply.md").write_text(translate(cfg, text) + "\n", "utf-8")


def main() -> None:  # python -m tokengate.style  (Stop hook entry point; always exits 0)
    import sys
    try:
        stop_hook(json.loads(sys.stdin.read() or "{}"))
    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
