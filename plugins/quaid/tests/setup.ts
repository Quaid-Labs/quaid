import { beforeEach, afterEach } from 'vitest'
import { unlink } from 'fs/promises'
import { spawn } from 'node:child_process'
import * as path from 'node:path'

const WORKSPACE = process.env.CLAWDBOT_WORKSPACE
  || process.env.QUAID_HOME
  || path.resolve(process.cwd(), '../..')
const PYTHON_SCRIPT = path.join(WORKSPACE, "plugins/quaid/memory_graph.py")

function createUniqueTestDbPath(): string {
  const nonce = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  return `/tmp/test-memory-${process.pid}-${nonce}.db`
}

// Test memory interface
export class TestMemoryInterface {
  constructor(public dbPath: string = createUniqueTestDbPath()) {}

  async callPython(command: string, args: string[] = []): Promise<string> {
    return new Promise((resolve, reject) => {
      const proc = spawn("python3", [PYTHON_SCRIPT, command, ...args], {
        env: {
          ...process.env,
          MEMORY_DB_PATH: this.dbPath,
          MOCK_EMBEDDINGS: "1",
          QUAID_DISABLE_LLM: "1",
          QUAID_HOME: WORKSPACE,
          CLAWDBOT_WORKSPACE: WORKSPACE,
        },
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
          reject(new Error(`Python error: ${stderr || stdout}`))
        }
      })

      proc.on("error", reject)
    })
  }

  async initialize(): Promise<void> {
    await this.callPython("init")
    // Apply missing schema migrations for tests
    await this.applyMigrations()
  }

  private async applyMigrations(): Promise<void> {
    const { spawn } = await import('node:child_process')
    
    return new Promise((resolve) => {
      // Apply schema migrations directly using sqlite3
      const migrations = [
        `ALTER TABLE nodes ADD COLUMN session_id TEXT;`,
        `ALTER TABLE nodes ADD COLUMN fact_type TEXT DEFAULT 'unknown';`,
        `ALTER TABLE nodes ADD COLUMN extraction_confidence REAL DEFAULT 0.5;`,
        `ALTER TABLE nodes ADD COLUMN status TEXT DEFAULT 'pending';`,
        `ALTER TABLE nodes ADD COLUMN deleted_at TEXT;`,
        `ALTER TABLE nodes ADD COLUMN deletion_reason TEXT;`,
        // RAG doc_chunks table for documentation system
        `CREATE TABLE IF NOT EXISTS doc_chunks (
          id TEXT PRIMARY KEY,
          source_file TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          content TEXT NOT NULL,
          section_header TEXT,
          embedding BLOB NOT NULL,
          created_at TEXT DEFAULT (datetime('now')),
          updated_at TEXT DEFAULT (datetime('now')),
          UNIQUE(source_file, chunk_index)
        );`,
        `CREATE INDEX IF NOT EXISTS idx_doc_chunks_source ON doc_chunks(source_file);`,
        `CREATE INDEX IF NOT EXISTS idx_doc_chunks_updated ON doc_chunks(updated_at);`,
        // Dedup log table for tracking dedup rejections
        `CREATE TABLE IF NOT EXISTS dedup_log (
          id TEXT PRIMARY KEY,
          new_text TEXT NOT NULL,
          existing_node_id TEXT NOT NULL REFERENCES nodes(id),
          existing_text TEXT NOT NULL,
          similarity REAL NOT NULL,
          decision TEXT NOT NULL,
          llm_reasoning TEXT,
          review_status TEXT DEFAULT 'unreviewed'
              CHECK(review_status IN ('unreviewed', 'confirmed', 'reversed')),
          review_resolution TEXT,
          reviewed_at TEXT,
          owner_id TEXT,
          source TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        );`,
        `CREATE INDEX IF NOT EXISTS idx_dedup_log_review ON dedup_log(review_status);`,
        // Decay review queue for queuing memories instead of silent deletion
        `CREATE TABLE IF NOT EXISTS decay_review_queue (
          id TEXT PRIMARY KEY,
          node_id TEXT NOT NULL REFERENCES nodes(id),
          node_text TEXT NOT NULL,
          node_type TEXT,
          confidence_at_queue REAL NOT NULL,
          access_count INTEGER DEFAULT 0,
          last_accessed TEXT,
          verified INTEGER DEFAULT 0,
          created_at_node TEXT,
          decision TEXT,
          decision_reason TEXT,
          status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'reviewed')),
          queued_at TEXT DEFAULT (datetime('now')),
          reviewed_at TEXT
        );`,
        `CREATE INDEX IF NOT EXISTS idx_decay_queue_status ON decay_review_queue(status);`
      ]
      
      const proc = spawn('sqlite3', [this.dbPath], {
        stdio: ['pipe', 'pipe', 'pipe']
      })
      
      migrations.forEach(sql => {
        proc.stdin.write(sql + '\n')
      })
      proc.stdin.write('.quit\n')
      proc.stdin.end()
      
      proc.on('close', () => {
        // Ignore errors - columns/tables might already exist
        resolve()
      })
    })
  }

  async store(content: string, owner: string = 'testuser', options: any = {}): Promise<any> {
    const args = [content, '--owner', owner]
    if (options.verified) args.push('--verified')
    if (options.pinned) args.push('--pinned')
    if (options.category) args.push('--category', options.category)
    if (options.confidence !== undefined) args.push('--confidence', options.confidence.toString())
    if (options.skipDedup) args.push('--skip-dedup')
    
    const result = await this.callPython("store", args)
    
    // Parse the output format: "Stored: <id>" 
    if (result.startsWith('Stored: ')) {
      const id = result.replace('Stored: ', '').trim()
      
      // Return a mock structure that matches what tests expect
      return {
        id,
        content: content,
        name: content,
        owner: owner,
        owner_id: owner,
        created_at: new Date().toISOString(),
        confidence: options.confidence || 1.0,
        verified: options.verified || false,
        pinned: options.pinned || false,
        category: options.category,
        type: options.category || 'fact',
        embedding: this.generateMockEmbedding(content) // Mock embedding based on content
      }
    } else if (result.startsWith('Duplicate')) {
      throw new Error('Duplicate memory detected')
    } else if (result.startsWith('Updated existing:')) {
      // Handle update case - extract ID and treat as successful store
      const id = result.replace('Updated existing: ', '').trim()
      return {
        id,
        content: content,
        name: content,
        owner: owner,
        owner_id: owner,
        created_at: new Date().toISOString(),
        confidence: options.confidence || 1.0,
        verified: options.verified || false,
        pinned: options.pinned || false,
        category: options.category,
        type: options.category || 'fact',
        embedding: this.generateMockEmbedding(content) // Mock embedding based on content
      }
    } else {
      throw new Error(`Unexpected store result: ${result}`)
    }
  }

  async search(query: string, owner: string = 'testuser', limit: number = 5, minSimilarity: number = 0.3): Promise<any[]> {
    const args = [query, '--owner', owner, '--min-similarity', minSimilarity.toString()]
    if (limit !== 5) {
      args.push('--limit', limit.toString())
    }
    const result = await this.callPython("search", args)
    
    // Parse output format: "[0.84] [fact](date)[flags][C:0.5] text |ID:id|T:created|VF:vf|VU:vu|P:privacy|O:owner"
    const lines = result.trim().split('\n').filter(line => line.length > 0)

    return lines.map((line, index) => {
      // Match pattern with optional date, flags, confidence, and metadata suffix
      const match = line.match(/^\[([0-9.]+)\]\s+\[([^\]]+)\](?:\([^)]*\))?((?:\[[^\]]*\])*)\s+(.+?)\s*\|ID:([^|]+)(?:\|T:[^|]*)?(?:\|VF:[^|]*)?(?:\|VU:[^|]*)?(?:\|P:[^|]*)?(?:\|O:.*)?$/)
      if (match) {
        const [, similarity, type, flags, content, actualId] = match
        const flagStr = flags || ''
        
        return {
          id: actualId,
          similarity: parseFloat(similarity),
          type,
          content,
          text: content, 
          name: content,
          owner,
          owner_id: owner,
          verified: flagStr.includes('[V]'),
          pinned: flagStr.includes('[P]')
        }
      } else {
        // Fallback for lines without proper format
        return {
          id: `search-result-${index}`,
          similarity: 0.5,
          type: 'unknown',
          content: line,
          text: line,
          name: line,
          owner,
          owner_id: owner,
          verified: false,
          pinned: false
        }
      }
    })
  }

  async delete(memoryId: string, reason?: string): Promise<void> {
    const args = [memoryId]
    if (reason) args.push('--reason', reason)
    await this.callPython("delete", args)
  }

  async forget(memoryId: string): Promise<void> {
    await this.callPython("forget", ['--id', memoryId])
  }

  async getRaw(memoryId: string): Promise<any> {
    const result = await this.callPython("get", [memoryId])
    return JSON.parse(result)
  }

  async stats(): Promise<any> {
    const result = await this.callPython("stats")
    return JSON.parse(result)
  }

  async runDecay(): Promise<void> {
    await this.callPython("decay")
  }

  async querySql(sql: string): Promise<string> {
    const { spawn } = await import('node:child_process')
    return new Promise((resolve, reject) => {
      const proc = spawn('sqlite3', ['-json', this.dbPath], {
        stdio: ['pipe', 'pipe', 'pipe']
      })
      let stdout = ''
      let stderr = ''
      proc.stdout.on('data', (data: Buffer) => { stdout += data })
      proc.stderr.on('data', (data: Buffer) => { stderr += data })
      proc.on('close', (code: number | null) => {
        if (code === 0) resolve(stdout.trim())
        else reject(new Error(`sqlite3 error: ${stderr || stdout}`))
      })
      proc.stdin.write(sql + '\n')
      proc.stdin.write('.quit\n')
      proc.stdin.end()
    })
  }

  private generateMockEmbedding(content: string): number[] {
    // Generate a pseudo-random but deterministic 128-dim embedding (matches Python mock)
    const hash = this.simpleHash(content)
    const embedding = new Array(128)

    for (let i = 0; i < 128; i++) {
      const seed = (hash + i * 7) % 1000
      embedding[i] = (seed / 500) - 1
    }

    const magnitude = Math.sqrt(embedding.reduce((sum, val) => sum + val * val, 0))
    return embedding.map(val => val / magnitude)
  }

  private simpleHash(str: string): number {
    let hash = 0
    for (let i = 0; i < str.length; i++) {
      const char = str.charCodeAt(i)
      hash = ((hash << 5) - hash) + char
      hash = hash & hash // Convert to 32bit integer
    }
    return Math.abs(hash)
  }

  async close(): Promise<void> {
    // Python script handles DB connections per call, no explicit close needed
  }
}

