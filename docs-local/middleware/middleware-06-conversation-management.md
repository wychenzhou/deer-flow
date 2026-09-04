# 对话管理中间件：压缩 · 待办 · token 归因 · 标题 · 记忆（链位 18、19、20、21、22）

> 本篇讲解负责「对话生命周期管理」的五款中间件：上下文压缩、待办追踪、token 归因、标题生成、记忆入队。它们都长在 lead-only 追加段，共同前提是用同一套「真实用户消息」身份排除框架噪音。阅读前提：LangChain `AgentMiddleware` 钩子模型（`before_model`/`after_model`/`wrap_model_call`、`jump_to`）、LangGraph checkpoint 与消息 reducer、以及 `docs-local/harness-strategies-agent-execution.md` 第 2/5/7 节的概念。
> 源码相对路径：`backend/packages/harness/deerflow/agents/middlewares/`；链装配基线见 [`agents/middlewares/AGENTS.md`](../../backend/packages/harness/deerflow/agents/middlewares/AGENTS.md) 与 `lead_agent/agent.py::build_middlewares`。

## 本文件覆盖的中间件

| 链位 | 类 | 一句话职责 | 主钩子 | 装配条件 |
|---|---|---|---|---|
| 18 | `DeerFlowSummarizationMiddleware` | 上下文逼近上限时把早期消息压缩成摘要 | `before_model` | `summarization.enabled`（默认关） |
| 19 | `TodoMiddleware` | 维护待办清单，防压缩丢待办、防带未完成待办草草收场 | `before_model`/`after_model`/`wrap_model_call` | `is_plan_mode` |
| 20 | `TokenUsageMiddleware` | 记录每次模型调用用量，子代理用量归因到派发它的 AI 消息 | `after_model` | `token_usage.enabled` |
| 21 | `TitleMiddleware` | 首轮完整交换后自动给 thread 起标题 | `after_model` | 恒装配（生成时按 `title.enabled` 判定） |
| 22 | `MemoryMiddleware` | 整轮结束后把原始消息投进记忆队列（异步提取） | `after_agent` | 非 tool-mode；tool-mode 仅当后端要求被动写入 |

---

## 0. 总览：五款中间件在链上的位置与分工

主 Agent 链 = 共享运行时底座（链位 1–13）+ lead-only 追加段（`build_middlewares`）。本文五款都在追加段：

| 链位 | 类 | 装配条件 | 一句话职责 |
|---|---|---|---|
| 18 | `DeerFlowSummarizationMiddleware` | 可选：`summarization.enabled` | 上下文逼近上限时把早期消息压成摘要，保住最新用户请求 |
| 19 | `TodoMiddleware` | 可选：runtime `is_plan_mode` | 维护待办清单，防压缩丢待办、防带未完成待办草草收场 |
| 20 | `TokenUsageMiddleware` | 可选：`token_usage.enabled` | 记录每次模型调用用量，把子代理用量归因到派发它的 AI 消息 |
| 21 | `TitleMiddleware` | 恒装配（生成时再按 `title.enabled` 判定） | 首轮完整交换后自动给 thread 起标题 |
| 22 | `MemoryMiddleware` | 非 tool-mode；tool-mode 仅当后端要求被动写入 | 整轮结束后把原始消息投进记忆队列（异步提取） |

装配代码（`lead_agent/agent.py::build_middlewares`，约 569–607 行）：

```python
summarization_middleware = _create_summarization_middleware(...)      # 18
if summarization_middleware is not None: middlewares.append(summarization_middleware)
if _get_runtime_config(config).get("is_plan_mode"):                   # 19 plan mode 才装
    middlewares.append(_create_todo_list_middleware(True))
if resolved_app_config.token_usage.enabled: middlewares.append(TokenUsageMiddleware())   # 20
middlewares.append(TitleMiddleware(app_config=..., extensions=...))   # 21
if should_use_memory_tools(...):  # tool-mode：仅后端要求被动写入才装
    …
else: middlewares.append(MemoryMiddleware(agent_name=agent_name, ...))# 22
```

```
…14 DynamicContext→15 SkillActivation→16 SkillToolPolicy→17 DurableContext→18 Summarization
→19 Todo(plan)→20 TokenUsage→21 Title→22 Memory→23 ViewImage→…→35 Clarification
每个中间件沿模型调用生命周期展开: before_model(压缩检测/待办提醒)→model call(wrap_model_call 包裹)
→after_model(Todo 防退出/Token 归因/标题)→工具执行→…→after_agent(Memory 入队)
```

**贯穿性事实**（`agents/middlewares/AGENTS.md`）：
- 五款中**只有 Summarization 在子代理链也有变体**——子代理链在 `DurableContext` 之后装一份 `skip_memory_flush=True` 的压缩中间件（见 1.5）。其余四款 lead-only。
- Summarization / Title / Memory 刻意**不 stamp provenance**：前两者自己的模型调用已通过 system-model-call 观测（`SUMMARIZATION`/`TITLE`）归因，摘要文本只经 DurableContext 已 stamp 的 `durable_context_data` 块进请求；Memory 只读消息，回灌内容走 DynamicContext 的 stamp——它们没有「自己的消息」要打标。
- Todo/Title/Memory 对「真实用户消息」的判定都排除框架隐藏消息（`hide_from_ui`、dynamic-context reminder）——这是整套对话管理不把框架噪音当用户意图的共同前提。

---

## 1. DeerFlowSummarizationMiddleware —— 上下文压缩

