"""Codex adapter for Quaid memory system."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from lib.adapter import QuaidAdapter, read_env_file
from lib.agent_notice import (
    dedupe_pending_notice_messages,
    format_pending_notice_relay,
    pending_notice_source,
    should_persist_pending_notice,
)
from lib.fail_policy import is_fail_hard_enabled
from lib.instance import instance_id, instance_slug_from_project_dir


def _trace_m15(event: str, **fields) -> None:
    try:
        from lib.m15_trace import trace_m15

        trace_m15(event, **fields)
    except Exception:
        pass


def _now_iso() -> str:
    return _now_datetime().isoformat()


def _now_datetime() -> datetime:
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _is_invalid_quaid_now_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and str(exc).startswith("Invalid QUAID_NOW=")


class CodexAdapter(QuaidAdapter):
    """Adapter for Codex CLI/app sessions."""

    ADAPTER_CONFIG = {
        "deferred_notice_relay": True,
        "deferred_notice_relay_immediate": True,
        "inject_tool_output_trace": True,
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
        "prompt_model_config_probe": True,
        "turn_scoped_provider_notices": True,
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
    _VISIBLE_MEMORY_CONTEXT_HEADER_RE = re.compile(
        r"^\s*\[Quaid Memory Context\]\s*$",
        flags=re.IGNORECASE,
    )
    _VISIBLE_MEMORY_CONTEXT_ROW_RE = re.compile(
        r"^\s*\d+\.\s*(?:\[[a-z0-9_:-]+(?:[^\]]*)\]){2,}\s+.+\s+\(relevance:\s*\d+(?:\.\d+)?\)\s*$",
        flags=re.IGNORECASE,
    )
    _VISIBLE_RECALL_OUTPUT_ROW_RE = re.compile(
        r"^\s*\[\d+(?:\.\d+)?\]\s+(?:\[[a-z0-9_:-]+(?:[^\]]*)\])+\s+\[memory\]\s+.+",
        flags=re.IGNORECASE,
    )
    _TRANSCRIPT_ROLE_LINE_RE = re.compile(
        r"^\s*(?:User|Assistant|Subagent/User|Subagent/Assistant):\s+",
        flags=re.IGNORECASE,
    )
    _ROLLOUT_SESSION_ID_RE = re.compile(
        r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$",
        flags=re.IGNORECASE,
    )
    _ROW_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d")

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
        def _is_deleted_misc_instance(instance_id: str) -> bool:
            try:
                from core.project_registry import is_misc_project_deleted

                return bool(is_misc_project_deleted(instance_id, quaid_home=self.quaid_home()))
            except Exception as exc:
                if is_fail_hard_enabled():
                    raise
                print(
                    "[adapter][WARN] Could not check deleted misc project state "
                    f"for {instance_id}: {exc}",
                    file=sys.stderr,
                )
                return False

        prefix = self.agent_id_prefix() + "-"
        current = self.instance_id()
        found = []
        try:
            home = self.quaid_home() / "instances"
            candidates = list(home.iterdir())
        except Exception as exc:
            if is_fail_hard_enabled():
                raise
            print(f"[adapter][WARN] Could not list Codex instances: {exc}", file=sys.stderr)
            candidates = []
        for d in candidates:
            if d.is_dir() and d.name.startswith(prefix) and not _is_deleted_misc_instance(d.name):
                found.append(d.name)
        found = sorted(found)
        if _is_deleted_misc_instance(current):
            return [item for item in found if item != current]
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
            timestamp = _now_iso()
            pending = self._pending_notifications_path()
            pending.parent.mkdir(parents=True, exist_ok=True)
            entry_payload = {"message": message, "ts": timestamp}
            source = pending_notice_source(message)
            if source:
                entry_payload["source"] = source
            with open(pending, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry_payload) + "\n")
            _trace_m15(
                "adapter.codex.notify.write",
                path=str(pending),
                force=force,
                message=message,
                size=pending.stat().st_size if pending.exists() else None,
            )
            return True
        except Exception as exc:
            if _is_invalid_quaid_now_error(exc) or is_fail_hard_enabled():
                raise
            _trace_m15("adapter.codex.notify.error", message=message, error=str(exc))
            print(f"[notify] Failed to queue Codex notification: {exc}", file=sys.stderr)
            return False

    def get_pending_context(self, max_age_seconds: int = 300) -> str:
        pending = self._pending_notifications_path()
        if not pending.is_file():
            _trace_m15("adapter.codex.pending.missing", path=str(pending))
            return ""
        now = _now_datetime()
        try:
            messages = []
            sticky_entries = {}
            total_lines = 0
            expired = 0
            malformed = 0
            with open(pending, "r", encoding="utf-8") as handle:
                for line in handle:
                    total_lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    ts = str(entry.get("ts") or "").strip()
                    if ts and max_age_seconds > 0:
                        entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if (now - entry_dt).total_seconds() > max_age_seconds:
                            expired += 1
                            continue
                    message = str(entry.get("message") or "").strip()
                    if message:
                        messages.append(message)
                        source = str(entry.get("source") or "").strip().lower() or pending_notice_source(message)
                        if should_persist_pending_notice(source):
                            sticky_entries[(source, message)] = {
                                "message": message,
                                "ts": ts or _now_iso(),
                                "source": source,
                            }
            if sticky_entries:
                with open(pending, "w", encoding="utf-8") as handle:
                    for entry in sticky_entries.values():
                        handle.write(json.dumps(entry) + "\n")
                unlinked = False
            else:
                pending.unlink(missing_ok=True)
                unlinked = True
            _trace_m15(
                "adapter.codex.pending.drain",
                path=str(pending),
                total_lines=total_lines,
                messages_count=len(messages),
                expired=expired,
                malformed=malformed,
                unlinked=unlinked,
                sticky_count=len(sticky_entries),
                messages=messages,
            )
        except Exception as exc:
            if _is_invalid_quaid_now_error(exc) or is_fail_hard_enabled():
                raise
            _trace_m15("adapter.codex.pending.error", path=str(pending), error=str(exc))
            print(f"[notify] Failed to drain Codex notifications: {exc}", file=sys.stderr)
            return ""
        messages = dedupe_pending_notice_messages(messages)
        if not messages:
            _trace_m15("adapter.codex.pending.empty_after_filter", path=str(pending))
            return ""
        _trace_m15(
            "adapter.codex.pending.context",
            path=str(pending),
            messages_count=len(messages),
            context_preview="\n".join(f"• {message}" for message in messages)[:1000],
        )
        return format_pending_notice_relay(messages)

    def get_last_channel(self, session_key: str = "") -> None:
        _ = session_key
        return None

    def _read_codex_auth_token_file(self) -> Optional[str]:
        path = self.auth_token_path()
        if path is None or not path.is_file():
            return None
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(f"Failed to read Codex auth token file {path}") from exc
            print(f"[adapter][FALLBACK] Failed to read Codex auth token file {path}: {exc}", file=sys.stderr)
            return None
        return token or None

    def _resolve_openai_api_key(self, api_key_env: str = "OPENAI_API_KEY") -> Optional[str]:
        configured_env = str(api_key_env or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
        if configured_env != "OPENAI_API_KEY":
            configured_key = os.environ.get(configured_env, "").strip()
            if configured_key:
                return configured_key
        return self.get_api_key("OPENAI_API_KEY")

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        if env_var_name == "ANTHROPIC_API_KEY":
            token = self.read_shared_auth_token(["anthropic_oauth", "anthropic_api"]) or self.read_auth_token()
            if token:
                return token
        elif env_var_name == "OPENAI_API_KEY":
            token = self.read_shared_auth_token(["codex_oauth", "openai_api"]) or self._read_codex_auth_token_file()
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

    def get_compatibility_context_files(self):
        path = Path(__file__).with_name("COMPATIBILITY.md")
        if not path.is_file():
            return {}
        return {
            str(path.resolve()): {
                "purpose": "Codex compatibility notes",
                "maxLines": 120,
            }
        }

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
        except OSError as exc:
            print(f"[adapter][WARN] Failed to write Codex last session id: {exc}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise

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
        # Session changed — the old session ended via /new or /clear.  The
        # prior session id comes from this instance's own state file, so allow
        # a just-rotated Codex rollout whose session_meta.cwd has not landed yet.
        #
        # Some live-test/production flows intentionally name a CDX instance
        # explicitly instead of using the cwd-derived slug. In that case the
        # rollout session_meta.cwd looks "foreign" to the ownership filter, but
        # this instance's cursor still records the exact transcript it touched on
        # the prior turn. Trust that instance-local cursor as the fallback source.
        transcript_path = (
            self._get_session_path_from_cursor(last_id)
            or self._get_session_path(last_id, allow_unclassified=True)
        )
        if transcript_path is None:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "[fail_hard] Codex session transition detected, but the prior "
                    f"session transcript could not be resolved: session_id={last_id}"
                )
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

    def _get_session_path(self, session_id: str, *, allow_unclassified: bool = False) -> Optional[Path]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        sessions_dir = self.get_sessions_dir()
        if sessions_dir is None:
            return None
        expected = self._current_instance_id_for_sessions()
        owned_matches: list[Path] = []
        unclassified_matches: list[Path] = []
        for path in sessions_dir.rglob(f"rollout-*{session_id}.jsonl"):
            actual = self._session_instance_id_from_path(path)
            if actual == expected:
                owned_matches.append(path)
            elif allow_unclassified and not actual:
                unclassified_matches.append(path)
        matches = owned_matches or unclassified_matches
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return matches[0] if matches else None

    def _get_session_path_from_cursor(self, session_id: str) -> Optional[Path]:
        """Return the transcript path this instance already recorded for a session."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        if not os.environ.get("QUAID_INSTANCE", "").strip():
            return None
        try:
            from core.extraction_daemon import read_cursor

            cursor = read_cursor(session_id)
        except Exception:
            if is_fail_hard_enabled():
                raise
            return None
        raw_path = str((cursor or {}).get("transcript_path") or "").strip()
        candidates: list[tuple[int, float, Path]] = []

        def add_candidate(raw: str) -> None:
            raw = str(raw or "").strip()
            if not raw:
                return
            path = Path(raw).expanduser()
            try:
                if not path.is_file():
                    return
                is_snapshot = 1 if "rolling-transcript-snapshots" in str(path) else 0
                candidates.append((is_snapshot, path.stat().st_mtime, path))
            except OSError:
                return

        add_candidate(raw_path)
        cursor_dir = self.data_dir() / "session-cursors"
        try:
            cursor_files = list(cursor_dir.glob("*.json")) if cursor_dir.is_dir() else []
        except OSError:
            cursor_files = []
        for cursor_file in cursor_files:
            try:
                data = json.loads(cursor_file.read_text(encoding="utf-8"))
            except Exception:
                if is_fail_hard_enabled():
                    raise
                continue
            if str(data.get("session_id") or "").strip() != session_id:
                continue
            add_candidate(str(data.get("transcript_path") or ""))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2] if candidates else None

    def get_session_path(self, session_id: str) -> Optional[Path]:
        return self._get_session_path(session_id, allow_unclassified=False)

    def _current_instance_id_for_sessions(self) -> str:
        try:
            return str(self.instance_id() or "").strip()
        except Exception as exc:
            name = self.get_instance_name()
            if name:
                return f"{self.agent_id_prefix()}-{name}"
            if is_fail_hard_enabled():
                raise
            print(f"[adapter][WARN] Could not resolve Codex instance id for sessions: {exc}", file=sys.stderr)
            return self.agent_id_prefix()

    def _session_meta_payload(self, path: Path) -> dict:
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
                    return payload
        except OSError:
            return {}
        return {}

    def _session_instance_id_from_path(self, path: Path) -> str:
        payload = self._session_meta_payload(path)
        cwd = str(payload.get("cwd") or "").strip()
        if not cwd:
            return ""
        slug = instance_slug_from_project_dir(cwd)
        return f"{self.agent_id_prefix()}-{slug}" if slug else ""

    def owns_session_path(self, path: Path, session_id: str = "") -> bool:
        """Return whether a global Codex rollout file belongs to this instance."""
        _ = session_id
        expected = self._current_instance_id_for_sessions()
        actual = self._session_instance_id_from_path(Path(path))
        if not actual:
            return False
        return actual == expected

    def filter_system_messages(self, text: str) -> bool:
        if (
            text.startswith("[notify]")
            or text.startswith("[quaid]")
            or text.startswith("<environment_context>")
        ):
            return True
        return False

    @classmethod
    def _starts_visible_memory_context(cls, line: str) -> bool:
        return bool(
            cls._VISIBLE_MEMORY_CONTEXT_HEADER_RE.match(line)
            or cls._VISIBLE_MEMORY_CONTEXT_ROW_RE.match(line)
            or cls._VISIBLE_RECALL_OUTPUT_ROW_RE.match(line)
        )

    @classmethod
    def _strip_visible_memory_context(cls, text: str) -> str:
        """Remove CDX-rendered Quaid recall context before session extraction.

        Codex can persist hook additionalContext in the visible transcript.  If
        that rendered recall output is extracted again, old evidence is promoted
        into current-dated source chunks.  Tagged blocks are stripped elsewhere;
        this catches the rendered inner rows when CDX drops or splits tags.
        """
        lines = str(text or "").splitlines()
        if not lines:
            return ""
        cleaned: list[str] = []
        skipping_context = False
        for line in lines:
            if skipping_context:
                if not line.strip():
                    skipping_context = False
                    continue
                if cls._TRANSCRIPT_ROLE_LINE_RE.match(line):
                    skipping_context = False
                else:
                    continue
            if cls._starts_visible_memory_context(line):
                skipping_context = True
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    def sanitize_transcript_text(self, text: str) -> str:
        # Base class strips <quaid_system_message> tag blocks.
        value = super().sanitize_transcript_text(text)
        if not value:
            return ""
        # Strip Codex-visible Quaid helper blocks before extraction parsing.
        value = self._QUAID_MEMORY_CONTEXT_RE.sub("", value)
        value = self._QUAID_NOTIFICATION_RE.sub("", value)
        value = self._strip_visible_memory_context(value)
        if not value:
            return ""
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
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
        return value

    @classmethod
    def _row_timestamp(cls, row: dict) -> str:
        value = str(row.get("timestamp") or "").strip()
        return value if cls._ROW_TIMESTAMP_RE.match(value) else ""

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
            for line_index, line in enumerate(handle):
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
                    session_source_type = ""
                    if isinstance(subagent, dict) and isinstance(subagent.get("thread_spawn"), dict):
                        session_source_type = "subagent"
                    continue
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                row_timestamp = self._row_timestamp(obj)

                if record_type == "event_msg":
                    payload_type = str(payload.get("type") or "").strip()
                    if payload_type == "user_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({
                                "role": "user",
                                "content": text,
                                "source_type": session_source_type,
                                "timestamp": row_timestamp,
                                "line_index": line_index,
                            })
                    elif payload_type == "agent_message":
                        text = str(payload.get("message") or "").strip()
                        if text:
                            messages.append({
                                "role": "assistant",
                                "content": text,
                                "source_type": session_source_type,
                                "timestamp": row_timestamp,
                                "line_index": line_index,
                            })
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
                        fallback_messages.append({
                            "role": role,
                            "content": text,
                            "source_type": session_source_type,
                            "timestamp": row_timestamp,
                            "line_index": line_index,
                        })

        if messages:
            selected = sorted(
                messages
                + [
                    message
                    for message in fallback_messages
                    # Event rows remain canonical for assistant output; fallback
                    # user rows cover CDX task turns whose event row has not
                    # landed in the current extraction window yet.
                    if message.get("role") == "user"
                ],
                key=lambda message: int(message.get("line_index", 0) or 0),
            )
        else:
            selected = fallback_messages
        deduped = []
        last_pair = None
        for message in selected:
            pair = (message.get("role"), message.get("content"))
            if pair == last_pair:
                continue
            deduped.append(message)
            last_pair = pair
        return self._build_timestamped_transcript(deduped)

    def _build_timestamped_transcript(self, messages: list[dict]) -> str:
        parts = []
        for message in messages:
            rendered = self.build_transcript([message]).strip()
            if not rendered:
                continue
            timestamp = str(message.get("timestamp") or "").strip()
            parts.append(f"[{timestamp}] {rendered}" if timestamp else rendered)
        return "\n\n".join(parts)

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
            if not self.owns_session_path(path):
                continue
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

    def _has_provider_credential(self, provider: str, api_key_env: str = "OPENAI_API_KEY") -> bool:
        normalized = str(provider or "").strip().lower()
        if normalized == "anthropic":
            return bool(
                self.read_shared_auth_token(["anthropic_oauth", "anthropic_api"])
                or os.environ.get("ANTHROPIC_API_KEY", "").strip()
            )
        if normalized in ("openai", "openai-compatible"):
            configured_env = str(api_key_env or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
            return bool(
                self.read_shared_auth_token(["codex_oauth", "openai_api"])
                or self._read_codex_auth_token_file()
                or os.environ.get("OPENAI_OAUTH_TOKEN", "").strip()
                or os.environ.get(configured_env, "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip()
            )
        return False

    def _resolve_central_provider(
        self,
        configured: str,
        deep_model: str,
        fast_model: str,
        api_key_env: str = "OPENAI_API_KEY",
    ) -> str:
        provider = str(configured or "").strip().lower()
        if provider and provider != "default":
            if self._has_provider_credential(provider, api_key_env=api_key_env):
                return provider
            detected = self._detect_shared_primary_provider()
            if detected and detected != provider:
                print(
                    "[adapter][provider] configured provider "
                    f"{provider!r} has no credential; using single available shared "
                    f"{detected!r} credential.",
                    file=sys.stderr,
                )
                return detected
            return provider

        detected = self._detect_shared_primary_provider()
        if detected:
            return detected

        inferred = self._infer_provider_from_models(deep_model, fast_model)
        if inferred:
            return inferred

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

    def _resolve_model_for_provider(self, cfg, provider_id: str, configured_model: str, tier: str) -> str:
        provider = str(provider_id or "").strip().lower()
        model = str(configured_model or "").strip()
        inferred = self._infer_provider_from_models(model)
        if model and model != "default" and (not inferred or inferred == provider):
            return model

        class_attr = "deep_reasoning_model_classes" if tier == "deep" else "fast_reasoning_model_classes"
        class_map = getattr(cfg.models, class_attr, {}) or {}
        if isinstance(class_map, dict):
            mapped = str(class_map.get(provider) or "").strip()
            if mapped:
                return mapped

        defaults = self.installer_default_models(provider) or {}
        default_key = "deep" if tier == "deep" else "fast"
        return str(defaults.get(default_key) or model or "default")

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from config import get_config
        from lib.providers import AnthropicLLMProvider, OpenAICodexOAuthLLMProvider

        cfg = get_config()
        deep_model = getattr(cfg.models, "deep_reasoning", "claude-sonnet-4-5")
        fast_model = getattr(cfg.models, "fast_reasoning", "claude-haiku-4-5")
        deep_effort = getattr(cfg.models, "deep_reasoning_effort", "high")
        fast_effort = getattr(cfg.models, "fast_reasoning_effort", "none")
        api_key_env = str(getattr(cfg.models, "api_key_env", "") or "OPENAI_API_KEY")
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
            api_key_env=api_key_env,
        )
        provider_id = str(provider_id or "").strip().lower()
        resolved_deep = self._resolve_model_for_provider(cfg, provider_id, str(deep_model or ""), "deep")
        resolved_fast = self._resolve_model_for_provider(cfg, provider_id, str(fast_model or ""), "fast")

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
                deep_model=str(resolved_deep or "claude-sonnet-4-5"),
                fast_model=str(resolved_fast or "claude-haiku-4-5"),
            )

        if provider_id in ("openai", "openai-compatible"):
            api_key = self._resolve_openai_api_key(api_key_env)
            if not api_key:
                raise RuntimeError(
                    "LLM provider is 'openai' but no Codex/OpenAI credential was found. "
                    "Write a Codex OAuth token or OpenAI API key to "
                    "QUAID_HOME/shared/auth/credentials.json via "
                    "'quaid auth refresh --kind codex_oauth|openai_api', write "
                    "QUAID_HOME/adaptors/codex/.auth-token, or set "
                    f"OPENAI_OAUTH_TOKEN / {api_key_env}."
                )
            configured_base_url = str(getattr(cfg.models, "base_url", "") or "").strip()
            env_base_url = str(os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            base_url = configured_base_url or env_base_url or "https://chatgpt.com/backend-api"
            return OpenAICodexOAuthLLMProvider(
                base_url=base_url,
                api_key=api_key,
                deep_model=str(resolved_deep or "gpt-5.4"),
                fast_model=str(resolved_fast or "gpt-5.4-mini"),
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
