/** The memory settings section rendered inside the dsh settings panel. */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import type {
  InjectFace, PropsLocale, PropsRuntime,
} from '@deepseek-ai/dsh-client-ui-slots'
/**
 * Inline stylesheet (hand-Rolled). The browser bundle is built standalone
 * (tsdown, no lightningcss CSS-modules pass), so the class map lives here as a
 * plain object instead of a `.module.css` import — identical class names, no
 * build-time CSS plugin required.
 */
const css = {
  section: 'atom-memory-section',
  header: 'atom-memory-header',
  error: 'atom-memory-error',
  status: 'atom-memory-status',
  block: 'atom-memory-block',
  switchRow: 'atom-memory-switch-row',
  radioRow: 'atom-memory-radio-row',
  inputs: 'atom-memory-inputs',
  hint: 'atom-memory-hint',
  empty: 'atom-memory-empty',
  add: 'atom-memory-add',
  actions: 'atom-memory-actions',
  fileLabel: 'atom-memory-file-label',
  factRow: 'atom-memory-fact-row',
  badge: 'atom-memory-badge',
  factFields: 'atom-memory-fact-fields',
  rowActions: 'atom-memory-row-actions',
  rowBtn: 'atom-memory-row-btn',
  btn: 'atom-memory-btn',
  btnPrimary: 'atom-memory-btn-primary',
  btnDanger: 'atom-memory-btn-danger',
  btnRowDelete: 'atom-memory-btn-row-delete',
  memoryMdView: 'atom-memory-memory-md',
  overlay: 'atom-memory-overlay',
  modal: 'atom-memory-modal',
  modalHeader: 'atom-memory-modal-header',
  modalBody: 'atom-memory-modal-body',
  modalFooter: 'atom-memory-modal-footer',
  editor: 'atom-memory-editor',
  editorRowActions: 'atom-memory-editor-row-actions',
}
import { LOCALE_NS, type MemorySettingsLocaleKey } from './locales.ts'
import { ensureMemorySettingsStyle } from './styles.ts'
import type {
  FactEditRow, MemorySettingsFace, MemorySettingsState, ProfileEditRow,
} from './memory-settings-controller.ts'

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

/** A locale-typed translate used by the rows/subcomponents. */
type RowTranslate = (key: MemorySettingsLocaleKey, params?: Record<string, unknown>) => string

/** Modal state of a fact-editing draft row. */
interface FactsDraft extends FactEditRow {}

/** Modal state of a profile-editing draft row. */
interface ProfileDraft extends ProfileEditRow {}

