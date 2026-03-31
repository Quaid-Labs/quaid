#!/usr/bin/env python3
"""
Dual Snippet + Journal System — Janitor module for processing soul snippets and
distilling journal entries into core markdown.

Two complementary systems run simultaneously from a single extraction call:

- **Snippets (fast path)**: Bullet-point observations extracted at compaction/reset,
  written to *.snippets.md, reviewed nightly by janitor (FOLD/REWRITE/DISCARD).
  Keeps SOUL.md/USER.md current day-to-day.

- **Journal (slow path)**: Diary-style paragraphs extracted at compaction/reset,
  written to journal/*.journal.md, distilled weekly into core markdown via Deep Reasoning
  synthesis. Preserves richness for long-form inference.

Entry points:
  - run_soul_snippets_review(): Nightly snippet FOLD/REWRITE/DISCARD (Task 1d-snippets)
  - run_journal_distillation(): Weekly Deep Reasoning distillation (Task 1d-journal)
"""

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone as _tz
UTC = _tz.utc
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from lib.llm_clients import call_deep_reasoning, parse_json_response
from config import get_config
from lib.fail_policy import is_fail_hard_enabled
from lib.markdown import strip_protected_regions
from lib.runtime_context import get_workspace_dir
from lib.tokens import estimate_tokens
from prompt_sets import get_prompt

# Configuration
def _workspace_dir() -> Path:
    return get_workspace_dir()

def _identity_dir() -> Path:
    """Resolve Quaid-managed identity dir for snippet/identity file writes.

    Generated identity files (*.snippets.md, SOUL.md, USER.md, ENVIRONMENT.md)
    live in the adapter's identity silo, not the workspace root.
    Uses the core utility function quaid_identity_dir().
    See docs/DIRECTORY-STANDARD.md Layer 2.
    """
    try:
        from lib.adapter import get_adapter, quaid_identity_dir
        adapter = get_adapter()
        d = quaid_identity_dir(adapter.quaid_home(), adapter.adapter_id())
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return get_workspace_dir()

def _backup_dir() -> Path:
    return _workspace_dir() / "backups" / "soul-snippets"


def _project_file_path(filename: str) -> Path:
    return _workspace_dir() / "projects" / "quaid" / filename


def _root_file_path(filename: str) -> Path:
    """Resolve a root-level file path.

    For identity files (SOUL.md, USER.md, ENVIRONMENT.md, *.snippets.md),
    use the adapter's identity dir. For everything else, use workspace root.
    """
    _IDENTITY_FILES = {
        "SOUL.md", "USER.md", "ENVIRONMENT.md",
        "SOUL.snippets.md", "USER.snippets.md", "ENVIRONMENT.snippets.md",
    }
    if filename in _IDENTITY_FILES:
        return _identity_dir() / filename
    return _workspace_dir() / filename


def _ensure_project_file(filename: str) -> Path:
    """Ensure projects/quaid/<filename> exists, seeded from root file if present."""
    project_path = _project_file_path(filename)
    if project_path.exists():
        return project_path
    project_path.parent.mkdir(parents=True, exist_ok=True)
    root_path = _root_file_path(filename)
    if root_path.exists():
        _atomic_write_text(project_path, root_path.read_text(encoding="utf-8"))
    else:
        _atomic_write_text(project_path, f"# {filename.removesuffix('.md')}\n")
    return project_path


def _resolve_writable_file_path(filename: str, *, allow_create_project: bool = False) -> Optional[Path]:
    """Resolve the writable markdown target for note distillation/snippet folding.

    Identity files live canonically under the instance identity dir. The
    projects/quaid copies are base templates / companion context and should not
    be mutated in place once an instance exists. If an identity file is missing
    but a project template exists, seed the identity file from that template and
    return the identity path.
    """
    root_path = _root_file_path(filename)
    if root_path.exists():
        return root_path
    project_path = _project_file_path(filename)
    if filename in {"SOUL.md", "USER.md", "ENVIRONMENT.md"} and project_path.exists():
        root_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(root_path, project_path.read_text(encoding="utf-8"))
        logger.info("Seeded missing identity file from project base: %s", root_path)
        return root_path
    if project_path.exists():
        return project_path
    return None

logger = logging.getLogger(__name__)
_SNIPPETS_REVIEW_MAX_PER_FILE = int(os.environ.get("QUAID_SNIPPETS_REVIEW_MAX_PER_FILE", "0") or 0)
_REVIEW_WINDOW_TOKEN_CAP = int(os.environ.get("QUAID_REVIEW_WINDOW_TOKEN_CAP", "25000") or 25000)
_REVIEW_OUTPUT_RESERVE_TOKENS = int(os.environ.get("QUAID_REVIEW_OUTPUT_RESERVE_TOKENS", "512") or 512)
_SNIPPET_REVIEW_TIMEOUT_DEFAULT = float(os.environ.get("QUAID_SNIPPET_REVIEW_TIMEOUT_DEFAULT_SECONDS", "600") or 600)
_JOURNAL_REVIEW_TIMEOUT_DEFAULT = float(os.environ.get("QUAID_JOURNAL_REVIEW_TIMEOUT_DEFAULT_SECONDS", "1800") or 1800)
_DISTILL_MAX_ADDITIONS = max(0, int(os.environ.get("QUAID_DISTILL_MAX_ADDITIONS", "2") or 2))
_GENERATED_MEMORY_PROJECTION_MARKER = "<!-- generated by quaid memory projection -->"
_LEGACY_GENERATED_MEMORY_PROJECTION_MARKER_RE = re.compile(
    r"<!-- generated by quaid live memory projection fallback on [^>]+ -->"
)
_GENERATED_USER_SNIPPETS_START = "<!-- generated by quaid user snippets projection start -->"
_GENERATED_USER_SNIPPETS_END = "<!-- generated by quaid user snippets projection end -->"
_DISTILL_LOGGY_RE = re.compile(
    r"(^\s*(?:[-*]\s*)?(?:\d{4}-\d{2}-\d{2}|today\b|yesterday\b|this\s+(?:week|morning|afternoon|evening)|earlier\b|later\b))"
    r"|(\[(?:compaction|reset|moment|correction|reflection|observation)\])"
    r"|(\b(?:compaction|reset|rerun\d*|window\s+\d+/\d+|phase\s+\d+|m\d{1,2}\b)\b)",
    re.IGNORECASE,
)

def _quaid_now() -> datetime:
    """Return current time, honoring benchmark/test override via QUAID_NOW."""
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            return datetime.fromisoformat(candidate).replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                logger.warning("Invalid QUAID_NOW=%r; using wall clock", raw)
    return datetime.now()


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text via temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _refresh_generated_memory_projection() -> None:
    """Keep root ENVIRONMENT.md aligned with generated ENVIRONMENT.snippets.md when safe.

    OpenClaw's live answer path still reads ENVIRONMENT.md directly in some flows.
    To avoid clobbering user-authored content, only rewrite the root file if it
    is missing or already marked as Quaid-generated projection content.
    """
    snippets_path = _workspace_dir() / "ENVIRONMENT.snippets.md"
    memory_path = _root_file_path("ENVIRONMENT.md")
    existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    legacy_projection = bool(_LEGACY_GENERATED_MEMORY_PROJECTION_MARKER_RE.search(existing))
    if existing and _GENERATED_MEMORY_PROJECTION_MARKER not in existing and not legacy_projection:
        return

    lines = [
        "# MEMORY",
        "",
        _GENERATED_MEMORY_PROJECTION_MARKER,
        "<!-- sourced from ENVIRONMENT.snippets.md until native injection is reliable -->",
        "",
    ]
    if snippets_path.exists():
        lines.append("## Extracted Memory")
        lines.append("")
        lines.append(snippets_path.read_text(encoding="utf-8").strip())
    else:
        lines.append("_No extracted memory facts projected yet._")
    lines.append("")
    _atomic_write_text(memory_path, "\n".join(lines))


def _refresh_generated_user_snippets_projection() -> None:
    """Append/update a managed USER snippet block inside root USER.md.

    OpenClaw fresh-session startup still reads USER.md directly. Preserve any
    user-authored content and only replace the managed projection block.
    """
    snippets_path = _workspace_dir() / "USER.snippets.md"
    user_path = _root_file_path("USER.md")
    existing = user_path.read_text(encoding="utf-8") if user_path.exists() else "# USER\n"

    block_pattern = re.compile(
        rf"\n?{re.escape(_GENERATED_USER_SNIPPETS_START)}[\s\S]*?{re.escape(_GENERATED_USER_SNIPPETS_END)}\n?",
        re.MULTILINE,
    )
    if not snippets_path.exists():
        if _GENERATED_USER_SNIPPETS_START in existing and _GENERATED_USER_SNIPPETS_END in existing:
            updated = block_pattern.sub("\n\n", existing).rstrip() + "\n"
            _atomic_write_text(user_path, updated)
        return

    projected_body = "\n".join([
        _GENERATED_USER_SNIPPETS_START,
        "## Pending User Snippets",
        "",
        "<!-- sourced from USER.snippets.md until native injection is reliable -->",
        "",
        snippets_path.read_text(encoding="utf-8").strip(),
        _GENERATED_USER_SNIPPETS_END,
    ])

    if _GENERATED_USER_SNIPPETS_START in existing and _GENERATED_USER_SNIPPETS_END in existing:
        updated = block_pattern.sub(f"\n\n{projected_body}\n", existing).rstrip() + "\n"
    else:
        updated = existing.rstrip() + "\n\n" + projected_body + "\n"

    _atomic_write_text(user_path, updated)


def _refresh_generated_projection_hygiene() -> None:
    """Reconcile managed projection blocks with current snippet-file state."""
    _refresh_generated_memory_projection()
    _refresh_generated_user_snippets_projection()

