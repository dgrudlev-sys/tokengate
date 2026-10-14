"""Per-user key + model store, shared by every project on this machine.

Keys live in a user-only file instead of environment variables, so `tokengate setup` takes
effect immediately: no `setx`, no shell profile, no Cursor restart. Real environment variables
still win if set.

  Windows  %APPDATA%\\tokengate\\keys.env
  macOS    ~/Library/Application Support/tokengate/keys.env
  Linux    ~/.config/tokengate/keys.env
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_cache: dict[str, tuple[float, dict]] = {}


def user_dir() -> Path:
    if os.environ.get("TOKENGATE_HOME"):
        d = Path(os.environ["TOKENGATE_HOME"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "tokengate"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "tokengate"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tokengate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keys_file() -> Path:
    return user_dir() / "keys.env"


def _models_file() -> Path:
    return user_dir() / "models.json"


def _read(path: Path, parse) -> dict:
    """mtime-cached read so a long-running MCP server sees new keys without restarting."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = parse(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    _cache[str(path)] = (mtime, data)
    return data


def _parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def get(name: str) -> str | None:
    """Environment first, then the user key file."""
    return os.environ.get(name) or _read(_keys_file(), _parse_env).get(name)


def save(name: str, value: str) -> None:
    path = _keys_file()
    data = dict(_read(path, _parse_env))
    data[name] = value.strip()
    path.write_text("# tokengate API keys (user-only file). Managed by `tokengate setup`.\n"
                    + "".join(f"{k}={v}\n" for k, v in sorted(data.items())), "utf-8")
    try:
        os.chmod(path, 0o600)  # no-op on Windows; the file is under the user's profile there
    except OSError:
        pass


def remove(name: str) -> None:
    path = _keys_file()
    data = {k: v for k, v in _read(path, _parse_env).items() if k != name}
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(data.items())), "utf-8")


def models(provider: str) -> list[str] | None:
    """Models picked by `tokengate setup` for this provider (validated against the live list)."""
    return _read(_models_file(), json.loads).get(provider)


def save_models(provider: str, picked: list[str]) -> None:
    path = _models_file()
    data = dict(_read(path, json.loads))
    data[provider] = picked
    path.write_text(json.dumps(data, indent=2), "utf-8")


def location() -> str:
    return str(_keys_file())
