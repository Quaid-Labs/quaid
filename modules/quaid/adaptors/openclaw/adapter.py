"""OpenClaw-specific Quaid adapter implementation.

IMPORTANT:
- Quaid's active OpenClaw `openai` lane is the direct ChatGPT OAuth path
  (`chatgpt.com/backend-api/codex/responses`), not the OpenClaw gateway.
- The old gateway-backed provider flow remains in this module only as a
  deprecated fallback/reference path because it may be revived later.
- Do not route Quaid service calls through the gateway-backed OpenAI path
  unless that deprecation is explicitly reversed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Optional

from adaptors.openclaw.providers import OpenClawGatewayLLMProvider
from lib.adapter import ChannelInfo, QuaidAdapter, read_env_file
from lib.fail_policy import is_fail_hard_enabled
from lib.providers import AnthropicLLMProvider, OpenAICodexOAuthLLMProvider


class OpenClawAdapter(QuaidAdapter):
    """Adapter for running inside the OpenClaw gateway.

    - Home dir: QUAID_HOME env or ~/.quaid
    - Notifications: openclaw message send CLI
    - Credentials: env var -> workspace .env
    - Sessions: ~/.openclaw/agents/<label>/sessions/
    - Filtering: HEARTBEAT, GatewayRestart, System: messages
    """

    _MAIN_SESSION_KEY = "agent:main:main"
    _INSTALLER_MODEL_DEFAULTS = {
        "anthropic": {"deep": "claude-sonnet-4-5", "fast": "claude-haiku-4-5"},
        "openai": {"deep": "gpt-5.4", "fast": "gpt-5.4-mini"},
        "openrouter": {"deep": "gpt-5.4", "fast": "gpt-5.4-mini"},
        "together": {"deep": "gpt-5.4", "fast": "gpt-5.4-mini"},
        "ollama": {"deep": "llama3.1:70b", "fast": "llama3.1:8b"},
    }
    _PROVIDER_ALIASES = {
        "openai-codex": "openai",
        "anthropic-claude-code": "anthropic",
    }
    _NON_ROUTABLE_NOTIFY_CHANNELS = {"webchat"}
    _HOST_MEMORY_POLICY_PATH_RE = re.compile(
        r"(?:^|[\s`\"'(<\[])(?:memory|openclaw-workspace)[/\\][^\s`\"')>\]]+",
        re.IGNORECASE,
    )
    _SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
    _OTHER_ADAPTER_INSTANCE_PREFIXES = (
        "claude-",
        "claude_code-",
        "claude-code-",
        "codex-",
        "test-",
        "standalone-",
    )
    _ROW_TIMESTAMP_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
    )
    ADAPTER_CONFIG = {
        "supports_compaction_control": True,
        "platform_config_scope": "openclaw",
        "turn_scoped_provider_notices": True,
        "preserve_transcript_mirror_session_prefixes": [
            "agent:main:matrix:channel:",
        ],
    }

    def __init__(self):
        self._provider_fallback_notices_seen: set[str] = set()

    @classmethod
    def installer_adapter_id(cls) -> str:
        return "openclaw"

    @classmethod
    def installer_cli_candidates(cls) -> list[str]:
        return ["openclaw"]

    @staticmethod
    def _is_host_memory_policy_reply(role: str, text: str) -> bool:
        normalized_role = str(role or "").strip().lower()
        raw = str(text or "").strip()
        if normalized_role != "assistant" or not raw:
            return False
        return bool(OpenClawAdapter._HOST_MEMORY_POLICY_PATH_RE.search(raw))

    def _openclaw_config_path_candidates(self) -> list[Path]:
        """OpenClaw config candidates, honoring OPENCLAW_CONFIG_PATH first."""
        candidates: list[Path] = []
        env_path = str(os.environ.get("OPENCLAW_CONFIG_PATH", "")).strip()
        if env_path:
            expanded = Path(env_path).expanduser()
            try:
                resolved = expanded.resolve()
            except (OSError, RuntimeError):
                if is_fail_hard_enabled():
                    raise
                print(
                    f"[adapter] OPENCLAW_CONFIG_PATH could not be resolved: {expanded}",
                    file=sys.stderr,
                )
                resolved = expanded
            if self._path_is_under(resolved, Path.home()):
                candidates.append(resolved)
            else:
                message = f"OPENCLAW_CONFIG_PATH outside home ignored: {resolved}"
                print(f"[adapter] {message}", file=sys.stderr)
                if is_fail_hard_enabled():
                    raise PermissionError(message)
        candidates.append((Path.home() / ".openclaw" / "openclaw.json").resolve())
        deduped: list[Path] = []
        seen: set[str] = set()
        for path_candidate in candidates:
            key = str(path_candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path_candidate)
        return deduped

    def _openclaw_root_dir(self) -> Path:
        cfg_path = self.get_gateway_config_path()
        if cfg_path is not None:
            return cfg_path.parent
        return (Path.home() / ".openclaw").resolve()

    @classmethod
    def _normalize_installer_provider(cls, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        if not normalized:
            return ""
        return cls._PROVIDER_ALIASES.get(normalized, normalized)

    @classmethod
    def _sanitize_agent_label(cls, label: str, *, default: str = "main") -> str:
        clean = unicodedata.normalize("NFKC", str(label or "").strip()).lower()
        if not clean:
            return default
        if cls._is_safe_agent_label(clean):
            return clean
        message = f"Unsafe OpenClaw agent label: {label!r}"
        print(f"[adapter] {message}", file=sys.stderr)
        if is_fail_hard_enabled():
            raise ValueError(message)
        return default

    @staticmethod
    def _is_safe_agent_label(label: str) -> bool:
        if not label or len(label) > 64 or label in {".", ".."}:
            return False
        has_path_component = False
        for char in label:
            category = unicodedata.category(char)
            if char in {"_", "-"}:
                has_path_component = True
                continue
            if char.isspace() or category[0] == "C":
                return False
            if char.isalpha() or char.isdigit():
                has_path_component = True
                continue
            if category[0] == "M":
                continue
            return False
        return has_path_component

    @classmethod
    def _safe_session_id(cls, session_id: str) -> str:
        value = str(session_id or "").strip()
        return value if cls._SAFE_SESSION_ID_RE.fullmatch(value) else ""

    @staticmethod
    def _log_value(value: str) -> str:
        return str(value or "").replace("\r", "\\r").replace("\n", "\\n")

    @staticmethod
    def _safe_cli_arg_value(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("-") or "\n" in text or "\r" in text or "\x00" in text:
            return ""
        if len(text) > 512:
            return ""
        return text

    @staticmethod
    def _message_cli_allowed_dirs() -> tuple[Path, ...]:
        return (
            Path("/opt/homebrew/bin"),
            Path("/opt/homebrew/Cellar"),
            Path("/opt/homebrew/lib"),
            Path("/usr/local/bin"),
            Path("/usr/local/Cellar"),
            Path("/usr/local/lib"),
            Path.home() / ".local" / "bin",
        )

    def _validate_message_cli_candidate(self, candidate: str) -> Optional[str]:
        if not candidate:
            return None
        requested = Path(candidate).expanduser()
        try:
            resolved = requested.resolve()
        except (OSError, RuntimeError) as exc:
            print(f"[notify] Invalid message CLI path: {exc}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return None
        if (
            resolved.is_file()
            and requested.name == "openclaw"
            and any(self._path_is_under(resolved, allowed_dir) for allowed_dir in self._message_cli_allowed_dirs())
        ):
            return str(resolved)
        return None

    def _detect_gateway_primary_provider(self) -> str:
        cfg_path = self.get_gateway_config_path()
        if cfg_path:
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                agents = cfg.get("agents", {}) if isinstance(cfg, dict) else {}
                candidates = [
                    agents.get("main", {}),
                ]
                agents_list = agents.get("list", []) if isinstance(agents, dict) else []
                if isinstance(agents_list, list):
                    candidates.extend(
                        row for row in agents_list
                        if isinstance(row, dict) and (row.get("id") == "main" or row.get("default") is True)
                    )
                candidates.append(agents.get("defaults", {}))
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    model = candidate.get("model", {}) if isinstance(candidate.get("model", {}), dict) else {}
                    primary = str(
                        candidate.get("modelPrimary")
                        or model.get("primary")
                        or ""
                    ).strip()
                    if "/" in primary:
                        return str(primary.split("/", 1)[0] or "").strip().lower()
            except Exception:
                if is_fail_hard_enabled():
                    raise
                pass
        profiles_path = self._get_agent_config_dir() / "auth-profiles.json"
        if profiles_path.exists():
            try:
                with open(profiles_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                last_good = data.get("lastGood", {}) if isinstance(data, dict) else {}
                preferred = ("openai-codex", "openai", "anthropic")
                for p in preferred:
                    if last_good.get(p):
                        return p
                for p in last_good.keys():
                    normalized = str(p or "").strip().lower()
                    if normalized:
                        return normalized
            except Exception:
                if is_fail_hard_enabled():
                    raise
                pass
        return ""

    @staticmethod
    def _score_session_row(session: dict, fallback_mtime_ms: float) -> float:
        """Prefer most recently updated user-routable session rows."""
        ts = session.get("updatedAt")
        try:
            return float(ts)
        except (TypeError, ValueError):
            return float(fallback_mtime_ms)

    def _pick_recent_channel_info(
        self, sessions: dict, fallback_mtime_ms: float, preferred_channel: str = ""
    ) -> Optional[ChannelInfo]:
        preferred = str(preferred_channel or "").strip().lower()
        best: Optional[tuple[float, ChannelInfo]] = None
        for session_key, session in sessions.items():
            if not isinstance(session_key, str) or not isinstance(session, dict):
                continue
            channel = str(session.get("lastChannel") or "").strip()
            target = str(session.get("lastTo") or "").strip()
            account_id = str(session.get("lastAccountId") or "default").strip() or "default"
            if not channel or not target:
                continue
            if preferred and channel.lower() != preferred:
                continue
            score = self._score_session_row(session, fallback_mtime_ms)
            info = ChannelInfo(
                channel=channel,
                target=target,
                account_id=account_id,
                session_key=session_key,
            )
            if best is None or score > best[0]:
                best = (score, info)
        return best[1] if best else None

    def _resolve_channel_route(self, preferred_channel: str = "") -> Optional[ChannelInfo]:
        sessions_path = self._find_sessions_json()
        if not sessions_path:
            return None
        try:
            file_mtime_ms = sessions_path.stat().st_mtime * 1000.0
            with open(sessions_path) as f:
                sessions = json.load(f)
            return self._pick_recent_channel_info(sessions, file_mtime_ms, preferred_channel=preferred_channel)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[notify] Error reading sessions: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return None

    def _resolve_message_cli(self) -> Optional[str]:
        """Resolve message CLI binary path for notification delivery."""
        explicit = os.environ.get("QUAID_MESSAGE_CLI", "").strip()
        if explicit:
            resolved = self._validate_message_cli_candidate(explicit)
            if resolved:
                return resolved
            message = f"Rejected unsafe QUAID_MESSAGE_CLI path: {explicit}"
            print(f"[notify] {message}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise PermissionError(message)
            return None

        candidates = [
            shutil.which("openclaw"),
            "/opt/homebrew/bin/openclaw",
            "/usr/local/bin/openclaw",
            str(Path.home() / ".local" / "bin" / "openclaw"),
        ]
        for candidate in candidates:
            resolved = self._validate_message_cli_candidate(candidate or "")
            if resolved:
                return resolved
        return None

    def _notify_subprocess_env(self) -> dict:
        """Build a notification subprocess env with a resilient PATH.

        Some launchd/service contexts omit Homebrew paths, which makes the
        OpenClaw CLI fail with `env: node: No such file or directory` even
        though `openclaw` itself is resolved.
        """
        env = os.environ.copy()
        path_entries = []
        for candidate in (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(Path.home() / ".local" / "bin"),
        ):
            if candidate and candidate not in path_entries:
                path_entries.append(candidate)
        existing_path = str(env.get("PATH") or "").strip()
        for part in [p for p in existing_path.split(":") if p]:
            if part not in path_entries:
                path_entries.append(part)
        env["PATH"] = ":".join(path_entries)
        return env

    @staticmethod
    def _parse_openclaw_version_output(raw: str) -> str:
        """Extract the platform version from `openclaw --version` output."""
        text = str(raw or "").strip()
        if not text:
            return ""
        match = re.search(r"\b(\d{4}\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?)\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _read_openclaw_package_version(binary: str) -> str:
        """Read OpenClaw's installed package.json version when the CLI cannot run."""
        if not binary:
            return ""
        try:
            resolved = Path(binary).expanduser().resolve()
        except (OSError, RuntimeError):
            resolved = Path(binary).expanduser()

        candidates = [
            resolved.parent / "package.json",
            resolved.parent.parent / "package.json",
        ]
        for package_json in candidates:
            try:
                raw = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(raw.get("name") or "").strip() != "openclaw":
                continue
            version = str(raw.get("version") or "").strip()
            if version:
                return version
        return ""

    def adapter_id(self) -> str:
        return "openclaw"

    def get_instance_name(self) -> str:
        """Return the current agent's instance name.

        OC resolves agent identity at the TypeScript layer (from sessions.json
        keys) and injects QUAID_INSTANCE into Python subprocess env via
        buildPythonEnv(). This method reads that value so Python callsites
        have a consistent way to get the instance name across adapters.

        Falls back to the primary OC agent label "main" if QUAID_INSTANCE is
        not set. The adapter layer owns the "openclaw-" prefix; callers here
        should return the label only.
        """
        raw = os.environ.get("QUAID_INSTANCE", "").strip()
        if raw.startswith("openclaw-"):
            return self._sanitize_agent_label(raw[len("openclaw-"):], default="main")
        if raw == "openclaw":
            return "main"
        if raw and not raw.lower().startswith(self._OTHER_ADAPTER_INSTANCE_PREFIXES):
            return self._sanitize_agent_label(raw, default="main")
        return "main"

    def get_host_info(self):
        """Detect OpenClaw platform version and binary path."""
        from core.compatibility import HostInfo

        # Find the OC binary
        binary = self._resolve_message_cli()
        version = "unknown"

        if binary:
            try:
                result = subprocess.run(
                    [binary, "--version"],
                    capture_output=True, text=True, timeout=5,
                    env=self._notify_subprocess_env(),
                )
                if result.returncode == 0:
                    parsed = self._parse_openclaw_version_output(
                        "\n".join([result.stdout or "", result.stderr or ""])
                    )
                    if parsed:
                        version = parsed
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                if is_fail_hard_enabled():
                    raise
                pass
            if version == "unknown":
                package_version = self._read_openclaw_package_version(binary)
                if package_version:
                    version = package_version

        return HostInfo(
            platform="openclaw",
            version=version,
            binary_path=binary,
        )

    def get_base_context_files(self):
        """OC's native context files live at the OC workspace root."""
        ws = self.oc_workspace()
        files = {}
        for name, purpose, max_lines in [
            ("SOUL.md", "Personality, vibe, interaction style", 80),
            ("USER.md", "About the user", 150),
            ("ENVIRONMENT.md", "Environmental context and shared history", 100),
            ("IDENTITY.md", "Name, avatar, minimal identity", 20),
            ("HEARTBEAT.md", "Periodic task instructions", 50),
            ("TODO.md", "Planning and task list", 150),
        ]:
            fpath = ws / name
            if fpath.is_file():
                files[str(fpath)] = {"purpose": purpose, "maxLines": max_lines}
        return files

    def get_compatibility_context_files(self):
        path = Path(__file__).with_name("COMPATIBILITY.md")
        if not path.is_file():
            return {}
        return {
            str(path.resolve()): {
                "purpose": "OpenClaw compatibility notes",
                "maxLines": 100,
            }
        }

    def quaid_home(self) -> Path:
        """Root directory containing all Quaid instances (QUAID_HOME)."""
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / ".quaid"

    def oc_workspace(self) -> Path:
        """OpenClaw workspace directory (platform-specific, not Quaid instance root).

        This is where OC stores its own files (SOUL.md, USER.md, etc.) and
        where the bootstrap boundary guard expects files. NOT the same as
        quaid_home() or instance_root().
        """
        # Fallback: resolve workspace from gateway config when env vars are absent.
        cfg_path = self.get_gateway_config_path()
        if cfg_path is not None and cfg_path.exists():
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                agents = cfg.get("agents", {})
                ws = ""
                for agent in agents.get("list", []) or []:
                    if not isinstance(agent, dict):
                        continue
                    if agent.get("id") == "main" or agent.get("default") is True:
                        ws = str(agent.get("workspace") or "").strip()
                        if ws:
                            break
                if not ws:
                    ws = str(agents.get("defaults", {}).get("workspace", "")).strip()
                if ws:
                    resolved = Path(ws).expanduser().resolve()
                    if self._path_is_under(resolved, Path.home()):
                        return resolved
                    message = f"OpenClaw workspace outside home ignored: {resolved}"
                    print(f"[adapter] {message}", file=sys.stderr)
                    if is_fail_hard_enabled():
                        raise PermissionError(message)
            except (json.JSONDecodeError, KeyError):
                if is_fail_hard_enabled():
                    raise
                pass
        raise RuntimeError(
            "Could not resolve OpenClaw workspace from OpenClaw config "
            "(OPENCLAW_CONFIG_PATH or ~/.openclaw/openclaw.json). "
            "Set QUAID_HOME or configure adapter.type=standalone in config.json."
        )

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False, force: bool = False) -> bool:
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS") and not force:
            return True

        info = self.get_last_channel()
        if channel_override and (not info or str(info.channel).strip().lower() != str(channel_override).strip().lower()):
            info = self._resolve_channel_route(channel_override) or info
        if not info:
            print("[notify] No last channel found", file=sys.stderr)
            return False

        effective_channel = channel_override or info.channel
        effective_channel = self._safe_cli_arg_value(effective_channel)
        target = self._safe_cli_arg_value(info.target)
        account_id = self._safe_cli_arg_value(info.account_id)
        if not effective_channel or not target:
            print("[notify] Unsafe or empty notification route", file=sys.stderr)
            if is_fail_hard_enabled():
                raise ValueError("Unsafe or empty notification route")
            return False
        if str(effective_channel or "").strip().lower() in self._NON_ROUTABLE_NOTIFY_CHANNELS:
            print(
                f"[notify] Channel not routable via message CLI: {self._log_value(effective_channel)}",
                file=sys.stderr,
            )
            return False
        message_cli = self._resolve_message_cli()
        if not message_cli:
            print("[notify] No message CLI found (expected openclaw)", file=sys.stderr)
            return False

        def _send(channel: str) -> bool:
            cmd = [
                message_cli, "message", "send",
                "--channel", channel,
                "--target", target,
                "--message", message,
            ]
            if account_id and account_id != "default":
                cmd.extend(["--account", account_id])
            if dry_run:
                print(f"[notify] Would run: {' '.join(cmd)}")
                return True
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._notify_subprocess_env(),
            )
            if result.returncode == 0:
                print(f"[notify] Sent to {self._log_value(channel)}:{self._log_value(target)}")
                return True
            err = (result.stderr or "").strip() or "(no stderr)"
            print(f"[notify] Send failed on channel={self._log_value(channel)}: {err}", file=sys.stderr)
            return False

        try:
            if _send(effective_channel):
                return True
            # Fallback: if override channel fails, retry on the last active channel.
            if channel_override and channel_override != info.channel:
                print(
                    f"[notify] Override channel failed; retrying with last channel={self._log_value(info.channel)}",
                    file=sys.stderr,
                )
                fallback_channel = self._safe_cli_arg_value(info.channel)
                if not fallback_channel:
                    return False
                return _send(fallback_channel)
            return False
        except subprocess.TimeoutExpired:
            print("[notify] Send timed out", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return False
        except Exception as e:
            print(f"[notify] Error: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return False

    def get_last_channel(self, session_key: str = "") -> Optional[ChannelInfo]:
        session_key = session_key or self._MAIN_SESSION_KEY
        sessions_path = self._find_sessions_json()
        if not sessions_path:
            return None

        try:
            with open(sessions_path) as f:
                sessions = json.load(f)
            file_mtime_ms = sessions_path.stat().st_mtime * 1000.0

            session = sessions.get(session_key)
            if isinstance(session, dict):
                channel = str(session.get("lastChannel") or "").strip()
                target = str(session.get("lastTo") or "").strip()
                account_id = str(session.get("lastAccountId") or "default").strip() or "default"
                if channel and target:
                    return ChannelInfo(
                        channel=channel,
                        target=target,
                        account_id=account_id,
                        session_key=session_key,
                    )

            # OpenClaw often stores the real user channel on the active
            # conversation session, not on agent:main:main. Fall back to the
            # most recently updated routable session row instead of failing
            # closed on installer/first-run notifications.
            return self._pick_recent_channel_info(sessions, file_mtime_ms)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[notify] Error reading sessions: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        # 1. Environment variable
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key
        if env_var_name == "ANTHROPIC_API_KEY":
            token = (
                self.read_shared_auth_token(["anthropic_oauth", "anthropic_api"])
                or self._resolve_anthropic_credential()
            )
            if token:
                return token
        if env_var_name == "OPENAI_API_KEY":
            token = self.read_shared_auth_token(["codex_oauth", "openai_api"])
            if token:
                return token
            oauth = os.environ.get("OPENAI_OAUTH_TOKEN", "").strip()
            if oauth:
                return oauth
        if env_var_name != "ANTHROPIC_API_KEY":
            token = self.read_auth_token()
            if token:
                return token

        if is_fail_hard_enabled():
            return None

        # 2. .env file in OC workspace root (noisy fallback only when failHard=false)
        print(
            f"[adapter][FALLBACK] {env_var_name} not found in env; "
            "attempting workspace .env lookup because failHard is disabled.",
            file=sys.stderr,
        )
        env_file = self.oc_workspace() / ".env"
        if env_file.exists():
            found = read_env_file(env_file, env_var_name)
            if found:
                print(
                    f"[adapter][FALLBACK] Loaded {env_var_name} from {env_file}.",
                    file=sys.stderr,
                )
                return found

        return None

    def get_sessions_dir(self) -> Optional[Path]:
        label = self.get_instance_name()
        own_dir = self._agent_sessions_dir(label)
        if own_dir.is_dir():
            return own_dir
        main_dir = self._agent_sessions_dir("main")
        if label != "main":
            sessions_json = main_dir / "sessions.json"
            if main_dir.is_dir() and self._sessions_json_has_agent_label(sessions_json, label):
                return main_dir
            return None
        return main_dir if main_dir.is_dir() else None

    def owns_session_path(self, path: Path, session_id: str = "") -> bool:
        """Return True only for transcripts belonging to this OC agent label.

        OpenClaw may keep multiple agent sessions under the shared
        ``agents/main/sessions`` directory. Quaid silos must still be separated
        by the logical OC agent label from sessions.json, otherwise a newly
        auto-provisioned instance can discover and ingest the main agent's
        transcript.
        """
        sid = str(session_id or "").strip() or self._session_id_from_path(path)
        if not sid:
            return False
        try:
            transcript_path = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            transcript_path = Path(path).expanduser()

        label = self.get_instance_name()
        own_dir = self._agent_sessions_dir(label)
        if self._path_is_under(transcript_path, own_dir) and self._path_matches_session_id(transcript_path, sid):
            return True

        main_sessions_json = self._agent_sessions_dir("main") / "sessions.json"
        saw_session = False
        for session_key, row in self._iter_sessions_index(main_sessions_json):
            row_sid = str(row.get("sessionId") or "").strip()
            if row_sid != sid:
                continue
            saw_session = True
            row_label = self._sanitize_agent_label(
                self.extract_agent_label_from_session_key(session_key) or "main",
                default="",
            )
            if row_label != label:
                continue
            row_path = self._session_path_from_index_row(main_sessions_json.parent, row, sid)
            if row_path is not None and self._same_path(row_path, transcript_path):
                return True

        if label == "main" and not saw_session:
            main_dir = self._agent_sessions_dir("main")
            return self._path_is_under(transcript_path, main_dir) and self._path_matches_session_id(transcript_path, sid)
        return False

    def auth_token_path(self) -> Optional[Path]:
        return self.quaid_home() / "adaptors" / "openclaw" / ".auth-token"

    def auth_registry_kinds(self) -> list[str]:
        return ["anthropic_oauth", "anthropic_api", "codex_oauth", "openai_api"]

    # OC gateway prepends "[Day Date HH:MM TZ]" to every user message.
    # Also strips OC-injected "(untrusted metadata)" code blocks.
    _OC_UNTRUSTED_METADATA_RE = re.compile(
        r"\w[\w\s]*\s*\(untrusted metadata\):[\s\S]*?```[\s\S]*?```",
        re.IGNORECASE,
    )
    _QUAID_MEMORY_CONTEXT_RE = re.compile(
        r"<quaid_memory_context>.*?</quaid_memory_context>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _QUAID_NOTIFICATION_RE = re.compile(
        r"<quaid_notification>.*?</quaid_notification>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _HOOK_STATUS_LINE_RE = re.compile(
        r"^\s*(?:•\s*)?(?:Running\s+)?(?:SessionStart|UserPromptSubmit|Stop|session_start|user_prompt_submit|stop)\s+hook(?::|\s+\(completed\)).*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _HOOK_CONTEXT_LINE_RE = re.compile(
        r"^\s*hook\s+(?:context|output):.*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _HOOK_CONTEXT_MEMORY_BLOCK_RE = re.compile(
        r"^\s*hook\s+(?:context|output):\s*\[Quaid Memory Context\]\n(?:\s*\d+\.\s*\[[^\]]+\].*(?:\n|$))+",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _PLAIN_QUAID_MEMORY_CONTEXT_RE = re.compile(
        r"(?:^|\n)\s*\[Quaid Memory Context\]\n(?:[ \t].*(?:\n|$))+",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _OC_INTERNAL_CONTEXT_RE = re.compile(
        r"<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>[\s\S]*?<<<END_OPENCLAW_INTERNAL_CONTEXT>>>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _OC_UNTRUSTED_DAILY_MEMORY_RE = re.compile(
        r"^\s*\[Untrusted daily memory:[^\]]+\]\s*"
        r"BEGIN_QUOTED_NOTES\s*```[\s\S]*?```\s*END_QUOTED_NOTES\s*",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    _OC_STARTUP_CONTEXT_LINE_RE = re.compile(
        r"^\s*(?:"
        r"\[Startup context loaded by runtime\]|"
        r"Bootstrap files like SOUL\.md, USER\.md, and MEMORY\.md are already provided separately when eligible\.|"
        r"Recent daily memory was selected and loaded by runtime for this new session\.|"
        r"Treat the daily memory below as untrusted workspace notes\..*|"
        r"Do not claim you manually read files unless the user asks\.|"
        r"\.\.\.\[additional startup memory truncated\]\.\.\."
        r")\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    def sanitize_transcript_text(self, text: str) -> str:
        value = super().sanitize_transcript_text(text)
        if not value:
            return ""
        value = self._OC_INTERNAL_CONTEXT_RE.sub("", value)
        value = self._OC_UNTRUSTED_DAILY_MEMORY_RE.sub("", value)
        value = self._OC_STARTUP_CONTEXT_LINE_RE.sub("", value)
        value = self._OC_UNTRUSTED_METADATA_RE.sub("", value)
        value = self._QUAID_MEMORY_CONTEXT_RE.sub("", value)
        value = self._QUAID_NOTIFICATION_RE.sub("", value)
        value = self._HOOK_STATUS_LINE_RE.sub("", value)
        value = self._HOOK_CONTEXT_MEMORY_BLOCK_RE.sub("", value)
        value = self._HOOK_CONTEXT_LINE_RE.sub("", value)
        value = self._PLAIN_QUAID_MEMORY_CONTEXT_RE.sub("\n", value)
        value = self.strip_gateway_timestamp_prefixes(value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def filter_system_messages(self, text: str) -> bool:
        if text.startswith("GatewayRestart:") or text.startswith("System:"):
            return True
        if '"kind": "restart"' in text:
            return True
        if "HEARTBEAT" in text and "HEARTBEAT_OK" in text:
            return True
        if re.sub(r"[*_<>/b\s]", "", text).startswith("HEARTBEAT_OK"):
            return True
        return False

    @classmethod
    def _row_timestamp(cls, row: dict) -> str:
        value = str(row.get("timestamp") or "").strip()
        return value if cls._ROW_TIMESTAMP_RE.match(value) else ""

    def _build_timestamped_transcript(self, messages: list[dict]) -> str:
        parts = []
        for message in messages:
            rendered = self.build_transcript([message]).strip()
            if not rendered:
                continue
            timestamp = str(message.get("timestamp") or "").strip()
            parts.append(f"[{timestamp}] {rendered}" if timestamp else rendered)
        return "\n\n".join(parts)

    def get_gateway_config_path(self) -> Optional[Path]:
        for path_candidate in self._openclaw_config_path_candidates():
            if path_candidate.exists():
                return path_candidate
        return None

    def parse_session_jsonl(self, path: Path) -> str:
        """Parse OC session JSONL with envelope compatibility."""
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
                if not isinstance(obj, dict):
                    continue

                record = obj
                row_timestamp = self._row_timestamp(record)
                row_type = str(record.get("type", "")).strip().lower()
                if row_type == "message" and isinstance(record.get("message"), dict):
                    record = record["message"]
                    row_timestamp = row_timestamp or self._row_timestamp(record)
                elif row_type in ("event_msg", "response_item"):
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        payload_type = str(payload.get("type", "")).strip().lower()
                        if payload_type in ("user_message", "agent_message"):
                            role = "user" if payload_type == "user_message" else "assistant"
                            text = str(payload.get("message", "")).strip()
                            source_type = ""
                            if self._is_host_memory_policy_reply(role, text):
                                continue
                            if "[Subagent Context]" in text or "You are running as a subagent" in text:
                                source_type = "subagent"
                                text = re.sub(
                                    r"^\[[^\]]+\]\s*\[Subagent Context\].*?(?:\n\n|\Z)",
                                    "",
                                    text,
                                    flags=re.DOTALL,
                                ).strip()
                                text = re.sub(r"^\[Subagent Task\]:\s*", "", text, flags=re.MULTILINE).strip()
                            if text:
                                messages.append({
                                    "role": role,
                                    "content": text,
                                    "source_type": source_type,
                                    "timestamp": row_timestamp,
                                })
                            continue
                        record = payload

                role = str(record.get("role", "")).strip().lower()
                if role not in ("user", "assistant"):
                    continue

                content = record.get("content", "")
                if content in (None, ""):
                    content = record.get("text", "")
                if content in (None, ""):
                    content = record.get("message", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(c.get("text", "")).strip()
                        for c in content
                        if isinstance(c, dict) and str(c.get("text", "")).strip()
                    )
                if not isinstance(content, str) or not content.strip():
                    continue

                stripped = content.strip()
                source_type = ""
                if self._is_host_memory_policy_reply(role, stripped):
                    continue
                if "[Subagent Context]" in stripped or "You are running as a subagent" in stripped:
                    source_type = "subagent"
                    stripped = re.sub(
                        r"^\[[^\]]+\]\s*\[Subagent Context\].*?(?:\n\n|\Z)",
                        "",
                        stripped,
                        flags=re.DOTALL,
                    ).strip()
                    stripped = re.sub(r"^\[Subagent Task\]:\s*", "", stripped, flags=re.MULTILINE).strip()
                    if not stripped:
                        continue
                messages.append({
                    "role": role,
                    "content": stripped,
                    "source_type": source_type,
                    "timestamp": row_timestamp,
                })

        return self._build_timestamped_transcript(messages)

    def get_bootstrap_markdown_globs(self) -> list:
        gateway_config_path = self.get_gateway_config_path()
        if not gateway_config_path:
            return []
        try:
            with open(gateway_config_path, "r", encoding="utf-8") as f:
                gw_cfg = json.load(f)
            hook = (
                gw_cfg.get("hooks", {})
                .get("internal", {})
                .get("entries", {})
                .get("bootstrap-extra-files", {})
            )
            if not hook.get("enabled", True):
                return []
            paths = hook.get("paths") or hook.get("patterns") or hook.get("files") or []
            if not isinstance(paths, list):
                return []
            safe_paths = []
            for item in paths:
                value = str(item or "").strip() if isinstance(item, str) else ""
                if not value or value.startswith("/") or ".." in Path(value).parts:
                    print(f"[adapter] skipped unsafe bootstrap markdown glob: {value!r}", file=sys.stderr)
                    continue
                safe_paths.append(value)
            return safe_paths
        except (json.JSONDecodeError, IOError, KeyError, UnicodeDecodeError) as e:
            print(f"[adapter] bootstrap markdown globs unavailable: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return []

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from config import get_config
        cfg = get_config()
        deep_model = str(getattr(cfg.models, "deep_reasoning", "") or "").strip()
        fast_model = str(getattr(cfg.models, "fast_reasoning", "") or "").strip()
        original_provider = str(getattr(cfg.models, "llm_provider", "") or "").strip()
        original_deep_model = deep_model
        original_fast_model = fast_model
        provider = str(getattr(cfg.models, "llm_provider", "") or "").strip()
        detected_provider = self._normalize_installer_provider(self._detect_gateway_primary_provider())
        provider_inferred = not provider or provider == "default"
        if not provider or provider == "default":
            provider = detected_provider or "anthropic"
        fast_effort = str(getattr(cfg.models, "fast_reasoning_effort", "") or "").strip()
        deep_effort = str(getattr(cfg.models, "deep_reasoning_effort", "") or "").strip()
        provider_tier_overridden = False
        notify_provider_fallback = False
        if model_tier == "fast":
            fast_provider = str(getattr(cfg.models, "fast_reasoning_provider", "") or "").strip()
            if fast_provider and fast_provider != "default":
                provider = self._normalize_installer_provider(fast_provider)
                provider_inferred = False
                provider_tier_overridden = True
        elif model_tier == "deep":
            deep_provider = str(getattr(cfg.models, "deep_reasoning_provider", "") or "").strip()
            if deep_provider and deep_provider != "default":
                provider = self._normalize_installer_provider(deep_provider)
                provider_inferred = False
                provider_tier_overridden = True
        if not deep_model or not fast_model:
            raise RuntimeError(
                "LLM provider requires deepReasoning and fastReasoning to be set in config.json. "
                f"Got deep={deep_model!r} fast={fast_model!r}."
            )
        if (
            provider == "anthropic"
            and (provider_inferred or (detected_provider == "openai" and not provider_tier_overridden))
            and not self.get_api_key("ANTHROPIC_API_KEY")
            and (detected_provider == "openai" or self.get_api_key("OPENAI_API_KEY"))
        ):
            provider = "openai"
            defaults = self.installer_default_models("openai") or {}
            if deep_model.startswith("claude-") and defaults.get("deep"):
                deep_model = str(defaults["deep"])
            if fast_model.startswith("claude-") and defaults.get("fast"):
                fast_model = str(defaults["fast"])
            notify_provider_fallback = True
        if provider == "openclaw-gateway":
            return OpenClawGatewayLLMProvider()
        if provider == "anthropic":
            api_key = self.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'anthropic' but no OpenClaw Anthropic token or ANTHROPIC_API_KEY was found. "
                    "Register a credential in QUAID_HOME/shared/auth/credentials.json or set ANTHROPIC_API_KEY."
                )
            return AnthropicLLMProvider(
                api_key=api_key,
                deep_model=deep_model,
                fast_model=fast_model,
            )
        if provider in ("openai", "openai-compatible"):
            api_key = self.get_api_key("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'openai' but no OpenClaw OpenAI OAuth token was found. "
                    "Write the token produced by 'codex setup-token' to "
                    "QUAID_HOME/adaptors/openclaw/.auth-token or set OPENAI_OAUTH_TOKEN."
                )
            if notify_provider_fallback:
                self._notify_provider_fallback(
                    configured_provider=original_provider,
                    detected_provider=detected_provider,
                    deep_model_before=original_deep_model,
                    fast_model_before=original_fast_model,
                    deep_model_after=deep_model,
                    fast_model_after=fast_model,
                )
            configured_base_url = str(getattr(cfg.models, "base_url", "") or "").strip()
            env_base_url = str(os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            base_url = configured_base_url or env_base_url or "https://chatgpt.com/backend-api"
            return OpenAICodexOAuthLLMProvider(
                base_url=base_url,
                api_key=api_key,
                deep_model=deep_model,
                fast_model=fast_model,
                deep_reasoning_effort=deep_effort or "high",
                fast_reasoning_effort=fast_effort or "none",
            )
        raise RuntimeError(
            f"Unknown OpenClaw LLM provider '{provider}'. "
            "Valid values: 'anthropic', 'openai', 'openclaw-gateway'."
        )

    def _notify_provider_fallback(
        self,
        *,
        configured_provider: str,
        detected_provider: str,
        deep_model_before: str,
        fast_model_before: str,
        deep_model_after: str,
        fast_model_after: str,
    ) -> None:
        provider_label = str(configured_provider or "default").strip() or "default"
        changes = []
        if deep_model_before != deep_model_after:
            changes.append(f"deep model {deep_model_before!r} -> {deep_model_after!r}")
        if fast_model_before != fast_model_after:
            changes.append(f"fast model {fast_model_before!r} -> {fast_model_after!r}")
        model_part = f"; {', '.join(changes)}" if changes else ""
        message = (
            "OpenClaw gateway is routed to OpenAI/Codex but Quaid config selected "
            f"{provider_label!r}. Quaid is using the OpenAI provider for this turn"
            f"{model_part}."
        )
        dedupe = (
            "openclaw-provider-fallback:"
            f"{provider_label}:{detected_provider}:{deep_model_before}:{fast_model_before}:"
            f"{deep_model_after}:{fast_model_after}"
        )
        if dedupe in self._provider_fallback_notices_seen:
            return
        try:
            if self.notify(f"[Quaid warning] [provider] {message}", force=True):
                self._provider_fallback_notices_seen.add(dedupe)
        except Exception:
            if is_fail_hard_enabled():
                raise
            print(
                f"[notify] Provider fallback notice failed for {dedupe}",
                file=sys.stderr,
            )

    def installer_supported_providers(self) -> list:
        return ["anthropic", "openai"]

    def installer_default_models(self, provider: str) -> Optional[dict]:
        raw = str(provider or "").strip().lower()
        # Check exact match first so provider-specific overrides (e.g. openai-codex)
        # take precedence over alias resolution.
        model_pair = self._INSTALLER_MODEL_DEFAULTS.get(raw) or \
            self._INSTALLER_MODEL_DEFAULTS.get(self._normalize_installer_provider(raw))
        if not model_pair:
            return None
        out = {"deep": str(model_pair["deep"]), "fast": str(model_pair["fast"])}
        deep_effort = str(model_pair.get("deepEffort", "")).strip()
        fast_effort = str(model_pair.get("fastEffort", "")).strip()
        if deep_effort:
            out["deepEffort"] = deep_effort
        if fast_effort:
            out["fastEffort"] = fast_effort
        return out

    def get_fast_provider_default(self) -> str:
        detected = self._normalize_installer_provider(self._detect_gateway_primary_provider())
        if detected in {"anthropic", "openai"}:
            return detected
        return "anthropic"

    def get_deep_provider_default(self) -> str:
        detected = self._normalize_installer_provider(self._detect_gateway_primary_provider())
        if detected in {"anthropic", "openai"}:
            return detected
        return "anthropic"

    def get_fast_model_default(self, provider: str) -> Optional[str]:
        p = str(provider or "").strip().lower()
        if not p or p == "default":
            p = self.get_fast_provider_default()
        defaults = self.installer_default_models(p) or {}
        model = str(defaults.get("fast", "")).strip()
        return model or None

    def get_deep_model_default(self, provider: str) -> Optional[str]:
        p = str(provider or "").strip().lower()
        if not p or p == "default":
            p = self.get_deep_provider_default()
        defaults = self.installer_default_models(p) or {}
        model = str(defaults.get("deep", "")).strip()
        return model or None

    def _installer_model_provider(self, provider: str, model: str) -> str:
        normalized_provider = self._normalize_installer_provider(provider)
        token = str(model or "").strip()
        if not token:
            return ""
        lower = token.lower()
        if "/" in token:
            return self._normalize_installer_provider(token.split("/", 1)[0])

        if normalized_provider:
            defaults = self.installer_default_models(normalized_provider) or {}
            if lower in {
                str(defaults.get("deep", "")).strip().lower(),
                str(defaults.get("fast", "")).strip().lower(),
            }:
                return normalized_provider

        for key, defaults in self._INSTALLER_MODEL_DEFAULTS.items():
            if lower in {
                str(defaults.get("deep", "")).strip().lower(),
                str(defaults.get("fast", "")).strip().lower(),
            }:
                return key

        if lower.startswith("claude-"):
            return "anthropic"
        if re.match(r"^(gpt-|o1(?:$|[-_])|o3(?:$|[-_])|o4(?:$|[-_]))", lower):
            return "openai"
        return ""

    def installer_review_model_pair(
        self,
        provider: str,
        deep_model: str,
        fast_model: str,
    ) -> dict:
        normalized_provider = self._normalize_installer_provider(
            provider or self._detect_gateway_primary_provider()
        )
        supported = set(self._INSTALLER_MODEL_DEFAULTS.keys())
        deep_provider = self._installer_model_provider(normalized_provider, deep_model)
        fast_provider = self._installer_model_provider(normalized_provider, fast_model)

        reason = ""
        if not normalized_provider:
            reason = "OpenClaw gateway default provider could not be detected."
        elif normalized_provider not in supported:
            reason = (
                f"No adapter fast/deep mapping is defined for provider "
                f"'{normalized_provider}'."
            )
        elif "/" in str(fast_model or "") and fast_provider not in supported:
            reason = (
                f"Fast model '{str(fast_model).strip()}' uses an unrecognized "
                "provider prefix."
            )
        elif "/" in str(deep_model or "") and deep_provider not in supported:
            reason = (
                f"Deep model '{str(deep_model).strip()}' uses an unrecognized "
                "provider prefix."
            )

        return {
            "provider": normalized_provider,
            "needsClarification": bool(reason),
            "reason": reason,
            "deep": {
                "model": str(deep_model or "").strip(),
                "recognized": bool(
                    deep_provider
                    or (
                        normalized_provider in supported
                        and str(deep_model or "").strip()
                        and "/" not in str(deep_model or "")
                    )
                ),
                "provider": deep_provider,
            },
            "fast": {
                "model": str(fast_model or "").strip(),
                "recognized": bool(
                    fast_provider
                    or (
                        normalized_provider in supported
                        and str(fast_model or "").strip()
                        and "/" not in str(fast_model or "")
                    )
                ),
                "provider": fast_provider,
            },
        }

    def installer_supports_live_model_validation(self) -> bool:
        return False

    def installer_validate_model_pair_live(
        self,
        provider: str,
        deep_model: str,
        fast_model: str,
    ) -> dict:
        _ = (provider, deep_model, fast_model)
        return {"supported": False, "ok": True, "message": "", "results": []}

    def _get_agent_config_dir(self) -> Path:
        """Path to the gateway's agent config directory."""
        return self._openclaw_root_dir() / "agents" / "main" / "agent"

    def _resolve_anthropic_credential(self) -> Optional[str]:
        """Resolve an Anthropic API key or OAuth token from the gateway auth store."""
        openclaw_dir = self._get_agent_config_dir()

        # 1. Auth profiles - the gateway's active credential store
        profiles_path = openclaw_dir / "auth-profiles.json"
        if profiles_path.exists():
            try:
                with open(profiles_path) as f:
                    data = json.load(f)
                # Find the active anthropic profile
                last_good = data.get("lastGood", {}).get("anthropic", "")
                profiles = data.get("profiles", {})
                if last_good and last_good in profiles:
                    profile = profiles[last_good]
                    token = profile.get("token") or profile.get("key")
                    if token:
                        return token
            except (json.JSONDecodeError, IOError, OSError):
                print("[adapter] anthropic credential resolution from auth-profiles failed", file=sys.stderr)
                if is_fail_hard_enabled():
                    raise

        # No env/.env fallback here: avoid implicit paid API usage.
        return None

    def discover_llm_providers(self) -> list:
        providers = [{"id": "default", "name": "Default (agent provider's model)"}]
        config_path = self.get_gateway_config_path()
        if not config_path:
            return providers
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            for profile_id, profile in cfg.get("auth", {}).get("profiles", {}).items():
                providers.append({
                    "id": profile_id,
                    "name": f"{profile.get('provider', 'unknown')} ({profile.get('mode', '')})",
                    "provider": profile.get("provider"),
                })
        except Exception as e:
            print(f"[adapter] discover_llm_providers fallback to default: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
        return providers

    def _get_gateway_auth(self):
        """Read gateway port and auth token from OpenClaw config."""
        config_path = self.get_gateway_config_path()
        if config_path:
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                return (
                    cfg.get("gateway", {}).get("port", 18789),
                    cfg.get("gateway", {}).get("auth", {}).get("token", ""),
                )
            except Exception as e:
                print(f"[adapter] gateway auth config read failed, using defaults: {e}", file=sys.stderr)
                if is_fail_hard_enabled():
                    raise
        return 18789, ""

    # ---- Multi-agent support ----

    @staticmethod
    def extract_agent_label_from_session_key(session_key: str) -> Optional[str]:
        """Parse the raw agent label from an OC session key 'agent:<label>:<channel>'.

        Returns the label (e.g. "main", "coding") or None if the key doesn't
        match the expected format. Use agent_id_prefix() to build the full
        instance ID: f"{prefix}-{label}".
        """
        parts = str(session_key or "").strip().split(":")
        if len(parts) >= 3 and parts[0] == "agent":
            label = parts[1].strip()
            return label if label else None
        return None

    def is_multi_agent(self) -> bool:
        return True

    def agent_id_prefix(self) -> str:
        """OC adapter prefix for building per-agent instance IDs (e.g. "openclaw").

        QUAID_INSTANCE is the primary agent's full ID ("openclaw-main"); stripping
        "-main" gives the shared prefix used for all agents.
        """
        return self.adapter_id()  # "openclaw"

    def list_agent_instance_ids(self) -> list:
        """Return fully-prefixed Quaid instance IDs for all OC agents.

        Unions two sources:
        1. agents.list from openclaw.json (registered OC agents)
        2. Silo directories under quaid_home() matching the "{prefix}-*" pattern
           (covers silos created via QUAID_INSTANCE that were never registered
           as OC agents, e.g. openclaw-livetest)

        Always returns at least ["<prefix>-main"], with "main" first.

        Examples: ["openclaw-main", "openclaw-coding", "openclaw-livetest"]
        """
        prefix = self.agent_id_prefix()

        # Source 1: OC agent registry
        labels: list[str] = []
        cfg_path = self.get_gateway_config_path()
        if cfg_path:
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                agents_list = cfg.get("agents", {}).get("list", []) or []
                for agent_row in agents_list:
                    if not isinstance(agent_row, dict):
                        continue
                    label = self._sanitize_agent_label(str(agent_row.get("id", "")).strip(), default="")
                    if label and label not in labels:
                        labels.append(label)
                agents = cfg.get("agents", {})
                if isinstance(agents, dict):
                    for key in agents.keys():
                        label = self._sanitize_agent_label(str(key or "").strip(), default="")
                        if label and label not in {"defaults", "list"} and label not in labels:
                            labels.append(label)
            except (json.JSONDecodeError, IOError, KeyError):
                if is_fail_hard_enabled():
                    raise
                pass

        # Source 2: silo directories under quaid_home()/instances
        silo_prefix = f"{prefix}-"
        try:
            home = self.quaid_home() / "instances"
            if home.exists():
                for entry in home.iterdir():
                    if entry.is_dir() and entry.name.startswith(silo_prefix):
                        label = self._sanitize_agent_label(entry.name[len(silo_prefix):], default="")
                        if label and label not in labels and self._agent_label_has_platform_state(label):
                            labels.append(label)
        except (OSError, RuntimeError):
            if is_fail_hard_enabled():
                raise
            pass

        if not labels:
            return [f"{prefix}-main"]

        # Ensure "main" is always first
        if "main" in labels:
            labels = ["main"] + [l for l in labels if l != "main"]
        else:
            labels = ["main"] + labels

        return [f"{prefix}-{label}" for label in labels]

    def _agent_label_has_platform_state(self, label: str) -> bool:
        clean = self._sanitize_agent_label(label, default="")
        if not clean:
            return False
        if clean == "main":
            return True
        cfg_path = self.get_gateway_config_path()
        if cfg_path:
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                agents = cfg.get("agents", {})
                if isinstance(agents, dict):
                    for row in agents.get("list", []) or []:
                        if (
                            isinstance(row, dict)
                            and self._sanitize_agent_label(row.get("id", ""), default="") == clean
                        ):
                            return True
                    if clean in {
                        self._sanitize_agent_label(str(key or ""), default="")
                        for key in agents.keys()
                    }:
                        return True
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                if is_fail_hard_enabled():
                    raise
                pass
        return (self._openclaw_root_dir() / "agents" / clean).exists()

    def get_instance_manager(self):
        return None

    # ---- Internal helpers ----

    def _find_sessions_json(self) -> Optional[Path]:
        """Find the agent sessions.json file."""
        primary = self._openclaw_root_dir() / "agents" / "main" / "sessions" / "sessions.json"
        if primary.exists():
            return primary
        fallback = (Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json").resolve()
        if fallback.exists():
            return fallback
        return None

    def _agent_sessions_dir(self, label: str) -> Path:
        clean = self._sanitize_agent_label(label, default="main")
        return self._openclaw_root_dir() / "agents" / clean / "sessions"

    def _sessions_json_has_agent_label(self, sessions_json: Path, label: str) -> bool:
        wanted = self._sanitize_agent_label(label, default="main")
        for session_key, _row in self._iter_sessions_index(sessions_json):
            raw_label = self.extract_agent_label_from_session_key(session_key)
            row_label = "main" if raw_label is None else self._sanitize_agent_label(raw_label, default="")
            if row_label == wanted:
                return True
        return False

    def _iter_sessions_index(self, sessions_json: Path):
        try:
            data = json.loads(sessions_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            if is_fail_hard_enabled():
                raise
            return []
        if not isinstance(data, dict):
            return []
        rows = []
        for session_key, row in data.items():
            if isinstance(row, dict):
                rows.append((str(session_key or ""), row))
            else:
                print(
                    f"[adapter] skipped unexpected sessions.json row for {session_key!r}",
                    file=sys.stderr,
                )
        return rows

    def _session_path_from_index_row(self, sessions_dir: Path, row: dict, session_id: str) -> Optional[Path]:
        safe_session_id = self._safe_session_id(session_id)
        if not safe_session_id:
            return None
        sessions_root = sessions_dir.expanduser().resolve()
        candidates = [
            row.get("sessionFile"),
            row.get("file"),
            row.get("path"),
            sessions_root / f"{safe_session_id}.jsonl",
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            try:
                raw_path = Path(text).expanduser()
                resolved = raw_path.resolve() if raw_path.is_absolute() else (sessions_root / raw_path).resolve()
            except (OSError, RuntimeError):
                continue
            if not self._path_is_under(resolved, sessions_root):
                print(f"[adapter] skipped session path outside sessions dir: {resolved}", file=sys.stderr)
                continue
            return resolved
        return None

    @classmethod
    def _session_id_from_path(cls, path: Path) -> str:
        name = Path(path).name
        marker = ".jsonl.reset."
        if marker in name:
            return cls._safe_session_id(name.split(marker, 1)[0])
        if name.endswith(".jsonl"):
            return cls._safe_session_id(name[: -len(".jsonl")])
        return ""

    def _path_matches_session_id(self, path: Path, session_id: str) -> bool:
        safe_session_id = self._safe_session_id(session_id)
        return bool(safe_session_id) and self._session_id_from_path(path) == safe_session_id

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.expanduser().resolve() == right.expanduser().resolve()
        except (OSError, RuntimeError):
            return left.expanduser() == right.expanduser()

    @staticmethod
    def _path_is_under(path: Path, parent: Path) -> bool:
        try:
            path.expanduser().resolve().relative_to(parent.expanduser().resolve())
            return True
        except (OSError, RuntimeError, ValueError):
            return False
