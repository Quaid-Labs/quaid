"""Claude Code instance manager — per-project silo creation.

Creates a Quaid silo and writes QUAID_INSTANCE into the target project's
.claude/settings.json so Claude Code picks it up for that workspace.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from lib.instance_manager import InstanceManager

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter


class ClaudeCodeInstanceManager(InstanceManager):
    """Instance manager for Claude Code per-project isolation."""

    def settings_snippet(self, instance_id: str) -> str:
        """Return the .claude/settings.json env block for this instance."""
        return json.dumps(
            {"env": {"QUAID_INSTANCE": instance_id}},
            indent=2,
        ) + "\n"

    def make_instance(self, project_path: str, name: str, *, dry_run: bool = False) -> Path:
        """Create a Quaid instance silo and wire it into a CC project.

        Args:
            project_path: Path to the CC project root (must contain or will
                          receive a .claude/settings.json).
            name: Short label for the instance (e.g. "myapp").
                  Full instance ID will be "<prefix>-<name>" (e.g. "claude-code-myapp").

        Returns:
            The silo root path.
        """
        project_dir = Path(project_path).resolve()
        if not project_dir.is_dir():
            raise ValueError(f"Project path does not exist: {project_dir}")

        silo_root = self.create(name, dry_run=dry_run)
        instance_id = self.resolve_instance_id(name)

        if not dry_run:
            self._write_settings(project_dir, instance_id)

        return silo_root

    def _write_settings(self, project_dir: Path, instance_id: str) -> None:
        """Write QUAID_INSTANCE into <project_dir>/.claude/settings.json."""
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.json"

        # Load existing settings or start fresh
        if settings_path.is_file():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                settings = {}
        else:
            settings = {}

        # Overwrite QUAID_INSTANCE in env block; preserve everything else
        env = settings.setdefault("env", {})
        env["QUAID_INSTANCE"] = instance_id

        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
