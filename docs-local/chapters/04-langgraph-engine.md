# 04 LangGraph 引擎与运行模型

> 基于 DeerFlow 最新源码（本仓库 commit 2672e209，2026-09）编写。
> 本章代码位于 `backend/packages/harness/deerflow/`（图与装配）、
> `backend/packages/harness/deerflow/runtime/`（运行引擎）与
> `backend/app/gateway/`（Gateway 接入面）。

## 本章导览

DeerFlow 自称 "LangGraph-based AI super agent system"，但它的主图**不是手写节点
图**：`agents/lead_agent/agent.py` 里没有一个 `StateGraph`/`add_node`/`add_edge`
调用，整个 lead agent 是 `langchain.agents.create_agent()` 按配置一次性"编译"
出来的工具调用智能体（模型 + 工具 + 中间件 + 系统提示词 + 状态 schema），节点与
边由框架隐式生成。因此本章把状态设计、注册、执行模型讲透，而把"行为怎么注进图"
（那是中间件干的事）留给第 6 章。

运行引擎分三层，各司其职：

1. **图层（compile-time）**——`ThreadState` 状态 schema、`create_agent` 编译、
   `langgraph.json` 注册的 `make_lead_agent` ABI；
2. **执行层（run-time）**——`runtime/` 里的 `RunManager`（run 记账/准入/幂等/并发
   治理/取消）+ `run_agent()`（worker 主循环）+ `StreamBridge`（流事件分发），这是
   DeerFlow 自研的、运行在 Gateway 进程内的 **LangGraph 兼容运行时**；
3. **接入层**——Gateway HTTP 路由（`/api/threads/{id}/runs*`）与嵌入式
   `DeerFlowClient`（`packages/harness/deerflow/client.py`）两条并行路径。

一个 run 的完整旅程：`POST /threads/{id}/runs`（或 `DeerFlowClient.stream()`）
→ `RunManager` 准入 → `run_agent()` 用带 `checkpointer` 的 config 驱动编译好的图
`astream` → checkpoint 写库 → 流事件经 `StreamBridge` 出 SSE。

---

## 4.1 状态设计：从 AgentState 到 ThreadState

### 4.1.1 AgentState 基类与 messages 规约

`ThreadState` 继承自 `langchain.agents.AgentState`（`langchain` 对
`TypedDict` 状态类型的薄包装），`messages` 键由
`langchain_core` 的 `add_messages` 规约管理：节点写入的新消息按 message `id`
追加/替换/删除（`RemoveMessage`）。DeerFlow 的关键在于**不满足于默认规约**——一个
超级智能体要跨轮持久化沙箱、待办、目标、委托台账、技能上下文等共享状态，这些字段
几乎每个都有自定义 reducer。

### 4.1.2 ThreadState 字段与自定义 reducer

定义在 `backend/packages/harness/deerflow/agents/thread_state.py`（全部字段
与规约一眼看完）：

```python
class ThreadState(AgentState):
    sandbox: SandboxStateField                 # Annotated[..., merge_sandbox]
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    goal: Annotated[GoalState | None, merge_goal]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    promoted: Annotated[PromotedTools | None, merge_promoted]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
    skill_context: Annotated[list[SkillEntry], merge_skill_context]
    summary_text: NotRequired[str | None]
    background_tasks: NotRequired[list[BackgroundTaskState]]
```

各自定义 reducer 的语义（都定义在本文件，单文件即可读全）：

| 字段 | reducer | 语义 |
|------|---------|------|
| `sandbox` | `merge_sandbox` | 只接受幂等写入：同一 thread 出现两个不同 `sandbox_id` 视为生命周期/隔离 bug，直接抛 `ValueError` fail-closed |
| `artifacts` | `merge_artifacts` | 追加 + `dict.fromkeys` 去重保序 |
| `todos` | `merge_todos` | `None` 表示节点没碰 → 保留旧值；显式新值（含空表）覆盖 |
| `goal` | `merge_goal` | 节点不写就保留当前 goal；仅当 goal 写入者显式替换时才变 |
| `viewed_images` | `merge_viewed_images` | 按 path 合并元数据；**空 dict 特例 = 清空**（中间件处理后清场）；只存轻量元数据，图像字节按需从盘读（#4138） |
| `promoted` | `merge_promoted` | 延迟工具提升表，按 `catalog_hash` 作用域：hash 变了整体替换（防漂移后旧名字暴露新工具），同 hash 则并集去重 |
| `delegations` | `merge_delegations` | 委托台账：同 id 后者胜但保留首见顺序；**终态永不被非终态降级**；封顶最近 50 条（`_DELEGATION_LEDGER_MAX_ENTRIES`） |
| `skill_context` | `merge_skill_context` | 技能引用按 `path` 去重、最近读取者排前、封顶 8 条、description 截断 500 字符；存的是"引用"而非 SKILL.md 正文 |

