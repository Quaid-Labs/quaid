"""Subagent registry — tracks parent/child session relationships.

Adapter-agnostic registry that allows extraction to merge subagent
transcripts with their parent session. Both Claude Code and OpenClaw
adapters write to the same registry via lifecycle hooks.

Storage: JSON files per parent session under QUAID_HOME/data/subagent-registry/

Design principles:
  - Parent session owns memory; subagents are attached work units
  - Registered subagent sessions skip standalone timeout extraction
  - Parent extraction events collect completed subagent transcripts
  - Facts are attributed to the parent session lineage
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _registry_dir() -> Path:
    """Resolve registry directory from QUAID_HOME."""
    home = os.environ.get("QUAID_HOME", "").strip()
    base = Path(home).resolve() if home else Path.home() / "quaid"
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
    data = _read_registry(parent_session_id)
    children = data.setdefault("children", {})
    children[child_id] = {
        "child_id": child_id,
        "parent_session_id": parent_session_id,
        "child_type": child_type or "unknown",
        "transcript_path": child_transcript_path or "",
        "status": "running",
        "harvested": False,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed_at": None,
        "harvested_at": None,
        **(metadata or {}),
    }
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
    data = _read_registry(parent_session_id)
    children = data.get("children", {})
    if child_id not in children:
        # Late registration — register + complete in one shot
        register(parent_session_id, child_id, transcript_path)
        data = _read_registry(parent_session_id)
        children = data.get("children", {})

    child = children.get(child_id, {})
    child["status"] = "complete"
    child["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if transcript_path:
        child["transcript_path"] = transcript_path
    children[child_id] = child
    _write_registry(parent_session_id, data)
    logger.info(
        "[subagent-registry] completed child=%s parent=%s",
        child_id, parent_session_id,
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
    data = _read_registry(parent_session_id)
    children = data.get("children", {})
    if child_id in children:
        children[child_id]["harvested"] = True
        children[child_id]["harvested_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
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
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return False


def cleanup_old_registries(max_age_hours: float = 48.0) -> int:
    """Remove registry files older than max_age_hours. Returns count removed."""
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    try:
        for p in _registry_dir().glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
