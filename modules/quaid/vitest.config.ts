import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      all: true,
      include: ['adaptors/openclaw/**/*.{ts,js}', 'core/**/*.{ts,js}'],
      exclude: ['tests/**/*', 'node_modules/**/*', '__pycache__/**/*', '**/*.d.ts', 'test-runner.js'],
      thresholds: {
        lines: 10,
        statements: 10,
        functions: 10,
        branches: 5
      }
    },
    // Use separate test database
    env: {
      TEST_DB_PATH: process.env.TEST_DB_PATH || '/tmp/test-memory.db',
      MEMORY_DB_PATH: process.env.MEMORY_DB_PATH || process.env.TEST_DB_PATH || '/tmp/test-memory.db'
    },
    // Python-backed memory tests spawn subprocesses; loaded CI runners can
    // exceed 30s per test or setup hook while live-test VMs are active.
    testTimeout: 60000,
    hookTimeout: 60000
  }
})
