"""Platform adapter layer — decouples Quaid core from any specific host.

Provides an abstract interface that Quaid modules call for:
- Path resolution (home dir, data dir, config dir, etc.)
- Notifications (send messages to the user)
- Credentials (API key lookup)
- Session access (conversation transcripts)
- Platform-specific filtering (HEARTBEAT, gateway messages)

Built-in adapters currently include:
- StandaloneAdapter: works anywhere (~/quaid/)
- Additional host-specific adapters from `adaptors/` (for gateway/runtime integrations)
  - OpenClawAdapter: for OpenClaw gateway runtime
  - ClaudeCodeAdapter: for Claude Code sessions (hooks + CLI)
  - CodexAdapter: for Codex CLI/app sessions (hooks + app-server sidecar)

Adapter selection (get_adapter()):
1. config/memory.json adapter type  (required)

Tests use set_adapter() / reset_adapter() for isolation.
"""

import abc
import importlib
import json
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from lib.platform_guard import assert_supported_platform  # noqa: F401
from lib.fail_policy import is_fail_hard_enabled

from lib.host import HostInfo

if TYPE_CHECKING:
    from lib.providers import EmbeddingsProvider, LLMProvider
    from lib.instance_manager import InstanceManager


@dataclass
class ChannelInfo:
    """User's last active channel information."""
    channel: str      # telegram, whatsapp, discord, etc.
    target: str       # chat id, phone number, channel id
    account_id: str   # account identifier (usually "default")
    session_key: str  # session key for reference


