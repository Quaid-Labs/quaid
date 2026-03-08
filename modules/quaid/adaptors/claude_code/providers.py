"""LLM providers for the Claude Code adapter.

ClaudeCodeOAuthLLMProvider — Direct API calls using Claude Code's OAuth
access token from ~/.claude/.credentials.json. Handles token refresh
automatically. Falls back to ClaudeCodeLLMProvider (claude -p subprocess)
with a noisy stderr warning if OAuth is completely unavailable.
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

from lib.providers import ClaudeCodeLLMProvider, LLMProvider, LLMResult

logger = logging.getLogger(__name__)

# Claude Code OAuth constants
_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Refresh 5 minutes before expiry (same buffer Claude Code uses)
_REFRESH_BUFFER_MS = 300_000


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
    """Exchange a refresh token for a new access token.

    POST https://platform.claude.com/v1/oauth/token
    {grant_type: "refresh_token", refresh_token: "...", client_id: "..."}

    Returns updated OAuth block on success, None on failure.
    """
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
    # 1. Check env var override (no expiry management)
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token, "ok"

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


class ClaudeCodeOAuthLLMProvider(LLMProvider):
    """Direct Anthropic API calls using Claude Code's OAuth access token.

    Token lifecycle:
    1. Read from CLAUDE_CODE_OAUTH_TOKEN env or ~/.claude/.credentials.json
    2. If expired, refresh via platform.claude.com/v1/oauth/token
    3. If refresh fails, fall back to claude -p with noisy warning
    4. On 401 during API call, attempt one refresh+retry before fallback

    Falls back to ClaudeCodeLLMProvider (claude -p subprocess) if OAuth
    is completely unavailable.
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
        self._fallback: Optional[ClaudeCodeLLMProvider] = None

    def _get_fallback(self) -> ClaudeCodeLLMProvider:
        if self._fallback is None:
            self._fallback = ClaudeCodeLLMProvider(
                deep_model=self._deep_model,
                fast_model=self._fast_model,
            )
        return self._fallback

    def _resolve_model(self, model_tier: str) -> str:
        if model_tier == "fast" and self._fast_model:
            return self._fast_model
        return self._deep_model

    def _api_call(self, token: str, model: str, messages: list,
                  max_tokens: int, timeout: float) -> LLMResult:
        """Make a single API call with the given token."""
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

    def _fallback_with_warning(self, messages, model_tier, max_tokens, timeout,
                               reason: str):
        """Fall back to claude -p with a noisy stderr warning."""
        print(
            f"[quaid][WARN] OAuth unavailable ({reason}); falling back to claude -p "
            "(slower due to subprocess overhead). "
            "Run 'claude login' to refresh your Claude Code credentials.",
            file=sys.stderr,
        )
        return self._get_fallback().llm_call(
            messages, model_tier=model_tier,
            max_tokens=max_tokens, timeout=timeout,
        )

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        token, status = _get_valid_token()

        if not token:
            return self._fallback_with_warning(
                messages, model_tier, max_tokens, timeout, reason=status,
            )

        model = self._resolve_model(model_tier)

        try:
            return self._api_call(token, model, messages, max_tokens, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                logger.error("Anthropic API HTTP %d", e.code)
                raise

            # 401 — try one refresh+retry before falling back
            logger.info("[claude-code-oauth] 401 on API call, attempting token refresh")
            oauth = _read_credentials()
            refresh_tok = oauth.get("refreshToken", "") if oauth else ""
            if not refresh_tok:
                return self._fallback_with_warning(
                    messages, model_tier, max_tokens, timeout,
                    reason="401 and no refresh token",
                )

            new_block = _refresh_token(refresh_tok)
            if not new_block:
                return self._fallback_with_warning(
                    messages, model_tier, max_tokens, timeout,
                    reason="401 and refresh failed",
                )

            _write_credentials(new_block)
            try:
                return self._api_call(
                    new_block["accessToken"], model, messages, max_tokens, timeout,
                )
            except Exception:
                return self._fallback_with_warning(
                    messages, model_tier, max_tokens, timeout,
                    reason="401 persisted after refresh",
                )
        except Exception as e:
            logger.error("Anthropic OAuth API error: %s", e)
            raise

    def get_profiles(self):
        return {
            "deep": {"model": self._deep_model, "available": True},
            "fast": {"model": self._fast_model, "available": True},
        }
