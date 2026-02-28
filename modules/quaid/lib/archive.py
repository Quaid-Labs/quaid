"""Compatibility wrapper for archive operations.

Archive storage is datastore-owned. Import from
`datastore.memorydb.archive_store` for new code.
"""

from datastore.memorydb.archive_store import (
    _get_archive_conn,
    archive_node,
    is_fail_hard_enabled,
    search_archive,
)

__all__ = [
    "_get_archive_conn",
    "archive_node",
    "is_fail_hard_enabled",
    "search_archive",
]
