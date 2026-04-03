# Config Reference

Auto-generated from `modules/quaid/config.py` dataclasses and defaults.
Do not edit manually. Regenerate with:

```bash
python3 modules/quaid/scripts/generate-config-reference.py
```

Source hash: `b784ed00ee87`

Notes:
- Keys are documented in `snake_case` (loader also accepts camelCase aliases).
- `default` is the runtime dataclass default; nested dataclass containers are flattened.
- Inline `notes` come from trailing `# ...` comments in `config.py` field definitions.

## `adapter`

| Key | Type | Default | Notes |
|---|---|---|---|
| `adapter.type` | `str` | `"standalone"` | standalone \| openclaw \| claude-code \| codex |

## `capture`

| Key | Type | Default | Notes |
|---|---|---|---|
| `capture.enabled` | `bool` | `true` |  |
| `capture.strictness` | `str` | `"high"` | high \| medium \| low |
| `capture.skip_patterns` | `list[str]` | `[]` |  |
| `capture.inactivity_timeout_minutes` | `int` | `60` | Extract after N minutes of inactivity (daemon clamps to 120m max for system health; 0 disables user-requested timeout compaction only) |
| `capture.compact_on_timeout` | `bool` | `true` | After timeout extraction, request compaction on adapters that support it |
| `capture.chunk_tokens` | `int` | `8000` | Max tokens per extraction chunk (messages never split) |
| `capture.chunk_max_lines` | `int` | `0` | Optional line cap for rolling extraction windows |
| `capture.chunk_size` | `int` | `8000` | Deprecated legacy field; mirror token cap for old configs |

## `core`

| Key | Type | Default | Notes |
|---|---|---|---|
| `core.parallel.enabled` | `bool` | `true` |  |
| `core.parallel.llm_workers` | `int` | `4` |  |
| `core.parallel.embedding_workers` | `int` | `6` |  |
| `core.parallel.task_workers` | `dict[str, int]` | `{}` |  |
| `core.parallel.lifecycle_prepass_workers` | `int` | `3` |  |
| `core.parallel.lifecycle_prepass_timeout_seconds` | `int` | `1200` |  |
| `core.parallel.lifecycle_prepass_timeout_retries` | `int` | `1` |  |
| `core.parallel.lock_enforcement_enabled` | `bool` | `true` |  |
| `core.parallel.lock_wait_seconds` | `int` | `120` |  |
| `core.parallel.lock_require_registration` | `bool` | `true` |  |

## `database`

| Key | Type | Default | Notes |
|---|---|---|---|
| `database.path` | `str` | `"data/memory.db"` |  |
| `database.archive_path` | `str` | `"data/memory_archive.db"` |  |
| `database.wal_mode` | `bool` | `true` |  |

## `decay`

| Key | Type | Default | Notes |
|---|---|---|---|
| `decay.enabled` | `bool` | `true` |  |
| `decay.threshold_days` | `int` | `30` |  |
| `decay.rate_percent` | `float` | `10.0` |  |
| `decay.minimum_confidence` | `float` | `0.1` |  |
| `decay.protect_verified` | `bool` | `true` |  |
| `decay.protect_pinned` | `bool` | `true` |  |
| `decay.review_queue_enabled` | `bool` | `true` |  |
| `decay.mode` | `str` | `"exponential"` | "linear" or "exponential" |
| `decay.base_half_life_days` | `float` | `60.0` | Half-life in days for standard facts |
| `decay.access_bonus_factor` | `float` | `0.15` | Each access extends half-life by this fraction |

## `docs`

