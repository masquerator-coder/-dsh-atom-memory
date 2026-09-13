import { describe, it, expect } from 'vitest'
import { Config } from '../src/config.ts'

describe('Config defaults', () => {
  it('defaults extractionMaxTokens to a budget that fits a knowledge body', () => {
    // 600 (the old hard-coded value) truncated long knowledge; a truncated
    // extraction is discarded rather than persisted, losing the fact silently.
    expect(Config({}).extractionMaxTokens).toBe(2048)
  })

  it('keeps snapshot injection on and memory.md budget at 1500 by default', () => {
    const c = Config({})
    expect(c.contextInjectionEnabled).toBe(true)
    expect(c.memoryMdTokens).toBe(1500)
  })
})
