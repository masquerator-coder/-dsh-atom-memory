# Changelog

## [0.1.1] — 修复 git 分发装配

### Fixed
- **`inject` 增加 `systemPrompt`**：`context.ts` 里 `ctx.systemPrompt.section(...)`
  此前未声名为依赖，装配时报
  `cannot get property "systemPrompt" without inject`，导致插件挂载失败、进而
  拖垮 `dsh web` 启动（EPIPE 崩溃）。现与参考 dsh-memory 一致：
  `inject = ['tools', 'systemPrompt'] as const`。
- **bridge 子进程流 error 处理**：`bridge.ts` 现在对 child
  `stdin`/`stdout`/`stderr` 及 `spawn` `error` 事件挂监听（含 EPIPE），子进程
  异常死亡改走共享 `handleExit` → `rejectAll` 收尾，不再因未捕获的 stream
  `error` 事件使宿主进程致命崩溃。
- **仓库根节点 bundle 壳**：根 `package.json`（`name: dsh-atom-memory-dsh`、
  `dsh.bundle.patch → ./dsh/cordis.patch.yml`）让 `dsh plugin add <git-url>`
  把整个仓库安装为 profile layer；`dsh/lib` 产物入库（对齐 dsh-memory），
  git clone 无需现场 build。

## [0.1.0] — dsh 接入（未 release）

### Added
- **Python 侧** `dsh_atom_memory/rpc.py`：NDJSON stdio RPC 服务入口
  （`python -m dsh_atom_memory.rpc`），映射 `AtomMem` 公有 API，支持
  `start`/`stop`/`health`/`add`/`recall`/`replace`/`forget`/`forget_all`/
  `memory_md`/`user_md`/`stats`/`persist_candidates`，后台事件经 stderr
  tag 上报。
- **Python 侧** worker 新增 `persist_pre` 任务：接受 dsh 侧 LLM 预抽取的
  类型化候选，复用 validate + persist 链（获得冲突消解与 identical-SPO 去重）。
- **dsh 侧** `dsh/` 独立 npm 子包：`bridge.ts`（子进程 NDJSON 管理）、
  `tools.ts`（memory_* 工具）、`llm-extractor.ts`（agentDefaultModel +
  ctx.llm LLM-first）、`capture.ts`（per-message / pre-compression /
  periodic nudge 三钩子）、`context.ts`（系统提示 awareness 段）、
  `index.ts`（装配 + 生命周期可逆性）。
- 测试：Python `tests/test_rpc.py`（6），dsh vitest 19（bridge/llm-extractor/
  capture）。真实 Python 子进程端到端桥接 smoke 通过。

### Fixed
- `dsh_atom_memory/rpc.py` Windows stdin：改为 `run_in_executor` 线程读
  stdin（`connect_read_pipe` 在 Proactor 下报 WinError 6）。

### Notes
- LLM-first 抽取在 dsh 进程内执行（那里有 `ctx.llm`），类型化候选经
  `persist_candidates` → `persist_pre` 交给 Python 持久化；规则抽取始终是
  Python 侧的回退。语义符合「LLM 默认用 dsh 配置的第一个模型」。