| Key | Type | Default | Notes |
|---|---|---|---|
| `docs.auto_update_on_compact` | `bool` | `true` |  |
| `docs.max_docs_per_update` | `int` | `3` |  |
| `docs.staleness_check_enabled` | `bool` | `true` |  |
| `docs.update_timeout_seconds` | `int` | `480` | Timeout for Opus/Sonnet doc updates |
| `docs.notify_on_update` | `bool` | `true` | Notify user when docs are auto-updated |
| `docs.source_mapping` | `dict[str, SourceMapping]` | `{}` |  |
| `docs.doc_purposes` | `dict[str, str]` | `{}` |  |
| `docs.core_markdown.enabled` | `bool` | `true` |  |
| `docs.core_markdown.monitor_for_bloat` | `bool` | `true` |  |
| `docs.core_markdown.monitor_for_outdated` | `bool` | `true` |  |
| `docs.core_markdown.files` | `dict[str, dict[str, Any]]` | `{}` |  |
| `docs.journal.enabled` | `bool` | `true` |  |
| `docs.journal.snippets_enabled` | `bool` | `true` | Enable snippet extraction (fast path, nightly review) |
| `docs.journal.mode` | `str` | `"distilled"` | "distilled" or "full" |
| `docs.journal.inject_full` | `bool` | `false` | EXPERIMENTAL: inject full journal into context every turn (uncapped size — use with caution) |
| `docs.journal.journal_dir` | `str` | `"journal"` | relative to workspace |
| `docs.journal.target_files` | `list[str]` | `["SOUL.md", "USER.md", "ENVIRONMENT.md"]` |  |
| `docs.journal.max_entries_per_file` | `int` | `0` | 0 disables active journal capping (unlimited) |
| `docs.journal.max_tokens` | `int` | `8192` |  |
| `docs.journal.generated_markdown_line_limit` | `int` | `0` | 0 disables soft target |
| `docs.journal.distillation_interval_days` | `int` | `7` |  |
| `docs.journal.archive_after_distillation` | `bool` | `true` |  |

## `identity`

| Key | Type | Default | Notes |
|---|---|---|---|
| `identity.mode` | `str` | `"single_user"` | single_user \| multi_user |
| `identity.auto_link_threshold` | `float` | `0.95` |  |
| `identity.require_review_threshold` | `float` | `0.75` |  |

## `janitor`

| Key | Type | Default | Notes |
|---|---|---|---|
| `janitor.enabled` | `bool` | `true` |  |
| `janitor.dry_run` | `bool` | `false` |  |
| `janitor.apply_mode` | `str` | `"auto"` | master mode: auto \| ask \| dry_run |
| `janitor.token_budget` | `int` | `0` | Max total LLM tokens per janitor run (0 = unlimited) |
| `janitor.approval_policies` | `dict[str, str]` | `<dict keys=4>` |  |
| `janitor.task_timeout_minutes` | `int` | `240` |  |
| `janitor.scheduled_hour` | `int` | `4` | Hour of day (0-23) for scheduled janitor run |
| `janitor.window_hours` | `int` | `2` | Allowed window in hours after scheduled_hour |
| `janitor.run_tests` | `bool` | `false` | Only enable in dev (or set QUAID_DEV=1) |
| `janitor.opus_review.enabled` | `bool` | `true` |  |
| `janitor.opus_review.batch_size` | `int` | `50` |  |
| `janitor.opus_review.max_tokens` | `int` | `4000` |  |
| `janitor.opus_review.model` | `str` | `""` | Defaults to models.deep_reasoning at load time |
| `janitor.dedup.similarity_threshold` | `float` | `0.85` |  |
| `janitor.dedup.high_similarity_threshold` | `float` | `0.95` |  |
| `janitor.dedup.auto_reject_threshold` | `float` | `0.98` |  |
| `janitor.dedup.gray_zone_low` | `float` | `0.88` |  |
| `janitor.dedup.llm_verify_enabled` | `bool` | `true` |  |
| `janitor.contradiction.enabled` | `bool` | `false` |  |
| `janitor.contradiction.timeout_minutes` | `int` | `60` |  |
| `janitor.contradiction.min_similarity` | `float` | `0.6` |  |
| `janitor.contradiction.max_similarity` | `float` | `0.85` |  |

## `logging`

| Key | Type | Default | Notes |
|---|---|---|---|
| `logging.enabled` | `bool` | `true` |  |
| `logging.level` | `str` | `"info"` |  |
| `logging.retention_days` | `int` | `7` |  |
| `logging.components` | `list[str]` | `["memory", "janitor"]` |  |

## `models`