源码 `agents/middlewares/summarization_middleware.py`（976 行）；父类 LangChain `SummarizationMiddleware`（继承触发/切割骨架，重写生成模型与保留策略）。

### 1.1 它解决什么问题
- **上下文长度失控**：多轮对话 + 大工具输出逼近输入上限，粗暴删旧消息会让模型忘约束、答错 turn。
- **摘要的三副作用**：① 把「最新真实用户请求」卷进摘要 → 模型这轮回答的对象变成旧问题（答错 turn）；② DynamicContext ID-swap 注入的日期/记忆 reminder 被删 → 后续注入错位/污染旧 prompt；③ 被压掉的历史里藏着**还没进长期记忆的事实** → 永久丢失。
- 所以压缩不是「删消息」，而是一次**有保留策略的状态重写**：可丢的旧消息变 `summary_text`，必须留的（当前请求、注入 reminder）按**精确身份**留在消息流，动手前给记忆子系统一个抢救机会。

### 1.2 钩子与执行时机/链位置
- 只挂 `before_model`/`abefore_model`——**每次模型调用前**重新评估，未超阈值则零开销返回。链位 18，紧跟 DurableContext（17）——顺序含义见 1.5「先投影后压缩」。
- lead 与子代理链都有；lead 经 `create_summarization_middleware` 工厂创建，手动 `/compact` 也走同一工厂（`runtime/context_compaction.py` 调 `acompact_state(force=True)`）——自动/手动路径的模型解析、钩子、保留策略永不漂移。

### 1.3 内部实现逻辑

**① 触发检测：把已有摘要也算进总量**
```python
trigger_messages = messages + [HumanMessage(content=previous_summary, name="summary")]  # 摘要也计数!
total_tokens = self.token_counter(trigger_messages)
if not force and not self._should_summarize(trigger_messages, total_tokens):
    return None   # 未达任何阈值 → 本轮不动
```
已有摘要以一条 `name="summary"` 的 HumanMessage 混进计数，否则旧摘要不再计入总量、上下文仍会悄悄涨破上限。trigger 支持 `(tokens,N)`/`(messages,N)`/`(fraction,0.8)`（任一达标即触发，fraction 按模型 `max_input_tokens` 比例）；默认 `enabled=False`、`keep=(messages,20)`。防御：模型无可用的 `max_input_tokens` profile 时（第三方兼容模型没声明 `context_window`），工厂**丢弃 fraction 类子句**（绝对值存活）、fraction keep 回退共享常量 `DEFAULT_KEEP=(messages,20)`——否则 LangChain 父类构造器会在 agent 构建期 `ValueError`，一个配置缺失杀掉整个构建（#3103）；全被丢则以 `trigger=None` 继续（「enabled 但永不自动触发」，手动 `/compact` 的 `force=True` 不 consult trigger，仍可用）。

**② 切割与保留判定**
```python
cutoff_index = self._determine_cutoff_index(messages)                  # 按 keep 从尾部留窗口
to_summarize, preserved = self._partition_messages(messages, cutoff_index)
latest_user_id = 反向第一条 is_real_user_message 的 .id                # 当前请求(精确 ID)
to_summarize, preserved = self._preserve_dynamic_context_reminders(
    to_summarize, preserved, latest_user_id=latest_user_id)
if not to_summarize: return None                                       # 没东西可压 → 不压
```
保留/可丢判定表（对「待压段」逐条过筛）：

| 消息 | 判定依据 | 去向 |
|---|---|---|
| 带 `dynamic_context_reminder=True` tag 的日期 SystemMessage / `__memory` peer | `is_dynamic_context_reminder` 查 `additional_kwargs`（不靠正则扫内容） | 救回保留段 |
| ID-swap 生成的 `{id}__user` peer（未 tag 的历史用户消息） | 故意**不救**（跨轮 prompt 污染源） | 允许进摘要 |
| 最新真实用户请求 | `is_real_user_message`（HumanMessage、非 `summary` 名、非 `hide_from_ui`）+ **精确消息 ID** | 救回保留段 |
| 其余 AI / Tool / 更早消息 | 按 cutoff | 进摘要 |

`__user` peer 是 DynamicContext 为**历史** turn 做的 ID-swap 副本，若按前缀救回等于把旧问题端回模型面前；当前请求改由 `latest_user_id` 精确 ID 保护，于是「首轮超长分析」也能压缩——用户首条请求留下，早期 AI/Tool 轮次照常进摘要；若把 cutoff 前移保当前请求，早期轮次也活下来，压缩变 no-op（`tests/test_summarization_middleware.py` 同时钉住 stale-peer 与首轮长分析两用例；压缩前 `_ensure_message_ids` 保证 ID 可用）。

