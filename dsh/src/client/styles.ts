/**
 * Plain stylesheet for the memory settings section, injected as a
 * `<style data-plugin="dsh-atom-memory">` tag on mount (the browser bundle is
 * served standalone without a separate stylesheet, mirroring the harness's
 * style-injection convention). Class names mirror the `css` map in
 * `MemorySettingsSection.tsx`.
 */

export const memorySettingsStyleText = `
.atom-memory-section{display:flex;flex-direction:column;gap:18px;max-width:760px}
.atom-memory-header h2{margin:0 0 4px;font-size:20px;color:var(--dsw-alias-label-primary,#e6e8eb)}
.atom-memory-header p{margin:0;color:var(--dsw-alias-label-secondary,#8a8f98);font-size:13px}
.atom-memory-error{padding:8px 12px;border-radius:8px;background:color-mix(in srgb,var(--dsw-alias-state-error-primary,#e5484d) 12%,transparent);color:var(--dsw-alias-state-error-primary,#e5484d);font-size:13px}
.atom-memory-status{padding:6px 12px;border-radius:8px;background:color-mix(in srgb,var(--dsw-alias-state-success-primary,#46a758) 12%,transparent);color:var(--dsw-alias-label-primary,#e6e8eb);font-size:13px}
.atom-memory-block{display:flex;flex-direction:column;gap:8px;margin:0;padding:12px 14px;border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,0.12));border-radius:10px;background:var(--dsw-alias-bg-layer-1,#1f2126)}
.atom-memory-block legend{font-weight:600;padding:0 4px;color:var(--dsw-alias-label-primary,#e6e8eb)}
.atom-memory-switch-row,.atom-memory-radio-row{display:flex;align-items:flex-start;gap:8px;font-size:14px;cursor:pointer;color:var(--dsw-alias-label-primary,#e6e8eb)}
.atom-memory-inputs{display:flex;gap:8px;margin-top:4px}
.atom-memory-field{display:flex;flex-direction:column;gap:3px;margin-top:8px}
.atom-memory-field-label{font-size:12px;color:var(--dsw-alias-label-secondary,#8a8f98)}
.atom-memory-field input,.atom-memory-field select{padding:6px 8px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:6px;background:var(--dsw-alias-bg-layer-3,#24262b);color:var(--dsw-alias-label-primary,#e6e8eb);font-size:13px;box-sizing:border-box}
.atom-memory-field select{appearance:auto}
.atom-memory-inputs input,.atom-memory-fact-fields input,.atom-memory-fact-fields textarea{flex:1;padding:6px 8px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:6px;background:var(--dsw-alias-bg-layer-3,#24262b);color:var(--dsw-alias-label-primary,#e6e8eb);font-size:13px;min-width:0;box-sizing:border-box}
.atom-memory-fact-fields textarea{min-height:40px;resize:vertical;flex-basis:100%}
.atom-memory-hint{margin:0;color:var(--dsw-alias-label-secondary,#8a8f98);font-size:12px}
.atom-memory-empty{color:var(--dsw-alias-label-secondary,#8a8f98);font-size:13px;margin:0}
.atom-memory-add{align-self:flex-start;padding:5px 12px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:6px;background:var(--dsw-alias-interactive-bg-hover,rgba(255,255,255,0.08));color:var(--dsw-alias-label-primary,#e6e8eb);cursor:pointer;font-size:13px}
.atom-memory-actions{display:flex;gap:10px;align-items:center}
.atom-memory-actions button,.atom-memory-file-label{padding:6px 14px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:6px;background:var(--dsw-alias-interactive-bg-hover,rgba(255,255,255,0.08));color:var(--dsw-alias-label-primary,#e6e8eb);cursor:pointer;font-size:13px;display:inline-block}
.atom-memory-file-label input{display:none}
.atom-memory-fact-row{display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:var(--dsw-alias-bg-layer-2,#24262b)}
.atom-memory-badge{font-family:var(--dsw-font-mono,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace);font-size:11px;color:var(--dsw-alias-label-secondary,#8a8f98)}
.atom-memory-fact-fields{display:flex;flex-wrap:wrap;gap:6px}
.atom-memory-row-actions{display:flex;gap:8px;justify-content:flex-end}
.atom-memory-memory-md{max-height:320px;overflow:auto;margin:0;padding:10px 12px;border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,0.12));border-radius:8px;background:var(--dsw-alias-bg-layer-2,#24262b);color:var(--dsw-alias-label-primary,#e6e8eb);font-family:var(--dsw-font-mono,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace);font-size:12px;white-space:pre-wrap;word-break:break-word}

/* Buttons follow the system theme via the harness design tokens (light/dark aware). */
.atom-memory-row-btn,.atom-memory-btn{padding:5px 12px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:6px;background:var(--dsw-alias-interactive-bg-hover,rgba(255,255,255,0.08));color:var(--dsw-alias-label-primary,#e6e8eb);cursor:pointer;font-size:13px}
.atom-memory-row-btn:hover,.atom-memory-btn:hover,.atom-memory-add:hover,.atom-memory-actions button:hover{background:var(--dsw-alias-interactive-bg-hover-accent,rgba(255,255,255,0.16))}
.atom-memory-btn-primary{padding:6px 16px;border:none;border-radius:6px;background:var(--dsw-alias-button-primary-fill,rgb(65,118,230));color:var(--dsw-alias-label-primary-foreground,#ffffff);font-weight:600;cursor:pointer;font-size:13px}
.atom-memory-btn-primary:disabled,.atom-memory-btn:disabled,.atom-memory-row-btn:disabled{opacity:.5;cursor:not-allowed}
.atom-memory-btn-danger{color:var(--dsw-alias-state-error-primary,#e5484d);border-color:color-mix(in srgb,var(--dsw-alias-state-error-primary,#e5484d) 50%,transparent)}
.atom-memory-btn-row-delete{flex:none;padding:4px 10px;border:1px solid color-mix(in srgb,var(--dsw-alias-state-error-primary,#e5484d) 50%,transparent);border-radius:6px;background:transparent;color:var(--dsw-alias-state-error-primary,#e5484d);cursor:pointer;font-size:12px}

/* Modal overlay. */
.atom-memory-overlay{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:var(--dsw-alias-bg-mask-1,rgba(0,0,0,0.5))}
.atom-memory-modal{display:flex;flex-direction:column;width:min(720px,92vw);max-height:82vh;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:12px;background:var(--dsw-alias-bg-layer-3,#24262b);box-shadow:0 18px 48px rgba(0,0,0,0.4);overflow:hidden}
.atom-memory-modal-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border-bottom:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,0.12))}
.atom-memory-modal-header h3{margin:0;font-size:15px;color:var(--dsw-alias-label-primary,#e6e8eb)}
.atom-memory-modal-body{overflow:auto;padding:12px 16px}
.atom-memory-modal-footer{display:flex;justify-content:flex-end;gap:10px;padding:12px 16px;border-top:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,0.12))}

/* Excel-like editable table. */
.atom-memory-editor{width:100%;border-collapse:collapse;font-size:13px}
.atom-memory-editor th{position:sticky;top:0;text-align:left;padding:8px;border-bottom:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));color:var(--dsw-alias-label-secondary,#8a8f98);font-weight:600;background:var(--dsw-alias-bg-layer-2,#24262b)}
.atom-memory-editor td{padding:5px 6px;border-bottom:1px solid var(--dsw-alias-border-l1,rgba(255,255,255,0.06));vertical-align:middle}
.atom-memory-editor input,.atom-memory-editor textarea{width:100%;box-sizing:border-box;padding:5px 7px;border:1px solid var(--dsw-alias-border-l3,rgba(255,255,255,0.16));border-radius:5px;background:var(--dsw-alias-bg-layer-1,#1f2126);color:var(--dsw-alias-label-primary,#e6e8eb);font-size:13px}
.atom-memory-editor textarea{min-height:34px;resize:vertical}
.atom-memory-editor-row-actions{display:flex;gap:6px;align-items:center;justify-content:flex-end;white-space:nowrap}
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
