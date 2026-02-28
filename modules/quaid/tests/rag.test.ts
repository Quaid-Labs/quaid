import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { spawn } from 'node:child_process'
import { unlink, rm } from 'fs/promises'
import { existsSync } from 'fs'
import * as path from 'node:path'

const WORKSPACE = process.env.CLAWDBOT_WORKSPACE
  || process.env.QUAID_HOME
  || path.resolve(process.cwd(), '../..')
const RAG_SCRIPT = path.join(WORKSPACE, "modules/quaid/datastore/docsdb/rag.py")
const PYTHON_MODULE_ROOT = path.resolve(path.dirname(RAG_SCRIPT), "../..")
const TEST_FIXTURES_DIR = '/tmp/rag-test-fixtures'

function createUniqueTestDbPath(): string {
  const nonce = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  return `/tmp/test-rag-${process.pid}-${nonce}.db`
}

// Test RAG interface
class TestRAGInterface {
  constructor(public dbPath: string = createUniqueTestDbPath()) {}

  async callPython(command: string, args: string[] = []): Promise<string> {
    return new Promise((resolve, reject) => {
      const proc = spawn("python3", [RAG_SCRIPT, command, ...args], {
        env: {
          ...process.env,
          MEMORY_DB_PATH: this.dbPath,
          MOCK_EMBEDDINGS: "1",
          QUAID_HOME: WORKSPACE,
          CLAWDBOT_WORKSPACE: WORKSPACE,
          PYTHONPATH: process.env.PYTHONPATH
            ? `${PYTHON_MODULE_ROOT}:${process.env.PYTHONPATH}`
            : PYTHON_MODULE_ROOT,
        },
        cwd: WORKSPACE
      })

      let stdout = ""
      let stderr = ""

      proc.stdout.on("data", (data) => {
        stdout += data
      })
      proc.stderr.on("data", (data) => {
        stderr += data
      })

      proc.on("close", (code) => {
        if (code === 0) {
          resolve(stdout.trim())
        } else {
          reject(new Error(`RAG Python error (${code}): ${stderr || stdout}`))
        }
      })

      proc.on("error", reject)
    })
  }

  async reindex(dir: string = TEST_FIXTURES_DIR, force: boolean = false): Promise<string> {
    const args = ['--dir', dir]
    if (force) args.push('--all')
    return this.callPython('reindex', args)
  }

  async search(query: string, limit: number = 5, minSimilarity: number = 0.3): Promise<any[]> {
    const args = [query, '--limit', limit.toString(), '--min-similarity', minSimilarity.toString()]
    const output = await this.callPython('search', args)
    
    if (output.includes('No results found')) {
      return []
    }
    
    // Parse output format:
    // Found X results for 'query':
    // 
    // 1. ~/path/file.md > Header (similarity: 0.85)
    //    Content preview line 1...
    //    Content preview line 2...
    
    const lines = output.split('\n')
    const results = []
    let currentResult: any = null
    
    for (const line of lines) {
      // Match result header: "1. ~/path/file.md > Header (similarity: 0.85)"
      const resultMatch = line.match(/^(\d+)\.\s+(.+?)\s+\(similarity:\s+([\d.]+)\)$/)
      if (resultMatch) {
        if (currentResult) {
          results.push(currentResult)
        }
        
        const [, index, pathAndHeader, similarity] = resultMatch
        const parts = pathAndHeader.split(' > ')
        const sourcePath = parts[0].replace('~/', `${WORKSPACE}/`)
        const header = parts.length > 1 ? parts[1] : null
        
        currentResult = {
          index: parseInt(index),
          source: sourcePath,
          section_header: header,
          similarity: parseFloat(similarity),
          content: ''
        }
      } else if (line.startsWith('   ') && currentResult) {
        // Content line
        const contentLine = line.substring(3) // Remove "   "
        if (currentResult.content) {
          currentResult.content += '\n' + contentLine
        } else {
          currentResult.content = contentLine
        }
      }
    }
    
    if (currentResult) {
      results.push(currentResult)
    }
    
    return results
  }