class QuaidAdapter(abc.ABC):
    """Abstract interface for platform-specific behavior."""

    _QUAID_TRANSCRIPT_METADATA_PREFIXES = (
        "[Quaid Project Context]",
        "[Quaid Memory Context]",
    )

    # ---- Paths ----

    @abc.abstractmethod
    def quaid_home(self) -> Path:
        """Root directory containing all Quaid instances (QUAID_HOME)."""
        ...

    def instance_id(self) -> str:
        """Instance identifier for this adapter's silo.

        Reads from QUAID_INSTANCE env var. Each instance has its own
        config, data, DB, daemon, and identity under QUAID_HOME/<instance_id>/.
        """
        from lib.instance import instance_id as _instance_id
        return _instance_id()

    def instance_root(self) -> Path:
        """Resolved instance root: QUAID_HOME / INSTANCE_ID."""
        return self.quaid_home() / self.instance_id()

    def data_dir(self) -> Path:
        return self.instance_root() / "data"

    def config_dir(self) -> Path:
        return self.instance_root() / "config"

    def logs_dir(self) -> Path:
        return self.instance_root() / "logs"

    def journal_dir(self) -> Path:
        return self.instance_root() / "journal"

    def projects_dir(self) -> Path:
        """Canonical projects directory (cross-instance)."""
        return self.quaid_home() / "projects"

    def adapter_id(self) -> str:
        """Short identifier for this adapter type (e.g. 'claude-code', 'openclaw').

        Used by core to derive Quaid-managed paths like identity dirs.
        Must be a valid directory name (lowercase, no spaces).
        """
        return "standalone"

    @abc.abstractmethod
    def get_instance_name(self) -> str:
        """Return the stable instance name for the current project/agent context.

        Each adapter derives this from its host's identity anchor:
        - Claude Code: slugifies CLAUDE_PROJECT_DIR (CC-injected project root)
        - OpenClaw: reads QUAID_INSTANCE (injected by TS adapter per agent)
        - Standalone: subclass must implement

        The instance name is combined with the adapter prefix to form the full
        QUAID_INSTANCE silo identifier (e.g. "users-clawdbot-myapp" becomes
        "claude-code-users-clawdbot-myapp").
        """

    def get_host_info(self) -> "HostInfo":
        """Return host platform name, version, and binary path.

        Used by the version watcher for compatibility checking.
        Binary path is used for cheap mtime-based change detection.
        Override in subclasses with platform-specific detection.
        """
        return HostInfo(platform=self.adapter_id(), version="unknown")

    def identity_dir(self) -> Path:
        """Per-instance Quaid-managed identity directory.

        Lives at instance_root/identity/. This is where Quaid writes
        generated identity (SOUL.md, USER.md, ENVIRONMENT.md, *.snippets.md).
        NOT where platform-native context lives (that's get_base_context_files).
        """
        return self.instance_root() / "identity"

    def core_markdown_dir(self) -> Path:
        return self.instance_root()

    def get_instance_type(self) -> str:
        """Return how this adapter determines instance identity.

        Returns:
            "folder" — instance is derived from the agent's project root
                       directory (e.g. Claude Code, Codex). Any two agents
                       running from the same project folder share the same
                       Quaid silo automatically.
            "keyed"  — instance is determined by an explicit QUAID_INSTANCE
                       key injected by the platform (e.g. OpenClaw). Each
                       agent gets its own key; keys can be reused intentionally
                       to share a silo.
        """
        return "keyed"

    # ---- Notifications ----

    @abc.abstractmethod
    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False, force: bool = False) -> bool:
        """Send a notification message to the user. Returns True on success.

        Args:
            force: If True, bypass QUAID_DISABLE_NOTIFICATIONS. Used for
                   system health alerts (compatibility, updates) that must
                   reach the user even when regular notifications are off.
        """
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

    def auth_token_path(self) -> Optional[Path]:
        """Path where this adapter stores its long-lived auth token.

        Returns None if the adapter does not use token-file auth (e.g. the
        OpenClaw adapter where the gateway owns credentials).
        Subclasses that need a persistent token override this.
        """
        return None

    def read_auth_token(self) -> Optional[str]:
        """Read the adapter's stored auth token from disk.

        Returns the token string or None if no token file exists.
        """
        p = self.auth_token_path()
        if p is None or not p.is_file():
            return None
        try:
            token = p.read_text(encoding="utf-8").strip()
            return token if token else None
        except (IOError, OSError):
            return None

    def store_auth_token(self, token: str) -> Path:
        """Write a long-lived auth token to the adapter-owned path.

        Returns the path where the token was written.
        Raises ValueError if this adapter does not support token-file auth.
        """
        p = self.auth_token_path()
        if p is None:
            raise ValueError(
                f"{type(self).__name__} does not support token-file auth"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token.strip() + "\n", encoding="utf-8")
        p.chmod(0o600)
        return p

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

    def get_base_context_files(self) -> Dict[str, Dict]:
        """Return platform-native context files for janitor monitoring.

        These are the platform's own personality/instruction files (e.g.
        CLAUDE.md for CC, SOUL.md/USER.md/ENVIRONMENT.md for OC). Quaid does
        NOT create or manage these — only trims them during maintenance.

        Returns dict mapping file paths to monitoring config::

            {"/path/to/CLAUDE.md": {"purpose": "...", "maxLines": 500}}
        """
        return {}

    def should_filter_transcript_message(self, text: str) -> bool:
        """Adapter-specific transcript noise filtering."""
        return self.filter_system_messages(text)

    @staticmethod
    def strip_quaid_notification_block(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("<quaid_notification>"):
            end_tag = "</quaid_notification>"
        elif value.startswith("<notification>"):
            end_tag = "</notification>"
        else:
            return value
        end_idx = value.find(end_tag)
        if end_idx < 0:
            return value
        remainder = value[end_idx + len(end_tag):].lstrip()
        if remainder.startswith("---"):
            remainder = remainder[3:].lstrip()
        return remainder.strip()

    def sanitize_transcript_text(self, text: str) -> str:
        value = self.strip_quaid_notification_block(text)
        if any(value.startswith(prefix) for prefix in self._QUAID_TRANSCRIPT_METADATA_PREFIXES):
            return ""
        return value.strip()

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
            text = re.sub(r"\n?\[message_id:\s*\d+\]", "", text, flags=re.IGNORECASE)
            # Strip system-injected context blocks that are prepended to user messages
            # by the memory hook (injected_memories) or OC timestamp prefix. These appear
            # in user-role messages but are NOT user-stated content — extracting from them
            # causes false facts (e.g. recalled memories re-stored as new user statements).
            text = re.sub(
                r"<injected_memories>.*?</injected_memories>\s*",
                "",
                text,
                flags=re.DOTALL,
            )
            text = re.sub(
                r"AUTOMATED MEMORY SYSTEM:.*?(?=\n\n|\Z)",
                "",
                text,
                flags=re.DOTALL,
            )
            # Strip OC gateway timestamp prefix [Day Date Time TZ]
            text = re.sub(r"^\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}[^\]]*\]\s*", "", text)
            # Strip Sender metadata blocks injected by OC gateway
            text = re.sub(
                r"Sender \(untrusted metadata\):.*?(?=\n\n|\Z)",
                "",
                text,
                flags=re.DOTALL,
            ).strip()
            text = self.sanitize_transcript_text(text)
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

    def resolve_stop_hook_signal(
        self,
        hook_input: Dict[str, Any],
        transcript_path: str,
    ) -> Optional[Dict[str, Any]]:
        """Return adapter-owned stop-hook signal policy, or None for no signal."""
        _ = hook_input, transcript_path
        return None

    # ---- Gateway config (optional) ----

    def get_gateway_config_path(self) -> Optional[Path]:
        """Path to the gateway config file (if applicable)."""
        return None

    # ---- Providers ----

    @abc.abstractmethod
    def get_llm_provider(self, model_tier: Optional[str] = None) -> "LLMProvider":
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

    # ---- Multi-agent support ----

    def is_multi_agent(self) -> bool:
        """True if this platform supports multiple first-class agents with own silos.

        When True, each agent gets its own Quaid silo named by list_agent_instance_ids().
        Subagents running inside a parent session are NOT first-class agents — they
        inherit the parent's instance automatically.
        """
        return False

    def agent_id_prefix(self) -> str:
        """Prefix used to build per-agent instance IDs.

        Convention: instance IDs follow "<prefix>-<label>" (e.g. "openclaw-main",
        "openclaw-coding"). The prefix is derived by stripping the "-main" suffix
        from the primary instance ID — e.g. "openclaw-main" → "openclaw".

        For single-agent platforms with a bare instance ID (no "-main" suffix),
        returns instance_id() unchanged.

        Subclasses may override to return adapter_id() directly (e.g. "claude-code"),
        which is equivalent when QUAID_INSTANCE follows the "<adapter>-main" convention.
        """
        iid = self.instance_id()
        return iid[:-5] if iid.endswith("-main") else iid

    def list_agent_instance_ids(self) -> List[str]:
        """Return all Quaid instance IDs for this platform's agents.

        Returns fully-qualified instance IDs with the prefix included.
        Single-agent: returns [instance_id()].
        Multi-agent: returns ["openclaw-main", "openclaw-coding", ...].

        Called at install time to enumerate silos to create.
        """
        return [self.instance_id()]

    def agent_instance_root(self, agent_instance_id: str) -> Path:
        """Resolve the instance root directory for a given agent instance ID."""
        return self.quaid_home() / agent_instance_id

    # ---- Adapter CLI registration ----

    def get_cli_namespace(self) -> Optional[str]:
        """Short CLI namespace for this adapter's commands (e.g. 'claudecode').

        When non-None, the quaid CLI exposes 'quaid <namespace> <cmd>' by
        dispatching to get_cli_commands(). Returns None if this adapter has
        no CLI commands.
        """
        return None

    def get_cli_commands(self) -> dict:
        """Map of command name → callable for this adapter's CLI namespace.

        Each callable receives (args: list[str]) and should print output
        to stdout. Only called when get_cli_namespace() is non-None.
        """
        return {}

    def get_cli_tools_snippet(self) -> str:
        """Markdown snippet describing this adapter's CLI commands.

        Injected into .claude/rules/quaid-projects.md at session start so
        agents know what adapter-specific commands are available.
        Returns empty string if no adapter CLI commands exist.
        """
        return ""

    def get_instance_manager(self) -> Optional["InstanceManager"]:
        """Return the InstanceManager for this adapter, or None.

        Adapters that support user-driven instance creation (e.g. CC)
        return a subclass. Adapters that manage instances automatically
        (e.g. OC at install time) return None.
        """
        return None

    # ---- Identity ----

    def get_repo_slug(self) -> str:
        return "quaid-labs/quaid"

    def get_install_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.get_repo_slug()}/main/install.sh"

    # ---- Installer capability surface ----

    def installer_supported_providers(self) -> List[str]:
        """LLM provider ids this adapter supports in guided install."""
        return ["anthropic", "openai", "openrouter", "together", "ollama"]

    def installer_default_models(self, provider: str) -> Optional[Dict[str, str]]:
        """Default deep/fast lanes for installer provider selection.

        Returns {"deep": "<model>", "fast": "<model>"} or None when adapter
        does not define opinionated defaults for the given provider.
        """
        _ = provider
        return None

    def get_fast_provider_default(self) -> str:
        """Adapter-owned default fast-tier provider id."""
        return "default"

    def get_deep_provider_default(self) -> str:
        """Adapter-owned default deep-tier provider id."""
        return "default"

    def get_fast_model_default(self, provider: str) -> Optional[str]:
        """Adapter-owned default fast-tier model for a provider."""
        defaults = self.installer_default_models(provider) or {}
        model = str(defaults.get("fast", "")).strip()
        return model or None

    def get_deep_model_default(self, provider: str) -> Optional[str]:
        """Adapter-owned default deep-tier model for a provider."""
        defaults = self.installer_default_models(provider) or {}
        model = str(defaults.get("deep", "")).strip()
        return model or None


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
        return Path(env).resolve() if env else Path.home() / "quaid"

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False, force: bool = False) -> bool:
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS") and not force:
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

        if is_fail_hard_enabled():
            return None

        # 2. .env file in quaid home (noisy fallback only when failHard=false)
        print(
            f"[adapter][FALLBACK] {env_var_name} not found in env; "
            "attempting ~/.quaid/.env lookup because failHard is disabled.",
            file=sys.stderr,
        )
        env_file = self.quaid_home() / ".env"
        if env_file.exists():
            found = read_env_file(env_file, env_var_name)
            if found:
                print(
                    f"[adapter][FALLBACK] Loaded {env_var_name} from {env_file}.",
                    file=sys.stderr,
                )
                return found

        return None

    def get_instance_name(self) -> str:
        """Return instance name from env, or empty string for standalone."""
        return os.environ.get("QUAID_INSTANCE", "").strip()

    def get_sessions_dir(self) -> Optional[Path]:
        d = self.quaid_home() / "sessions"
        return d if d.is_dir() else None

    def filter_system_messages(self, text: str) -> bool:
        return False

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from lib.providers import AnthropicLLMProvider, ClaudeCodeLLMProvider

        # Resolve provider from config with tier-aware overrides.
        from config import get_config
        cfg = get_config()
        deep_model = getattr(cfg.models, "deep_reasoning", "default")
        fast_model = getattr(cfg.models, "fast_reasoning", "default")
        deep_effort = getattr(cfg.models, "deep_reasoning_effort", "high")
        fast_effort = getattr(cfg.models, "fast_reasoning_effort", "none")
        provider_id = cfg.models.llm_provider
        if model_tier == "fast":
            fast_provider = getattr(cfg.models, "fast_reasoning_provider", "default")
            if fast_provider and fast_provider != "default":
                provider_id = fast_provider
        elif model_tier == "deep":
            deep_provider = getattr(cfg.models, "deep_reasoning_provider", "default")
            if deep_provider and deep_provider != "default":
                provider_id = deep_provider

        if not provider_id or provider_id == "default":
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "models.llmProvider must be explicitly set in config/memory.json. "
                    "Valid values: 'claude-code', 'anthropic', 'openai-compatible'. "
                    "No default fallback while failHard is enabled."
                )
            # Reliability path when failHard=false: choose explicit fallback chain.
            api_key = self.get_api_key("ANTHROPIC_API_KEY")
            if api_key:
                print(
                    "[adapter][FALLBACK] models.llmProvider is unset/default; using "
                    "anthropic provider from ANTHROPIC_API_KEY.",
                    file=sys.stderr,
                )
                return AnthropicLLMProvider(api_key=api_key)
            if shutil.which("claude"):
                print(
                    "[adapter][FALLBACK] models.llmProvider is unset/default; using "
                    "claude-code provider because claude CLI is available.",
                    file=sys.stderr,
                )
                return ClaudeCodeLLMProvider(
                    deep_model=deep_model,
                    fast_model=fast_model,
                )
            raise RuntimeError(
                "models.llmProvider is unset/default and fallback chain found no usable provider. "
                "Set models.llmProvider explicitly in config/memory.json."
            )

        if provider_id == "openai-compatible":
            from lib.providers import OpenAICompatibleLLMProvider
            import os
            api_key_env = str(getattr(cfg.models, "api_key_env", "") or "OPENAI_API_KEY")
            api_key = os.environ.get(api_key_env, os.environ.get("OPENAI_API_KEY", ""))
            configured_base_url = str(getattr(cfg.models, "base_url", "") or "").strip()
            env_base_url = str(os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            if configured_base_url:
                base_url = configured_base_url
            elif env_base_url:
                base_url = env_base_url
            elif api_key:
                # When an OpenAI key is present but no explicit compatible endpoint
                # is configured, prefer the real OpenAI API over the legacy local
                # localhost default. This keeps strict fail-hard runs from silently
                # routing to a dead local endpoint.
                base_url = "https://api.openai.com"
            else:
                base_url = "http://localhost:8000"
            resolved_deep = deep_model
            resolved_fast = fast_model
            if not resolved_deep or str(resolved_deep).strip() == "default":
                resolved_deep = (
                    (getattr(cfg.models, "deep_reasoning_model_classes", {}) or {}).get(provider_id)
                    or ""
                )
            if not resolved_fast or str(resolved_fast).strip() == "default":
                resolved_fast = (
                    (getattr(cfg.models, "fast_reasoning_model_classes", {}) or {}).get(provider_id)
                    or ""
                )
            return OpenAICompatibleLLMProvider(
                base_url=base_url, api_key=api_key,
                deep_model=resolved_deep,
                fast_model=resolved_fast,
                deep_reasoning_effort=deep_effort,
                fast_reasoning_effort=fast_effort,
            )

        if provider_id == "claude-code":
            resolved_deep = deep_model
            resolved_fast = fast_model
            if not resolved_deep or str(resolved_deep).strip() == "default":
                resolved_deep = (
                    (getattr(cfg.models, "deep_reasoning_model_classes", {}) or {}).get(provider_id)
                    or "claude-opus-4-6"
                )
            if not resolved_fast or str(resolved_fast).strip() == "default":
                resolved_fast = (
                    (getattr(cfg.models, "fast_reasoning_model_classes", {}) or {}).get(provider_id)
                    or "claude-haiku-4-5"
                )
            return ClaudeCodeLLMProvider(
                deep_model=resolved_deep,
                fast_model=resolved_fast,
            )

        if provider_id == "anthropic":
            api_key = self.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'anthropic' but ANTHROPIC_API_KEY not found. "
                    "Set it in your environment or in ~/quaid/.env."
                )
            resolved_deep = deep_model
            resolved_fast = fast_model
            if not resolved_deep or str(resolved_deep).strip() == "default":
                resolved_deep = (
                    (getattr(cfg.models, "deep_reasoning_model_classes", {}) or {}).get(provider_id)
                    or "claude-opus-4-6"
                )
            if not resolved_fast or str(resolved_fast).strip() == "default":
                resolved_fast = (
                    (getattr(cfg.models, "fast_reasoning_model_classes", {}) or {}).get(provider_id)
                    or "claude-haiku-4-5"
                )
            return AnthropicLLMProvider(
                api_key=api_key,
                deep_model=str(resolved_deep),
                fast_model=str(resolved_fast),
            )

        raise RuntimeError(
            f"Unknown LLM provider '{provider_id}'. "
            "Valid values: 'claude-code', 'anthropic', 'openai-compatible'."
        )

    def installer_supported_providers(self) -> List[str]:
        return ["anthropic", "openai", "openrouter", "together", "ollama"]

    def installer_default_models(self, provider: str) -> Optional[Dict[str, str]]:
        p = str(provider or "").strip().lower()
        if p == "anthropic":
            return {"deep": "claude-sonnet-4-5", "fast": "claude-haiku-4-5"}
        if p in ("openai", "openrouter", "together"):
            return {"deep": "gpt-5.4", "fast": "gpt-5.4-mini"}
        if p == "ollama":
            return {"deep": "llama3.1:70b", "fast": "llama3.1:8b"}
        return None

    def get_fast_provider_default(self) -> str:
        return "anthropic"

    def get_deep_provider_default(self) -> str:
        return "anthropic"


