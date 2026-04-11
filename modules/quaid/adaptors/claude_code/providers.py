"""LLM providers for the Claude Code adapter.

Authentication layers (tried in order):
  1a. CLAUDE_CODE_OAUTH_TOKEN env var  — explicit override
  1b. .auth-token file  — long-lived setup-token/API token written by install/hooks
  2. ANTHROPIC_API_KEY env var  — direct API key fallback

Fail-hard behavior:
  - failHard=true:  fail immediately at the first gate that fails, no fallback
  - failHard=false: fall through all layers with loud stderr warnings at each step
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import queue_deferred_notice
from lib.providers import AnthropicLLMProvider, LLMProvider, LLMResult

logger = logging.getLogger(__name__)

# Track warnings per-process to avoid spam on every LLM call
_warned_oauth: bool = False
_warned_api_key: bool = False


def _read_token_file() -> Optional[str]:
    """Read a long-lived token from the adapter's auth token path.

    The CC adapter stores its token at QUAID_HOME/adaptors/claude-code/.auth-token.
    This is the recommended auth method for long-running processes like the
    extraction daemon, since it re-reads the file on every call.
    """
    try:
        from lib.adapter import get_adapter
        return get_adapter().read_auth_token()
    except Exception as e:
        logger.debug("[claude-code-oauth] adapter token read error: %s", e)
        return None


def _queue_auth_refresh_notice() -> None:
    """Queue a deferred operator notice for expired/invalid CC auth."""
    try:
        queue_deferred_notice(
            "[Quaid] Your Claude Code Anthropic auth token is no longer valid. To refresh it, run:\n"
            "claude setup-token\n"
            "Then update Quaid with:\n"
            "quaid auth refresh <token>\n"
            "Or, if you keep the token in a file:\n"
            "quaid auth refresh --file /path/to/anthtoken.md\n"
            "Memory features are paused until the token is refreshed.",
            kind="agent_notice",
            priority="high",
            source="claude_code_auth",
            dedupe_key="claude-code-auth-refresh",
        )
    except Exception as exc:
        logger.warning("[claude-code-oauth] failed queuing auth refresh notice: %s", exc)


def _get_valid_token() -> Tuple[Optional[str], str]:
    """Get a valid access token from explicit long-lived token sources.

    Returns:
        (token, status) where status is one of:
        - "ok": token is valid
        - "no_credentials": no long-lived token source found
    """
    # 1a. Check env var override (no expiry management)
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token, "ok"

    # 1b. Check config-driven token file (long-lived, re-read each call)
    file_token = _read_token_file()
    if file_token:
        return file_token, "ok"

    return None, "no_credentials"


def _warn_oauth_fallback(reason: str) -> None:
    """Emit a loud, user-facing warning about token failure. Once per process."""
    global _warned_oauth
    if _warned_oauth:
        return
    _warned_oauth = True
    print(
        f"\n[quaid][WARNING] Claude Code token unavailable ({reason}).\n"
        f"  Quaid could not use its provisioned Anthropic token for Claude Code.\n"
        f"\n"
        f"  Falling back to ANTHROPIC_API_KEY.\n"
        f"  To fix: run 'claude setup-token', then 'quaid auth refresh <token>'\n"
        f"  or 'quaid auth refresh --file /path/to/anthtoken.md'.\n",
        file=sys.stderr,
    )


def _warn_api_key_fallback() -> None:
    """Emit a loud warning when falling back to API key. Once per process."""
    global _warned_api_key
    if _warned_api_key:
        return
    _warned_api_key = True
    print(
        f"\n[quaid][WARNING] Using ANTHROPIC_API_KEY fallback for LLM calls.\n"
        f"  This uses your personal API quota instead of your Claude Code subscription.\n"
        f"  To use subscription: run 'claude setup-token', then\n"
        f"  'quaid auth refresh <token>'.\n",
        file=sys.stderr,
    )


class _OAuthUnavailable(Exception):
    """Internal signal: OAuth path failed, try next layer."""
    pass


class ClaudeCodeOAuthLLMProvider(LLMProvider):
    """Long-lived token auth fallback for Claude Code LLM calls.

    Layer 1: CLAUDE_CODE_OAUTH_TOKEN env var (explicit override)
    Layer 2: On-disk setup-token/API token from adapter auth path
    Layer 3: ANTHROPIC_API_KEY env var (direct API key, uses personal quota)

    Fail-hard (retrieval.failHard=true):
        Fails at the first gate. No fallback chain.

    Fail-soft (retrieval.failHard=false):
        Falls through all 3 layers with loud stderr warnings.
        If all 3 fail, raises RuntimeError.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"
    # CC identity required for OAuth tokens to access sonnet/opus.
    # Without these, the API restricts OAuth callers to haiku.
    _CC_IDENTITY_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
    _CC_USER_AGENT = "claude-cli/2.1.2 (external, cli)"

    # Sentinel values that mean "not configured"
    _MODEL_SENTINELS = ("", "default", None)

    def __init__(
        self,
        deep_model: Optional[str] = None,
        fast_model: Optional[str] = None,
    ):
        self._deep_model = deep_model
        self._fast_model = fast_model
        self._api_key_provider: Optional[AnthropicLLMProvider] = None

    def _get_api_key_provider(self) -> Optional[AnthropicLLMProvider]:
        """Layer 3: API key fallback provider."""
        if self._api_key_provider is not None:
            return self._api_key_provider
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return None
        self._api_key_provider = AnthropicLLMProvider(
            api_key=api_key,
            deep_model=self._resolve_model("deep"),
            fast_model=self._resolve_model("fast"),
        )
        return self._api_key_provider

    def _resolve_model(self, model_tier: str) -> str:
        m = self._fast_model if model_tier == "fast" else self._deep_model
        if m in self._MODEL_SENTINELS:
            raise RuntimeError(
                f"No model configured for tier '{model_tier}'. "
                f"Run 'quaid claudecode make_instance' to set models.deepReasoning "
                f"and models.fastReasoning in the instance config."
            )
        return m

    def _api_call(self, token: str, model: str, messages: list,
                  max_tokens: int, timeout: float) -> LLMResult:
        """Make a single API call with the given OAuth token."""
        system_prompt = ""
        user_message = ""
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            elif m["role"] == "user":
                user_message = m["content"]

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "anthropic-version": self.ANTHROPIC_VERSION,
            # claude-code-20250219 identity beta is required for OAuth tokens
            # to access sonnet/opus — without it the API restricts to haiku.
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            "User-Agent": self._CC_USER_AGENT,
            "x-app": "cli",
        }

        if not user_message:
            raise ValueError("Cannot make API call with empty user message")

        # CC identity block must be first in the system array.
        system_blocks = [
            {
                "type": "text",
                "text": self._CC_IDENTITY_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if system_prompt:
            system_blocks.append({
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            })

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_message}],
        }

        start_time = time.time()
        data_bytes = json.dumps(body).encode()
        req = urllib.request.Request(
            self.ANTHROPIC_API_URL, data=data_bytes,
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())

        duration = time.time() - start_time

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Anthropic API returned non-object JSON for model={model}"
            )

        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        content_blocks = data.get("content", [])
        if not isinstance(content_blocks, list):
            raise RuntimeError(
                f"Anthropic API returned invalid content for model={model}"
            )

        text_parts = [
            b["text"]
            for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
        text = "\n".join(text_parts).strip()

        return LLMResult(
            text=text,
            duration=duration,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            model=data.get("model", model),
            truncated=data.get("stop_reason", "") == "max_tokens",
        )

    def _try_oauth_call(self, messages, model_tier, max_tokens, timeout):
        """Attempt OAuth-based API call (layers 1+2). Returns result or raises."""
        token, status = _get_valid_token()

        if not token:
            raise _OAuthUnavailable(status)

        model = self._resolve_model(model_tier)

        system_len = sum(
            len(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        user_len = sum(
            len(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        logger.info(
            "[claude-code-oauth] API call: model=%s tier=%s max_tokens=%s "
            "system_len=%s user_len=%s",
            model, model_tier, max_tokens, system_len, user_len,
        )
        try:
            return self._api_call(token, model, messages, max_tokens, timeout)
        except urllib.error.HTTPError as e:
            body = "<unread>"
            try:
                body = e.read().decode("utf-8", errors="replace") or "<empty>"
            except Exception:
                pass
            if e.code == 401:
                _queue_auth_refresh_notice()
                logger.warning(
                    "[claude-code-oauth] HTTP 401 from API — queued refresh notice. "
                    "model=%s body: %s",
                    model, body[:400],
                )
                raise _OAuthUnavailable("http_401") from e
            if e.code == 400:
                # 400/401 can mean web-scoped token OR bad model name OR other
                # request errors.  Log the actual API body prominently so
                # failures are diagnosable, then fall through to next layer.
                logger.warning(
                    "[claude-code-oauth] HTTP %d from API — falling back. "
                    "model=%s body: %s",
                    e.code, model, body[:400],
                )
                raise _OAuthUnavailable(f"http_{e.code}") from e
            logger.error(
                "[claude-code-oauth] HTTP %d from API — model=%s "
                "max_tokens=%s system_len=%s user_len=%s body: %s",
                e.code, model, max_tokens, system_len, user_len, body[:1200],
            )
            raise

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        fail_hard = is_fail_hard_enabled()

        # --- Layer 1+2: OAuth (env token or on-disk, no refresh) ---
        try:
            return self._try_oauth_call(messages, model_tier, max_tokens, timeout)
        except _OAuthUnavailable as e:
            if fail_hard:
                raise RuntimeError(
                    f"OAuth unavailable ({e}) and failHard is enabled. "
                    f"Ensure a valid Anthropic token is provisioned via "
                    f"'claude setup-token' and 'quaid auth refresh'."
                ) from e
            _warn_oauth_fallback(str(e))
        except Exception as e:
            if fail_hard:
                raise
            logger.error("[claude-code-oauth] Unexpected error: %s", e)
            _warn_oauth_fallback(f"error: {e}")

        # --- Layer 3: ANTHROPIC_API_KEY fallback ---
        api_provider = self._get_api_key_provider()
        if api_provider:
            _warn_api_key_fallback()
            return api_provider.llm_call(
                messages, model_tier=model_tier,
                max_tokens=max_tokens, timeout=timeout,
            )

        # All layers exhausted
        raise RuntimeError(
            "All LLM auth methods failed.\n"
            "  Layer 1a: CLAUDE_CODE_OAUTH_TOKEN env var not set\n"
            "  Layer 1b: No adapter auth token (run 'quaid auth refresh <token>')\n"
            "  Layer 3: ANTHROPIC_API_KEY not set\n"
            "\n"
            "Fix: run 'claude setup-token', then 'quaid auth refresh <token>' "
            "or 'quaid auth refresh --file /path/to/anthtoken.md', or set ANTHROPIC_API_KEY."
        )

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": True},
            "fast": {"model": self._fast_model, "available": True},
        }
