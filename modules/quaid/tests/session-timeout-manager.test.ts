import { describe, it, expect, vi } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { SessionTimeoutManager } from '../core/session-timeout'

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

function writeFailHardConfig(workspace: string, failHard: boolean): void {
  const configDir = path.join(workspace, "config")
  fs.mkdirSync(configDir, { recursive: true })
  fs.writeFileSync(
    path.join(configDir, "memory.json"),
    JSON.stringify({ retrieval: { failHard } }),
    "utf8",
  )
}

describe('SessionTimeoutManager scheduling', () => {
  it('allows only one timeout worker leader per workspace', () => {
    const workspace = makeWorkspace('quaid-timeout-leader-')
    const managerA = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })
    const managerB = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    const first = managerA.startWorker(30)
    const second = managerB.startWorker(30)

    expect(first).toBe(true)
    expect(second).toBe(false)

    managerA.stopWorker()
    const afterRelease = managerB.startWorker(30)
    expect(afterRelease).toBe(true)
    managerB.stopWorker()
  })

  it('does not remove worker lock when stale check sees lock content change', () => {
    const workspace = makeWorkspace('quaid-timeout-lock-race-')
    const managerA = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })
    const managerB = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    expect(managerA.startWorker(30)).toBe(true)
    const lockPath = (managerA as any).workerLockPath as string
    const originalRaw = fs.readFileSync(lockPath, 'utf8')

    vi.spyOn(managerB as any, 'isPidAlive').mockImplementation(() => {
      fs.writeFileSync(
        lockPath,
        JSON.stringify({ pid: 99999999, token: 'different-token', started_at: new Date().toISOString() }),
        'utf8',
      )
      return false
    })

    const acquired = (managerB as any).tryAcquireWorkerLock()
    expect(acquired).toBe(false)
    expect(JSON.parse(fs.readFileSync(lockPath, 'utf8')).token).toBe('different-token')
    managerA.stopWorker()
  })

  it('coalesces duplicate extraction signals per session and promotes reset over compaction', () => {
    const workspace = makeWorkspace('quaid-timeout-signal-')
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    manager.queueExtractionSignal('session-1', 'Compaction')
    manager.queueExtractionSignal('session-1', 'Reset')

    const signalDir = path.join(workspace, 'data', 'pending-extraction-signals')
    const files = fs.readdirSync(signalDir).filter((f) => f.endsWith('.json'))
    expect(files).toHaveLength(1)
    const queued = JSON.parse(fs.readFileSync(path.join(signalDir, files[0]), 'utf8'))
    expect(queued.label).toBe('Reset')
  })

  it('processes signal extraction from per-session message log', async () => {
    const workspace = makeWorkspace('quaid-timeout-extract-')
    const calls: Array<{ messages: any[]; sessionId?: string; label?: string }> = []

    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label })
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    const msgs = [
      { id: 'u1', role: 'user', content: 'my fact', timestamp: new Date().toISOString() },
      { id: 'a1', role: 'assistant', content: 'ack', timestamp: new Date().toISOString() },
    ]

    manager.onAgentEnd(msgs, 'session-2')
    manager.queueExtractionSignal('session-2', 'Reset')
    await manager.processPendingExtractionSignals()

    expect(calls).toHaveLength(1)
    expect(calls[0].sessionId).toBe('session-2')
    expect(calls[0].label).toBe('Reset')
    expect(calls[0].messages).toHaveLength(2)
  })

  it('filters internal/system traffic from extraction payloads', async () => {
    const workspace = makeWorkspace('quaid-timeout-filter-')
    const calls: Array<{ messages: any[]; sessionId?: string; label?: string }> = []
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label })
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    manager.onAgentEnd([
      { role: 'user', content: 'Given a personal memory query and memory documents', timestamp: Date.now() },
      { role: 'assistant', content: '{"facts":[],"journal_entries":[]}', timestamp: Date.now() + 1 },
      { role: 'user', content: 'My father is Kent', timestamp: Date.now() + 2 },
      { role: 'assistant', content: 'Saved.', timestamp: Date.now() + 3 },
    ], 'session-filter')

    manager.queueExtractionSignal('session-filter', 'Reset')
    await manager.processPendingExtractionSignals()

    expect(calls).toHaveLength(1)
    expect(calls[0].messages).toHaveLength(2)
    expect(calls[0].messages[0].content).toContain('father is Kent')
  })

  it('recovers orphaned signal processing claims from dead pids', async () => {
    const workspace = makeWorkspace('quaid-timeout-orphan-signal-')
    const calls: Array<{ sessionId?: string; label?: string }> = []
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (_messages, sessionId, label) => {
        calls.push({ sessionId, label })
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    const signalDir = path.join(workspace, 'data', 'pending-extraction-signals')
    const orphanClaim = path.join(signalDir, 'session-orphan.json.processing.99999999')
    fs.writeFileSync(orphanClaim, JSON.stringify({
      sessionId: 'session-orphan',
      label: 'Reset',
      queuedAt: new Date().toISOString(),
    }))
    manager.onAgentEnd([
      { role: 'user', content: 'remember this', timestamp: Date.now() },
      { role: 'assistant', content: 'ok', timestamp: Date.now() + 1 },
    ], 'session-orphan')

    await manager.processPendingExtractionSignals()

    expect(calls).toHaveLength(1)
    expect(calls[0].sessionId).toBe('session-orphan')
  })

  it('blocks fallback event payload extraction when failHard=true', async () => {
    const workspace = makeWorkspace('quaid-timeout-failhard-')
    writeFailHardConfig(workspace, true)
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    await expect(
      manager.extractSessionFromLog('session-failhard', 'Reset', [
        { role: 'user', content: 'remember this', timestamp: Date.now() },
      ]),
    ).rejects.toThrow(/fallback payload blocked by failHard/i)
  })

  it('allows fallback event payload extraction when failHard=false', async () => {
    const workspace = makeWorkspace('quaid-timeout-soft-')
    writeFailHardConfig(workspace, false)
    const calls: Array<{ messages: any[] }> = []
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (messages) => {
        calls.push({ messages })
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    const ok = await manager.extractSessionFromLog('session-soft', 'Reset', [
      { role: 'user', content: 'remember this', timestamp: Date.now() },
    ])
    expect(ok).toBe(true)
    expect(calls).toHaveLength(1)
    expect(calls[0].messages).toHaveLength(1)
  })

  it('serializes concurrent extractSessionFromLog calls to avoid double extraction', async () => {
    const workspace = makeWorkspace('quaid-timeout-serialize-')
    writeFailHardConfig(workspace, false)
    const calls: Array<{ messages: any[]; sessionId?: string; label?: string }> = []
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label })
        await new Promise((resolve) => setTimeout(resolve, 15))
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    })
    ;(manager as any).failHard = false

    manager.onAgentEnd([
      { role: 'user', content: 'remember this once', timestamp: Date.now() },
    ], 'session-serialize')

    const [first, second] = await Promise.all([
      manager.extractSessionFromLog('session-serialize', 'Reset'),
      manager.extractSessionFromLog('session-serialize', 'Reset'),
    ])

    expect(first).toBe(true)
    expect(second).toBe(false)
    expect(calls).toHaveLength(1)
  })

  it('bounds in-memory buffers to avoid unbounded session growth', () => {
    const workspace = makeWorkspace('quaid-timeout-buffer-cap-')
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    for (let i = 0; i < 240; i += 1) {
      manager.onAgentEnd(
        [{ role: 'user', content: `message ${i}`, timestamp: Date.now() + i }],
        `session-${i}`,
      )
    }

    const buffers: Map<string, unknown[]> = (manager as any).buffers
    expect(buffers.size).toBeLessThanOrEqual(200)
  })

  it('caches failHard config reads between extraction calls', async () => {
    const workspace = makeWorkspace('quaid-timeout-failhard-cache-')
    writeFailHardConfig(workspace, false)

    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async () => {},
      isBootstrapOnly: () => false,
      logger: () => {},
    })

    const payload = [{ role: 'user', content: 'remember this', timestamp: Date.now() }]
    const firstOk = await manager.extractSessionFromLog('session-cache-1', 'Reset', payload)
    expect(firstOk).toBe(true)

    // Flip config immediately; second call should use cached failHard state.
    writeFailHardConfig(workspace, true)
    const secondOk = await manager.extractSessionFromLog('session-cache-2', 'Reset', payload)

    expect(secondOk).toBe(true)
  })
})