export function MemorySettingsSection(props: MemorySettingsSectionProps) {
  const { t } = props
  const state = props.useMemorySettings(snapshot => snapshot)

  // Back-up/restore transient feedback.
  const [status, setStatus] = useState<string>()
  const [phase, setPhase] = useState<'idle' | 'busy'>('idle')

  // Which modal is open: 'memoryMd' | 'facts' | 'profile' | undefined.
  const [modal, setModal] = useState<'memoryMd' | 'facts' | 'profile'>()
  // memory.md fetched content is held in state.data.memoryMd by the controller.
  const [memoryMdBusy, setMemoryMdBusy] = useState(false)

  // "手动指定模型" selection: the settings document only stores
  // `extractionModel {provider, model}`, so tracking the user's mode choice
  // locally lets the manual radio stay selected even while provider is still
  // empty (the user is about to type one).
  const [modelManual, setModelManual] = useState<boolean>(
    () => Boolean(state.section.extractionModel?.provider || state.section.extractionModel?.model),
  )

  const openMemoryMd = (): void => {
    setModal('memoryMd')
    if (state.data.memoryMd === undefined) {
      setMemoryMdBusy(true)
      void props.fetchMemoryMd().finally(() => setMemoryMdBusy(false))
    }
  }

  // Load dynamic data on first mount.
  const loadedRef = useRef(false)
  useEffect(() => {
    if (loadedRef.current) return
    loadedRef.current = true
    ensureMemorySettingsStyle()
    void props.refreshData()
  }, [props])

  const busy = state.loading || phase === 'busy'

  // Belt-and-suspenders: never let a nullish `state.data` (or a malformed
  // facts/profile payload) blank the panel — default to empty lists.
  const profile = state.data?.profile ?? []
  const facts = state.data?.facts ?? []

  return (
    <div className={css.section}>
      <header className={css.header}>
        <h2>{t('title')}</h2>
        <p>{t('intro')}</p>
      </header>

      {state.lastError ? <div className={css.error}>{t('error', { message: state.lastError })}</div> : null}
      {status ? <div className={css.status}>{status}</div> : null}

      {/* 0) memory.md — button on left, description below, content in a modal */}
      <fieldset className={css.block} disabled={busy || memoryMdBusy}>
        <legend>{t('memoryMdHeader')}</legend>
        <button
          type="button"
          className={css.btn}
          style={{ alignSelf: 'flex-start' }}
          disabled={busy || memoryMdBusy}
          onClick={openMemoryMd}
        >
          {t('memoryMdOpen')}
        </button>
        <p className={css.hint}>{t('memoryMdDesc')}</p>
      </fieldset>

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
            checked={!modelManual}
            onChange={() => {
              setModelManual(false)
              void props.setExtractionModel('', '')
            }}
          />
          <span>{t('modelFollowDefault')}</span>
        </label>
        <label className={css.radioRow}>
          <input
            type="radio"
            name="extraction-model-mode"
            checked={modelManual}
            onChange={() => setModelManual(true)}
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

      {/* 3) user profile — open an Excel-style modal editor */}
      <fieldset className={css.block} disabled={busy}>
        <legend>{t('profileHeader')}</legend>
        {profile.length === 0 ? <p className={css.empty}>{t('profileEmpty')}</p> : null}
        <button type="button" className={css.btn} style={{ alignSelf: 'flex-start' }} onClick={() => setModal('profile')}>
          {t('profileEditBtn')}
        </button>
      </fieldset>

      {/* 4) memory & edit — open an Excel-style modal editor */}
      <fieldset className={css.block} disabled={busy}>
        <legend>{t('memoryHeader')} · {t('factsHeader')}</legend>
        {facts.length === 0 ? <p className={css.empty}>{t('factsEmpty')}</p> : null}
        <button type="button" className={css.btn} style={{ alignSelf: 'flex-start' }} onClick={() => setModal('facts')}>
          {t('memoryEditBtn')}
        </button>
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

      {/* ===== modals ===== */}
      {modal === 'memoryMd' ? (
        <MemoryMdModal
          t={t}
          busy={memoryMdBusy}
          content={state.data.memoryMd}
          onClose={() => setModal(undefined)}
        />
      ) : null}
      {modal === 'facts' ? (
        <FactsEditorModal
          t={t}
          initial={facts}
          onSave={(rows) => props.saveAllFacts(rows)}
          onClose={() => setModal(undefined)}
        />
      ) : null}
      {modal === 'profile' ? (
        <ProfileEditorModal
          t={t}
          initial={profile}
          onSave={(rows) => props.saveAllProfile(rows)}
          onClose={() => setModal(undefined)}
        />
      ) : null}
    </div>
  )
}

/* ============================================================================
 * Modal primitives
 * ========================================================================== */

/** A simple themed centered modal shell (header + scrollable body + footer). */
function Modal(props: {
  t: RowTranslate
  title: string
  children: ReactNode
  footer?: ReactNode
  onClose: () => void
}) {
  const { t, title, children, footer, onClose } = props
  return (
    <div className={css.overlay} onClick={onClose}>
      <div
        className={css.modal}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className={css.modalHeader}>
          <h3>{title}</h3>
          <button type="button" className={css.btn} onClick={onClose}>{t('close')}</button>
        </div>
        <div className={css.modalBody}>{children}</div>
        {footer ? <div className={css.modalFooter}>{footer}</div> : null}
      </div>
    </div>
  )
}

/** The memory.md viewer modal (read-only). */
function MemoryMdModal(props: {
  t: RowTranslate
  busy: boolean
  content?: string
  onClose: () => void
}) {
  const { t, busy, content, onClose } = props
  return (
    <Modal t={t} title={t('memoryMdHeader')} onClose={onClose}>
      {busy && content === undefined
        ? <p className={css.hint}>{t('memoryMdLoading')}</p>
        : content === undefined
          ? <p className={css.empty}>{t('memoryMdEmpty')}</p>
          : <pre className={css.memoryMdView}>{content}</pre>}
    </Modal>
  )
}

/* ============================================================================
 * Excel-style table editors
 * ========================================================================== */

