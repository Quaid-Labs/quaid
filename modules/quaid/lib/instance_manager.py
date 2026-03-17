"""Instance manager — adapter-specific instance lifecycle.

Provides a common interface for creating, listing, and configuring Quaid
instance silos. Each adapter that supports named instances registers its own
InstanceManager subclass via QuaidAdapter.get_instance_manager().

Usage::

    mgr = get_adapter().get_instance_manager()
    if mgr:
        silo_root = mgr.create("myproject")
        print(mgr.settings_snippet(silo_root.name))
"""

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter


class InstanceManager:
    """Base class for adapter-specific instance lifecycle management.

    Subclasses override create() / settings_snippet() with adapter-specific
    behaviour. The base implementation provides silo initialization that works
    for any adapter following the "<prefix>-<label>" instance naming convention.
    """

    def __init__(self, adapter: "QuaidAdapter"):
        self.adapter = adapter

    # ---- Naming ----

    def resolve_instance_id(self, label: str) -> str:
        """Derive the full instance ID from a short label.

        Uses the adapter's agent_id_prefix() so naming is always consistent:
          label "myproject" + prefix "claude-code" → "claude-code-myproject"
        """
        label = str(label or "").strip().lower()
        if not label:
            raise ValueError("Instance label must be a non-empty string.")
        prefix = self.adapter.agent_id_prefix()
        return f"{prefix}-{label}" if prefix else label

    def describe_naming(self) -> str:
        """Human-readable description of this adapter's naming convention."""
        prefix = self.adapter.agent_id_prefix()
        return (
            f"Instance IDs use prefix '{prefix}': "
            f"{prefix}-main, {prefix}-myproject, {prefix}-work, …"
        )

    # ---- Create ----

    def create(self, label: str, *, dry_run: bool = False) -> Path:
        """Create a new instance silo for the given short label.

        Returns the silo root path. Raises if the silo already exists.
        """
        from lib.instance import validate_instance_id, instance_exists

        instance_id = self.resolve_instance_id(label)
        validate_instance_id(instance_id)

        silo_root = self.adapter.quaid_home() / instance_id

        if instance_exists(instance_id):
            raise ValueError(
                f"Instance '{instance_id}' already exists at {silo_root}. "
                "Use a different label or delete the existing instance first."
            )

        if not dry_run:
            self._init_silo(silo_root, instance_id)
        return silo_root

    def _init_silo(self, silo_root: Path, instance_id: str) -> None:
        """Initialize a silo with config, DB, identity stubs, and PROJECT.md."""
        for subdir in ("config", "data", "identity", "journal", "logs"):
            (silo_root / subdir).mkdir(parents=True, exist_ok=True)

        # Config
        config_path = silo_root / "config" / "memory.json"
        if not config_path.exists():
            config_path.write_text(
                json.dumps(self._default_config(), indent=2) + "\n",
                encoding="utf-8",
            )

        # Database
        db_path = silo_root / "data" / "memory.db"
        if not db_path.exists():
            schema_path = (
                Path(__file__).parent.parent
                / "datastore" / "memorydb" / "schema.sql"
            )
            if schema_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.executescript(schema_path.read_text(encoding="utf-8"))
                conn.close()
                db_path.chmod(0o600)

        # Identity stubs
        for fname in ("SOUL.md", "USER.md", "MEMORY.md"):
            fpath = silo_root / "identity" / fname
            if not fpath.exists():
                fpath.write_text(f"# {fname[:-3]}\n", encoding="utf-8")

        # PROJECT.md
        project_md = silo_root / "PROJECT.md"
        if not project_md.exists():
            project_md.write_text(
                f"# Quaid Instance: {instance_id}\n\n"
                "Quaid knowledge layer instance.\n\n"
                "## Identity\n"
                "- `identity/SOUL.md` — Agent personality and interaction style\n"
                "- `identity/USER.md` — About the user\n"
                "- `identity/MEMORY.md` — Core facts loaded every session\n\n"
                "## Structure\n"
                "- `data/` — Memory database (SQLite)\n"
                "- `journal/` — Journal entries\n"
                "- `logs/` — Janitor and system logs\n",
                encoding="utf-8",
            )

        # Misc project — per-instance scratch pad registered at silo creation so
        # agents can find it immediately without a manual create-project step.
        try:
            misc_name = f"misc--{instance_id}"
            misc_desc = "Scratch pad for ephemeral and temporary files."
            from core.project_registry import create_project as _cp, get_project as _gp
            if not _gp(misc_name):
                _cp(misc_name, description=misc_desc)
        except Exception:
            pass  # Non-fatal at silo init

    def _default_config(self) -> dict:
        return {
            "adapter": {"type": self.adapter.adapter_id()},
            "retrieval": {"failHard": False, "autoInject": True},
        }

    # ---- Settings / integration snippet ----

    def settings_snippet(self, instance_id: str) -> str:
        """Return a config snippet to activate this instance in a project.

        Output format is adapter-specific. Override in subclasses.
        """
        return f"# Set QUAID_INSTANCE={instance_id} in your project environment.\n"
