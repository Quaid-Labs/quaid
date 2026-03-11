"""Claude Code adapter for Quaid memory system.

Integrates Quaid as a lifecycle-aware memory layer for Claude Code sessions.
Uses CLI subcommands via the existing Bash tool + hooks for automation.

- Home dir: QUAID_HOME env or ~/quaid/
- Notifications: stderr
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
        return Path(env) if env else Path.home() / "quaid"

    def notify(self, message: str, channel_override: Optional[str] = None,
               dry_run: bool = False, force: bool = False) -> bool:
        if os.environ.get("QUAID_DISABLE_NOTIFICATIONS") and not force:
            return True
        if dry_run:
            print(f"[notify] (dry-run) {message}", file=sys.stderr)
            return True
        print(f"[quaid] {message}", file=sys.stderr)
        return True

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
