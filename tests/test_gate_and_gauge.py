import json
import time
from datetime import datetime, timedelta, timezone

from tokengate import cursor_usage, gauge, prompt_gate
from tokengate.cache import Store
from tokengate.config import Provider
from tokengate.graph import Graph
from tokengate.router import Gate

from test_tokengate import FakeLLM, fake_llm, make_cfg, project  # noqa: F401  (fixtures)


# ------------------------------------------------------------ prompt gate
def test_classify():
    assert prompt_gate.classify("Explain how search() ranks results") == "explain"
    assert prompt_gate.classify("write unit tests for parseReference") == "tests"
    assert prompt_gate.classify("add JSDoc comments to math.ts") == "docs"
    assert prompt_gate.classify("review SearchScreen for bugs") == "review"
    assert prompt_gate.classify("rename add to sum in math.ts") == "edit"


def test_decide_auto_mode():
    go = lambda p, files=(), level="ok", mode="auto": prompt_gate.decide(p, mode, level, list(files))[0]
    assert go("explain how the search pipeline works")
    assert not go("yes do it")                                   # needs chat history
    assert not go("fix the error above in the reader")           # needs chat history
    assert not go("!explain how the search pipeline works")      # user forced Cursor
    assert go("rename add to sum in math.ts", files=["src/lib/math.ts"])
    assert not go("refactor the whole navigation architecture")  # open-ended, no files
    assert go("refactor the whole navigation architecture", level="reserve")
    assert go("hello there friend ok", mode="always")
    assert not go("explain everything", mode="off")


def test_hook_blocks_cursor_when_free_model_answers(project, fake_llm, monkeypatch):
    FakeLLM.replies = {"m": "add() returns the sum of its two arguments."}
    (project / "tokengate.toml").write_text(
        f'[[providers]]\nname = "nvidia"\nbase_url = "{fake_llm}"\nmodels = ["m"]\nrpm = 100\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    out = prompt_gate.handle({"prompt": "explain what add does in math.ts", "workspace_roots": [str(project)]})
    assert out["continue"] is False
    assert "0 Cursor tokens" in out["user_message"]
    assert (project / ".tokengate" / "last_answer.md").exists()


def test_hook_lets_cursor_run_and_writes_handoff(project, fake_llm, monkeypatch):
    FakeLLM.replies = {"m": "ESCALATE: too hard"}
    (project / "tokengate.toml").write_text(
        f'[[providers]]\nname = "nvidia"\nbase_url = "{fake_llm}"\nmodels = ["m"]\nrpm = 100\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    out = prompt_gate.handle({"prompt": "explain what add does in math.ts", "workspace_roots": [str(project)]})
    assert out == {"continue": True}
    handoff = (project / ".tokengate" / "HANDOFF.md").read_text()
    assert "export function add" in handoff


# ------------------------------------------------------------ cursor usage
def test_parse_usage_summary_plan_bucket():
    u = cursor_usage.parse_summary({
        "billingCycleStart": "2026-10-01T00:00:00.000Z", "billingCycleEnd": "2026-11-01T00:00:00.000Z",
        "membershipType": "pro", "isUnlimited": False,
        "individualUsage": {"plan": {"enabled": True, "used": 1500, "limit": 2000, "remaining": 500}}})
    assert u.percent_used == 75 and u.used == 15.0 and u.limit == 20.0 and u.unit == "USD"
    assert u.cycle_end.month == 11


def test_parse_usage_summary_team_seat_without_individual_numbers():
    u = cursor_usage.parse_summary({"membershipType": "enterprise",
                                    "individualUsage": {"overall": {"enabled": False, "used": 0}}})
    assert u.percent_used is None and u.error


def test_jwt_user_id():
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "auth0|user_123"}).encode()).decode().rstrip("=")
    assert cursor_usage._user_id_from_jwt(f"x.{payload}.y") == "user_123"


