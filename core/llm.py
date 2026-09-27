"""LLM gateway — real providers with safe, fast defaults.

Providers (set LLM_PROVIDER in .env):
  mock        no network; agents use deterministic templates (default, and the automatic fallback)
  groq        Groq Cloud (free tier)             OpenAI-compatible, default llama-3.3-70b-versatile
  gemini      Google AI Studio (free tier)       OpenAI-compatible endpoint, default gemini-2.5-flash
  openai      OpenAI                             default gpt-4o-mini
  openrouter  OpenRouter (many models)           OpenAI-compatible
  ollama      local model, no key, no internet   OpenAI-compatible at http://localhost:11434/v1
  anthropic   Anthropic Messages API             default claude-haiku-4-5-20251001
  compatible  any other OpenAI-compatible server (vLLM, LM Studio, Together ...) — set LLM_BASE_URL

Model names change over time: if a default is rejected, set LLM_MODEL to a current model of your provider.

What this layer guarantees
* JSON output (provider JSON mode when available, tolerant parsing otherwise)
* PII masking: account IDs become ACC_01... before any text leaves the process
* one shared HTTP connection pool, per-call timeout, retries with backoff on 429/5xx (honours Retry-After)
* a concurrency limit so parallel agents cannot trip provider rate limits
* an on-disk response cache (same prompt -> same answer, zero cost) for repeatable demos
* usage accounting: calls, tokens, latency, cache hits, failures — shown in /api/status and every report
The verdict is always computed by code; the LLM only writes and argues. If it fails, the caller
falls back to template text, so a live demo can never break because of the network.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import RUNTIME_DIR, Settings

PRESETS: dict[str, dict] = {
    "groq": {"key_env": "GROQ_API_KEY", "api": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
             "judge_model": "llama-3.3-70b-versatile", "json_mode": True, "key_required": True},
    "gemini": {"key_env": "GEMINI_API_KEY", "api": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "judge_model": "gemini-2.5-flash", "json_mode": True, "key_required": True,
               # 2.5 models "think" before answering and the thinking eats max_tokens: switch it off, keep headroom
               "extra_body": {"reasoning_effort": "none"}, "min_tokens": 1024},
    "openai": {"key_env": "OPENAI_API_KEY", "api": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "judge_model": "gpt-4o-mini", "json_mode": True, "key_required": True},
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "api": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "model": "meta-llama/llama-3.3-70b-instruct", "judge_model": "meta-llama/llama-3.3-70b-instruct",
                   "json_mode": True, "key_required": True},
    "ollama": {"api": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b-instruct",
               "judge_model": "qwen2.5:7b-instruct", "json_mode": True, "key_required": False},
    "compatible": {"api": "openai", "base_url": "http://localhost:8001/v1", "model": "local-model",
                   "judge_model": "local-model", "json_mode": True, "key_required": False},
    "anthropic": {"key_env": "ANTHROPIC_API_KEY", "api": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-haiku-4-5-20251001",
                  "judge_model": "claude-sonnet-5", "json_mode": False, "key_required": True},
}

CACHE_DIR = RUNTIME_DIR / "llm_cache"


class LLMError(RuntimeError):
    pass


class Masker:
    """Replaces real account IDs with neutral tokens and back."""

    def __init__(self, accounts: list[str]) -> None:
        self.fwd = {a: f"ACC_{i:02d}" for i, a in enumerate(sorted(accounts, key=lambda x: (-len(x), x)), 1)}
        self.back = {v: k for k, v in self.fwd.items()}
        if self.fwd:
            self._fwd_re = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(map(re.escape, self.fwd)) + r")(?![A-Za-z0-9_-])")
        self._back_re = re.compile(r"ACC_\d{2}")

    def mask(self, text: str) -> str:
        return self._fwd_re.sub(lambda m: self.fwd[m.group(1)], text) if self.fwd else text

    def unmask(self, text: str) -> str:
        return self._back_re.sub(lambda m: self.back.get(m.group(0), m.group(0)), text)

    def unmask_obj(self, obj):
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, list):
            return [self.unmask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {self.unmask(k): self.unmask_obj(v) for k, v in obj.items()}
        return obj


class LLM:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.s = settings
        self._transport = transport  # tests inject a fake provider here
        provider = settings.llm_provider
        if provider == "openai" and settings.llm_base_url and "api.openai.com" not in settings.llm_base_url:
            provider = "compatible"  # backwards compatible: openai + custom base URL
        self.provider = provider
        self.preset = PRESETS.get(provider, {})
        self.api_key = settings.llm_api_key or os.getenv(self.preset.get("key_env", "") or "_NONE_", "")
        self.base_url = (settings.llm_base_url or self.preset.get("base_url", "")).rstrip("/")
        self.model = settings.llm_model or self.preset.get("model", "")
        # Judge: LLM_JUDGE_MODEL, else your LLM_MODEL, else the preset's (possibly stronger) judge model
        self.judge_model = (settings.llm_judge_model or (settings.llm_model and self.model)
                            or self.preset.get("judge_model", "") or self.model)
        self._json_mode = self.preset.get("json_mode", True)
        self._extra = dict(self.preset.get("extra_body", {}))
        self._min_tokens = int(self.preset.get("min_tokens", 0))
        self._client: Optional[httpx.AsyncClient] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "retries": 0, "input_tokens": 0,
                      "output_tokens": 0, "latency_s": 0.0, "last_error": ""}

    # ---------- status ----------
    @property
    def enabled(self) -> bool:
        if self.provider == "mock" or not self.preset:
            return False
        return bool(self.api_key) or not self.preset.get("key_required", True)

    @property
    def calls(self) -> int:  # kept for older callers
        return self.stats["calls"]

    @property
    def total_tokens(self) -> int:
        return self.stats["input_tokens"] + self.stats["output_tokens"]

    def describe(self) -> dict:
        n = max(self.stats["calls"], 1)
        return {"provider": self.provider, "enabled": self.enabled, "model": self.model if self.enabled else "template",
                "judge_model": self.judge_model if self.enabled else "template", "base_url": self.base_url,
                **self.stats, "avg_latency_s": round(self.stats["latency_s"] / n, 2)}

    # ---------- plumbing ----------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.s.llm_timeout, connect=10),
                                             limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
                                             transport=self._transport)
        return self._client

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.s.llm_concurrency)
        return self._sem

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _cache_path(self, model: str, system: str, user: str, max_tokens: int) -> Path:
        key = hashlib.sha256(f"{self.provider}|{model}|{max_tokens}|{system}|{user}".encode()).hexdigest()
        return CACHE_DIR / f"{key[:2]}/{key}.json"

    # ---------- public ----------
    async def json(self, system: str, user: str, model: Optional[str] = None, max_tokens: int = 700) -> dict:
        if not self.enabled:
            raise LLMError("LLM disabled")
        model = model or self.model
        cache = self._cache_path(model, system, user, max_tokens) if self.s.llm_cache else None
        if cache and cache.exists():
            self.stats["cache_hits"] += 1
            return json.loads(cache.read_text(encoding="utf-8"))
        async with self._semaphore():
            start = time.perf_counter()
            try:
                text = await self._with_retries(system, user, model, max_tokens)
            except LLMError as exc:
                self.stats["failures"] += 1
                self.stats["last_error"] = str(exc)[:300]
                raise
            finally:
                self.stats["latency_s"] = round(self.stats["latency_s"] + time.perf_counter() - start, 3)
        self.stats["calls"] += 1
        out = _parse_json(text)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    async def _with_retries(self, system: str, user: str, model: str, max_tokens: int) -> str:
        last = ""
        for attempt in range(self.s.llm_retries + 1):
            try:
                if self.preset["api"] == "anthropic":
                    return await self._anthropic(system, user, model, max_tokens)
                return await self._openai(system, user, model, max_tokens)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                last = f"HTTP {code}: {exc.response.text[:200]}"
                if code == 400 and self._json_mode and "response_format" in exc.response.text:
                    self._json_mode = False  # model/server without JSON mode: retry with prompt-only JSON
                    continue
                if code not in (408, 409, 429, 500, 502, 503, 504, 529):
                    break
                wait = _retry_after(exc.response) or min(8.0, 0.8 * 2 ** attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                wait = min(8.0, 0.8 * 2 ** attempt)
            if attempt < self.s.llm_retries:
                self.stats["retries"] += 1
                await asyncio.sleep(wait)
        raise LLMError(last or "LLM call failed")

    async def _openai(self, system: str, user: str, model: str, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": model, "temperature": 0, "max_tokens": max(max_tokens, self._min_tokens),
                "messages": [{"role": "system", "content": system + "\nReply with one JSON object only."},
                             {"role": "user", "content": user}], **self._extra}
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self._http().post(self.base_url + "/chat/completions", headers=headers, json=body)
        if r.status_code == 400 and self._extra:  # model does not accept the extra options: drop them, retry once
            for k in self._extra:
                body.pop(k, None)
            self._extra = {}
            r = await self._http().post(self.base_url + "/chat/completions", headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        self.stats["input_tokens"] += usage.get("prompt_tokens", 0)
        self.stats["output_tokens"] += usage.get("completion_tokens", 0)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected response: {str(data)[:200]}") from exc
        if not content.strip():
            raise LLMError(f"Empty answer (finish_reason={choice.get('finish_reason')}); a 'thinking' model may have "
                           "used the whole token budget — raise max_tokens or turn thinking off")
        return content

    async def _anthropic(self, system: str, user: str, model: str, max_tokens: int) -> str:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "system": system + "\nReply with one JSON object only.",
                "messages": [{"role": "user", "content": user}]}
        r = await self._http().post(self.base_url + "/v1/messages", headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        self.stats["input_tokens"] += usage.get("input_tokens", 0)
        self.stats["output_tokens"] += usage.get("output_tokens", 0)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text


def _retry_after(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("retry-after")
    try:
        return min(20.0, float(value)) if value else None
    except ValueError:
        return None


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < 0:
        raise LLMError("No JSON object in model output")
    try:
        out = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"Invalid JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise LLMError("JSON output is not an object")
    return out