### 4.1.3 delta 模式：DeltaThreadState 与 DeltaChannel

默认 checkpoint 存储整份 `messages` 快照（full 模式）。`config.yaml` 里
`database.checkpoint_channel_mode: delta` 时，messages 改用 LangGraph 1.2 的
`DeltaChannel`：checkpoint 只存"哨兵 + 每步增量写入"，存储/序列化从随轮数
O(N²) 降到 O(N)：

```python
def delta_messages_field(snapshot_frequency: int = DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY) -> Any:
    return Annotated[
        list[AnyMessage],
        DeltaChannel(merge_message_writes, snapshot_frequency=snapshot_frequency),
    ]

class DeltaThreadState(ThreadState):
    messages: DELTA_MESSAGES_FIELD
```

`merge_message_writes`（thread_state.py:323）是 delta 写入的折叠规约：把当前
消息态归一化一次，再用 message-id 位置索引按序折叠每次写入、延迟墓碑压缩，线性
时间复刻公开 `add_messages` 的全部语义——重复 id、原位替换、删除报错、
`REMOVE_ALL_MESSAGES`、空写入报错、缺 id 分配顺序——不重扫累积态。LangGraph
私有的 `_messages_delta_reducer` 同样线性但有意省略部分公开语义，不能直接替代
（有差分测试全量覆盖该契约）。

**模式是进程冻结的、改配置必须重启**：`assemble_lead_agent` 和嵌入式
`DeerFlowClient` 在建图前调用
`runtime/checkpoint_mode.py::freeze_checkpoint_channel_mode` /
`freeze_checkpoint_snapshot_frequency` 冻结模式与快照节奏，再用模式匹配的 schema
编译：`thread_state.get_thread_state_schema(mode)` 返回 `ThreadState` 或
`DeltaThreadState`；中间件 schema 走 `adapt_state_schema_for_mode` +
`normalize_middleware_state_schemas` 逐个适配（按 schema/模式/频率缓存）。同进程
出现第二种模式或频率直接抛 `CheckpointModeReconfigurationError`。
`database.checkpoint_delta.snapshot_frequency`（默认 10）决定 DeltaChannel 的
快照节奏，随模式一起冻结，且**每个共享同一 checkpoint 库的进程必须一致**——节奏
存在编译图的 channel 表里，故意不写进 checkpoint 元数据。

**兼容性不对称、fail-closed**：delta 写出的每个 checkpoint 带元数据标记
`deerflow_checkpoint_channel_mode: "delta"`（`inject_checkpoint_mode` 注入；无标记
即 full，老 checkpoint 无需迁移）。任何读写前 `ensure_checkpoint_mode_compatible`
拒绝 full 进程打开 delta thread（HTTP 409）——full 直读 delta blob 会静默拿到空的
messages。反向允许：delta 进程透明读 full checkpoint（旧快照播种 delta channel），
所以 full → delta 是平滑迁移；delta → full 必须先物化数据。

---

## 4.2 图结构：create_agent 抽象构建

### 4.2.1 为什么没有手写节点边

在 `agents/lead_agent/agent.py` 全文件里 grep `add_node`/`add_edge`/`StateGraph`
均为空。主图由 `langchain.agents.create_agent()`（注意：**不是**
`langgraph.prebuilt` 的 `create_react_agent`，是 langchain 框架层的 Agent 构造器）
编译。它内部生成经典的工具调用循环图：模型节点（含 `wrap_model_call`
中间件钩子）与工具节点（`wrap_tool_call` 钩子）之间条件边，由中间件栈改写每轮
行为——DeerFlow 把"图长什么样"外包给框架，"图怎么表现"全部收进中间件
（第 6 章专述）。

真实构建点有两处（agent.py:1056-1062 bootstrap agent、1174-1180 默认 agent，
两段同构）：

