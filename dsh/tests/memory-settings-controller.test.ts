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
import {
  DEFAULT_INJECTED_MD_TOKENS,
  MAX_INJECTED_MD_TOKENS,
  MIN_INJECTED_MD_TOKENS,
} from '../src/injection-budget.ts'

function snapshot(over: Partial<SettingsScopeSnapshot<MemorySettingsSection>>): SettingsScopeSnapshot<MemorySettingsSection> {
  return {
    status: 'ready',
    value: {
      enabled: true,
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedMemoryMdTokens: DEFAULT_INJECTED_MD_TOKENS,
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
  const backup = vi.fn(async () => ({ ok: true, value: { version: 1, facts: [], profile: [] } }))
  const restore = vi.fn(async () => ({ ok: true, value: { facts_written: 2, profile_written: 1 } }))
  const listFacts = vi.fn(async (): Promise<{ ok: boolean; value: { facts: Array<{ fact_id: string; subject: string; predicate: string; object: string }>; total: number } }> => ({ ok: true, value: { facts: [], total: 0 } }))
  const listProfile = vi.fn(async () => ({ ok: true, value: { profile: [] } }))
  const deleteFact = vi.fn(async () => ({ ok: true, value: {} }))
  const memoryMd = vi.fn(async () => ({ ok: true, value: '# memory.md\ntest' }))
  const remote: Record<string, unknown> = { listFacts, editFact: vi.fn(async () => ({ ok: true, value: {} })), deleteFact, memoryMd, listProfile, upsertProfile: vi.fn(async () => ({ ok: true, value: {} })), deleteProfile: vi.fn(async () => ({ ok: true, value: {} })), backup, restore }
  return { remote, backup, restore, listFacts, listProfile, deleteFact, memoryMd }
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

  it('routes the full extraction-model override (provider/model/baseURL/protocol/apiKey)', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    await controller.inject().setExtractionModelOverride({
      provider: 'custom', model: 'gpt-4o-mini',
      baseURL: 'https://api.example.com/v1', protocol: 'openai', apiKey: 'sk-test',
    })
    expect(set).toHaveBeenCalledWith('extractionModel', {
      provider: 'custom', model: 'gpt-4o-mini',
      baseURL: 'https://api.example.com/v1', protocol: 'openai', apiKey: 'sk-test',
    })
  })

  it('routes the injection budget through the settings scope, clamped', async () => {
    const { scope, set } = fakeScope(snapshot({}))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    const face = controller.inject()

    await face.setInjectedMemoryMdTokens(1200)
    expect(set).toHaveBeenCalledWith('injectedMemoryMdTokens', 1200)

    // The panel may hand over anything a text field produced; the controller is
    // the last line of defence before the Host (which clamps again).
    await face.setInjectedMemoryMdTokens(Number.NaN)
    expect(set).toHaveBeenCalledWith('injectedMemoryMdTokens', DEFAULT_INJECTED_MD_TOKENS)
    await face.setInjectedMemoryMdTokens(-5)
    expect(set).toHaveBeenCalledWith('injectedMemoryMdTokens', MIN_INJECTED_MD_TOKENS)
    await face.setInjectedMemoryMdTokens(9_999_999)
    expect(set).toHaveBeenCalledWith('injectedMemoryMdTokens', MAX_INJECTED_MD_TOKENS)
  })

  it('defaults a missing injection budget instead of exposing undefined', () => {
    const { scope } = fakeScope(snapshot({ value: { injectedMemoryMdTokens: undefined } as never }))
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, [])
    expect(controller.inject().hooks.memorySettings.getSnapshot().section.injectedMemoryMdTokens)
      .toBe(DEFAULT_INJECTED_MD_TOKENS)
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
      ok: true,
      value: { facts: [{ fact_id: 'f1', subject: 's', predicate: 'p', object: 'o' }], total: 1 },
    })
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    expect(listFacts).toHaveBeenCalledWith({ user: 'global', limit: 200 })
    expect(listProfile).toHaveBeenCalledWith({ user: 'global' })
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    // Regression: the wire shape is `{ok, value}` — data must hold the raw
    // arrays (never undefined), otherwise `state.data.profile.length` throws
    // and the settings section blanks.
    expect(Array.isArray(snap.data.facts)).toBe(true)
    expect(Array.isArray(snap.data.profile)).toBe(true)
    expect(snap.data.facts[0]?.fact_id).toBe('f1')
  })

  it('normalizes malformed remote list results to empty arrays (no blank-section crash)', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, listFacts, listProfile } = fakeRemote()
    listFacts.mockResolvedValue({ ok: true, value: { facts: undefined, total: 0 } } as never)
    listProfile.mockResolvedValue({ ok: true, value: { profile: undefined } } as never)
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().refreshData()
    const snap = controller.inject().hooks.memorySettings.getSnapshot()
    expect(snap.data.facts).toEqual([])
    expect(snap.data.profile).toEqual([])
  })

  it('deletes a fact through the Remote namespace and refreshes', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, deleteFact } = fakeRemote()
    const listFacts = (remote.listFacts as ReturnType<typeof vi.fn>)
    listFacts.mockResolvedValue({
      ok: true,
      value: { facts: [], total: 0 },
    })
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().deleteFact('f1')
    expect(deleteFact).toHaveBeenCalledWith({ user: 'global', fact_id: 'f1' })
  })

  it('fetches and stores the rendered memory.md view', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote, memoryMd } = fakeRemote()
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    const face = controller.inject()
    const text = await face.fetchMemoryMd()
    expect(memoryMd).toHaveBeenCalledWith({ user: 'global' })
    expect(text).toBe('# memory.md\ntest')
    expect(face.hooks.memorySettings.getSnapshot().data.memoryMd).toBe('# memory.md\ntest')
  })

  it('batch-saves a facts table: edits non-deleted rows, deletes marked rows, one refresh', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const editFact = remote.editFact as ReturnType<typeof vi.fn>
    const deleteFact = remote.deleteFact as ReturnType<typeof vi.fn>
    const listFacts = remote.listFacts as ReturnType<typeof vi.fn>
    listFacts.mockResolvedValue({ ok: true, value: { facts: [], total: 0 } })
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().saveAllFacts([
      { fact_id: 'f1', subject: 'a', predicate: 'b', object: 'c', deleted: false },
      { fact_id: 'f2', subject: 'x', predicate: 'y', object: 'z', deleted: true },
    ])
    expect(editFact).toHaveBeenCalledWith(expect.objectContaining({ fact_id: 'f1' }))
    expect(deleteFact).toHaveBeenCalledWith({ user: 'global', fact_id: 'f2' })
    // After the batch, the panel refreshes (so one listFacts read happens again).
    expect(listFacts.mock.calls.length).toBeGreaterThan(0)
  })

  it('batch-saves a profile table: upserts non-deleted rows, deletes marked rows', async () => {
    const { scope } = fakeScope(snapshot({}))
    const { remote } = fakeRemote()
    const upsertProfile = remote.upsertProfile as ReturnType<typeof vi.fn>
    const deleteProfile = remote.deleteProfile as ReturnType<typeof vi.fn>
    const listProfile = remote.listProfile as ReturnType<typeof vi.fn>
    listProfile.mockResolvedValue({ ok: true, value: { profile: [] } })
    const controller = new MemorySettingsController(scope as unknown as SettingsScope<MemorySettingsSection>, remote)
    await controller.inject().saveAllProfile([
      { section: '背景', key: '职业', value: '工程师', deleted: false },
      { section: '偏好', key: '语言', value: 'Python', deleted: true },
    ])
    expect(upsertProfile).toHaveBeenCalledWith({ user: 'global', section: '背景', key: '职业', value: '工程师' })
    expect(deleteProfile).toHaveBeenCalledWith({ user: 'global', section: '偏好', key: '语言' })
    expect(listProfile.mock.calls.length).toBeGreaterThan(0)
  })
})
