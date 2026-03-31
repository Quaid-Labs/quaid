"""OpenClaw-specific Quaid adapter implementation."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from adaptors.openclaw.providers import GatewayLLMProvider
from lib.adapter import ChannelInfo, QuaidAdapter, read_env_file
from lib.fail_policy import is_fail_hard_enabled


class OpenClawAdapter(QuaidAdapter):
    """Adapter for running inside the OpenClaw gateway.

    - Home dir: QUAID_HOME env or ${QUAID_WORKSPACE}/
    - Notifications: openclaw message send CLI
    - Credentials: env var -> workspace .env
    - Sessions: ~/.openclaw/sessions/
    - Filtering: HEARTBEAT, GatewayRestart, System: messages
    """

    _MAIN_SESSION_KEY = "agent:main:main"

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
            return None

    def _resolve_message_cli(self) -> Optional[str]:
        """Resolve message CLI binary path for notification delivery."""
        explicit = os.environ.get("QUAID_MESSAGE_CLI", "").strip()
        if explicit:
            return explicit

        candidates = [
            shutil.which("openclaw"),
            "/opt/homebrew/bin/openclaw",
            shutil.which("clawdbot"),
            "/opt/homebrew/bin/clawdbot",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    def adapter_id(self) -> str:
        return "openclaw"

    def get_instance_name(self) -> str:
        """Return the current agent's instance name.

        OC resolves agent identity at the TypeScript layer (from sessions.json
        keys) and injects QUAID_INSTANCE into Python subprocess env via
        buildPythonEnv(). This method reads that value so Python callsites
        have a consistent way to get the instance name across adapters.

        Falls back to "openclaw-main" if QUAID_INSTANCE is not set.
        """
        return os.environ.get("QUAID_INSTANCE", "openclaw-main")

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
                )
                if result.returncode == 0 and result.stdout.strip():
                    version = result.stdout.strip().split()[-1]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

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

    def quaid_home(self) -> Path:
        """Root directory containing all Quaid instances (QUAID_HOME)."""
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / "quaid"

    def oc_workspace(self) -> Path:
        """OpenClaw workspace directory (platform-specific, not Quaid instance root).

        This is where OC stores its own files (SOUL.md, USER.md, etc.) and
        where the bootstrap boundary guard expects files. NOT the same as
        quaid_home() or instance_root().
        """
        # Fallback: resolve workspace from gateway config when env vars are absent.
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if cfg_path.exists():
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
                    return Path(ws).resolve()
            except (json.JSONDecodeError, KeyError):
                pass
        raise RuntimeError(
            "Could not resolve OpenClaw workspace from ~/.openclaw/openclaw.json. "
            "Set QUAID_HOME or configure adapter.type=standalone in config/memory.json."
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
        message_cli = self._resolve_message_cli()
        if not message_cli:
            print("[notify] No message CLI found (expected openclaw)", file=sys.stderr)
            return False

        def _send(channel: str) -> bool:
            cmd = [
                message_cli, "message", "send",
                "--channel", channel,
                "--target", info.target,
                "--message", message,
            ]
            if info.account_id and info.account_id != "default":
                cmd.extend(["--account", info.account_id])
            if dry_run:
                print(f"[notify] Would run: {' '.join(cmd)}")
                return True
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                print(f"[notify] Sent to {channel}:{info.target}")
                return True
            err = (result.stderr or "").strip() or "(no stderr)"
            print(f"[notify] Send failed on channel={channel}: {err}", file=sys.stderr)
            return False

        try:
            if _send(effective_channel):
                return True
            # Fallback: if override channel fails, retry on the last active channel.
            if channel_override and channel_override != info.channel:
                print(
                    f"[notify] Override channel failed; retrying with last channel={info.channel}",
                    file=sys.stderr,
                )
                return _send(info.channel)
            return False
        except subprocess.TimeoutExpired:
            print("[notify] Send timed out", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[notify] Error: {e}", file=sys.stderr)
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
            return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        # 1. Environment variable
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key

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
        d = Path.home() / ".openclaw" / "sessions"
        return d if d.is_dir() else None

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

    def get_gateway_config_path(self) -> Optional[Path]:
        p = Path.home() / ".openclaw" / "openclaw.json"
        return p if p.exists() else None

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
            return paths if isinstance(paths, list) else []
        except (json.JSONDecodeError, IOError, KeyError, UnicodeDecodeError) as e:
            print(f"[adapter] bootstrap markdown globs unavailable: {e}", file=sys.stderr)
            return []

    def get_llm_provider(self, model_tier: Optional[str] = None):
        port, token = self._get_gateway_auth()
        return GatewayLLMProvider(port=port, token=token)

    def installer_supported_providers(self) -> list:
        return ["anthropic", "openai", "openrouter", "together", "ollama"]

    def installer_default_models(self, provider: str) -> Optional[dict]:
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

    def _get_agent_config_dir(self) -> Path:
        """Path to the gateway's agent config directory."""
        return Path.home() / ".openclaw" / "agents" / "main" / "agent"

    def _resolve_anthropic_credential(self) -> Optional[str]:
        """Resolve an Anthropic API key or OAuth token from the gateway's auth store.

        Resolution chain:
        1. Gateway auth-profiles.json (lastGood.anthropic -> profile token/key)
        """
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
                labels = [
                    str(a.get("id", "")).strip().lower()
                    for a in agents_list
                    if isinstance(a, dict) and a.get("id")
                ]
            except (json.JSONDecodeError, IOError, KeyError):
                pass

        # Source 2: silo directories under quaid_home()
        silo_prefix = f"{prefix}-"
        try:
            home = self.quaid_home()
            for entry in home.iterdir():
                if entry.is_dir() and entry.name.startswith(silo_prefix):
                    label = entry.name[len(silo_prefix):]
                    if label and label not in labels:
                        labels.append(label)
        except (OSError, RuntimeError):
            pass

        if not labels:
            return [f"{prefix}-main"]

        # Ensure "main" is always first
        if "main" in labels:
            labels = ["main"] + [l for l in labels if l != "main"]
        elif "main" not in labels:
            labels = ["main"] + labels

        return [f"{prefix}-{label}" for label in labels]

    # ---- Internal helpers ----

    def _find_sessions_json(self) -> Optional[Path]:
        """Find the agent sessions.json file."""
        p = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
        return p if p.exists() else None
