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
        "env": {"TOKENGATE_ROOT": "${workspaceFolder}", "NVIDIA_API_KEY": "${env:NVIDIA_API_KEY}"},
    }
    mcp_path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    print(f"wrote {mcp_path}")

    for target, lines in ((root / ".cursorignore", _template("cursorignore").splitlines()),
                          (root / ".gitignore", [".tokengate/"])):
        existing = target.read_text("utf-8").splitlines() if target.exists() else []
        missing = [l for l in lines if l not in existing]
        if missing:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(("\n" if existing and existing[-1] else "") + "\n".join(missing) + "\n")
            print(f"updated {target} (+{len(missing)} lines)")

    cfg = config.load(root)
    print("graph:", Graph(cfg).build(incremental=False))
    from .visualize import export
    for k, v in export(Graph(cfg).load()).items():
        print(f"{k}: {v}")
    if not shutil.which("rg"):
        print("tip: install ripgrep (rg) for faster search")
    print("\nNext: set NVIDIA_API_KEY (https://build.nvidia.com), restart Cursor, and enable the "
          "'tokengate' server under Settings > MCP.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tokengate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("project", nargs="?")
    sub.add_parser("index")
    sub.add_parser("viz")
    p = sub.add_parser("query"); p.add_argument("what", choices=["symbol", "file", "impact"]); p.add_argument("name")
    p = sub.add_parser("pack"); p.add_argument("task"); p.add_argument("-f", "--files", nargs="*")
    p = sub.add_parser("ask"); p.add_argument("task"); p.add_argument("-f", "--files", nargs="*")
    p.add_argument("-k", "--kind", default="general"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("fanout"); p.add_argument("task"); p.add_argument("glob"); p.add_argument("-k", "--kind", default="summarize")
    sub.add_parser("check")
    sub.add_parser("models")
    sub.add_parser("stats")
    sub.add_parser("serve")
    args = ap.parse_args(argv)

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
        if args.what == "symbol":
            for h in graph.find_symbol(args.name)[:25]:
                print(f"{h['file']}:{h['line']} [{h['kind']}] {h['signature']}")
        else:
            p = graph.resolve_file(args.name)
            if not p:
                sys.exit(f"no file matching {args.name!r}")
            print(graph.outline(p) if args.what == "file" else "\n".join(graph.impact(p)))
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
