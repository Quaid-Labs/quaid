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
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, List, Optional

# Instance name: alphanumeric start, then alphanumeric/dot/underscore/hyphen, max 64 chars
_INSTANCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_PROJECT_INSTANCE_SLUG_MAX_LENGTH = 52
_PROJECT_INSTANCE_SLUG_HASH_LENGTH = 12

RESERVED_INSTANCE_NAMES = frozenset({
    "shared", "projects", "config", "data", "logs", "temp", "tmp",
    "quaid", "plugins", "lib", "core", "docs", "assets", "release",
    "scripts", "test", "tests", "benchmark", "node_modules",
})


def _resolved_project_dir(project_dir: str) -> Path:
    return Path(project_dir).resolve() if project_dir else Path(os.getcwd()).resolve()


def _legacy_instance_slug_from_project_dir(project_dir: str) -> str:
    """Previous lossy project-dir slug, retained only for guarded migration reads."""
    root = _resolved_project_dir(project_dir)
    slug = re.sub(r"[^a-z0-9]+", "-", str(root).lower()).strip("-")
    if slug:
        return slug
    # Legacy migration bindings still need a valid ASCII filename for non-ASCII-only paths.
    digest = hashlib.sha256(str(root).encode("utf-8", "surrogatepass")).hexdigest()
    return f"project-{digest[:_PROJECT_INSTANCE_SLUG_HASH_LENGTH]}"


def instance_slug_from_project_dir(project_dir: str) -> str:
    """Derive a stable instance slug from a project directory path.

    Resolves symlinks before slugifying so that paths pointing to the same
    directory (e.g. /tmp -> /private/tmp on macOS) always produce the same
    slug.  This is the single source of truth for project-dir-to-slug
    conversion — all callers (adapter, config search, auto-provision) must
    use this function, not inline regex.

    The readable prefix is anchored by a hash of the resolved path so sibling
    paths that differ only by punctuation (my_app vs my-app) cannot collapse
    into the same memory silo. The slug is capped so the longest current
    adapter prefix still produces a valid 64-character instance id.
    """
    root = _resolved_project_dir(project_dir)
    digest = hashlib.sha256(str(root).encode("utf-8", "surrogatepass")).hexdigest()
    suffix = f"-{digest[:_PROJECT_INSTANCE_SLUG_HASH_LENGTH]}"
    readable = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    head_len = _PROJECT_INSTANCE_SLUG_MAX_LENGTH - len(suffix)
    readable = readable[:head_len].strip("-") or "project"
    return f"{readable}{suffix}"


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
            "Set it to a valid instance name."
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


def _active_openclaw_agent_labels() -> tuple[set[str], list[Path], bool]:
    labels = {"main"}
    roots: list[Path] = []
    has_authoritative_list = False
    for cfg_path in _openclaw_config_candidates():
        if not cfg_path.exists():
            continue
        roots.append(cfg_path.parent)
        cfg = _read_json_object(cfg_path)
        agents = cfg.get("agents")
        if not isinstance(agents, dict):
            continue
        agent_list = agents.get("list")
        if isinstance(agent_list, list):
            has_authoritative_list = True
        for row in agent_list or []:
            if isinstance(row, dict):
                label = str(row.get("id") or "").strip().lower()
                if label:
                    labels.add(label)
        if not has_authoritative_list:
            for key in agents.keys():
                label = str(key or "").strip().lower()
                if label and label not in {"defaults", "list"}:
                    labels.add(label)
    fallback_root = Path.home() / ".openclaw"
    if fallback_root.exists() and all(str(root) != str(fallback_root) for root in roots):
        roots.append(fallback_root)
    return labels, roots, has_authoritative_list


def _adapter_id_with_runtime_flag(flag: str) -> str:
    manifests_dir = Path(__file__).resolve().parent.parent / "adaptors" / "manifests"
    try:
        for manifest_path in sorted(manifests_dir.glob("*.json")):
            try:
                data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            runtime = data.get("runtime")
            if not isinstance(runtime, dict) or not runtime.get(flag):
                continue
            adapter_id = str(data.get("id") or manifest_path.stem).strip().lower()
            if adapter_id:
                return adapter_id
    except Exception:
        return ""
    return ""


