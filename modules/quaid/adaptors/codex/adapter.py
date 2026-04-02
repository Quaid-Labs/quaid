"""Codex adapter for Quaid memory system."""

from __future__ import annotations

from collections import deque
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from adaptors.codex.providers import CodexLLMProvider
from lib.adapter import QuaidAdapter, read_env_file
from lib.fail_policy import is_fail_hard_enabled
from lib.instance import instance_id, instance_slug_from_project_dir


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class CodexAdapter(QuaidAdapter):
    """Adapter for Codex CLI/app sessions."""

    def __init__(self, home: Optional[Path] = None):
        self._home = home

    def quaid_home(self) -> Path:
        if self._home is not None:
            return self._home
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / "quaid"

    def adapter_id(self) -> str:
        return "codex"

    def get_instance_type(self) -> str:
        return "folder"

    def get_instance_name(self) -> str:
        project_dir = os.environ.get("CODEX_PROJECT_DIR", "").strip() or os.getcwd()
        return instance_slug_from_project_dir(project_dir)

    def agent_id_prefix(self) -> str:
        return self.adapter_id()

    def list_agent_instance_ids(self) -> list:
        prefix = self.agent_id_prefix() + "-"
        current = self.instance_id()
        try:
            home = self.quaid_home()
            found = sorted(
                d.name for d in home.iterdir()
                if d.is_dir() and d.name.startswith(prefix)
            )
        except Exception:
            found = []
        if current in found:
            return [current] + [item for item in found if item != current]
        return [current] + found

    def _pending_notifications_path(self) -> Path:
        return self.data_dir() / "codex-pending-notifications.jsonl"

    def notify(
        self,
        message: str,
        channel_override: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> bool:
        _ = channel_override
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS") and not force:
            return True
        if dry_run:
            print(f"[notify] (dry-run) {message}", file=sys.stderr)
            return True
        try:
            pending = self._pending_notifications_path()
            pending.parent.mkdir(parents=True, exist_ok=True)
            with open(pending, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"message": message, "ts": _now_iso()}) + "\n")
            return True
        except Exception as exc:
            print(f"[notify] Failed to queue Codex notification: {exc}", file=sys.stderr)
            return False

    def get_pending_context(self, max_age_seconds: int = 3600) -> str:
        pending = self._pending_notifications_path()
        if not pending.is_file():
            return ""
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            messages = []
            with open(pending, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = str(entry.get("ts") or "").strip()
                    if ts and max_age_seconds > 0:
                        entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if (now - entry_dt).total_seconds() > max_age_seconds:
                            continue
                    message = str(entry.get("message") or "").strip()
                    if message:
                        messages.append(message)
            pending.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[notify] Failed to drain Codex notifications: {exc}", file=sys.stderr)
            return ""
        if not messages:
            return ""
        body = "\n".join(f"• {message}" for message in messages)
        return (
            "Print <quaid_notification> verbatim before your response, then print ---\n\n"
            f"<quaid_notification>\n{body}\n</quaid_notification>"
        )

    def get_last_channel(self, session_key: str = "") -> None:
        _ = session_key
        return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key
        if is_fail_hard_enabled():
            raise RuntimeError(
                f"[fail_hard] {env_var_name} is required but not set in the environment."
            )
        print(
            f"[adapter][FALLBACK] {env_var_name} not found in env; "
            "attempting .env lookup because failHard is disabled.",
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

    def get_host_info(self):
        from core.compatibility import HostInfo

        binary = shutil.which("codex")
        if not binary:
            for candidate in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
                if Path(candidate).exists():
                    binary = candidate
                    break

        version = "unknown"
        if binary:
            try:
                result = subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    version = result.stdout.strip().split()[-1].lstrip("v")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        return HostInfo(platform="codex", version=version, binary_path=binary)

    def get_base_context_files(self):
        files = {}
        for candidate in (
            Path.cwd() / "AGENTS.md",
            Path.cwd() / ".codex" / "AGENTS.md",
        ):
            if candidate.is_file():
                files[str(candidate.resolve())] = {
                    "purpose": "Codex project instructions",
                    "maxLines": 500,
                }
                break
        return files

    def get_cli_tools_snippet(self) -> str:
        instance_name = os.environ.get("QUAID_INSTANCE", "").strip()
        if not instance_name:
            try:
                instance_name = instance_id()
            except Exception:
                instance_name = ""
        home = self.quaid_home()
        misc_path = home / "projects" / f"misc--{instance_name}" if instance_name else None
        if not instance_name or misc_path is None:
            return ""
        return (
            "### Codex Project Placement Rules\n\n"
            f"- Active instance: `{instance_name}`\n"
            "- Before writing any file, running a multi-step task, or delegating work, first create a tracked project:\n"
            "  - `quaid registry create-project <name> --source-roots <path>`\n"
            "- If the user asks to change a project's metadata, update the registry directly:\n"
            "  - `quaid project update <name> --description \"...\"`\n"
            "- Do not treat edits to `PROJECT.md` as the authoritative way to change project description metadata.\n"
            "- Do not write files outside tracked projects.\n"
            "- For throwaway or temporary work, use the misc project for this instance:\n"
            f"  - `misc--{instance_name}` at `{misc_path}/`\n"
            f"  - If it is missing, register it first with `quaid registry create-project misc--{instance_name} --source-roots {misc_path}/`\n"
            "- Always tell the user which project received the file.\n"
        )

    def get_sessions_dir(self) -> Optional[Path]:
        sessions_dir = Path.home() / ".codex" / "sessions"
        return sessions_dir if sessions_dir.is_dir() else None

    def get_session_path(self, session_id: str) -> Optional[Path]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        sessions_dir = self.get_sessions_dir()
        if sessions_dir is None:
            return None
        matches = sorted(
            sessions_dir.rglob(f"rollout-*{session_id}.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    def filter_system_messages(self, text: str) -> bool:
        if (
            text.startswith("[notify]")
            or text.startswith("[quaid]")
            or text.startswith("<environment_context>")
        ):
            return True
        return False

    @staticmethod
    def _extract_lifecycle_command(text: str) -> str:
        value = str(text or "").strip()
        if not value.startswith("/"):
            return ""
        command = value.split()[0].lower()
        if command in ("/new", "/clear", "/reset", "/restart"):
            return command
        return ""

    def _scan_lifecycle_candidates(self, container: dict) -> str:
        if not isinstance(container, dict):
            return ""
        for key in ("command", "prompt", "message", "input", "last_user_message", "text"):
            cmd = self._extract_lifecycle_command(container.get(key, ""))
            if cmd:
                return cmd
        payload = container.get("payload")
        if isinstance(payload, dict):
            for key in ("command", "prompt", "message", "input", "last_user_message", "text"):
                cmd = self._extract_lifecycle_command(payload.get(key, ""))
                if cmd:
                    return cmd
        return ""

    def _detect_lifecycle_command(self, hook_input: dict, transcript_path: str) -> str:
        if not isinstance(hook_input, dict):
            hook_input = {}

        direct = self._scan_lifecycle_candidates(hook_input)
        if direct:
            return direct

        try:
            tail = deque(maxlen=128)
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    line = raw.strip()
                    if line:
                        tail.append(line)
            for raw in reversed(tail):
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                record_type = str(obj.get("type") or "").strip()
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                if record_type == "event_msg" and str(payload.get("type") or "").strip() == "user_message":
                    return self._extract_lifecycle_command(str(payload.get("message") or ""))
                if record_type == "response_item" and str(payload.get("type") or "").strip() == "message":
                    role = str(payload.get("role") or "").strip().lower()
                    if role != "user":
                        continue
                    content = payload.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            cmd = self._extract_lifecycle_command(
                                str(item.get("text") or item.get("input_text") or item.get("output_text") or "")
                            )
                            if cmd:
                                return cmd
                    elif isinstance(content, str):
                        cmd = self._extract_lifecycle_command(content)
                        if cmd:
                            return cmd
        except OSError:
            return ""
        return ""

    def resolve_prompt_submit_signal(self, hook_input):
        command = self._scan_lifecycle_candidates(hook_input)
        if not command:
            return None
        return {
            "signal_type": "session_end",
            "meta": {
                "source": "hook_inject",
                "command": command,
                "reason": f"command:{command.lstrip('/')}",
            },
        }

    def parse_session_jsonl(self, path: Path) -> str:
        messages = []
        fallback_messages = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record_type = str(obj.get("type") or "").strip()
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                if record_type == "event_msg":
                    payload_type = str(payload.get("type") or "").strip()
                    if payload_type == "user_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({"role": "user", "content": text})
                    elif payload_type == "agent_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({"role": "assistant", "content": text})
                    continue

                if record_type == "response_item" and str(payload.get("type") or "").strip() == "message":
                    role = str(payload.get("role") or "").strip().lower()
                    if role not in ("user", "assistant"):
                        continue
                    content = payload.get("content", [])
                    text_parts = []
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            text = str(item.get("text") or item.get("input_text") or item.get("output_text") or "").strip()
                            if text:
                                text_parts.append(text)
                    elif isinstance(content, str) and content.strip():
                        text_parts.append(content.strip())
                    text = "\n".join(text_parts).strip()
                    if text:
                        fallback_messages.append({"role": role, "content": text})

        selected = messages if messages else fallback_messages
        deduped = []
        last_pair = None
        for message in selected:
            pair = (message.get("role"), message.get("content"))
            if pair == last_pair:
                continue
            deduped.append(message)
            last_pair = pair
        return self.build_transcript(deduped)

    def resolve_stop_hook_signal(self, hook_input, transcript_path):
        command = self._detect_lifecycle_command(hook_input, transcript_path)
        if not command:
            return None
        return {
            "signal_type": "session_end",
            "meta": {
                "source": "hook_codex_stop",
                "command": command,
                "reason": f"command:{command.lstrip('/')}",
            },
        }

    def get_llm_provider(self, model_tier: Optional[str] = None):
        _ = model_tier
        try:
            from config import get_config

            cfg = get_config()
            deep_model = getattr(cfg.models, "deep_reasoning", "gpt-5.4") or "gpt-5.4"
            fast_model = getattr(cfg.models, "fast_reasoning", "gpt-5.4-mini") or "gpt-5.4-mini"
            deep_effort = getattr(cfg.models, "deep_reasoning_effort", "high") or "high"
            fast_effort = getattr(cfg.models, "fast_reasoning_effort", "none") or "none"
        except Exception:
            deep_model = "gpt-5.4"
            fast_model = "gpt-5.4-mini"
            deep_effort = "high"
            fast_effort = "none"
        return CodexLLMProvider(
            deep_model=str(deep_model),
            fast_model=str(fast_model),
            deep_reasoning_effort=str(deep_effort),
            fast_reasoning_effort=str(fast_effort),
        )

    def installer_supported_providers(self) -> list:
        return ["openai"]

    def installer_default_models(self, provider: str) -> Optional[dict]:
        if str(provider or "").strip().lower() != "openai":
            return None
        return {"deep": "gpt-5.4", "fast": "gpt-5.4-mini"}

    def get_fast_provider_default(self) -> str:
        return "openai"

    def get_deep_provider_default(self) -> str:
        return "openai"
