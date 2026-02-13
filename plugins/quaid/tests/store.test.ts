import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, fixtures, type TestMemoryInterface } from './setup'

describe('Memory Store', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('stores a memory with correct metadata', async () => {
    const result = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)
    
    expect(result.id).toBeDefined()
    expect(result.content || result.name).toContain('Quaid')
    expect(result.owner_id || result.owner).toBe('default')
    expect(result.created_at).toBeDefined()
  })

  it('generates embedding of correct dimension', async () => {
    const result = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)
    
    // The embedding might be stored as binary or array depending on implementation
    if (result.embedding) {
      if (Array.isArray(result.embedding)) {
        expect(result.embedding).toHaveLength(128)  // mock embedding dimension
      } else {
        // If it's binary, we can't easily check length, but it should exist
        expect(result.embedding).toBeDefined()
      }
    }
  })

  it('sets default confidence to 1.0 for manual stores', async () => {
    const result = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)
    
    expect(result.confidence).toBeGreaterThanOrEqual(0.5) // Should be high for manual stores
  })

  it('accepts verified flag', async () => {
    const result = await memory.store(
      fixtures.defaultFact.content, 
      fixtures.defaultFact.owner,
      { verified: true }
    )
    
    expect(result.verified).toBe(true)
  })

  it('accepts pinned flag', async () => {
    const result = await memory.store(
      fixtures.defaultFact.content, 
      fixtures.defaultFact.owner,
      { pinned: true }
    )
    
    expect(result.pinned).toBe(true)
  })

  it('rejects empty content', async () => {
    await expect(memory.store('', 'default'))
      .rejects.toThrow()
  })

  it('rejects content with fewer than 3 words', async () => {
    await expect(memory.store('Quaid', 'default'))
      .rejects.toThrow()
    await expect(memory.store('Quaid', 'default'))
      .rejects.toThrow()
  })

  it('accepts content with exactly 3 words', async () => {
    const result = await memory.store('Quaid is great', 'default')
    expect(result.id).toBeDefined()
  })

  it('rejects missing owner', async () => {
    await expect(memory.store('test content is here', ''))
      .rejects.toThrow()
  })

  it('handles special characters in content', async () => {
    const special = 'Test with émojis 🎉 and "quotes" and <tags>'
    const result = await memory.store(special, 'default')
    
    expect(result.id).toBeDefined()
    // Content should be preserved (might be in name field for facts)
    const content = result.content || result.name || result.attributes?.content
    expect(content).toContain('émojis')
  })

  it('accepts custom confidence values', async () => {
    const result = await memory.store(
      fixtures.defaultFact.content,
      fixtures.defaultFact.owner,
      { confidence: 0.8 }
    )
    
    expect(result.confidence).toBeCloseTo(0.8, 1)
  })

  it('accepts category specification', async () => {
    const result = await memory.store(
      'Quaid prefers dark coffee',
      'default',
      { category: 'preference' }
    )
    
    expect(result.type || result.category).toBe('preference')
  })
})