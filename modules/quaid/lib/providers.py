"""Provider ABCs and concrete implementations for LLM and embeddings.

Providers are the lowest-level abstraction for calling models.  Adapters
produce providers (e.g. OpenClawAdapter → AnthropicLLMProvider), but
providers also work standalone (e.g. OllamaEmbeddingsProvider).

Concrete providers shipped:

  LLM:
    AnthropicLLMProvider  — calls Anthropic Messages API directly (API key)
    ClaudeCodeLLMProvider — wraps ``claude -p`` CLI (uses subscription)
    TestLLMProvider       — canned responses for tests

  Embeddings:
    OllamaEmbeddingsProvider — HTTP call to local Ollama instance
    MockEmbeddingsProvider   — deterministic MD5 vectors for tests
"""

import abc
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lib.fail_policy import is_fail_hard_enabled
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class LLMResult:
    """Result from an LLM call."""
    text: Optional[str]
    duration: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str = ""
    truncated: bool = False
    # Per-model token breakdown (for ClaudeCodeLLMProvider which may report
    # usage across multiple models in a single call).
    model_usage: Dict[str, Dict[str, int]] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════
# Model tier table — maps known providers to deep/fast reasoning models
# ═══════════════════════════════════════════════════════════════════════

MODEL_TIERS: Dict[str, Dict[str, object]] = {
    "anthropic": {
        "deep": ["claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-4-5"],
        "fast": ["claude-haiku-4-5"],
        "default_deep": "claude-opus-4-6",
        "default_fast": "claude-haiku-4-5",
    },
    "openai": {
        "deep": ["gpt-5", "gpt-4o", "o4-mini"],
        "fast": ["gpt-4o-mini"],
        "default_deep": "gpt-4o",
        "default_fast": "gpt-4o-mini",
    },
}


# ═══════════════════════════════════════════════════════════════════════
# LLM Provider ABC
# ═══════════════════════════════════════════════════════════════════════

class LLMProvider(abc.ABC):
    """Abstract LLM provider. Produced by adapters or standalone."""

    @abc.abstractmethod
    def llm_call(self, messages: list, model_tier: str = "deep",
                 max_tokens: int = 4000, timeout: float = 600) -> LLMResult:
        """Make an LLM call.

        Args:
            messages: List of dicts with 'role' and 'content' keys.
                      Roles: 'system', 'user'.
            model_tier: 'deep' (quality/slow) or 'fast' (cheap/fast).
            max_tokens: Maximum output tokens.
            timeout: Request timeout in seconds.

        Returns:
            LLMResult with response text and usage metadata.
        """
        ...

    @abc.abstractmethod
    def get_profiles(self) -> dict:
        """Return model profiles for each tier.

        Returns:
            {"deep": {"model": "...", "available": bool},
             "fast": {"model": "...", "available": bool}}
        """
        ...


# ═══════════════════════════════════════════════════════════════════════
# Embeddings Provider ABC
# ═══════════════════════════════════════════════════════════════════════

