#!/usr/bin/env python3
from __future__ import annotations

"""
User Notification Module

Channel-agnostic messaging to the user via the platform adapter.

Usage:
  python3 notify.py "Your message here"
  python3 notify.py --check  # Just show last channel info

Programmatic:
  from core.runtime.notify import notify_user, get_last_channel

  info = get_last_channel()
  if info:
      notify_user("Doc updated: janitor-reference.md")
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_THIS_FILE = Path(__file__).resolve()
_MODULE_ROOT = _THIS_FILE.parents[2]  # .../modules/quaid
if str(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULE_ROOT))

from lib.adapter import ChannelInfo
from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import (
    get_install_url,
    get_last_channel as _ctx_get_last_channel,
    send_notification as _ctx_send_notification,
)

# Branding prefix for all notifications
QUAID_HEADER = "**[Quaid]**"
MAX_NOTIFY_CHARS = 3500
logger = logging.getLogger(__name__)


def send_direct_notification(
    message: str,
    *,
    channel: str,
    target: str,
    account: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """Send via message CLI directly, bypassing adapter/session lookup."""
    if os.environ.get("QUAID_DISABLE_NOTIFICATIONS"):
        return True
    cli = shutil.which("clawdbot") or shutil.which("openclaw")
    if not cli:
        print("[notify] No message CLI found (expected openclaw or clawdbot)", file=sys.stderr)
        return False
    cmd = [
        cli,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        message,
    ]
    if account:
        cmd.extend(["--account", account])
    if dry_run:
        print(f"[notify] Would run: {' '.join(cmd)}")
        return True
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        print(f"[notify] Sent to {channel}:{target}")
        return True
    err = (result.stderr or result.stdout or "").strip()
    print(f"[notify] Send failed: {err}", file=sys.stderr)
    return False


def _notify_full_text() -> bool:
    """Check if notifications should show full text (no truncation)."""
    try:
        from config import get_config
        return get_config().notifications.full_text
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to read notifications.full_text while fail-hard mode is enabled"
            ) from exc
        logger.warning("Failed to read notifications.full_text config: %s", exc)
        return False


def _check_janitor_health() -> Optional[str]:
    """Check if the janitor has run recently. Returns warning string or None.

    This is a lightweight DB check (single query, no LLM calls).
    Called during extraction notifications to alert the user if their
    janitor isn't running — e.g. misconfigured heartbeat, missing API key, etc.
    """
    try:
        from datetime import datetime, timedelta
        from core.lifecycle.datastore_runtime import (
            get_graph,
            get_last_successful_janitor_completed_at,
        )

        last_completed_at = get_last_successful_janitor_completed_at(get_graph())
        if not last_completed_at:
            return (
                "⚠️ **Janitor has never run.** New memories are pending review.\n"
                "Make sure the janitor is scheduled in your HEARTBEAT.md.\n"
                "It must be triggered by your bot."
            )

        last_run = datetime.fromisoformat(last_completed_at)
        hours_ago = (datetime.now() - last_run).total_seconds() / 3600

        if hours_ago > 72:
            return (
                f"⚠️ **Janitor hasn't run in {int(hours_ago)}h!** Memories are piling up.\n"
                "Check your HEARTBEAT.md schedule and ensure the bot can reach Quaid."
            )
        elif hours_ago > 48:
            return (
                f"⚠️ **Janitor last ran {int(hours_ago)}h ago.** "
                "Check your HEARTBEAT.md schedule if this is unexpected."
            )

        return None
    except Exception as exc:
        logger.warning("Failed to evaluate janitor health status: %s", exc)
        return (
            "⚠️ **Unable to verify janitor health.**\n"
            "Please check database/runtime status and your HEARTBEAT.md schedule."
        )

def get_last_channel(session_key: str = "") -> Optional[ChannelInfo]:
    """Get the user's last active channel from session state (delegates to adapter)."""
    return _ctx_get_last_channel(session_key)


def _resolve_channel(feature: Optional[str] = None) -> Optional[str]:
    """Get the configured channel override for a notification feature.
    Returns None for 'last_used' (use session default), or a specific channel name."""
    if not feature:
        return None
    try:
        from config import get_config
        channel = get_config().notifications.effective_channel(feature)
        if channel and channel != "last_used":
            return channel
    except Exception as exc:
        logger.debug("Failed to resolve notification channel override for feature '%s': %s", feature, exc)
    return None


