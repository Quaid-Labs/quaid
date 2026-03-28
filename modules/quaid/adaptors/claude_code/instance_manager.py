"""Claude Code instance manager — per-project silo creation.

Creates a Quaid silo and writes QUAID_INSTANCE into the target project's
.claude/settings.json so Claude Code picks it up for that workspace.
"""

import json
import os
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

    # Default model IDs written during installation.
    # OAuth tokens with CC identity headers can access all model tiers.
    DEFAULT_DEEP_MODEL = "claude-haiku-4-5"
    DEFAULT_FAST_MODEL = "claude-haiku-4-5"

    def make_instance(
        self,
        project_path: str,
        name: str,
        *,
        token: str = "",
        deep_model: str = "",
        fast_model: str = "",
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
            self._store_auth_token(token)
            self._write_model_config(
                silo_root,
                deep_model=deep_model or self.DEFAULT_DEEP_MODEL,
                fast_model=fast_model or self.DEFAULT_FAST_MODEL,
            )

        return silo_root

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

    def _write_model_config(self, silo_root: Path, deep_model: str, fast_model: str) -> None:
        """Write model IDs into the instance config/memory.json.

        The daemon reads models.deep_reasoning and models.fast_reasoning from
        this file.  Without explicit values the provider has no fallback and
        will raise at call time.
        """
        config_path = silo_root / "config" / "memory.json"
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
        silo_root = self.adapter.quaid_home() / instance_id
        # Use config dir existence as the "silo is initialized" signal
        was_new = not (silo_root / "config").exists()

        if was_new:
            self.create(name)
            # Write model config inline — avoid _write_model_config() stdout
            # prints which would corrupt hook JSON output on stdout
            config_path = silo_root / "config" / "memory.json"
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
