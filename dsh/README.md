# dsh-atom-memory

dsh 侧接入插件：把纯 Python 记忆库（`dsh-atom-memory`）作为**独立子进程**接入
DeepSeek Harness，通过 NDJSON stdio 桥接，暴露 `memory_*` 工具、LLM-first
抽取、以及 durable 会话捕获钩子。**不改动 dsh 源码，不 import Python 库** ——
所有记忆逻辑都运行在被隔离的 Python 子进程里。

- **桥接**：`child_process.spawn('python', ['-m', 'atom_memory.rpc'])` +
  stdin/stdout NDJSON 请求/响应 + stderr 带 tag 的后台事件。
- **LLM-first**：默认用 dsh 当前预设的第一个模型（`agentDefaultModel`
  `currentSelection()`）调用 `ctx.llm` 抽取，类型化候选交给 Python 持久化；
  LLM 不可用/为空时回退到 Python 规则抽取 —— 绝不静默丢弃。
- **捕获钩子**：per-message 捕获、压缩前抢救（pre-compression rescue）、
  周期性微调（periodic nudge），只读 durable 会话事件。
- **生命周期**：子进程与插件绑定，卸载时优雅 stop 落库并 kill。

## 安装与挂载

```bash
cd dsh
pnpm install
pnpm build       # -> lib/index.mjs
```

挂载：`package.json` 声明 `dsh.bundle.patch = ./cordis.patch.yml`，将其作为一个
外置插件 `dsh plugin add`（或加入 profile）。`cordis.patch.yml` 是 **insert-only**
补丁，挂上后即插入 `atom-memory` 条目。

## 配置（`Config`，schemastery）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `dbPath` | `~/.dsh/atom-memory/memory.db` | Python 侧 SQLite 路径 |
| `pythonBin` | `''`（用 PATH 上的 `python`） | 覆盖解释器（如 venv） |
| `autostart` | `true` | 加载即启动桥接（部署期开关） |
| `captureEnabled` | `true` | per-message 捕获 |
| `llmExtractionEnabled` | `true` | 启用以 dsh 默认模型作 LLM-first 抽取 |
| `preCompressionCapture` | `true` | 压缩前抢救 |
| `nudgeEnabled` | `true` | 周期微调（写路径） |
| `nudgeIntervalMinutes` | `30` | 微调周期 |
| `maxRecalledFacts` | `10` | 每次召回给模型的条数上限 |
| `memoryMdTokens` | `1500` | memory.md token 上限 |
| `rpcTimeoutMs` | `30000` | 单次 RPC 超时 |

## 工具（模型可见面）

| 工具 | 说明 |
| --- | --- |
| `memory_add` | 显式记住原始内容（LLM-first → 规则回退） |
| `memory_summary` | 返回记忆的聚合摘要（属性/偏好/工作流程/事件/轻量知识），"先看摘要、再查明细"入口；附带覆盖的 `fact_id` 清单与「未展开长文知识」提示 |
| `memory_recall` | 语义+全文混合召回；模型可见内容含 `fact_id`、`type` **与 `content` 正文**，并前置聚合摘要 |
| `memory_forget` | 软删除（retract）一条事实 |
| `memory_memory_md` | 渲染 memory.md（含 fact_id） |
| `memory_user_md` | 渲染用户画像 markdown |
| `memory_stats` | 记忆统计计数 |

> **注意：模型只读 `output.render` 的返回值**（`ToolResult.content` 才是 model-facing），
> `output.schema` 仅用于校验/类型。因此事实的任何字段若未写进 `render`，对模型就是不可见的。

### 查询（下钻）链路

```
memory_summary（概览：聚合摘要 + 覆盖的 fact_id + 未展开长文知识提示）
      │
      ├─▶ 需要具体事实 ──▶ memory_recall(query)
      │        ├─ render 输出 fact_id / type / content 正文（知识类事实的正文即答案）
      │        └─ 前置【摘要】块，便于把召回结果放回整体语境
      │
      └─▶ 需要全量清单 ──▶ memory_memory_md（含 fact_id + 知识正文折叠行，受 token 预算截断）
```

长文知识（`sop` / `few_shot`）的正文**被有意排除在摘要文本之外**（体量太大），但摘要会
显式提示「另有 N 条未展开」并给出 `fact_id`，避免"先看摘要"反而把需要下钻的内容藏起来。

> **用户作用域**：所有 `memory_*` 工具的 `user_id` 统一落入 fallback 用户作用域
> （`global`），与写入侧（capture / LLM-first）保持一致，因此记忆能在会话间
> 共享与检索；当前会话 id 仅作为 `session_id` 记录归属溯源。调用方可通过可选的
> `user` 参数显式指定其他用户作用域。

## Model Experience

模型被注入一段系统提示，说明它拥有持久记忆以及哪个工具用于保存/读取，并被告知
「用户明确陈述的偏好/决策要保存」。`memory_summary` 与 `memory_recall` 都会返回
聚合摘要，模型可先读摘要再按需查明细；召回结果的可见文本包含 `fact_id`、`type` 与
`content` 正文；长文知识（SOP/few-shot）不进摘要但仍可被召回并展开正文。

> **摘要的读取语义**：写入侧对摘要重建做了防抖，而被防抖推迟的重建不会被重新排期，
> 因此摘要可能长期停留在 `stale`。读取路径（`recall` / `summary`）会在返回前按需
> 重建，保证读到的摘要始终是当前内容（聚合是纯内存字符串工作，无模型调用，代价低）。

## Known Limitations

- **跨进程一致性**：记忆完全在 Python 侧；dsh 重启后需重新 `start` 桥接
  （`autostart` 会自启）。子进程异常退出时在途请求会被拒绝并记录，插件不自动
  无上限重生（最多 3 次后退避）。
- **LLM 默认模型**：抽取使用 `agentDefaultModel.currentSelection()`；若当前预设
  无默认模型，则 LLM 路径关闭，退化为纯规则抽取。
- **捕获钩子**：per-message/压缩前/微调钩子是 best-effort（不会打断主线循环）；
  强特征关键词门避免整段闲聊入库。`user/message` 等事件来自 dsh 的 durable
  会话日志，可重放。
- **Windows**：stdio 轮询通过 `run_in_executor` 线程读取 stdin（Proactor 事件循环
  无法用 `connect_read_pipe` 驱动管道读）。

## 开发

```bash
pnpm run typecheck   # tsc --noEmit
pnpm run test        # vitest（bridge 用假子进程，hermetic）
pnpm run build       # tsdown -> lib/
```

端到端验证（真实 Python 子进程）：见根 README 的 <a href="#integration">桥接集成</a>。


