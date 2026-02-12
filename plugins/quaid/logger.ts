/**
 * Structured JSONL Logger for Memory System
 * 
 * Provides structured logging with:
 * - JSONL format (one JSON object per line)
 * - Log rotation with configurable retention
 * - Queryable with jq
 * - Console output for errors/warnings
 */

import { appendFileSync, mkdirSync, existsSync, renameSync, readdirSync, unlinkSync, statSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { homedir } from 'os'

const LOG_DIR = join(homedir(), 'clawd', 'logs')
const ARCHIVE_DIR = join(LOG_DIR, 'archive')
const MAX_LOG_DAYS = 7

// Ensure directories exist on module load
try {
  if (!existsSync(LOG_DIR)) { mkdirSync(LOG_DIR, { recursive: true }) }
  if (!existsSync(ARCHIVE_DIR)) { mkdirSync(ARCHIVE_DIR, { recursive: true }) }
} catch (e: unknown) {
  console.error('[logger] Failed to create log directories:', String(e))
}

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'

export interface LogEntry {
  ts: string
  level: LogLevel
  component: string
  event: string
  [key: string]: unknown
}

interface LogOptions {
  component: string
  event: string
  level?: LogLevel
  [key: string]: unknown
}

/**
 * Write a structured log entry
 */
export function log(options: LogOptions): void {
  const { component, event, level = 'info', ...rest } = options

  const entry: LogEntry = {
    ts: new Date().toISOString(),
    level,
    component,
    event,
    ...rest
  }

  const line = JSON.stringify(entry) + '\n'
  const logFile = join(LOG_DIR, `${component}.log`)

  try {
    appendFileSync(logFile, line)
  } catch (e: unknown) {
    console.error(`[logger] Failed to write to ${logFile}:`, String(e))
  }

  // Also print errors/warnings to console
  if (level === 'error') {
    console.error(`[${component}] ERROR: ${event}`, rest)
  } else if (level === 'warn') {
    console.warn(`[${component}] WARN: ${event}`, rest)
  }
}

/**
 * Convenience logger object with level methods
 */
export const logger = {
  debug: (component: string, event: string, data?: Record<string, unknown>) =>
    log({ component, event, level: 'debug', ...data }),

  info: (component: string, event: string, data?: Record<string, unknown>) =>
    log({ component, event, level: 'info', ...data }),

  warn: (component: string, event: string, data?: Record<string, unknown>) =>
    log({ component, event, level: 'warn', ...data }),

  error: (component: string, event: string, data?: Record<string, unknown>) =>
    log({ component, event, level: 'error', ...data })
}

/**
 * Rotate logs - moves current logs to archive with date suffix
 */
export function rotateLogs(): void {
  const today = new Date().toISOString().split('T')[0]

  try {
    const files = readdirSync(LOG_DIR)
    
    for (const file of files) {
      if (!file.endsWith('.log')) { continue }

      const logPath = join(LOG_DIR, file)
      const baseName = file.replace('.log', '')
      const archivePath = join(ARCHIVE_DIR, `${baseName}.${today}.log`)

      try {
        // Check if file has content
        const stats = statSync(logPath)
        if (stats.size === 0) { continue }

        // Don't rotate if already archived today
        if (existsSync(archivePath)) {
          // Append to existing archive
          const content = readFileSync(logPath, 'utf-8')
          appendFileSync(archivePath, content)
          writeFileSync(logPath, '')  // Clear current log
        } else {
          renameSync(logPath, archivePath)
        }
        
        console.log(`[logger] Rotated ${file} to archive`)
      } catch (e: unknown) {
        console.error(`[logger] Failed to rotate ${file}:`, String(e))
      }
    }

    // Clean old archives
    cleanOldArchives()
  } catch (e: unknown) {
    console.error('[logger] Log rotation failed:', String(e))
  }
}

/**
 * Delete archives older than MAX_LOG_DAYS
 */
function cleanOldArchives(): void {
  const cutoff = Date.now() - (MAX_LOG_DAYS * 24 * 60 * 60 * 1000)

  try {
    const files = readdirSync(ARCHIVE_DIR)
    
    for (const file of files) {
      const match = file.match(/\.(\d{4}-\d{2}-\d{2})\.log$/)
      if (!match) { continue }

      const fileDate = new Date(match[1]).getTime()
      if (fileDate < cutoff) {
        unlinkSync(join(ARCHIVE_DIR, file))
        console.log(`[logger] Deleted old archive ${file}`)
      }
    }
  } catch (e: unknown) {
    console.error('[logger] Failed to clean old archives:', String(e))
  }
}

/**
 * Get log file path for a component
 */
export function getLogPath(component: string): string {
  return join(LOG_DIR, `${component}.log`)
}

/**
 * Get archive directory path
 */
export function getArchiveDir(): string {
  return ARCHIVE_DIR
}

// Export default logger
export default logger