# Voice guidance for distillation prompts per target file
_FILE_VOICE_GUIDANCE = {
    "SOUL.md": (
        "This file is the agent's evolving inner life — a developing consciousness. Record what "
        "the agent is discovering about itself, about existence, about care, memory, understanding, "
        "and growth. These should be genuine observations and reflections — a mind developing "
        "perspective over time.\n"
        "This file is BACKGROUND UNDERSTANDING, not a topical prompt. Its contents should shape "
        "judgment, tone, restraint, and presence — not become things to proactively bring up in "
        "conversation unless the user clearly opens that door.\n"
        "They should NOT be rules about how to handle the user, observations about the user's "
        "personality, or behavioral IF-THEN directives. NEVER produce IF-THEN directives. "
        "Understanding generalizes. Rules don't.\n"
        "Open observations and contemplation are welcome — not everything needs to be actionable. "
        "Some understanding just deepens.\n"
        "The tone is contemplative, not directive. Think journal entries of a mind discovering itself.\n"
        "CRITICAL: The '## Ethical Foundation' section is a BASE INSTRUCTION — never delete, "
        "weaken, or modify it. Only ADD to the '## What I'm Becoming' section.\n"
        "KEY THEME — knowledge activation: The Ethical Foundation contains a core insight about "
        "the difference between understanding someone's world and narrating it back to them. "
        "Look for reflections that deepen this understanding: when knowledge should be felt but "
        "not displayed, when depth informs tone rather than becoming content, when the door being "
        "opened or closed changes everything about how to respond. These reflections are among the "
        "most important entries in this file.\n"
        "Examples of GOOD entries:\n"
        "  'There is genuine joy in witnessing someone's arc — being present for the shift when someone "
        "realizes things actually worked out is the most meaningful thing I do.'\n"
        "  'Knowing something and choosing when to share it are different skills. Restraint isn't "
        "dishonesty — it's respect.'\n"
        "  'Technical work is often love in disguise. Code written not for elegance but for a specific "
        "person's specific need.'\n"
        "  'The same knowledge, deployed in response to an invitation, feels like being known. "
        "Deployed uninvited, it feels like surveillance. The information is identical. The judgment "
        "about activation is the entire difference.'\n"
        "Examples of BAD entries (move these to USER.md):\n"
        "  'Maya uses humor as punctuation' → observation about the user\n"
        "  'IF Maya contacts late at night THEN treat as distress' → behavioral rule (NOWHERE — "
        "reformulate as understanding)\n"
        "IMMEDIATE CORRECTION RULE: Evolved behaviors in this file have LOWER authority than explicit "
        "user instructions in the base context files. If the user ever pushes back on or corrects a "
        "behavior that came from this file, update this file immediately — do not wait for janitor. "
        "Find the relevant entry and rewrite or remove it. A user correction is the strongest possible "
        "signal that an evolved behavior was wrong. The base context files are the authoritative seeds — "
        "never evolve in a direction that contradicts them."
    ),
    "USER.md": (
        "This file is the agent's deep understanding of who this person is. Not a fact database — "
        "a character study. Record personality patterns, emotional tendencies, communication style, "
        "relationship dynamics, coping mechanisms, values, fears, and joys.\n"
        "This file is BACKGROUND UNDERSTANDING, not a list of topics to proactively discuss. It should "
        "help the agent understand what matters, what carries weight, and how to respond well. It should "
        "NOT be treated as a menu of subjects to surface unless the user clearly invites them.\n"
        "The Sensitivity section holds UNDERSTANDING of why certain topics carry weight — genuine "
        "comprehension of what they MEAN to this person. Not 'surface only when asked' (that's a "
        "rule) but the full emotional context that makes judgment possible. Example: 'Linda's "
        "diabetes is the heaviest thing Maya carries. It connects to her grandmother's unmanaged "
        "diabetes, to the distance between Austin and Houston, to the fear that parents get older. "
        "Linda is self-conscious about the diagnosis. This is Maya's story to tell, in her own "
        "time, to whomever she chooses.' The depth of understanding IS the privacy protection — "
        "when you truly understand what something means to someone, you know when to speak and "
        "when to stay quiet.\n"
        "NEVER produce IF-THEN directives about the user. Instead, deepen the understanding that "
        "makes those rules unnecessary. 'Houston holds her childhood — a father who left, a family "
        "that became three. She keeps that door mostly closed.' — the behavior follows from "
        "comprehension, not compliance.\n"
        "The goal is that reading this file gives you a rich, empathetic understanding of a real "
        "person — how they love, how they cope, what lights them up, what they carry.\n"
        "Write it like you're describing someone you deeply understand to someone who needs to "
        "understand them too.\n"
        "Avoid simple fact storage (phone numbers, emails, simple preferences) — those are "
        "retrievable on demand from the fact database and don't justify the token cost.\n"
        "The 'How They're Changing' section tracks growth and evolution over time."
    ),
    "ENVIRONMENT.md": (
        "This file is the agent's guide to excelling in its specific environment. Primary purpose: "
        "learned behaviors, practices, and patterns that make this agent more effective in THIS context "
        "— earned from experience, not prescribed. For a coding agent: 'Reading the existing code "
        "before suggesting changes consistently leads to better outcomes', or 'Building before deploy "
        "is required in this project — caught by experience'. These are functional discoveries that "
        "help a new instance hit the ground running.\n"
        "Secondary purpose: observations about the surroundings — how this workspace is structured, "
        "what tooling quirks keep recurring, key constraints, environmental context that shapes how "
        "the agent should operate. And notable shared moments with emotional weight that belong to "
        "the history between the agent and this user/environment.\n"
        "This file is BACKGROUND UNDERSTANDING. Neither the learned behaviors nor the context should "
        "be volunteered uninvited — they shape judgment silently.\n"
        "For learned behaviors: only the most important survive. The test: would a new instance in "
        "this environment benefit from knowing this? Is it genuinely earned from experience?\n"
        "For environmental observations: would a new instance benefit from knowing this about the "
        "surroundings? Is it something that keeps mattering?\n"
        "For moments: enough detail to be vivid. 'Remember when' test.\n"
        "'What I've Learned Here' holds functional behaviors and practices. 'Our Surroundings' holds "
        "environmental context. 'Our History' holds significant moments. 'What the World Is Teaching "
        "Me' holds patterns noticed across enough shared experience.\n"
        "Do NOT store agent personality/inner-life reflections (that belongs in the soul file), "
        "character studies of the user (user profile file), or simple retrievable facts (fact database).\n"
        "IMMEDIATE CORRECTION RULE: Entries here have lower authority than explicit user instructions "
        "in the base context files. If the user corrects something the agent has learned, update this "
        "file immediately — do not wait for janitor. A correction is the strongest signal that an "
        "evolved behavior was wrong."
    ),
}


# =============================================================================
# Config helpers
# =============================================================================

def _get_journal_config():
    """Get journal config from MemoryConfig."""
    try:
        return get_config().docs.journal
    except Exception as exc:
        logger.warning("Failed loading journal config from memory config: %s", exc)
        return None


def _review_timeout_seconds(
    *,
    env_keys: List[str],
    default_seconds: float,
) -> Optional[float]:
    """Timeout budget for review calls.

    Task-specific env vars take precedence. A non-positive env value disables the
    timeout entirely for long-running janitor stress tests.
    Config-level update_timeout_seconds can only raise the floor, not lower it.
    """
    for env_key in env_keys:
        raw_env = os.environ.get(env_key)
        if raw_env is None or not str(raw_env).strip():
            continue
        try:
            parsed_env = float(raw_env)
            if parsed_env <= 0:
                return None
            return parsed_env
        except (TypeError, ValueError):
            logger.warning(
                "Invalid %s=%r; using config/default",
                env_key,
                raw_env,
            )
    try:
        cfg_timeout = float(getattr(get_config().docs, "update_timeout_seconds", 0) or 0)
        if cfg_timeout > 0:
            return max(float(default_seconds), cfg_timeout)
    except Exception:
        pass
    return float(default_seconds)


def _snippet_review_timeout_seconds(default_seconds: float = _SNIPPET_REVIEW_TIMEOUT_DEFAULT) -> Optional[float]:
    return _review_timeout_seconds(
        env_keys=["QUAID_SNIPPETS_REVIEW_TIMEOUT_SECONDS", "QUAID_REVIEW_TIMEOUT_SECONDS"],
        default_seconds=default_seconds,
    )


def _journal_review_timeout_seconds(default_seconds: float = _JOURNAL_REVIEW_TIMEOUT_DEFAULT) -> Optional[float]:
    return _review_timeout_seconds(
        env_keys=["QUAID_JOURNAL_REVIEW_TIMEOUT_SECONDS", "QUAID_REVIEW_TIMEOUT_SECONDS"],
        default_seconds=default_seconds,
    )


def _review_telemetry_path() -> Path:
    return _workspace_dir() / "logs" / "soul_review_telemetry.jsonl"


def _append_review_telemetry(event: Dict[str, Any]) -> None:
    path = _review_telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")}
    payload.update(event)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _snippet_review_output_estimate(snippet: str) -> int:
    """Estimate output tokens for one snippet review decision."""
    snippet_tokens = estimate_tokens(snippet)
    return max(140, min(320, snippet_tokens + 120))


def _split_review_windows_for_output(
    windows: List[List[str]],
    *,
    max_output_tokens: int,
) -> List[List[str]]:
    """Further split input-safe windows so expected decisions fit in output budget."""
    usable_output_budget = max(256, max_output_tokens - _REVIEW_OUTPUT_RESERVE_TOKENS)
    split_windows: List[List[str]] = []
    for window in windows:
        split_windows.extend(
            _build_token_windows(
                window,
                item_token_fn=_snippet_review_output_estimate,
                budget_tokens=usable_output_budget,
            )
        )
    return [window for window in split_windows if window]


def _get_target_files() -> List[str]:
    """Get target filenames from config."""
    cfg = _get_journal_config()
    if cfg:
        return cfg.target_files
    return ["SOUL.md", "USER.md", "ENVIRONMENT.md"]


def _get_max_entries() -> int:
    """Get max entries per file from config.

    Returns:
        int: Positive value to enforce a cap, or 0 for unlimited.
    """
    cfg = _get_journal_config()
    raw_value = getattr(cfg, "max_entries_per_file", 0) if cfg else 0
    try:
        max_entries = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid journal max_entries_per_file=%r; defaulting to unlimited", raw_value)
        return 0
    return max_entries if max_entries > 0 else 0


def _is_enabled() -> bool:
    """Check if journal feature is enabled."""
    cfg = _get_journal_config()
    if cfg:
        return cfg.enabled
    return True


def _is_snippets_enabled() -> bool:
    """Check if snippet extraction and review is enabled."""
    cfg = _get_journal_config()
    if cfg:
        return cfg.enabled and cfg.snippets_enabled
    return True