```python
graph = create_agent(
    model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled,
                            reasoning_effort=reasoning_effort,
                            app_config=resolved_app_config, attach_tracing=False,
                            model_overrides=agent_model_overrides),
    tools=final_tools,                                   # 授权过滤后的最终工具集
    middleware=normalize_middleware_state_schemas(middlewares, mode),  # 按模式适配过的中间件栈
    system_prompt=system_prompt,                         # apply_prompt_template 渲染
    state_schema=get_thread_state_schema(mode),          # full → ThreadState / delta → DeltaThreadState
)
```

要点：

- `state_schema=` 显式传入状态 schema——这是图与 4.1 状态设计的咬合点；
- `checkpointer` **不在编译期绑定**：graph 对象本身无状态，持久化由执行层把
  checkpointer 放进运行 config 驱动（见 4.4/4.5）；
- 调用前完成的决定性装配（都在 `_assemble_lead_agent`，agent.py:869 起）：
  runtime config 解析（`model_name`/`is_plan_mode`/`thinking_enabled`/子代理开关）、
  `resolve_config_user_id` 定身份、`_resolve_model_name` + `_authorize_model_name`
  定模型并做授权降级、`apply_tool_authorization` 过滤工具、`apply_prompt_template`
  渲染系统提示词、`build_tracing_callbacks` 在**图调用根**挂 tracing（模型层一律
  `attach_tracing=False` 防重复 span，agent.py 文件头 INVARIANT 注释讲透）。

### 4.2.2 图的"可观察形状"：assembly descriptor

执行者拿到的不是裸图，而是 `LeadAgentAssembly(graph, descriptor)`
（agent.py:90）。`assemble_lead_agent`（agent.py:754）是 Gateway 用的富入口，
返回"编译好的图 + 图由什么装配而成"：模型（运行时覆盖后）、渲染提示词 hash、
授权后留下的工具清单、按序的中间件栈——由
`agents/assembly_descriptor.py::build_assembly_descriptor` 构建，扩展观察者
（`notify_agent_assembled`）拿它做观测；无观察者时 descriptor 为 `None` 跳过
构建（零观察者快速路径）。`_complete_assembly` 顺带把 `recursion_limit`
折进 descriptor——它是每次调用的预算、由 Gateway 夹取上限
（`config.get("recursion_limit", "framework-default")`；web UI 与调度器默认
1000，见 AGENTS.md）。

`make_lead_agent`（agent.py:749）只是 `assemble_lead_agent(config).graph` 的薄
包装——签名与"返回裸图"是**发布 ABI**：LangGraph Server 直接调用它，二者皆不可
改。运行时拿图必须防御性解包（`unwrap_agent_graph`），因为第三方/测试工厂可能
返回裸图（worker.py::_agent_graph 同款逻辑）。若图是 delta 模式编译的，裸图只读
会看到哨兵而非消息——所以线程态读写一律经 `CheckpointStateAccessor`（4.4）。

### 4.2.3 运行时的"节点/边"控制面

图结构不可手改，但每次运行仍可控制图的走法——`run_agent()` 签名直接暴露
LangGraph 的图级开关（worker.py:744）：

```python
async def run_agent(bridge, run_manager, record, *, ctx, agent_factory, graph_input,
                    config, stream_modes=None, stream_subgraphs=False,
                    interrupt_before=None, interrupt_after=None) -> None:
```

- `interrupt_before`/`interrupt_after`：在指定节点前后中断（配合 checkpointer
  做人工审批类交互，以源码为准）；
- `config["configurable"]` 运行时开关：`is_plan_mode`、`thinking_enabled`、
  `subagent_enabled`、`max_concurrent_subagents`、`non_interactive` 等
  （agent.py 读取清单见 4.2.1，行为面见第 6 章中间件）；
- `recursion_limit` 限制单次运行的图步数（防死循环，与子代理 `max_turns`
  是两层不同的护栏）。

一句话总结本小节：**在 DeerFlow 里，"图"是中间件策略的执行骨架，静态编译、
按 config 调参；真正可变的行为都长在中间件与 reducer 上。**

---

## 4.3 langgraph.json：向 LangGraph Server 注册

`backend/langgraph.json` 是 LangGraph Studio/Server 与 SDK 看到的世界：

```json
{
  "$schema": "https://langgra.ph/schema.json",
  "python_version": "3.12",
  "dependencies": ["."],
  "env": "../.env",
  "graphs": { "lead_agent": "deerflow.agents:make_lead_agent" },
  "auth": { "path": "./app/gateway/langgraph_auth.py:auth" },
  "http": { "app": "./app/gateway/langgraph_studio.py:langgraph_app" },
  "checkpointer": {
    "path": "./packages/harness/deerflow/runtime/checkpointer/async_provider.py:make_checkpointer"
  }
}
```

