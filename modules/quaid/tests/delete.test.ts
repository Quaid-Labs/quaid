import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, fixtures, type TestMemoryInterface } from './setup'

describe('Memory Delete', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('hard deletes memory from database', async () => {
    const stored = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)

    await memory.delete(stored.id)

    // Should not appear in normal queries
    const results = await memory.search('engaged', 'quaid')
    const foundDeleted = results.some(r => r.id === stored.id)
    expect(foundDeleted).toBe(false)
  })

  it('completely removes memory on delete', async () => {
    const stored = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)

    await memory.delete(stored.id)

    // Row should be completely gone from DB.
    await expect(memory.getRaw(stored.id)).rejects.toThrow()
  })

  it('handles deletion of non-existent memory gracefully', async () => {
    const fakeId = 'non-existent-id-12345'
    
    await expect(memory.delete(fakeId)).resolves.toBeUndefined()
  })

  it('removes memory from search results after deletion', async () => {
    const stored = await memory.store(fixtures.coffeePreference.content, fixtures.coffeePreference.owner)
    
    // Verify it's findable before deletion
    const beforeResults = await memory.search('coffee', 'quaid')
    const foundBefore = beforeResults.some(r => r.id === stored.id)
    expect(foundBefore).toBe(true)
    
    // Delete it
    await memory.delete(stored.id)
    
    // Verify it's not findable after deletion
    const afterResults = await memory.search('coffee', 'quaid')
    const foundAfter = afterResults.some(r => r.id === stored.id)
    expect(foundAfter).toBe(false)
  })

  it('supports forget operation for permanent deletion', async () => {
    const stored = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    
    await memory.forget(stored.id)
    
    // Should not be findable
    const results = await memory.search('engaged', 'quaid')
    const found = results.some(r => r.id === stored.id)
    expect(found).toBe(false)
    
    // Raw access should fail for forgotten memories.
    await expect(memory.getRaw(stored.id)).rejects.toThrow()
  })

  it('handles empty reason gracefully', async () => {
    const stored = await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    
    await expect(memory.delete(stored.id, '')).resolves.toBeUndefined()
  })

  it('deletion API removes the target memory id', async () => {
    const solomonMemory = await memory.store(fixtures.solomonFact.content, 'quaid')

    // Current test helper API does not pass a caller owner, so delete is ID-scoped.
    await memory.delete(solomonMemory.id)

    await expect(memory.getRaw(solomonMemory.id)).rejects.toThrow()
  })
})