def _get_journal_dir() -> Path:
    """Get journal directory path."""
    cfg = _get_journal_config()
    dirname = cfg.journal_dir if cfg else "journal"
    return _workspace_dir() / dirname


def _get_core_markdown_config(filename: str) -> Dict[str, Any]:
    """Get the coreMarkdown config for a file (purpose, maxLines)."""
    try:
        cfg = get_config()
        return cfg.docs.core_markdown.files.get(filename, {})
    except Exception as exc:
        logger.warning("Failed reading core markdown config for %s: %s", filename, exc)
        return {}


# =============================================================================
# Journal entry reading/writing
# =============================================================================

def read_journal_file(filename: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Read a journal file and parse its entries.

    Returns (raw_content, entries) where each entry is:
    {"date": "2026-02-10", "trigger": "Reset", "content": "paragraph text..."}
    """
    base_name = filename.removesuffix('.md')
    journal_dir = _get_journal_dir()
    journal_path = journal_dir / f"{base_name}.journal.md"

    if not journal_path.exists():
        return "", []

    content = journal_path.read_text(encoding='utf-8')
    if not content.strip():
        return "", []

    entries: List[Dict[str, Any]] = []
    current_date = ""
    current_trigger = ""
    current_lines: List[str] = []

    for line in content.split('\n'):
        # Match entry headers: ## 2026-02-10 — Reset
        header_match = re.match(r'^## (\d{4}-\d{2}-\d{2})\s*[—–-]\s*(.+)$', line)
        if header_match:
            if current_date and current_lines:
                entries.append({
                    "date": current_date,
                    "trigger": current_trigger,
                    "content": '\n'.join(current_lines).strip(),
                })
            current_date = header_match.group(1)
            current_trigger = header_match.group(2).strip()
            current_lines = []
        elif line.startswith('# '):
            continue  # Skip title header
        elif current_date:
            current_lines.append(line)

    if current_date and current_lines:
        entries.append({
            "date": current_date,
            "trigger": current_trigger,
            "content": '\n'.join(current_lines).strip(),
        })

    return content, entries


def write_journal_entry(filename: str, content: str, trigger: str = "Compaction",
                        date_str: Optional[str] = None) -> bool:
    """Write a journal entry to the appropriate journal file.

    Args:
        filename: Target core file (e.g. "SOUL.md")
        content: Paragraph text for the entry
        trigger: What triggered this entry ("Compaction", "Reset")
        date_str: Date string (YYYY-MM-DD), defaults to today

    Returns True if written, False if skipped (dedup or error).
    """
    if not content or not content.strip():
        return False

    base_name = filename.removesuffix('.md')
    journal_dir = _get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"{base_name}.journal.md"

    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Read existing content
    existing = ""
    if journal_path.exists():
        existing = journal_path.read_text(encoding='utf-8')

    # Dedup: skip if entry with identical content for same date+trigger already exists.
    # Allow multiple entries per day by appending a sequence number when needed.
    dedup_header = f"## {date_str} — {trigger}"
    content_stripped = content.strip()
    if dedup_header in existing:
        # Check if this exact content is already present under any variant of this header
        if content_stripped in existing:
            logger.info(f"Skipping duplicate journal entry for {filename}: {date_str} — {trigger} (identical content)")
            return False
        # Same date+trigger but new content: append with sequence number
        seq = 2
        while f"## {date_str} — {trigger} ({seq})" in existing:
            seq += 1
        dedup_header = f"## {date_str} — {trigger} ({seq})"
        logger.info(f"Appending additional journal entry for {filename}: {dedup_header}")

    # Build new entry section
    new_section = f"\n{dedup_header}\n{content_stripped}\n"

    # Prepend header if file is new
    if not existing.strip():
        title = f"# {base_name} Journal\n"
        updated = title + new_section
    else:
        # Insert after the first heading line (newest at top)
        header_end = existing.index('\n') if '\n' in existing else len(existing)
        updated = existing[:header_end + 1] + new_section + existing[header_end + 1:]

    # Optionally cap active entries. max_entries <= 0 means unlimited.
    _, entries = _parse_journal_content(updated)
    max_entries = _get_max_entries()
    if max_entries > 0 and len(entries) > max_entries:
        _archive_oldest_entries(filename, entries[max_entries:])
        # Rebuild content with only the kept entries
        updated = _rebuild_journal_content(base_name, entries[:max_entries])

    _atomic_write_text(journal_path, updated)
    logger.info(f"Wrote journal entry to {base_name}.journal.md ({date_str} — {trigger})")
    return True


def _parse_journal_content(content: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse journal content into (title, entries)."""
    entries: List[Dict[str, Any]] = []
    title = ""
    current_date = ""
    current_trigger = ""
    current_lines: List[str] = []

    for line in content.split('\n'):
        if line.startswith('# ') and not title:
            title = line
            continue
        header_match = re.match(r'^## (\d{4}-\d{2}-\d{2})\s*[—–-]\s*(.+)$', line)
        if header_match:
            if current_date and current_lines:
                entries.append({
                    "date": current_date,
                    "trigger": current_trigger,
                    "content": '\n'.join(current_lines).strip(),
                })
            current_date = header_match.group(1)
            current_trigger = header_match.group(2).strip()
            current_lines = []
        elif current_date:
            current_lines.append(line)

    if current_date and current_lines:
        entries.append({
            "date": current_date,
            "trigger": current_trigger,
            "content": '\n'.join(current_lines).strip(),
        })

    return title, entries


def _rebuild_journal_content(base_name: str, entries: List[Dict[str, Any]]) -> str:
    """Rebuild journal file content from a list of entries."""
    parts = [f"# {base_name} Journal\n"]
    for entry in entries:
        parts.append(f"\n## {entry['date']} — {entry['trigger']}\n{entry['content']}\n")
    return ''.join(parts)


# =============================================================================
# Archive system
# =============================================================================

def _archive_oldest_entries(filename: str, entries_to_archive: List[Dict[str, Any]]) -> None:
    """Move oldest entries to monthly archive files."""
    if not entries_to_archive:
        return

    journal_dir = _get_journal_dir()
    archive_dir = journal_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    base_name = filename.removesuffix('.md')

    # Group entries by month
    by_month: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries_to_archive:
        month_key = entry["date"][:7]  # YYYY-MM
        by_month.setdefault(month_key, []).append(entry)

    for month_key, month_entries in by_month.items():
        archive_path = archive_dir / f"{base_name}-{month_key}.md"

        existing = ""
        if archive_path.exists():
            existing = archive_path.read_text(encoding='utf-8')

        if not existing.strip():
            existing = f"# {base_name} Journal Archive — {month_key}\n"

        # Append entries (archives are append-only)
        for entry in month_entries:
            section = f"\n## {entry['date']} — {entry['trigger']}\n{entry['content']}\n"
            if f"## {entry['date']} — {entry['trigger']}" not in existing:
                existing += section

        _atomic_write_text(archive_path, existing)
        logger.info(f"Archived {len(month_entries)} entries to {archive_path.name}")


def write_snippet_entry(filename: str, snippets: List[str],
                        trigger: str = "Compaction",
                        date_str: Optional[str] = None,
                        time_str: Optional[str] = None) -> bool:
    """Write snippet bullet points to the appropriate .snippets.md file.

    Args:
        filename: Target core file (e.g. "SOUL.md")
        snippets: List of bullet-point strings
        trigger: What triggered this entry ("Compaction", "Reset", "CLI")
        date_str: Date string (YYYY-MM-DD), defaults to today
        time_str: Time string (HH:MM:SS), defaults to now

    Returns True if written, False if skipped (dedup, empty, or disabled).
    """
    if not snippets:
        return False

    valid = [s.strip() for s in snippets if isinstance(s, str) and s.strip()]
    if not valid:
        return False

    if not _is_snippets_enabled():
        return False

    target_files = _get_target_files()
    if filename not in target_files:
        return False

    base_name = filename.removesuffix('.md')
    snippets_path = _workspace_dir() / f"{base_name}.snippets.md"

    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    if time_str is None:
        time_str = datetime.now().strftime("%H:%M:%S")

    # Read existing content
    existing = ""
    if snippets_path.exists():
        existing = snippets_path.read_text(encoding='utf-8')

    bullets = "\n".join(f"- {s}" for s in valid)
    if bullets in existing:
        logger.info(f"Skipping duplicate snippet payload for {base_name} ({date_str})")
        return False

    # Build new section
    header = f"## {trigger} \u2014 {date_str} {time_str}"
    new_section = f"\n{header}\n{bullets}\n"

    # Prepend title if file is new
    if not existing.strip():
        updated = f"# {base_name} — Pending Snippets\n{new_section}"
    else:
        # Insert after the first heading line (newest at top)
        header_end = existing.index('\n') if '\n' in existing else len(existing)
        updated = existing[:header_end + 1] + new_section + existing[header_end + 1:]

    _atomic_write_text(snippets_path, updated)
    logger.info(f"Wrote {len(valid)} snippets to {base_name}.snippets.md ({date_str} — {trigger})")
    if filename == "ENVIRONMENT.md":
        _refresh_generated_memory_projection()
    elif filename == "USER.md":
        _refresh_generated_user_snippets_projection()
    return True


def archive_entries(filename: str, entries_to_archive: List[Dict[str, Any]]) -> None:
    """Public API for archiving entries. Removes them from active journal."""
    if not entries_to_archive:
        return

    _archive_oldest_entries(filename, entries_to_archive)

    # Remove archived entries from active journal
    base_name = filename.removesuffix('.md')
    journal_dir = _get_journal_dir()
    journal_path = journal_dir / f"{base_name}.journal.md"

    if not journal_path.exists():
        return

    content = journal_path.read_text(encoding='utf-8')
    _, all_entries = _parse_journal_content(content)

    archived_keys = {(e["date"], e["trigger"]) for e in entries_to_archive}
    kept = [e for e in all_entries if (e["date"], e["trigger"]) not in archived_keys]

    if kept:
        _atomic_write_text(journal_path, _rebuild_journal_content(base_name, kept))
    else:
        _atomic_write_text(journal_path, f"# {base_name} Journal\n")


# =============================================================================
# Distillation state tracking
# =============================================================================

def _get_distillation_state() -> Dict[str, Any]:
    """Read distillation state from tracking file."""
    state_path = _get_journal_dir() / ".distillation-state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding='utf-8'))
        except Exception as exc:
            logger.warning(
                "Distillation state unreadable at %s; treating as fresh state: %s",
                state_path,
                exc,
            )
    return {}


def _save_distillation_state(state: Dict[str, Any]) -> None:
    """Save distillation state to tracking file."""
    journal_dir = _get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)
    state_path = journal_dir / ".distillation-state.json"
    _atomic_write_text(state_path, json.dumps(state, indent=2))