| Key | Type | Default | Notes |
|---|---|---|---|
| `models.llm_provider` | `str` | `"default"` | "default" (gateway active provider) or explicit provider ID |
| `models.fast_reasoning_provider` | `str` | `"default"` |  |
| `models.deep_reasoning_provider` | `str` | `"default"` |  |
| `models.embeddings_provider` | `str` | `"ollama"` | "ollama" (default) or adapter/provider-specific ID |
| `models.fast_reasoning` | `str` | `"default"` |  |
| `models.deep_reasoning` | `str` | `"default"` |  |
| `models.fast_reasoning_effort` | `str` | `"none"` |  |
| `models.deep_reasoning_effort` | `str` | `"high"` |  |
| `models.deep_reasoning_model_classes` | `dict[str, str]` | `{}` |  |
| `models.fast_reasoning_model_classes` | `dict[str, str]` | `{}` |  |
| `models.fast_reasoning_context` | `int` | `200000` |  |
| `models.deep_reasoning_context` | `int` | `200000` |  |
| `models.fast_reasoning_max_output` | `int` | `8192` |  |
| `models.deep_reasoning_max_output` | `int` | `16384` |  |
| `models.batch_budget_percent` | `float` | `0.5` |  |
| `models.api_key_env` | `str` | `"OPENAI_API_KEY"` |  |
| `models.base_url` | `str` | `""` |  |

## `notifications`

| Key | Type | Default | Notes |
|---|---|---|---|
| `notifications.level` | `str` | `"normal"` |  |
| `notifications.janitor.verbosity` | `str \| None` | `null` | "off", "summary", "full" — None inherits from master level |
| `notifications.janitor.channel` | `str` | `"last_used"` | "last_used" (follow session), or a specific channel name |
| `notifications.extraction.verbosity` | `str \| None` | `null` | "off", "summary", "full" — None inherits from master level |
| `notifications.extraction.channel` | `str` | `"last_used"` | "last_used" (follow session), or a specific channel name |
| `notifications.retrieval.verbosity` | `str \| None` | `null` | "off", "summary", "full" — None inherits from master level |
| `notifications.retrieval.channel` | `str` | `"last_used"` | "last_used" (follow session), or a specific channel name |
| `notifications.full_text` | `bool` | `false` | Show full text in notifications (no truncation) |
| `notifications.show_processing_start` | `bool` | `true` | Notify user when extraction starts |
| `notifications.project_create_enabled` | `bool` | `true` | Notify when a new project is registered |

## `ollama`

| Key | Type | Default | Notes |
|---|---|---|---|
| `ollama.url` | `str` | `"http://localhost:11434"` |  |
| `ollama.embedding_model` | `str` | `"nomic-embed-text"` |  |
| `ollama.embedding_dim` | `int` | `768` |  |

## `plugins`

| Key | Type | Default | Notes |
|---|---|---|---|
| `plugins.enabled` | `bool` | `true` |  |
| `plugins.strict` | `bool` | `true` | fail boot on invalid manifests/registration conflicts |
| `plugins.api_version` | `int` | `1` |  |
| `plugins.paths` | `list[str]` | `["plugins"]` |  |
| `plugins.allowlist` | `list[str]` | `[]` | empty => allow all discovered |
| `plugins.slots.adapter` | `str` | `""` | single active adapter plugin ID |
| `plugins.slots.ingest` | `list[str]` | `[]` | enabled ingest plugin IDs |
| `plugins.slots.datastores` | `list[str]` | `[]` | enabled datastore plugin IDs |
| `plugins.config` | `dict[str, Any]` | `{}` | plugin_id -> plugin-specific config payload |

## `privacy`

| Key | Type | Default | Notes |
|---|---|---|---|
| `privacy.default_scope_dm` | `str` | `"private_subject"` |  |
| `privacy.default_scope_group` | `str` | `"source_shared"` |  |
| `privacy.enforce_strict_filters` | `bool` | `true` |  |

## `projects`

| Key | Type | Default | Notes |
|---|---|---|---|
| `projects.enabled` | `bool` | `true` |  |
| `projects.projects_dir` | `str` | `"projects/"` |  |
| `projects.staging_dir` | `str` | `"projects/staging/"` |  |
| `projects.definitions` | `dict[str, ProjectDefinition]` | `{}` |  |
| `projects.default_project` | `str` | `"default"` |  |
| `projects.log_token_budget` | `int` | `0` | Token budget for project log rotation (0 = use DEFAULT_LOG_TOKEN_BUDGET) |

## `prompt_set`

| Key | Type | Default | Notes |
|---|---|---|---|
| `prompt_set` | `str` | `"default"` |  |

## `rag`

| Key | Type | Default | Notes |
|---|---|---|---|
| `rag.docs_dir` | `str` | `"docs"` |  |
| `rag.chunk_max_tokens` | `int` | `800` |  |
| `rag.chunk_overlap_tokens` | `int` | `100` |  |
| `rag.max_results` | `int` | `5` |  |
| `rag.search_limit` | `int` | `5` |  |
| `rag.min_similarity` | `float` | `0.3` |  |