- `graphs.lead_agent` 指向 **harness 包根** `deerflow.agents:make_lead_agent`
  （`agents/__init__.py:20` 的轻包装：先预取技能缓存再惰性导入 lead_agent 模块，
  保持包导入轻量），**不是** `agents/lead_agent/agent.py` 里的同名函数。必须是
  模块级具名函数——LangGraph Server 直接从模块 `__dict__` 解析工厂。
- `auth` 把 LangGraph 的请求鉴权接到 `app/gateway/langgraph_auth.py`（用户
  主体、owner 过滤、assistant 作用域）；
- `http.app` 指向 `langgraph_studio.py:langgraph_app`——本地 LangGraph Studio
  用的自定义应用模块；
- `checkpointer` 把图的持久化实现声明到
  `runtime/checkpointer/async_provider.py:make_checkpointer`（4.4 详述）。

注意部署真相：生产/开发全栈**并不跑独立的 LangGraph Platform**，nginx 把
`/api/langgraph/*` 重写到 Gateway 原生 `/api/*` 路由（8001），LangGraph SDK
客户端把 DeerFlow 的运行时当成 LangGraph 兼容端点用；`langgraph.json` 主要是
Studio 开发面与 SDK 兼容契约（4.8 展开）。

---

## 4.4 Checkpointer：thread 续跑与恢复

### 4.4.1 运行时如何拿到 checkpointer

编译图是无状态的；持久化能力按配置组装：

- `runtime/checkpointer/async_provider.py::make_checkpointer`——langgraph.json
  声明的工厂，按 `config.yaml`（`database` 段）产出异步 checkpointer；
- `runtime/checkpointer/provider.py` 定义接口，`cached_saver.py` 加缓存层；
- 后端：SQLite / Postgres（表结构见 docs-local/checkpoint-tables.md，含
  `checkpoints`/`checkpoint_writes`/`checkpoint_blobs`/`checkpoint_migrations`
  四表语义）/ memory（进程内，`database.backend: memory` 时）。

一个 thread（`thread_id`）的全部 checkpoint 构成一条**时间线**：每次 run 结束
写一个新 checkpoint 并带 `parent_checkpoint_id` 指针（谱系 lineage）；同 thread
再次 `invoke`/`astream` 时 LangGraph 自动从最新 checkpoint 续跑——这就是"多轮
对话 + 断点续跑"的机制本体。`thread` 续跑语义：`config["configurable"]` 里
`thread_id` 不变即续聊，改 `checkpoint_id` 可回到历史节点；`checkpoint_ns`/
`checkpoint_id`/`checkpoint_map` 等键由运行时上下文注入（`context_keys.py`）。

### 4.4.2 CheckpointStateAccessor：唯一的线程态访问咽喉

`runtime/checkpoint_state.py`——AGENTS.md 明令"**绝不绕过**"。它把 graph +
checkpointer + 模式绑在一起：给 config 注入模式标记、每次 get/update/history 前
跑兼容性检查、返回**物化后的状态**（delta checkpoint 的裸 `get_tuple` 只见哨兵
没有 `channel_values.messages`）。Gateway `services.py` 构建并传递 accessor；
线程态读取（state/history/regenerate）必须走
`build_thread_checkpoint_state_accessor`，让记录的 assistant 中间件 schema
物化每个 channel。`history(limit)` 语义特殊：`0` = 显式空，`None` = 不限——
别把 `limit=0` 透传给 `graph.get_state_history`。full 模式下工厂建不出图时
（坏模型配置、MCP 宕机）读路径降级为裸 checkpointer 读
（`_RawCheckpointReadAccessor`，full checkpoint 带完整 `channel_values` 所以
不需要图），但 delta 模式无此降级（物化需要 channel 表）。

### 4.4.3 delta 模式不能 fork：worker 线性化续跑

