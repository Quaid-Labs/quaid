"""Claude Code instance manager — per-project silo creation.

Creates a Quaid silo and writes QUAID_INSTANCE into the target project's
.claude/settings.json so Claude Code picks it up for that workspace.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from lib.fail_policy import is_fail_hard_enabled
from lib.instance import _legacy_instance_slug_from_project_dir, instance_slug_from_project_dir
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

    # Default model IDs written during installation.
    # OAuth tokens with CC identity headers can access all model tiers.
    DEFAULT_DEEP_MODEL = "claude-sonnet-4-6"
    DEFAULT_FAST_MODEL = "claude-haiku-4-5"

    @staticmethod
    def _is_managed_quaid_hook_command(command: str) -> bool:
        text = str(command or "")
        return (
            "hook-session-init" in text
            or "hook-inject" in text
            or "hook-extract" in text
            or "hook-subagent-start" in text
            or "hook-subagent-stop" in text
        )

    def make_instance(
        self,
        project_path: str,
        name: str,
        *,
        token: str = "",
        deep_model: str = "",
        fast_model: str = "",
        settings_path: Path | None = None,
        dry_run: bool = False,
    ) -> Path:
        """Create a Quaid instance silo and wire it into a CC project.

        Args:
            project_path: Path to the CC project root (must contain or will
                          receive a .claude/settings.json).
            name: Short label for the instance (e.g. "myapp").
                  Full instance ID will be "<prefix>-<name>" (e.g. "claude-code-myapp").
            token: API-scoped OAuth token to store for daemon use.
            deep_model: Override for deep-reasoning model ID.
            fast_model: Override for fast-reasoning model ID.
            settings_path: Optional path for Claude global settings hooks file.
                          Defaults to ~/.claude/settings.json.

        Returns:
            The silo root path.
        """
        project_dir = Path(project_path).resolve()
        if not project_dir.is_dir():
            raise ValueError(f"Project path does not exist: {project_dir}")

        label = self.canonical_project_label(project_dir, name)
        silo_root = self.create(label, dry_run=dry_run)
        instance_id = self.resolve_instance_id(label)

        if not dry_run:
            project_settings_path = settings_path or (project_dir / ".claude" / "settings.json")
            self._write_settings(project_dir, instance_id)
            self._write_hooks(instance_id, settings_path=project_settings_path)
            self._store_auth_token(token)
            self._write_model_config(
                silo_root,
                deep_model=deep_model or self.DEFAULT_DEEP_MODEL,
                fast_model=fast_model or self.DEFAULT_FAST_MODEL,
            )

        return silo_root

    def canonical_project_label(self, project_dir: Path, name: str) -> str:
        """Normalize stale path-derived labels to the runtime slug for this project."""
        label = str(name or "").strip()
        prefix = f"{self.adapter.agent_id_prefix()}-"
        if prefix and label.startswith(prefix):
            label = label[len(prefix):]

        current_slug = instance_slug_from_project_dir(str(project_dir))
        legacy_slug = _legacy_instance_slug_from_project_dir(str(project_dir))
        if label == legacy_slug:
            return current_slug
        return label

    def _store_auth_token(self, explicit_token: str = "") -> None:
        """Write an API-scoped OAuth token to the silo's .auth-token file.

        Token resolution order:
          1. explicit_token argument (passed by caller / installer CLI)
          2. QUAID_AUTH_TOKEN env var (set by user or CI environment)
          3. CLAUDE_CODE_OAUTH_TOKEN env var (available in some CC contexts)

        If no token is found, prints a warning and skips.  The daemon will
        fall back to 'claude -p' subprocess calls in that case.
        """
        token = (
            explicit_token.strip()
            or os.environ.get("QUAID_AUTH_TOKEN", "").strip()
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        )
        if not token:
            print(
                "  Warning: no auth token provided.  Daemon LLM calls will fail "
                "without a token.  Re-run install with --token <your-token> "
                "or set QUAID_AUTH_TOKEN."
            )
            return
        try:
            path = self.adapter.store_auth_token(token)
            print(f"  Auth token written to {path}")
        except Exception as e:
            print(f"  Warning: could not write auth token: {e}")
            if is_fail_hard_enabled():
                raise

    def _write_model_config(self, silo_root: Path, deep_model: str, fast_model: str) -> None:
        """Write model IDs into the instance config.json.

        The daemon reads models.deep_reasoning and models.fast_reasoning from
        this file.  Without explicit values the provider has no fallback and
        will raise at call time.
        """
        config_path = silo_root / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        cfg = {}
        if config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cfg = {}

        models = cfg.setdefault("models", {})
        models["deepReasoning"] = deep_model
        models["fastReasoning"] = fast_model

        config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"  Model config written: deep={deep_model} fast={fast_model}")

    def auto_provision(self, name: str) -> tuple:
        """Provision a silo for the given name without touching settings.json.

        Unlike make_instance, this does not write QUAID_INSTANCE to any project
        settings file — instance identity is derived from PWD at hook runtime.

        Returns:
            (instance_id, was_new): instance ID and whether the silo was just created.
        """
        instance_id = self.resolve_instance_id(name)
        silo_root = self.adapter.quaid_home() / "instances" / instance_id
        was_new = not (silo_root / "config.json").exists()

        if was_new:
            self.create(name)
            # Write model config inline — avoid _write_model_config() stdout
            # prints which would corrupt hook JSON output on stdout
            config_path = silo_root / "config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            cfg: dict = {}
            if config_path.is_file():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            models = cfg.setdefault("models", {})
            models.setdefault("deepReasoning", self.DEFAULT_DEEP_MODEL)
            models.setdefault("fastReasoning", self.DEFAULT_FAST_MODEL)
            config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        return instance_id, was_new

    def _write_hooks(self, instance_id: str, settings_path: Path | None = None) -> None:
        """Write the 6 Quaid hook commands into Claude Code settings.

        Mirrors what the JS installer's setupClaudeCodeHooks() does. Called
        by make_instance() so newly created project instances get hooks wired
        without requiring a full re-run of the installer.

        Skips any hook command already present to avoid duplicates. If the
        main install already wrote identical hooks, this is a no-op.
        """
        import shutil as _shutil

        # Prefer the quaid binary co-located with this package; fall back to PATH.
        _pkg_root = Path(__file__).resolve().parent.parent.parent
        _candidate = _pkg_root / "quaid"
        quaid_bin = str(_candidate) if _candidate.is_file() else (_shutil.which("quaid") or "quaid")

        workspace = str(self.adapter.quaid_home())
        visible_home = str(self.adapter.visible_home())
        env_prefix = (
            f"QUAID_HOME='{workspace}' "
            f"QUAID_VISIBLE_HOME='{visible_home}' "
            "CLAUDE_PROJECT_DIR=\"${CLAUDE_PROJECT_DIR:-$PWD}\""
        )

        desired: dict = {
            "SessionStart":     f"{env_prefix} {quaid_bin} hook-session-init",
            "UserPromptSubmit": f"{env_prefix} {quaid_bin} hook-inject",
            "PreCompact":       f"{env_prefix} {quaid_bin} hook-extract --precompact",
            "SessionEnd":       f"{env_prefix} {quaid_bin} hook-extract",
            "SubagentStart":    f"{env_prefix} {quaid_bin} hook-subagent-start",
            "SubagentStop":     f"{env_prefix} {quaid_bin} hook-subagent-stop",
        }

        if settings_path is None:
            settings_path = Path.home() / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        settings: dict = {}
        if settings_path.is_file():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                settings = {}

        if "hooks" not in settings:
            settings["hooks"] = {}

        changed = False
        for event, command in desired.items():
            entries = settings["hooks"].setdefault(event, [])
            filtered_entries = []
            for entry in entries:
                hooks = list(entry.get("hooks", []))
                kept = [h for h in hooks if not self._is_managed_quaid_hook_command(h.get("command", ""))]
                if kept:
                    cleaned = dict(entry)
                    cleaned["hooks"] = kept
                    filtered_entries.append(cleaned)
            if filtered_entries != entries:
                settings["hooks"][event] = filtered_entries
                changed = True
            existing_cmds = {
                h.get("command", "")
                for entry in settings["hooks"][event]
                for h in entry.get("hooks", [])
            }
            if command not in existing_cmds:
                settings["hooks"][event].append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
                changed = True

        env = settings.setdefault("env", {})
        if env.get("QUAID_HOME") != workspace:
            env["QUAID_HOME"] = workspace
            changed = True
        if env.get("QUAID_VISIBLE_HOME") != visible_home:
            env["QUAID_VISIBLE_HOME"] = visible_home
            changed = True
        if env.get("QUAID_INSTANCE") != instance_id:
            env["QUAID_INSTANCE"] = instance_id
            changed = True

        if changed:
            settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            print(f"  CC hooks written for {instance_id}")
        else:
            print(f"  CC hooks already present for {instance_id}")

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
