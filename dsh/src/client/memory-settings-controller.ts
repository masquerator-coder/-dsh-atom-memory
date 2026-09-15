/**
 * Controller bridging the `atom-memory` settings namespace and the Host Remote
 * operations onto a reactive snapshot for the settings panel.
 *
 * Features 1 (master switch) & 2 (extraction model) ride the settings document;
 * features 3-5 (profile, facts editing, backup/restore) ride the Remote gateway
 * (`ctx.remote.atomMemory`). The controller owns no model-visible state — it
 * only stages the panel's drafts and forwards writes.
 *
 * @module dsh-atom-memory/client/memory-settings-controller
 */

import type { SettingsScope, SettingsScopeSnapshot } from '@deepseek-ai/dsh-client-ui-settings/client'
import { createSnapshotStore, type SnapshotStore } from '@deepseek-ai/dsh-client-store'
import {
  DEFAULT_INJECTED_MD_TOKENS,
  clampInjectedMdTokens,
} from '../injection-budget.ts'

/** The live settings section this panel edits (mirrors the Host side). */
export interface MemorySettingsSection {
  enabled: boolean
  captureEnabled: boolean
  llmExtractionEnabled: boolean
  contextInjectionEnabled: boolean
  /** Estimated-token budget for the memory.md snapshot injected into the prompt. */
  injectedMemoryMdTokens: number
  extractionModel?: {
    provider?: string
    model?: string
    /** Custom OpenAI-compatible endpoint base URL (API 地址). */
    baseURL?: string
    /** Wire protocol (only `openai` supported). */
    protocol?: string
    /** API key for a custom endpoint (plaintext). */
    apiKey?: string
  }
}

/** The dynamic facts/profile/backup data the panel fetches via Remote. */
export interface MemoryData {
  facts: Array<{
    fact_id: string
    subject: string
    predicate: string
    object: string
    type?: string
    content?: string
  }>
  profile: Array<{ section: string; key: string; value: string; pinned?: boolean }>
  /** The rendered `memory.md` view (injected system-prompt memory), lazy-loaded. */
  memoryMd?: string
  /** The rendered aggregate memory summary digest, lazy-loaded. */
  summary?: string
}

/** What the panel renders. */
export interface MemorySettingsState {
  /** Whether the settings namespace is served and writable. */
  available: boolean
  loading: boolean
  section: MemorySettingsSection
  /** Dynamic data (facts + profile), fetched lazily. */
  data: MemoryData
  lastError?: string
}

/** The registration-side face the section's slot entry injects.
 *
 * Per `InjectFace`, the `hooks` compartment arrives as `use<Name>` hooks and
 * every other member passes through verbatim as props, so the actions live at
 * the top level (not nested under `actions`).
 */
export interface MemorySettingsFace {
  hooks: {
    /** Section snapshot bound by the renderer as useMemorySettings. */
    memorySettings: SnapshotStore<MemorySettingsState>
  }
  setEnabled: (enabled: boolean) => Promise<void>
  /**
   * Set the injected memory.md token budget.
   *
   * Clamped by the caller (`clampInjectedMdTokens`) before it gets here so the
   * panel and the Host agree on the bounds; the Host clamps again on the way in.
   */
  setInjectedMemoryMdTokens: (tokens: number) => Promise<void>
  setExtractionModel: (provider: string, model: string) => Promise<void>
  /** Write the whole extraction-model override (provider/model/baseURL/protocol/apiKey). */
  setExtractionModelOverride: (override: NonNullable<MemorySettingsSection['extractionModel']>) => Promise<void>
  refreshData: () => Promise<void>
  saveFact: (fact: MemoryData['facts'][number]) => Promise<void>
  deleteFact: (factId: string) => Promise<void>
  fetchMemoryMd: () => Promise<string>
  /** Lazy-load the user's aggregate memory summary digest (see Host `summary`). */
  fetchSummary: () => Promise<string>
  upsertProfile: (section: string, key: string, value: string, pinned: boolean) => Promise<void>
  deleteProfile: (section: string, key: string) => Promise<void>
  /** Batch-save an Excel-style facts table (edit changed rows, delete marked rows) in one pass. */
  saveAllFacts: (rows: FactEditRow[]) => Promise<void>
  /** Batch-save an Excel-style profile table (upsert changed rows, delete marked rows) in one pass. */
  saveAllProfile: (rows: ProfileEditRow[]) => Promise<void>
  backup: () => Promise<Record<string, unknown>>
  restore: (payload: Record<string, unknown>) => Promise<{ facts_written: number; profile_written: number }>
}

