/**
 * Unit tests for the browser settings controller (`src/client/`).
 *
 * The controller is plain TS (no DOM), so it runs in the node vitest env. It
 * bridges the `atom-memory` settings scope (features 1 & 2) and the Remote
 * operations (features 3-5) onto a snapshot store for the settings panel.
 */
import { describe, expect, it, vi } from 'vitest'
import type { SettingsScope, SettingsScopeSnapshot } from '@deepseek-ai/dsh-client-ui-settings/client'
import {
  MemorySettingsController,
  type MemorySettingsSection,
} from '../src/client/memory-settings-controller.ts'

function snapshot(over: Partial<SettingsScopeSnapshot<MemorySettingsSection>>): SettingsScopeSnapshot<MemorySettingsSection> {
  return {
    status: 'ready',
    value: {
      enabled: true,
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      extractionModel: undefined,
      ...(over.value as Partial<MemorySettingsSection> | undefined),
    },
    base: undefined,
    user: undefined,
    revision: 1,
    writable: true,
    mode: 'host',
    ...over,
  }
}

function fakeScope(initial: SettingsScopeSnapshot<MemorySettingsSection>) {
  const set = vi.fn(async (_field: string, _value: unknown) => {})
  const unset = vi.fn(async (_field: string) => {})
  const scope: SettingsScope<MemorySettingsSection> = {
    getSnapshot: () => initial,
    subscribe: () => () => {},
    mutate: vi.fn(async () => {}),
    set,
    unset,
  }
  return { scope, set, unset }
}

function fakeRemote() {
  const backup = vi.fn(async () => ({ version: 1, facts: [], profile: [] }))
  const restore = vi.fn(async () => ({ facts_written: 2, profile_written: 1 }))
  const listFacts = vi.fn(async (): Promise<{ facts: Array<{ fact_id: string; subject: string; predicate: string; object: string }>; total: number }> => ({ facts: [], total: 0 }))
  const listProfile = vi.fn(async () => ({ profile: [] }))
  const remote = { listFacts, editFact: vi.fn(), listProfile, upsertProfile: vi.fn(), deleteProfile: vi.fn(), backup, restore }
  return { remote, backup, restore, listFacts, listProfile }
}

describe('MemorySettingsController', () => {
  it('publishes the initial settings snapshot into the store', () => {
    const { scope } = fakeScope(snapshot({ value: { enabled: false } as MemorySettingsSection }))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    const face = controller.inject()
    const state = face.hooks.memorySettings.getSnapshot()
    expect(state.available).toBe(true)
    expect(state.section.enabled).toBe(false)
    expect(state.loading).toBe(false)
  })

  it('exposes top-level face actions alongside the memorySettings hook', () => {
    const { scope } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    const face = controller.inject()
    // InjectFace maps hooks -> useX, other members pass through.
    expect(typeof face.setEnabled).toBe('function')
    expect(typeof face.setExtractionModel).toBe('function')
    expect(typeof face.backup).toBe('function')
    expect(typeof face.restore).toBe('function')
    expect(typeof face.hooks.memorySettings.getSnapshot).toBe('function')
  })

  it('routes setEnabled through the settings scope', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    await controller.inject().setEnabled(false)
    expect(set).toHaveBeenCalledWith('enabled', false)
  })

  it('routes the extraction model override through the settings scope', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    await controller.inject().setExtractionModel('deepseek', 'deepseek-chat')
    expect(set).toHaveBeenCalledWith('extractionModel', { provider: 'deepseek', model: 'deepseek-chat' })
  })

  it('calls the Remote namespace for backup and restore', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, backup, restore } = fakeRemote()
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    const face = controller.inject()
    const exported = await face.backup()
    expect(backup).toHaveBeenCalledWith({ user: 'global' })
    expect(exported.version).toBe(1)
    const result = await face.restore({ version: 1, facts: [], profile: [] })
    expect(restore).toHaveBeenCalledWith({ user: 'global', payload: { version: 1, facts: [], profile: [] } })
    expect(result.facts_written).toBe(2)
    expect(result.profile_written).toBe(1)
  })

  it('refreshes dynamic data through the Remote list calls', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts, listProfile } = fakeRemote()
    listFacts.mockResolvedValue({
      facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }],
      total: 1,
    })
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    expect(listFacts).toHaveBeenCalledWith({ user: 'global', limit: 200 })
    expect(listProfile).toHaveBeenCalledWith({ user: 'global' })
    expect(controller.inject().hooks.memorySettings.getSnapshot().data.facts[0]?.fact_id).toBe('f1')
  })
})
