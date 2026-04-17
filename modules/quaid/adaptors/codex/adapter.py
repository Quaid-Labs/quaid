"""Codex adapter for Quaid memory system."""

from __future__ import annotations

from collections import deque
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from lib.adapter import QuaidAdapter, read_env_file
from lib.fail_policy import is_fail_hard_enabled
from lib.instance import instance_id, instance_slug_from_project_dir


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class CodexAdapter(QuaidAdapter):
    """Adapter for Codex CLI/app sessions."""

    ADAPTER_CONFIG = {
        "deferred_notice_relay": True,
        "inject_tool_output_trace": True,
        "inject_project_list_fidelity_context": True,
        "context_refresh_strategy": "turn_based",
        "context_refresh_guard": {
            "min_interval_minutes": 30,
            "min_turns": 50,
        },
        "session_lookup_glob_template": "rollout-*{session_id}.jsonl",
        "session_pending_path_template": "{date_prefix}/rollout-pending-{session_id}.jsonl",
        "session_pending_default_root": "~/.codex/sessions",
        "session_fallback_path_template": "",
        "session_start_output_mode": "additional_context",
        "session_start_include_pending_context": True,
        "platform_config_scope": "codex",
    }

    _HOOK_STATUS_LINE_RE = re.compile(
        r"^\s*(?:•\s*)?(?:Running\s+)?(?:SessionStart|UserPromptSubmit|Stop|session_start|user_prompt_submit|stop)\s+hook(?::|\s+\(completed\)).*$",
        flags=re.IGNORECASE,
    )
    _HOOK_CONTEXT_LINE_RE = re.compile(
        r"^\s*hook\s+(?:context|output):.*$",
        flags=re.IGNORECASE,
    )
    _QUAID_MEMORY_CONTEXT_RE = re.compile(
        r"<quaid_memory_context>.*?</quaid_memory_context>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _QUAID_NOTIFICATION_RE = re.compile(
        r"<quaid_notification>.*?</quaid_notification>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _QUAID_NOTICE_COMMENTARY_RE = re.compile(
        r"^\s*(?:You started a new interaction\..*?pending Quaid notice.*?|I(?:'|’)m checking .*?Quaid.*?notice.*?)\s*$",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _QUAID_NOTICE_BULLET_BLOCK_RE = re.compile(
        r"\n\nQuaid notices?:\n(?:- .*(?:\n|$))+",
        flags=re.IGNORECASE,
    )
    _QUAID_NOTICE_INLINE_RE = re.compile(
        r"\n\nQuaid notice:\s*.*?(?=\n\n(?:[A-Z][a-z]+:|Subagent/)|\Z)",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _ROLLOUT_SESSION_ID_RE = re.compile(
        r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$",
        flags=re.IGNORECASE,
    )

    def __init__(self, home: Optional[Path] = None):
        self._home = home

    def quaid_home(self) -> Path:
        if self._home is not None:
            return self._home
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / ".quaid"

    @classmethod
    def installer_adapter_id(cls) -> str:
        return "codex"

    @classmethod
    def installer_cli_candidates(cls) -> list[str]:
        return ["codex"]

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
            home = self.quaid_home() / "instances"
            found = sorted(
                d.name for d in home.iterdir()
                if d.is_dir() and d.name.startswith(prefix)
            )
        except Exception:
            found = []
        if current in found:
            return [current] + [item for item in found if item != current]
        return [current] + found

    def get_instance_manager(self):
        # CDX manages instances automatically via CODEX_PROJECT_DIR path-hash.
        # No user-driven instance creation needed.
        return None

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

    def get_pending_context(self, max_age_seconds: int = 300) -> str:
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
            "The following are pending notifications for the user — please relay them in your response:\n\n"
            f"<quaid_system_message>\n{body}\n</quaid_system_message>"
        )

    def get_last_channel(self, session_key: str = "") -> None:
        _ = session_key
        return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        if env_var_name == "ANTHROPIC_API_KEY":
            token = self.read_shared_auth_token(["anthropic_oauth", "anthropic_api"]) or self.read_auth_token()
            if token:
                return token
        elif env_var_name == "OPENAI_API_KEY":
            token = self.read_shared_auth_token(["codex_oauth", "openai_api"])
            if token:
                return token
        else:
            token = self.read_auth_token()
            if token:
                return token
        if env_var_name == "OPENAI_API_KEY":
            oauth = os.environ.get("OPENAI_OAUTH_TOKEN", "").strip()
            if oauth:
                return oauth
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

    def auth_token_path(self) -> Optional[Path]:
        return self.quaid_home() / "adaptors" / "codex" / ".auth-token"

    def auth_registry_kinds(self) -> list[str]:
        return ["anthropic_oauth", "anthropic_api", "codex_oauth", "openai_api"]

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
        home = self.visible_home()
        misc_path = home / "projects" / f"misc--{instance_name}" if instance_name else None
        if not instance_name or misc_path is None:
            return ""
        return (
            "### Codex Project Placement Rules\n\n"
            f"- Active instance: `{instance_name}`\n"
            "- Before writing any file, running a multi-step task, or delegating work, first create a tracked project:\n"
            "  - `quaid project create <name> --source-root <path>`\n"
            "- If the user asks to change a project's metadata, update the registry directly:\n"
            "  - `quaid project update <name> --description \"...\"`\n"
            "- Do not treat edits to `PROJECT.md` as the authoritative way to change project description metadata.\n"
            "- Do not write files outside tracked projects.\n"
            "- For throwaway or temporary work, use the misc project for this instance:\n"
            f"  - `misc--{instance_name}` at `{misc_path}/`\n"
            f"  - If it is missing, create it first with `quaid project create misc--{instance_name} --source-root {misc_path}/`\n"
            f"  - After writing a throwaway file, register it: `quaid registry register <absolute-file-path> --project misc--{instance_name}`\n"
            "- Always tell the user which project received the file.\n"
        )

    def _last_session_path(self) -> Path:
        return self.data_dir() / "codex-last-session.json"

    def _read_last_session_id(self) -> str:
        try:
            data = json.loads(self._last_session_path().read_text(encoding="utf-8"))
            return str(data.get("session_id") or "").strip()
        except (OSError, json.JSONDecodeError):
            return ""

    def _write_last_session_id(self, session_id: str) -> None:
        try:
            path = self._last_session_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
        except OSError:
            pass

    def _extract_hook_session_id(self, hook_input: dict) -> str:
        if not isinstance(hook_input, dict):
            return ""

        def _candidate(value) -> str:
            return str(value or "").strip()

        candidates = [
            _candidate(hook_input.get("session_id")),
            _candidate(hook_input.get("thread_id")),
            _candidate(hook_input.get("threadId")),
            _candidate(hook_input.get("conversation_id")),
        ]
        session_obj = hook_input.get("session")
        if isinstance(session_obj, dict):
            candidates.extend([
                _candidate(session_obj.get("id")),
                _candidate(session_obj.get("session_id")),
                _candidate(session_obj.get("thread_id")),
            ])
        thread_obj = hook_input.get("thread")
        if isinstance(thread_obj, dict):
            candidates.extend([
                _candidate(thread_obj.get("id")),
                _candidate(thread_obj.get("thread_id")),
                _candidate(thread_obj.get("session_id")),
            ])
        transcript_path = _candidate(hook_input.get("transcript_path"))
        if transcript_path:
            match = self._ROLLOUT_SESSION_ID_RE.search(Path(transcript_path).name)
            if match:
                candidates.append(_candidate(match.group(1)))
        for value in candidates:
            if value:
                return value
        return ""

    def check_session_transition(self, hook_input: dict) -> Optional[dict]:
        """Detect when a CDX /new or /clear started a new session.

        CDX CLI intercepts lifecycle commands before UserPromptSubmit fires, so
        the command text never reaches the hook payload or transcript.  Instead,
        we track the last seen session_id in a silo file.  When the session_id
        changes on a new UserPromptSubmit, the previous session ended (via /new
        or /clear) and we return a session_end signal spec for it so the caller
        can write an extraction signal for the now-closed session.

        Returns a signal-spec dict for the ENDED session, or None.
        """
        if not isinstance(hook_input, dict):
            return None
        current_id = self._extract_hook_session_id(hook_input)
        if not current_id:
            return None
        last_id = self._read_last_session_id()
        # Always update to the current session_id.
        self._write_last_session_id(current_id)
        if not last_id or last_id == current_id:
            return None
        # Session changed — the old session ended via /new or /clear.
        transcript_path = self.get_session_path(last_id)
        if transcript_path is None:
            return None
        return {
            "ended_session_id": last_id,
            "ended_transcript_path": str(transcript_path),
            "signal_type": "session_end",
            "meta": {
                "source": "session_transition",
                "command": "/new",
                "reason": "command:new",
            },
        }

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

    def sanitize_transcript_text(self, text: str) -> str:
        # Base class strips <quaid_system_message> tag blocks.
        value = super().sanitize_transcript_text(text)
        if not value:
            return ""
        # Strip Codex-visible Quaid helper blocks before extraction parsing.
        value = self._QUAID_MEMORY_CONTEXT_RE.sub("", value)
        value = self._QUAID_NOTIFICATION_RE.sub("", value)
        # Strip CDX UI lifecycle lines that Quaid cannot tag (generated by the
        # Codex CLI itself, not by Quaid injection).
        lines = value.splitlines()
        cleaned = [
            line for line in lines
            if not self._HOOK_STATUS_LINE_RE.match(line)
            and not self._HOOK_CONTEXT_LINE_RE.match(line)
        ]
        value = "\n".join(cleaned).strip()
        if not value:
            return ""
        value = self._QUAID_NOTICE_COMMENTARY_RE.sub("", value)
        value = self._QUAID_NOTICE_BULLET_BLOCK_RE.sub("", value)
        value = self._QUAID_NOTICE_INLINE_RE.sub("", value)
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
        return value

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
        session_source_type = ""
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
                if record_type == "session_meta":
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
                    subagent = source.get("subagent") if isinstance(source, dict) else {}
                    if isinstance(subagent, dict) and isinstance(subagent.get("thread_spawn"), dict):
                        session_source_type = "subagent"
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

                if record_type == "event_msg":
                    payload_type = str(payload.get("type") or "").strip()
                    if payload_type == "user_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({"role": "user", "content": text, "source_type": session_source_type})
                    elif payload_type == "agent_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({"role": "assistant", "content": text, "source_type": session_source_type})
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
                        fallback_messages.append({"role": role, "content": text, "source_type": session_source_type})

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

    @classmethod
    def _path_session_id(cls, path: Path) -> str:
        stem = path.stem
        match = cls._ROLLOUT_SESSION_ID_RE.search(stem)
        return str(match.group(1) if match else "").strip()

    @staticmethod
    def _subagent_parent_id_from_path(path: Path) -> str:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(obj.get("type") or "").strip() != "session_meta":
                        continue
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
                    subagent = source.get("subagent") if isinstance(source, dict) else {}
                    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else {}
                    if isinstance(spawn, dict):
                        return str(spawn.get("parent_thread_id") or "").strip()
        except OSError:
            return ""
        return ""

    def is_subagent_session(self, session_id: str, transcript_path: Optional[Path] = None) -> bool:
        path = transcript_path or self.get_session_path(session_id)
        if path is None or not path.is_file():
            return False
        return bool(self._subagent_parent_id_from_path(path))

    def discover_subagent_children(self, parent_session_id: str) -> list[dict]:
        parent = str(parent_session_id or "").strip()
        if not parent:
            return []
        sessions_dir = self.get_sessions_dir()
        if sessions_dir is None:
            return []
        found: list[dict] = []
        seen_ids: set[str] = set()
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            child_parent = self._subagent_parent_id_from_path(path)
            if child_parent != parent:
                continue
            child_id = self._path_session_id(path) or path.stem
            if not child_id or child_id in seen_ids:
                continue
            seen_ids.add(child_id)
            found.append(
                {
                    "child_id": child_id,
                    "transcript_path": str(path),
                    "child_type": "codex-subagent",
                }
            )
        return found

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

    @staticmethod
    def _infer_provider_from_models(*models: str) -> str:
        for raw in models:
            token = str(raw or "").strip().lower()
            if not token or token == "default":
                continue
            if token.startswith("claude-"):
                return "anthropic"
            if re.match(r"^(gpt-|o1(?:$|[-_])|o3(?:$|[-_])|o4(?:$|[-_]))", token):
                return "openai"
        return ""

    def _detect_shared_primary_provider(self) -> str:
        has_anthropic = bool(self.read_shared_auth_token(["anthropic_oauth", "anthropic_api"]))
        has_openai = bool(self.read_shared_auth_token(["codex_oauth", "openai_api"]))
        if has_anthropic and not has_openai:
            return "anthropic"
        if has_openai and not has_anthropic:
            return "openai"
        return ""

    def _resolve_central_provider(self, configured: str, deep_model: str, fast_model: str) -> str:
        provider = str(configured or "").strip().lower()
        if provider and provider != "default":
            return provider

        inferred = self._infer_provider_from_models(deep_model, fast_model)
        if inferred:
            return inferred

        detected = self._detect_shared_primary_provider()
        if detected:
            return detected

        if is_fail_hard_enabled():
            raise RuntimeError(
                "[fail_hard] models.llmProvider is unset/default and no central provider could be inferred "
                "from configured models or shared auth credentials."
            )

        print(
            "[adapter][FALLBACK] models.llmProvider is unset/default and central provider inference failed; "
            "defaulting to anthropic because failHard is disabled.",
            file=sys.stderr,
        )
        return "anthropic"

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from config import get_config
        from lib.providers import AnthropicLLMProvider, OpenAICodexOAuthLLMProvider

        cfg = get_config()
        deep_model = getattr(cfg.models, "deep_reasoning", "claude-sonnet-4-5")
        fast_model = getattr(cfg.models, "fast_reasoning", "claude-haiku-4-5")
        deep_effort = getattr(cfg.models, "deep_reasoning_effort", "high")
        fast_effort = getattr(cfg.models, "fast_reasoning_effort", "none")
        provider_id = getattr(cfg.models, "llm_provider", "") or "anthropic"
        if model_tier == "fast":
            fast_provider = getattr(cfg.models, "fast_reasoning_provider", "default")
            if fast_provider and fast_provider != "default":
                provider_id = fast_provider
        elif model_tier == "deep":
            deep_provider = getattr(cfg.models, "deep_reasoning_provider", "default")
            if deep_provider and deep_provider != "default":
                provider_id = deep_provider
        provider_id = self._resolve_central_provider(
            provider_id,
            str(deep_model or ""),
            str(fast_model or ""),
        )
        provider_id = str(provider_id or "").strip().lower()

        if provider_id == "anthropic":
            api_key = self.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'anthropic' but no Codex Anthropic credential was found. "
                    "Write an Anthropic OAuth token or API key to "
                    "QUAID_HOME/shared/auth/credentials.json via 'quaid auth refresh --kind anthropic_oauth|anthropic_api', "
                    "or set ANTHROPIC_API_KEY."
                )
            return AnthropicLLMProvider(
                api_key=api_key,
                deep_model=str(deep_model or "claude-sonnet-4-5"),
                fast_model=str(fast_model or "claude-haiku-4-5"),
            )

        if provider_id in ("openai", "openai-compatible"):
            api_key = self.get_api_key("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'openai' but no Codex/OpenAI credential was found. "
                    "Write a Codex OAuth token or OpenAI API key to "
                    "QUAID_HOME/shared/auth/credentials.json via "
                    "'quaid auth refresh --kind codex_oauth|openai_api', "
                    "or set OPENAI_OAUTH_TOKEN / OPENAI_API_KEY."
                )
            configured_base_url = str(getattr(cfg.models, "base_url", "") or "").strip()
            env_base_url = str(os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            base_url = configured_base_url or env_base_url or "https://chatgpt.com/backend-api"
            return OpenAICodexOAuthLLMProvider(
                base_url=base_url,
                api_key=api_key,
                deep_model=str(deep_model or "gpt-5.4"),
                fast_model=str(fast_model or "gpt-5.4-mini"),
                deep_reasoning_effort=str(deep_effort or "high"),
                fast_reasoning_effort=str(fast_effort or "none"),
            )

        raise RuntimeError(
            f"Unknown Codex LLM provider '{provider_id}'. "
            "Valid values: 'anthropic', 'openai'."
        )

    def installer_supported_providers(self) -> list:
        return ["anthropic", "openai"]

    def installer_default_models(self, provider: str) -> Optional[dict]:
        normalized = str(provider or "").strip().lower()
        if normalized == "anthropic":
            return {
                "deep": "claude-sonnet-4-5",
                "fast": "claude-haiku-4-5",
            }
        if normalized == "openai":
            return {
                "deep": "gpt-5.4",
                "fast": "gpt-5.4-mini",
                "deepEffort": "high",
                "fastEffort": "none",
            }
        return None

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

    def get_fast_provider_default(self) -> str:
        return "anthropic"

    def get_deep_provider_default(self) -> str:
        return "anthropic"
