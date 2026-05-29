"""Compatibility wrapper for archive operations.

Archive storage is datastore-owned. Import from
`datastore.memorydb.archive_store` for new code.
"""

def _archive_store_module():
    from datastore.memorydb import archive_store

    return archive_store


def _get_archive_conn(*args, **kwargs):
    return _archive_store_module()._get_archive_conn(*args, **kwargs)


def archive_node(*args, **kwargs):
    return _archive_store_module().archive_node(*args, **kwargs)


def is_fail_hard_enabled(*args, **kwargs):
    return _archive_store_module().is_fail_hard_enabled(*args, **kwargs)


def search_archive(*args, **kwargs):
    return _archive_store_module().search_archive(*args, **kwargs)

__all__ = [
    "_get_archive_conn",
    "archive_node",
    "is_fail_hard_enabled",
    "search_archive",
]
