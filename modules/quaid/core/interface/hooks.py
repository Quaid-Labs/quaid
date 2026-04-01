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
from collections import deque
import fcntl
import glob as glob_mod
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List


def _read_stdin_json() -> dict:
    """Read a JSON object from stdin without blocking on newline or EOF.

    CC sends the JSON payload as a single write without a trailing newline
    and keeps stdin open. readline() blocks waiting for newline; json.load()
    blocks waiting for EOF. Use select + non-blocking read to consume only
    what is available, then parse.
    """
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 10.0)
        if not ready:
            return {}
        flags = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)
        buf = ""
        while True:
            try:
                chunk = sys.stdin.read(65536)
                if not chunk:
                    break
                buf += chunk
            except (IOError, OSError):
                break
        # Restore blocking mode
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETFL, flags)
        return json.loads(buf.strip()) if buf.strip() else {}
    except Exception:
        return {}

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


from lib.adapter import get_owner_id as _get_owner_id


_TOOLS_DOMAIN_BLOCK_RE = re.compile(
    r"<!-- AUTO-GENERATED:DOMAIN-LIST:START -->.*?<!-- AUTO-GENERATED:DOMAIN-LIST:END -->\n*",
    flags=re.DOTALL,
)


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


def _format_project_docs(docs_bundle: Dict) -> str:
    """Format injected project-doc search hits as readable context text."""
    chunks = list((docs_bundle or {}).get("chunks") or [])
    if not chunks:
        return ""

    project = str((docs_bundle or {}).get("project") or "").strip()
    heading = f"[Quaid Project Docs: {project}]" if project else "[Quaid Project Docs]"
    lines = [heading]
    for i, chunk in enumerate(chunks, 1):
        text = str(chunk.get("text") or chunk.get("content") or "").strip()
        if not text:
            continue
        source = Path(str(chunk.get("source") or "")).name
        sim = float(chunk.get("similarity") or 0.0)
        label = f" (from {source})" if source else ""
        lines.append(f"  {i}. {text}{label} (relevance: {sim:.2f})")
    return "\n".join(lines) if len(lines) > 1 else ""


def _strip_tools_domain_block(doc_file: str, content: str) -> str:
    if doc_file != "TOOLS.md":
        return content
    return re.sub(_TOOLS_DOMAIN_BLOCK_RE, "", content).strip()


def _build_runtime_context_block() -> str:
    from core.runtime.system_context import build_system_context_block

    return build_system_context_block()


def _hook_trace_path() -> Path:
    workspace = str(
        os.environ.get("QUAID_HOME")
        or os.environ.get("QUAID_WORKSPACE")
        or os.environ.get("CLAWDBOT_WORKSPACE")
        or os.getcwd()
    ).strip()
    instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    root = Path(workspace).expanduser()
    if instance:
        root = root / instance
    return root / "logs" / "quaid-hook-trace.jsonl"


def _write_hook_trace(event: str, payload: dict | None = None) -> None:
    trace_path = _hook_trace_path()
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **(payload or {}),
    }
    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _extract_lifecycle_command(text: str) -> str:
    value = str(text or "").strip()
    if not value.startswith("/"):
        return ""
    command = value.split()[0].lower()
    if command in ("/new", "/clear", "/reset", "/restart"):
        return command
    return ""


def _detect_codex_lifecycle_command(hook_input: dict, transcript_path: str) -> str:
    """Best-effort detection for slash lifecycle commands on Codex Stop hooks."""
    if not isinstance(hook_input, dict):
        hook_input = {}

    def _scan_candidates(container: dict) -> str:
        for key in ("command", "prompt", "message", "input", "last_user_message", "text"):
            cmd = _extract_lifecycle_command(container.get(key, ""))
            if cmd:
                return cmd
        payload = container.get("payload")
        if isinstance(payload, dict):
            for key in ("command", "prompt", "message", "input", "last_user_message", "text"):
                cmd = _extract_lifecycle_command(payload.get(key, ""))
                if cmd:
                    return cmd
        return ""

    direct = _scan_candidates(hook_input)
    if direct:
        return direct

    # Fallback: inspect the tail of the transcript for the latest user slash
    # command. Avoid full-file reads by sampling only the final rows.
    try:
        tail = deque(maxlen=128)
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if line:
                    tail.append(line)
        for raw in reversed(tail):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            record_type = str(obj.get("type") or "").strip()
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if record_type == "event_msg" and str(payload.get("type") or "").strip() == "user_message":
                return _extract_lifecycle_command(str(payload.get("message") or ""))
            if record_type == "response_item" and str(payload.get("type") or "").strip() == "message":
                role = str(payload.get("role") or "").strip().lower()
                if role != "user":
                    continue
                content = payload.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        cmd = _extract_lifecycle_command(
                            str(item.get("text") or item.get("input_text") or item.get("output_text") or "")
                        )
                        if cmd:
                            return cmd
                elif isinstance(content, str):
                    cmd = _extract_lifecycle_command(content)
                    if cmd:
                        return cmd
    except OSError:
        return ""
    return ""