/** Modal editor for atomic facts: Excel-like editable table + single save all. */
function FactsEditorModal(props: {
  t: RowTranslate
  initial: MemorySettingsState['data']['facts']
  onSave: (rows: FactEditRow[]) => void
  onClose: () => void
}) {
  const { t, initial, onSave, onClose } = props
  const [rows, setRows] = useState<FactsDraft[]>(() =>
    initial.map(f => ({
      fact_id: f.fact_id, subject: f.subject, predicate: f.predicate,
      object: f.object, content: f.content ?? '', type: f.type, deleted: false,
    })),
  )
  const [saving, setSaving] = useState(false)

  const setRow = (index: number, patch: Partial<FactsDraft>) =>
    setRows(prev => prev.map((r, i) => i === index ? { ...r, ...patch } : r))

  const addRow = () =>
    setRows(prev => [...prev, {
      fact_id: '', subject: '', predicate: '', object: '', content: '', deleted: false,
    }])

  const save = () => {
    setSaving(true)
    void Promise.resolve(onSave(rows)).finally(() => { setSaving(false); onClose() })
  }

  const footer = (
    <>
      <button type="button" className={css.btn} onClick={onClose} disabled={saving}>{t('cancel')}</button>
      <button type="button" className={css.btnPrimary} onClick={save} disabled={saving}>
        {saving ? t('saving') : t('saveAll')}
      </button>
    </>
  )

  return (
    <Modal t={t} title={t('memoryModalTitle')} footer={footer} onClose={onClose}>
      <table className={css.editor}>
        <thead>
          <tr>
            <th>{t('colSubject')}</th>
            <th>{t('colPredicate')}</th>
            <th>{t('colObject')}</th>
            <th>{t('colContent')}</th>
            <th>{t('colActions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row.fact_id || `new-${i}`} style={row.deleted ? { opacity: 0.45 } : undefined}>
              <td><input value={row.subject} disabled={row.deleted} placeholder={t('newRowPlaceholder')} onChange={(e) => setRow(i, { subject: e.currentTarget.value })} /></td>
              <td><input value={row.predicate} disabled={row.deleted} onChange={(e) => setRow(i, { predicate: e.currentTarget.value })} /></td>
              <td><input value={row.object} disabled={row.deleted} onChange={(e) => setRow(i, { object: e.currentTarget.value })} /></td>
              <td><textarea value={row.content ?? ''} disabled={row.deleted} onChange={(e) => setRow(i, { content: e.currentTarget.value })} /></td>
              <td>
                <div className={css.editorRowActions}>
                  <button
                    type="button"
                    className={css.btnRowDelete}
                    onClick={() => setRow(i, { deleted: !row.deleted })}
                  >
                    {row.deleted ? t('addRow') : t('factDelete')}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className={css.add} style={{ marginTop: 10 }} onClick={addRow}>{t('addRow')}</button>
    </Modal>
  )
}

/** Modal editor for the user profile: Excel-like editable table + single save all. */
function ProfileEditorModal(props: {
  t: RowTranslate
  initial: MemorySettingsState['data']['profile']
  onSave: (rows: ProfileEditRow[]) => void
  onClose: () => void
}) {
  const { t, initial, onSave, onClose } = props
  const [rows, setRows] = useState<ProfileDraft[]>(() =>
    initial.map(r => ({ section: r.section, key: r.key, value: r.value, deleted: false })),
  )
  const [saving, setSaving] = useState(false)

  const setRow = (index: number, patch: Partial<ProfileDraft>) =>
    setRows(prev => prev.map((r, i) => i === index ? { ...r, ...patch } : r))

  const addRow = () => setRows(prev => [...prev, { section: '', key: '', value: '', deleted: false }])

  const save = () => {
    setSaving(true)
    void Promise.resolve(onSave(rows)).finally(() => { setSaving(false); onClose() })
  }

  const footer = (
    <>
      <button type="button" className={css.btn} onClick={onClose} disabled={saving}>{t('cancel')}</button>
      <button type="button" className={css.btnPrimary} onClick={save} disabled={saving}>
        {saving ? t('saving') : t('saveAll')}
      </button>
    </>
  )

  return (
    <Modal t={t} title={t('profileModalTitle')} footer={footer} onClose={onClose}>
      <table className={css.editor}>
        <thead>
          <tr>
            <th>{t('profileColSection')}</th>
            <th>{t('profileColKey')}</th>
            <th>{t('profileColValue')}</th>
            <th>{t('colActions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={`${row.section}:${row.key}:${i}`} style={row.deleted ? { opacity: 0.45 } : undefined}>
              <td><input value={row.section} disabled={row.deleted} onChange={(e) => setRow(i, { section: e.currentTarget.value })} /></td>
              <td><input value={row.key} disabled={row.deleted} onChange={(e) => setRow(i, { key: e.currentTarget.value })} /></td>
              <td><input value={row.value} disabled={row.deleted} onChange={(e) => setRow(i, { value: e.currentTarget.value })} /></td>
              <td>
                <div className={css.editorRowActions}>
                  <button
                    type="button"
                    className={css.btnRowDelete}
                    onClick={() => setRow(i, { deleted: !row.deleted })}
                  >
                    {row.deleted ? t('addRow') : t('factDelete')}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className={css.add} style={{ marginTop: 10 }} onClick={addRow}>{t('addRow')}</button>
    </Modal>
  )
}