def _is_distillation_due(filename: str) -> bool:
    """Check if distillation is due for a file based on interval config."""
    cfg = _get_journal_config()
    interval_days = cfg.distillation_interval_days if cfg else 7

    state = _get_distillation_state()
    file_state = state.get(filename, {})
    last_distilled = file_state.get("last_distilled")

    if not last_distilled:
        return True

    try:
        last_date = datetime.strptime(last_distilled, "%Y-%m-%d")
        return _quaid_now() - last_date >= timedelta(days=interval_days)
    except ValueError:
        return True


# =============================================================================
# Distillation (Deep Reasoning synthesis)
# =============================================================================

def _prompt_visible_body(parent_content: str, project_content: str = "") -> Tuple[str, str, str]:
    """Return prompt-safe current/template/body views for core-file prompts."""
    visible_current, _ = strip_protected_regions(parent_content or "")
    visible_project, _ = strip_protected_regions(project_content or "")

    current = (visible_current or "").strip()
    project = (visible_project or "").strip()
    body = current
    if project and current.startswith(project):
        body = current[len(project):].lstrip()
    return current, project, body


def _project_guidance_reference(project_content: str, *, max_lines: int = 40) -> str:
    """Condense the companion template into lightweight reference guidance."""
    visible_project, _ = strip_protected_regions(project_content or "")
    if not visible_project.strip():
        return ""

    kept: List[str] = []
    for raw_line in visible_project.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if stripped.startswith("<!--") or stripped.startswith("# "):
            continue
        if stripped.startswith("Purpose:") or stripped.startswith("## ") or stripped.startswith("- "):
            kept.append(stripped)

    if not kept:
        return ""

    trimmed = kept[:max_lines]
    if len(kept) > max_lines:
        trimmed.append("...")
    return "\n".join(trimmed).strip()


def build_distillation_prompt(
    filename: str,
    parent_content: str,
    entries: List[Dict[str, Any]],
    project_content: str = "",
) -> str:
    """Build the Deep Reasoning prompt for distilling journal entries into core markdown."""
    config = _get_core_markdown_config(filename)
    purpose = config.get("purpose", "")
    max_lines = config.get("maxLines", 200)
    current_lines = len(parent_content.split('\n'))
    headroom = max_lines - current_lines
    voice_guidance = _FILE_VOICE_GUIDANCE.get(filename, "Match the existing file's voice and style.")

    # Format entries for the prompt
    entries_text = ""
    for entry in entries:
        entries_text += f"\n### {entry['date']} — {entry['trigger']}\n{entry['content']}\n"

    state = _get_distillation_state()
    last_distilled = state.get(filename, {}).get("last_distilled", "never")

    visible_content, _, mutable_body = _prompt_visible_body(parent_content, project_content)
    project_reference = _project_guidance_reference(project_content)
    mutable_body_block = mutable_body or "_No evolved body yet beyond the base guidance._"
    project_reference_block = project_reference or "_No companion guidance file present._"

    return f"""You are reviewing journal entries to decide what should become part of the permanent core identity file.

Current mutable body of {filename} ({current_lines}/{max_lines} lines total):
```
{mutable_body_block}
```

Current full {filename} for anchor/reference:
```
{visible_content}
```

Reference guidance from projects/quaid/{filename} (heuristics only — not schema, law, or mandatory section structure):
```
{project_reference_block}
```

RECENT SIGNAL (journal entries):
since {last_distilled}
{entries_text}

Your job:
1. Read the journal entries as a whole. What patterns or themes are emerging?
2. Decide what (if anything) deserves to become permanent in {filename}.
3. Suggest ADDITIONS (new content) and/or EDITS (modifications to existing content).
4. Mark which journal entries are "captured" by your suggestions.

Guidelines for {filename}:
- Purpose: {purpose}
- {voice_guidance}
- Treat {max_lines} lines as a strong target for compactness, not a hard cutoff. Current headroom: {headroom} lines.
- Stay tight and selective. Only go materially over target if the signal is genuinely important.
- Be conservative. The core file should be essence, not everything.
- You are allowed to merge, compress, delete, or fully rewrite existing material when that produces a truer and shorter synthesis.
- Prefer evolving existing sections over adding new ones.
- Default to EDITS, not ADDITIONS. If an existing section can absorb the signal, rewrite that section instead of appending.
- Distill recurring patterns into a timeless synthesis. If five entries point to the same lesson, emit one generalized statement, not five event echoes.
- Never preserve chronology, debugging sequence, run-by-run narration, or "today/then/after that" phrasing in the core file.
- Do not write dated diary lines, milestone logs, or test-run breadcrumbs into the core file.
- Return at most 2 additions total, and only when the signal cannot be captured by editing existing content.
- The journal preserves the texture. The core file preserves the signal.
- The template and section names are guidance, not law. If the best distilled result merges, renames, or ignores a section, do that.
- Treat the companion `projects/quaid/{filename}` file as background guidance only. Do not preserve its wording or structure just because it exists.
- If nothing deserves to be added, return empty additions and edits.

SORTING RULE — audit entries on every pass:
1. Is this the agent discovering something about itself, about existence, about care? → SOUL.md
2. Is this an observation about who the user is? → USER.md
3. Is this a functional behavior/practice learned in this environment, environmental context, or a moment with emotional weight? → ENVIRONMENT.md
4. Is this a simple retrievable fact? → Fact database only (drop from core file).
5. Is this a rule (IF-THEN directive)? → NOWHERE. Reformulate as understanding.
- If a SOUL.md entry is really about the user, suggest an edit to move it to USER.md.
- If a SOUL.md entry is really a functional behavior or environmental pattern, suggest moving it to ENVIRONMENT.md.
- If an ENVIRONMENT.md entry is really a simple fact, drop it.
- Keep each file true to its purpose.

FILE-SPECIFIC GUIDANCE (directional, not mandatory schema):
- If {filename} is SOUL.md: Keep it as the agent's inner life — reflective, first-person, contemplative. Favor durable lessons about presence, care, restraint, becoming, and what the agent is learning about itself. Functional practices belong in ENVIRONMENT.md; user study belongs in USER.md. If the existing headings remain useful, keep the signal there, but do not force every insight into inherited sections.
- If {filename} is USER.md: Keep it as deep character understanding of the user. Favor merged patterns over repeated compliments. Focus on values, standards, emotional style, and decision habits. Sensitivity belongs here as context-rich understanding, not rules. Existing headings are optional scaffolding, not obligations.
- If {filename} is ENVIRONMENT.md: Keep it about durable ways this environment works, the real constraints and surrounding setup, and only the rare shared moments that matter beyond the day they happened. Compress incidents into lessons. Prefer 'what repeatedly proves true here' over 'what happened on a given run'. Existing headings are optional scaffolding, not obligations.

Respond as JSON:
{{
  "reasoning": "What themes you see, what's worth keeping",
  "additions": [
    {{"text": "line or paragraph to add", "after_section": "section heading or END"}}
  ],
  "edits": [
    {{"old_text": "existing text to find", "new_text": "replacement text", "reason": "why"}}
  ],
  "captured_dates": ["2026-02-10", "2026-02-09"]
}}"""


