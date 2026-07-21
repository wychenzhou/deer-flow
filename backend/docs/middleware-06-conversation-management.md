# 对话管理类中间件详解

本文档详细介绍 DeerFlow 中负责"对话生命周期管理"的五款中间件：上下文压缩（Summarization）、待办追踪（Todo）、Token 归因（TokenUsage）、标题生成（Title）、记忆入队（Memory）。它们覆盖了从历史压缩、任务推进、计费/可观测性、UI 元数据到长期记忆的完整闭环。

所有文件位于 `backend/packages/harness/deerflow/agents/middlewares/`，在主 Agent 中间件链中的位置参见 `backend/AGENTS.md` 的 "Middleware Chain" 一节（编号 18、19、20、21、22）。

---

# 1. DeerFlowSummarizationMiddleware

## 概述
当对话 token 数逼近模型上限时，自动把较早的消息压缩成一段摘要文本（`summary_text`），并保留近期消息窗口；压缩前会派发 `BeforeSummarizationHook`，让记忆子系统抢救即将丢失的内容。

## 为什么需要这个中间件

### 场景痛点
当对话不断延长时，token 总数会逼近模型上下文窗口上限，导致模型"忘记"早期的重要信息，或直接因超出限制而报错中断对话。同时，被压缩的消息中的动态上下文（如 DynamicContextMiddleware 注入的 ID-Swap 三元组）如果直接丢弃，会导致后续注入错位。

### 为什么模型自身无法避免
模型是被动接收消息序列的，不具备主动管理上下文窗口的能力——它无法自主选择"遗忘"哪些内容，也无法自动生成摘要来替代原始对话历史。压缩后的摘要需要作为独立通道（`summary_text`）持久化，这是基础设施层面的职责。

### 解决思路
在每次模型调用前由中间件检测 token 压力，自动将早期消息压缩成摘要并保留近期窗口，同时通过钩子机制让记忆子系统在压缩前抢救即将丢失的内容。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/summarization_middleware.py` |
| 父类 | `langchain.agents.middleware.SummarizationMiddleware` |
| 实现的钩子 | `before_model` / `abefore_model`（同步、异步双版本）；同时暴露 `compact_state` / `acompact_state` 供手动压缩路由复用 |
| 持久化 | State：写回 `state["messages"]`（通过 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` + 保留尾段）以及 `state["summary_text"]`（LastValue 通道） |
| 配置依赖 | `config.yaml -> summarization.*`（`enabled`、`trigger`、`keep`、`model_name`、`summary_prompt`、`trim_tokens_to_summarize`）；`memory.enabled` 控制是否挂载 `memory_flush_hook` |
| 工厂函数 | `create_summarization_middleware(app_config, keep, skip_memory_flush)` |

## 核心逻辑

### 1.1 触发条件

`before_model` 在每次模型调用前被调用，进入 `_maybe_summarize` -> `compact_state(force=False)`。`_prepare_compaction` 依次完成：

1. **计算 token 总量**：`_messages_for_trigger_count` 把上一轮的 `summary_text` 包成一个 `name="summary"` 的 `HumanMessage` 拼到末尾，再用 `self.token_counter` 统计 `total_tokens`。这样触发判定把"已经压缩过的摘要"也算进上下文压力，避免摘要本身无限累积。
2. **判定是否触发**：`self._should_summarize(trigger_messages, total_tokens)`（父类逻辑，支持 token / 消息条数 / 最大输入比例三种 `trigger`）。
3. **确定切割点**：`_determine_cutoff_index(messages)` 根据配置的 `keep` 策略（例如保留最后 N 条、保留最后 N tokens）求出 cutoff 下标。
4. **分区**：`_partition_messages` 把 `messages` 切成 `messages_to_summarize`（将被压缩）和 `preserved_messages`（原样保留）。
5. **抢救动态上下文**：调用 `_preserve_dynamic_context_reminders`（详见 1.2）。
6. **触发钩子**：`_fire_hooks(...)`（详见 1.3）。
7. **生成摘要**：`_summarize_with` / `_asummarize_with`（详见 1.4）。
8. **回写状态**：返回 `{"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *preserved_messages], "summary_text": summary}`。

### 1.2 DynamicContext 抢救：`_preserve_dynamic_context_reminders`

`DynamicContextMiddleware`（链中 14 号）会向历史注入"当前日期 + 可选记忆"的隐藏 `<system-reminder>` 消息。它采用 ID-Swap 三元组结构：`SystemMessage(id=X)` + `HumanMessage(id=X__memory)` 都带 `dynamic_context_reminder=True` 标记，而 `HumanMessage(id=X__user)` 是真实用户消息、**不带** 标记。如果压缩把三元组拆散，会发生：

- 带标记的 reminder 被压成摘要文本 → `DynamicContextMiddleware` 以为还没注入，又往错误的位置插一遍；
- 不带标记的 `__user` 留在待压缩段 → 用户问题被压成散文、脱离模型直接可见上下文。

为此本方法：

```python
reminders = [msg for msg in messages_to_summarize if is_dynamic_context_reminder(msg)]
if not reminders:
    return messages_to_summarize, preserved_messages

# 用 removesuffix（仅剥后缀，不会误切 "ctx__001" 这种中间带 __ 的稳定 id）
# 收集所有 base id（ctx-001 / ctx-001__memory → base = ctx-001）
reminder_base_ids: set[str] = set()
for msg in reminders:
    if msg.id:
        base = msg.id.removesuffix("__memory").removesuffix("__user")
        reminder_base_ids.add(base)

# 单次扫描保持时间顺序：rescued 既包含带标记的 reminder，
# 也包含 id 以 "<base>__" 开头的未标记 peer（如 X__user、X__memory）
rescued, remaining = [], []
for msg in messages_to_summarize:
    if is_dynamic_context_reminder(msg) or (msg.id and any(msg.id.startswith(b + "__") for b in reminder_base_ids)):
        rescued.append(msg)
    else:
        remaining.append(msg)
return remaining, rescued + preserved_messages
```

关键点：单次扫描保证多个三元组在同一压缩窗口内仍保持原始时间顺序，省去旧实现 `reminders + peers` 拼接所必需的 `id(m)` 去重。

### 1.3 `memory_flush_hook`

`create_summarization_middleware` 在 `memory.enabled` 且 `skip_memory_flush=False` 时，把 `deerflow.agents.memory.summarization_hook.memory_flush_hook` 挂到 `before_summarization` 钩子列表。它在压缩**之前**把 `messages_to_summarize` 投递到记忆队列（`manager.add_nowait`），由记忆后端过滤出用户输入 + 最终 AI 回复并异步入库——这样即使消息被压成摘要，长期记忆里仍保留了原始对话的事实。