**③ 摘要生成：候选模型链 + 无流式 + 预算对半 + 转义**
- `self._summary_model` 是主模型副本打上 `TAG_NOSTREAM`——摘要调用发生在钩子里，不禁流式会把摘要当幻影 AI 消息广播给前端；锚模型（token 计数/profile）保持裸模型不被 swap（agent 实例跨并发 run 复用，临时 swap 会泄漏给其他 coroutine）。
- 模型解析走候选链，**摘要模型坏了不能禁用压缩**：显式配置 → `[配置模型, run模型]`；`model_name=null` → `[run模型]`（run 实际执行的模型，lead/subagent/custom 各自 resolve 后传入）。`_model_for` 懒构建+缓存，构建失败缓存 `None`（本轮不再重试也不会逃出 fail-open 边界）。每个候选失败落到下一个，全失败→自动路径吞掉（记日志、本轮跳过、下轮重试）；手动路径 `raise_on_failure=True` 抛 `SummaryGenerationError`。
- 异步调用包在 `observe_system_model_call(SystemOperationKind.SUMMARIZATION, …)` 里供扩展观测；prompt 把 `get_buffer_string` 后的新消息嵌 `<new_messages>`、旧摘要嵌 `<existing_summary>`；`trim_tokens_to_summarize`（默认 4000）预算**新旧对半**——旧摘要 `strategy="last"` 保尾部、新消息 `strategy="first"`；**写入前 `html.escape(quote=False)`**：`</new_messages>` 不转义会闭合 XML 块、伪造权威 section 喂给摘要模型（同 #4162/#4097 块逃逸防御；转义在裁剪后，避免 `...` 截断实体）。
- 两个罐装摘要短路生成且**不算失败**：无消息 → `"No previous conversation history."`；裁剪为空 → `"Previous conversation was too long to summarize."`。模型返回空白文本视为失败（空摘要会触发钩子并清空全部历史）。

**④ 写回：全清再追加 + 摘要走独立通道**
```python
# _maybe_summarize:compact_state 成功后
return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result.preserved_messages],
        "summary_text": result.summary_text}   # LastValue 通道,不混进 messages
```
「全清再追加」防「该删却漏了」的消息残留；摘要不是 messages 里的一条消息（`name="summary"` 只在计数时临时构造），真正进请求靠 DurableContext 的 `durable_context_data` 块（见 1.5），永远以「不可信字段数据块」而非系统指令出现。

**⑤ 压缩前钩子：先救记忆，再删消息**
```python
summary = await self._asummarize_with(...)                    # 1. 先确保替换摘要物化
if summary is None: return None                               #    失败→不 fire 钩子、不动 state
self._fire_hooks(messages_to_summarize, preserved_messages, runtime)  # 2. 消息还在时通知
self._record_compaction(...)                                  # 3. CompactionEvent(kind=summarization, v1)
```
钩子时序是正确性核心：只在「替换 summary 已存在」且「消息尚未移除」时触发——摘要失败仍先冲刷，同一批消息下次会被**提取两遍**（5.7「记忆↔总结」坑）。lead 链的 `memory_flush_hook`（`agents/memory/summarization_hook.py`）此时把 `messages_to_summarize` 原样交给 `manager.add_nowait()`（bypass watermark 一次性快照），后端过滤出 user/final-AI 片段；thread_id/agent_name/user_id 在事件里解析好。删除前还会冻结每个待压消息 content 的 `canonical_hash` 发 `CompactionEvent`（仅注册了 `ContextCompactionObserver` 才付 O(context) 哈希；直接 hash `content` 而非 `str(content)`——dict 经 `str()` 按插入序渲染，逻辑相同会 hash 不同）。**子代理 `skip_memory_flush=True`**：子代理与父线程共享 `thread_id`，钩子按 thread 归桶，不跳过则子代理内部轮次（"Task" human + 中间 AI/Tool）会写进父线程长期记忆（#3875 Phase 3）。

压缩前后示意：

```
压缩前: [H u1] [A ai1] [T r1] … [H u_old(id=z__user)] [Sys reminder(date)] [H __memory] [H u_new(id=n)←当前请求] [窗口尾部…]
              └────────────── 压成 summary_text ──────────────┘    └──── 保留段(keep=20) ────┘
压缩后: messages=[Sys reminder][H __memory][H u_new(id=n)]…(保留段)   summary_text=“此前:用户先问 X,助手做了 Y…(早期事实)”
        u1/ai1/r1/z__user 消失;reminder 与当前请求因 tag/精确 ID 存活;摘要由 DurableContext 注入下次请求
```

### 1.4 与邻居的关系
- **DurableContext（17，前面）——先投影再压缩**：它先把 `task` 委派结果、技能引用捕获进 `delegations`/`skill_context` 并投影每次请求，压缩才删底层消息——委派摘要/技能引用**压缩删不掉**；反过来压缩新写的 `summary_text` 也由 DurableContext 以隐藏 `durable_context_data` HumanMessage 投影（不可信字段不给 system 权威）。子代理链把 DurableContext 装在压缩之前，使 summary 先于保留的 assistant/tool 尾部投影——否则严格 provider 收到 assistant-first 请求。
- **MemoryMiddleware（22，后面）**：压缩冲刷与 MemoryMiddleware 是记忆入库的**两个互补入口**——Memory 覆盖「活到 run 结束」的轮次，被压掉的历史靠压缩前钩子抢救，最终进同一个去抖队列。
- **DynamicContext（14）**：保留判定理解其 ID-swap 协议（`__user` 后缀 + tag），只救 tag 标记、不救 stale `__user` peer——两者共享 `utils/messages.py` 的身份规则。
- **ReadBeforeWrite（11，底座）**：read 结果消息被压掉 → 其上的文件哈希 mark 消失 → 写门自动失效（再写必须先重读），压缩顺带清了陈旧读锁。