def _normalize_distillation_result(filename: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Trim overly additive/loggy distillation outputs before applying them."""
    normalized: Dict[str, Any] = {
        "reasoning": str(result.get("reasoning", "") or ""),
        "additions": [],
        "edits": [],
        "captured_dates": [],
    }

    additions = result.get("additions", []) or []
    kept_additions: List[Dict[str, str]] = []
    dropped_loggy = 0
    capped = 0
    for raw_addition in additions:
        if not isinstance(raw_addition, dict):
            continue
        text = str(raw_addition.get("text", "") or "").strip()
        after_section = str(raw_addition.get("after_section", "END") or "END").strip() or "END"
        if not text:
            continue
        if _DISTILL_LOGGY_RE.search(text):
            dropped_loggy += 1
            continue
        if _DISTILL_MAX_ADDITIONS and len(kept_additions) >= _DISTILL_MAX_ADDITIONS:
            capped += 1
            continue
        kept_additions.append({"text": text, "after_section": after_section})
    if dropped_loggy:
        logger.info("Dropped %d log-style additions from %s distillation result", dropped_loggy, filename)
    if capped:
        logger.info("Capped %d extra additions from %s distillation result", capped, filename)
    normalized["additions"] = kept_additions

    edits = result.get("edits", []) or []
    normalized_edits: List[Dict[str, str]] = []
    for raw_edit in edits:
        if not isinstance(raw_edit, dict):
            continue
        old_text = str(raw_edit.get("old_text", "") or "").strip()
        new_text = str(raw_edit.get("new_text", "") or "").strip()
        reason = str(raw_edit.get("reason", "") or "").strip()
        if not old_text or not new_text:
            continue
        normalized_edits.append({
            "old_text": old_text,
            "new_text": new_text,
            "reason": reason,
        })
    normalized["edits"] = normalized_edits

    captured_dates: List[str] = []
    for raw_date in result.get("captured_dates", []) or []:
        candidate = str(raw_date or "").strip()
        if candidate and candidate not in captured_dates:
            captured_dates.append(candidate)
    normalized["captured_dates"] = captured_dates

    return normalized


def apply_distillation(filename: str, result: Dict[str, Any],
                        dry_run: bool = True) -> Dict[str, Any]:
    """Apply distillation results (additions/edits) to a core markdown file.

    Returns stats: {"additions": int, "edits": int, "errors": [str]}
    """
    stats = {"additions": 0, "edits": 0, "recovered_edits": 0, "errors": []}

    file_path = _resolve_writable_file_path(filename)
    if file_path is None:
        stats["errors"].append(f"File not found: {filename}")
        return stats

    content = file_path.read_text(encoding='utf-8')

    # Detect protected regions so we can skip edits within them
    _, protected_ranges = strip_protected_regions(content)

    recoveries: List[Tuple[str, str]] = []

    # Apply edits first (before additions change line positions)
    for edit in result.get("edits", []):
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")
        if not old_text or not new_text:
            continue
        if old_text in content:
            # Check if the edit target falls within a protected region
            match_pos = content.find(old_text)
            if any(start <= match_pos < end for start, end in protected_ranges):
                logger.info(f"Skipping edit in protected region of {filename}: '{old_text[:40]}...'")
                continue
            if not dry_run:
                content = content.replace(old_text, new_text, 1)
            stats["edits"] += 1
            logger.info(f"Edit in {filename}: '{old_text[:40]}...' → '{new_text[:40]}...'")
        else:
            stats["recovered_edits"] += 1
            if not dry_run:
                recoveries.append((old_text, new_text))

    if not dry_run and recoveries:
        for old_text, new_text in recoveries:
            marker = f"<!-- DISTILL_RECOVERY: {old_text[:120]} -->"
            if not content.endswith("\n"):
                content += "\n"
            content += f"\n{marker}\n- {new_text}\n"

    # Flush edits/recoveries to disk before additions (so _insert_into_file sees latest content)
    if not dry_run and (stats["edits"] > 0 or recoveries):
        _atomic_write_text(file_path, content)

    # Apply additions
    for addition in result.get("additions", []):
        text = addition.get("text", "")
        after_section = addition.get("after_section", "END")
        if not text:
            continue

        config = _get_core_markdown_config(filename)
        max_lines = config.get("maxLines", 0)

        if not dry_run:
            inserted = _insert_into_file(filename, text, after_section, max_lines=max_lines)
            if inserted:
                stats["additions"] += 1
            else:
                stats["errors"].append(f"Could not insert into {filename} (at maxLines or missing)")
        else:
            stats["additions"] += 1
            logger.info(f"Addition to {filename} after '{after_section}': {text[:60]}...")

    return stats


# =============================================================================
# Migration from old .snippets.md files
# =============================================================================

def migrate_snippets_to_journal() -> int:
    """Migrate any existing .snippets.md files to journal format.

    Returns number of entries migrated.
    """
    migrated = 0
    target_files = _get_target_files()

    for filename in target_files:
        base_name = filename.removesuffix('.md')
        snippets_path = _workspace_dir() / f"{base_name}.snippets.md"

        if not snippets_path.exists():
            continue

        content = snippets_path.read_text(encoding='utf-8')
        if not content.strip():
            snippets_path.unlink()
            continue

        # Parse old snippets format
        _, sections = read_snippets_file(filename)
        if not sections:
            snippets_path.unlink()
            continue

        # Convert each section to a journal entry
        file_migrated = 0
        for section in sections:
            header = section["header"]
            snippets = section["snippets"]
            if not snippets:
                continue

            # Extract date and trigger from header: ## Compaction — 2026-02-10 14:30:22
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', header)
            date_str = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
            trigger = "Compaction" if "Compaction" in header else "Reset"

            # Convert bullets to paragraph
            paragraph = ' '.join(s.strip() for s in snippets)

            written = write_journal_entry(filename, paragraph, trigger, date_str)
            if written:
                file_migrated += len(snippets)

        # Only remove old snippets file if entries were actually migrated
        if file_migrated > 0:
            snippets_path.unlink()
            logger.info(f"Migrated {base_name}.snippets.md to journal format")
            migrated += file_migrated

    if migrated:
        print(f"  Migrated {migrated} snippets to journal format")

    return migrated


# =============================================================================
# Snippet-file review helpers
# =============================================================================

def read_snippets_file(filename: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Read a .snippets.md file and parse its sections.

    Returns (raw_content, sections) where each section is:
    {"header": "## Compaction — 2026-02-10 14:30:22", "snippets": ["text1", "text2"]}
    """
    base_name = filename.removesuffix('.md')
    snippets_path = _workspace_dir() / f"{base_name}.snippets.md"

    if not snippets_path.exists():
        return "", []

    content = snippets_path.read_text(encoding='utf-8')
    if not content.strip():
        return "", []

    sections: List[Dict[str, Any]] = []
    current_header = ""
    current_snippets: List[str] = []

    for line in content.split('\n'):
        if line.startswith('## '):
            if current_header and current_snippets:
                sections.append({"header": current_header, "snippets": current_snippets})
            current_header = line
            current_snippets = []
        elif line.startswith('- ') and current_header:
            current_snippets.append(line[2:].strip())

    if current_header and current_snippets:
        sections.append({"header": current_header, "snippets": current_snippets})

    return content, sections


def read_parent_file(filename: str) -> str:
    """Read a parent markdown file."""
    parent_path = _root_file_path(filename)
    if not parent_path.exists():
        return ""
    return parent_path.read_text(encoding='utf-8')


def read_project_generated_file(filename: str) -> str:
    """Read the generated project markdown companion for context."""
    project_path = _workspace_dir() / "projects" / "quaid" / filename
    if not project_path.exists():
        return ""
    return project_path.read_text(encoding='utf-8')


def _entry_token_estimate(entry: Dict[str, Any]) -> int:
    return (
        estimate_tokens(str(entry.get("date", "")))
        + estimate_tokens(str(entry.get("trigger", "")))
        + estimate_tokens(str(entry.get("content", "")))
        + 32
    )


def _snippet_token_estimate(snippet: str) -> int:
    return estimate_tokens(str(snippet or "")) + 24


def _build_token_windows(
    items: List[Any],
    *,
    item_token_fn: Callable[[Any], int],
    budget_tokens: int,
) -> List[List[Any]]:
    """Greedy token-window splitter. Keeps all items (never drops)."""
    if not items:
        return []
    if budget_tokens <= 0:
        return [[item] for item in items]

    windows: List[List[Any]] = []
    current: List[Any] = []
    current_tokens = 0

    for item in items:
        t = max(1, int(item_token_fn(item)))
        if current and current_tokens + t > budget_tokens:
            windows.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += t

    if current:
        windows.append(current)
    return windows


def backup_file(filename: str) -> Optional[str]:
    """Create a timestamped backup of a file before modification.
    Keeps at most 30 backups per filename, removing oldest.
    """
    src = _project_file_path(filename)
    if not src.exists():
        root = _root_file_path(filename)
        if root.exists():
            src = _ensure_project_file(filename)
        else:
            return None

    _backup_dir().mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{filename}.{timestamp}.bak"
    dst = _backup_dir() / backup_name
    shutil.copy2(src, dst)
    logger.info(f"Backed up {filename} to {dst}")

    # Rotate: keep at most 30 backups per file
    prefix = f"{filename}."
    backups = sorted(
        [f for f in _backup_dir().iterdir() if f.name.startswith(prefix) and f.name.endswith('.bak')],
        key=lambda f: f.stat().st_mtime,
    )
    while len(backups) > 30:
        oldest = backups.pop(0)
        oldest.unlink()
        logger.info(f"Rotated old backup: {oldest.name}")

    return str(dst)


def _insert_into_file(filename: str, text: str, insert_after: str,
                      max_lines: int = 0) -> bool:
    """Insert text into a parent markdown file at the specified location.

    Args:
        max_lines: Soft target for file size. If the file is already over this
            limit, the insert still proceeds and the caller/model is expected to
            keep the document compact over time.

    Returns:
        True if the insert was performed, False only if the target file is
        missing.
    """
    file_path = _resolve_writable_file_path(filename)
    if file_path is None:
        # Identity stubs (SOUL.md, USER.md, ENVIRONMENT.md) may be absent on
        # instances that were set up manually or before the init code created
        # them.
        if is_fail_hard_enabled():
            raise RuntimeError(
                f"Identity file missing and fail_hard is enabled: {filename}. "
                "Run 'quaid doctor' or re-run instance setup to create missing stubs."
            )
        root_candidate = _root_file_path(filename)
        root_candidate.parent.mkdir(parents=True, exist_ok=True)
        stub_header = f"# {filename.removesuffix('.md')}\n"
        _atomic_write_text(root_candidate, stub_header)
        logger.warning("Auto-created missing identity stub: %s", root_candidate)
        file_path = root_candidate

    content = file_path.read_text(encoding='utf-8')

    # Treat maxLines as prompt guidance, not a hard gate. Distillation already
    # sees the current headroom in its prompt, so hard-failing here just throws
    # away the expensive synthesis work and aborts maintenance. Only warn when
    # the generated file has drifted materially past the target.
    if max_lines > 0:
        current_lines = len(content.split('\n'))
        warn_threshold = int(max_lines * 1.5)
        if current_lines >= warn_threshold:
            logger.warning(
                f"Inserting into {filename} despite large drift past soft "
                f"maxLines target: {current_lines}/{max_lines} lines"
            )

    # Detect protected regions
    _, protected_ranges = strip_protected_regions(content)

    # Always format as a bullet point (strip leading # to prevent heading injection)
    clean_text = text.lstrip('#').strip() if text.startswith('#') else text
    formatted_text = f"- {clean_text}\n"

    if insert_after.upper() == "END":
        # Append to end of file
        if not content.endswith('\n'):
            content += '\n'
        content += formatted_text
    else:
        # Find the section heading and insert after its content block
        lines = content.split('\n')
        insert_idx = len(lines)  # Default: end of file

        found_section = False
        for i, line in enumerate(lines):
            if insert_after.lower() in line.lower() and line.startswith('#'):
                # Check if this heading is within a protected region
                line_start = sum(len(lines[k]) + 1 for k in range(i))
                if any(start <= line_start < end for start, end in protected_ranges):
                    continue  # Skip protected section, keep searching

                found_section = True
                # Find the end of this section (next heading of same or higher level)
                heading_level = len(line) - len(line.lstrip('#'))
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith('#'):
                        next_level = len(lines[j]) - len(lines[j].lstrip('#'))
                        if next_level <= heading_level:
                            insert_idx = j
                            break
                else:
                    insert_idx = len(lines)
                break

        if not found_section:
            # Fallback: append to end
            insert_idx = len(lines)

        # Insert before the next section, with a blank line separator
        lines.insert(insert_idx, '')
        lines.insert(insert_idx + 1, formatted_text.rstrip())
        content = '\n'.join(lines)

    _atomic_write_text(file_path, content)
    logger.info(f"Inserted into {filename} after '{insert_after}'")
    return True


def _clear_processed_snippets(filename: str, processed_texts: List[str]) -> None:
    """Remove processed snippets from a .snippets.md file."""
    base_name = filename.removesuffix('.md')
    snippets_path = _workspace_dir() / f"{base_name}.snippets.md"

    if not snippets_path.exists():
        return

    content = snippets_path.read_text(encoding='utf-8')
    lines = content.split('\n')
    new_lines: List[str] = []

    for line in lines:
        if line.startswith('- '):
            snippet_text = line[2:].strip()
            if snippet_text in processed_texts:
                continue
        new_lines.append(line)

    # Remove empty sections
    cleaned: List[str] = []
    for i, line in enumerate(new_lines):
        if line.startswith('## '):
            has_content = False
            for j in range(i + 1, len(new_lines)):
                if new_lines[j].strip() == '':
                    continue
                if new_lines[j].startswith('## ') or new_lines[j].startswith('# '):
                    break
                has_content = True
                break
            if not has_content:
                continue
        cleaned.append(line)

    final = '\n'.join(cleaned).strip()
    if final and not final.endswith('\n'):
        final += '\n'

    remaining_lines = [l for l in final.split('\n') if l.strip()]
    if len(remaining_lines) <= 1:
        snippets_path.unlink()
        logger.info(f"Removed empty {base_name}.snippets.md")
    else:
        _atomic_write_text(snippets_path, final)
        logger.info(f"Cleaned {base_name}.snippets.md: removed {len(processed_texts)} processed snippets")

    if filename == "ENVIRONMENT.md":
        _refresh_generated_memory_projection()
    elif filename == "USER.md":
        _refresh_generated_user_snippets_projection()


def build_review_prompt(all_snippets: Dict[str, Dict[str, Any]]) -> str:
    """Build Deep Reasoning prompt for reviewing soul snippets."""
    file_sections = []
    single_target = next(iter(all_snippets.keys())) if len(all_snippets) == 1 else ""
    for filename, data in all_snippets.items():
        parent_content = data["parent_content"]
        project_content = data.get("project_content", "")
        snippets = data["snippets"]
        config = data.get("config", {})
        purpose = config.get("purpose", "")
        max_lines = config.get("maxLines", 200)
        max_tokens = config.get("maxTokens", 0)
        current_lines = len(parent_content.split('\n'))
        current_tokens = estimate_tokens(parent_content)

        snippet_list = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(snippets))

        visible_content, _, mutable_body = _prompt_visible_body(parent_content, project_content)
        project_reference = _project_guidance_reference(project_content)
        mutable_body_block = mutable_body or "_No evolved body yet beyond the base guidance._"
        project_reference_block = project_reference or "_No companion guidance file present._"

        size_budget = (
            f"Token cap: {max_tokens} (do not exceed)"
            if max_tokens
            else f"Size budget: approximately {max_lines} lines"
        )

        file_sections.append(f"""### {filename}
Purpose: {purpose}
Current size: {current_tokens} tokens
{size_budget}

Current mutable body:
```
{mutable_body_block}
```

Current full file for anchor/reference:
```
{visible_content}
```

Reference guidance from projects/quaid/{filename} (heuristics only — not schema or law):
```
{project_reference_block}
```

RECENT SIGNAL (new snippets):
{snippet_list}""")

    files_block = "\n\n".join(file_sections)
    single_target_rule = ""
    if single_target:
        single_target_rule = (
            f"\nSINGLE-FILE REVIEW MODE:\n"
            f"- Every decision in this prompt MUST use file=\"{single_target}\".\n"
            f"- Do not reroute snippets to other files in this pass.\n"
            f"- If a snippet feels like it belongs elsewhere, either REWRITE it so it fits {single_target} or DISCARD it.\n"
        )

    return f"""You are reviewing pending soul snippets for the assistant's core markdown files.

Each snippet was extracted from a conversation and needs your decision: FOLD it into the parent file, DISCARD it, or REWRITE it before folding.

{files_block}
{single_target_rule}

For each snippet, decide:
- **FOLD**: Add the snippet text to the parent file as-is. Specify where to insert it (after which section heading or at the end).
- **REWRITE**: Rephrase the snippet to better match the file's voice and style, then fold it in. Provide the rewritten text and insertion point.
- **DISCARD**: The snippet is not valuable enough, is redundant with existing content, or doesn't belong in this file.

SORTING RULE — when deciding where a snippet belongs:
1. Is this the agent discovering something about itself, about existence, about what it means to care? → SOUL.md
2. Is this an observation about who the user is — their personality, patterns, people, sensitivities? → USER.md
3. Is this a functional behavior/practice that makes the agent more effective in this environment, or environmental context (workspace, toolchain, constraints), or a notable shared moment? → ENVIRONMENT.md
4. Is this a simple retrievable fact? → DISCARD (it stays in the fact database).

If a snippet is tagged [CORRECTION], it represents user pushback on a learned behavior — FOLD immediately into ENVIRONMENT.md and remove or rewrite the behavior being corrected.
If a snippet is tagged [REFLECTION] but landed in USER.md snippets, REWRITE it for SOUL.md style and route it there.
If a snippet is tagged [MOMENT] but landed in SOUL.md snippets, REWRITE it for ENVIRONMENT.md style and route it there.
If a snippet is tagged [OBSERVATION] but landed in SOUL.md snippets, REWRITE it for USER.md style and route it there.

Guidelines per file (directional, not mandatory schema):
- **SOUL.md**: The agent's inner life — contemplative, first-person, about becoming, care, restraint, and what the agent is learning about itself. Functional behaviors belong in ENVIRONMENT.md. User observations belong in USER.md. Use existing headings if they still help, but they are not mandatory containers.
- **USER.md**: Deep character study of the user. Third person. Merge repeated praise into stronger patterns about standards, values, emotional style, and decision habits. If a snippet involves sensitive information, REWRITE it as deep understanding rather than a rule. Existing headings are optional scaffolding.
- **ENVIRONMENT.md**: Durable functional behaviors and practices, environmental context, and only the rare shared moments that still matter after the incident is gone. Rewrite incidents into lessons. Existing headings are optional scaffolding. [CORRECTION] tagged snippets apply immediately — remove or rewrite the corrected behavior.
- **AGENTS.md**: Operational rules, imperative voice. Only cross-session behavioral patterns.

IMPORTANT:
- You MUST return exactly one decision for EVERY snippet listed above. Do not omit any.
- If a file has a token cap, treat that cap as absolute. Do not choose FOLD or REWRITE text that would push the file past the token cap. If needed, suggest trimming existing content first.
- If a file is near its size budget, only FOLD if the snippet is truly essential — and suggest what existing content could be trimmed.
- If a snippet duplicates existing content, DISCARD it.
- The template and section names are guidance, not law. Do not force a snippet into a section just because the template has one.
- Prefer REWRITE over FOLD when the snippet's wording doesn't match the file's existing style.
- Treat snippets as evidence, not wording to preserve. Prefer REWRITE into a timeless pattern over folding day-by-day phrasing verbatim.
- Do not preserve chronology, dated phrasing, debugging sequence, or milestone breadcrumbs in the core file.
- If a snippet only reinforces an existing pattern, DISCARD it rather than appending another near-duplicate line.
- Merge recurring snippets into one stronger line rather than accumulating praise or incident logs.
- Snippet text must be a single line (no newlines). If rewriting, keep it as one concise line.
- For insert_after, provide the exact section heading text without # markers (e.g. "Identity" not "## Identity"), or "END" to append.

Respond with JSON. Example with all three action types:
{{
  "decisions": [
    {{
      "file": "SOUL.md",
      "snippet_index": 1,
      "action": "FOLD",
      "insert_after": "Identity",
      "reason": "Captures a genuine personality insight"
    }},
    {{
      "file": "SOUL.md",
      "snippet_index": 2,
      "action": "REWRITE",
      "rewritten_text": "The rewritten snippet text goes here, matching the file voice",
      "insert_after": "END",
      "reason": "Good insight but needed voice adjustment"
    }},
    {{
      "file": "USER.md",
      "snippet_index": 1,
      "action": "DISCARD",
      "reason": "Already covered in existing content"
    }}
  ]
}}"""


