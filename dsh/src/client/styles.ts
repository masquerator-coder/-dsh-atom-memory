/**
 * Plain stylesheet for the memory settings section, injected as a
 * `<style data-plugin="dsh-atom-memory">` tag on mount (the browser bundle is
 * served standalone without a separate stylesheet, mirroring the harness's
 * style-injection convention). Class names mirror the `css` map in
 * `MemorySettingsSection.tsx`.
 */

export const memorySettingsStyleText = `
.atom-memory-section{display:flex;flex-direction:column;gap:18px;max-width:720px}
.atom-memory-header h2{margin:0 0 4px;font-size:20px}
.atom-memory-header p{margin:0;color:var(--dsh-text-muted,#8a8f98);font-size:13px}
.atom-memory-error{padding:8px 12px;border-radius:8px;background:color-mix(in srgb,var(--dsh-danger,#e5484d) 12%,transparent);color:var(--dsh-danger,#e5484d);font-size:13px}
.atom-memory-status{padding:6px 12px;border-radius:8px;background:color-mix(in srgb,var(--dsh-accent,#5b8def) 12%,transparent);font-size:13px}
.atom-memory-block{display:flex;flex-direction:column;gap:8px;margin:0;padding:12px 14px;border:1px solid var(--dsh-border,#33363d);border-radius:10px}
.atom-memory-block legend{font-weight:600;padding:0 4px}
.atom-memory-switch-row,.atom-memory-radio-row{display:flex;align-items:flex-start;gap:8px;font-size:14px;cursor:pointer}
.atom-memory-inputs{display:flex;gap:8px;margin-top:4px}
.atom-memory-inputs input,.atom-memory-fact-fields input,.atom-memory-fact-fields textarea{flex:1;padding:6px 8px;border:1px solid var(--dsh-border,#3a3d44);border-radius:6px;background:var(--dsh-surface-2,#24262b);color:var(--dsh-text,#e6e8eb);font-size:13px;min-width:0;box-sizing:border-box}
.atom-memory-fact-fields textarea{min-height:40px;resize:vertical;flex-basis:100%}
.atom-memory-hint{margin:0;color:var(--dsh-text-muted,#8a8f98);font-size:12px}
.atom-memory-empty{color:var(--dsh-text-muted,#8a8f98);font-size:13px;margin:0}
.atom-memory-add{align-self:flex-start;padding:5px 12px;border:1px solid var(--dsh-border,#3a3d44);border-radius:6px;background:var(--dsh-surface-2,#24262b);color:var(--dsh-text,#e6e8eb);cursor:pointer;font-size:13px}
.atom-memory-actions{display:flex;gap:10px;align-items:center}
.atom-memory-actions button,.atom-memory-file-label{padding:6px 14px;border:1px solid var(--dsh-border,#3a3d44);border-radius:6px;background:var(--dsh-surface-2,#24262b);color:var(--dsh-text,#e6e8eb);cursor:pointer;font-size:13px;display:inline-block}
.atom-memory-file-label input{display:none}
.atom-memory-fact-row{display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:var(--dsh-surface-1,#1f2126)}
.atom-memory-badge{font-family:var(--dsh-font-mono,monospace);font-size:11px;color:var(--dsh-text-muted,#8a8f98)}
.atom-memory-fact-fields{display:flex;flex-wrap:wrap;gap:6px}
`

/** Ensure the stylesheet is present exactly once (data-plugin guarded). */
export function ensureMemorySettingsStyle(): void {
  if (typeof document === 'undefined') return
  const tagId = 'dsh-atom-memory/memory-settings'
  if (document.querySelector(`style[data-plugin-css="${tagId}"]`) !== null) return
  const style = document.createElement('style')
  style.dataset.plugin = 'dsh-atom-memory'
  style.dataset.pluginCss = tagId
  style.textContent = memorySettingsStyleText
  document.head.appendChild(style)
}
