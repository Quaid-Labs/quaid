"""Instance resolution — zero-dependency module for Quaid instance identity.

This module owns the INSTANCE_ID concept: a short identifier (valid folder name)
that uniquely identifies a Quaid memory instance. Two terminals with the same
INSTANCE_ID share the same memory.

Zero imports from lib.adapter, config, or any core module.
Reads only os.environ and pathlib.Path.

Environment:
    QUAID_HOME      Hidden system root containing all instances (default: ~/.quaid)
    QUAID_VISIBLE_HOME  Visible user-facing root (default: ~/quaid)
    QUAID_INSTANCE  Instance identifier (required — no implicit default)
"""

import os
import json
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


def _derive_visible_home(hidden_root: Path) -> Path:
    name = hidden_root.name
    if name.startswith(".") and len(name) > 1:
        return hidden_root.with_name(name[1:])
    return hidden_root


def quaid_home() -> Path:
    """Hidden system root containing all Quaid instances.

    Reads from QUAID_HOME env var. Defaults to ~/.quaid.
    """
    env = os.environ.get("QUAID_HOME", "").strip()
    return Path(env).resolve() if env else Path.home() / ".quaid"


def visible_home() -> Path:
    """Visible user-facing Quaid root.

    Reads from QUAID_VISIBLE_HOME env var. Defaults to ~/quaid or a sibling
    path derived from QUAID_HOME when the hidden root is customized.
    """
    env = os.environ.get("QUAID_VISIBLE_HOME", "").strip()
    if env:
        return Path(env).resolve()
    return _derive_visible_home(quaid_home())


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
    """Resolved hidden instance root directory: QUAID_HOME/instances/INSTANCE_ID."""
    return quaid_home() / "instances" / instance_id()


def visible_instance_root(name: Optional[str] = None) -> Path:
    """Resolved visible instance root directory: QUAID_VISIBLE_HOME/instances/INSTANCE_ID."""
    iid = validate_instance_id(name) if name else instance_id()
    return visible_home() / "instances" / iid


def shared_dir() -> Path:
    """Shared directory for cross-instance resources."""
    return quaid_home() / "shared"


def shared_projects_dir() -> Path:
    """Canonical visible projects directory: QUAID_VISIBLE_HOME/projects/."""
    return visible_projects_dir()


def visible_projects_dir() -> Path:
    """Canonical visible projects directory: QUAID_VISIBLE_HOME/projects/."""
    return visible_home() / "projects"


def shared_registry_path() -> Path:
    """Global project registry stored in hidden QUAID_HOME."""
    return quaid_home() / "project-registry.json"


def shared_config_path() -> Path:
    """Shared global config file: QUAID_HOME/shared/config/global/config.json.

    Contains machine-wide settings (embeddings model, Ollama URL) that all
    instances on this machine inherit.  Instance configs can override individual
    keys; shared config is the fallback layer below instance config.
    """
    return shared_dir() / "config" / "global" / "config.json"


def misc_project_name(name: Optional[str] = None) -> str:
    """Registry name for an instance's temp project.

    Convention: misc--{instance_id}  (e.g. misc--openclaw-main)
    Single shared bucket for miscellaneous work without a proper project home:
    drafts, one-offs, quick scripts, staging. Not "temp" (implies deletion) —
    misc files may stick around. Lives as a tracked project in QUAID_VISIBLE_HOME/projects/.
"""
    iid = validate_instance_id(name) if name else instance_id()
    return f"misc--{iid}"


def instance_misc_dir(name: Optional[str] = None) -> Path:
    """Per-instance misc project directory: QUAID_VISIBLE_HOME/projects/misc--{instance}/"""
    return shared_projects_dir() / misc_project_name(name)


def instance_exists(name: str) -> bool:
    """Check if an instance directory exists and has config."""
    try:
        validated = validate_instance_id(name)
    except InstanceError:
        return False
    if is_internal_path_derived_instance_id(validated):
        return False
    return (quaid_home() / "instances" / validated / "config.json").is_file()


