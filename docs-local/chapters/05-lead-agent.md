# 05 Lead Agent：装配与主循环

> 基于 DeerFlow 最新源码（本仓库 commit 2672e209，2026-09）编写。
> 本章代码位于 `backend/packages/harness/deerflow/agents/` 与
> `backend/packages/harness/deerflow/client.py`，均为重构后的 harness 包
> （`deerflow.*` 导入路径），与旧版 `backend/src/agents/lead_agent` 目录无关。

## 本章导览

Lead Agent 是 DeerFlow 的“主智能体”：用户每一次对话、Gateway 的每一次 run、
子代理委托的发起方，最终都落到这一个图上。它不是手写节点图，而是
`langchain.agents.create_agent()` 按配置“编译”的产物——模型、工具、中间件、
系统提示词在编译期一次性确定，之后由 LangGraph 执行引擎驱动主循环。本章重心是
**装配（assembly）**：`make_lead_agent` 是 LangGraph Server 的 ABI（签名不可改），
真正的装配在 `assemble_lead_agent`——它多返回一份描述符；装配输入是
`ThreadState`、运行时 `configurable`、提示词模板与模型工厂；产物是
`LeadAgentAssembly(graph, descriptor)`（可运行图 + “图由什么组成”的指纹）。
另有一条第二流路径 `DeerFlowClient.stream()` 复用同一装配逻辑但独立于 Gateway，
其 agent 缓存键把授权身份（Principal）纳入其中。

---

## 5.1 两层工厂：ABI 与装配

### 5.1.1 langgraph.json 里的注册

`backend/langgraph.json` 声明 LangGraph Server（Platform/Studio）可见的图：

```json
{ "graphs": { "lead_agent": "deerflow.agents:make_lead_agent" } }
```

同文件还声明了 `auth`（`./app/gateway/langgraph_auth.py:auth`）、`http`
（`langgraph_studio.py:langgraph_app`）与 `checkpointer`
（`runtime/checkpointer/async_provider.py:make_checkpointer`）。

注意指向的是 **harness 包根** `deerflow.agents:make_lead_agent`
（`agents/__init__.py`），不是 `agents/lead_agent/agent.py` 里的同名函数。包根
版本是轻包装：先 `prime_enabled_skills_cache()`（预取提示词要用的技能清单，避免
冷启动首轮阻塞在技能目录扫描），再把 lead_agent 模块的导入延迟到首次调用；且
必须是**模块级具名函数**——LangGraph Server 直接从模块 `__dict__` 解析工厂，
`__getattr__` 惰性值不可用（docstring 明示）。

### 5.1.2 `make_lead_agent`：薄 ABI 层

`agents/lead_agent/agent.py` 的实现只有一行：

```python
def make_lead_agent(config: RunnableConfig):
    """LangGraph graph factory; keep the signature compatible with LangGraph Server."""
    return assemble_lead_agent(config).graph
```

`agents/AGENTS.md` 把它标为 **published ABI**：LangGraph Server 直接调用它，
**签名（一个 `RunnableConfig`）与返回类型（裸图）都不得改变**。

### 5.1.3 `assemble_lead_agent`：Gateway 使用的富入口

Gateway 不满足于一张图——运行时要汇报、扩展要观测、调试要复现，因此它需要
“图是从什么装配出来的”。装配体是 frozen dataclass：

```python
@dataclass(frozen=True)
class LeadAgentAssembly:
    graph: Any
    descriptor: Any   # typed loosely on purpose —— 见下
```

`descriptor` 故意宽松标注：`agent.py` 在 LangGraph Server 启动时就被导入，不能
把扩展契约包（`deerflow_extension_api`）拖进该导入路径；真实类型是其中的
`AgentAssemblyDescriptor`（见 5.8）。`assemble_lead_agent` 比 `make_lead_agent`
多做三件事：

1. 解析 `AppConfig`：显式参数 > `config.configurable/context` 里的 `app_config`
   键 > 进程级 `get_app_config()` 单例。
2. **冻结 checkpoint 通道模式**（`freeze_checkpoint_channel_mode`）与增量快照
   频率（`freeze_checkpoint_snapshot_frequency`），优先级被 `test_checkpoint_mode.py`
   钉死：进程首次冻结时 **app 配置说了算**（客户端伪造键被忽略，直连 LangGraph
   请求不能重配一个全新进程）；冻结后任何不一致 fail closed，改模式 = 改配置 +
   重启（restart-required）。
3. `inject_checkpoint_mode(config, mode)` 把模式作为内部键写回 config，保证同
   进程编译出的通道表与执行配置一致。

### 5.1.4 解包约定 `unwrap_agent_graph`

Gateway run worker（`runtime/runs/worker.py::_agent_graph`）消费工厂结果时不能
假设一定是装配体——第三方/测试工厂可能仍返回裸图：

```python
def unwrap_agent_graph(agent_result: Any) -> Any:
    return agent_result.graph if isinstance(agent_result, LeadAgentAssembly) else agent_result
```

用 `isinstance` 而非鸭子类型（裸图有 `.graph` 也不能误当装配体），函数与
dataclass 同居一模块。worker 与 Gateway 状态访问器在无法导入本模块时守卫导入
并原样返回。

---

## 5.2 装配主流程 `_assemble_lead_agent`

### 5.2.1 运行时上下文合并与身份解析

```python
def _get_runtime_config(config: RunnableConfig) -> dict:
    """Merge legacy configurable options with LangGraph runtime context."""
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg
```

