"""tokengate CLI.

  tokengate init [PROJECT]          plug into a project (Cursor rules + MCP config + graph)
  tokengate index                   (re)build the code graph
  tokengate viz                     write graph.html / graph.mmd / REPO_MAP.md
  tokengate query symbol|file|impact NAME
  tokengate pack "task" [-f FILE..]
  tokengate ask "task" [-f FILE..] [-k KIND] [--dry-run]
  tokengate fanout "task" "GLOB" [-k KIND]
  tokengate check                   run local typecheck/lint/tests
  tokengate models                  list models each provider offers your key
  tokengate stats                   tokens saved
  tokengate gauge [--set PCT] [--json]   Cursor fuel gauge (burn rate, runs-out date)
  tokengate dashboard [--port 8765]      local gauge dashboard in the browser
  tokengate install-statusbar            fuel gauge in Cursor's status bar
  tokengate setup [PROVIDER..]           guided free-API sign-up: open page, paste key, validated
  tokengate doctor [--offline]           check everything, with the fix for each problem
  tokengate demo                         offline end-to-end tour with a scripted model
  tokengate cursor-byok                  exact settings to run Cursor's chat on a free provider
  tokengate serve                   run the MCP server (Cursor starts this for you)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib import resources
from pathlib import Path

from . import config, local_tools
from .cache import Store
from .graph import Graph
from .providers import Client, ProviderError


def _template(name: str) -> str:
    return resources.files("tokengate").joinpath("templates", name).read_text("utf-8")


def cmd_init(args) -> None:
    root = Path(args.project or ".").resolve()
    (root / ".cursor" / "rules").mkdir(parents=True, exist_ok=True)

    toml = root / "tokengate.toml"
    if not toml.exists():
        toml.write_text(_template("tokengate.toml"), "utf-8")
        print(f"wrote {toml}")

    rule = root / ".cursor" / "rules" / "tokengate.mdc"
    rule.write_text(_template("tokengate.mdc"), "utf-8")
    print(f"wrote {rule}")

    mcp_path = root / ".cursor" / "mcp.json"
    data = json.loads(mcp_path.read_text("utf-8")) if mcp_path.exists() else {}
    data.setdefault("mcpServers", {})["tokengate"] = {
        "command": sys.executable,
        "args": ["-m", "tokengate.mcp_server"],
        "env": {"TOKENGATE_ROOT": "${workspaceFolder}"},  # API keys come from the tokengate key store
    }
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    print(f"wrote {mcp_path}")

    # Front door: tokengate sees each chat prompt before Cursor's model does.
    hooks_path = root / ".cursor" / "hooks.json"
    hooks = json.loads(hooks_path.read_text("utf-8")) if hooks_path.exists() else {}
    hooks.setdefault("version", 1)
    entries = [h for h in hooks.setdefault("hooks", {}).get("beforeSubmitPrompt", [])
               if "tokengate.prompt_gate" not in h.get("command", "")]
    entries.append({"command": f'"{sys.executable}" -m tokengate.prompt_gate'})
    hooks["hooks"]["beforeSubmitPrompt"] = entries
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n", "utf-8")
    print(f"wrote {hooks_path}")

    for target, lines in ((root / ".cursorignore", _template("cursorignore").splitlines() + [".graphifyignore"]),
                          (root / ".gitignore", [".tokengate/"])):
        existing = target.read_text("utf-8").splitlines() if target.exists() else []
        missing = [l for l in lines if l not in existing]
        if missing:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if existing and existing[-1] else "") + "\n".join(missing) + "\n")
            print(f"updated {target} (+{len(missing)} lines)")

    cfg = config.load(root)
    print("graph:", Graph(cfg).build(incremental=False))
    from . import gfy
    print("graphify:", gfy.build(cfg) or "not installed (pip install graphifyy) - using built-in graph only")
    from .visualize import export
    for k, v in export(Graph(cfg).load()).items():
        print(f"{k}: {v}")
    if not shutil.which("rg"):
        print("tip: install ripgrep (rg) for faster search")
    from .setup_wizard import status_rows
    ready = sum(1 for _, st, _ in status_rows(cfg) if st == "key saved")
    if ready == 0 and sys.stdin.isatty() and input("\nNo free providers set up yet. Do it now (~5 min)? [Y/n] ").strip().lower() not in ("n", "no"):
        from .setup_wizard import run as setup
        setup(cfg)
    print("\nNext: restart Cursor, enable 'tokengate' under Settings > MCP, then run `tokengate doctor`.")


def cmd_install_statusbar() -> None:
    """Copy the status-bar gauge into Cursor's extensions folder (unpacked VS Code extension)."""
    src = resources.files("tokengate").joinpath("templates", "statusbar")
    dest = Path.home() / ".cursor" / "extensions" / "tokengate.tokengate-gauge-0.1.0"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "extension.js"):
        (dest / name).write_text(src.joinpath(name).read_text("utf-8"), "utf-8")
    print(f"installed to {dest}\nRestart Cursor. Set 'tokengate.python' in settings if `python` isn't the one with tokengate.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tokengate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("project", nargs="?")
    sub.add_parser("index")
    sub.add_parser("viz")
    p = sub.add_parser("query"); p.add_argument("what", choices=["map", "file", "find", "explain", "impact", "path", "hubs", "pack", "search"])
    p.add_argument("q", nargs="?", default=""); p.add_argument("q2", nargs="?", default="")
    p = sub.add_parser("pack"); p.add_argument("task"); p.add_argument("-f", "--files", nargs="*")
    p = sub.add_parser("ask"); p.add_argument("task"); p.add_argument("-f", "--files", nargs="*")
    p.add_argument("-k", "--kind", default="general"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("fanout"); p.add_argument("task"); p.add_argument("glob"); p.add_argument("-k", "--kind", default="summarize")
    sub.add_parser("check")
    sub.add_parser("models")
    sub.add_parser("stats")
    sub.add_parser("serve")
    p = sub.add_parser("gauge"); p.add_argument("--set", type=float, metavar="PCT", help="manual reading from cursor.com/dashboard")
    p.add_argument("--cycle-end", help="YYYY-MM-DD, with --set"); p.add_argument("--json", action="store_true")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("dashboard"); p.add_argument("--port", type=int, default=8765)
    sub.add_parser("install-statusbar")
    p = sub.add_parser("setup"); p.add_argument("providers", nargs="*"); p.add_argument("-y", "--yes", action="store_true")
    p = sub.add_parser("doctor"); p.add_argument("--offline", action="store_true", help="don't call provider APIs")
    sub.add_parser("demo")
    sub.add_parser("cursor-byok")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        from .demo import run as demo
        sys.exit(demo())

    if args.cmd == "install-statusbar":
        return cmd_install_statusbar()

    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "serve":
        from .mcp_server import main as serve
        return serve()

    cfg = config.load()
    if args.cmd == "index":
        print(Graph(cfg).build())
        return
    if args.cmd == "check":
        print(json.dumps(local_tools.run_checks(cfg), indent=2))
        return
    if args.cmd == "setup":
        from .setup_wizard import run as setup
        return setup(cfg, args.providers or None, args.yes)
    if args.cmd == "doctor":
        from .doctor import report
        print(report(cfg, live=not args.offline))
        return
    if args.cmd == "cursor-byok":
        from .setup_wizard import byok_instructions
        print(byok_instructions(cfg))
        return
    if args.cmd == "gauge":
        from . import gauge
        store = Store(cfg.state_dir)
        if args.set is not None:
            gauge.set_manual(store, args.set, args.cycle_end)
        r = gauge.read(cfg, store, refresh=args.refresh)
        print(gauge.to_json(r) if args.json else gauge.bar(r))
        return
    if args.cmd == "dashboard":
        from .dashboard import serve
        return serve(cfg, args.port)
    if args.cmd == "stats":
        print(Store(cfg.state_dir).dump())
        return
    if args.cmd == "models":
        for prov in cfg.providers:
            c = Client(prov)
            if not prov.available:
                print(f"[{prov.name}] unavailable (missing {prov.api_key_env} or disabled)")
                continue
            try:
                models = c.list_models()
                print(f"[{prov.name}] {len(models)} models")
                for m in models:
                    print(("  * " if m in prov.models else "    ") + m)
            except ProviderError as e:
                print(f"[{prov.name}] error: {e}")
        return

    graph = Graph(cfg).load()
    graph.build()
    if args.cmd == "viz":
        from .visualize import export
        for k, v in export(graph).items():
            print(f"{k}: {v}")
    elif args.cmd == "query":
        from . import gfy
        from .explore import Explorer
        from .router import Gate
        if gfy.available() and not gfy.graph_path(cfg).exists():
            gfy.build(cfg)
        print(Explorer(Gate(cfg, graph)).run(args.what, args.q, args.q2))
    else:
        from .router import Gate
        gate = Gate(cfg, graph)
        if args.cmd == "pack":
            print(gate.context_pack(args.task, args.files))
        elif args.cmd == "ask":
            print(gate.delegate(args.task, args.files, args.kind, apply=not args.dry_run).render())
        elif args.cmd == "fanout":
            from .distribute import fan_out, to_text
            print(to_text(fan_out(gate, args.task, args.glob, args.kind)))


if __name__ == "__main__":
    main()
