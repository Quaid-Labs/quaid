"""Core-owned identity/privacy runtime registration points.

Datastores should not import this module directly. Core orchestrators/services
use it to apply resolver/policy hooks while keeping single-registration safety.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from config import get_config


IdentityResolver = Callable[[Dict[str, Any]], Dict[str, Any]]
AccessDecision = Dict[str, Any]
PrivacyPolicy = Callable[[str, Dict[str, Any], Dict[str, Any]], Any]


@dataclass
class _RegisteredHook:
    owner: str
    fn: Callable[..., Any]


_identity_resolver: Optional[_RegisteredHook] = None
_privacy_policy: Optional[_RegisteredHook] = None
_ALLOWED_POLICY_ACTIONS = {"allow", "deny", "allow_redacted"}
_hooks_lock = threading.RLock()
_identity_mode_fallback_warned = False
logger = logging.getLogger(__name__)


def register_identity_resolver(owner: str, resolver: IdentityResolver) -> None:
    global _identity_resolver
    owner_name = str(owner or "").strip() or "unknown"
    with _hooks_lock:
        if _identity_resolver and _identity_resolver.owner != owner_name:
            raise RuntimeError(
                f"Identity resolver already registered by '{_identity_resolver.owner}', "
                f"cannot register another from '{owner_name}'."
            )
        _identity_resolver = _RegisteredHook(owner=owner_name, fn=resolver)


def register_privacy_policy(owner: str, policy: PrivacyPolicy) -> None:
    global _privacy_policy
    owner_name = str(owner or "").strip() or "unknown"
    with _hooks_lock:
        if _privacy_policy and _privacy_policy.owner != owner_name:
            raise RuntimeError(
                f"Privacy policy already registered by '{_privacy_policy.owner}', "
                f"cannot register another from '{owner_name}'."
            )
        _privacy_policy = _RegisteredHook(owner=owner_name, fn=policy)


def clear_registrations() -> None:
    global _identity_resolver, _privacy_policy, _identity_mode_fallback_warned
    with _hooks_lock:
        _identity_resolver = None
        _privacy_policy = None
        _identity_mode_fallback_warned = False


def _normalize_policy_decision(raw: Any) -> AccessDecision:
    """Normalize policy responses while preserving bool backward compatibility."""
    if isinstance(raw, bool):
        return {"action": "allow" if raw else "deny"}
    if not isinstance(raw, dict):
        return {"action": "deny", "reason": "invalid_policy_decision_type"}
    action = str(raw.get("action", "")).strip().lower()
    if action not in _ALLOWED_POLICY_ACTIONS:
        return {"action": "deny", "reason": "invalid_policy_action"}
    decision = dict(raw)
    decision["action"] = action
    return decision


def identity_mode() -> str:
    global _identity_mode_fallback_warned
    try:
        mode = str(get_config().identity.mode or "single_user").strip().lower()
    except Exception as exc:
        with _hooks_lock:
            if not _identity_mode_fallback_warned:
                logger.warning(
                    "identity_mode config read failed; defaulting to single_user: %s",
                    exc,
                )
                _identity_mode_fallback_warned = True
        mode = "single_user"
    return mode if mode in {"single_user", "multi_user"} else "single_user"


def assert_multi_user_runtime_ready(*, require_write: bool = False, require_read: bool = False) -> None:
    """Fail fast if multi-user mode is enabled without required hook wiring."""
    if identity_mode() != "multi_user":
        return
    with _hooks_lock:
        resolver = _identity_resolver
        policy = _privacy_policy
    resolver_owner = resolver.owner if resolver else "none"
    policy_owner = policy.owner if policy else "none"
    runtime_state = f"(resolver_owner={resolver_owner}, policy_owner={policy_owner})"
    if require_write and resolver is None:
        raise RuntimeError(
            "multi_user mode is enabled but no identity resolver is registered. "
            "Register exactly one resolver before write operations. "
            + runtime_state
        )
    if require_read and policy is None:
        raise RuntimeError(
            "multi_user mode is enabled but no privacy policy is registered. "
            "Register exactly one privacy policy before recall operations. "
            + runtime_state
        )


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
    with _hooks_lock:
        resolver = _identity_resolver
    if resolver is None:
        return payload
    resolved = resolver.fn(dict(payload))
    if not isinstance(resolved, dict):
        raise RuntimeError(
            f"identity resolver returned non-dict payload type={type(resolved).__name__}"
        )
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
    with _hooks_lock:
        policy = _privacy_policy
    if policy is None:
        return results
    viewer = str(viewer_entity_id or "").strip()
    if not viewer:
        raise ValueError("multi_user recall contract violation: viewer_entity_id is required")
    filtered: List[Dict[str, Any]] = []
    for row in results:
        decision = _normalize_policy_decision(policy.fn(viewer, row, context))
        action = decision.get("action")
        if action in {"allow", "allow_redacted"}:
            filtered.append(row)
    return filtered
