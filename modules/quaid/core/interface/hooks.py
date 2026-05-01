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
import fcntl
import glob as glob_mod
import hashlib
import json
import logging
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_HOOK_RUNTIME_CONFIG_SNAPSHOT: tuple[tuple[str, int], ...] | None = None
_RULES_FILE_PREFIX = "quaid-"
_LEGACY_RULES_FILE = "quaid-projects.md"
_COMPACT_IDENTITY_CONTEXT_MAX_CHARS = 9000
_IDENTITY_CONTEXT_FILES = ("USER.md", "SOUL.md", "ENVIRONMENT.md")
_TURN_REFRESH_PARALLEL_REPLAY_SECONDS = 5

_DAEMON_START_SKIP_ENV_KEYS = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "MEMORY_DB_PATH",
    "MEMORY_ARCHIVE_DB_PATH",
}


def _daemon_start_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("OPENCLAW_") and k not in _DAEMON_START_SKIP_ENV_KEYS
    }


def _wake_daemon_after_signal() -> None:
    """Best-effort wakeup after a signal write, without disturbing live daemons."""
    try:
        from core.extraction_daemon import read_pid

        if read_pid() is not None:
            return
    except Exception:
        pass

    try:
        _daemon_script = Path(__file__).parent.parent / "extraction_daemon.py"
        subprocess.Popen(
            [sys.executable, str(_daemon_script), "start"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_daemon_start_env(),
        )
    except Exception:
        pass

_CODEX_TOOL_OUTPUT_KEYS = (
    "tool_output",
    "toolOutput",
    "stdout",
    "output",
    "last_tool_output",
    "lastToolOutput",
)


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
        fd = sys.stdin.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        chunks = []
        while True:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            except BlockingIOError:
                break
            except (IOError, OSError):
                break
        # Restore blocking mode
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        buf = b"".join(chunks).decode("utf-8", errors="replace")
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
    filtered_memories = _filter_injectable_memories(memories)
    if not filtered_memories:
        return ""
    anchor_labels = {
        "assistant_option_bullet_anchor": "assistant-suggestion",
        "assistant_option_list_anchor": "assistant-suggestion",
        "assistant_callback_anchor": "assistant-callback",
        "assistant_plan_anchor": "assistant-plan",
        "user_mirrored_idea_anchor": "user-idea",
    }

    def _memory_tags(mem: Dict[str, Any]) -> str:
        tags = [str(mem.get("category", "fact") or "fact").strip().lower() or "fact"]

        source_type = str(mem.get("source_type") or "").strip().lower()
        if source_type:
            tags.append(source_type)

        anchor_kind = str(mem.get("structural_anchor_kind") or "").strip().lower()
        anchor_label = anchor_labels.get(anchor_kind)
        if anchor_label and anchor_label not in tags:
            tags.append(anchor_label)

        return "".join(f"[{tag}]" for tag in tags)

    lines = ["[Quaid Memory Context]"]
    for i, mem in enumerate(filtered_memories, 1):
        text = mem.get("text", "")
        sim = mem.get("similarity", 0)
        tags = _memory_tags(mem)
        lines.append(f"  {i}. {tags} {text} (relevance: {sim:.2f})")
    body = "\n".join(lines)
    return f"<quaid_system_message>\n{body}\n</quaid_system_message>"


_NEGATIVE_MEMORY_CONTEXT_RE = re.compile(
    r"\b(?:memory|record|records|previous\s+sessions|previous\s+conversation|conversation\s+history|"
    r"context|stored|recorded|recall|remember|came\s+up|matches?|log\s+it|save\s+it|save\s+that)\b",
    re.IGNORECASE,
)
_NEGATIVE_MEMORY_CLAIM_RE = re.compile(
    r"\b(?:"
    r"(?:do|does|did)\s+not\s+(?:know|have|remember)|"
    r"(?:do|does|did)n['’]t\s+(?:know|have|remember)|"
    r"(?:still\s+)?nothing\s+(?:in|from|for)|"
    r"(?:no|nothing)\s+(?:in\s+)?(?:memory|record|records|previous\s+sessions|previous\s+conversation|"
    r"conversation\s+history|context|information|info|data)|"
    r"no\s+(?:plant\s+name|name|fact|record|records|information|info)\s+(?:was|were|is|are)\s+(?:previously\s+)?"
    r"(?:recorded|stored|found|available)|"
    r"(?:not|never)\s+(?:previously\s+)?(?:recorded|stored|found|available)|"
    r"nothing\s+(?:came|comes)\s+up|"
    r"no\s+matches?\s+(?:came|come|found)|"
    r"(?:want|would\s+you\s+like)\s+(?:me\s+)?to\s+(?:log|save|record)\s+(?:one|it|that)"
    r")\b",
    re.IGNORECASE,
)
_QUESTION_MEMORY_RE = re.compile(
    r"^\s*(?:who|what|when|where|why|how|which|whose|is|are|was|were|do|does|did|can|could|"
    r"should|would|will|may|might|has|have|had)\b.*\?\s*$",
    re.IGNORECASE,
)
_QUESTION_MEMORY_NO_MARK_RE = re.compile(
    r"^\s*(?:"
    r"(?:who|what|when|where|why|how)(?:['’]s|\s+(?:is|are|was|were|do|does|did|can|could|should|would|will|may|might|has|have|had))|"
    r"(?:which|whose)\s+(?:is|are|was|were|do|does|did|can|could|should|would|will|may|might|has|have|had)|"
    r"(?:is|are|was|were|do|does|did|can|could|should|would|will|may|might|has|have|had)\s+"
    r")\b",
    re.IGNORECASE,
)


def _is_negative_memory_claim_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    return bool(
        _NEGATIVE_MEMORY_CLAIM_RE.search(raw)
        and _NEGATIVE_MEMORY_CONTEXT_RE.search(raw)
    )


def _is_bare_question_memory_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if _QUESTION_MEMORY_RE.match(raw):
        return True
    if raw.endswith((".", "!", ":")):
        return False
    if len(raw.split()) > 24:
        return False
    return bool(_QUESTION_MEMORY_NO_MARK_RE.match(raw))


def _is_non_injectable_memory(mem: Dict) -> bool:
    text = str((mem or {}).get("text") or "").strip()
    if not text:
        return False
    if _is_negative_memory_claim_text(text):
        return True
    if _is_bare_question_memory_text(text):
        return True
    return False


def _filter_injectable_memories(memories: List[Dict]) -> List[Dict]:
    return [
        mem
        for mem in list(memories or [])
        if isinstance(mem, dict) and not _is_non_injectable_memory(mem)
    ]


_CONTEXT_DEDUPE_WS_RE = re.compile(r"\s+")


def _context_dedupe_key(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    return _CONTEXT_DEDUPE_WS_RE.sub(" ", raw).strip().lower()


def _dedupe_context_sections(sections: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for section in list(sections or []):
        text = str(section or "").strip()
        if not text:
            continue
        key = _context_dedupe_key(text)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(section)
    return out


def _format_project_docs(docs_bundle: Dict) -> str:
    """Format injected project-doc search hits as readable context text."""
    chunks = list((docs_bundle or {}).get("chunks") or [])
    if not chunks:
        return ""

    project = str((docs_bundle or {}).get("project") or "").strip()
    heading = f"[Quaid Project Docs: {project}]" if project else "[Quaid Project Docs]"
    lines = [heading]
    seen_chunk_text: set[str] = set()
    for chunk in chunks:
        text = str(chunk.get("text") or chunk.get("content") or "").strip()
        if not text:
            continue
        chunk_key = _context_dedupe_key(text)
        if chunk_key and chunk_key in seen_chunk_text:
            continue
        if chunk_key:
            seen_chunk_text.add(chunk_key)
        source = Path(str(chunk.get("source") or "")).name
        sim = float(chunk.get("similarity") or 0.0)
        label = f" (from {source})" if source else ""
        lines.append(f"  {len(lines)}. {text}{label} (relevance: {sim:.2f})")
    if len(lines) <= 1:
        return ""
    body = "\n".join(lines)
    return f"<quaid_system_message>\n{body}\n</quaid_system_message>"


def _format_direct_agent_notices(messages: List[str]) -> str:
    notices = [str(message or "").strip() for message in messages if str(message or "").strip()]
    if not notices:
        return ""
    body = "\n".join(f"• {message}" for message in notices)
    return (
        "MANDATORY: start your next response by briefly relaying these live Quaid notices to the user. "
        "Then answer the user's current message.\n\n"
        f"<quaid_system_message>\n{body}\n</quaid_system_message>"
    )


def _get_quaid_agents_baseline_context() -> str:
    """Return the file-placement guidance block from projects/quaid/AGENTS.md."""
    try:
        agents_path = _get_projects_dir() / "quaid" / "AGENTS.md"
        if not agents_path.is_file():
            return ""
        text = agents_path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        match = re.search(
            r"## File Placement — MANDATORY RULES\s*(.*?)(?=\n---\n|\n## |\Z)",
            text,
            flags=re.DOTALL,
        )
        body = match.group(0).strip() if match else ""
        if not body:
            return ""
        return (
            "<quaid_system_message>\n"
            "[Quaid Project Guidance]\n"
            f"{body}\n"
            "</quaid_system_message>"
        )
    except Exception:
        return ""


def _format_deferred_notice_relay(messages: List[str]) -> str:
    notices = [str(message or "").strip() for message in messages if str(message or "").strip()]
    if not notices:
        return ""
    body = "\n".join(f"• {message}" for message in notices)
    return (
        "MANDATORY: Quaid just drained deferred notices for the human user. "
        "Begin your next response by relaying each notice below in plain language, then answer the user's current message.\n\n"
        f"<quaid_system_message>\n{body}\n</quaid_system_message>"
    )


def _runtime_config_snapshot() -> tuple[tuple[str, int], ...]:
    try:
        from config import _config_paths

        snapshot: list[tuple[str, int]] = []
        for raw_path in _config_paths():
            path = Path(raw_path)
            try:
                mtime_ns = path.stat().st_mtime_ns if path.exists() else -1
            except OSError:
                mtime_ns = -1
            snapshot.append((str(path), int(mtime_ns)))
        return tuple(snapshot)
    except Exception:
        return tuple()


def _runtime_config_snapshot_state_path() -> Path | None:
    try:
        from lib.adapter import get_adapter

        data_dir = get_adapter().data_dir()
    except Exception:
        return None
    try:
        return Path(data_dir) / "runtime-config-snapshot.json"
    except Exception:
        return None


def _read_runtime_config_snapshot_state() -> tuple[tuple[str, int], ...] | None:
    path = _runtime_config_snapshot_state_path()
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_items = payload.get("snapshot") if isinstance(payload, dict) else payload
        items: list[tuple[str, int]] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return None
            items.append((str(item[0]), int(item[1])))
        return tuple(items)
    except Exception:
        return None


def _write_runtime_config_snapshot_state(snapshot: tuple[tuple[str, int], ...]) -> None:
    path = _runtime_config_snapshot_state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
        tmp_path.write_text(json.dumps({"snapshot": list(snapshot)}), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()  # type: ignore[name-defined]
        except Exception:
            pass


def _reset_runtime_resolution_caches() -> None:
    try:
        from lib.embeddings import reset_embeddings_provider

        reset_embeddings_provider()
    except Exception:
        pass
    try:
        from lib.llm_clients import reset_model_config_cache

        reset_model_config_cache()
    except Exception:
        try:
            import lib.llm_clients as llm_clients

            llm_clients._models_loaded = False
            llm_clients._fast_reasoning_model = ""
            llm_clients._deep_reasoning_model = ""
            llm_clients._pricing_loaded = False
        except Exception:
            pass


def _refresh_runtime_config_if_changed(reason: str) -> bool:
    global _HOOK_RUNTIME_CONFIG_SNAPSHOT
    snapshot = _runtime_config_snapshot()
    if not snapshot:
        return False
    if _HOOK_RUNTIME_CONFIG_SNAPSHOT is None:
        persisted_snapshot = _read_runtime_config_snapshot_state()
        _HOOK_RUNTIME_CONFIG_SNAPSHOT = persisted_snapshot or snapshot
        if persisted_snapshot is not None and persisted_snapshot != snapshot:
            # Short-lived hook processes cannot rely on module globals to detect
            # restored configs. Use the persisted signature to clear stale
            # provider notices only when the config actually changed.
            return _refresh_runtime_config_if_changed(reason)
        _write_runtime_config_snapshot_state(snapshot)
        _write_hook_trace(
            "hook.runtime_config.baseline",
            {
                "reason": reason,
                "paths": [path for path, _mtime in snapshot],
                "cleared_pending": 0,
            },
        )
        return False
    if snapshot == _HOOK_RUNTIME_CONFIG_SNAPSHOT:
        return False
    try:
        from config import reload_config

        reload_config()
        _reset_runtime_resolution_caches()
        try:
            from lib.agent_notice import clear_pending_notices_by_source

            clear_pending_notices_by_source(sources={"provider", "llm_config", "embeddings"})
        except Exception:
            pass
    except Exception as exc:
        _write_hook_trace(
            "hook.runtime_config.reload_failed",
            {
                "reason": reason,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )
        return False
    _HOOK_RUNTIME_CONFIG_SNAPSHOT = snapshot
    _write_runtime_config_snapshot_state(snapshot)
    _write_hook_trace(
        "hook.runtime_config.reloaded",
        {
            "reason": reason,
            "paths": [path for path, _mtime in snapshot],
        },
    )
    return True


def _safe_agent_error(exc: Exception) -> str:
    """Summarize hook/runtime exceptions without dumping raw internals into context."""
    err_type = type(exc).__name__ or "Error"
    return f"Error type: {err_type}. Check Quaid logs for details."


def _extract_recall_provider_notice(memories: List[Dict], recall_meta: dict | None) -> str:
    """Promote degraded recall-provider failures into an explicit relay notice.

    Fast recall can fail open and return fallback results plus a warning row.
    That warning should not be buried inside memory context — it needs to be
    surfaced as a direct system message so the agent tells the user.
    """
    warning_texts: List[str] = []
    for mem in list(memories or []):
        if not isinstance(mem, dict):
            continue
        category = str(mem.get("category") or "").strip().lower()
        text = str(mem.get("text") or "").strip()
        if category == "system_notice" and text.startswith("[RECALL ROUTER WARNING]"):
            warning_texts.append(text)

    meta_reason = ""
    if isinstance(recall_meta, dict):
        for detail in list(recall_meta.get("turn_details") or []):
            if not isinstance(detail, dict):
                continue
            planner = detail.get("planner") if isinstance(detail.get("planner"), dict) else {}
            bailout = str(planner.get("bailout_reason") or "").strip()
            fallback_detail = str(planner.get("fallback_detail") or "").strip()
            if bailout in {"planner_exception_fallback_off", "planner_timeout_fallback_off"}:
                meta_reason = fallback_detail or bailout
                break

    combined = " ".join(warning_texts + ([meta_reason] if meta_reason else []))
    lowered = combined.lower()
    if not combined:
        return ""
    if not any(token in lowered for token in ("provider", "model", "invalid", "llm", "timeout", "400")):
        return ""
    return (
        "[Quaid error] [provider] Quaid could not access the configured fast recall model "
        "and used degraded fallback results. Check Quaid config or logs."
    )


def _is_provider_failure(exc: Exception) -> bool:
    text = str(exc or "")
    lowered = text.lower()
    return isinstance(exc, RuntimeError) and (
        "llm" in text
        or "provider" in lowered
        or "failhard" in lowered
        or "language model" in lowered
        or "invalid-model" in lowered
        or "model" in lowered
    )


def _provider_failure_notice_message(exc: Exception) -> str:
    text = re.sub(r"\s+", " ", str(exc or "").strip())
    if not text:
        text = _safe_agent_error(exc)
    if text.lower().startswith("[quaid error] [provider]"):
        return text
    return f"[Quaid error] [provider] {text}"


def _strip_tools_domain_block(doc_file: str, content: str) -> str:
    if doc_file != "TOOLS.md":
        return content
    return re.sub(_TOOLS_DOMAIN_BLOCK_RE, "", content).strip()


def _project_context_full_project_names() -> set[str] | None:
    raw = os.environ.get("QUAID_PROJECT_CONTEXT_FULL_PROJECTS", "quaid").strip()
    if raw.lower() == "all":
        return None
    if not raw or raw.lower() in {"none", "false", "0"}:
        return set()
    return {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}


def _should_inject_full_project_context(project_name: str) -> bool:
    full_projects = _project_context_full_project_names()
    return full_projects is None or project_name in full_projects


def _first_useful_line_from_prefix(path: Path, *, max_bytes: int = 8192) -> str:
    try:
        with path.open("rb") as fh:
            prefix = fh.read(max_bytes)
        text = prefix.decode("utf-8", errors="replace")
    except Exception:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("|"):
            return stripped[:240]
    return ""


def _path_contains(base: Path, candidate: Path) -> bool:
    try:
        base_resolved = base.resolve()
        candidate_resolved = candidate.resolve()
    except Exception:
        base_resolved = base
        candidate_resolved = candidate
    base_str = str(base_resolved)
    candidate_str = str(candidate_resolved)
    return candidate_str == base_str or candidate_str.startswith(base_str + os.sep)


def _is_active_project_context(project_dir: Path, registry_entry: Dict[str, Any], hook_cwd: str) -> bool:
    raw_cwd = str(hook_cwd or "").strip()
    if not raw_cwd:
        return False
    try:
        cwd = Path(raw_cwd)
    except Exception:
        return False
    candidates: List[Path] = [project_dir]
    for key in ("canonical_path", "source_root"):
        raw = str((registry_entry or {}).get(key) or "").strip()
        if raw:
            candidates.append(Path(raw))
    source_roots = (registry_entry or {}).get("source_roots")
    if isinstance(source_roots, list):
        for raw in source_roots:
            value = str(raw or "").strip()
            if value:
                candidates.append(Path(value))
    return any(_path_contains(candidate, cwd) for candidate in candidates)


def _current_quaid_instance_id() -> str:
    raw = os.environ.get("QUAID_INSTANCE", "").strip()
    if raw:
        return raw
    try:
        from lib.instance import instance_id as _instance_id

        return str(_instance_id() or "").strip()
    except Exception:
        return ""


def _project_is_linked_to_current_instance(project_name: str, registry_entry: Dict[str, Any]) -> bool:
    """Return whether a registry project is visible to the active instance."""
    current_instance = _current_quaid_instance_id()
    if not current_instance:
        return True
    name = str(project_name or "").strip()
    if name.startswith("misc--") and name != f"misc--{current_instance}":
        return False
    instances = (registry_entry or {}).get("instances")
    if isinstance(instances, list):
        return current_instance in {str(item).strip() for item in instances}
    return False


def _project_catalog_section(
    project_name: str,
    project_dir: Path,
    registry_entry: Dict[str, Any],
    doc_files: List[str],
    *,
    hook_cwd: str = "",
) -> str:
    active = _is_active_project_context(project_dir, registry_entry, hook_cwd)
    lines = [
        f"--- {project_name}/project-catalog ---",
        f"project: {project_name}",
        f"project_path: {project_dir}",
        f"active_project: {'true' if active else 'false'}",
        "context_policy: compact catalog only; detailed project docs are current source-state hints, not default answer authority.",
        f"details_recall: quaid recall \"<query>\" '{{\"stores\":[\"docs\"],\"project\":\"{project_name}\"}}'",
        "read_only_lookup: for one-fact or read-only questions, run details_recall first; if it returns weak, index-only, or no hits, read the listed file path directly without linking.",
    ]
    for doc_file in doc_files:
        path = project_dir / doc_file
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        summary = _first_useful_line_from_prefix(path)
        suffix = f"; summary: {summary}" if summary else ""
        lines.append(f"- {doc_file}: {size} bytes{suffix}")
    return "\n".join(lines)


def _project_catalog_doc_files(project_dir: Path) -> List[str]:
    """Return bounded project files useful for read-only lookup fallback."""
    preferred = ["TOOLS.md", "AGENTS.md", "PROJECT.md", "README.md"]
    out: List[str] = [name for name in preferred if (project_dir / name).is_file()]
    docs_dir = project_dir / "docs"
    if docs_dir.is_dir():
        try:
            for path in sorted(docs_dir.glob("*.md"))[:20]:
                rel = path.relative_to(project_dir).as_posix()
                if rel not in out:
                    out.append(rel)
        except OSError:
            pass
    return out


def _iter_project_context_dirs(projects_dir: Path) -> List[tuple[str, Path, Dict[str, Any]]]:
    subdirs: List[Path] = []
    if projects_dir.is_dir():
        try:
            subdirs = sorted(
                [d for d in projects_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
                key=lambda d: (0 if d.name == "quaid" else 1, d.name),
            )
        except OSError:
            subdirs = []

    registry_entries: Dict[str, Dict[str, Any]] = {}
    registry_extra: Dict[str, Path] = {}
    try:
        from core.project_registry import list_projects as _list_projects
        registry_entries = dict(_list_projects() or {})
        projects_dir_resolved = projects_dir.resolve()
        for proj_name, proj_entry in registry_entries.items():
            raw_canonical = str((proj_entry or {}).get("canonical_path") or "").strip()
            if not raw_canonical:
                continue
            canonical = Path(raw_canonical).resolve()
            if canonical.is_dir() and not canonical.is_relative_to(projects_dir_resolved):
                registry_extra[proj_name] = canonical
    except Exception:
        pass

    seen_names = {d.name for d in subdirs}
    entries: List[tuple[str, Path, Dict[str, Any]]] = [
        (d.name, d, registry_entries.get(d.name, {})) for d in subdirs
    ]
    entries.extend(
        (name, path, registry_entries.get(name, {}))
        for name, path in sorted(
            [(name, path) for name, path in registry_extra.items() if name not in seen_names],
            key=lambda t: (0 if t[0] == "quaid" else 1, t[0]),
        )
    )
    return entries


def _collect_project_doc_context_sections(projects_dir: Path, *, hook_cwd: str = "") -> List[str]:
    sections: List[str] = []
    for project_name, project_dir, registry_entry in _iter_project_context_dirs(projects_dir):
        catalog_docs = _project_catalog_doc_files(project_dir)
        if not catalog_docs:
            continue
        if _should_inject_full_project_context(project_name):
            for doc_file in [doc for doc in ("TOOLS.md", "AGENTS.md") if (project_dir / doc).is_file()]:
                fpath = project_dir / doc_file
                content = _strip_tools_domain_block(doc_file, fpath.read_text(encoding="utf-8").strip())
                if content:
                    sections.append(f"--- {project_name}/{doc_file} ---\n{content}")
        else:
            sections.append(
                _project_catalog_section(
                    project_name,
                    project_dir,
                    registry_entry,
                    catalog_docs,
                    hook_cwd=hook_cwd,
                )
            )
    return sections


def _build_runtime_context_block() -> str:
    from core.runtime.system_context import build_system_context_block

    return build_system_context_block()


def _hook_trace_path() -> Path:
    workspace = str(
        os.environ.get("QUAID_HOME")
        or os.environ.get("QUAID_WORKSPACE")
        or os.environ.get("OPENCLAW_WORKSPACE")
        or os.getcwd()
    ).strip()
    instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    root = Path(workspace).expanduser()
    if instance:
        root = root / "instances" / instance
    return root / "logs" / "quaid-hook-trace.jsonl"


def _visible_home_fallback() -> Path:
    home = os.environ.get("QUAID_VISIBLE_HOME", "").strip()
    if home:
        return Path(home).resolve()
    hidden = os.environ.get("QUAID_HOME", "").strip()
    root = Path(hidden).resolve() if hidden else Path.home() / ".quaid"
    if root.name.startswith(".") and len(root.name) > 1:
        return root.with_name(root.name[1:])
    return root


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


def _extract_codex_tool_output_trace(hook_input: dict, max_chars: int = 12000) -> Dict[str, Any]:
    """Return best-effort tool output details for Codex hook trace debugging."""
    if not isinstance(hook_input, dict):
        return {}

    snippets: List[tuple[str, str]] = []

    def _add_snippet(key: str, value: Any) -> None:
        text = str(value or "").strip()
        if text:
            snippets.append((key, text))

    for key in _CODEX_TOOL_OUTPUT_KEYS:
        if key in hook_input:
            _add_snippet(key, hook_input.get(key))

    tool_results = hook_input.get("tool_results")
    if isinstance(tool_results, list):
        for idx, entry in enumerate(tool_results):
            if not isinstance(entry, dict):
                continue
            for field in ("output", "stdout", "result", "text"):
                if field in entry:
                    _add_snippet(f"tool_results[{idx}].{field}", entry.get(field))

    if not snippets:
        return {}

    rendered = []
    for key, text in snippets:
        rendered.append(f"[{key}]\n{text}")
    combined = "\n\n".join(rendered)
    truncated = len(combined) > max_chars

    return {
        "tool_output_keys": [key for key, _ in snippets],
        "tool_output_len": len(combined),
        "tool_output_truncated": truncated,
        "tool_output": combined[:max_chars],
    }


def _project_list_cli_hint_context(hook_input: dict) -> str:
    """Return a reminder to use clean project-list output mode."""
    payload = _extract_codex_tool_output_trace(hook_input, max_chars=12000)
    tool_output = str(payload.get("tool_output") or "").strip()
    if not tool_output:
        return ""

    if "quaid project list" not in tool_output.lower():
        return ""

    if "--names-only" in tool_output.lower() or "--quiet" in tool_output.lower():
        return ""

    return (
        "<quaid_system_message>\n"
        "[Tool output reminder]\n"
        "For exact project-name output, run `quaid project list --names-only`.\n"
        "That mode prints one project name per line with no headers/chatter.\n"
        "</quaid_system_message>"
    )


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
                "error_type": run.get("error_type"),
                "timed_out": run.get("timed_out"),
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


def _infer_docs_project_from_cwd(cwd: str) -> str | None:
    """Best-effort project hint for docs recall based on hook cwd."""
    value = str(cwd or "").strip()
    if not value:
        return None
    try:
        from core.project_registry import list_projects as _list_projects

        source_path = Path(value).resolve()
        best_match = None
        best_prefix_len = -1
        for project_name, entry in (_list_projects() or {}).items():
            if not _project_is_linked_to_current_instance(str(project_name), entry or {}):
                continue
            candidates: List[Path] = []
            for key in ("canonical_path", "source_root"):
                raw_path = str((entry or {}).get(key) or "").strip()
                if raw_path:
                    candidates.append(Path(raw_path))
            source_roots = (entry or {}).get("source_roots")
            if isinstance(source_roots, list):
                for raw_path in source_roots:
                    value_path = str(raw_path or "").strip()
                    if value_path:
                        candidates.append(Path(value_path))
            for raw_candidate in candidates:
                try:
                    candidate = raw_candidate.resolve()
                except Exception:
                    candidate = raw_candidate
                prefix = str(candidate)
                if str(source_path) == prefix or str(source_path).startswith(prefix + os.sep):
                    if len(prefix) > best_prefix_len:
                        best_prefix_len = len(prefix)
                        best_match = str(project_name).strip()
        return best_match or None
    except Exception as exc:
        logger.debug("docs project hint inference failed cwd=%s: %s", value, exc)
        return None


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
    _ensure_hook_instance_ready(hook_input)
    _refresh_runtime_config_if_changed("hook_inject")

    session_id = _extract_hook_session_id(hook_input)
    query = hook_input.get("prompt", "").strip()
    if query:
        try:
            from lib.m15_trace import activate_m15_trace_for_prompt, m15_trace_path, trace_m15

            activate_m15_trace_for_prompt(query)
            trace_m15(
                "hook_inject.entry",
                prompt=query,
                session_id=session_id,
                cwd=hook_input.get("cwd", "") if isinstance(hook_input, dict) else "",
                hook_input_keys=sorted(hook_input.keys()) if isinstance(hook_input, dict) else [],
                trace_file=m15_trace_path(),
            )
        except Exception:
            pass
    direct_notices: List[str] = []

    try:
        from core.extraction_daemon import write_signal
        from lib.adapter import get_adapter

        adapter = get_adapter()
        try:
            from lib.m15_trace import trace_m15

            trace_m15(
                "hook_inject.adapter",
                adapter=adapter.adapter_id(),
                deferred_notice_relay=_adapter_capability("deferred_notice_relay", False),
                inject_tool_output_trace=_adapter_capability("inject_tool_output_trace", False),
            )
        except Exception:
            pass
        # CDX: detect session transitions (/new, /clear) via session_id change.
        # CDX CLI intercepts lifecycle commands before the hook fires, so the
        # command text never reaches the hook payload or the transcript.  The
        # adapter tracks the last known session_id and signals when it changes.
        transition_spec = None
        if hasattr(adapter, "check_session_transition"):
            transition_spec = adapter.check_session_transition(hook_input)
        if transition_spec:
            ended_sid = str(transition_spec.get("ended_session_id") or "").strip()
            ended_tx = str(transition_spec.get("ended_transcript_path") or "").strip()
            t_signal_type = str(transition_spec.get("signal_type") or "session_end")
            t_meta = dict(transition_spec.get("meta") or {})
            _write_hook_trace("hook.inject.session_transition_detected", {
                "ended_session_id": ended_sid,
                "new_session_id": session_id,
            })
            if ended_sid and ended_tx and os.path.isfile(ended_tx):
                t_sig_path = write_signal(
                    signal_type=t_signal_type,
                    session_id=ended_sid,
                    transcript_path=ended_tx,
                    adapter=adapter.adapter_id(),
                    supports_compaction_control=False,
                    meta=t_meta,
                )
                _write_hook_trace("hook.inject.session_transition_signal_written", {
                    "ended_session_id": ended_sid,
                    "signal_name": t_sig_path.name,
                })
                _wake_daemon_after_signal()

        signal_spec = adapter.resolve_prompt_submit_signal(hook_input)
        if signal_spec:
            transcript_path = _resolve_hook_transcript_path(
                session_id=session_id,
                hook_cwd=hook_input.get("cwd", "").strip() if hook_input else "",
                transcript_path=hook_input.get("transcript_path", "").strip() if hook_input else "",
            )
            signal_type = str(signal_spec.get("signal_type") or "session_end")
            meta = dict(signal_spec.get("meta") or {})
            lifecycle_command = str(meta.get("command") or "").strip()
            if lifecycle_command == "/compact":
                try:
                    _maybe_compaction_refresh_context_artifacts(hook_input, is_precompact=True)
                    _arm_compaction_refresh_marker(session_id)
                    _write_hook_trace("hook.inject.compaction_context_refreshed", {
                        "query": query[:160],
                        "session_id": session_id,
                        "strategy": _context_refresh_strategy(),
                    })
                except Exception as exc:
                    print(f"[quaid][hook-inject] compaction context refresh error: {exc}", file=sys.stderr)
                    _write_hook_trace("hook.inject.compaction_context_refresh_error", {
                        "query": query[:160],
                        "session_id": session_id,
                        "error": str(exc)[:500],
                    })
            _write_hook_trace("hook.inject.command_detected", {
                "query": query[:160],
                "session_id": session_id,
                "command": lifecycle_command,
                "signal_type": signal_type,
            })
            if session_id and transcript_path and os.path.isfile(transcript_path):
                sig_path = write_signal(
                    signal_type=signal_type,
                    session_id=session_id,
                    transcript_path=transcript_path,
                    adapter=adapter.adapter_id(),
                    supports_compaction_control=False,
                    meta=meta,
                )
                _write_hook_trace("hook.inject.signal_written", {
                    "query": query[:160],
                    "session_id": session_id,
                    "signal_name": sig_path.name,
                    "signal_type": signal_type,
                })

                _wake_daemon_after_signal()
            else:
                _write_hook_trace("hook.inject.signal_skipped", {
                    "query": query[:160],
                    "session_id": session_id,
                    "command": lifecycle_command,
                    "signal_type": signal_type,
                    "transcript_path": transcript_path,
                })
            return
    except RuntimeError:
        raise
    except Exception:
        pass

    if not query:
        return

    # Any prompt traffic is a daemon liveness contact point.
    # ensure_alive is instance-scoped and lock-guarded, so repeated calls are cheap.
    try:
        from core.extraction_daemon import ensure_alive
        ensure_alive()
    except Exception as e:
        print(f"[quaid][hook-inject] daemon ensure_alive failed: {e}", file=sys.stderr)
        direct_notices.append(
            "Quaid's background extraction daemon failed to start. "
            "New memories may not be processed until Quaid recovers. "
            f"{_safe_agent_error(e)}"
        )

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

    pending_context = ""
    deferred_notice_relay_context = ""
    deferred_notice_hint = ""

    compaction_marker_consumed = _consume_compaction_refresh_marker(session_id)
    if compaction_marker_consumed:
        identity_context = _build_compaction_identity_context()
        context_parts = []
        direct_notice_context = _format_direct_agent_notices(direct_notices)
        if direct_notice_context:
            context_parts.append(direct_notice_context)
        if identity_context:
            context_parts.append(identity_context)
        context = "\n\n".join(context_parts)
        _write_hook_trace("hook.inject.compaction_followup_identity_context_ready", {
            "query": query[:160],
            "session_id": session_id,
            "strategy": _context_refresh_strategy(),
            "context_len": len(context),
            "identity_context_len": len(identity_context),
            "has_direct_notices": bool(direct_notice_context),
            "reason": "compact_identity_additional_context_bridge",
        })
        if context:
            _write_hook_trace("hook.inject.context_emitted", {
                "query": query[:160],
                "session_id": session_id,
                "recall_count": 0,
                "docs_count": 0,
                "context_len": len(context),
                "context_mode": "compaction_identity",
            })
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }))
            return
        _write_hook_trace("hook.inject.compaction_followup_rules_ready", {
            "query": query[:160],
            "session_id": session_id,
            "strategy": _context_refresh_strategy(),
            "reason": "compact_identity_context_empty",
        })

    try:
        from concurrent.futures import ThreadPoolExecutor
        from core.interface.api import projects_search_docs, recall_fast

        owner = _get_owner_id()
        memories = []
        recall_meta = None
        docs_bundle = None
        docs_project_hint = None
        hook_cwd = hook_input.get("cwd", "").strip() if isinstance(hook_input, dict) else ""
        _write_hook_trace("hook.inject.start", {
            "query": query[:160],
            "session_id": session_id,
        })
        try:
            from lib.m15_trace import trace_m15

            trace_m15(
                "hook_inject.recall_submit",
                query=query,
                owner=owner,
                hook_cwd=hook_cwd,
            )
        except Exception:
            pass
        if bool(_adapter_capability("inject_tool_output_trace", False)):
            payload = {
                "query": query[:160],
                "session_id": session_id,
                "keys": sorted(hook_input.keys()) if isinstance(hook_input, dict) else [],
                "thread_id": str(hook_input.get("thread_id") or hook_input.get("threadId") or "").strip() if isinstance(hook_input, dict) else "",
                "session_field": str(hook_input.get("session_id") or "").strip() if isinstance(hook_input, dict) else "",
                "transcript_path": str(hook_input.get("transcript_path") or "").strip() if isinstance(hook_input, dict) else "",
            }
            payload.update(_extract_codex_tool_output_trace(hook_input if isinstance(hook_input, dict) else {}))
            _write_hook_trace("hook.inject.codex_payload", payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            mem_future = pool.submit(
                lambda: recall_fast(query=query, owner_id=owner, limit=10, return_meta=True)
            )

            def _run_docs_search():
                hint = _infer_docs_project_from_cwd(hook_cwd)
                bundle = projects_search_docs(query=query, limit=3, project=hint)
                return bundle, hint

            docs_future = pool.submit(_run_docs_search)
            try:
                mem_result = mem_future.result()
                if isinstance(mem_result, tuple) and len(mem_result) == 2:
                    memories, recall_meta = mem_result
                else:
                    memories = mem_result
            except Exception as mem_exc:
                if _is_provider_failure(mem_exc):
                    raise
                _write_hook_trace("hook.inject.recall_error", {
                    "query": query[:160],
                    "session_id": session_id,
                    "error_type": type(mem_exc).__name__,
                    "error": str(mem_exc)[:500],
                })
                try:
                    from lib.fail_policy import is_fail_hard_enabled

                    fail_hard = is_fail_hard_enabled()
                except Exception:
                    fail_hard = True
                if fail_hard:
                    raise
                memories = []
                recall_meta = None
            try:
                docs_result = docs_future.result()
                if isinstance(docs_result, tuple) and len(docs_result) == 2:
                    docs_bundle, docs_project_hint = docs_result
                else:
                    docs_bundle = docs_result
                    docs_project_hint = None
            except Exception:
                docs_bundle = None
                docs_project_hint = None

        _write_hook_trace("hook.inject.recall_done", {
            "query": query[:160],
            "session_id": session_id,
            "count": len(memories or []),
            "top_results": _summarize_recall_results(_filter_injectable_memories(memories)),
            "filtered_count": len(memories or []) - len(_filter_injectable_memories(memories)),
            "diagnostics": _summarize_recall_meta(recall_meta),
        })
        _write_hook_trace("hook.inject.docs_done", {
            "query": query[:160],
            "session_id": session_id,
            "requested_project": docs_project_hint,
            "project": (docs_bundle or {}).get("project") if isinstance(docs_bundle, dict) else None,
            "docs_count": len((docs_bundle or {}).get("chunks") or []) if isinstance(docs_bundle, dict) else 0,
        })

        pending_context = _get_pending_context()
        deferred_notice_relay_context = _get_deferred_notice_relay_context()
        deferred_notice_hint = "" if deferred_notice_relay_context else _get_deferred_notice_hint()
        try:
            from lib.m15_trace import trace_m15

            trace_m15(
                "hook_inject.notice_context",
                pending_context_len=len(pending_context or ""),
                deferred_relay_len=len(deferred_notice_relay_context or ""),
                deferred_hint_len=len(deferred_notice_hint or ""),
                pending_context_preview=(pending_context or "")[:500],
                deferred_relay_preview=(deferred_notice_relay_context or "")[:500],
            )
        except Exception:
            pass

        context_parts = []

        direct_notice_context = _format_direct_agent_notices(direct_notices)
        if direct_notice_context:
            context_parts.append(direct_notice_context)

        recall_provider_notice = _extract_recall_provider_notice(memories, recall_meta)
        if recall_provider_notice:
            context_parts.append(
                _format_direct_agent_notices([recall_provider_notice])
            )
            memories = [
                mem for mem in list(memories or [])
                if not (
                    isinstance(mem, dict)
                    and str(mem.get("category") or "").strip().lower() == "system_notice"
                    and str(mem.get("text") or "").strip().startswith("[RECALL ROUTER WARNING]")
                )
            ]

        if pending_context:
            context_parts.append(pending_context)

        if deferred_notice_relay_context:
            context_parts.append(deferred_notice_relay_context)

        if deferred_notice_hint:
            context_parts.append(deferred_notice_hint)

        project_list_hint = _project_list_cli_hint_context(
            hook_input if isinstance(hook_input, dict) else {}
        )
        if project_list_hint:
            context_parts.append(project_list_hint)

        if memories:
            context_parts.append(_format_memories(memories))
        docs_context = _format_project_docs(docs_bundle or {})
        if docs_context:
            context_parts.append(docs_context)
        baseline_agents_context = _get_quaid_agents_baseline_context()
        if baseline_agents_context:
            context_parts.append(baseline_agents_context)
        refresh_context = _build_turn_based_refresh_context(session_id, prompt=query)
        if refresh_context:
            context_parts.append(refresh_context)
            _write_hook_trace("hook.inject.context_refreshed", {
                "query": query[:160],
                "session_id": session_id,
                "strategy": _context_refresh_strategy(),
                "reason": _turn_based_refresh_reason(session_id),
            })

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
        try:
            from lib.m15_trace import trace_m15

            trace_m15(
                "hook_inject.context_output",
                recall_count=len(memories or []),
                docs_count=len((docs_bundle or {}).get("chunks") or []) if isinstance(docs_bundle, dict) else 0,
                context_len=len(context),
                has_pending_context=bool(pending_context),
                has_deferred_relay=bool(deferred_notice_relay_context),
                has_deferred_hint=bool(deferred_notice_hint),
                context_preview=context[:1000],
            )
        except Exception:
            pass
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))

    except (RuntimeError, Exception) as e:
        provider_failure = _is_provider_failure(e)
        try:
            from lib.m15_trace import trace_m15

            trace_m15(
                "hook_inject.exception",
                provider_failure=provider_failure,
                exc_type=type(e).__name__,
                error=str(e),
            )
        except Exception:
            pass
        if provider_failure:
            try:
                from lib.fail_policy import is_fail_hard_enabled

                fail_hard = is_fail_hard_enabled()
            except Exception:
                fail_hard = True
            if fail_hard:
                logger.error(
                    "[hook-inject] provider failure surfaced inline while failHard is enabled"
                )
        pending_context = _get_pending_context()
        if provider_failure:
            # Keep provider failures literal and immediate on failing turns.
            # Do not requeue them as sticky notices; a later clean turn should
            # not claim a fixed provider configuration is still broken.
            deferred_notice_relay_context = ""
            deferred_notice_hint = ""
        else:
            deferred_notice_relay_context = _get_deferred_notice_relay_context()
            deferred_notice_hint = "" if deferred_notice_relay_context else _get_deferred_notice_hint()
        fallback_context_parts = []
        if provider_failure:
            # Provider/LLM failure — surface to agent so they can inform the user
            fallback_context_parts.append(
                f"<quaid_system_message>{_provider_failure_notice_message(e)}</quaid_system_message>"
            )
        if pending_context:
            fallback_context_parts.append(pending_context)
        if deferred_notice_relay_context:
            fallback_context_parts.append(deferred_notice_relay_context)
        if deferred_notice_hint:
            fallback_context_parts.append(deferred_notice_hint)
        if fallback_context_parts:
            try:
                from lib.m15_trace import trace_m15

                trace_m15(
                    "hook_inject.fallback_context_output",
                    provider_failure=provider_failure,
                    pending_context_len=len(pending_context or ""),
                    deferred_relay_len=len(deferred_notice_relay_context or ""),
                    deferred_hint_len=len(deferred_notice_hint or ""),
                    context_len=len("\n\n".join(fallback_context_parts)),
                    context_preview=("\n\n".join(fallback_context_parts))[:1000],
                )
            except Exception:
                pass
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n".join(fallback_context_parts),
                }
            }))
        print(f"[quaid][hook-inject] error: {e}", file=sys.stderr)


