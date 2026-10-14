"""`tokengate setup`: guided sign-up for free providers.

For each provider: open its key page, paste the key (hidden input), validate it live, pick
the best models that key can actually use, and store everything in the per-user key store.
Takes effect immediately in every project: no env vars, no Cursor restart.
"""

from __future__ import annotations

import getpass
import json
import re
import subprocess
import urllib.request
import webbrowser
from dataclasses import dataclass, field

from . import keys
from .config import Config, Provider
from .providers import Client, ProviderError

SKIP_MODELS = re.compile(r"embed|rerank|guard|whisper|tts|audio|vision-only|image|moderation|safety|ocr|parse|retriev", re.I)
CODER_PREFERENCE = [r"qwen3-coder", r"devstral", r"codestral", r"gpt-oss-120b", r"qwen.*coder.*32b",
                    r"deepseek.*v3", r"kimi-k2", r"llama-?4", r"llama-3\.3-70b", r"qwen3", r"mixtral-8x22b",
                    r"gpt-oss-20b", r"mistral-(medium|small)", r"gemini-.*flash"]


@dataclass
class Guide:
    name: str
    label: str
    env: str
    url: str
    steps: str
    effort: str
    value: str
    extra_env: dict = field(default_factory=dict)   # {ENV: prompt}
    model_filter: str = ""                           # regex the model id must match
    recommended: bool = True


GUIDES = [
    Guide("groq", "Groq", "GROQ_API_KEY", "https://console.groq.com/keys",
          "Sign in (email/Google/GitHub) -> Create API Key -> copy", "2 min", "~1,000 req/day per model, very fast"),
    Guide("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/settings/keys",
          "Sign in (email/Google) -> Create Key -> copy", "2 min",
          "50 req/day on :free models (1,000/day after a one-time $10)", model_filter=r":free$"),
    Guide("nvidia", "NVIDIA NIM", "NVIDIA_API_KEY", "https://build.nvidia.com",
          "Sign in / join Developer Program -> profile menu -> API Keys -> Generate -> copy (starts nvapi-)",
          "5 min", "~40 req/min, 100+ models"),
    Guide("mistral", "Mistral", "MISTRAL_API_KEY", "https://console.mistral.ai/api-keys",
          "Create account -> verify phone -> choose free plan -> Create new key -> copy", "5 min",
          "large monthly allowance incl. Codestral (trains on prompts)"),
    Guide("cerebras", "Cerebras", "CEREBRAS_API_KEY", "https://cloud.cerebras.ai",
          "Sign in -> API Keys (left menu) -> copy", "2 min", "very fast; free limits vary"),
    Guide("ollama_cloud", "Ollama Cloud", "OLLAMA_API_KEY", "https://ollama.com/settings/keys",
          "Sign in -> Settings -> Keys -> Add API key -> copy", "2 min", "large open models; session/weekly limits"),
    Guide("cloudflare", "Cloudflare Workers AI", "CLOUDFLARE_API_TOKEN", "https://dash.cloudflare.com/profile/api-tokens",
          "Create Token -> template 'Workers AI' -> copy token; Account ID is on the dashboard home / Workers AI page",
          "5 min", "10,000 Neurons/day", extra_env={"CLOUDFLARE_ACCOUNT_ID": "Cloudflare Account ID"}),
    Guide("gemini", "Google Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/apikey",
          "Sign in with Google -> Create API key -> copy", "1 min",
          "Flash models; trains on prompts (needs allow_training)", recommended=False),
    Guide("huggingface", "Hugging Face", "HF_TOKEN", "https://huggingface.co/settings/tokens",
          "New token (Read) -> copy", "1 min", "tiny monthly credits; last resort", recommended=False),
]


def guide(name: str) -> Guide | None:
    return next((g for g in GUIDES if g.name == name), None)


def pick_models(available: list[str], g: Guide, limit: int = 3) -> list[str]:
    cands = [m for m in available if not SKIP_MODELS.search(m) and (not g.model_filter or re.search(g.model_filter, m))]
    picked: list[str] = []
    for pat in CODER_PREFERENCE:
        for m in sorted(cands, key=len):
            if re.search(pat, m, re.I) and m not in picked:
                picked.append(m)
                break
        if len(picked) >= limit:
            break
    return picked


def validate(provider: Provider, g: Guide) -> tuple[str, list[str], str]:
    """-> (status ok|bad_key|offline|unknown, picked models, detail)"""
    client = Client(provider)
    try:
        listed = client.list_models()
    except ProviderError as e:
        msg = str(e)
        if msg.startswith(("401", "403")):
            return "bad_key", [], msg[:120]
        if "unreachable" in msg:
            return "offline", [], msg[:120]
        listed = []  # endpoint has no /models (e.g. Cloudflare): fall back to a 1-token chat
    picked = pick_models(listed, g) if listed else []
    probe = (picked or provider.models)[:1]
    if not probe:
        return "unknown", [], "no chat models found for this key"
    try:
        client.chat(probe[0], [{"role": "user", "content": "Reply with: ok"}], max_tokens=3, retries=0)
    except ProviderError as e:
        msg = str(e)
        if msg.startswith(("401", "403")):
            return "bad_key", [], msg[:120]
        return ("offline" if "unreachable" in msg else "unknown"), picked, msg[:120]
    return "ok", picked or probe, f"{len(listed)} models listed" if listed else "chat probe ok"