### 1.5 设计权衡
- **全清再追加 vs 逐条删**：保证零残留，代价是重写 messages 通道；用 RemoveMessage 全清是给 reducer 的删除信号，落库的只是保留窗口，量级可控。
- **摘要计入触发总量**：防摘要无限累积，但也让压过的 thread 更早再触发——预期行为，总成本仍 sub-linear。
- **自动路径吞失败**：生成失败只记日志本轮跳过，**绝不把损坏/空摘要写进 checkpoint**（宁可晚一轮不可写坏一轮）；手动 `/compact` 才抛错让用户看到真失败。
- **丢 fraction 子句保构建**：fraction 无 profile 时降级而非让 agent 构建失败；「永不自动触发+手动可用」是 fail-safe 形态。
- **nostream 副本模型**：为防幻影消息付一份 tagged 副本的代价，换实例级 swap 会泄漏给并发 coroutine。

### 1.6 源码阅读指引
`create_summarization_middleware`（候选模型/fraction 降级/钩子装配）→ `_build_summary_anchor`/`_drop_unusable_fraction_clauses`（构建期防御）→ `_prepare_compaction`（触发+保留判定）→ `_preserve_dynamic_context_reminders`（身份规则）→ `_summarize_with`/`_asummarize_with`（候选链+罐装摘要）→ `_build_summary_input_text`（预算+转义）→ `compact_state`/`_maybe_summarize`（钩子时序+全清重写）。配套 `config/summarization_config.py`（阈值字面量与校验，`messages` 值必须整数——它切片列表，浮点会在压缩中途 `TypeError`）、`agents/memory/summarization_hook.py`。

---

## 2. TodoMiddleware —— 待办追踪（plan mode）

源码 `agents/middlewares/todo_middleware.py`（375 行）；父类 LangChain `TodoListMiddleware`（提供 `write_todos` + 系统提示注入），本类修两个坑。

### 2.1 它解决什么问题
- **压缩把待办"变没"**：`write_todos` 调用及其结果被 Summarization 压出窗口后，模型**不知道还有一份清单**，可能空手收尾或重复建单。
- **模型在待办未完成时草草结束**：给出干净最终答案（无工具调用）但 `state.todos` 仍有未完成项，run 却以成功收场。

### 2.2 钩子与执行时机/链位置
链位 19（`is_plan_mode` 才装，lead 专属；须在 Clarification 之前）。五个钩子管各自时段：`before_model`/`abefore_model` 上下文丢失检测；`after_model`/`aafter_model` 防提前退出；`wrap_model_call`/`awrap_model_call` 先调父类注入 `write_todos` 系统提示、再把挂起提醒追加到请求末尾；`before_agent`/`after_agent` 清理 run 级账本。

### 2.3 内部实现逻辑

**① 上下文丢失检测（before_model）——提醒写进 state（可跨 checkpoint）**：
```python
if not todos: return None
if _todos_in_messages(messages): return None       # write_todos 仍可见 → 无事
if _reminder_in_messages(messages): return None    # 已注入且未被截断 → 不重复
return {"messages": [HumanMessage(name="todo_reminder",
    additional_kwargs={"hide_from_ui": True},
    content="<system_reminder>你的待办列表仍生效…</system_reminder>" + _format_todos(todos))]}
```
提醒是真实 state 消息（隐藏、不入 UI/记忆），能跨 checkpoint 存活；重复注入由 `_reminder_in_messages` 自身拦截。

**② 防提前退出（after_model）——完成提醒只活在请求里**：
```python
base_result = super().after_model(state, runtime)          # 1. 保留父类"并行 write_todos"检测
if base_result is not None: return base_result
last_ai = 最近的 AIMessage
if _has_tool_call_intent_or_error(last_ai): return None    # 2. 工具意图/解析错误 → 放行走工具路径
if not todos or all(t.status=="completed"): return None    # 3. 全完成/无待办 → 放行
if 本 run 提醒计数 >= _MAX_COMPLETION_REMINDERS(2): return None   # 4. 提醒 2 次 → 放行防死循环
self._queue_completion_reminder(runtime, _format_completion_reminder(todos))
return {"jump_to": "model"}                                # 5. 强制回 model 节点继续
```
「干净最终答案」判定集中在 `_has_tool_call_intent_or_error`：结构化 `tool_calls`、`invalid_tool_calls`、`additional_kwargs` 的 `tool_calls`/`function_call`（跨 LangChain 版本/provider 兼容层）、`response_metadata.finish_reason ∈ {tool_calls, function_call}`——任一命中即非干净收尾，绝不用提醒掩盖工具路径错误。`jump_to` 由 `@hook_config(can_jump_to=["model"])` 声明。完成提醒**不能写 state**（会泄漏进用户可见流/存档/记忆后端），所以进 per-run 内存队列，`wrap_model_call` 时 drain 并以 `name="todo_completion_reminder"` 隐藏 HumanMessage 追加到请求末尾（去重合并一条）——**模型看得到，state 从没见过它**。

**③ run 级内存账本**：计数按 `(thread_id, run_id)` 键管理、持 `threading.Lock`。`before_agent` 清同 thread 其他 run 的残留键（新 run 不背旧额度），`after_agent` 清当前 run（防长活实例泄漏）；键超 `_MAX_COMPLETION_REMINDER_KEYS=4096` 按 LRU touch 序淘汰（保护当前键）。

```
压缩滚掉 write_todos → before_model 注入 [H todo_reminder](hide_from_ui,进 state,防重复)
模型想带未完成待办收尾 → after_model: 队列化 + jump_to=model(≤2 次) → wrap_model_call 时
                          drain 追加 [H todo_completion_reminder] 到请求末尾(不进 state/UI/记忆)
                          第 3 次再收尾 → 计数到上限放行给最终答案
```

