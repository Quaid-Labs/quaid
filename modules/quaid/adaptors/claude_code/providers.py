"""LLM providers for the Claude Code adapter.

3-layer authentication fallback:
  1. CLAUDE_CODE_OAUTH_TOKEN env var (explicit override)
  2. On-disk OAuth token from ~/.claude/.credentials.json (with refresh)
  3. ANTHROPIC_API_KEY env var (direct API key)

Fail-hard behavior:
  - failHard=true:  fail immediately at the first gate that fails, no fallback
  - failHard=false: fall through all 3 layers with loud stderr warnings at each step
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from lib.fail_policy import is_fail_hard_enabled
from lib.providers import AnthropicLLMProvider, LLMProvider, LLMResult

logger = logging.getLogger(__name__)

# Claude Code OAuth constants
_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh 5 minutes before expiry (same buffer Claude Code uses)
_REFRESH_BUFFER_MS = 300_000

# Track warnings per-process to avoid spam on every LLM call
_warned_oauth: bool = False
_warned_api_key: bool = False


def _read_credentials() -> Optional[dict]:
    """Read the claudeAiOauth block from credentials file."""
    if not _CREDS_PATH.exists():
        return None
    try:
        with open(_CREDS_PATH, "r", encoding="utf-8") as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth", {})
        return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning("[claude-code-oauth] Failed to read credentials: %s", e)
        return None


def _write_credentials(oauth_block: dict) -> bool:
    """Write updated OAuth block back to credentials file."""
    try:
        creds = {}
        if _CREDS_PATH.exists():
            with open(_CREDS_PATH, "r", encoding="utf-8") as f:
                creds = json.load(f)
        creds["claudeAiOauth"] = oauth_block
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CREDS_PATH, "w", encoding="utf-8") as f:
            json.dump(creds, f, indent=2)
        return True
    except (IOError, OSError) as e:
        logger.warning("[claude-code-oauth] Failed to write credentials: %s", e)
        return False


def _refresh_token(refresh_token: str) -> Optional[dict]:
    """Exchange a refresh token for a new access token."""
    endpoint = os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL", _TOKEN_ENDPOINT)
    client_id = os.environ.get("CLAUDE_CODE_OAUTH_CLIENT_ID", _CLIENT_ID)

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()

    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.warning("[claude-code-oauth] Token refresh failed: %s", e)
        return None

    if not isinstance(data, dict) or "access_token" not in data:
        logger.warning("[claude-code-oauth] Refresh response missing access_token")
        return None

    expires_in_s = data.get("expires_in", 3600)
    new_block = {
        "accessToken": data["access_token"],
        "refreshToken": data.get("refresh_token", refresh_token),
        "expiresAt": int(time.time() * 1000) + (expires_in_s * 1000),
    }

    # Preserve existing metadata
    existing = _read_credentials()
    if existing:
        for key in ("scopes", "subscriptionType", "rateLimitTier"):
            if key in existing and key not in new_block:
                new_block[key] = existing[key]

    return new_block


def _read_token_file() -> Optional[str]:
    """Read a long-lived token from the adapter's auth token path.

    The CC adapter stores its token at QUAID_HOME/config/adapters/claude-code/.auth-token.
    This is the recommended auth method for long-running processes like the
    extraction daemon, since it re-reads the file on every call.
    """
    try:
        from lib.adapter import get_adapter
        return get_adapter().read_auth_token()
    except Exception as e:
        logger.debug("[claude-code-oauth] adapter token read error: %s", e)
        return None


def _get_valid_token() -> Tuple[Optional[str], str]:
    """Get a valid OAuth access token, refreshing if needed.

    Returns:
        (token, status) where status is one of:
        - "ok": token is valid
        - "refreshed": token was expired, successfully refreshed
        - "no_credentials": no credentials file found
        - "refresh_failed": token expired and refresh failed
        - "no_refresh_token": token expired but no refresh token available
    """
    # 1a. Check env var override (no expiry management)
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token, "ok"

    # 1b. Check config-driven token file (long-lived, re-read each call)
    file_token = _read_token_file()
    if file_token:
        return file_token, "ok"

    # 2. Read from credentials file
    oauth = _read_credentials()
    if not oauth:
        return None, "no_credentials"

    token = oauth.get("accessToken", "")
    if not token:
        return None, "no_credentials"

    # 3. Check expiry (expiresAt is in milliseconds)
    expires_at_ms = oauth.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)

    if expires_at_ms and now_ms < (expires_at_ms - _REFRESH_BUFFER_MS):
        # Token is still valid (with buffer)
        return token, "ok"

    # 4. Token expired or expiring soon — try refresh
    refresh_token = oauth.get("refreshToken", "")
    if not refresh_token:
        logger.warning("[claude-code-oauth] Token expired and no refreshToken available")
        return None, "no_refresh_token"

    logger.info("[claude-code-oauth] Token expired or expiring soon, refreshing...")
    new_block = _refresh_token(refresh_token)
    if not new_block:
        return None, "refresh_failed"

    # 5. Write refreshed credentials back
    if _write_credentials(new_block):
        logger.info("[claude-code-oauth] Token refreshed and saved")
    else:
        logger.warning("[claude-code-oauth] Token refreshed but failed to persist")

    return new_block["accessToken"], "refreshed"


def _warn_oauth_fallback(reason: str) -> None:
    """Emit a loud, user-facing warning about OAuth failure. Once per process."""
    global _warned_oauth
    if _warned_oauth:
        return
    _warned_oauth = True
    print(
        f"\n[quaid][WARNING] OAuth token unavailable ({reason}).\n"
        f"  Claude Code's on-disk token (~/.claude/.credentials.json) is expired\n"
        f"  and could not be refreshed.\n"
        f"\n"
        f"  Falling back to ANTHROPIC_API_KEY.\n"
        f"  To fix: run 'claude setup-token', then 'quaid config set-auth <token>'\n"
        f"  to store it in the adapter's auth path.\n",
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
        f"  'quaid config set-auth <token>'.\n",
        file=sys.stderr,
    )


class _OAuthUnavailable(Exception):
    """Internal signal: OAuth path failed, try next layer."""
    pass


class ClaudeCodeOAuthLLMProvider(LLMProvider):
    """3-layer auth fallback for Claude Code LLM calls.

    Layer 1: CLAUDE_CODE_OAUTH_TOKEN env var (explicit override)
    Layer 2: On-disk OAuth from ~/.claude/.credentials.json (with refresh)
    Layer 3: ANTHROPIC_API_KEY env var (direct API key, uses personal quota)

    Fail-hard (retrieval.failHard=true):
        Fails at the first gate. No fallback chain.

    Fail-soft (retrieval.failHard=false):
        Falls through all 3 layers with loud stderr warnings.
        If all 3 fail, raises RuntimeError.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        deep_model: str = "claude-opus-4-6",
        fast_model: str = "claude-haiku-4-5",
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
            deep_model=self._deep_model,
            fast_model=self._fast_model,
        )
        return self._api_key_provider

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

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
            "Authorization": f"Bearer {token}",
            "anthropic-version": self.ANTHROPIC_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31,oauth-2025-04-20",
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

        try:
            return self._api_call(token, model, messages, max_tokens, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise

            # 401 — try one refresh+retry
            logger.info("[claude-code-oauth] 401 on API call, attempting token refresh")
            oauth = _read_credentials()
            refresh_tok = oauth.get("refreshToken", "") if oauth else ""
            if not refresh_tok:
                raise _OAuthUnavailable("401_no_refresh_token") from e

            new_block = _refresh_token(refresh_tok)
            if not new_block:
                raise _OAuthUnavailable("401_refresh_failed") from e

            _write_credentials(new_block)
            try:
                return self._api_call(
                    new_block["accessToken"], model, messages, max_tokens, timeout,
                )
            except Exception:
                raise _OAuthUnavailable("401_persisted_after_refresh") from e

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        fail_hard = is_fail_hard_enabled()

        # --- Layer 1+2: OAuth (env token or on-disk with refresh) ---
        try:
            return self._try_oauth_call(messages, model_tier, max_tokens, timeout)
        except _OAuthUnavailable as e:
            reason = str(e)
            if fail_hard:
                raise RuntimeError(
                    f"OAuth unavailable ({reason}) and failHard is enabled. "
                    f"Set CLAUDE_CODE_OAUTH_TOKEN, run 'claude login', "
                    f"or set ANTHROPIC_API_KEY and disable failHard."
                ) from e
            _warn_oauth_fallback(reason)
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
            "  Layer 1b: No adapter auth token (run 'quaid config set-auth <token>')\n"
            "  Layer 2: On-disk OAuth token expired, refresh failed\n"
            "  Layer 3: ANTHROPIC_API_KEY not set\n"
            "\n"
            "Fix: run 'claude setup-token', then 'quaid config set-auth <token>', "
            "or set ANTHROPIC_API_KEY."
        )

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": True},
            "fast": {"model": self._fast_model, "available": True},
        }
