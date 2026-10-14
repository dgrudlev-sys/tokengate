"""Cursor `beforeSubmitPrompt` hook: tokengate gets the first dip, Cursor's model the last.

Cursor runs this before your chat message reaches its model (stdin: JSON with `prompt`,
`attachments`, `workspace_roots`; stdout: {"continue": bool, "user_message": str}).

  * free model finishes the job  -> {"continue": false}: Cursor's model never runs (0 tokens).
                                    Answer in the popup + .tokengate/last_answer.md, edits on disk.
  * free models can't            -> {"continue": true}: Cursor runs, with .tokengate/HANDOFF.md
                                    holding the context already gathered.
  * anything goes wrong          -> {"continue": true} (fail open; never blocks your work).

Modes (tokengate.toml `prompt_gate`): "always" (every self-contained prompt), "auto" (clear
explain/review/tests/docs tasks and small named-file edits only), "off". Prompts that lean on
chat history ("yes do it", "fix the error above") always go to Cursor. Reserve/empty fuel level upgrades "auto" to "always".
Start a message with "!" to send it straight to Cursor.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

KINDS = [  # first match wins
    ("tests", r"\b(write|add|generate|create)\b.{0,30}\b(unit |integration )?tests?\b"),
    ("docs", r"\b(docstrings?|jsdoc|tsdoc|comment(s|ing)?|document(ation)?|readme)\b"),
    ("review", r"\b(review|audit|find (the )?bugs?|code smells?|check for (bugs|issues))\b"),
    ("summarize", r"\b(summari[sz]e|overview|tl;?dr|outline)\b"),
    ("explain", r"^\s*(explain|what (does|is|are)|how (does|do|is)|why (does|is)|walk me through|describe)\b"),
    ("edit", r"\b(rename|refactor|fix|add|change|update|implement|convert|replace|remove|extract|move|create)\b"),
]
# Prompts that lean on chat history tokengate can't see.
NEEDS_HISTORY = re.compile(r"^\s*(yes|no|ok|okay|do it|go|continue|try again|that|this|same)\b|\b(above|previous|you (just|said)|last (answer|message|change)|that error)\b", re.I)
FILE_TOKEN = re.compile(r"[\w@./\\-]+\.(?:tsx?|jsx?|mjs|cjs|py|sql|json|md)\b|\b[A-Z][A-Za-z0-9]+(?:Screen|Service|Store|Navigator|Detector|Orchestrator)\b")


def classify(prompt: str) -> str:
    low = prompt.lower()
    for kind, pattern in KINDS:
        if re.search(pattern, low):
            return kind
    return "general"


def mentioned_files(prompt: str, attachments, graph) -> list[str]:
    found: list[str] = []
    for a in attachments or []:
        path = a.get("file_path") or a.get("filePath") or a.get("path") if isinstance(a, dict) else a
        if isinstance(path, str) and (p := graph.resolve_file(path)) and p not in found:
            found.append(p)
    for tok in FILE_TOKEN.findall(prompt):
        if (p := graph.resolve_file(tok)) and p not in found:
            found.append(p)
    for word in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", prompt):  # symbol names -> defining file
        hits = graph.find_symbol(word)
        if hits and hits[0]["exact"] and hits[0]["exported"] and hits[0]["file"] not in found:
            found.append(hits[0]["file"])
    return found[:5]


IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg|heic|tiff?)\b", re.I)
IMAGE_WORDS = re.compile(r"\b(screenshot|screen ?shot|this (image|picture|pic|mockup|design)|the (image|picture|mockup)"
                         r"|attached (image|picture|screenshot)|looks like this|as shown)\b", re.I)


def image_reason(prompt: str, attachments) -> str | None:
    """Why this prompt depends on an image, or None. Free text models can't see images,
    so these always go to Cursor rather than getting a blind answer."""
    for a in attachments or []:
        if isinstance(a, dict):
            kind = str(a.get("type") or a.get("mime_type") or a.get("mimeType") or "").lower()
            path = str(a.get("file_path") or a.get("filePath") or a.get("path") or a.get("name") or "")
            if "image" in kind or IMAGE_EXT.search(path) or any(k in a for k in ("image", "image_url", "data_url")):
                return "image attached"
        elif isinstance(a, str) and IMAGE_EXT.search(a):
            return "image attached"
    if IMAGE_EXT.search(prompt) or IMAGE_WORDS.search(prompt) or re.search(r"\[Image #?\d+\]", prompt):
        return "prompt refers to an image"
    return None


def decide(prompt: str, mode: str, level: str, files: list[str]) -> tuple[bool, str, str]:
    """-> (try_tokengate, kind, reason)"""
    text = prompt.strip()
    if mode == "off":
        return False, "", "prompt gate off"
    if text.startswith("!"):
        return False, "", "user forced Cursor with '!'"
    kind = classify(text)
    if len(text.split()) < 4 or NEEDS_HISTORY.search(text):
        return False, kind, "depends on chat history"  # a fresh model can't see the chat
    if mode == "always" or level in ("reserve", "empty"):
        return True, kind, f"mode={mode} level={level}"
    if kind in ("explain", "summarize", "review", "docs", "tests"):
        return True, kind, "self-contained task"
    if kind == "edit" and 1 <= len(files) <= 2:
        return True, kind, "small edit in named files"
    return False, kind, "open-ended; leaving to Cursor"


def _open_in_editor(path: Path) -> None:
    exe = shutil.which("cursor")
    if exe:
        try:
            subprocess.Popen([exe, "-r", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def handle(payload: dict) -> dict:
    """Run the gate for one prompt. Accepts Cursor's or Claude Code's hook payload and
    answers in the same client's format."""
    claude = "hook_event_name" in payload or "transcript_path" in payload
    out = _run(payload, claude)
    if not claude:
        r = {"continue": out["continue"]}
        if out.get("message"):
            r["user_message"] = out["message"]
        return r
    if not out["continue"]:
        return {"decision": "block", "reason": out["message"]}
    if out.get("context"):
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": out["context"]}}
    return {}


