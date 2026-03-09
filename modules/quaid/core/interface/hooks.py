#!/usr/bin/env python3
"""Quaid hook entry points — adapter-agnostic lifecycle integration.

Generic hook handlers invoked by host platforms (Claude Code, OpenClaw, etc.)
via the quaid CLI. Reads JSON from stdin, writes to stdout/stderr.

Hook commands:
    inject          Recall memories for a user message (stdin: JSON with "prompt")
    inject-compact  Re-inject critical memories after compaction
    extract         Extract knowledge from a conversation transcript
    search          Interactive search across memories + docs
    session-init    Collect and output project docs for session start injection

Usage:
    quaid hook-inject             (reads JSON from stdin)
    quaid hook-inject-compact     (reads JSON from stdin)
    quaid hook-extract [--precompact]  (reads JSON from stdin)
    quaid hook-search "query"
    quaid hook-session-init       (outputs project context to stdout)
"""

import argparse
import glob as glob_mod
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


from lib.adapter import get_owner_id as _get_owner_id


def _format_memories(memories: List[Dict]) -> str:
    """Format recalled memories as readable context text."""
    if not memories:
        return ""
    lines = ["[Quaid Memory Context]"]
    for i, mem in enumerate(memories, 1):
        text = mem.get("text", "")
        sim = mem.get("similarity", 0)
        category = mem.get("category", "fact")
        lines.append(f"  {i}. [{category}] {text} (relevance: {sim:.2f})")
    return "\n".join(lines)


def hook_inject(args):
    """Recall memories for each user message and inject as context.

    Reads hook JSON from stdin:
        {"prompt": "...", "cwd": "...", "session_id": "..."}

    Writes to stdout:
        {"additionalContext": "..."} (for hosts that support structured output)
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    query = hook_input.get("prompt", "").strip()
    if not query:
        return

    try:
        from core.interface.api import recall
        owner = _get_owner_id()
        memories = recall(
            query=query,
            owner_id=owner,
            limit=10,
            use_reranker=False,  # Keep hook fast
        )
        if not memories:
            return

        context = _format_memories(memories)
        print(json.dumps({"additionalContext": context}))
    except Exception as e:
        print(f"[quaid][hook-inject] error: {e}", file=sys.stderr)


def hook_inject_compact(args):
    """Re-inject critical memories after context compaction.

    Reads hook JSON from stdin:
        {"cwd": "...", "session_id": "..."}

    Writes plain text to stdout.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    cwd = hook_input.get("cwd", os.getcwd())

    try:
        from core.interface.api import recall
        owner = _get_owner_id()
        # No user message available — recall based on workspace context
        memories = recall(
            query=f"project context for {cwd}",
            owner_id=owner,
            limit=10,
            use_reranker=False,
        )
        if memories:
            print(_format_memories(memories))
    except Exception as e:
        print(f"[quaid][hook-inject-compact] error: {e}", file=sys.stderr)


