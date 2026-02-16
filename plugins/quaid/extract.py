#!/usr/bin/env python3
"""
Quaid Extraction Module — Extract memories from conversation transcripts.

Sends a transcript to Opus for fact/edge/snippet/journal extraction, then
stores everything via the existing Python infrastructure. This is the Python
equivalent of the extraction pipeline in index.ts.

Entry points:
    - extract_from_transcript(): Core extraction function
    - parse_session_jsonl(): Parse OpenClaw session JSONL into transcript
    - CLI: python3 extract.py <file> [--dry-run] [--json] ...

Usage:
    python3 extract.py transcript.txt --owner solomon
    python3 extract.py session.jsonl --dry-run --json
    echo "User: hi" | python3 extract.py - --owner solomon
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_clients import call_high_reasoning, parse_json_response
from api import store, create_edge
from config import get_config
from soul_snippets import write_journal_entry, write_snippet_entry

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "extraction.txt"


def _load_extraction_prompt() -> str:
    """Load the extraction system prompt from file."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _get_owner_id(override: Optional[str] = None) -> str:
    """Resolve owner ID from override, config, or default."""
    if override:
        return override
    try:
        cfg = get_config()
        return cfg.users.default_owner
    except Exception:
        return "default"


def parse_session_jsonl(path: str) -> str:
    """Parse an OpenClaw session JSONL file into a human-readable transcript.

    Handles two JSONL formats:
    1. Wrapped: {"type": "message", "message": {"role": ..., "content": ...}}
    2. Direct:  {"role": ..., "content": ...}

    Returns a formatted "User: ...\n\nAssistant: ..." transcript string.
    """
    messages = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Unwrap if wrapped in {"type": "message", "message": {...}}
            if obj.get("type") == "message" and "message" in obj:
                obj = obj["message"]

            role = obj.get("role")
            if role not in ("user", "assistant"):
                continue

            content = obj.get("content", "")
            if isinstance(content, list):
                # Multi-part content blocks
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            if not isinstance(content, str) or not content.strip():
                continue

            messages.append({"role": role, "content": content.strip()})

    return build_transcript(messages)


def build_transcript(messages: List[Dict[str, str]]) -> str:
    """Format messages as 'User: ...\nAssistant: ...' transcript.

    Filters out system messages, heartbeats, and gateway restarts
    (matching the TS buildTranscript logic).
    """
    parts = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        text = msg.get("content", "")
        if not text:
            continue

        # Strip channel prefixes like [Telegram user@123]
        text = re.sub(r"^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*", "", text)
        # Strip message_id tags
        text = re.sub(r"\n?\[message_id:\s*\d+\]", "", text, flags=re.IGNORECASE).strip()

        # Skip system/restart/heartbeat messages
        if text.startswith("GatewayRestart:") or text.startswith("System:"):
            continue
        if '"kind": "restart"' in text:
            continue
        if "HEARTBEAT" in text and "HEARTBEAT_OK" in text:
            continue
        if re.sub(r"[*_<>/b\s]", "", text).startswith("HEARTBEAT_OK"):
            continue

        if not text:
            continue

        label = "User" if role == "user" else "Assistant"
        parts.append(f"{label}: {text}")

    return "\n\n".join(parts)


