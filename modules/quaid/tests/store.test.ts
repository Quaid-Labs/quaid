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
    const result = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    
    expect(result.id).toBeDefined()
    expect(result.content || result.name).toContain('Quaid')
    expect(result.owner_id || result.owner).toBe('quaid')
    expect(result.created_at).toBeDefined()
  })

  it('generates embedding of correct dimension', async () => {
    const result = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)

    expect(result.embedding).toBeDefined()
    if (Array.isArray(result.embedding)) {
      expect(result.embedding).toHaveLength(128) // mock embedding dimension
    } else {
      expect(String(result.embedding).length).toBeGreaterThan(0)
    }
  })

  it('sets default confidence to 1.0 for manual stores', async () => {
    const result = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    
    expect(result.confidence).toBe(1.0)
  })

  it('accepts verified flag', async () => {
    const result = await memory.store(
      fixtures.solomonFact.content, 
      fixtures.solomonFact.owner,
      { verified: true }
    )
    
    expect(result.verified).toBe(true)
  })

  it('accepts pinned flag', async () => {
    const result = await memory.store(
      fixtures.solomonFact.content, 
      fixtures.solomonFact.owner,
      { pinned: true }
    )
    
    expect(result.pinned).toBe(true)
  })

  it('rejects empty content', async () => {
    await expect(memory.store('', 'quaid'))
      .rejects.toThrow()
  })

  it('rejects content with fewer than 3 words', async () => {
    await expect(memory.store('Quaid', 'quaid'))
      .rejects.toThrow()
    await expect(memory.store('Douglas Quaid', 'quaid'))
      .rejects.toThrow()
  })

  it('accepts content with exactly 3 words', async () => {
    const result = await memory.store('Quaid is great', 'quaid')
    expect(result.id).toBeDefined()
  })

  it('defaults owner when owner is missing', async () => {
    const result = await memory.store('test content is here', '')
    expect(result.id).toBeDefined()
    // CLI store path resolves blank owner to configured default owner.
    expect(result.owner_id || result.owner).toBeDefined()
  })

  it('handles special characters in content', async () => {
    const special = 'Test with émojis 🎉 and "quotes" and <tags>'
    const result = await memory.store(special, 'quaid')
    
    expect(result.id).toBeDefined()
    // Content should be preserved (might be in name field for facts)
    const content = result.content || result.name || result.attributes?.content
    expect(content).toContain('émojis')
  })

  it('accepts custom confidence values', async () => {
    const result = await memory.store(
      fixtures.solomonFact.content,
      fixtures.solomonFact.owner,
      { confidence: 0.8 }
    )
    
    expect(result.confidence).toBeCloseTo(0.8, 1)
  })

  it('accepts category specification', async () => {
    const result = await memory.store(
      'Quaid prefers dark coffee',
      'quaid',
      { category: 'preference' }
    )
    
    expect(result.type || result.category).toBe('preference')
  })
})
