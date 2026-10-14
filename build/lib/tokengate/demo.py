"""`tokengate demo`: the whole pipeline end to end, offline, in a throwaway sample project.

A scripted OpenAI-compatible server stands in for the free providers: one model that always
escalates, one that returns a broken edit, one that does the job. Nothing touches your project,
your keys or the network. Use it to see how tokengate behaves before adding real keys.
"""

from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import config, gfy, prompt_gate
from .cache import Store
from .explore import Explorer
from .graph import Graph
from .router import Gate

SAMPLE = {
    "src/lib/price.ts": "export function applyDiscount(price: number, pct: number): number {\n"
                        "  return price - price * pct;\n}\n",
    "src/cart.ts": "import { applyDiscount } from './lib/price';\n\n"
                   "export function total(items: number[], pct: number): number {\n"
                   "  return applyDiscount(items.reduce((a, b) => a + b, 0), pct);\n}\n",
    "src/screens/Checkout.tsx": "import { total } from '../cart';\n\n"
                                "export function Checkout() {\n  return total([10, 20], 0.1);\n}\n",
}

GOOD_EDIT = ("src/lib/price.ts\n<<<<<<< SEARCH\n  return price - price * pct;\n=======\n"
             "  if (pct < 0 || pct > 1) throw new RangeError('pct must be 0..1');\n"
             "  return price - price * pct;\n>>>>>>> REPLACE")
BAD_EDIT = "src/lib/price.ts\n<<<<<<< SEARCH\n  return price * 2;\n=======\n  return 0;\n>>>>>>> REPLACE"


class _Scripted(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        model = body["model"]
        if model == "always-escalates":
            text = "ESCALATE: not confident"
        elif model == "sloppy-coder":
            text = BAD_EDIT if "SEARCH" in prompt else "ESCALATE: unsure"
        elif "architecture" in prompt:
            text = "ESCALATE: needs the whole design discussion"
        elif "<<<<<<< SEARCH" in prompt:
            text = GOOD_EDIT
        else:
            text = "applyDiscount(price, pct) returns price minus pct of it; total() sums the cart then applies it."
        data = json.dumps({"choices": [{"message": {"content": text}}],
                           "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _step(n: int, title: str) -> None:
    print(f"\n\033[1m{n}. {title}\033[0m")


def run() -> int:
    srv = HTTPServer(("127.0.0.1", 0), _Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in SAMPLE.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        (root / "tokengate.toml").write_text(
            'prompt_gate = "always"\nlong_chat_warn = 3\n'
            f'[[providers]]\nname = "demo"\nbase_url = "{url}"\npin_models = true\n'
            'models = ["always-escalates", "sloppy-coder", "good-coder"]\nrpm = 600\n')
        cfg = config.load(root)
        graph = Graph(cfg)
        graph.build()
        store = Store(cfg.state_dir)
        gate = Gate(cfg, graph, store)
        ex = Explorer(gate)

        _step(1, "Explore without reading files (tier 0, free)")
        if gfy.build(cfg):
            print(ex.run("explain", "applyDiscount()"))
        print(ex.run("file", "price.ts"))
        print("impact ->", ex.run("impact", "src/lib/price.ts").replace("\n", " | "))

        _step(2, "Chat question answered before Cursor sees it (prompt-gate hook)")
        out = prompt_gate.handle({"prompt": "explain how the cart total is calculated", "workspace_roots": [str(root)]})
        print(f"hook -> continue={out['continue']} (False = Cursor's model never ran)")
        print("popup:", out.get("user_message", "").splitlines()[0])

        _step(3, "Edit delegated: broken edit rejected, next model's edit applied")
        res = gate.delegate("validate that pct is between 0 and 1 in applyDiscount", ["price.ts"], kind="edit", verify=False)
        print(res.render().splitlines()[0], "| tried:", " -> ".join(a.split(":")[0] for a in res.attempts) or "-")
        print((root / "src/lib/price.ts").read_text())

        _step(4, "Same request again: served from cache (no API call)")
        (root / "src/lib/price.ts").write_text(SAMPLE["src/lib/price.ts"])
        graph.build()
        print(gate.delegate("validate that pct is between 0 and 1 in applyDiscount", ["price.ts"], kind="edit",
                            verify=False).render().splitlines()[0])

        _step(5, "Too hard for free models: handed to Cursor with context already gathered")
        out = prompt_gate.handle({"prompt": "redesign the checkout architecture for multi-currency",
                                  "workspace_roots": [str(root)]})
        print(f"hook -> continue={out['continue']} (True = Cursor runs)")
        print("handoff:", (cfg.state_dir / "HANDOFF.md").read_text().splitlines()[1])

        _step(6, "Long-chat guard (demo threshold: 3 messages)")
        for i in range(2):
            prompt_gate.handle({"prompt": f"question {i}", "conversation_id": "c1", "workspace_roots": [str(root)]})
        out = prompt_gate.handle({"prompt": "and one more question about totals please",
                                  "conversation_id": "c1", "workspace_roots": [str(root)]})
        print(out.get("user_message", "(no pause)"))

        _step(7, "Savings ledger")
        s = store.stats()
        print({k: v["calls"] for k, v in s["tiers"].items()}, "| est. Cursor tokens avoided:",
              s["total_cursor_tokens_avoided"])
    srv.shutdown()
    print("\nDemo finished. Next: `tokengate setup` to add real free providers, then `tokengate doctor`.")
    return 0
