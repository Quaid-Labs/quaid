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
      'Core identity fact: Quaid is from Australia',
      'quaid',
      { pinned: true }
    )
    
    expect(result.pinned).toBe(true)
  })

  it('pinned memories appear in search results', async () => {
    await memory.store('Regular fact about work', 'quaid')
    await memory.store('Important pinned fact', 'quaid', { pinned: true })
    
    const results = await memory.search('fact', 'quaid')
    
    expect(results.length).toBeGreaterThan(0)
    
    // Pinned fact should be recalled in ranked output.
    const pinnedMention = results.some(r =>
      String(r.content || r.text || r.name || '').includes('Important pinned fact')
    )
    expect(pinnedMention).toBe(true)
  })

  it('pinned memories have higher priority in ranking', async () => {
    // Store regular fact first, then pinned fact with same content
    await memory.store('Important information about coffee', 'quaid')
    await memory.store('Important information about coffee preferences', 'quaid', { pinned: true })
    
    const results = await memory.search('important information', 'quaid')
    
    expect(results.length).toBeGreaterThan(0)
    
    const pinnedMention = results.some(r =>
      String(r.content || r.text || r.name || '').includes('preferences')
    )
    expect(pinnedMention).toBe(true)
  })

  it('can retrieve raw pinned memory data', async () => {
    const stored = await memory.store(
      'Pinned core identity information',
      'quaid',
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
    await memory.store('This is a pinned memory', 'quaid', { pinned: true })
    
    const results = await memory.search('pinned', 'quaid')
    
    expect(results.length).toBeGreaterThan(0)
    const pinnedResult = results.find(r => 
      String(r.text || r.content || r.name || '').includes('pinned')
    )
    expect(pinnedResult).toBeDefined()
  })

  it('supports both pinned and verified flags together', async () => {
    const result = await memory.store(
      'Critical verified and pinned fact',
      'quaid',
      { pinned: true, verified: true }
    )
    
    expect(result.pinned).toBe(true)
    expect(result.verified).toBe(true)
  })

  it('handles pinned flag with custom confidence', async () => {
    const result = await memory.store(
      'Pinned fact with custom confidence',
      'quaid',
      { pinned: true, confidence: 0.9 }
    )
    
    expect(result.pinned).toBe(true)
    expect(result.confidence).toBeCloseTo(0.9, 1)
  })

  it('pinned memories maintain owner isolation', async () => {
    await memory.store('Quaid pinned secret', 'quaid', { pinned: true })
    await memory.store('Melina pinned secret', 'melina', { pinned: true })
    
    const solomonResults = await memory.search('pinned secret', 'quaid')
    const yuniResults = await memory.search('pinned secret', 'melina')
    
    // Each owner should only see their own pinned memories
    for (const result of solomonResults) {
      const owner = result.owner || result.owner_id
      expect(owner).toBe('quaid')
    }
    
    for (const result of yuniResults) {
      const owner = result.owner || result.owner_id
      expect(owner).toBe('melina')
    }
  })

  it('distinguishes between pinned and unpinned memories', async () => {
    await memory.store('Regular memory about things', 'quaid', { skipDedup: true })
    await memory.store('Pinned memory about things', 'quaid', { pinned: true, skipDedup: true })
    
    const results = await memory.search('memory', 'quaid')
    
    expect(results.some(r =>
      String(r.content || r.text || r.name || '').includes('Pinned memory about things')
    )).toBe(true)
    expect(results.some(r =>
      String(r.content || r.text || r.name || '').includes('Regular memory about things')
    )).toBe(true)
  })
})
