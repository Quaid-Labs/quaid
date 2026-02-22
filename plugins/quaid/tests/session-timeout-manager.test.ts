import { describe, it, expect } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { SessionTimeoutManager } from '../core/session-timeout'

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

describe('SessionTimeoutManager scheduling', () => {
  it('coalesces duplicate extraction signals per session', () => {
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
})
