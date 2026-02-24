"""Platform adapter layer — decouples Quaid core from any specific host.

Provides an abstract interface that Quaid modules call for:
- Path resolution (home dir, data dir, config dir, etc.)
- Notifications (send messages to the user)
- Credentials (API key lookup)
- Session access (conversation transcripts)
- Platform-specific filtering (HEARTBEAT, gateway messages)

Two concrete adapters ship out of the box:
- StandaloneAdapter: works anywhere (~/quaid/)
- OpenClawAdapter: for OpenClaw gateway runtime

Adapter selection (get_adapter()):
1. config/memory.json adapter type  (required)

Tests use set_adapter() / reset_adapter() for isolation.
"""

import abc
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from lib.providers import EmbeddingsProvider, LLMProvider


@dataclass
class ChannelInfo:
    """User's last active channel information."""
    channel: str      # telegram, whatsapp, discord, etc.
    target: str       # chat id, phone number, channel id
    account_id: str   # account identifier (usually "default")
    session_key: str  # session key for reference


class QuaidAdapter(abc.ABC):
    """Abstract interface for platform-specific behavior."""

    # ---- Paths ----

    @abc.abstractmethod
    def quaid_home(self) -> Path:
        """Root directory for all Quaid data (config, data, logs, etc.)."""
        ...

    def data_dir(self) -> Path:
        return self.quaid_home() / "data"

    def config_dir(self) -> Path:
        return self.quaid_home() / "config"

    def logs_dir(self) -> Path:
        return self.quaid_home() / "logs"

    def journal_dir(self) -> Path:
        return self.quaid_home() / "journal"

    def projects_dir(self) -> Path:
        return self.quaid_home() / "projects"

    def core_markdown_dir(self) -> Path:
        return self.quaid_home()

    # ---- Notifications ----

    @abc.abstractmethod
    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False) -> bool:
        """Send a notification message to the user. Returns True on success."""
        ...

    @abc.abstractmethod
    def get_last_channel(self, session_key: str = "") -> Optional[ChannelInfo]:
        """Get the user's last active channel from session state."""
        ...

    # ---- Credentials ----

    @abc.abstractmethod
    def get_api_key(self, env_var_name: str) -> Optional[str]:
        """Retrieve an API key by environment variable name.

        Resolution chain is adapter-specific. Returns None if not found.
        """
        ...

    # ---- Sessions ----

    @abc.abstractmethod
    def get_sessions_dir(self) -> Optional[Path]:
        """Get the directory containing session transcript files."""
        ...

    def get_session_path(self, session_id: str) -> Optional[Path]:
        """Get the path to a specific session's JSONL file."""
        sessions_dir = self.get_sessions_dir()
        if sessions_dir is None:
            return None
        path = sessions_dir / f"{session_id}.jsonl"
        return path if path.exists() else None

    # ---- Platform filtering ----

    @abc.abstractmethod
    def filter_system_messages(self, text: str) -> bool:
        """Return True if this message should be filtered out of transcripts."""
        ...

    def get_bootstrap_markdown_globs(self) -> List[str]:
        """Return host-managed markdown bootstrap glob patterns.

        Adapters should return workspace-relative glob patterns used to inject
        project markdown into runtime bootstrap context.
        """
        return []

    def get_bootstrap_markdown_filenames(self) -> List[str]:
        """Return host-managed markdown bootstrap file name allowlist.

        When provided, callers should treat this as the source of truth for which
        matched files from bootstrap globs are actually in scope.
        """
        return []

    def should_filter_transcript_message(self, text: str) -> bool:
        """Adapter-specific transcript noise filtering."""
        return self.filter_system_messages(text)

    def build_transcript(self, messages: List[Dict]) -> str:
        """Format role/content messages into a normalized transcript."""
        parts: List[str] = []
        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue

            text = msg.get("content", "")
            if isinstance(text, list):
                text = " ".join(
                    b.get("text", "") for b in text if isinstance(b, dict)
                )
            if not isinstance(text, str):
                continue

            text = re.sub(
                r"^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )
            text = re.sub(r"\n?\[message_id:\s*\d+\]", "", text, flags=re.IGNORECASE).strip()
            if not text or self.should_filter_transcript_message(text):
                continue

            label = "User" if role == "user" else "Assistant"
            parts.append(f"{label}: {text}")

        return "\n\n".join(parts)

    def parse_session_jsonl(self, path: Path) -> str:
        """Parse platform session JSONL into a normalized transcript."""
        messages = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") == "message" and "message" in obj:
                    obj = obj["message"]

                role = obj.get("role")
                if role not in ("user", "assistant"):
                    continue

                content = obj.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                if not isinstance(content, str) or not content.strip():
                    continue

                messages.append({"role": role, "content": content.strip()})

        return self.build_transcript(messages)

    # ---- Gateway config (optional) ----

    def get_gateway_config_path(self) -> Optional[Path]:
        """Path to the gateway config file (if applicable)."""
        return None

    # ---- Providers ----

    @abc.abstractmethod
    def get_llm_provider(self) -> "LLMProvider":
        """Produce the configured LLM provider for this platform.

        Reads user selection from config if multiple providers available.
        """
        ...

    def get_embeddings_provider(self) -> Optional["EmbeddingsProvider"]:
        """Produce an embeddings provider, if this platform provides one.

        Returns None if embeddings should be handled by a standalone provider
        (e.g. OllamaEmbeddingsProvider).
        """
        return None

    def discover_llm_providers(self) -> List[Dict]:
        """Discover all available LLM providers on this platform.

        Returns list of dicts:
            [{"id": "default", "name": "Default", "provider": "anthropic", ...}]

        Used at install time for user selection.
        """
        return []

    def discover_embeddings_providers(self) -> List[Dict]:
        """Discover available embeddings providers on this platform.

        Returns list of dicts:
            [{"id": "ollama", "name": "Ollama (local)", ...}]
        """
        return []

    # ---- Identity ----

    def get_repo_slug(self) -> str:
        return "steadman-labs/quaid"

    def get_install_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.get_repo_slug()}/main/install.sh"