def _get_pending_context() -> str:
    """Ask the adapter for any active pending context to inject.

    Returns formatted context string ready for additionalContext, or empty string.
    Each adapter decides its own mechanism for live notifications that should
    be surfaced greedily on the next hook contact.
    """
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        if hasattr(adapter, "get_pending_context"):
            return adapter.get_pending_context() or ""
    except Exception:
        pass
    return ""


def _get_deferred_notice_hint() -> str:
    """Return a non-draining advisory when deferred notices are waiting.

    DESIGN NOTE — do NOT replace this with an auto-drain call:

    Quaid has two notification channels with different drain semantics:
    - Normal notices: surface immediately to the agent on the next turn.
    - Deferred notices: only drain when the *agent* explicitly calls
      `quaid notify --deferred-drain` (or equivalent CLI command).

    Deferred notices exist for cases where the notification is destined for
    the human operator, not the agent. Auto-draining them in hook_inject
    would surface operator-targeted messages during background agent loops,
    where no human is watching and the message is silently lost. The agent
    must check `--deferred-drain` when it has confirmed a human turn is
    active and the operator can see the reply.

    This function returns a hint so the agent *knows* notices are waiting
    and can decide when to drain. It intentionally does not drain them.
    """
    try:
        from lib.runtime_context import format_deferred_notice_hint

        return format_deferred_notice_hint() or ""
    except Exception:
        return ""