所有工厂输入统一走这里：**`configurable` 与 `context` 合并成一张字典，后者覆盖
前者**。运行时选项既可通过 `configurable`（如 `thinking_enabled`），也可通过
运行时上下文（Gateway 注入的 user_id、run_id、trace、沙箱租约）传递。

**用户身份**由 `resolve_config_user_id(config)`（`runtime/user_context.py`）
解析，是“每个用户作用域工厂输入”的唯一权威：① Agent Server 保留 auth 字段
（`configurable.langgraph_auth_user_id` / `langgraph_auth_user`，普通客户端值
无效，Server 会覆写）；② `context["user_id"]`（嵌入式 Gateway 路径）；
③ `configurable["user_id"]`；④ 兜底 `get_effective_user_id()`。身份决定
per-user 自定义技能存储、per-user 记忆、线程目录桶与授权 Principal。

### 5.2.2 读运行选项

```python
requested_model_name: str | None = cfg.get("model_name") or cfg.get("model")
is_plan_mode = cfg.get("is_plan_mode", False)
requested_subagent_enabled = cfg.get("subagent_enabled", False)
is_bootstrap = cfg.get("is_bootstrap", False)
non_interactive = bool(cfg.get("non_interactive", False))
agent_name = validate_agent_name(cfg.get("agent_name"))
```

`model_name`/`model` 两键互换（兼容旧客户端）；`agent_name` 过
`validate_agent_name`。`is_bootstrap` 表示自定义 agent 创建向导用的最小代理。
**请求开关不能宽于服务端策略**：

```python
allowed_subagents = getattr(agent_config, "allowed_subagents", None) ...
subagent_enabled = bool(requested_subagent_enabled and allowed_subagents != [])
config.setdefault("configurable", {})["subagent_enabled"] = subagent_enabled
```

显式空列表是硬拒绝（请求想开也开不了），且解析结果**写回** `configurable` 与
`context`，让下游中间件/工具看到的与装配决策一致。

thinking/reasoning 优先级为 **request > 自定义 agent 默认 > 运行时默认**
（issue #4336），由 `_resolve_runtime_option` 实现：

```python
def _resolve_runtime_option(cfg: dict, key: str, agent_value, default):
    if key in cfg:            # 注意是 key in cfg，不是 cfg.get(key)
        return cfg[key]
    if agent_value is not None:
        return agent_value
    return default
```

`key in cfg` 区分“没传”与“传了 falsy 值”：请求显式传 `thinking_enabled: false`
必须受尊重。agent 值只在非 None 时用（自定义 agent 未设置 = “不覆盖”）。

### 5.2.3 模型名解析与授权

```python
model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=...)
model_name = _authorize_model_name(model_name, context=cfg, app_config=...)
```

`_resolve_model_name`：请求名 → agent 配置名 → 全局默认（`config.models[0]`）；
未知请求名打 warning 回退默认；`models` 为空直接 ValueError。
`_authorize_model_name`：`authorization.enabled` 时对解析结果执行 **`model:use`
动作级授权**（与 Gateway `get_model` 路由同一契约，区分 `list`/`use` 的自定义
provider 无法被运行时绕过）。拒绝时按 RFC §9 优雅降级——挑第一个
`filter_resources` 可见且通过 `model:use` 的模型；全不放行时按 `fail_closed`
抛 ValueError 或 fail-open 返回原名。若 `thinking_enabled` 但模型
`supports_thinking=False`，只降级打 warning，不中断。

### 5.2.4 tracing 回调与元数据戳

LangSmith/Langfuse 回调挂在**图调用根**，保证一次 run 一条 trace、所有
node/LLM/tool 调用成为子 span，且 Langfuse handler 能看到
`on_chain_start(parent_run_id=None)` 从而把 `langfuse_session_id`/`user_id`
从 `config["metadata"]` 传播到 trace 根：

```python
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    config["callbacks"] = [*existing, *tracing_callbacks]
```

装配决策同时写进 `config["metadata"]`（agent_name/model_name/thinking_enabled/
is_plan_mode/subagent_enabled/tool_groups/available_skills/allowed_subagents）。
由此引出模块级不变式（agent.py docstring 的 INVARIANT）：凡本模块及本图可达
中间件内新建的 `create_chat_model(...)` **必须传 `attach_tracing=False`**——否则
同一次 LLM 调用发双份 span（一根在图、一根在模型），且 Langfuse
`propagate_attributes` 不触发，`session_id`/`user_id` 到不了 trace。

### 5.2.5 双分支：bootstrap 与正常装配

**bootstrap 分支**（`is_bootstrap`）：技能集钉死在 `{"bootstrap"}` 保持确定性；
工具 = `get_available_tools(...) + [setup_agent]`（无 `update_agent`）；
`owns_agent_skill_projection=False`（提示词型 bootstrap 不拥有线程物理技能投影，
窄技能集不得重建共享线程视图）；descriptor 的 agent_name 固定 `"bootstrap"`。

**正常分支**（默认/自定义 agent）：工具 = `get_available_tools(model_name=...,
groups=agent_config.tool_groups, subagent_enabled=..., app_config=...)`；自定义
agent 额外挂 `update_agent`（持久化自身 SOUL.md/config 用），**默认 agent 与
webhook 频道不挂**——`github` webhook 提示词来自任意外部评论者，暴露该工具等于
允许任何人永久改写 agent 的 tool_groups/SOUL.md/model（`_WEBHOOK_CHANNELS =
{"github"}`，频道名由 `ChannelManager._resolve_run_params` 注入 run_context）。
`non_interactive` 时剔除 `ask_clarification`（无人可答的澄清无意义）。