子 Agent 链路（`build_subagent_runtime_middlewares`）显式传 `skip_memory_flush=True`：子 Agent 共享父 thread_id，若不跳过，子 Agent 的内部 "Task" 人类消息 + 中间 AI/工具轮次会被错误地写入**父线程**的长期记忆。

`_fire_hooks` 对每个钩子用 `try/except` 包裹，失败仅 `logger.exception` 而不影响压缩本身。事件 `SummarizationEvent` 是 `frozen` dataclass，附带 `thread_id`（从 `runtime.context` 或 `get_config()["configurable"]` 解析）、`agent_name`、原始 `runtime`，便于钩子做归属与 trace 关联。

### 1.4 摘要生成：`_summarize_with`

- **模型绑定**：构造时把 `self.model` 复制为 `self._summary_model`，并合并 `TAG_NOSTREAM` 到现有 tags。注释明确解释了为何要打 `TAG_NOSTREAM`：摘要 LLM 调用本身跑在 LangGraph 中间件钩子里，其 token 流会被 messages-tuple 流回调捕获、作为"幻影 AI 消息"广播给前端。注释也解释了为何**不**在实例级替换 `self.model`：中间件被缓存、跨并发 run 复用，临时替换会在 `await` 期间把 `RunnableBinding` 泄漏给别的协程，破坏 `profile` / `_get_ls_params` 对裸模型的检视；因此使用独立属性 `_summary_model`。同时保留 `middleware:summarize` tag 以便 RunJournal 归因。
- **Prompt 构造**：`_build_summary_prompt` -> `_build_summary_input_text` 把 `formatted_messages` 与可选的 `previous_summary` 套进 `<existing_summary>...</existing_summary>` 与 `<new_messages>...</new_messages>` 标签。
- **XSS / 块逃逸防护**：在写入 XML 块之前用 `html.escape(text, quote=False)` 转义 `<`、`>`、`&`，防止用户消息中包含 `</new_messages>...` 之类的内容提前闭合标签、伪造一个"权威段"给抽取 LLM。注释引用 #4162（`<conversation>` 块）和 #4097（`<memory>` 块）的同源修复。
- **Token 预算**：当配置 `trim_tokens_to_summarize` 时，新消息与旧摘要各占一半预算（`new_message_tokens = max_tokens // 2`），分别用 `first` / `last` 策略裁剪；若 `token_counter` 失败则回退到确定性文本裁剪 `_bound_text`（按 2/3 头部 + 1/3 尾部 + `\n...\n` 占位符，保证截断点不会切分实体）。
- **异常**：任意失败都 `logger.exception` 并 `return None`，`_maybe_summarize` 据此放弃本次压缩（本轮不写状态），避免把损坏的摘要写进 checkpoint。

### 1.5 手动压缩入口

`compact_state` / `acompact_state` 是公开方法，支持 `force=True` 绕过 `_should_summarize` 判定。`POST /api/threads/{id}/compact` 路由复用它：与 `/goal` 写入、run 入场共用同一条 per-thread 串行锁，确保压缩不会与正在运行的 run 或目标更新抢 checkpoint。返回 `ContextCompactionResult` 数据类（`summary_text`、`messages_to_summarize`、`preserved_messages`、`total_tokens`），调用方可只写 `messages` 与 `summary_text` 两个通道版本号，避免触碰其他状态。

## 关键设计决策

1. **把摘要存进 `summary_text` 而不是 `messages`**：`summary_text` 是 `LastValue` 通道，被 `DurableContextMiddleware` 投影成隐藏 `HumanMessage` 数据块注入模型请求，不混入 `messages` 历史。这样既能让模型看到压缩上下文，又不会让摘要文本被新一轮压缩误当作"用户消息"再次压缩，也避免严格 OpenAI 兼容后端（vLLM/SGLang/Qwen/Anthropic）拒绝非前导 SystemMessage。
2. **`RemoveMessage(id=REMOVE_ALL_MESSAGES)` 一次性清空再追加**：LangGraph 提供的"全清"语义，比分条 `RemoveMessage(id=...)` 更稳健，不会因为某条消息缺 id 而残留。注意：这会让 `len(messages)` 在 run 中途骤降，`subagents/step_events.py::capture_new_step_messages` 的游标需在收缩时重置到新尾（详见 backend/AGENTS.md "History contraction"），否则压缩之后的步骤会被丢失到游标差里。
3. **`TAG_NOSTREAM` 与 `_summary_model` 双模型属性**：既防止幻影流，又不破坏父类检视逻辑；用属性拷贝而非实例替换来保证并发安全。
4. **块逃逸防护写在 Summarization 层**：`InputSanitizationMiddleware` 只覆写 ModelRequest、不动 state，所以 summarizer 看到的是真实用户文本。此处必须再转义一次。
5. **Hook 失败不阻断压缩**：记忆 flush 失败只 log，不阻塞主流程；反之压缩失败也不会让本轮状态被污染。

## 与其他中间件的协作

- **DynamicContextMiddleware（链 14）**：被本中间件"抢救"以防止 ID-Swap 三元组被拆散；`is_dynamic_context_reminder` 是共享判定函数。
- **DurableContextMiddleware（链 17）**：把本中间件产出的 `summary_text` 投影成隐藏 HumanMessage 数据块；子 Agent 链路把它放在 summarization **之前**，保证压缩后的请求不以 assistant/tool 开头被严格后端拒绝（#4039）。
- **MemoryMiddleware / memory_flush_hook**：通过 `before_summarization` 钩子把待压缩消息投递给记忆队列；共享 `thread_id`、`agent_name`、`runtime`。
- **SystemMessageCoalescingMiddleware（链 26）**：子 Agent 链路中放在 summarization 之后最内层，合并 DurableContext 注入的第二个 SystemMessage。
- **RunJournal**：通过 `middleware:summarize` tag 把摘要 LLM 调用归因到 summarization 中间件而非 lead_agent。
- **手动压缩路由 `/api/threads/{id}/compact`**：直接调用 `compact_state`，共享 per-thread 串行锁。

---

# 2. TodoMiddleware

## 概述
扩展 `TodoListMiddleware`：当 `write_todos` 工具调用被 summarization 滚出上下文窗口时，重新注入"待办列表仍存在"的提醒；并在模型试图带未完成待办提前结束时，强制跳回 `model` 节点继续工作。

## 为什么需要这个中间件

