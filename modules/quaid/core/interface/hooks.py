#!/usr/bin/env python3
"""Quaid hook entry points — adapter-agnostic lifecycle integration.

Generic hook handlers invoked by host platforms (Claude Code, OpenClaw, etc.)
via the quaid CLI. Reads JSON from stdin, writes to stdout/stderr.

Hook commands:
    inject          Recall memories for a user message (stdin: JSON with "prompt")
    inject-compact  Re-inject critical memories after compaction
    extract         Extract knowledge from a conversation transcript
    session-init    Collect and output project docs for session start injection

Usage:
    quaid hook-inject             (reads JSON from stdin)
    quaid hook-inject-compact     (reads JSON from stdin)
    quaid hook-extract [--precompact]  (reads JSON from stdin)
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
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}

    Also drains any pending notifications (from extraction, janitor, etc.)
    and appends them to the context so Claude can relay them to the user.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    session_id = hook_input.get("session_id", "").strip()
    query = hook_input.get("prompt", "").strip()
    if not query:
        return

    # Ensure a cursor exists for this session so the daemon can discover it
    # for timeout extraction.  Lightweight: skips if cursor already exists.
    if session_id:
        try:
            from core.extraction_daemon import write_cursor, read_cursor
            existing = read_cursor(session_id)
            if not existing.get("transcript_path"):
                from lib.adapter import get_adapter
                sessions_dir = get_adapter().get_sessions_dir()
                transcript_path = ""
                if sessions_dir:
                    for candidate in Path(sessions_dir).rglob(f"{session_id}.jsonl"):
                        transcript_path = str(candidate)
                        break
                # Fallback: rglob misses when the transcript hasn't been created yet
                # (race: CC creates the .jsonl file after UserPromptSubmit fires).
                # Derive the expected path from cwd using CC's encoding scheme
                # (abs path with '/' replaced by '-') so the daemon can discover
                # the session once the transcript exists and has content.
                if not transcript_path:
                    hook_cwd = hook_input.get("cwd", "").strip() if hook_input else ""
                    if hook_cwd and sessions_dir:
                        cwd_encoded = hook_cwd.replace("/", "-")
                        predicted = Path(sessions_dir) / cwd_encoded / f"{session_id}.jsonl"
                        transcript_path = str(predicted)
                if transcript_path:
                    write_cursor(session_id, 0, transcript_path)
        except Exception:
            pass

    # Ask the adapter for any pending context (e.g. deferred notifications).
    # Adapters without pending context return empty string.
    pending_context = _get_pending_context()

    try:
        from core.interface.api import recall_fast
        from datastore.memorydb.memory_graph import plan_tool_hint
        from concurrent.futures import ThreadPoolExecutor, as_completed

        owner = _get_owner_id()

        # Run recall and tool hint in parallel — both are fast-tier, independent calls.
        with ThreadPoolExecutor(max_workers=2) as pool:
            recall_future = pool.submit(recall_fast, query=query, owner_id=owner, limit=10)
            hint_future = pool.submit(plan_tool_hint, query)
            memories = recall_future.result()
            tool_hint = hint_future.result()

        context_parts = []

        if tool_hint:
            context_parts.append(tool_hint)

        if pending_context:
            context_parts.append(pending_context)

        if memories:
            context_parts.append(_format_memories(memories))

        if not context_parts:
            return

        context = "\n\n".join(context_parts)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))

    except Exception as e:
        # Still try to surface pending context even if recall fails
        if pending_context:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": pending_context,
                }
            }))
        print(f"[quaid][hook-inject] error: {e}", file=sys.stderr)


def _get_pending_context() -> str:
    """Ask the adapter for any pending context to inject.

    Returns formatted context string ready for additionalContext, or empty string.
    Each adapter decides its own mechanism (deferred file, queue, etc.).
    """
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        if hasattr(adapter, "get_pending_context"):
            return adapter.get_pending_context() or ""
    except Exception:
        pass
    return ""


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

        # Capture session-scoped OAuth token for the daemon.
        # Stop/PreCompact hooks run after CC's auth is established, so
        # CLAUDE_CODE_OAUTH_TOKEN may be available here even though it
        # isn't in SessionInit hooks (which run before auth).
        try:
            _cc_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            if _cc_token:
                from lib.adapter import get_adapter as _get_adapter
                _tok_path = _get_adapter().store_auth_token(_cc_token)
                print(f"[quaid][{label}] auth token captured at {_tok_path}", file=sys.stderr)
            else:
                print(f"[quaid][{label}] CLAUDE_CODE_OAUTH_TOKEN not in env", file=sys.stderr)
        except Exception as _te:
            print(f"[quaid][{label}] auth token capture failed: {_te}", file=sys.stderr)

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

    # Refresh the adapter's auth token from the session-scoped CC OAuth token.
    # CLAUDE_CODE_OAUTH_TOKEN is a properly API-scoped token that CC injects
    # into its own process.  Writing it to .auth-token keeps the daemon and
    # janitor able to make LLM calls without having to inherit this env var.
    try:
        import os as _os
        _session_token = _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if _session_token:
            from lib.adapter import get_adapter as _get_adapter
            _tok_path = _get_adapter().store_auth_token(_session_token)
            print(f"[quaid][session-init] auth token refreshed at {_tok_path}", file=sys.stderr)
        else:
            print("[quaid][session-init] CLAUDE_CODE_OAUTH_TOKEN not in env — .auth-token not updated", file=sys.stderr)
    except Exception as _e:
        print(f"[quaid][session-init] auth token capture failed: {_e}", file=sys.stderr)

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

    # Seed an initial cursor for the current session so the daemon's idle
    # check can discover it for timeout extraction.  Without this, new
    # sessions that never trigger SessionEnd or PreCompact would be invisible
    # to check_idle_sessions().
    if current_session_id:
        try:
            from core.extraction_daemon import write_cursor, read_cursor
            existing = read_cursor(current_session_id)
            if not existing.get("transcript_path"):
                # Resolve transcript path: adapter.get_sessions_dir() + search
                transcript_path = ""
                try:
                    from lib.adapter import get_adapter
                    sessions_dir = get_adapter().get_sessions_dir()
                    if sessions_dir:
                        for candidate in Path(sessions_dir).rglob(f"{current_session_id}.jsonl"):
                            transcript_path = str(candidate)
                            break
                except Exception:
                    pass
                if transcript_path:
                    write_cursor(current_session_id, 0, transcript_path)
                    print(f"[quaid][session-init] seeded cursor for {current_session_id}", file=sys.stderr)
        except Exception as e:
            print(f"[quaid][session-init] cursor seed error: {e}", file=sys.stderr)

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

    # 2. Collect TOOLS.md and AGENTS.md from all project subdirs.
    #    Also include canonical_paths from the project registry so that
    #    projects whose docs live outside projects_dir (e.g. in an OC silo
    #    but registered as shared) are included without requiring symlinks.
    try:
        subdirs = sorted(
            [d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
            key=lambda d: (0 if d.name == "quaid" else 1, d.name),
        )
    except OSError:
        subdirs = []

    # Collect registry canonical_paths for projects not already under projects_dir.
    # Keyed by project name so registry entries win for the same name.
    registry_extra: Dict[str, Path] = {}
    try:
        from core.project_registry import list_projects as _list_projects
        for proj_name, proj_entry in _list_projects().items():
            canonical = Path(proj_entry.get("canonical_path", "")).resolve()
            if canonical.is_dir() and not canonical.is_relative_to(projects_dir.resolve()):
                registry_extra[proj_name] = canonical
    except Exception:
        pass

    # Merge: projects_dir subdirs first, then registry extras not yet covered.
    seen_names = {d.name for d in subdirs}
    extra_subdirs = sorted(
        [(name, path) for name, path in registry_extra.items() if name not in seen_names],
        key=lambda t: (0 if t[0] == "quaid" else 1, t[0]),
    )

    for project_dir in subdirs:
        project_name = project_dir.name
        for doc_file in ("TOOLS.md", "AGENTS.md"):
            fpath = project_dir / doc_file
            if fpath.is_file():
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")

    for project_name, project_dir in extra_subdirs:
        for doc_file in ("TOOLS.md", "AGENTS.md"):
            fpath = project_dir / doc_file
            if fpath.is_file():
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")

    # 2b. Append adapter CLI tools snippet (registered by the active adapter)
    try:
        from lib.adapter import get_adapter
        cli_snippet = get_adapter().get_cli_tools_snippet()
        if cli_snippet:
            sections.append(f"--- adapter-cli ---\n{cli_snippet.strip()}")
    except Exception as e:
        print(f"[quaid][session-init] adapter CLI snippet error: {e}", file=sys.stderr)

    if not sections:
        print("[quaid][session-init] no project docs found", file=sys.stderr)
        return

    # 3. Check janitor health and prepend warning if stale
    janitor_warning = _check_janitor_health()
    if janitor_warning:
        sections.insert(0, janitor_warning)

    # 3b. Check compatibility and prepend warning if degraded/safe
    try:
        from core.compatibility import notify_on_use_if_degraded
        from lib.adapter import get_adapter
        compat_warning = notify_on_use_if_degraded(get_adapter().data_dir())
        if compat_warning:
            sections.insert(0, f"--- SYSTEM WARNING ---\n{compat_warning}")
            print(f"[quaid][session-init] {compat_warning}", file=sys.stderr)
    except Exception:
        pass

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



def hook_subagent_start(args):
    """Register a subagent in the subagent registry.

    Reads hook JSON from stdin (CC SubagentStart / OC subagent_spawned):
        {"session_id": "...", "agent_id": "...", "agent_type": "...", ...}

    Registers the child so the daemon knows to:
      - Skip standalone timeout extraction for this subagent
      - Merge its transcript into the parent on parent extraction
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[quaid][subagent-start] invalid JSON on stdin: {e}", file=sys.stderr)
        return

    parent_session_id = hook_input.get("session_id", "").strip()
    child_id = hook_input.get("agent_id", "").strip()
    child_type = hook_input.get("agent_type", "").strip()

    if not parent_session_id or not child_id:
        return

    try:
        from core.subagent_registry import register
        register(
            parent_session_id=parent_session_id,
            child_id=child_id,
            child_type=child_type or None,
        )
        print(f"[quaid][subagent-start] registered {child_id} under {parent_session_id}", file=sys.stderr)
    except Exception as e:
        print(f"[quaid][subagent-start] error: {e}", file=sys.stderr)