两分支共享的工具流水线：① `apply_tool_authorization`（Layer 1 能力过滤，授权
身份与模型授权同源），按对象 id 分成 `configured_tools` 与 `late_tools`
（authorization 补进的 `describe_skill`/记忆工具原序追加在后）；②
`assemble_deferred_tools`（`tool_search` 开启时把大目录 MCP 工具延迟化，返回
deferred 名单 + catalog hash）+ `build_mcp_routing_middleware`（PR1 路由元数据，
为自动提升 top-k 做准备）；③ deferred 名单交给提示词（
`get_deferred_tools_prompt_section`）与中间件（`DeferredToolFilterMiddleware`）。

### 5.2.6 编译与收尾

```python
graph = create_agent(
    model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled,
                            reasoning_effort=reasoning_effort, app_config=...,
                            attach_tracing=False, model_overrides=agent_model_overrides),
    tools=final_tools,
    middleware=normalize_middleware_state_schemas(middlewares, mode),
    system_prompt=system_prompt,
    state_schema=get_thread_state_schema(mode),
)
return _complete_assembly(...)
```

`middleware` 先过 `normalize_middleware_state_schemas`（delta 模式下把中间件自带
state_schema 换 delta 版），`state_schema` 由 `get_thread_state_schema(mode)`
按模式选择（5.4.4）。`_complete_assembly` 构建 descriptor 并通知扩展观察者
（5.8）；无 observer 时快速返回 `LeadAgentAssembly(graph, descriptor=None)`。

---

## 5.3 中间件栈：装配顺序与条件

中间件在模型调用前后、每次工具调用前后、状态写入时按序生效。装配职责是把
**条件性**中间件按依赖排好；`agents/middlewares/AGENTS.md` 明确链由三层组装：
① `tool_error_handling_middleware.py::_build_runtime_middlewares`（经
`build_lead_runtime_middlewares` 暴露）——共享运行时底座；②
`lead_agent/agent.py::build_middlewares`——lead 专属层；③
`compose_with_extensions(..., AgentScope.LEAD, ...)`——扩展贡献的
`MODEL_PHYSICAL` 中间件只在全栈成型后合并一次，否则会改变“最终请求”对观察者
的含义。

### 5.3.1 共享底座（`build_lead_runtime_middlewares`）

- **外层 wrap_model_call 包装**：`InputSanitizationMiddleware`（最外层，内层
  看到的都是已净化的消息）、`ToolOutputBudgetMiddleware`（超限工具输出外置到
  `.tool-results` 再截断）、`ToolResultSanitizationMiddleware`（`web_fetch`/
  `web_search` 等远程内容工具结果做同样中和——攻击者网页不能伪造框架标签）；
- **线程钩子**：`ThreadDataMiddleware`（建线程目录桶，必须先于 Sandbox 保证
  thread_id 可用）→ `UploadsMiddleware`（lead 独有）→ `SandboxMiddleware`
  （惰性获取沙箱，`sandbox_id` 写 state）；
- **收尾层**：`DanglingToolCallMiddleware`（为缺响应的 tool_calls 补占位
  ToolMessage）→ `LLMErrorHandlingMiddleware` → 可选 `ToolReceiptMiddleware`
  → 授权 `GuardrailMiddleware`（authorization provider 包 adapter 做执行期
  Layer 2 门禁，先于显式 guardrail 可省一次外部调用）→ 可选显式
  `GuardrailMiddleware` → `SandboxAuditMiddleware` → 可选
  `ReadBeforeWriteMiddleware` → 可选 `ToolProgressMiddleware` →
  `ToolErrorHandlingMiddleware`（最内层，工具异常转 assistant 可见 ToolMessage）。

### 5.3.2 lead 专属层（`agent.py::build_middlewares` 追加顺序）

| # | 中间件 | 条件 / 位置理由 |
|---|--------|-----------------|
| 1 | `DynamicContextMiddleware` | 恒加：日期（及可选记忆）作 `<system-reminder>` 注入首个 HumanMessage，系统提示词因此全静态（5.6） |
| 2 | `SkillActivationMiddleware` | 恒加：`/skill-name` 开头时确定性加载完整 SKILL.md |
| 3 | `SkillToolPolicyMiddleware` | 恒加：技能文件真正加载后才施加 allowed-tools |
| 4 | `DurableContextMiddleware` | 恒加：在摘要压缩掉委托记录/技能读取前捕获，注入 durable 通道 |
| 5 | summarization | `summarization` 开启时；尽早以压缩上下文 |
| 6 | `TodoMiddleware` | **仅 plan mode**：`_create_todo_list_middleware(is_plan_mode)`，system_prompt/tool_description 源码内联 |
| 7 | `TokenUsageMiddleware` | `token_usage.enabled` |
| 8 | `TitleMiddleware` | 恒加：首轮后生成标题（其模型调用也须 `attach_tracing=False`） |
| 9 | `MemoryMiddleware` | 记忆逻辑：tool 模式 + 后端要被动写入时加；非 tool 模式也加 |
| 10 | `ViewImageMiddleware` | **仅当解析后的模型 `supports_vision`**（用运行时 model_name，避免陈旧配置） |
| 11 | MCP routing 中间件 | `mcp_routing_middleware` 非空（tool_search 自动提升） |
| 12 | `DeferredToolFilterMiddleware` | deferred 名单非空；随后 `assert_mcp_routing_before_deferred_filter` 校验顺序 |
| 13 | `SystemMessageCoalescingMiddleware` | 恒加：合并所有 SystemMessage 为开头的单个——vLLM/SGLang/Qwen/Anthropic 拒绝非开头 SystemMessage |
| 14 | `SubagentLimitMiddleware` | **仅 subagent_enabled**：截断超额并行 task 调用 |
| 15 | `LoopDetectionMiddleware` | `loop_detection.enabled` |
| 16 | `TokenBudgetMiddleware` | `token_budget.enabled`（per-run 上限） |
| 17 | 调用方 `custom_middlewares` | 恒加（Clarification 之前注入） |
| 18 | `load_configured_extension_middlewares` | 配置的扩展中间件 |
| 19 | `TerminalResponseMiddleware` | 恒加：provider 返回空 AIMessage 时重试一次最终响应，兜“静默成功” |
| 20 | `ModelLengthFinishReasonMiddleware` | 恒加：输出达上限时保留内容、打 `stop_reason` |
| 21 | `SafetyFinishReasonMiddleware` | `safety_finish_reason.enabled` |
| 22 | `ClarificationMiddleware` | **恒在最后**：模型调用后拦截澄清请求 |