### 场景痛点
当待办列表通过 `write_todos` 工具调用写入上下文后，被 summarization 滚出窗口，模型会"忘记"自己还有未完成的待办事项，直接结束回答。另一种情况是模型在待办未完成时就试图提前收尾，用户得到不完整的结果。

### 为什么模型自身无法避免
模型只能基于当前可见的消息序列做出判断，无法感知已被压缩截断的工具调用记录。同时模型天生倾向于在"看起来完成了"时结束回答，缺少主动检查 `state["todos"]` 是否还有未完成项的机制。

### 解决思路
在 `before_model` 中检测 write_todos 调用是否已不在窗口中并注入丢失提醒；在 `after_model` 中检测模型意图提前结束时，通过 `jump_to=model` 强制跳回 model 节点继续工作。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/todo_middleware.py` |
| 父类 | `langchain.agents.middleware.TodoListMiddleware` |
| State Schema | `ThreadState`（持有 `todos`、`messages`） |
| 实现的钩子 | `before_agent` / `abefore_agent`、`before_model` / `abefore_model`、`after_model` / `aafter_model`（带 `@hook_config(can_jump_to=["model"])`）、`wrap_model_call` / `awrap_model_call`、`after_agent` / `aafter_agent` |
| 持久化 | 内存：`_pending_completion_reminders` 等 dict（thread_id+run_id 为 key）—— 完成提醒**不**写入 graph state；`before_model` 注入的 `todo_reminder` 写回 `state["messages"]` |
| 配置依赖 | `config.configurable.is_plan_mode=True` 时才被装配；`_MAX_COMPLETION_REMINDERS=2`、`_MAX_COMPLETION_REMINDER_KEYS=4096` 为硬编码常量 |

## 核心逻辑

### 2.1 上下文丢失检测：`before_model`

```python
todos = state.get("todos") or []
if not todos: return None

messages = state.get("messages") or []
if _todos_in_messages(messages):  # 还能看到 write_todos 工具调用
    return None
if _reminder_in_messages(messages):  # 已经注入过提醒且未被截断
    return None

# 待办在 state 但 write_todos 调用已不在窗口 → 注入提醒
reminder = HumanMessage(
    name="todo_reminder",
    additional_kwargs={"hide_from_ui": True},
    content=("<system_reminder>\n"
             "Your todo list from earlier is no longer visible..."
             f"{formatted}\n..."
             "</system_reminder>"),
)
return {"messages": [reminder]}
```

`_todos_in_messages` 扫描 `AIMessage.tool_calls` 中是否存在 `name=="write_todos"`；`_reminder_in_messages` 检查是否已存在 `name=="todo_reminder"` 的 HumanMessage。这条路径写 state，因此提醒会进入 checkpoint、跨轮次存活，直到被 summarization 再次截断。

### 2.2 完成提醒与 `jump_to`：`after_model`

`after_model` 被 `@hook_config(can_jump_to=["model"])` 装饰，允许返回 `{"jump_to": "model"}` 把控制权强回 model 节点。逻辑：

```python
# 1. 保留父类并行 write_todos 检测
base_result = super().after_model(state, runtime)
if base_result is not None: return base_result

# 2. 只在模型想干净退出时介入；工具调用意图 / 解析错误走工具路径
last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
if not last_ai or _has_tool_call_intent_or_error(last_ai): return None

# 3. 全部完成或没有待办 → 放行
todos = state.get("todos") or []
if not todos or all(t.get("status") == "completed" for t in todos): return None

# 4. 重试上限防死循环
if self._completion_reminder_count_for_runtime(runtime) >= self._MAX_COMPLETION_REMINDERS:
    return None

# 5. 入队下一轮提醒，跳回 model；不把控制提示持久化为普通 HumanMessage
self._queue_completion_reminder(runtime, _format_completion_reminder(todos))
return {"jump_to": "model"}
```

**关键 helper `_has_tool_call_intent_or_error`**：跨 LangChain 版本与 provider，工具调用意图可能出现在 `tool_calls`、`invalid_tool_calls`、`additional_kwargs["tool_calls"]` / `["function_call"]`、`response_metadata["finish_reason"] in {"tool_calls","function_call"}` 任一字段。该 helper 统一兜底，**只**有"干净的最终答案"才触发完成提醒；如果哪天 LangChain 升级引入新字段，哨兵测试 `TestToolCallIntentOrError.test_langchain_ai_message_tool_fields_are_explicitly_handled` 会失败，提示维护者审阅此 helper。

### 2.3 提醒注入：`wrap_model_call`

`after_model` 返回 `jump_to` 后，模型节点被重新进入，但提醒**没有**作为 state HumanMessage 持久化（注释解释：否则会"泄漏进用户可见的消息流和保存的 transcript"）。`wrap_model_call` 在每次模型调用前从内存队列里 drain 出来，追加到本次请求的 `request.messages` 末尾：

```python
def _augment_request(self, request: ModelRequest) -> ModelRequest:
    reminders = self._drain_completion_reminders(request.runtime)
    if not reminders: return request
    new_messages = [
        *request.messages,
        HumanMessage(
            content=self._format_pending_completion_reminders(reminders),
            name="todo_completion_reminder",
            additional_kwargs={"hide_from_ui": True},
        ),
    ]
    return request.override(messages=new_messages)