分支/再生（regenerate）从旧 checkpoint 恢复会 fork 谱系，而 delta 态的 fork
**不可物化**：`BaseCheckpointSaver.get_delta_channel_history`（及
`InMemorySaver`/`PostgresSaver` 覆写）收集每个路径祖先的全部 `pending_writes`，
但共享父节点还带着被弃用兄弟子节点写入的 writes——重放进 fork，run 就从一份仍
含待替换答案的消息清单开始（#4458）。所以 worker 把 delta resume 线性化
（`worker.py::_linearize_delta_checkpoint_resume`，2099 起）。错误回滚同样需要
fork：`_capture_rollback_point`/`_rollback_to_pre_run_checkpoint`（2035/2188）。
回滚点、fork、谱系走查的完整规则（`app/gateway/checkpoint_lineage.py`）以源码
为准；一句话：**先走 `parent_config` 谱系，只有显式缺失才做有界的最新优先回退，
环/悬空/深度耗尽一律 fail-closed**。

同 thread 的写安全由 `worker.py::_checkpoint_thread_lock`（233 起，每线程一把
异步锁）保证；gateway 手工 state 更新与摘要压缩复用
`reserve_checkpoint_write()` 边界，与 run 准入共享"同一 thread 同时只有一个
活跃写者"的持久化唯一约束（见 4.5.2）。

---

## 4.5 Run 生命周期：准入 → 执行 → 收尾

### 4.5.1 入口：两条路径汇入同一个 run_agent

run 的 HTTP 面在 `app/gateway/routers/thread_runs.py`：`POST
/{thread_id}/runs`（后台 run）、`POST /{thread_id}/runs/stream`（SSE）、
`POST /wait`、`POST /{run_id}/cancel`、`GET /{run_id}/join`、`GET /messages`、
`GET /events` 等；`app/gateway/deps.py::langgraph_runtime()` 组装每请求依赖：
StreamBridge + 持久化 + checkpointer + store + RunManager。嵌入式面
`DeerFlowClient.stream()` 走同一条 `run_agent`（4.7）。无论哪条路，run 记录
（`RunRecord`，runs/manager.py:156）与状态机都由 `RunManager` 持有，真正的图执行
都由 `runtime/runs/worker.py::run_agent()` 承担。

### 4.5.2 RunManager：记账 / 准入 / 幂等 / 并发治理

`runtime/runs/manager.py`（~2300 行，72 个成员）。按职责归类（一句职责 +
精确路径）：

- **记账**：`create`(:572) 建记录入 store；`persist_status`(:376)/
  `set_status`(:917)/`update_run_completion`(:477) 推进状态机（queued →
  launching → running → finalizing → completed/failed/cancelled/rolled_back，
  以 runs/schemas.py 的 `RunStatus` 为准）；`_persist_to_store` 带重试策略
  `PersistenceRetryPolicy`(:146)；
- **准入**：`try_start`(:819) 拿 run 的启动权（pending → running）；
  `create_or_reject`(:1424) 建记录时校验并发预算；
  `reserve_thread_operation`(:1725) 是**线程级互斥准入**——同一 thread 的
  新 run/手工 state 写/压缩共享"活跃写者唯一"的持久化约束，worker 内外的
  竞争都由此裁决；`has_inflight`(:1869)/`has_later_run`(:1098) 做新旧次序判定；
  撞上并发即 `ConflictError`(:2332)（HTTP 409，SDK 侧复用 thread 重试的标准信号）；
- **幂等**：run 幂等键 + `try_start` 的原子状态迁移保证重复投递只会有一个
  执行者；调度任务每次发生都带稳定幂等键（见根 AGENTS.md 的
  `uq_scheduled_task_run_active` 语义，runs/ 共享同一套簿记）；
- **取消**：`cancel`(:1235) 三路并施——`_signal_local_cancel`（进程内
  asyncio）/`_request_durable_cancel`（库内持久化取消标记）/`_request_remote_cancel`
  （跨实例），action 区分 `interrupt` 与 `rollback`；`set_status_if_not_cancelled`
  (:976) 保证被取消的 run 不再推进成功态；`CancelOutcome`(:2320) 枚举结果；
- **多实例自愈**：`start_heartbeat`(:1966) 周期 `_renew_leases`(:2041)，
  `reconcile_orphaned_inflight_runs`(:1783) + 周期
  `_reconcile_orphans_periodic`(:2154) 把 lease 过期的孤儿 run 接管回来——
  默认单实例，`run_ownership.heartbeat_enabled` 开启后多 Gateway 共享 Postgres
  才能多实例（AGENTS.md 的校验清单）；
- **收尾**：`update_finalizing_progress`(:552)/`set_finalizing`(:1056)/
  `wait_for_prior_finalizing`(:1066) 保证同 thread 的"先 finalize 完旧的再开始
  新的"次序；`cleanup`(:1874) 延迟（默认 300s）清内存索引。

