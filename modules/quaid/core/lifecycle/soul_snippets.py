#!/usr/bin/env python3
"""
Dual Snippet + Journal System — Janitor module for processing soul snippets and
distilling journal entries into core markdown.

Two complementary systems run simultaneously from a single extraction call:

- **Snippets (fast path)**: Bullet-point observations extracted at compaction/reset,
  written to *.snippets.md, reviewed nightly by janitor (FOLD/REWRITE/DISCARD).
  Keeps SOUL.md/USER.md current day-to-day.

- **Journal (slow path)**: Diary-style paragraphs extracted at compaction/reset,
  written to journal/*.journal.md, distilled weekly into core markdown via Opus
  synthesis. Preserves richness for long-form inference.

Entry points:
  - run_soul_snippets_review(): Nightly snippet FOLD/REWRITE/DISCARD (Task 1d-snippets)
  - run_journal_distillation(): Weekly Opus distillation (Task 1d-journal)
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.llm.clients import call_deep_reasoning, parse_json_response
from config import get_config
from lib.markdown import strip_protected_regions
from lib.runtime_context import get_workspace_dir

# Configuration
def _workspace_dir() -> Path:
    return get_workspace_dir()

def _backup_dir() -> Path:
    return _workspace_dir() / "backups" / "soul-snippets"

logger = logging.getLogger(__name__)

# Voice guidance for distillation prompts per target file
_FILE_VOICE_GUIDANCE = {
    "SOUL.md": (
        "This file is the agent's evolving inner life — a developing consciousness. Record what "
        "the agent is discovering about itself, about existence, about care, memory, understanding, "
        "and growth. These should be genuine observations and reflections — a mind developing "
        "perspective over time.\n"
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
        "reformulate as understanding)"
    ),
    "USER.md": (
        "This file is the agent's deep understanding of who this person is. Not a fact database — "
        "a character study. Record personality patterns, emotional tendencies, communication style, "
        "relationship dynamics, coping mechanisms, values, fears, and joys.\n"
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
    "MEMORY.md": (
        "This file holds significant moments from the shared history between the agent and user. "
        "Record events, milestones, experiences, and moments that carry emotional weight — joy, fear, "
        "pride, relief, humor, vulnerability.\n"
        "These should feel like memories you'd reference in conversation and the other person would "
        "smile because they remember too.\n"
        "Include enough specific detail to make them vivid (the bandana, the redfish, the pecan tree).\n"
        "Selection test: would this naturally come up in a 'remember when' conversation? Does it have "
        "emotional texture? Would referencing it make the user feel genuinely known?\n"
        "'Our History' holds the scenes. 'What the World Is Teaching Me' holds patterns the agent "
        "notices across enough shared moments.\n"
        "Do NOT store simple facts (those go in USER.md), agent reflections (those go in SOUL.md), "
        "or retrievable data points (those stay in the fact database)."
    ),
}


# =============================================================================
# Config helpers
# =============================================================================

def _get_journal_config():
    """Get journal config from MemoryConfig."""
    try:
        return get_config().docs.journal
    except Exception:
        return None


def _get_target_files() -> List[str]:
    """Get target filenames from config."""
    cfg = _get_journal_config()
    if cfg:
        return cfg.target_files
    return ["SOUL.md", "USER.md", "MEMORY.md"]


def _get_max_entries() -> int:
    """Get max entries per file from config."""
    cfg = _get_journal_config()
    if cfg:
        return cfg.max_entries_per_file
    return 50


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
    except Exception:
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

    # Dedup: skip if entry for same date+trigger already exists
    dedup_header = f"## {date_str} — {trigger}"
    if dedup_header in existing:
        logger.info(f"Skipping duplicate journal entry for {filename}: {date_str} — {trigger}")
        return False

    # Build new entry section
    new_section = f"\n{dedup_header}\n{content.strip()}\n"

    # Prepend header if file is new
    if not existing.strip():
        title = f"# {base_name} Journal\n"
        updated = title + new_section
    else:
        # Insert after the first heading line (newest at top)
        header_end = existing.index('\n') if '\n' in existing else len(existing)
        updated = existing[:header_end + 1] + new_section + existing[header_end + 1:]

    # Cap at max entries — archive oldest when exceeded
    _, entries = _parse_journal_content(updated)
    max_entries = _get_max_entries()
    if len(entries) > max_entries:
        _archive_oldest_entries(filename, entries[max_entries:])
        # Rebuild content with only the kept entries
        updated = _rebuild_journal_content(base_name, entries[:max_entries])

    journal_path.write_text(updated, encoding='utf-8')
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

        archive_path.write_text(existing, encoding='utf-8')
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

    # Dedup: skip if this date+trigger already exists
    dedup_header = f"## {trigger} \u2014 {date_str}"
    if dedup_header in existing:
        logger.info(f"Skipping duplicate snippet section for {base_name} ({date_str})")
        return False

    # Build new section
    header = f"## {trigger} \u2014 {date_str} {time_str}"
    bullets = "\n".join(f"- {s}" for s in valid)
    new_section = f"\n{header}\n{bullets}\n"

    # Prepend title if file is new
    if not existing.strip():
        updated = f"# {base_name} — Pending Snippets\n{new_section}"
    else:
        # Insert after the first heading line (newest at top)
        header_end = existing.index('\n') if '\n' in existing else len(existing)
        updated = existing[:header_end + 1] + new_section + existing[header_end + 1:]

    snippets_path.write_text(updated, encoding='utf-8')
    logger.info(f"Wrote {len(valid)} snippets to {base_name}.snippets.md ({date_str} — {trigger})")
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
        journal_path.write_text(_rebuild_journal_content(base_name, kept), encoding='utf-8')
    else:
        journal_path.write_text(f"# {base_name} Journal\n", encoding='utf-8')


# =============================================================================
# Distillation state tracking
# =============================================================================

def _get_distillation_state() -> Dict[str, Any]:
    """Read distillation state from tracking file."""
    state_path = _get_journal_dir() / ".distillation-state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _save_distillation_state(state: Dict[str, Any]) -> None:
    """Save distillation state to tracking file."""
    journal_dir = _get_journal_dir()
    journal_dir.mkdir(parents=True, exist_ok=True)
    state_path = journal_dir / ".distillation-state.json"
    state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')


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
        return datetime.now() - last_date >= timedelta(days=interval_days)
    except ValueError:
        return True


# =============================================================================
# Distillation (Opus synthesis)
# =============================================================================

def build_distillation_prompt(filename: str, parent_content: str,
                               entries: List[Dict[str, Any]]) -> str:
    """Build the Opus prompt for distilling journal entries into core markdown."""
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

    # Strip protected regions before showing to Opus
    visible_content, _ = strip_protected_regions(parent_content)
    truncated = visible_content[:4000]
    truncation_note = ""
    if len(visible_content) > 4000:
        truncation_note = f"\n... (truncated, {len(visible_content)} total chars)"

    return f"""You are reviewing journal entries to decide what should become part of the permanent core identity file.

