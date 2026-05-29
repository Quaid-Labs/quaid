"""SessionDB plugin contract helpers.

This module owns SessionDB transcript/provenance helper logic and the
`session.ingest_log.request.v1` request handler.
"""

from __future__ import annotations

from typing import Any, Dict

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext


def record_session_lifecycle_observation(event: Dict[str, Any]) -> Dict[str, Any]:
    """Record SessionDB-owned lifecycle metadata for concrete sessions."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session_id = str(event.get("session_id") or payload.get("session_id") or "").strip()
    if not session_id:
        return {"status": "skipped", "persisted": False, "reason": "missing_session_id"}
    owner_id = str(event.get("owner_id") or payload.get("owner_id") or "default").strip() or "default"

    from datastore.sessiondb.session_store import record_lifecycle_observation

    return record_lifecycle_observation(event, owner_id=owner_id, session_id=session_id)


def run_session_ingest_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run session transcript ingest from the SessionDB-owned payload helper."""
    session_id = str(payload.get("session_id") or "").strip()
    owner_id = str(payload.get("owner_id") or "default").strip() or "default"
    label = str(payload.get("label") or "unknown").strip() or "unknown"
    session_file = payload.get("session_file")
    transcript_path = payload.get("transcript_path")
    source_channel = str(payload.get("source_channel") or "").strip() or None
    conversation_id = str(payload.get("conversation_id") or "").strip() or None
    participant_ids_raw = payload.get("participant_ids")
    participant_aliases_raw = payload.get("participant_aliases")
    participant_ids = participant_ids_raw if isinstance(participant_ids_raw, list) else []
    participant_aliases = participant_aliases_raw if isinstance(participant_aliases_raw, dict) else {}
    message_count = int(payload.get("message_count") or 0)
    topic_hint = str(payload.get("topic_hint") or "").strip()

    if not session_id:
        return {"status": "failed", "error": "payload.session_id is required"}

    from core.ingest_runtime import run_session_logs_ingest

    return run_session_logs_ingest(
        session_id=session_id,
        owner_id=owner_id,
        label=label,
        session_file=str(session_file) if session_file else None,
        transcript_path=str(transcript_path) if transcript_path else None,
        source_channel=source_channel,
        conversation_id=conversation_id,
        participant_ids=[str(p).strip() for p in participant_ids if str(p).strip()],
        participant_aliases={str(k): str(v) for k, v in participant_aliases.items() if str(k).strip()},
        message_count=message_count,
        topic_hint=topic_hint,
    )


def handle_session_ingest_log_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle synchronous session transcript ingest through SessionDB ownership."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return run_session_ingest_payload(payload)


def register_session_ingest_log_request_handler() -> None:
    from core.runtime.events import SESSION_INGEST_LOG_REQUEST_EVENT, register_request_handler

    register_request_handler(
        SESSION_INGEST_LOG_REQUEST_EVENT,
        handle_session_ingest_log_request,
        datastore_id="sessiondb",
        force=True,
    )


class SessionDbPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        _ = ctx

    def on_config(self, ctx: PluginHookContext) -> None:
        _ = ctx

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"datastore": "sessiondb", "ready": True}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"panel": "sessiondb", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        # SessionDB currently exposes lifecycle/provenance metadata only; no
        # periodic janitor task is active for this datastore.
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        return {"healthy": True, "status": self.on_status(ctx)}


_CONTRACT = SessionDbPluginContract()


def on_init(ctx: PluginHookContext) -> None:
    _CONTRACT.on_init(ctx)


def on_config(ctx: PluginHookContext) -> None:
    _CONTRACT.on_config(ctx)


def on_status(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_status(ctx)


def on_dashboard(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_dashboard(ctx)


def on_maintenance(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_maintenance(ctx)


def on_tool_runtime(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_tool_runtime(ctx)


def on_health(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_health(ctx)