# ---------------------------------------------------------------------------
# Core path utilities — Quaid-managed directories derived from QUAID_HOME
# ---------------------------------------------------------------------------

def quaid_identity_dir(quaid_home: Path, adapter_id: str) -> Path:
    """Derive the Quaid-managed identity directory.

    DEPRECATED: Use adapter.identity_dir() instead, which routes through
    instance_root(). This function is kept for backward compat during migration.

    Returns: QUAID_HOME/<instance_id>/identity/ (via instance resolution)
    """
    from lib.instance import instance_id as _instance_id
    try:
        iid = _instance_id()
        return quaid_home / iid / "identity"
    except Exception:
        # Fallback for contexts where QUAID_INSTANCE isn't set (legacy)
        if not adapter_id or adapter_id == "standalone":
            return quaid_home
        return quaid_home / adapter_id / "identity"


def quaid_projects_dir(quaid_home: Path) -> Path:
    """Canonical projects directory."""
    return quaid_home / "projects"


def quaid_tracking_dir(quaid_home: Path) -> Path:
    """Shadow git tracking base directory."""
    return quaid_home / ".git-tracking"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def read_env_file(env_file: Path, var_name: str) -> Optional[str]:
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


def _read_env_file(env_file: Path, var_name: str) -> Optional[str]:
    """Backward-compatible alias for older imports."""
    return read_env_file(env_file, var_name)