# ------------------------------------------------------------------ gauge
def test_gauge_projection_and_levels(project, monkeypatch):
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    cfg = make_cfg(project)
    store = Store(cfg.state_dir)
    end = (datetime.now(timezone.utc) + timedelta(days=15)).date().isoformat()
    gauge.set_manual(store, 30, end)
    r = gauge.read(cfg, store)
    assert r.level == "ok" and r.runs_out_on is None

    gauge.set_manual(store, 80, end)  # 80% used, ~half the cycle gone -> runs out early
    r = gauge.read(cfg, store)
    assert r.level == "reserve" and r.runs_out_on is not None
    assert "RESERVE" in gauge.banner(cfg, store)
    assert "█" in gauge.bar(r)


def test_daily_cap_skips_provider(project, fake_llm):
    FakeLLM.replies = {"capped": "from capped", "spare": "from spare"}
    capped = Provider("openrouter", fake_llm, None, ["capped"], rpm=100, rpd=1)
    spare = Provider("groq", fake_llm, None, ["spare"], rpm=100)
    gate = Gate(make_cfg(project, [capped, spare]))
    assert gate.delegate("explain add", ["math.ts"], kind="explain").provider == "openrouter"
    res = gate.delegate("explain PI", ["math.ts"], kind="explain")
    assert res.provider == "groq" and any("daily cap" in a for a in res.attempts)


def test_training_providers_off_by_default(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "x")
    p = Provider("gemini", "http://x", "SOME_KEY", ["m"], may_train=True)
    assert not p.available
    monkeypatch.setenv("TOKENGATE_ALLOW_TRAINING", "1")
    assert p.available


def test_training_allow_list(monkeypatch):
    from tokengate import config as c
    monkeypatch.setenv("SOME_KEY", "x")
    monkeypatch.setattr(c, "_ALLOW_TRAINING", [["mistral"]])
    assert Provider("mistral", "http://x", "SOME_KEY", ["m"], may_train=True).available
    assert not Provider("gemini", "http://x", "SOME_KEY", ["m"], may_train=True).available


def test_base_url_env_expansion(monkeypatch):
    p = Provider("cloudflare", "https://x/accounts/${CF_ACC}/ai/v1", "SOME_KEY", ["m"])
    monkeypatch.setenv("SOME_KEY", "x")
    monkeypatch.delenv("CF_ACC", raising=False)
    assert not p.available
    monkeypatch.setenv("CF_ACC", "abc")
    assert p.available and p.url == "https://x/accounts/abc/ai/v1"


def test_explorer_with_graphify(project):
    import pytest
    from tokengate import gfy
    from tokengate.explore import Explorer
    if not gfy.available():
        pytest.skip("graphify not installed")
    cfg = make_cfg(project)
    assert "nodes" in gfy.build(cfg)
    ex = Explorer(Gate(cfg))
    assert "math.ts" in ex.run("explain", "add()") or "add" in ex.run("explain", "add()")
    assert "app.tsx" in ex.run("impact", "add()")
    assert "export function add" in ex.run("file", "math.ts")
    assert "unknown op" in ex.run("nope")