### 4.5.3 run_agent：worker 主循环

`runtime/runs/worker.py::run_agent`(:744，2900 行)——"Execute an agent in the
background, publishing events to bridge"。职责一句话版：

- **preflight 与回滚点**：进 try 前捕获 `pre_run_checkpoint_id` 与 workspace
  快照（`_capture_rollback_point`），失败路径 `_rollback_to_pre_run_checkpoint`
  把线程态滚回 run 之前（fork 谱系），`checkpoint_rollback_completed` 防二次回滚；
- **图与上下文**：`_agent_graph`(:645) 从工厂结果解包 LeadAgentAssembly；
  `_install_runtime_context`(:604) 把 RunContext（checkpointer/store/event_store/
  thread_store/租户身份/trace 等）注入 config；`_bind_trace_id`(:718) 打
  `X-Trace-Id`（trace 上下文在 worker 内部是唯一事实源）；
- **流主循环**：graph `astream`，逐帧 `_unpack_stream_item`(:2724) →
  `_compose_sse_event`(:2757) 组事件名（子图加 `|<ns>` 命名空间）→
  `_publish_stream_item`(:2871) 发布到 StreamBridge；`_lg_mode_to_sse_event`
  (:2593) 把 LangGraph mode 映射为 SSE event 名；文件工具增量
  `_LargeFileToolChunkBatcher`(:402) 按有界批次合并 write_file/str_replace
  delta（多模式消费契约，AGENTS.md 有专段）；
- **中断与恢复**：LLM 错误兜底检测（`_extract_llm_error_fallback_message`
  :2655，用 pre-existing message ids 屏蔽历史遗留兜底标记）；中断后
  `_ensure_interrupted_title`(:2508) 补标题；
- **委托记账**：子代理事件缓冲 `_SubagentEventBuffer`(:656) 批量持久化；
  goal 续跑（`_persist_goal_evaluation`/`_reread_goal_and_checkpoint` 等，
  1726 起）驱动自主目标推进；
- **收尾**：terminal status 记账——`persist_run_history_metadata`(:2392)/
  `persist_run_durations`(:2479)/`_persist_delivery_receipt`(:267)——成功、
  失败、取消都产出一份投递回执进 journal（`runtime/journal.py`），terminal
  kwargs 在 event_store 存在时 `persist=False` 省一次写。

### 4.5.4 并发总纲

一句话记住治理层级：**跨线程并行**（不同 thread 的 run 各自独立、受
`max_concurrent_runs` 全局预算约束）；**同线程串行**（`reserve_thread_operation`
+ 持久化唯一约束 + finalizing 次序）；**幂等由 run 级状态机保证**（try_start 原子
迁移）；**崩溃由 lease 接管兜底**（heartbeat + orphan reconciliation）。
前台 web 用 SDK `useStream` 消费、后台 run 用 join/wait 轮询、调度任务复用同一
run 生命周期（不准另起执行栈——AGENTS.md 红线）。

---

## 4.6 流式：stream_mode、SSE 与 StreamBridge

### 4.6.1 stream_mode 归一化

`runtime/stream_modes.py` 是流模式的统一入口：

```python
SUPPORTED_STREAM_MODES = ("values", "messages-tuple", "custom", ...)  # 8 行附近
def normalize_stream_modes(raw) -> list[str]:      # None → ["values"]
def to_langgraph_stream_modes(raw) -> list[str]:   # "messages-tuple" → "messages"
```

对外契约用 **`messages-tuple`**（带 message 元组的增量），对 LangGraph 引擎翻译成
**`messages`**——这个改写是 DeerFlow 的流协议边界。三种核心模式的语义：

| 模式 | 帧内容 | 用途 |
|------|--------|------|
| `values` | 每步之后整份状态快照（title/messages/artifacts/…） | 完整线程视图，可选（不是批处理的先决条件） |
| `messages-tuple` | 增量：AI 文本按 chunk 发（按 `id` concat 还原整条）；工具调用/结果各发一次 | 前端逐字打字机效果；工具结果保留非 None 原生 artifact |
| `custom` | 业务自定义事件（`task_*` 等，经 `StreamWriter` + `emit_custom_event` 双发射） | 子任务进度、卡片状态等 UI 细节 |

### 4.6.2 从 LangGraph 帧到 SSE