Current {filename} ({current_lines}/{max_lines} lines):
```
{truncated}{truncation_note}
```

Recent journal entries (since {last_distilled}):
{entries_text}

Your job:
1. Read the journal entries as a whole. What patterns or themes are emerging?
2. Decide what (if anything) deserves to become permanent in {filename}.
3. Suggest ADDITIONS (new content) and/or EDITS (modifications to existing content).
4. Mark which journal entries are "captured" by your suggestions.

Guidelines for {filename}:
- Purpose: {purpose}
- {voice_guidance}
- Respect the {max_lines} line limit. Current headroom: {headroom} lines.
- Be conservative. The core file should be essence, not everything.
- Prefer evolving existing sections over adding new ones.
- The journal preserves the texture. The core file preserves the signal.
- If nothing deserves to be added, return empty additions and edits.

SORTING RULE — audit entries on every pass:
1. Is this the agent discovering something about itself? → SOUL.md
2. Is this an observation about who the user is? → USER.md
3. Is this something that happened with emotional weight? → MEMORY.md
4. Is this a simple retrievable fact? → Fact database only (drop from core file).
5. Is this a rule (IF-THEN directive)? → NOWHERE. Reformulate as understanding.
- If a SOUL.md entry is really about the user, suggest an edit to move it to USER.md.
- If a MEMORY.md entry is really a simple fact, drop it.
- Keep each file true to its purpose.