class EmbeddingsProvider(abc.ABC):
    """Abstract embeddings provider. Produced by adapters or standalone."""

    @abc.abstractmethod
    def embed(self, text: str) -> Optional[List[float]]:
        """Generate an embedding vector for the given text.

        Returns:
            List of floats (embedding vector) or None on failure.
        """
        ...

    @abc.abstractmethod
    def dimension(self) -> int:
        """Return the embedding vector dimension."""
        ...

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Return the model name used for embeddings."""
        ...


# ═══════════════════════════════════════════════════════════════════════
# Concrete LLM Providers
# ═══════════════════════════════════════════════════════════════════════

class AnthropicLLMProvider(LLMProvider):
    """Calls the Anthropic Messages API directly with an API key.

    Used by OpenClawAdapter and any installation that has an API key.
    Supports prompt caching via cache_control on the system block.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, api_key: str = "",
                 deep_model: str = "claude-opus-4-6",
                 fast_model: str = "claude-haiku-4-5",
                 base_url: str = ""):
        self._api_key = api_key
        self._deep_model = deep_model
        self._fast_model = fast_model
        self._base_url = base_url or self.ANTHROPIC_API_URL

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        model = self._resolve_model(model_tier)
        system_prompt = ""
        user_message = ""
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            elif m["role"] == "user":
                user_message = m["content"]

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_message}],
        }

        start_time = time.time()
        try:
            data_bytes = json.dumps(body).encode()
            req = urllib.request.Request(self._base_url, data=data_bytes,
                                        headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"Anthropic API returned non-object JSON for model={model}: {type(data).__name__}"
                    )
                duration = time.time() - start_time

                usage = data.get("usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_create = usage.get("cache_creation_input_tokens", 0)
                truncated = data.get("stop_reason", "") == "max_tokens"

                content_blocks = data.get("content", [])
                if not isinstance(content_blocks, list):
                    raise RuntimeError(
                        f"Anthropic API returned invalid content payload for model={model}"
                    )
                text_parts = [
                    b["text"]
                    for b in content_blocks
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                ]
                text = "\n".join(text_parts).strip()

                return LLMResult(
                    text=text,
                    duration=duration,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_create,
                    model=data.get("model", model),
                    truncated=truncated,
                )
        except Exception as e:
            duration = time.time() - start_time
            logger.error("Anthropic API error: %s", e)
            raise

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": bool(self._api_key)},
            "fast": {"model": self._fast_model, "available": bool(self._api_key)},
        }


