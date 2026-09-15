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
  group: 'atom-memory-group',
  groupTitle: 'atom-memory-group-title',
  contentActions: 'atom-memory-content-actions',
  toggle: 'atom-memory-toggle',
  tooltip: 'atom-memory-tooltip',
  switchRow: 'atom-memory-switch-row',
  switch: 'atom-memory-switch',
  switchInput: 'atom-memory-switch-input',
  switchTrack: 'atom-memory-switch-track',
  switchThumb: 'atom-memory-switch-thumb',
  radioRow: 'atom-memory-radio-row',
  inputs: 'atom-memory-inputs',
  field: 'atom-memory-field',
  fieldLabel: 'atom-memory-field-label',
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
  slider: 'atom-memory-slider',
  ticks: 'atom-memory-ticks',
  tick: 'atom-memory-tick',
  tickActive: 'atom-memory-tick-active',
  pin: 'atom-memory-pin',
  summaryView: 'atom-memory-summary-view',
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
import {
  INJECTED_SUMMARY_TOKEN_PRESETS,
  nearestInjectedSummaryPresetIndex,
} from '../injection-budget.ts'
import type {
  FactEditRow, MemorySettingsFace, MemorySettingsState, ProfileEditRow,
} from './memory-settings-controller.ts'

/** Locale key of each gear shown in the panel, smallest gear first. */
const PRESET_LABEL_KEYS: Record<(typeof INJECTED_SUMMARY_TOKEN_PRESETS)[number], MemorySettingsLocaleKey> = {
  300: 'injectPresetCompact',
  800: 'injectPresetStandard',
  1500: 'injectPresetDetailed',
  3000: 'injectPresetAmple',
  6000: 'injectPresetBroad',
  12000: 'injectPresetMax',
}

/** DOM id of the injection-budget slider (its `<label>` points at it). */
const BUDGET_SLIDER_ID = 'atom-memory-inject-budget'

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

/** Monotonic source of client-side draft-row identities. */
let draftSeq = 0

/** @returns a fresh, process-unique draft-row identity. */
const nextDraftUid = (): number => (draftSeq += 1)

/** Modal state of a fact-editing draft row. */
interface FactsDraft extends FactEditRow {
  /**
   * Stable React key for the row. Deliberately NOT derived from cell content:
   * a content-derived key changes on every keystroke, which remounts the `<tr>`
   * and destroys the focused `<input>` (focus falls back to `<body>` and the
   * user can only ever type one character at a time).
   */
  uid: number
}

/** Modal state of a profile-editing draft row. */
interface ProfileDraft extends ProfileEditRow {
  /** Stable React key for the row — see {@link FactsDraft.uid}. */
  uid: number
}

/** Strip the client-only render identity before handing drafts to `onSave`. */
function withoutUid<T extends { uid: number }>(rows: T[]): Omit<T, 'uid'>[] {
  return rows.map(({ uid: _uid, ...rest }) => rest)
}

/**
 * A text field that owns its draft while the user types and commits it on blur
 * or Enter.
 *
 * A bare `<input value={x} onBlur={...} />` is NOT usable here: React treats a
 * `value` prop without `onChange` as a read-only field ("You provided a `value`
 * prop to a form field without an `onChange` handler"), so every keystroke is
 * reverted and the value never reaches the DOM — the field can never be filled
 * in. Holding the draft locally fixes that, and committing on blur (instead of
 * per keystroke) keeps a settings round-trip off the typing path.
 */
function DraftInput(props: {
  /** The committed value owned by the settings document. */
  value: string
  placeholder?: string
  type?: 'text' | 'password'
  autoComplete?: string
  /**
   * Canonicalise the draft on commit (e.g. clamp a number into range). The
   * field snaps to the returned text, so an out-of-range or unparsable entry
   * visibly corrects itself instead of silently disagreeing with the settings
   * document.
   */
  normalize?: (raw: string) => string
  /** Called with the current draft when the edit is done (blur / Enter). */
  onCommit: (value: string) => void
}) {
  const { value, placeholder, type, autoComplete, normalize, onCommit } = props
  const [draft, setDraft] = useState(value)
  /** Read through a ref so the sync effect below needs no extra re-render. */
  const editingRef = useRef(false)

  // Adopt external updates (settings reload, a sibling field's write) only while
  // this field is idle — never mid-edit, which would clobber what is typed.
  useEffect(() => {
    if (!editingRef.current) setDraft(value)
  }, [value])

  return (
    <input
      type={type ?? 'text'}
      autoComplete={autoComplete}
      placeholder={placeholder}
      value={draft}
      onChange={(e) => setDraft(e.currentTarget.value)}
      onFocus={() => { editingRef.current = true }}
      onBlur={(e) => {
        editingRef.current = false
        const next = normalize ? normalize(e.currentTarget.value) : e.currentTarget.value
        // Show the canonical form even when the commit itself is a no-op.
        if (next !== e.currentTarget.value) setDraft(next)
        if (next !== value) onCommit(next)
      }}
      onKeyDown={(e) => {
        // Enter is an explicit "done": blur commits through the same path.
        if (e.key === 'Enter') e.currentTarget.blur()
      }}
    />
  )
}