def _run(payload: dict, claude: bool) -> dict:
    from . import config, gauge
    from .cache import Store
    from .graph import Graph
    from .router import Gate

    host = "Claude" if claude else "Cursor"
    prompt = payload.get("prompt") or ""
    roots = [payload["cwd"]] if claude and payload.get("cwd") else (payload.get("workspace_roots") or [])
    cfg = config.load(Path(roots[0]) if roots else None)
    store = Store(cfg.state_dir, cfg.cache_ttl_days)
    log = cfg.state_dir / "hook.log"
    passthrough = {"continue": True}

    level = "unknown"
    try:
        level = gauge.read(cfg, store).level
    except Exception:
        pass
    conv = payload.get("session_id") if claude else payload.get("conversation_id")
    if conv and cfg.long_chat_warn and not prompt.strip().startswith("!"):
        limit = cfg.long_chat_warn // 2 if level in ("reserve", "empty") else cfg.long_chat_warn
        turns, warned = store.chat_turn(conv)
        if turns >= limit and not warned:
            store.mark_warned(conv)
            fresh = ("Run /compact (keeps a summary) or /clear (fresh start) to save tokens."
                     if claude else "Start a new chat (Ctrl/Cmd+N) with a one-line summary to save tokens.")
            return {"continue": False, "message":
                    f"This chat has {turns} messages, and every new message re-sends all of them. {fresh} "
                    "To keep going here anyway, just send your message again."}

    why_image = image_reason(prompt, payload.get("attachments"))
    if why_image:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} go=False ({why_image}; text-only free models can't see it) "
                     f":: {prompt[:120]!r}\n")
        return passthrough

    graph = Graph(cfg).load()
    graph.build()
    files = mentioned_files(prompt, payload.get("attachments"), graph)
    go, kind, reason = decide(prompt, cfg.prompt_gate, level, files)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} [{host}] go={go} kind={kind} files={files} ({reason}) :: {prompt[:120]!r}\n")
    if not go:
        return passthrough

    gate = Gate(cfg, graph, store)
    res = gate.delegate(prompt, files or None, kind)
    if res.status == "done":
        answer = cfg.state_dir / "last_answer.md"
        answer.write_text(f"# {prompt.strip()}\n\n_{res.provider}/{res.model} via tokengate, 0 {host} tokens_\n\n"
                          + res.render(), "utf-8")
        if not claude:
            _open_in_editor(answer)
        head = f"✅ Answered by {res.provider}/{res.model} (0 {host} tokens)."
        if res.files_changed:
            head += " Edited: " + ", ".join(res.files_changed) + " — review with git diff."
        body = res.text if len(res.text) < 1500 else res.text[:1500] + "…"
        return {"continue": False, "message": f"{head}\nFull answer: .tokengate/last_answer.md\n\n{body}\n\n"
                                              f"(Start with '!' to send to {host} instead.)"}

    handoff = (f"# Handoff from tokengate\nprompt: {prompt.strip()}\nwritten: {time.time():.0f}\n\n"
               f"Free models could not finish ({'; '.join(res.attempts)[:500]}).\n\n"
               f"Relevant context (already gathered, don't re-explore):\n\n{res.context_pack}\n")
    (cfg.state_dir / "HANDOFF.md").write_text(handoff, "utf-8")
    if claude and files:  # Claude Code lets the hook hand this to the model; only when the prompt names code
        return {"continue": True, "context": "tokengate already gathered this context; start from it instead of "
                                             "exploring:\n" + res.context_pack}
    return passthrough


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = handle(payload)
    except Exception as e:  # fail open
        out = {}
        try:
            (Path.cwd() / ".tokengate" / "hook.log").open("a").write(f"error: {e!r}\n")
        except OSError:
            pass
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