def hook_subagent_stop(args):
    """Mark a subagent as complete in the registry.

    Reads hook JSON from stdin (CC SubagentStop / OC subagent_ended):
        {"session_id": "...", "agent_id": "...", "agent_type": "...",
         "agent_transcript_path": "...", "last_assistant_message": "...", ...}

    Updates the registry with the transcript path and marks the child
    as complete/harvestable.
    """
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[quaid][subagent-stop] invalid JSON on stdin: {e}", file=sys.stderr)
        return

    parent_session_id = hook_input.get("session_id", "").strip()
    child_id = hook_input.get("agent_id", "").strip()
    transcript_path = hook_input.get("agent_transcript_path", "").strip()

    if not parent_session_id or not child_id:
        return

    # Expand ~ in transcript path
    if transcript_path:
        transcript_path = os.path.expanduser(transcript_path)

    try:
        from core.subagent_registry import mark_complete
        mark_complete(
            parent_session_id=parent_session_id,
            child_id=child_id,
            transcript_path=transcript_path or None,
        )
        print(f"[quaid][subagent-stop] completed {child_id} under {parent_session_id}", file=sys.stderr)
    except Exception as e:
        print(f"[quaid][subagent-stop] error: {e}", file=sys.stderr)


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

    subparsers.add_parser("subagent-start", help="Register subagent in registry")
    subparsers.add_parser("subagent-stop", help="Mark subagent complete in registry")

    args = parser.parse_args()

    if args.command == "inject":
        hook_inject(args)
    elif args.command == "inject-compact":
        hook_inject_compact(args)
    elif args.command == "session-init":
        hook_session_init(args)
    elif args.command == "extract":
        hook_extract(args)
    elif args.command == "subagent-start":
        hook_subagent_start(args)
    elif args.command == "subagent-stop":
        hook_subagent_stop(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