def _summarize_recall_results(memories: List[Dict], limit: int = 5) -> List[Dict]:
    out: List[Dict] = []
    for mem in list(memories or [])[: max(1, limit)]:
        if not isinstance(mem, dict):
            continue
        out.append({
            "id": mem.get("id"),
            "text": str(mem.get("text", "")).strip()[:180],
            "similarity": round(float(mem.get("similarity", 0) or 0), 3),
            "category": mem.get("category"),
            "via": mem.get("via"),
            "extraction_confidence": mem.get("extraction_confidence"),
            "created_at": mem.get("created_at") or mem.get("createdAt"),
        })
    return out


def _summarize_recall_meta(meta: dict | None) -> dict | None:
    if not isinstance(meta, dict):
        return None
    quality_gate = meta.get("quality_gate") if isinstance(meta.get("quality_gate"), dict) else {}
    evaluation = quality_gate.get("evaluation") if isinstance(quality_gate.get("evaluation"), dict) else {}
    memory_quality = meta.get("memory_quality") if isinstance(meta.get("memory_quality"), dict) else {}
    turn_details = meta.get("turn_details") if isinstance(meta.get("turn_details"), list) else []
    first_turn = turn_details[0] if turn_details and isinstance(turn_details[0], dict) else {}
    planner = first_turn.get("planner") if isinstance(first_turn.get("planner"), dict) else {}
    store_runs = meta.get("store_runs") if isinstance(meta.get("store_runs"), list) else []
    phases = meta.get("phases_ms") if isinstance(meta.get("phases_ms"), dict) else {}
    return {
        "mode": meta.get("mode"),
        "stop_reason": meta.get("stop_reason"),
        "selected_path": meta.get("selected_path"),
        "planned_stores": list(meta.get("planned_stores") or [])[:8] if isinstance(meta.get("planned_stores"), list) else None,
        "planned_project": meta.get("planned_project"),
        "planner": {
            "bailout_reason": planner.get("bailout_reason"),
            "planner_profile": planner.get("planner_profile"),
            "queries_count": planner.get("queries_count"),
            "used_llm": planner.get("used_llm"),
        },
        "store_runs": [
            {
                "store": run.get("store"),
                "result_count": run.get("result_count"),
                "total_ms": run.get("total_ms"),
                "selected_path": run.get("selected_path"),
            }
            for run in store_runs[:6]
            if isinstance(run, dict)
        ],
        "quality_gate": {
            "fast_drill_candidate": quality_gate.get("fast_drill_candidate"),
            "fast_drill_enabled": quality_gate.get("fast_drill_enabled"),
            "fast_drill_reasons": list(quality_gate.get("fast_drill_reasons") or [])[:8]
            if isinstance(quality_gate.get("fast_drill_reasons"), list) else None,
            "requirements": list(evaluation.get("requirements") or [])[:8]
            if isinstance(evaluation.get("requirements"), list) else None,
            "covered_terms_ratio": evaluation.get("covered_terms_ratio"),
            "top_similarity": evaluation.get("top_similarity"),
        },
        "memory_quality": {
            "surface_quality": memory_quality.get("surface_quality"),
            "another_recall_may_help": memory_quality.get("another_recall_may_help"),
            "signals": list(memory_quality.get("signals") or [])[:8]
            if isinstance(memory_quality.get("signals"), list) else None,
        },
        "phases_ms": {
            "total_ms": phases.get("total_ms"),
            "store_plan_wall_ms": phases.get("store_plan_wall_ms"),
            "planner_ms": phases.get("planner_ms"),
            "reranker_ms": phases.get("reranker_ms"),
        },
    }


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
        hook_input = _read_stdin_json()
    except (json.JSONDecodeError, ValueError):
        return

    session_id = hook_input.get("session_id", "").strip()
    query = hook_input.get("prompt", "").strip()
    if not query:
        return

    # Any prompt traffic is a daemon liveness contact point.
    # ensure_alive is instance-scoped and lock-guarded, so repeated calls are cheap.
    try:
        from core.extraction_daemon import ensure_alive
        ensure_alive()
    except Exception as e:
        print(f"[quaid][hook-inject] daemon ensure_alive failed: {e}", file=sys.stderr)

    # Ensure a cursor exists for this session so the daemon can discover it
    # for timeout extraction.  Lightweight: skips if cursor already exists.
    if session_id:
        try:
            from core.extraction_daemon import write_cursor, read_cursor
            existing = read_cursor(session_id)
            if not existing.get("transcript_path"):
                transcript_path = _resolve_hook_transcript_path(
                    session_id=session_id,
                    hook_cwd=hook_input.get("cwd", "").strip() if hook_input else "",
                    transcript_path=hook_input.get("transcript_path", "").strip() if hook_input else "",
                )
                if transcript_path:
                    write_cursor(session_id, 0, transcript_path)
        except Exception:
            pass

    # Ask the adapter for any pending context (e.g. deferred notifications).
    # Adapters without pending context return empty string.
    pending_context = _get_pending_context()

    try:
        from concurrent.futures import ThreadPoolExecutor
        from core.interface.api import projects_search_docs, recall_fast

        owner = _get_owner_id()
        memories = []
        recall_meta = None
        docs_bundle = None
        _write_hook_trace("hook.inject.start", {
            "query": query[:160],
            "session_id": session_id,
        })
        with ThreadPoolExecutor(max_workers=2) as pool:
            mem_future = pool.submit(
                lambda: recall_fast(query=query, owner_id=owner, limit=10, return_meta=True)
            )
            docs_future = pool.submit(projects_search_docs, query=query, limit=3)
            try:
                mem_result = mem_future.result()
                if isinstance(mem_result, tuple) and len(mem_result) == 2:
                    memories, recall_meta = mem_result
                else:
                    memories = mem_result
            except Exception:
                memories = []
                recall_meta = None
            try:
                docs_bundle = docs_future.result()
            except Exception:
                docs_bundle = None

        _write_hook_trace("hook.inject.recall_done", {
            "query": query[:160],
            "session_id": session_id,
            "count": len(memories or []),
            "top_results": _summarize_recall_results(memories),
            "diagnostics": _summarize_recall_meta(recall_meta),
        })
        _write_hook_trace("hook.inject.docs_done", {
            "query": query[:160],
            "session_id": session_id,
            "project": (docs_bundle or {}).get("project") if isinstance(docs_bundle, dict) else None,
            "docs_count": len((docs_bundle or {}).get("chunks") or []) if isinstance(docs_bundle, dict) else 0,
        })

        context_parts = []

        if pending_context:
            context_parts.append(pending_context)

        if memories:
            context_parts.append(_format_memories(memories))
        docs_context = _format_project_docs(docs_bundle or {})
        if docs_context:
            context_parts.append(docs_context)

        if not context_parts:
            _write_hook_trace("hook.inject.empty", {
                "query": query[:160],
                "session_id": session_id,
                "recall_count": len(memories or []),
                "docs_count": len((docs_bundle or {}).get("chunks") or []) if isinstance(docs_bundle, dict) else 0,
            })
            return

        context = "\n\n".join(context_parts)
        _write_hook_trace("hook.inject.context_emitted", {
            "query": query[:160],
            "session_id": session_id,
            "recall_count": len(memories or []),
            "docs_count": len((docs_bundle or {}).get("chunks") or []) if isinstance(docs_bundle, dict) else 0,
            "context_len": len(context),
        })
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))

    except RuntimeError:
        raise
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


