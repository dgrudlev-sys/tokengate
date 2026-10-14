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
