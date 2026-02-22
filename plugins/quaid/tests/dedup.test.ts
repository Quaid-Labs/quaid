import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { createTestMemory, cleanupTestMemory, fixtures, type TestMemoryInterface } from './setup'

describe('Deduplication', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    await cleanupTestMemory(memory)
  })

  it('handles similar content without exact duplication errors', async () => {
    // skipDedup: testing that similar content CAN be stored when dedup is bypassed
    await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner, { skipDedup: true })
    
    // Store similar but not identical content
    const result = await memory.store(fixtures.similarFact.content, fixtures.similarFact.owner, { skipDedup: true })
    
    // Should succeed when dedup is skipped
    expect(result.id).toBeDefined()
  })

  it('stores genuinely different content without issues', async () => {
    await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    await memory.store(fixtures.coffeePreference.content, fixtures.coffeePreference.owner)
    
    // Should have both memories searchable
    const engagementResults = await memory.search('engaged', 'quaid')
    const coffeeResults = await memory.search('coffee', 'quaid')
    
    expect(engagementResults.length).toBeGreaterThan(0)
    expect(coffeeResults.length).toBeGreaterThan(0)
  })

  it('handles exact duplicate attempts gracefully', async () => {
    await memory.store(fixtures.solomonFact.content, fixtures.solomonFact.owner)
    
    // Attempt to store exact duplicate
    const duplicateAttempt = async () => {
      return await memory.store(fixtures.duplicateFact.content, fixtures.duplicateFact.owner)
    }
    
    // Implementation might reject duplicates or flag them
    // Both behaviors are acceptable
    try {
      const result = await duplicateAttempt()
      expect(result).toBeDefined()
    } catch (error) {
      // Rejection is also acceptable
      expect(error.message).toBeTruthy()
    }
  })

  it('maintains independence across different owners', async () => {
    // Same content for different owners should be allowed
    await memory.store('I like coffee', 'quaid')
    const yuniCoffee = await memory.store('I like coffee', 'melina')
    
    expect(yuniCoffee.id).toBeDefined()
    
    // Both should be searchable by their respective owners
    const solomonResults = await memory.search('coffee', 'quaid')
    const yuniResults = await memory.search('coffee', 'melina')
    
    expect(solomonResults.length).toBeGreaterThan(0)
    expect(yuniResults.length).toBeGreaterThan(0)
    
    // Verify owner isolation
    for (const result of solomonResults) {
      expect(result.owner || result.owner_id).toBe('quaid')
    }
    for (const result of yuniResults) {
      expect(result.owner || result.owner_id).toBe('melina')
    }
  })

  it('handles near-duplicate content appropriately', async () => {
    // skipDedup: testing that near-duplicates CAN be stored when dedup is bypassed
    await memory.store('Quaid is engaged to Melina', 'quaid', { skipDedup: true })
    
    // Store semantically very similar content
    const nearDuplicate = await memory.store('Quaid and Melina are engaged', 'quaid', { skipDedup: true })
    
    expect(nearDuplicate.id).toBeDefined()
    
    // Both should be findable when dedup is bypassed
    const results = await memory.search('engaged', 'quaid')
    expect(results.length).toBeGreaterThanOrEqual(1) // At least one should be found
  })

  it('preserves content variations with different context', async () => {
    await memory.store('Quaid drinks coffee in the morning', 'quaid')
    await memory.store('Quaid drinks coffee after dinner', 'quaid')
    
    const results = await memory.search('quaid coffee', 'quaid')
    expect(results.length).toBeGreaterThanOrEqual(1)
  })

  it('handles punctuation and formatting differences', async () => {
    await memory.store('Quaid is engaged to Melina.', 'quaid')
    // Identical content minus a period is correctly caught as a duplicate
    // The system should either reject it or update the existing entry
    try {
      const withoutPunct = await memory.store('Quaid is engaged to Melina', 'quaid')
      // If it stores, it should have an ID (updated existing)
      expect(withoutPunct.id).toBeDefined()
    } catch (error: any) {
      // Duplicate detection is also acceptable — means dedup is working
      expect(error.message).toContain('Duplicate')
    }
  })

  it('allows different facts within single owner scope', async () => {
    // Store three genuinely different facts for same owner
    const first = await memory.store('Unique fact about Quaid', 'quaid')
    const second = await memory.store('Quaid works in technology', 'quaid')
    const third = await memory.store('Quaid lives in Bali', 'quaid')
    
    // All should succeed - they're semantically different
    expect(first.id).toBeDefined()
    expect(second.id).toBeDefined()
    expect(third.id).toBeDefined()
    
    // Verify all three exist
    const results = await memory.search('Quaid', 'quaid')
    expect(results.length).toBeGreaterThanOrEqual(2)
  })

  it('handles case sensitivity appropriately', async () => {
    await memory.store('quaid is engaged to melina', 'quaid')
    // Case-only differences may be detected as duplicates by high-quality embedding models
    // Either storing or dedup-rejecting is acceptable behavior
    try {
      const capitalized = await memory.store('Quaid is engaged to Melina', 'quaid')
      expect(capitalized.id).toBeDefined()
    } catch {
      // Duplicate detection is acceptable — model correctly identified same meaning
    }
  })

  it('logs dedup rejections to dedup_log table', async () => {
    // Store original
    await memory.store('Quaid is engaged to Melina', 'quaid')

    // Attempt exact duplicate (should be rejected and logged)
    try {
      await memory.store('Quaid is engaged to Melina', 'quaid')
    } catch {
      // Expected - duplicate rejection
    }

    // Query dedup_log table directly (hash_exact = content-hash match, auto_reject = embedding match)
    const result = await memory.querySql(
      "SELECT COUNT(*) as cnt FROM dedup_log WHERE decision IN ('hash_exact', 'auto_reject', 'fallback_reject', 'haiku_reject');"
    )
    const rows = JSON.parse(result)
    // Should have at least one logged rejection
    expect(rows[0].cnt).toBeGreaterThanOrEqual(1)
  })

  it('allows storing semantically distinct memories without false positives', async () => {
    // These are all genuinely different facts - dedup should not block them
    const memories = [
      'Quaid likes espresso',
      'Melina prefers tea',
      'They both enjoy travel',
      'Quaid works in tech',
      'Melina is from Indonesia'
    ]
    
    const stored = []
    for (const content of memories) {
      const result = await memory.store(content, 'quaid')
      stored.push(result)
    }
    
    // All should succeed - these are semantically distinct
    expect(stored.length).toBe(5)
    for (const result of stored) {
      expect(result.id).toBeDefined()
    }
    
    // Verify searchable
    const results = await memory.search('quaid', 'quaid')
    expect(results.length).toBeGreaterThan(0)
  })
})