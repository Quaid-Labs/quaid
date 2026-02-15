#!/usr/bin/env python3
"""
User Notification Module

Channel-agnostic messaging to the user's last active channel.
Reads session state from OpenClaw and sends via CLI.

Usage:
  python3 notify.py "Your message here"
  python3 notify.py --check  # Just show last channel info

Programmatic:
  from notify import notify_user, get_last_channel

  info = get_last_channel()
  if info:
      notify_user("Doc updated: janitor-reference.md")
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Branding prefix for all notifications
QUAID_HEADER = "**[Quaid]**"


def _notify_full_text() -> bool:
    """Check if notifications should show full text (no truncation)."""
    try:
        from config import get_config
        return get_config().notifications.full_text
    except Exception:
        return False


def _check_janitor_health() -> Optional[str]:
    """Check if the janitor has run recently. Returns warning string or None.

    This is a lightweight DB check (single query, no LLM calls).
    Called during extraction notifications to alert the user if their
    janitor isn't running — e.g. misconfigured heartbeat, missing API key, etc.
    """
    try:
        from lib.config import get_db_path
        import sqlite3
        from datetime import datetime, timedelta

        db_path = get_db_path()
        if not os.path.exists(db_path):
            return None

        conn = sqlite3.connect(db_path, timeout=2)
        try:
            row = conn.execute(
                "SELECT MAX(completed_at) FROM janitor_runs WHERE status = 'completed'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # Table doesn't exist yet (fresh install)
        finally:
            conn.close()

        if not row or not row[0]:
            return (
                "⚠️ **Janitor has never run.** New memories are pending review.\n"
                "Make sure the janitor is scheduled in your HEARTBEAT.md.\n"
                "It must be triggered by your bot (which passes the API key)."
            )

        last_run = datetime.fromisoformat(row[0])
        hours_ago = (datetime.now() - last_run).total_seconds() / 3600

        if hours_ago > 72:
            return (
                f"⚠️ **Janitor hasn't run in {int(hours_ago)}h!** Memories are piling up.\n"
                "Check your HEARTBEAT.md schedule and ensure the bot can reach the API."
            )
        elif hours_ago > 48:
            return (
                f"⚠️ **Janitor last ran {int(hours_ago)}h ago.** "
                "Check your HEARTBEAT.md schedule if this is unexpected."
            )

        return None
    except Exception:
        return None  # Never crash extraction over a health check

# Session state location (OpenClaw standard path)
SESSIONS_FILE = Path.home() / ".clawdbot" / "agents" / "main" / "sessions" / "sessions.json"
# Alternative path for openclaw branding
SESSIONS_FILE_ALT = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"

# Main session key (Alfie's primary session)
MAIN_SESSION_KEY = "agent:main:main"


@dataclass
class ChannelInfo:
    """User's last active channel information."""
    channel: str      # telegram, whatsapp, discord, etc.
    target: str       # chat id, phone number, channel id
    account_id: str   # account identifier (usually "default")
    session_key: str  # session key for reference


def get_sessions_path() -> Optional[Path]:
    """Find the sessions.json file."""
    if SESSIONS_FILE.exists():
        return SESSIONS_FILE
    if SESSIONS_FILE_ALT.exists():
        return SESSIONS_FILE_ALT
    return None


def get_last_channel(session_key: str = MAIN_SESSION_KEY) -> Optional[ChannelInfo]:
    """
    Get the user's last active channel from session state.

    Args:
        session_key: Session to look up (default: main Alfie session)

    Returns:
        ChannelInfo if found, None otherwise
    """
    sessions_path = get_sessions_path()
    if not sessions_path:
        return None

    try:
        with open(sessions_path) as f:
            sessions = json.load(f)

        session = sessions.get(session_key)
        if not session:
            return None

        channel = session.get("lastChannel")
        target = session.get("lastTo")
        account_id = session.get("lastAccountId", "default")

        if not channel or not target:
            return None

        return ChannelInfo(
            channel=channel,
            target=target,
            account_id=account_id,
            session_key=session_key
        )
    except (json.JSONDecodeError, IOError) as e:
        print(f"[notify] Error reading sessions: {e}", file=sys.stderr)
        return None


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
    except Exception:
        pass
    return None