### 2.4 与邻居的关系
- **Summarization（18）**：正是它的压缩把 `write_todos` 滚出窗口——本中间件是压缩副作用的专门修补；`hide_from_ui` 让压缩保留判定/Title 计数/Memory 过滤都不把提醒当真实内容。
- **TokenUsage（20）**：Todo 状态在 `state.todos`；TokenUsage 归因时对新老列表 diff 生成 `todo_*` 动作，其前端展示数据源正是 TodoMiddleware 维护的 state。
- **Clarification（35）**：`ask_clarification` 打断等用户回答——`_has_tool_call_intent_or_error` 对工具意图放行，提醒不会把打断误判成「提前收尾」。

### 2.5 设计权衡
- **两个提醒两条通道**：context-loss 提醒进 state（防丢是目的）、completion 提醒只进请求（防泄漏是目的）——同类控制文本按副作用选持久化策略。
- **重试上限 2**：给模型两次机会继续，到上限放行，避免真卡住时无限 jump_to 空转烧钱——纠偏与终止的边界是**计数**而非置信度。
- **`(thread,run)` 键 + LRU**：中间件实例跨 run 复用是常态，一切 per-run 状态都需清/淘汰策略；额度按 run 隔离。
- **不做语义判断**：只认工具意图不判断回答质量——「无法继续」这类真结论会被提醒两次，为简单可靠付出的代价。

### 2.6 源码阅读指引
`_todos_in_messages`/`_reminder_in_messages` → `before_model` → `after_model`（五步决策 + `_has_tool_call_intent_or_error`）→ `_queue/_drain_completion_reminders` 与 `_*_locked` 账本（run 生命周期 + LRU）→ `wrap_model_call` 父类调用 + `_augment_request`。对照父类看哪些行为被保留（并行检测、系统提示注入）。

---

## 3. TokenUsageMiddleware —— Token 记录与子代理归因

源码 `agents/middlewares/token_usage_middleware.py`（372 行）；基类 LangChain `AgentMiddleware`。

### 3.1 它解决什么问题
- **可观测/计费**：每次模型调用的 `usage_metadata` 记录为 INFO 日志（含 input_token_details 等明细）。
- **子代理用量"悬空"**：子代理是独立 run，消耗只存在于**终态 ToolMessage** 的 `additional_kwargs["subagent_token_usage"]`（`subagents/status_contract.py` 契约化写入）。不归因，前端只看到 `task` 调用看不到成本。
- **重放/重入重复累加**：LangGraph 回放 checkpoint、中间件重入时对同一批用量加两次，计费翻倍。

### 3.2 钩子与执行时机/链位置
链位 20（`token_usage.enabled`）；只挂 `after_model`/`aafter_model`，共用同一 `_apply(state)`。时序：

```
模型响应落地(消息尾: …T_r1 T_r2 A_new)
after_model._apply
 ├─ 从 A_new 往前扫连续 ToolMessage 尾巴: additional_kwargs 有 subagent_token_usage 且未 stamp attributed?
 │    → 往回找派发它的 AIMessage(按 tool_call id) → usage_metadata 累加 → stamp attributed=true
 ├─ INFO 日志最新 AI 的 usage
 └─ 为最新 AI 步骤构建 token_usage_attribution(与旧值相同则跳过重建)
```

### 3.3 内部实现逻辑

**① 子代理归因：按消息位置 + 按 tool_call id 找派发者**
```python
if len(messages) >= 2:
    idx = len(messages) - 2
    while idx >= 0 and isinstance(messages[idx], ToolMessage) and messages[idx].tool_call_id:
        usage = _subagent_usage_from_tool_message(messages[idx])
        # 读 additional_kwargs["subagent_token_usage"] 并经 normalize_token_usage 校验;
        # 已 stamp SUBAGENT_TOKEN_USAGE_ATTRIBUTED_KEY=True → None(防重放重复累加)
        if usage:
            dispatch_idx = idx - 1
            while dispatch_idx >= 0:
                if 是 AIMessage 且 _has_tool_call(candidate, tool_call_id):
                    state_updates[dispatch_idx] = candidate.model_copy(update={
                        "usage_metadata": {input/output/total: 原值 + usage[…]}})   # 多 task 累加
                    state_updates[idx] = tool_msg stamp attributed=True
                    break
                dispatch_idx -= 1
        idx -= 1
```
要点：只扫「最新 AI 前的连续 ToolMessage」——遇非 ToolMessage/无 `tool_call_id` 即停，只归因本步骤并行子代理；**位置读取而非进程级 id 缓存**——provider tool_call_id 跨父 run 重复，进程缓存会让一个 run 的清理污染另一个 run（§6.2 身份拆分）；同一响应派发多个 `task` 调用累加进同一条 AI 消息；stamp 随消息进 checkpoint，重放即跳过；用量缺失/畸形/找不到派发 AI 的保持**未标记可重试**。

**② 步骤归因（结构化元数据给前端）**：
```python
attribution = _build_attribution(last_ai, todos)
# {"version":1, "kind": todo_update|subagent_dispatch|tool_batch|final_answer|thinking,
#  "shared_attribution":bool, "tool_call_ids":[…], "actions":[{kind, tool_name/description/query/content, tool_call_id},…]}
if additional_kwargs.get(TOKEN_USAGE_ATTRIBUTION_KEY) == attribution:
    return …   # 幂等:相同 → 跳过重建对象,减 checkpoint 写入压力
```
动作路由（`_describe_tool_call`）：`write_todos`→todo diff 动作；`task`→`subagent`（带 description/subagent_type）；`web_search`/`image_search`→`search`；`present_files`→`present_files`；`ask_clarification`→`clarification`；其余→`tool`。类型推断：单个 todo→`todo_update`、单个 subagent→`subagent_dispatch`、多动作→`tool_batch`、无动作有内容→`final_answer`、无动作无内容→`thinking`。