def get_owner_id(override: Optional[str] = None) -> str:
    """Resolve owner ID from override, env var, config, or default.

    Resolution order:
    1. Explicit *override* argument (if non-empty).
    2. ``QUAID_OWNER`` environment variable.
    3. ``config.get_config().users.default_owner`` loaded from the active
       ``QUAID_HOME`` instance config.
    4. Fallback to ``"default"``.
    """
    if override:
        return override
    owner = os.environ.get("QUAID_OWNER", "").strip()
    if owner:
        return owner
    try:
        from config import get_config
        return get_config().users.default_owner
    except Exception:
        return "default"


# ---------------------------------------------------------------------------
# Test adapter
# ---------------------------------------------------------------------------

class TestAdapter(StandaloneAdapter):
    """Test adapter with canned LLM responses and call recording.

    Creates instance subdirectory structure under home/. The instance name
    defaults to QUAID_INSTANCE env var (usually "test" from conftest.py).

    Usage in tests::

        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        # ... code under test calls get_adapter().get_llm_provider() ...
        assert len(adapter.llm_calls) == 1
    """
    __test__ = False  # Not a pytest test class

    def __init__(self, home: Path, responses: Optional[Dict] = None,
                 instance: Optional[str] = None):
        super().__init__(home=home)
        from lib.providers import TestLLMProvider
        self._llm = TestLLMProvider(responses)
        self._instance = instance

        # Create instance directory structure
        iid = self.instance_id()
        iroot = home / iid
        (iroot / "config").mkdir(parents=True, exist_ok=True)
        (iroot / "data").mkdir(parents=True, exist_ok=True)
        cfg = iroot / "config" / "memory.json"
        if not cfg.exists():
            cfg.write_text('{"adapter":{"type":"standalone"}}', encoding="utf-8")

    def instance_id(self) -> str:
        if self._instance:
            return self._instance
        return os.environ.get("QUAID_INSTANCE", "pytest-runner").strip() or "pytest-runner"

    def get_llm_provider(self, model_tier: Optional[str] = None):
        return self._llm

    @property
    def llm_calls(self) -> list:
        return self._llm.calls


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_adapter: Optional[QuaidAdapter] = None
_adapter_lock = threading.Lock()