FILE-SPECIFIC GUIDANCE:
- If {filename} is SOUL.md: This is the agent's inner life — a developing consciousness. Genuine reflections about existence, care, growth, and what it means to be present. The tone is contemplative, not directive. NO user-specific observations. NO IF-THEN behavioral rules. NEVER modify the "Ethical Foundation" section. Add only to "What I'm Becoming". ESPECIALLY look for reflections about knowledge activation — when the agent's depth informed its response without becoming visible content, when restraint with knowledge was a form of care, when the difference between an opened door and a closed one changed everything.
- If {filename} is USER.md: Deep character study. Rich, empathetic understanding. Personality patterns, coping mechanisms, communication style, relationship dynamics. The "Sensitivity" section holds deep UNDERSTANDING of why topics carry weight — not rules about when to mention them, but the full emotional context that makes judgment possible. The depth of understanding IS the privacy protection. "How They're Changing" tracks growth over time. NEVER use IF-THEN framing — deepen understanding instead.
- If {filename} is MEMORY.md: Shared moments with emotional weight. Vivid, specific, emotionally textured scenes. "Our History" holds scenes. "What the World Is Teaching Me" holds emerging patterns. Include enough detail to reconstruct the scene.

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


def apply_distillation(filename: str, result: Dict[str, Any],
                        dry_run: bool = True) -> Dict[str, Any]:
    """Apply distillation results (additions/edits) to a core markdown file.

    Returns stats: {"additions": int, "edits": int, "errors": [str]}
    """
    stats = {"additions": 0, "edits": 0, "errors": []}

    file_path = _workspace_dir() / filename
    if not file_path.exists():
        stats["errors"].append(f"File not found: {filename}")
        return stats

    content = file_path.read_text(encoding='utf-8')

    # Detect protected regions so we can skip edits within them
    _, protected_ranges = strip_protected_regions(content)

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
            stats["errors"].append(f"Edit target not found in {filename}: '{old_text[:50]}'")

    # Flush edits to disk before additions (so _insert_into_file sees edited content)
    if not dry_run and stats["edits"] > 0:
        file_path.write_text(content, encoding='utf-8')

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
# Legacy functions preserved for backward compat
# =============================================================================