```

`_format_pending_completion_reminders` 用 `dict.fromkeys` 去重并保持顺序（同一轮可能多次入队同一文本）。

### 2.4 per-(thread, run) 计数与清理

key 是 `(thread_id, run_id)` 二元组：

- `before_agent`：`_clear_other_run_completion_reminders` —— 同一线程其他 run 的残留清掉（防止旧 run 的未完成待办污染新 run）。
- `after_agent`：`_clear_current_run_completion_reminders` —— 本 run 结束清自己。
- `_prune_completion_reminder_state_locked`：当 key 数量超过 `_MAX_COMPLETION_REMINDER_KEYS=4096` 时，按 `_completion_reminder_touch_order`（LRU 顺序）淘汰最久未访问的 key，保护长生命周期中间件实例的内存占用。
- 所有 dict 操作都在 `self._lock`（`threading.Lock`）保护下，因 `aafter_model` 直接调 `after_model`、`awrap_model_call` 调 `wrap_model_call`，同步路径会被 LangGraph 在事件循环线程上调用，必须线程安全。

`_get_run_id` 从 `runtime.context` 取 `run_id`，缺省 `"default"`；若 run_id 缺失，同一 thread 的不同 run 会塌缩成同一 key，提醒计数会互相干扰——因此 Gateway 与 `DeerFlowClient.stream()` 都强制提供 run_id。

## 关键设计决策

1. **两种提醒两种持久化策略**：
   - "上下文丢失"提醒 → 写 state，因为它的目标是让"列表仍存在"这一事实在多轮间持续可见；
   - "未完成强行退出"提醒 → 不写 state，只活在内存队列，因为它是控制提示，一旦持久化会污染用户可见消息流。`wrap_model_call` 在请求级别追加是更安全的注入点。
2. **`jump_to` + `wrap_model_call` 组合**：`jump_to` 是 LangGraph 中间件协议里"回到 model 节点"的合法手段，比手动重新调度模型调用更干净；但 `jump_to` 不会带数据，所以需要 `wrap_model_call` 在下轮请求里把提醒补上。
3. **重试硬上限 2 次**：防止 LLM 死循环——模型可能反复尝试"我就要结束"，2 次后放行让它给出最终答案，由用户判断结果质量。
4. **LRU + 4096 硬上限**：长生命周期中间件实例被多线程复用，无界 dict 会内存泄漏；LRU 淘汰最旧 key 而非随机，避免热线程被误清。
5. **`hide_from_ui=True` + `name="todo_*"`**：前端识别这两个标记后隐藏消息，用户不会看到系统提示原文。

## 与其他中间件的协作

- **DeerFlowSummarizationMiddleware**：是本中间件存在的前提——summarization 把 `write_todos` 调用截出窗口才会触发 `before_model` 提醒路径。
- **TodoListMiddleware（父类）**：保留父类的并行 `write_todos` 检测（`super().after_model` 先跑）。
- **TokenUsageMiddleware**：`_build_attribution` 在 `after_model` 中读 `state["todos"]` 把 `write_todos` 调用拆成 `todo_start` / `todo_complete` / `todo_update` / `todo_remove` 动作，依赖本中间件维护的 `todos` 通道。
- **RunJournal / 前端**：通过 `additional_kwargs.hide_from_ui` 与 `name` 识别提醒消息，不展示给用户。

---

# 3. TokenUsageMiddleware

## 概述
在每次模型响应后记录 token 用量日志，并为该 AI 步骤构建结构化的 `token_usage_attribution` 元数据（动作类型、工具调用 id、子 Agent 归因），写入 `AIMessage.additional_kwargs`，供前端步骤化展示与 RunJournal 计费。

## 为什么需要这个中间件

### 场景痛点
每次模型调用产生的 token 消耗缺少结构化归因——运营方无法知道每条 AI 回复消耗了多少 token、调用了哪些工具、子 Agent 的 token 用量归属到了哪个父步骤。这导致计费不精确、调试困难、成本优化无从下手。

### 为什么模型自身无法避免
模型的输出只包含文本和工具调用信息，不包含自身的 token 计量数据。子 Agent 的 token 用量在后台线程中累积，与主线程的模型调用异步解耦。token 统计与归因必须是基础设施层面的职责，模型无法也无法负责提供结构化的用量分解。

### 解决思路
在 `after_model` 中捕获 `usage_metadata`，通过 `pop_cached_subagent_usage` 回填子 Agent 用量，并通过 `write_todos` diff 分析构建 `token_usage_attribution` 元数据写入消息附加信息。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/token_usage_middleware.py` |
| 父类 | `langchain.agents.middleware.AgentMiddleware` |
| 实现的钩子 | `after_model` / `aafter_model`（两者都调 `self._apply`） |
| 持久化 | State：重写 `messages` 列表中相关 `AIMessage` / `ToolMessage` 的 `usage_metadata` 与 `additional_kwargs` |
| 配置依赖 | `token_usage.enabled`（装配开关）；`tools.builtins.task_tool.pop_cached_subagent_usage`（子 Agent 归因数据源） |
| 常量 | `TOKEN_USAGE_ATTRIBUTION_KEY = "token_usage_attribution"` |

## 核心逻辑

### 3.1 差值追踪与子 Agent 归因

子 Agent 通过 `task` 工具派发，其 token 用量被 `SubagentTokenCollector` 累积并按 `tool_call_id` 缓存。本中间件在 `after_model` 中通过 `pop_cached_subagent_usage(tool_call_id)` 把这些用量"回填"到**派发它的那条 AIMessage**：

```python
state_updates: dict[int, AIMessage] = {}
if len(messages) >= 2:
    idx = len(messages) - 2  # 跳过最新 AIMessage，从倒数第二条 ToolMessage 往回扫
    while idx >= 0:
        tool_msg = messages[idx]
        if not isinstance(tool_msg, ToolMessage) or not tool_msg.tool_call_id:
            break  # 遇到非 ToolMessage 或无 id 就停，保证只处理本步骤的工具结果
        subagent_usage = pop_cached_subagent_usage(tool_msg.tool_call_id)
        if subagent_usage:
            # 再往回找派发它的 AIMessage（同一步可派发多个 task，偏移不固定）
            dispatch_idx = idx - 1
            while dispatch_idx >= 0:
                candidate = messages[dispatch_idx]
                if isinstance(candidate, AIMessage) and _has_tool_call(candidate, tool_msg.tool_call_id):
                    # 多个 task 同一 response 时累加，而非覆盖
                    existing = state_updates.get(dispatch_idx)
                    prev = existing.usage_metadata if existing else (getattr(candidate, "usage_metadata", None) or {})
                    merged = {
                        **prev,
                        "input_tokens":  prev.get("input_tokens", 0)  + subagent_usage["input_tokens"],
                        "output_tokens": prev.get("output_tokens", 0) + subagent_usage["output_tokens"],
                        "total_tokens":   prev.get("total_tokens", 0)   + subagent_usage["total_tokens"],
                    }
                    state_updates[dispatch_idx] = candidate.model_copy(update={"usage_metadata": merged})
                    break
                dispatch_idx -= 1
        idx -= 1
```

关键点：
- `pop_` 语义：取出并清缓存，避免下一轮重复归因；
- 连续 `ToolMessage` 扫描：支持同一 model response 派发多个并发 task；
- `state_updates` dict 累积：同一 AIMessage 可能被多个 ToolMessage 回填，用 `model_copy` 生成新对象再合并进既有 update，避免链式 `model_copy` 丢失前面的合并；
- `_has_tool_call` 容错 dict 与对象两种 tool_call 表示。

### 3.2 Attribution 构建

最新 AIMessage（`messages[-1]`）如果确实是 AIMessage，会被 `_build_attribution` 标注：