def notify_user(
    message: str,
    session_key: str = "",
    dry_run: bool = False,
    channel_override: Optional[str] = None,
) -> bool:
    """Send a notification to the user (delegates to adapter).

    Args:
        message: Message to send
        session_key: Ignored (adapter manages sessions)
        dry_run: If True, print command but don't send
        channel_override: If set, send to this channel instead of the session's last channel.

    Returns:
        True if message sent successfully, False otherwise
    """
    return _ctx_send_notification(message, channel_override=channel_override, dry_run=dry_run)


def notify_memory_recall(
    memories: list,
    min_similarity: int = 70,
    dry_run: bool = False,
    source_breakdown: Optional[dict] = None
) -> bool:
    """
    Notify user about memories being injected into context.

    Args:
        memories: List of memory dicts with 'text', 'similarity', and optionally 'via' fields
        min_similarity: Only show memories with similarity >= this (default 70%)
        dry_run: If True, don't actually send
        source_breakdown: Optional dict with vector_count, graph_count, pronoun_resolved, owner_person

    Returns:
        True if notification sent
    """
    if not memories:
        return False

    mode = ""
    if source_breakdown:
        mode = str(source_breakdown.get("mode", "")).strip().lower()
    effective_min_similarity = 0 if mode in {"auto_inject", "tool", "tool_call", "manual"} else min_similarity

    # Separate direct matches from graph discoveries
    direct_matches = []
    graph_discoveries = []
    low_confidence_count = 0
    full_text = _notify_full_text()

    for mem in memories:
        if isinstance(mem, dict):
            text = mem.get("text", str(mem))
            similarity = mem.get("similarity", 0)
            via = mem.get("via", "vector")
            category = str(mem.get("category", "")).strip().lower()
        else:
            text = str(mem)
            similarity = 0
            via = "vector"
            category = ""

        # Clean up the text
        text = " ".join(text.split())
        if not full_text and len(text) > 120:
            text = text[:117] + "..."

        if via == "graph":
            # Graph discoveries always show (they're relationships, not similarity-ranked)
            graph_discoveries.append(text)
        elif similarity >= effective_min_similarity:
            label = ""
            # Label graph-origin node hits explicitly; do not relabel normal vector facts/events.
            if via == "graph" and category in {"person", "concept", "event", "entity"}:
                label = f"graph-node/{category}"
            elif category and category not in {"fact", "preference", "decision", "relationship", "other"}:
                label = f"node/{category}"
            direct_matches.append((text, similarity, label))
        else:
            low_confidence_count += 1

    if not direct_matches and not graph_discoveries:
        # Nothing worth showing
        return False

    # Build notification message
    msg_parts = [f"{QUAID_HEADER} 🧠 **Memory Context Loaded:**", ""]

    if direct_matches:
        msg_parts.append("**Direct Matches:**")
        for text, similarity, label in direct_matches:
            if label:
                msg_parts.append(f"• [{similarity}%] [{label}] {text}")
            else:
                msg_parts.append(f"• [{similarity}%] {text}")

    if graph_discoveries:
        if direct_matches:
            msg_parts.append("")
        msg_parts.append("**Graph Discoveries:**")
        for text in graph_discoveries:
            msg_parts.append(f"• {text}")

    # Add source breakdown if provided
    if source_breakdown:
        msg_parts.append("")
        vector_count = source_breakdown.get("vector_count", len(direct_matches))
        graph_count = source_breakdown.get("graph_count", len(graph_discoveries))
        pronoun_resolved = source_breakdown.get("pronoun_resolved", False)
        query = source_breakdown.get("query", "")
        mode_label = ""
        if mode == "auto_inject":
            mode_label = "auto-inject"
        elif mode in ("tool", "tool_call", "manual"):
            mode_label = "tool recall"
        elif mode:
            mode_label = mode.replace("_", "-")
        if mode_label:
            msg_parts.append(f"_Mode: {mode_label}_")

        if query:
            msg_parts.append(f"_Query: \"{query}\"_")

        sources_line = f"_Sources: {vector_count} vector, {graph_count} graph"
        if pronoun_resolved:
            owner_person = source_breakdown.get("owner_person", "")
            if owner_person:
                sources_line += f" | Pronoun resolved: {owner_person}"
            else:
                sources_line += " | Pronoun: ✓"
        sources_line += "_"
        msg_parts.append(sources_line)
    elif low_confidence_count > 0:
        msg_parts.append("")
        msg_parts.append(f"_({low_confidence_count} low-confidence facts filtered)_")

    message = "\n".join(msg_parts)

    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("retrieval"))