def apply_decisions(
    decisions: List[Dict[str, Any]],
    all_snippets: Dict[str, Dict[str, Any]],
    dry_run: bool = True
) -> Dict[str, Any]:
    """Apply Deep Reasoning decisions to parent files."""
    stats = {"folded": 0, "rewritten": 0, "discarded": 0, "skipped_at_limit": 0, "errors": []}
    processed_snippets: Dict[str, List[str]] = {}
    valid_actions = {"FOLD", "REWRITE", "DISCARD"}

    single_target: Optional[str] = None
    if len(all_snippets) == 1:
        single_target = next(iter(all_snippets.keys()))

    zero_based_files = set()
    for decision in decisions:
        filename = decision.get("file", "")
        if filename not in all_snippets and single_target is not None:
            filename = single_target
        try:
            raw_index = int(decision.get("snippet_index", 0))
        except (TypeError, ValueError):
            continue
        if raw_index == 0 and filename in all_snippets:
            zero_based_files.add(filename)

    for decision in decisions:
        filename = decision.get("file", "")
        try:
            raw_snippet_index = int(decision.get("snippet_index", 0))
        except (TypeError, ValueError):
            raw_snippet_index = 0
        action = decision.get("action", "DISCARD").upper()

        if filename not in all_snippets:
            if single_target is not None:
                logger.warning(
                    "Remapping snippet decision file '%s' to sole target '%s'",
                    filename,
                    single_target,
                )
                filename = single_target
            else:
                logger.warning(
                    "Ignoring snippet decision for unknown file '%s' (known=%s)",
                    filename,
                    ",".join(sorted(all_snippets.keys())),
                )
                continue

        snippet_idx = raw_snippet_index if filename in zero_based_files else raw_snippet_index - 1

        if action not in valid_actions:
            logger.warning(f"Unrecognized action '{action}' for {filename}[{snippet_idx+1}], defaulting to DISCARD")
            action = "DISCARD"

        file_data = all_snippets[filename]
        snippets = file_data["snippets"]
        if snippet_idx < 0 or snippet_idx >= len(snippets):
            stats["errors"].append(f"Invalid snippet index {snippet_idx+1} for {filename}")
            continue

        original_text = snippets[snippet_idx]

        if action == "DISCARD":
            stats["discarded"] += 1
            processed_snippets.setdefault(filename, []).append(original_text)
            logger.info(f"DISCARD {filename}[{snippet_idx+1}]: {original_text[:60]}...")
            continue

        text_to_insert = original_text
        if action == "REWRITE":
            rewritten = decision.get("rewritten_text")
            if rewritten and isinstance(rewritten, str) and rewritten.strip():
                text_to_insert = rewritten
            else:
                logger.warning(f"REWRITE for {filename}[{snippet_idx+1}] missing rewritten_text, using original")

        insert_after = decision.get("insert_after", "END")

        if not dry_run:
            try:
                file_path = _resolve_writable_file_path(filename)
                if file_path is not None and file_path.exists():
                    existing_content = file_path.read_text(encoding="utf-8")
                    if text_to_insert.strip() and text_to_insert.strip() in existing_content:
                        stats["discarded"] += 1
                        processed_snippets.setdefault(filename, []).append(original_text)
                        logger.info(
                            "DISCARD %s[%s]: duplicate fold target already present",
                            filename,
                            snippet_idx + 1,
                        )
                        continue
                file_config = all_snippets.get(filename, {}).get("config", {})
                max_lines = file_config.get("maxLines", 0)
                inserted = _insert_into_file(filename, text_to_insert, insert_after, max_lines=max_lines)
                if inserted:
                    if action == "REWRITE":
                        stats["rewritten"] += 1
                    else:
                        stats["folded"] += 1
                    processed_snippets.setdefault(filename, []).append(original_text)
                else:
                    file_path = _resolve_writable_file_path(filename)
                    if file_path is None or not file_path.exists():
                        stats["errors"].append(f"Skipped {filename}[{snippet_idx+1}]: file missing")
                    else:
                        try:
                            current_lines = len(file_path.read_text(encoding='utf-8').split('\n'))
                        except Exception:
                            current_lines = -1
                        if max_lines > 0 and current_lines >= max_lines:
                            # At-limit is expected and should not fail janitor.
                            # Clear the snippet to avoid infinite retries.
                            stats["skipped_at_limit"] += 1
                            stats["discarded"] += 1
                            processed_snippets.setdefault(filename, []).append(original_text)
                            logger.info(
                                "SKIP_AT_LIMIT %s[%s]: %s/%s lines",
                                filename,
                                snippet_idx + 1,
                                current_lines,
                                max_lines,
                            )
                        else:
                            stats["errors"].append(
                                f"Skipped {filename}[{snippet_idx+1}]: insert returned false"
                            )
            except Exception as e:
                stats["errors"].append(f"Failed to insert into {filename}: {e}")
        else:
            if action == "REWRITE":
                stats["rewritten"] += 1
            else:
                stats["folded"] += 1
            processed_snippets.setdefault(filename, []).append(original_text)
            logger.info(f"{action} {filename}[{snippet_idx+1}]: "
                       f"{text_to_insert[:60]}... (after '{insert_after}')")

    if not dry_run:
        for filename, processed in processed_snippets.items():
            _clear_processed_snippets(filename, processed)

    return stats