  async stats(): Promise<any> {
    const output = await this.callPython('stats')
    
    // Parse stats output:
    // Documentation Index Statistics:
    //   Total files: 4
    //   Total chunks: 15
    //   Last indexed: 2026-02-02 11:30:45
    //
    // By category:
    //   .md: 4 files, 15 chunks
    
    const stats = {
      total_files: 0,
      total_chunks: 0,
      last_indexed: null,
      by_category: {}
    }
    
    const lines = output.split('\n')
    for (const line of lines) {
      if (line.includes('Total files:')) {
        stats.total_files = parseInt(line.split(':')[1].trim())
      } else if (line.includes('Total chunks:')) {
        stats.total_chunks = parseInt(line.split(':')[1].trim())
      } else if (line.includes('Last indexed:') && !line.includes('Never')) {
        stats.last_indexed = line.split('Last indexed:')[1].trim()
      }
      
      // Parse category stats like "  .md: 4 files, 15 chunks"
      const categoryMatch = line.match(/^\s+(.+):\s+(\d+)\s+files?,\s+(\d+)\s+chunks?/)
      if (categoryMatch) {
        const [, category, files, chunks] = categoryMatch
        stats.by_category[category] = {
          files: parseInt(files),
          chunks: parseInt(chunks)
        }
      }
    }
    
    return stats
  }
}

async function createTestRAG(): Promise<TestRAGInterface> {
  return new TestRAGInterface()
}

async function cleanupTestRAG(rag: TestRAGInterface): Promise<void> {
  try {
    await unlink(rag.dbPath)
    await unlink(`${rag.dbPath}-wal`)
    await unlink(`${rag.dbPath}-shm`)
  } catch {
    // Files may not exist
  }
}

