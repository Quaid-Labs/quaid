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
    expect(colNames).toContain('haiku_reasoning')
    expect(colNames).toContain('review_status')
    expect(colNames).toContain('review_resolution')
    expect(colNames).toContain('reviewed_at')
    expect(colNames).toContain('owner_id')
    expect(colNames).toContain('source')
  })

  it('pinned memories are never queued for decay', async () => {
    // Store a pinned memory
    await memory.store('Core identity fact', 'default', { pinned: true })

    // Run decay
    await memory.runDecay()

    // Queue should be empty - pinned memories skip decay entirely
    const result = await memory.querySql(
      "SELECT COUNT(*) as cnt FROM decay_review_queue;"
    )
    const rows = JSON.parse(result)
    expect(rows[0].cnt).toBe(0)
  })

  it('decay runs without errors when review queue is enabled', async () => {
    await memory.store('Test memory for decay queue', 'default')

    // Run decay multiple times - should not error
    await expect(memory.runDecay()).resolves.toBeUndefined()
    await expect(memory.runDecay()).resolves.toBeUndefined()
  })

  it('memories remain searchable after decay with queue enabled', async () => {
    const stored = await memory.store('Searchable after decay test', 'default')

    await memory.runDecay()

    const results = await memory.search('searchable decay', 'default')
    expect(results.length).toBeGreaterThan(0)
    const found = results.some(r => r.id === stored.id)
    expect(found).toBe(true)
  })
})