def test_image_prompts_go_to_cursor(project, fake_llm, monkeypatch):
    FakeLLM.replies = {"m": "a confident answer that never saw the image"}
    (project / "tokengate.toml").write_text(
        f'[[providers]]\nname = "nvidia"\nbase_url = "{fake_llm}"\nmodels = ["m"]\nrpm = 100\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    root = [str(project)]
    for payload in (
        {"prompt": "make the header in app.tsx match this layout", "attachments": [{"type": "image", "file_path": "/tmp/x.png"}]},
        {"prompt": "make the header in app.tsx match this layout", "attachments": [{"file_path": "C:/Users/me/shot.PNG"}]},
        {"prompt": "fix the bug shown in the screenshot in app.tsx please"},
        {"prompt": "use assets/logo.png as the header icon in app.tsx"},
    ):
        assert prompt_gate.handle({**payload, "workspace_roots": root}) == {"continue": True}, payload
    assert FakeLLM.calls == []                       # no free model was asked to answer blind
    assert prompt_gate.image_reason("explain what add does in math.ts", [{"file_path": "src/lib/math.ts"}]) is None


# ---------------------------------------------------------------- Claude Code
def _claude(prompt, project, **kw):
    return {"hook_event_name": "UserPromptSubmit", "session_id": kw.get("sid", "s1"),
            "transcript_path": "/tmp/t.jsonl", "cwd": str(project), "prompt": prompt}


def test_claude_hook_blocks_when_free_model_answers(project, fake_llm, monkeypatch):
    FakeLLM.replies = {"m": "add() returns the sum of its two arguments."}
    (project / "tokengate.toml").write_text(
        f'[[providers]]\nname = "nvidia"\nbase_url = "{fake_llm}"\nmodels = ["m"]\nrpm = 100\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    out = prompt_gate.handle(_claude("explain what add does in math.ts", project))
    assert out["decision"] == "block" and "0 Claude tokens" in out["reason"]


def test_claude_hook_injects_context_on_escalation(project, fake_llm, monkeypatch):
    FakeLLM.replies = {"m": "ESCALATE: too hard"}
    (project / "tokengate.toml").write_text(
        f'[[providers]]\nname = "nvidia"\nbase_url = "{fake_llm}"\nmodels = ["m"]\nrpm = 100\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    out = prompt_gate.handle(_claude("explain what add does in math.ts", project))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit" and "export function add" in ctx
    # history-dependent and image prompts pass straight through with no output
    assert prompt_gate.handle(_claude("yes do it", project)) == {}
    assert prompt_gate.handle(_claude("fix this [Image #1] in app.tsx please", project)) == {}


def test_claude_long_chat_guard_suggests_compact(project, monkeypatch):
    (project / "tokengate.toml").write_text('long_chat_warn = 2\nproviders = []\n')
    monkeypatch.setattr(cursor_usage, "fetch", lambda: cursor_usage.CursorUsage(None, source="unavailable"))
    prompt_gate.handle(_claude("ok", project, sid="x"))
    out = prompt_gate.handle(_claude("ok again", project, sid="x"))
    assert out["decision"] == "block" and "/compact" in out["reason"]


def test_init_for_claude_writes_config(project):
    import json
    from tokengate.cli import main
    main(["init", str(project), "--for", "claude"])
    mcp = json.loads((project / ".mcp.json").read_text())
    assert "tokengate" in mcp["mcpServers"]
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert "tokengate.prompt_gate" in json.dumps(settings["hooks"]["UserPromptSubmit"])
    assert "tokengate" in settings["enabledMcpjsonServers"]
    assert "tokengate:start" in (project / "CLAUDE.md").read_text()
    assert not (project / ".cursor").exists()
    main(["init", str(project), "--for", "claude"])          # idempotent: no duplicate hooks
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1
    assert (project / "CLAUDE.md").read_text().count("tokengate:start") == 1


# ---------------------------------------------------------------- caveman style
def test_caveman_rule_lands_in_both_clients(project):
    import json
    from tokengate.cli import main
    main(["init", str(project)])
    assert "caveman-medium" in (project / ".cursor" / "rules" / "tokengate.mdc").read_text()
    assert "caveman-medium" in (project / "CLAUDE.md").read_text()
    stop = json.loads((project / ".claude" / "settings.json").read_text())["hooks"]["Stop"]
    assert "tokengate.style" in json.dumps(stop) and stop[0]["hooks"][0]["async"] is True


def test_translate_squeeze_and_stop_hook(project, fake_llm, tmp_path):
    import json
    from tokengate import style
    FakeLLM.replies = {"m": "The bug was a missing empty-book check; it is fixed and tests pass."}
    cfg = make_cfg(project, [Provider("nvidia", fake_llm, None, ["m"], rpm=100)])
    assert "missing empty-book check" in style.translate(cfg, "Bug: no empty-book check. Fixed. Tests ✓.")
    assert style.squeeze(make_cfg(project, []), "raw log") == "raw log"      # no free model: unchanged
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"role": "user", "content": "fix it"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Bug fixed."}]}},
    ]))
    assert style.last_assistant_text(tr) == "Bug fixed."


def test_hook_input_decoding_windows_variants():
    payload = {"prompt": "explain add", "workspace_roots": ["C:\\\\proj"]}
    import json as _j
    raw = _j.dumps(payload)
    for blob in (raw.encode(), b"\xef\xbb\xbf" + raw.encode(), raw.encode("utf-16"), raw.encode("utf-16-le"),
                 b"\r\n" + raw.encode() + b"\r\n"):
        assert prompt_gate.read_payload(blob) == payload
    assert prompt_gate.read_payload(b"   ") == {}
