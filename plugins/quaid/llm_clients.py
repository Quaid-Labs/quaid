#!/usr/bin/env python3
"""
Shared LLM client functions for janitor and workspace_audit.

Provides unified interfaces to:
- Anthropic API (high-reasoning model) for review and workspace tasks
- Anthropic API (low-reasoning model) for duplicates, contradictions, edges
- JSON response parsing with markdown fence stripping

Model selection is config-driven via config/memory.json — callers use
call_low_reasoning() and call_high_reasoning() without knowing which
specific model is behind them.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

# Anthropic defaults
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_TIMEOUT = 120
ANTHROPIC_VERSION = "2023-06-01"
LOW_REASONING_TIMEOUT = 30

# Model names — loaded from config on first use (lazy to avoid circular imports)
_models_loaded: bool = False
_low_reasoning_model: str = "claude-haiku-4-5"
_high_reasoning_model: str = "claude-opus-4-6"


def _load_model_config():
    """Load model names from config.py (once)."""
    global _models_loaded, _low_reasoning_model, _high_reasoning_model
    if _models_loaded:
        return
    try:
        from config import get_config, resolve_model
        cfg = get_config()
        _low_reasoning_model = cfg.models.low_reasoning
        _high_reasoning_model = resolve_model(cfg.models.high_reasoning)
        _models_loaded = True  # Only after success — allows retry on transient failures
    except ImportError:
        pass  # Config not available (test environment) — use dataclass defaults
    except Exception as e:
        print(f"[llm_clients] FATAL: Config loaded but model resolution failed: {e}", file=sys.stderr)
        raise

# Cached API key state — avoids hammering Keychain on repeated failures
_api_key_cache: Optional[str] = None
_api_key_failed: bool = False
_api_key_failed_at: float = 0.0  # timestamp of last failure
_API_KEY_RETRY_SECONDS: int = 300  # retry after 5 minutes

# Token usage accumulator — reset per janitor run, read at end for cost report
_usage_input_tokens: int = 0
_usage_output_tokens: int = 0
_usage_calls: int = 0
_usage_cache_read_tokens: int = 0
_usage_cache_creation_tokens: int = 0
_usage_by_model: Dict[str, Dict[str, int]] = {}  # {model: {input: N, output: N}}

# Pricing per million tokens (as of Feb 2026)
_PRICING = {
    "claude-opus-4-6":            {"input":  5.00, "output": 25.00},
    "claude-opus-4-6-20260205":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-5-20251101":   {"input": 15.00, "output": 75.00},
    "claude-opus-4-5":            {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001":  {"input":  0.80, "output":  4.00},
    "claude-haiku-4-5":           {"input":  0.80, "output":  4.00},
}


def reset_token_usage():
    """Reset the per-run token usage counters."""
    global _usage_input_tokens, _usage_output_tokens, _usage_calls
    global _usage_cache_read_tokens, _usage_cache_creation_tokens, _usage_by_model
    _usage_input_tokens = 0
    _usage_output_tokens = 0
    _usage_calls = 0
    _usage_cache_read_tokens = 0
    _usage_cache_creation_tokens = 0
    _usage_by_model = {}


def get_token_usage() -> dict:
    """Return accumulated token usage and estimated cost for this run."""
    return {
        "input_tokens": _usage_input_tokens,
        "output_tokens": _usage_output_tokens,
        "api_calls": _usage_calls,
        "cache_read_tokens": _usage_cache_read_tokens,
        "cache_creation_tokens": _usage_cache_creation_tokens,
    }


def estimate_cost() -> float:
    """Estimate USD cost based on per-model token usage.

    Uses actual model rates when available, falls back to Haiku rates
    for any untracked tokens (backward compat).
    """
    cost = 0.0
    tracked_input = 0
    tracked_output = 0
    for model_name, counts in _usage_by_model.items():
        rate = _PRICING.get(model_name, _PRICING.get(_low_reasoning_model, _PRICING["claude-haiku-4-5"]))
        cost += counts.get("input", 0) / 1_000_000 * rate["input"]
        cost += counts.get("output", 0) / 1_000_000 * rate["output"]
        tracked_input += counts.get("input", 0)
        tracked_output += counts.get("output", 0)
    # Fall back to cheapest rate for any untracked remainder
    untracked_in = max(0, _usage_input_tokens - tracked_input)
    untracked_out = max(0, _usage_output_tokens - tracked_output)
    if untracked_in or untracked_out:
        lr_rate = _PRICING.get(_low_reasoning_model, _PRICING["claude-haiku-4-5"])
        cost += untracked_in / 1_000_000 * lr_rate["input"]
        cost += untracked_out / 1_000_000 * lr_rate["output"]
    return round(cost, 4)


def get_api_key(env_var_name: str = None) -> str:
    """Fetch API key from env var, .env file, or macOS Keychain (cached).

    Resolution order:
    1. Named environment variable (from config.models.api_key_env or parameter)
    2. .env file in workspace root
    3. macOS Keychain (Anthropic only, for backward compatibility)

    Caches the result so lookups are only done once per process.
    Raises RuntimeError if the key cannot be retrieved from any source.
    """
    global _api_key_cache, _api_key_failed, _api_key_failed_at
    import os

    if env_var_name is None:
        _, _, env_var_name = _get_provider_config()

    if _api_key_cache:
        return _api_key_cache
    if _api_key_failed:
        # Allow retry after timeout (e.g. Keychain became available)
        if (time.time() - _api_key_failed_at) < _API_KEY_RETRY_SECONDS:
            raise RuntimeError("API key unavailable (cached failure, retrying in %ds)" % int(_API_KEY_RETRY_SECONDS - (time.time() - _api_key_failed_at)))
        _api_key_failed = False

    # Empty env var name means no API key needed (e.g. local Ollama)
    if not env_var_name:
        _api_key_cache = "no-key-required"
        return _api_key_cache

    env_key = os.environ.get(env_var_name, "").strip()
    if env_key:
        _api_key_cache = env_key
        return env_key

    # Try .env file in workspace root
    from pathlib import Path
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        prefix = f"{env_var_name}="
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(prefix) and not line.startswith("#"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    _api_key_cache = key
                    return key

    _api_key_failed = True
    _api_key_failed_at = time.time()
    raise RuntimeError(f"API key not found: set {env_var_name} env var or add it to .env file")


# Backward-compatible alias
def get_anthropic_api_key() -> str:
    """Fetch Anthropic API key (backward-compatible wrapper for get_api_key)."""
    return get_api_key("ANTHROPIC_API_KEY")


_RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504, 529}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry


def _is_retryable(exc: Exception) -> Tuple[bool, float]:
    """Check if an exception is retryable. Returns (retryable, suggested_delay).

    Retries on: 429 (rate limit), 500/502/503/529 (server errors),
    connection resets, and timeouts.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in _RETRYABLE_HTTP_CODES:
            # Respect Retry-After header for 429s
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            try:
                delay = float(retry_after) if retry_after else 0
            except (ValueError, TypeError):
                delay = 0
            return True, delay
        return False, 0
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True, 0
    return False, 0