def _current_adapter_id() -> str:
    try:
        from lib.adapter import get_adapter

        return str(get_adapter().adapter_id() or "").strip().lower()
    except Exception:
        return ""


def _resolve_hook_transcript_path(session_id: str, hook_cwd: str = "", transcript_path: str = "") -> str:
    """Resolve hook transcript paths across adapter-specific session layouts."""
    session_id = str(session_id or "").strip()
    if not session_id:
        return ""

    explicit = str(transcript_path or "").strip()
    if explicit:
        return explicit

    sessions_dir = None
    adapter_id = ""
    try:
        from lib.adapter import get_adapter

        adapter = get_adapter()
        adapter_id = str(adapter.adapter_id() or "").strip().lower()
        resolved = adapter.get_session_path(session_id)
        if resolved:
            return str(resolved)
        sessions_dir = adapter.get_sessions_dir()
    except Exception:
        sessions_dir = None
        adapter_id = ""

    if sessions_dir:
        pattern = f"rollout-*{session_id}.jsonl" if adapter_id == "codex" else f"{session_id}.jsonl"
        for candidate in Path(sessions_dir).rglob(pattern):
            return str(candidate)

    if hook_cwd and sessions_dir and adapter_id == "claude-code":
        cwd_encoded = hook_cwd.replace("/", "-")
        return str(Path(sessions_dir) / cwd_encoded / f"{session_id}.jsonl")

    if sessions_dir and adapter_id == "codex":
        from datetime import datetime

        date_prefix = datetime.now().strftime("%Y/%m/%d")
        return str(Path(sessions_dir) / date_prefix / f"rollout-pending-{session_id}.jsonl")

    if sessions_dir and adapter_id in ("openclaw", "standalone", ""):
        return str(Path(sessions_dir) / f"{session_id}.jsonl")

    return ""


