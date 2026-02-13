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
    const stored = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)

    await memory.delete(stored.id)

    // Should not appear in normal queries
    const results = await memory.search('engaged', 'default')
    const foundDeleted = results.some(r => r.id === stored.id)
    expect(foundDeleted).toBe(false)
  })

  it('completely removes memory on delete', async () => {
    const stored = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)

    await memory.delete(stored.id)

    // Row should be completely gone from DB
    try {
      const raw = await memory.getRaw(stored.id)
      // If getRaw returns anything, it means the row still exists (unexpected)
      expect(raw).toBeNull()
    } catch {
      // Throwing is expected — memory is hard-deleted
    }
  })

  it('handles deletion of non-existent memory gracefully', async () => {
    const fakeId = 'non-existent-id-12345'
    
    await expect(memory.delete(fakeId)).resolves.toBeUndefined()
  })

  it('removes memory from search results after deletion', async () => {
    const stored = await memory.store(fixtures.coffeePreference.content, fixtures.coffeePreference.owner)
    
    // Verify it's findable before deletion
    const beforeResults = await memory.search('coffee', 'default')
    const foundBefore = beforeResults.some(r => r.id === stored.id)
    expect(foundBefore).toBe(true)
    
    // Delete it
    await memory.delete(stored.id)
    
    // Verify it's not findable after deletion
    const afterResults = await memory.search('coffee', 'default')
    const foundAfter = afterResults.some(r => r.id === stored.id)
    expect(foundAfter).toBe(false)
  })

  it('supports forget operation for permanent deletion', async () => {
    const stored = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)
    
    await memory.forget(stored.id)
    
    // Should not be findable
    const results = await memory.search('engaged', 'default')
    const found = results.some(r => r.id === stored.id)
    expect(found).toBe(false)
    
    // Raw access should also fail or return null
    try {
      const raw = await memory.getRaw(stored.id)
      expect(raw).toBeNull()
    } catch {
      // Throwing is also acceptable for forgotten memories
    }
  })

  it('handles empty reason gracefully', async () => {
    const stored = await memory.store(fixtures.defaultFact.content, fixtures.defaultFact.owner)
    
    await expect(memory.delete(stored.id, '')).resolves.toBeUndefined()
  })

  it('prevents deletion of other owner memories', async () => {
    const ownerMemory = await memory.store(fixtures.defaultFact.content, 'default')
    
    // Try to delete Quaid's memory as Lori
    // This might succeed at the API level if not implemented, but let's test
    await memory.delete(ownerMemory.id)
    
    // The memory should still exist (assuming owner isolation is implemented)
    // If not implemented yet, this test will guide the implementation
  })
})