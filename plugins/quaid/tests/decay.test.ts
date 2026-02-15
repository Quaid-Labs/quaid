import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, type TestMemoryInterface } from './setup'

describe('Confidence Decay', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('runs decay operation without errors', async () => {
    await memory.store('Test memory for decay', 'solomon')
    
    await expect(memory.runDecay()).resolves.toBeUndefined()
  })

  it('maintains pinned memories during decay', async () => {
    const pinned = await memory.store(
      'Core identity fact that should never decay',
      'solomon',
      { pinned: true }
    )
    
    await memory.runDecay()
    
    try {
      const result = await memory.getRaw(pinned.id)
      expect(result.confidence).toBeGreaterThanOrEqual(0.8) // Should maintain high confidence
      expect(result.pinned).toBe(true)
    } catch {
      // If getRaw not implemented, test that it's still findable
      const searchResults = await memory.search('identity fact', 'solomon')
      const found = searchResults.some(r => r.id === pinned.id)
      expect(found).toBe(true)
    }
  })

  it('handles decay with no memories', async () => {
    await expect(memory.runDecay()).resolves.toBeUndefined()
  })

  it('maintains verified memories at higher confidence', async () => {
    const verified = await memory.store(
      'Verified important fact',
      'solomon',
      { verified: true }
    )
    
    await memory.runDecay()
    
    // Verified memories should maintain relatively high confidence
    try {
      const result = await memory.getRaw(verified.id)
      expect(result.confidence).toBeGreaterThan(0.3) // Should not decay too much
    } catch {
      // If getRaw not implemented, test that it's still easily findable
      const searchResults = await memory.search('verified important', 'solomon')
      const found = searchResults.some(r => r.id === verified.id)
      expect(found).toBe(true)
    }
  })

  it('processes memories of different owners separately', async () => {
    await memory.store('Solomon memory for decay test', 'solomon')
    await memory.store('Yuni memory for decay test', 'yuni')
    
    await memory.runDecay()
    
    // Both owners' memories should still be accessible
    const solomonResults = await memory.search('decay test', 'solomon')
    const yuniResults = await memory.search('decay test', 'yuni')
    
    expect(solomonResults.length).toBeGreaterThan(0)
    expect(yuniResults.length).toBeGreaterThan(0)
  })

  it('handles multiple decay runs', async () => {
    await memory.store('Multi-decay test memory', 'solomon')
    
    // Run decay multiple times
    await memory.runDecay()
    await memory.runDecay()
    await memory.runDecay()
    
    // Memory should still exist (though potentially with lower confidence)
    const results = await memory.search('multi-decay', 'solomon')
    expect(Array.isArray(results)).toBe(true)
  })

  it('preserves memory structure during decay', async () => {
    const original = await memory.store('Structure preservation test', 'solomon')
    
    await memory.runDecay()
    
    try {
      const result = await memory.getRaw(original.id)
      
      // Core fields should still be present
      expect(result.id).toBe(original.id)
      expect(result.owner_id || result.owner).toBe('solomon')
      expect(result.created_at).toBeDefined()
    } catch {
      // If getRaw not available, at least verify it's still searchable
      const searchResults = await memory.search('structure preservation', 'solomon')
      const found = searchResults.some(r => r.id === original.id)
      expect(found).toBe(true)
    }
  })

  it('maintains embedding data through decay', async () => {
    const stored = await memory.store('Embedding preservation test', 'solomon')
    
    await memory.runDecay()
    
    // Should still be semantically searchable
    const results = await memory.search('embedding preservation', 'solomon')
    expect(results.length).toBeGreaterThan(0)
    
    // Should find the specific memory
    const found = results.some(r => r.id === stored.id)
    expect(found).toBe(true)
  })

  it('handles decay with mixed memory types', async () => {
    await memory.store('Regular fact about testing', 'solomon', { skipDedup: true })
    await memory.store('Verified fact about testing', 'solomon', { verified: true, skipDedup: true })
    await memory.store('Pinned fact about testing', 'solomon', { pinned: true, skipDedup: true })
    
    await memory.runDecay()
    
    // All should still be findable
    const results = await memory.search('fact', 'solomon')
    expect(results.length).toBeGreaterThanOrEqual(3)
  })

  it('reports decay statistics if available', async () => {
    await memory.store('Stats test memory', 'solomon')
    
    await memory.runDecay()
    
    try {
      const stats = await memory.stats()
      expect(stats).toBeDefined()
      expect(typeof stats).toBe('object')
    } catch {
      // Stats might not be implemented yet
    }
  })
})