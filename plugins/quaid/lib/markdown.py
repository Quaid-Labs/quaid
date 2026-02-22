"""Shared markdown processing helpers.

Used by workspace_audit.py and soul_snippets.py for handling
<!-- protected --> regions in core markdown files.
"""

import re
from typing import List, Tuple


def strip_protected_regions(content: str) -> Tuple[str, List[Tuple[int, int]]]:
    """Strip <!-- protected --> ... <!-- /protected --> blocks from content.

    Returns:
        (stripped_content, protected_ranges) where protected_ranges is a list of
        (start, end) character positions of the protected blocks in the original content.
    """
    pattern = re.compile(
        r'<!--\s*protected\s*-->.*?<!--\s*/protected\s*-->',
        re.DOTALL
    )

    protected_ranges = []
    for match in pattern.finditer(content):
        protected_ranges.append((match.start(), match.end()))

    if not protected_ranges:
        return content, []

    # Build stripped content by removing protected regions
    parts = []
    prev_end = 0
    for start, end in protected_ranges:
        parts.append(content[prev_end:start])
        prev_end = end
    parts.append(content[prev_end:])
    stripped = ''.join(parts)

    return stripped, protected_ranges


def is_position_protected(pos: int, protected_ranges: List[Tuple[int, int]]) -> bool:
    """Check if a character position falls within a protected region."""
    for start, end in protected_ranges:
        if start <= pos < end:
            return True
    return False


def section_overlaps_protected(section_start: int, section_end: int, protected_ranges: List[Tuple[int, int]]) -> bool:
    """Check if a section range overlaps with any protected region."""
    for p_start, p_end in protected_ranges:
        if p_start < section_end and p_end > section_start:
            return True
    return False
