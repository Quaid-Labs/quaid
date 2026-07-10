"""Pure helpers for merging extracted fact variants before publish."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, List, Optional, Tuple


_OCCURRED_SOURCE_TIMESTAMP_FILL_KEY = "_occurred_filled_from_source_timestamp"


def _normalize_extracted_timestamp(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T23:59:59+00:00"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")


def _timestamp_sort_key(value: Any) -> Tuple[int, str]:
    normalized = _normalize_extracted_timestamp(value)
    if not normalized:
        return (1, "")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return (1, normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (0, parsed.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _parse_extracted_datetime(value: Any) -> Optional[datetime]:
    normalized = _normalize_extracted_timestamp(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None


def _source_week_bounds(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return the ISO-week bounds containing a source timestamp."""
    parsed = _parse_extracted_datetime(value)
    if parsed is None:
        return (None, None)
    week_start = parsed - timedelta(days=parsed.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=6)
    week_end = week_end.replace(hour=23, minute=59, second=59, microsecond=0)
    return (
        week_start.isoformat(timespec="seconds"),
        week_end.isoformat(timespec="seconds"),
    )


def _prefer_earlier_timestamp(first: Any, second: Any) -> Optional[str]:
    first_normalized = _normalize_extracted_timestamp(first)
    second_normalized = _normalize_extracted_timestamp(second)
    if first_normalized and second_normalized:
        return (
            first_normalized
            if _timestamp_sort_key(first_normalized) <= _timestamp_sort_key(second_normalized)
            else second_normalized
        )
    return first_normalized or second_normalized


def _prefer_later_timestamp(first: Any, second: Any) -> Optional[str]:
    first_normalized = _normalize_extracted_timestamp(first)
    second_normalized = _normalize_extracted_timestamp(second)
    if first_normalized and second_normalized:
        return (
            first_normalized
            if _timestamp_sort_key(first_normalized) >= _timestamp_sort_key(second_normalized)
            else second_normalized
        )
    return first_normalized or second_normalized