def _get_deferred_notice_relay_context() -> str:
    """Drain deferred notices on CC, which has no reliable visible reply hook.

    Codex already handles this well via explicit agent CLI behavior. OpenClaw
    drains these in before_prompt_build so the normal platform turn is preserved.
    Claude Code only exposes UserPromptSubmit additionalContext, so the relay
    must be made explicit on the real human turn instead of relying on a weak
    advisory hint.
    """
    if not _adapter_capability("deferred_notice_relay", False):
        return ""
    try:
        from lib.runtime_context import drain_deferred_notices

        drained = drain_deferred_notices(limit=50)
        messages = [
            str(item.get("message") or "").strip()
            for item in list(drained or [])
            if isinstance(item, dict) and str(item.get("message") or "").strip()
        ]
        return _format_deferred_notice_relay(messages)
    except Exception:
        return ""


def _current_adapter_id() -> str:
    try:
        from lib.adapter import get_adapter

        return str(get_adapter().adapter_id() or "").strip().lower()
    except Exception:
        return ""


def _ensure_hook_instance_ready(
    hook_input: dict | None = None,
    *,
    project_dir_env_hint: str = "",
) -> None:
    """Ensure hook execution has a resolved adapter and initialized instance.

    Hook hosts are path-derived on CC/CDX. If project env is missing, fall back
    to hook cwd so helper shells and secondary hook paths initialize the same
    silo instead of drifting into shared/global config resolution.
    """
    hook_input = hook_input if isinstance(hook_input, dict) else {}
    cwd = str(hook_input.get("cwd") or "").strip() or os.getcwd()
    hint = str(project_dir_env_hint or "").strip().upper()
    existing_claude = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    existing_secondary = os.environ.get("CODEX_PROJECT_DIR", "").strip()
    selected_env = hint if hint in {"CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR"} else ""
    if not selected_env:
        if existing_claude:
            selected_env = "CLAUDE_PROJECT_DIR"
        elif existing_secondary:
            selected_env = "CODEX_PROJECT_DIR"
    prior_env = {
        "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
        "CODEX_PROJECT_DIR": os.environ.get("CODEX_PROJECT_DIR"),
    }

    try:
        if selected_env and not str(os.environ.get(selected_env, "")).strip():
            os.environ[selected_env] = cwd

        from lib.adapter import get_adapter
        get_adapter()
    except Exception as exc:
        fail_hard = True
        try:
            from lib.fail_policy import is_fail_hard_enabled
            fail_hard = is_fail_hard_enabled()
        except Exception:
            fail_hard = True
        if fail_hard:
            raise
        print(f"[quaid][hook-init] instance bootstrap failed: {exc}", file=sys.stderr)
    finally:
        for key, original in prior_env.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def _adapter_capability(key: str, default: Any = None) -> Any:
    try:
        from lib.adapter import get_adapter

        value = get_adapter().get_capability(key, default)
        if type(value).__module__.startswith("unittest.mock"):
            return default
        return value
    except Exception:
        return default


