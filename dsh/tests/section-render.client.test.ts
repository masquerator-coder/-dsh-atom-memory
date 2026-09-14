/**
 * Client render test for the settings section (jsdom).
 *
 * @vitest-environment jsdom
 *
 * Reproduces the LIVE render path the ui-renderer uses for a `settings.section`
 * entry: hooks are bound with the real uSES selector hook (matching
 * `bindInjectSources` → `observableHook` → `bindSnapshotSelector`), `t` is bound
 * through a LocaleFace, and the component is rendered client-side (so effects
 * run and real `useSyncExternalStoreWithSelector` without a server snapshot is
 * used). Any render/effect crash that slots' SlotErrorBoundary would otherwise
 * swallow into a blank panel surfaces here.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { createElement } from 'react'
import { useSyncExternalStoreWithSelector } from 'use-sync-external-store/shim/with-selector'
import { MemorySettingsController } from '../src/client/memory-settings-controller.ts'
import { MemorySettingsSection } from '../src/client/MemorySettingsSection.tsx'
import { dicts, LOCALE_NS } from '../src/client/locales.ts'

const zh = dicts.zh

/** Real observer hook construction, matching bindSnapshotSelector. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function useSnapshotHook<T>(store: { getSnapshot(): T; subscribe(fn: () => void): () => void }) {
  const subscribe = (fn: () => void) => store.subscribe(fn)
  const getSnapshot = () => store.getSnapshot()
  return function useSelector<S>(select: (s: T) => S, equal?: (a: S, b: S) => boolean): S {
    return useSyncExternalStoreWithSelector(subscribe, getSnapshot, undefined, select, equal)
  }
}

function buildController() {
  const scope = {
    getSnapshot: () => ({
      status: 'ready' as const,
      value: {
        enabled: true, captureEnabled: true, llmExtractionEnabled: true,
        contextInjectionEnabled: true, extractionModel: undefined,
      },
      base: undefined, user: undefined, revision: 1, writable: true, mode: 'host' as const,
    }),
    subscribe: () => () => {},
    set: async () => {}, unset: async () => {}, mutate: async () => {},
  }
  const remote = {
    listFacts: async () => ({ ok: true, value: { facts: [], total: 0 } }),
    editFact: async () => ({ ok: true, value: {} }),
    deleteFact: async () => ({ ok: true, value: {} }),
    memoryMd: async () => ({ ok: true, value: '# memory.md\ntest' }),
    listProfile: async () => ({ ok: true, value: { profile: [] } }),
    upsertProfile: async () => ({ ok: true, value: {} }),
    deleteProfile: async () => ({ ok: true, value: {} }),
    backup: async () => ({ ok: true, value: { version: 1, facts: [], profile: [] } }),
    restore: async () => ({ ok: true, value: { facts_written: 0, profile_written: 0 } }),
  }
  return new MemorySettingsController(scope as never, remote as never)
}

const refreshData = vi.fn(async () => {})
const setEnabled = vi.fn(async () => {})

afterEach(cleanup)

describe('MemorySettingsSection client render', () => {
  it('renders and runs effects without throwing', async () => {
    const controller = buildController()
    const face = controller.inject()
    const hooks = face.hooks as { memorySettings: { getSnapshot(): unknown; subscribe(fn: () => void): () => void } }
    const useMemorySettings = useSnapshotHook(hooks.memorySettings)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const t = (key: string, params?: Record<string, unknown>) => {
      const tmpl = (zh as Record<string, string | undefined>)[key]
      if (tmpl === undefined) throw new Error(`missing locale key: ${key}`)
      if (!params) return tmpl
      return tmpl.replace(/\{(\w+)\}/g, (_s, k) => String((params as Record<string, unknown>)[k]))
    }
    const props = {
      t: t as never,
      useMemorySettings: useMemorySettings as never,
      setEnabled, refreshData, setExtractionModel: face.setExtractionModel,
      saveFact: face.saveFact, deleteFact: face.deleteFact, fetchMemoryMd: face.fetchMemoryMd,
      upsertProfile: face.upsertProfile,
      deleteProfile: face.deleteProfile, backup: face.backup, restore: face.restore,
      close: () => {},
    }
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    expect(screen.getByText('记忆')).toBeTruthy()
    expect(screen.getByText('LLM 抽取模型')).toBeTruthy()
    expect(screen.getByText('查看 memory.md')).toBeTruthy()
    expect(refreshData).toHaveBeenCalled()
    expect(screen.queryByText(LOCALE_NS + ':title')).toBeNull()
  })
})