def _provider_for(cfg: Config, name: str) -> Provider | None:
    return next((p for p in cfg.providers if p.name == name), None)


def status_rows(cfg: Config, live: bool = False) -> list[tuple[str, str, str]]:
    rows = []
    for g in GUIDES:
        p = _provider_for(cfg, g.name)
        if not p:
            rows.append((g.label, "not in tokengate.toml", ""))
        elif not keys.get(g.env):
            rows.append((g.label, "no key", f"tokengate setup {g.name}"))
        elif not p.available:
            rows.append((g.label, "key saved, disabled", "trains on prompts: add to allow_training" if p.may_train else "check base_url vars"))
        elif live:
            st, models, detail = validate(p, g)
            rows.append((g.label, {"ok": "OK", "bad_key": "KEY REJECTED", "offline": "unreachable",
                                   "unknown": "?"}[st], ", ".join(models) or detail))
        else:
            rows.append((g.label, "key saved", ", ".join(p.models[:2])))
    return rows


def run(cfg: Config, only: list[str] | None = None, yes: bool = False) -> None:
    targets = [g for g in GUIDES if (g.name in only if only else g.recommended)]
    print(f"Keys are stored in {keys.location()} (only your user can read it).\n"
          "Press Enter at any prompt to skip a provider.\n")
    done = []
    for g in targets:
        p = _provider_for(cfg, g.name)
        if p is None:
            print(f"- {g.label}: not in this project's tokengate.toml, skipping")
            continue
        have = keys.get(g.env)
        print(f"== {g.label}  ({g.effort}, {g.value})")
        if have and not only:
            st, models, detail = validate(p, g)
            if st == "ok":
                keys.save_models(g.name, models)
                print(f"   already set up and working: {', '.join(models)}\n")
                done.append(g.label)
                continue
            print(f"   saved key isn't working ({detail}); let's replace it")
        if not yes and input("   Set it up now? [Y/n] ").strip().lower() in ("n", "no"):
            print()
            continue
        print(f"   1. {g.steps}\n   2. opening {g.url}")
        try:
            webbrowser.open(g.url)
        except Exception:
            pass
        for env, prompt in g.extra_env.items():
            val = input(f"   {prompt}: ").strip()
            if val:
                keys.save(env, val)
        for attempt in range(3):
            key = getpass.getpass("   Paste the key (hidden): ").strip()
            if not key:
                break
            keys.save(g.env, key)
            st, models, detail = validate(p, g)
            if st == "ok":
                keys.save_models(g.name, models)
                print(f"   OK. Using: {', '.join(models)}\n")
                done.append(g.label)
                break
            if st == "bad_key":
                print(f"   The provider rejected that key ({detail}). Try again.")
                keys.remove(g.env)
                continue
            print(f"   Saved, but couldn't verify right now ({detail}). It will be retried automatically.\n")
            break
        else:
            print("   Skipped after 3 attempts.\n")
    _setup_local_ollama(yes)
    print(f"\nDone. Working providers: {', '.join(done) or 'none yet'}.")
    print("Check anytime with `tokengate doctor`. Changes apply immediately (no Cursor restart).")
    if done:
        print("\nBiggest extra saving: point Cursor's own chat at a free provider. See `tokengate cursor-byok`.")


def _setup_local_ollama(yes: bool) -> None:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            tags = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        print("== Ollama (local, optional): not running. Install from https://ollama.com/download "
              "for an offline, private fallback.")
        return
    if any("coder" in t for t in tags):
        print(f"== Ollama (local): running with {', '.join(t for t in tags if 'coder' in t)}")
        return
    if yes or input("== Ollama is running but has no coder model. Pull qwen2.5-coder:7b (~4.7 GB)? [y/N] ").lower().startswith("y"):
        subprocess.run(["ollama", "pull", "qwen2.5-coder:7b"])


def byok_instructions(cfg: Config) -> str:
    """Exact values for Cursor's 'Override OpenAI Base URL' using a provider that already works."""
    for name in ("groq", "openrouter", "cerebras", "nvidia", "mistral"):
        p = _provider_for(cfg, name)
        if p and p.available:
            models = "\n".join(f"     + {m}" for m in p.models[:3])
            return (f"Use {name} as Cursor's chat model (no Cursor usage):\n"
                    f"  1. Cursor -> Settings -> Models -> API Keys\n"
                    f"  2. OpenAI API Key: paste your {p.api_key_env} (tokengate stores it at {keys.location()})\n"
                    f"  3. Turn on 'Override OpenAI Base URL': {p.url}\n"
                    f"  4. Add custom models (exact names):\n{models}\n"
                    f"  5. Pick one of them in the chat model dropdown. Switch back to Auto for Agent work if it misbehaves.\n"
                    f"Notes: the override applies to all OpenAI models; Tab always uses Cursor; Agent support varies by version.")
    return "No working provider yet. Run `tokengate setup` first."
