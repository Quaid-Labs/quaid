"""Claude Code adapter for Quaid memory system.

Integrates Quaid as a lifecycle-aware memory layer for Claude Code sessions.
Uses CLI subcommands via the existing Bash tool + hooks for automation.

- Home dir: QUAID_HOME env or ~/.quaid (visible files under ~/quaid/)
- Notifications: deferred via pending file → surfaced in next UserPromptSubmit
- Credentials: env var → ~/.claude/.credentials.json OAuth token
- Sessions: ~/.claude/projects/ (Claude Code transcripts)
- Filtering: <system-reminder> tags, tool blocks, thinking blocks
- LLM: OAuth direct API (fast) or claude -p CLI (fallback)
"""

import ast
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
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
from lib.instance import _legacy_instance_slug_from_project_dir, instance_slug_from_project_dir

logger = logging.getLogger(__name__)


def _trace_m15(event: str, **fields) -> None:
    try:
        from lib.m15_trace import trace_m15

        trace_m15(event, **fields)
    except Exception:
        logger.debug("M15 trace write failed for Claude Code adapter event %s", event, exc_info=True)


class ClaudeCodeAdapter(QuaidAdapter):
    ADAPTER_CONFIG = {
        "deferred_notice_relay": True,
        "session_cwd_path_template": "{cwd_encoded}/{session_id}.jsonl",
        "platform_config_scope": "claude-code",
        "prompt_model_config_probe": True,
    }

    _QUAID_MEMORY_CONTEXT_RE = re.compile(
        r"<quaid_memory_context>.*?</quaid_memory_context>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _QUAID_NOTIFICATION_RE = re.compile(
        r"<quaid_notification>.*?</quaid_notification>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _LOCAL_COMMAND_CAVEAT_RE = re.compile(
        r"<local-command-caveat>.*?</local-command-caveat>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _LOCAL_COMMAND_STDOUT_RE = re.compile(
        r"<local-command-stdout>.*?</local-command-stdout>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _LOCAL_COMMAND_METADATA_RE = re.compile(
        r"<command-name>.*?</command-name>\s*"
        r"<command-message>.*?</command-message>\s*"
        r"<command-args>.*?</command-args>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _LOCAL_COMMAND_NAME_RE = re.compile(
        r"<command-name>\s*(/(?:new|clear|reset|restart|compact))\b.*?</command-name>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    _SESSION_ID_FROM_TRANSCRIPT_RE = re.compile(
        r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,})",
        flags=re.IGNORECASE,
    )
    _ROW_TIMESTAMP_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
    )
    """Adapter for running Quaid inside Claude Code sessions."""

    def __init__(self, home: Optional[Path] = None):
        self._home = home

    def quaid_home(self) -> Path:
        """Root directory containing all Quaid instances (QUAID_HOME)."""
        if self._home is not None:
            return self._home
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / ".quaid"

    @classmethod
    def installer_adapter_id(cls) -> str:
        return "claude-code"

    @classmethod
    def installer_cli_candidates(cls) -> list[str]:
        return ["claude"]

    def get_instance_name(self) -> str:
        """Derive a stable instance name from the CC project root.

        Uses CLAUDE_PROJECT_DIR env var which CC injects for all hooks and
        Bash tool calls — this is the project root regardless of the shell's
        current working directory.

        Delegates to the module-level instance_slug_from_project_dir() so the
        derivation logic is defined once and can be called without instantiation
        (used by lib.adapter._adapter_config_paths() for early config resolution).
        """
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
        return instance_slug_from_project_dir(project_dir)

    def _pending_notifications_path(self) -> Path:
        """Path to the pending notifications file for deferred delivery."""
        return self.data_dir() / "cc-pending-notifications.jsonl"

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False, force: bool = False) -> bool:
        """Write notification to pending file for next UserPromptSubmit pickup.

        CC has no in-terminal notification channel, so notifications are
        deferred and surfaced via additionalContext on the next hook_inject().
        """
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS") and not force:
            return True
        if dry_run:
            print(f"[notify] (dry-run) {message}", file=sys.stderr)
            return True

        timestamp = _now_iso()
        try:
            pending = self._pending_notifications_path()
            pending.parent.mkdir(parents=True, exist_ok=True)
            entry_payload = {"message": message, "ts": timestamp}
            source = pending_notice_source(message)
            if source:
                entry_payload["source"] = source
            entry = json.dumps(entry_payload)
            with open(pending, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            _trace_m15(
                "adapter.claude_code.notify.write",
                path=str(pending),
                force=force,
                message=message,
                size=pending.stat().st_size if pending.exists() else None,
            )
            return True
        except Exception as e:
            _trace_m15("adapter.claude_code.notify.error", message=message, error=str(e))
            print(f"[notify] Failed to queue notification: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return False

    def get_pending_context(self, max_age_seconds: int = 300) -> str:
        """Drain pending notifications and return formatted context for injection.

        CC has no in-terminal notification channel, so notifications are
        deferred to a file and surfaced via additionalContext on the next
        UserPromptSubmit hook. Returns formatted context string with relay
        instructions, or empty string if nothing pending.
        """
        pending = self._pending_notifications_path()
        if not pending.is_file():
            _trace_m15("adapter.claude_code.pending.missing", path=str(pending))
            return ""

        messages = []
        sticky_entries = {}
        now = _now_datetime()
        try:
            total_lines = 0
            expired = 0
            malformed = 0
            with open(pending, "r", encoding="utf-8") as f:
                for line in f:
                    total_lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts = entry.get("ts", "")
                        if ts and max_age_seconds > 0:
                            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - entry_dt).total_seconds() > max_age_seconds:
                                expired += 1
                                continue
                        message = str(entry.get("message") or "").strip()
                        source = str(entry.get("source") or "").strip().lower() or pending_notice_source(message)
                        if message:
                            messages.append(message)
                            if should_persist_pending_notice(source):
                                sticky_entries[(source, message)] = {
                                    "message": message,
                                    "ts": str(entry.get("ts") or "").strip() or _now_iso(),
                                    "source": source,
                                }
                    except (json.JSONDecodeError, ValueError):
                        malformed += 1
                        continue
        except Exception as e:
            _trace_m15("adapter.claude_code.pending.read_error", path=str(pending), error=str(e))
            print(f"[notify] Failed to read pending notifications: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return ""

        try:
            if sticky_entries:
                with open(pending, "w", encoding="utf-8") as f:
                    for entry in sticky_entries.values():
                        f.write(json.dumps(entry) + "\n")
                unlinked = False
            else:
                pending.unlink(missing_ok=True)
                unlinked = True
            _trace_m15(
                "adapter.claude_code.pending.drain",
                path=str(pending),
                total_lines=total_lines,
                messages_count=len([m for m in messages if m]),
                expired=expired,
                malformed=malformed,
                unlinked=unlinked,
                sticky_count=len(sticky_entries),
                messages=[m for m in messages if m],
            )
        except Exception as e:
            _trace_m15("adapter.claude_code.pending.cleanup_error", path=str(pending), error=str(e))
            print(f"[notify] Failed to clean up pending notifications: {e}", file=sys.stderr)
            if is_fail_hard_enabled():
                raise
            return ""

        notes = dedupe_pending_notice_messages(messages)
        if not notes:
            _trace_m15("adapter.claude_code.pending.empty_after_filter", path=str(pending))
            return ""

        _trace_m15(
            "adapter.claude_code.pending.context",
            path=str(pending),
            messages_count=len(notes),
            context_preview="\n".join(f"• {n}" for n in notes)[:1000],
        )
        return format_pending_notice_relay(notes)

    def get_last_channel(self, session_key: str = "") -> None:
        return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key

        if is_fail_hard_enabled():
            raise RuntimeError(
                f"[fail_hard] {env_var_name} is required but not set in the environment."
            )

        # Fallback: .env in quaid home
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

    def adapter_id(self) -> str:
        return "claude-code"

    def get_instance_type(self) -> str:
        return "folder"

    def agent_id_prefix(self) -> str:
        """CC adapter prefix for building instance IDs (e.g. "claude-code").

        QUAID_INSTANCE is the current instance's full ID
        ("claude-code-<project-slug>" for path-derived isolation).
        """
        return self.adapter_id()  # "claude-code"

    def list_agent_instance_ids(self) -> list:
        """Return all known CC instance IDs under QUAID_HOME.

        Scans QUAID_HOME for subdirectories whose name starts with the CC
        adapter prefix (e.g. "claude-code-"), mirroring how OC reads its
        agents.list from openclaw.json. Each CC instance silo is created by
        make_instance and has its QUAID_INSTANCE packed into the project's
        .claude/settings.json.

        Includes the current instance (from QUAID_INSTANCE env var) even if
        its silo does not yet exist on disk, unless its misc project was
        explicitly deleted.
        """
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

        prefix = self.agent_id_prefix() + "-"  # "claude-code-"
        current = self.instance_id()
        found = []
        try:
            home = self.quaid_home() / "instances"
            candidates = list(home.iterdir())
        except Exception as exc:
            if is_fail_hard_enabled():
                raise
            print(f"[adapter][WARN] Could not list Claude Code instances: {exc}", file=sys.stderr)
            candidates = []
        for d in candidates:
            if d.is_dir() and d.name.startswith(prefix) and not _is_deleted_misc_instance(d.name):
                found.append(d.name)
        found = sorted(found)
        # Ensure current instance is always present and listed first
        if _is_deleted_misc_instance(current):
            found = [x for x in found if x != current]
        elif current in found:
            found = [current] + [x for x in found if x != current]
        else:
            found = [current] + found
        return found

    def get_instance_manager(self):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        return ClaudeCodeInstanceManager(self)

    def get_cli_namespace(self) -> str:
        return "claudecode"

    def get_cli_commands(self) -> dict:
        return {
            "make_instance": self._cli_make_instance,
        }

    def _cli_make_instance(self, args: list) -> None:
        """quaid claudecode make_instance <path> <name> [--token <token>] [--deep-model <id>] [--fast-model <id>] [--dry-run]"""
        if len(args) < 2:
            print("Usage: quaid claudecode make_instance <project-path> <name> [options]")
            print("  project-path   Path to the Claude Code project root")
            print("  name           Short label for the instance (e.g. 'myapp')")
            print("  --token        API-scoped OAuth token for daemon LLM calls")
            print("  --deep-model   Deep reasoning model ID (default: claude-sonnet-4-6)")
            print("  --fast-model   Fast reasoning model ID (default: claude-haiku-4-5)")
            print("  --dry-run      Preview without making changes")
            return
        project_path, name = args[0], args[1]
        dry_run = "--dry-run" in args
        token = ""
        deep_model = ""
        fast_model = ""
        for i, a in enumerate(args):
            if a == "--token" and i + 1 < len(args):
                token = args[i + 1]
            elif a == "--deep-model" and i + 1 < len(args):
                deep_model = args[i + 1]
            elif a == "--fast-model" and i + 1 < len(args):
                fast_model = args[i + 1]

        mgr = self.get_instance_manager()
        label = mgr.canonical_project_label(Path(project_path).resolve(), name)
        instance_id = mgr.resolve_instance_id(label)
        _deep = deep_model or mgr.DEFAULT_DEEP_MODEL
        _fast = fast_model or mgr.DEFAULT_FAST_MODEL

        if dry_run:
            print(f"[dry-run] Would create silo: {mgr.adapter.quaid_home() / 'instances' / instance_id}")
            print(f"[dry-run] Would write QUAID_INSTANCE={instance_id} to {project_path}/.claude/settings.json")
            print(f"[dry-run] Would write models: deep={_deep} fast={_fast}")
            if token:
                print(f"[dry-run] Would write auth token to adapter config dir")
            return

        silo_root = mgr.make_instance(project_path, label, token=token,
                                      deep_model=deep_model, fast_model=fast_model)
        print(f"Created silo: {silo_root}")
        print(f"Instance ID:  {instance_id}")
        print(f"Wrote QUAID_INSTANCE={instance_id} to {project_path}/.claude/settings.json")

    def get_cli_tools_snippet(self) -> str:
        prefix = self.agent_id_prefix()
        return (
            "### Claude Code Instance Commands (`quaid claudecode`)\n\n"
            "- `quaid claudecode make_instance <path> <name>` — Create a Quaid instance "
            "for a Claude Code project. Initializes a silo at "
            f"`~/quaid/instances/{prefix}-<name>/` and writes `QUAID_INSTANCE={prefix}-<name>` "
            "into `<path>/.claude/settings.json`. Use this to give a CC project its own "
            "isolated memory store.\n"
            "  - `--dry-run` — Preview without making changes\n"
        )

    def get_host_info(self):
        """Detect Claude Code platform version and binary path."""
        import shutil
        import subprocess
        from core.compatibility import HostInfo

        # Find the claude binary
        binary = shutil.which("claude")
        if not binary:
            for candidate in ["/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
                if Path(candidate).exists():
                    binary = candidate
                    break

        version = "unknown"
        if binary:
            try:
                result = subprocess.run(
                    [binary, "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Output might be "claude v2.1.72" or just "2.1.72"
                    version = result.stdout.strip().split()[-1].lstrip("v")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        return HostInfo(
            platform="claude-code",
            version=version,
            binary_path=binary,
        )

    def auth_token_path(self) -> Optional[Path]:
        return self.quaid_home() / "adaptors" / "claude-code" / ".auth-token"

    def auth_registry_kinds(self) -> list[str]:
        return ["anthropic_oauth", "anthropic_api"]

    def get_base_context_files(self):
        """CLAUDE.md is CC's native context file — janitor can slim it."""
        # CLAUDE.md lives in the user's project cwd
        candidates = [
            Path.cwd() / "CLAUDE.md",
            Path.cwd() / ".claude" / "CLAUDE.md",
        ]
        files = {}
        for p in candidates:
            if p.is_file():
                files[str(p.resolve())] = {
                    "purpose": "Claude Code project instructions and rules",
                    "maxLines": 500,
                }
                break  # Only the first match
        return files

    def get_compatibility_context_files(self):
        path = Path(__file__).with_name("COMPATIBILITY.md")
        if not path.is_file():
            return {}
        return {
            str(path.resolve()): {
                "purpose": "Claude Code compatibility notes",
                "maxLines": 100,
            }
        }

    @staticmethod
    def _normalize_claude_project_dir_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")

    def _bound_project_dir_for_current_instance(self) -> str:
        try:
            instance_id = str(self.instance_id() or "").strip()
        except Exception as exc:
            if is_fail_hard_enabled():
                raise
            print(
                f"[adapter][WARN] Could not resolve current Claude Code instance: {exc}",
                file=sys.stderr,
            )
            instance_id = os.environ.get("QUAID_INSTANCE", "").strip()
        if not instance_id:
            return ""

        bindings_dir = self.quaid_home() / "shared" / "instance-bindings" / self.adapter_id()
        try:
            binding_paths = sorted(
                bindings_dir.glob("*.json"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            )
        except (OSError, RuntimeError):
            return ""

        explicit_matches: list[str] = []
        prefix = f"{self.agent_id_prefix()}-"
        for binding_path in binding_paths:
            try:
                data = json.loads(binding_path.read_text(encoding="utf-8"))
                if str(data.get("instance") or "").strip() != instance_id:
                    continue
                adapter_id = str(data.get("adapter") or "").strip()
                if adapter_id and adapter_id != self.adapter_id():
                    continue
                project_dir = str(data.get("project_dir") or "").strip()
                if not project_dir:
                    continue
                resolved_project = Path(project_dir).expanduser().resolve()
                new_slug = instance_slug_from_project_dir(str(resolved_project))
                legacy_slug = _legacy_instance_slug_from_project_dir(str(resolved_project))
                if binding_path.name not in {f"{new_slug}.json", f"{legacy_slug}.json"}:
                    continue
                if instance_id in {f"{prefix}{new_slug}", f"{prefix}{legacy_slug}"}:
                    return str(resolved_project)
                explicit_matches.append(str(resolved_project))
            except Exception as exc:
                logger.warning("failed resolving Claude Code project binding %s: %s", binding_path, exc)
                if is_fail_hard_enabled():
                    raise
                continue

        unique_matches = list(dict.fromkeys(explicit_matches))
        return unique_matches[0] if len(unique_matches) == 1 else ""

    def _current_project_session_slug(self) -> str:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
        if project_dir:
            return _legacy_instance_slug_from_project_dir(project_dir)
        bound_project_dir = self._bound_project_dir_for_current_instance()
        if bound_project_dir:
            return _legacy_instance_slug_from_project_dir(bound_project_dir)
        instance_id = ""
        try:
            instance_id = str(self.instance_id() or "").strip()
        except Exception as exc:
            logger.warning("failed resolving Claude Code instance for session slug: %s", exc)
            if is_fail_hard_enabled():
                raise
            instance_id = os.environ.get("QUAID_INSTANCE", "").strip()
        prefix = f"{self.agent_id_prefix()}-"
        if instance_id.startswith(prefix):
            return instance_id[len(prefix):].strip()
        return ""

    def _project_sessions_root(self) -> Path:
        return Path.home() / ".claude" / "projects"

    def _session_project_slug_from_path(self, path: Path) -> str:
        try:
            root = self._project_sessions_root().expanduser().resolve()
            candidate = Path(path).expanduser().resolve()
            rel = candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return ""
        parts = rel.parts
        if len(parts) < 2:
            return ""
        return self._normalize_claude_project_dir_name(parts[0])

    def get_sessions_dir(self) -> Optional[Path]:
        root = self._project_sessions_root()
        if not root.is_dir():
            return None
        return root

    def get_discovery_sessions_dir(self) -> Optional[Path]:
        """Directory the daemon may scan for this instance's CC transcripts."""
        root = self.get_sessions_dir()
        if root is None:
            return None
        expected_slug = self._current_project_session_slug()
        if not expected_slug:
            return root
        matches = [
            candidate
            for candidate in root.iterdir()
            if candidate.is_dir()
            and self._normalize_claude_project_dir_name(candidate.name) == expected_slug
        ]
        if matches:
            matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return matches[0]
        return root / f"-{expected_slug}"

    def owns_session_path(self, path: Path, session_id: str = "") -> bool:
        """Return True only for transcripts under this CC project's session dir."""
        transcript_path = Path(path)
        if not transcript_path.name.endswith(".jsonl"):
            return False
        sid = str(session_id or "").strip()
        if sid and transcript_path.stem != sid and sid not in transcript_path.stem:
            return False
        expected_slug = self._current_project_session_slug()
        if not expected_slug:
            return True
        return self._session_project_slug_from_path(transcript_path) == expected_slug

    def get_session_path(self, session_id: str) -> Optional[Path]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        sessions_dir = self.get_sessions_dir()
        if sessions_dir is None:
            return None
        direct = sessions_dir / f"{session_id}.jsonl"
        if direct.is_file() and self.owns_session_path(direct, session_id=session_id):
            return direct
        matches = [
            path
            for path in sessions_dir.rglob(f"*{session_id}*.jsonl")
            if self.owns_session_path(path, session_id=session_id)
        ]
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return matches[0] if matches else None

    def _session_transition_state_path(self) -> Path:
        return self.data_dir() / "claude-code-last-session.json"

    def _read_session_transition_state(self) -> dict:
        path = self._session_transition_state_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_session_transition_state(self, session_id: str, transcript_path: str = "") -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        path = self._session_transition_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "transcript_path": str(transcript_path or "").strip(),
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )

    def _extract_hook_session_id(self, hook_input) -> str:
        if not isinstance(hook_input, dict):
            return ""
        for key in ("session_id", "sessionId", "thread_id", "threadId", "conversation_id"):
            value = str(hook_input.get(key) or "").strip()
            if value:
                return value
        transcript_path = str(hook_input.get("transcript_path") or "").strip()
        if transcript_path:
            match = self._SESSION_ID_FROM_TRANSCRIPT_RE.search(Path(transcript_path).name)
            if match:
                return match.group(1)
        return ""

    def _transition_command_for_hook(self, hook_input: dict) -> str:
        command = self._scan_lifecycle_candidates(hook_input)
        if command:
            return command
        source = " ".join(
            str(hook_input.get(key) or "")
            for key in ("source", "reason", "hook_event_name", "hookEventName")
        ).lower()
        if "clear" in source or "reset" in source:
            return "/clear"
        return "/new"

    def check_session_transition(self, hook_input: dict) -> Optional[dict]:
        """Detect Claude Code transcript rotation, including /clear and /reset.

        Claude Code can handle /clear and /reset without sending a
        UserPromptSubmit hook for the old transcript. It starts a new transcript
        with SessionStart:clear instead, so SessionStart must flush the previous
        transcript that accumulated rolling state.
        """
        if not isinstance(hook_input, dict):
            return None
        current_id = self._extract_hook_session_id(hook_input)
        if not current_id:
            return None

        current_tx = str(hook_input.get("transcript_path") or "").strip()
        last = self._read_session_transition_state()
        last_id = str(last.get("session_id") or "").strip()
        last_tx = str(last.get("transcript_path") or "").strip()
        self._write_session_transition_state(current_id, current_tx)

        if not last_id or last_id == current_id:
            return None
        if not last_tx or not Path(last_tx).is_file():
            resolved = self.get_session_path(last_id)
            last_tx = str(resolved) if resolved else ""
        if not last_tx:
            return None
        return {
            "ended_session_id": last_id,
            "ended_transcript_path": last_tx,
            "signal_type": "session_end",
            "meta": {
                "source": "session_transition",
                "command": self._transition_command_for_hook(hook_input),
                "reason": "session_start_transition",
            },
        }

    @classmethod
    def _extract_lifecycle_command(cls, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        local_command = cls._LOCAL_COMMAND_NAME_RE.search(value)
        if local_command:
            return local_command.group(1).lower()
        if not value.startswith("/"):
            return ""
        command = value.split()[0].lower()
        if command in ("/new", "/clear", "/reset", "/restart", "/compact"):
            return command
        return ""

    @classmethod
    def _scan_lifecycle_candidates(cls, value) -> str:
        if isinstance(value, str):
            return cls._extract_lifecycle_command(value)
        if isinstance(value, list):
            for item in value:
                cmd = cls._scan_lifecycle_candidates(item)
                if cmd:
                    return cmd
            return ""
        if not isinstance(value, dict):
            return ""
        for key in ("command", "prompt", "message", "input", "last_user_message", "text"):
            cmd = cls._scan_lifecycle_candidates(value.get(key))
            if cmd:
                return cmd
        payload = value.get("payload")
        if isinstance(payload, dict):
            cmd = cls._scan_lifecycle_candidates(payload)
            if cmd:
                return cmd
        content = value.get("content")
        if isinstance(content, (list, dict, str)):
            cmd = cls._scan_lifecycle_candidates(content)
            if cmd:
                return cmd
        return ""

    def resolve_prompt_submit_signal(self, hook_input):
        command = self._scan_lifecycle_candidates(hook_input)
        if not command:
            return None
        signal_type = "compaction" if command == "/compact" else "session_end"
        return {
            "signal_type": signal_type,
            "meta": {
                "source": "hook_inject",
                "command": command,
                "reason": f"command:{command.lstrip('/')}",
            },
        }

    def sanitize_transcript_text(self, text: str) -> str:
        value = super().sanitize_transcript_text(text)
        if not value:
            return ""
        value = self._QUAID_MEMORY_CONTEXT_RE.sub("", value)
        value = self._QUAID_NOTIFICATION_RE.sub("", value)
        value = self._LOCAL_COMMAND_CAVEAT_RE.sub("", value)
        value = self._LOCAL_COMMAND_METADATA_RE.sub("", value)
        value = self._LOCAL_COMMAND_STDOUT_RE.sub("", value)
        return value.strip()

    def filter_system_messages(self, text: str) -> bool:
        if "<system-reminder>" in text:
            return True
        if text.startswith("[quaid]") or text.startswith("[notify]"):
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

    def parse_session_jsonl(self, path: Path) -> str:
        """Parse Claude Code session JSONL into a normalized transcript.

        Claude Code JSONL format:
            {"type": "user", "message": {"role": "user", "content": [...]}}
            {"type": "assistant", "message": {"role": "assistant", "content": [...]}}

        Skips: file-history-snapshot, progress, thinking records, tool_use/tool_result blocks.
        Extracts text from content arrays, keeping only text blocks.
        """
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

                record_type = obj.get("type", "")

                # Skip non-message records
                if record_type in (
                    "file-history-snapshot", "progress", "system",
                    "result", "summary",
                ):
                    continue
                row_timestamp = self._row_timestamp(obj)

                # Handle wrapped message format
                # CC may encode message as JSON or Python repr string instead
                # of an inline dict; parse both before falling back to the row.
                if "message" in obj:
                    raw_msg = obj["message"]
                    if isinstance(raw_msg, dict):
                        msg = raw_msg
                    elif isinstance(raw_msg, str):
                        try:
                            parsed = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            try:
                                parsed = ast.literal_eval(raw_msg)
                            except (ValueError, SyntaxError):
                                parsed = None
                        msg = parsed if isinstance(parsed, dict) else obj
                    else:
                        msg = obj
                else:
                    msg = obj
                if isinstance(msg, dict):
                    row_timestamp = row_timestamp or self._row_timestamp(msg)

                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                content = msg.get("content", "")

                # Extract text from content arrays
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type", "")
                        # Skip tool blocks and thinking
                        if block_type in ("tool_use", "tool_result", "thinking"):
                            continue
                        if block_type == "text":
                            text = block.get("text", "")
                            if text:
                                text_parts.append(text)
                    content = "\n".join(text_parts)
                elif not isinstance(content, str):
                    continue

                content = content.strip()
                if not content:
                    continue

                source_type = ""
                if bool(obj.get("isSidechain")) or str(obj.get("agentId", "")).strip():
                    source_type = "subagent"

                messages.append({
                    "role": role,
                    "content": content,
                    "source_type": source_type,
                    "timestamp": row_timestamp,
                })

        return self._build_timestamped_transcript(messages)

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from adaptors.claude_code.providers import ClaudeCodeOAuthLLMProvider
        try:
            from config import get_config
            cfg = get_config()
            deep = cfg.models.deep_reasoning
            fast = cfg.models.fast_reasoning
        except Exception as exc:
            logger.warning("failed to load Claude Code model config: %s", exc)
            if is_fail_hard_enabled():
                raise
            deep = None
            fast = None
        return ClaudeCodeOAuthLLMProvider(deep_model=deep, fast_model=fast)

    def installer_supported_providers(self) -> list:
        # CC adapter is OAuth Anthropic-backed in current shipping flow.
        return ["anthropic"]

    def installer_default_models(self, provider: str) -> Optional[dict]:
        if str(provider or "").strip().lower() != "anthropic":
            return None
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        return {
            "deep": ClaudeCodeInstanceManager.DEFAULT_DEEP_MODEL,
            "fast": ClaudeCodeInstanceManager.DEFAULT_FAST_MODEL,
        }

    def get_fast_provider_default(self) -> str:
        return "anthropic"

    def get_deep_provider_default(self) -> str:
        return "anthropic"


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
