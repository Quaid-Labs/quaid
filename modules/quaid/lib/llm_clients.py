#!/usr/bin/env python3
"""
Shared LLM client functions for janitor and workspace_audit.

Provides unified interfaces to:
- LLM providers (via adapter layer) for review and workspace tasks
- JSON response parsing with markdown fence stripping

Model selection is config-driven via config/memory.json — callers use
call_fast_reasoning() and call_deep_reasoning() without knowing which
specific model or provider is behind them.

LLM calls are routed through the adapter's LLMProvider. Quaid never
manages API keys directly — the adapter/provider handles authentication.
"""

import json
import hashlib
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
import urllib.error

from lib.fail_policy import is_fail_hard_enabled
from lib.llm_pool import acquire_llm_slot
from lib.providers import LLMResult
from lib.runtime_context import get_llm_provider
from prompt_sets import get_prompt

logger = logging.getLogger(__name__)


class ProviderUnavailableError(Exception):
    """Raised when the LLM provider is confirmed unavailable after retries.

    This is distinct from RuntimeError so callers can choose to let it propagate
    instead of catching it. The daemon uses this to shut down cleanly on confirmed
    provider outages — ensure_alive auto-restarts it on the next hook call (which
    can only happen when the provider is back, since the user can't chat without it).
    """

# Timeouts (seconds)
# Allow runtime overrides for bounded e2e/CI lanes without changing defaults.
DEEP_REASONING_TIMEOUT = float(os.environ.get("QUAID_DEEP_REASONING_TIMEOUT", "600"))
FAST_REASONING_TIMEOUT = float(os.environ.get("QUAID_FAST_REASONING_TIMEOUT", "120"))

# Model names — loaded from config on first use (lazy to avoid circular imports)
_models_loaded: bool = False
_fast_reasoning_model: str = ""
_deep_reasoning_model: str = ""
_model_config_lock = threading.Lock()
_trace_lock = threading.Lock()
_usage_log_lock = threading.Lock()


def _load_model_config():
    """Load model names from config.py (once)."""
    global _models_loaded, _fast_reasoning_model, _deep_reasoning_model
    if _models_loaded:
        return
    with _model_config_lock:
        if _models_loaded:
            return
        try:
            from config import get_config
            cfg = get_config()
            _fast_reasoning_model = cfg.models.fast_reasoning
            _deep_reasoning_model = cfg.models.deep_reasoning
            _models_loaded = True  # Only after success — allows retry on transient failures
        except ImportError:
            pass  # Config not available (test environment) — defaults set by provider
        except Exception as e:
            logger.error("[llm_clients] FATAL: Config loaded but model resolution failed: %s", e)
            raise


# Token usage accumulator — reset per janitor run, read at end for cost report
_usage_input_tokens: int = 0
_usage_output_tokens: int = 0
_usage_calls: int = 0
_usage_cache_read_tokens: int = 0
_usage_cache_creation_tokens: int = 0
_usage_by_model: Dict[str, Dict[str, int]] = {}  # {model: {input: N, output: N}}
_usage_lock = threading.RLock()

# Default pricing per million tokens (as of Feb 2026)
# Used for cost estimation in janitor runs. Config overrides via models.pricing.
_PRICING: Dict[str, Dict[str, float]] = {
    "claude-opus-4-6":            {"input":  5.00, "output": 25.00},
    "claude-opus-4-6-20260205":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-5-20251101":   {"input": 15.00, "output": 75.00},
    "claude-opus-4-5":            {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001":  {"input":  0.80, "output":  4.00},
    "claude-haiku-4-5":           {"input":  0.80, "output":  4.00},
}
_pricing_loaded: bool = False
_pricing_error_logged: bool = False
_pricing_lock = threading.Lock()


def _load_pricing():
    """Merge config pricing into the default table (once)."""
    global _pricing_loaded, _pricing_error_logged
    if _pricing_loaded:
        return
    with _pricing_lock:
        if _pricing_loaded:
            return
        try:
            from config import get_config
            get_config()
            # Config uses snake_case; pricing would be under models section
            # but MemoryConfig doesn't expose raw_config — use ModelConfig fields instead.
            # Custom pricing can be added to _PRICING dict directly or via env overrides.
            _pricing_loaded = True
        except ImportError:
            _pricing_loaded = True
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Failed to load pricing configuration while failHard is enabled."
                ) from exc
            if not _pricing_error_logged:
                logger.warning("Pricing config load failed; using built-in defaults: %s", exc)
                _pricing_error_logged = True