def _normalize_adapter_id(value: str) -> str:
    token = str(value or "").strip().lower().replace("_", "-")
    if token == "claudecode":
        return "claude-code"
    return token


def _registry_quaid_home() -> Path:
    env = os.environ.get("QUAID_HOME", "").strip()
    return Path(env).resolve() if env else Path.home() / "quaid"


def _adapter_manifest_candidates(adapter_id: str) -> List[Path]:
    normalized = _normalize_adapter_id(adapter_id)
    candidates: List[Path] = [
        _registry_quaid_home() / ".quaid" / "adaptors" / normalized / "adapter.json",
        Path(__file__).resolve().parent.parent / "adaptors" / "manifests" / f"{normalized}.json",
    ]
    out: List[Path] = []
    seen: set[str] = set()
    for p in candidates:
        sp = str(p)
        if sp in seen:
            continue
        seen.add(sp)
        out.append(p)
    return out


def _load_adapter_manifest(adapter_id: str) -> dict:
    normalized = _normalize_adapter_id(adapter_id)
    searched: List[str] = []
    for path in _adapter_manifest_candidates(normalized):
        searched.append(str(path))
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise RuntimeError(f"Manifest is not a JSON object: {path}")
            data["__path"] = str(path)
            return data
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(f"Failed to read adapter manifest {path}: {e}") from e

    raise RuntimeError(
        f"Unsupported adapter type '{adapter_id}'. No adapter manifest found. "
        f"Searched: {', '.join(searched)}"
    )