注意 LangChain 的 `after_model` 钩子是**逆序**调度的：Clarification 最后注册
却最先裁决；Safety 注册在 Terminal 之后，反而保证 Safety 先于
Terminal/accounting 执行。

---

## 5.4 ThreadState：扩展字段与自定义 reducer

### 5.4.1 继承与字段表

`thread_state.py::ThreadState(AgentState)` 继承 LangChain `AgentState`
（自带 `messages` 基干），再扩展线程独有通道；`DeltaThreadState(ThreadState)`
是 delta checkpoint 模式变体，只重定义 `messages`。

| 字段 | 类型 | reducer | 语义 |
|------|------|---------|------|
| `sandbox` | `SandboxState`(`sandbox_id`) | `merge_sandbox` | 幂等写：同一步多工具惰性初始化写同一 id 接受；**不同 id 直接 ValueError**（隔离 bug，fail closed 而非静默选一个） |
| `thread_data` | `ThreadDataState`(workspace/uploads/outputs) | 无 | 线程目录桶 |
| `title` | `str \| None` | 无 | 线程标题 |
| `artifacts` | `list[str]` | `merge_artifacts` | 追加去重、**保序**（`dict.fromkeys`） |
| `todos` | `list \| None` | `merge_todos` | “最后非 None 赢”：没碰就保留旧值；空列表也算显式更新 |
| `goal` | `GoalState \| None` | `merge_goal` | 普通更新不覆盖活跃目标，仅目标写入者显式替换 |
| `uploaded_files` | `list[dict] \| None` | 无 | 本轮上传文件清单 |
| `viewed_images` | `dict[str, ViewedImageData]` | `merge_viewed_images` | 按路径索引的**轻量元数据**（mime/size/actual_path，无 base64，见 #4138）；**空 dict 写 = 清空** |
| `promoted` | `PromotedTools`(catalog_hash+names) | `merge_promoted` | 延迟工具提升**按 catalog hash 作用域**：hash 变则整体替换（防持久化裸名在目录漂移后指错工具）；同 hash 并集去重保序 |
| `delegations` | `list[DelegationEntry]` | `merge_delegations` | 委托台账：同 id 以最新版替换、保留首见顺序；**终态永不被非终态降级**；只留最近 50 条 |
| `skill_context` | `list[SkillEntry]` | `merge_skill_context` | 按 `path` 去重、最近读取刷新；只存引用**非 SKILL.md 正文**；最多 8 条 |
| `summary_text` | `str \| None` | 无（LastValue） | 摘要通道，经 DurableContextMiddleware 投影进模型请求，**不写成 messages 条目** |
| `background_tasks` | `list[BackgroundTaskState]` | 无 | MCP 长任务的有界投影 |

reducer 集合被 `THREAD_STATE_REDUCER_FIELDS` 冻结（messages、sandbox、
artifacts、todos、goal、viewed_images、promoted、delegations、skill_context）。

### 5.4.2 自定义 reducer 关键语义

- `merge_delegations` 终态锁定靠 `TERMINAL_STATUSES`（= `SUBAGENT_STATUS_VALUES`
  全值）：已 completed/failed 条目收到非终态更新时跳过；同 id 重写保留最早
  `created_at`（及缺失 `run_id`）。`DelegationEntry` 还带 `stop_reason`（守卫
  上限提前结束的附加信号，状态本身不变）、`receipt_verdict`/`acceptance_verdict`
  （父侧引用核查/验收结论，任务写回时盖章）。
- `merge_skill_context`：legacy 条目先归一化（丢弃正文键），描述截 500 字符；
  `loaded_at` 仅观察用（压缩后消息索引会重置）。

### 5.4.3 messages 通道与 delta 模式

`ThreadState.messages` 沿用 `add_messages`；**delta checkpoint 模式**
（`database.checkpoint_channel_mode="delta"`，默认 `"full"`）换成 `DeltaChannel`：

```python
def delta_messages_field(snapshot_frequency: int = DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY):
    return Annotated[list[AnyMessage],
                     DeltaChannel(merge_message_writes, snapshot_frequency=snapshot_frequency)]
```

delta 下 checkpoint 存哨兵 + 逐步写入，存储/serde 随轮次从 O(N²) 降 O(N)。
`merge_message_writes` 是 LangGraph 私有 `_messages_delta_reducer` 的**全语义
替代**：先把当前状态归一化一次，再按序折叠写入（消息 ID 位置索引 + 延迟墓碑
压缩），保持公共 `add_messages` 行为——重复 ID、替换位置、删除报错、
`REMOVE_ALL_MESSAGES`、null 写报错、缺 ID 分配顺序——而不必为每次写入重扫全量
状态。私有 reducer 虽也线性但刻意省略部分公共语义，不能直接替换；契约由差分
测试守护。