def notify_docs_search(
    query: str,
    results: list,
    dry_run: bool = False
) -> bool:
    """
    Notify user about docs being searched.

    Args:
        query: The search query used
        results: List of result dicts with 'doc', 'section', 'score' fields
        dry_run: If True, don't actually send

    Returns:
        True if notification sent
    """
    if not results:
        return False

    msg_parts = [f"{QUAID_HEADER} 📚 **Docs Search Results:**", ""]
    msg_parts.append(f"_Query: \"{query}\"_")
    msg_parts.append("")

    for r in results[:5]:  # Limit to top 5
        doc = r.get("doc", "unknown")
        section = r.get("section", "")
        score = r.get("score", 0)

        # Clean up section title
        if section:
            section = section[:60] + "..." if len(section) > 60 else section
            msg_parts.append(f"• `{doc}` — {section} ({int(score*100)}%)")
        else:
            msg_parts.append(f"• `{doc}` ({int(score*100)}%)")

    if len(results) > 5:
        msg_parts.append(f"_...and {len(results) - 5} more_")

    message = "\n".join(msg_parts)
    return notify_user(message, dry_run=dry_run)


def notify_memory_extraction(
    facts_stored: int,
    facts_skipped: int,
    edges_created: int,
    trigger: str = "extraction",
    details: Optional[list] = None,
    dry_run: bool = False,
    snippet_details: Optional[dict] = None,
    always_notify: bool = False,
) -> bool:
    """
    Notify user about memories extracted from conversation.

    Args:
        facts_stored: Number of facts successfully stored
        facts_skipped: Number of facts skipped (duplicates, too short, etc.)
        edges_created: Number of relationship edges created
        trigger: What triggered extraction ("compaction", "reset", etc.)
        details: Optional list of fact details [{text, status, reason?, edges?}]
        dry_run: If True, don't actually send
        snippet_details: Optional dict of soul snippets {filename: [snippet_text, ...]}

    Returns:
        True if notification sent
    """
    has_snippets = snippet_details and any(v for v in snippet_details.values())
    try:
        from config import get_config
        extraction_level = get_config().notifications.effective_level("extraction")
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to resolve extraction notification level while fail-hard mode is enabled"
            ) from exc
        extraction_level = "summary"
    if extraction_level == "off":
        return False
    show_trigger = extraction_level == "full"
    no_results = (facts_stored == 0 and facts_skipped == 0 and edges_created == 0 and not details and not has_snippets)
    if not always_notify and not details and facts_stored == 0 and edges_created == 0 and not has_snippets:
        return False  # Nothing to report

    if always_notify and no_results:
        if show_trigger:
            trigger_label = {
                "compaction": "compaction",
                "reset": "reset",
                "timeout": "timeout",
                "extraction": "extraction",
                "new": "new",
            }.get(trigger, trigger)
            message = f"{QUAID_HEADER} 💾 Memory Extraction ({trigger_label}): No facts found."
        else:
            message = f"{QUAID_HEADER} 💾 Memory Extraction: No facts found."
        return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("extraction"))

    full_text = _notify_full_text()
    msg_parts = [f"{QUAID_HEADER} 💾 **Memory Extraction:**", ""]

    # Trigger info (full verbosity only)
    if show_trigger:
        trigger_label = {
            "compaction": "Context compacted",
            "reset": "Session reset (/new)",
            "timeout": "Inactivity timeout",
            "extraction": "Extraction complete"
        }.get(trigger, trigger)
        msg_parts.append(f"_Trigger: {trigger_label}_")
        msg_parts.append("")

    # Summary stats
    msg_parts.append(f"**Summary:** {facts_stored} stored, {facts_skipped} skipped, {edges_created} edges")
    msg_parts.append("")

    # Detailed fact list
    if details:
        for i, fact in enumerate(details, 1):
            text = fact.get("text", "")
            status = fact.get("status", "unknown")
            reason = fact.get("reason", "")
            edges = fact.get("edges", [])

            # Truncate text for display (unless fullText enabled)
            display_text = text if full_text else (text[:80] + "..." if len(text) > 80 else text)

            # Status emoji
            if status == "stored":
                emoji = "✅"
            elif status == "updated":
                emoji = "🔄"
            elif status == "duplicate":
                emoji = "⏭️"
            elif status == "skipped":
                emoji = "⚠️"
            else:
                emoji = "❌"

            line = f"{emoji} {display_text}"

            # Add reason for skipped/duplicate
            if status == "duplicate" and reason:
                line += f"\n   ↳ _dup of: {reason}_"
            elif status == "skipped" and reason:
                line += f" _({reason})_"

            # Add edges
            if edges:
                for edge in edges:
                    line += f"\n   📎 {edge}"

            msg_parts.append(line)
            msg_parts.append("")

    # Journal entries section
    if has_snippets:
        msg_parts.append("✨ **Journal Entries:**")
        msg_parts.append("")
        for filename, snippets in snippet_details.items():
            if snippets:
                entry_word = "entry" if len(snippets) == 1 else "entries"
                msg_parts.append(f"**{filename}** ({len(snippets)} {entry_word}):")
                for s in snippets:
                    display = s if full_text else (s[:120] + "..." if len(s) > 120 else s)
                    msg_parts.append(f"  📝 {display}")
                msg_parts.append("")

    message = "\n".join(msg_parts)
    channel = _resolve_channel("extraction")
    if len(message) <= MAX_NOTIFY_CHARS:
        return notify_user(message, dry_run=dry_run, channel_override=channel)

    # Some channels drop oversized messages; split so extraction details still arrive.
    lines = message.split("\n")
    chunks = []
    current_lines = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_lines and (current_len + line_len > MAX_NOTIFY_CHARS):
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += line_len
    if current_lines:
        chunks.append("\n".join(current_lines))

    sent_any = False
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            chunk = (
                f"{QUAID_HEADER} 💾 **Memory Extraction (cont. {idx + 1}/{len(chunks)}):**\n\n"
                f"{chunk}"
            )
        if notify_user(chunk, dry_run=dry_run, channel_override=channel):
            sent_any = True
    return sent_any