# ==========================================================================
# Provider Dispatch Helpers
# ==========================================================================

# Default API endpoints per provider format
_PROVIDER_URLS = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
}


def _build_anthropic_body(model: str, system_prompt: str, user_message: str, max_tokens: int) -> dict:
    """Build Anthropic Messages API request body (with prompt caching)."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ],
        "messages": [{"role": "user", "content": user_message}]
    }


def _build_openai_body(model: str, system_prompt: str, user_message: str, max_tokens: int) -> dict:
    """Build OpenAI-compatible chat completions request body."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
    }


def _make_headers(provider: str, api_key: str) -> dict:
    """Build HTTP headers for the given provider format."""
    if provider == "anthropic":
        return {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
    else:  # openai-compatible
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }


def _extract_response(provider: str, data: dict) -> tuple:
    """Extract (text, input_tokens, output_tokens, cache_read, cache_create, truncated) from API response."""
    if provider == "anthropic":
        usage = data.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        truncated = data.get("stop_reason", "") == "max_tokens"
        content_blocks = data.get("content", [])
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        text = "\n".join(text_parts).strip()
    else:  # openai-compatible
        usage = data.get("usage", {})
        in_tok = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)
        cache_read = 0
        cache_create = 0
        choices = data.get("choices", [{}])
        truncated = choices[0].get("finish_reason", "") == "length" if choices else False
        text = choices[0].get("message", {}).get("content", "").strip() if choices else ""
    return text, in_tok, out_tok, cache_read, cache_create, truncated


