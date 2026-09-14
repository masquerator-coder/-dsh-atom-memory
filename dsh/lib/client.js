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
					refreshData: () => this.refreshData(),
					saveFact: (fact) => this.saveFact(fact),
					upsertProfile: (section, key, value) => this.upsertProfile(section, key, value),
					deleteProfile: (section, key) => this.deleteProfile(section, key),
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
					const [facts, profile] = await Promise.all([this.r().listFacts({
						user: USER,
						limit: 200
					}), this.r().listProfile({ user: USER })]);
					this.store.set({
						...this.store.getSnapshot(),
						data: {
							facts: facts.facts,
							profile: profile.profile
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
			async backup() {
				return this.r().backup({ user: USER });
			}
			async restore(payload) {
				const result = await this.r().restore({
					user: USER,
					payload
				});
				await this.refreshData();
				return result;
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
				modelProviderPlaceholder: "如 deepseek",
				modelName: "Model",
				modelNamePlaceholder: "如 deepseek-chat",
				modelHint: "provider 留空视为跟随 dsh 默认模型。",
				profileHeader: "User 画像编辑",
				profileEmpty: "暂无画像条目。",
				profileSection: "属性(Section)",
				profileKey: "键(Key)",
				profileValue: "值(Value)",
				profileAdd: "添加条目",
				memoryHeader: "记忆与编辑",
				factsHeader: "原子事实",
				factsEmpty: "暂无原子事实。",
				factsColumns: "主语 / 谓词 / 宾语 / 类型 / 内容(折叠)",
				editSave: "保存修改",
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
				modelProviderPlaceholder: "e.g. deepseek",
				modelName: "Model",
				modelNamePlaceholder: "e.g. deepseek-chat",
				modelHint: "Leaving provider empty follows the dsh default model.",
				profileHeader: "User profile editing",
				profileEmpty: "No profile entries yet.",
				profileSection: "Section",
				profileKey: "Key",
				profileValue: "Value",
				profileAdd: "Add entry",
				memoryHeader: "Memory & edit",
				factsHeader: "Atomic facts",
				factsEmpty: "No atomic facts yet.",
				factsColumns: "Subject / Predicate / Object / Type / Content (collapsed)",
				editSave: "Save changes",
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
			hint: "atom-memory-hint",
			empty: "atom-memory-empty",
			add: "atom-memory-add",
			actions: "atom-memory-actions",
			fileLabel: "atom-memory-file-label",
			factRow: "atom-memory-fact-row",
			badge: "atom-memory-badge",
			factFields: "atom-memory-fact-fields"
		};
		function MemorySettingsSection(props) {
			const { t } = props;
			const state = props.useMemorySettings((snapshot) => snapshot);
			const [status, setStatus] = (0, react.useState)();
			const [phase, setPhase] = (0, react.useState)("idle");
			const loadedRef = (0, react.useRef)(false);
			(0, react.useEffect)(() => {
				if (loadedRef.current) return;
				loadedRef.current = true;
				ensureMemorySettingsStyle();
				props.refreshData();
			}, [props]);
			const busy = state.loading || phase === "busy";
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
									checked: !state.section.extractionModel?.provider,
									onChange: () => {
										props.setExtractionModel("", "");
									}
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("modelFollowDefault") })]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: css.radioRow,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									type: "radio",
									name: "extraction-model-mode",
									checked: Boolean(state.section.extractionModel?.provider),
									onChange: () => {
										props.setExtractionModel(state.section.extractionModel?.provider || "", state.section.extractionModel?.model || "");
									}
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("modelManual") })]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: css.inputs,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									placeholder: t("modelProviderPlaceholder"),
									value: state.section.extractionModel?.provider ?? "",
									onBlur: (e) => {
										props.setExtractionModel(e.currentTarget.value, state.section.extractionModel?.model ?? "");
									}
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									placeholder: t("modelNamePlaceholder"),
									value: state.section.extractionModel?.model ?? "",
									onBlur: (e) => {
										props.setExtractionModel(state.section.extractionModel?.provider ?? "", e.currentTarget.value);
									}
								})]
							}),
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
							state.data.profile.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: css.empty,
								children: t("profileEmpty")
							}) : state.data.profile.map((row, index) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProfileRow, {
								t,
								row,
								onSave: (d) => {
									props.upsertProfile(d.section, d.key, d.value);
								},
								onDelete: () => {
									props.deleteProfile(row.section, row.key);
								}
							}, `${row.section}:${row.key}:${index}`)),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: css.add,
								onClick: () => {
									props.upsertProfile("", "", "");
								},
								children: t("profileAdd")
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
						className: css.block,
						disabled: busy,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("legend", { children: [
							t("memoryHeader"),
							" · ",
							t("factsHeader")
						] }), state.data.facts.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: css.empty,
							children: t("factsEmpty")
						}) : state.data.facts.map((fact) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FactRow, {
							t,
							fact,
							onSave: (d) => {
								props.saveFact({
									...fact,
									...d
								});
							}
						}, fact.fact_id))]
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
					})
				]
			});
		}
		function FactRow(props) {
			const { t, fact } = props;
			const [draft, setDraft] = (0, react.useState)(() => ({
				fact_id: fact.fact_id,
				subject: fact.subject,
				predicate: fact.predicate,
				object: fact.object,
				content: fact.content ?? ""
			}));
			const set = (patch) => setDraft((prev) => ({
				...prev,
				...patch
			}));
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: css.factRow,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: css.badge,
						children: draft.fact_id.slice(0, 8)
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: css.factFields,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.subject,
								onChange: (e) => set({ subject: e.currentTarget.value })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.predicate,
								onChange: (e) => set({ predicate: e.currentTarget.value })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.object,
								onChange: (e) => set({ object: e.currentTarget.value })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("textarea", {
								value: draft.content,
								onChange: (e) => set({ content: e.currentTarget.value })
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						onClick: () => props.onSave(draft),
						children: t("editSave")
					})
				]
			});
		}
		function ProfileRow(props) {
			const { t, row } = props;
			const [draft, setDraft] = (0, react.useState)(() => ({
				section: row.section,
				key: row.key,
				value: row.value,
				newRow: false
			}));
			const set = (patch) => setDraft((prev) => ({
				...prev,
				...patch
			}));
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: css.factRow,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: css.factFields,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.section,
								placeholder: t("profileSection"),
								onChange: (e) => set({ section: e.currentTarget.value })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.key,
								placeholder: t("profileKey"),
								onChange: (e) => set({ key: e.currentTarget.value })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: draft.value,
								placeholder: t("profileValue"),
								onChange: (e) => set({ value: e.currentTarget.value })
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						onClick: () => props.onSave(draft),
						children: t("editSave")
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						onClick: props.onDelete,
						children: "×"
					})
				]
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
			const controller = new MemorySettingsController(ctx.settingsScope.bind({ namespace: SETTINGS_NAMESPACE }), ctx.remote.atomMemory);
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