```python
def _build_attribution(message: AIMessage, todos: list[Todo]) -> dict[str, Any]:
    tool_calls = getattr(message, "tool_calls", None) or []
    actions, current_todos = [], list(todos)
    for raw_tool_call in tool_calls:
        described = _describe_tool_call(raw_tool_call, current_todos)
        actions.extend(described)
        if raw_tool_call.get("name") == "write_todos":
            current_todos = _normalize_todos(args.get("todos"))  # 顺序推进 current_todos
    return {
        "version": 1,
        "kind": _infer_step_kind(message, actions),
        "shared_attribution": len(actions) > 1,
        "tool_call_ids": [...],
        "actions": actions,
    }
```

**动作分类 `_describe_tool_call`**（按工具名路由）：
- `write_todos`：把 `args.todos` 与 `current_todos` diff，生成 `todo_start` / `todo_complete` / `todo_update` / `todo_remove` 动作（见 3.3）；
- `task`：生成 `subagent` 动作，带 `description`、`subagent_type`；
- `web_search` / `image_search`：`search` 动作带 `query`；
- `present_files`：`present_files` 动作；
- `ask_clarification`：`clarification` 动作；
- 其他：通用 `tool` 动作带 `tool_name` + `description`。

**步骤类型推断 `_infer_step_kind`**：
- 有动作：单个 todo 类动作 → `todo_update`；单个 subagent → `subagent_dispatch`；多动作 → `tool_batch`；
- 无动作 + 有 content → `final_answer`；
- 无动作 + 无 content → `thinking`。

**幂等保护**：如果新计算的 `attribution` 与既有 `additional_kwargs[TOKEN_USAGE_ATTRIBUTION_KEY]` 相等，就跳过 `model_copy`（减少无谓的对象重建），只在有子 Agent update 时返回它们；否则把新 attribution 写进 `additional_kwargs` 并加入 `state_updates`。

### 3.3 `_build_todo_actions` —— 单一真源

代码注释明确："This is the single source of truth for precise write_todos token attribution. The frontend intentionally falls back to a generic 'Update to-do list' label when this metadata is missing or malformed."

匹配算法：
1. 先按 `content` 把 `previous_todos` 索引到 `previous_by_content: dict[str, list[tuple[int, Todo]]]`；
2. 遍历 `next_todos`，优先按 content 匹配并标记 `matched_previous_indices`，避免一条 prev 被多条 next 错配；
3. 若 content 没匹配但 `index < len(previous_todos)` 且该位置未占用，退而求其次按位置匹配；
4. 完全匹配（content + status 都一致）的动作跳过；
5. 最后把 `previous_todos` 中未被匹配的标为 `todo_remove`。

**`_todo_action_kind`**：prev is None 且 `completed`→`todo_complete`、`in_progress`→`todo_start`、其他→`todo_update`；prev 存在但 content 变了→`todo_update`；其余按 status 分流。

### 3.4 日志

最新 AIMessage 的 `usage_metadata` 被格式化为 `INFO` 级日志：

```
LLM token usage: input=1234 output=567 total=1801 input_token_details={...} output_token_details={...}
```

带 `input_token_details` / `output_token_details`（如 cache hit、reasoning tokens）时附在末尾。

## 关键设计决策

1. **Schema 版本字段 `version: 1`**：注释说明"Schema changes should remain additive where possible so older frontends can ignore unknown fields and fall back safely"。新增字段而非改变枚举是跨语言契约（前端 TS、run_events SQL）的兼容性策略。
2. **`shared_attribution` 标志**：一条 AIMessage 多个 tool_call 时前端可决定是展示"批量步骤"还是逐个展示。
3. **子 Agent 归因合并到派发 AIMessage 而非独立行**：让一条 AI 步骤的总 token 数（lead 自己 + 所有 subagent）在同一处可读，计费/Console 视图不必跨表 JOIN；但代价是子 Agent 的明细只能通过 `subagent.start`/`subagent.step`/`subagent.end` 事件查看，不能从 `messages` 重建。
4. **顺序推进 `current_todos`**：同一 AIMessage 里多个 `write_todos` 调用按时间顺序应用，`_describe_tool_call` 的每次调用都看到前一个调用之后的 todos，diff 才正确。
5. **幂等判定**：避免每轮 `model_copy` 重建最后一条 AIMessage，减少 checkpoint 写入压力。

## 与其他中间件的协作

- **TodoMiddleware / TodoListMiddleware**：本中间件读 `state["todos"]` 并按 `write_todos` diff 生成动作；依赖 Todo 中间件维护的 `todos` 通道。
- **SubagentExecutor / task_tool**：通过 `pop_cached_subagent_usage` 消费子 Agent 累积的 token 缓存；`SubagentTokenCollector` 是生产者。
- **RunJournal / 计费层**：`token_usage_by_model`、`cache_read_tokens` 等字段最终被 `runs` 表与 Console `/api/console` 路由消费，构成 per-model 成本与日序列。
- **前端**：`token_usage_attribution` 是步骤化 UI 的数据源；缺失时前端回退到"Update to-do list"等通用标签。

---

# 4. TitleMiddleware

## 概述
在首轮完整对话（一问一答）结束后自动为 thread 生成标题；支持 LLM 生成与本地回退两条路径，并在首轮运行被中断时由 run worker 调用本地回退保证标题仍能持久化。

## 为什么需要这个中间件

### 场景痛点
新创建的 thread 会一直显示为"New Conversation"或空标题，用户在侧边栏的对话列表中无法快速定位到目标对话。如果首轮对话在标题生成前被中断（取消或失败），标题永远不会生成，thread 始终是未命名状态。

### 为什么模型自身无法避免
模型专注于回答用户问题本身，不具备在对话结束后自动回顾并总结标题的机制。即使提示模型在回答末尾生成标题，也无法保证格式一致性、长度控制，以及中断场景下的兜底处理。

### 解决思路
在首轮完整对话（一问一答）结束后自动调用 LLM 生成结构化标题，并配置本地回退路径（截取首条用户消息前 50 字符）；首轮被中断时由 run worker 独立调用回退逻辑确保持久化。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/title_middleware.py` |
| 父类 | `langchain.agents.middleware.AgentMiddleware` |
| State Schema | `TitleMiddlewareState`（兼容 `ThreadState`，扩展 `title: NotRequired[str | None]`） |
| 实现的钩子 | `after_model` / `aafter_model` |
| 持久化 | State：写 `state["title"]`；最终同步到 `threads_meta.display_name` |
| 配置依赖 | `config.yaml -> title.*`（`enabled`、`max_words`、`max_chars`、`model_name`、`prompt_template`）；`title_config` 可显式注入（测试用） |

## 核心逻辑

### 4.1 触发判定：`_should_generate_title`

```python
if not config.enabled: return False
if state.get("title"): return False  # 已有标题不覆盖

