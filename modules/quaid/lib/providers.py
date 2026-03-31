"""Provider ABCs and concrete implementations for LLM and embeddings.

Providers are the lowest-level abstraction for calling models. Platform adapters
produce providers (e.g. host adapter → AnthropicLLMProvider), but
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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from lib.fail_policy import is_fail_hard_enabled
logger = logging.getLogger(__name__)

_ANTHROPIC_OAUTH_IDENTITY_TEXT = (
    "You are Claude Code, Anthropic's official CLI for Claude."
)
_ANTHROPIC_OAUTH_USER_AGENT = "claude-cli/2.1.2 (external, cli)"
_ANTHROPIC_OAUTH_CLAUDE_CODE_BETA = "claude-code-20250219"


def _sanitize_url_for_logs(url: str) -> str:
    """Redact credentials/query fragments from URLs before logging."""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"


def _is_anthropic_oauth_token(token: str) -> bool:
    return str(token or "").strip().startswith("sk-ant-oat")


def _anthropic_headers(credential: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": AnthropicLLMProvider.ANTHROPIC_VERSION,
    }
    betas = ["prompt-caching-2024-07-31"]
    if _is_anthropic_oauth_token(credential):
        headers["Authorization"] = f"Bearer {credential}"
        headers["Accept"] = "application/json"
        headers["user-agent"] = _ANTHROPIC_OAUTH_USER_AGENT
        headers["x-app"] = "cli"
        betas.append(_ANTHROPIC_OAUTH_CLAUDE_CODE_BETA)
        betas.append("oauth-2025-04-20")
    else:
        headers["x-api-key"] = credential
    headers["anthropic-beta"] = ",".join(betas)
    return headers


def _anthropic_system_blocks(system_prompt: str, credential: str) -> list:
    blocks = []
    if _is_anthropic_oauth_token(credential):
        blocks.append(
            {
                "type": "text",
                "text": _ANTHROPIC_OAUTH_IDENTITY_TEXT,
                "cache_control": {"type": "ephemeral"},
            }
        )
    blocks.append(
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    )
    return blocks


def _summarize_error_text(text: str, max_len: int = 300) -> str:
    """Return compact error summary preserving tail context."""
    msg = str(text or "").strip()
    if len(msg) <= max_len:
        return msg
    head = max_len // 3
    tail = max_len - head - 5
    return f"{msg[:head]} ... {msg[-tail:]}"


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
    """Calls the Anthropic Messages API directly with an API key or OAuth token."""

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

        if not user_message:
            raise ValueError("Cannot make API call with empty user message")

        headers = _anthropic_headers(self._api_key)

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": _anthropic_system_blocks(system_prompt, self._api_key),
            "messages": [{"role": "user", "content": user_message}],
        }

        retry_attempts = max(1, int(os.environ.get("ANTHROPIC_RETRY_ATTEMPTS", "3") or "3"))
        backoff_s = max(0.0, float(os.environ.get("ANTHROPIC_RETRY_BACKOFF_S", "2") or "2"))
        backoff_cap_s = max(backoff_s, float(os.environ.get("ANTHROPIC_RETRY_BACKOFF_CAP_S", "60") or "60"))
        retryable_http_codes = {408, 429, 500, 502, 503, 504, 529}
        start_time = time.time()

        for attempt in range(1, retry_attempts + 1):
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
            except urllib.error.HTTPError as e:
                body_preview = ""
                try:
                    body_preview = (e.read() or b"").decode("utf-8", errors="ignore")
                except Exception:
                    body_preview = ""
                body_preview = body_preview.replace("\n", " ")[:400]
                headers_lc = {}
                try:
                    headers_lc = {str(k).lower(): str(v) for k, v in (e.headers.items() if e.headers else [])}
                except Exception:
                    headers_lc = {}
                request_id = (
                    headers_lc.get("anthropic-request-id")
                    or headers_lc.get("request-id")
                    or headers_lc.get("x-request-id")
                    or ""
                )
                ratelimit_headers = {
                    k: v for k, v in headers_lc.items() if "ratelimit" in k
                }
                retriable = getattr(e, "code", None) in retryable_http_codes
                if retriable and attempt < retry_attempts:
                    delay = min(backoff_cap_s, backoff_s * (2 ** (attempt - 1)))
                    logger.warning(
                        "Anthropic API retryable HTTPError code=%s model=%s tier=%s "
                        "attempt=%s/%s request_id=%s ratelimit=%s delay=%.1fs",
                        getattr(e, "code", "unknown"),
                        model,
                        model_tier,
                        attempt,
                        retry_attempts,
                        request_id,
                        ratelimit_headers,
                        delay,
                    )
                    if delay > 0:
                        time.sleep(delay)
                    continue
                logger.error(
                    "Anthropic API HTTPError code=%s model=%s tier=%s request_id=%s "
                    "ratelimit=%s body_preview=%s",
                    getattr(e, "code", "unknown"),
                    model,
                    model_tier,
                    request_id,
                    ratelimit_headers,
                    body_preview,
                )
                raise RuntimeError(
                    "Anthropic API HTTPError "
                    f"code={getattr(e, 'code', 'unknown')} "
                    f"model={model} tier={model_tier} request_id={request_id} "
                    f"ratelimit={ratelimit_headers} body={body_preview}"
                ) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < retry_attempts:
                    delay = min(backoff_cap_s, backoff_s * (2 ** (attempt - 1)))
                    logger.warning(
                        "Anthropic API transient error=%s model=%s tier=%s attempt=%s/%s delay=%.1fs",
                        type(e).__name__,
                        model,
                        model_tier,
                        attempt,
                        retry_attempts,
                        delay,
                    )
                    if delay > 0:
                        time.sleep(delay)
                    continue
                logger.error("Anthropic API error: %s", e)
                raise
            except Exception as e:
                logger.error("Anthropic API error: %s", e)
                raise

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": bool(self._api_key)},
            "fast": {"model": self._fast_model, "available": bool(self._api_key)},
        }


class ClaudeCodeLLMProvider(LLMProvider):
    """Wraps the ``claude -p`` CLI.  Uses the user's Claude Code subscription.

    DEPRECATED: Prefer ``adaptors.claude_code.providers.ClaudeCodeOAuthLLMProvider``
    which uses direct API calls with 3-layer auth fallback (OAuth env → on-disk
    token → API key). This subprocess provider is slower and has CC-specific token
    loading logic that should not live in shared ``lib/``. Kept for backward
    compatibility until all callers migrate.
    """

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

    @staticmethod
    def _malformed_retry_budget() -> int:
        try:
            return max(0, int(os.environ.get("CLAUDE_CODE_MALFORMED_RETRIES", "2") or "2"))
        except ValueError:
            return 2

    @staticmethod
    def _malformed_retry_budget_for_tier(model_tier: str) -> int:
        if model_tier != "fast":
            try:
                return max(
                    0,
                    int(
                        os.environ.get(
                            "CLAUDE_CODE_DEEP_MALFORMED_RETRIES",
                            os.environ.get("CLAUDE_CODE_MALFORMED_RETRIES", "4") or "4",
                        )
                    ),
                )
            except ValueError:
                return 4
        return ClaudeCodeLLMProvider._malformed_retry_budget()

    @staticmethod
    def _malformed_retry_delay() -> float:
        try:
            return max(0.0, float(os.environ.get("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "1.0") or "1.0"))
        except ValueError:
            return 1.0

    @staticmethod
    def _malformed_retry_delay_for_tier(model_tier: str) -> float:
        if model_tier != "fast":
            try:
                return max(
                    0.0,
                    float(
                        os.environ.get(
                            "CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S",
                            os.environ.get("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "2.0") or "2.0",
                        )
                    ),
                )
            except ValueError:
                return 2.0
        return ClaudeCodeLLMProvider._malformed_retry_delay()

    @staticmethod
    def _malformed_retry_sleep(base_delay: float, attempt: int, model_tier: str) -> float:
        if base_delay <= 0:
            return 0.0
        if model_tier == "fast":
            return base_delay
        return min(base_delay * (2 ** max(0, attempt)), 15.0)

    @staticmethod
    def _malformed_payload_detail(result: subprocess.CompletedProcess, max_len: int = 240) -> str:
        stdout_tail = _summarize_error_text((result.stdout or "").strip(), max_len)
        stderr_tail = _summarize_error_text((result.stderr or "").strip(), max_len)
        parts = []
        if stdout_tail:
            parts.append(f"stdout={stdout_tail}")
        if stderr_tail:
            parts.append(f"stderr={stderr_tail}")
        return " ".join(parts) if parts else "stdout=<empty> stderr=<empty>"

    @staticmethod
    def _parse_result_payload(text: str) -> Optional[dict]:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _should_text_fallback(data: dict) -> bool:
        return (
            isinstance(data, dict)
            and data.get("type") == "result"
            and data.get("subtype") == "success"
            and not bool(data.get("is_error"))
        )

    @staticmethod
    def _should_retry_error_payload(data: Optional[dict]) -> bool:
        return (
            isinstance(data, dict)
            and data.get("type") == "result"
            and data.get("subtype") == "success"
            and bool(data.get("is_error"))
        )

    @staticmethod
    def _text_fallback_cmd(cmd: list[str]) -> list[str]:
        fallback = list(cmd)
        if "--output-format" in fallback:
            idx = fallback.index("--output-format")
            del fallback[idx:idx + 2]
        return fallback

    @staticmethod
    def _command_cmd_for_attempt(cmd: list[str], command_attempt: int) -> list[str]:
        # After the first malformed command attempt, switch to text mode.
        # Claude Code sometimes emits success payloads with an empty/missing
        # `result` field in JSON mode while the same request succeeds in text mode.
        if command_attempt <= 0:
            return list(cmd)
        return ClaudeCodeLLMProvider._text_fallback_cmd(cmd)

    def _resolve_alias(self, model_tier: str) -> str:
        model = self._fast_model if model_tier == "fast" else self._deep_model
        return self._MODEL_ALIASES.get(model, model)

    @staticmethod
    def _max_turns_for_tier(model_tier: str) -> int:
        if model_tier == "fast":
            key = "CLAUDE_CODE_FAST_MAX_TURNS"
            default = "1"
        else:
            key = "CLAUDE_CODE_DEEP_MAX_TURNS"
            default = "2"
        try:
            return max(1, int(os.environ.get(key, default) or default))
        except ValueError:
            return int(default)

    @staticmethod
    def _command_retry_budget_for_tier(model_tier: str) -> int:
        if model_tier != "fast":
            try:
                return max(
                    0,
                    int(
                        os.environ.get(
                            "CLAUDE_CODE_DEEP_COMMAND_RETRIES",
                            os.environ.get("CLAUDE_CODE_COMMAND_RETRIES", "4") or "4",
                        )
                    ),
                )
            except ValueError:
                return 4
        try:
            return max(0, int(os.environ.get("CLAUDE_CODE_COMMAND_RETRIES", "0") or "0"))
        except ValueError:
            return 0

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        # Allow environment overrides for claude-code subprocess timeout.
        # Useful in slower or heavily loaded environments where fast-tier
        # dedup/review calls may exceed conservative defaults.
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
        try:
            timeout_cap = float(os.environ.get("CLAUDE_CODE_TIMEOUT_CAP_S", "0") or 0)
        except ValueError:
            timeout_cap = 0.0
        try:
            timeout_multiplier = float(os.environ.get("CLAUDE_CODE_TIMEOUT_MULTIPLIER", "1") or 1)
        except ValueError:
            timeout_multiplier = 1.0
        if timeout_multiplier <= 0:
            timeout_multiplier = 1.0

        effective_timeout = float(timeout)
        if global_timeout > 0:
            effective_timeout = max(effective_timeout, global_timeout)
        if model_tier == "fast" and fast_timeout > 0:
            effective_timeout = max(effective_timeout, fast_timeout)
        if model_tier != "fast":
            if deep_timeout <= 0:
                # Deep Claude Code calls regularly cover long review/distillation
                # windows. Keep the floor high enough that lifecycle tasks do not
                # fail on normal long-running responses.
                deep_timeout = 600.0
            effective_timeout = max(effective_timeout, deep_timeout)
        effective_timeout *= timeout_multiplier
        if timeout_cap > 0:
            effective_timeout = min(effective_timeout, timeout_cap)

        model_alias = self._resolve_alias(model_tier)
        max_turns = self._max_turns_for_tier(model_tier)
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
            "--max-turns", str(max_turns),
            "--system-prompt", system_prompt,
        ]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Allow nested invocation
        # Claude CLI auth hygiene:
        # If Anthropic key/token env vars leak into this subprocess, claude -p can
        # switch away from subscription auth and hit the wrong workspace limits.
        # For claude-code provider, always scrub these and rely on CLI auth state.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)

        # Ensure OAuth token is available (load from .env only when fail-hard is disabled)
        if "CLAUDE_CODE_OAUTH_TOKEN" not in env and not is_fail_hard_enabled():
            logger.warning(
                "CLAUDE_CODE_OAUTH_TOKEN missing for ClaudeCode provider; "
                "attempting adapter .env fallback because failHard is disabled "
                "(tier=%s model=%s).",
                model_tier,
                model_alias,
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
                                    logger.warning(
                                        "Loaded CLAUDE_CODE_OAUTH_TOKEN from adapter env fallback path=%s "
                                        "(tier=%s model=%s).",
                                        env_path,
                                        model_tier,
                                        model_alias,
                                    )
                                    break
                    except OSError:
                        logger.error(
                            "Failed reading adapter env fallback path=%s for CLAUDE_CODE_OAUTH_TOKEN "
                            "(tier=%s model=%s).",
                            env_path,
                            model_tier,
                            model_alias,
                        )
                    if "CLAUDE_CODE_OAUTH_TOKEN" in env:
                        break
        if "CLAUDE_CODE_OAUTH_TOKEN" not in env and is_fail_hard_enabled():
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN is required while failHard is enabled; "
                "fallback token loading is disabled."
            )

        start_time = time.time()
        malformed_retries = self._malformed_retry_budget_for_tier(model_tier)
        malformed_delay = self._malformed_retry_delay_for_tier(model_tier)
        command_retries = self._command_retry_budget_for_tier(model_tier)
        try:
            for command_attempt in range(command_retries + 1):
                active_cmd = self._command_cmd_for_attempt(cmd, command_attempt)
                for attempt in range(malformed_retries + 1):
                    result = subprocess.run(
                        active_cmd, capture_output=True, text=True,
                        timeout=effective_timeout, env=env,
                        input=user_message,
                        cwd="/tmp",  # Avoid loading CLAUDE.md project context
                    )
                    duration = time.time() - start_time

                    if result.returncode != 0:
                        err = (result.stderr or result.stdout or "").strip()
                        err_payload = self._parse_result_payload(result.stderr or result.stdout or "")
                        if self._should_retry_error_payload(err_payload) and command_attempt < command_retries:
                            sleep_s = self._malformed_retry_sleep(
                                malformed_delay, command_attempt, model_tier
                            )
                            logger.warning(
                                "Claude Code returned retryable error payload; rerunning command %s/%s "
                                "(tier=%s model=%s, sleep=%.1fs, subtype=%s, %s).",
                                command_attempt + 1,
                                command_retries + 1,
                                model_tier,
                                model_alias,
                                sleep_s,
                                err_payload.get("subtype"),
                                self._malformed_payload_detail(result),
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            break
                        raise RuntimeError(
                            f"Claude Code failed (rc={result.returncode}) for tier={model_tier}, "
                            f"model={model_alias}: {_summarize_error_text(err, 300)}"
                        )

                    try:
                        data = json.loads(result.stdout)
                    except (json.JSONDecodeError, ValueError) as e:
                        if result.returncode == 0 and not (result.stdout or "").strip() and not (result.stderr or "").strip():
                            if command_attempt < command_retries:
                                sleep_s = self._malformed_retry_sleep(
                                    malformed_delay, command_attempt, model_tier
                                )
                                logger.warning(
                                    "Claude Code returned completely empty output; rerunning command %s/%s "
                                    "(tier=%s model=%s, sleep=%.1fs).",
                                    command_attempt + 1,
                                    command_retries + 1,
                                    model_tier,
                                    model_alias,
                                    sleep_s,
                                )
                                if sleep_s > 0:
                                    time.sleep(sleep_s)
                                break
                        if command_attempt > 0:
                            fallback_text = (result.stdout or "").strip()
                            if result.returncode == 0 and fallback_text:
                                return LLMResult(
                                    text=fallback_text,
                                    duration=time.time() - start_time,
                                    input_tokens=0,
                                    output_tokens=0,
                                    model=model_alias,
                                    model_usage={},
                                )
                            if command_attempt < command_retries:
                                sleep_s = self._malformed_retry_sleep(
                                    malformed_delay, command_attempt, model_tier
                                )
                                logger.warning(
                                    "Claude Code returned empty text-mode output; rerunning command %s/%s "
                                    "(tier=%s model=%s, sleep=%.1fs, %s).",
                                    command_attempt + 1,
                                    command_retries + 1,
                                    model_tier,
                                    model_alias,
                                    sleep_s,
                                    self._malformed_payload_detail(result),
                                )
                                if sleep_s > 0:
                                    time.sleep(sleep_s)
                                break
                        if attempt < malformed_retries:
                            sleep_s = self._malformed_retry_sleep(malformed_delay, attempt, model_tier)
                            logger.warning(
                                "Claude Code returned non-JSON output; retrying malformed response %s/%s "
                                "(tier=%s model=%s, sleep=%.1fs, %s).",
                                attempt + 1,
                                malformed_retries + 1,
                                model_tier,
                                model_alias,
                                sleep_s,
                                self._malformed_payload_detail(result),
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            continue
                        raise RuntimeError(
                            f"Claude Code returned non-JSON output for tier={model_tier}, "
                            f"model={model_alias}: {self._malformed_payload_detail(result, 300)}"
                        ) from e
                    if not isinstance(data, dict):
                        if attempt < malformed_retries:
                            sleep_s = self._malformed_retry_sleep(malformed_delay, attempt, model_tier)
                            logger.warning(
                                "Claude Code returned non-object JSON; retrying malformed response %s/%s "
                                "(tier=%s model=%s, sleep=%.1fs, %s).",
                                attempt + 1,
                                malformed_retries + 1,
                                model_tier,
                                model_alias,
                                sleep_s,
                                self._malformed_payload_detail(result),
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            continue
                        raise RuntimeError(
                            f"Claude Code returned non-object JSON for tier={model_tier}, "
                            f"model={model_alias}: {self._malformed_payload_detail(result, 300)}"
                        )
                    raw_text = data.get("result")
                    if raw_text is None:
                        should_fallback = self._should_text_fallback(data)
                        if attempt < malformed_retries:
                            sleep_s = self._malformed_retry_sleep(malformed_delay, attempt, model_tier)
                            logger.warning(
                                "Claude Code response missing result; retrying malformed response %s/%s "
                                "(tier=%s model=%s, sleep=%.1fs, %s).",
                                attempt + 1,
                                malformed_retries + 1,
                                model_tier,
                                model_alias,
                                sleep_s,
                                self._malformed_payload_detail(result),
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            continue
                        if should_fallback:
                            logger.warning(
                                "Claude Code response missing result in success payload; retrying once via plain text "
                                "(tier=%s model=%s).",
                                model_tier,
                                model_alias,
                            )
                            fallback_result = subprocess.run(
                                self._text_fallback_cmd(cmd),
                                capture_output=True,
                                text=True,
                                timeout=effective_timeout,
                                env=env,
                                input=user_message,
                                cwd="/tmp",
                            )
                            if fallback_result.returncode == 0:
                                fallback_text = (fallback_result.stdout or "").strip()
                                if fallback_text:
                                    return LLMResult(
                                        text=fallback_text,
                                        duration=time.time() - start_time,
                                        input_tokens=0,
                                        output_tokens=0,
                                        model=model_alias,
                                        model_usage={},
                                    )
                    elif not isinstance(raw_text, str):
                        if attempt < malformed_retries:
                            sleep_s = self._malformed_retry_sleep(malformed_delay, attempt, model_tier)
                            logger.warning(
                                "Claude Code returned non-string result; retrying malformed response %s/%s "
                                "(tier=%s model=%s, sleep=%.1fs, %s).",
                                attempt + 1,
                                malformed_retries + 1,
                                model_tier,
                                model_alias,
                                sleep_s,
                                self._malformed_payload_detail(result),
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                            continue
                        raise RuntimeError(
                            f"Claude Code returned non-string result for tier={model_tier}, "
                            f"model={model_alias}: {self._malformed_payload_detail(result, 300)}"
                        )
                    else:
                        text = raw_text.strip()
                        if not text:
                            should_fallback = self._should_text_fallback(data)
                            if attempt < malformed_retries:
                                sleep_s = self._malformed_retry_sleep(malformed_delay, attempt, model_tier)
                                logger.warning(
                                    "Claude Code returned empty result; retrying malformed response %s/%s "
                                    "(tier=%s model=%s, sleep=%.1fs, %s).",
                                    attempt + 1,
                                    malformed_retries + 1,
                                    model_tier,
                                    model_alias,
                                    sleep_s,
                                    self._malformed_payload_detail(result),
                                )
                                if sleep_s > 0:
                                    time.sleep(sleep_s)
                                continue
                            if should_fallback:
                                logger.warning(
                                    "Claude Code returned empty result in success payload; retrying once via plain text "
                                    "(tier=%s model=%s).",
                                    model_tier,
                                    model_alias,
                                )
                                fallback_result = subprocess.run(
                                    self._text_fallback_cmd(cmd),
                                    capture_output=True,
                                    text=True,
                                    timeout=effective_timeout,
                                    env=env,
                                    input=user_message,
                                    cwd="/tmp",
                                )
                                if fallback_result.returncode == 0:
                                    fallback_text = (fallback_result.stdout or "").strip()
                                    if fallback_text:
                                        return LLMResult(
                                            text=fallback_text,
                                            duration=time.time() - start_time,
                                            input_tokens=0,
                                            output_tokens=0,
                                            model=model_alias,
                                            model_usage={},
                                        )
                        else:
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

                    if command_attempt < command_retries:
                        sleep_s = self._malformed_retry_sleep(malformed_delay, command_attempt, model_tier)
                        logger.warning(
                            "Claude Code malformed success payload persisted after retries; rerunning command %s/%s "
                            "(tier=%s model=%s, sleep=%.1fs).",
                            command_attempt + 1,
                            command_retries + 1,
                            model_tier,
                            model_alias,
                            sleep_s,
                        )
                        if sleep_s > 0:
                            time.sleep(sleep_s)
                        break
                    raise RuntimeError(
                        f"Claude Code returned empty result for tier={model_tier}, "
                        f"model={model_alias}: {self._malformed_payload_detail(result, 300)}"
                    )
                else:
                    continue
                continue
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
                 deep_model: str = "", fast_model: str = "",
                 deep_reasoning_effort: str = "high",
                 fast_reasoning_effort: str = "none"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._deep_model = deep_model
        self._fast_model = fast_model or deep_model
        self._deep_reasoning_effort = self._normalize_reasoning_effort(deep_reasoning_effort, "high")
        self._fast_reasoning_effort = self._normalize_reasoning_effort(fast_reasoning_effort, "none")

    @staticmethod
    def _normalize_reasoning_effort(value: str, default: str) -> str:
        effort = str(value or "").strip().lower()
        if effort in {"none", "low", "medium", "high", "xhigh"}:
            return effort
        return default

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

    def _resolve_reasoning_effort(self, model_tier: str) -> str:
        if model_tier == "fast":
            return self._fast_reasoning_effort
        return self._deep_reasoning_effort

    def _should_use_openai_responses(self, model: str) -> bool:
        host = str(urllib.parse.urlsplit(self._base_url).hostname or "").strip().lower()
        model_name = str(model or "").strip().lower()
        return host == "api.openai.com" and model_name.startswith("gpt-5")

    @staticmethod
    def _clean_response_text(text: str) -> str:
        return re.sub(r"<think>[\s\S]*?</think>\s*", "", str(text or "")).strip()

    @staticmethod
    def _extract_responses_text(data: dict) -> str:
        if not isinstance(data, dict):
            return ""
        text = data.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        output = data.get("output")
        if not isinstance(output, list):
            return ""
        chunks = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content")
            if isinstance(content_items, list):
                for content_item in content_items:
                    if not isinstance(content_item, dict):
                        continue
                    if content_item.get("type") not in ("output_text", "text"):
                        continue
                    value = content_item.get("text")
                    if isinstance(value, str) and value:
                        chunks.append(value)
            else:
                value = item.get("text")
                if isinstance(value, str) and value:
                    chunks.append(value)
        return "\n".join(chunks).strip()

    @staticmethod
    def _messages_to_responses_payload(messages):
        instructions = []
        input_items = []
        for message in messages:
            role = str(message.get("role", "user") or "user").strip().lower()
            content = message.get("content", "")
            text = str(content if content is not None else "")
            if role in ("system", "developer"):
                if text.strip():
                    instructions.append(text)
                continue
            if role not in ("user", "assistant"):
                text = f"{role}: {text}" if text else role
                role = "user"
            input_items.append({"role": role, "content": text})

        input_payload = ""
        if len(input_items) == 1 and input_items[0]["role"] == "user":
            input_payload = input_items[0]["content"]
        elif input_items:
            input_payload = input_items

        return "\n\n".join(instructions).strip(), input_payload

    def _llm_call_responses(self, messages, *, model: str, model_tier: str, max_tokens: int, timeout: int):
        import time as _time

        url = f"{self._base_url}/v1/responses"
        instructions, input_payload = self._messages_to_responses_payload(messages)
        payload = {
            "model": model,
            "input": input_payload,
            "max_output_tokens": max_tokens,
            "reasoning": {
                "effort": self._resolve_reasoning_effort(model_tier),
            },
        }
        if instructions:
            payload["instructions"] = instructions

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
            raise RuntimeError(f"OpenAI responses payload must be a JSON object, got {type(data).__name__}")

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        input_details = usage.get("input_tokens_details", {})
        if not isinstance(input_details, dict):
            input_details = {}

        return LLMResult(
            text=self._clean_response_text(self._extract_responses_text(data)),
            duration=elapsed,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(
                input_details.get("cached_tokens", usage.get("cache_read_input_tokens", 0)) or 0
            ),
            cache_creation_tokens=int(
                input_details.get("cache_creation_tokens", usage.get("cache_creation_input_tokens", 0)) or 0
            ),
            model=str(data.get("model", model) or model),
            truncated=bool(data.get("incomplete", False)),
        )

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        import time as _time
        model = self._resolve_model(model_tier)
        if self._should_use_openai_responses(model):
            return self._llm_call_responses(
                messages,
                model=model,
                model_tier=model_tier,
                max_tokens=max_tokens,
                timeout=timeout,
            )
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
        text = self._clean_response_text(str(content))
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
                 model: str = "nomic-embed-text", dim: int = 768):
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
                with urllib.request.urlopen(req, timeout=120) as resp:
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
                _sanitize_url_for_logs(self._url),
                len(str(text or "")),
                e,
            )
            if is_fail_hard_enabled():
                raise RuntimeError(
                    f"Ollama embeddings provider failed while failHard is enabled: model={self._model}"
                ) from e
        return None

    def embed_many(self, texts: List[str]) -> List[Optional[List[float]]]:
        items = list(texts or [])
        if not items:
            return []

        try:
            batch_size = int(os.environ.get("OLLAMA_EMBED_BATCH_SIZE", "16") or 16)
        except Exception:
            batch_size = 16
        if batch_size <= 0:
            batch_size = 16

        try:
            timeout_s = float(os.environ.get("OLLAMA_EMBED_TIMEOUT_S", "120") or 120)
        except Exception:
            timeout_s = 120.0
        if timeout_s <= 0:
            timeout_s = 120.0

        retries = 1
        retryable_errors = (urllib.error.URLError, TimeoutError, OSError, ConnectionError)

        def _request_batch(batch: List[str]) -> List[Optional[List[float]]]:
            batch_error = None
            for attempt in range(retries + 1):
                try:
                    data = json.dumps({
                        "model": self._model,
                        "input": batch,
                        "keep_alive": -1,
                    }).encode("utf-8")
                    req = urllib.request.Request(
                        f"{self._url}/api/embed",
                        data=data,
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                        embeddings = result.get("embeddings", [])
                        if len(embeddings) != len(batch):
                            raise RuntimeError(
                                f"Ollama embeddings batch returned {len(embeddings)} vectors for {len(batch)} inputs"
                            )
                        return embeddings
                except retryable_errors as e:
                    batch_error = e
                    if attempt < retries:
                        time.sleep(0.2 * (2 ** attempt))
                        continue
                    break
                except Exception as e:
                    batch_error = e
                    break
            if batch_error is None:
                return []
            if len(batch) > 1 and isinstance(batch_error, retryable_errors):
                split_at = max(1, len(batch) // 2)
                logger.warning(
                    "Ollama embeddings batch timed out; retrying with smaller batches model=%s batch_len=%d split_at=%d error=%s",
                    self._model,
                    len(batch),
                    split_at,
                    batch_error,
                )
                left = _request_batch(batch[:split_at])
                right = _request_batch(batch[split_at:])
                return left + right
            raise batch_error

        all_embeddings: List[Optional[List[float]]] = []
        last_error = None
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            try:
                all_embeddings.extend(_request_batch(batch))
            except Exception as e:
                last_error = e
                break

        if last_error is not None:
            e = last_error
            logger.error(
                "Ollama embeddings batch call failed provider=ollama model=%s url=%s items=%d batch_size=%d error=%s",
                self._model,
                _sanitize_url_for_logs(self._url),
                len(items),
                batch_size,
                e,
            )
            if is_fail_hard_enabled():
                raise RuntimeError(
                    f"Ollama embeddings provider failed while failHard is enabled: model={self._model}"
                ) from e
            return []
        return all_embeddings

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
