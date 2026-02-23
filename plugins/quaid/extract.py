#!/usr/bin/env python3
"""
Quaid Extraction Module — Extract memories from conversation transcripts.

Sends a transcript to Opus for fact/edge/snippet/journal extraction, then
stores everything via the existing Python infrastructure. This is the Python
equivalent of the extraction pipeline in index.ts.

Entry points:
    - extract_from_transcript(): Core extraction function
    - parse_session_jsonl(): Parse adapter session JSONL into transcript
    - CLI: python3 extract.py <file> [--dry-run] [--json] ...

Usage:
    python3 extract.py transcript.txt --owner alice
    python3 extract.py session.jsonl --dry-run --json
    echo "User: hi" | python3 extract.py - --owner alice
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_clients import call_deep_reasoning, parse_json_response
from api import store, create_edge
from datastore_maintenance import get_graph
from config import get_config
from soul_snippets import write_journal_entry, write_snippet_entry
from lib.runtime_context import parse_session_jsonl as _ctx_parse_session_jsonl, build_transcript as _ctx_build_transcript

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
    """Parse a platform session JSONL file into a human-readable transcript."""
    return _ctx_parse_session_jsonl(Path(path))


def build_transcript(messages: List[Dict[str, str]]) -> str:
    """Format messages as 'User: ...\nAssistant: ...' transcript.

    Filters out system messages via the active adapter contract.
    """
    return _ctx_build_transcript(messages)


def _chunk_messages(messages: List[Dict], max_chars: int = 30_000) -> List[List[Dict]]:
    """Split messages into chunks, never splitting a single message."""
    if not messages:
        return []
    chunks: List[List[Dict]] = []
    current: List[Dict] = []
    size = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        msg_size = len(str(msg.get("role", ""))) + len(str(content)) + 10
        if size + msg_size > max_chars and current:
            chunks.append(current)
            current, size = [], 0
        current.append(msg)
        size += msg_size
    if current:
        chunks.append(current)
    return chunks


def _chunk_transcript_text(transcript: str, max_chars: int = 30_000) -> List[str]:
    """Split plain text transcript at turn boundaries."""
    if len(transcript) <= max_chars:
        return [transcript]
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for turn in transcript.split('\n\n'):
        turn_size = len(turn) + 2
        if size + turn_size > max_chars and current:
            chunks.append('\n\n'.join(current))
            current, size = [], 0
        current.append(turn)
        size += turn_size
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def _build_chunk_carry_context(
    extracted_facts: List[Dict[str, Any]],
    max_items: int = 40,
    max_chars: int = 4000,
) -> str:
    """Build concise context from earlier chunk extractions.

    Carries forward high-signal facts to help pronoun/coreference resolution
    across chunk boundaries. Keeps output bounded to avoid prompt bloat.
    """
    if not extracted_facts:
        return ""

    weighted: List[tuple[int, str]] = []
    for fact in extracted_facts:
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text", "")).strip()
        if len(text.split()) < 3:
            continue

        conf = str(fact.get("extraction_confidence", "medium")).lower()
        score = 3 if conf == "high" else 2 if conf == "medium" else 1

        category = str(fact.get("category", "fact")).strip() or "fact"
        source = str(fact.get("source", "unknown")).strip() or "unknown"
        line = f"- [{category} | {source} | {conf}] {text}"

        edges = fact.get("edges", [])
        if isinstance(edges, list):
            edge_bits: List[str] = []
            for e in edges[:3]:
                if not isinstance(e, dict):
                    continue
                subj = str(e.get("subject", "")).strip()
                rel = str(e.get("relation", "")).strip()
                obj = str(e.get("object", "")).strip()
                if subj and rel and obj:
                    edge_bits.append(f"{subj} --{rel}--> {obj}")
            if edge_bits:
                line += f" | edges: {', '.join(edge_bits)}"

        weighted.append((score, line))

    if not weighted:
        return ""

    # Prefer higher-confidence lines first, then stable order.
    weighted.sort(key=lambda x: x[0], reverse=True)

    selected: List[str] = []
    used_chars = 0
    for _, line in weighted:
        if len(selected) >= max_items:
            break
        add_len = len(line) + (1 if selected else 0)
        if used_chars + add_len > max_chars:
            break
        selected.append(line)
        used_chars += add_len

    if not selected:
        return ""

    return "\n".join(selected)


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

    # Chunk transcript for extraction (split at turn boundaries)
    try:
        chunk_size = get_config().capture.chunk_size
    except Exception:
        chunk_size = 30_000
    transcript_chunks = _chunk_transcript_text(transcript, max_chars=chunk_size)

    MAX_CHUNKS = 10
    if len(transcript_chunks) > MAX_CHUNKS:
        logger.warning(f"[extract] {label}: transcript too large ({len(transcript_chunks)} chunks), capping at {MAX_CHUNKS}")
        transcript_chunks = transcript_chunks[:MAX_CHUNKS]

    if len(transcript_chunks) > 1:
        logger.info(f"[extract] {label}: splitting into {len(transcript_chunks)} chunks")

    logger.info(f"[extract] {label}: sending {len(transcript)} chars to Opus")

    # Extract from each chunk, merge results
    all_facts: List[Dict] = []
    all_snippets: Dict[str, List[str]] = {}
    all_journal: Dict[str, str] = {}
    carry_facts: List[Dict[str, Any]] = []

    for ci, chunk in enumerate(transcript_chunks):
        if not chunk.strip():
            continue

        if len(transcript_chunks) > 1:
            logger.info(f"[extract] {label}: chunk {ci + 1}/{len(transcript_chunks)} ({len(chunk)} chars)")

        carry_context = _build_chunk_carry_context(carry_facts)
        if carry_context:
            user_message = (
                "Use this context from earlier conversation chunks for continuity. "
                "Use it only as reference and avoid duplicate facts unless new details are added.\n\n"
                f"=== EARLIER CHUNK CONTEXT ===\n{carry_context}\n=== END CONTEXT ===\n\n"
                f"Extract memorable facts and journal entries from this conversation chunk:\n\n{chunk}"
            )
        else:
            user_message = f"Extract memorable facts and journal entries from this conversation chunk:\n\n{chunk}"

        response_text, duration = call_deep_reasoning(
            prompt=user_message,
            system_prompt=system_prompt,
            max_tokens=6144,
        )

        if not response_text:
            logger.error(f"[extract] {label} chunk {ci + 1}: Opus returned no response")
            continue

        logger.info(f"[extract] {label} chunk {ci + 1}: Opus responded in {duration:.1f}s")

        parsed = parse_json_response(response_text)
        if not parsed or not isinstance(parsed, dict):
            logger.error(f"[extract] {label} chunk {ci + 1}: could not parse Opus response: {response_text[:200]}")
            continue

        parsed_facts = parsed.get("facts", []) or []
        all_facts.extend(parsed_facts)
        if isinstance(parsed_facts, list):
            carry_facts.extend([f for f in parsed_facts if isinstance(f, dict)])

        for file, snips in (parsed.get("soul_snippets", {}) or {}).items():
            if isinstance(snips, list):
                combined = [s for s in (all_snippets.get(file, []) + snips) if isinstance(s, str)]
                all_snippets[file] = list(dict.fromkeys(combined))

        for file, entry in (parsed.get("journal_entries", {}) or {}).items():
            if isinstance(entry, list):
                entry = "\n\n".join(s for s in entry if isinstance(s, str))
            if isinstance(entry, str) and entry.strip():
                all_journal[file] = (all_journal[file] + "\n\n" + entry) if file in all_journal else entry

    facts = all_facts
    logger.info(f"[extract] {label}: LLM returned {len(facts)} candidate facts{f' from {len(transcript_chunks)} chunks' if len(transcript_chunks) > 1 else ''}")

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
        is_technical = bool(fact.get("is_technical", False))
        knowledge_type = "preference" if category == "preference" else "fact"
        source_label = f"{label}-extraction"
        raw_source = str(fact.get("source", "user")).strip().lower()
        source_type = (
            "assistant" if raw_source == "agent"
            else "both" if raw_source == "both"
            else "user"
        )

        fact_entry = {"text": text, "status": "pending", "edges": []}

        if not dry_run:
            store_result = store(
                text=text,
                category=category,
                verified=False,
                pinned=False,
                confidence=conf_num,
                privacy=privacy,
                source=source_label,
                source_id=session_id,
                owner_id=owner_id,
                session_id=session_id,
                knowledge_type=knowledge_type,
                keywords=keywords,
                source_type=source_type,
                is_technical=is_technical,
            )

            # Ensure is_technical survives dedup/update paths.
            fact_id = store_result.get("id")
            if fact_id and is_technical:
                try:
                    graph = get_graph()
                    node = graph.get_node(fact_id)
                    if node:
                        attrs = node.attributes if isinstance(node.attributes, dict) else {}
                        if not attrs.get("is_technical"):
                            attrs["is_technical"] = True
                            node.attributes = attrs
                            graph.update_node(node)
                except Exception:
                    pass

            if store_result.get("status") == "created":
                fact_entry["status"] = "stored"
                result["facts_stored"] += 1

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

            # Create edges for any successful fact write where we have a fact id.
            fact_id = store_result.get("id")
            if fact_id and isinstance(fact.get("edges"), list):
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
                                source_fact_id=fact_id,
                            )
                            if edge_result.get("status") == "created":
                                result["edges_created"] += 1
                                fact_entry["edges"].append(
                                    f"{subj} --{rel}--> {obj}"
                                )
                        except Exception as e:
                            logger.warning(f"[extract] {label}: edge failed: {e}")
        else:
            # Dry run — just mark as would-store
            fact_entry["status"] = "would_store"
            result["facts_stored"] += 1

        result["facts"].append(fact_entry)

    # Process snippets (from merged chunk results)
    if isinstance(all_snippets, dict):
        for filename, items in all_snippets.items():
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

    # Process journal entries (from merged chunk results)
    if isinstance(all_journal, dict):
        for filename, text in all_journal.items():
            if isinstance(text, str) and text.strip():
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
  python3 extract.py conversation.txt --owner alice
  python3 extract.py session.jsonl --dry-run --json
  cat transcript.txt | python3 extract.py - --owner alice
""",
    )
    parser.add_argument(
        "transcript",
        help="Path to transcript file (JSONL or text), or - for stdin",
    )
    parser.add_argument("--owner", default=None, help="Owner ID (default: from config)")
    parser.add_argument("--label", default="cli", help="Source label for logging")
    parser.add_argument("--session-id", default=None, help="Optional session ID")
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
        session_id=args.session_id,
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