min_messages = 1 if allow_partial_exchange else 2
if len(messages) < min_messages: return False

user_messages = [m for m in messages if self._is_user_message_for_title(m)]
assistant_messages = [m for m in messages if self._message_type(m) == "ai"]
return len(user_messages) == 1 and (len(assistant_messages) >= 1 or allow_partial_exchange)
```

- 默认要求 1 条用户消息 + 至少 1 条 AI 回复（完整一轮）。
- `allow_partial_exchange=True` 的路径：只需 1 条用户消息，给 run worker 在运行被取消、还没有 AI chunk 落到 checkpoint 时生成回退标题用。
- 只处理**首轮**——`len(user_messages) == 1`；第二轮以后用户追加消息不重新生成标题。
- `_is_user_message_for_title` 排除了 `is_dynamic_context_reminder` 标记的隐藏 reminder——否则首轮用户消息可能被 DynamicContext 注入的 reminder 顶替。

### 4.2 LLM 生成路径：`_agenerate_title_result`

```python
config = self._get_title_config()
if not config.model_name:
    return {"title": self._fallback_title(user_msg)}  # 无模型 → 直接本地回退

prompt, user_msg = self._build_title_prompt(state)
model_kwargs = {"thinking_enabled": False, "attach_tracing": False}
if self._app_config is not None: model_kwargs["app_config"] = self._app_config
model = create_chat_model(name=config.model_name, **model_kwargs)
response = await model.ainvoke(prompt, config=self._get_runnable_config())
title = self._parse_title(response.content)
if title: return {"title": title}
# 失败或空标题 → 回退
return {"title": self._fallback_title(user_msg)}
```

**`_build_title_prompt`**：从 `config.prompt_template` 取模板，填入 `max_words`、`user_msg[:500]`、`assistant_msg[:500]`。注意截断到 500 字符——既控制 prompt 成本，又避免长输入让小模型跑偏。

**`_strip_think_tags`**：`re.sub(r"\<think\>[\s\S]*?\</think\>", "", text, flags=re.IGNORECASE)`。兼容 `reasoning` 模型（minimax、DeepSeek-R1）在输出中夹带的 `<think>...</think>` 推理块，把其剥离后再解析标题。

**`_parse_title`**：规范化流程 —— `_normalize_content` 把 list/dict/str 形式的 content 拍平成字符串 → `_strip_think_tags` 再剥一次（模型可能把 think 放在 content 里）→ `strip().strip('"').strip("'")` 去掉常见引号包裹 → 按 `config.max_chars` 截断。

**`_get_runnable_config`**：

```python
parent = get_config()  # 继承 graph 级 RunnableConfig
config = {**parent}
config["run_name"] = "title_agent"
config["tags"] = [*(config.get("tags") or []), "middleware:title", TAG_NOSTREAM]
```

`TAG_NOSTREAM` 与 SummarizationMiddleware 同源意图：阻止标题 LLM 调用的 token 流被 messages-tuple 回调当成幻影 AI 消息广播给前端。`middleware:title` 让 RunJournal 把这次调用归到 title 中间件而非 lead_agent。`attach_tracing=False` 因为父 config 的 callbacks 已带 tracing handler，再绑一次会重复发 span。

### 4.3 本地回退：`_fallback_title`

```python
fallback_chars = min(config.max_chars, 50)
if len(user_msg) > fallback_chars:
    ellipsis = "..."
    body = min(fallback_chars, config.max_chars - len(ellipsis))
    return user_msg[:body].rstrip() + ellipsis