worker 的流主循环把引擎帧逐条转成 SSE（worker.py 见 4.5.3）：
`event: values|messages-tuple|custom` + `data:` JSON 行，经
`runtime/stream_bridge/` 分发——`base.py` 定义接口、`memory.py` 进程内、
`redis.py` 跨实例（多 Gateway 时 SSE 可订阅任一实例的事件）。两个关键约定
（AGENTS.md 专段，行为以源码为准）：

- **子图帧保持命名空间**：`stream_subgraphs` 打开时，委托子代理的帧发成
  `values|<ns>` 而非冒充根帧——子代理继承父 checkpoint 命名空间，若把它
  `values` 快照发成裸 `values` 会在 SDK 客户端里整份替换线程视图（#4399）。
  DeerFlow 前端不请求子图流，子任务进度靠根命名空间的 `task_*` custom 事件；
- **批处理边界**：`write_file`/`str_replace` 参数 delta 对有界批次消费者聚合
  发送、单模式消息消费者保留逐 chunk 原契约；非消息帧冲刷待发批次；
  `values` 只是可选的完整快照，不是批处理的先决条件。

### 4.6.3 客户端消费

前端 `NEXT_PUBLIC_LANGGRAPH_BASE_URL` 默认 `/api/langgraph`（经 nginx），用
`@langchain/langgraph-sdk` 的 `useStream` React hook 消费——它按 `Content-Location`
头解析 run 元数据（Gateway 已把该头加入 `CORS_EXPOSED_HEADERS`，否则 JS 读不到）。
SSE 在代理层**不做压缩**（nginx 只压 HTML 与配置的文本资源，SSE 保持原样）。
协议设计全貌见 `backend/docs/STREAMING.md`（docs-local 阅读时的推荐延伸）。

---

## 4.7 双路径：DeerFlowClient 与 Gateway

两个运行时入口共享同一套图装配与执行核心，却互不依赖：

**Gateway HTTP 路径**：FastAPI（8001）→ `thread_runs.py` 路由 → `RunManager` +
`run_agent()` + `StreamBridge`（nginx 2026 暴露为 `/api/langgraph/*`）。面向
浏览器、IM 渠道、调度器。

**DeerFlowClient 嵌入式路径**：`packages/harness/deerflow/client.py`——进程内直连、
零 FastAPI 依赖。`chat(message, thread_id)` 同步累积 delta 返回最终文本；
`stream(message, thread_id)` 订阅 `stream_mode=["values","messages-tuple","custom"]`
（注意客户端这层叫 `messages-tuple`，与 SDK 契约一致），yield `StreamEvent`：
`values` 整态快照（AI 文本不重复合成，避免重复投递）、`messages-tuple` 逐块增量
（文本按 id 拼接、工具调用与结果各一次、工具结果带原生 artifact）、`custom`
转发（内建事件经 `deerflow.utils.custom_events` 双发射，`astream_events v2`
消费者也能收到）、`end` 携带按 message id 只计一次的累计 usage。

其余同 Gateway：agent 由 `create_agent()` + `build_middlewares()` 惰性构建，
`make_lead_agent` 同款装配；支持 `checkpointer` 参数做跨轮持久化；
`reset_agent()` 强制重建（记忆/技能变更后）；`list_models`/`get_goal`/
`set_goal` 等返回格式与 Gateway API schema 完全对齐，消费代码 HTTP/嵌入式
两写通用。

为什么两条路并行？AGENTS.md/STREAMING.md 的回答：客户端要与 SDK、前端保持协议
一致，又不想背 HTTP 服务；Gateway 要能独立于嵌入式 API 演进。流事件里
"按 message id 去重、AI 文本只在 messages-tuple 里出现一次"的不变量由两路共同
遵守，并有回归测试钉住。

---

## 4.8 Runtime API：代码里的 LangGraph Runtime 兼容面

DeerFlow 不跑独立 LangGraph Platform，但代码里存在完整的 **LangGraph Runtime
兼容层**，让 LangGraph SDK 客户端（`@langchain/langgraph-sdk` 全家桶）把
DeerFlow 当 LangGraph 端点用：

- **`app/gateway/deps.py::langgraph_runtime()`**——名字即声明：每请求组装
  "stream bridge, persistence, checkpointer, store" + `RunManager`
  （deps.py 模块 docstring 原话），是 Gateway 的运行时依赖注入根；
