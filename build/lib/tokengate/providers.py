"""OpenAI-compatible chat clients (NVIDIA NIM, Ollama, anything else) with rate limiting.

stdlib only: urllib + threading.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

from .config import Provider


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = False, fatal_for_model: bool = False):
        super().__init__(msg)
        self.retryable = retryable
        self.fatal_for_model = fatal_for_model


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int


class RateLimiter:
    """Sliding-window limiter: at most `rpm` calls per 60s, `parallel` at once."""

    def __init__(self, rpm: int, parallel: int):
        self.rpm = max(1, rpm)
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()
        self.sem = threading.BoundedSemaphore(max(1, parallel))

    def __enter__(self):
        self.sem.acquire()
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    return self
                wait = 60 - (now - self.calls[0]) + 0.05
            time.sleep(wait)

    def __exit__(self, *exc):
        self.sem.release()


class Client:
    _limiters: dict[str, RateLimiter] = {}
    _dead_models: set[str] = set()   # 404 / not entitled; skipped for the rest of the process
    _down_until: dict[str, float] = {}  # provider unreachable -> skip for a while

    def __init__(self, provider: Provider):
        self.p = provider
        self.limiter = self._limiters.setdefault(provider.name, RateLimiter(provider.rpm, provider.max_parallel))

    @property
    def usable(self) -> bool:
        return self.p.available and time.time() > self._down_until.get(self.p.name, 0)

    def models(self) -> list[str]:
        return [m for m in self.p.models if f"{self.p.name}/{m}" not in self._dead_models]

    def _request(self, path: str, body: dict | None = None, timeout: int | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.p.api_key:
            headers["Authorization"] = f"Bearer {self.p.api_key}"
        req = urllib.request.Request(
            self.p.url.rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.p.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 504):
                raise ProviderError(f"{e.code} {detail}", retryable=True) from e
            if e.code == 404:
                raise ProviderError(f"{e.code} {detail}", fatal_for_model=True) from e
            if e.code in (400, 413, 422):  # e.g. context too long: skip this model for this call only
                raise ProviderError(f"{e.code} {detail}") from e
            self._down_until[self.p.name] = time.time() + 3600  # 401/403: bad key / no credits
            raise ProviderError(f"{e.code} {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            self._down_until[self.p.name] = time.time() + 300
            raise ProviderError(f"unreachable: {e}") from e

    def list_models(self) -> list[str]:
        return sorted(m["id"] for m in self._request("/models", timeout=15).get("data", []))

    def chat(self, model: str, messages: list[dict], max_tokens: int = 2048,
             temperature: float = 0.2, retries: int = 3) -> Completion:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": False}
        for attempt in range(retries + 1):
            try:
                with self.limiter:
                    data = self._request("/chat/completions", body)
                choice = data["choices"][0]["message"]
                text = choice.get("content") or ""
                usage = data.get("usage") or {}
                return Completion(text, self.p.name, model,
                                  usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            except ProviderError as e:
                if e.fatal_for_model:
                    self._dead_models.add(f"{self.p.name}/{model}")
                    raise
                if not e.retryable or attempt == retries:
                    raise
                time.sleep(min(30, 2 ** attempt * 2) + random.random())
        raise ProviderError("unreachable")
