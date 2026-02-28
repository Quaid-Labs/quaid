"""Shared runtime state helpers for domain registry integration."""

from __future__ import annotations

import threading
from typing import Dict, Any

# Single lock used by all domain->runtime publication paths.
DOMAIN_PUBLISH_LOCK = threading.RLock()


def publish_domains_to_runtime_config(config_obj: Any, domains: Dict[str, str]) -> None:
    retrieval = getattr(config_obj, "retrieval", None)
    if retrieval is None:
        return
    with DOMAIN_PUBLISH_LOCK:
        setattr(retrieval, "domains", dict(domains))