/** One facts-table row edited by the Excel-style modal editor. */
export interface FactEditRow {
  fact_id: string
  subject: string
  predicate: string
  object: string
  content?: string
  type?: string
  /** Marked for soft-delete when saving. */
  deleted?: boolean
}

/** One profile-table row edited by the Excel-style modal editor. */
export interface ProfileEditRow {
  section: string
  key: string
  value: string
  /**
   * Pinned rows are frozen against the memory pipeline: derived writes from
   * facts never update or replace them. Only this editor changes a pinned row.
   */
  pinned?: boolean
  /** Marked for deletion when saving. */
  deleted?: boolean
}

/** A minimal view of the wire result the client namespace methods resolve to.
 *
 * The dsh Client Remote `invoke` path returns `RemoteResult<unknown>` —
 * `{ ok: true, value: <method return> }` or `{ ok: false, error }` — NOT the
 * bare method return. Consumers unwrap `.value` (the harness's own idiom, e.g.
 * `settings-scope.ts` reads `response.value`). This interface models that shape.
 */
interface WireResult<T> {
  ok: boolean
  /** The Host method's JSON return when `ok` is true. */
  value: T
  error?: unknown
}

/** Minimal structural shape of the `atom-memory` Remote namespace. */
interface RemoteAtomMemory {
  listFacts(args: { user: string; offset?: number; limit?: number }): Promise<WireResult<{ facts: MemoryData['facts']; total: number }>>
  editFact(args: {
    user: string
    fact_id: string
    subject?: string
    predicate?: string
    object?: string
    content?: string
    type?: string
  }): Promise<WireResult<unknown>>
  deleteFact(args: { user: string; fact_id: string }): Promise<WireResult<unknown>>
  memoryMd(args: { user: string; maxTokens?: number }): Promise<WireResult<string>>
  summary(args: { user: string }): Promise<WireResult<string>>
  listProfile(args: { user: string }): Promise<WireResult<{ profile: MemoryData['profile'] }>>
  upsertProfile(args: { user: string; section: string; key: string; value: string; pinned?: boolean }): Promise<WireResult<unknown>>
  deleteProfile(args: { user: string; section: string; key: string }): Promise<WireResult<unknown>>
  backup(args: { user: string }): Promise<WireResult<Record<string, unknown>>>
  restore(args: { user: string; payload: Record<string, unknown> }): Promise<WireResult<{ facts_written: number; profile_written: number }>>
}

/** Unwrap a `WireResult` to its `.value`, throwing on a failed call. */
function unwrap<T>(result: WireResult<T>): T {
  if (result == null || result.ok === false) {
    const message = result?.error != null
      ? String((result.error as { message?: unknown } | undefined)?.message ?? result.error)
      : 'Remote call failed'
    throw new Error(message)
  }
  return result.value
}

const USER = 'global'

export class MemorySettingsController {
  private readonly store = createSnapshotStore<MemorySettingsState>({
    available: false,
    loading: true,
    section: {
      enabled: true,
      captureEnabled: true,
      llmExtractionEnabled: true,
      contextInjectionEnabled: true,
      injectedMemoryMdTokens: DEFAULT_INJECTED_MD_TOKENS,
      extractionModel: undefined,
    },
    data: { facts: [], profile: [] },
  })
  private readonly unsubscribe: () => void

  constructor(
    private readonly scope: SettingsScope<MemorySettingsSection>,
    private readonly remote: unknown,
  ) {
    this.unsubscribe = scope.subscribe(() => this.publish())
    this.publish()
  }

  /** @returns the face the section's slot registration injects. */
  inject(): MemorySettingsFace {
    return {
      hooks: { memorySettings: this.store },
      setEnabled: (enabled) => this.scope.set('enabled', enabled),
      setInjectedMemoryMdTokens: (tokens) =>
        this.scope.set('injectedMemoryMdTokens', clampInjectedMdTokens(tokens)),
      setExtractionModel: (provider, model) =>
        this.scope.set('extractionModel', { provider, model }),
      setExtractionModelOverride: (override) =>
        this.scope.set('extractionModel', override),
      refreshData: () => this.refreshData(),
      saveFact: (fact) => this.saveFact(fact),
      deleteFact: (factId) => this.deleteFact(factId),
      fetchMemoryMd: () => this.fetchMemoryMd(),
      fetchSummary: () => this.fetchSummary(),
      upsertProfile: (section, key, value, pinned) => this.upsertProfile(section, key, value, pinned),
      deleteProfile: (section, key) => this.deleteProfile(section, key),
      saveAllFacts: (rows) => this.saveAllFacts(rows),
      saveAllProfile: (rows) => this.saveAllProfile(rows),
      backup: () => this.backup(),
      restore: (payload) => this.restore(payload),
    }
  }

