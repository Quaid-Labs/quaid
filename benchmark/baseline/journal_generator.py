#!/usr/bin/env python3
"""
Generate realistic journal files from stress test scenarios.

Converts scenarios.py weekly facts into memory/*.md files that look like
real OpenClaw daily journals. Also generates MEMORY.md from EDGE_FACTS.

Design decisions for fairness:
- Structured markdown with headers (matches real OpenClaw format)
- NOT pure bullet dumps (unfairly handicaps baseline)
- NOT elaborate prose (unfairly helps baseline)
- Deterministic templates, no LLM calls
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
if str(_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_DIR.parent))

import runner  # noqa: F401
from scenarios import (
    get_week_facts,
    get_evolution_facts,
    EDGE_FACTS,
    _base_date,
)


def _date_for_offset(week: int, day: int) -> datetime:
    """Get datetime for a given week + day offset."""
    return _base_date() + timedelta(weeks=week, days=day)


def _group_facts_by_category(facts: List[Dict]) -> Dict[str, List[str]]:
    """Group fact texts by category for journal sections."""
    groups: Dict[str, List[str]] = {
        "fact": [],
        "preference": [],
        "entity": [],
        "decision": [],
    }
    for f in facts:
        cat = f.get("category", "fact")
        if cat not in groups:
            cat = "fact"
        groups[cat].append(f["text"])
    return groups


def _format_journal_entry(date: datetime, facts: List[Dict]) -> str:
    """Format a single journal entry as structured markdown."""
    date_str = date.strftime("%Y-%m-%d")
    day_name = date.strftime("%A")
    groups = _group_facts_by_category(facts)

    lines = [f"# {date_str}", ""]

    # Summary line
    total = len(facts)
    lines.append(f"## Summary")
    lines.append(f"Notes from {day_name} — {total} items logged.")
    lines.append("")

    # What Happened (facts + experiences)
    if groups["fact"]:
        lines.append("## What Happened")
        for text in groups["fact"]:
            lines.append(f"- {text}")
        lines.append("")

    # People & Entities
    if groups["entity"]:
        lines.append("## People")
        for text in groups["entity"]:
            lines.append(f"- {text}")
        lines.append("")

    # Preferences & Decisions
    prefs_and_decisions = groups["preference"] + groups["decision"]
    if prefs_and_decisions:
        lines.append("## Preferences & Decisions")
        for text in prefs_and_decisions:
            lines.append(f"- {text}")
        lines.append("")

    return "\n".join(lines)


def _generate_memory_md() -> str:
    """Generate MEMORY.md from EDGE_FACTS — the baseline's equivalent of
    Quaid's entity nodes + edges.

    This is fair because real users curate MEMORY.md with core facts.
    Both systems get the same information, just in different forms.
    """
    family = []
    friends = []
    pets = []
    other = []

    for ef in EDGE_FACTS:
        text = ef["text"]
        t_lower = text.lower()
        if "mom" in t_lower or "dad" in t_lower:
            family.append(text)
        elif "dog" in t_lower or "cat" in t_lower or "pet" in t_lower or "biscuit" in t_lower or "pixel" in t_lower:
            pets.append(text)
        elif any(kw in t_lower for kw in ["friend", "coworker", "roommate", "college", "introduced"]):
            friends.append(text)
        else:
            other.append(text)

    lines = ["# Core Memories", ""]

    if family:
        lines.append("## Family")
        for text in family:
            lines.append(f"- {text}")
        lines.append("")

    if friends:
        lines.append("## Friends & Colleagues")
        for text in friends:
            lines.append(f"- {text}")
        lines.append("")

    if pets:
        lines.append("## Pets")
        for text in pets:
            lines.append(f"- {text}")
        lines.append("")

    if other:
        lines.append("## Other")
        for text in other:
            lines.append(f"- {text}")
        lines.append("")

    return "\n".join(lines)


def generate_journals(
    weeks: int,
    mode: str,
    output_dir: Path,
) -> Dict:
    """
    Generate journal files from scenario facts.

    For each week:
    1. Get facts from scenarios.get_week_facts(week, mode)
    2. Split into 2-3 journal entries per week (Mon/Wed/Fri pattern)
    3. Format as structured markdown
    4. Write to output_dir/YYYY-MM-DD.md

    Also generates MEMORY.md from EDGE_FACTS.

    Args:
        weeks: Number of weeks to generate
        mode: "fast" or "full" (controls fact count)
        output_dir: Directory to write journal files

    Returns:
        Dict with keys: files, memory_md_path, stats
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    files_written = []
    total_facts = 0

    for week in range(weeks):
        facts = get_week_facts(week, mode)

        # Also include evolution chain facts for this week
        evo_facts = get_evolution_facts(week)
        existing_texts = {f["text"] for f in facts}
        for ef in evo_facts:
            if ef["text"] not in existing_texts:
                facts.append(ef)
                existing_texts.add(ef["text"])

        total_facts += len(facts)

        # Split into 2-3 journal files per week (Mon/Wed/Fri)
        day_offsets = [0, 2, 4]  # Mon, Wed, Fri
        num_entries = min(3, max(2, len(facts) // 10))
        day_offsets = day_offsets[:num_entries]

        chunk_size = len(facts) // num_entries
        for i, day_offset in enumerate(day_offsets):
            start = i * chunk_size
            end = start + chunk_size if i < num_entries - 1 else len(facts)
            entry_facts = facts[start:end]

            if not entry_facts:
                continue

            date = _date_for_offset(week, day_offset)
            content = _format_journal_entry(date, entry_facts)
            filename = f"{date.strftime('%Y-%m-%d')}.md"
            filepath = output_dir / filename

            filepath.write_text(content)
            files_written.append(str(filepath))

    # Generate MEMORY.md
    memory_md_content = _generate_memory_md()
    memory_md_path = output_dir.parent / "MEMORY.md"
    memory_md_path.write_text(memory_md_content)

    return {
        "files": files_written,
        "memory_md_path": str(memory_md_path),
        "stats": {
            "total_files": len(files_written),
            "total_facts": total_facts,
            "weeks": weeks,
        },
    }


def main():
    """CLI for journal generation with optional dump for human review."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate baseline journal files")
    parser.add_argument("--weeks", type=int, default=1)
    parser.add_argument("--mode", choices=["fast", "full"], default="fast")
    parser.add_argument("--output-dir", type=str, default="/tmp/baseline-journals/memory")
    parser.add_argument("--dump", action="store_true", help="Print all generated files to stdout")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    result = generate_journals(args.weeks, args.mode, output_dir)

    print(f"Generated {result['stats']['total_files']} journal files "
          f"with {result['stats']['total_facts']} total facts")
    print(f"MEMORY.md: {result['memory_md_path']}")

    if args.dump:
        print("\n" + "=" * 60)
        # Print MEMORY.md
        memory_path = Path(result["memory_md_path"])
        print(f"\n--- {memory_path.name} ---")
        print(memory_path.read_text())
        # Print journal files
        for fp in result["files"]:
            print(f"\n--- {Path(fp).name} ---")
            print(Path(fp).read_text())

    return 0


if __name__ == "__main__":
    sys.exit(main())
