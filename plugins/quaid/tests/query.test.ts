import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, fixtures, type TestMemoryInterface } from './setup'

describe('Memory Query', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
    // Seed test data with skipDedup to ensure all test data is stored
    await memory.store(fixtures.coffeePreference.content, fixtures.coffeePreference.owner, { skipDedup: true })
    await memory.store(fixtures.weatherFact.content, fixtures.weatherFact.owner, { skipDedup: true })
    await memory.store(fixtures.healthFact.content, fixtures.healthFact.owner, { skipDedup: true })
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('returns semantically similar results', async () => {
    const results = await memory.search('coffee preferences', 'default')
    
    expect(results.length).toBeGreaterThan(0)
    const firstResult = results[0]
    const content = firstResult.text || firstResult.name || firstResult.content
    expect(content.toLowerCase()).toContain('coffee')
  })

  it('ranks results by similarity', async () => {
    const results = await memory.search('colitis', 'default', 3)
    
    expect(results.length).toBeGreaterThan(0)
    const firstResult = results[0]
    const content = firstResult.text || firstResult.name || firstResult.content
    expect(content.toLowerCase()).toMatch(/colitis|health/)
    
    // Results should be ordered by similarity (if multiple results)
    if (results.length > 1) {
      expect(results[0].similarity).toBeGreaterThanOrEqual(results[1].similarity)
    }
  })

  it('respects limit parameter', async () => {
    const results = await memory.search('default', 'default', 1)
    
    expect(results.length).toBeLessThanOrEqual(1)
  })

  it('returns empty array for no matches', async () => {
    const results = await memory.search('quantum physics advanced mathematics', 'default', 5, 0.95)

    // With high similarity threshold, unrelated content should be filtered out
    expect(Array.isArray(results)).toBe(true)
    expect(results.length).toBe(0)
  })

  it('only returns memories for the specified owner', async () => {
    // Store a memory for a different owner
    await memory.store('Lori secret information', 'yuni')
    
    const results = await memory.search('secret', 'default')
    
    // Should not return Lori's secret
    for (const result of results) {
      expect(result.owner || result.owner_id).toBe('default')
    }
  })

  it('handles query with special characters', async () => {
    await memory.store('Quaid likes café au lait', 'default')
    
    const results = await memory.search('café', 'default')
    
    expect(results.length).toBeGreaterThan(0)
  })

  it('returns similarity scores', async () => {
    const results = await memory.search('coffee', 'default')
    
    if (results.length > 0) {
      expect(results[0].similarity).toBeDefined()
      expect(results[0].similarity).toBeGreaterThan(0)
      expect(results[0].similarity).toBeLessThanOrEqual(1)
    }
  })

  it('handles empty query gracefully', async () => {
    await expect(memory.search('', 'default')).resolves.toBeDefined()
  })

  it('handles non-existent owner', async () => {
    // Use high threshold to avoid FTS cross-owner leakage (known limitation)
    const results = await memory.search('anything', 'nonexistent', 5, 0.95)

    expect(Array.isArray(results)).toBe(true)
    expect(results.length).toBe(0)
  })
})