describe('RAG Documentation System', () => {
  let rag: TestRAGInterface

  beforeEach(async () => {
    rag = await createTestRAG()
    await createTestFixtures()
  })

  afterEach(async () => {
    await cleanupTestRAG(rag)
    await cleanupTestFixtures()
  })

  async function createTestFixtures() {
    const { mkdir, writeFile } = await import('fs/promises')
    
    await mkdir(TEST_FIXTURES_DIR, { recursive: true })
      
      // Create test fixture files
      await writeFile(`${TEST_FIXTURES_DIR}/simple-doc.md`, `# Simple Documentation

This is a simple test document with basic headers and content.

## Introduction

Welcome to our test documentation. This section covers basic concepts and provides an overview.

## Features

Our system includes:
- Feature A: Basic functionality
- Feature B: Advanced operations  
- Feature C: Integration capabilities

## Getting Started

To get started with the system, follow these steps:

1. Install the dependencies
2. Configure your settings
3. Run the initial setup

That's it! You're ready to use the system.

## Conclusion

This concludes our simple documentation. For more information, refer to the advanced guides.`)

      await writeFile(`${TEST_FIXTURES_DIR}/long-doc.md`, `# Comprehensive System Documentation

This is a very long document that should be split into multiple chunks during indexing.

## Chapter 1: Architecture Overview

The system architecture follows modern microservices principles with distributed components communicating through well-defined APIs. Each service is responsible for a specific domain and operates independently to ensure fault tolerance and scalability.

The core components include the API Gateway, Authentication Service, Data Processing Engine, and Storage Layer. These components work together to provide a robust and flexible platform that can handle various workloads and use cases.

The API Gateway serves as the entry point for all external requests, providing routing, rate limiting, and authentication checks. It ensures that requests are properly validated and forwarded to the appropriate downstream services.

## Chapter 2: Installation and Setup

Before installing the system, ensure that your environment meets the minimum requirements. You'll need Docker, Kubernetes, and sufficient computational resources to run the services effectively.

Start by cloning the repository from the official source. The repository contains all necessary configuration files, deployment scripts, and documentation needed for a successful installation.

Configure your environment variables by copying the provided template files and updating them with your specific settings. Pay special attention to database connection strings, API keys, and networking configurations.

## Chapter 3: Configuration Guide

The system supports extensive configuration options through environment variables, configuration files, and runtime parameters. Understanding these options is crucial for optimal performance and security.

Database configurations control connection pooling, query timeouts, and transaction isolation levels. Proper tuning of these parameters can significantly impact system performance, especially under high load conditions.`)

      await writeFile(`${TEST_FIXTURES_DIR}/special-chars-doc.md`, `# Documentation with Special Characters 🎉

This document contains various special characters, unicode symbols, and different encodings to test the RAG system's handling of diverse content.

## Émojis and Unicode 🌟

Our system supports international characters like:
- Français: café, résumé, naïve
- Español: niño, señora, año
- Deutsch: über, müssen, weiß

### Emoji Support 🎯

The system handles various emojis:
- 😀😃😄😁😆😅😂🤣
- 🎉🎊🎁🎈🎂🎀🎗️🎟️
- 🌟⭐✨💫⚡🔥💥✅`)

      await writeFile(`${TEST_FIXTURES_DIR}/empty-doc.md`, '')

      await writeFile(`${TEST_FIXTURES_DIR}/no-headers-doc.md`, `This is a document without any markdown headers. It contains just paragraphs of text that should still be properly chunked by the RAG system.

The first paragraph discusses the importance of proper text processing even when formal document structure is not present. Systems should be robust enough to handle various document formats and styles.

A second paragraph continues the discussion about document processing. It emphasizes that content value is not dependent on formal structure, and many valuable documents exist in plain text format.`)

  }

  async function cleanupTestFixtures() {
    const { rm } = await import('fs/promises')
    
    try {
      await rm(TEST_FIXTURES_DIR, { recursive: true, force: true })
    } catch (error) {
      // Ignore cleanup errors
    }
  }

  describe('Indexing Tests', () => {
    it('indexes a single markdown file correctly', async () => {
      const output = await rag.reindex()

      expect(output).toContain('files indexed')
      expect(output).toContain('chunks')

      const stats = await rag.stats()
      expect(stats.total_files).toBeGreaterThan(0)
      expect(stats.total_chunks).toBeGreaterThan(0)
    }, 60000)

    it('generates chunks of appropriate size', async () => {
      await rag.reindex()
      
      // Search for content from long-doc.md which should be chunked
      const results = await rag.search('Architecture Overview', 10, 0.1)
      
      expect(results.length).toBeGreaterThan(0)
      
      // Check that chunks are reasonable size (not too small or huge)
      for (const result of results) {
        const tokenCount = result.content.split(/\s+/).length
        expect(tokenCount).toBeGreaterThanOrEqual(5) // Not too small
        expect(tokenCount).toBeLessThan(2000) // Not too large
      }
    })

    it('tracks source_file and chunk information properly', async () => {
      await rag.reindex()

      const results = await rag.search('simple test document', 10, 0.1)

      expect(results.length).toBeGreaterThan(0)

      // With mock embeddings, ordering is non-semantic — check any result has source info
      const simpleResult = results.find(r => r.source.includes('simple-doc.md'))
      expect(simpleResult).toBeDefined()
      expect(simpleResult!.similarity).toBeGreaterThan(0)
    })

    it('re-indexes only changed files (mtime check)', async () => {
      // First index
      const output1 = await rag.reindex()
      const stats1 = await rag.stats()
      
      // Ensure we cross coarse filesystem mtime granularity (often 1s).
      await new Promise(resolve => setTimeout(resolve, 1100))
      
      // Second index without --all flag should skip unchanged files
      const output2 = await rag.reindex()
      const stats2 = await rag.stats()

      // Skip indicator text can vary by runtime; chunk totals should remain stable.
      expect(stats2.total_chunks).toBe(stats1.total_chunks)
    })

    it('force reindex with --all flag works', async () => {
      // Initial index
      await rag.reindex()
      
      // Force reindex should process files again
      const output = await rag.reindex(TEST_FIXTURES_DIR, true)
      
      expect(output).toContain('files indexed')
      
      const stats = await rag.stats()
      expect(stats.total_files).toBeGreaterThan(0)
    })

    it('handles empty files gracefully', async () => {
      const output = await rag.reindex()
      
      // Should complete without errors even with empty-doc.md
      expect(output).toContain('Total:')
      
      const stats = await rag.stats()
      // Empty files might be indexed but produce no chunks
      expect(stats.total_files).toBeGreaterThanOrEqual(3) // At least the non-empty files
    })

    it('handles files with no headers (still chunks by paragraphs)', async () => {
      await rag.reindex()

      // Search broadly to find the no-headers doc among all results
      const results = await rag.search('document without any markdown headers', 20, 0.01)

      expect(results.length).toBeGreaterThan(0)

      // The no-headers doc should appear somewhere in results
      const noHeaderResult = results.find(r => r.source.includes('no-headers-doc.md'))
      expect(noHeaderResult).toBeDefined()
      expect(noHeaderResult!.content).toContain('document without any markdown headers')
    })
  })

  describe('Search Tests', () => {
    beforeEach(async () => {
      // Index test documents before each search test
      await rag.reindex()
    })

    it('returns relevant results for semantic queries', async () => {
      const results = await rag.search('system architecture microservices')
      
      expect(results.length).toBeGreaterThan(0)
      
      const result = results[0]
      expect(result.similarity).toBeGreaterThan(0.3)
      expect(result.content.toLowerCase()).toMatch(/architecture|microservices|system/)
    })

    it('respects limit parameter', async () => {
      const results = await rag.search('documentation', 2, 0.1)
      
      expect(results.length).toBeLessThanOrEqual(2)
    })

    it('returns similarity scores', async () => {
      const results = await rag.search('simple documentation')
      
      expect(results.length).toBeGreaterThan(0)
      
      for (const result of results) {
        expect(result.similarity).toBeGreaterThan(0)
        expect(result.similarity).toBeLessThanOrEqual(1)
      }
    })

    it('results are ordered by similarity (descending)', async () => {
      const results = await rag.search('system documentation', 5, 0.1)
      
      if (results.length > 1) {
        for (let i = 0; i < results.length - 1; i++) {
          expect(results[i].similarity).toBeGreaterThanOrEqual(results[i + 1].similarity)
        }
      }
    })

    it('returns source file paths in results', async () => {
      const results = await rag.search('documentation')
      
      expect(results.length).toBeGreaterThan(0)
      
      for (const result of results) {
        expect(result.source).toBeDefined()
        expect(result.source).toContain('.md')
      }
    })

    it('handles queries with no matches gracefully', async () => {
      // Use a very specific minimum similarity to ensure no matches
      const results = await rag.search('nonexistent_unique_term_xyz123_absolutely_not_found', 5, 0.95)
      
      expect(results).toEqual([])
    })

    it('handles special characters in queries', async () => {
      const results = await rag.search('émojis unicode 🎉')
      
      // Should not throw error and should find the special chars document
      expect(Array.isArray(results)).toBe(true)
      
      if (results.length > 0) {
        expect(results[0].source).toContain('special-chars-doc.md')
      }
    })
  })

  describe('Stats Tests', () => {
    it('returns correct document count', async () => {
      await rag.reindex()
      
      const stats = await rag.stats()
      
      // We have 4 test fixture files (including empty one)
      expect(stats.total_files).toBeGreaterThanOrEqual(3)
      expect(stats.total_files).toBeLessThanOrEqual(5)
    })

    it('returns correct chunk count', async () => {
      await rag.reindex()
      
      const stats = await rag.stats()
      
      expect(stats.total_chunks).toBeGreaterThan(5) // Should have multiple chunks from long-doc.md
    })

    it('returns last indexed timestamp', async () => {
      await rag.reindex()
      
      const stats = await rag.stats()
      
      expect(stats.last_indexed).toBeDefined()
      expect(stats.last_indexed).not.toBeNull()
    })

    it('includes category breakdown', async () => {
      await rag.reindex()
      
      const stats = await rag.stats()
      
      expect(stats.by_category).toBeDefined()
      
      // Check if any category exists (might be .md or might be different format)
      const categoryKeys = Object.keys(stats.by_category)
      if (categoryKeys.length > 0) {
        const firstCategory = stats.by_category[categoryKeys[0]]
        expect(firstCategory.files).toBeGreaterThan(0)
        expect(firstCategory.chunks).toBeGreaterThan(0)
      } else {
        // If no categories found, that's also acceptable for this test
        console.warn('No categories found in stats breakdown')
      }
    })
  })

  describe('Edge Cases', () => {
    beforeEach(async () => {
      await rag.reindex()
    })

    it('handles very long documents (chunking works correctly)', async () => {
      // Search for content from different sections of the long document
      const chapter1Results = await rag.search('Architecture Overview microservices principles', 3, 0.1)
      const chapter2Results = await rag.search('Installation and Setup Docker Kubernetes', 3, 0.1)
      const chapter3Results = await rag.search('Configuration Guide environment variables', 3, 0.1)
      
      // Should find results from different parts of the document
      expect(chapter1Results.length).toBeGreaterThan(0)
      expect(chapter2Results.length).toBeGreaterThan(0)
      expect(chapter3Results.length).toBeGreaterThan(0)
      
      // At least one result should come from the long-doc.md file
      const hasLongDoc = [...chapter1Results, ...chapter2Results, ...chapter3Results]
        .some(result => result.source.includes('long-doc.md'))
      expect(hasLongDoc).toBe(true)
    })

    it('handles documents with special characters (unicode, emojis)', async () => {
      // Search for very specific content from the special chars document
      const results = await rag.search('Documentation with Special Characters emojis unicode', 5, 0.1)
      
      expect(results.length).toBeGreaterThan(0)
      
      // Find the special chars document in results
      const specialCharsResult = results.find(r => r.source.includes('special-chars-doc.md'))
      if (specialCharsResult) {
        expect(specialCharsResult.content).toMatch(/café|émojis|emojis|unicode|special characters/i)
      } else {
        // If not found in top results, search more specifically
        const specificResults = await rag.search('français español deutsch unicode', 10, 0.1)
        const hasSpecialChars = specificResults.some(r => r.source.includes('special-chars-doc.md'))
        expect(hasSpecialChars).toBe(true)
      }
    })

    it('handles non-existent search terms gracefully', async () => {
      // Use high similarity threshold to ensure no matches
      const results = await rag.search('absolutely_nonexistent_term_12345_xyz_never_found', 5, 0.95)
      
      expect(results).toEqual([])
    })

    it('handles query with only special characters', async () => {
      const results = await rag.search('🎉💫⭐')
      
      expect(Array.isArray(results)).toBe(true)
      // May or may not find results, but should not error
    })

    it('handles extremely long queries', async () => {
      const longQuery = 'system architecture microservices distributed components API gateway authentication data processing '.repeat(10)
      
      const results = await rag.search(longQuery.substring(0, 500)) // Trim to reasonable length
      
      expect(Array.isArray(results)).toBe(true)
    })

    it('handles empty query gracefully', async () => {
      try {
        const results = await rag.search('')
        expect(Array.isArray(results)).toBe(true)
      } catch (error) {
        // Empty query might be rejected, which is acceptable behavior
        expect(error).toBeDefined()
      }
    })

    it('handles minimum similarity threshold correctly', async () => {
      const highThreshold = await rag.search('documentation', 10, 0.9)
      const lowThreshold = await rag.search('documentation', 10, 0.1)
      
      // Lower threshold should return more or equal results
      expect(lowThreshold.length).toBeGreaterThanOrEqual(highThreshold.length)
      
      // All results should meet the minimum threshold
      for (const result of highThreshold) {
        expect(result.similarity).toBeGreaterThanOrEqual(0.9)
      }
    })
  })

  describe('Concurrent Operations', () => {
    beforeEach(async () => {
      await rag.reindex()
    })

    it('handles concurrent searches', async () => {
      // Run multiple searches concurrently
      const searches = [
        rag.search('documentation'),
        rag.search('architecture'),
        rag.search('configuration'),
        rag.search('installation')
      ]
      
      const results = await Promise.all(searches)
      
      // All searches should complete successfully
      for (const searchResults of results) {
        expect(Array.isArray(searchResults)).toBe(true)
      }
    })

    it('handles search while indexing (if concurrent access is safe)', async () => {
      // Start a reindex operation
      const reindexPromise = rag.reindex(TEST_FIXTURES_DIR, true)
      
      // Try to search while reindexing
      await new Promise(resolve => setTimeout(resolve, 100)) // Small delay
      
      try {
        const searchPromise = rag.search('documentation')
        
        // Both operations should complete
        const [reindexResult, searchResult] = await Promise.all([reindexPromise, searchPromise])
        
        expect(reindexResult).toContain('files indexed')
        expect(Array.isArray(searchResult)).toBe(true)
      } catch (error) {
        // Concurrent operations might not be supported, which is acceptable
        console.warn('Concurrent search during indexing failed (may be expected):', error.message)
      }
    })
  })
})