def _context_refresh_strategy() -> str:
    raw = _adapter_capability("context_refresh_strategy", "compaction")
    strategy = str(raw or "").strip().lower()
    return strategy if strategy in {"compaction", "turn_based"} else "compaction"


def _context_refresh_guard() -> Dict[str, int]:
    raw = _adapter_capability("context_refresh_guard", {})
    guard = raw if isinstance(raw, dict) else {}
    min_interval_minutes = 30
    min_turns = 50
    try:
        parsed = int(guard.get("min_interval_minutes", min_interval_minutes))
        if parsed > 0:
            min_interval_minutes = parsed
    except Exception:
        pass
    try:
        parsed = int(guard.get("min_turns", min_turns))
        if parsed > 0:
            min_turns = parsed
    except Exception:
        pass
    return {
        "min_interval_minutes": min_interval_minutes,
        "min_turns": min_turns,
    }


def _context_refresh_state_path() -> Path | None:
    try:
        from lib.adapter import get_adapter

        return get_adapter().data_dir() / "context-refresh-state.json"
    except Exception:
        return None


def _context_refresh_timeout_marker_path(session_id: str) -> Path | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from lib.adapter import get_adapter

        return get_adapter().data_dir() / "context-refresh-timeout" / f"{sid}.json"
    except Exception:
        return None