return user_msg if user_msg else "New Conversation"
```

- 不调用 LLM，直接从用户首条消息截前 50 字符（或 `max_chars`，取小）；
- 留出 `...` 占位符空间，保证最终标题长度严格不超过 `max_chars`（与 `_parse_title` 的截断行为一致）；
- 空用户消息返回 `"New Conversation"`。

### 4.4 同步路径 `after_model`

只调 `_generate_title_result`，即只走本地回退，不调用 LLM。注释明确这是有意为之：同步路径不应阻塞事件循环去做网络 LLM 调用，留给异步路径处理；如果只配置了 `after_model`（同步图）就只生成本地标题。

### 4.5 中断恢复路径

`backend/AGENTS.md` 的 TitleMiddleware 条目说明：如果首轮 run 在本中间件能写标题之前就被取消，`runtime/runs/worker.py` 会让 run 维持 finalizing 状态、从最新 checkpoint 或原始 run 输入里生成一个本地回退标题，然后同步到 `threads_meta.display_name`。`_generate_title_result` 与 `_fallback_title` 被设计成可独立由 worker 调用（不依赖 state 完整性），支撑这一路径。后续被 `multitask_strategy="interrupt"` / `"rollback"` 接纳的替换 run 会等待旧 run 的 finalization 完成再入图，避免标题竞争。

### 4.6 消息规范化 helpers

- `_normalize_content`：递归把 list（多模态 content）、dict（`{"text": ...}` 或 `{"content": ...}` 嵌套）、str 拍平为字符串，兼容 provider 返回的多种 content 形态。
- `_message_type`：兼容 `message.type` 属性与 dict 的 `type` / `role`；把 `"user"` 映射为 `"human"`、`"assistant"` 映射为 `"ai"`。
- `_message_content`：兼容 dict 与对象。
- `_is_dynamic_context_reminder_message`：除 `is_dynamic_context_reminder` 还额外检查 dict 形式的 `additional_kwargs.dynamic_context_reminder`。

## 关键设计决策

1. **LLM 与本地回退二元化**：`title.model_name=null` 走本地截断（便宜、零延迟、无网络依赖），显式配置 `model_name` 才走 LLM。运营方按成本/质量选择。
2. **同步钩子只回退、异步钩子才 LLM**：避免同步图上的网络调用阻塞事件循环；LangGraph 优先走异步路径，所以生产里 LLM 仍会被触发。
3. **`TAG_NOSTREAM` + `middleware:title` 双 tag**：与 Summarization 同模式，统一中间件发起的 LLM 调用治理。
4. **继承父 RunnableConfig**：tracing callbacks 已在 graph 根绑定，模型级不再重复绑定，避免 span 重复。
5. **首轮判定用 `user_messages == 1` 而非 `len(messages)`**：DynamicContext、todo reminder、summarization summary 都可能往 messages 里加非用户消息，按类型计数更稳健。
6. **中断 fallback 单独由 worker 调用**：让"首轮被取消"不再是用户看到 "New Conversation" 的原因，首条用户消息前 50 字符总能成为标题。

## 与其他中间件的协作

- **DynamicContextMiddleware**：通过 `is_dynamic_context_reminder` 排除 reminder 消息，避免误把它当用户首问。
- **DeerFlowSummarizationMiddleware**：首轮一般不会触发压缩；但若用户首问特长，summarization 可能在首轮就跑，Title 依赖 `messages` 里有 ≥1 条 HumanMessage 与 ≥1 条 AIMessage 的存在，summarization 的 `RemoveMessage(REMOVE_ALL_MESSAGES)` 不会清空 `state["title"]` 通道，所以两者无直接冲突。
- **RunJournal / `runtime/runs/worker.py`**：通过 `middleware:title` tag 归因；worker 在 finalizing 阶段兜底调用 `_fallback_title`。
- **`threads_meta` 表 / Gateway threads 路由**：`state["title"]` 的最终落地是 `threads_meta.display_name`，前端侧边栏列表由它驱动。

---

# 5. MemoryMiddleware

## 概述
在 Agent 执行结束后把本轮对话投递到记忆队列，由后端异步 LLM 抽取出用户上下文与离散事实写入 per-user `memory.json`；中间件本身不做记忆抽取，只负责在请求上下文还活着时捕获 `user_id` 与 `trace_id`。

## 为什么需要这个中间件

### 场景痛点
每次对话结束后，模型对用户一无所知——用户上次提到的个人偏好、工作背景、项目细节全部丢失。下次交互时模型无法引用任何历史信息，导致每次对话都要重新建立上下文，体验割裂。

### 为什么模型自身无法避免
模型是无状态的，每次调用都独立于之前的调用。模型无法自动将本次对话中学到的信息持久化到下次对话。跨会话的"长期记忆"必须是基础设施层面的持久化、抽取和检索系统。

### 解决思路
在 Agent 执行结束后将本轮消息投递到记忆队列，由后端异步 LLM 抽取出用户上下文与离散事实写入 per-user 的 `memory.json`，下次交互时通过 DynamicContextMiddleware 注入 system prompt。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/memory_middleware.py` |
| 父类 | `langchain.agents.middleware.AgentMiddleware` |
| State Schema | `MemoryMiddlewareState`（兼容 `ThreadState`，无额外字段） |
| 实现的钩子 | `after_agent` / `aafter_agent` |
| 持久化 | 无 State 写入；投递到 `MemoryManager` 的内存队列（debounced） |
| 配置依赖 | `config.yaml -> memory.*`（`enabled`、`mode`（middleware/tool）、`debounce_seconds`、`shutdown_flush_timeout_seconds` 等）；`memory.mode=tool` 时本中间件不被装配 |
| 关键依赖 | `deerflow.agents.memory.get_memory_manager`、`deerflow.runtime.user_context.get_effective_user_id`、`deerflow.trace_context`（`DEERFLOW_TRACE_METADATA_KEY`、`get_current_trace_id`、`normalize_trace_id`） |

## 核心逻辑

### 5.1 入队时机：`after_agent`

在 Agent 完成全部轮次后被调用一次（非每轮）：

```python
config = self._memory_config or get_memory_config()
if not config.enabled: return None

# 1. 解析 thread_id
thread_id = runtime.context.get("thread_id") if runtime.context else None
if thread_id is None:
    config_data = get_config()
    thread_id = config_data.get("configurable", {}).get("thread_id")
if not thread_id:
    logger.debug("No thread_id in context, skipping memory update")
    return None

# 2. 取消息
messages = state.get("messages", [])
if not messages:
    logger.debug("No messages in state, skipping memory update")
    return None

# 3. 在请求上下文还活着时捕获 user_id
user_id = get_effective_user_id()

# 4. 解析 trace_id
runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
trace_id = normalize_trace_id(runtime_context.get(DEERFLOW_TRACE_METADATA_KEY))
if trace_id is None:
    try: config_data = get_config()
    except RuntimeError: config_data = {}
    config_metadata = config_data.get("metadata", {}) if isinstance(config_data.get("metadata"), dict) else {}
    trace_id = normalize_trace_id(config_metadata.get(DEERFLOW_TRACE_METADATA_KEY))
if trace_id is None:
    trace_id = get_current_trace_id()

# 5. 投递（过滤、校验、去重由后端做）
get_memory_manager().add(
    thread_id, messages,
    agent_name=self._agent_name,
    user_id=user_id, trace_id=trace_id,
)
return None
```

注释明确分工："Hand raw messages to the manager; the backend filters to user + final-AI turns, validates, detects correction/reinforcement, and enqueues."——本中间件只负责"在正确的时机、带着正确的身份"把原始消息递过去。

### 5.2 `user_id` 捕获：Timer 线程边界

注释解释了为什么要在此处显式捕获 `user_id`：

> Capture user_id at enqueue time while the request context is still alive.
> threading.Timer fires on a different thread where ContextVar values are not
> propagated, so we must store user_id explicitly in ConversationContext.

`MemoryManager.add` 内部用 `threading.Timer(debounce_seconds, ...)` 做去抖批量处理。`Timer` 在独立线程跑，而 `get_effective_user_id()` 依赖 ContextVar（请求级 user_id 由 `AuthMiddleware` / `get_effective_user_id` 写入）。ContextVar 默认**不跨线程传播**，所以若在 Timer 线程才解析 user_id 会拿到 `"default"`，导致认证模式下的记忆全部落到默认桶。这里在 `after_agent`（仍在请求线程）解析、作为参数传入，让 `MemoryManager` 把它存进 `ConversationContext`，Timer 线程读的是对象属性而非 ContextVar。

### 5.3 `trace_id` 解析三级回退

1. `runtime.context[DEERFLOW_TRACE_METADATA_KEY]`：run 启动时由 worker / `DeerFlowClient.stream` 注入的 trace 元数据；
2. `get_config()["metadata"][DEERFLOW_TRACE_METADATA_KEY]`：RunnableConfig 级别的 metadata（由 `build_langfuse_trace_metadata` 写入）；
3. `get_current_trace_id()`：直接读 `trace_context` ContextVar（`TraceMiddleware` 在 HTTP 请求级绑定）。

三级回退确保无论哪个入口（Gateway HTTP、DeerFlowClient 嵌入、TUI）运行的 Agent，记忆更新都能关联到正确的 trace，便于在 Langfuse 里把"记忆抽取 LLM 调用"与"主对话"归到同一 trace。

