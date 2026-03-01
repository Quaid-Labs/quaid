"""
Configuration loader for Memory System

Loads memory-specific settings from <quaid_home>/config/memory.json
Falls back to sensible defaults if config is missing.
"""

import copy
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from lib.runtime_context import get_workspace_dir
logger = logging.getLogger(__name__)


def _coerce_positive_int(raw: Any, default: int) -> int:
    """Return a positive int; fallback to default for invalid values."""
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def _coerce_nonnegative_int(raw: Any, default: int) -> int:
    """Return a non-negative int; fallback to default for invalid values."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _coerce_positive_float(raw: Any, default: float) -> float:
    """Return a positive float; fallback to default for invalid values."""
    try:
        value = float(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _default_deep_reasoning_model_classes() -> Dict[str, str]:
    return {}


def _default_fast_reasoning_model_classes() -> Dict[str, str]:
    return {}

def _workspace_root() -> Path:
    """Get workspace root from runtime context."""
    return get_workspace_dir()


def _config_paths() -> list:
    """Config file search paths (in priority order)."""
    root = _workspace_root()
    return [
        root / "config" / "memory.json",
        Path.home() / ".quaid" / "memory-config.json",
        Path("./memory-config.json"),
    ]


@dataclass
class ModelConfig:
    # User-selected providers (LLM and embeddings are independent).
    llm_provider: str = "default"        # "default" (gateway active provider) or explicit provider ID
    # Optional tier-specific provider overrides; "default" inherits llm_provider.
    fast_reasoning_provider: str = "default"
    deep_reasoning_provider: str = "default"
    embeddings_provider: str = "ollama"  # "ollama" (default) or adapter/provider-specific ID
    # Model names per tier; "default" resolves via *_reasoning_model_classes.
    fast_reasoning: str = "default"
    deep_reasoning: str = "default"
    deep_reasoning_model_classes: Dict[str, str] = field(default_factory=_default_deep_reasoning_model_classes)
    fast_reasoning_model_classes: Dict[str, str] = field(default_factory=_default_fast_reasoning_model_classes)
    fast_reasoning_context: int = 200000
    deep_reasoning_context: int = 200000
    fast_reasoning_max_output: int = 8192
    deep_reasoning_max_output: int = 16384
    batch_budget_percent: float = 0.50
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = ""

    def context_window(self, tier: str) -> int:
        """Get context window size for a model tier ('deep' or 'fast')."""
        if tier == 'deep':
            return self.deep_reasoning_context
        return self.fast_reasoning_context

    def max_output(self, tier: str) -> int:
        """Get max output tokens for a model tier ('deep' or 'fast')."""
        if tier == 'deep':
            return self.deep_reasoning_max_output
        return self.fast_reasoning_max_output


@dataclass
class CaptureConfig:
    enabled: bool = True
    strictness: str = "high"  # high | medium | low
    skip_patterns: List[str] = field(default_factory=list)
    inactivity_timeout_minutes: int = 120  # Extract after N minutes of inactivity (0 = disabled)
    auto_compaction_on_timeout: bool = True  # Trigger gateway compaction after timeout extraction
    chunk_size: int = 30_000  # Max chars per extraction chunk (messages never split)


@dataclass
class DecayConfig:
    enabled: bool = True
    threshold_days: int = 30
    rate_percent: float = 10.0
    minimum_confidence: float = 0.1
    protect_verified: bool = True
    protect_pinned: bool = True
    review_queue_enabled: bool = True
    # Ebbinghaus exponential decay parameters
    mode: str = "exponential"  # "linear" or "exponential"
    base_half_life_days: float = 60.0  # Half-life in days for standard facts
    access_bonus_factor: float = 0.15  # Each access extends half-life by this fraction


@dataclass
class DedupConfig:
    similarity_threshold: float = 0.85
    high_similarity_threshold: float = 0.95
    auto_reject_threshold: float = 0.98
    gray_zone_low: float = 0.88
    llm_verify_enabled: bool = True


@dataclass
class ContradictionConfig:
    enabled: bool = True
    timeout_minutes: int = 60
    min_similarity: float = 0.6
    max_similarity: float = 0.85


@dataclass
class OpusReviewConfig:
    enabled: bool = True
    batch_size: int = 50
    max_tokens: int = 4000
    model: str = ""  # Defaults to models.deep_reasoning at load time


@dataclass
class CoreParallelConfig:
    enabled: bool = True
    llm_workers: int = 4
    task_workers: Dict[str, int] = field(default_factory=dict)
    lifecycle_prepass_workers: int = 3
    lifecycle_prepass_timeout_seconds: int = 300
    lifecycle_prepass_timeout_retries: int = 1
    lock_enforcement_enabled: bool = True
    lock_wait_seconds: int = 120
    lock_require_registration: bool = True


@dataclass
class CoreConfig:
    parallel: CoreParallelConfig = field(default_factory=CoreParallelConfig)


@dataclass
class JanitorConfig:
    enabled: bool = True
    dry_run: bool = False
    apply_mode: str = "auto"  # master mode: auto | ask | dry_run
    token_budget: int = 0  # Max total LLM tokens per janitor run (0 = unlimited)
    approval_policies: Dict[str, str] = field(default_factory=lambda: {
        "core_markdown_writes": "ask",
        "project_docs_writes": "ask",
        "workspace_file_moves_deletes": "ask",
        "destructive_memory_ops": "auto",
    })
    task_timeout_minutes: int = 60
    run_tests: bool = False  # Only enable in dev (or set QUAID_DEV=1)
    opus_review: OpusReviewConfig = field(default_factory=OpusReviewConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    contradiction: ContradictionConfig = field(default_factory=ContradictionConfig)


@dataclass
class TraversalConfig:
    use_beam: bool = True  # Use BEAM search instead of BFS
    beam_width: int = 5  # Top-B candidates per hop level
    max_depth: int = 2  # Maximum traversal depth
    scoring_mode: str = "heuristic"
    hop_decay: float = 0.7  # Score decay per hop (0.7^depth)


@dataclass
class RetrievalConfig:
    default_limit: int = 5
    max_limit: int = 8
    min_similarity: float = 0.80
    notify_min_similarity: float = 0.85
    boost_recent: bool = True
    boost_frequent: bool = True
    max_tokens: int = 2000
    reranker_enabled: bool = True
    reranker_top_k: int = 20
    reranker_instruction: str = "Given a personal memory query, determine if this memory is relevant to the query"
    # Tuning parameters (externalized from hardcoded values)
    rrf_k: int = 60  # RRF fusion constant
    reranker_blend: float = 0.5  # Blend weight: reranker vs original score
    composite_relevance_weight: float = 0.60  # Weight for relevance in composite score
    composite_recency_weight: float = 0.20  # Weight for recency
    composite_frequency_weight: float = 0.15  # Weight for frequency
    multi_pass_gate: float = 0.70  # Quality gate for triggering second pass
    mmr_lambda: float = 0.7  # MMR diversity parameter
    co_session_decay: float = 0.6  # Score fraction for co-session facts
    recency_decay_days: int = 90  # Days over which recency decays to 0
    pre_injection_pass: bool = True  # Auto-inject: use total_recall planning pass
    router_fail_open: bool = True  # If true, total_recall router failures use deterministic fallback recall instead of raising
    fail_hard: bool = True  # If true, embedding outages raise instead of silent degraded fallback
    auto_inject: bool = False  # Auto-inject memories into context (Mem0-style)
    use_hyde: bool = True  # Enable HyDE query expansion by default
    domains: Dict[str, str] = field(default_factory=dict)  # Domain id -> brief description
    traversal: TraversalConfig = field(default_factory=TraversalConfig)


@dataclass
class LoggingConfig:
    enabled: bool = True
    level: str = "info"
    retention_days: int = 7
    components: List[str] = field(default_factory=lambda: ["memory", "janitor"])


@dataclass
class DatabaseConfig:
    path: str = "data/memory.db"
    archive_path: str = "data/memory_archive.db"
    wal_mode: bool = True


@dataclass
class OllamaConfig:
    url: str = "http://localhost:11434"
    embedding_model: str = "qwen3-embedding:8b"
    embedding_dim: int = 4096


@dataclass
class RagConfig:
    docs_dir: str = "docs"
    chunk_max_tokens: int = 800
    chunk_overlap_tokens: int = 100
    max_results: int = 5
    search_limit: int = 5
    min_similarity: float = 0.3


@dataclass
class SourceMapping:
    docs: List[str] = field(default_factory=list)
    label: str = ""


@dataclass
class CoreMarkdownFileConfig:
    purpose: str = ""
    maxLines: int = 200


@dataclass
class CoreMarkdownConfig:
    enabled: bool = True
    monitor_for_bloat: bool = True
    monitor_for_outdated: bool = True
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class JournalConfig:
    enabled: bool = True
    snippets_enabled: bool = True  # Enable snippet extraction (fast path, nightly review)
    mode: str = "distilled"  # "distilled" or "full"
    inject_full: bool = False  # EXPERIMENTAL: inject full journal into context every turn (uncapped size — use with caution)
    journal_dir: str = "journal"  # relative to workspace
    target_files: List[str] = field(default_factory=lambda: ["SOUL.md", "USER.md", "MEMORY.md"])
    max_entries_per_file: int = 50
    max_tokens: int = 8192
    distillation_interval_days: int = 7
    archive_after_distillation: bool = True


@dataclass
class DocsConfig:
    auto_update_on_compact: bool = True
    max_docs_per_update: int = 3
    staleness_check_enabled: bool = True
    update_timeout_seconds: int = 120  # Timeout for Opus doc updates
    notify_on_update: bool = True  # Notify user when docs are auto-updated
    source_mapping: Dict[str, SourceMapping] = field(default_factory=dict)
    doc_purposes: Dict[str, str] = field(default_factory=dict)
    core_markdown: CoreMarkdownConfig = field(default_factory=CoreMarkdownConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)


@dataclass
class ProjectDefinition:
    label: str = ""
    home_dir: str = ""
    source_roots: List[str] = field(default_factory=list)
    auto_index: bool = False
    patterns: List[str] = field(default_factory=lambda: ["*.md"])
    exclude: List[str] = field(default_factory=lambda: ["*.db", "*.log", "*.pyc", "__pycache__/"])
    description: str = ""
    state: str = "active"  # active, archived, deleted


@dataclass
class ProjectsConfig:
    enabled: bool = True
    projects_dir: str = "projects/"
    staging_dir: str = "projects/staging/"
    definitions: Dict[str, ProjectDefinition] = field(default_factory=dict)
    default_project: str = "default"


@dataclass
class UserIdentity:
    channels: Dict[str, List[str]] = field(default_factory=dict)  # channel_type -> [allowed_names]
    speakers: List[str] = field(default_factory=list)             # speaker name matches
    person_node_name: str = ""                                     # Name of the Person node in memory graph


@dataclass
class UsersConfig:
    default_owner: str = "default"
    identities: Dict[str, UserIdentity] = field(default_factory=dict)


@dataclass
class IdentityConfig:
    mode: str = "single_user"  # single_user | multi_user
    auto_link_threshold: float = 0.95
    require_review_threshold: float = 0.75


@dataclass
class PrivacyConfig:
    default_scope_dm: str = "private_subject"
    default_scope_group: str = "source_shared"
    enforce_strict_filters: bool = True


@dataclass
class FeatureNotificationConfig:
    """Per-feature notification settings (verbosity + channel routing)."""
    verbosity: Optional[str] = None  # "off", "summary", "full" — None inherits from master level
    channel: str = "last_used"       # "last_used" (follow session), or a specific channel name

    @staticmethod
    def from_config(value) -> 'FeatureNotificationConfig':
        """Parse per-feature notification config."""
        if value is None:
            return FeatureNotificationConfig()
        if isinstance(value, dict):
            return FeatureNotificationConfig(
                verbosity=value.get('verbosity', None),
                channel=value.get('channel', 'last_used'),
            )
        return FeatureNotificationConfig()


@dataclass
class NotificationsConfig:
    # Master verbosity level — sets defaults for all features below.
    # Levels: "quiet" (errors only), "normal" (summaries), "verbose" (full detail), "debug" (everything)
    level: str = "normal"

    # Per-feature settings (verbosity override + channel routing).
    janitor: FeatureNotificationConfig = field(default_factory=FeatureNotificationConfig)
    extraction: FeatureNotificationConfig = field(default_factory=FeatureNotificationConfig)
    retrieval: FeatureNotificationConfig = field(default_factory=FeatureNotificationConfig)

    # Notification presentation controls
    full_text: bool = False             # Show full text in notifications (no truncation)
    show_processing_start: bool = True  # Notify user when extraction starts
    project_create_enabled: bool = True  # Notify when a new project is registered

    # Level presets:
    #   quiet:   janitor=off, extraction=off, retrieval=off
    #   normal:  janitor=summary, extraction=summary, retrieval=off
    #   verbose: janitor=full, extraction=summary, retrieval=summary
    #   debug:   janitor=full, extraction=full, retrieval=full
    _LEVEL_DEFAULTS: dict = field(default_factory=lambda: {
        "quiet":   {"janitor": "off",     "extraction": "off",     "retrieval": "off"},
        "normal":  {"janitor": "summary", "extraction": "summary", "retrieval": "off"},
        "verbose": {"janitor": "full",    "extraction": "summary", "retrieval": "summary"},
        "debug":   {"janitor": "full",    "extraction": "full",    "retrieval": "full"},
    }, repr=False)

    def effective_level(self, feature: str) -> str:
        """Get effective verbosity for a feature ('janitor', 'extraction', 'retrieval').
        Per-feature override wins, otherwise falls back to master level preset."""
        feat_config = getattr(self, feature, None)
        if isinstance(feat_config, FeatureNotificationConfig) and feat_config.verbosity is not None:
            return feat_config.verbosity
        defaults = self._LEVEL_DEFAULTS.get(self.level, self._LEVEL_DEFAULTS["normal"])
        return defaults.get(feature, "off")

    def effective_channel(self, feature: str) -> str:
        """Get the delivery channel for a feature. Returns 'last_used' or a specific channel name."""
        feat_config = getattr(self, feature, None)
        if isinstance(feat_config, FeatureNotificationConfig):
            return feat_config.channel
        return "last_used"

    def should_notify(self, feature: str, detail: str = "summary") -> bool:
        """Check if a notification should fire.
        detail='summary' fires for 'summary' or 'full'. detail='full' only fires for 'full'."""
        eff = self.effective_level(feature)
        if eff == "off":
            return False
        if detail == "summary":
            return eff in ("summary", "full")
        if detail == "full":
            return eff == "full"
        return False


@dataclass
class SystemsConfig:
    memory: bool = True       # Extract and recall facts from conversations
    journal: bool = True      # Track personality evolution via snippets + journal
    projects: bool = True     # Auto-update project docs from code changes
    workspace: bool = True    # Monitor core markdown file health


@dataclass
class AdapterConfig:
    type: str = "standalone"  # standalone | openclaw


@dataclass
class PluginSlotsConfig:
    adapter: str = ""  # single active adapter plugin ID
    ingest: List[str] = field(default_factory=list)  # enabled ingest plugin IDs
    datastores: List[str] = field(default_factory=list)  # enabled datastore plugin IDs


@dataclass
class PluginsConfig:
    enabled: bool = True
    strict: bool = True  # fail boot on invalid manifests/registration conflicts
    api_version: int = 1
    paths: List[str] = field(default_factory=lambda: ["plugins"])
    allowlist: List[str] = field(default_factory=list)  # empty => allow all discovered
    slots: PluginSlotsConfig = field(default_factory=PluginSlotsConfig)
    config: Dict[str, Any] = field(default_factory=dict)  # plugin_id -> plugin-specific config payload


@dataclass
class MemoryConfig:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    core: CoreConfig = field(default_factory=CoreConfig)
    systems: SystemsConfig = field(default_factory=SystemsConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
    janitor: JanitorConfig = field(default_factory=JanitorConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    docs: DocsConfig = field(default_factory=DocsConfig)
    projects: ProjectsConfig = field(default_factory=ProjectsConfig)
    users: UsersConfig = field(default_factory=UsersConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    prompt_set: str = "default"


# Global config instance (lazy loaded)
_config: Optional[MemoryConfig] = None
_config_loading: bool = False  # Re-entrancy guard (load_config → get_db_path → get_config)
_config_lock = threading.RLock()
_warned_unknown_config_keys: set[str] = set()
_config_callbacks_lock = threading.RLock()
_config_callbacks: Dict[str, List[Callable[[Any, "MemoryConfig"], None]]] = {}

_KNOWN_TOP_LEVEL_CONFIG_KEYS = {
    "adapter",
    "capture",
    "core",
    "database",
    "decay",
    "docs",
    "identity",
    "janitor",
    "logging",
    "models",
    "notifications",
    "ollama",
    "plugins",
    "privacy",
    "projects",
    "rag",
    "retrieval",
    "systems",
    "users",
    "prompt_set",
}

_KNOWN_MODELS_KEYS = {
    "llm_provider",
    "provider",
    "fast_reasoning_provider",
    "deep_reasoning_provider",
    "embeddings_provider",
    "fast_reasoning",
    "deep_reasoning",
    "deep_reasoning_model_classes",
    "fast_reasoning_model_classes",
    "fast_reasoning_context",
    "deep_reasoning_context",
    "fast_reasoning_max_output",
    "deep_reasoning_max_output",
    "batch_budget_percent",
    "api_key_env",
    "base_url",
}

_KNOWN_CAPTURE_KEYS = {
    "enabled",
    "strictness",
    "skip_patterns",
    "inactivity_timeout_minutes",
    "auto_compaction_on_timeout",
    "chunk_size",
}

_KNOWN_RETRIEVAL_KEYS = {
    "default_limit",
    "max_limit",
    "min_similarity",
    "notify_min_similarity",
    "boost_recent",
    "boost_frequent",
    "max_tokens",
    "reranker_enabled",
    "reranker_top_k",
    "reranker_instruction",
    "rrf_k",
    "reranker_blend",
    "composite_relevance_weight",
    "composite_recency_weight",
    "composite_frequency_weight",
    "multi_pass_gate",
    "mmr_lambda",
    "co_session_decay",
    "recency_decay_days",
    "pre_injection_pass",
    "router_fail_open",
    "fail_hard",
    "auto_inject",
    "domains",
    "traversal",
    "reranker",
}

try:
    from datastore.memorydb.domain_defaults import default_domain_descriptions as _default_domain_descriptions
except Exception:
    def _default_domain_descriptions() -> Dict[str, str]:
        return {
            "personal": "identity, preferences, relationships, life events",
            "technical": "code, infra, APIs, architecture",
            "project": "project status, tasks, files, milestones",
            "work": "job/team/process decisions not deeply technical",
            "health": "training, injuries, routines, wellness",
            "finance": "budgeting, purchases, salary, bills",
            "travel": "trips, moves, places, logistics",
            "schedule": "dates, appointments, deadlines",
            "research": "options considered, comparisons, tradeoff analysis",
            "household": "home, chores, food planning, shared logistics",
            "legal": "contracts, policy, and regulatory constraints",
        }


_DEFAULT_DOMAIN_DESCRIPTIONS = _default_domain_descriptions()


def _normalize_domain_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    norm = re.sub(r"[^a-z0-9_]+", "_", raw)
    norm = re.sub(r"_{2,}", "_", norm).strip("_")
    return norm[:64]


def _validate_plugin_id(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if not re.fullmatch(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", token):
        raise ValueError(f"Invalid plugin id: {token}")
    return token


def register_config_callback(path: str, callback: Callable[[Any, "MemoryConfig"], None]) -> None:
    key = str(path or "").strip()
    if not key:
        raise ValueError("Callback path is required")
    with _config_callbacks_lock:
        callbacks = _config_callbacks.setdefault(key, [])
        if callback not in callbacks:
            callbacks.append(callback)


def _config_value_for_path(config: "MemoryConfig", path: str) -> Any:
    cur: Any = config
    for segment in str(path or "").split("."):
        token = segment.strip()
        if not token:
            continue
        if isinstance(cur, dict):
            cur = cur.get(token)
            continue
        cur = getattr(cur, token, None)
    return cur


def _run_config_callbacks(config: "MemoryConfig") -> None:
    strict = bool(getattr(getattr(config, "plugins", None), "strict", True))
    with _config_callbacks_lock:
        registered = {path: list(callbacks) for path, callbacks in _config_callbacks.items()}
    for path, callbacks in registered.items():
        value = _config_value_for_path(config, path)
        for callback in callbacks:
            try:
                callback(value, config)
            except Exception as exc:
                msg = f"Config callback failed for '{path}': {exc}"
                if strict:
                    raise ValueError(msg) from exc
                print(f"[plugins][warn] {msg}", file=sys.stderr)


def _on_adapter_slot_config(path_value: Any, _: "MemoryConfig") -> None:
    if path_value in (None, ""):
        return
    if not isinstance(path_value, str):
        raise ValueError("plugins.slots.adapter must be a string")
    _validate_plugin_id(path_value)


def _on_ingest_slots_config(path_value: Any, _: "MemoryConfig") -> None:
    if path_value in (None, ""):
        return
    if not isinstance(path_value, list):
        raise ValueError("plugins.slots.ingest must be a list of plugin ids")
    for item in path_value:
        _validate_plugin_id(item)


def _on_datastore_slots_config(path_value: Any, _: "MemoryConfig") -> None:
    if path_value in (None, ""):
        return
    if not isinstance(path_value, list):
        raise ValueError("plugins.slots.datastores must be a list of plugin ids")
    for item in path_value:
        _validate_plugin_id(item)


def _register_builtin_config_callbacks() -> None:
    register_config_callback("plugins.slots.adapter", _on_adapter_slot_config)
    register_config_callback("plugins.slots.ingest", _on_ingest_slots_config)
    register_config_callback("plugins.slots.datastores", _on_datastore_slots_config)


_register_builtin_config_callbacks()


def _warn_unknown_keys(section: str, data: Any, known_keys: set[str]) -> None:
    if not isinstance(data, dict):
        return
    for key in data.keys():
        token = f"{section}.{key}" if section else str(key)
        if key in known_keys:
            continue
        if token in _warned_unknown_config_keys:
            continue
        _warned_unknown_config_keys.add(token)
        if not os.environ.get("QUAID_QUIET"):
            print(f"[config] Unknown config key ignored: {token}", file=sys.stderr)


def _camel_to_snake(camel_str: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(camel_str):
        if char.isupper() and i > 0:
            result.append('_')
        result.append(char.lower())
    return ''.join(result)


def _load_nested(data: Dict[str, Any], keys_map: Dict[str, str] = None) -> Dict[str, Any]:
    """Convert camelCase keys to snake_case recursively."""
    result = {}
    for key, value in data.items():
        snake_key = _camel_to_snake(key)
        if isinstance(value, dict):
            result[snake_key] = _load_nested(value)
        elif isinstance(value, list):
            result[snake_key] = value
        else:
            result[snake_key] = value
    return result


def _extract_raw_plugin_config(raw_config: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve plugin-owned config keys as opaque payload (no key normalization)."""
    plugins = raw_config.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    cfg = plugins.get("config")
    if not isinstance(cfg, dict):
        return {}
    return dict(cfg)


