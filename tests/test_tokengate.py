import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tokengate import config
from tokengate.config import Provider
from tokengate.graph import Graph
from tokengate.providers import Client
from tokengate.router import Gate, apply_edits, parse_edits


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tsconfig.json").write_text('{\n // comment\n "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}')
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "math.ts").write_text(
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
        "export const PI = 3.14;\nconst hidden = 1;\n")
    (tmp_path / "src" / "app.tsx").write_text(
        "import { add } from '@/lib/math';\nimport React from 'react';\n"
        "export default function App() { return add(1, 2); }\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("export const x = 1;")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("from .util import helper\n\nclass Engine:\n    def run(self):\n        return helper()\n")
    (tmp_path / "pkg" / "util.py").write_text("def helper():\n    return 1\n")
    return tmp_path


def make_cfg(root: Path, providers: list[Provider] | None = None):
    cfg = config.Config(root=root)
    cfg.providers = providers or []
    return cfg


def test_graph_resolves_aliases_relative_and_python(project):
    g = Graph(make_cfg(project))
    stats = g.build()
    assert stats["files"] == 5  # node_modules ignored
    app = g.files["src/app.tsx"]
    assert app.imports == ["src/lib/math.ts"]
    assert app.imported_names["src/lib/math.ts"] == ["add"]
    assert app.external == ["react"]
    assert g.files["pkg/core.py"].imports == ["pkg/util.py"]
    assert g.impact("src/lib/math.ts") == ["src/app.tsx"]
    assert g.find_symbol("add")[0]["file"] == "src/lib/math.ts"
    assert "Engine.run" in [s.name for s in g.files["pkg/core.py"].symbols]
    assert "hidden" not in g.outline("src/lib/math.ts")
    assert g.rank("math add")[0][0] == "src/lib/math.ts"


def test_incremental_build_reuses_unchanged(project):
    g = Graph(make_cfg(project))
    g.build()
    (project / "src" / "app.tsx").write_text("export const y = 2;\n")
    assert Graph(make_cfg(project)).build()["reparsed"] == 1


def test_apply_edits_all_or_nothing(project):
    good = [("src/lib/math.ts", "return a + b;", "return b + a;")]
    bad = good + [("src/lib/math.ts", "does not exist", "x")]
    assert apply_edits(project, bad) is None
    assert "a + b" in (project / "src/lib/math.ts").read_text()
    assert apply_edits(project, good) is not None
    assert "b + a" in (project / "src/lib/math.ts").read_text()
    assert apply_edits(project, [("../escape.ts", "", "x")]) is None


def test_parse_edits():
    text = "src/a.ts\n```ts\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n```"
    assert parse_edits(text) == [("src/a.ts", "old", "new")]


# ---------------------------------------------------------------- fake NIM
class FakeLLM(BaseHTTPRequestHandler):
    replies: dict[str, object] = {}
    calls: list[str] = []
    listed: list[str] = []
    good_key = ""

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        code = 401 if FakeLLM.good_key and auth != f"Bearer {FakeLLM.good_key}" else 200
        body = json.dumps({"data": [{"id": m} for m in FakeLLM.listed]}).encode()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body if code == 200 else b"{}")

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = body["model"]
        FakeLLM.calls.append(model)
        reply = FakeLLM.replies.get(model, 404)
        if isinstance(reply, int):
            self.send_response(reply)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        out = {"choices": [{"message": {"content": reply}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())


@pytest.fixture
def fake_llm():
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeLLM.calls = []
    FakeLLM.listed, FakeLLM.good_key = [], ""
    Client._dead_models.clear()
    Client._down_until.clear()
    Client._limiters.clear()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_escalation_ladder(project, fake_llm):
    FakeLLM.replies = {"dead": 404, "shy": "ESCALATE: not enough context", "good": "It adds two numbers."}
    prov = Provider("nvidia", fake_llm, None, ["dead", "shy", "good"], rpm=100)
    gate = Gate(make_cfg(project, [prov]))
    res = gate.delegate("explain add", ["math.ts"], kind="explain")
    assert res.status == "done" and res.model == "good"
    assert FakeLLM.calls == ["dead", "shy", "good"]

    # Second identical call is served from cache; no HTTP.
    FakeLLM.calls = []
    assert gate.delegate("explain add", ["math.ts"], kind="explain").tier == "cache"
    assert FakeLLM.calls == []
    stats = gate.store.stats()
    assert stats["tiers"]["nvidia"]["calls"] == 1 and stats["total_cursor_tokens_avoided"] > 0


def test_escalates_to_cursor_with_context_pack(project, fake_llm):
    FakeLLM.replies = {"a": "ESCALATE: hard"}
    gate = Gate(make_cfg(project, [Provider("nvidia", fake_llm, None, ["a"], rpm=100)]))
    res = gate.delegate("refactor add", ["math.ts"], kind="edit")
    assert res.status == "escalate"
    assert "export function add" in res.context_pack


def test_edit_is_applied(project, fake_llm):
    edit = "src/lib/math.ts\n<<<<<<< SEARCH\n  return a + b;\n=======\n  return a + b + 0;\n>>>>>>> REPLACE"
    FakeLLM.replies = {"broken": "src/lib/math.ts\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE", "ok": edit}
    gate = Gate(make_cfg(project, [Provider("nvidia", fake_llm, None, ["broken", "ok"], rpm=100)]))
    res = gate.delegate("make add explicit", ["math.ts"], kind="edit", verify=False)
    assert res.status == "done" and res.model == "ok"
    assert res.files_changed == ["src/lib/math.ts"]
    assert "a + b + 0" in (project / "src/lib/math.ts").read_text()


def test_unreachable_provider_is_skipped(project, fake_llm):
    FakeLLM.replies = {"m": "fine"}
    dead = Provider("ollama", "http://127.0.0.1:9/v1", None, ["x"], timeout=2)
    gate = Gate(make_cfg(project, [dead, Provider("nvidia", fake_llm, None, ["m"], rpm=100)]))
    res = gate.delegate("explain", ["math.ts"], kind="explain")
    assert res.status == "done" and res.provider == "nvidia"


def test_fan_out(project, fake_llm):
    from tokengate.distribute import fan_out
    FakeLLM.replies = {"a": "summary A", "b": "summary B"}
    gate = Gate(make_cfg(project, [Provider("nvidia", fake_llm, None, ["a", "b"], rpm=100)]))
    out = fan_out(gate, "summarize", "src/**")
    assert out["files"] == 2 and out["done"] == 2 and out["summary"]
    assert (project / out["report"]).exists()


def test_dart_symbols_public_unless_underscore(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: calc\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "keypad.dart").write_text(
        "import 'package:flutter/material.dart';\n\nclass Keypad extends StatelessWidget {\n}\n"
        "class _KeypadState {}\nmixin Speaks {}\n")
    cfg = make_cfg(tmp_path)
    g = Graph(cfg)
    g.build()
    syms = {s.name: s.exported for s in g.files["lib/keypad.dart"].symbols}
    assert syms["Keypad"] and not syms["_KeypadState"] and syms["Speaks"]
    assert g.find_symbol("keypad")[0]["file"] == "lib/keypad.dart"