def internal_path_derived_instance_ids(home: Optional[Path] = None) -> set[str]:
    """Instance ids accidentally derived from Quaid's own hidden runtime paths."""
    root = Path(home).resolve() if home is not None else quaid_home().resolve()
    internal_paths = [
        root,
        root / "plugins",
        root / "plugins" / "quaid",
        root / "extensions",
        root / "adaptors",
        root / "shared",
        root / "instances",
        root / "runtime",
        root / ".runtime",
    ]
    out: set[str] = set()
    for candidate in internal_paths:
        slug = instance_slug_from_project_dir(str(candidate))
        if not slug:
            continue
        out.add(f"claude-code-{slug}")
        out.add(f"codex-{slug}")
    return out


def is_internal_path_derived_instance_id(name: str, home: Optional[Path] = None) -> bool:
    try:
        validated = validate_instance_id(name)
    except InstanceError:
        return False
    return validated in internal_path_derived_instance_ids(home)


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _raw_adapter_type(instance_dir: Path) -> str:
    config = _read_json_object(instance_dir / "config.json")
    adapter = config.get("adapter")
    if isinstance(adapter, dict):
        return str(adapter.get("type") or "").strip().lower()
    return ""


def _openclaw_config_candidates() -> List[Path]:
    out: List[Path] = []
    raw = str(os.environ.get("OPENCLAW_CONFIG_PATH", "") or "").strip()
    if raw:
        out.append(Path(raw).expanduser())
    out.append(Path.home() / ".openclaw" / "openclaw.json")
    deduped: List[Path] = []
    seen: set[str] = set()
    for path in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _active_openclaw_agent_labels() -> tuple[set[str], list[Path]]:
    labels = {"main"}
    roots: list[Path] = []
    for cfg_path in _openclaw_config_candidates():
        if not cfg_path.exists():
            continue
        roots.append(cfg_path.parent)
        cfg = _read_json_object(cfg_path)
        agents = cfg.get("agents")
        if not isinstance(agents, dict):
            continue
        for row in agents.get("list") or []:
            if isinstance(row, dict):
                label = str(row.get("id") or "").strip().lower()
                if label:
                    labels.add(label)
        for key in agents.keys():
            label = str(key or "").strip().lower()
            if label and label not in {"defaults", "list"}:
                labels.add(label)
    fallback_root = Path.home() / ".openclaw"
    if fallback_root.exists() and all(str(root) != str(fallback_root) for root in roots):
        roots.append(fallback_root)
    return labels, roots


def _is_stale_openclaw_agent_instance(name: str, instance_dir: Path) -> bool:
    prefix = "openclaw-"
    if not name.startswith(prefix) or name == f"{prefix}main":
        return False
    if _raw_adapter_type(instance_dir) != "openclaw":
        return False
    labels, roots = _active_openclaw_agent_labels()
    if not roots:
        return False
    label = name[len(prefix):].strip().lower()
    if not label or label in labels:
        return False
    for root in roots:
        if (root / "agents" / label).exists():
            return False
    return True


def list_instances() -> List[str]:
    """List all registered instance names under QUAID_HOME/instances/.

    An instance is a directory under instances/ that contains config.json.
    """
    instances_dir = quaid_home() / "instances"
    if not instances_dir.is_dir():
        return []
    instances = []
    for entry in sorted(instances_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("."):
            continue
        try:
            validate_instance_id(name)
        except InstanceError:
            continue
        if is_internal_path_derived_instance_id(name):
            continue
        if _is_stale_openclaw_agent_instance(name, entry):
            continue
        if (entry / "config.json").is_file():
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
        msg = f"Instance '{name}' does not exist (no config.json found)."
        if existing:
            msg += f" Existing instances: {', '.join(existing)}"
        raise InstanceError(msg)
    return name
