"""Claude Code adapter for Quaid memory system.

Integrates Quaid as a lifecycle-aware memory layer for Claude Code sessions.
Uses CLI subcommands via the existing Bash tool + hooks for automation.

- Home dir: QUAID_HOME env or ~/quaid/
- Notifications: deferred via pending file → surfaced in next UserPromptSubmit
- Credentials: env var → ~/.claude/.credentials.json OAuth token
- Sessions: ~/.claude/projects/ (Claude Code transcripts)
- Filtering: <system-reminder> tags, tool blocks, thinking blocks
- LLM: OAuth direct API (fast) or claude -p CLI (fallback)
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from lib.adapter import QuaidAdapter, read_env_file
from lib.fail_policy import is_fail_hard_enabled


class ClaudeCodeAdapter(QuaidAdapter):
    """Adapter for running Quaid inside Claude Code sessions."""

    def __init__(self, home: Optional[Path] = None):
        self._home = home

    def quaid_home(self) -> Path:
        if self._home is not None:
            return self._home
        env = os.environ.get("QUAID_HOME", "").strip()
        return Path(env).resolve() if env else Path.home() / "quaid"

    def _pending_notifications_path(self) -> Path:
        """Path to the pending notifications file for deferred delivery."""
        return self.quaid_home() / "data" / "cc-pending-notifications.jsonl"

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

        try:
            pending = self._pending_notifications_path()
            pending.parent.mkdir(parents=True, exist_ok=True)
            entry = json.dumps({"message": message, "ts": _now_iso()})
            with open(pending, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            return True
        except Exception as e:
            print(f"[notify] Failed to queue notification: {e}", file=sys.stderr)
            return False

    def get_pending_context(self, max_age_seconds: int = 3600) -> str:
        """Drain pending notifications and return formatted context for injection.

        CC has no in-terminal notification channel, so notifications are
        deferred to a file and surfaced via additionalContext on the next
        UserPromptSubmit hook. Returns formatted context string with relay
        instructions, or empty string if nothing pending.
        """
        pending = self._pending_notifications_path()
        if not pending.is_file():
            return ""

        messages = []
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            with open(pending, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts = entry.get("ts", "")
                        if ts and max_age_seconds > 0:
                            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - entry_dt).total_seconds() > max_age_seconds:
                                continue
                        messages.append(entry.get("message", ""))
                    except (json.JSONDecodeError, ValueError):
                        continue
            pending.unlink(missing_ok=True)
        except Exception as e:
            print(f"[notify] Failed to drain pending notifications: {e}", file=sys.stderr)

        notes = [m for m in messages if m]
        if not notes:
            return ""

        body = "\n".join(f"• {n}" for n in notes)
        return (
            "Print <notification> verbatim before your response, then print ---\n\n"
            f"<notification>\n{body}\n</notification>"
        )

    def get_last_channel(self, session_key: str = "") -> None:
        return None

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        key = os.environ.get(env_var_name, "").strip()
        if key:
            return key

        if is_fail_hard_enabled():
            return None

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
        return self.quaid_home() / "config" / "adapters" / "claude-code" / ".auth-token"

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

    def get_sessions_dir(self) -> Optional[Path]:
        d = Path.home() / ".claude" / "projects"
        return d if d.is_dir() else None

    def filter_system_messages(self, text: str) -> bool:
        if "<system-reminder>" in text:
            return True
        if text.startswith("[quaid]") or text.startswith("[notify]"):
            return True
        return False

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

                # Handle wrapped message format
                if "message" in obj and isinstance(obj["message"], dict):
                    msg = obj["message"]
                else:
                    msg = obj

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
                    content = " ".join(text_parts)
                elif not isinstance(content, str):
                    continue

                content = content.strip()
                if not content:
                    continue

                messages.append({"role": role, "content": content})

        return self.build_transcript(messages)

    def get_llm_provider(self, model_tier: Optional[str] = None):
        from adaptors.claude_code.providers import ClaudeCodeOAuthLLMProvider
        return ClaudeCodeOAuthLLMProvider()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
