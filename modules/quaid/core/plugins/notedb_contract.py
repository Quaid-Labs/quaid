"""NoteDB datastore plugin contract hooks."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext

logger = logging.getLogger(__name__)


def _fail_hard_enabled() -> bool:
    from lib.fail_policy import is_fail_hard_enabled

    return is_fail_hard_enabled()


def _logical_snippet_target(filename: str) -> str:
    return f"{str(filename).removesuffix('.md')}.snippets.md"


def _logical_journal_target(filename: str) -> str:
    return f"{str(filename).removesuffix('.md')}.journal.md"


def run_snippet_journal_write_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist extraction snippet/journal payloads through the EvolutionDB seam."""
    if not isinstance(payload, dict):
        raise TypeError("snippet/journal write payload must be an object")
    source = str(payload.get("source") or "").strip()
    if source and source != "extraction-apply-payloads":
        raise ValueError("payload.source must be extraction-apply-payloads")

    snippets = payload.get("snippets") or {}
    journal = payload.get("journal") or {}
    if not isinstance(snippets, dict):
        raise TypeError("payload.snippets must be an object")
    if not isinstance(journal, dict):
        raise TypeError("payload.journal must be an object")

    trigger = str(payload.get("trigger") or "CLI").strip() or "CLI"
    date_str = str(payload.get("date_str") or "").strip() or None
    time_str = str(payload.get("time_str") or "").strip() or None
    write_snippets = bool(payload.get("write_snippets", True))
    write_journal = bool(payload.get("write_journal", True))
    dry_run = bool(payload.get("dry_run", False))

    result: Dict[str, Any] = {
        "status": "ok",
        "snippet_files_seen": 0,
        "snippet_items_seen": 0,
        "snippet_files_written": 0,
        "snippet_items_written": 0,
        "snippet_files_skipped": 0,
        "journal_files_seen": 0,
        "journal_files_written": 0,
        "journal_files_skipped": 0,
        "target_files": {"snippets": [], "journal": []},
        "errors": [],
    }

    from datastore.notedb import soul_snippets

    snippet_targets: List[str] = result["target_files"]["snippets"]
    journal_targets: List[str] = result["target_files"]["journal"]

    for filename, items in snippets.items():
        if not isinstance(items, list):
            continue
        valid = [str(item).strip() for item in items if isinstance(item, str) and str(item).strip()]
        if not valid:
            continue
        result["snippet_files_seen"] += 1
        result["snippet_items_seen"] += len(valid)
        snippet_targets.append(_logical_snippet_target(str(filename)))
        if not write_snippets or dry_run:
            result["snippet_files_skipped"] += 1
            continue
        try:
            written = soul_snippets.write_snippet_entry(
                str(filename),
                valid,
                trigger=trigger,
                date_str=date_str,
                time_str=time_str,
            )
        except Exception as exc:
            message = f"snippet write failed for {filename}: {exc}"
            logger.warning("[notedb] %s", message)
            if _fail_hard_enabled():
                raise
            result["status"] = "failed"
            result["errors"].append(message)
            result["snippet_files_skipped"] += 1
            continue
        if written:
            result["snippet_files_written"] += 1
            result["snippet_items_written"] += len(valid)
        else:
            result["snippet_files_skipped"] += 1

    for filename, text in journal.items():
        if not isinstance(text, str) or not text.strip():
            continue
        result["journal_files_seen"] += 1
        journal_targets.append(_logical_journal_target(str(filename)))
        if not write_journal or dry_run:
            result["journal_files_skipped"] += 1
            continue
        try:
            written = soul_snippets.write_journal_entry(
                str(filename),
                text.strip(),
                trigger=trigger,
                date_str=date_str,
            )
        except Exception as exc:
            message = f"journal write failed for {filename}: {exc}"
            logger.warning("[notedb] %s", message)
            if _fail_hard_enabled():
                raise
            result["status"] = "failed"
            result["errors"].append(message)
            result["journal_files_skipped"] += 1
            continue
        if written:
            result["journal_files_written"] += 1
        else:
            result["journal_files_skipped"] += 1

    return result


class NoteDbPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        _ = ctx

    def on_config(self, ctx: PluginHookContext) -> None:
        _ = ctx

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"datastore": "notedb", "ready": True}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"panel": "notedb", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"healthy": True}


_CONTRACT = NoteDbPluginContract()


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