def _get_provider_config() -> tuple:
    """Get (provider, base_url, api_key_env) from config. Falls back to Anthropic defaults."""
    try:
        from config import get_config
        cfg = get_config()
        return cfg.models.provider, cfg.models.base_url, cfg.models.api_key_env
    except Exception:
        return "anthropic", None, "ANTHROPIC_API_KEY"


def call_llm(system_prompt: str, user_message: str,
             model: Optional[str] = None,
             max_tokens: int = 4000,
             timeout: float = ANTHROPIC_TIMEOUT,
             max_retries: Optional[int] = None) -> Tuple[Optional[str], float]:
    """Call the configured LLM provider and return (response text, duration).

    Dispatches to Anthropic or OpenAI-compatible API based on config.models.provider.
    Retries up to max_retries times on transient errors with exponential backoff.
    Raises RuntimeError if the API key cannot be retrieved.
    Returns (None, duration) on persistent API/network errors.
    """
    _load_model_config()
    if model is None:
        model = _high_reasoning_model
    # Cap max_tokens to API limits (prevents runaway from batch size * per-item estimate)
    try:
        from config import get_config as _get_cfg
        _tier = 'low' if model and 'haiku' in model else 'high'
        api_max = _get_cfg().models.max_output(_tier)
    except Exception:
        api_max = 16384
    max_tokens = min(max_tokens, api_max)
    # Cost circuit breaker: abort if accumulated cost exceeds safety threshold
    import os as _os
    _COST_CAP = float(_os.environ.get("JANITOR_COST_CAP", "5.0"))
    current_cost = estimate_cost()
    if current_cost > _COST_CAP:
        print(f"[llm_clients] COST CAP EXCEEDED: ${current_cost:.4f} > ${_COST_CAP:.2f}, aborting call", file=sys.stderr)
        return (None, 0.0)

    # Provider dispatch
    provider, base_url, api_key_env = _get_provider_config()
    api_key = get_api_key(api_key_env)
    api_url = base_url or _PROVIDER_URLS.get(provider, _PROVIDER_URLS["openai"])
    headers = _make_headers(provider, api_key)

    if provider == "anthropic":
        body = _build_anthropic_body(model, system_prompt, user_message, max_tokens)
    else:
        body = _build_openai_body(model, system_prompt, user_message, max_tokens)

    start_time = time.time()
    retries = _MAX_RETRIES if max_retries is None else max_retries
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(body).encode(),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                global _usage_input_tokens, _usage_output_tokens, _usage_calls, _usage_cache_read_tokens, _usage_cache_creation_tokens, _usage_by_model
                data = json.loads(resp.read().decode())
                duration = time.time() - start_time
                # Extract response using provider-appropriate format
                text, in_tok, out_tok, cache_read, cache_create, truncated = _extract_response(provider, data)
                # Track token usage (total + per-model)
                _usage_input_tokens += in_tok
                _usage_output_tokens += out_tok
                _usage_cache_read_tokens += cache_read
                _usage_cache_creation_tokens += cache_create
                _usage_calls += 1
                # Per-model tracking for accurate cost estimation
                if model not in _usage_by_model:
                    _usage_by_model[model] = {"input": 0, "output": 0}
                _usage_by_model[model]["input"] += in_tok
                _usage_by_model[model]["output"] += out_tok
                if truncated:
                    print(f"[llm_clients] WARNING: Response truncated (max_tokens) for model={model}", file=sys.stderr)
                return text, duration
        except Exception as e:
            last_error = e
            retryable, retry_after = _is_retryable(e)
            if retryable and attempt < retries:
                delay = max(retry_after, _RETRY_BASE_DELAY * (2 ** attempt))
                code = getattr(e, 'code', '')
                print(f"[llm_clients] Retryable error ({code or type(e).__name__}), "
                      f"attempt {attempt + 1}/{retries + 1}, retrying in {delay:.1f}s",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            break

    duration = time.time() - start_time
    print(f"[llm_clients] LLM API error ({provider}): {last_error}", file=sys.stderr)
    return None, duration


# Backward-compatible alias
call_anthropic = call_llm


_low_warned: bool = False

def call_low_reasoning(prompt: str, max_tokens: int = 200,
                       timeout: float = LOW_REASONING_TIMEOUT,
                       system_prompt: str = "Respond with JSON only. No explanation, no markdown fencing.",
                       max_retries: Optional[int] = None) -> Tuple[Optional[str], float]:
    """Call the low-reasoning model and return (response text, duration).

    Used for: dedup verification, contradiction detection, edge extraction (batched), HyDE.
    Never raises; returns (None, duration) on any error (including missing API key).
    Logs API-key errors once, then goes silent to avoid log spam.
    """
    global _low_warned
    _load_model_config()
    try:
        return call_anthropic(
            system_prompt=system_prompt,
            user_message=prompt,
            model=_low_reasoning_model,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
        )
    except RuntimeError as e:
        if not _low_warned:
            print(f"[llm_clients] Low-reasoning model unavailable: {e}", file=sys.stderr)
            _low_warned = True
        return None, 0.0


_high_warned: bool = False

def call_high_reasoning(prompt: str, system_prompt: str = "Respond with JSON only. No explanation, no markdown fencing.",
                        max_tokens: int = 2000,
                        timeout: float = ANTHROPIC_TIMEOUT) -> Tuple[Optional[str], float]:
    """Call the high-reasoning model and return (response text, duration).

    Used for: memory review, workspace audit, contradiction resolution, edge extraction.
    Never raises; returns (None, duration) on any error.
    """
    global _high_warned
    _load_model_config()
    try:
        return call_anthropic(
            system_prompt=system_prompt,
            user_message=prompt,
            model=_high_reasoning_model,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except RuntimeError as e:
        if not _high_warned:
            print(f"[llm_clients] High-reasoning model unavailable: {e}", file=sys.stderr)
            _high_warned = True
        return None, 0.0


# Backward-compatible aliases (deprecated — use call_low_reasoning / call_high_reasoning)
call_haiku = call_low_reasoning
call_opus = call_high_reasoning
HAIKU_TIMEOUT = LOW_REASONING_TIMEOUT


def parse_json_response(text: str) -> Optional[object]:
    """Strip markdown fences and parse JSON from an LLM response.

    Handles responses wrapped in ```json ... ``` blocks as well as bare JSON.
    Returns parsed JSON (dict or list) or None on failure.
    """
    if not text:
        return None

    cleaned = text.strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate and (candidate.startswith("{") or candidate.startswith("[")):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue

    # Last resort: find first { or [ and try to parse from there
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start_idx = cleaned.find(start_char)
        end_idx = cleaned.rfind(end_char)
        if start_idx != -1 and end_idx > start_idx:
            try:
                return json.loads(cleaned[start_idx:end_idx + 1])
            except json.JSONDecodeError:
                continue

    return None


# ==========================================================================
# LLM Output Validation
# ==========================================================================

from dataclasses import dataclass
from typing import List as _List


@dataclass
class ReviewDecision:
    """Validated review decision from Opus."""
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

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            # Map dict keys to dataclass fields (case-insensitive for common typos)
            field_names = {f.name for f in schema_class.__dataclass_fields__.values()}
            mapped = {}
            for k, v in item.items():
                k_lower = k.lower().replace("-", "_")
                if k_lower in field_names:
                    mapped[k_lower] = v
                elif k in field_names:
                    mapped[k] = v

            obj = schema_class(**mapped)
            results.append(obj)
        except (TypeError, ValueError) as e:
            print(f"[llm_clients] Validation warning: {e} for item {item}", file=sys.stderr)
            continue

    return results
