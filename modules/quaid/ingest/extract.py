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
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_clients import call_deep_reasoning, parse_json_response
from config import get_config
from core.services.memory_service import get_memory_service
from core.lifecycle import soul_snippets as soul_snippets_runtime
from lib.runtime_context import (
    parse_session_jsonl as runtime_parse_session_jsonl,
    build_transcript as runtime_build_transcript,
)
from lib.fail_policy import is_fail_hard_enabled
from prompt_sets import get_prompt

logger = logging.getLogger(__name__)
_memory = get_memory_service()

MAX_EXTRACT_WALL_SECONDS = 600.0
_SOUL_SNIPPETS_MODULE = None


def _load_soul_snippets_module():
    global _SOUL_SNIPPETS_MODULE
    if _SOUL_SNIPPETS_MODULE is not None:
        return _SOUL_SNIPPETS_MODULE
    _SOUL_SNIPPETS_MODULE = soul_snippets_runtime
    return _SOUL_SNIPPETS_MODULE


def _load_extraction_prompt(domain_defs: Optional[Dict[str, str]] = None) -> str:
    """Load the extraction system prompt from file."""
    prompt = get_prompt("ingest.extraction.system")
    domain_defs = domain_defs or {}
    if domain_defs:
        lines = [
            "",
            "AVAILABLE DOMAINS (use exact ids in facts[].domains):",
        ]
        for domain_id, desc in sorted(domain_defs.items()):
            lines.append(f"- {domain_id}: {str(desc or '').strip()}")
        lines.extend([
            "",
            "DOMAIN OUTPUT CONTRACT (MANDATORY):",
            '- Every fact MUST include "domains": ["..."] with at least one allowed domain id.',
        ])
        prompt += "\n".join(lines) + "\n"
    return prompt


def _get_owner_id(override: Optional[str] = None) -> str:
    """Resolve owner ID from override, config, or default."""
    if override:
        return override
    try:
        cfg = get_config()
        return cfg.users.default_owner
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError("Failed to resolve extract owner from config") from exc
        logger.warning("[extract] owner resolution failed; defaulting to 'default': %s", exc)
        return "default"


def parse_session_jsonl(path: str) -> str:
    """Parse a platform session JSONL file into a human-readable transcript."""
    return runtime_parse_session_jsonl(Path(path))


def build_transcript(messages: List[Dict[str, str]]) -> str:
    """Format messages as 'User: ...\nAssistant: ...' transcript.

    Filters out system messages via the active adapter contract.
    """
    return runtime_build_transcript(messages)


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