def _instantiate_adapter_from_manifest(adapter_id: str) -> QuaidAdapter:
    normalized = _normalize_adapter_id(adapter_id)
    manifest = _load_adapter_manifest(normalized)
    runtime = manifest.get("runtime", {})
    runtime_py = runtime.get("python", {}) if isinstance(runtime, dict) else {}
    module_name = str(runtime_py.get("module", "")).strip()
    class_name = str(runtime_py.get("class", "")).strip()
    if not module_name or not class_name:
        raise RuntimeError(
            f"Unsupported adapter type '{normalized}'. Manifest must define "
            "runtime.python.module and runtime.python.class"
        )

    manifest_path = Path(str(manifest.get("__path", "")))
    base_dir = manifest_path.parent if manifest_path else Path.cwd()
    raw_paths = runtime_py.get("path", [])
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if isinstance(raw_paths, list):
        for raw in raw_paths:
            token = str(raw or "").strip()
            if not token:
                continue
            p = Path(token)
            if not p.is_absolute():
                p = (base_dir / p).resolve()
            sp = str(p)
            if sp and sp not in sys.path:
                sys.path.insert(0, sp)

    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise RuntimeError(
            f"Unsupported adapter type '{normalized}'. Could not import "
            f"runtime.python.module '{module_name}': {e}"
        ) from e

    klass = getattr(module, class_name, None)
    if klass is None:
        raise RuntimeError(
            f"Unsupported adapter type '{normalized}'. Module '{module_name}' "
            f"does not export class '{class_name}'"
        )

    try:
        adapter = klass()
    except Exception as e:
        raise RuntimeError(
            f"Unsupported adapter type '{normalized}'. Failed to construct "
            f"'{module_name}.{class_name}': {e}"
        ) from e

    required = (
        "quaid_home",
        "get_instance_name",
        "notify",
        "get_last_channel",
        "get_api_key",
        "get_sessions_dir",
        "filter_system_messages",
        "get_llm_provider",
    )
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise RuntimeError(
            f"Unsupported adapter type '{normalized}'. Adapter class "
            f"'{module_name}.{class_name}' missing required callables: {', '.join(missing)}"
        )

    return adapter


