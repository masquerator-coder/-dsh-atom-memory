# Changelog

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