def hook_inject_compact(args):
    """Re-inject critical memories after context compaction.

    Reads hook JSON from stdin:
        {"cwd": "...", "session_id": "..."}

    Writes plain text to stdout.
    """
    try:
        hook_input = _read_stdin_json()
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
    except RuntimeError:
        raise
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
        hook_input = _read_stdin_json()
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
        from core.extraction_daemon import write_signal

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

        # Signal write is complete (the critical part). Now ensure the daemon
        # is alive to process it. Run in a detached subprocess so host hook
        # cancellation cannot interrupt daemon startup.
        try:
            _daemon_script = Path(__file__).parent.parent / "extraction_daemon.py"
            _env = {
                k: v for k, v in os.environ.items()
                if not k.startswith("OPENCLAW_") and k != "CLAUDE_CODE_OAUTH_TOKEN"
            }
            subprocess.Popen(
                [sys.executable, str(_daemon_script), "start"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_env,
            )
        except Exception:
            pass  # best-effort; signal is already written

    except Exception as e:
        print(f"[quaid][{label}] error: {e}", file=sys.stderr)


def hook_codex_stop(args):
    """Queue Codex Stop extraction work for the daemon (signal-only path)."""
    try:
        hook_input = _read_stdin_json()
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    session_id = str(hook_input.get("session_id") or "").strip()
    transcript_path = _resolve_hook_transcript_path(
        session_id=session_id,
        hook_cwd=str(hook_input.get("cwd") or "").strip(),
        transcript_path=str(hook_input.get("transcript_path") or "").strip(),
    )

    if not session_id or not transcript_path:
        print("{}")
        return

    transcript_path = os.path.expanduser(transcript_path)
    if not os.path.isfile(transcript_path):
        _write_hook_trace("hook.codex.stop.transcript_missing", {
            "session_id": session_id,
            "transcript_path": transcript_path,
        })
        print("{}")
        return

    try:
        from core.extraction_daemon import write_signal

        lifecycle_command = _detect_codex_lifecycle_command(hook_input, transcript_path)
        signal_type = "session_end" if lifecycle_command in ("/new", "/clear", "/reset", "/restart") else "rolling"
        meta = {"source": "hook_codex_stop"}
        if lifecycle_command:
            meta["command"] = lifecycle_command
            meta["reason"] = f"command:{lifecycle_command.lstrip('/')}"
            _write_hook_trace("hook.codex.stop.command_detected", {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "command": lifecycle_command,
                "signal_type": signal_type,
            })

        sig_path = write_signal(
            signal_type=signal_type,
            session_id=session_id,
            transcript_path=transcript_path,
            adapter="codex",
            supports_compaction_control=False,
            meta=meta,
        )
        _write_hook_trace("hook.codex.stop.signal_written", {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "signal_name": sig_path.name,
            "signal_type": signal_type,
        })

        # Best-effort daemon wakeup using the same detached launcher strategy as hook_extract.
        try:
            _daemon_script = Path(__file__).parent.parent / "extraction_daemon.py"
            _env = {
                k: v for k, v in os.environ.items()
                if not k.startswith("OPENCLAW_") and k != "CLAUDE_CODE_OAUTH_TOKEN"
            }
            subprocess.Popen(
                [sys.executable, str(_daemon_script), "start"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_env,
            )
        except Exception:
            pass  # signal write is complete; wakeup remains best-effort

        print("{}")
    except RuntimeError:
        raise
    except Exception as exc:
        try:
            from lib.fail_policy import is_fail_hard_enabled

            if is_fail_hard_enabled():
                raise
        except RuntimeError:
            raise
        _write_hook_trace("hook.codex.stop.error", {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "error": str(exc),
        })
        print(f"[quaid][codex-stop] error: {exc}", file=sys.stderr)
        print("{}")


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
    Collects identity files (USER.md, SOUL.md, ENVIRONMENT.md) from the adapter's
    per-instance identity directory (not the shared project dir).
    Writes the combined content to .claude/rules/quaid-projects.md.

    Also sweeps for orphaned sessions (previous sessions whose transcripts
    have un-extracted content past the extraction cursor).
    """
    # Read hook input to get current session_id for orphan sweep
    try:
        hook_input = _read_stdin_json()
    except (json.JSONDecodeError, ValueError):
        hook_input = {}

    current_session_id = hook_input.get("session_id", "")
    adapter_id = _current_adapter_id()

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

    # Sweep orphaned sessions via the extraction daemon.
    # Always call ensure_alive() on session init across all adapters.
    # Daemons are instance-scoped and ensure_alive()/start_daemon() is
    # lock-guarded (PID + flock), so repeated contact points are idempotent.
    multi_instance_warning = ""
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

    # Warn when multiple agents share the same instance silo. This setup is
    # not supported — platform limitations (e.g. Codex /new creating a new
    # session_id without a lifecycle hook) mean the orphan sweep may flush
    # one agent's staged carry_facts while another is still mid-conversation,
    # which can cause memory quality loss.
    try:
        import time as _time
        import os as _os
        from core.extraction_daemon import _cursor_dir as _get_cursor_dir
        _cursor_dir = _get_cursor_dir()
        if _cursor_dir.is_dir():
            _now = _time.time()
            _active_threshold = 120  # seconds: transcript modified within 2 min = active
            for _cf in _cursor_dir.glob("*.json"):
                try:
                    _cd = json.loads(_cf.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                _other_sid = _cd.get("session_id", "")
                if not _other_sid or _other_sid == current_session_id:
                    continue
                _tp = _cd.get("transcript_path", "")
                if not _tp or not _os.path.isfile(_tp):
                    continue
                try:
                    if _now - _os.path.getmtime(_tp) < _active_threshold:
                        try:
                            from lib.adapter import get_adapter as _get_adapter
                            _instance_type = _get_adapter().get_instance_type()
                        except Exception:
                            _instance_type = "keyed"
                        if _instance_type == "folder":
                            _project_dir = _os.environ.get(
                                "CLAUDE_PROJECT_DIR",
                                _os.environ.get("CODEX_PROJECT_DIR", _os.getcwd()),
                            )
                            multi_instance_warning = (
                                "⚠️  [Quaid] WARNING: Multiple agents are sharing the same "
                                "Quaid instance. On this platform, your Quaid instance is "
                                f"tied to your project root folder (`{_project_dir}`). Any "
                                "agent running from that folder shares the same memory silo. "
                                "Concurrent use by multiple agents is not supported and may "
                                "cause memory quality loss. To give each agent its own "
                                "isolated memory, run it from a different project directory. "
                                "Proceed at your own risk."
                            )
                        else:
                            _instance_id = _os.environ.get("QUAID_INSTANCE", "unknown")
                            multi_instance_warning = (
                                "⚠️  [Quaid] WARNING: Multiple agents are sharing the same "
                                f"Quaid instance (`{_instance_id}`). Concurrent use on the "
                                "same instance is not supported and may cause memory quality "
                                "loss. To isolate an agent, assign it a different "
                                "`QUAID_INSTANCE`. To intentionally share memory between "
                                "separate instances, symlink their instance folders together. "
                                "Proceed at your own risk."
                            )
                        print("[quaid][session-init] WARNING: multiple active sessions detected on same instance", file=sys.stderr)
                        break
                except OSError:
                    continue
    except Exception as _e:
        print(f"[quaid][session-init] multi-instance check error: {_e}", file=sys.stderr)

    # Seed an initial cursor for the current session so the daemon's idle
    # check can discover it for timeout extraction.  Without this, new
    # sessions that never trigger SessionEnd or PreCompact would be invisible
    # to check_idle_sessions().
    if current_session_id:
        try:
            from core.extraction_daemon import write_cursor, read_cursor
            existing = read_cursor(current_session_id)
            if not existing.get("transcript_path"):
                transcript_path = _resolve_hook_transcript_path(
                    session_id=current_session_id,
                    hook_cwd=hook_input.get("cwd", "").strip() if hook_input else "",
                    transcript_path=hook_input.get("transcript_path", "").strip() if hook_input else "",
                )
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

    # 1. Collect identity files (SOUL.md, USER.md, ENVIRONMENT.md) from instance silo
    identity_dir = _get_identity_dir()
    for special_file in ("USER.md", "SOUL.md", "ENVIRONMENT.md"):
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
                content = _strip_tools_domain_block(doc_file, fpath.read_text(encoding="utf-8").strip())
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")

    for project_name, project_dir in extra_subdirs:
        for doc_file in ("TOOLS.md", "AGENTS.md"):
            fpath = project_dir / doc_file
            if fpath.is_file():
                content = _strip_tools_domain_block(doc_file, fpath.read_text(encoding="utf-8").strip())
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")

    # 2b. Append base context file names so guidance can refer to them generically.
    #     These are the adapter's authoritative instruction files (e.g. CLAUDE.md for CC).
    try:
        from lib.adapter import get_adapter
        base_files = get_adapter().get_base_context_files()
        if base_files:
            names = [str(Path(p).name) for p in base_files]
            sections.append(
                f"--- base-context-files ---\n"
                f"Your authoritative base context files are: {', '.join(names)}\n"
                f"These have higher authority than any evolved guidance."
            )
    except Exception as e:
        print(f"[quaid][session-init] base context files error: {e}", file=sys.stderr)

    # 2c. Append adapter CLI tools snippet (registered by the active adapter)
    try:
        from lib.adapter import get_adapter
        cli_snippet = get_adapter().get_cli_tools_snippet()
        if cli_snippet:
            sections.append(f"--- adapter-cli ---\n{cli_snippet.strip()}")
    except Exception as e:
        print(f"[quaid][session-init] adapter CLI snippet error: {e}", file=sys.stderr)

    if sections:
        sections.insert(0, _build_runtime_context_block())

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

    # 3c. Prepend multi-instance warning if detected
    if multi_instance_warning:
        sections.insert(0, f"--- SYSTEM WARNING ---\n{multi_instance_warning}")

    content = "# Quaid Project Context\n\n" + "\n\n".join(sections) + "\n"

    if adapter_id == "codex":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        }))
        print("[quaid][session-init] emitted Codex startup context", file=sys.stderr)
        return

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
        hook_input = _read_stdin_json()
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
        hook_input = _read_stdin_json()
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
    # Prevent recursive CC session spawning: any LLM calls made from within a
    # hook must use OAuth/API-key paths directly.  Without this, the query
    # planner (claude -p "Generate 1 to 5 search queries...") spawns a new CC
    # session which re-fires the inject hook — infinite recursion.
    import os as _os
    _os.environ["QUAID_DAEMON"] = "1"

    parser = argparse.ArgumentParser(
        description="Quaid hook entry points for platform lifecycle integration",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("inject", help="Recall + inject memories for a user message")
    subparsers.add_parser("inject-compact", help="Re-inject memories after compaction")
    subparsers.add_parser("session-init", help="Inject project docs at session start")
    subparsers.add_parser("codex-stop", help="Queue Codex Stop extraction for the daemon")

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
    elif args.command == "codex-stop":
        hook_codex_stop(args)
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