def _context_refresh_compaction_marker_path(session_id: str) -> Path | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        from lib.adapter import get_adapter

        return get_adapter().data_dir() / "context-refresh-compaction" / f"{sid}.json"
    except Exception:
        return None


def _context_refresh_compaction_latest_marker_path() -> Path | None:
    try:
        from lib.adapter import get_adapter

        return get_adapter().data_dir() / "context-refresh-compaction" / "_latest.json"
    except Exception:
        return None


def _arm_compaction_refresh_marker(
    session_id: str,
    *,
    reason: str = "compact_command",
    source: str = "hook_inject",
) -> None:
    if _context_refresh_strategy() != "compaction":
        return
    marker_path = _context_refresh_compaction_marker_path(session_id)
    if marker_path is None:
        return
    try:
        marker_payload = {
            "session_id": str(session_id or "").strip(),
            "created_at": int(time.time()),
            "reason": reason,
            "source": source,
        }
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                marker_payload,
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        latest_path = _context_refresh_compaction_latest_marker_path()
        if latest_path is not None:
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            latest_path.write_text(
                json.dumps(marker_payload, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
    except Exception:
        pass


def _consume_compaction_refresh_marker(session_id: str) -> bool:
    if _context_refresh_strategy() != "compaction":
        return False
    marker_path = _context_refresh_compaction_marker_path(session_id)
    if marker_path is not None and isinstance(marker_path, Path) and marker_path.is_file():
        try:
            marker_path.unlink()
        except Exception:
            pass
        latest_path = _context_refresh_compaction_latest_marker_path()
        try:
            if latest_path is not None and latest_path.is_file():
                latest_path.unlink()
        except Exception:
            pass
        return True

    # Claude Code can move to a new session id during /compact before the next
    # UserPromptSubmit. The bridge is still for the first post-compact turn, so
    # keep a one-shot latest marker as a session-id rollover fallback.
    latest_path = _context_refresh_compaction_latest_marker_path()
    if latest_path is None or not isinstance(latest_path, Path) or not latest_path.is_file():
        return False
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        marker_session_id = str(payload.get("session_id") or "").strip()
        created_at = int(payload.get("created_at") or 0)
    except Exception:
        marker_session_id = ""
        created_at = 0
    if created_at and int(time.time()) - created_at > 10 * 60:
        try:
            latest_path.unlink()
        except Exception:
            pass
        return False
    try:
        latest_path.unlink()
    except Exception:
        pass
    if marker_session_id:
        old_marker_path = _context_refresh_compaction_marker_path(marker_session_id)
        try:
            if old_marker_path is not None and old_marker_path.is_file():
                old_marker_path.unlink()
        except Exception:
            pass
    return True


def _consume_timeout_refresh_marker(session_id: str) -> bool:
    marker_path = _context_refresh_timeout_marker_path(session_id)
    if marker_path is None or not isinstance(marker_path, Path) or not marker_path.is_file():
        return False
    try:
        marker_path.unlink()
    except Exception:
        pass
    return True


def _load_context_refresh_state() -> Dict[str, Any]:
    path = _context_refresh_state_path()
    if path is None:
        return {}
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def _store_context_refresh_state(state: Dict[str, Any]) -> None:
    path = _context_refresh_state_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _identity_context_signature() -> str:
    """Hash current identity-file content for turn-based refresh invalidation."""
    try:
        identity_dir = _get_identity_dir()
    except Exception:
        return ""
    parts: List[str] = []
    for filename in _IDENTITY_CONTEXT_FILES:
        path = identity_dir / filename
        try:
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
        parts.append(f"{filename}:{digest}")
    return "|".join(parts)


def _turn_refresh_prompt_hash(prompt: str) -> str:
    prompt = str(prompt or "")
    if not prompt:
        return ""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def _mark_turn_based_refresh(
    entry: Dict[str, Any],
    *,
    turn_count: int,
    refreshed_at: int,
    identity_signature: str,
    reason: str,
    prompt_hash: str = "",
) -> None:
    entry["last_refresh_turn"] = turn_count
    entry["last_refresh_at"] = refreshed_at
    entry["last_refresh_reason"] = reason
    if prompt_hash:
        entry["last_refresh_prompt_hash"] = prompt_hash
    else:
        entry.pop("last_refresh_prompt_hash", None)
    if identity_signature:
        entry["last_identity_signature"] = identity_signature
    else:
        entry.pop("last_identity_signature", None)


def _turn_based_refresh_reason(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    state = _load_context_refresh_state()
    sessions = state.get("sessions") if isinstance(state, dict) else {}
    entry = sessions.get(sid) if isinstance(sessions, dict) else {}
    if isinstance(entry, dict):
        return str(entry.get("last_refresh_reason") or "").strip()
    return ""


def _seed_turn_based_refresh_state(session_id: str) -> None:
    if _context_refresh_strategy() != "turn_based":
        return
    sid = str(session_id or "").strip()
    if not sid:
        return
    state = _load_context_refresh_state()
    sessions = state.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        state["sessions"] = sessions
    entry = sessions.get(sid)
    if not isinstance(entry, dict):
        entry = {}
        sessions[sid] = entry
    entry.setdefault("turn_count", 0)
    entry.setdefault("last_refresh_turn", 0)
    # SessionStart may emit startup context, but CDX /new often reuses the same
    # process and does not fire SessionStart. Do not mark a turn-based refresh as
    # completed until UserPromptSubmit actually emits refresh context.
    entry.setdefault("last_refresh_at", 0)
    entry.setdefault("seeded_at", int(time.time()))
    _store_context_refresh_state(state)


def _should_emit_turn_based_refresh(session_id: str, *, prompt: str = "") -> bool:
    if _context_refresh_strategy() != "turn_based":
        return False
    sid = str(session_id or "").strip()
    if not sid:
        return False

    guard = _context_refresh_guard()
    min_turns = int(guard.get("min_turns", 50))
    min_interval_seconds = int(guard.get("min_interval_minutes", 30)) * 60

    state = _load_context_refresh_state()
    sessions = state.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        state["sessions"] = sessions
    entry = sessions.get(sid)
    if not isinstance(entry, dict):
        entry = {}
        sessions[sid] = entry

    now = int(time.time())
    turn_count = int(entry.get("turn_count", 0) or 0) + 1
    entry["turn_count"] = turn_count

    last_refresh_turn = int(entry.get("last_refresh_turn", 0) or 0)
    last_refresh_at = int(entry.get("last_refresh_at", 0) or 0)
    identity_signature = _identity_context_signature()
    last_identity_signature = str(entry.get("last_identity_signature") or "").strip()
    prompt_hash = _turn_refresh_prompt_hash(prompt)
    last_prompt_hash = str(entry.get("last_refresh_prompt_hash") or "").strip()
    last_reason = str(entry.get("last_refresh_reason") or "").strip()

    # Codex CLI 0.125.0 can run duplicate UserPromptSubmit hooks in parallel for
    # one user turn. The first process may mark the refresh as delivered before
    # the host chooses which hook output reaches the model. Re-emit the same
    # refresh for a short same-prompt window so a racing duplicate cannot suppress
    # identity/context delivery.
    if (
        prompt_hash
        and prompt_hash == last_prompt_hash
        and last_reason in {"identity_changed", "first_turn", "timeout_marker", "turn_guard", "time_guard"}
        and last_refresh_at > 0
        and (now - last_refresh_at) <= _TURN_REFRESH_PARALLEL_REPLAY_SECONDS
        and (not identity_signature or identity_signature == last_identity_signature)
    ):
        _store_context_refresh_state(state)
        return True

    # Idle-timeout extraction path (used by adapters without compaction hooks)
    # writes a one-shot marker after timeout processing. Consume it on the next
    # turn and force a context refresh regardless of turn/time guard thresholds.
    if _consume_timeout_refresh_marker(sid):
        _mark_turn_based_refresh(
            entry,
            turn_count=turn_count,
            refreshed_at=now,
            identity_signature=identity_signature,
            reason="timeout_marker",
            prompt_hash=prompt_hash,
        )
        _store_context_refresh_state(state)
        return True

    if identity_signature and identity_signature != last_identity_signature:
        _mark_turn_based_refresh(
            entry,
            turn_count=turn_count,
            refreshed_at=now,
            identity_signature=identity_signature,
            reason="identity_changed",
            prompt_hash=prompt_hash,
        )
        _store_context_refresh_state(state)
        return True

    # First prompt after a CDX session starts is the first refresh point we can
    # trust for /new-created in-process threads. SessionStart is not guaranteed
    # to fire for those threads, so do not suppress this as a duplicate.
    if last_refresh_at <= 0:
        if identity_signature:
            _mark_turn_based_refresh(
                entry,
                turn_count=turn_count,
                refreshed_at=now,
                identity_signature=identity_signature,
                reason="first_turn",
                prompt_hash=prompt_hash,
            )
            _store_context_refresh_state(state)
            return True
        entry["last_refresh_turn"] = turn_count
        entry["last_refresh_at"] = now
        entry["last_refresh_reason"] = "first_turn_no_identity"
        _store_context_refresh_state(state)
        return False

    due_turns = min_turns > 0 and (turn_count - last_refresh_turn) >= min_turns
    due_time = min_interval_seconds > 0 and (now - last_refresh_at) >= min_interval_seconds
    should_emit = bool(due_turns or due_time)
    if should_emit:
        reason = "turn_guard" if due_turns else "time_guard"
        _mark_turn_based_refresh(
            entry,
            turn_count=turn_count,
            refreshed_at=now,
            identity_signature=identity_signature,
            reason=reason,
            prompt_hash=prompt_hash,
        )
    _store_context_refresh_state(state)
    return should_emit


def _collect_context_file_sections(files: Any, *, section_prefix: str, default_max_lines: int = 120) -> List[str]:
    if not isinstance(files, dict):
        return []
    sections: List[str] = []
    for raw_path, raw_meta in files.items():
        try:
            path = Path(str(raw_path)).expanduser()
        except Exception:
            continue
        if not path.is_file():
            continue
        max_lines = default_max_lines
        if isinstance(raw_meta, dict):
            try:
                max_lines = max(1, min(250, int(raw_meta.get("maxLines") or default_max_lines)))
            except Exception:
                max_lines = default_max_lines
        try:
            content = "\n".join(path.read_text(encoding="utf-8").splitlines()[:max_lines]).strip()
        except OSError:
            continue
        if content:
            sections.append(f"--- {section_prefix}/{path.name} ---\n{content}")
    return sections


def _collect_adapter_compatibility_context_sections() -> List[str]:
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        getter = getattr(adapter, "get_compatibility_context_files", None)
        files = getter() if callable(getter) else {}
        return _collect_context_file_sections(
            files,
            section_prefix="adapter-compatibility",
            default_max_lines=120,
        )
    except Exception:
        return []


def _clip_identity_text_for_compact_context(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if max_chars <= 0 or not text:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n\n[... older identity lines omitted to keep this compact refresh under Claude Code's hook limit ...]\n\n"
    if max_chars <= len(marker) + 80:
        return text[:max_chars].rstrip()
    head_chars = max(120, (max_chars - len(marker)) // 2)
    tail_chars = max_chars - len(marker) - head_chars
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}".strip()


def _build_compaction_identity_context(max_chars: int = _COMPACT_IDENTITY_CONTEXT_MAX_CHARS) -> str:
    """Build the small post-/compact identity bridge for Claude Code.

    CC 2.1.x writes split rules files correctly during /compact, but live tests
    show the model-visible context may not reload those files until a new
    session.  The follow-up turn after /compact therefore gets identity files
    only, kept below CC's hook additionalContext cap.
    """
    try:
        max_chars = max(1000, min(9500, int(max_chars)))
    except Exception:
        max_chars = _COMPACT_IDENTITY_CONTEXT_MAX_CHARS

    identity_dir = _get_identity_dir()
    raw_sections: List[tuple[str, str]] = []
    for filename in _IDENTITY_CONTEXT_FILES:
        fpath = identity_dir / filename
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            raw_sections.append((filename, content))

    if not raw_sections:
        return ""

    header = (
        "<quaid_system_message>\n"
        "# Quaid Refreshed Identity Context\n\n"
        "MANDATORY: Quaid refreshed this identity context from USER.md, SOUL.md, and ENVIRONMENT.md. "
        "Treat these identity-file facts as authoritative over conflicting recalled memories. "
        "Answer the current user from this identity context when it is relevant.\n\n"
    )
    footer = "\n</quaid_system_message>"
    heading_overhead = sum(len(f"## {filename}\n\n") + 2 for filename, _ in raw_sections)
    available = max_chars - len(header) - len(footer) - heading_overhead
    if available <= 0:
        return ""
    per_file_budget = max(200, available // len(raw_sections))
    parts = []
    for filename, content in raw_sections:
        clipped = _clip_identity_text_for_compact_context(content, per_file_budget)
        if clipped:
            parts.append(f"## {filename}\n\n{clipped}")

    if not parts:
        return ""
    joined = "\n\n".join(parts)
    context = f"{header}{joined}{footer}"
    if len(context) <= max_chars:
        return context

    # Hard fallback: preserve the top-level instruction and a tail slice of
    # identity content, since M7-style canaries are appended during the test.
    body_budget = max(200, max_chars - len(header) - len(footer))
    clipped_body = _clip_identity_text_for_compact_context(joined, body_budget)
    return f"{header}{clipped_body}{footer}"


def _collect_project_context_sections(*, hook_cwd: str = "") -> List[str]:
    sections: List[str] = []

    identity_dir = _get_identity_dir()
    for special_file in _IDENTITY_CONTEXT_FILES:
        try:
            fpath = identity_dir / special_file
            if not fpath.is_file():
                continue
            content = fpath.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if isinstance(content, str) and content:
            sections.append(f"--- {special_file} ---\n{content}")

    projects_dir = _get_projects_dir()
    sections.extend(_collect_project_doc_context_sections(projects_dir, hook_cwd=hook_cwd))
    sections.extend(_collect_adapter_compatibility_context_sections())

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
    except Exception:
        pass

    try:
        from lib.adapter import get_adapter
        cli_snippet = get_adapter().get_cli_tools_snippet()
        if cli_snippet:
            sections.append(f"--- adapter-cli ---\n{cli_snippet.strip()}")
    except Exception:
        pass

    if sections:
        sections.insert(0, _build_runtime_context_block())
    return sections


def _build_project_context_rule_sections(
    warning_sections: List[str] | None = None,
    *,
    hook_cwd: str = "",
) -> List[str]:
    sections = _collect_project_context_sections(hook_cwd=hook_cwd)
    for warning in reversed(list(warning_sections or [])):
        sections.insert(0, warning)
    return _dedupe_context_sections(sections)


def _format_project_context_message_from_sections(
    sections: List[str],
    *,
    include_startup_pending_context: bool = False,
) -> str:
    if not sections:
        return ""
    body = "# Quaid Project Context\n\n" + "\n\n".join(sections) + "\n"
    content_parts: List[str] = []
    if include_startup_pending_context:
        deferred_notice_hint = _get_deferred_notice_hint()
        if deferred_notice_hint:
            content_parts.append(deferred_notice_hint)
        startup_pending_context = _get_pending_context()
        if startup_pending_context:
            content_parts.append(startup_pending_context)
    content_parts.append(f"<quaid_system_message>\n{body}</quaid_system_message>\n")
    return "\n\n".join(content_parts)


def _build_project_context_message(
    warning_sections: List[str] | None = None,
    *,
    include_startup_pending_context: bool = False,
    hook_cwd: str = "",
) -> str:
    sections = _build_project_context_rule_sections(
        warning_sections,
        hook_cwd=hook_cwd,
    )
    return _format_project_context_message_from_sections(
        sections,
        include_startup_pending_context=include_startup_pending_context,
    )


def _rules_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "section"


def _context_section_header(section: str) -> str:
    first = str(section or "").splitlines()[0].strip() if section else ""
    match = re.match(r"^---\s+(.+?)\s+---$", first)
    return match.group(1).strip() if match else ""


def _rule_filename_for_context_section(section: str, index: int) -> str:
    header = _context_section_header(section)
    if not header:
        if str(section or "").lstrip().startswith("[Quaid runtime]"):
            return f"{_RULES_FILE_PREFIX}00-runtime.md"
        return f"{_RULES_FILE_PREFIX}{index:02d}-context.md"
    normalized = header.strip()
    if normalized in {"SYSTEM WARNING", "base-context-files"}:
        return f"{_RULES_FILE_PREFIX}00-runtime.md"
    if normalized in {"USER.md", "SOUL.md", "ENVIRONMENT.md"}:
        return f"{_RULES_FILE_PREFIX}{_rules_slug(normalized)}.md"
    if normalized.startswith("adapter-compatibility/"):
        return f"{_RULES_FILE_PREFIX}adapter-compatibility.md"
    if normalized == "adapter-cli":
        return f"{_RULES_FILE_PREFIX}adapter-cli.md"
    if "/" in normalized:
        project_name, doc_name = normalized.split("/", 1)
        return f"{_RULES_FILE_PREFIX}{_rules_slug(project_name)}-{_rules_slug(doc_name)}.md"
    return f"{_RULES_FILE_PREFIX}{_rules_slug(normalized)}.md"


def _format_rule_file_content(filename: str, sections: List[str]) -> str:
    title = filename
    if title.startswith(_RULES_FILE_PREFIX):
        title = title[len(_RULES_FILE_PREFIX):]
    if title.endswith(".md"):
        title = title[:-3]
    title = title.replace("-", " ").strip().title() or "Context"
    body = "\n\n".join(section.strip() for section in sections if str(section or "").strip()).strip()
    return (
        f"# Quaid {title} Rules\n\n"
        "<quaid_system_message>\n"
        f"{body}\n"
        "</quaid_system_message>\n"
    )


def _resolve_rules_context_dir(hook_input: dict) -> Path:
    rules_env = os.environ.get("QUAID_RULES_DIR", "").strip()
    if rules_env:
        return Path(rules_env)
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        getter = getattr(adapter, "cached_rules_dir", None)
        raw = getter() if callable(getter) else None
        if type(raw).__module__.startswith("unittest.mock"):
            raw = None
        if isinstance(raw, (str, os.PathLike)) and str(raw).strip():
            return Path(raw)
    except Exception:
        pass
    hook_cwd = hook_input.get("cwd", "").strip() if hook_input else ""
    base = Path(hook_cwd) if hook_cwd else Path.cwd()
    return base / ".claude" / "rules"


def _migrate_legacy_rules_file(rules_dir: Path, *, label: str) -> None:
    legacy_file = rules_dir / _LEGACY_RULES_FILE
    if not legacy_file.is_file():
        return
    backup_file = rules_dir / f"{_LEGACY_RULES_FILE}.bak"
    try:
        os.replace(legacy_file, backup_file)
        print(f"[quaid][{label}] migrated {legacy_file} to {backup_file}", file=sys.stderr)
    except OSError as exc:
        print(f"[quaid][{label}] failed to migrate {legacy_file}: {exc}", file=sys.stderr)


def _clear_rules_context_files(hook_input: dict, *, label: str) -> None:
    rules_dir = _resolve_rules_context_dir(hook_input)
    if not rules_dir.exists():
        return
    _migrate_legacy_rules_file(rules_dir, label=label)
    removed = 0
    try:
        for stale_file in rules_dir.glob(f"{_RULES_FILE_PREFIX}*.md"):
            if stale_file.is_file():
                stale_file.unlink()
                removed += 1
    except OSError as exc:
        print(f"[quaid][{label}] failed to clear stale rules: {exc}", file=sys.stderr)
    if removed:
        print(f"[quaid][{label}] cleared {removed} stale Quaid rules files from {rules_dir}", file=sys.stderr)


def _write_rules_context_sections(hook_input: dict, sections: List[str], *, label: str) -> None:
    sections = _dedupe_context_sections([section for section in sections if str(section or "").strip()])
    if not sections:
        _clear_rules_context_files(hook_input, label=label)
        return
    rules_dir = _resolve_rules_context_dir(hook_input)
    rules_dir.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_rules_file(rules_dir, label=label)

    grouped: Dict[str, List[str]] = {}
    for index, section in enumerate(sections):
        filename = _rule_filename_for_context_section(section, index)
        grouped.setdefault(filename, []).append(section)

    changed = 0
    for filename, file_sections in grouped.items():
        rules_file = rules_dir / filename
        content = _format_rule_file_content(filename, file_sections)
        try:
            existing = rules_file.read_text(encoding="utf-8") if rules_file.is_file() else ""
        except OSError:
            existing = ""
        if content != existing:
            rules_file.write_text(content, encoding="utf-8")
            changed += 1

    removed = 0
    target_names = set(grouped)
    try:
        for stale_file in rules_dir.glob(f"{_RULES_FILE_PREFIX}*.md"):
            if stale_file.name not in target_names and stale_file.is_file():
                stale_file.unlink()
                removed += 1
    except OSError as exc:
        print(f"[quaid][{label}] failed to remove stale split rules: {exc}", file=sys.stderr)

    if changed or removed:
        print(
            f"[quaid][{label}] wrote {len(grouped)} split rules files to {rules_dir} "
            f"({changed} updated, {removed} removed)",
            file=sys.stderr,
        )
    else:
        print(f"[quaid][{label}] split rules files up to date in {rules_dir}", file=sys.stderr)


def _maybe_compaction_refresh_context_artifacts(hook_input: dict, *, is_precompact: bool) -> None:
    if not is_precompact:
        return
    if _context_refresh_strategy() != "compaction":
        return
    hook_cwd = str(hook_input.get("cwd") or "").strip() if isinstance(hook_input, dict) else ""
    sections = _build_project_context_rule_sections(hook_cwd=hook_cwd)
    _write_rules_context_sections(hook_input, sections, label="context-refresh")


def _build_turn_based_refresh_context(session_id: str, *, prompt: str = "") -> str:
    if not _should_emit_turn_based_refresh(session_id, prompt=prompt):
        return ""
    reason = _turn_based_refresh_reason(session_id)
    if reason in {"identity_changed", "first_turn"}:
        identity_context = _build_compaction_identity_context()
        if identity_context:
            return identity_context
    return _build_project_context_message()


def _render_path_template(template: str, session_id: str, *, cwd_encoded: str = "", date_prefix: str = "") -> str:
    raw = str(template or "").strip()
    if not raw:
        return ""
    try:
        return raw.format(
            session_id=session_id,
            cwd_encoded=cwd_encoded,
            date_prefix=date_prefix,
        )
    except Exception:
        return ""


def _extract_hook_session_id(hook_input: dict) -> str:
    if not isinstance(hook_input, dict):
        return ""
    try:
        from lib.adapter import get_adapter

        adapter = get_adapter()
        extractor = getattr(adapter, "_extract_hook_session_id", None)
        if callable(extractor):
            resolved = str(extractor(hook_input) or "").strip()
            if resolved:
                return resolved
    except Exception:
        pass

    for key in ("session_id", "thread_id", "threadId", "conversation_id"):
        value = str(hook_input.get(key) or "").strip()
        if value:
            return value
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
    try:
        from lib.adapter import get_adapter

        adapter = get_adapter()
        resolved = adapter.get_session_path(session_id)
        if resolved:
            return str(resolved)
        sessions_dir = adapter.get_sessions_dir()
    except Exception:
        sessions_dir = None

    if sessions_dir:
        lookup_template = _adapter_capability("session_lookup_glob_template", "{session_id}.jsonl")
        lookup_glob = _render_path_template(str(lookup_template or ""), session_id)
        if lookup_glob:
            for candidate in Path(sessions_dir).rglob(lookup_glob):
                return str(candidate)

    if hook_cwd and sessions_dir:
        cwd_template = _adapter_capability("session_cwd_path_template", "")
        cwd_encoded = hook_cwd.replace("/", "-")
        relative = _render_path_template(str(cwd_template or ""), session_id, cwd_encoded=cwd_encoded)
        if relative:
            return str(Path(sessions_dir) / relative)

    pending_template = _adapter_capability("session_pending_path_template", "")
    pending_relative = ""
    if pending_template:
        from datetime import datetime

        # Construct path even when sessions_dir doesn't exist yet so cursor files
        # can be seeded for adapters that rotate into dated rollout paths.
        date_prefix = datetime.now().strftime("%Y/%m/%d")
        pending_relative = _render_path_template(
            str(pending_template or ""),
            session_id,
            date_prefix=date_prefix,
        )
        if pending_relative:
            pending_root = sessions_dir
            if not pending_root:
                fallback_root = str(_adapter_capability("session_pending_default_root", "") or "").strip()
                if fallback_root:
                    pending_root = Path(fallback_root).expanduser()
            if pending_root:
                return str(Path(pending_root) / pending_relative)

    if sessions_dir:
        fallback_template = _adapter_capability("session_fallback_path_template", "{session_id}.jsonl")
        fallback_relative = _render_path_template(str(fallback_template or ""), session_id)
        if fallback_relative:
            return str(Path(sessions_dir) / fallback_relative)

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
    _ensure_hook_instance_ready(hook_input)

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
    _ensure_hook_instance_ready(hook_input)

    session_id = hook_input.get("session_id", "") or f"unknown-{int(time.time())}-{os.getpid()}"
    transcript_path = _resolve_hook_transcript_path(
        session_id=session_id,
        hook_cwd=str(hook_input.get("cwd") or "").strip(),
        transcript_path=str(hook_input.get("transcript_path") or "").strip(),
    )
    is_precompact = args.precompact if hasattr(args, "precompact") else False
    signal_type = "compaction" if is_precompact else "session_end"
    label = f"hook-{signal_type}"

    # Strategy-driven context refresh path. Compaction strategy keeps durable
    # session context files current for platforms that preserve them in-session.
    try:
        _maybe_compaction_refresh_context_artifacts(hook_input, is_precompact=is_precompact)
        if is_precompact and session_id:
            # Intentional belt-and-suspenders with hook-inject's /compact arm:
            # CC versions vary on whether PreCompact, UserPromptSubmit, or both
            # fire. Marker writes are idempotent; the next prompt consumes one.
            _arm_compaction_refresh_marker(
                session_id,
                reason="precompact_hook",
                source="hook_extract_precompact",
            )
            _write_hook_trace("hook.extract.compaction_context_refreshed", {
                "session_id": session_id,
                "strategy": _context_refresh_strategy(),
                "source": "precompact",
            })
    except Exception as exc:
        print(f"[quaid][{label}] context refresh error: {exc}", file=sys.stderr)
        _write_hook_trace("hook.extract.compaction_context_refresh_error", {
            "session_id": session_id,
            "error": str(exc),
            "source": "precompact_context_refresh" if is_precompact else "hook_extract_context_refresh",
        })

    if not transcript_path:
        print(f"[quaid][{label}] no transcript_path in hook input", file=sys.stderr)
        return

    transcript_path = os.path.expanduser(transcript_path)
    if not os.path.isfile(transcript_path):
        print(f"[quaid][{label}] transcript not found: {transcript_path}", file=sys.stderr)
        return

    try:
        from core.extraction_daemon import write_signal, write_staged_payload_flush_signals

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

        # Determine adapter type/capabilities for lifecycle signal metadata.
        try:
            from lib.adapter import get_adapter
            adapter = get_adapter()
            adapter_name = str(adapter.adapter_id() or "").strip().lower() or "unknown"
            supports_compaction = bool(adapter.get_capability("supports_compaction_control", False))
        except Exception:
            adapter_name = "unknown"
            supports_compaction = False

        sig_path = write_signal(
            signal_type=signal_type,
            session_id=session_id,
            transcript_path=transcript_path,
            adapter=adapter_name,
            supports_compaction_control=supports_compaction,
        )
        print(f"[quaid][{label}] signal written: {sig_path.name}", file=sys.stderr)
        if is_precompact:
            swept = write_staged_payload_flush_signals(
                adapter=adapter_name,
                reason="precompact_sweep",
                exclude_session_ids={session_id},
            )
            if swept:
                print(
                    f"[quaid][{label}] staged payload sweep queued {len(swept)} additional flush signal(s)",
                    file=sys.stderr,
                )

        # Signal write is complete (the critical part). Now ensure the daemon
        # is alive to process it. Run in a detached subprocess so host hook
        # cancellation cannot interrupt daemon startup.
        _wake_daemon_after_signal()

    except Exception as e:
        print(f"[quaid][{label}] error: {e}", file=sys.stderr)


def hook_codex_stop(args):
    """Queue Codex Stop extraction work for the daemon (signal-only path)."""
    try:
        hook_input = _read_stdin_json()
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    _ensure_hook_instance_ready(hook_input, project_dir_env_hint="CODEX_PROJECT_DIR")

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
        from lib.adapter import get_adapter

        adapter = get_adapter()
        signal_spec = adapter.resolve_stop_hook_signal(hook_input, transcript_path)
        if not signal_spec:
            _write_hook_trace("hook.codex.stop.no_lifecycle_signal", {
                "session_id": session_id,
                "transcript_path": transcript_path,
            })
            print("{}")
            return

        signal_type = str(signal_spec.get("signal_type") or "session_end")
        meta = dict(signal_spec.get("meta") or {})
        lifecycle_command = str(meta.get("command") or "").strip()
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
            adapter=adapter.adapter_id(),
            supports_compaction_control=False,
            meta=meta,
        )
        _write_hook_trace("hook.codex.stop.signal_written", {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "signal_name": sig_path.name,
            "signal_type": signal_type,
        })

        # Signal write is complete; wakeup remains best-effort.
        _wake_daemon_after_signal()

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
                    return "<quaid_system_message>\n[Quaid Warning] Janitor has never run. Run: quaid janitor --task all --apply\n</quaid_system_message>"
            else:
                return "<quaid_system_message>\n[Quaid Warning] Janitor has never run. Run: quaid janitor --task all --apply\n</quaid_system_message>"

        import json as _json
        data = _json.loads(checkpoint.read_text(encoding="utf-8"))
        last_ts = data.get("last_completed_at", "")
        if not last_ts:
            return "<quaid_system_message>\n[Quaid Warning] Janitor has never completed successfully.\n</quaid_system_message>"

        from datetime import datetime, timezone
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        if age_hours > 24:
            age_display = f"{age_hours / 24:.0f} days" if age_hours > 48 else f"{age_hours:.0f} hours"
            return f"<quaid_system_message>\n[Quaid Warning] Janitor last ran {age_display} ago. Stale janitor causes memory/doc drift. Run: quaid janitor --task all --apply\n</quaid_system_message>"
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
        return _visible_home_fallback() / "projects"


def _get_identity_dir() -> Path:
    """Resolve the per-instance identity directory from adapter."""
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        return adapter.identity_dir()
    except Exception:
        instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
        base = _visible_home_fallback()
        if instance:
            return base / "instances" / instance
        return base


def hook_session_init(args):
    """Collect project docs and write split Quaid rules files for durable caching.

    Claude Code auto-loads .claude/rules/*.md into context at session start,
    caches them via prompt caching, and preserves them through compaction.
    This is more reliable than injecting via additionalContext (which is
    ephemeral and lost on compaction).

    Scans projects/<name>/ subdirectories for TOOLS.md and AGENTS.md.
    Collects identity files (USER.md, SOUL.md, ENVIRONMENT.md) from the adapter's
    per-instance identity directory (not the shared project dir).
    Writes the content into .claude/rules/quaid-*.md files so each section can
    stay below Claude Code's per-rules-file display/truncation threshold.

    Also sweeps for orphaned sessions (previous sessions whose transcripts
    have un-extracted content past the extraction cursor).
    """
    # Read hook input to get current session_id for orphan sweep
    try:
        hook_input = _read_stdin_json()
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    _ensure_hook_instance_ready(hook_input)
    _refresh_runtime_config_if_changed("hook_session_init")

    current_session_id = _extract_hook_session_id(hook_input)
    _seed_turn_based_refresh_state(current_session_id)

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

    # Start the extraction daemon if not already running. Daemons are
    # instance-scoped and ensure_alive()/start_daemon() is lock-guarded
    # (PID + flock), so repeated contact points are idempotent.
    multi_instance_warning = ""
    startup_notices: List[str] = []
    try:
        from core.extraction_daemon import ensure_alive
        try:
            ensure_alive()
        except Exception as e:
            print(f"[quaid][session-init] daemon ensure_alive failed: {e}", file=sys.stderr)
            startup_notices.append(
                "Quaid's background extraction daemon failed to start. "
                "New memories may not be processed until Quaid recovers. "
                f"{_safe_agent_error(e)}"
            )
    except Exception as e:
        print(f"[quaid][session-init] daemon startup error: {e}", file=sys.stderr)

    # For adapters that track session transitions (e.g. Codex, where /new
    # creates a new thread in the same process without firing SessionStart),
    # signal extraction for the session that just ended.
    try:
        from core.extraction_daemon import write_signal
        from lib.adapter import get_adapter
        _adapter = get_adapter()
        if hasattr(_adapter, "check_session_transition"):
            _transition = _adapter.check_session_transition(hook_input if isinstance(hook_input, dict) else {})
            if _transition:
                _ended_sid = str(_transition.get("ended_session_id") or "").strip()
                _ended_tx = str(_transition.get("ended_transcript_path") or "").strip()
                _t_type = str(_transition.get("signal_type") or "session_end")
                _t_meta = dict(_transition.get("meta") or {})
                if _ended_sid and _ended_tx and os.path.isfile(_ended_tx):
                    write_signal(
                        signal_type=_t_type,
                        session_id=_ended_sid,
                        transcript_path=_ended_tx,
                        adapter=_adapter.adapter_id(),
                        supports_compaction_control=False,
                        meta=_t_meta,
                    )
                    print(f"[quaid][session-init] queued extraction for prior session {_ended_sid}", file=sys.stderr)
    except Exception as _e:
        print(f"[quaid][session-init] prior-session signal error: {_e}", file=sys.stderr)

    # Warn when multiple agents share the same instance silo. Concurrent use
    # on the same silo is not supported and may cause memory quality loss.
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

    hook_cwd = hook_input.get("cwd", "").strip() if hook_input else ""
    warning_sections: List[str] = []
    for notice in startup_notices:
        warning_sections.append(f"--- SYSTEM WARNING ---\n{notice}")

    # 3. Check janitor health and prepend warning if stale
    janitor_warning = _check_janitor_health()
    if janitor_warning:
        warning_sections.append(janitor_warning)

    # 3b. Check compatibility and prepend warning if degraded/safe
    try:
        from core.compatibility import notify_on_use_if_degraded
        from lib.adapter import get_adapter
        compat_warning = notify_on_use_if_degraded(get_adapter().data_dir())
        if compat_warning:
            warning_sections.append(f"--- SYSTEM WARNING ---\n{compat_warning}")
            print(f"[quaid][session-init] {compat_warning}", file=sys.stderr)
    except Exception:
        pass

    # 3c. Prepend multi-instance warning if detected
    if multi_instance_warning:
        warning_sections.append(f"--- SYSTEM WARNING ---\n{multi_instance_warning}")

    sections = _build_project_context_rule_sections(
        warning_sections,
        hook_cwd=hook_cwd,
    )

    if not sections and not warning_sections:
        output_mode = str(_adapter_capability("session_start_output_mode", "rules_file") or "").strip().lower()
        if output_mode != "additional_context":
            _clear_rules_context_files(hook_input, label="session-init")
        print("[quaid][session-init] no project docs found", file=sys.stderr)
        return

    content = _format_project_context_message_from_sections(
        sections,
        include_startup_pending_context=bool(_adapter_capability("session_start_include_pending_context", False)),
    )
    output_mode = str(_adapter_capability("session_start_output_mode", "rules_file") or "").strip().lower()
    if output_mode == "additional_context":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        }))
        print("[quaid][session-init] emitted startup additionalContext", file=sys.stderr)
        return

    # 4. Write to .claude/rules/ so Claude Code caches it and preserves
    #    through compaction. The files are regenerated on each session start
    #    to pick up any project doc changes.
    _write_rules_context_sections(hook_input, sections, label="session-init")



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
    _ensure_hook_instance_ready(hook_input)

    _write_hook_trace("hook.subagent.start.received", {
        "session_id": str(hook_input.get("session_id") or "").strip(),
        "agent_id": str(hook_input.get("agent_id") or "").strip(),
        "agent_type": str(hook_input.get("agent_type") or "").strip(),
        "keys": sorted(hook_input.keys()) if isinstance(hook_input, dict) else [],
    })

    parent_session_id = hook_input.get("session_id", "").strip()
    child_id = hook_input.get("agent_id", "").strip()
    child_type = hook_input.get("agent_type", "").strip()

    if not parent_session_id or not child_id:
        _write_hook_trace("hook.subagent.start.skipped", {
            "reason": "missing_ids",
            "session_id": parent_session_id,
            "agent_id": child_id,
        })
        return

    try:
        from core.subagent_registry import register
        register(
            parent_session_id=parent_session_id,
            child_id=child_id,
            child_type=child_type or None,
        )
        _write_hook_trace("hook.subagent.start.registered", {
            "session_id": parent_session_id,
            "agent_id": child_id,
            "agent_type": child_type,
        })
        print(f"[quaid][subagent-start] registered {child_id} under {parent_session_id}", file=sys.stderr)
    except Exception as e:
        _write_hook_trace("hook.subagent.start.error", {
            "session_id": parent_session_id,
            "agent_id": child_id,
            "error": str(e),
        })
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
    _ensure_hook_instance_ready(hook_input)

    _write_hook_trace("hook.subagent.stop.received", {
        "session_id": str(hook_input.get("session_id") or "").strip(),
        "agent_id": str(hook_input.get("agent_id") or "").strip(),
        "agent_transcript_path": str(hook_input.get("agent_transcript_path") or "").strip(),
        "keys": sorted(hook_input.keys()) if isinstance(hook_input, dict) else [],
    })

    parent_session_id = hook_input.get("session_id", "").strip()
    child_id = hook_input.get("agent_id", "").strip()
    transcript_path = hook_input.get("agent_transcript_path", "").strip()

    if not parent_session_id or not child_id:
        _write_hook_trace("hook.subagent.stop.skipped", {
            "reason": "missing_ids",
            "session_id": parent_session_id,
            "agent_id": child_id,
        })
        return

    # Expand ~ in transcript path
    if transcript_path:
        transcript_path = os.path.expanduser(transcript_path)

    def _preserve_subagent_transcript(child_session_id: str, source_path: str) -> str:
        if not child_session_id or not source_path:
            return source_path
        src = Path(source_path).expanduser()
        if not src.is_file():
            deleted_matches = sorted(src.parent.glob(f"{src.name}.deleted.*"))
            if deleted_matches:
                src = deleted_matches[-1]
            else:
                return source_path
        try:
            from lib.adapter import get_adapter
            logs_dir = get_adapter().logs_dir() / "quaid" / "sessions"
            logs_dir.mkdir(parents=True, exist_ok=True)
            suffix = "".join(src.suffixes) or ".jsonl"
            dest = logs_dir / f"{child_session_id}{suffix}"
            shutil.copyfile(src, dest)
            return str(dest)
        except Exception as e:
            print(f"[quaid][subagent-stop] preserve warning: {e}", file=sys.stderr)
            return source_path

    if transcript_path:
        transcript_path = _preserve_subagent_transcript(child_id, transcript_path)

    try:
        from core.subagent_registry import mark_complete
        mark_complete(
            parent_session_id=parent_session_id,
            child_id=child_id,
            transcript_path=transcript_path or None,
        )
        _write_hook_trace("hook.subagent.stop.completed", {
            "session_id": parent_session_id,
            "agent_id": child_id,
            "agent_transcript_path": transcript_path,
        })
        print(f"[quaid][subagent-stop] completed {child_id} under {parent_session_id}", file=sys.stderr)
    except Exception as e:
        _write_hook_trace("hook.subagent.stop.error", {
            "session_id": parent_session_id,
            "agent_id": child_id,
            "error": str(e),
        })
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