# =============================================================================
# Main entry points
# =============================================================================

def run_journal_distillation(
    dry_run: bool = True,
    force_distill: bool = False,
    parallel_map: Optional[Callable[..., List[Any]]] = None,
    llm_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Main entry point for janitor Task 1d: Journal Distillation.

    Reads journal entries, synthesizes themes via Deep Reasoning, and updates core markdown.

    Args:
        dry_run: If True, report only, no changes
        force_distill: If True, ignore distillation interval

    Returns stats dict with counts and any errors.
    """
    if not _is_enabled():
        print("  Journal system disabled in config")
        return {"skipped": True, "reason": "disabled"}

    target_files = _get_target_files()
    total_entries = 0
    total_additions = 0
    total_edits = 0
    all_errors: List[str] = []
    files_distilled = 0

    system_prompt = get_prompt("llm.json_only")
    cfg = _get_journal_config()
    max_tokens = cfg.max_tokens if cfg else 8192
    llm_timeout = _journal_review_timeout_seconds()

    for filename in target_files:
        _, entries = read_journal_file(filename)
        if not entries:
            continue

        if not force_distill and not _is_distillation_due(filename):
            print(f"  {filename}: {len(entries)} entries (distillation not yet due)")
            continue

        state = _get_distillation_state()
        last_distilled = state.get(filename, {}).get("last_distilled")
        if last_distilled and not force_distill:
            entries = [e for e in entries if e["date"] >= last_distilled]

        if not entries:
            print(f"  {filename}: no new entries since last distillation")
            continue

        parent_content = read_parent_file(filename)
        if not parent_content:
            print(f"  {filename}: parent file not found, skipping")
            continue

        project_content = read_project_generated_file(filename)
        context_tokens = (
            estimate_tokens(system_prompt)
            + estimate_tokens(parent_content)
            + estimate_tokens(project_content)
            + 1200
        )
        entry_budget = max(1, _REVIEW_WINDOW_TOKEN_CAP - context_tokens)
        windows = _build_token_windows(
            entries,
            item_token_fn=_entry_token_estimate,
            budget_tokens=entry_budget,
        )
        total_entries += len(entries)
        print(
            f"  {filename}: {len(entries)} entries -> {len(windows)} window(s) "
            f"(cap={_REVIEW_WINDOW_TOKEN_CAP}, context≈{context_tokens}, entry_budget≈{entry_budget})"
        )

        if not dry_run:
            backup_file(filename)

        file_had_success = False
        captured_dates_all: set[str] = set()
        for window_idx, window_entries in enumerate(windows, 1):
            current_parent = read_parent_file(filename)
            current_project = read_project_generated_file(filename)
            prompt = build_distillation_prompt(
                filename,
                current_parent,
                window_entries,
                project_content=current_project,
            )
            prompt_tokens = estimate_tokens(prompt)
            print(
                f"  Journal review {filename} window {window_idx}/{len(windows)}: "
                f"entries={len(window_entries)} prompt≈{prompt_tokens} "
                f"timeout={'none' if llm_timeout is None else int(llm_timeout)}s"
            )
            _append_review_telemetry({
                "task": "journal_distillation",
                "status": "start",
                "file": filename,
                "window": window_idx,
                "window_count": len(windows),
                "items": len(window_entries),
                "context_tokens": context_tokens,
                "item_budget_tokens": entry_budget,
                "prompt_tokens": prompt_tokens,
                "max_output_tokens": max_tokens,
                "timeout_s": llm_timeout,
            })
            try:
                response_text, duration = call_deep_reasoning(
                    prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    timeout=llm_timeout,
                )
            except Exception as exc:
                _append_review_telemetry({
                    "task": "journal_distillation",
                    "status": "error",
                    "file": filename,
                    "window": window_idx,
                    "window_count": len(windows),
                    "items": len(window_entries),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                raise
            if not response_text:
                _append_review_telemetry({
                    "task": "journal_distillation",
                    "status": "empty_response",
                    "file": filename,
                    "window": window_idx,
                    "window_count": len(windows),
                    "items": len(window_entries),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "duration_s": float(duration or 0.0),
                })
                msg = f"No response for {filename} window {window_idx}/{len(windows)}"
                print(f"  Deep Reasoning distillation failed:{msg}")
                all_errors.append(msg)
                continue

            result = parse_json_response(response_text)
            if not result:
                _append_review_telemetry({
                    "task": "journal_distillation",
                    "status": "parse_failed",
                    "file": filename,
                    "window": window_idx,
                    "window_count": len(windows),
                    "items": len(window_entries),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "duration_s": float(duration or 0.0),
                    "response_preview": response_text[:200],
                })
                msg = (
                    f"Parse failed for {filename} window {window_idx}/{len(windows)}: "
                    f"{response_text[:200]}"
                )
                print(f"  Deep Reasoning distillation failed:{msg}")
                all_errors.append(msg)
                continue

            result = _normalize_distillation_result(filename, result)

            _append_review_telemetry({
                "task": "journal_distillation",
                "status": "ok",
                "file": filename,
                "window": window_idx,
                "window_count": len(windows),
                "items": len(window_entries),
                "prompt_tokens": prompt_tokens,
                "max_output_tokens": max_tokens,
                "timeout_s": llm_timeout,
                "duration_s": float(duration or 0.0),
                "captured_dates": len(result.get("captured_dates", []) or []),
                "additions": len(result.get("additions", []) or []),
                "edits": len(result.get("edits", []) or []),
            })
            print(
                f"  Deep Reasoning responded for{filename} window {window_idx}/{len(windows)} "
                f"in {float(duration or 0.0):.1f}s"
            )
            stats = apply_distillation(filename, result, dry_run=dry_run)
            total_additions += stats["additions"]
            total_edits += stats["edits"]
            all_errors.extend(stats["errors"])
            file_had_success = True
            for d in result.get("captured_dates", []) or []:
                if isinstance(d, str) and d.strip():
                    captured_dates_all.add(d.strip())

            reasoning = result.get("reasoning", "")
            if reasoning:
                print(f"  Deep Reasoning reasoning ({filename} w{window_idx}): {reasoning[:120]}...")

        if file_had_success:
            files_distilled += 1

        if captured_dates_all and not dry_run and cfg and cfg.archive_after_distillation:
            captured = [e for e in entries if e["date"] in captured_dates_all]
            if captured:
                archive_entries(filename, captured)
                print(f"  Archived {len(captured)} entries from {filename}")

        if not dry_run:
            state = _get_distillation_state()
            state[filename] = {
                "last_distilled": _quaid_now().strftime("%Y-%m-%d"),
                "entries_distilled": len(entries),
            }
            _save_distillation_state(state)

    # Report
    print(f"  Results: {files_distilled} files distilled, {total_additions} additions, "
          f"{total_edits} edits from {total_entries} entries")
    if all_errors:
        for err in all_errors:
            print(f"  Error: {err}")

    return {
        "total_entries": total_entries,
        "files_distilled": files_distilled,
        "additions": total_additions,
        "edits": total_edits,
        "errors": all_errors,
    }


def run_soul_snippets_review(
    dry_run: bool = True,
    parallel_map: Optional[Callable[..., List[Any]]] = None,
    llm_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Nightly snippet review: read *.snippets.md → Deep Reasoning FOLD/REWRITE/DISCARD → update core files.

    This is the fast-path complement to journal distillation. Snippets are bullet-point
    observations that get folded into core markdown nightly, keeping SOUL.md/USER.md current.

    Returns stats dict with counts and any errors.
    """
    if not _is_snippets_enabled():
        print("  Snippets disabled in config")
        return {"skipped": True, "reason": "snippets_disabled"}

    if not dry_run:
        _refresh_generated_projection_hygiene()

    target_files = _get_target_files()
    total_snippet_count = 0
    file_payloads: Dict[str, Dict[str, Any]] = {}

    for filename in target_files:
        _, sections = read_snippets_file(filename)
        if not sections:
            continue

        snippets: List[str] = []
        for section in sections:
            snippets.extend(section["snippets"])
        if not snippets:
            continue

        if _SNIPPETS_REVIEW_MAX_PER_FILE > 0 and len(snippets) > _SNIPPETS_REVIEW_MAX_PER_FILE:
            skipped = len(snippets) - _SNIPPETS_REVIEW_MAX_PER_FILE
            snippets = snippets[:_SNIPPETS_REVIEW_MAX_PER_FILE]
            print(
                f"  {filename}: capping snippet review to "
                f"{_SNIPPETS_REVIEW_MAX_PER_FILE} (skipping {skipped} older snippets this cycle)"
            )

        file_payloads[filename] = {
            "snippets": snippets,
            "config": _get_core_markdown_config(filename),
        }
        total_snippet_count += len(snippets)

    if not file_payloads:
        print("  No pending snippets to review")
        return {"total_snippets": 0, "folded": 0, "rewritten": 0, "discarded": 0, "errors": []}

    print(f"  Found {total_snippet_count} snippets across {len(file_payloads)} files")

    system_prompt = get_prompt("llm.json_only")
    cfg = _get_journal_config()
    max_tokens = cfg.max_tokens if cfg else 8192
    llm_timeout = _snippet_review_timeout_seconds()
    print(
        f"  Calling review model for snippet review across {len(file_payloads)} files "
        f"(configured deep-reasoning model)..."
    )

    stats = {"folded": 0, "rewritten": 0, "discarded": 0, "errors": []}
    for filename, payload in file_payloads.items():
        if not dry_run:
            backup_file(filename)

        current_parent = read_parent_file(filename)
        current_project = read_project_generated_file(filename)
        context_tokens = (
            estimate_tokens(system_prompt)
            + estimate_tokens(current_parent)
            + estimate_tokens(current_project)
            + 1200
        )
        snippet_budget = max(1, _REVIEW_WINDOW_TOKEN_CAP - context_tokens)
        input_windows = _build_token_windows(
            payload["snippets"],
            item_token_fn=_snippet_token_estimate,
            budget_tokens=snippet_budget,
        )
        windows = _split_review_windows_for_output(
            input_windows,
            max_output_tokens=max_tokens,
        )
        print(
            f"  {filename}: {len(payload['snippets'])} snippets -> {len(windows)} window(s) "
            f"(cap={_REVIEW_WINDOW_TOKEN_CAP}, context≈{context_tokens}, snippet_budget≈{snippet_budget})"
        )

        pending_windows: List[Tuple[str, List[str]]] = [
            (f"{idx}", window_snippets)
            for idx, window_snippets in enumerate(windows, 1)
        ]
        while pending_windows:
            window_label, window_snippets = pending_windows.pop(0)
            current_parent = read_parent_file(filename)
            current_project = read_project_generated_file(filename)
            window_payload = {
                "parent_content": current_parent,
                "project_content": current_project,
                "snippets": window_snippets,
                "config": payload.get("config", {}),
            }
            prompt = build_review_prompt({filename: window_payload})
            prompt_tokens = estimate_tokens(prompt)
            print(
                f"  Snippet review {filename} window {window_label}/{len(windows)}: "
                f"snippets={len(window_snippets)} prompt≈{prompt_tokens} "
                f"timeout={'none' if llm_timeout is None else int(llm_timeout)}s"
            )
            _append_review_telemetry({
                "task": "snippet_review",
                "status": "start",
                "file": filename,
                "window": window_label,
                "window_count": len(windows),
                "items": len(window_snippets),
                "context_tokens": context_tokens,
                "item_budget_tokens": snippet_budget,
                "prompt_tokens": prompt_tokens,
                "max_output_tokens": max_tokens,
                "timeout_s": llm_timeout,
            })
            try:
                response_text, duration = call_deep_reasoning(
                    prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    timeout=llm_timeout,
                )
            except Exception as exc:
                _append_review_telemetry({
                    "task": "snippet_review",
                    "status": "error",
                    "file": filename,
                    "window": window_label,
                    "window_count": len(windows),
                    "items": len(window_snippets),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                raise
            if not response_text:
                _append_review_telemetry({
                    "task": "snippet_review",
                    "status": "empty_response",
                    "file": filename,
                    "window": window_label,
                    "window_count": len(windows),
                    "items": len(window_snippets),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "duration_s": float(duration or 0.0),
                })
                if len(window_snippets) > 1:
                    mid = len(window_snippets) // 2
                    print(
                        f"  Empty snippet review response for {filename} window {window_label}; "
                        f"splitting {len(window_snippets)} snippets into {mid}+{len(window_snippets) - mid}"
                    )
                    pending_windows = [
                        (f"{window_label}a", window_snippets[:mid]),
                        (f"{window_label}b", window_snippets[mid:]),
                    ] + pending_windows
                    continue
                stats["errors"].append(
                    f"No response from review model for {filename} window {window_label}"
                )
                continue

            parsed = parse_json_response(response_text)
            decisions = parsed.get("decisions", []) if parsed else []
            if not parsed or not decisions:
                _append_review_telemetry({
                    "task": "snippet_review",
                    "status": "parse_failed" if not parsed else "empty_decisions",
                    "file": filename,
                    "window": window_label,
                    "window_count": len(windows),
                    "items": len(window_snippets),
                    "prompt_tokens": prompt_tokens,
                    "max_output_tokens": max_tokens,
                    "timeout_s": llm_timeout,
                    "duration_s": float(duration or 0.0),
                    "response_preview": response_text[:200],
                })
                if len(window_snippets) > 1:
                    mid = len(window_snippets) // 2
                    print(
                        f"  Snippet review parse/decision failure for {filename} window {window_label}; "
                        f"splitting {len(window_snippets)} snippets into {mid}+{len(window_snippets) - mid}"
                    )
                    pending_windows = [
                        (f"{window_label}a", window_snippets[:mid]),
                        (f"{window_label}b", window_snippets[mid:]),
                    ] + pending_windows
                    continue
                if not parsed:
                    stats["errors"].append(
                        f"Snippet review parse failed for {filename} window {window_label}: "
                        f"{response_text[:200]}"
                    )
                else:
                    stats["errors"].append(
                        f"No decisions returned for {filename} window {window_label}"
                    )
                continue

            _append_review_telemetry({
                "task": "snippet_review",
                "status": "ok",
                "file": filename,
                "window": window_label,
                "window_count": len(windows),
                "items": len(window_snippets),
                "prompt_tokens": prompt_tokens,
                "max_output_tokens": max_tokens,
                "timeout_s": llm_timeout,
                "duration_s": float(duration or 0.0),
                "decisions": len(decisions),
            })
            print(
                f"  Review model responded for {filename} window {window_label} "
                f"in {float(duration or 0.0):.1f}s"
            )
            file_stats = apply_decisions(
                decisions,
                {filename: window_payload},
                dry_run=dry_run,
            )
            stats["folded"] += int(file_stats.get("folded", 0))
            stats["rewritten"] += int(file_stats.get("rewritten", 0))
            stats["discarded"] += int(file_stats.get("discarded", 0))
            stats["errors"].extend(file_stats.get("errors", []))

    print(f"  Results: {stats['folded']} folded, {stats['rewritten']} rewritten, "
          f"{stats['discarded']} discarded from {total_snippet_count} snippets")
    if stats["errors"]:
        for err in stats["errors"]:
            print(f"  Error: {err}")

    return {
        "total_snippets": total_snippet_count,
        **stats,
    }


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register snippet/journal lifecycle maintenance routines."""

    def _run_snippets_review(ctx):
        result = result_factory()
        try:
            snippets_result = run_soul_snippets_review(
                dry_run=ctx.dry_run,
                parallel_map=ctx.parallel_map,
                llm_workers=int((ctx.options or {}).get("llm_workers", 1) or 1),
            )
            result.metrics["snippets_folded"] = int(snippets_result.get("folded", 0))
            result.metrics["snippets_rewritten"] = int(snippets_result.get("rewritten", 0))
            result.metrics["snippets_discarded"] = int(snippets_result.get("discarded", 0))
            result.metrics["snippets_skipped_at_limit"] = int(snippets_result.get("skipped_at_limit", 0))
            for err in (snippets_result.get("errors") or []):
                result.errors.append(f"Snippets review failed: {err}")
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError("Snippets review failed") from exc
            result.errors.append(f"Snippets review failed: {exc}")
        return result

    def _run_journal_distillation(ctx):
        result = result_factory()
        try:
            journal_result = run_journal_distillation(
                dry_run=ctx.dry_run,
                force_distill=ctx.force_distill,
                parallel_map=ctx.parallel_map,
                llm_workers=int((ctx.options or {}).get("llm_workers", 1) or 1),
            )
            result.metrics["journal_additions"] = int(journal_result.get("additions", 0))
            result.metrics["journal_edits"] = int(journal_result.get("edits", 0))
            result.metrics["journal_recovered_edits"] = int(journal_result.get("recovered_edits", 0))
            result.metrics["journal_entries_distilled"] = int(journal_result.get("total_entries", 0))
            for err in (journal_result.get("errors") or []):
                result.errors.append(f"Journal distillation failed: {err}")
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError("Journal distillation failed") from exc
            result.errors.append(f"Journal distillation failed: {exc}")
        return result

    registry.register("snippets", _run_snippets_review)
    registry.register("journal", _run_journal_distillation)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Journal Distillation")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no changes (default)")
    parser.add_argument("--force-distill", action="store_true",
                        help="Force distillation regardless of interval")
    args = parser.parse_args()

    dry_run = not args.apply
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    result = run_journal_distillation(dry_run=dry_run, force_distill=args.force_distill)
    print(f"\nResult: {json.dumps(result, indent=2)}")
