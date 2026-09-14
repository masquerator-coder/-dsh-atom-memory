import { describe, it, expect, vi } from 'vitest'
import { createRuntime, Runtime } from '../src/runtime.ts'
import {
  DEFAULT_INJECTED_MD_TOKENS,
  MAX_INJECTED_MD_TOKENS,
  MIN_INJECTED_MD_TOKENS,
  clampInjectedMdTokens,
} from '../src/injection-budget.ts'

describe('createRuntime', () => {
  it('applies defaults when the seed is empty', () => {
    const rt = createRuntime({})
    expect(rt).toMatchObject({
      enabled: true,
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedMemoryMdTokens: DEFAULT_INJECTED_MD_TOKENS,
    })
  })

  it('carries an extractionModel override when provided', () => {
    const rt = createRuntime({ extractionModel: { provider: 'p', model: 'm' } })
    expect(rt.extractionModel).toEqual({ provider: 'p', model: 'm' })
  })

  it('carries a configured injection budget, clamped', () => {
    expect(createRuntime({ injectedMemoryMdTokens: 1200 }).injectedMemoryMdTokens).toBe(1200)
    expect(createRuntime({ injectedMemoryMdTokens: 0 }).injectedMemoryMdTokens)
      .toBe(MIN_INJECTED_MD_TOKENS)
  })
})

describe('clampInjectedMdTokens', () => {
  it('keeps an in-range integer as-is', () => {
    expect(clampInjectedMdTokens(800)).toBe(800)
    expect(clampInjectedMdTokens(1200.7)).toBe(1200)
  })

  it('snaps out-of-range values to the nearest bound', () => {
    expect(clampInjectedMdTokens(MIN_INJECTED_MD_TOKENS - 1)).toBe(MIN_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(0)).toBe(MIN_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(-100)).toBe(MIN_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(MAX_INJECTED_MD_TOKENS + 1)).toBe(MAX_INJECTED_MD_TOKENS)
  })

  it('falls back to the default for anything unusable', () => {
    // A malformed settings document must not break prompt assembly.
    expect(clampInjectedMdTokens(undefined)).toBe(DEFAULT_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(null)).toBe(DEFAULT_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens('abc')).toBe(DEFAULT_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(Number.NaN)).toBe(DEFAULT_INJECTED_MD_TOKENS)
    expect(clampInjectedMdTokens(Number.POSITIVE_INFINITY)).toBe(DEFAULT_INJECTED_MD_TOKENS)
  })

  it('accepts a numeric string (a text field submits strings)', () => {
    expect(clampInjectedMdTokens('900')).toBe(900)
  })
})

describe('Runtime', () => {
  it('reads back an internally stable snapshot', () => {
    const runtime = new Runtime(createRuntime({ enabled: true }))
    expect(runtime.isEnabled()).toBe(true)
    expect(runtime.get().enabled).toBe(true)
  })

  it('notifies listeners only when the live flags actually change', () => {
    const runtime = new Runtime(createRuntime({ enabled: true, captureEnabled: true }))
    const listener = vi.fn()
    const off = runtime.subscribe(listener)

    runtime.set({ ...runtime.get(), enabled: false })
    expect(listener).toHaveBeenCalledTimes(1)

    // Same flag value again -> no notification.
    runtime.set({ ...runtime.get(), extractionModel: { provider: 'p', model: 'm' } })
    expect(listener).toHaveBeenCalledTimes(1)

    off()
    runtime.set({ ...runtime.get(), captureEnabled: false })
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('reports disabled after a toggle', () => {
    const runtime = new Runtime(createRuntime({ enabled: true }))
    runtime.set({ ...runtime.get(), enabled: false })
    expect(runtime.isEnabled()).toBe(false)
  })

  it('keeps the injection budget out of the change notification', () => {
    // Nothing re-wires on a budget change: it is read when a snapshot freezes,
    // so notifying listeners would only cause pointless work.
    const runtime = new Runtime(createRuntime({}))
    const listener = vi.fn()
    runtime.subscribe(listener)
    runtime.set({ ...runtime.get(), injectedMemoryMdTokens: 300 })
    expect(runtime.get().injectedMemoryMdTokens).toBe(300)
    expect(listener).not.toHaveBeenCalled()
  })

  it('clamps a budget written straight through set()', () => {
    const runtime = new Runtime(createRuntime({}))
    runtime.set({ ...runtime.get(), injectedMemoryMdTokens: Number.NaN })
    expect(runtime.get().injectedMemoryMdTokens).toBe(DEFAULT_INJECTED_MD_TOKENS)
  })
})
