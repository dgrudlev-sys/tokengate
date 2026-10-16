import builtins
import getpass
import webbrowser

import pytest

from tokengate import keys, setup_wizard
from tokengate.config import Provider

from test_tokengate import FakeLLM, fake_llm, make_cfg, project  # noqa: F401


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)


def test_pick_models_prefers_coders_and_skips_junk():
    g = setup_wizard.guide("groq")
    listed = ["whisper-large-v3", "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "meta-llama/llama-guard-4-12b",
              "qwen/qwen3-coder-480b", "openai/gpt-oss-20b"]
    assert setup_wizard.pick_models(listed, g) == ["qwen/qwen3-coder-480b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile"]
    orr = setup_wizard.guide("openrouter")
    assert setup_wizard.pick_models(["qwen/qwen3-coder", "qwen/qwen3-coder:free"], orr) == ["qwen/qwen3-coder:free"]


def test_key_store_roundtrip_and_env_precedence(monkeypatch):
    keys.save("GROQ_API_KEY", "gsk_file")
    assert keys.get("GROQ_API_KEY") == "gsk_file"
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    assert keys.get("GROQ_API_KEY") == "gsk_env"
    keys.save_models("groq", ["a", "b"])
    p = Provider("groq", "http://x", "GROQ_API_KEY", ["c", "a"])
    assert p.models == ["a", "b"]                      # only models setup verified; stale defaults dropped
    assert Provider("groq", "http://x", "GROQ_API_KEY", ["c"], pin_models=True).models == ["c"]


def test_wizard_rejects_bad_key_then_accepts_good_one(project, fake_llm, monkeypatch):
    FakeLLM.listed = ["text-embedding-3", "qwen/qwen3-coder-480b", "llama-3.3-70b-versatile"]
    FakeLLM.good_key = "gsk_good"
    FakeLLM.replies = {"qwen/qwen3-coder-480b": "ok"}
    cfg = make_cfg(project, [Provider("groq", fake_llm, "GROQ_API_KEY", ["fallback"], rpm=100)])
    answers = iter(["gsk_bad", "gsk_good"])
    monkeypatch.setattr(getpass, "getpass", lambda *a: next(answers))
    monkeypatch.setattr(builtins, "input", lambda *a: "")
    setup_wizard.run(cfg, ["groq"])
    assert keys.get("GROQ_API_KEY") == "gsk_good"
    assert keys.models("groq") == ["qwen/qwen3-coder-480b"]          # only models that actually answered
    rows = dict((r[0], r[1]) for r in setup_wizard.status_rows(cfg, live=True))
    assert rows["Groq"] == "OK"


def test_doctor_reports(project):
    from tokengate.doctor import report
    text = report(make_cfg(project), live=False)
    assert "free provider(s) ready" in text and "fix:" in text


def test_demo_runs(capsys):
    from tokengate.demo import run
    assert run() == 0
    out = capsys.readouterr().out
    assert "continue=False" in out and "tier=cache" in out and "This chat has 3 messages" in out


def test_circuit_breaker_moves_failing_provider_last(project):
    from tokengate.router import Gate
    a, b = Provider("a", "http://x", None, ["m"]), Provider("b", "http://x", None, ["m"])
    gate = Gate(make_cfg(project, [a, b]))
    for _ in range(3):
        gate.store.log("fail", "a", "m")
    assert [p.name for p in gate._ordered_providers()] == ["b", "a"]


def test_validate_skips_models_that_fail(project, fake_llm):
    FakeLLM.listed = ["qwen/qwen3-coder:free", "deepseek/deepseek-chat-v3.1:free"]
    FakeLLM.replies = {"deepseek/deepseek-chat-v3.1:free": "ok"}          # first one 404s (not free any more)
    p = Provider("openrouter", fake_llm, None, ["x"], rpm=100)
    status, models, _ = setup_wizard.validate(p, setup_wizard.guide("openrouter"))
    assert status == "ok" and models == ["deepseek/deepseek-chat-v3.1:free"]


def test_redact_hides_tokens_and_account_ids():
    out = setup_wizard._redact("404 Could not route to /client/v4/accounts/cfut_p9nPmjtABCDEF/ai and key nvapi-abcdef1234")
    assert "p9nPmjt" not in out and "abcdef1234" not in out


def test_pick_models_falls_back_to_any_free_model():
    g = setup_wizard.guide("openrouter")
    listed = ["some-lab/new-model-7b:free", "other/big-model-70b:free", "paid/model-405b"]
    assert setup_wizard.pick_models(listed, g) == ["other/big-model-70b:free", "some-lab/new-model-7b:free"]