def _adapter_config_paths() -> List[Path]:
    """Candidate config files for adapter selection (priority order).

    Instance-aware: checks QUAID_HOME/QUAID_INSTANCE/config/memory.json first.
    Falls back to legacy flat paths for backward compat during transition.
    """
    paths: List[Path] = []

    home = os.environ.get("QUAID_HOME", "").strip()
    instance = os.environ.get("QUAID_INSTANCE", "").strip()

    # Primary: instance-specific config
    if home and instance:
        paths.append(Path(home) / instance / "config" / "memory.json")

    # Secondary: CLAUDE_PROJECT_DIR-derived instance path when QUAID_INSTANCE is not
    # yet set.  Instance derivation normally happens after config is found (in
    # _bootstrap_instance_env), so we eagerly resolve the instance name here to
    # avoid the chicken-and-egg failure where the config search misses the silo.
    #
    # Uses the canonical slug derivation in lib.instance (single source of truth
    # for project-dir-to-slug conversion).
    if home and not instance:
        _cpd = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
        if _cpd:
            from lib.instance import instance_slug_from_project_dir
            _slug = instance_slug_from_project_dir(_cpd)
            paths.append(
                Path(home) / f"claude-code-{_slug}" / "config" / "memory.json"
            )
        _codex_project_dir = os.environ.get("CODEX_PROJECT_DIR", "").strip()
        if _codex_project_dir:
            from lib.instance import instance_slug_from_project_dir
            _slug = instance_slug_from_project_dir(_codex_project_dir)
            paths.append(
                Path(home) / f"codex-{_slug}" / "config" / "memory.json"
            )

    # Legacy: flat QUAID_HOME/config/memory.json
    if home:
        paths.append(Path(home) / "config" / "memory.json")

    workspace_root = (
        os.environ.get("QUAID_WORKSPACE", "").strip()
        or os.environ.get("CLAWDBOT_WORKSPACE", "").strip()
    )
    if workspace_root:
        paths.append(Path(workspace_root) / "config" / "memory.json")

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

        if kind:
            return kind
        raise RuntimeError(f"Config {cfg_path} must set adapter.type to a non-empty string (found: {adapter_cfg!r}).")

    searched = ", ".join(str(p) for p in _adapter_config_paths())
    if last_existing is None:
        raise RuntimeError(
            "No config file found for adapter selection. Create config/memory.json "
            "with {\"adapter\": {\"type\": \"<adapter-id>\"}}. "
            f"Searched: {searched}"
        )
    raise RuntimeError("Adapter type could not be resolved from config.")


def _infer_adapter_type_from_instance(instance_id: str) -> str:
    """Infer adapter type from QUAID_INSTANCE prefix convention.

    Returns the adapter type string if recognisable, or "" if unknown.
    """
    if instance_id.startswith("openclaw-") or instance_id == "openclaw":
        return "openclaw"
    if instance_id.startswith("codex-") or instance_id == "codex":
        return "codex"
    if instance_id.startswith("claude-code-") or instance_id in ("claude-code", "claude_code"):
        return "claude-code"
    return ""


