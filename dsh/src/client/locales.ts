/** Localized copy for the memory settings section. */

const zh = {
  title: '记忆',
  intro: '管理 dsh-atom-memory 的记忆能力：开关、抽取模型、用户画像、记忆内容与备份恢复。',
  // 1) master switch
  masterHeader: '记忆开关',
  masterDesc: '关闭后停用记忆插件：不再捕获、不再注入上下文，记忆工具也会拒绝调用。打开即时恢复。',
  // 2) extraction model
  modelHeader: 'LLM 抽取模型',
  modelFollowDefault: '跟随 dsh 默认模型',
  modelManual: '手动指定模型',
  modelProvider: 'Provider',
  modelProviderPlaceholder: '如 deepseek',
  modelName: 'Model',
  modelNamePlaceholder: '如 deepseek-chat',
  modelHint: 'provider 留空视为跟随 dsh 默认模型。',
  // 3) profile
  profileHeader: 'User 画像编辑',
  profileEmpty: '暂无画像条目。',
  profileSection: '属性(Section)',
  profileKey: '键(Key)',
  profileValue: '值(Value)',
  profileAdd: '添加条目',
  // 4) memory & edit
  memoryHeader: '记忆与编辑',
  factsHeader: '原子事实',
  factsEmpty: '暂无原子事实。',
  factsColumns: '主语 / 谓词 / 宾语 / 类型 / 内容(折叠)',
  editSave: '保存修改',
  // 5) backup / restore
  backupHeader: '记忆备份与恢复',
  backupDesc: '把记忆导出为 JSON 文件，或从 JSON 文件导入恢复（replace 语义：覆盖当前记忆）。',
  exportBtn: '导出 JSON',
  importBtn: '导入 JSON',
  restored: '已恢复：{facts} 条事实、{profile} 条画像。',
  error: '操作失败：{message}',
} as const

const en: Record<keyof typeof zh, string> = {
  title: 'Memory',
  intro: 'Manage dsh-atom-memory: master switch, extraction model, user profile, memory content, and backup/restore.',
  masterHeader: 'Memory switch',
  masterDesc: 'When off the memory plugin is disabled: no capture, no context injection, and memory tools refuse calls. Turning on restores immediately.',
  modelHeader: 'LLM extraction model',
  modelFollowDefault: 'Follow the dsh default model',
  modelManual: 'Specify a model manually',
  modelProvider: 'Provider',
  modelProviderPlaceholder: 'e.g. deepseek',
  modelName: 'Model',
  modelNamePlaceholder: 'e.g. deepseek-chat',
  modelHint: 'Leaving provider empty follows the dsh default model.',
  profileHeader: 'User profile editing',
  profileEmpty: 'No profile entries yet.',
  profileSection: 'Section',
  profileKey: 'Key',
  profileValue: 'Value',
  profileAdd: 'Add entry',
  memoryHeader: 'Memory & edit',
  factsHeader: 'Atomic facts',
  factsEmpty: 'No atomic facts yet.',
  factsColumns: 'Subject / Predicate / Object / Type / Content (collapsed)',
  editSave: 'Save changes',
  backupHeader: 'Backup & restore',
  backupDesc: 'Export memory to a JSON file, or import from a JSON file to restore (replace semantics: overwrites current memory).',
  exportBtn: 'Export JSON',
  importBtn: 'Import JSON',
  restored: 'Restored: {facts} facts, {profile} profile rows.',
  error: 'Operation failed: {message}',
}

export const dicts = { zh, en }

/** The union of dictionary keys (used to declare the locale namespace). */
export type MemorySettingsLocaleKey = keyof typeof zh

/** The locale namespace key used by this section. */
export const LOCALE_NS = 'settings.atomMemory'