- **SDK 兼容端点**：`app/gateway/routers/thread_runs.py` 的
  `/api/threads/{thread_id}/runs*`（create/stream/wait/cancel/join/messages/
  events/artifacts）与 `assistants_compat.py`（把 `langgraph.json` 图注册表 +
  `config.yaml` agent 定义映射成 LangGraph assistant 资源）——`useStream`
  React hook 直接消费（thread_runs.py:8 自述）；
- **`langgraph_studio.py:langgraph_app`**（langgraph.json 的 `http.app`）：
  本地 LangGraph Studio 的挂载面；附带的 assistant 注册清理逻辑要跑在内存
  运行时 0.30.0 载入并 purge 系统行之前（gateway/AGENTS.md 专段）；
- **auth 面**：`langgraph_auth.py` 把 `StudioUser` 主体映射为普通 owner 作用域，
  assistant 读写对 server-registered assistants 施加 owner filter；
- 嵌入式对等物：runtime AGENTS.md 提到的 `subagent_runtime` DI 路径与
  `deerflow.client` 的 graph 缓存——凡"像 LangGraph Runtime 实例"的东西，在
  DeerFlow 里都是 `RunManager`/accessor/bridge 的组合，无独立服务进程。

若未来要接真 LangGraph Platform/自托管 runtime，图侧只需 `make_lead_agent`
（4.3），状态侧需迁移 checkpoint 模式与谱系语义（4.4），执行侧无法复用
RunManager 的准入/幂等/取消账本——这是自研引擎的迁移成本所在（以源码为准）。

---

## 4.9 运行模型小结与延伸阅读

**一张图记住本层**：

```
  HTTP (nginx:2026 /api/langgraph/*)          DeerFlowClient.stream()
        │ rewrite                                  │ 同装配
        ▼                                          ▼
  Gateway thread_runs.py 路由            deerflow/client.py
        │ deps.langgraph_runtime()                 │
        ▼                                          ▼
  RunManager ──准入/幂等/取消/lease──► run_agent()  ◄─ LeadAgentAssembly.graph
        │        runs/manager.py        runs/worker.py   (create_agent 编译)
        ▼                                    │
  持久化: make_checkpointer ──graph astream──┤
        │ (checkpointer/*)  state 经 CheckpointStateAccessor
        ▼                                    ▼
   checkpoint 库                    StreamBridge(memory/redis)
   (sqlite/postgres/memory)              │ SSE: values / messages-tuple / custom
                                         ▼
                              前端 useStream / 轮询 join、wait
```

**延伸阅读——runtime/ 源码导览**（每个文件一句职责）：

- `runs/manager.py` — RunManager：run 记账/准入/幂等/取消/lease（本章 4.5.2）；
- `runs/worker.py` — run_agent 主循环：preflight、流翻译、回滚、收尾（4.5.3）；
- `runs/schemas.py` — RunRecord/状态机/请求负载类型；
- `checkpoint_state.py` — CheckpointStateAccessor，线程态读写的唯一咽喉（4.4.2）；
- `checkpoint_mode.py` — full/delta 模式冻结、标记注入、兼容性检查（4.1.3）；
- `checkpointer/` — async_provider(make_checkpointer)/provider/cached_saver；
- `stream_bridge/` — memory/redis 流桥与订阅心跳（4.6.2）；
- `stream_modes.py` — 流模式归一化（4.6.1）；
- `events/` — run 事件存储（jsonl/db/memory）与消息序号 `message_seq.py`；
- `store/` — thread store（线程元数据，memory/sqlite/postgres 语义一致）；
- `checkpoint_cache/` — checkpoint 内存缓存（memory/redis）；
- `goal.py` — 自主目标状态机（配合 worker 的 goal 续跑段）；
- `journal.py` — run 投递回执/完成快照日志；
- `serialization.py` / `converters.py` / `context_keys.py` / `user_context.py` /
  `secret_context.py` — 序列化、运行时上下文键、用户身份与密钥解析；
- `runs/` 之上：`app/gateway/routers/thread_runs.py`（HTTP 面）、
  `app/gateway/checkpoint_lineage.py`（谱系/回退）、
  `backend/docs/STREAMING.md`（流协议设计）、docs-local/checkpoint-tables.md
  （checkpoint 表结构）。

**与相邻章节的分界**：状态 schema 与 reducer 在本章（4.1）；中间件如何在这些
状态字段上读写、如何接管模型/工具调用见第 6 章；子代理如何作为图的"第二执行
面"并行推进见第 8 章；调度器如何复用本章的 run 生命周期做定时任务见第 9 章。