def _fact_text_key(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _confidence_rank(value: Any) -> int:
    conf = str(value or "medium").strip().lower()
    return {"high": 3, "medium": 2, "low": 1}.get(conf, 2)


def _fact_has_occurred_bounds(fact: Dict[str, Any]) -> bool:
    return bool(
        _normalize_extracted_timestamp(
            (fact or {}).get("occurred_start") or (fact or {}).get("occurred_at")
        )
        or _normalize_extracted_timestamp((fact or {}).get("occurred_end"))
    )


def _fact_source_timestamp(fact: Dict[str, Any]) -> Optional[str]:
    return _normalize_extracted_timestamp(
        (fact or {}).get("_source_timestamp")
        or (fact or {}).get("mentioned_at")
    )


def _fact_is_event_category(fact: Dict[str, Any]) -> bool:
    category = str(
        (fact or {}).get("category") or (fact or {}).get("type") or ""
    ).strip().casefold()
    return category in {"event", "milestone"}


def _fill_unbounded_event_occurred_from_source(fact: Dict[str, Any]) -> Dict[str, Any]:
    """Use the source week for event rows only when the extractor left occurred blank."""
    if (
        not isinstance(fact, dict)
        or not _fact_is_event_category(fact)
        or _fact_has_occurred_bounds(fact)
    ):
        return fact
    source_timestamp = _fact_source_timestamp(fact)
    if not source_timestamp:
        return fact
    source_start, source_end = _source_week_bounds(source_timestamp)
    if not source_start:
        return fact
    filled = dict(fact)
    filled["occurred_start"] = source_start
    filled["occurred_end"] = source_end or source_start
    filled[_OCCURRED_SOURCE_TIMESTAMP_FILL_KEY] = True
    return filled


def _strip_temporal_merge_internal_fields(fact: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(fact, dict) or _OCCURRED_SOURCE_TIMESTAMP_FILL_KEY not in fact:
        return fact
    cleaned = dict(fact)
    cleaned.pop(_OCCURRED_SOURCE_TIMESTAMP_FILL_KEY, None)
    return cleaned


def _alnum_char_grams(text: str, *, width: int = 4) -> set[str]:
    compact = "".join(ch.casefold() for ch in str(text or "") if ch.isalnum())
    if not compact:
        return set()
    gram_width = max(1, int(width or 1))
    if len(compact) <= gram_width:
        return {compact}
    return {compact[i:i + gram_width] for i in range(len(compact) - gram_width + 1)}


def _fact_text_temporal_variant_overlap(first: str, second: str) -> bool:
    # High-overlap character n-grams keep this language-neutral while limiting
    # matches to same-source restatements of one event, not broad semantic dedup.
    first_grams = _alnum_char_grams(first)
    second_grams = _alnum_char_grams(second)
    if len(first_grams) < 16 or len(second_grams) < 16:
        return False
    shared = len(first_grams & second_grams)
    union = len(first_grams | second_grams)
    smaller = min(len(first_grams), len(second_grams))
    return bool(
        union
        and smaller
        and (shared / union) >= 0.40
        and (shared / smaller) >= 0.60
    )


def _facts_share_source_day(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    first_source = _fact_source_timestamp(first)
    second_source = _fact_source_timestamp(second)
    if not first_source or not second_source:
        return False
    return first_source[:10] == second_source[:10]


def _facts_are_temporal_sibling_variants(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    """Detect same-payload variants of one extracted event without phrase rules."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    first_text = first.get("text", "")
    second_text = second.get("text", "")
    if not isinstance(first_text, str) or not isinstance(second_text, str):
        return False
    if not _facts_share_source_day(first, second):
        return False
    if not (
        _fact_has_occurred_bounds(first)
        or _fact_has_occurred_bounds(second)
        or _fact_is_event_category(first)
        or _fact_is_event_category(second)
    ):
        return False
    return _fact_text_temporal_variant_overlap(first_text, second_text)


def _timestamp_year(value: Any) -> Optional[int]:
    normalized = _normalize_extracted_timestamp(value)
    if not normalized:
        return None
    try:
        return int(normalized[:4])
    except Exception:
        return None


def _timestamp_day_distance(first: Any, second: Any) -> Optional[int]:
    first_normalized = _normalize_extracted_timestamp(first)
    second_normalized = _normalize_extracted_timestamp(second)
    if not first_normalized or not second_normalized:
        return None
    try:
        first_dt = datetime.fromisoformat(first_normalized.replace("Z", "+00:00"))
        second_dt = datetime.fromisoformat(second_normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if first_dt.tzinfo is None:
        first_dt = first_dt.replace(tzinfo=timezone.utc)
    if second_dt.tzinfo is None:
        second_dt = second_dt.replace(tzinfo=timezone.utc)
    return abs(
        (
            first_dt.astimezone(timezone.utc).date()
            - second_dt.astimezone(timezone.utc).date()
        ).days
    )


def _fact_text_contains_timestamp_year(fact: Dict[str, Any], timestamp: Any) -> bool:
    year = _timestamp_year(timestamp)
    if year is None:
        return False
    return str(year) in str((fact or {}).get("text") or "")


def _source_filled_occurred_should_win(source_filled: Dict[str, Any], other: Dict[str, Any]) -> bool:
    if not (
        bool((source_filled or {}).get(_OCCURRED_SOURCE_TIMESTAMP_FILL_KEY))
        or _fact_bounds_match_source_week(source_filled)
    ):
        return False
    source_timestamp = _fact_source_timestamp(source_filled)
    other_start = _normalize_extracted_timestamp((other or {}).get("occurred_start"))
    if not source_timestamp or not other_start:
        return False
    if _timestamp_year(source_timestamp) == _timestamp_year(other_start):
        return False
    distance = _timestamp_day_distance(source_timestamp, other_start)
    if distance is None or distance <= 370:
        return False
    return not _fact_text_contains_timestamp_year(other, other_start)


def _fact_bounds_match_source_week(fact: Dict[str, Any]) -> bool:
    source_timestamp = _fact_source_timestamp(fact)
    if not source_timestamp:
        return False
    source_start, source_end = _source_week_bounds(source_timestamp)
    if not source_start:
        return False
    return (
        _normalize_extracted_timestamp((fact or {}).get("occurred_start")) == source_start
        and _normalize_extracted_timestamp((fact or {}).get("occurred_end")) == (source_end or source_start)
    )


def _explicit_occurred_should_win_over_source_fill(explicit: Dict[str, Any], source_filled: Dict[str, Any]) -> bool:
    return (
        _fact_has_occurred_bounds(explicit)
        and not _fact_bounds_match_source_week(explicit)
        and _fact_bounds_match_source_week(source_filled)
    )


def _merged_occurred_bounds_for_duplicate(
    primary: Dict[str, Any],
    duplicate: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    if _source_filled_occurred_should_win(primary, duplicate):
        start = _normalize_extracted_timestamp(primary.get("occurred_start"))
        return (start, _normalize_extracted_timestamp(primary.get("occurred_end")) or start)
    if _source_filled_occurred_should_win(duplicate, primary):
        start = _normalize_extracted_timestamp(duplicate.get("occurred_start"))
        return (start, _normalize_extracted_timestamp(duplicate.get("occurred_end")) or start)
    if _explicit_occurred_should_win_over_source_fill(primary, duplicate):
        start = _normalize_extracted_timestamp(primary.get("occurred_start"))
        return (start, _normalize_extracted_timestamp(primary.get("occurred_end")) or start)
    if _explicit_occurred_should_win_over_source_fill(duplicate, primary):
        start = _normalize_extracted_timestamp(duplicate.get("occurred_start"))
        return (start, _normalize_extracted_timestamp(duplicate.get("occurred_end")) or start)
    return (
        _prefer_earlier_timestamp(primary.get("occurred_start"), duplicate.get("occurred_start")),
        _prefer_later_timestamp(primary.get("occurred_end"), duplicate.get("occurred_end")),
    )


def _merge_fact_edges(existing: Any, incoming: Any) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in (existing, incoming):
        if not isinstance(source, list):
            continue
        for edge in source:
            if not isinstance(edge, dict):
                continue
            subj = str(edge.get("subject", "") or "").strip()
            rel = str(edge.get("relation", "") or "").strip()
            obj = str(edge.get("object", "") or "").strip()
            key = (subj, rel, obj)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            merged.append({"subject": subj, "relation": rel, "object": obj})
    return merged


def _merge_fact_keywords(existing: Any, incoming: Any) -> Optional[str]:
    tokens: List[str] = []
    seen: set[str] = set()
    for raw in (existing, incoming):
        if not isinstance(raw, str):
            continue
        for token in raw.split():
            tok = token.strip()
            if not tok or tok in seen:
                continue
            seen.add(tok)
            tokens.append(tok)
    return " ".join(tokens) if tokens else None


def _fact_provenance_specificity(fact: Dict[str, Any]) -> int:
    score = 0
    raw_source = str((fact or {}).get("source", "") or "").strip().lower()
    if raw_source == "subagent":
        score += 100
    elif raw_source in {"assistant", "agent", "tool", "both", "user"}:
        score += 10
    if str((fact or {}).get("_source_label", "") or "").strip():
        score += 5
    if str((fact or {}).get("_source_id", "") or "").strip():
        score += 5
    if str(
        (fact or {}).get("_source_chunk_id")
        or (fact or {}).get("source_chunk_id")
        or ""
    ).strip():
        score += 3
    return score


def _merge_duplicate_fact_entries(primary: Dict[str, Any], duplicate: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(primary)
    selected_duplicate = False
    primary_has_occurred = _fact_has_occurred_bounds(primary)
    duplicate_has_occurred = _fact_has_occurred_bounds(duplicate)
    if duplicate_has_occurred and not primary_has_occurred:
        merged = dict(duplicate)
        selected_duplicate = True
    elif primary_has_occurred and not duplicate_has_occurred:
        merged = dict(primary)
    else:
        primary_provenance = _fact_provenance_specificity(primary)
        duplicate_provenance = _fact_provenance_specificity(duplicate)
        if duplicate_provenance > primary_provenance:
            merged = dict(duplicate)
            selected_duplicate = True
        elif duplicate_provenance == primary_provenance:
            primary_rank = _confidence_rank(primary.get("extraction_confidence"))
            duplicate_rank = _confidence_rank(duplicate.get("extraction_confidence"))
            if duplicate_rank > primary_rank:
                merged = dict(duplicate)
                selected_duplicate = True
            elif duplicate_rank == primary_rank:
                primary_text = str(primary.get("text", "") or "")
                duplicate_text = str(duplicate.get("text", "") or "")
                if len(duplicate_text) > len(primary_text):
                    merged = dict(duplicate)
                    selected_duplicate = True

    other = primary if selected_duplicate else duplicate

    merged["edges"] = _merge_fact_edges(merged.get("edges"), other.get("edges"))

    domains: List[str] = []
    seen_domains: set[str] = set()
    for source in (merged.get("domains"), other.get("domains")):
        if isinstance(source, str):
            source = [source]
        if not isinstance(source, list):
            continue
        for raw in source:
            dom = str(raw or "").strip()
            if not dom or dom in seen_domains:
                continue
            seen_domains.add(dom)
            domains.append(dom)
    if domains:
        merged["domains"] = domains

    keywords = _merge_fact_keywords(merged.get("keywords"), other.get("keywords"))
    if keywords:
        merged["keywords"] = keywords

    for key in ("category", "speaker", "project", "privacy"):
        if not merged.get(key) and other.get(key):
            merged[key] = other.get(key)

    if _fact_provenance_specificity(other) > _fact_provenance_specificity(merged):
        for key in (
            "source",
            "_source_label",
            "_source_id",
            "_source_chunk_id",
            "source_chunk_id",
            "_source_chunk_index",
        ):
            if other.get(key):
                merged[key] = other.get(key)
    else:
        for key in (
            "source",
            "_source_label",
            "_source_id",
            "_source_chunk_id",
            "source_chunk_id",
            "_source_chunk_index",
        ):
            if not merged.get(key) and other.get(key):
                merged[key] = other.get(key)

    if _confidence_rank(other.get("extraction_confidence")) > _confidence_rank(merged.get("extraction_confidence")):
        merged["extraction_confidence"] = other.get("extraction_confidence")

    merged.pop("created_at", None)
    merged_source_timestamp = _prefer_earlier_timestamp(
        merged.get("_source_timestamp"),
        other.get("_source_timestamp"),
    )
    if merged_source_timestamp:
        merged["_source_timestamp"] = merged_source_timestamp
    else:
        merged.pop("_source_timestamp", None)
    merged_occurred_start, merged_occurred_end = _merged_occurred_bounds_for_duplicate(merged, other)
    if merged_occurred_start:
        merged["occurred_start"] = merged_occurred_start
    else:
        merged.pop("occurred_start", None)
    if merged_occurred_end:
        merged["occurred_end"] = merged_occurred_end
    else:
        merged.pop("occurred_end", None)
    merged_mentioned_at = _prefer_earlier_timestamp(merged.get("mentioned_at"), other.get("mentioned_at"))
    if merged_mentioned_at:
        merged["mentioned_at"] = merged_mentioned_at
    else:
        merged.pop("mentioned_at", None)

    return merged


def facts_are_temporal_sibling_variants(first: Dict[str, Any], second: Dict[str, Any]) -> bool:
    """Public wrapper for datastore-boundary temporal duplicate checks."""
    return _facts_are_temporal_sibling_variants(first, second)


def merge_duplicate_fact_entries(primary: Dict[str, Any], duplicate: Dict[str, Any]) -> Dict[str, Any]:
    """Public wrapper for deterministic duplicate fact metadata merging."""
    return _strip_temporal_merge_internal_fields(_merge_duplicate_fact_entries(primary, duplicate))


def collapse_duplicate_payload_facts(facts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse duplicate fact variants within one extracted payload before publish.

    This is intentionally narrow: exact normalized-text duplicates are merged,
    plus same-source temporal sibling variants so a bounded event fact does not
    coexist with an unbounded duplicate emitted by the extractor.
    """
    collapsed: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    dropped = 0

    for fact in facts or []:
        if not isinstance(fact, dict):
            collapsed.append(fact)
            continue
        text = fact.get("text", "")
        if not isinstance(text, str):
            collapsed.append(fact)
            continue
        fact = _fill_unbounded_event_occurred_from_source(fact)
        key = _fact_text_key(text)
        if not key:
            collapsed.append(fact)
            continue
        prior_idx = seen.get(key)
        if prior_idx is not None:
            collapsed[prior_idx] = _merge_duplicate_fact_entries(collapsed[prior_idx], fact)
            dropped += 1
            continue
        temporal_prior_idx = None
        for idx, prior in enumerate(collapsed):
            if _facts_are_temporal_sibling_variants(prior, fact):
                temporal_prior_idx = idx
                break
        if temporal_prior_idx is not None:
            collapsed[temporal_prior_idx] = _merge_duplicate_fact_entries(collapsed[temporal_prior_idx], fact)
            dropped += 1
            continue
        seen[key] = len(collapsed)
        collapsed.append(dict(fact))

    return [_strip_temporal_merge_internal_fields(fact) for fact in collapsed], dropped
