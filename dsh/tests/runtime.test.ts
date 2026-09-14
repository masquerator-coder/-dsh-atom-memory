import { describe, it, expect, vi } from 'vitest'
import { createRuntime, Runtime } from '../src/runtime.ts'

describe('createRuntime', () => {
  it('applies defaults when the seed is empty', () => {
    const rt = createRuntime({})
    expect(rt).toMatchObject({
      enabled: true,
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
    })
  })

  it('carries an extractionModel override when provided', () => {
    const rt = createRuntime({ extractionModel: { provider: 'p', model: 'm' } })
    expect(rt.extractionModel).toEqual({ provider: 'p', model: 'm' })
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
})
