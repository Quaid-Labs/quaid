import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, type TestMemoryInterface } from './setup'

describe('Decay Review Queue', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('decay_review_queue table exists and is queryable', async () => {
    const result = await memory.querySql(
      "SELECT COUNT(*) as cnt FROM decay_review_queue;"
    )
    const rows = JSON.parse(result)
    expect(rows[0].cnt).toBe(0)
  })

  it('dedup_log table exists and is queryable', async () => {
    const result = await memory.querySql(
      "SELECT COUNT(*) as cnt FROM dedup_log;"
    )
    const rows = JSON.parse(result)
    expect(rows[0].cnt).toBe(0)
  })

  it('queue entries have correct schema columns', async () => {
    const result = await memory.querySql(
      "PRAGMA table_info(decay_review_queue);"
    )
    const columns = JSON.parse(result)
    const colNames = columns.map((c: any) => c.name)

    expect(colNames).toContain('id')
    expect(colNames).toContain('node_id')
    expect(colNames).toContain('node_text')
    expect(colNames).toContain('node_type')
    expect(colNames).toContain('confidence_at_queue')
    expect(colNames).toContain('access_count')
    expect(colNames).toContain('last_accessed')
    expect(colNames).toContain('verified')
    expect(colNames).toContain('created_at_node')
    expect(colNames).toContain('decision')
    expect(colNames).toContain('decision_reason')
    expect(colNames).toContain('status')
    expect(colNames).toContain('queued_at')
    expect(colNames).toContain('reviewed_at')
  })

  it('dedup_log entries have correct schema columns', async () => {
    const result = await memory.querySql(
      "PRAGMA table_info(dedup_log);"
    )
    const columns = JSON.parse(result)
    const colNames = columns.map((c: any) => c.name)

    expect(colNames).toContain('id')
    expect(colNames).toContain('new_text')
    expect(colNames).toContain('existing_node_id')
    expect(colNames).toContain('existing_text')
    expect(colNames).toContain('similarity')
    expect(colNames).toContain('decision')
    expect(colNames).toContain('llm_reasoning')
    expect(colNames).toContain('review_status')
    expect(colNames).toContain('review_resolution')
    expect(colNames).toContain('reviewed_at')
    expect(colNames).toContain('owner_id')
    expect(colNames).toContain('source')
  })

  it('pinned memories are never queued for decay', async () => {
    const pinned = await memory.store('Core identity fact for pinned decay guard', 'quaid', {
      pinned: true,
      confidence: 0.15,
      skipDedup: true,
    })
    const unpinned = await memory.store('Low-confidence aging memory expected in decay queue', 'quaid', {
      confidence: 0.15,
      skipDedup: true,
    })
    await memory.querySql(
      `UPDATE nodes
          SET accessed_at='2000-01-01T00:00:00Z', created_at='2000-01-01T00:00:00Z'
        WHERE id IN ('${pinned.id.replace(/'/g, "''")}', '${unpinned.id.replace(/'/g, "''")}');`
    )

    // Run decay
    await memory.runDecay()

    // Pinned memories must never be queued for review.
    const result = await memory.querySql(
      `SELECT node_id
         FROM decay_review_queue
        WHERE node_id IN ('${pinned.id.replace(/'/g, "''")}', '${unpinned.id.replace(/'/g, "''")}');`
    )
    const rows = result.trim() ? JSON.parse(result) : []
    const queuedIds = new Set(rows.map((r: any) => String(r.node_id)))
    expect(queuedIds.has(pinned.id)).toBe(false)
  })

  it('decay runs without errors when review queue is enabled', async () => {
    await memory.store('Test memory for decay queue', 'quaid')

    // Run decay multiple times - should not error
    await expect(memory.runDecay()).resolves.toBeUndefined()
    await expect(memory.runDecay()).resolves.toBeUndefined()
  })

  it('memories remain searchable after decay with queue enabled', async () => {
    const stored = await memory.store('Searchable after decay test', 'quaid')

    await memory.runDecay()

    const results = await memory.search('searchable decay', 'quaid')
    expect(results.length).toBeGreaterThan(0)
    const found = results.some(r =>
      String(r.content || r.text || r.name || '').includes('Searchable after decay test')
    )
    expect(found).toBe(true)
  })
})