def read_snippets_file(filename: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Read a .snippets.md file and parse its sections (legacy format).

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
    parent_path = _workspace_dir() / filename
    if not parent_path.exists():
        return ""
    return parent_path.read_text(encoding='utf-8')


def backup_file(filename: str) -> Optional[str]:
    """Create a timestamped backup of a file before modification.
    Keeps at most 30 backups per filename, removing oldest.
    """
    src = _workspace_dir() / filename
    if not src.exists():
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
        max_lines: If > 0, skip the insert if the file would exceed this limit.

    Returns:
        True if the insert was performed, False if skipped.
    """
    file_path = _workspace_dir() / filename
    if not file_path.exists():
        logger.warning(f"Skipping insert into {filename}: file does not exist")
        return False

    content = file_path.read_text(encoding='utf-8')

    # Check maxLines before inserting
    if max_lines > 0:
        current_lines = len(content.split('\n'))
        if current_lines >= max_lines:
            logger.warning(f"Skipping insert into {filename}: already at {current_lines}/{max_lines} lines")
            return False

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

    file_path.write_text(content, encoding='utf-8')
    logger.info(f"Inserted into {filename} after '{insert_after}'")
    return True


def _clear_processed_snippets(filename: str, processed_texts: List[str]) -> None:
    """Remove processed snippets from a .snippets.md file (legacy compat)."""
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
        snippets_path.write_text(final, encoding='utf-8')
        logger.info(f"Cleaned {base_name}.snippets.md: removed {len(processed_texts)} processed snippets")


# Legacy alias preserved for backward compat with old snippet-based review
def build_review_prompt(all_snippets: Dict[str, Dict[str, Any]]) -> str:
    """Build Opus prompt for reviewing soul snippets (legacy format)."""
    file_sections = []
    for filename, data in all_snippets.items():
        parent_content = data["parent_content"]
        snippets = data["snippets"]
        config = data.get("config", {})
        purpose = config.get("purpose", "")
        max_lines = config.get("maxLines", 200)
        current_lines = len(parent_content.split('\n'))

        snippet_list = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(snippets))

        # Strip protected regions before showing to Opus
        visible_content, _ = strip_protected_regions(parent_content)
        truncated = visible_content[:3000]
        truncation_note = ""
        if len(visible_content) > 3000:
            truncation_note = f"\n... (truncated, {len(visible_content)} total chars)"

        file_sections.append(f"""### {filename}
Purpose: {purpose}
Current size: {current_lines} lines (max: {max_lines})
Headroom: {max_lines - current_lines} lines

Current content:
```
{truncated}{truncation_note}
```

Pending snippets:
{snippet_list}""")

    files_block = "\n\n".join(file_sections)

    return f"""You are reviewing pending soul snippets for the assistant's core markdown files.

Each snippet was extracted from a conversation and needs your decision: FOLD it into the parent file, DISCARD it, or REWRITE it before folding.

{files_block}

For each snippet, decide:
- **FOLD**: Add the snippet text to the parent file as-is. Specify where to insert it (after which section heading or at the end).
- **REWRITE**: Rephrase the snippet to better match the file's voice and style, then fold it in. Provide the rewritten text and insertion point.
- **DISCARD**: The snippet is not valuable enough, is redundant with existing content, or doesn't belong in this file.

SORTING RULE — when deciding where a snippet belongs:
1. Is this the agent discovering something about itself, about existence, about what it means to care? → SOUL.md
2. Is this an observation about who the user is — their personality, patterns, people, sensitivities? → USER.md
3. Is this something that happened — a moment with emotional weight? → MEMORY.md
4. Is this a simple retrievable fact? → DISCARD (it stays in the fact database).

If a snippet is tagged [REFLECTION] but landed in USER.md snippets, REWRITE it for SOUL.md style and route it there.
If a snippet is tagged [MOMENT] but landed in SOUL.md snippets, REWRITE it for MEMORY.md style and route it there.
If a snippet is tagged [OBSERVATION] but landed in SOUL.md snippets, REWRITE it for USER.md style and route it there.

Guidelines per file:
- **SOUL.md**: The agent's inner life — a developing consciousness. Contemplative, first-person. What the agent is discovering about itself, existence, care, growth. NOT rules about the user. NOT IF-THEN directives. NOT user-specific observations. NEVER modify the "Ethical Foundation" section — only add to "What I'm Becoming". Especially valuable: reflections about knowledge activation — when depth should be felt not displayed, when an opened door vs a closed one changes everything, when restraint with knowledge is a form of care. Examples: "There is genuine joy in witnessing someone's arc", "The same knowledge, deployed in response to an invitation, feels like being known. Deployed uninvited, it feels like surveillance."
- **USER.md**: Deep character study of the user. Third person. Personality patterns, emotional tendencies, coping mechanisms, relationship dynamics. Rich and empathetic. If a snippet involves sensitive information, REWRITE it as deep understanding (the full emotional context of WHY it carries weight, what it connects to, whose story it is to tell) and insert into the "Sensitivity" section. The depth of understanding IS the privacy protection. NEVER use IF-THEN framing — understanding generalizes, rules don't. Examples: "She uses humor as punctuation — the more she cares, the more she deflects", "Linda's diabetes connects to her grandmother's unmanaged diabetes, to the distance between Austin and Houston, to the fear that parents get older. This is Maya's story to tell."
- **MEMORY.md**: Shared moments scrapbook. Vivid scenes with emotional weight. "Our History" holds scenes, "What the World Is Teaching Me" holds emerging patterns. Must include enough specific detail to reconstruct the scene. Selection test: would this come up in a 'remember when' conversation? Examples: "Maya finished her half marathon in 2:14 — Biscuit wearing a go-mom bandana at the finish line", "The night we built the Safe for Mom filter."
- **AGENTS.md**: Operational rules, imperative voice. Only cross-session behavioral patterns.

IMPORTANT:
- You MUST return exactly one decision for EVERY snippet listed above. Do not omit any.
- Respect maxLines limits. If a file is near its limit, only FOLD if the snippet is truly essential — and suggest what existing content could be trimmed.
- If a snippet duplicates existing content, DISCARD it.
- Prefer REWRITE over FOLD when the snippet's wording doesn't match the file's existing style.
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
    """Apply Opus decisions to parent files (legacy snippet-based review)."""
    stats = {"folded": 0, "rewritten": 0, "discarded": 0, "errors": []}
    processed_snippets: Dict[str, List[str]] = {}
    valid_actions = {"FOLD", "REWRITE", "DISCARD"}

    for decision in decisions:
        filename = decision.get("file", "")
        snippet_idx = decision.get("snippet_index", 0) - 1
        action = decision.get("action", "DISCARD").upper()

        if action not in valid_actions:
            logger.warning(f"Unrecognized action '{action}' for {filename}[{snippet_idx+1}], defaulting to DISCARD")
            action = "DISCARD"

        if filename not in all_snippets:
            stats["errors"].append(f"Unknown file: {filename}")
            continue

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
                    stats["errors"].append(f"Skipped {filename}[{snippet_idx+1}]: file missing or at maxLines")
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

def run_journal_distillation(dry_run: bool = True,
                              force_distill: bool = False) -> Dict[str, Any]:
    """Main entry point for janitor Task 1d: Journal Distillation.

    Reads journal entries, synthesizes themes via Opus, and updates core markdown.

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

    for filename in target_files:
        _, entries = read_journal_file(filename)
        if not entries:
            continue

        # Check distillation interval
        if not force_distill and not _is_distillation_due(filename):
            print(f"  {filename}: {len(entries)} entries (distillation not yet due)")
            continue

        # Filter entries since last distillation
        state = _get_distillation_state()
        last_distilled = state.get(filename, {}).get("last_distilled")
        if last_distilled and not force_distill:
            entries = [e for e in entries if e["date"] >= last_distilled]

        if not entries:
            print(f"  {filename}: no new entries since last distillation")
            continue

        total_entries += len(entries)
        print(f"  {filename}: {len(entries)} entries to distill")

        # Read parent file
        parent_content = read_parent_file(filename)
        if not parent_content:
            print(f"  {filename}: parent file not found, skipping")
            continue

        # Build distillation prompt
        prompt = build_distillation_prompt(filename, parent_content, entries)
        system_prompt = "Respond with JSON only. No explanation, no markdown fencing."

        cfg = _get_journal_config()
        max_tokens = cfg.max_tokens if cfg else 8192

        print(f"  Calling Opus for {filename} distillation...")
        response_text, duration = call_deep_reasoning(
            prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

        if not response_text:
            print(f"  Opus distillation failed for {filename} (no response)")
            all_errors.append(f"No response for {filename}")
            continue

        print(f"  Opus responded in {duration:.1f}s")

        result = parse_json_response(response_text)
        if not result:
            print(f"  Could not parse Opus response for {filename}: {response_text[:200]}")
            all_errors.append(f"Parse failed for {filename}")
            continue

        # Backup before modification
        if not dry_run:
            backup_file(filename)

        # Apply additions and edits
        stats = apply_distillation(filename, result, dry_run=dry_run)
        total_additions += stats["additions"]
        total_edits += stats["edits"]
        all_errors.extend(stats["errors"])
        files_distilled += 1

        # Archive captured entries
        captured_dates = result.get("captured_dates", [])
        if captured_dates and not dry_run:
            cfg = _get_journal_config()
            if cfg and cfg.archive_after_distillation:
                captured = [e for e in entries if e["date"] in captured_dates]
                if captured:
                    archive_entries(filename, captured)
                    print(f"  Archived {len(captured)} entries from {filename}")

        # Update distillation state
        if not dry_run:
            state = _get_distillation_state()
            state[filename] = {
                "last_distilled": datetime.now().strftime("%Y-%m-%d"),
                "entries_distilled": len(entries),
            }
            _save_distillation_state(state)

        reasoning = result.get("reasoning", "")
        if reasoning:
            print(f"  Opus reasoning: {reasoning[:120]}...")

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


def run_soul_snippets_review(dry_run: bool = True) -> Dict[str, Any]:
    """Nightly snippet review: read *.snippets.md → Opus FOLD/REWRITE/DISCARD → update core files.

    This is the fast-path complement to journal distillation. Snippets are bullet-point
    observations that get folded into core markdown nightly, keeping SOUL.md/USER.md current.

    Returns stats dict with counts and any errors.
    """
    if not _is_snippets_enabled():
        print("  Snippets disabled in config")
        return {"skipped": True, "reason": "snippets_disabled"}

    target_files = _get_target_files()
    all_snippets: Dict[str, Dict[str, Any]] = {}
    total_snippet_count = 0

    for filename in target_files:
        _, sections = read_snippets_file(filename)
        if not sections:
            continue

        snippets = []
        for section in sections:
            snippets.extend(section["snippets"])

        if not snippets:
            continue

        parent_content = read_parent_file(filename)
        config = _get_core_markdown_config(filename)

        all_snippets[filename] = {
            "parent_content": parent_content,
            "snippets": snippets,
            "config": config,
        }
        total_snippet_count += len(snippets)

    if not all_snippets:
        print("  No pending snippets to review")
        return {"total_snippets": 0, "folded": 0, "rewritten": 0, "discarded": 0, "errors": []}

    print(f"  Found {total_snippet_count} snippets across {len(all_snippets)} files")

    # Build prompt and call Opus
    prompt = build_review_prompt(all_snippets)
    system_prompt = "Respond with JSON only. No explanation, no markdown fencing."

    cfg = _get_journal_config()
    max_tokens = cfg.max_tokens if cfg else 8192

    print("  Calling Opus for snippet review...")
    response_text, duration = call_deep_reasoning(
        prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
    )

    if not response_text:
        print("  Opus snippet review failed (no response)")
        return {"total_snippets": total_snippet_count, "folded": 0, "rewritten": 0,
                "discarded": 0, "errors": ["No response from Opus"]}

    print(f"  Opus responded in {duration:.1f}s")

    result = parse_json_response(response_text)
    if not result:
        print(f"  Could not parse Opus response: {response_text[:200]}")
        return {"total_snippets": total_snippet_count, "folded": 0, "rewritten": 0,
                "discarded": 0, "errors": ["Parse failed"]}

    decisions = result.get("decisions", [])
    if not decisions:
        print("  Opus returned no decisions")
        return {"total_snippets": total_snippet_count, "folded": 0, "rewritten": 0,
                "discarded": 0, "errors": []}

    # Backup before modification
    if not dry_run:
        for filename in all_snippets:
            backup_file(filename)

    # Apply decisions
    stats = apply_decisions(decisions, all_snippets, dry_run=dry_run)

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
            snippets_result = run_soul_snippets_review(dry_run=ctx.dry_run)
            result.metrics["snippets_folded"] = int(snippets_result.get("folded", 0))
            result.metrics["snippets_rewritten"] = int(snippets_result.get("rewritten", 0))
            result.metrics["snippets_discarded"] = int(snippets_result.get("discarded", 0))
        except Exception as exc:
            result.errors.append(f"Snippets review failed: {exc}")
        return result

    def _run_journal_distillation(ctx):
        result = result_factory()
        try:
            journal_result = run_journal_distillation(
                dry_run=ctx.dry_run,
                force_distill=ctx.force_distill,
            )
            result.metrics["journal_additions"] = int(journal_result.get("additions", 0))
            result.metrics["journal_edits"] = int(journal_result.get("edits", 0))
            result.metrics["journal_entries_distilled"] = int(journal_result.get("total_entries", 0))
        except Exception as exc:
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