**③ todo diff（前端"单一真源"）**：`_build_todo_actions(previous, next)` 先按 **content 优先匹配**（防重排错配），content 未命中且索引空闲才退**按位置**；内容+状态一致跳过（无动作）；旧列表未匹配项 → `todo_remove`。前端直接消费精确动作，缺失/畸形才回退 "Update to-do list" 泛标签。

### 3.4 与邻居的关系
- **SubagentExecutor**：终态 ToolMessage 由执行器按 `status_contract.py` 写 `subagent_token_usage`；本中间件是唯一把它合并回派发 AI 消息的地方。
- **TodoMiddleware（19）**：归因读 `state.todos` + 本次 `write_todos` 参数做 diff，与 Todo 共享数据源。
- **checkpoint/重放**：归因与 attributed stamp 都是消息上的**加法字段**随 checkpoint 持久化——不回放丢归因、重放不重复累加；`version:1` 让旧前端安全忽略未知字段。

### 3.5 设计权衡
- **日志 + 结构化归因双轨**：日志面向运维（含 input_token_details 明细），`token_usage_attribution` 面向前端，一份状态两用。
- **相等跳过 vs 无脑重写**：整 dict 相等则跳过，减大 state 消息的 checkpoint 写放大，代价是每次 after_model 多一次比较。
- **扫描边界保守**：只扫最新 AI 前连续 ToolMessage 尾巴，不跨步骤误归因；找不到派发者不 stamp，保持可重试。
- **加法字段不换枚举**：不引入新 status/事件，向后兼容旧消费方。

### 3.6 源码阅读指引
`_apply`（主流程）→ `_subagent_usage_from_tool_message`/`_has_tool_call` → `_describe_tool_call`/`_build_todo_actions`/`_todo_action_kind`（动作与 diff）→ `_infer_step_kind` → `_build_attribution`。配套 `subagents/status_contract.py` 的 `SUBAGENT_TOKEN_USAGE_KEY`/`normalize_token_usage`。

---

## 4. TitleMiddleware —— 自动标题生成

源码 `agents/middlewares/title_middleware.py`（289 行）；基类 LangChain `AgentMiddleware`。

### 4.1 它解决什么问题
thread 列表全是 "New Conversation" 没信息量；而标题 LLM 调用若混进主消息流或阻塞首轮，用户会看到幻影消息与延迟。

### 4.2 钩子与执行时机/链位置
链位 21（恒装配，内部按 `title.enabled` 判定），只挂 `after_model`/`aafter_model`——**首轮完整交换后触发一次**。**同步路径只做本地回退**（不调 LLM，防阻塞事件循环）；**异步路径（默认 `aafter_model`）才走 LLM**。`_should_generate_title` 同时被 worker 中断兜底复用（4.3-④）。

### 4.3 内部实现逻辑

**① 触发判定：首轮完整交换后，只数真实用户消息**
```python
def _should_generate_title(state, *, allow_partial_exchange=False):
    if not config.enabled or state.get("title"): return False     # 已禁用/已有 → 永不覆盖
    user_msgs = [m for m in messages if 类型=="human" and not is_dynamic_context_reminder(m)]
    ai_msgs   = [m for m in messages if 类型=="ai"]
    return len(user_msgs) == 1 and (len(ai_msgs) >= 1 or allow_partial_exchange)
```
`_is_user_message_for_title` 排除 DynamicContext reminder（日期 SystemMessage + `__memory` peer）——分号计数不能把框架注入当「第二条用户消息」，否则首轮标题永不生成。`allow_partial_exchange=True` 是中断兜底的放宽（允许仅一条用户消息、无 AI 回复）。

**② 生成与解析（异步 LLM 路径）**：prompt = `config.prompt_template.format(max_words, user_msg[:500], strip_think(assistant)[:500])`；模型 `create_chat_model(name=config.model_name, thinking_enabled=False, attach_tracing=False)`（关 thinking、防重复 span、打 `TAG_NOSTREAM`+`middleware:title` tag 供 RunJournal 归因、`run_name="title_agent"`），经 `observe_system_model_call(SystemOperationKind.TITLE, …)` 观测。`_parse_title`：拍平多模态 content → `_strip_think_tags` 剥 `<think>…</think>`（reasoning 模型内心独白不能当标题）→ 剥引号 → 截 `max_chars`（默认 60）。attachment-only 首轮（用户无文字）不调标题模型，直接本地回退。

**③ 本地回退**（同步路径/LLM 失败/无模型共用）：
```python
if not user_msg.strip(): return "New Conversation"
fallback_chars = min(config.max_chars, 50)
if len(user_msg) > fallback_chars:
    body = min(fallback_chars, config.max_chars - 3)     # 预扣省略号空间,保证不超 max_chars
    return user_msg[:body].rstrip() + "..."
return user_msg
```

