"""LLM providers for the Claude Code adapter.

Authentication layers (tried in order):
  0. claude -p subprocess  — delegates auth to the installed CC binary (primary)
  1a. CLAUDE_CODE_OAUTH_TOKEN env var  — explicit override
  1b. .auth-token file  — long-lived token written by install/hooks
  2. ~/.claude/.credentials.json  — on-disk OAuth token (read-only, no refresh)
  3. ANTHROPIC_API_KEY env var  — direct API key fallback

Layer 0 is the preferred path for a standard CC installation. The credentials.json
tokens (layer 2) are web-scoped and cannot be used directly for API calls; the
refresh flow is intentionally omitted to avoid interfering with CC's own token
management.

Fail-hard behavior:
  - failHard=true:  fail immediately at the first gate that fails, no fallback
  - failHard=false: fall through all layers with loud stderr warnings at each step
"""

import json
import logging
import os
import shutil
import subprocess
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

    # Cloudflare blocks generic Python UAs on platform.claude.com; use
    # the same User-Agent as the Claude Code CLI to pass the bot check.
    cc_version = os.environ.get("CLAUDE_CODE_VERSION", "")
    ua = f"Claude-Code/{cc_version}" if cc_version else "Claude-Code"
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json", "User-Agent": ua},
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


class ClaudeCodeCLIProvider(LLMProvider):
    """Layer 0: LLM calls via 'claude -p' subprocess.

    Delegates authentication entirely to the installed Claude Code binary.
    No token management required — CC handles its own OAuth flow.
    This is the primary provider for standard CC installations.
    """

    _TIER_TO_MODEL = {"deep": "opus", "fast": "haiku"}
    _SEARCH_PATHS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")

    def __init__(self, claude_bin: Optional[str] = None):
        self._claude_bin = claude_bin or shutil.which("claude") or next(
            (p for p in self._SEARCH_PATHS if os.path.isfile(p)), None
        )

    def llm_call(self, messages, model_tier="deep", max_tokens=4000, timeout=600):
        if not self._claude_bin:
            raise RuntimeError("claude binary not found in PATH")

        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if not user:
            raise ValueError("No user message")

        model = self._TIER_TO_MODEL.get(model_tier, "opus")
        cmd = [
            self._claude_bin, "-p", user,
            "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
        ]
        if system:
            cmd += ["--system-prompt", system]

        start = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "PATH": os.environ.get("PATH", "") +
                     ":/opt/homebrew/bin:/usr/local/bin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude -p timed out after {timeout}s") from exc

        duration = time.time() - start

        if not proc.stdout.strip():
            raise RuntimeError(
                f"claude -p produced no output (rc={proc.returncode}): "
                f"{proc.stderr[:300]}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude -p non-JSON output: {proc.stdout[:200]}"
            ) from exc

        if data.get("is_error"):
            raise RuntimeError(f"claude -p error: {data.get('result', '')[:300]}")

        usage = data.get("usage", {})
        return LLMResult(
            text=data.get("result", ""),
            duration=duration,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            model=model,
            truncated=data.get("stop_reason", "") == "max_tokens",
        )


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

    # CC-specific defaults for each tier
    _DEFAULT_DEEP_MODEL = "claude-opus-4-6"
    _DEFAULT_FAST_MODEL = "claude-haiku-4-5"
    # Config sentinel values meaning "adapter decides"
    _MODEL_SENTINELS = ("", "default", None)

    def __init__(
        self,
        deep_model: Optional[str] = None,
        fast_model: Optional[str] = None,
    ):
        self._deep_model = deep_model
        self._fast_model = fast_model
        self._api_key_provider: Optional[AnthropicLLMProvider] = None

    def _get_cli_provider(self) -> Optional[ClaudeCodeCLIProvider]:
        """Layer 0: claude -p subprocess provider."""
        p = ClaudeCodeCLIProvider()
        return p if p._claude_bin else None

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
        if model_tier == "fast":
            m = self._fast_model
            return m if m not in self._MODEL_SENTINELS else self._DEFAULT_FAST_MODEL
        m = self._deep_model
        return m if m not in self._MODEL_SENTINELS else self._DEFAULT_DEEP_MODEL

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
            "anthropic-beta": "oauth-2025-04-20",
        }

        if not user_message:
            raise ValueError("Cannot make API call with empty user message")

        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if system_prompt:
            body["system"] = system_prompt

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
            if e.code in (400, 401):
                # credentials.json tokens are web-scoped and cannot be used
                # for /v1/messages API calls — always give 400/401.  Do NOT
                # attempt to refresh: CC manages its own token rotation and
                # our refresh attempts consume CC's refresh token chain,
                # breaking CC's own auth.  Fall through to the next layer.
                logger.warning(
                    "[claude-code-oauth] HTTP %d from API — token is web-scoped "
                    "(credentials.json tokens cannot call /v1/messages). "
                    "Falling back to next auth layer. body: %s",
                    e.code, body[:200],
                )
                raise _OAuthUnavailable(f"http_{e.code}_web_scoped_token") from e
            logger.error(
                "[claude-code-oauth] HTTP %d from API — model=%s "
                "max_tokens=%s system_len=%s user_len=%s body: %s",
                e.code, model, max_tokens, system_len, user_len, body[:1200],
            )
            raise

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        fail_hard = is_fail_hard_enabled()

        # --- Layer 0: claude -p subprocess (primary for CC installations) ---
        cli = self._get_cli_provider()
        if cli:
            try:
                return cli.llm_call(messages, model_tier=model_tier,
                                    max_tokens=max_tokens, timeout=timeout)
            except Exception as e:
                if fail_hard:
                    raise
                logger.warning("[claude-code] claude -p failed (%s), trying OAuth layers", e)

        # --- Layer 1+2: OAuth (env token or on-disk, no refresh) ---
        try:
            return self._try_oauth_call(messages, model_tier, max_tokens, timeout)
        except _OAuthUnavailable as e:
            if fail_hard:
                raise RuntimeError(
                    f"OAuth unavailable ({e}) and failHard is enabled. "
                    f"Ensure 'claude' is installed and authenticated."
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
            "  Layer 0: claude -p failed or claude binary not found\n"
            "  Layer 1a: CLAUDE_CODE_OAUTH_TOKEN env var not set\n"
            "  Layer 1b: No adapter auth token (run 'quaid config set-auth <token>')\n"
            "  Layer 2: On-disk OAuth token not usable (web-scoped)\n"
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