class StandaloneAdapter(QuaidAdapter):
    """Default adapter for standalone Quaid installations.

    - Home dir: QUAID_HOME env or ~/quaid/
    - Notifications: stderr
    - Credentials: env var → .env file in quaid home
    - Sessions: quaid_home/sessions/ (if exists)
    - Filtering: no platform messages to filter
    - LLM: AnthropicLLMProvider (direct API with key from .env)
    """

    def __init__(self, home: Optional[Path] = None):
        self._home = home

    def quaid_home(self) -> Path:
        if self._home is not None:
            return self._home
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env) if env else Path.home() / "quaid"

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False) -> bool:
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS"):
            return True
        if dry_run:
            print(f"[notify] (dry-run) {message}", file=sys.stderr)
            return True
        print(f"[quaid] {message}", file=sys.stderr)
        return True

    def get_last_channel(self, session_key: str = "") -> Optional[ChannelInfo]:
        return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        # 1. Environment variable
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key

        # 2. .env file in quaid home
        env_file = self.quaid_home() / ".env"
        if env_file.exists():
            return _read_env_file(env_file, env_var_name)

        return None

    def get_sessions_dir(self) -> Optional[Path]:
        d = self.quaid_home() / "sessions"
        return d if d.is_dir() else None

    def filter_system_messages(self, text: str) -> bool:
        return False

    def get_llm_provider(self):
        from lib.providers import AnthropicLLMProvider, ClaudeCodeLLMProvider

        # Resolve provider from config — models.llmProvider MUST be set.
        # No ENV sniffing, no fallbacks. Explicit config only.
        from config import get_config
        cfg = get_config()
        provider_id = cfg.models.llm_provider

        if not provider_id or provider_id == "default":
            raise RuntimeError(
                "models.llmProvider must be explicitly set in config/memory.json. "
                "Valid values: 'claude-code', 'anthropic', 'openai-compatible'. "
                "No default fallback — set it explicitly."
            )

        if provider_id == "openai-compatible":
            from lib.providers import OpenAICompatibleLLMProvider
            import os
            base_url = os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "http://localhost:8000")
            api_key = os.environ.get("OPENAI_API_KEY", "")
            return OpenAICompatibleLLMProvider(
                base_url=base_url, api_key=api_key,
                deep_model=cfg.models.deep_reasoning,
                fast_model=cfg.models.fast_reasoning,
            )

        if provider_id == "claude-code":
            return ClaudeCodeLLMProvider()

        if provider_id == "anthropic":
            api_key = self.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'anthropic' but ANTHROPIC_API_KEY not found. "
                    "Set it in your environment or in ~/quaid/.env."
                )
            return AnthropicLLMProvider(api_key=api_key)

        raise RuntimeError(
            f"Unknown LLM provider '{provider_id}'. "
            "Valid values: 'claude-code', 'anthropic', 'openai-compatible'."
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _read_env_file(env_file: Path, var_name: str) -> Optional[str]:
    """Read a variable from a .env file.

    Handles: KEY=value, KEY="value", KEY='value', inline # comments,
    comment lines, and empty values.  Does NOT handle ``export`` prefix
    or multi-line values (these are uncommon in .env files).
    """
    prefix = f"{var_name}="
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or not line.startswith(prefix):
                continue
            value = line.split("=", 1)[1].strip()
            # Quoted values: extract content between matching quotes
            if value.startswith('"') and '"' in value[1:]:
                value = value[1:value.index('"', 1)]
            elif value.startswith("'") and "'" in value[1:]:
                value = value[1:value.index("'", 1)]
            else:
                # Unquoted: strip inline comments
                if " #" in value:
                    value = value[:value.index(" #")].rstrip()
            if value:
                return value
    except (IOError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Test adapter
# ---------------------------------------------------------------------------

class TestAdapter(StandaloneAdapter):
    """Test adapter with canned LLM responses and call recording.

    Usage in tests::

        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        # ... code under test calls get_adapter().get_llm_provider() ...
        assert len(adapter.llm_calls) == 1
    """
    __test__ = False  # Not a pytest test class

    def __init__(self, home: Path, responses: Optional[Dict] = None):
        super().__init__(home=home)
        from lib.providers import TestLLMProvider
        self._llm = TestLLMProvider(responses)

    def get_llm_provider(self):
        return self._llm

    @property
    def llm_calls(self) -> list:
        return self._llm.calls


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_adapter: Optional[QuaidAdapter] = None


def _adapter_config_paths() -> List[Path]:
    """Candidate config files for adapter selection (priority order)."""
    paths: List[Path] = []

    quaid_home = os.environ.get("QUAID_HOME", "").strip()
    if quaid_home:
        paths.append(Path(quaid_home) / "config" / "memory.json")

    openclaw_workspace = os.environ.get("CLAWDBOT_WORKSPACE", "").strip()
    if openclaw_workspace:
        paths.append(Path(openclaw_workspace) / "config" / "memory.json")

    cwd = Path.cwd()
    paths.append(cwd / "config" / "memory.json")
    paths.append(cwd / "memory-config.json")

    # De-duplicate while preserving order
    seen = set()
    unique: List[Path] = []
    for p in paths:
        sp = str(p)
        if sp not in seen:
            seen.add(sp)
            unique.append(p)
    return unique


def _read_adapter_type_from_config() -> str:
    """Read adapter type from config file.

    Accepted formats:
      {"adapter": "standalone"}
      {"adapter": {"type": "openclaw"}}
    """
    last_existing: Optional[Path] = None
    for cfg_path in _adapter_config_paths():
        if not cfg_path.exists():
            continue
        last_existing = cfg_path
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"Failed to read adapter config from {cfg_path}: {e}") from e

        adapter_cfg = data.get("adapter")
        if isinstance(adapter_cfg, str):
            kind = adapter_cfg.strip().lower()
        elif isinstance(adapter_cfg, dict):
            kind = str(
                adapter_cfg.get("type")
                or adapter_cfg.get("kind")
                or adapter_cfg.get("id")
                or ""
            ).strip().lower()
        else:
            kind = ""

        if kind in ("standalone", "openclaw"):
            return kind
        raise RuntimeError(
            f"Config {cfg_path} must set adapter type to 'standalone' or "
            f"'openclaw' (found: {adapter_cfg!r})."
        )

    searched = ", ".join(str(p) for p in _adapter_config_paths())
    if last_existing is None:
        raise RuntimeError(
            "No config file found for adapter selection. Create config/memory.json "
            "with {\"adapter\": {\"type\": \"standalone\"|\"openclaw\"}}. "
            f"Searched: {searched}"
        )
    raise RuntimeError("Adapter type could not be resolved from config.")


def get_adapter() -> QuaidAdapter:
    """Get the current adapter (resolved on first call).

    Selection:
    1. config/memory.json adapter.type  (required)
    """
    global _adapter
    if _adapter is not None:
        return _adapter

    kind = _read_adapter_type_from_config()
    if kind == "openclaw":
        from adaptors.openclaw.adapter import OpenClawAdapter
        _adapter = OpenClawAdapter()
    elif kind == "standalone":
        _adapter = StandaloneAdapter()
    else:
        raise RuntimeError(
            f"Unsupported adapter type: {kind!r}. "
            "Expected 'standalone' or 'openclaw'."
        )

    return _adapter


def set_adapter(adapter: QuaidAdapter) -> None:
    """Override the adapter (for tests)."""
    global _adapter
    _adapter = adapter


def reset_adapter() -> None:
    """Reset adapter resolution state (for tests).

    Also clears cached providers and config so they re-resolve
    against the next adapter.
    """
    global _adapter
    _adapter = None
    # Clear embeddings provider cache so it re-resolves against the new adapter
    try:
        from lib.embeddings import reset_embeddings_provider
        reset_embeddings_provider()
    except ImportError:
        pass
    # Clear cached model names so they re-resolve from new config/adapter
    try:
        import lib.llm_clients as llm_clients
        llm_clients._models_loaded = False
        llm_clients._fast_reasoning_model = ""
        llm_clients._deep_reasoning_model = ""
        llm_clients._pricing_loaded = False
    except ImportError:
        pass
