/**
 * Render smoke test for the settings section component (node env).
 *
 * Rebuilds the exact props the ui-renderer hands a registered section:
 *   - `t` locale seat
 *   - `useMemorySettings` selector hook bound over the controller store
 *   - top-level action props (setEnabled / refreshData / etc.)
 * and renders to a string with react-dom/server to surface any render-time
 * crash that SlotErrorBoundary would otherwise swallow into a blank panel.
 */
import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { useSyncExternalStoreWithSelector } from 'use-sync-external-store/shim/with-selector'
import { MemorySettingsController } from '../src/client/memory-settings-controller.ts'
import { MemorySettingsSection } from '../src/client/MemorySettingsSection.tsx'
import { dicts } from '../src/client/locales.ts'
import { DEFAULT_INJECTED_SUMMARY_TOKENS } from '../src/injection-budget.ts'

const zh = dicts.zh

/** The REAL renderer binding (bindSnapshotSelector): a uSES selector hook. */
function selectorHook<T>(store: { getSnapshot(): T; subscribe(fn: () => void): () => void }) {
  const subscribe = (fn: () => void) => store.subscribe(fn)
  const getSnapshot = () => store.getSnapshot()
  return function useSelector<S>(select: (s: T) => S, equal?: (a: S, b: S) => boolean): S {
    return useSyncExternalStoreWithSelector(subscribe, getSnapshot, getSnapshot, select, equal)
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function buildProps(): Record<string, any> {
  const scope = {
    getSnapshot: () => ({
      status: 'ready' as const,
      value: {
        enabled: true, captureEnabled: true, llmExtractionEnabled: true,
        contextInjectionEnabled: true, extractionModel: undefined,
        injectedSummaryTokens: DEFAULT_INJECTED_SUMMARY_TOKENS,
      },
      base: undefined, user: undefined, revision: 1, writable: true, mode: 'host' as const,
    }),
    subscribe: () => () => {},
    set: async () => {}, unset: async () => {}, mutate: async () => {},
  }
  const remote = {
    listFacts: async () => ({ ok: true, value: { facts: [], total: 0 } }),
    editFact: async () => ({ ok: true, value: {} }),
    listProfile: async () => ({ ok: true, value: { profile: [] } }),
    upsertProfile: async () => ({ ok: true, value: {} }),
    deleteProfile: async () => ({ ok: true, value: {} }),
    backup: async () => ({ ok: true, value: { version: 1, facts: [], profile: [] } }),
    restore: async () => ({ ok: true, value: { facts_written: 0, profile_written: 0 } }),
  }
  const controller = new MemorySettingsController(scope as never, remote as never)
  const face = controller.inject()
  const hooks = face.hooks as { memorySettings: { getSnapshot(): unknown; subscribe(fn: () => void): () => void } }
  // Forward the face verbatim, mirroring the renderer's InjectFace contract
  // (hooks -> use<Name>, every other member -> a prop of the same name). A
  // hand-written list silently goes stale whenever a face action is added, and
  // the missing prop only shows up as a crash at the call site.
  const { hooks: _hooks, ...actions } = face
  const t = (key: string, params?: Record<string, unknown>) => {
    const tmpl = (zh as Record<string, string | undefined>)[key]
    if (tmpl === undefined) throw new Error(`missing locale key: ${key}`)
    if (!params) return tmpl
    return tmpl.replace(/\{(\w+)\}/g, (_s, k) => String((params as Record<string, unknown>)[k]))
  }
  return {
    ...actions,
    t,
    useMemorySettings: selectorHook(hooks.memorySettings),
    close: () => {},
  }
}

describe('MemorySettingsSection render smoke', () => {
  it('renders to markup without throwing', () => {
    const props = buildProps()
    expect(() => {
      const html = renderToStaticMarkup(createElement(MemorySettingsSection, props as Parameters<typeof MemorySettingsSection>[0]))
      expect(html).toContain('atom-memory-section')
      expect(html).toContain('记忆')
      // The injection budget renders as a gear slider, named for the system
      // prompt it sizes.
      expect(html).toContain('系统提示词注入体积（记忆摘要）')
      expect(html).toContain('type="range"')
    }).not.toThrow()
  })
})