export async function createTestMemory(): Promise<TestMemoryInterface> {
  const memory = new TestMemoryInterface()
  await memory.initialize()
  return memory
}

export async function cleanupTestMemory(memory: TestMemoryInterface): Promise<void> {
  await memory.close()
  delete process.env.TEST_SESSION_ID
  const dbPath = memory.dbPath
  try {
    await unlink(dbPath)
    await unlink(`${dbPath}-wal`)
    await unlink(`${dbPath}-shm`)
  } catch {
    // Files may not exist
  }
}

// Test fixtures
export const fixtures = {
  solomonFact: {
    content: 'Quaid is engaged to Melina',
    owner: 'quaid'
  },
  yuniFact: {
    content: 'Melina is from Indonesia',
    owner: 'melina'
  },
  duplicateFact: {
    content: 'Quaid is engaged to Melina',  // Exact duplicate
    owner: 'quaid'
  },
  similarFact: {
    content: 'Quaid and Melina are engaged',  // Semantically similar
    owner: 'quaid'
  },
  coffeePreference: {
    content: 'Quaid enjoys espresso coffee',
    owner: 'quaid'
  },
  healthFact: {
    content: 'Quaid has eosinophilic colitis',
    owner: 'quaid'
  },
  weatherFact: {
    content: 'The weather in Bali is tropical',
    owner: 'quaid'
  }
}

// Test utilities
export function mockSessionId(): string {
  return `test-session-${Date.now()}`
}
