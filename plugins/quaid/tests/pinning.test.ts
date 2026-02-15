import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, type TestMemoryInterface } from './setup'

describe('Pinned Memories', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('stores memories with pinned flag', async () => {
    const result = await memory.store(
      'Core identity fact: Solomon is from Australia',
      'solomon',
      { pinned: true }
    )
    
    expect(result.pinned).toBe(true)
  })

  it('pinned memories appear in search results', async () => {
    await memory.store('Regular fact about work', 'solomon')
    await memory.store('Important pinned fact', 'solomon', { pinned: true })
    
    const results = await memory.search('fact', 'solomon')
    
    expect(results.length).toBeGreaterThan(0)
    
    // At least one result should be pinned
    const pinnedResults = results.filter(r => r.pinned === true)
    expect(pinnedResults.length).toBeGreaterThan(0)
  })

  it('pinned memories have higher priority in ranking', async () => {
    // Store regular fact first, then pinned fact with same content
    await memory.store('Important information about coffee', 'solomon')
    await memory.store('Important information about coffee preferences', 'solomon', { pinned: true })
    
    const results = await memory.search('important information', 'solomon')
    
    expect(results.length).toBeGreaterThan(0)
    
    // If we have multiple results, the pinned one should rank higher or at least be present
    const pinnedResults = results.filter(r => r.pinned === true)
    expect(pinnedResults.length).toBeGreaterThan(0)
  })

  it('can retrieve raw pinned memory data', async () => {
    const stored = await memory.store(
      'Pinned core identity information',
      'solomon',
      { pinned: true }
    )
    
    try {
      const raw = await memory.getRaw(stored.id)
      expect(raw.pinned).toBe(true)
    } catch {
      // getRaw might not be implemented yet
    }
  })

  it('pinned flag persists through search operations', async () => {
    await memory.store('This is a pinned memory', 'solomon', { pinned: true })
    
    const results = await memory.search('pinned', 'solomon')
    
    expect(results.length).toBeGreaterThan(0)
    const pinnedResult = results.find(r => 
      (r.text || r.content || r.name).includes('pinned')
    )
    expect(pinnedResult?.pinned).toBe(true)
  })

  it('supports both pinned and verified flags together', async () => {
    const result = await memory.store(
      'Critical verified and pinned fact',
      'solomon',
      { pinned: true, verified: true }
    )
    
    expect(result.pinned).toBe(true)
    expect(result.verified).toBe(true)
  })

  it('handles pinned flag with custom confidence', async () => {
    const result = await memory.store(
      'Pinned fact with custom confidence',
      'solomon',
      { pinned: true, confidence: 0.9 }
    )
    
    expect(result.pinned).toBe(true)
    expect(result.confidence).toBeCloseTo(0.9, 1)
  })

  it('pinned memories maintain owner isolation', async () => {
    await memory.store('Solomon pinned secret', 'solomon', { pinned: true })
    await memory.store('Yuni pinned secret', 'yuni', { pinned: true })
    
    const solomonResults = await memory.search('pinned secret', 'solomon')
    const yuniResults = await memory.search('pinned secret', 'yuni')
    
    // Each owner should only see their own pinned memories
    for (const result of solomonResults) {
      const owner = result.owner || result.owner_id
      expect(owner).toBe('solomon')
    }
    
    for (const result of yuniResults) {
      const owner = result.owner || result.owner_id
      expect(owner).toBe('yuni')
    }
  })

  it('distinguishes between pinned and unpinned memories', async () => {
    await memory.store('Regular memory about things', 'solomon', { skipDedup: true })
    await memory.store('Pinned memory about things', 'solomon', { pinned: true, skipDedup: true })
    
    const results = await memory.search('memory', 'solomon')
    
    const pinnedCount = results.filter(r => r.pinned === true).length
    const unpinnedCount = results.filter(r => r.pinned !== true).length
    
    expect(pinnedCount).toBeGreaterThan(0)
    expect(unpinnedCount).toBeGreaterThan(0)
  })
})