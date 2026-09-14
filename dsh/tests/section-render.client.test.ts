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
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react'
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

function buildController(seedFacts = false) {
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
    listFacts: async () => ({
      ok: true,
      value: seedFacts
        ? { facts: [{ fact_id: 'f1', subject: '张三', predicate: '是', object: '工程师', content: '' }], total: 1 }
        : { facts: [], total: 0 },
    }),
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

function bind(controller: MemorySettingsController) {
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
    setEnabled: async () => {}, refreshData: face.refreshData,
    setExtractionModel: face.setExtractionModel,
    setExtractionModelOverride: face.setExtractionModelOverride,
    saveFact: face.saveFact, deleteFact: face.deleteFact,
    upsertProfile: face.upsertProfile, deleteProfile: face.deleteProfile,
    fetchMemoryMd: face.fetchMemoryMd,
    saveAllFacts: face.saveAllFacts, saveAllProfile: face.saveAllProfile,
    backup: face.backup, restore: face.restore,
    close: () => {},
  }
  return { face, props }
}

const refreshData = vi.fn(async () => {})

afterEach(cleanup)

describe('MemorySettingsSection client render', () => {
  it('renders and runs effects without throwing', async () => {
    const controller = buildController()
    const { face, props } = bind(controller)
    // Override the default refresh stub with an observable spy.
    props.refreshData = refreshData
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    expect(screen.getByText('记忆')).toBeTruthy()
    expect(screen.getByText('LLM 抽取模型')).toBeTruthy()
    expect(screen.getByText('查看 memory.md')).toBeTruthy()
    expect(screen.getByText('编辑记忆')).toBeTruthy()
    expect(screen.getByText('编辑画像')).toBeTruthy()
    expect(refreshData).toHaveBeenCalled()
    expect(screen.queryByText(LOCALE_NS + ':title')).toBeNull()
  })

  it('opens the memory.md view in a modal', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // Not open initially.
    expect(screen.queryByText(/# memory\.md/)).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByText('查看 memory.md'))
    })
    await act(async () => {})
    expect(screen.getByText(/# memory\.md/)).toBeTruthy()
  })

  it('opens the facts editor as an Excel-like table with the saved data', async () => {
    const controller = buildController(true)
    const { props } = bind(controller)
    // Let the real refreshData load the seeded fact into the store.
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    await act(async () => {})
    await act(async () => {
      fireEvent.click(screen.getByText('编辑记忆'))
    })
    await act(async () => {})
    // The seeded fact row's subject is present as an editable cell value.
    const subjectInput = screen.getByDisplayValue('张三')
    expect(subjectInput).toBeTruthy()
    // One save-all button, one 添加一行 button, one 取消 (close) button.
    expect(screen.getAllByText('保存全部').length).toBeGreaterThan(0)
  })

  it('reveals the manual-model parameters (base URL / protocol / API key) when manual is selected', async () => {
    const controller = buildController()
    const { props } = bind(controller)
    await act(async () => {
      render(createElement(MemorySettingsSection, props))
    })
    // Not visible while following the default model.
    expect(screen.queryByText('API 地址 (Base URL)')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByText('手动指定模型'))
    })
    expect(screen.getByText('Provider ID')).toBeTruthy()
    expect(screen.getByText('API 地址 (Base URL)')).toBeTruthy()
    expect(screen.getByText('API 协议')).toBeTruthy()
    expect(screen.getByText('API 密钥')).toBeTruthy()
  })
})