def notify_janitor_summary(
    metrics: dict,
    applied_changes: dict,
    dry_run: bool = False
) -> bool:
    """
    Notify user about janitor run summary.

    Args:
        metrics: Dict with total_duration_seconds, llm_calls, errors, task_durations
        applied_changes: Dict with counts of changes made
        dry_run: If True, don't actually send

    Returns:
        True if notification sent
    """
    message = format_janitor_summary_message(metrics, applied_changes)
    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("janitor"))


def format_janitor_summary_message(metrics: dict, applied_changes: dict) -> str:
    """Format janitor summary content for either direct or delayed delivery."""
    msg_parts = [f"{QUAID_HEADER} 🧹 **Nightly Janitor Complete:**", ""]

    # Duration and stats
    duration = metrics.get("total_duration_seconds", 0)
    if duration >= 60:
        msg_parts.append(f"⏱️ Duration: {duration/60:.1f}min")
    else:
        msg_parts.append(f"⏱️ Duration: {duration:.0f}s")

    llm_calls = metrics.get("llm_calls", 0)
    if llm_calls > 0:
        msg_parts.append(f"🤖 LLM calls: {llm_calls}")

    errors = metrics.get("errors", 0)
    if errors > 0:
        msg_parts.append(f"❌ Errors: {errors}")

    msg_parts.append("")

    # Changes applied
    msg_parts.append("**Changes:**")
    change_labels = {
        "reviewed": "📝 Reviewed",
        "kept": "✅ Kept",
        "deleted": "🗑️ Deleted",
        "fixed": "🔧 Fixed",
        "merged": "🔀 Merged",
        "edges_created": "📎 Edges created",
        "contradictions_found": "⚡ Contradictions",
        "duplicates_rejected": "⏭️ Duplicates rejected",
        "decayed": "📉 Decayed",
    }

    has_changes = False
    for key, label in change_labels.items():
        count = applied_changes.get(key, 0)
        if count and count > 0:
            msg_parts.append(f"• {label}: {count}")
            has_changes = True

    if not has_changes:
        msg_parts.append("• No changes applied")

    # Contradiction decisions are always shown in full detail, regardless of verbosity.
    contradiction_findings = applied_changes.get("contradiction_findings") or []
    contradiction_decisions = applied_changes.get("contradiction_decisions") or []
    if contradiction_findings or contradiction_decisions:
        msg_parts.append("")
        msg_parts.append("**Contradiction Details (full):**")
        for f in contradiction_findings[:10]:
            if not isinstance(f, dict):
                continue
            msg_parts.append(f"• Found: \"{f.get('text_a', '')}\" ↔ \"{f.get('text_b', '')}\"")
            msg_parts.append(f"  Reason: {f.get('reason', '')}")
        for d in contradiction_decisions[:15]:
            if not isinstance(d, dict):
                continue
            msg_parts.append(f"• Decision: {d.get('action', 'UNKNOWN')}")
            msg_parts.append(f"  A: {d.get('text_a', '')}")
            msg_parts.append(f"  B: {d.get('text_b', '')}")
            msg_parts.append(f"  Why: {d.get('reason', '')}")

    # Update alert — super visible at the end
    update_info = applied_changes.get("update_available")
    if isinstance(update_info, dict) and update_info.get("latest"):
        msg_parts.append("")
        msg_parts.append("⚠️⚠️⚠️ **UPDATE AVAILABLE** ⚠️⚠️⚠️")
        msg_parts.append(f"v{update_info['current']} → v{update_info['latest']}")
        msg_parts.append("")
        msg_parts.append("Update with:")
        msg_parts.append(f"`curl -fsSL {get_install_url()} | bash`")
        msg_parts.append(f"Release notes: {update_info.get('url', '')}")

    return "\n".join(msg_parts)


