"""Instance resolution — zero-dependency module for Quaid instance identity.

This module owns the INSTANCE_ID concept: a short identifier (valid folder name)
that uniquely identifies a Quaid memory instance. Two terminals with the same
INSTANCE_ID share the same memory.

Zero imports from lib.adapter, config, or any core module.
Reads only os.environ and pathlib.Path.

Environment:
    QUAID_HOME      Root dir containing all instances (default: ~/quaid)
    QUAID_INSTANCE  Instance identifier (required — no implicit default)
"""

import os
import re
from pathlib import Path
from typing import List, Optional

# Instance name: alphanumeric start, then alphanumeric/dot/underscore/hyphen, max 64 chars
_INSTANCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

RESERVED_INSTANCE_NAMES = frozenset({
    "shared", "projects", "config", "data", "logs", "temp", "tmp",
    "quaid", "plugins", "lib", "core", "docs", "assets", "release",
    "scripts", "test", "tests", "benchmark", "node_modules",
})


def instance_slug_from_project_dir(project_dir: str) -> str:
    """Derive a stable instance slug from a project directory path.

    Resolves symlinks before slugifying so that paths pointing to the same
    directory (e.g. /tmp -> /private/tmp on macOS) always produce the same
    slug.  This is the single source of truth for project-dir-to-slug
    conversion — all callers (adapter, config search, auto-provision) must
    use this function, not inline regex.
    """
    root = Path(project_dir).resolve() if project_dir else Path(os.getcwd()).resolve()
    return re.sub(r"[^a-z0-9]+", "-", str(root).lower()).strip("-")


class InstanceError(Exception):
    """Raised when instance resolution or validation fails."""


def validate_instance_id(name: str) -> str:
    """Validate an instance identifier.

    Returns the validated name (stripped). Raises InstanceError on invalid input.
    """
    name = name.strip()
    if not name:
        raise InstanceError("QUAID_INSTANCE must be a non-empty string.")
    if name.lower() in RESERVED_INSTANCE_NAMES:
        raise InstanceError(
            f"Instance name '{name}' is reserved. "
            f"Reserved names: {', '.join(sorted(RESERVED_INSTANCE_NAMES))}"
        )
    if not _INSTANCE_ID_PATTERN.match(name):
        raise InstanceError(
            f"Instance name '{name}' is invalid. "
            "Must start with alphanumeric, contain only [a-zA-Z0-9._-], max 64 chars."
        )
    return name


def quaid_home() -> Path:
    """Root directory containing all Quaid instances.

    Reads from QUAID_HOME env var. Defaults to ~/quaid.
    """
    env = os.environ.get("QUAID_HOME", "").strip()
    return Path(env).resolve() if env else Path.home() / "quaid"


def instance_id() -> str:
    """Current instance identifier. Reads QUAID_INSTANCE env var.

    Raises InstanceError if QUAID_INSTANCE is not set or invalid.
    """
    env = os.environ.get("QUAID_INSTANCE", "").strip()
    if not env:
        raise InstanceError(
            "QUAID_INSTANCE environment variable is not set. "
            "Set it to a valid instance name (e.g. 'openclaw', 'claude-code')."
        )
    return validate_instance_id(env)


def instance_root() -> Path:
    """Resolved instance root directory: QUAID_HOME / INSTANCE_ID."""
    return quaid_home() / instance_id()


def shared_dir() -> Path:
    """Shared directory for cross-instance resources."""
    return quaid_home() / "shared"


def shared_projects_dir() -> Path:
    """Canonical projects directory: QUAID_HOME/projects/."""
    return quaid_home() / "projects"


def shared_registry_path() -> Path:
    """Global project registry: QUAID_HOME/projects/project-registry.json."""
    return shared_projects_dir() / "project-registry.json"


def shared_config_path() -> Path:
    """Shared global config file: QUAID_HOME/shared/config/global/memory.json.

    Contains machine-wide settings (embeddings model, Ollama URL) that all
    instances on this machine inherit.  Instance configs can override individual
    keys; shared config is the fallback layer below instance config.
    """
    return shared_dir() / "config" / "global" / "memory.json"


def misc_project_name(name: Optional[str] = None) -> str:
    """Registry name for an instance's temp project.

    Convention: misc--{instance_id}  (e.g. misc--openclaw-main)
    Single shared bucket for miscellaneous work without a proper project home:
    drafts, one-offs, quick scripts, staging. Not "temp" (implies deletion) —
    misc files may stick around. Lives as a tracked project in QUAID_HOME/projects/.
"""
    iid = validate_instance_id(name) if name else instance_id()
    return f"misc--{iid}"


def instance_misc_dir(name: Optional[str] = None) -> Path:
    """Per-instance misc project directory: QUAID_HOME/projects/misc--{instance}/"""
    return shared_projects_dir() / misc_project_name(name)


def instance_exists(name: str) -> bool:
    """Check if an instance directory exists and has config."""
    try:
        validated = validate_instance_id(name)
    except InstanceError:
        return False
    config_path = quaid_home() / validated / "config" / "memory.json"
    return config_path.is_file()


def list_instances() -> List[str]:
    """List all registered instance names under QUAID_HOME.

    An instance is a directory that is not a reserved name and contains
    config/memory.json.
    """
    home = quaid_home()
    if not home.is_dir():
        return []
    instances = []
    for entry in sorted(home.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        if name.lower() in RESERVED_INSTANCE_NAMES:
            continue
        if (entry / "config" / "memory.json").is_file():
            instances.append(name)
    return instances


def require_instance_exists(name: Optional[str] = None) -> str:
    """Validate that the instance exists on disk. Returns the validated name.

    If name is None, reads from QUAID_INSTANCE env var.
    Raises InstanceError if the instance doesn't exist.
    """
    if name is None:
        name = instance_id()
    else:
        name = validate_instance_id(name)
    if not instance_exists(name):
        existing = list_instances()
        msg = f"Instance '{name}' does not exist (no config/memory.json found)."
        if existing:
            msg += f" Existing instances: {', '.join(existing)}"
        raise InstanceError(msg)
    return name