三个 schema 助手：`get_thread_state_schema(mode, snapshot_frequency)`（full 返
`ThreadState`；delta 返按频率缓存的 `DeltaThreadState`，默认频率保持静态同一性，
类型检查不破）；`adapt_state_schema_for_mode`（把任意 schema 的 messages 换成
delta 版）；`normalize_middleware_state_schemas`（遍历中间件栈，`copy.copy` 后
逐个替换 `state_schema`）。模式在进程内冻结（5.1.3），快照频率
（`checkpoint_delta.snapshot_frequency` 默认 10）随模式冻结且**不写进 checkpoint
元数据**（活在编译图通道表里，共享 checkpoint 库的进程必须一致）。

---

## 5.5 运行时 configurable：选项总表

| 键 | 语义 | 默认 | 消费方 |
|----|------|------|--------|
| `model_name`（别名 `model`） | 选择 LLM | 配置默认 | `_resolve_model_name` → `create_chat_model` |
| `thinking_enabled` | 扩展思考 | `True`（request>agent>默认） | 模型工厂；模型不支持则降级 |
| `reasoning_effort` | 推理档位 | agent 配置或 None | 模型工厂（按 `supports_reasoning_effort` 过滤） |
| `is_plan_mode` | 计划模式 | `False` | `TodoMiddleware` 进链 |
| `subagent_enabled` | 任务委托 | `False` | 工具/提示词/`SubagentLimitMiddleware`；**服务端 `allowed_subagents` 可硬拒** |
| `max_concurrent_subagents` | 每响应 task 并发上限 | 配置/容量推导 | `effective_subagent_concurrency` + `SubagentLimitMiddleware` clamp |
| `max_total_subagents` | 每 run 委托总上限（覆盖） | `subagents.max_total_per_run` | clamp 1–50 |
| `agent_name` | 自定义 agent | 无 | 技能 whitelist/SOUL/记忆域/`update_agent` |
| `is_bootstrap` / `non_interactive` | 引导代理 / 无交互 | `False` | 工具与提示词最小化 / 剔除 `ask_clarification` |
| `channel_name` | 渠道名 | 无 | `github` webhook 门禁（不挂 `update_agent`） |
| `run_id` | 本次 run id | **Gateway 与 `DeerFlowClient.stream()` 总是提供** | 委托台账按 run 记账（见下） |
| `app_config`（context） | 请求级配置注入 | 进程单例 | 装配全程 |
| `INTERNAL_CHECKPOINT_MODE_KEY` | 冻结模式的内部注入键 | — | 客户端伪造键被冻结逻辑拒绝 |

**容量约束的组装路径**：`configured_subagent_max_running()` 给出进程启动时冻结
的执行容量（`subagent_runtime.max_running`），作为 `subagent_execution_capacity`
贯穿 `effective_subagent_concurrency`、提示词与中间件，保证重载后广告容量与强制
容量一致。并发显式值时按容量 clamp，缺省走 `subagents` 配置推导；总数缺省回退
`subagents.max_total_per_run`。按 run 记账依赖 `run_id`：`SubagentLimitMiddleware`
从 runtime context 取 `run_id`，把台账中同 run_id 的历史委托计作 prior usage；
**run_id 缺失时 fail-restrictive**——把线程完整委托台账都算 prior 并打 warning，
自定义图集成必须自己提供 run_id。`recursion_limit` 是 per-invocation 预算，由
Gateway clamp，不属于工厂选择，但被折进装配描述符（`effective_policies`，缺省
记 `"framework-default"`）。优先级总原则即 5.2.2 的 request > agent > 默认。

---

## 5.6 系统提示词：生成与静态化

### 5.6.1 模板结构

`lead_agent/prompt.py::SYSTEM_PROMPT_TEMPLATE` 用 `str.format` 填充，顶层：

```
<role>                    你是 {agent_name};用户输入夹在 --- BEGIN/END USER INPUT ---
                          标记内(不可信数据);框架注入内容保密
{soul} / {self_update_section}   SOUL.md(转义) / update_agent 自我更新
<thinking_style> + <clarification_system>   CLARIFY → PLAN → ACT
{skills_section} / {memory_tool_section}    <skill_system> / tool 模式记忆指引
{deferred_tools_section} / {mcp_routing_hints_section}   延迟工具 / MCP 路由提示
{subagent_section}        <subagent_system>(仅 subagent_enabled)
<working_directory>       目录说明 + {acp_section}
<response_style> + <citations> + <critical_reminders>    收尾风格/引用/提醒块
```

### 5.6.2 `apply_prompt_template` 装配逻辑

```python
def apply_prompt_template(subagent_enabled=False, max_concurrent_subagents=3,
        max_total_subagents=None, *, agent_name=None, available_skills=None,
        app_config=None, deferred_names=frozenset(), mcp_routing_hints_section="",
        user_id=None, skill_names=None, allowed_subagents=None,
        subagent_execution_capacity=None) -> str:
```

- `subagent_enabled` 门控 `<subagent_system>` 全段及其 thinking/reminders 变体；
  `n`（每响应）与 `total`（每 run）经 `clamp_subagent_concurrency` /
  `clamp_total_subagents_per_run` 夹取后渲染进 HARD LIMITS；`n==1` 与 `n>1`
  措辞不同（并行硬否决清单、多批示例只在并发 >1 出现）；段内文字随
  `verification.receipts_enabled` 变化。可用子代理名来自注册表，`allowed_subagents`
  只收窄不扩宽；