**④ 中断兜底（worker.py `_ensure_interrupted_title`）**——首轮被取消时中间件没机会跑：
```python
# run worker finalizing(取消收尾)路径
await run_manager.wait_for_prior_finalizing(thread_id, run_id)     # 等更早同 thread finalizing
if not await run_manager.has_later_started_run(thread_id, run_id): # 无更晚已启动 run 才写
    await _ensure_interrupted_title(checkpointer=…, graph_input=graph_input)
    # → TitleMiddleware._generate_title_result(allow_partial_exchange=True) 生成本地回退标题(不调 LLM)
    # → 3 次重试 stale-snapshot CAS 写 checkpoint(读后写前校验 checkpoint 身份)
    # → bump channel_versions["title"] + 写 new_versions(DB saver 只落 new_versions 声明的通道)
# 主流程随后把 checkpoint title 同步到 threads_meta.display_name
```
两个门保证不写坏：`wait_for_prior_finalizing`（更早 run 收尾中先等）与 `has_later_started_run`（已有更晚 run 启动、checkpoint 可能被推进 → 跳过兜底，防覆盖新 run 进展）。中断 checkpoint 可能无 messages 通道，`_title_generation_state` 回退 `_graph_input_messages(graph_input)`（本轮原始输入）取用户消息。结果：**中断的首轮也有标题**。

### 4.4 与邻居的关系
- **DynamicContext（14）**：标题计数依赖其 reminder 识别，注入的隐藏用户消息不算「第二条用户」。
- **InputSanitization（1）**：标题取用户文本优先 `original_user_content`（`get_original_user_content_text`）——用净化前真实文本，避免传输包装/上下文噪音混进标题。
- **run worker（runtime 层）**：中间件只在 run 正常走完 after_model 触发；中断路径由 worker 在 finalizing 代跑本地回退，两者写同一 `title` checkpoint 通道，worker 侧靠 CAS+双门不覆盖新 run。
- **Summarization（18）**：标题首轮即生成，几乎不受压缩影响；且 `hide_from_ui` 规则保证 reminder 不进标题素材。

### 4.5 设计权衡
- **同步只走本地回退**：sync `after_model` 里 await LLM 会阻塞 agent 主循环，故同步路径干脆用本地截断——牺牲标题质量换不阻塞；异步路径才享受 LLM 标题。
- **首轮后生成 + 已有不覆盖**：三个硬条件（config 开、无标题、恰一条真实用户消息）使标题成为一次性动作，后续轮次零开销。
- **四条兜底收敛到本地回退**：模型坏/无模型/空输出/attachment-only 都落到 `_fallback_title`——标题是 UI 必需品，不能因模型可用性缺位。
- **中断兜底不用 LLM**：finalizing 阶段再调 LLM 会拖长取消流程，worker 只做确定性本地截断。

### 4.6 源码阅读指引
`_should_generate_title` → `_get_title_user_message`/`_is_user_message_for_title`/`_normalize_content`（素材清洗）→ `_agenerate_title_result`（LLM 路径与四条兜底）→ `_strip_think_tags`/`_parse_title` → `_fallback_title`。兜底读 `runtime/runs/worker.py::_ensure_interrupted_title`（2508 行起）与 `_title_generation_state`。配套 `config/title_config.py`（enabled/max_words=6/max_chars=60/model_name=None→本地回退）。

---

## 5. MemoryMiddleware —— 记忆入队（异步提取入口）

源码 `agents/middlewares/memory_middleware.py`（123 行）；基类 LangChain `AgentMiddleware`。

### 5.1 它解决什么问题
用户画像/偏好/跨会话事实要靠 LLM 抽取，但抽取昂贵——**绝不能跑在 agent 主循环里**。解法：run 结束后把原始消息交给记忆管理器入队，管理器去抖合并，后台线程做过滤、抽取、合并、原子写盘。**中间件只负责投递，不负责抽取**——链上唯一产生记忆写入副作用的地方是后台管理器。

### 5.2 钩子与执行时机/链位置
链位 22，lead-only：非 tool-mode 装配；tool-mode 仅当后端声明需要被动写入（`should_use_memory_tools` + `backend_requires_passive_writes_in_tool_mode`）。只挂 `after_agent`/`aafter_agent`：**一次完整 agent run（可能多轮模型+工具）结束后**投递整轮消息。子代理链不装配（子代理与父线程共享 `thread_id`，装了会把内部轮次写进父线程记忆）。

### 5.3 内部实现逻辑
```python
def after_agent(self, state, runtime):
    add_args = self._resolve_add_args(state, runtime)      # 所有"该不该投"判定
    if add_args is None: return None
    thread_id, messages, user_id, trace_id = add_args
    get_memory_manager().add(thread_id, messages,          # 去抖入队,立即返回
        agent_name=self._agent_name, user_id=user_id, trace_id=trace_id)
    return None
# aafter_agent: async 路径走 manager.aadd()(asyncio.to_thread 取 manager,不阻塞 loop)

def _resolve_add_args(state, runtime):
    if not config.enabled: return None
    thread_id = runtime.context.get("thread_id") or get_config()["configurable"]["thread_id"]
    if not thread_id or not state.get("messages"): return None   # 无 thread/无消息 → 跳过
    user_id  = resolve_runtime_user_id(runtime)   # ★ 入队时(请求上下文还活着)捕获
    trace_id = resolve_trace_id(runtime.context[DEERFLOW_TRACE_METADATA_KEY])
    return thread_id, state["messages"], user_id, trace_id
```
**两个跨线程陷阱的解法**（本中间件最精妙处）：
- **user_id 必须在入队时捕获**：队列用 `threading.Timer` 在独立线程去抖，ContextVar **不跨线程传播**——到 Timer 回调才解析身份会拿默认桶、把记忆写进别人作用域。故在请求上下文活着时把 runtime-resolved `user_id` 显式存进待处理项；`resolve_runtime_user_id` 让 Gateway 与 standalone LangGraph Server 的读写落同一 user 桶。
- **trace_id 三级回退**：`resolve_trace_id` 覆盖 runtime context → RunnableConfig metadata → 环境 trace context，保证 HTTP/嵌入/TUI 各入口下后台抽取调用在 Langfuse 能与主对话关联（抽取在线程里，不提前捕获就丢了关联）。