## `retrieval`

| Key | Type | Default | Notes |
|---|---|---|---|
| `retrieval.default_limit` | `int` | `5` |  |
| `retrieval.max_limit` | `int` | `8` |  |
| `retrieval.min_similarity` | `float` | `0.8` |  |
| `retrieval.notify_min_similarity` | `float` | `0.85` |  |
| `retrieval.boost_recent` | `bool` | `true` |  |
| `retrieval.boost_frequent` | `bool` | `true` |  |
| `retrieval.max_tokens` | `int` | `2000` |  |
| `retrieval.reranker_enabled` | `bool` | `true` |  |
| `retrieval.reranker_top_k` | `int` | `20` |  |
| `retrieval.reranker_instruction` | `str` | `"Given a personal memory query, determine if this memory is relevant to the query"` |  |
| `retrieval.reranker_timeout_ms` | `int` | `15000` | Full-recall reranker wall timeout; preinject disables reranker entirely |
| `retrieval.rrf_k` | `int` | `60` | RRF fusion constant |
| `retrieval.reranker_blend` | `float` | `0.5` | Blend weight: reranker vs original score |
| `retrieval.composite_relevance_weight` | `float` | `0.6` | Weight for relevance in composite score |
| `retrieval.composite_recency_weight` | `float` | `0.2` | Weight for recency |
| `retrieval.composite_frequency_weight` | `float` | `0.15` | Weight for frequency |
| `retrieval.multi_pass_gate` | `float` | `0.7` | Quality gate for triggering second pass |
| `retrieval.mmr_lambda` | `float` | `0.7` | MMR diversity parameter |
| `retrieval.co_session_decay` | `float` | `0.6` | Score fraction for co-session facts |
| `retrieval.recency_decay_days` | `int` | `90` | Days over which recency decays to 0 |
| `retrieval.pre_injection_pass` | `bool` | `true` | Auto-inject: use total_recall planning pass |
| `retrieval.router_fail_open` | `bool` | `true` | If true, total_recall router failures use deterministic fallback recall instead of raising |
| `retrieval.fail_hard` | `bool` | `true` | If true, embedding outages raise instead of silent degraded fallback |
| `retrieval.auto_inject` | `bool` | `true` | Auto-inject memories into context (Mem0-style) |
| `retrieval.use_hyde` | `bool` | `true` | Enable HyDE query expansion by default |
| `retrieval.hyde_timeout_ms` | `int` | `15000` | Fast-tier timeout for HyDE query routing |
| `retrieval.hyde_max_retries` | `int` | `1` | Extra retries for HyDE route calls (fail-hard still enforced) |
| `retrieval.injection_timeout_ms` | `int` | `3000` | Overall wall-clock budget for pre-injection recall |
| `retrieval.injection_fanout_max` | `int` | `5` | Max parallel HyDE queries for injection |
| `retrieval.injection_fanout_llm_ms` | `int` | `1500` | LLM budget for query fanout within injection |
| `retrieval.domains` | `dict[str, str]` | `{}` | Domain id -> brief description |
| `retrieval.traversal.use_beam` | `bool` | `true` | Use BEAM search instead of BFS |
| `retrieval.traversal.beam_width` | `int` | `5` | Top-B candidates per hop level |
| `retrieval.traversal.max_depth` | `int` | `2` | Maximum traversal depth |
| `retrieval.traversal.scoring_mode` | `str` | `"heuristic"` |  |
| `retrieval.traversal.hop_decay` | `float` | `0.7` | Score decay per hop (0.7^depth) |
| `retrieval.tool_hint_timeout_ms` | `int` | `1500` | LLM budget for tool hint planner (reads TOOLS.md) |

## `systems`

| Key | Type | Default | Notes |
|---|---|---|---|
| `systems.memory` | `bool` | `true` | Extract and recall facts from conversations |
| `systems.journal` | `bool` | `true` | Track personality evolution via snippets + journal |
| `systems.projects` | `bool` | `true` | Auto-update project docs from code changes |
| `systems.workspace` | `bool` | `true` | Monitor core markdown file health |

## `users`

| Key | Type | Default | Notes |
|---|---|---|---|
| `users.default_owner` | `str` | `"default"` |  |
| `users.identities` | `dict[str, UserIdentity]` | `{}` |  |
