#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { writeFile, appendFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'

const LOGS_DIR = process.env.CLAWDBOT_WORKSPACE
  ? path.join(process.env.CLAWDBOT_WORKSPACE, 'logs')
  : path.join(path.resolve(process.cwd(), '../..'), 'logs')
const LOG_FILE = path.join(LOGS_DIR, 'test-results.jsonl')

async function runTests(vitestArgs = []) {
  const startTime = Date.now()
  
  console.log('🧪 Running quaid tests...')
  
  return new Promise((resolve, reject) => {
    const cmd = ['vitest', 'run', '--reporter=verbose', ...vitestArgs]
    const child = spawn('npx', cmd, {
      stdio: ['inherit', 'pipe', 'pipe'],
      cwd: process.cwd()
    })
    
    let stdout = ''
    let stderr = ''
    
    child.stdout.on('data', (data) => {
      const output = data.toString()
      stdout += output
      process.stdout.write(output) // Still show output to console
    })
    
    child.stderr.on('data', (data) => {
      const output = data.toString()
      stderr += output
      process.stderr.write(output) // Still show errors to console
    })
    
    child.on('close', async (code) => {
      const endTime = Date.now()
      const duration = endTime - startTime
      
      try {
        const results = parseTestResults(stdout, stderr, code, duration)
        await logResults(results)
        
        console.log(`\n📊 Test Results Summary:`)
        console.log(`   Total: ${results.total}`)
        console.log(`   Passed: ${results.passed}`)
        console.log(`   Failed: ${results.failed}`)
        console.log(`   Duration: ${results.duration}ms`)
        console.log(`   Results logged to: ${LOG_FILE}`)
        
        resolve(code)
      } catch (error) {
        console.error('❌ Failed to log test results:', error)
        reject(error)
      }
    })
    
    child.on('error', reject)
  })
}

function parseTestResults(stdout, stderr, exitCode, duration) {
  const timestamp = new Date().toISOString()
  let passed = 0
  let failed = 0
  let total = 0
  const failures = []
  
  // Strip ANSI escape codes for easier parsing
  const cleanOutput = stdout.replace(/\x1B\[[0-9;]*[a-zA-Z]/g, '')
  
  // Parse Vitest output for test counts
  // Look for lines like: "Tests  140 passed (140)" or "Tests  140 passed"
  const testSummaryMatch = cleanOutput.match(/Tests\s+(\d+)\s+passed(?:\s*\((\d+)\))?/)
  if (testSummaryMatch) {
    passed = parseInt(testSummaryMatch[1], 10)
    total = testSummaryMatch[2] ? parseInt(testSummaryMatch[2], 10) : passed
  }
  
  // Look for failed tests pattern: "Tests  X failed | Y passed (Z)"
  const failedTestMatch = cleanOutput.match(/Tests\s+(\d+)\s+failed\s*\|\s*(\d+)\s+passed(?:\s*\((\d+)\))?/)
  if (failedTestMatch) {
    failed = parseInt(failedTestMatch[1], 10)
    passed = parseInt(failedTestMatch[2], 10)
    total = failedTestMatch[3] ? parseInt(failedTestMatch[3], 10) : (failed + passed)
  }
  
  // Also check for just passed count in format "140 passed"
  if (total === 0) {
    const simpleMatch = cleanOutput.match(/(\d+)\s+passed/)
    if (simpleMatch) {
      passed = parseInt(simpleMatch[1], 10)
      total = passed
    }
  }
  
  // Extract failed test names if any
  if (failed > 0) {
    // Look for failure patterns in output
    const failurePattern = /FAIL\s+(.+?)(?:\n|\r)/g
    let match
    while ((match = failurePattern.exec(stdout)) !== null) {
      failures.push(match[1].trim())
    }
    
    // Alternative pattern for test names
    const testFailPattern = /✗\s+(.+?)(?:\n|\r)/g
    while ((match = testFailPattern.exec(stdout)) !== null) {
      failures.push(match[1].trim())
    }
  }
  
  // Fallback: if we can't parse, but exit code indicates failure
  if (exitCode !== 0 && failed === 0 && passed === 0) {
    failed = 1
    total = 1
    failures.push('Unknown test failure - check logs')
  }
  
  return {
    timestamp,
    passed,
    failed,
    total,
    duration,
    success: exitCode === 0,
    failures
  }
}

async function logResults(results) {
  const logEntry = JSON.stringify({
    timestamp: results.timestamp,
    passed: results.passed,
    failed: results.failed,
    total: results.total,
    duration: results.duration,
    success: results.success,
    failures: results.failures,
    source: 'quaid'
  }) + '\n'
  
  try {
    // Append to JSONL file
    await appendFile(LOG_FILE, logEntry)
  } catch (error) {
    // If append fails, try to create the file
    if (error.code === 'ENOENT') {
      await writeFile(LOG_FILE, logEntry)
    } else {
      throw error
    }
  }
}

// Handle command line arguments
const args = process.argv.slice(2)
if (args.length > 0) {
  // If arguments provided, pass them to vitest (e.g., specific test files)
  console.log(`🧪 Running specific tests: ${args.join(' ')}`)
}

runTests(args)
  .then((exitCode) => {
    process.exit(exitCode)
  })
  .catch((error) => {
    console.error('❌ Test runner failed:', error)
    process.exit(1)
  })