def notify_user(
    message: str,
    session_key: str = MAIN_SESSION_KEY,
    dry_run: bool = False,
    channel_override: Optional[str] = None,
) -> bool:
    """
    Send a notification to the user's last active channel (or a specific channel).

    Args:
        message: Message to send
        session_key: Session to look up for channel info
        dry_run: If True, print command but don't send
        channel_override: If set, send to this channel instead of the session's last channel.
            Use _resolve_channel(feature) to get from config.

    Returns:
        True if message sent successfully, False otherwise
    """
    if os.environ.get("QUAID_DISABLE_NOTIFICATIONS"):
        print(f"[notify] Notifications disabled (QUAID_DISABLE_NOTIFICATIONS set)", file=sys.stderr)
        return True

    info = get_last_channel(session_key)
    if not info:
        print("[notify] No last channel found", file=sys.stderr)
        return False

    # Use channel override if configured, otherwise session's last channel
    effective_channel = channel_override or info.channel

    cmd = [
        "clawdbot", "message", "send",
        "--channel", effective_channel,
        "--target", info.target,
        "--message", message
    ]

    if info.account_id and info.account_id != "default":
        cmd.extend(["--account", info.account_id])

    if dry_run:
        print(f"[notify] Would run: {' '.join(cmd)}")
        return True

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            print(f"[notify] Sent to {effective_channel}:{info.target}")
            return True
        else:
            print(f"[notify] Send failed: {result.stderr}", file=sys.stderr)
            return False

    except subprocess.TimeoutExpired:
        print("[notify] Send timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[notify] Error: {e}", file=sys.stderr)
        return False


def notify_doc_update(
    doc_path: str,
    trigger: str,
    summary: Optional[str] = None,
    dry_run: bool = False
) -> bool:
    """
    Notify user about a doc auto-update.

    Args:
        doc_path: Path to the updated doc
        trigger: What triggered the update (compact, janitor, manual)
        summary: Optional summary of changes
        dry_run: If True, don't actually send

    Returns:
        True if notification sent
    """
    # Extract just the filename for brevity
    doc_name = Path(doc_path).name

    # Map trigger to human-readable description
    trigger_desc = {
        "compact": "conversation compaction",
        "reset": "session reset",
        "janitor": "nightly maintenance",
        "manual": "manual request",
        "on-demand": "staleness detection",
    }.get(trigger, trigger)

    # Build message with header
    msg_parts = [
        f"{QUAID_HEADER} 📋 **Auto-Documentation System Report**",
        "",
        f"Updated: `{doc_name}`",
        f"Trigger: {trigger_desc}",
    ]

    if summary:
        # Truncate summary if too long (unless fullText enabled)
        if not _notify_full_text() and len(summary) > 200:
            summary = summary[:197] + "..."
        msg_parts.append(f"Changes: {summary}")

    msg_parts.append("")
    msg_parts.append("_This doc was auto-updated to stay in sync with code changes._")

    message = "\n".join(msg_parts)

    return notify_user(message, dry_run=dry_run)


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
        else:
            text = str(mem)
            similarity = 0
            via = "vector"

        # Clean up the text
        text = " ".join(text.split())
        if not full_text and len(text) > 120:
            text = text[:117] + "..."

        if via == "graph":
            # Graph discoveries always show (they're relationships, not similarity-ranked)
            graph_discoveries.append(text)
        elif similarity >= min_similarity:
            direct_matches.append((text, similarity))
        else:
            low_confidence_count += 1

    if not direct_matches and not graph_discoveries:
        # Nothing worth showing
        return False

    # Build notification message
    msg_parts = [f"{QUAID_HEADER} 🧠 **Memory Context Loaded:**", ""]

    if direct_matches:
        msg_parts.append("**Direct Matches:**")
        for text, similarity in direct_matches:
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
    if not details and facts_stored == 0 and edges_created == 0 and not has_snippets:
        return False  # Nothing to report

    full_text = _notify_full_text()
    msg_parts = [f"{QUAID_HEADER} 💾 **Memory Extraction:**", ""]

    # Trigger info
    trigger_label = {
        "compaction": "Context compacted",
        "reset": "Session reset (/new)",
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

    # Janitor health check — warn if janitor hasn't run recently
    janitor_warning = _check_janitor_health()
    if janitor_warning:
        msg_parts.append("")
        msg_parts.append(janitor_warning)

    message = "\n".join(msg_parts)
    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("extraction"))


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

    # Update alert — super visible at the end
    update_info = applied_changes.get("update_available")
    if isinstance(update_info, dict) and update_info.get("latest"):
        msg_parts.append("")
        msg_parts.append("⚠️⚠️⚠️ **UPDATE AVAILABLE** ⚠️⚠️⚠️")
        msg_parts.append(f"v{update_info['current']} → v{update_info['latest']}")
        msg_parts.append("")
        msg_parts.append("Update with:")
        msg_parts.append("`curl -fsSL https://raw.githubusercontent.com/rekall-inc/quaid/main/install.sh | bash`")
        msg_parts.append(f"Release notes: {update_info.get('url', '')}")

    message = "\n".join(msg_parts)
    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("janitor"))


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
    if not memories:
        return False

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

    message = "\n".join(msg_parts)
    return notify_user(message, dry_run=dry_run, channel_override=_resolve_channel("retrieval"))


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
        default=MAIN_SESSION_KEY,
        help=f"Session key (default: {MAIN_SESSION_KEY})"
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

    success = notify_user(args.message, args.session, args.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