def _apply_capture_skip_patterns(transcript: str, patterns: List[str]) -> str:
    """Remove transcript lines matching configured capture skip regex patterns."""
    if not patterns:
        return transcript
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error:
            logger.warning("[extract] invalid capture.skip_patterns regex ignored: %r", pattern)
    if not compiled:
        return transcript
    kept_lines = []
    removed = 0
    for line in transcript.splitlines():
        if any(rx.search(line) for rx in compiled):
            removed += 1
            continue
        kept_lines.append(line)
    if removed:
        logger.info("[extract] capture.skip_patterns removed %d transcript line(s)", removed)
    return "\n".join(kept_lines)


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
    actor_id: Optional[str] = None,
    speaker_entity_id: Optional[str] = None,
    subject_entity_id: Optional[str] = None,
    source_channel: Optional[str] = None,
    target_datastore: Optional[str] = None,
    source_conversation_id: Optional[str] = None,
    participant_entity_ids: Optional[List[str]] = None,
    source_author_id: Optional[str] = None,
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

    capture_skip_patterns: List[str] = []
    try:
        capture_cfg = get_config().capture
        if not bool(getattr(capture_cfg, "enabled", True)):
            logger.info(f"[extract] {label}: capture disabled, skipping extraction")
            return result
        raw_skip = getattr(capture_cfg, "skip_patterns", []) or []
        if isinstance(raw_skip, list):
            capture_skip_patterns = [str(p) for p in raw_skip if str(p).strip()]
    except Exception as exc:
        logger.warning("[extract] capture config read failed; proceeding without skip patterns: %s", exc)

    transcript = _apply_capture_skip_patterns(transcript, capture_skip_patterns)
    if not transcript.strip():
        logger.info(f"[extract] {label}: transcript emptied by capture.skip_patterns")
        return result

    # Resolve active domains once, before any LLM calls, and use this same snapshot
    # for both prompt injection and output validation.
    retrieval_cfg = get_config().retrieval
    domain_defs = getattr(retrieval_cfg, "domains", {}) or {}
    if not isinstance(domain_defs, dict):
        domain_defs = {}
    if not domain_defs:
        raise RuntimeError(
            "No active domains are registered for extraction. "
            "Configure domains through the memorydb contract before running extraction."
        )
    allowed_domains = {str(k).strip() for k in domain_defs.keys() if str(k).strip()}
    if not allowed_domains:
        raise RuntimeError(
            "No active domains are registered for extraction. "
            "Configure domains through the memorydb contract before running extraction."
        )

    # Load extraction prompt
    system_prompt = _load_extraction_prompt(domain_defs)

    # Chunk transcript for extraction (split at turn boundaries)
    try:
        chunk_size = get_config().capture.chunk_size
    except Exception as exc:
        logger.warning("[extract] capture.chunk_size config read failed; defaulting to 30000: %s", exc)
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
    extract_deadline = time.time() + MAX_EXTRACT_WALL_SECONDS

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

        remaining = extract_deadline - time.time()
        if remaining <= 0:
            logger.warning(
                f"[extract] {label}: extraction deadline reached after {ci}/{len(transcript_chunks)} chunks; "
                "stopping further chunk processing"
            )
            break

        response_text, duration = call_deep_reasoning(
            prompt=user_message,
            system_prompt=system_prompt,
            max_tokens=6144,
            timeout=min(600.0, remaining),
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
        if isinstance(parsed_facts, list):
            valid_facts: List[Dict[str, Any]] = []
            invalid_fact_count = 0
            for raw_fact in parsed_facts:
                if not isinstance(raw_fact, dict):
                    invalid_fact_count += 1
                    continue
                if not isinstance(raw_fact.get("text"), str):
                    invalid_fact_count += 1
                    continue
                valid_facts.append(raw_fact)
            if invalid_fact_count:
                logger.warning(
                    f"[extract] {label} chunk {ci + 1}: skipped {invalid_fact_count} invalid fact payload(s)"
                )
                result["facts_skipped"] += invalid_fact_count
            all_facts.extend(valid_facts)
            carry_facts.extend(valid_facts)

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
        domains = fact.get("domains")
        if isinstance(domains, str):
            domains = [domains]
        if not isinstance(domains, list):
            domains = []
        domains = [str(d).strip() for d in domains if str(d).strip()]
        if not domains:
            logger.warning(
                "[extract] skipped fact with missing required domains array (fact=%r)",
                text[:120],
            )
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text,
                "status": "skipped",
                "reason": "missing required domains",
            })
            continue
        invalid_domains = [d for d in domains if d not in allowed_domains]
        if invalid_domains:
            logger.warning(
                "[extract] skipped fact with unsupported domains %s (allowed=%s, fact=%r)",
                invalid_domains,
                sorted(allowed_domains),
                text[:120],
            )
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text,
                "status": "skipped",
                "reason": f"unsupported domains: {invalid_domains}",
            })
            continue
        project = fact.get("project")
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
            store_result = _memory.store(
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
                domains=domains,
                project=project,
                actor_id=actor_id,
                speaker_entity_id=speaker_entity_id,
                subject_entity_id=subject_entity_id,
                source_channel=source_channel,
                target_datastore=target_datastore,
                source_conversation_id=source_conversation_id,
                conversation_id=source_conversation_id,
                participant_entity_ids=participant_entity_ids,
                source_author_id=source_author_id,
            )

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
                            edge_result = _memory.create_edge(
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
                            logger.warning(
                                "[extract] %s: edge failed for %s --%s--> %s: %s",
                                label,
                                subj,
                                rel,
                                obj,
                                e,
                                exc_info=True,
                            )
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
            _load_soul_snippets_module().write_snippet_entry(filename, items, trigger=trigger)

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
            _load_soul_snippets_module().write_journal_entry(filename, text, trigger=trigger)

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
