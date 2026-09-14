window.__ModuleLoader__.load({
	id: "dsh-atom-memory",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let _deepseek_ai_dsh_client_store = require("@deepseek-ai/dsh-client-store");
		let react = require("react");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region src/client/memory-settings-controller.ts
		/** Unwrap a `WireResult` to its `.value`, throwing on a failed call. */
		function unwrap(result) {
			if (result == null || result.ok === false) {
				const message = result?.error != null ? String(result.error?.message ?? result.error) : "Remote call failed";
				throw new Error(message);
			}
			return result.value;
		}
		const USER = "global";
		var MemorySettingsController = class {
			scope;
			remote;
			store = (0, _deepseek_ai_dsh_client_store.createSnapshotStore)({
				available: false,
				loading: true,
				section: {
					enabled: true,
					captureEnabled: true,
					llmExtractionEnabled: true,
					contextInjectionEnabled: true,
					extractionModel: void 0
				},
				data: {
					facts: [],
					profile: []
				}
			});
			unsubscribe;
			constructor(scope, remote) {
				this.scope = scope;
				this.remote = remote;
				this.unsubscribe = scope.subscribe(() => this.publish());
				this.publish();
			}
			/** @returns the face the section's slot registration injects. */
			inject() {
				return {
					hooks: { memorySettings: this.store },
					setEnabled: (enabled) => this.scope.set("enabled", enabled),
					setExtractionModel: (provider, model) => this.scope.set("extractionModel", {
						provider,
						model
					}),
					setExtractionModelOverride: (override) => this.scope.set("extractionModel", override),
					refreshData: () => this.refreshData(),
					saveFact: (fact) => this.saveFact(fact),
					deleteFact: (factId) => this.deleteFact(factId),
					fetchMemoryMd: () => this.fetchMemoryMd(),
					upsertProfile: (section, key, value) => this.upsertProfile(section, key, value),
					deleteProfile: (section, key) => this.deleteProfile(section, key),
					saveAllFacts: (rows) => this.saveAllFacts(rows),
					saveAllProfile: (rows) => this.saveAllProfile(rows),
					backup: () => this.backup(),
					restore: (payload) => this.restore(payload)
				};
			}
			dispose() {
				this.unsubscribe();
			}
			r() {
				return this.remote;
			}
			publish() {
				const snap = this.scope.getSnapshot();
				const value = snap.value;
				this.store.set({
					available: snap.status === "ready" || snap.status === "loading",
					loading: snap.status === "loading",
					section: value === void 0 ? this.store.getSnapshot().section : defaulted(value),
					data: this.store.getSnapshot().data,
					lastError: this.store.getSnapshot().lastError
				});
			}
			async refreshData() {
				try {
					const [factsR, profileR] = await Promise.all([this.r().listFacts({
						user: USER,
						limit: 200
					}), this.r().listProfile({ user: USER })]);
					const facts = unwrap(factsR);
					const profile = unwrap(profileR);
					this.store.set({
						...this.store.getSnapshot(),
						data: {
							facts: Array.isArray(facts.facts) ? facts.facts : [],
							profile: Array.isArray(profile.profile) ? profile.profile : []
						},
						lastError: void 0
					});
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async saveFact(fact) {
				try {
					await this.r().editFact({
						user: USER,
						fact_id: fact.fact_id,
						subject: fact.subject,
						predicate: fact.predicate,
						object: fact.object,
						content: fact.content,
						type: fact.type
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async deleteFact(factId) {
				try {
					await this.r().deleteFact({
						user: USER,
						fact_id: factId
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async fetchMemoryMd() {
				try {
					const text = unwrap(await this.r().memoryMd({ user: USER }));
					this.store.set({
						...this.store.getSnapshot(),
						data: {
							...this.store.getSnapshot().data,
							memoryMd: text
						},
						lastError: void 0
					});
					return text;
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
					throw err;
				}
			}
			async upsertProfile(section, key, value) {
				try {
					await this.r().upsertProfile({
						user: USER,
						section,
						key,
						value
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async deleteProfile(section, key) {
				try {
					await this.r().deleteProfile({
						user: USER,
						section,
						key
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async saveAllFacts(rows) {
				try {
					for (const row of rows) if (row.deleted) await this.r().deleteFact({
						user: USER,
						fact_id: row.fact_id
					});
					else await this.r().editFact({
						user: USER,
						fact_id: row.fact_id,
						subject: row.subject,
						predicate: row.predicate,
						object: row.object,
						content: row.content,
						type: row.type
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async saveAllProfile(rows) {
				try {
					for (const row of rows) if (row.deleted) await this.r().deleteProfile({
						user: USER,
						section: row.section,
						key: row.key
					});
					else await this.r().upsertProfile({
						user: USER,
						section: row.section,
						key: row.key,
						value: row.value
					});
					await this.refreshData();
				} catch (err) {
					this.store.set({
						...this.store.getSnapshot(),
						lastError: err?.message ?? String(err)
					});
				}
			}
			async backup() {
				return unwrap(await this.r().backup({ user: USER }));
			}
			async restore(payload) {
				const result = unwrap(await this.r().restore({
					user: USER,
					payload
				}));
				await this.refreshData();
				return {
					facts_written: Number(result.facts_written ?? 0),
					profile_written: Number(result.profile_written ?? 0)
				};
			}
		};
		/** Fill defaults onto a (possibly partial / identical) section value. */
		function defaulted(value) {
			return {
				enabled: value.enabled ?? true,
				captureEnabled: value.captureEnabled ?? true,
				llmExtractionEnabled: value.llmExtractionEnabled ?? true,
				contextInjectionEnabled: value.contextInjectionEnabled ?? true,
				extractionModel: value.extractionModel
			};
		}
		const dicts = {
			zh: {
				title: "记忆",
				intro: "管理 dsh-atom-memory 的记忆能力：开关、抽取模型、用户画像、记忆内容与备份恢复。",
				masterHeader: "记忆开关",
				masterDesc: "关闭后停用记忆插件：不再捕获、不再注入上下文，记忆工具也会拒绝调用。打开即时恢复。",
				modelHeader: "LLM 抽取模型",
				modelFollowDefault: "跟随 dsh 默认模型",
				modelManual: "手动指定模型",
				modelProvider: "Provider",
				modelProviderPlaceholder: "Provider ID，如 deepseek",
				modelProviderLabel: "Provider ID",
				modelName: "Model",
				modelNamePlaceholder: "如 deepseek-chat",
				modelNameLabel: "Model",
				modelBaseUrlLabel: "API 地址 (Base URL)",
				modelBaseUrlPlaceholder: "如 https://api.deepseek.com/v1",
				modelProtocolLabel: "API 协议",
				modelProtocolOpenai: "openai（OpenAI 兼容）",
				modelApiKeyLabel: "API 密钥",
				modelApiKeyPlaceholder: "sk-...",
				modelHint: "选择“手动指定模型”后可填 Provider ID 与 Model（跟随默认时留空）；填了 API 地址则由插件直连该 OpenAI 兼容端点，否则走 dsh 默认模型。",
				profileHeader: "User 画像编辑",
				profileEmpty: "暂无画像条目。",
				profileEditBtn: "编辑画像",
				profileSection: "属性(Section)",
				profileKey: "键(Key)",
				profileValue: "值(Value)",
				profileAdd: "添加条目",
				profileModalTitle: "编辑 User 画像",
				profileColSection: "Section",
				profileColKey: "Key",
				profileColValue: "Value",
				memoryHeader: "记忆与编辑",
				factsHeader: "原子事实",
				factsEmpty: "暂无原子事实。",
				memoryEditBtn: "编辑记忆",
				memoryModalTitle: "编辑记忆（原子事实）",
				factsColumns: "主语 / 谓词 / 宾语 / 类型 / 内容(折叠)",
				colSubject: "主语",
				colPredicate: "谓词",
				colObject: "宾语",
				colContent: "内容",
				colActions: "操作",
				newRowPlaceholder: "（新条目，保存时写入）",
				editSave: "保存修改",
				factDelete: "删除",
				delete: "删除",
				saveAll: "保存全部",
				cancel: "取消",
				addRow: "添加一行",
				close: "关闭",
				saving: "保存中…",
				memoryMdHeader: "memory.md 记忆视图（注入视图）",
				memoryMdDesc: "只读展示注入会话系统提示词的那份紧凑记忆视图——按类型分组、按重要度排序、不含 fact_id，与模型看到的文本一致。若要拿到 fact_id 定位某条事实，请用 memory_memory_md 工具查看完整清单。",
				memoryMdOpen: "查看 memory.md",
				memoryMdClose: "收起",
				memoryMdLoading: "正在加载…",
				memoryMdEmpty: "暂无内容（可能是空记忆或尚未加载）。",
				backupHeader: "记忆备份与恢复",
				backupDesc: "把记忆导出为 JSON 文件，或从 JSON 文件导入恢复（replace 语义：覆盖当前记忆）。",
				exportBtn: "导出 JSON",
				importBtn: "导入 JSON",
				restored: "已恢复：{facts} 条事实、{profile} 条画像。",
				error: "操作失败：{message}"
			},
			en: {
				title: "Memory",
				intro: "Manage dsh-atom-memory: master switch, extraction model, user profile, memory content, and backup/restore.",
				masterHeader: "Memory switch",
				masterDesc: "When off the memory plugin is disabled: no capture, no context injection, and memory tools refuse calls. Turning on restores immediately.",
				modelHeader: "LLM extraction model",
				modelFollowDefault: "Follow the dsh default model",
				modelManual: "Specify a model manually",
				modelProvider: "Provider",
				modelProviderPlaceholder: "Provider ID, e.g. deepseek",
				modelProviderLabel: "Provider ID",
				modelName: "Model",
				modelNamePlaceholder: "e.g. deepseek-chat",
				modelNameLabel: "Model",
				modelBaseUrlLabel: "API Base URL",
				modelBaseUrlPlaceholder: "e.g. https://api.deepseek.com/v1",
				modelProtocolLabel: "API protocol",
				modelProtocolOpenai: "openai (OpenAI-compatible)",
				modelApiKeyLabel: "API key",
				modelApiKeyPlaceholder: "sk-...",
				modelHint: "With “manual model” you can set Provider ID and Model (leave empty to follow default); filling in the API Base URL makes the plugin call that OpenAI-compatible endpoint directly, otherwise the dsh default model is used.",
				profileHeader: "User profile editing",
				profileEmpty: "No profile entries yet.",
				profileEditBtn: "Edit profile",
				profileSection: "Section",
				profileKey: "Key",
				profileValue: "Value",
				profileAdd: "Add entry",
				profileModalTitle: "Edit user profile",
				profileColSection: "Section",
				profileColKey: "Key",
				profileColValue: "Value",
				memoryHeader: "Memory & edit",
				factsHeader: "Atomic facts",
				factsEmpty: "No atomic facts yet.",
				memoryEditBtn: "Edit memory",
				memoryModalTitle: "Edit memory (atomic facts)",
				factsColumns: "Subject / Predicate / Object / Type / Content (collapsed)",
				colSubject: "Subject",
				colPredicate: "Predicate",
				colObject: "Object",
				colContent: "Content",
				colActions: "Actions",
				newRowPlaceholder: "(new row, written on save)",
				editSave: "Save changes",
				factDelete: "Delete",
				delete: "Delete",
				saveAll: "Save all",
				cancel: "Cancel",
				addRow: "Add row",
				close: "Close",
				saving: "Saving…",
				memoryMdHeader: "memory.md memory view (as injected)",
				memoryMdDesc: "Read-only render of the compact memory view injected into the session system prompt — grouped by type, ordered by importance, no fact_ids — i.e. exactly the text the model sees. For a full list carrying fact_ids (to locate one fact), use the memory_memory_md tool.",
				memoryMdOpen: "View memory.md",
				memoryMdClose: "Collapse",
				memoryMdLoading: "Loading…",
				memoryMdEmpty: "No content yet (empty memory or not loaded).",
				backupHeader: "Backup & restore",
				backupDesc: "Export memory to a JSON file, or import from a JSON file to restore (replace semantics: overwrites current memory).",
				exportBtn: "Export JSON",
				importBtn: "Import JSON",
				restored: "Restored: {facts} facts, {profile} profile rows.",
				error: "Operation failed: {message}"
			}
		};
		/** The locale namespace key used by this section. */
		const LOCALE_NS = "settings.atomMemory";
		//#endregion
		//#region src/client/styles.ts
		/**
		* Plain stylesheet for the memory settings section, injected as a
		* `<style data-plugin="dsh-atom-memory">` tag on mount (the browser bundle is
		* served standalone without a separate stylesheet, mirroring the harness's
		* style-injection convention). Class names mirror the `css` map in
		* `MemorySettingsSection.tsx`.
		*/
		const memorySettingsStyleText = `
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
`;
		/** Ensure the stylesheet is present exactly once (data-plugin guarded). */
		function ensureMemorySettingsStyle() {
			if (typeof document === "undefined") return;
			const tagId = "dsh-atom-memory/memory-settings";
			if (document.querySelector(`style[data-plugin-css="${tagId}"]`) !== null) return;
			const style = document.createElement("style");
			style.dataset.plugin = "dsh-atom-memory";
			style.dataset.pluginCss = tagId;
			style.textContent = memorySettingsStyleText;
			document.head.appendChild(style);
		}
		//#endregion
		//#region src/client/MemorySettingsSection.tsx
		/** The memory settings section rendered inside the dsh settings panel. */
		/**
		* Inline stylesheet (hand-Rolled). The browser bundle is built standalone
		* (tsdown, no lightningcss CSS-modules pass), so the class map lives here as a
		* plain object instead of a `.module.css` import — identical class names, no
		* build-time CSS plugin required.
		*/
		const css = {
			section: "atom-memory-section",
			header: "atom-memory-header",
			error: "atom-memory-error",
			status: "atom-memory-status",
			block: "atom-memory-block",
			switchRow: "atom-memory-switch-row",
			radioRow: "atom-memory-radio-row",
			inputs: "atom-memory-inputs",
			field: "atom-memory-field",
			fieldLabel: "atom-memory-field-label",
			hint: "atom-memory-hint",
			empty: "atom-memory-empty",
			add: "atom-memory-add",
			actions: "atom-memory-actions",
			fileLabel: "atom-memory-file-label",
			factRow: "atom-memory-fact-row",
			badge: "atom-memory-badge",
			factFields: "atom-memory-fact-fields",
			rowActions: "atom-memory-row-actions",
			rowBtn: "atom-memory-row-btn",
			btn: "atom-memory-btn",
			btnPrimary: "atom-memory-btn-primary",
			btnDanger: "atom-memory-btn-danger",
			btnRowDelete: "atom-memory-btn-row-delete",
			memoryMdView: "atom-memory-memory-md",
			overlay: "atom-memory-overlay",
			modal: "atom-memory-modal",
			modalHeader: "atom-memory-modal-header",
			modalBody: "atom-memory-modal-body",
			modalFooter: "atom-memory-modal-footer",
			editor: "atom-memory-editor",
			editorRowActions: "atom-memory-editor-row-actions"
		};
		/** Monotonic source of client-side draft-row identities. */
		let draftSeq = 0;
		/** @returns a fresh, process-unique draft-row identity. */
		const nextDraftUid = () => draftSeq += 1;
		/** Strip the client-only render identity before handing drafts to `onSave`. */
		function withoutUid(rows) {
			return rows.map(({ uid: _uid, ...rest }) => rest);
		}
		function MemorySettingsSection(props) {
			const { t } = props;
			const state = props.useMemorySettings((snapshot) => snapshot);
			const [status, setStatus] = (0, react.useState)();
			const [phase, setPhase] = (0, react.useState)("idle");
			const [modal, setModal] = (0, react.useState)();
			const [memoryMdBusy, setMemoryMdBusy] = (0, react.useState)(false);
			const [modelManual, setModelManual] = (0, react.useState)(() => Boolean(state.section.extractionModel?.provider || state.section.extractionModel?.model));
			const openMemoryMd = () => {
				setModal("memoryMd");
				if (state.data.memoryMd === void 0) {
					setMemoryMdBusy(true);
					props.fetchMemoryMd().finally(() => setMemoryMdBusy(false));
				}
			};
			const loadedRef = (0, react.useRef)(false);
			(0, react.useEffect)(() => {
				if (loadedRef.current) return;
				loadedRef.current = true;
				ensureMemorySettingsStyle();
				props.refreshData();
			}, [props]);
			const busy = state.loading || phase === "busy";
			const profile = state.data?.profile ?? [];
			const facts = state.data?.facts ?? [];
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: css.section,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
						className: css.header,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h2", { children: t("title") }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: t("intro") })]
					}),
					state.lastError ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: css.error,
						children: t("error", { message: state.lastError })
					}) : null,
					status ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: css.status,
						children: status
					}) : null,
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: busy || memoryMdBusy,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", { children: t("memoryMdHeader") }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: css.btn,
								style: { alignSelf: "flex-start" },
								disabled: busy || memoryMdBusy,
								onClick: openMemoryMd,
								children: t("memoryMdOpen")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.hint,
								children: t("memoryMdDesc")
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: !state.available,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", { children: t("masterHeader") }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
							className: css.switchRow,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								type: "checkbox",
								checked: state.section.enabled,
								onChange: (e) => {
									props.setEnabled(e.currentTarget.checked);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("masterDesc") })]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: !state.available,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", { children: t("modelHeader") }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: css.radioRow,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									type: "radio",
									name: "extraction-model-mode",
									checked: !modelManual,
									onChange: () => {
										setModelManual(false);
										props.setExtractionModel("", "");
									}
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("modelFollowDefault") })]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: css.radioRow,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									type: "radio",
									name: "extraction-model-mode",
									checked: modelManual,
									onChange: () => setModelManual(true)
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("modelManual") })]
							}),
							modelManual && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: css.field,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
										className: css.fieldLabel,
										children: t("modelProviderLabel")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
										placeholder: t("modelProviderPlaceholder"),
										value: state.section.extractionModel?.provider ?? "",
										onBlur: (e) => {
											props.setExtractionModelOverride({
												...state.section.extractionModel ?? {},
												provider: e.currentTarget.value
											});
										}
									})]
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: css.field,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
										className: css.fieldLabel,
										children: t("modelNameLabel")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
										placeholder: t("modelNamePlaceholder"),
										value: state.section.extractionModel?.model ?? "",
										onBlur: (e) => {
											props.setExtractionModelOverride({
												...state.section.extractionModel ?? {},
												model: e.currentTarget.value
											});
										}
									})]
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: css.field,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
										className: css.fieldLabel,
										children: t("modelBaseUrlLabel")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
										placeholder: t("modelBaseUrlPlaceholder"),
										value: state.section.extractionModel?.baseURL ?? "",
										onBlur: (e) => {
											props.setExtractionModelOverride({
												...state.section.extractionModel ?? {},
												baseURL: e.currentTarget.value.trim()
											});
										}
									})]
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: css.field,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
										className: css.fieldLabel,
										children: t("modelProtocolLabel")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("select", {
										value: state.section.extractionModel?.protocol || "openai",
										onChange: (e) => {
											props.setExtractionModelOverride({
												...state.section.extractionModel ?? {},
												protocol: e.currentTarget.value
											});
										},
										children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("option", {
											value: "openai",
											children: t("modelProtocolOpenai")
										})
									})]
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: css.field,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
										className: css.fieldLabel,
										children: t("modelApiKeyLabel")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
										type: "password",
										autoComplete: "off",
										placeholder: t("modelApiKeyPlaceholder"),
										value: state.section.extractionModel?.apiKey ?? "",
										onBlur: (e) => {
											props.setExtractionModelOverride({
												...state.section.extractionModel ?? {},
												apiKey: e.currentTarget.value
											});
										}
									})]
								})
							] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.hint,
								children: t("modelHint")
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: busy,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", { children: t("profileHeader") }),
							profile.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.empty,
								children: t("profileEmpty")
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: css.btn,
								style: { alignSelf: "flex-start" },
								onClick: () => setModal("profile"),
								children: t("profileEditBtn")
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: busy,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("legend", { children: [
								t("memoryHeader"),
								" · ",
								t("factsHeader")
							] }),
							facts.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.empty,
								children: t("factsEmpty")
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: css.btn,
								style: { alignSelf: "flex-start" },
								onClick: () => setModal("facts"),
								children: t("memoryEditBtn")
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: busy,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", { children: t("backupHeader") }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.hint,
								children: t("backupDesc")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: css.actions,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									disabled: busy,
									onClick: () => {
										setPhase("busy");
										props.backup().then((payload) => {
											const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
											const url = URL.createObjectURL(blob);
											const a = document.createElement("a");
											a.href = url;
											a.download = "atom-memory-backup.json";
											a.click();
											URL.revokeObjectURL(url);
											setStatus("✔ " + (/* @__PURE__ */ new Date()).toLocaleString());
											setPhase("idle");
										}).catch((err) => {
											setStatus(t("error", { message: err?.message ?? err }));
											setPhase("idle");
										});
									},
									children: t("exportBtn")
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
									className: css.fileLabel,
									children: [t("importBtn"), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
										type: "file",
										accept: "application/json,.json",
										hidden: true,
										disabled: busy,
										onChange: async (e) => {
											const file = e.currentTarget.files?.[0];
											e.currentTarget.value = "";
											if (!file) return;
											setPhase("busy");
											try {
												const text = await file.text();
												const payload = JSON.parse(text);
												const result = await props.restore(payload);
												setStatus(t("restored", {
													facts: String(result.facts_written),
													profile: String(result.profile_written)
												}));
											} catch (err) {
												setStatus(t("error", { message: err?.message ?? err }));
											} finally {
												setPhase("idle");
											}
										}
									})]
								})]
							})
						]
					}),
					modal === "memoryMd" ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(MemoryMdModal, {
						t,
						busy: memoryMdBusy,
						content: state.data.memoryMd,
						onClose: () => setModal(void 0)
					}) : null,
					modal === "facts" ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FactsEditorModal, {
						t,
						initial: facts,
						onSave: (rows) => props.saveAllFacts(rows),
						onClose: () => setModal(void 0)
					}) : null,
					modal === "profile" ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProfileEditorModal, {
						t,
						initial: profile,
						onSave: (rows) => props.saveAllProfile(rows),
						onClose: () => setModal(void 0)
					}) : null
				]
			});
		}
		/** A simple themed centered modal shell (header + scrollable body + footer). */
		function Modal(props) {
			const { t, title, children, footer, onClose } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: css.overlay,
				onClick: onClose,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: css.modal,
					role: "dialog",
					"aria-modal": "true",
					onClick: (e) => e.stopPropagation(),
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: css.modalHeader,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", { children: title }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: css.btn,
								onClick: onClose,
								children: t("close")
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: css.modalBody,
							children
						}),
						footer ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: css.modalFooter,
							children: footer
						}) : null
					]
				})
			});
		}
		/** The memory.md viewer modal (read-only). */
		function MemoryMdModal(props) {
			const { t, busy, content, onClose } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Modal, {
				t,
				title: t("memoryMdHeader"),
				onClose,
				children: busy && content === void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: css.hint,
					children: t("memoryMdLoading")
				}) : content === void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: css.empty,
					children: t("memoryMdEmpty")
				}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
					className: css.memoryMdView,
					children: content
				})
			});
		}
		/** Modal editor for atomic facts: Excel-like editable table + single save all. */
		function FactsEditorModal(props) {
			const { t, initial, onSave, onClose } = props;
			const [rows, setRows] = (0, react.useState)(() => initial.map((f) => ({
				uid: nextDraftUid(),
				fact_id: f.fact_id,
				subject: f.subject,
				predicate: f.predicate,
				object: f.object,
				content: f.content ?? "",
				type: f.type,
				deleted: false
			})));
			const [saving, setSaving] = (0, react.useState)(false);
			const setRow = (index, patch) => setRows((prev) => prev.map((r, i) => i === index ? {
				...r,
				...patch
			} : r));
			const addRow = () => setRows((prev) => [...prev, {
				uid: nextDraftUid(),
				fact_id: "",
				subject: "",
				predicate: "",
				object: "",
				content: "",
				deleted: false
			}]);
			const save = () => {
				setSaving(true);
				Promise.resolve(onSave(withoutUid(rows))).finally(() => {
					setSaving(false);
					onClose();
				});
			};
			const footer = /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				type: "button",
				className: css.btn,
				onClick: onClose,
				disabled: saving,
				children: t("cancel")
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				type: "button",
				className: css.btnPrimary,
				onClick: save,
				disabled: saving,
				children: saving ? t("saving") : t("saveAll")
			})] });
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Modal, {
				t,
				title: t("memoryModalTitle"),
				footer,
				onClose,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("table", {
					className: css.editor,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("thead", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("tr", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colSubject") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colPredicate") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colObject") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colContent") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colActions") })
					] }) }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("tbody", { children: rows.map((row, i) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("tr", {
						style: row.deleted ? { opacity: .45 } : void 0,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.subject,
								disabled: row.deleted,
								placeholder: t("newRowPlaceholder"),
								onChange: (e) => setRow(i, { subject: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.predicate,
								disabled: row.deleted,
								onChange: (e) => setRow(i, { predicate: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.object,
								disabled: row.deleted,
								onChange: (e) => setRow(i, { object: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("textarea", {
								value: row.content ?? "",
								disabled: row.deleted,
								onChange: (e) => setRow(i, { content: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: css.editorRowActions,
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: css.btnRowDelete,
									onClick: () => setRow(i, { deleted: !row.deleted }),
									children: row.deleted ? t("addRow") : t("factDelete")
								})
							}) })
						]
					}, row.uid)) })]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: css.add,
					style: { marginTop: 10 },
					onClick: addRow,
					children: t("addRow")
				})]
			});
		}
		/** Modal editor for the user profile: Excel-like editable table + single save all. */
		function ProfileEditorModal(props) {
			const { t, initial, onSave, onClose } = props;
			const [rows, setRows] = (0, react.useState)(() => initial.map((r) => ({
				uid: nextDraftUid(),
				section: r.section,
				key: r.key,
				value: r.value,
				deleted: false
			})));
			const [saving, setSaving] = (0, react.useState)(false);
			const setRow = (index, patch) => setRows((prev) => prev.map((r, i) => i === index ? {
				...r,
				...patch
			} : r));
			const addRow = () => setRows((prev) => [...prev, {
				uid: nextDraftUid(),
				section: "",
				key: "",
				value: "",
				deleted: false
			}]);
			const save = () => {
				setSaving(true);
				Promise.resolve(onSave(withoutUid(rows))).finally(() => {
					setSaving(false);
					onClose();
				});
			};
			const footer = /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				type: "button",
				className: css.btn,
				onClick: onClose,
				disabled: saving,
				children: t("cancel")
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				type: "button",
				className: css.btnPrimary,
				onClick: save,
				disabled: saving,
				children: saving ? t("saving") : t("saveAll")
			})] });
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Modal, {
				t,
				title: t("profileModalTitle"),
				footer,
				onClose,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("table", {
					className: css.editor,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("thead", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("tr", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("profileColSection") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("profileColKey") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("profileColValue") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("th", { children: t("colActions") })
					] }) }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("tbody", { children: rows.map((row, i) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("tr", {
						style: row.deleted ? { opacity: .45 } : void 0,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.section,
								disabled: row.deleted,
								onChange: (e) => setRow(i, { section: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.key,
								disabled: row.deleted,
								onChange: (e) => setRow(i, { key: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: row.value,
								disabled: row.deleted,
								onChange: (e) => setRow(i, { value: e.currentTarget.value })
							}) }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("td", { children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: css.editorRowActions,
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: css.btnRowDelete,
									onClick: () => setRow(i, { deleted: !row.deleted }),
									children: row.deleted ? t("addRow") : t("factDelete")
								})
							}) })
						]
					}, row.uid)) })]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: css.add,
					style: { marginTop: 10 },
					onClick: addRow,
					children: t("addRow")
				})]
			});
		}
		//#endregion
		//#region src/client/remote.ts
		/** The Remote wire namespace this browser half mounts (matches the Host binding). */
		const REMOTE_NAMESPACE = "atomMemory";
		const JSON_CODEC = {
			mode: "strict",
			typeSymbol: "dsh-atom-memory#JsonValue",
			schema: { parse: (value) => value }
		};
		/** One descriptor for a Host method whose single argument is a JSON `args` object. */
		function jsonArgsMethod(method, hasArgs) {
			return {
				id: `${REMOTE_NAMESPACE}/${method}`,
				service: "atomMemoryController",
				namespace: REMOTE_NAMESPACE,
				method,
				invocation: { kind: "direct" },
				parameters: hasArgs ? [{
					name: "args",
					wire: "args",
					source: "json",
					codec: JSON_CODEC
				}] : [],
				result: JSON_CODEC
			};
		}
		/** The `atomMemory` contribution mounted by this browser half. */
		const ATOM_MEMORY_REMOTE = {
			package: "dsh-atom-memory",
			descriptors: [
				jsonArgsMethod("listFacts", true),
				jsonArgsMethod("editFact", true),
				jsonArgsMethod("deleteFact", true),
				jsonArgsMethod("memoryMd", true),
				jsonArgsMethod("listProfile", true),
				jsonArgsMethod("upsertProfile", true),
				jsonArgsMethod("deleteProfile", true),
				jsonArgsMethod("backup", true),
				jsonArgsMethod("restore", true),
				jsonArgsMethod("getRuntime", false)
			]
		};
		//#endregion
		//#region src/client/index.ts
		/** The settings namespace registered by the Host plugin. */
		const SETTINGS_NAMESPACE = "atom-memory";
		/** Required services (cordis fiber inject). */
		const inject = [
			"slots",
			"locale",
			"settingsScope",
			"remote"
		];
		/**
		* Mount the memory settings section.
		* @param ctx - the browser plugin context.
		*/
		async function apply(ctx) {
			const t = ctx.locale.bind(LOCALE_NS);
			ctx.effect(() => ctx.locale.register(LOCALE_NS, dicts), "atom-memory: section dictionaries");
			const disposeRemote = await ctx.remote.$mount(ATOM_MEMORY_REMOTE);
			const memoryRemote = ctx.get("remote.atomMemory");
			if (memoryRemote === void 0) ctx.logger && ctx.logger.warn("[dsh-atom-memory] remote.atomMemory was not provided after mount; memory panel remote calls disabled");
			const controller = new MemorySettingsController(ctx.settingsScope.bind({ namespace: SETTINGS_NAMESPACE }), memoryRemote);
			ctx.effect(() => () => {
				controller.dispose();
			}, "atom-memory: controller");
			ctx.slots.inject("settings.section", () => ctx.slots.register({
				name: "settings.section",
				id: "memory",
				order: 60,
				label: () => t("title"),
				locale: LOCALE_NS,
				inject: () => controller.inject()
			}, MemorySettingsSection));
			return async () => {
				controller.dispose();
				await disposeRemote();
			};
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map