- `skill_names is not None` 走 **deferred discovery**：渲染紧凑 `<skill_index>`
  （仅名字），模型用 `describe_skill` 发现；否则 legacy 全元数据
  `<available_skills>`。两模式 “Skill First” 提醒措辞不同；
- `deferred_names` → `get_deferred_tools_prompt_section`；ACP 段（`acp_agents`
  配置了才有）与自定义挂载段（`sandbox.mounts`）拼进 working_directory 块；
- `agent_name or "DeerFlow 2.0"` 为默认角色名。

**渲染侧注入防护**：凡是“agent 可编辑/外部”文本进模板前一律
`html.escape(..., quote=False)`（元素文本位）：SOUL.md 进 `<soul>`、技能
name/description/location 进 `<skill>`、子代理 `config.description` 首行进
`<subagent_system>`。理由同类：一行 `</soul><system-reminder>...` 即可闭合框架
块、伪造保留标签（#4137/#4097/#4128/#4099 修复族）。技能清单按
`(name, description, category, location)` 签名 LRU 缓存。

### 5.6.3 静态化：为前缀缓存服务

`apply_prompt_template` 收尾注释点名设计目标：

> Build and return the fully static system prompt. Memory and current date are
> injected per-turn via DynamicContextMiddleware as a `<system-reminder>` in the
> first HumanMessage, keeping this prompt identical across users and sessions
> for maximum prefix-cache reuse.

即系统提示词**装配期渲染一次，之后对同一 (app_config, agent, skills) 组合完全
不变**——不掺日期、不掺内存。日期与记忆由 `DynamicContextMiddleware` 每轮注入
首个 HumanMessage，保证请求前缀（system prompt 恒在最前）跨用户/会话/轮次
可复用，最大化 KV 前缀缓存命中。

支撑缓存：① enabled-skills **后台线程缓存**（模块级 `_enabled_skills_cache`，
守护线程 `deerflow-enabled-skills-loader` 加载，带版本号与等待者队列；请求路径
`get_cached_enabled_skills()` 未命中不阻塞磁盘 I/O，先空表后补热）；②
per-(config, user) LRU（`get_enabled_skills_for_config` 以
`(id(app_config), user_id)` 键缓存、容量 256 逐出最久未用——长跑多用户进程
不会按见过的每个用户泄漏；`invalidate_user_skill_cache` 支持只清单用户）；③
技能变更后经 `clear_skills_system_prompt_cache` / `refresh_skills_system_prompt_
cache_async` 失效。提示词身份最终以 hash 进装配描述符（`base_prompt_hash` +
`prompt_template_id="deerflow-lead-agent-v1"`）。

---

## 5.7 模型：`create_chat_model` 与能力矩阵

Lead Agent 不直接 new provider client，统一走工厂：

```python
# deerflow/models/factory.py
def create_chat_model(name: str | None = None, thinking_enabled: bool = False, *,
                      app_config: AppConfig | None = None, attach_tracing: bool = True,
                      model_overrides: dict | None = None, **kwargs) -> BaseChatModel
```

装配形态：`create_chat_model(name=model_name, thinking_enabled=...,
reasoning_effort=..., app_config=..., attach_tracing=False,
model_overrides=agent_model_overrides)`。

- **能力矩阵声明在模型档案（ModelConfig）上**：`supports_thinking` /
  `supports_reasoning_effort` / `supports_vision` 三个布尔能力位；
  `when_thinking_enabled` / `when_thinking_disabled` / `thinking` 是思考开关的
  逐 provider 形态（原生 Anthropic 构造参数、OpenAI 兼容网关
  `extra_body.thinking.type`、vLLM Qwen `chat_template_kwargs.enable_thinking`）。
  `supports_vision` 直接门控 `ViewImageMiddleware` 进链；thinking 不支持只降级，
  reasoning_effort 不支持则弹掉该键。
- `model_overrides` 层叠自定义 agent 采样覆盖（temperature/max_tokens），None
  不覆盖档案；Codex Responses API 模型把 thinking 映射为 `reasoning_effort`
  （low/medium/high/xhigh/none）并丢 `max_tokens`；OpenAI 兼容客户端默认注入
  240s `stream_chunk_timeout`（推理模型首块可合法迟到 90–150s，见 #3189）。
- 工厂按 `model_config.use` 反射解析 provider 类，`$` 开头配置值解析环境变量，
  缺 provider 模块给出可执行安装提示。

能力矩阵完整展开（逐 provider 思考/视觉/工具调用形态）属模型工厂专题，见
**ch16**；Lead Agent 只消费矩阵布尔位。

---

## 5.8 Assembly Descriptor：装配的可观测身份

### 5.8.1 为什么与何时

工厂知道下游永远无法恢复的事：运行时覆盖后**哪个模型活下来**、渲染出的提示词
**实际说了什么**、授权后**哪些工具留下**、中间件栈**最终顺序**。
`assembly_descriptor.py` 把它们投影成 `deerflow_extension_api.assembly.
AgentAssemblyDescriptor`，每次装配经 `notify_agent_assembled` 通知扩展观察者。
两条投影规则（模块 docstring）：**Declared beats probed**——实现
`release_policy_parameters()` 的中间件自述行为身份，探测私有属性只是没实现者的
回退且被标记为探测；**Hash, do not copy**——提示词、工具描述、参数 schema
一律归约为 hash，描述符是身份而非载荷的第二份拷贝。

构建点唯一在 `_complete_assembly`（agent.py），且有零观察者快速通道：

```python
if not resolved_extensions.has_agent_assembly_observers:
    return LeadAgentAssembly(graph=graph, descriptor=None)
```

### 5.8.2 描述符字段