def _extract_raw_user_identities(raw_config: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve user identity map keys and nested payload keys as-is."""
    users = raw_config.get("users")
    if not isinstance(users, dict):
        return {}
    identities = users.get("identities")
    if not isinstance(identities, dict):
        return {}
    out: Dict[str, Any] = {}
    for raw_user_id, raw_identity in identities.items():
        user_id = str(raw_user_id)
        if isinstance(raw_identity, dict):
            out[user_id] = copy.deepcopy(raw_identity)
        else:
            out[user_id] = {}
    return out


def _decode_json_list(raw_value: Any, *, field_name: str, default: Optional[List[Any]] = None) -> List[Any]:
    """Decode list-typed JSON fields defensively."""
    fallback = list(default or [])
    if not raw_value:
        return fallback
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid JSON for %s; using default: %s", field_name, exc)
        return fallback
    if not isinstance(parsed, list):
        logger.warning(
            "Invalid JSON type for %s (expected list, got %s); using default",
            field_name,
            type(parsed).__name__,
        )
        return fallback
    return parsed


def _coerce_list_field(value: Any, *, field_name: str, default: Optional[List[Any]] = None) -> List[Any]:
    """Normalize list-like config fields from raw JSON."""
    fallback = list(default or [])
    if value is None:
        return fallback
    if isinstance(value, list):
        return value
    logger.warning(
        "Invalid type for %s (expected list, got %s); using default",
        field_name,
        type(value).__name__,
    )
    return fallback


def load_config() -> MemoryConfig:
    """Load configuration from file or use defaults."""
    global _config, _config_loading

    with _config_lock:
        if _config is not None:
            return _config

        # Re-entrancy guard: load_config() → get_db_path() → _get_cfg() → load_config()
        # The project definitions loader calls get_db_path which recurses back here.
        # Return a default config to break the cycle on the same thread.
        if _config_loading:
            return MemoryConfig()

        _config_loading = True
        try:
            return _load_config_inner()
        finally:
            _config_loading = False


def _load_config_inner() -> MemoryConfig:
    """Inner config loader (called with re-entrancy guard held)."""
    global _config

    raw_config = {}

    for config_path in _config_paths():
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    raw_config = json.load(f)
                if not os.environ.get("QUAID_QUIET"):
                    print(f"[config] Loaded from {config_path}", file=sys.stderr)
                break
            except json.JSONDecodeError as e:
                print(f"[config] Failed to parse {config_path}: {e}", file=sys.stderr)
            except Exception as e:
                print(f"[config] Failed to read {config_path}: {e}", file=sys.stderr)

    if not raw_config:
        if not os.environ.get("QUAID_QUIET"):
            print("[config] Using defaults (no config file found)", file=sys.stderr)
    
    raw_plugin_config = _extract_raw_plugin_config(raw_config)
    raw_user_identities = _extract_raw_user_identities(raw_config)
    # Convert camelCase to snake_case
    config_data = _load_nested(raw_config)
    if raw_plugin_config:
        plugins_section = config_data.setdefault("plugins", {})
        if isinstance(plugins_section, dict):
            plugins_section["config"] = raw_plugin_config
    if raw_user_identities:
        users_section = config_data.setdefault("users", {})
        if isinstance(users_section, dict):
            users_section["identities"] = raw_user_identities
    _warn_unknown_keys("", config_data, _KNOWN_TOP_LEVEL_CONFIG_KEYS)
    _warn_unknown_keys("models", config_data.get("models", {}), _KNOWN_MODELS_KEYS)
    _warn_unknown_keys("capture", config_data.get("capture", {}), _KNOWN_CAPTURE_KEYS)
    _warn_unknown_keys("retrieval", config_data.get("retrieval", {}), _KNOWN_RETRIEVAL_KEYS)
    
    # Build config objects
    models_data = config_data.get('models', {})
    # New shape: separate deep/fast model class tables.
    raw_high_classes = models_data.get(
        'deep_reasoning_model_classes',
        models_data.get('deepReasoningModelClasses', _default_deep_reasoning_model_classes()),
    )
    raw_low_classes = models_data.get(
        'fast_reasoning_model_classes',
        models_data.get('fastReasoningModelClasses', _default_fast_reasoning_model_classes()),
    )
    deep_reasoning_model_classes = _default_deep_reasoning_model_classes()
    fast_reasoning_model_classes = _default_fast_reasoning_model_classes()
    if isinstance(raw_high_classes, dict):
        for provider, model in raw_high_classes.items():
            key = str(provider).strip().lower()
            val = str(model).strip()
            if key and val:
                deep_reasoning_model_classes[key] = val
    if isinstance(raw_low_classes, dict):
        for provider, model in raw_low_classes.items():
            key = str(provider).strip().lower()
            val = str(model).strip()
            if key and val:
                fast_reasoning_model_classes[key] = val

    models = ModelConfig(
        llm_provider=models_data.get('llm_provider', models_data.get('llmProvider', models_data.get('provider', 'default'))),
        fast_reasoning_provider=models_data.get('fast_reasoning_provider', models_data.get('fastReasoningProvider', 'default')),
        deep_reasoning_provider=models_data.get('deep_reasoning_provider', models_data.get('deepReasoningProvider', 'default')),
        embeddings_provider=models_data.get('embeddings_provider', models_data.get('embeddingsProvider', 'ollama')),
        fast_reasoning=models_data.get('fast_reasoning', ModelConfig.fast_reasoning),
        deep_reasoning=models_data.get('deep_reasoning', ModelConfig.deep_reasoning),
        deep_reasoning_model_classes=deep_reasoning_model_classes,
        fast_reasoning_model_classes=fast_reasoning_model_classes,
        fast_reasoning_context=_coerce_positive_int(models_data.get('fast_reasoning_context', 200000), 200000),
        deep_reasoning_context=_coerce_positive_int(models_data.get('deep_reasoning_context', 200000), 200000),
        fast_reasoning_max_output=_coerce_positive_int(models_data.get('fast_reasoning_max_output', 8192), 8192),
        deep_reasoning_max_output=_coerce_positive_int(models_data.get('deep_reasoning_max_output', 16384), 16384),
        batch_budget_percent=_coerce_positive_float(models_data.get('batch_budget_percent', 0.50), 0.50),
        api_key_env=str(models_data.get('api_key_env', models_data.get('apiKeyEnv', 'OPENAI_API_KEY')) or 'OPENAI_API_KEY'),
        base_url=str(models_data.get('base_url', models_data.get('baseUrl', '')) or ''),
    )

    capture_data = config_data.get('capture', {})
    capture = CaptureConfig(
        enabled=capture_data.get('enabled', True),
        strictness=capture_data.get('strictness', 'high'),
        skip_patterns=capture_data.get('skip_patterns', capture_data.get('skipPatterns', [])),
        inactivity_timeout_minutes=capture_data.get('inactivity_timeout_minutes', capture_data.get('inactivityTimeoutMinutes', 120)),
        auto_compaction_on_timeout=bool(capture_data.get('auto_compaction_on_timeout', capture_data.get('autoCompactionOnTimeout', True))),
        chunk_size=capture_data.get('chunk_size', capture_data.get('chunkSize', 30_000)),
    )
    
    decay_section = config_data.get('decay', {})
    decay = DecayConfig(
        enabled=decay_section.get('enabled', True),
        threshold_days=decay_section.get('threshold_days', 30),
        rate_percent=decay_section.get('rate_percent', 10.0),
        minimum_confidence=decay_section.get('minimum_confidence', 0.1),
        protect_verified=decay_section.get('protect_verified', True),
        protect_pinned=decay_section.get('protect_pinned', True),
        review_queue_enabled=decay_section.get('review_queue_enabled', True),
        mode=decay_section.get('mode', 'exponential'),
        base_half_life_days=decay_section.get('base_half_life_days', 60.0),
        access_bonus_factor=decay_section.get('access_bonus_factor', 0.15),
    )
    
    dedup = DedupConfig(
        similarity_threshold=config_data.get('janitor', {}).get('dedup', {}).get('similarity_threshold', 0.85),
        high_similarity_threshold=config_data.get('janitor', {}).get('dedup', {}).get('high_similarity_threshold', 0.95),
        auto_reject_threshold=config_data.get('janitor', {}).get('dedup', {}).get('auto_reject_threshold', 0.98),
        gray_zone_low=config_data.get('janitor', {}).get('dedup', {}).get('gray_zone_low', 0.88),
        llm_verify_enabled=config_data.get('janitor', {}).get('dedup', {}).get('llmVerifyEnabled',
                            config_data.get('janitor', {}).get('dedup', {}).get('llm_verify_enabled', True))
    )
    
    contradiction = ContradictionConfig(
        enabled=config_data.get('janitor', {}).get('contradiction', {}).get('enabled', True),
        timeout_minutes=config_data.get('janitor', {}).get('contradiction', {}).get('timeout_minutes', 60),
        min_similarity=config_data.get('janitor', {}).get('contradiction', {}).get('min_similarity', 0.6),
        max_similarity=config_data.get('janitor', {}).get('contradiction', {}).get('max_similarity', 0.85)
    )
    
    opus_review = OpusReviewConfig(
        enabled=config_data.get('janitor', {}).get('opus_review', {}).get('enabled', True),
        batch_size=config_data.get('janitor', {}).get('opus_review', {}).get('batch_size', 50),
        max_tokens=config_data.get('janitor', {}).get('opus_review', {}).get('max_tokens', 4000),
        model=config_data.get('janitor', {}).get('opus_review', {}).get('model', models.deep_reasoning)
    )

    core_parallel_data = config_data.get('core', {}).get('parallel', {})
    parallel_data = core_parallel_data if isinstance(core_parallel_data, dict) else {}
    raw_task_workers = parallel_data.get('task_workers', parallel_data.get('taskWorkers', {}))
    task_workers: Dict[str, int] = {}
    if isinstance(raw_task_workers, dict):
        for key, value in raw_task_workers.items():
            try:
                task_workers[str(key)] = max(1, int(value))
            except Exception:
                continue
    core_parallel = CoreParallelConfig(
        enabled=bool(parallel_data.get('enabled', True)),
        llm_workers=_coerce_positive_int(
            parallel_data.get('llm_workers', parallel_data.get('llmWorkers', 4)),
            4,
        ),
        task_workers=task_workers,
        lifecycle_prepass_workers=_coerce_positive_int(
            parallel_data.get(
                'lifecycle_prepass_workers',
                parallel_data.get('lifecyclePrepassWorkers', 3),
            ),
            3,
        ),
        lifecycle_prepass_timeout_seconds=_coerce_positive_int(
            parallel_data.get(
                'lifecycle_prepass_timeout_seconds',
                parallel_data.get('lifecyclePrepassTimeoutSeconds', 300),
            ),
            300,
        ),
        lifecycle_prepass_timeout_retries=_coerce_nonnegative_int(
            parallel_data.get(
                'lifecycle_prepass_timeout_retries',
                parallel_data.get('lifecyclePrepassTimeoutRetries', 1),
            ),
            1,
        ),
        lock_enforcement_enabled=bool(parallel_data.get(
            'lock_enforcement_enabled',
            parallel_data.get('lockEnforcementEnabled', True),
        )),
        lock_wait_seconds=_coerce_positive_int(
            parallel_data.get(
                'lock_wait_seconds',
                parallel_data.get('lockWaitSeconds', 120),
            ),
            120,
        ),
        lock_require_registration=bool(parallel_data.get(
            'lock_require_registration',
            parallel_data.get('lockRequireRegistration', True),
        )),
    )
    core_cfg = CoreConfig(parallel=core_parallel)
    
    janitor = JanitorConfig(
        enabled=config_data.get('janitor', {}).get('enabled', True),
        dry_run=config_data.get('janitor', {}).get('dry_run', False),
        apply_mode=config_data.get('janitor', {}).get('apply_mode',
                    config_data.get('janitor', {}).get('applyMode', 'auto')),
        token_budget=_coerce_nonnegative_int(
            config_data.get('janitor', {}).get(
                'token_budget',
                config_data.get('janitor', {}).get('tokenBudget', 0),
            ),
            0,
        ),
        approval_policies={
            "core_markdown_writes": str(config_data.get('janitor', {}).get('approval_policies', {})
                                        .get('core_markdown_writes',
                                        config_data.get('janitor', {}).get('approvalPolicies', {})
                                        .get('coreMarkdownWrites', 'ask'))),
            "project_docs_writes": str(config_data.get('janitor', {}).get('approval_policies', {})
                                       .get('project_docs_writes',
                                       config_data.get('janitor', {}).get('approvalPolicies', {})
                                       .get('projectDocsWrites', 'ask'))),
            "workspace_file_moves_deletes": str(config_data.get('janitor', {}).get('approval_policies', {})
                                                .get('workspace_file_moves_deletes',
                                                config_data.get('janitor', {}).get('approvalPolicies', {})
                                                .get('workspaceFileMovesDeletes', 'ask'))),
            "destructive_memory_ops": str(config_data.get('janitor', {}).get('approval_policies', {})
                                          .get('destructive_memory_ops',
                                          config_data.get('janitor', {}).get('approvalPolicies', {})
                                          .get('destructiveMemoryOps', 'auto'))),
        },
        task_timeout_minutes=config_data.get('janitor', {}).get('task_timeout_minutes', 60),
        run_tests=config_data.get('janitor', {}).get('run_tests',
                  config_data.get('janitor', {}).get('runTests', False)),
        opus_review=opus_review,
        dedup=dedup,
        contradiction=contradiction,
    )
    
    retrieval_data = config_data.get('retrieval', {})
    reranker_data = retrieval_data.get('reranker', {})
    traversal_data = retrieval_data.get('traversal', {})
    traversal = TraversalConfig(
        use_beam=traversal_data.get('useBeam', traversal_data.get('use_beam', True)),
        beam_width=traversal_data.get('beamWidth', traversal_data.get('beam_width', 5)),
        max_depth=traversal_data.get('maxDepth', traversal_data.get('max_depth', 2)),
        scoring_mode=traversal_data.get('scoringMode', traversal_data.get('scoring_mode', 'heuristic')),
        hop_decay=traversal_data.get('hopDecay', traversal_data.get('hop_decay', 0.7)),
    )
    _fail_hard_snake = retrieval_data.get('fail_hard', None)
    _fail_hard_camel = retrieval_data.get('failHard', None)
    if (
        isinstance(_fail_hard_snake, bool)
        and isinstance(_fail_hard_camel, bool)
        and _fail_hard_snake != _fail_hard_camel
    ):
        print(
            "[config] WARNING: retrieval.fail_hard and retrieval.failHard disagree; "
            "using retrieval.fail_hard as canonical value.",
            file=sys.stderr,
        )

    domains_data = retrieval_data.get("domains", {})
    parsed_domains: Dict[str, str] = {}
    if isinstance(domains_data, dict):
        for raw_key, raw_desc in domains_data.items():
            key = _normalize_domain_key(raw_key)
            if not key:
                continue
            parsed_domains[key] = str(raw_desc or "").strip() or _DEFAULT_DOMAIN_DESCRIPTIONS.get(key, "")
    elif isinstance(domains_data, list):
        for item in domains_data:
            if not isinstance(item, dict):
                continue
            key = _normalize_domain_key(item.get("id") or item.get("domain") or "")
            if not key:
                continue
            desc = str(item.get("description") or item.get("desc") or "").strip()
            parsed_domains[key] = desc or _DEFAULT_DOMAIN_DESCRIPTIONS.get(key, "")
    if not parsed_domains:
        parsed_domains = dict(_DEFAULT_DOMAIN_DESCRIPTIONS)

    retrieval = RetrievalConfig(
        default_limit=retrieval_data.get('default_limit', 5),
        max_limit=retrieval_data.get('max_limit', 8),
        min_similarity=retrieval_data.get('min_similarity', 0.80),
        notify_min_similarity=retrieval_data.get('notify_min_similarity',
                              retrieval_data.get('notifyMinSimilarity', 0.85)),
        boost_recent=retrieval_data.get('boost_recent', True),
        boost_frequent=retrieval_data.get('boost_frequent', True),
        max_tokens=retrieval_data.get('max_tokens', 2000),
        reranker_enabled=reranker_data.get('enabled', True),
        reranker_top_k=reranker_data.get('top_k', 20),
        reranker_instruction=reranker_data.get('instruction', 'Given a personal memory query, determine if this memory is relevant to the query'),
        rrf_k=retrieval_data.get('rrf_k', 60),
        reranker_blend=retrieval_data.get('reranker_blend', 0.5),
        composite_relevance_weight=retrieval_data.get('composite_relevance_weight', 0.60),
        composite_recency_weight=retrieval_data.get('composite_recency_weight', 0.20),
        composite_frequency_weight=retrieval_data.get('composite_frequency_weight', 0.15),
        multi_pass_gate=retrieval_data.get('multi_pass_gate', 0.70),
        mmr_lambda=retrieval_data.get('mmr_lambda', 0.7),
        co_session_decay=retrieval_data.get('co_session_decay', 0.6),
        recency_decay_days=retrieval_data.get('recency_decay_days', 90),
        pre_injection_pass=retrieval_data.get('pre_injection_pass', retrieval_data.get('preInjectionPass', True)),
        router_fail_open=bool(retrieval_data.get('router_fail_open', retrieval_data.get('routerFailOpen', True))),
        fail_hard=retrieval_data.get('fail_hard', retrieval_data.get('failHard', True)),
        auto_inject=retrieval_data.get('auto_inject', retrieval_data.get('autoInject', False)),
        use_hyde=retrieval_data.get('use_hyde', retrieval_data.get('useHyde', True)),
        domains=parsed_domains,
        traversal=traversal,
    )
    
    logging_cfg = LoggingConfig(
        enabled=config_data.get('logging', {}).get('enabled', True),
        level=config_data.get('logging', {}).get('level', 'info'),
        retention_days=config_data.get('logging', {}).get('retention_days', 7),
        components=config_data.get('logging', {}).get('components', ['memory', 'janitor'])
    )
    
    # Parse docs config
    docs_data = config_data.get('docs', {})
    source_mapping = {}
    for src_path, mapping_data in docs_data.get('source_mapping', {}).items():
        source_mapping[src_path] = SourceMapping(
            docs=mapping_data.get('docs', []),
            label=mapping_data.get('label', '')
        )

    # Parse coreMarkdown config - use raw_config to preserve filename keys
    # (snake_case conversion would corrupt "SOUL.md" → "s_o_u_l.md")
    raw_core_md = raw_config.get('docs', {}).get('coreMarkdown', {})
    core_markdown = CoreMarkdownConfig(
        enabled=raw_core_md.get('enabled', True),
        monitor_for_bloat=raw_core_md.get('monitorForBloat', True),
        monitor_for_outdated=raw_core_md.get('monitorForOutdated', True),
        files=raw_core_md.get('files', {})
    )

    # Parse journal config — use raw_config to preserve camelCase keys
    raw_journal = raw_config.get('docs', {}).get('journal', {})
    _default_targets = ["SOUL.md", "USER.md", "MEMORY.md"]
    journal = JournalConfig(
        enabled=raw_journal.get('enabled', True),
        snippets_enabled=raw_journal.get('snippetsEnabled', True),
        mode=raw_journal.get('mode', 'distilled'),
        inject_full=raw_journal.get('injectFull', False),
        journal_dir=raw_journal.get('journalDir', 'journal'),
        target_files=raw_journal.get('targetFiles', _default_targets),
        max_entries_per_file=raw_journal.get('maxEntriesPerFile',
                             raw_journal.get('maxSnippetsPerFile', 50)),
        max_tokens=raw_journal.get('maxTokens', 8192),
        distillation_interval_days=raw_journal.get('distillationIntervalDays', 7),
        archive_after_distillation=raw_journal.get('archiveAfterDistillation', True),
    )

    docs = DocsConfig(
        auto_update_on_compact=docs_data.get('auto_update_on_compact', True),
        max_docs_per_update=docs_data.get('max_docs_per_update', 3),
        staleness_check_enabled=docs_data.get('staleness_check_enabled', True),
        update_timeout_seconds=docs_data.get('update_timeout_seconds', 120),
        notify_on_update=docs_data.get('notify_on_update', True),
        source_mapping=source_mapping,
        doc_purposes=docs_data.get('doc_purposes', {}),
        core_markdown=core_markdown,
        journal=journal,
    )

    # Parse projects config — load definitions from DB (source of truth)
    # Direct DB query avoids DocsRegistry side effects (seeding, WORKSPACE dependency)
    raw_projects = raw_config.get('projects', {})
    project_definitions = {}
    try:
        from lib.config import get_db_path as _get_db_path
        from lib.database import get_connection as _get_conn
        _db = _get_db_path()
        if _db.exists():
            with _get_conn(_db) as _conn:
                _rows = _conn.execute(
                    "SELECT * FROM project_definitions WHERE state = 'active'"
                ).fetchall()
                for _row in _rows:
                    project_definitions[_row["name"]] = ProjectDefinition(
                        label=_row["label"],
                        home_dir=_row["home_dir"],
                        source_roots=_decode_json_list(
                            _row["source_roots"],
                            field_name="project_definitions.source_roots",
                        ),
                        auto_index=bool(_row["auto_index"]),
                        patterns=_decode_json_list(
                            _row["patterns"],
                            field_name="project_definitions.patterns",
                            default=["*.md"],
                        ),
                        exclude=_decode_json_list(
                            _row["exclude"],
                            field_name="project_definitions.exclude",
                        ),
                        description=_row["description"] or "",
                        state=_row["state"],
                    )
    except Exception as exc:
        logger.warning("Failed to load project definitions from datastore; falling back to JSON config: %s", exc)
    if not project_definitions:
        # Fallback: load from JSON if DB not available (fresh install, tests)
        for proj_name, proj_data in raw_projects.get('definitions', {}).items():
            project_definitions[proj_name] = ProjectDefinition(
                label=proj_data.get('label', ''),
                home_dir=proj_data.get('homeDir', ''),
                source_roots=_coerce_list_field(
                    proj_data.get('sourceRoots'),
                    field_name=f"projects.definitions.{proj_name}.sourceRoots",
                ),
                auto_index=proj_data.get('autoIndex', False),
                patterns=_coerce_list_field(
                    proj_data.get('patterns'),
                    field_name=f"projects.definitions.{proj_name}.patterns",
                    default=['*.md'],
                ),
                exclude=_coerce_list_field(
                    proj_data.get('exclude'),
                    field_name=f"projects.definitions.{proj_name}.exclude",
                    default=['*.db', '*.log', '*.pyc', '__pycache__/'],
                ),
                description=proj_data.get('description', ''),
            )
    projects = ProjectsConfig(
        enabled=raw_projects.get('enabled', True),
        projects_dir=raw_projects.get('projectsDir', 'projects/'),
        staging_dir=raw_projects.get('stagingDir', 'projects/staging/'),
        definitions=project_definitions,
        default_project=raw_projects.get('defaultProject', 'default'),
    )

    # Parse users config
    users_data = config_data.get('users', {})
    identities = {}
    for user_id, identity_data in users_data.get('identities', {}).items():
        identities[user_id] = UserIdentity(
            channels=identity_data.get('channels', {}),
            speakers=identity_data.get('speakers', []),
            person_node_name=identity_data.get('person_node_name', '')
        )
    users = UsersConfig(
        default_owner=users_data.get('default_owner', 'default'),
        identities=identities
    )

    # Parse identity config (forward-looking multi-user mode control)
    identity_data = config_data.get('identity', {})
    identity_mode = str(identity_data.get('mode', 'single_user')).strip().lower()
    if identity_mode not in {'single_user', 'multi_user'}:
        identity_mode = 'single_user'
    identity = IdentityConfig(
        mode=identity_mode,
        auto_link_threshold=float(identity_data.get('auto_link_threshold',
                                  identity_data.get('autoLinkThreshold', 0.95))),
        require_review_threshold=float(identity_data.get('require_review_threshold',
                                     identity_data.get('requireReviewThreshold', 0.75))),
    )

    # Parse privacy config (policy defaults)
    privacy_data = config_data.get('privacy', {})
    privacy = PrivacyConfig(
        default_scope_dm=str(privacy_data.get('default_scope_dm',
                             privacy_data.get('defaultScopeDm', 'private_subject'))),
        default_scope_group=str(privacy_data.get('default_scope_group',
                                privacy_data.get('defaultScopeGroup', 'source_shared'))),
        enforce_strict_filters=bool(privacy_data.get('enforce_strict_filters',
                                   privacy_data.get('enforceStrictFilters', True))),
    )

    # Parse database config
    db_data = config_data.get('database', {})
    database = DatabaseConfig(
        path=db_data.get('path', 'data/memory.db'),
        archive_path=db_data.get('archive_path', 'data/memory_archive.db'),
        wal_mode=db_data.get('wal_mode', True)
    )

    # Parse ollama config — support both camelCase (JSON) and snake_case (converted) keys
    ollama_data = config_data.get('ollama', {})
    ollama = OllamaConfig(
        url=ollama_data.get('url', 'http://localhost:11434'),
        embedding_model=ollama_data.get('embedding_model', ollama_data.get('embeddingModel', 'qwen3-embedding:8b')),
        embedding_dim=ollama_data.get('embedding_dim', ollama_data.get('embeddingDim', 4096)),
    )

    # Parse rag config
    rag_data = config_data.get('rag', {})
    rag = RagConfig(
        docs_dir=rag_data.get('docs_dir', 'docs'),
        chunk_max_tokens=rag_data.get('chunk_max_tokens', 800),
        chunk_overlap_tokens=rag_data.get('chunk_overlap_tokens', 100),
        max_results=rag_data.get('max_results', 5),
        search_limit=rag_data.get('search_limit', 5),
        min_similarity=rag_data.get('min_similarity', 0.3)
    )

    # Parse notifications config
    notif_data = config_data.get('notifications', {})
    project_create_data = notif_data.get('project_create', {})
    if not isinstance(project_create_data, dict):
        project_create_data = {}
    project_create_camel_data = notif_data.get('projectCreate', {})
    if not isinstance(project_create_camel_data, dict):
        project_create_camel_data = {}
    notifications = NotificationsConfig(
        level=notif_data.get('level', 'normal'),
        janitor=FeatureNotificationConfig.from_config(notif_data.get('janitor', None)),
        extraction=FeatureNotificationConfig.from_config(notif_data.get('extraction', None)),
        retrieval=FeatureNotificationConfig.from_config(notif_data.get('retrieval', None)),
        full_text=notif_data.get('full_text', False),
        show_processing_start=notif_data.get('show_processing_start', True),
        project_create_enabled=bool(
            project_create_data.get(
                'enabled',
                project_create_camel_data.get('enabled', True),
            )
        ),
    )

    # Parse systems config (toggleable subsystems)
    adapter_data = config_data.get('adapter', {})
    if isinstance(adapter_data, str):
        adapter_data = {"type": adapter_data}
    adapter = AdapterConfig(
        type=str(adapter_data.get('type', 'standalone')).strip().lower(),
    )
    plugins_data = config_data.get('plugins', {})
    raw_paths = plugins_data.get('paths', [])
    plugin_paths: List[str] = []
    if isinstance(raw_paths, list):
        plugin_paths = [str(p).strip() for p in raw_paths if str(p).strip()]
    if not plugin_paths:
        plugin_paths = ["plugins"]
    raw_allowlist = plugins_data.get(
        'allowlist',
        plugins_data.get('allow_list', plugins_data.get('allowList', []))
    )
    allowlist: List[str] = []
    if isinstance(raw_allowlist, list):
        allowlist = [str(p).strip() for p in raw_allowlist if str(p).strip()]
    slots_data = plugins_data.get('slots', {})
    raw_ingest = slots_data.get('ingest', [])
    ingest_slots = [str(p).strip() for p in raw_ingest if str(p).strip()] if isinstance(raw_ingest, list) else []
    raw_datastores = slots_data.get(
        'datastores',
        slots_data.get('data_stores', slots_data.get('dataStores', []))
    )
    datastore_slots = [str(p).strip() for p in raw_datastores if str(p).strip()] if isinstance(raw_datastores, list) else []
    plugins = PluginsConfig(
        enabled=bool(plugins_data.get('enabled', True)),
        strict=bool(plugins_data.get('strict', True)),
        api_version=max(1, int(plugins_data.get('api_version', plugins_data.get('apiVersion', 1)))),
        paths=plugin_paths,
        allowlist=allowlist,
        slots=PluginSlotsConfig(
            adapter=str(slots_data.get('adapter', '')).strip(),
            ingest=ingest_slots,
            datastores=datastore_slots,
        ),
        config=plugins_data.get('config', {}) if isinstance(plugins_data.get('config', {}), dict) else {},
    )
    if retrieval.fail_hard != plugins.strict and not os.environ.get("QUAID_QUIET"):
        print(
            "[config] WARNING: retrieval.fail_hard and plugins.strict differ; "
            "retrieval controls memory/LLM fallback, plugins.strict controls plugin contract enforcement.",
            file=sys.stderr,
        )

    systems_data = config_data.get('systems', {})
    systems = SystemsConfig(
        memory=systems_data.get('memory', True),
        journal=systems_data.get('journal', True),
        projects=systems_data.get('projects', True),
        workspace=systems_data.get('workspace', True),
    )
    raw_prompt_set = config_data.get("prompt_set", "default")
    if raw_prompt_set is None:
        prompt_set = "default"
    else:
        prompt_set = str(raw_prompt_set).strip() or "default"

    candidate = MemoryConfig(
        adapter=adapter,
        plugins=plugins,
        core=core_cfg,
        systems=systems,
        models=models,
        capture=capture,
        decay=decay,
        janitor=janitor,
        retrieval=retrieval,
        logging=logging_cfg,
        docs=docs,
        projects=projects,
        users=users,
        identity=identity,
        privacy=privacy,
        database=database,
        ollama=ollama,
        rag=rag,
        notifications=notifications,
        prompt_set=prompt_set,
    )
    _run_config_callbacks(candidate)

    # Fail fast on unknown prompt sets to keep prompt-family swaps explicit and safe.
    from prompt_sets import set_active_prompt_set

    set_active_prompt_set(candidate.prompt_set)

    if plugins.enabled:
        from core.runtime.plugins import initialize_plugin_runtime, run_plugin_contract_surface
        from core.runtime.events import validate_declared_event_contract

        def _emit_plugin_messages() -> None:
            for msg in plugin_errors:
                print(f"[plugins][error] {msg}", file=sys.stderr)
            for msg in plugin_warnings:
                print(f"[plugins][warn] {msg}", file=sys.stderr)

        active_slots = {
            "adapter": plugins.slots.adapter,
            "ingest": list(plugins.slots.ingest),
            "datastores": list(plugins.slots.datastores),
        }
        registry, plugin_errors, plugin_warnings = initialize_plugin_runtime(
            api_version=plugins.api_version,
            paths=plugins.paths,
            allowlist=plugins.allowlist,
            strict=plugins.strict,
            slots=active_slots,
            workspace_root=str(_workspace_root()),
        )
        try:
            event_errors = validate_declared_event_contract(
                registry=registry,
                slots=active_slots,
                strict=plugins.strict,
            )
            plugin_warnings.extend(event_errors)
        except Exception as exc:
            msg = f"Event contract validation failed: {exc}"
            if plugins.strict:
                raise
            plugin_warnings.append(msg)
        init_errors, init_warnings = run_plugin_contract_surface(
            registry=registry,
            slots=active_slots,
            surface="init",
            config=candidate,
            plugin_config=plugins.config,
            workspace_root=str(_workspace_root()),
            strict=plugins.strict,
        )
        plugin_errors.extend(init_errors)
        plugin_warnings.extend(init_warnings)
        if plugins.strict and plugin_errors:
            _emit_plugin_messages()
            raise ValueError("Plugin contract init failures: " + "; ".join(plugin_errors))
        failed_init_plugin_ids: set[str] = set()
        for msg in list(init_errors) + list(init_warnings):
            m = re.search(r"Plugin '([^']+)' init hook failed", str(msg))
            if m:
                failed_init_plugin_ids.add(m.group(1).strip())
        cfg_errors, cfg_warnings = run_plugin_contract_surface(
            registry=registry,
            slots=active_slots,
            surface="config",
            config=candidate,
            plugin_config=plugins.config,
            workspace_root=str(_workspace_root()),
            strict=plugins.strict,
            skip_plugin_ids=sorted(failed_init_plugin_ids),
        )
        plugin_errors.extend(cfg_errors)
        plugin_warnings.extend(cfg_warnings)
        if plugins.strict and plugin_errors:
            _emit_plugin_messages()
            raise ValueError("Plugin contract config failures: " + "; ".join(plugin_errors))
        tool_runtime_errors, tool_runtime_warnings = run_plugin_contract_surface(
            registry=registry,
            slots=active_slots,
            surface="tool_runtime",
            config=candidate,
            plugin_config=plugins.config,
            workspace_root=str(_workspace_root()),
            strict=plugins.strict,
            skip_plugin_ids=sorted(failed_init_plugin_ids),
        )
        plugin_errors.extend(tool_runtime_errors)
        plugin_warnings.extend(tool_runtime_warnings)
        _emit_plugin_messages()
        if plugins.strict and plugin_errors:
            raise ValueError("Plugin contract tool_runtime failures: " + "; ".join(plugin_errors))

    _config = candidate
    return _config


def get_config() -> MemoryConfig:
    """Get the loaded config (loads on first call)."""
    return load_config()


def reload_config() -> MemoryConfig:
    """Force reload configuration from file."""
    global _config, _config_loading
    from core.runtime.plugins import reset_plugin_runtime
    from prompt_sets import reset_registry

    with _config_lock:
        _config = None
        # Allow nested reloads (for example from config callbacks) to rebuild
        # config instead of short-circuiting to a bare MemoryConfig().
        _config_loading = False
        _warned_unknown_config_keys.clear()
        reset_plugin_runtime()
        reset_registry()
        return load_config()


if __name__ == "__main__":
    # Test loading
    config = load_config()
    print(f"\nCapture strictness: {config.capture.strictness}")
    print(f"Decay threshold days: {config.decay.threshold_days}")
    print(f"Dedup similarity threshold: {config.janitor.dedup.similarity_threshold}")
    print(f"Retrieval default limit: {config.retrieval.default_limit}")
