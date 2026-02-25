"""Core-owned identity/privacy runtime registration points.

Datastores should not import this module directly. Core orchestrators/services
use it to apply resolver/policy hooks while keeping single-registration safety.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from config import get_config


IdentityResolver = Callable[[Dict[str, Any]], Dict[str, Any]]
PrivacyPolicy = Callable[[str, Dict[str, Any], Dict[str, Any]], bool]


@dataclass
class _RegisteredHook:
    owner: str
    fn: Callable[..., Any]


_identity_resolver: Optional[_RegisteredHook] = None
_privacy_policy: Optional[_RegisteredHook] = None


def register_identity_resolver(owner: str, resolver: IdentityResolver) -> None:
    global _identity_resolver
    owner_name = str(owner or "").strip() or "unknown"
    if _identity_resolver and _identity_resolver.owner != owner_name:
        raise RuntimeError(
            f"Identity resolver already registered by '{_identity_resolver.owner}', "
            f"cannot register another from '{owner_name}'."
        )
    _identity_resolver = _RegisteredHook(owner=owner_name, fn=resolver)


def register_privacy_policy(owner: str, policy: PrivacyPolicy) -> None:
    global _privacy_policy
    owner_name = str(owner or "").strip() or "unknown"
    if _privacy_policy and _privacy_policy.owner != owner_name:
        raise RuntimeError(
            f"Privacy policy already registered by '{_privacy_policy.owner}', "
            f"cannot register another from '{owner_name}'."
        )
    _privacy_policy = _RegisteredHook(owner=owner_name, fn=policy)


def clear_registrations() -> None:
    global _identity_resolver, _privacy_policy
    _identity_resolver = None
    _privacy_policy = None


def identity_mode() -> str:
    try:
        mode = str(get_config().identity.mode or "single_user").strip().lower()
    except Exception:
        mode = "single_user"
    return mode if mode in {"single_user", "multi_user"} else "single_user"


def enforce_write_contract(payload: Dict[str, Any]) -> None:
    """Fail fast on malformed identity envelopes in multi-user mode."""
    if identity_mode() != "multi_user":
        return
    required = ("source_channel", "source_conversation_id", "source_author_id")
    missing = [k for k in required if not str(payload.get(k) or "").strip()]
    if missing:
        raise ValueError(
            "multi_user write contract violation: missing required identity fields: "
            + ", ".join(missing)
        )


def enrich_identity_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if _identity_resolver is None:
        return payload
    resolved = _identity_resolver.fn(dict(payload))
    if not isinstance(resolved, dict):
        raise RuntimeError("identity resolver returned non-dict payload")
    return resolved


def filter_recall_results(
    *,
    viewer_entity_id: Optional[str],
    results: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Apply privacy policy hook to recall output.

    In single-user mode or when no policy is registered, this is a no-op.
    """
    if identity_mode() != "multi_user":
        return results
    if _privacy_policy is None:
        return results
    viewer = str(viewer_entity_id or "").strip()
    if not viewer:
        raise ValueError("multi_user recall contract violation: viewer_entity_id is required")
    filtered: List[Dict[str, Any]] = []
    for row in results:
        if bool(_privacy_policy.fn(viewer, row, context)):
            filtered.append(row)
    return filtered

