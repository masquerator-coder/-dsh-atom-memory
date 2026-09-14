/** The memory settings section rendered inside the dsh settings panel. */

import { useEffect, useRef, useState } from 'react'
import type {
  InjectFace, PropsLocale, PropsRuntime,
} from '@deepseek-ai/dsh-client-ui-slots'
import css from './MemorySettingsSection.module.css'
import { LOCALE_NS, type MemorySettingsLocaleKey } from './locales.ts'
import type { MemorySettingsFace, MemorySettingsState } from './memory-settings-controller.ts'

/** Declare the section's locale dictionary namespace (type-only merge). */
declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    'settings.atomMemory': MemorySettingsLocaleKey
  }
}

/** Props the renderer binds for the memory settings section. */
export type MemorySettingsSectionProps =
  PropsRuntime<'settings.section'>
  & PropsLocale<'settings.atomMemory'>
  & InjectFace<MemorySettingsFace>

/** One editable fact row's local (unsaved) draft. */
interface FactDraft { fact_id: string; subject: string; predicate: string; object: string; content: string }

/** One editable profile row's local draft. */
interface ProfileDraft { section: string; key: string; value: string; newRow: boolean }

export function MemorySettingsSection(props: MemorySettingsSectionProps) {
  const { t } = props
  const state = props.useMemorySettings(snapshot => snapshot)

  // Back-up/restore transient feedback.
  const [status, setStatus] = useState<string>()
  const [phase, setPhase] = useState<'idle' | 'busy'>('idle')

  // Load dynamic data on first mount.
  const loadedRef = useRef(false)
  useEffect(() => {
    if (loadedRef.current) return
    loadedRef.current = true
    void props.refreshData()
  }, [props])

  const busy = state.loading || phase === 'busy'

  return (
    <div className={css.section}>
      <header className={css.header}>
        <h2>{t('title')}</h2>
        <p>{t('intro')}</p>
      </header>

      {state.lastError ? <div className={css.error}>{t('error', { message: state.lastError })}</div> : null}
      {status ? <div className={css.status}>{status}</div> : null}

      {/* 1) master switch */}
      <fieldset className={css.block} disabled={!state.available}>
        <legend>{t('masterHeader')}</legend>
        <label className={css.switchRow}>
          <input
            type="checkbox"
            checked={state.section.enabled}
            onChange={(e) => { void props.setEnabled(e.currentTarget.checked) }}
          />
          <span>{t('masterDesc')}</span>
        </label>
      </fieldset>

      {/* 2) extraction model */}
      <fieldset className={css.block} disabled={!state.available}>
        <legend>{t('modelHeader')}</legend>
        <label className={css.radioRow}>
          <input
            type="radio"
            name="extraction-model-mode"
            checked={!state.section.extractionModel?.provider}
            onChange={() => { void props.setExtractionModel('', '') }}
          />
          <span>{t('modelFollowDefault')}</span>
        </label>
        <label className={css.radioRow}>
          <input
            type="radio"
            name="extraction-model-mode"
            checked={Boolean(state.section.extractionModel?.provider)}
            onChange={() => {
              void props.setExtractionModel(
                state.section.extractionModel?.provider || '', state.section.extractionModel?.model || '',
              )
            }}
          />
          <span>{t('modelManual')}</span>
        </label>
        <div className={css.inputs}>
          <input
            placeholder={t('modelProviderPlaceholder')}
            value={state.section.extractionModel?.provider ?? ''}
            onBlur={(e) => {
              void props.setExtractionModel(
                e.currentTarget.value, state.section.extractionModel?.model ?? '',
              )
            }}
          />
          <input
            placeholder={t('modelNamePlaceholder')}
            value={state.section.extractionModel?.model ?? ''}
            onBlur={(e) => {
              void props.setExtractionModel(
                state.section.extractionModel?.provider ?? '', e.currentTarget.value,
              )
            }}
          />
        </div>
        <p className={css.hint}>{t('modelHint')}</p>
      </fieldset>

      {/* 3) user profile editing */}
      <fieldset className={css.block} disabled={busy}>
        <legend>{t('profileHeader')}</legend>
        {state.data.profile.length === 0 ? <p className={css.empty}>{t('profileEmpty')}</p> : (
          state.data.profile.map((row, index) => (
            <ProfileRow
              key={`${row.section}:${row.key}:${index}`}
              t={t}
              row={row}
              onSave={(d) => {
                void props.upsertProfile(d.section, d.key, d.value)
              }}
              onDelete={() => {
                void props.deleteProfile(row.section, row.key)
              }}
            />
          ))
        )}
        <button type="button" className={css.add} onClick={() => { void props.upsertProfile('', '', '') }}>
          {t('profileAdd')}
        </button>
      </fieldset>

      {/* 4) memory & edit */}
      <fieldset className={css.block} disabled={busy}>
        <legend>{t('memoryHeader')} · {t('factsHeader')}</legend>
        {state.data.facts.length === 0 ? <p className={css.empty}>{t('factsEmpty')}</p> : (
          state.data.facts.map(fact => (
            <FactRow
              key={fact.fact_id}
              t={t}
              fact={fact}
              onSave={(d) => {
                void props.saveFact({ ...fact, ...d })
              }}
            />
          ))
        )}
      </fieldset>

      {/* 5) backup / restore */}
      <fieldset className={css.block} disabled={busy}>
        <legend>{t('backupHeader')}</legend>
        <p className={css.hint}>{t('backupDesc')}</p>
        <div className={css.actions}>
          <button type="button" disabled={busy} onClick={() => {
            setPhase('busy')
            void props.backup()
              .then(payload => {
                const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = 'atom-memory-backup.json'
                a.click()
                URL.revokeObjectURL(url)
                setStatus('✔ ' + new Date().toLocaleString())
                setPhase('idle')
              })
              .catch(err => { setStatus(t('error', { message: (err as Error)?.message ?? err })); setPhase('idle') })
          }}>
            {t('exportBtn')}
          </button>
          <label className={css.fileLabel}>
            {t('importBtn')}
            <input type="file" accept="application/json,.json" hidden disabled={busy} onChange={async (e) => {
              const file = e.currentTarget.files?.[0]
              e.currentTarget.value = ''
              if (!file) return
              setPhase('busy')
              try {
                const text = await file.text()
                const payload = JSON.parse(text) as Record<string, unknown>
                const result = await props.restore(payload)
                setStatus(t('restored', { facts: String(result.facts_written), profile: String(result.profile_written) }))
              } catch (err) {
                setStatus(t('error', { message: (err as Error)?.message ?? err }))
              } finally {
                setPhase('idle')
              }
            }} />
          </label>
        </div>
      </fieldset>
    </div>
  )
}

