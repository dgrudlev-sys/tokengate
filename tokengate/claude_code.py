"""Wire tokengate into Claude Code (CLI, desktop, and claude.ai/code cloud sessions).

Same idea as the Cursor setup, Claude Code's way:
  .mcp.json              tokengate MCP server (code / delegate / distribute / check / gauge_status)
  .claude/settings.json  UserPromptSubmit hook (free models first; hand gathered context to Claude),
                         SessionStart hook (installs tokengate in fresh cloud containers),
                         read denials for token sinks (node_modules, lockfiles)
  CLAUDE.md              a ~100-token policy block (loaded every session, so kept tiny)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = "git+https://github.com/dgrudlev-sys/tokengate.git"
MARK_START, MARK_END = "<!-- tokengate:start -->", "<!-- tokengate:end -->"

POLICY = f"""{MARK_START}
## Token budget (tokengate)
- Explore with the `tokengate` MCP `code` tool (map / file / find / explain / impact / pack / search), then read only the line ranges you need. Never read whole large files, lockfiles or node_modules.
- Hand edits, tests, docs, reviews and explanations to `delegate` first; do it yourself only on status=escalate. Done edits are already on disk.
- Many files: `distribute`. Verify with `check`.
- If a prompt arrives with "tokengate already gathered this context", start from it.
- Keep replies short; small diffs, no restating unchanged code.
{MARK_END}
"""


def _commands() -> dict[str, str]:
    """Shell commands for the hooks / MCP server. On POSIX (incl. cloud containers) they
    self-install tokengate if missing; on native Windows they use this machine's Python."""
    if os.name == "nt":
        py = f'"{sys.executable}"'
        return {"install": f'{py} -c "import tokengate"',
                "mcp": [sys.executable, ["-m", "tokengate.mcp_server"]],
                "hook": f"{py} -m tokengate.prompt_gate",
                "stop": f"{py} -m tokengate.style"}
    install = (f'python3 -c "import tokengate" 2>/dev/null || flock /tmp/tokengate-install.lock '
               f'sh -c \'python3 -c "import tokengate" 2>/dev/null || '
               f'python3 -m pip install -q "tokengate[mcp] @ {REPO}" >/dev/null 2>&1\'')
    return {"install": install,
            "mcp": ["sh", ["-c", f"{install}; exec python3 -m tokengate.mcp_server"]],
            # never install inside the prompt hook (it would delay the prompt); fail open if missing
            "hook": 'python3 -c "import tokengate" 2>/dev/null && python3 -m tokengate.prompt_gate || echo "{}"',
            "stop": 'python3 -c "import tokengate" 2>/dev/null && python3 -m tokengate.style || echo "{}"'}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _without_tokengate(entries: list) -> list:
    return [e for e in entries if "tokengate" not in json.dumps(e)]


def install(root: Path, caveman: str = "medium") -> list[str]:
    cmds, written = _commands(), []

    mcp_path = root / ".mcp.json"
    mcp = _load(mcp_path)
    command, args = cmds["mcp"]
    mcp.setdefault("mcpServers", {})["tokengate"] = {"command": command, "args": args}
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", "utf-8")
    written.append(str(mcp_path))

    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(exist_ok=True)
    s = _load(settings_path)
    hooks = s.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = _without_tokengate(hooks.get("UserPromptSubmit", [])) + [
        {"hooks": [{"type": "command", "command": cmds["hook"], "timeout": 90,
                    "statusMessage": "tokengate: trying free models first"}]}]
    stop = [] if caveman in ("off", "lite") else [{"hooks": [{"type": "command", "async": True, "timeout": 120,
                                                             "command": cmds["stop"]}]}]
    hooks["Stop"] = _without_tokengate(hooks.get("Stop", [])) + stop
    hooks["SessionStart"] = _without_tokengate(hooks.get("SessionStart", [])) + [
        {"hooks": [{"type": "command", "command": cmds["install"] + " || true", "timeout": 300}]}]
    servers = s.setdefault("enabledMcpjsonServers", [])
    if "tokengate" not in servers:
        servers.append("tokengate")
    deny = s.setdefault("permissions", {}).setdefault("deny", [])
    for rule in ("Read(**/node_modules/**)", "Read(**/package-lock.json)", "Read(**/yarn.lock)",
                 "Read(**/.tokengate/**)"):
        if rule not in deny:
            deny.append(rule)
    settings_path.write_text(json.dumps(s, indent=2) + "\n", "utf-8")
    written.append(str(settings_path))

    from .style import rule
    policy = POLICY.replace(MARK_END, (f"- {rule(caveman)}\n" if rule(caveman) else "") + MARK_END)
    md = root / "CLAUDE.md"
    text = md.read_text("utf-8") if md.exists() else ""
    if MARK_START in text:
        a, b = text.index(MARK_START), text.index(MARK_END) + len(MARK_END)
        text = text[:a] + policy.strip() + text[b:]
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + policy
    md.write_text(text, "utf-8")
    written.append(str(md))
    return written
