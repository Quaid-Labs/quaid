"""Subagent registry — tracks parent/child session relationships.

Adapter-agnostic registry that allows extraction to merge subagent
transcripts with their parent session. Both Claude Code and OpenClaw
adapters write to the same registry via lifecycle hooks.

Storage: JSON files per parent session under QUAID_INSTANCE_ROOT/data/subagent-registry/

Design principles:
  - Parent session owns memory; subagents are attached work units
  - Registered subagent sessions skip standalone timeout extraction
  - Parent extraction events collect completed subagent transcripts
  - Facts are attributed to the parent session lineage
"""

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PLACEHOLDER_CHILD_IDS = {
    "default",
    "general-purpose",
    "subagent",
    "unknown",
}


def _now_datetime() -> datetime:
    raw = str(os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _now_iso_z() -> str:
    return _now_datetime().strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> float:
    return _now_datetime().timestamp()


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception as exc:
        logger.critical("fail-hard policy unavailable in subagent registry: %s", exc)
        return True


def _registry_dir() -> Path:
    """Resolve registry directory from the active hidden instance root."""
    try:
        from lib.instance import instance_root
        base = instance_root()
    except Exception:
        home = os.environ.get("QUAID_HOME", "").strip()
        instance = os.environ.get("QUAID_INSTANCE", "").strip() or "standalone"
        hidden_root = Path(home).resolve() if home else Path.home() / ".quaid"
        base = hidden_root / "instances" / instance
    d = base / "data" / "subagent-registry"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _registry_path(parent_session_id: str) -> Path:
    """Path to the registry file for a given parent session."""
    return _registry_dir() / f"{parent_session_id}.json"


def _read_registry(parent_session_id: str) -> Dict[str, Any]:
    """Read registry file for a parent session."""
    p = _registry_path(parent_session_id)
    if not p.exists():
        return {"parent_session_id": parent_session_id, "children": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"parent_session_id": parent_session_id, "children": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[subagent-registry] failed to read %s: %s", p, e)
        return {"parent_session_id": parent_session_id, "children": {}}


@contextmanager
def _lock_registry(parent_session_id: str):
    """Context manager for exclusive registry access. B004: prevents concurrent write loss."""
    lock_file = _registry_dir() / f"{parent_session_id}.lock"
    fd = None
    try:
        fd = open(lock_file, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
            except OSError:
                pass


def _write_registry(parent_session_id: str, data: Dict[str, Any]) -> None:
    """Write registry file atomically."""
    p = _registry_path(parent_session_id)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.error("[subagent-registry] failed to write %s: %s", p, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _is_placeholder_running_child(child_id: str, entry: Dict[str, Any]) -> bool:
    """Return True for CC-style placeholder rows that cannot be harvested."""
    if not isinstance(entry, dict):
        return False
    if str(entry.get("status") or "").strip() != "running":
        return False
    if str(entry.get("transcript_path") or "").strip():
        return False
    normalized_id = str(child_id or "").strip().lower()
    child_type = str(entry.get("child_type") or "").strip().lower()
    return normalized_id in _PLACEHOLDER_CHILD_IDS or child_type in _PLACEHOLDER_CHILD_IDS


def _prune_placeholder_running_children(children: Dict[str, Any], completed_child_id: str) -> List[str]:
    """Remove stale start-hook placeholders after a real completed child arrives."""
    removed: List[str] = []
    for child_id, entry in list(children.items()):
        if child_id == completed_child_id:
            continue
        if _is_placeholder_running_child(child_id, entry):
            children.pop(child_id, None)
            removed.append(child_id)
    return removed


def register(
    parent_session_id: str,
    child_id: str,
    child_transcript_path: Optional[str] = None,
    child_type: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Register a subagent as a child of a parent session.

    Called on SubagentStart (CC) or subagent_spawned (OC).
    """
    with _lock_registry(parent_session_id):
        data = _read_registry(parent_session_id)
        children = data.setdefault("children", {})
        existing = children.get(child_id, {})
        if not isinstance(existing, dict):
            existing = {}
        # B001: Apply metadata first so authoritative fields can't be overwritten
        entry = {**existing, **(metadata or {})}
        existing_transcript = str(existing.get("transcript_path") or "").strip()
        existing_status = str(existing.get("status") or "").strip()
        existing_completed_at = existing.get("completed_at")
        existing_registered_at = existing.get("registered_at")
        entry.update({
            "child_id": child_id,
            "parent_session_id": parent_session_id,
            "child_type": child_type or existing.get("child_type") or "unknown",
            "transcript_path": child_transcript_path or existing_transcript,
            "status": existing_status if existing_status == "complete" else "running",
            "harvested": bool(existing.get("harvested", False)),
            "registered_at": existing_registered_at or _now_iso_z(),
            "completed_at": existing_completed_at if existing_status == "complete" else None,
            "harvested_at": existing.get("harvested_at"),
        })
        children[child_id] = entry
        _write_registry(parent_session_id, data)
    logger.info(
        "[subagent-registry] registered child=%s parent=%s type=%s",
        child_id, parent_session_id, child_type,
    )


def mark_complete(
    parent_session_id: str,
    child_id: str,
    transcript_path: Optional[str] = None,
) -> None:
    """Mark a subagent as complete and harvestable.

    Called on SubagentStop (CC) or subagent_ended (OC).
    Updates transcript_path if provided (CC provides it at stop time).
    """
    with _lock_registry(parent_session_id):
        data = _read_registry(parent_session_id)
        children = data.get("children", {})
        if child_id not in children:
            # Late registration — create entry inline (don't call register() to avoid deadlock)
            children[child_id] = {
                "child_id": child_id,
                "parent_session_id": parent_session_id,
                "child_type": "unknown",
                "transcript_path": transcript_path or "",
                "status": "running",
                "harvested": False,
                "registered_at": _now_iso_z(),
                "completed_at": None,
                "harvested_at": None,
            }
            data.setdefault("children", children)

        child = children.get(child_id, {})
        child["status"] = "complete"
        child["completed_at"] = _now_iso_z()
        if transcript_path:
            child["transcript_path"] = transcript_path
        children[child_id] = child
        removed_placeholders = []
        if str(child.get("transcript_path") or "").strip():
            removed_placeholders = _prune_placeholder_running_children(children, child_id)
        _write_registry(parent_session_id, data)
    logger.info(
        "[subagent-registry] completed child=%s parent=%s",
        child_id, parent_session_id,
    )
    if removed_placeholders:
        logger.info(
            "[subagent-registry] pruned stale placeholder child rows parent=%s completed=%s placeholders=%s",
            parent_session_id,
            child_id,
            ",".join(removed_placeholders),
        )


def get_harvestable(parent_session_id: str) -> List[Dict[str, Any]]:
    """Get completed, un-harvested children for a parent session."""
    data = _read_registry(parent_session_id)
    children = data.get("children", {})
    return [
        c for c in children.values()
        if c.get("status") == "complete"
        and not c.get("harvested", False)
        and c.get("transcript_path")
    ]


def mark_harvested(parent_session_id: str, child_id: str) -> None:
    """Mark a child's transcript as harvested (extracted)."""
    with _lock_registry(parent_session_id):
        data = _read_registry(parent_session_id)
        children = data.get("children", {})
        if child_id in children:
            children[child_id]["harvested"] = True
            children[child_id]["harvested_at"] = _now_iso_z()
            _write_registry(parent_session_id, data)


def is_registered_subagent(session_id: str) -> bool:
    """Check if a session_id is a registered subagent child.

    Scans all registry files. Used by the daemon to suppress
    standalone timeout extraction for registered subagents.
    """
    try:
        for p in _registry_dir().glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                children = data.get("children", {})
                if session_id in children:
                    return True
            except (json.JSONDecodeError, OSError) as exc:
                if _fail_hard_enabled():
                    raise
                logger.warning("Skipping unreadable subagent registry file %s: %s", p, exc)
                continue
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("Failed scanning subagent registry directory: %s", exc)
    return False


def cleanup_old_registries(max_age_hours: float = 48.0) -> int:
    """Remove registry files older than max_age_hours. Returns count removed."""
    cutoff = _now_epoch() - (max_age_hours * 3600)
    removed = 0
    try:
        for p in _registry_dir().glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError as exc:
                if _fail_hard_enabled():
                    raise
                logger.warning("Failed checking/removing old subagent registry %s: %s", p, exc)
                continue
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("Failed listing subagent registry directory for cleanup: %s", exc)
    return removed