export function MemorySettingsSection(props: MemorySettingsSectionProps) {
  const { t } = props
  const state = props.useMemorySettings(snapshot => snapshot)

  // Back-up/restore transient feedback.
  const [status, setStatus] = useState<string>()
  const [phase, setPhase] = useState<'idle' | 'busy'>('idle')

  // Which modal is open: 'summary' | 'facts' | 'profile' | undefined.
  const [modal, setModal] = useState<'summary' | 'facts' | 'profile'>()
  // Summary fetched content is held in state.data.summary by the controller.
  const [summaryBusy, setSummaryBusy] = useState(false)
  // Set when the lazy summary fetch rejects, so the modal shows a real failure
  // instead of a misleading "empty" state (a rejection is never "no memory").
  const [summaryError, setSummaryError] = useState<string>()

  // "手动指定模型" selection: the settings document only stores
  // `extractionModel {provider, model}`, so tracking the user's mode choice
  // locally lets the manual radio stay selected even while provider is still
  // empty (the user is about to type one).
  const [modelManual, setModelManual] = useState<boolean>(
    () => Boolean(state.section.extractionModel?.provider || state.section.extractionModel?.model),
  )

  /** The committed injection budget (the Host clamps it on the way in). */
  const tokens = state.section.injectedSummaryTokens
  // The slider is positioned by *gear index*, not by token count: the gears are
  // deliberately uneven (300 → 800 → 1500 → …), so a linear token axis would
  // bunch every small gear into the first few pixels and make the cheap end
  // impossible to hit. Off-ladder values (from the old free-text field, or from
  // the plugin composition) park the handle at the nearest gear, and the
  // read-out says so instead of silently pretending they are that gear.
  const rungIndex = nearestInjectedSummaryPresetIndex(tokens)
  const rung = INJECTED_SUMMARY_TOKEN_PRESETS[rungIndex]!
  const offGrid = rung !== tokens

  const openSummary = (): void => {
    setModal('summary')
    if (state.data.summary === undefined) {
      setSummaryBusy(true)
      setSummaryError(undefined)
      // The controller sets its own `lastError` and rethrows, so the modal's
      // failure state is driven here (summaryError) rather than by the render
      // path — never leave the rejection unhandled.
      void props.fetchSummary()
        .then(() => setSummaryError(undefined))
        .catch((err) => setSummaryError((err as Error)?.message ?? String(err)))
        .finally(() => setSummaryBusy(false))
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

      {/* 1) master switch — a sliding toggle */}
      <fieldset className={css.block} disabled={!state.available}>
        <legend>{t('masterHeader')}</legend>
        <label className={css.switchRow}>
          <span className={css.switch}>
            {/* Native checkbox drives state & a11y; visually replaced by the
                sliding track. Kept focusable (visually hidden, not display:none)
                so keyboard focus + screen readers still work. */}
            <input
              type="checkbox"
              className={css.switchInput}
              checked={state.section.enabled}
              onChange={(e) => { void props.setEnabled(e.currentTarget.checked) }}
            />
            <span className={css.switchTrack} aria-hidden="true">
              <span className={css.switchThumb} />
            </span>
          </span>
          <span>{t('masterDesc')}</span>
        </label>
      </fieldset>

      {/* 2) system-prompt injection size — a slider over fixed gears */}
      <fieldset className={css.block} disabled={!state.available}>
        <legend>{t('injectHeader')}</legend>
        <div className={css.field}>
          <label className={css.fieldLabel} htmlFor={BUDGET_SLIDER_ID}>{t('injectSliderLabel')}</label>
          <input
            id={BUDGET_SLIDER_ID}
            className={css.slider}
            type="range"
            min={0}
            max={INJECTED_SUMMARY_TOKEN_PRESETS.length - 1}
            step={1}
            value={rungIndex}
            // The gear names are the only meaningful reading of the handle
            // position, so hand assistive tech the label rather than an index.
            aria-valuetext={t(PRESET_LABEL_KEYS[rung], { tokens: String(rung) })}
            onChange={(e) => {
              const next = INJECTED_SUMMARY_TOKEN_PRESETS[Number(e.currentTarget.value)]
              if (next !== undefined) void props.setInjectedSummaryTokens(next)
            }}
          />
          <div className={css.ticks}>
            {INJECTED_SUMMARY_TOKEN_PRESETS.map((preset, i) => (
              <span key={preset} className={i === rungIndex ? css.tickActive : css.tick}>{preset}</span>
            ))}
          </div>
          <p className={css.hint}>
            {offGrid
              ? t('injectOffGrid', { tokens: String(tokens) })
              : t(PRESET_LABEL_KEYS[rung], { tokens: String(rung) })}
          </p>
          <p className={css.hint}>
            {t('injectSliderHint', { rungs: INJECTED_SUMMARY_TOKEN_PRESETS.join(' / ') })}
          </p>
        </div>
        <p className={css.hint}>{t('injectHint', { tokens: String(tokens) })}</p>
      </fieldset>

      {/* 3) extraction model */}
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
        {modelManual && (
          <div>
            <div className={css.field}>
              <label className={css.fieldLabel}>{t('modelProviderLabel')}</label>
              <DraftInput
                placeholder={t('modelProviderPlaceholder')}
                value={state.section.extractionModel?.provider ?? ''}
                onCommit={(next) => {
                  void props.setExtractionModelOverride({
                    ...(state.section.extractionModel ?? {}), provider: next,
                  })
                }}
              />
            </div>
            <div className={css.field}>
              <label className={css.fieldLabel}>{t('modelNameLabel')}</label>
              <DraftInput
                placeholder={t('modelNamePlaceholder')}
                value={state.section.extractionModel?.model ?? ''}
                onCommit={(next) => {
                  void props.setExtractionModelOverride({
                    ...(state.section.extractionModel ?? {}), model: next,
                  })
                }}
              />
            </div>
            <div className={css.field}>
              <label className={css.fieldLabel}>{t('modelBaseUrlLabel')}</label>
              <DraftInput
                placeholder={t('modelBaseUrlPlaceholder')}
                value={state.section.extractionModel?.baseURL ?? ''}
                onCommit={(next) => {
                  void props.setExtractionModelOverride({
                    ...(state.section.extractionModel ?? {}), baseURL: next.trim(),
                  })
                }}
              />
            </div>
            <div className={css.field}>
              <label className={css.fieldLabel}>{t('modelProtocolLabel')}</label>
              <select
                value={state.section.extractionModel?.protocol || 'openai'}
                onChange={(e) => {
                  void props.setExtractionModelOverride({
                    ...(state.section.extractionModel ?? {}), protocol: e.currentTarget.value,
                  })
                }}
              >
                <option value="openai">{t('modelProtocolOpenai')}</option>
              </select>
            </div>
            <div className={css.field}>
              <label className={css.fieldLabel}>{t('modelApiKeyLabel')}</label>
              <DraftInput
                type="password"
                autoComplete="off"
                placeholder={t('modelApiKeyPlaceholder')}
                value={state.section.extractionModel?.apiKey ?? ''}
                onCommit={(next) => {
                  void props.setExtractionModelOverride({
                    ...(state.section.extractionModel ?? {}), apiKey: next,
                  })
                }}
              />
            </div>
          </div>
        )}
        <p className={css.hint}>{t('modelHint')}</p>
      </fieldset>

      {/* 4) 记忆内容 group: summary view + user profile + memory & facts
           share one region; each action is a button in a horizontal row whose explanation
           appears as a CSS hover tooltip (not inline text). */}
      <fieldset className={css.group} disabled={busy}>
        <legend className={css.groupTitle}>{t('contentGroupHeader')}</legend>
        <div className={css.contentActions}>
          {/* summary — read-only view of the injected system-prompt memory */}
          <div className={css.toggle}>
            <button type="button" className={css.btn} disabled={busy || summaryBusy} onClick={openSummary}>
              {t('summaryOpen')}
            </button>
            <div className={css.tooltip}>{t('summaryDesc')}</div>
          </div>

          {/* user profile — open an Excel-style modal editor */}
          <div className={css.toggle}>
            <button type="button" className={css.btn} disabled={busy} onClick={() => setModal('profile')}>
              {t('profileEditBtn')}
            </button>
            {profile.length === 0 ? <div className={css.tooltip}>{t('profileEmpty')}</div> : null}
          </div>

          {/* memory & facts — open an Excel-style modal editor */}
          <div className={css.toggle}>
            <button type="button" className={css.btn} disabled={busy} onClick={() => setModal('facts')}>
              {t('memoryEditBtn')}
            </button>
            {facts.length === 0 ? <div className={css.tooltip}>{t('factsEmpty')}</div> : null}
          </div>
        </div>
      </fieldset>

      {/* 6) backup / restore */}
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
                document.body.appendChild(a)
                a.click()
                // Revoke only after the download has had a chance to start;
                // revoking synchronously right after click() can abort the
                // export in some engines (esp. Firefox/Safari).
                setTimeout(() => {
                  document.body.removeChild(a)
                  URL.revokeObjectURL(url)
                }, 0)
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
      {modal === 'summary' ? (
        <SummaryModal
          t={t}
          busy={summaryBusy}
          content={state.data.summary}
          error={summaryError}
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

/** The summary viewer modal (read-only, renders the injected markdown). */
function SummaryModal(props: {
  t: RowTranslate
  busy: boolean
  content?: string
  /** Set when the lazy summary fetch rejected; rendered instead of "empty". */
  error?: string
  onClose: () => void
}) {
  const { t, busy, content, error, onClose } = props
  return (
    <Modal t={t} title={t('summaryHeader')} onClose={onClose}>
      {busy && content === undefined
        ? <p className={css.hint}>{t('summaryLoading')}</p>
        : error !== undefined
          ? <p className={css.empty}>{t('summaryLoadError', { message: error })}</p>
          : content === undefined
            ? <p className={css.empty}>{t('summaryEmpty')}</p>
            : <pre className={css.summaryView}>{content}</pre>}
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
      uid: nextDraftUid(),
      fact_id: f.fact_id, subject: f.subject, predicate: f.predicate,
      object: f.object, content: f.content ?? '', type: f.type, deleted: false,
    })),
  )
  const [saving, setSaving] = useState(false)

  const setRow = (index: number, patch: Partial<FactsDraft>) =>
    setRows(prev => prev.map((r, i) => i === index ? { ...r, ...patch } : r))

  const save = () => {
    setSaving(true)
    void Promise.resolve(onSave(withoutUid(rows))).finally(() => { setSaving(false); onClose() })
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
            <tr key={row.uid} style={row.deleted ? { opacity: 0.45 } : undefined}>
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
      {/* No "add row" here on purpose: the panel can only *edit / delete* existing
          facts (there is no create endpoint — new facts come from conversation
          capture). A row with an empty fact_id would fail the host editFact
          guard, so offering "add row" would be a silently-broken affordance. */}
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
    initial.map(r => ({
      uid: nextDraftUid(), section: r.section, key: r.key, value: r.value,
      pinned: r.pinned === true, deleted: false,
    })),
  )
  const [saving, setSaving] = useState(false)

  const setRow = (index: number, patch: Partial<ProfileDraft>) =>
    setRows(prev => prev.map((r, i) => i === index ? { ...r, ...patch } : r))

  const addRow = () =>
    setRows(prev => [...prev, {
      uid: nextDraftUid(), section: '', key: '', value: '', pinned: false, deleted: false,
    }])

  const save = () => {
    setSaving(true)
    void Promise.resolve(onSave(withoutUid(rows))).finally(() => { setSaving(false); onClose() })
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
            <th>{t('profileColPinned')}</th>
            <th>{t('colActions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row.uid} style={row.deleted ? { opacity: 0.45 } : undefined}>
              <td><input value={row.section} disabled={row.deleted} onChange={(e) => setRow(i, { section: e.currentTarget.value })} /></td>
              <td><input value={row.key} disabled={row.deleted} onChange={(e) => setRow(i, { key: e.currentTarget.value })} /></td>
              <td><input value={row.value} disabled={row.deleted} onChange={(e) => setRow(i, { value: e.currentTarget.value })} /></td>
              <td>
                <input
                  type="checkbox"
                  className={css.pin}
                  aria-label={t('profileColPinned')}
                  checked={row.pinned === true}
                  disabled={row.deleted}
                  onChange={(e) => setRow(i, { pinned: e.currentTarget.checked })}
                />
              </td>
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
      <p className={css.hint} style={{ marginTop: 8 }}>{t('profilePinnedHint')}</p>
      <button type="button" className={css.add} style={{ marginTop: 10 }} onClick={addRow}>{t('addRow')}</button>
    </Modal>
  )
}
