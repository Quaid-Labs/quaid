# Decision 003: Memory Tier System

**Date:** 2026-02-01  
**Status:** Accepted  
**Deciders:** Quaid, Alfie

## Context

Not all memories are equal. Some are core identity facts that should never disappear. Others are transient conversation context that should fade over time. We needed a way to differentiate.

## Decision

**Implement a four-tier memory hierarchy with different decay behaviors.**

## The Tiers

### 1. Pinned Memories (`pinned = 1`)
- **Never decay** — Confidence stays at 1.0 forever
- **Always available** — Prioritized in semantic recall
- **Use for:** Core identity facts, critical system knowledge, explicit "remember this forever"
- **Set by:** Manual `memory_store` with pinned flag

### 2. Verified Memories (`verified = 1`)
- **Slow decay** — 50% of normal decay rate
- **Higher confidence ranking** — Boosted in search results
- **Use for:** Important but not eternal facts, reviewed/confirmed information
- **Set by:** Manual verification or Opus review approval

### 3. Auto-captured Memories (default)
- **Normal decay** — Confidence reduces after 30 days unused
- **Background context** — Surfaces when semantically relevant
- **Use for:** Conversation extractions, casual facts
- **Set by:** Automatic extraction via qwen2.5:7b classifier

### 4. Archived Memories (`status = 'archived'`)
- **Fully decayed** — Confidence dropped below minimum threshold
- **Not returned in searches** — But preserved for potential recovery
- **Use for:** Soft graveyard before permanent deletion

## Decay Formula

```python
if not accessed in 30+ days:
    if pinned:
        pass  # No decay
    elif verified:
        confidence *= 0.95  # 5% decay (half rate)
    else:
        confidence *= 0.90  # 10% decay (normal rate)
```

## Trade-offs Accepted

- More complex than single-tier system
- Requires judgment calls on what to pin
- Decay thresholds are somewhat arbitrary (tunable via config)

## Consequences

- Core facts persist indefinitely
- Noise naturally fades away
- Memory database stays high-signal over time
- Janitor can be aggressive with unverified memories

## See Also

- `modules/quaid/core/lifecycle/janitor.py`
- `config/memory.json` § decay settings