/** A locale-typed translate used by the row subcomponents. */
type RowTranslate = (key: MemorySettingsLocaleKey, params?: Record<string, unknown>) => string

function FactRow(props: {
  t: RowTranslate
  fact: { fact_id: string; subject: string; predicate: string; object: string; content?: string }
  onSave: (d: FactDraft) => void
}) {
  const { t, fact } = props
  const [draft, setDraft] = useState<FactDraft>(() => ({
    fact_id: fact.fact_id, subject: fact.subject,
    predicate: fact.predicate, object: fact.object, content: fact.content ?? '',
  }))
  const set = (patch: Partial<FactDraft>) => setDraft(prev => ({ ...prev, ...patch }))
  return (
    <div className={css.factRow}>
      <div className={css.badge}>{draft.fact_id.slice(0, 8)}</div>
      <div className={css.factFields}>
        <input value={draft.subject} onChange={(e) => set({ subject: e.currentTarget.value })} />
        <input value={draft.predicate} onChange={(e) => set({ predicate: e.currentTarget.value })} />
        <input value={draft.object} onChange={(e) => set({ object: e.currentTarget.value })} />
        <textarea value={draft.content} onChange={(e) => set({ content: e.currentTarget.value })} />
      </div>
      <button type="button" onClick={() => props.onSave(draft)}>{t('editSave')}</button>
    </div>
  )
}

function ProfileRow(props: {
  t: RowTranslate
  row: { section: string; key: string; value: string }
  onSave: (d: ProfileDraft) => void
  onDelete: () => void
}) {
  const { t, row } = props
  const [draft, setDraft] = useState<ProfileDraft>(() => ({
    section: row.section, key: row.key, value: row.value, newRow: false,
  }))
  const set = (patch: Partial<ProfileDraft>) => setDraft(prev => ({ ...prev, ...patch }))
  return (
    <div className={css.factRow}>
      <div className={css.factFields}>
        <input value={draft.section} placeholder={t('profileSection')} onChange={(e) => set({ section: e.currentTarget.value })} />
        <input value={draft.key} placeholder={t('profileKey')} onChange={(e) => set({ key: e.currentTarget.value })} />
        <input value={draft.value} placeholder={t('profileValue')} onChange={(e) => set({ value: e.currentTarget.value })} />
      </div>
      <button type="button" onClick={() => props.onSave(draft)}>{t('editSave')}</button>
      <button type="button" onClick={props.onDelete}>×</button>
    </div>
  )
}