### 5.4 后端做什么（不在本文件内，但被本中间件依赖）

`MemoryManager.add(thread_id, messages, agent_name, user_id, trace_id)` 内部（参见 `agents/memory/queue.py` 与 `updater.py`）：
- 过滤 messages 到 "用户输入 + 最终 AI 回复"；
- 校验消息结构；
- 检测纠正/强化信号；
- per-thread 去重，按 `debounce_seconds` 去抖批量处理；
- `MemoryUpdater` 调用 LLM 抽取上下文更新 + 离散事实 + 过时审查 + 合并；
- 原子写 `memory.json`（temp file + rename），缓存失效；
- 下次交互时 `DynamicContextMiddleware` 把记忆以 `<memory>` 块注入 system prompt（由 `injection_enabled` 控制）。

### 5.5 模式二选一

- `memory.mode: middleware`（默认）：本中间件被装配，走被动抽取；
- `memory.mode: tool`：本中间件**不被装配**，改由 `memory_search` / `memory_add` / `memory_update` / `memory_delete` 工具让模型主动 CRUD 记忆。两种模式共享同一套 `FileMemoryStorage` / `MemoryUpdater` / per-user 隔离 / prompt 体系。

## 关键设计决策

1. **`after_agent` 而非 `after_model`**：一次 Agent 执行可能多轮模型调用，每轮都投递会重复处理相同消息、浪费 LLM 调用；`after_agent` 在整轮结束后投递一次，去抖批量更合理。
2. **不做抽取、只做入队**：抽取需要 LLM 调用，耗时数百毫秒到秒级；放在请求线程会拖慢响应。入队 + 去抖 + 后台 Timer 让用户立刻拿到响应，记忆异步生成。
3. **`user_id` 在请求线程捕获**：解决 ContextVar 不跨 `threading.Timer` 线程的问题；否则认证模式记忆会塌缩到默认桶。
4. **`trace_id` 三级回退**：覆盖 HTTP / 嵌入 / TUI 三入口；确保 Langfuse 里记忆抽取与主对话可关联。
5. **显式跳过子 Agent 链路**：子 Agent 共享父 thread_id，若也装配本中间件，子 Agent 内部轮次会污染父线程记忆。这与 Summarization 的 `skip_memory_flush=True` 是同一问题两面：前者防"压缩时 flush 子 Agent 轮次"，后者防"结束后 queue 子 Agent 轮次"。
6. **`memory.enabled` 与 `memory.mode` 分离**：`enabled` 是总开关；`mode` 决定走哪条路径。`enabled=false` 时无论 mode 为何，本中间件都不做事。

## 与其他中间件的协作

- **DeerFlowSummarizationMiddleware**：通过 `before_summarization` 钩子（`memory_flush_hook`）在压缩前把消息 flush 到同一 `MemoryManager`（`add_nowait` 旁路，绕过去抖立即入队）。两者都向同一队列投递，但时机不同：summarization flush 的是"即将被压缩丢失"的消息，本中间件 flush 的是"本轮结束后的消息"。
- **DynamicContextMiddleware**：消费记忆产物——把 `memory.json` 的内容以 `<memory>` 块注入下一轮 system prompt。本中间件是生产者，DynamicContext 是消费者。
- **SubagentExecutor / `build_subagent_runtime_middlewares`**：显式不装配本中间件，防止子 Agent 污染父线程记忆。
- **`runtime/runs/worker.py`**：通过 `DEERFLOW_TRACE_METADATA_KEY` 把 trace id 注入 `runtime.context`，本中间件读取之。
- **Gateway lifespan**：在优雅停机时调用 `MemoryManager.shutdown_flush(timeout)`，把还在队列里的待处理项 drain 完（`memory.shutdown_flush_timeout_seconds`，默认 30s）；本中间件本身不参与 drain，但它的入队行为决定了 drain 的工作量。
- **`/api/memory*` 路由**：`GET /` 读 `memory.json`、`POST /reload` 强制重读、`GET /status` 返回配置 + 数据；这些是用户可见的记忆管理入口，本中间件产生的数据通过它们被查看与刷新。

---

## 五款中间件协作全景

| 流程阶段 | Summarization | Todo | TokenUsage | Title | Memory |
|---------|---------------|------|------------|-------|--------|
| 模型调用前 | `before_model` 压缩 | `before_model` 注入 todo_reminder | — | — | — |
| 模型调用 wrap | — | `wrap_model_call` 注入完成提醒 | — | — | — |
| 模型响应后 | — | `after_model` `jump_to=model` | `after_model` 记 token + 归因 | `after_model` 同步回退标题 | — |
| Agent 结束后 | — | `after_agent` 清当前 run 计数 | — | — | `after_agent` 入队记忆 |
| Agent 开始前 | — | `before_agent` 清他 run 计数 | — | — | — |
| 压缩前钩子 | `_fire_hooks` 派发 `BeforeSummarizationHook` | — | — | — | `memory_flush_hook` flush 到记忆队列 |

数据流概览：

```
用户消息 → DynamicContextMiddleware 注入 <memory> 块（来自 MemoryMiddleware 上一轮入队、MemoryUpdater 异步抽取的结果）
        ↓
模型调用 → (TodoMiddleware.wrap_model_call 注入完成提醒)
        ↓
模型响应 → TokenUsageMiddleware 归因 + 子 Agent token 回填
        ↓
若模型试图提前结束 → TodoMiddleware.after_model 返回 jump_to=model
        ↓
Agent 结束 → TitleMiddleware 写 state["title"]
        ↓  MemoryMiddleware.after_agent 入队 (thread_id, messages, user_id, trace_id)
        ↓
MemoryManager (debounced Timer 线程) → MemoryUpdater LLM 抽取 → memory.json
        ↓
若 token 超限 → SummarizationMiddleware.before_model 压缩前 fire memory_flush_hook → 把待压缩消息 flush 到同一队列
        ↓
下一轮 DynamicContextMiddleware 读 memory.json → 注入 <memory> 块 → 循环
```

这五款中间件共同构成了 DeerFlow 的"对话状态管理平面"：Summarization 控制上下文窗口大小，Todo 维持任务推进，TokenUsage 提供计费与可观测性，Title 维护 UI 元数据，Memory 维护跨会话长期记忆。它们通过共享 `ThreadState` 通道（`messages`、`todos`、`title`、`summary_text`）、共享 `runtime.context`（`thread_id`、`run_id`、`agent_name`、`DEERFLOW_TRACE_METADATA_KEY`）以及 `BeforeSummarizationHook` 钩子协议协作，既各司其职又彼此补位。