class ClaudeCodeLLMProvider(LLMProvider):
    """Wraps the ``claude -p`` CLI.  Uses the user's Claude Code subscription."""

    # Map full model IDs to claude CLI aliases
    _MODEL_ALIASES = {
        "claude-opus-4-6": "opus",
        "claude-opus-4-5": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-sonnet-4-5": "sonnet",
        "claude-haiku-4-5": "haiku",
        "claude-haiku-4-5-20251001": "haiku",
    }

    def __init__(self, deep_model: str = "claude-opus-4-6",
                 fast_model: str = "claude-haiku-4-5"):
        self._deep_model = deep_model
        self._fast_model = fast_model

    def _resolve_alias(self, model_tier: str) -> str:
        model = self._fast_model if model_tier == "fast" else self._deep_model
        return self._MODEL_ALIASES.get(model, model)

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        # Allow environment overrides for claude-code subprocess timeout.
        # Useful for benchmark/LoCoMo lanes where fast-tier dedup/review calls
        # may exceed conservative defaults.
        try:
            global_timeout = float(os.environ.get("CLAUDE_CODE_TIMEOUT_S", "0") or 0)
        except ValueError:
            global_timeout = 0.0
        try:
            fast_timeout = float(os.environ.get("CLAUDE_CODE_FAST_TIMEOUT_S", "0") or 0)
        except ValueError:
            fast_timeout = 0.0
        try:
            deep_timeout = float(os.environ.get("CLAUDE_CODE_DEEP_TIMEOUT_S", "0") or 0)
        except ValueError:
            deep_timeout = 0.0

        effective_timeout = float(timeout)
        if global_timeout > 0:
            effective_timeout = max(effective_timeout, global_timeout)
        if model_tier == "fast" and fast_timeout > 0:
            effective_timeout = max(effective_timeout, fast_timeout)
        if model_tier != "fast" and deep_timeout > 0:
            effective_timeout = max(effective_timeout, deep_timeout)

        model_alias = self._resolve_alias(model_tier)
        system_prompt = ""
        user_message = ""
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            elif m["role"] == "user":
                user_message = m["content"]

        cmd = [
            "claude", "-p",
            "--model", model_alias,
            "--output-format", "json",
            "--no-session-persistence",
            "--max-turns", "1",
            "--system-prompt", system_prompt,
            user_message,
        ]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Allow nested invocation

        # Ensure OAuth token is available (load from .env only when fail-hard is disabled)
        if "CLAUDE_CODE_OAUTH_TOKEN" not in env and not is_fail_hard_enabled():
            print(
                "[providers][FALLBACK] CLAUDE_CODE_OAUTH_TOKEN not present in env; "
                "attempting ~/.quaid/.env fallback because failHard is disabled.",
                file=sys.stderr,
            )
            adapter_env_path = ""
            try:
                # Lazy import to avoid module-level circular dependency.
                from lib.adapter import get_adapter
                adapter_env_path = str(get_adapter().quaid_home() / ".env")
            except Exception:
                adapter_env_path = ""
            for env_path in [adapter_env_path]:
                if env_path and os.path.isfile(env_path):
                    try:
                        with open(env_path) as f:
                            for line in f:
                                if line.strip().startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                                    env["CLAUDE_CODE_OAUTH_TOKEN"] = line.strip().split("=", 1)[1].strip()
                                    print(
                                        f"[providers][FALLBACK] Loaded CLAUDE_CODE_OAUTH_TOKEN from {env_path}.",
                                        file=sys.stderr,
                                    )
                                    break
                    except OSError:
                        print(
                            f"[providers][FALLBACK] Failed reading {env_path} for CLAUDE_CODE_OAUTH_TOKEN.",
                            file=sys.stderr,
                        )
                    if "CLAUDE_CODE_OAUTH_TOKEN" in env:
                        break
        if "CLAUDE_CODE_OAUTH_TOKEN" not in env and is_fail_hard_enabled():
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN is required while failHard is enabled; "
                "fallback token loading is disabled."
            )

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=effective_timeout, env=env,
                cwd="/tmp",  # Avoid loading CLAUDE.md project context
            )
            duration = time.time() - start_time

            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"Claude Code failed (rc={result.returncode}) for tier={model_tier}, "
                    f"model={model_alias}: {err[:300]}"
                )

            try:
                data = json.loads(result.stdout)
            except (json.JSONDecodeError, ValueError) as e:
                raise RuntimeError(
                    f"Claude Code returned non-JSON output for tier={model_tier}, "
                    f"model={model_alias}: {result.stdout[:300]}"
                ) from e
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Claude Code returned non-object JSON for tier={model_tier}, "
                    f"model={model_alias}"
                )
            raw_text = data.get("result")
            if raw_text is None:
                raise RuntimeError(
                    f"Claude Code response missing result for tier={model_tier}, "
                    f"model={model_alias}"
                )
            if not isinstance(raw_text, str):
                raise RuntimeError(
                    f"Claude Code returned non-string result for tier={model_tier}, "
                    f"model={model_alias}"
                )
            text = raw_text.strip()

            # Collect per-model usage
            model_usage: Dict[str, Dict[str, int]] = {}
            total_in = 0
            total_out = 0
            raw_model_usage = data.get("modelUsage", {})
            if not isinstance(raw_model_usage, dict):
                raw_model_usage = {}
            for _m, u in raw_model_usage.items():
                if not isinstance(u, dict):
                    continue
                in_tok = (u.get("inputTokens", 0)
                          + u.get("cacheReadInputTokens", 0)
                          + u.get("cacheCreationInputTokens", 0))
                out_tok = u.get("outputTokens", 0)
                model_usage[_m] = {"input": in_tok, "output": out_tok}
                total_in += in_tok
                total_out += out_tok

            return LLMResult(
                text=text,
                duration=duration,
                input_tokens=total_in,
                output_tokens=total_out,
                model=model_alias,
                model_usage=model_usage,
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            logger.error("Claude Code timed out after %.1fs", duration)
            raise
        except Exception as e:
            duration = time.time() - start_time
            logger.error("Claude Code error: %s", e)
            raise

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": True},
            "fast": {"model": self._fast_model, "available": True},
        }