def extract_from_transcript(
    transcript: str,
    owner_id: str,
    label: str = "cli",
    session_id: Optional[str] = None,
    write_snippets: bool = True,
    write_journal: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Extract memories from a conversation transcript using Opus.

    Args:
        transcript: The conversation text to extract from.
        owner_id: Owner identity for stored memories.
        label: Source label for logging ("cli", "mcp", "compaction", "reset").
        session_id: Optional session identifier.
        write_snippets: Whether to write soul snippets.
        write_journal: Whether to write journal entries.
        dry_run: If True, parse and plan but don't store anything.

    Returns:
        {
            facts_stored: int, facts_skipped: int, edges_created: int,
            facts: [{text, status, edges}],
            snippets: {file: [str]}, journal: {file: str},
            dry_run: bool,
        }
    """
    result = {
        "facts_stored": 0,
        "facts_skipped": 0,
        "edges_created": 0,
        "facts": [],
        "snippets": {},
        "journal": {},
        "dry_run": dry_run,
    }

    if not transcript or not transcript.strip():
        logger.info(f"[extract] {label}: empty transcript, nothing to extract")
        return result

    # Load extraction prompt
    system_prompt = _load_extraction_prompt()

    # Truncate very long transcripts (matching TS limit of 100k chars)
    user_message = f"Extract memorable facts and journal entries from this conversation:\n\n{transcript[:100000]}"

    logger.info(f"[extract] {label}: sending {len(transcript)} chars to Opus")

    # Call Opus
    response_text, duration = call_high_reasoning(
        prompt=user_message,
        system_prompt=system_prompt,
        max_tokens=6144,
    )

    if not response_text:
        logger.error(f"[extract] {label}: Opus returned no response")
        return result

    logger.info(f"[extract] {label}: Opus responded in {duration:.1f}s")

    # Parse JSON response
    parsed = parse_json_response(response_text)
    if not parsed or not isinstance(parsed, dict):
        logger.error(f"[extract] {label}: could not parse Opus response: {response_text[:200]}")
        return result

    facts = parsed.get("facts", [])
    logger.info(f"[extract] {label}: LLM returned {len(facts)} candidate facts")

    # Process facts
    for fact in facts:
        if not isinstance(fact, dict):
            continue

        text = fact.get("text", "")
        if not text or len(text.strip().split()) < 3:
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text or "(empty)",
                "status": "skipped",
                "reason": "too short (need 3+ words)",
            })
            continue

        # Map confidence string to numeric
        conf_str = fact.get("extraction_confidence", "medium")
        conf_num = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf_str, 0.6)

        category = fact.get("category", "fact")
        privacy = fact.get("privacy", "shared")
        keywords = fact.get("keywords")
        knowledge_type = "preference" if category == "preference" else "fact"
        source_label = f"{label}-extraction"

        fact_entry = {"text": text, "status": "pending", "edges": []}

        if not dry_run:
            store_result = store(
                text=text,
                owner_id=owner_id,
                category=category,
                confidence=conf_num,
                source=source_label,
                knowledge_type=knowledge_type,
                source_type="user",
            )

            if store_result.get("status") == "created":
                fact_entry["status"] = "stored"
                result["facts_stored"] += 1

                # Create edges
                for edge in fact.get("edges", []):
                    if not isinstance(edge, dict):
                        continue
                    subj = edge.get("subject")
                    rel = edge.get("relation")
                    obj = edge.get("object")
                    if subj and rel and obj:
                        try:
                            edge_result = create_edge(
                                subject_name=subj,
                                relation=rel,
                                object_name=obj,
                                owner_id=owner_id,
                                source_fact_id=store_result.get("id"),
                            )
                            if edge_result.get("status") == "created":
                                result["edges_created"] += 1
                                fact_entry["edges"].append(
                                    f"{subj} --{rel}--> {obj}"
                                )
                        except Exception as e:
                            logger.warning(f"[extract] {label}: edge failed: {e}")

            elif store_result.get("status") == "duplicate":
                fact_entry["status"] = "duplicate"
                fact_entry["reason"] = store_result.get("existing_text", "")[:50]
                result["facts_skipped"] += 1
            elif store_result.get("status") == "updated":
                fact_entry["status"] = "updated"
                result["facts_stored"] += 1
            else:
                fact_entry["status"] = "failed"
                result["facts_skipped"] += 1
        else:
            # Dry run — just mark as would-store
            fact_entry["status"] = "would_store"
            result["facts_stored"] += 1

        result["facts"].append(fact_entry)

    # Process snippets
    snippets_raw = parsed.get("soul_snippets", {})
    if isinstance(snippets_raw, dict):
        for filename, items in snippets_raw.items():
            if not isinstance(items, list):
                continue
            valid = [s.strip() for s in items if isinstance(s, str) and s.strip()]
            if valid:
                result["snippets"][filename] = valid

    if write_snippets and result["snippets"] and not dry_run:
        trigger = "Compaction" if "compaction" in label.lower() else (
            "Reset" if "reset" in label.lower() else "CLI"
        )
        for filename, items in result["snippets"].items():
            write_snippet_entry(filename, items, trigger=trigger)

    # Process journal entries
    journal_raw = parsed.get("journal_entries", {})
    if isinstance(journal_raw, dict):
        for filename, entry in journal_raw.items():
            # Handle both string (expected) and array (LLM fallback)
            if isinstance(entry, list):
                text = "\n\n".join(s for s in entry if isinstance(s, str))
            elif isinstance(entry, str):
                text = entry
            else:
                text = ""
            if text.strip():
                result["journal"][filename] = text.strip()

    if write_journal and result["journal"] and not dry_run:
        trigger = "Compaction" if "compaction" in label.lower() else (
            "Reset" if "reset" in label.lower() else "CLI"
        )
        for filename, text in result["journal"].items():
            write_journal_entry(filename, text, trigger=trigger)

    logger.info(
        f"[extract] {label}: {result['facts_stored']} stored, "
        f"{result['facts_skipped']} skipped, {result['edges_created']} edges"
    )

    return result


def _format_human_summary(result: Dict[str, Any]) -> str:
    """Format extraction result as a human-readable summary."""
    lines = []
    prefix = "[DRY RUN] " if result.get("dry_run") else ""

    lines.append(f"{prefix}Extraction complete:")
    lines.append(f"  Facts stored:  {result['facts_stored']}")
    lines.append(f"  Facts skipped: {result['facts_skipped']}")
    lines.append(f"  Edges created: {result['edges_created']}")

    if result["snippets"]:
        total = sum(len(v) for v in result["snippets"].values())
        lines.append(f"  Snippets:      {total} across {len(result['snippets'])} files")

    if result["journal"]:
        lines.append(f"  Journal:       {len(result['journal'])} entries")

    if result["facts"]:
        lines.append("")
        lines.append("Facts:")
        for i, f in enumerate(result["facts"], 1):
            status = f["status"]
            text = f["text"][:80]
            marker = {
                "stored": "+", "updated": "~", "would_store": "?",
                "duplicate": "=", "skipped": "-", "failed": "!",
            }.get(status, " ")
            lines.append(f"  {marker} {i}. [{status}] {text}")
            if f.get("edges"):
                for edge in f["edges"]:
                    lines.append(f"        -> {edge}")

    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract memories from a conversation transcript.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 extract.py conversation.txt --owner solomon
  python3 extract.py session.jsonl --dry-run --json
  cat transcript.txt | python3 extract.py - --owner solomon
""",
    )
    parser.add_argument(
        "transcript",
        help="Path to transcript file (JSONL or text), or - for stdin",
    )
    parser.add_argument("--owner", default=None, help="Owner ID (default: from config)")
    parser.add_argument("--label", default="cli", help="Source label for logging")
    parser.add_argument("--dry-run", action="store_true", help="Parse and plan but don't store")
    parser.add_argument("--no-snippets", action="store_true", help="Skip writing snippets")
    parser.add_argument("--no-journal", action="store_true", help="Skip writing journal entries")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human summary")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    # Read transcript
    if args.transcript == "-":
        raw = sys.stdin.read()
    else:
        path = Path(args.transcript)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        raw = path.read_text(encoding="utf-8")

    # Detect JSONL and parse if needed
    transcript = raw
    source_path = args.transcript if args.transcript != "-" else None
    if source_path and source_path.endswith(".jsonl"):
        transcript = parse_session_jsonl(source_path)
    elif raw.lstrip().startswith("{"):
        # Heuristic: if first non-empty line is JSON, try JSONL parse
        try:
            first_line = raw.strip().split("\n")[0]
            obj = json.loads(first_line)
            if "role" in obj or ("type" in obj and "message" in obj):
                # Write to temp file for parse_session_jsonl
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False
                ) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                try:
                    transcript = parse_session_jsonl(tmp_path)
                finally:
                    os.unlink(tmp_path)
        except (json.JSONDecodeError, KeyError):
            pass  # Not JSONL, use as plain text

    if not transcript.strip():
        print("Error: empty transcript after parsing", file=sys.stderr)
        sys.exit(1)

    owner_id = _get_owner_id(args.owner)

    result = extract_from_transcript(
        transcript=transcript,
        owner_id=owner_id,
        label=args.label,
        write_snippets=not args.no_snippets,
        write_journal=not args.no_journal,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_format_human_summary(result))


if __name__ == "__main__":
    main()