def _is_stale_openclaw_agent_instance(name: str, instance_dir: Path) -> bool:
    adapter_id = _adapter_id_with_runtime_flag("staleAgentSiloPrune")
    if not adapter_id:
        return False
    prefix = f"{adapter_id}-"
    if not name.startswith(prefix) or name == f"{prefix}main":
        return False
    if _raw_adapter_type(instance_dir) != adapter_id:
        return False
    labels, roots, has_authoritative_list = _active_openclaw_agent_labels()
    if not roots:
        return False
    label = name[len(prefix):].strip().lower()
    if not label or label in labels:
        return False
    # OpenClaw 2026.4.26 can leave ~/.openclaw/agents/<label> behind after
    # `openclaw agents delete`; agents.list is the authoritative active set.
    if has_authoritative_list:
        return True
    for root in roots:
        agent_dir = root / "agents" / label
        if not agent_dir.exists():
            continue
        try:
            if agent_dir.is_dir() and not any(agent_dir.iterdir()):
                continue
        except OSError:
            return False
        return False
    return True


def _prune_stale_openclaw_agent_instance(
    name: str,
    instance_dir: Path,
    *,
    home: Optional[Path] = None,
) -> bool:
    """Remove a stale OpenClaw agent silo after the native agent is gone."""
    if not _is_stale_openclaw_agent_instance(name, instance_dir):
        return False
    try:
        root = (
            (Path(home).resolve() if home is not None else quaid_home())
            / "instances"
        ).resolve()
        target = instance_dir.resolve()
        if instance_dir.is_symlink() or target.parent != root or target.name != name:
            return False
        shutil.rmtree(target)
        return True
    except OSError:
        return False


def _livetest_harness_enabled() -> bool:
    return os.environ.get("QUAID_LIVETEST_HARNESS", "").strip().lower() in {"1", "true", "yes", "on"}


def stale_openclaw_agent_instances(home: Optional[Path] = None) -> List[str]:
    """List stale OpenClaw agent silos whose native agent state is gone."""
    instances_dir = (Path(home).resolve() if home is not None else quaid_home()) / "instances"
    if not instances_dir.is_dir():
        return []
    stale: List[str] = []
    for entry in sorted(instances_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        name = entry.name
        try:
            validate_instance_id(name)
        except InstanceError:
            continue
        if _is_stale_openclaw_agent_instance(name, entry):
            stale.append(name)
    return stale


def prune_stale_openclaw_agent_instances(home: Optional[Path] = None) -> List[str]:
    """Delete stale OpenClaw agent silos whose native agent state is gone.

    This is intentionally restricted to the livetest harness. User-facing
    instance listing must never delete local instance data as a side effect.
    """
    if not _livetest_harness_enabled():
        raise InstanceError(
            "Refusing to delete stale OpenClaw silos outside the livetest harness; "
            "set QUAID_LIVETEST_HARNESS=1 only from tests/livetest cleanup tools."
        )
    pruned: List[str] = []
    instances_dir = (Path(home).resolve() if home is not None else quaid_home()) / "instances"
    for name in stale_openclaw_agent_instances(home):
        entry = instances_dir / name
        if _prune_stale_openclaw_agent_instance(name, entry, home=instances_dir.parent):
            pruned.append(name)
    return pruned


def _is_empty_instance_bind_point_residue(instance_dir: Path) -> bool:
    """Return true for configless instance dirs that contain only empty dirs.

    The live-test harness can leave a bind-point behind when a daemon or hook
    creates ``data/extraction-signals`` after the test has already torn down the
    sibling silo. Only delete directories with no files at all; anything with a
    config, database, cursor, log, or other file is real state.
    """
    if not instance_dir.is_dir() or instance_dir.is_symlink():
        return False
    if (instance_dir / "config.json").exists():
        return False
    try:
        for child in instance_dir.rglob("*"):
            if child.is_symlink() or child.is_file():
                return False
            if not child.is_dir():
                return False
        return True
    except OSError:
        return False


def prune_livetest_instance_residues(home: Optional[Path] = None) -> List[str]:
    """Delete empty configless instance bind-points in the livetest harness."""
    if not _livetest_harness_enabled():
        raise InstanceError(
            "Refusing to delete instance residues outside the livetest harness; "
            "set QUAID_LIVETEST_HARNESS=1 only from tests/livetest cleanup tools."
        )
    pruned: List[str] = []
    instances_dir = (Path(home).resolve() if home is not None else quaid_home()) / "instances"
    if not instances_dir.is_dir():
        return pruned
    root = instances_dir.resolve()
    for entry in sorted(instances_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        name = entry.name
        try:
            validate_instance_id(name)
        except InstanceError:
            continue
        target = entry.resolve()
        if target.parent != root or target.name != name:
            continue
        if not _is_empty_instance_bind_point_residue(entry):
            continue
        try:
            shutil.rmtree(target)
            pruned.append(name)
        except OSError:
            continue
    return pruned


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