投递后全在 manager/backend：过滤（human + 无 tool_calls 的最终 AI；剔工具调用与 `hide_from_ui` 框架消息）、trivial 过滤、纠错/强化信号检测、按 `(thread_id,user_id,agent_name)` 去抖合并、LLM 抽取（确定性门 + scope/durability/authority 校验）、原子写文件+失效缓存；优雅停机 `shutdown_flush` 冲刷剩余项不丢失。

```
[run 结束] after_agent
 ├─ 捕获 thread_id / user_id(runtime-resolved) / trace_id  ← 此刻请求上下文还活着
 ├─ manager.add() → 去抖队列(键: thread_id+user_id+agent_name) → 立即返回
 └─ [Timer 线程/后台] 过滤(user+最终AI)→校验→LLM 抽取→合并→原子写 per-user 记忆文件
      MemoryMiddleware 到此为止——不回写 state、不改消息、不入 agent loop
```

### 5.4 与邻居的关系（读写闭环）
- **DynamicContext（14，注入侧）**：MemoryMiddleware 是**写侧入口**（原始消息→抽取→落盘）；DynamicContext 是**读侧出口**（把已抽取记忆作为 `__memory` HumanMessage 注入下一轮）。中间件不注入记忆，回灌内容走 DynamicContext 的 `dynamic_context_memory` stamp。
- **Summarization（18）**：本中间件只见「活到 run 结束」的消息；被压历史由压缩前 `memory_flush_hook`（同一队列、bypass watermark）抢救——两个入口互补。
- **Todo（19）/ Title（21）**：Todo 提醒带 `hide_from_ui`，后端过滤剔除——框架提醒不会被当用户事实入库。
- **子代理**：轮次既不入队（无中间件）也不经 flush（skip_memory_flush）——父线程记忆只记父线程对话，记忆不串台的硬边界。

### 5.5 设计权衡
- **只入队不抽取**：昂贵且可失败的 LLM 抽取移出 agent loop，延迟/失败面最小；代价是记忆有秒级去抖延迟。
- **原始消息全量投递、过滤下放后端**：中间件保持薄，过滤/校验/去重集中在一个后端，所有写路径共享同一规则。
- **tool-mode 默认不装配**：模型主动管记忆（`memory_*` 工具）时被动抽取是噪音；仅后端依赖被动写才保留——同一存储/prompt 体系由装配点切换。
- **身份在入队时定格**：为跨线程正确性把 user 捕获提前——会话归属应在会话内确定，是特性非缺陷。

### 5.6 源码阅读指引
中间件本身很短：`_resolve_add_args`（三道跳过+双捕获）→ `after_agent`/`aafter_agent`。理解「投递之后」继续读 `agents/memory/manager.py`（`add`/`aadd` 去抖契约）、`backends/deermem/core/queue.py`（键/bypass watermark/背压）、`core/message_processing.py`（过滤）、`summarization_hook.py`（压缩冲刷入口）。装配条件看 `lead_agent/agent.py` 597–607 行与 `config/memory_config.py`。

---

## 6. 五款中间件的设计共性

1. **框架控制文本与用户内容分流**：Todo 提醒、压缩计数 marker、标题素材、记忆入队——框架自产消息要么 `hide_from_ui`、要么 `name="summary"` 等保留名、要么只活在 per-request；下游（Title 计数、Memory 过滤、压缩保留判定）用同一套「真实用户消息」身份排除它们。
2. **能进 state 的才进 state**：Todo context-loss 提醒进 state（要跨 checkpoint 活）、completion 提醒只进 request（要防泄漏）、摘要走独立 `summary_text` 通道——**按副作用选通道**。
3. **会重复跑的动作都有幂等/防重入**：压缩（摘要失败不 fire 钩子、不写坏 checkpoint）、TokenUsage（`subagent_token_usage_attributed` stamp + 相等跳过）、标题（已有短路 + CAS）、记忆（去抖合并）、Todo（run 级计数 + LRU）——「要么一次性、要么可重试」是铁的纪律。
4. **跨线程/跨 run 的身份状态显式化**：Memory 入队时定格 user/trace、Todo 按 `(thread,run)` 记账、TokenUsage 从消息位置读取而非进程缓存——ContextVar 不跨线程、中间件实例跨 run 复用，凡带记忆的状态都必须显式携带作用域。
5. **压缩不是终点而是枢纽**：Summarization 把 DurableContext 的任务主线、Memory 的事实抢救、Todo 的待办可见性、TokenUsage 的消息配对全串起来——改它的保留策略前，先想清四家邻居。

> **源码速查**：五款中间件在 `backend/packages/harness/deerflow/agents/middlewares/`（`summarization_middleware.py`/`todo_middleware.py`/`token_usage_middleware.py`/`title_middleware.py`/`memory_middleware.py`）；链装配见同目录 `AGENTS.md`（18–22）与 `lead_agent/agent.py::build_middlewares`；中断标题兜底在 `runtime/runs/worker.py::_ensure_interrupted_title`；记忆后端与队列在 `agents/memory/`。