def reset_token_usage():
    """Reset the per-run token usage counters."""
    global _usage_input_tokens, _usage_output_tokens, _usage_calls
    global _usage_cache_read_tokens, _usage_cache_creation_tokens, _usage_by_model
    with _usage_lock:
        _usage_input_tokens = 0
        _usage_output_tokens = 0
        _usage_calls = 0
        _usage_cache_read_tokens = 0
        _usage_cache_creation_tokens = 0
        _usage_by_model = {}


def get_token_usage() -> dict:
    """Return accumulated token usage and estimated cost for this run."""
    with _usage_lock:
        model_usage = {
            model_name: {
                "input_tokens": int(counts.get("input", 0)),
                "output_tokens": int(counts.get("output", 0)),
                "total_tokens": int(counts.get("input", 0)) + int(counts.get("output", 0)),
            }
            for model_name, counts in _usage_by_model.items()
        }
        tier_usage = {
            "fast": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "deep": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        for model_name, counts in model_usage.items():
            tier = _resolve_tier(model_name)
            tier_usage[tier]["input_tokens"] += int(counts["input_tokens"])
            tier_usage[tier]["output_tokens"] += int(counts["output_tokens"])
            tier_usage[tier]["total_tokens"] += int(counts["total_tokens"])
        return {
            "input_tokens": _usage_input_tokens,
            "output_tokens": _usage_output_tokens,
            "api_calls": _usage_calls,
            "cache_read_tokens": _usage_cache_read_tokens,
            "cache_creation_tokens": _usage_cache_creation_tokens,
            "model_usage": model_usage,
            "tier_usage": tier_usage,
        }


def estimate_cost() -> float:
    """Estimate USD cost based on per-model token usage.

    Uses actual model rates when available, falls back to cheapest
    known rate for any untracked tokens.
    """
    _load_pricing()
    if not _PRICING:
        return 0.0

    # Find the cheapest rate as fallback for unknown models
    _cheapest = min(_PRICING.values(), key=lambda r: r["input"])

    with _usage_lock:
        cost = 0.0
        tracked_input = 0
        tracked_output = 0
        for model_name, counts in _usage_by_model.items():
            rate = _PRICING.get(model_name, _cheapest)
            cost += counts.get("input", 0) / 1_000_000 * rate["input"]
            cost += counts.get("output", 0) / 1_000_000 * rate["output"]
            tracked_input += counts.get("input", 0)
            tracked_output += counts.get("output", 0)
        # Fall back to cheapest rate for any untracked remainder
        untracked_in = max(0, _usage_input_tokens - tracked_input)
        untracked_out = max(0, _usage_output_tokens - tracked_output)
        if untracked_in or untracked_out:
            cost += untracked_in / 1_000_000 * _cheapest["input"]
            cost += untracked_out / 1_000_000 * _cheapest["output"]
        return round(cost, 4)


# Per-operation token budget — set by callers to limit total tokens for a
# sequence of LLM calls (e.g., janitor pipeline, recall with retries).
# 0 = unlimited. Checked in call_llm() after the cost cap.
# Config should be the primary source (janitor.token_budget). Env var
# fallback is handled by janitor runner.
_token_budget: int = 0
_token_budget_used: int = 0


def set_token_budget(max_tokens: int) -> None:
    """Set a token budget for subsequent LLM calls. 0 = unlimited."""
    global _token_budget, _token_budget_used
    with _usage_lock:
        _token_budget = max(0, max_tokens)
        _token_budget_used = 0


def reset_token_budget() -> None:
    """Clear the token budget (unlimited)."""
    global _token_budget, _token_budget_used
    with _usage_lock:
        _token_budget = 0
        _token_budget_used = 0


def get_token_budget_remaining() -> int:
    """Return remaining tokens in budget. -1 = unlimited."""
    with _usage_lock:
        if _token_budget <= 0:
            return -1
        return max(0, _token_budget - _token_budget_used)


def is_token_budget_exhausted() -> bool:
    """Check if token budget is set and exhausted."""
    with _usage_lock:
        return _token_budget > 0 and _token_budget_used >= _token_budget


def get_token_budget_usage() -> Tuple[int, int]:
    """Return a consistent snapshot of (used_tokens, budget_tokens)."""
    with _usage_lock:
        return _token_budget_used, _token_budget


# Retry config
_MAX_RETRIES = max(0, int(os.environ.get("QUAID_LLM_MAX_RETRIES", "3") or "3"))
_RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry


def _trace_enabled() -> bool:
    """Benchmark-gated trace switch for per-call LLM diagnostics."""
    raw = str(os.environ.get("BENCHMARK_LLM_TRACE", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    workspace = str(
        os.environ.get("QUAID_WORKSPACE", "")
        or os.environ.get("CLAWDBOT_WORKSPACE", "")
        or ""
    ).strip()
    return "/runs/quaid-" in workspace


def _trace_path() -> Optional[Path]:
    workspace = str(
        os.environ.get("QUAID_WORKSPACE", "")
        or os.environ.get("CLAWDBOT_WORKSPACE", "")
        or ""
    ).strip()
    if not workspace:
        return None
    return Path(workspace) / "logs" / "llm-call-trace.jsonl"


def _preview(text: Optional[str], limit: int = 30) -> str:
    if not text:
        return ""
    first = str(text).splitlines()[0].strip()
    if len(first) <= limit:
        return first
    return first[:limit]


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code is not None:
        return str(code)
    return type(exc).__name__


def _rate_limit_headers(exc: BaseException) -> Dict[str, str]:
    if not isinstance(exc, urllib.error.HTTPError):
        return {}
    try:
        headers = getattr(exc, "headers", None)
        if headers is None:
            return {}
        out: Dict[str, str] = {}
        for k, v in headers.items():
            key = str(k or "").strip().lower()
            if not key:
                continue
            if key == "retry-after" or key.startswith("anthropic-ratelimit-"):
                out[key] = str(v)
        return out
    except Exception:
        return {}


def _retry_delay_for_error(exc: BaseException, fallback_delay: float) -> float:
    delay = max(0.0, float(fallback_delay))
    if not isinstance(exc, urllib.error.HTTPError):
        return delay
    if int(getattr(exc, "code", 0) or 0) != 429:
        return delay
    hdrs = _rate_limit_headers(exc)
    retry_after_raw = str(hdrs.get("retry-after", "")).strip()
    if retry_after_raw:
        try:
            return max(delay, float(retry_after_raw))
        except Exception:
            pass
    reset_candidates = []
    for key, value in hdrs.items():
        if key.endswith("-reset"):
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                reset_candidates.append(max(0.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                continue
    if reset_candidates:
        return max(delay, min(reset_candidates))
    return delay


def _key_fp() -> str:
    """Return short fingerprint for active auth env (never the raw secret)."""
    # Check known auth env vars in priority order.
    # NOTE: adapter-specific vars (CLAUDE_CODE_OAUTH_TOKEN) should eventually
    # be injected by the adapter, not hard-coded here.
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        token = str(os.environ.get(var, "")).strip()
        if token:
            break
    else:
        token = ""
    if not token:
        return "missing"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _append_trace(payload: Dict[str, object]) -> None:
    """Best-effort append of per-call trace payload."""
    if not _trace_enabled():
        return
    path = _trace_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(payload or {})
        row["ts"] = datetime.now(timezone.utc).isoformat()
        with _trace_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        # Never let tracing alter runtime behavior.
        return


def _usage_log_path() -> Optional[Path]:
    raw = str(os.environ.get("QUAID_LLM_USAGE_LOG_PATH", "")).strip()
    if not raw:
        return None
    return Path(raw)


def _append_usage_event(
    result: LLMResult,
    *,
    tier: Optional[str],
    provider_name: str,
    requested_model: Optional[str],
) -> None:
    """Best-effort append of per-call token usage to a shared JSONL log."""
    path = _usage_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        model_usage = {}
        if result.model_usage:
            for model_name, counts in result.model_usage.items():
                model_usage[str(model_name)] = {
                    "input_tokens": int(counts.get("input", 0)),
                    "output_tokens": int(counts.get("output", 0)),
                    "total_tokens": int(counts.get("input", 0)) + int(counts.get("output", 0)),
                }
        elif result.model:
            model_usage[str(result.model)] = {
                "input_tokens": int(result.input_tokens),
                "output_tokens": int(result.output_tokens),
                "total_tokens": int(result.input_tokens) + int(result.output_tokens),
            }
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": str(os.environ.get("QUAID_LLM_USAGE_PHASE", "")).strip() or "unknown",
            "source": str(os.environ.get("QUAID_LLM_USAGE_SOURCE", "")).strip() or "runtime",
            "provider": provider_name,
            "tier": tier or "",
            "requested_model": requested_model or "",
            "resolved_model": result.model or requested_model or "",
            "input_tokens": int(result.input_tokens),
            "output_tokens": int(result.output_tokens),
            "total_tokens": int(result.input_tokens) + int(result.output_tokens),
            "cache_read_tokens": int(result.cache_read_tokens),
            "cache_creation_tokens": int(result.cache_creation_tokens),
            "api_calls": 1,
            "duration_ms": round(float(result.duration) * 1000),
            "model_usage": model_usage,
        }
        with _usage_log_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        return


def _track_usage(result: LLMResult) -> None:
    """Accumulate token usage from an LLMResult into module-level counters."""
    global _usage_input_tokens, _usage_output_tokens, _usage_calls
    global _usage_cache_read_tokens, _usage_cache_creation_tokens, _usage_by_model
    global _token_budget_used

    with _usage_lock:
        _usage_input_tokens += result.input_tokens
        _usage_output_tokens += result.output_tokens
        _usage_cache_read_tokens += result.cache_read_tokens
        _usage_cache_creation_tokens += result.cache_creation_tokens
        _usage_calls += 1

        # Track against per-operation token budget
        _token_budget_used += result.input_tokens + result.output_tokens

        # Per-model tracking (for accurate cost estimation)
        if result.model_usage:
            for m, counts in result.model_usage.items():
                if m not in _usage_by_model:
                    _usage_by_model[m] = {"input": 0, "output": 0}
                _usage_by_model[m]["input"] += counts.get("input", 0)
                _usage_by_model[m]["output"] += counts.get("output", 0)
        elif result.model:
            model_key = result.model
            if model_key not in _usage_by_model:
                _usage_by_model[model_key] = {"input": 0, "output": 0}
            _usage_by_model[model_key]["input"] += result.input_tokens
            _usage_by_model[model_key]["output"] += result.output_tokens


def _resolve_tier(model: Optional[str]) -> str:
    """Determine the model tier ('deep' or 'fast') from a model name.

    Compares against the configured fast-reasoning model name. If it matches,
    returns 'fast'; otherwise 'deep'. Provider-agnostic.
    """
    _load_model_config()
    if model and _fast_reasoning_model and model == _fast_reasoning_model:
        return "fast"
    return "deep"


def call_llm(system_prompt: str, user_message: str,
             model: Optional[str] = None,
             model_tier: Optional[str] = None,
             max_tokens: int = 4000,
             timeout: float = DEEP_REASONING_TIMEOUT,
             max_retries: Optional[int] = None) -> Tuple[Optional[str], float]:
    """Call the configured LLM provider and return (response text, duration).

    Dispatches to the adapter's LLM provider (Gateway, ClaudeCode, Test, etc.).
    Retries up to max_retries times on transient errors with exponential backoff.
    Returns (None, duration) on persistent errors.

    Set QUAID_DISABLE_LLM=1 to disable all LLM calls (returns None immediately).
    Used by subprocess tests to avoid hitting real providers.
    """
    if os.environ.get("QUAID_DISABLE_LLM"):
        return (None, 0.0)

    _load_model_config()
    if model is None:
        model = _deep_reasoning_model

    resolved_tier = model_tier if model_tier in ("deep", "fast") else _resolve_tier(model)

    # Cap max_tokens to API limits
    try:
        from config import get_config as _get_cfg
        api_max = _get_cfg().models.max_output(resolved_tier)
    except Exception:
        api_max = 16384
    max_tokens = min(max_tokens, api_max)

    # Cost circuit breaker
    _COST_CAP = float(os.environ.get("JANITOR_COST_CAP", "5.0"))
    current_cost = estimate_cost()
    if current_cost > _COST_CAP:
        logger.warning(
            "[llm_clients] COST CAP EXCEEDED: $%.4f > $%.2f, aborting call",
            current_cost,
            _COST_CAP,
        )
        if is_fail_hard_enabled():
            raise RuntimeError(
                f"LLM cost cap exceeded while failHard is enabled: ${current_cost:.4f} > ${_COST_CAP:.2f}"
            )
        return (None, 0.0)

    # Token budget check
    if is_token_budget_exhausted():
        budget_used, budget_total = get_token_budget_usage()
        logger.warning(
            "[llm_clients] TOKEN BUDGET EXHAUSTED: %s >= %s, aborting call",
            f"{budget_used:,}",
            f"{budget_total:,}",
        )
        if is_fail_hard_enabled():
            raise RuntimeError(
                f"LLM token budget exhausted while failHard is enabled: {budget_used:,} >= {budget_total:,}"
            )
        return (None, 0.0)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    llm = get_llm_provider(model_tier=resolved_tier)
    provider_name = llm.__class__.__name__

    start_time = time.time()
    deadline = None
    if timeout is not None:
        deadline = start_time + max(0.0, float(timeout))
    retries = _MAX_RETRIES if max_retries is None else max_retries
    last_error = None

    _RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504, 529}

    for attempt in range(retries + 1):
        attempt_started = time.monotonic()
        try:
            timeout_for_attempt = timeout
            if deadline is not None:
                timeout_for_attempt = deadline - time.time()
                if timeout_for_attempt <= 0:
                    raise TimeoutError("LLM deadline exhausted before provider call")
            with acquire_llm_slot(timeout_seconds=timeout_for_attempt):
                call_timeout = timeout_for_attempt
                if deadline is not None:
                    call_timeout = deadline - time.time()
                    if call_timeout <= 0:
                        raise TimeoutError("LLM deadline exhausted while waiting for worker slot")
                result = llm.llm_call(messages, resolved_tier, max_tokens, call_timeout)
            _track_usage(result)
            _append_usage_event(
                result,
                tier=resolved_tier,
                provider_name=provider_name,
                requested_model=model,
            )
            if result.truncated:
                logger.warning(
                    "[llm_clients] Response truncated (max_tokens) for model=%s",
                    result.model,
                )
            if result.text is None:
                raise RuntimeError(
                    "No response from provider "
                    f"(provider={provider_name}, tier={resolved_tier}, model={result.model or model}, "
                    f"timeout={timeout_for_attempt})"
                )
            _append_trace({
                "status": "ok",
                "provider": provider_name,
                "tier": resolved_tier,
                "requested_model": str(model or ""),
                "resolved_model": str(model or ""),
                "returned_model": str(result.model or ""),
                "attempt": int(attempt + 1),
                "duration_ms": int(max(0.0, float(result.duration or 0.0)) * 1000),
                "timeout_s": float(timeout_for_attempt if timeout_for_attempt is not None else 0.0),
                "request_preview": _preview(user_message, 30),
                "system_preview": _preview(system_prompt, 30),
                "response_preview": _preview(result.text, 30),
                "error_code": "",
                "key_fp": _key_fp(),
            })
            return result.text, result.duration
        except Exception as e:
            last_error = e
            _append_trace({
                "status": "error",
                "provider": provider_name,
                "tier": resolved_tier,
                "requested_model": str(model or ""),
                "resolved_model": str(model or ""),
                "returned_model": "",
                "attempt": int(attempt + 1),
                "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                "timeout_s": float(timeout if timeout is not None else 0.0),
                "request_preview": _preview(user_message, 30),
                "system_preview": _preview(system_prompt, 30),
                "response_preview": "",
                "error_code": _error_code(e),
                "error": str(e),
                "rate_limits": _rate_limit_headers(e),
                "key_fp": _key_fp(),
            })
            # Only retry on transient errors (rate limit, server errors, timeouts)
            retryable = isinstance(e, (TimeoutError, ConnectionError, OSError,
                                       subprocess.TimeoutExpired))
            if isinstance(e, urllib.error.HTTPError):
                retryable = e.code in _RETRYABLE_HTTP_CODES
            if retryable and attempt < retries:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                delay = _retry_delay_for_error(e, delay)
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    delay = min(delay, max(0.0, remaining))
                    if delay <= 0:
                        break
                code = getattr(e, 'code', type(e).__name__)
                logger.warning(
                    "[llm_clients] Retryable error (%s), attempt %s/%s, retrying in %.1fs",
                    code,
                    attempt + 1,
                    retries + 1,
                    delay,
                )
                time.sleep(delay)
                continue
            break

    duration = time.time() - start_time
    logger.error("[llm_clients] LLM error: %s", last_error)

    # Determine if this is a confirmed provider outage (retryable HTTP codes
    # exhausted) vs. a bug or config error in our code.
    _is_provider_outage = False
    if isinstance(last_error, (TimeoutError, ConnectionError, OSError)):
        _is_provider_outage = True
    elif isinstance(last_error, urllib.error.HTTPError):
        _is_provider_outage = last_error.code in _RETRYABLE_HTTP_CODES

    if _is_provider_outage:
        if is_fail_hard_enabled():
            err_type = type(last_error).__name__ if last_error is not None else "UnknownError"
            raise RuntimeError(
                "LLM call failed after retries while failHard is enabled "
                f"(provider={provider_name}, tier={resolved_tier}, model={model}, "
                f"error_type={err_type}, error={last_error})."
            ) from last_error
        raise ProviderUnavailableError(
            f"Provider unavailable after {retries + 1} attempts "
            f"(provider={provider_name}, tier={resolved_tier}, model={model}, "
            f"error={last_error})"
        ) from last_error

    if is_fail_hard_enabled():
        err_type = type(last_error).__name__ if last_error is not None else "UnknownError"
        raise RuntimeError(
            "LLM call failed after retries while failHard is enabled "
            f"(provider={provider_name}, tier={resolved_tier}, model={model}, "
            f"error_type={err_type}, error={last_error})."
        ) from last_error
    logger.warning(
        "[llm_clients][FALLBACK] Returning None after LLM failure because failHard is disabled."
    )
    return None, duration


def call_fast_reasoning(prompt: str, max_tokens: int = 200,
                       timeout: float = FAST_REASONING_TIMEOUT,
                       system_prompt: Optional[str] = None,
                       max_retries: Optional[int] = None) -> Tuple[Optional[str], float]:
    """Call the fast-reasoning model and return (response text, duration).

    Used for: dedup verification, contradiction detection, edge extraction (batched), HyDE.
    Raises on provider/config errors (no silent fallback).
    Returns (None, duration) only for transient LLM failures after retries.
    """
    _load_model_config()
    effective_system_prompt = get_prompt("llm.json_only") if system_prompt is None else system_prompt
    return call_llm(
        system_prompt=effective_system_prompt,
        user_message=prompt,
        model=_fast_reasoning_model,
        model_tier="fast",
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=max_retries,
    )


def call_deep_reasoning(prompt: str, system_prompt: Optional[str] = None,
                        max_tokens: int = 2000,
                        timeout: float = DEEP_REASONING_TIMEOUT) -> Tuple[Optional[str], float]:
    """Call the deep-reasoning model and return (response text, duration).

    Used for: memory review, workspace audit, contradiction resolution, edge extraction.
    Raises on provider/config errors (no silent fallback).
    Returns (None, duration) only for transient LLM failures after retries.
    """
    _load_model_config()
    effective_system_prompt = get_prompt("llm.json_only") if system_prompt is None else system_prompt
    return call_llm(
        system_prompt=effective_system_prompt,
        user_message=prompt,
        model=_deep_reasoning_model,
        model_tier="deep",
        max_tokens=max_tokens,
        timeout=timeout,
    )


def parse_json_response(text: str) -> Optional[object]:
    """Strip markdown fences and parse JSON from an LLM response.

    Handles responses wrapped in ```json ... ``` blocks as well as bare JSON.
    Returns parsed JSON (dict or list) or None on failure.
    """
    def _repair_invalid_string_escapes(candidate: str) -> str:
        """Repair invalid backslash escapes inside quoted JSON strings.

        Some LLMs emit otherwise-valid JSON but leave raw backslashes inside
        string values, for example ``"C:\\work\\cap"``. The JSON parser rejects
        those as invalid escapes. Preserve the literal backslash by doubling it
        before retrying.
        """
        candidate = candidate or ""
        if "\\" not in candidate:
            return candidate

        out: List[str] = []
        in_string = False
        escape = False
        idx = 0
        length = len(candidate)
        valid_simple = {'"', "\\", "/", "b", "f", "n", "r", "t"}
        hex_digits = set("0123456789abcdefABCDEF")

        while idx < length:
            ch = candidate[idx]

            if not in_string:
                out.append(ch)
                if ch == '"':
                    in_string = True
                idx += 1
                continue

            if escape:
                out.append(ch)
                escape = False
                idx += 1
                continue

            if ch == "\\":
                next_ch = candidate[idx + 1] if idx + 1 < length else ""
                if next_ch in valid_simple:
                    out.append(ch)
                    escape = True
                    idx += 1
                    continue
                if (
                    next_ch == "u"
                    and idx + 5 < length
                    and all(c in hex_digits for c in candidate[idx + 2:idx + 6])
                ):
                    out.append(ch)
                    escape = True
                    idx += 1
                    continue
                # Invalid escape inside a string: preserve the literal
                # backslash so json.loads can accept the repaired payload.
                out.append("\\\\")
                idx += 1
                continue

            out.append(ch)
            if ch == '"':
                in_string = False
            idx += 1

        return "".join(out)

    def _extract_balanced_json(candidate: str) -> Optional[str]:
        candidate = (candidate or "").strip()
        if not candidate:
            return None
        starts = [idx for idx in (candidate.find("{"), candidate.find("[")) if idx != -1]
        if not starts:
            return None
        start = min(starts)
        opening = candidate[start]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(candidate)):
            ch = candidate[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "\"":
                    in_string = False
                continue
            if ch == "\"":
                in_string = True
                continue
            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    return candidate[start:idx + 1]
        return None

    def _loads_with_relaxed_fallback(candidate: str):
        repaired = _repair_invalid_string_escapes(candidate)
        try_candidates = [candidate]
        if repaired != candidate:
            try_candidates.append(repaired)

        first_error = None
        for raw_candidate in try_candidates:
            try:
                return json.loads(raw_candidate)
            except json.JSONDecodeError as e:
                if first_error is None:
                    first_error = e
                try:
                    return json.loads(raw_candidate, strict=False)
                except json.JSONDecodeError as relaxed_error:
                    if first_error is None:
                        first_error = relaxed_error
                    continue
        raise first_error or json.JSONDecodeError("invalid JSON", candidate or "", 0)

    if not text:
        return None

    cleaned = text.strip()
    parse_errors = []

    # Try direct parse first
    try:
        return _loads_with_relaxed_fallback(cleaned)
    except json.JSONDecodeError as e:
        parse_errors.append(f"direct parse failed at line {e.lineno}, col {e.colno}: {e.msg}")

    # Strip markdown fences
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate and (candidate.startswith("{") or candidate.startswith("[")):
                try:
                    return _loads_with_relaxed_fallback(candidate)
                except json.JSONDecodeError as e:
                    balanced = _extract_balanced_json(candidate)
                    if balanced and balanced != candidate:
                        try:
                            return _loads_with_relaxed_fallback(balanced)
                        except json.JSONDecodeError:
                            pass
                    parse_errors.append(f"fenced parse failed at line {e.lineno}, col {e.colno}: {e.msg}")
                    continue

    # Last resort: find the first JSON token ({ or [) and extract the balanced
    # structure starting from it. _extract_balanced_json uses min(starts) so it
    # picks whichever of { or [ appears first — correct for both objects and
    # arrays, and avoids the previous bug where trying { before [ caused the
    # first dict inside an array (e.g. [{"fact":1}]) to be returned as a dict
    # instead of the full array.
    balanced = _extract_balanced_json(cleaned)
    if balanced:
        try:
            return _loads_with_relaxed_fallback(balanced)
        except json.JSONDecodeError as e:
            parse_errors.append(
                f"balanced parse failed at line {e.lineno}, col {e.colno}: {e.msg}"
            )

    if parse_errors:
        content_len = len(cleaned)
        content_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
        logger.warning(
            "[llm_clients] parse_json_response failed: "
            f"{'; '.join(parse_errors[:3])}; content_len={content_len}; content_sha256_prefix={content_hash}",
        )

    return None


# ==========================================================================
# LLM Output Validation
# ==========================================================================

from dataclasses import dataclass
from typing import List as _List


@dataclass
class ReviewDecision:
    """Validated review decision from deep-reasoning LLM."""
    action: str  # KEEP, REJECT, FIX, MERGE
    reasoning: str = ""
    fixed_text: str = ""  # Only for FIX

    _VALID_ACTIONS = {"KEEP", "REJECT", "FIX", "MERGE"}

    def __post_init__(self):
        if self.action not in self._VALID_ACTIONS:
            # Fuzzy match common typos
            upper = self.action.upper().strip()
            for valid in self._VALID_ACTIONS:
                if upper.startswith(valid[:3]):
                    self.action = valid
                    return
            raise ValueError(f"Invalid review action: {self.action!r} (expected one of {self._VALID_ACTIONS})")


@dataclass
class DedupDecision:
    """Validated dedup decision from LLM."""
    pair: int
    is_same: bool
    reasoning: str = ""

    def __post_init__(self):
        if not isinstance(self.pair, int) or self.pair < 1:
            raise ValueError(f"Invalid pair index: {self.pair}")
        if not isinstance(self.is_same, bool):
            # Coerce common LLM outputs
            if isinstance(self.is_same, str):
                self.is_same = self.is_same.lower() in ("true", "yes", "1")
            else:
                self.is_same = bool(self.is_same)


@dataclass
class ContradictionResult:
    """Validated contradiction check result from LLM."""
    pair: int
    contradicts: bool
    explanation: str = ""

    def __post_init__(self):
        if not isinstance(self.pair, int) or self.pair < 1:
            raise ValueError(f"Invalid pair index: {self.pair}")
        if not isinstance(self.contradicts, bool):
            if isinstance(self.contradicts, str):
                self.contradicts = self.contradicts.lower() in ("true", "yes", "1")
            else:
                self.contradicts = bool(self.contradicts)


@dataclass
class DecayDecision:
    """Validated decay review decision from LLM."""
    id: str
    action: str  # delete, extend, pin
    reason: str = ""

    _VALID_ACTIONS = {"delete", "extend", "pin"}

    def __post_init__(self):
        action_lower = self.action.lower().strip()
        if action_lower not in self._VALID_ACTIONS:
            # Fuzzy: check prefix
            for valid in self._VALID_ACTIONS:
                if action_lower.startswith(valid[:3]):
                    self.action = valid
                    return
            raise ValueError(f"Invalid decay action: {self.action!r} (expected one of {self._VALID_ACTIONS})")
        self.action = action_lower


def validate_llm_output(parsed: object, schema_class: type, list_mode: bool = True) -> _List:
    """Validate parsed LLM JSON output against a dataclass schema.

    Args:
        parsed: The parsed JSON (from parse_json_response)
        schema_class: The dataclass to validate against (e.g. ReviewDecision)
        list_mode: If True, expect a list; if False, expect a single object

    Returns:
        List of validated dataclass instances (or single-item list if list_mode=False).
        Invalid items are skipped with a warning.
    """
    if parsed is None:
        return []

    items = parsed if isinstance(parsed, list) else [parsed]
    results = []

    def _item_log_summary(item_obj: object) -> str:
        if isinstance(item_obj, dict):
            keys = list(item_obj.keys())
            return f"keys={keys}, key_count={len(keys)}"
        return f"type={type(item_obj).__name__}"

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            # Map dict keys to dataclass fields (case-insensitive for common typos)
            field_names = {f.name for f in schema_class.__dataclass_fields__.values()}
            mapped = {}
            dropped_keys = []
            for k, v in item.items():
                k_lower = k.lower().replace("-", "_")
                if k_lower in field_names:
                    mapped[k_lower] = v
                elif k in field_names:
                    mapped[k] = v
                else:
                    dropped_keys.append(k)

            if dropped_keys:
                logger.warning(
                    f"[llm_clients] Validation warning: dropping unknown keys {dropped_keys} "
                    f"for schema {schema_class.__name__}"
                )
            if not mapped:
                logger.warning(
                    "[llm_clients] Validation warning: no recognized keys for schema "
                    f"{schema_class.__name__} ({_item_log_summary(item)})"
                )
                continue

            obj = schema_class(**mapped)
            results.append(obj)
        except (TypeError, ValueError) as e:
            logger.warning(
                f"[llm_clients] Validation warning: {e} for schema {schema_class.__name__} "
                f"({_item_log_summary(item)})"
            )
            continue

    return results
