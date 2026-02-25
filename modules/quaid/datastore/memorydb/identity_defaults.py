"""Datastore-owned default identity resolver and privacy policy.

These defaults provide deterministic, minimal behavior so multi-user mode has
safe baseline semantics before advanced policy logic is layered in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

from config import get_config


def default_identity_resolver(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)

    speaker = str(out.get("speaker_entity_id") or out.get("actor_id") or "").strip() or None
    if speaker and not out.get("speaker_entity_id"):
        out["speaker_entity_id"] = speaker
    if speaker and not out.get("actor_id"):
        out["actor_id"] = speaker

    conv = str(out.get("conversation_id") or out.get("source_conversation_id") or "").strip() or None
    if conv and not out.get("conversation_id"):
        out["conversation_id"] = conv
    if conv and not out.get("source_conversation_id"):
        out["source_conversation_id"] = conv

    if speaker and not out.get("subject_entity_id"):
        out["subject_entity_id"] = speaker

    if not out.get("visibility_scope"):
        channel = str(out.get("source_channel") or "").strip().lower()
        out["visibility_scope"] = "private_subject" if channel in {"dm", "direct", "private"} else "source_shared"

    if out.get("provenance_confidence") is None:
        out["provenance_confidence"] = 0.5

    return out


def _participants_from_row_or_ctx(row: Dict[str, Any], context: Dict[str, Any]) -> Set[str]:
    values: List[str] = []
    for raw in (
        row.get("participant_entity_ids"),
        context.get("participant_entity_ids"),
    ):
        if isinstance(raw, list):
            values.extend(str(v).strip() for v in raw if str(v).strip())
    return {v for v in values if v}


def default_privacy_policy(viewer_entity_id: str, row: Dict[str, Any], context: Dict[str, Any]) -> bool:
    viewer = str(viewer_entity_id or "").strip()
    if not viewer:
        return False

    scope = str(row.get("visibility_scope") or "source_shared").strip().lower()
    sensitivity = str(row.get("sensitivity") or "normal").strip().lower()
    subject = str(row.get("subject_entity_id") or "").strip()
    speaker = str(row.get("speaker_entity_id") or row.get("actor_id") or "").strip()
    owner = str(row.get("owner_id") or "").strip()
    participants = _participants_from_row_or_ctx(row, context)

    strict = True
    try:
        strict = bool(get_config().privacy.enforce_strict_filters)
    except Exception:
        strict = True

    if sensitivity in {"restricted", "secret"}:
        return viewer in {subject, speaker, owner}

    if scope == "private_subject":
        return viewer in {subject, owner}
    if scope == "source_shared":
        if viewer in {subject, speaker, owner}:
            return True
        return viewer in participants
    if scope == "global_shared":
        return True
    if scope == "system":
        return viewer.startswith("agent:")

    return False if strict else True