  dispose(): void {
    this.unsubscribe()
  }

  private r(): RemoteAtomMemory {
    return this.remote as RemoteAtomMemory
  }

  private publish(): void {
    const snap = this.scope.getSnapshot()
    const value = snap.value
    this.store.set({
      available: snap.status === 'ready' || snap.status === 'loading',
      loading: snap.status === 'loading',
      section: value === undefined ? this.store.getSnapshot().section : defaulted(value),
      data: this.store.getSnapshot().data,
      lastError: this.store.getSnapshot().lastError,
    })
  }

  private async refreshData(): Promise<void> {
    try {
      const [factsR, profileR] = await Promise.all([
        this.r().listFacts({ user: USER, limit: 200 }),
        this.r().listProfile({ user: USER }),
      ])
      const facts = unwrap(factsR)
      const profile = unwrap(profileR)
      this.store.set({
        ...this.store.getSnapshot(),
        data: {
          facts: Array.isArray(facts.facts) ? facts.facts : [],
          profile: Array.isArray(profile.profile) ? profile.profile : [],
        },
        lastError: undefined,
      })
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async saveFact(fact: MemoryData['facts'][number]): Promise<void> {
    try {
      await this.r().editFact({
        user: USER,
        fact_id: fact.fact_id,
        subject: fact.subject,
        predicate: fact.predicate,
        object: fact.object,
        content: fact.content,
        type: fact.type,
      })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async deleteFact(factId: string): Promise<void> {
    try {
      await this.r().deleteFact({ user: USER, fact_id: factId })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async fetchMemoryMd(): Promise<string> {
    try {
      const text = unwrap(await this.r().memoryMd({ user: USER }))
      this.store.set({
        ...this.store.getSnapshot(),
        data: { ...this.store.getSnapshot().data, memoryMd: text },
        lastError: undefined,
      })
      return text
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
      throw err
    }
  }

  private async fetchSummary(): Promise<string> {
    try {
      const text = unwrap(await this.r().summary({ user: USER }))
      this.store.set({
        ...this.store.getSnapshot(),
        data: { ...this.store.getSnapshot().data, summary: text },
        lastError: undefined,
      })
      return text
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
      throw err
    }
  }

  private async upsertProfile(section: string, key: string, value: string, pinned: boolean): Promise<void> {
    try {
      await this.r().upsertProfile({ user: USER, section, key, value, pinned })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async deleteProfile(section: string, key: string): Promise<void> {
    try {
      await this.r().deleteProfile({ user: USER, section, key })
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async saveAllFacts(rows: FactEditRow[]): Promise<void> {
    try {
      for (const row of rows) {
        if (row.deleted) {
          await this.r().deleteFact({ user: USER, fact_id: row.fact_id })
        } else {
          await this.r().editFact({
            user: USER,
            fact_id: row.fact_id,
            subject: row.subject,
            predicate: row.predicate,
            object: row.object,
            content: row.content,
            type: row.type,
          })
        }
      }
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async saveAllProfile(rows: ProfileEditRow[]): Promise<void> {
    try {
      for (const row of rows) {
        if (row.deleted) {
          await this.r().deleteProfile({ user: USER, section: row.section, key: row.key })
        } else {
          await this.r().upsertProfile({
            user: USER, section: row.section, key: row.key, value: row.value,
            pinned: row.pinned === true,
          })
        }
      }
      await this.refreshData()
    } catch (err) {
      this.store.set({
        ...this.store.getSnapshot(), lastError: (err as Error)?.message ?? String(err),
      })
    }
  }

  private async backup(): Promise<Record<string, unknown>> {
    return unwrap(await this.r().backup({ user: USER }))
  }

  private async restore(payload: Record<string, unknown>): Promise<{ facts_written: number; profile_written: number }> {
    const result = unwrap(await this.r().restore({ user: USER, payload }))
    await this.refreshData()
    return {
      facts_written: Number(result.facts_written ?? 0),
      profile_written: Number(result.profile_written ?? 0),
    }
  }
}

/** Fill defaults onto a (possibly partial / identical) section value. */
function defaulted(value: MemorySettingsSection): MemorySettingsSection {
  return {
    enabled: value.enabled ?? true,
    captureEnabled: value.captureEnabled ?? true,
    llmExtractionEnabled: value.llmExtractionEnabled ?? true,
    contextInjectionEnabled: value.contextInjectionEnabled ?? true,
    injectedMemoryMdTokens: clampInjectedMdTokens(value.injectedMemoryMdTokens),
    extractionModel: value.extractionModel,
  }
}