- **身份**：`namespace`（恒 `"deerflow"`）、`agent_name`（默认 agent 记
  `"lead-agent"`，bootstrap 记 `"bootstrap"`）；
- **模型**：`requested_model` vs `effective_model`（授权回退后真正生效名）、
  `model_parameters`（档案行为字段，剥掉 name/display_name/description/use/
  context_window/pricing 等展示元数据再叠 overrides；`api_key` 等凭据形字段
  永不投影）、`thinking_enabled`、`reasoning_effort`；
- **提示词**：`base_prompt_hash = canonical_hash(rendered_base_prompt)` +
  `prompt_template_id`（默认 `"deerflow-lead-agent-v1"`）；
- **工具**：`tools` = 图绑定工具 ∪ 中间件自带工具（模型看到的视角一致），逐个
  `describe_tool`：name、`description_hash`、`schema_hash`（JSON schema 归约后
  hash）、`source`（builtin / `mcp:{server_name}` / skill / community，
  MCP 工具带 transport）；
- **中间件栈**：`middlewares` 按装配顺序逐条 `describe_middleware`：类名+模块、
  声明优先（`release_policy_parameters()`，无则 `{"probed": True, ...}` 探测公共
  字段、长文本只给 hash）、来源扩展（隔离 wrapper 经 `_unwrap_middleware` 逐层
  剥开找真正拥有行为的内层与它的 `source`）；
- **技能**：`enabled_skills` 只列名；`skill_catalog_hash` 覆盖全目录
  （name/description/allowed_tools/content_hash/secrets 标志），其中
  `content_hash` 直接 hash SKILL.md 正文——编辑技能正文改指纹而描述符保持小巧；
- **策略**：`effective_policies`——`bootstrap`、`non_interactive`、`plan_mode`、
  `subagents`（enabled/max_concurrent/max_total/type_allowlist/各类型
  runtime_limits{max_turns,timeout_seconds}，由 `_subagent_release_policy` 折叠：
  子代理配置改动改变 lead 可支出预算，必须显式入描述符）、`deferred_tools`
  （enabled/catalog_hash）、`deferred_skills`、`recursion_limit`；
- **构建信息**：`build` 独立字段（`package_version` + `DEER_FLOW_IMAGE_DIGEST`
  + `DEER_FLOW_GIT_COMMIT`），刻意不进 `effective_policies`——后者参与指纹 hash，
  “无关 agent 的重部署”不应改变 agent 指纹。

模型对象携带凭据与 client，绝不序列化：`describe_model_identity` 只取
“类名 + 配置模型名”，并穿过中间件套上的 bound runnable 包装逐层解包。

---

## 5.9 DeerFlowClient.stream()：第二条（并行）流路径

### 5.9.1 一个工厂，两条流路径

`DeerFlowClient`（`client.py`）是进程内嵌入式客户端：不依赖 FastAPI/Gateway，
直接 import 同一批 `deerflow` 模块、共享 config.yaml 与数据目录。它的
`stream()`/`chat()` 看似重复 Gateway `runtime/runs/worker.py::run_agent`，但
**两者不能共享执行**（`_stream_turn` docstring 逐条给原因）：

1. **异步 vs 同步**：`run_agent` 是 `async def` + `agent.astream()`；
   `DeerFlowClient.stream()` 是同步生成器 + `agent.stream()`，让调用方写
   `for event in client.stream(...)` 而不碰 asyncio；桥接 = 每次调用起事件循环
   + 线程。
2. **序列化层**：Gateway 事件经 `serialize()` 成 JSON 走 SSE；客户端直接 yield
   进程内 `StreamEvent`（`data` 是普通 dict），省整层 JSON/SSE 编码。
3. **解耦机制**：`StreamBridge` 是 asyncio 队列，服务 HTTP 边界（
   `Last-Event-ID` 重放、心跳、多订阅者扇出）；进程内单调用方拿直接迭代器即可。

结论：**`DeerFlowClient.stream()` 是同一 `create_agent()` 工厂的并行、同步、
进程内消费者，不是 Gateway 的包装**。两条路径订阅相同 LangGraph stream modes
这一不变式由 `tests/test_client.py::test_messages_mode_emits_token_deltas`
守护而非共享常量——三层（Graph/Platform SDK/HTTP）命名各异（`messages` vs
`messages-tuple`），无法字面共享字符串。

### 5.9.2 事件协议

```python
agent_items = self._agent.stream(state, config=config, context=context,
                                 stream_mode=["values", "messages", "custom"])
```

| type | data | 说明 |
|------|------|------|
| `"values"` | `{title, messages, artifacts}` | 节点完成后的全量状态快照 |
| `"messages-tuple"` | AI 文本 delta / tool_calls / 工具结果 | 见下 |
| `"custom"` | 任意 payload | 转发 `StreamWriter` 自定义事件 |
| `"end"` | `{usage}` | 流结束，usage 按 message id 只计一次 |

与 LangGraph SSE 协议对齐，消费方在 HTTP 流式与嵌入式模式间切换无需改事件
处理逻辑。`messages` 模式细节：

- **AI 文本是逐 token delta**：每个 delta 带稳定 `id`，要全文须按 `id` 累加
  `content`（`chat()` 内部如此）；工具调用与工具结果每个逻辑消息只发一次，
  工具结果保留非 None 原生 `artifact`；
- **跨模式去重**：`seen_ids` 挡重复消息；`streamed_ids` 让 `values` 快照不再
  重新合成已由 `messages` 流过的 AI 文本（避免重复投递）；同一 message id 的
  累计 `usage_metadata` 在末块与快照里重复，`counted_usage_ids` 保证只算先到
  的一次；`end` 的 `cumulative_usage` 由 `_account_usage` 按 id 去重累计。

