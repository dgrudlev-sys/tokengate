"""Configuration: tokengate.toml in the project root, overridable by env vars."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import keys

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

DEFAULT_IGNORE = [
    "node_modules", ".git", ".expo", "android", "ios", "build", "dist", ".next",
    "__pycache__", ".venv", "venv", ".tokengate", "coverage", ".turbo", "Pods",
]

DEFAULT_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".sql"]


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str | None = None
    models: list[str] = field(default_factory=list)
    rpm: int = 30                 # requests per minute we allow ourselves
    max_parallel: int = 4
    timeout: int = 120
    enabled: bool = True
    rpd: int = 0                  # requests per day we allow ourselves (0 = no cap)
    may_train: bool = False       # provider's free tier may train on prompts (your code)
    pin_models: bool = False      # true: always use `models` as written, ignore `tokengate setup` picks

    def __post_init__(self):
        picked = None if self.pin_models else keys.models(self.name)
        if picked:  # validated by `tokengate setup` against what the key can actually use
            self.models = picked + [m for m in self.models if m not in picked]

    @property
    def api_key(self) -> str | None:
        return keys.get(self.api_key_env) if self.api_key_env else None

    @property
    def url(self) -> str:
        """base_url with ${VAR} filled from the environment or the tokengate key store."""
        return re.sub(r"\$\{(\w+)\}", lambda m: keys.get(m.group(1)) or m.group(0), self.base_url)

    @property
    def available(self) -> bool:
        if self.may_train and not _training_allowed(self.name):
            return False
        if "$" in self.url:  # an env var in base_url isn't set
            return False
        return self.enabled and (self.api_key_env is None or bool(self.api_key))


_ALLOW_TRAINING: list = [False]  # tokengate.toml `allow_training`: true / false / ["mistral", ...]


def _training_allowed(name: str) -> bool:
    if os.environ.get("TOKENGATE_ALLOW_TRAINING", "") == "1":
        return True
    allowed = _ALLOW_TRAINING[0]
    return name in allowed if isinstance(allowed, list) else bool(allowed)


def _default_providers() -> list[Provider]:
    return [
        # NVIDIA NIM (build.nvidia.com) – OpenAI-compatible, free developer credits.
        # Run `tokengate models` to see what your key can use and edit tokengate.toml.
        Provider(
            name="nvidia",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_env="NVIDIA_API_KEY",
            models=[
                "qwen/qwen2.5-coder-32b-instruct",
                "meta/llama-3.3-70b-instruct",
                "mistralai/mixtral-8x22b-instruct-v0.1",
            ],
            rpm=35,
            max_parallel=4,
        ),
        # Local model last: offline fallback. Silently skipped if Ollama isn't running.
        Provider(
            name="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key_env=None,
            models=["qwen2.5-coder:7b"],
            rpm=600,
            max_parallel=1,
            enabled=os.environ.get("TOKENGATE_OLLAMA", "1") != "0",
        ),
    ]


@dataclass
class Config:
    root: Path
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    providers: list[Provider] = field(default_factory=_default_providers)
    # Context handed to a delegated model is capped to keep free quotas lasting.
    max_context_tokens: int = 12000
    # Default budget for context packs returned to Cursor.
    pack_budget_tokens: int = 2500
    cache_ttl_days: int = 30
    # Gas gauge thresholds (% of Cursor's included usage).
    gauge_warn_pct: float = 60
    gauge_reserve_pct: float = 85
    # Cursor beforeSubmitPrompt hook: "auto" | "always" | "off"
    prompt_gate: str = "auto"
    # Pause once per Cursor chat after this many messages and suggest a fresh chat (0 = off).
    # Every message re-sends the whole chat, so long chats are the biggest hidden cost.
    long_chat_warn: int = 20
    # Providers whose free tier may train on your prompts that you accept: true / false / ["mistral"]
    allow_training: bool | list = False

    @property
    def state_dir(self) -> Path:
        d = self.root / ".tokengate"
        d.mkdir(exist_ok=True)
        return d


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` to the nearest dir with tokengate.toml / .git / package.json."""
    env = os.environ.get("TOKENGATE_ROOT")
    if env:
        return Path(env).resolve()
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / "tokengate.toml").exists():
            return d
    for d in [p, *p.parents]:
        if (d / ".git").exists() or (d / "package.json").exists():
            return d
    return p


def load(root: Path | None = None) -> Config:
    root = find_root(root)
    cfg = Config(root=root)
    path = root / "tokengate.toml"
    if not tomllib:
        return cfg
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:  # no project config: use the shipped template so every provider is known
        from importlib import resources
        text = resources.files("tokengate").joinpath("templates", "tokengate.toml").read_text("utf-8")
    data = tomllib.loads(text)
    for key in ("ignore", "extensions", "max_context_tokens", "pack_budget_tokens", "cache_ttl_days",
                "gauge_warn_pct", "gauge_reserve_pct", "prompt_gate", "allow_training", "long_chat_warn"):
        if key in data:
            setattr(cfg, key, data[key])
    if "providers" in data:
        cfg.providers = [Provider(**p) for p in data["providers"]]
    if "allow_training_providers" in data:  # older name
        cfg.allow_training = data["allow_training_providers"]
    _ALLOW_TRAINING[0] = cfg.allow_training
    return cfg