def notify_daily_memories(
    memories: list,
    dry_run: bool = False
) -> bool:
    """
    Notify user about memories created today.

    Args:
        memories: List of memory dicts with 'text', 'category', 'created_at'
        dry_run: If True, don't actually send

    Returns:
        True if notification sent
    """
    message = format_daily_memories_message(memories)
    if not message:
        return False
    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("retrieval"))


def format_daily_memories_message(memories: list) -> Optional[str]:
    """Format daily memories digest for either direct or delayed delivery."""
    if not memories:
        return None

    full_text = _notify_full_text()
    msg_parts = [f"{QUAID_HEADER} 📚 **Today's New Memories:**", ""]
    msg_parts.append(f"_{len(memories)} memories added today_")
    msg_parts.append("")

    # Group by category
    by_category: dict = {}
    for mem in memories:
        cat = mem.get("category", "fact")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(mem)

    # Category emojis
    cat_emoji = {
        "fact": "📌",
        "preference": "💡",
        "relationship": "👥",
        "decision": "🎯",
        "other": "📝"
    }

    for cat, mems in by_category.items():
        emoji = cat_emoji.get(cat, "📝")
        msg_parts.append(f"**{emoji} {cat.title()}** ({len(mems)})")
        for mem in mems[:10]:  # Limit per category
            text = mem.get("text", "")
            # Truncate (unless fullText enabled)
            if not full_text and len(text) > 100:
                text = text[:97] + "..."
            msg_parts.append(f"• {text}")
        if len(mems) > 10:
            msg_parts.append(f"  _...and {len(mems) - 10} more_")
        msg_parts.append("")

    return "\n".join(msg_parts)


def main():
    parser = argparse.ArgumentParser(
        description="Send notifications to user's last active channel"
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Message to send"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Just show last channel info, don't send"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print command but don't send"
    )
    parser.add_argument(
        "--session",
        default="",
        help="Session key (default: adapter-managed)"
    )
    parser.add_argument(
        "--channel",
        default="",
        help="Send directly to this channel (bypass last-channel lookup)"
    )
    parser.add_argument(
        "--target",
        default="",
        help="Direct channel target (chat/channel id). Required with --channel unless env provides fallback."
    )
    parser.add_argument(
        "--account",
        default="",
        help="Optional account id for direct send"
    )

    args = parser.parse_args()

    if args.check:
        info = get_last_channel(args.session)
        if info:
            print(f"Channel: {info.channel}")
            print(f"Target: {info.target}")
            print(f"Account: {info.account_id}")
            print(f"Session: {info.session_key}")
        else:
            print("No last channel found")
            sys.exit(1)
        return

    if not args.message:
        parser.error("Message required (or use --check)")

    if args.channel:
        direct_target = args.target
        if not direct_target and args.channel.lower() == "telegram":
            direct_target = (
                os.environ.get("QUAID_TELEGRAM_TARGET")
                or os.environ.get("QUAID_NOTIFY_TARGET")
                or os.environ.get("TELEGRAM_TARGET")
                or ""
            ).strip()
        if not direct_target:
            parser.error("--target is required for direct sends (or set QUAID_TELEGRAM_TARGET for --channel telegram)")
        success = send_direct_notification(
            args.message,
            channel=args.channel,
            target=direct_target,
            account=(args.account or None),
            dry_run=args.dry_run,
        )
    else:
        success = notify_user(args.message, args.session, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
