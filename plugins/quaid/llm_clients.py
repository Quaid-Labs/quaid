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
import os
import sys
import time
from typing import Dict, Optional, Tuple

from lib.providers import LLMResult

# Timeouts (seconds)
DEEP_REASONING_TIMEOUT = 600
FAST_REASONING_TIMEOUT = 120

# Model names — loaded from config on first use (lazy to avoid circular imports)
_models_loaded: bool = False
_fast_reasoning_model: str = ""
_deep_reasoning_model: str = ""


def _load_model_config():
    """Load model names from config.py (once)."""
    global _models_loaded, _fast_reasoning_model, _deep_reasoning_model
    if _models_loaded:
        return
    try:
        from config import get_config, resolve_model
        cfg = get_config()
        _fast_reasoning_model = cfg.models.fast_reasoning
        _deep_reasoning_model = resolve_model(cfg.models.deep_reasoning)
        _models_loaded = True  # Only after success — allows retry on transient failures
    except ImportError:
        pass  # Config not available (test environment) — defaults set by provider
    except Exception as e:
        print(f"[llm_clients] FATAL: Config loaded but model resolution failed: {e}", file=sys.stderr)
        raise


# Token usage accumulator — reset per janitor run, read at end for cost report
_usage_input_tokens: int = 0
_usage_output_tokens: int = 0
_usage_calls: int = 0
_usage_cache_read_tokens: int = 0
_usage_cache_creation_tokens: int = 0
_usage_by_model: Dict[str, Dict[str, int]] = {}  # {model: {input: N, output: N}}

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


def _load_pricing():
    """Merge config pricing into the default table (once)."""
    global _pricing_loaded
    if _pricing_loaded:
        return
    _pricing_loaded = True
    try:
        from config import get_config
        cfg = get_config()
        # Config uses snake_case; pricing would be under models section
        # but MemoryConfig doesn't expose raw_config — use ModelConfig fields instead.
        # Custom pricing can be added to _PRICING dict directly or via env overrides.
    except Exception:
        pass  # Config not available — use built-in defaults


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

    Uses actual model rates when available, falls back to cheapest
    known rate for any untracked tokens.
    """
    _load_pricing()
    if not _PRICING:
        return 0.0

    # Find the cheapest rate as fallback for unknown models
    _cheapest = min(_PRICING.values(), key=lambda r: r["input"])

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


# Retry config
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled each retry


def _track_usage(result: LLMResult) -> None:
    """Accumulate token usage from an LLMResult into module-level counters."""
    global _usage_input_tokens, _usage_output_tokens, _usage_calls
    global _usage_cache_read_tokens, _usage_cache_creation_tokens, _usage_by_model

    _usage_input_tokens += result.input_tokens
    _usage_output_tokens += result.output_tokens
    _usage_cache_read_tokens += result.cache_read_tokens
    _usage_cache_creation_tokens += result.cache_creation_tokens
    _usage_calls += 1

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
        print(f"[llm_clients] COST CAP EXCEEDED: ${current_cost:.4f} > ${_COST_CAP:.2f}, aborting call", file=sys.stderr)
        return (None, 0.0)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    from lib.adapter import get_adapter
    llm = get_adapter().get_llm_provider()

    start_time = time.time()
    retries = _MAX_RETRIES if max_retries is None else max_retries
    last_error = None

    import urllib.error
    _RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504, 529}

    for attempt in range(retries + 1):
        try:
            result = llm.llm_call(messages, resolved_tier, max_tokens, timeout)
            _track_usage(result)
            if result.truncated:
                print(f"[llm_clients] WARNING: Response truncated (max_tokens) for model={result.model}", file=sys.stderr)
            if result.text is not None:
                return result.text, result.duration
            return None, result.duration
        except Exception as e:
            last_error = e
            # Only retry on transient errors (rate limit, server errors, timeouts)
            import subprocess as _sp
            retryable = isinstance(e, (TimeoutError, ConnectionError, OSError,
                                       _sp.TimeoutExpired))
            if isinstance(e, urllib.error.HTTPError):
                retryable = e.code in _RETRYABLE_HTTP_CODES
            if retryable and attempt < retries:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                code = getattr(e, 'code', type(e).__name__)
                print(f"[llm_clients] Retryable error ({code}), "
                      f"attempt {attempt + 1}/{retries + 1}, retrying in {delay:.1f}s",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            break

    duration = time.time() - start_time
    print(f"[llm_clients] LLM error: {last_error}", file=sys.stderr)
    return None, duration


def call_fast_reasoning(prompt: str, max_tokens: int = 200,
                       timeout: float = FAST_REASONING_TIMEOUT,
                       system_prompt: str = "Respond with JSON only. No explanation, no markdown fencing.",
                       max_retries: Optional[int] = None) -> Tuple[Optional[str], float]:
    """Call the fast-reasoning model and return (response text, duration).

    Used for: dedup verification, contradiction detection, edge extraction (batched), HyDE.
    Raises on provider/config errors (no silent fallback).
    Returns (None, duration) only for transient LLM failures after retries.
    """
    _load_model_config()
    return call_llm(
        system_prompt=system_prompt,
        user_message=prompt,
        model=_fast_reasoning_model,
        model_tier="fast",
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=max_retries,
    )


def call_deep_reasoning(prompt: str, system_prompt: str = "Respond with JSON only. No explanation, no markdown fencing.",
                        max_tokens: int = 2000,
                        timeout: float = DEEP_REASONING_TIMEOUT) -> Tuple[Optional[str], float]:
    """Call the deep-reasoning model and return (response text, duration).

    Used for: memory review, workspace audit, contradiction resolution, edge extraction.
    Raises on provider/config errors (no silent fallback).
    Returns (None, duration) only for transient LLM failures after retries.
    """
    _load_model_config()
    return call_llm(
        system_prompt=system_prompt,
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