### 5.9.3 一轮对话的装配与上下文

- `_get_runnable_config(thread_id, **overrides)` 组装
  `configurable={thread_id, model_name, thinking_enabled, is_plan_mode,
  subagent_enabled}` + `recursion_limit=100`（默认）；
- `run_id = str(uuid.uuid4())` 进 `context`，并盖章到首条
  `HumanMessage(additional_kwargs={"run_id": run_id})`——与 Gateway 一致满足
  子代理按 run 记账契约（5.5）；
- `inject_checkpoint_mode` 注入冻结模式；`ensure_checkpoint_mode_compatible`
  兼容检查（full 进程开 delta 线程 → 拒绝）；tracing 回调挂图根 +
  `inject_langfuse_metadata`（session/user/assistant_id/deerflow_trace_id）；
- 无 checkpointer 时每轮无状态（thread_id 只用于文件隔离）；agent 首次调用
  惰性创建，`reset_agent()` 强制重建。另：`stream()` 是同步生成器、与调用方共享
  ContextVar，trace id 只在每次 `next()` 内绑定、绝不在 `yield` 间持有（防泄漏
  与跨上下文 GC 终结报错）；沙箱执行租约由 `_stream_with_sandbox_lease_cleanup`
  在迭代器 finally 释放。

---

## 5.10 Agent 缓存键：Principal 参与其中

`DeerFlowClient._ensure_agent` 用配置键判断是否重建内部 agent：

```python
authorization_identity = None
if self._app_config.authorization.enabled:
    principal = build_principal_from_context(cfg, default_role=...)
    authorization_identity = (
        principal.user_id, principal.role, principal.oauth_provider,
        principal.oauth_id, principal.channel_user_id, principal.is_internal,
        copy.deepcopy(principal.attributes),
    )
key = (
    cfg.get("model_name"), cfg.get("thinking_enabled"),
    cfg.get("is_plan_mode"), cfg.get("subagent_enabled"),
    cfg.get("max_concurrent_subagents"), cfg.get("max_total_subagents"),
    self._agent_name,
    frozenset(self._available_skills) if self._available_skills is not None else None,
    self._checkpoint_channel_mode, self._checkpoint_snapshot_frequency,
    authorization_identity,
)
if self._agent is not None and self._agent_config_key == key:
    return   # 命中缓存,不重建
```

1. **键 = 一切影响装配结果的运行时输入**：模型、思考开关、计划模式、委托开关
   与容量、agent 名、可用技能集、checkpoint 模式与快照频率——任一变化都触发
   重建（系统提示词/工具集/中间件重算）。
2. **授权开启时 Principal 是键的一部分**：`build_principal_from_context` 从合并
   cfg/context 构造主身份，键保存其全部判别字段——user_id、role、
   oauth_provider/oauth_id、channel_user_id、is_internal，以及
   `deepcopy(principal.attributes)`（可变属性必须深拷贝，否则键随属性原地修改
   而漂移）。于是**角色或用户切换必然重建 agent**——Layer 1 工具过滤与模型
   授权都按 Principal 出结果，换身份继续用旧 agent 等于绕过授权边界。
3. 重建路径与 Gateway 装配同源：先解析默认模型名再做 `_authorize_model_name`
   （嵌入式同样跑 `model:use` 授权，无法绕过角色级模型策略），随后
   `apply_tool_authorization` → `assemble_deferred_tools` → `build_middlewares`
   + `apply_prompt_template` → `create_agent`，与 `_make_lead_agent` 逐段镜像。
4. `reset_agent()` 清空 `_agent` 与 `_agent_config_key`——记忆/技能外部变更后
   调用，下一次调用即重建。

---

## 5.11 小结与阅读地图

- **入口链**：`langgraph.json` → `deerflow.agents:make_lead_agent`（包根薄包装，
  预热技能缓存）→ `lead_agent/agent.py::make_lead_agent`（ABI，返回裸图）→
  `assemble_lead_agent`（冻结 checkpoint 模式、返回装配体）→
  `_assemble_lead_agent`（真正装配）。
- **产物**：`LeadAgentAssembly(graph, descriptor)`。图交给 LangGraph/Gateway/
  嵌入式客户端执行；descriptor 在有扩展观察者时构建，hash 化记录模型、提示词、
  工具、中间件栈、生效策略与构建信息。
- **状态**：`ThreadState` 用 9+ 个自定义 reducer 维护沙箱/工件/目标/委托台账/
  技能上下文等跨步状态；delta checkpoint 模式把 messages 换成
  `DeltaChannel(merge_message_writes)` 换 O(N) 存储。
- **选项**：运行时 configurable 按“request > agent 配置 > 默认”决议；容量键
  （并发/总数/递归上限/执行容量）在中间件与提示词间夹取一致；`run_id` 缺失时
  委托记账 fail-restrictive。
- **提示词**：一次渲染、完全静态，动态量经 DynamicContextMiddleware 注入首个
  HumanMessage，服务前缀缓存；渲染侧对 agent 可编辑文本做 HTML 转义。
- **嵌入式路径**：`DeerFlowClient.stream()` 是同步进程内第二流路径，事件协议与
  Gateway SSE 对齐；agent 缓存键含 Principal，身份切换即重建。

延伸阅读：中间件逐篇拆解见 `docs-local/middleware/`；模型工厂/能力矩阵见 ch16；
Gateway run worker / 子代理运行时 / checkpoint 与 delta 表结构（
`docs-local/checkpoint-tables.md`）见各自专题章节。
