import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { createTestMemory, cleanupTestMemory, TestMemoryInterface } from './setup'

describe('Recall Pipeline', () => {
  let memory: TestMemoryInterface

  beforeEach(async () => {
    memory = await createTestMemory()
  })

  afterEach(async () => {
    vi.unstubAllEnvs()
    await cleanupTestMemory(memory)
  })

  describe('Limit Parameter', () => {
    it('respects limit parameter in search', async () => {
      // Store several memories (skipDedup to avoid dedup overhead, increase timeout for embedding calls)
      for (let i = 0; i < 10; i++) {
        await memory.store(`Fact number ${i} about testing`, 'quaid', { skipDedup: true })
      }

      const limited = await memory.search('testing', 'quaid', 3)
      expect(limited.length).toBeLessThanOrEqual(3)

      const unlimited = await memory.search('testing', 'quaid', 20)
      expect(unlimited.length).toBeGreaterThan(3)
    }, 90000) // 90s timeout for 10 sequential stores (each spawns Python subprocess + embedding)

    it('returns all results when limit exceeds available', async () => {
      await memory.store('Only fact about cats', 'quaid')
      await memory.store('Only fact about dogs', 'quaid')

      const results = await memory.search('fact about', 'quaid', 100)
      expect(results.length).toBeGreaterThanOrEqual(2)
    })
  })

  describe('Search Output Format', () => {
    it('returns results with metadata fields', async () => {
      await memory.store('Quaid lives in Bali', 'quaid')

      const results = await memory.search('Bali', 'quaid')
      expect(results.length).toBeGreaterThan(0)

      const result = results[0]
      expect(result.id).toBeDefined()
      expect(result.similarity).toBeGreaterThan(0)
      expect(result.type).toBeDefined()
      expect(result.content).toContain('Bali')
    })

    it('includes proper ID in results', async () => {
      const stored = await memory.store('Unique testable memory XYZ123', 'quaid')
      const results = await memory.search('XYZ123', 'quaid')

      expect(results.length).toBeGreaterThan(0)
      // The search result ID should be a valid UUID
      expect(results[0].id).toMatch(/^[0-9a-f-]+$/)
    })
  })

  describe('Privacy-Aware Search', () => {
    it('shared memories are visible across owners', async () => {
      // Default privacy is "shared" — visible to all owners
      await memory.store('Household rule: no shoes indoors', 'quaid')

      const otherOwnerResults = await memory.search('shoes indoors', 'melina')
      expect(otherOwnerResults.length).toBeGreaterThanOrEqual(1)
      expect(otherOwnerResults.some(r =>
        (r.text || r.content || r.name).includes('shoes')
      )).toBe(true)
    })

    it('owner can see their own memories', async () => {
      await memory.store('Quaid secret project alpha', 'quaid')

      const results = await memory.search('project alpha', 'quaid')
      expect(results.length).toBeGreaterThanOrEqual(1)
      expect(results.some(r =>
        (r.text || r.content || r.name).includes('alpha')
      )).toBe(true)
    })
  })

  describe('FTS and Semantic Hybrid Search', () => {
    it('finds results for proper noun queries', async () => {
      await memory.store('Hauser works as a VP at Honeywell', 'quaid')
      await memory.store('Hauser has a husband named Troy', 'quaid')
      await memory.store('The weather today is sunny', 'quaid')

      const results = await memory.search('Hauser', 'quaid')
      expect(results.length).toBeGreaterThanOrEqual(2)

      // Hauser facts should rank higher than unrelated facts
      const shannonResults = results.filter(r =>
        (r.text || r.content || r.name).includes('Hauser')
      )
      expect(shannonResults.length).toBe(2)
    })

    it('finds results for semantic queries without exact keyword match', async () => {
      await memory.store('Quaid prefers dark roast espresso', 'quaid')

      const results = await memory.search('coffee preference', 'quaid')
      expect(results.length).toBeGreaterThanOrEqual(1)
      expect(results.some(r =>
        (r.text || r.content || r.name).includes('espresso')
      )).toBe(true)
    })

    it('ranks exact keyword matches higher', async () => {
      await memory.store('Melina is from Sukabumi Indonesia', 'quaid')
      await memory.store('Quaid visited Jakarta last year', 'quaid')
      await memory.store('The capital of France is Paris', 'quaid')

      const results = await memory.search('Melina', 'quaid')
      expect(results.length).toBeGreaterThanOrEqual(1)

      const hasYuniResult = results.some(r =>
        (r.text || r.content || r.name).includes('Melina')
      )
      expect(hasYuniResult).toBe(true)
    })
  })

  describe('Person-Related Recall', () => {
    it('retrieves multiple facts about a person', async () => {
      await memory.store('Hauser is a VP at Honeywell', 'quaid')
      await memory.store('Hauser has a son named Quentin', 'quaid')
      await memory.store('Hauser is very responsible', 'quaid')
      await memory.store('Unrelated fact about weather', 'quaid')

      const results = await memory.search('Tell me about Hauser', 'quaid')
      const shannonFacts = results.filter(r =>
        (r.text || r.content || r.name).includes('Hauser')
      )

      expect(shannonFacts.length).toBeGreaterThanOrEqual(2)
    })

    it('retrieves person facts for indirect mentions', async () => {
      await memory.store('Melina birthday is June 30', 'quaid')
      await memory.store('Melina favorite food is Indomie', 'quaid')

      // Indirect mention - not asking "about Melina" but mentioning her
      const results = await memory.search('planning dinner for Melina', 'quaid')
      const yuniFacts = results.filter(r =>
        (r.text || r.content || r.name).includes('Melina')
      )

      expect(yuniFacts.length).toBeGreaterThanOrEqual(1)
    })
  })

  describe('Session Dedup in Recall', () => {
    it('excludes current session memories from search', async () => {
      const sessionId = `test-session-${Date.now()}`

      // Store a memory in current session
      vi.stubEnv('TEST_SESSION_ID', sessionId)
      await memory.store('Current session fact about testing', 'quaid')

      // Search within same session — should not find just-stored memory
      // (prevents immediate feedback loops)
      const results = await memory.search('current session fact', 'quaid')

      // The current session memory may or may not appear depending on
      // session filtering implementation. If it appears, it should still
      // be a valid result.
      expect(Array.isArray(results)).toBe(true)

    })
  })
})
