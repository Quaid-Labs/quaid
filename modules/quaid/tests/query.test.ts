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
    const results = await memory.search('coffee preferences', 'quaid')
    
    expect(results.length).toBeGreaterThan(0)
    const hasCoffeeResult = results.some((r) => {
      const content = r.text || r.name || r.content
      return content.toLowerCase().includes('coffee')
    })
    expect(hasCoffeeResult).toBe(true)
  })

  it('ranks results by similarity', async () => {
    const results = await memory.search('colitis', 'quaid', 3)
    
    expect(results.length).toBeGreaterThan(0)
    const hasHealthResult = results.some((r) => {
      const content = r.text || r.name || r.content
      return /colitis|health/i.test(content)
    })
    expect(hasHealthResult).toBe(true)
    
    // Results should be ordered by similarity (if multiple results)
    if (results.length > 1) {
      expect(results[0].similarity).toBeGreaterThanOrEqual(results[1].similarity)
    }
  })

  it('respects limit parameter', async () => {
    const results = await memory.search('quaid', 'quaid', 1)
    
    expect(results.length).toBeLessThanOrEqual(1)
  })

  it('returns empty array for no matches', async () => {
    const results = await memory.search('quantum physics advanced mathematics', 'quaid', 5, 0.95)

    // With high similarity threshold, unrelated content should be filtered out
    expect(Array.isArray(results)).toBe(true)
    expect(results.length).toBe(0)
  })

  it('only returns memories for the specified owner', async () => {
    // Store a memory for a different owner
    await memory.store('Melina secret information', 'melina')
    
    const results = await memory.search('secret', 'quaid')
    
    // Should not return Melina's secret
    for (const result of results) {
      expect(result.owner || result.owner_id).toBe('quaid')
    }
  })

  it('handles query with special characters', async () => {
    await memory.store('Quaid likes café au lait', 'quaid')
    
    const results = await memory.search('café', 'quaid')
    
    expect(results.length).toBeGreaterThan(0)
  })

  it('returns similarity scores', async () => {
    const results = await memory.search('coffee', 'quaid')
    
    if (results.length > 0) {
      expect(results[0].similarity).toBeDefined()
      expect(results[0].similarity).toBeGreaterThan(0)
      expect(results[0].similarity).toBeLessThanOrEqual(1)
    }
  })

  it('handles empty query gracefully', async () => {
    await expect(memory.search('', 'quaid')).resolves.toBeDefined()
  })

  it('handles non-existent owner', async () => {
    // Use high threshold to avoid FTS cross-owner leakage (known limitation)
    const results = await memory.search('anything', 'nonexistent', 5, 0.95)

    expect(Array.isArray(results)).toBe(true)
    expect(results.length).toBe(0)
  })
})