class TestLLMProvider(LLMProvider):
    """Canned responses and call recording for tests."""
    __test__ = False  # Not a pytest test class

    def __init__(self, responses: Optional[Dict[str, str]] = None):
        self.calls: List[dict] = []
        self._responses = responses or {}
        self._default_response = '{"action": "KEEP", "reasoning": "test"}'

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        self.calls.append({
            "messages": messages,
            "model_tier": model_tier,
            "max_tokens": max_tokens,
        })
        text = self._responses.get(model_tier, self._default_response)
        return LLMResult(
            text=text,
            duration=0.01,
            input_tokens=100,
            output_tokens=50,
            model=f"test-{model_tier}",
        )

    def get_profiles(self):
        return {
            "deep": {"model": "test-deep", "available": True},
            "fast": {"model": "test-fast", "available": True},
        }


class OpenAICompatibleLLMProvider(LLMProvider):
    """Calls any OpenAI-compatible API (vLLM, Ollama chat, LiteLLM, etc.)."""

    def __init__(self, base_url: str = "http://localhost:8000",
                 api_key: str = "",
                 deep_model: str = "", fast_model: str = ""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._deep_model = deep_model
        self._fast_model = fast_model or deep_model

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        import time as _time
        model = self._resolve_model(model_tier)
        url = f"{self._base_url}/v1/chat/completions"

        # Convert messages to OpenAI format
        oai_messages = []
        for m in messages:
            oai_messages.append({"role": m["role"], "content": m["content"]})

        payload = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
        )

        t0 = _time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        elapsed = _time.time() - t0

        if not isinstance(data, dict):
            raise RuntimeError(f"OpenAI-compatible response must be a JSON object, got {type(data).__name__}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI-compatible response missing non-empty choices array")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            raise RuntimeError("OpenAI-compatible response missing choices[0].message.content")
        text = str(content)
        # Strip thinking tags (Qwen3 and similar models)
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()
        usage = data.get("usage", {})
        return LLMResult(
            text=text,
            duration=elapsed,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=model,
        )

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": True},
            "fast": {"model": self._fast_model, "available": True},
        }


# ═══════════════════════════════════════════════════════════════════════
# Concrete Embeddings Providers
# ═══════════════════════════════════════════════════════════════════════

class OllamaEmbeddingsProvider(EmbeddingsProvider):
    """Generates embeddings via a local Ollama instance."""

    def __init__(self, url: str = "http://localhost:11434",
                 model: str = "qwen3-embedding:8b", dim: int = 4096):
        self._url = url
        self._model = model
        self._dim = dim

    def embed(self, text):
        retries = 1
        last_error = None
        for attempt in range(retries + 1):
            try:
                data = json.dumps({
                    "model": self._model,
                    "input": text,
                    "keep_alive": -1,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{self._url}/api/embed",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    embeddings = result.get("embeddings", [])
                    if embeddings and embeddings[0]:
                        return embeddings[0]
                    return None
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as e:
                last_error = e
                if attempt < retries:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                break
            except Exception as e:
                last_error = e
                break
        if last_error is not None:
            e = last_error
            logger.error(
                "Ollama embeddings call failed provider=ollama model=%s url=%s text_len=%d error=%s",
                self._model,
                self._url,
                len(str(text or "")),
                e,
            )
            if is_fail_hard_enabled():
                raise RuntimeError(
                    f"Ollama embeddings provider failed while failHard is enabled: model={self._model}"
                ) from e
        return None

    def dimension(self):
        return self._dim

    @property
    def model_name(self):
        return self._model


class MockEmbeddingsProvider(EmbeddingsProvider):
    """Deterministic MD5-based embeddings for testing.  Returns 128-dim vectors."""

    def embed(self, text):
        h = hashlib.md5(text.encode()).digest()
        raw = [float(b) / 255.0 for b in h] * 8  # 16 bytes * 8 = 128-dim
        magnitude = sum(x * x for x in raw) ** 0.5
        return [x / magnitude for x in raw] if magnitude > 0 else raw

    def dimension(self):
        return 128

    @property
    def model_name(self):
        return "mock-md5"
