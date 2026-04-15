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
import logging
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter


def _deep_merge_defaults(defaults: dict, existing: dict) -> dict:
    """Return existing with any missing keys filled in from defaults.

    Merges recursively for nested dicts. Never clobbers an existing value —
    only fills in gaps.
    """
    result = dict(defaults)
    for key, value in existing.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_defaults(result[key], value)
        else:
            result[key] = value
    return result


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_temp_path(path: Path) -> bool:
    try:
        resolved = Path(os.path.realpath(path.expanduser().resolve()))
        tmp_root = Path(os.path.realpath(tempfile.gettempdir()))
        return resolved == tmp_root or str(resolved).startswith(f"{tmp_root}{os.sep}")
    except Exception:
        return False


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

    def create(
        self,
        label: str,
        *,
        dry_run: bool = False,
        register_misc_project: bool = True,
    ) -> Path:
        """Create a new instance silo for the given short label.

        Returns the silo root path. Raises if the silo already exists.
        """
        from lib.instance import validate_instance_id, instance_exists

        instance_id = self.resolve_instance_id(label)
        validate_instance_id(instance_id)

        silo_root = self.adapter.instance_root().parent / instance_id

        if instance_exists(instance_id):
            raise ValueError(
                f"Instance '{instance_id}' already exists at {silo_root}. "
                "Use a different label or delete the existing instance first."
            )

        if not dry_run:
            self._init_silo(
                silo_root,
                instance_id,
                register_misc_project=register_misc_project,
            )
        return silo_root

    def _init_silo(
        self,
        silo_root: Path,
        instance_id: str,
        *,
        register_misc_project: bool = True,
    ) -> None:
        """Initialize hidden and visible instance directories."""
        visible_root = self.adapter.visible_home() / "instances" / instance_id
        silo_root.mkdir(parents=True, exist_ok=True)
        for subdir in ("data", "logs"):
            (silo_root / subdir).mkdir(parents=True, exist_ok=True)
        visible_root.mkdir(parents=True, exist_ok=True)
        (visible_root / "journal").mkdir(parents=True, exist_ok=True)

        # Config — fold defaults into existing config (fill missing keys without
        # clobbering values that were written by the installer or a prior run).
        config_path = silo_root / "config.json"
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        merged = _deep_merge_defaults(self._default_config(), existing)
        config_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

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

        # Identity files — seed from shared project templates when available,
        # fall back to minimal stubs so the visible instance is always self-consistent.
        try:
            _template_dir = self.adapter.visible_home() / "projects" / "quaid"
        except Exception:
            _template_dir = None
        for fname in ("SOUL.md", "USER.md", "ENVIRONMENT.md"):
            fpath = visible_root / fname
            if not fpath.exists():
                template = _template_dir / fname if _template_dir else None
                if template and template.exists():
                    fpath.write_bytes(template.read_bytes())
                else:
                    fpath.write_text(f"# {fname[:-3]}\n", encoding="utf-8")

        # Misc project — per-instance scratch pad registered at silo creation so
        # agents can find it immediately without a manual project-create step.
        if register_misc_project:
            try:
                self.ensure_registered_projects(instance_id)
            except Exception as _e:
                logger.warning("misc project registration skipped at silo init: %s", _e)

    def ensure_registered_projects(self, instance_id: str) -> None:
        """Ensure shared/quaid and misc projects exist for an instance."""
        self._ensure_shared_quaid_project(instance_id)
        misc_name = f"misc--{instance_id}"
        misc_desc = "Scratch pad for ephemeral and temporary files."
        from core.project_registry import create_project as _cp, get_project as _gp, link_project as _lp, _sync_docs_registry_project
        if not _gp(misc_name):
            try:
                _cp(misc_name, description=misc_desc, initial_instance=instance_id)
            except ValueError:
                pass
        else:
            _lp(misc_name, instance_id=instance_id)
        misc_canonical = self.adapter.visible_home() / "projects" / misc_name
        if _is_temp_path(misc_canonical):
            logger.info(
                "Skipping docs-registry sync for temp misc project %s (%s)",
                misc_name,
                misc_canonical,
            )
            return
        _sync_docs_registry_project(
            misc_name,
            description=misc_desc,
            source_root=None,
            canonical=misc_canonical,
            db_path=(self.adapter.quaid_home() / "instances" / instance_id / "data" / "memory.db"),
        )

    def _ensure_shared_quaid_project(self, instance_id: str) -> None:
        """Ensure the shared quaid project exists and is linked to this instance."""
        from core.project_registry import create_project as _cp, get_project as _gp, link_project as _lp, _sync_docs_registry_project

        project_name = "quaid"
        project_dir = self.adapter.visible_home() / "projects" / project_name
        bundled_dir = Path(__file__).resolve().parent.parent / "projects" / project_name
        if bundled_dir.is_dir():
            shutil.copytree(bundled_dir, project_dir, dirs_exist_ok=True)
        else:
            project_dir.mkdir(parents=True, exist_ok=True)

        desc = "Quaid runtime, memory, project-doc, and adapter reference docs."
        if not _gp(project_name):
            _cp(project_name, description=desc, initial_instance=instance_id)
        else:
            _lp(project_name, instance_id=instance_id)

        # During silo creation the ambient QUAID_INSTANCE may still point at a
        # different instance. Mirror the project definition into the target
        # silo DB explicitly so PROJECT.log routing works on first use.
        if _is_temp_path(project_dir):
            logger.info(
                "Skipping docs-registry sync for temp project %s (%s)",
                project_name,
                project_dir,
            )
            return
        _sync_docs_registry_project(
            project_name,
            description=desc,
            source_root=None,
            canonical=project_dir,
            db_path=(self.adapter.instance_root().parent / instance_id / "data" / "memory.db"),
        )

    def _default_config(self) -> dict:
        """Return a complete default config skeleton.

        All retrieval keys are included so the TypeScript adapter's preflight
        checks pass even if the silo was created before a full config was written.
        Values here are the canonical defaults — they will be folded in only where
        the actual config has a gap (see _deep_merge_defaults).
        """
        try:
            import dataclasses
            from config import RetrievalConfig
            retrieval_defaults = {
                f.name: f.default
                for f in dataclasses.fields(RetrievalConfig)
                if f.default is not dataclasses.MISSING
            }
        except Exception:
            retrieval_defaults = {
                "fail_hard": False,
                "auto_inject": True,
                "default_limit": 5,
                "max_limit": 8,
                "min_similarity": 0.80,
                "max_tokens": 2000,
            }
        retrieval_defaults["fail_hard"] = False
        retrieval_defaults["auto_inject"] = True
        defaults = {
            "adapter": {"type": self.adapter.adapter_id()},
            "retrieval": retrieval_defaults,
        }
        try:
            home = self.adapter.quaid_home()
            adapter_id = str(self.adapter.adapter_id() or "").strip()
            shared_defaults = [
                home / "shared" / "config" / "global" / "config.json",
                home / "shared" / "config" / adapter_id / "config.json",
            ]
            for cfg_path in shared_defaults:
                cfg = _read_json_object(cfg_path)
                if cfg:
                    defaults = _deep_merge_defaults(defaults, cfg)
        except Exception:
            pass
        defaults.setdefault("adapter", {})
        defaults["adapter"]["type"] = self.adapter.adapter_id()
        return defaults

    # ---- Settings / integration snippet ----

    def settings_snippet(self, instance_id: str) -> str:
        """Return a config snippet to activate this instance in a project.

        Output format is adapter-specific. Override in subclasses.
        """
        return f"# Set QUAID_INSTANCE={instance_id} in your project environment.\n"
