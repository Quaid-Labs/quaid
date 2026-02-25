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
from lib.adapter import ChannelInfo, QuaidAdapter, _read_env_file


class OpenClawAdapter(QuaidAdapter):
    """Adapter for running inside the OpenClaw gateway.

    - Home dir: CLAWDBOT_WORKSPACE env or ${QUAID_WORKSPACE}/
    - Notifications: openclaw message send CLI
    - Credentials: env var -> workspace .env
    - Sessions: ~/.openclaw/sessions/
    - Filtering: HEARTBEAT, GatewayRestart, System: messages
    """

    _MAIN_SESSION_KEY = "agent:main:main"

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

    def quaid_home(self) -> Path:
        env = os.environ.get("CLAWDBOT_WORKSPACE", "").strip()
        if env:
            return Path(env)
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
                    return Path(ws)
            except (json.JSONDecodeError, KeyError):
                pass
        raise RuntimeError(
            "CLAWDBOT_WORKSPACE environment variable is not set and "
            "could not resolve workspace from ~/.openclaw/openclaw.json. "
            "Set CLAWDBOT_WORKSPACE or configure adapter.type=standalone in config/memory.json."
        )

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False) -> bool:
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS"):
            return True

        info = self.get_last_channel()
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

            session = sessions.get(session_key)
            if not session:
                return None

            channel = session.get("lastChannel")
            target = session.get("lastTo")
            account_id = session.get("lastAccountId", "default")

            if not channel or not target:
                return None

            return ChannelInfo(
                channel=channel,
                target=target,
                account_id=account_id,
                session_key=session_key,
            )
        except (json.JSONDecodeError, IOError) as e:
            print(f"[notify] Error reading sessions: {e}", file=sys.stderr)
            return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        # 1. Environment variable
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key

        # 2. .env file in workspace root
        env_file = self.quaid_home() / ".env"
        if env_file.exists():
            found = _read_env_file(env_file, env_var_name)
            if found:
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
        if re.sub(r"[*_<>/b\\s]", "", text).startswith("HEARTBEAT_OK"):
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
        except (json.JSONDecodeError, IOError, KeyError, UnicodeDecodeError):
            return []

    def get_bootstrap_markdown_filenames(self) -> list:
        gateway_config_path = self.get_gateway_config_path()
        if gateway_config_path:
            try:
                with open(gateway_config_path, "r", encoding="utf-8") as f:
                    gw_cfg = json.load(f)
                hook = (
                    gw_cfg.get("hooks", {})
                    .get("internal", {})
                    .get("entries", {})
                    .get("bootstrap-extra-files", {})
                )
                names = (
                    hook.get("fileNames")
                    or hook.get("filenames")
                    or hook.get("names")
                    or hook.get("allowedFileNames")
                    or []
                )
                if isinstance(names, list) and names:
                    return [str(n) for n in names if str(n).strip()]
            except (json.JSONDecodeError, IOError, KeyError, UnicodeDecodeError):
                pass
        # Conservative fallback matching gateway bootstrap file naming.
        return [
            "AGENTS.md",
            "SOUL.md",
            "TOOLS.md",
            "USER.md",
            "MEMORY.md",
            "IDENTITY.md",
            "HEARTBEAT.md",
            "TODO.md",
            "PROJECT.md",
        ]

    def get_llm_provider(self, model_tier: Optional[str] = None):
        port, token = self._get_gateway_auth()
        return GatewayLLMProvider(port=port, token=token)

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
                pass

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
        except Exception:
            pass
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
            except Exception:
                pass
        return 18789, ""

    # ---- Internal helpers ----

    def _find_sessions_json(self) -> Optional[Path]:
        """Find the agent sessions.json file."""
        p = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
        return p if p.exists() else None

    @staticmethod
    def _keychain_lookup(service: str, account: str) -> Optional[str]:
        """Keychain lookup stub - .env is preferred over Keychain."""
        return None