def hook_extract(args):
    """Write an extraction signal for the daemon to process.

    Reads hook JSON from stdin:
        {"transcript_path": "...", "session_id": "...", "cwd": "..."}

    Instead of extracting directly, writes a signal file to the
    extraction-signals directory. The daemon processes signals
    asynchronously, handling cursors, chunking, and carryover.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    transcript_path = hook_input.get("transcript_path", "")
    session_id = hook_input.get("session_id", "") or f"unknown-{int(time.time())}-{os.getpid()}"
    is_precompact = args.precompact if hasattr(args, "precompact") else False
    signal_type = "compaction" if is_precompact else "session_end"
    label = f"hook-{signal_type}"

    if not transcript_path:
        print(f"[quaid][{label}] no transcript_path in hook input", file=sys.stderr)
        return

    transcript_path = os.path.expanduser(transcript_path)
    if not os.path.isfile(transcript_path):
        print(f"[quaid][{label}] transcript not found: {transcript_path}", file=sys.stderr)
        return

    try:
        from core.extraction_daemon import write_signal, ensure_alive

        # Ensure daemon is running (launch if not)
        try:
            ensure_alive()
        except Exception as e:
            print(f"[quaid][{label}] daemon ensure_alive failed: {e}", file=sys.stderr)

        # Determine adapter type from config for compaction control advertisement
        try:
            from lib.adapter import get_adapter
            adapter = get_adapter()
            adapter_name = type(adapter).__name__.replace("Adapter", "").lower()
        except Exception:
            adapter_name = "unknown"
        # OC can force compaction; CC cannot
        supports_compaction = adapter_name in ("openclaw",)

        sig_path = write_signal(
            signal_type=signal_type,
            session_id=session_id,
            transcript_path=transcript_path,
            adapter=adapter_name,
            supports_compaction_control=supports_compaction,
        )
        print(f"[quaid][{label}] signal written: {sig_path.name}", file=sys.stderr)
    except Exception as e:
        print(f"[quaid][{label}] error: {e}", file=sys.stderr)


def _check_janitor_health() -> str:
    """Check if the janitor has run recently. Returns a warning string or empty."""
    try:
        from lib.adapter import get_adapter
        logs_dir = get_adapter().logs_dir()
        # Janitor writes per-task checkpoints; check the 'all' task as primary
        checkpoint = logs_dir / "janitor" / "checkpoint-all.json"
        if not checkpoint.is_file():
            # Fall back to any checkpoint file
            janitor_dir = logs_dir / "janitor"
            if janitor_dir.is_dir():
                checkpoints = sorted(janitor_dir.glob("checkpoint-*.json"))
                if checkpoints:
                    checkpoint = checkpoints[-1]
                else:
                    return "[Quaid Warning] Janitor has never run. Run: quaid janitor --task all --apply"
            else:
                return "[Quaid Warning] Janitor has never run. Run: quaid janitor --task all --apply"

        import json as _json
        data = _json.loads(checkpoint.read_text(encoding="utf-8"))
        last_ts = data.get("last_completed_at", "")
        if not last_ts:
            return "[Quaid Warning] Janitor has never completed successfully."

        from datetime import datetime, timezone
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        if age_hours > 24:
            age_display = f"{age_hours / 24:.0f} days" if age_hours > 48 else f"{age_hours:.0f} hours"
            return f"[Quaid Warning] Janitor last ran {age_display} ago. Stale janitor causes memory/doc drift. Run: quaid janitor --task all --apply"
    except Exception:
        pass
    return ""


def _get_projects_dir() -> Path:
    """Resolve the projects directory from adapter."""
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        return adapter.projects_dir()
    except Exception:
        home = os.environ.get("QUAID_HOME", "").strip()
        base = Path(home).resolve() if home else Path.home() / "quaid"
        return base / "projects"


def _get_identity_dir() -> Path:
    """Resolve the per-instance identity directory from adapter."""
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        return adapter.identity_dir()
    except Exception:
        # Fallback: quaid_home root (backward compat with standalone)
        home = os.environ.get("QUAID_HOME", "").strip()
        return Path(home).resolve() if home else Path.home() / "quaid"


def hook_session_init(args):
    """Collect project docs and write to .claude/rules/ for durable caching.

    Claude Code auto-loads .claude/rules/*.md into context at session start,
    caches them via prompt caching, and preserves them through compaction.
    This is more reliable than injecting via additionalContext (which is
    ephemeral and lost on compaction).

    Scans projects/<name>/ subdirectories for TOOLS.md and AGENTS.md.
    Collects identity files (USER.md, SOUL.md, MEMORY.md) from the adapter's
    per-instance identity directory (not the shared project dir).
    Writes the combined content to .claude/rules/quaid-projects.md.

    Also sweeps for orphaned sessions (previous sessions whose transcripts
    have un-extracted content past the extraction cursor).
    """
    # Read hook input to get current session_id for orphan sweep
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    current_session_id = hook_input.get("session_id", "")

    # Sweep orphaned sessions via the extraction daemon
    try:
        from core.extraction_daemon import sweep_orphaned_sessions, ensure_alive
        try:
            ensure_alive()
        except Exception as e:
            print(f"[quaid][session-init] daemon ensure_alive failed: {e}", file=sys.stderr)
        swept = sweep_orphaned_sessions(current_session_id)
        if swept:
            print(f"[quaid][session-init] swept {swept} orphaned session(s)", file=sys.stderr)
    except Exception as e:
        print(f"[quaid][session-init] orphan sweep error: {e}", file=sys.stderr)

    projects_dir = _get_projects_dir()
    if not projects_dir.is_dir():
        print(f"[quaid][session-init] projects dir not found: {projects_dir}", file=sys.stderr)
        return

    sections: List[str] = []

    # 1. Collect identity files (SOUL.md, USER.md, MEMORY.md) from instance silo
    identity_dir = _get_identity_dir()
    for special_file in ("USER.md", "SOUL.md", "MEMORY.md"):
        fpath = identity_dir / special_file
        if fpath.is_file():
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"--- {special_file} ---\n{content}")

    # 2. Collect TOOLS.md and AGENTS.md from all project subdirs
    try:
        subdirs = sorted(
            [d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
            key=lambda d: (0 if d.name == "quaid" else 1, d.name),
        )
    except OSError:
        subdirs = []

    for project_dir in subdirs:
        project_name = project_dir.name
        for doc_file in ("TOOLS.md", "AGENTS.md"):
            fpath = project_dir / doc_file
            if fpath.is_file():
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")

    if not sections:
        print("[quaid][session-init] no project docs found", file=sys.stderr)
        return

    # 3. Check janitor health and prepend warning if stale
    janitor_warning = _check_janitor_health()
    if janitor_warning:
        sections.insert(0, janitor_warning)

    # 4. Write to .claude/rules/ so Claude Code caches it and preserves
    #    through compaction. The file is regenerated on each session start
    #    to pick up any project doc changes.
    rules_env = os.environ.get("QUAID_RULES_DIR", "").strip()
    if rules_env:
        rules_dir = Path(rules_env)
    else:
        # B061: Use cwd from hook stdin (CC provides project root there),
        # falling back to os.getcwd() if not available
        hook_cwd = hook_input.get("cwd", "").strip() if hook_input else ""
        base = Path(hook_cwd) if hook_cwd else Path.cwd()
        rules_dir = base / ".claude" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    rules_file = rules_dir / "quaid-projects.md"
    content = "# Quaid Project Context\n\n" + "\n\n".join(sections) + "\n"

    # Only write if content changed (avoid unnecessary file churn)
    try:
        existing = rules_file.read_text(encoding="utf-8") if rules_file.is_file() else ""
    except OSError:
        existing = ""

    if content != existing:
        rules_file.write_text(content, encoding="utf-8")
        print(f"[quaid][session-init] updated {rules_file}", file=sys.stderr)
    else:
        print(f"[quaid][session-init] {rules_file} up to date", file=sys.stderr)


def hook_search(args):
    """Search memories and docs, print results to stdout.

    Usage: quaid hook-search "query"
    """
    query = " ".join(args.query) if hasattr(args, "query") and args.query else ""
    if not query:
        print("Usage: quaid hook-search \"query\"", file=sys.stderr)
        sys.exit(1)

    try:
        from core.interface.api import recall, projects_search_docs
        owner = _get_owner_id()

        # Search memories
        memories = recall(query=query, owner_id=owner, limit=5)

        # Search docs
        doc_results = projects_search_docs(query=query, limit=3)

        # Format output
        if memories:
            print("=== Memory Results ===")
            for i, mem in enumerate(memories, 1):
                text = mem.get("text", "")
                sim = mem.get("similarity", 0)
                cat = mem.get("category", "fact")
                print(f"  {i}. [{cat}] {text} (relevance: {sim:.2f})")

        chunks = doc_results.get("chunks", [])
        if chunks:
            print("\n=== Documentation Results ===")
            for i, chunk in enumerate(chunks, 1):
                title = chunk.get("title", "untitled")
                text = chunk.get("text", "")[:200]
                print(f"  {i}. [{title}] {text}")

        if not memories and not chunks:
            print("No results found.")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Quaid hook entry points for platform lifecycle integration",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("inject", help="Recall + inject memories for a user message")
    subparsers.add_parser("inject-compact", help="Re-inject memories after compaction")
    subparsers.add_parser("session-init", help="Inject project docs at session start")

    extract_parser = subparsers.add_parser("extract", help="Extract knowledge from transcript")
    extract_parser.add_argument(
        "--precompact", action="store_true",
        help="Flag indicating this is a pre-compaction extraction",
    )

    search_parser = subparsers.add_parser("search", help="Search memories + docs")
    search_parser.add_argument("query", nargs="*", help="Search query")

    args = parser.parse_args()

    if args.command == "inject":
        hook_inject(args)
    elif args.command == "inject-compact":
        hook_inject_compact(args)
    elif args.command == "session-init":
        hook_session_init(args)
    elif args.command == "extract":
        hook_extract(args)
    elif args.command == "search":
        hook_search(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