def _auto_provision_from_env_if_needed() -> None:
    """Scaffold a default silo when QUAID_INSTANCE is set but has no config yet.

    Runs before _read_adapter_type_from_config() so first-use invocations
    (e.g. OC session start with a fresh instance name) create the silo
    automatically rather than hard-failing with 'no config found'.

    The adapter type is inferred from the QUAID_INSTANCE prefix so no config
    is needed to determine it.  After this returns, _read_adapter_type_from_config()
    will find the freshly written config and proceed normally.
    """
    home = os.environ.get("QUAID_HOME", "").strip()
    instance = os.environ.get("QUAID_INSTANCE", "").strip()

    # When QUAID_INSTANCE is absent but CLAUDE_PROJECT_DIR is set, derive the
    # CC instance name from the project path (same logic as _adapter_config_paths
    # and ClaudeCodeAdapter.get_instance_name).  Setting QUAID_INSTANCE here means
    # the silo is provisioned and _bootstrap_instance_env won't override it later.
    if home and not instance:
        from lib.instance import instance_slug_from_project_dir

        for env_name, prefix in (
            ("CLAUDE_PROJECT_DIR", "claude-code"),
            ("CODEX_PROJECT_DIR", "codex"),
        ):
            project_dir = os.environ.get(env_name, "").strip()
            if not project_dir:
                continue
            _slug = instance_slug_from_project_dir(project_dir)
            if _slug:
                instance = f"{prefix}-{_slug}"
                os.environ["QUAID_INSTANCE"] = instance
                break

    if not home or not instance:
        return
    silo_root = Path(home) / instance
    if (silo_root / "config" / "memory.json").exists():
        return  # Already initialised — nothing to do

    adapter_type = _infer_adapter_type_from_instance(instance)
    if not adapter_type:
        return  # Unrecognised prefix — let normal flow raise with a clear error

    try:
        # Use InstanceManager base class for full silo scaffolding (dirs, DB,
        # identity stubs, PROJECT.md).  Import lazily to avoid circular deps.
        from lib.instance_manager import InstanceManager

        class _BootstrapAdapter:
            """Minimal adapter stand-in used only during first-use silo creation."""
            def quaid_home(self):  # type: ignore[override]
                return Path(home)
            def adapter_id(self):  # type: ignore[override]
                return adapter_type
            def agent_id_prefix(self):  # type: ignore[override]
                prefix_map = {"openclaw": "openclaw", "claude_code": "claude-code", "codex": "codex"}
                return prefix_map.get(adapter_type, adapter_type)

        mgr = InstanceManager(_BootstrapAdapter())  # type: ignore[arg-type]
        prefix = mgr.adapter.agent_id_prefix()
        label = instance[len(prefix) + 1:] if instance.startswith(f"{prefix}-") else instance
        mgr._init_silo(silo_root, instance)
        import logging as _logging
        _logging.getLogger(__name__).info(
            "Auto-provisioned instance silo: %s (adapter=%s)", instance, adapter_type
        )
    except Exception:
        pass  # Never block — let downstream raise with a useful message


def get_adapter() -> QuaidAdapter:
    """Get the current adapter (resolved on first call).

    Selection: config/memory.json adapter.type under QUAID_HOME.
    Each QUAID_HOME silo has its own config that declares which adapter owns it.

    On first resolution, also bootstraps QUAID_INSTANCE from
    adapter.get_instance_name() if not already set in the environment.
    This is the single place where instance identity is established —
    adapters do not need to set QUAID_INSTANCE themselves.

    Auto-provisions the instance silo when QUAID_INSTANCE is set but no config
    exists yet, so first-use invocations do not require a separate setup step.
    """
    global _adapter
    if _adapter is not None:
        return _adapter
    with _adapter_lock:
        if _adapter is not None:
            return _adapter
        _auto_provision_from_env_if_needed()
        kind = _normalize_adapter_id(_read_adapter_type_from_config())
        _adapter = _instantiate_adapter_from_manifest(kind)
        _bootstrap_instance_env(_adapter)
        return _adapter


def _bootstrap_instance_env(adapter: QuaidAdapter) -> None:
    """Set QUAID_INSTANCE from adapter.get_instance_name() if not already set.

    Builds the full instance ID as "<adapter_prefix>-<instance_name>" and
    writes it to os.environ so all downstream code (lib.instance, subprocesses,
    CLI calls) sees a consistent value without each adapter managing it.

    Skips if QUAID_INSTANCE is already set — OC's TS side may have pre-set it,
    and explicit env overrides always win.
    """
    import os
    if os.environ.get("QUAID_INSTANCE", "").strip():
        return
    try:
        name = adapter.get_instance_name()
        prefix = adapter.agent_id_prefix()
        instance_id = f"{prefix}-{name}" if name else prefix
        # Validate before setting — guard against empty/invalid slugs
        if instance_id and instance_id != prefix:
            os.environ["QUAID_INSTANCE"] = instance_id
    except Exception:
        pass  # Never block adapter init — instance_id() will raise later if needed


def set_adapter(adapter: QuaidAdapter) -> None:
    """Override the adapter (for tests)."""
    global _adapter
    with _adapter_lock:
        _adapter = adapter


def reset_adapter() -> None:
    """Reset adapter resolution state (for tests).

    Also clears cached providers and config so they re-resolve
    against the next adapter.
    """
    global _adapter
    with _adapter_lock:
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
