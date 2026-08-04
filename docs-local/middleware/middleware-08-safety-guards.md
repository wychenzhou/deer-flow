# 安全守卫中间件详解 (middleware-08-safety-guards)

本文档深入解析 DeerFlow 中六个核心"安全守卫类"中间件的实现细节。这些中间件位于 `packages/harness/deerflow/agents/middlewares/` 目录下，它们构成了 Lead Agent (以及子 Agent) 的运行时安全护栏：限制子 Agent 调用、打断工具调用死循环、约束 token 预算、保证终端响应可见、抑制被安全过滤截断的工具调用、以及人机交互式澄清。

下文按顺序对每个中间件展开说明，并附上源码关键片段、状态机/数据流图、设计权衡以及与其他中间件的协作关系。

---

## 目录

1. [SubagentLimitMiddleware](#1-subagentlimitmiddleware)
2. [LoopDetectionMiddleware](#2-loopdetectionmiddleware)
3. [TokenBudgetMiddleware](#3-tokenbudgetmiddleware)
4. [TerminalResponseMiddleware](#4-terminalresponsemiddleware)
5. [SafetyFinishReasonMiddleware](#5-safetyfinishreasonmiddleware)
6. [ClarificationMiddleware](#6-clarificationmiddleware)
7. [辅助模块](#辅助模块)
   - [safety_termination_detectors.py](#safety_termination_detectorspy)
   - [_bounded_dict.py](#bounded_dictpy)
   - [tool_call_metadata.py](#tool_call_metadatapy)

---

# 1. SubagentLimitMiddleware

## 概述

在单次模型响应或单次 run 中，强制截断超出配额的 `task` (子 Agent 委派) 工具调用，既防止一次响应里并发开过多子 Agent，也防止单次 run 内反复分批委派绕过并发限制。

## 为什么需要这个中间件

### 场景痛点

模型在一次响应中可能生成十几个甚至几十个 `task`（子 Agent 委派）工具调用，把线程池瞬间打满，导致其他并发的子 Agent 排队阻塞甚至超时。更隐蔽的情况是，Lead Agent 在同一个 run 的多个 planning checkpoint 分批发出"合法大小"的任务批次，累积总量远超预期，导致 run 无法收敛。

### 为什么模型自身无法避免

LLM 对并发执行环境一无所知——它不知道线程池有多大、当前还有多少子 Agent 在运行。它也缺少跨 turn 的累计计数能力：每一轮的 `task` 调用在模型看来都是独立决策，模型无法感知本 run 已经派发了多少委派、还剩多少配额。

### 解决思路

在 `after_model` 阶段截断超额的 `task` 调用：并发上限限制单次响应的并行子 Agent 数，总量上限通过 `delegations` 账本统计 run 内累计委派数，双重限制互补。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |
| 实现的钩子 | `after_model`, `aafter_model` |
| 持久化 | 内存计算 + 从 State(`delegations` 账本)读取，无自身持久化 |
| 配置依赖 | `subagents.max_total_per_run` (默认 6, 范围 1-50) 与运行时 `max_concurrent_subagents` (默认 3, 范围 2-4)，通过 `Runtime.context.run_id` 标记当前 run |

## 核心逻辑

### 双重限制

- **并发限制 (`max_concurrent`)**: 单条 `AIMessage` 中 `tool_calls` 数组里 `name == "task"` 的调用最多保留 N 个 (默认 3，clamp 到 2-4)，其余丢弃。
- **总量限制 (`max_total`)**: 单次 run 内累计的委派数不能超过 M (默认 6，clamp 到 1-50)。计数来自 `state["delegations"]` 账本中带相同 `run_id` 标签的条目，去重 `id`。

### 关键代码：截断逻辑

```python
def _truncate_task_calls(self, state: AgentState, runtime: Runtime | None = None) -> dict | None:
    # ...
    task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
    if not task_indices:
        return None

    run_id = _runtime_run_id(runtime)
    prior_delegation_count = _count_prior_delegations(state.get("delegations"), run_id=run_id)
    remaining_total = max(0, self.max_total - prior_delegation_count)
    allowed_task_calls = min(self.max_concurrent, remaining_total)

    if len(task_indices) <= allowed_task_calls:
        return None

    indices_to_drop = set(task_indices[allowed_task_calls:])
    truncated_tool_calls = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]
    # ...
```

`remaining_total = max(0, max_total - prior_delegation_count)` 计算当前 run 还剩多少委派额度。`allowed_task_calls = min(max_concurrent, remaining_total)` 取并发上限与剩余额度中的较小者，作为本次响应允许保留的 `task` 调用数。

### 总量耗尽时的标注

当 `remaining_total == 0` (本 run 已达总量上限) 时，中间件会做两件事：

1. 通过 `runtime.context["stop_reason"] = "subagent_limit_capped"` 写入运行时上下文，供 Lead Worker 读取 (#4176)。
2. 给 `AIMessage.content` 追加一条可见的告警文本：

```python
_TOTAL_LIMIT_STOP_MSG = (
    "[SUBAGENT LIMIT REACHED] The subagent delegation limit for this run has been reached. "
    "Continue using the subagent results already collected, execute remaining simple work "
    "directly, or summarize the remaining work instead of launching more subagents."
)
```

3. 调用 `clone_ai_message_with_tool_calls(last_msg, truncated_tool_calls, content=content)` 重建 `AIMessage`，保持相同的 `id` 以便 LangGraph 视为替换而非追加。

### run_id 计数语义

`_count_prior_delegations` 通过 `_delegation_run_id` 读取每条账本条目的 `run_id` 字段，只统计与当前 run 匹配的条目：

```python
def _count_prior_delegations(delegations: object, *, run_id: str | None) -> int:
    if not isinstance(delegations, list):
        return 0
    ids = set()
    for entry in delegations:
        if run_id is not None and _delegation_run_id(entry) != run_id:
            continue
        delegation_id = _delegation_id(entry)
        if delegation_id is not None:
            ids.add(delegation_id)
    return len(ids)
```

**fail-restrictive 语义**：当 `run_id` 缺失时，中间件记一条警告并退化为"统计该 thread 的全部历史委派"——即上限更紧，宁可误杀不可漏判。

## 状态机图

```
        ┌────────────────────────────────────────┐
        │ AIMessage(tool_calls=[task, ...]) 到达 │
        └──────────────────┬─────────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ 无 task 工具调用?       │── 是 ──▶ 不干预，返回 None
              └──────────┬─────────────┘
                         │ 否
                         ▼
            ┌──────────────────────────────┐
            │ 读取 run_id；统计账本中相同    │
            │ run_id 的委派数 N_prior       │
            └──────────┬───────────────────┘
                       │
                       ▼
       ┌────────────────────────────────────┐
       │ remaining = max(0, max_total - N)  │
       │ allowed  = min(max_concurrent, N) │
       └─────────────┬──────────────────────┘
                     │
                     ▼
        ┌───────────────────────────┐
        │ len(task_indices) <= allowed? │
        └──────┬────────────────┬─────┘
              是              否
               │                │
               ▼                ▼
        返回 None        截断 + 替换 AIMessage
                          │
                          ▼
              ┌──────────────────────────┐
              │ remaining == 0 ?         │
              │  是: 写 stop_reason、追加 │
              │  _TOTAL_LIMIT_STOP_MSG   │
              │  否: 仅截断 tool_calls   │
              └──────────────────────────┘
```

## 关键设计决策

1. **双重限制而非单一限制**：并发上限防止单条响应里开太多并行子 Agent 把 executor 线程池打爆；总量上限防止 Lead 在一次 run 的多个 planning checkpoint 反复发起"合法大小"的批次绕过并发限制。
2. **按 `run_id` 标签计数**：同一 thread 的历史 run 委派不消耗当前 run 的预算——用户在同一 thread 的下一个 turn 应获得全新的委派配额。
3. **fail-restrictive**：`run_id` 缺失时按全 thread 计数，宁可更紧也不放水。
4. **不改 model 调用而是替换 AIMessage**：用 `clone_ai_message_with_tool_calls` 保持相同 `id`，LangGraph 视作原消息的替换，下游 reducer / checkpointer 不会产生多余条目。
5. **配套 Lead prompt**：Lead 的系统提示里使用同样的 clamp 后的值，让模型可见的限制与实际强制对齐，减少模型尝试调用超额 task 的概率。

## 与其他中间件的协作

- **DurableContextMiddleware**：它将 `task` 委派结果落入 `ThreadState.delegations` 账本并打 `run_id` 标签，正是本中间件计数的数据来源。
- **LoopDetectionMiddleware / TokenBudgetMiddleware**：三者共同构成子 Agent 的"guard cap 三轴" (turn / token / loop)，本中间件负责"委派"这一独立轴。
- **SubagentExecutor**：执行实际 `task` 调用，依赖本中间件先做过并发截断。
- **Lead Worker (`runtime/runs/worker.py`)**：读取 `runtime.context["stop_reason"] == "subagent_limit_capped"` 将带 cap 原因的状态上报。

---

# 2. LoopDetectionMiddleware

## 概述

双层检测 + 双层响应 (warn / hard-stop) 的工具调用死循环守卫。Layer 1 基于工具调用集合的哈希，Layer 2 基于工具类型的滑动窗口频率。warn 阶段在下一次 model call 时注入 HumanMessage 提醒；hard-stop 阶段直接剥离 `tool_calls` 强制产出最终答案。

## 为什么需要这个中间件

### 场景痛点

Agent 可能陷入工具调用死循环：反复调用完全相同的工具集合（如连续多次 `read_file` + `bash`），或用不同的参数反复调用同一类工具（如连续 40 次 `read_file` 读不同文件却从不收敛到答案）。这类循环浪费大量 token 和模型配额，且用户等待时间被无限拉长。

### 为什么模型自身无法避免

LLM 在每一轮看到的只是当前上下文，它缺少对历史调用模式的整体视图。每次工具调用在模型看来都是合理的新步骤——上次调用没给出足够信息，所以再试一次。模型无法自我识别"我已经试过这个 5 次了，该换策略了"。

### 解决思路

双层检测互补：Layer 1 基于工具调用集合的顺序无关哈希检测精确重复，Layer 2 基于单工具类型的滑动窗口频率检测跨参数循环。先发警告，再硬停剥离 `tool_calls` 强制产出最终答案。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` |
| 实现的钩子 | `before_agent` / `abefore_agent`、`after_model` / `aafter_model`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call` |
| 持久化 | 内存 (跨 run 持久化，按 thread_id LRU 限 100，stop_reason 按 run_id 限 1000) |
| 配置依赖 | `LoopDetectionConfig`：`warn_threshold` (默认 3)、`hard_limit` (默认 5)、`window_size` (默认 20)、`max_tracked_threads` (默认 100)、`tool_freq_warn` (默认 30)、`tool_freq_hard_limit` (默认 50)、`tool_freq_overrides` (按工具名覆写) |

## 核心逻辑

### Layer 1：哈希检测 (相同调用集合)

`_hash_tool_calls` 对工具调用集合做"顺序无关"的哈希：

```python
def _hash_tool_calls(tool_calls: list[dict]) -> str:
    normalized: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args, fallback_key = _normalize_tool_call_args(tc.get("args", {}))
        key = _stable_tool_key(name, args, fallback_key)
        normalized.append(f"{name}:{key}")
    normalized.sort()  # 顺序无关
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]
```

### 工具感知哈希 (salient fields)

`_stable_tool_key` 针对不同工具采用不同策略，避免假阴性 / 假阳性：

- **`read_file`**：用 `path` + **200 行桶**分桶。`bucket = (line-1)//200`，使得读同一文件相邻区段被视为同一哈希——防止 Agent 翻同一文件不同页时被误判。代码：

```python
bucket_size = 200
bucket_start = (max(start_line, 1) - 1) // bucket_size
bucket_end = (max(end_line, 1) - 1) // bucket_size
return f"{path}:{bucket_start}-{bucket_end}"
```

- **`write_file` / `str_replace`**：内容敏感工具，相同 path 不同 payload 应视为不同调用。直接 hash 全 args：

```python
if name in {"write_file", "str_replace"}:
    if fallback_key is not None:
        return fallback_key
    return json.dumps(args, sort_keys=True, default=str)
```

- **其他工具**：抽取 salient 字段 (`path`、`url`、`query`、`command`、`pattern`、`glob`、`cmd`)：

```python
salient_fields = ("path", "url", "query", "command", "pattern", "glob", "cmd")
stable_args = {field: args[field] for field in salient_fields if args.get(field) is not None}
```

### Layer 2：工具类型频率检测 (跨文件循环)

Layer 1 只能抓"完全相同的调用集合"，但 Agent 可能用不同参数调用 `read_file` 40 次 (读 40 个不同文件)——这是真实死循环但 Layer 1 抓不到。Layer 2 用按工具名的滑动窗口计数补位。

**滑动窗口大小**的选取是一个关键不变量——必须不小于任何可能比较的 hard_limit，否则 hard-stop 分支永远不可达：

```python
self._tool_freq_window = max(
    self.window_size,
    self.tool_freq_hard_limit,
    *(hard for _, hard in self._tool_freq_overrides.values()),
)
```

**O(1) 频率计数**：用一个 `deque` + 镜像 `Counter`，避免每次调用都扫描整个窗口：

```python
self._tool_name_history: defaultdict[str, deque[str]] = defaultdict(deque)
self._tool_name_counter: defaultdict[str, Counter[str]] = defaultdict(Counter)

# 增量维护：
tool_name_history.append(name)
name_counter[name] += 1
while len(tool_name_history) > self._tool_freq_window:
    old = tool_name_history.popleft()
    c = name_counter[old] - 1
    if c <= 0:
        del name_counter[old]
    else:
        name_counter[old] = c
freq_count = name_counter.get(name, 0)
```

如果只有一个 per-tool override (例如 `bash: {hard_limit: 1000}`) 把全局窗口撑到 1000，每次扫描会让所有工具付出 1000 次开销；Counter 让增量在 append/popleft 时 O(1) 完成。

### 双层响应：warn 与 hard-stop

`_track_and_check` 返回 `(warning_message_or_none, should_hard_stop)`：

- `count >= hard_limit` → 返回 `(_HARD_STOP_MSG, True)`
- `count >= warn_threshold` 且该哈希未警告过 → 返回 `(_WARNING_MSG, False)`，并把 hash 加入 `_warned` 集合 (避免同一 hash 反复告警)
- Layer 2 同理：`freq_count >= eff_hard` → hard-stop；`>= eff_warn` 且未警告 → warn

### 延迟注入 (deferred injection)

警告**不能**在 `after_model` 直接插入消息，因为：

> `after_model` 紧跟在 `AIMessage(tool_calls=...)` 之后触发，但 tools node 还没运行，历史里还没有匹配的 `ToolMessage`。任何在这里插入的消息都会落在 assistant 的 `tool_calls` 与对应响应之间——OpenAI/Moonshot 校验器会以 `"tool_call_ids did not have response messages"` 拒绝下一次请求。

因此中间件采用**延迟注入**：`after_model` 把警告加入 `_pending_warnings` 队列，`wrap_model_call` 在下一次 model call 时取出并作为 `HumanMessage(name="loop_warning")` 追加到请求消息末尾。此时所有 `ToolMessage` 已就位，配对完整，且不修改任何已有 AIMessage。

### Hard-stop 时的消息改写

`_apply` 在 hard-stop 触发时剥离 `tool_calls` 并改写 `finish_reason`：

```python
content = self._append_text(last_msg.content, warning or _HARD_STOP_MSG)
stripped_msg = last_msg.model_copy(update=self._build_hard_stop_update(last_msg, content))
return {"messages": [stripped_msg]}
```

`_build_hard_stop_update` 清理 `tool_calls` / `function_call` 元数据，并把 `finish_reason: "tool_calls"` 改为 `"stop"`：

```python
response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
if response_metadata.get("finish_reason") == "tool_calls":
    response_metadata["finish_reason"] = "stop"
update["response_metadata"] = response_metadata
```

### Stop-reason 上报 (#3875 Phase 2)

hard-stop 不抛异常——剥离 `tool_calls` 后 run 自然终止并产出最终答案。为了让子 Agent 执行器能区分"被 cap 的完成"和"干净的完成"，hard-stop 触发时在两个地方写入 `loop_capped`：

```python
run_id = self._get_run_id(runtime)
with self._lock:
    self._stop_reason[run_id] = "loop_capped"
ctx = getattr(runtime, "context", None)
if isinstance(ctx, dict):
    ctx["stop_reason"] = "loop_capped"
```

`_stop_reason` 是 `BoundedDict(1000)`，防止 Lead 长期实例上累积无界条目。**关键**：`after_agent` / `_clear_current_run_pending_warnings` **故意不**清理 `_stop_reason`，以便子 Agent 执行器在 run 返回后通过 `consume_stop_reason(run_id)` 读取。只有 `reset()` 会清空。

### run_id 语义 ("按存在性而非真值")

`_get_run_id` 的实现是一个细节陷阱：

```python
def _get_run_id(self, runtime: Runtime) -> str:
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict) and "run_id" in ctx:
        return ctx["run_id"]
    return str(id(runtime))
```

注意：当 `ctx["run_id"] is None` 时也直接返回 `None` (而非回退到 `"default"`)。这是因为 `SubagentExecutor` 无条件写 `context["run_id"] = self.run_id`，嵌入式/TUI 分发的子 Agent 其 `run_id` 合法地为 `None` (键存在但值为 `None`)。执行器稍后用 `consume_stop_reason(self.run_id)` 读取——必须用完全一致的键值。如果用真值判断 (`if run_id:`) 会把 "存在但为 None" 与 "键缺失" 混淆，导致 hard-stop 写入 `"default"` 但读查询 `None`，静默丢失 `loop_capped`。

### LRU 与跨 run 清理

- `_history: OrderedDict[str, list[str]]` 按 thread_id LRU，超 `max_tracked_threads` 时淘汰最旧 thread。
- `before_agent` 清理同一 thread 其他 run 的待发警告 (`_clear_other_run_pending_warnings`)。
- `after_agent` 清理当前 run 的待发警告 (`_clear_current_run_pending_warnings`)。
- 每条 run 最多保留 4 条待发警告 (`_MAX_PENDING_WARNINGS_PER_RUN`)，溢出时丢弃最旧的。

## 状态机图

```
                  ┌──────────────────────────────┐
                  │ after_model: 读取最新 AIMessage│
                  │ 的 tool_calls                │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
            ┌─────────────────────────────────────────┐
            │ Layer 1: 哈希 call_hash 加入滑动窗口     │
            │ Layer 2: 工具名加入频率 deque + Counter │
            └────────────┬────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  Layer1 hard?      Layer1 warn?      Layer2 触发?
        │                │                │
        ▼                ▼                ▼
  写 stop_reason    入队 _WARNING     入队 freq warn
  = loop_capped     (一次)            (一次)
  剥 tool_calls        │                 │
  返回改写后 AIMsg     │                 │
        │              ▼                 ▼
        │     ┌──────────────────────────────┐
        │     │ _pending_warnings 队列       │
        │     └────────────┬─────────────────┘
        │                  │
        │                  ▼
        │     ┌──────────────────────────────────┐
        │     │ wrap_model_call: drain 队列      │
        │     │ → HumanMessage(name="loop_warning")│
        │     │ 追加到下次 model call 末尾       │
        │     └──────────────────────────────────┘
        ▼
  run 自然终止，最终答案输出
  executor 调 consume_stop_reason(run_id)
  → "loop_capped"
```

## 关键设计决策

1. **双层检测互补**：Layer 1 抓精确重复，Layer 2 抓类型频率。任何单层都会有盲区。
2. **延迟注入避免破坏 tool_call 配对**：这是与 OpenAI/Moonshot 校验器共存的必要条件。
3. **hard-stop 不抛异常**：让 run 自然产出最终答案，比起抛异常更易被消费者处理 (只是 `completed + loop_capped`)。
4. **工具感知 hash**：`read_file` 200 行桶防误判翻页，`write_file` 全 args hash 防漏判迭代写入。
5. **`_tool_freq_window` 的不变量**：必须 ≥ 任何 hard_limit，否则 hard-stop 不可达。代码注释明确禁止复用 `window_size` (默认 20 < hard 50)。
6. **Counter 镜像 deque**：避免 per-tool override 把窗口撑大后每次扫描付出 O(window) 代价。
7. **stop_reason 字典不随 `after_agent` 清理**：子 Agent 执行器在 run 返回后才读取，必须保留。靠 `BoundedDict(1000)` 防泄漏。
8. **`run_id` 按存在性判断**：避免 `run_id=None` 被混入 `"default"` 导致 stop_reason 丢失。

## 与其他中间件的协作

- **TokenBudgetMiddleware**：同形模式——同样用 `BoundedDict`、`consume_stop_reason`、`wrap_model_call` 延迟注入。两者一起构成子 Agent 的 guard cap 三轴中的 loop 轴与 token 轴。
- **SafetyFinishReasonMiddleware**：共享"剥离 tool_calls"的机制但触发条件不同。本中间件在 Lead 中间件链里注册在 Safety 之后——LangChain 反向 `after_model` 派发让 Safety 先看到原始响应，若 Safety 触发则先清理 tool_calls，Loop 再基于清理后的消息计数。
- **SubagentExecutor**：通过 `consume_stop_reason` (鸭子类型 `hasattr`) 收集每个 guard 的 cap 原因，取第一个非 None 作为该 run 的 stop_reason 上报给 Lead。
- **ToolProgressMiddleware**：分工明确——ToolProgress 是"结果质量守卫"(工具停止产出新信息时阻塞该工具)；LoopDetection 是"调用模式守卫"(模型反复发出相同 tool_calls 时硬停整个 turn)。两者可在同一次 model call 里各注入一条 HumanMessage 而互不冲突。

---

# 3. TokenBudgetMiddleware

## 概述

按 run 隔离的 token 预算守卫。累计每次 model call 的 `usage_metadata`，触发 warn 阈值时延迟注入提醒，触发 hard-stop 阈值时剥离 `tool_calls` 强制收尾，并通过 `consume_stop_reason` 上报 `token_capped`。

## 为什么需要这个中间件

### 场景痛点

一个 run 可能消耗不可控的 token 数量——特别是当子 Agent 的 token 用量回溯合并到父 run 的消息历史后，总 token 消耗可能远超预期，导致成本激增和上下文膨胀。如果只有总 token 限制，输入膨胀（上下文窗口撑满）和输出膨胀（模型生成超长回复）也无法被分别管控。

### 为什么模型自身无法避免

LLM 没有任何内在的 token 预算概念。它不会在生成过程中累计已消耗的 token 并自我约束——它会一直生成直到模型认为任务完成或上下文窗口耗尽。模型也无法感知子 Agent 消耗的 token 应该计入当前 run 的预算。

### 解决思路

按 run 隔离的差值追踪累计 token（正确处理子 Agent 回溯合并），计算 input / output / total 三维度分数，任一维度超阈值即触发 warn 或 hard-stop。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/token_budget_middleware.py` |
| 实现的钩子 | `before_agent` / `abefore_agent`、`after_model` / `aafter_model`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call` |
| 持久化 | 内存 (按 run_id 隔离的 `BoundedDict(1000)`) |
| 配置依赖 | `TokenBudgetConfig`：`enabled`、`max_tokens`、`max_input_tokens`、`max_output_tokens`、`warn_threshold`、`hard_stop_threshold` |

## 核心逻辑

### 差值追踪 (diff-based accounting)

关键算法：每条 `AIMessage` 在 `before_agent` 时被记入 `_seen_messages[run_id][msg.id] = (input, output)`。`_apply` 遍历历史时比较当前 `usage_metadata` 与已记录值的**差值**，只把新增的 token 累加到 `usage_accum`：

```python
for msg in messages:
    if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
        usage = msg.usage_metadata or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        prev_input, prev_output = seen.get(msg.id, (0, 0))
        diff_input = max(0, input_tokens - prev_input)
        diff_output = max(0, output_tokens - prev_output)
        if diff_input > 0 or diff_output > 0:
            usage_accum.input += diff_input
            usage_accum.output += diff_output
            usage_accum.total += diff_input + diff_output
            seen[msg.id] = (input_tokens, output_tokens)
```

### 子 Agent 回溯归因

为什么用差值而不是简单求和？源码注释明确说明：

> This automatically captures tokens from subagents because `TokenUsageMiddleware` retroactively adds them to the history.

子 Agent 的 token 用量由 `TokenUsageMiddleware` 在子 Agent 完成后**回溯合并**到分派它的那条 AIMessage 的 `usage_metadata` 里。差值追踪能在合并发生时把增量正确归因到当前 run 的预算——既不会漏算子 Agent 的 token (合并前那条 AIMessage 的 usage 为 0)，也不会重复计算 (合并后再来一次时 diff 为 0)。

### 三维度分数

`_apply` 计算三个维度的分数并取最高者作为触发依据：

```python
fractions = [("total", usage_accum.total, self._config.max_tokens)]
if self._config.max_input_tokens:
    fractions.append(("input", usage_accum.input, self._config.max_input_tokens))
if self._config.max_output_tokens:
    fractions.append(("output", usage_accum.output, self._config.max_output_tokens))

highest_fraction = 0.0
trigger_reason = ""
for reason, used, limit in fractions:
    frac = used / limit
    if frac > highest_fraction:
        highest_fraction = frac
        trigger_reason = reason
        trigger_used = used
        trigger_budget = limit
```

- **total**：总 token 数，始终参与比较
- **input**：可选，限制 prompt 端 token (主要约束上下文膨胀)
- **output**：可选，限制生成端 token (主要约束长输出)

任一维度超 `hard_stop_threshold` 即触发硬停，并在 `_BUDGET_EXCEEDED_MSG` 中指明是哪个维度超了：

```python
_BUDGET_EXCEEDED_MSG = "[TOKEN BUDGET EXCEEDED] The {reason} token usage ({used:,}) has exceeded the safety limit ({budget:,}). Producing final answer with results collected so far."
```

### Hard-stop 改写

`_build_hard_stop_update` 做的事与 LoopDetection 类似：剥离 `tool_calls` 与 `function_call`，把 `finish_reason: "tool_calls"` 改为 `"stop"`，并追加 stop 文本：

```python
stopped_msg = msg.model_copy(update={
    "content": updated_content,
    "tool_calls": [],
    "additional_kwargs": kwargs,
    "response_metadata": response_metadata,
})
return {"messages": [stopped_msg]}
```

### Warn 的延迟注入与去重

`_warned[run_id]` 是布尔标记——一个 run 只发一次 warn 提醒，避免反复打搅模型：

```python
if highest_fraction >= self._config.warn_threshold and not self._warned.get(run_id, False):
    self._warned[run_id] = True
    # ...
    warnings = self._pending_warnings.setdefault(run_id, [])
    warnings.append(warn_text)
    return None
```

`wrap_model_call` 取出并合并为一条 `HumanMessage(name="budget_warning")` 注入到下次 model call。

### run 生命周期清理

- `before_agent`：把当前 thread 历史中**所有先前 run 的 AIMessage** 标记为 "seen"——它们不算入本次 run 的预算。这就是"per-run 预算"的语义。
- `after_agent`：通过 `_clear_run_state(run_id)` 清理 `_warned` / `_pending_warnings` / `_seen_messages` / `_cumulative_usage`。**不清理 `_stop_reason`**——executor 要在 run 返回后读取。
- 每个 run 用全新中间件实例时无跨 run 污染；Lead 长期实例靠 `BoundedDict(1000)` 防泄漏。

## 状态机图

```
   ┌────────────────────────────────────────┐
   │ before_agent: 把历史 AIMessage 全部    │
   │ 标记为 seen (不算入本 run 预算)        │
   └─────────────────┬──────────────────────┘
                     │
                     ▼
          ┌──────────────────────┐
          │ model call 产出 AI   │
          │ Message              │
          └──────────┬───────────┘
                     │
                     ▼
        ┌──────────────────────────────┐
        │ after_model._apply:          │
        │ 遍历历史，按差值累加 token    │
        │ 算 3 维度分数取最高          │
        └──────────────┬───────────────┘
                       │
       ┌───────────────┼───────────────┐
       ▼               ▼               ▼
   < warn_th       ≥ warn_th        ≥ hard_stop_th
   (无操作)        且未警告过        │
                  │                 │
                  ▼                 ▼
       _warned[run_id]=True   写 _stop_reason[run_id]
       入队 budget_warning     = "token_capped"
                              写 ctx["stop_reason"]
                              剥 tool_calls + 追加 stop 文本
                              返回改写后 AIMessage
                  │                 │
                  ▼                 ▼
       ┌──────────────────────┐    │
       │ wrap_model_call:      │    │
       │ drain → HumanMessage  │    │
       │ (name="budget_warning")│   │
       │ 追加到下次 model call  │    │
       └──────────────────────┘    │
                                  ▼
                          run 自然终止
                          executor 读 stop_reason
                          → "token_capped"
                          
   ┌────────────────────────────────────────┐
   │ after_agent: _clear_run_state 清理     │
   │ _warned / _pending_warnings /           │
   │ _seen_messages / _cumulative_usage      │
   │ **不清理** _stop_reason                │
   └────────────────────────────────────────┘
```

## 关键设计决策

1. **差值追踪**：正确处理子 Agent token 的回溯合并——求和会重复计算，差值只在真正新增时累加。
2. **per-run 隔离**：`before_agent` 把历史 AIMessage 标为 seen，使每个 run 从 0 开始预算。同一 thread 的下一 turn 重新获得完整预算。
3. **三维度独立触发**：input/output/total 任一超阈值即触发，便于针对不同失效模式 (上下文膨胀 vs 长输出) 分别配置。
4. **warn 一次性**：避免反复打扰模型，warn 只在每个 run 触发一次。
5. **hard-stop 不抛异常**：与 LoopDetection 一致，让 run 自然终止。
6. **`_stop_reason` 跨 `after_agent` 保留**：子 Agent executor 在 run 返回后才读，必须保留；靠 `BoundedDict(1000)` 防泄漏。
7. **`run_id` 同样按存在性判断**：与 LoopDetectionMiddleware 一致，正确处理嵌入式 `run_id=None`。

## 与其他中间件的协作

- **TokenUsageMiddleware**：把子 Agent token 回溯合并到 AIMessage 的 `usage_metadata`，本中间件靠差值追踪正确归因。
- **LoopDetectionMiddleware**：同形实现 (BoundedDict、consume_stop_reason、wrap_model_call 延迟注入)，两者共同构成 guard cap 三轴中的 token 轴与 loop 轴。
- **SubagentExecutor**：通过 `consume_stop_reason` 鸭子类型接口收集，取第一个非 None 原因。子 Agent 的 token_budget 默认与 `summarization.enabled` 耦合 (启用时 1M，关闭时 2M)。
- **DurableContextMiddleware (子 Agent 路径)**：子 Agent 链中 summarization 在 guard 三轴之前，而 Lead 链是 summarization 在 guard 之后——良性，因为 summarization 只实现 `before_model` (compaction)，不会干扰 `after_model` / `consume_stop_reason` 通道。

---

# 4. TerminalResponseMiddleware

## 概述

当模型在工具执行后返回空 AIMessage (无可见文本、无 tool_calls) 时，注入隐藏 recovery prompt 重试一次模型；若再次返回空，则在 checkpoint 状态里用可见错误 fallback 替换，使 run 以错误而非"静默成功"结束。

## 为什么需要这个中间件

### 场景痛点

工具执行完成后，模型可能返回一个空 AIMessage——没有可见文本、没有 tool_calls。如果放任不管，run 会以"静默成功"结束，用户看不到任何输出，也不知道发生了什么。这种情况在某些 provider 或模型配置下并非罕见。

### 为什么模型自身无法避免

LLM 在收到工具返回结果后，偶尔会在合成最终响应时"卡住"——特别是当工具返回了大量数据或结果出乎模型预期时。模型自身没有自我纠正或重试机制，一旦产出了空消息，它不会自动重试。

### 解决思路

检测空响应后，删除空消息并通过 `jump_to: "model"` 配合隐藏 recovery prompt 重试一次；若再次为空，则写入可见的错误 fallback 文本并标记为失败完成。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/terminal_response_middleware.py` |
| 实现的钩子 | `before_agent` / `abefore_agent`、`after_model` / `aafter_model` (带 `@hook_config(can_jump_to=["model"])`)、`wrap_model_call` / `awrap_model_call`、`after_agent` / `aafter_agent` |
| 持久化 | 内存 (`BoundedDict(1000)` 按 (thread_id, run_id) 键) |
| 配置依赖 | 无显式配置；依赖 `Runtime.context.thread_id` / `run_id` / `run_attempt_id` |

## 核心逻辑

### 触发条件

`_apply` 的触发需要同时满足：

1. 最后一条消息是 `AIMessage`；
2. **无可见文本**：`_has_visible_content(last)` 返回 False (空字符串或空 content block 列表)；
3. **无 tool_call 意图或错误**：`_has_tool_call_intent_or_error(last)` 返回 False——避免干预正常 tool 路由或 malformed tool call 处理；
4. **当前 turn 有工具结果**：`_tool_result_in_current_turn(messages)` 返回 True——最近的"真实"HumanMessage 之后存在 ToolMessage。

### 真实用户消息界定

`_tool_result_in_current_turn` 只看"真实"用户消息之后的 ToolMessage，跳过带 `hide_from_ui` 标记的 HumanMessage：

```python
def _tool_result_in_current_turn(messages: list[Any]) -> bool:
    latest_user_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        if (message.additional_kwargs or {}).get("hide_from_ui"):
            continue
        latest_user_index = index
    if latest_user_index == -1:
        return False
    return any(isinstance(message, ToolMessage) for message in messages[latest_user_index + 1 :])
```

注释说明：#4027 仅覆盖交互式 post-tool turn；调度/内部调用没有真实 HumanMessage 时需要单独的 terminal-success 不变量，而非从任意历史 tool 推断。

### 一次重试 + jump_to model

第一次空响应时，中间件**删除**这条空 AIMessage 并 `jump_to: "model"` 让图重新调度模型节点：

```python
if retry_count == 0:
    self._retry_counts[key] = 1
    self._pending_prompts[key] = True
    message_updates = [RemoveMessage(id=last.id)] if last.id else []
    return {"messages": message_updates, "jump_to": "model"}
```

关键细节：

- **删除原消息**：下一次 model call 会获得新的 message id，提前 Remove 这条空消息，避免成功恢复后在 checkpoint 历史和未来 model 上下文里残留。
- **`jump_to: "model"`**：`@hook_config(can_jump_to=["model"])` 声明允许 `after_model` 钩子把控制权交回 model 节点，绕过默认的 tools → model 流。
- **预算按 run 而非按消息**：`retry_count` 是 per-(thread, run) 一次性预算，重试里如果又调用工具、又返回空，不会刷新预算，避免 empty → retry → tool 无限循环。

### wrap_model_call 注入 recovery prompt

`_pending_prompts[key]` 在下一次 model call 时被消费，追加一条隐藏的 `HumanMessage`：

```python
def _augment_request(self, request: ModelRequest) -> ModelRequest:
    key = self._key(request.runtime)
    with self._lock:
        pending = key in self._pending_prompts
        self._pending_prompts.pop(key, None)
    if not pending:
        return request
    reminder = HumanMessage(
        content=_RECOVERY_PROMPT,
        name="terminal_response_recovery",
        additional_kwargs={"hide_from_ui": True},
    )
    return request.override(messages=[*request.messages, reminder])
```

`_RECOVERY_PROMPT` 是 `<system_reminder>` 包裹的提醒文本，指导模型基于已有 tool 结果产出简洁的最终响应，不要再调工具。

### 第二次空响应 → 可见 fallback

如果重试后仍为空响应，直接在 checkpoint 里用 fallback 文本替换并打标：

```python
additional_kwargs.update({
    "deerflow_error_fallback": True,
    "error_reason": "Model returned an empty terminal response after one retry",
})
fallback = last.model_copy(update={
    "content": _FALLBACK_CONTENT,
    "additional_kwargs": additional_kwargs,
})
return {"messages": [fallback]}
```

`_FALLBACK_CONTENT` 是面向用户的错误文案，`deerflow_error_fallback=True` 标记会被 Run Worker / `LLMErrorHandlingMiddleware` / 子 Agent `SubagentExecutor` 识别为失败完成 (参考 `SubagentExecutor` 的"只有 marker 是权威的——没有 marker 的错误外观文本仍算正常完成"规则)。

### 清理时机

- `before_agent`：`_clear_other_runs` 清同 thread 其他 run 的残留；`_clear` 清当前 run 的预算——因为"上一次调用可通过 `Command(goto=END)` 绕过 `after_agent`，resume 时要让本 run 重新获得一次重试预算"。注意 `jump_to=model` 的内部循环不会重新触发 `before_agent`。
- `after_agent`：`_clear` 清当前 run。

## 状态机图

```
       ┌──────────────────────────────────────┐
       │ after_model: 最后一条消息是空 AIMessage│
       │ (无可见文本 + 无 tool_calls + 当前     │
       │ turn 有 ToolMessage)                  │
       └────────────────┬─────────────────────┘
                        │
                        ▼
           ┌──────────────────────────┐
           │ retry_count == 0?        │
           └──────┬──────────┬───────┘
                 是          否
                  │           │
                  ▼           ▼
       _retry_counts=1   写 deerflow_error_fallback=True
       _pending_prompts  error_reason=...
         =True           用 _FALLBACK_CONTENT 替换消息
       RemoveMessage(id)  返回 {"messages":[fallback]}
       返回 {jump_to:"model"}
                  │           │
                  ▼           ▼
       ┌────────────────────┐  checkpoint 持久化错误
       │ wrap_model_call:    │  run 以错误结束
       │ 注入 _RECOVERY_PROMPT│
       │ (hide_from_ui=True) │
       │ 作为 HumanMessage   │
       └────────────────────┘
                  │
                  ▼
          重新调度 model 节点
                  │
                  ▼
       ┌──────────────────────┐
       │ model 再次产出响应    │
       └──────┬───────────────┘
              │
              ▼
     有可见文本或 tool_call? ── 是 ──▶ 正常继续
              │ 否
              ▼
       retry_count == 1, 进入 fallback 分支
```

## 关键设计决策

1. **一次重试，不是无限重试**：单次 run 一次性预算，避免 empty → retry → tool → empty 的无限循环。
2. **删除空消息再 jump**：避免空 AIMessage 在 checkpoint 历史和未来上下文里污染。
3. **`jump_to: "model"` 而非抛异常**：让图自然重新调度 model 节点，与 LangGraph 的控制流语义对齐。
4. **`hide_from_ui` 的 recovery prompt**：避免提醒文本出现在用户 UI 里，它只是给模型的内部指引。
5. **第二次失败时 marker + 可见文案**：让 Run Worker 能识别为失败完成；让用户看到明确的错误说明而不是"静默成功"。
6. **`before_agent` 重置本 run 预算**：处理 `Command(goto=END)` 绕过 `after_agent` 的边界情况，resume 时有全新的一次重试预算。
7. **`_has_tool_call_intent_or_error` 排除**：避免干预正常 tool 路由或 malformed tool_call 处理，那些有专门中间件 (DanglingToolCallMiddleware / ToolErrorHandlingMiddleware) 负责。
8. **`_tool_result_in_current_turn` 限定**：只在交互式 post-tool turn 干预；调度/内部调用无真实 HumanMessage 时不干預。

## 与其他中间件的协作

- **DanglingToolCallMiddleware**：处理"AIMessage 有 tool_calls 但缺 ToolMessage"的情况；本中间件处理"AIMessage 完全空"的情况，互不重叠。
- **ToolErrorHandlingMiddleware**：把工具异常转为 error ToolMessage，让模型有机会在下一轮产出最终响应；若模型仍产出空响应则由本中间件兜底。
- **LLMErrorHandlingMiddleware**：把 provider/model 异常转为带 `deerflow_error_fallback` 标记的 AIMessage；本中间件在"无异常但空响应"时也写同样的 marker——下游消费者 (SubagentExecutor / Worker) 用同一标记判断失败完成。
- **SubagentExecutor**：通过检查最后一条 AIMessage 的 `deerflow_error_fallback` 标记把子 Agent 映射为 `SubagentStatus.FAILED`。
- **SafetyFinishReasonMiddleware**：注册在本中间件之后 (LangChain 反向派发 → Safety 先看)，若 Safety 触发剥离 tool_calls 后消息仍可能有可见文本，本中间件不再干预；若 Safety 触发后 content 也为空，理论上本中间件可能在下一轮生效。

---

# 5. SafetyFinishReasonMiddleware

## 概述

当 provider 因安全原因中途截断响应 (如 OpenAI `content_filter`、Anthropic `refusal`、Gemini `SAFETY`) 但仍返回半截 `tool_calls` 时，剥离这些不安全的 tool_calls，追加面向用户的解释，并通过 SSE 事件和 RunJournal 审计事件通知消费者。

## 为什么需要这个中间件

### 场景痛点

OpenAI、Anthropic、Gemini 等 provider 的安全过滤器可能在模型生成中途截断响应，但截断后的响应仍可能携带半截 `tool_calls`。让这些被安全机制拒绝的 tool_calls 继续执行可能导致危险的后果——例如在安全审查触发后的敏感上下文中继续执行工具操作。

### 为什么模型自身无法避免

安全终止信号来自 provider 层面，而非模型自身的输出。模型不知道自己的响应被截断了——它产出的 tool_calls 在模型看来是正常输出。模型无法区分"正常完成"和"被安全过滤截断"。

### 解决思路

在 `after_model` 中检测 provider 的安全终止信号（`content_filter` / `refusal` / `SAFETY` 等），剥离所有 tool_calls，追加面向用户的解释，保留原始 finish_reason 供下游消费，并通过 SSE 和 RunJournal 发出审计事件。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/safety_finish_reason_middleware.py` |
| 实现的钩子 | `after_model` / `aafter_model` |
| 持久化 | 无自身状态；改写 AIMessage 并写 `additional_kwargs.safety_termination` 审计字段、`runtime.context.stop_reason`、SSE 事件、RunJournal 记录 |
| 配置依赖 | `SafetyFinishReasonConfig` (可选 `detectors` 列表)；默认三个内置检测器 |

## 核心逻辑

### 三种内置检测器

定义在 `safety_termination_detectors.py`：

1. **`OpenAICompatibleContentFilterDetector`**：检测 `finish_reason in {"content_filter", ...}`。覆盖 OpenAI、Azure OpenAI、Moonshot/Kimi、DeepSeek、Mistral、vLLM、Qwen (OpenAI 兼容模式)。Azure 的 `content_filter_results` 结构化块被带入 `extras` 供操作员查看。
2. **`AnthropicRefusalDetector`**：检测 `stop_reason == "refusal"` (Anthropic 用 `stop_reason` 而非 `finish_reason`)。
3. **`GeminiSafetyDetector`**：检测 Gemini 大写枚举 `finish_reason`：`SAFETY`、`BLOCKLIST`、`PROHIBITED_CONTENT`、`SPII`、`RECITATION`、`IMAGE_SAFETY` 等。`safety_ratings` 带入 `extras`。**故意排除** `STOP`、`MAX_TOKENS`、`MALFORMED_FUNCTION_CALL` 等——它们不是安全过滤。

### 检测流程

`_detect` 顺序调用每个检测器，第一个返回非 None 结果即采用。检测器异常被捕获并记为 no-match，**绝不让 buggy 检测器中断 agent run**：

```python
def _detect(self, message: AIMessage) -> SafetyTermination | None:
    for detector in self._detectors:
        try:
            hit = detector.detect(message)
        except Exception:  # noqa: BLE001
            logger.exception("SafetyTerminationDetector %r raised; treating as no-match",
                             getattr(detector, "name", type(detector).__name__))
            continue
        if hit is not None:
            return hit
    return None
```

### 触发条件

`_apply` 只在"有 tool_calls 可抑制"时干预——`content_filter` 不带 tool_calls 的情况放行让部分文本响应自然到达用户：

```python
tool_calls = last.tool_calls
if not tool_calls:
    return None

termination = self._detect(last)
if termination is None:
    return None
```

### 消息改写：剥离 tool_calls 但保留 finish_reason

`_build_suppressed_message` 用 `clone_ai_message_with_tool_calls(message, [], content=new_content)` 一次性同步清理结构化 `tool_calls` 与 raw provider payload，**但保留原 `finish_reason`**——`content_filter` / `refusal` / `SAFETY` 原值不动，让下游 SSE / 转换器继续看到真实的 provider 原因：

```python
cleared = clone_ai_message_with_tool_calls(message, [], content=new_content)

kwargs = dict(getattr(cleared, "additional_kwargs", None) or {})
kwargs["safety_termination"] = {
    "detector": termination.detector,
    "reason_field": termination.reason_field,
    "reason_value": termination.reason_value,
    "suppressed_tool_call_count": len(suppressed_names),
    "suppressed_tool_call_names": suppressed_names,
    "extras": dict(termination.extras) if termination.extras else {},
}
return cleared.model_copy(update={"additional_kwargs": kwargs})
```

### 三种通知通道

#### 1. runtime.context stop_reason

```python
ctx = getattr(runtime, "context", None)
if isinstance(ctx, dict):
    ctx["stop_reason"] = "safety_capped"
```

供 Lead Worker 读取 (#4176)，与 `loop_capped` / `token_capped` / `subagent_limit_capped` 同形机制。

#### 2. SSE 事件 (`_emit_event`)

通过 `langgraph.config.get_stream_writer()` 发送自定义事件，让 Web UI 等 SSE 消费者能对已流出的 "tool starting..." 占位符做对账：

```python
writer({
    "type": "safety_termination",
    "detector": termination.detector,
    "reason_field": termination.reason_field,
    "reason_value": termination.reason_value,
    "suppressed_tool_call_count": len(suppressed_names),
    "suppressed_tool_call_names": suppressed_names,
    "thread_id": thread_id,
})
```

`get_stream_writer` 不可用时静默跳过 (debug 级日志)，这是 best-effort 信号。

#### 3. RunJournal 审计事件 (`_record_audit_event`)

持久化一条 `middleware:safety_termination` 记录到 `RunEventStore`，让操作员能事后用 SQL 查询"今天哪些 run 被安全抑制了"——无需 join 消息体。Worker 通过 `runtime.context["__run_journal"]` 暴露 run 级 `RunJournal`；子 Agent / 单测 / 无 event-store 路径下 silently skip。

**关键**：工具**参数**故意**不记录**——那正是 provider 过滤的内容；持久化它们就违背了安全过滤的初衷。names / count / ids 足够审计和调试。

```python
changes = {
    "detector": termination.detector,
    "reason_field": termination.reason_field,
    "reason_value": termination.reason_value,
    "suppressed_tool_call_count": len(tool_calls),
    "suppressed_tool_call_names": suppressed_names,
    "suppressed_tool_call_ids": suppressed_ids,
    "message_id": getattr(message, "id", None),
    "extras": dict(termination.extras) if termination.extras else {},
}
journal.record_middleware(
    tag="safety_termination",
    name=type(self).__name__,
    hook="after_model",
    action="suppress_tool_calls",
    changes=changes,
)
```

## 状态机图

```
   ┌──────────────────────────────────────┐
   │ after_model: AIMessage 到达          │
   └────────────────┬─────────────────────┘
                    │
                    ▼
       ┌──────────────────────────┐
       │ 有 tool_calls?            │── 否 ──▶ 放行 (部分文本自然到达用户)
       └──────────┬───────────────┘
                  │ 是
                  ▼
       ┌──────────────────────────┐
       │ _detect: 任一检测器命中? │── 否 ──▶ 不干预
       └──────────┬───────────────┘
                  │ 是
                  ▼
       写 ctx["stop_reason"]="safety_capped"
                  │
                  ▼
       _build_suppressed_message:
         - 追加 _USER_FACING_MESSAGE 到 content
         - 剥离所有 tool_calls (结构化 + raw)
         - 保留原 finish_reason (content_filter/refusal/SAFETY)
         - 写 additional_kwargs.safety_termination 审计字段
                  │
                  ▼
       _emit_event: SSE 自定义事件 (best-effort)
       _record_audit_event: RunJournal 持久化 (无参数)
                  │
                  ▼
       logger.warning + 返回 {"messages":[patched]}
```

## 关键设计决策

1. **`after_model` 而非 `wrap_model_call`**：因为这是一个**正常返回** (不是异常)，且要与 `LoopDetectionMiddleware` 共享同一 after-model 链和相同的 tool-call 抑制机制，只是触发条件不同。
2. **注册顺序：在 LoopDetectionMiddleware 之后**：LangChain 工厂按反向列表顺序连接 `after_model` 边，最后注册的最先观察模型输出。Safety 在 Loop 之后注册 = Safety 先看到原始响应；若 Safety 触发清理 tool_calls，Loop 再基于清理后的消息计数，避免 Loop 把已被 Safety 抑制的 tool_calls 误计入死循环计数。
3. **保留 `finish_reason`**：`clone_ai_message_with_tool_calls` 只在原值是 `"tool_calls"` 时改写为 `"stop"`——`content_filter` / `refusal` / `SAFETY` 原值保留，下游 SSE / 转换器继续看到真实的 provider 原因。
4. **不记录工具参数**：那正是 provider 过滤的内容；持久化它们违背安全过滤初衷。names / count / ids 足够审计。
5. **best-effort SSE**：`get_stream_writer` 不可用时静默跳过，不让可选的可观测性通道中断核心 run。
6. **检测器异常不中断 run**：每个检测器包 try/except，buggy 检测器记为 no-match 而非抛出。
7. **`from_config` 拒绝空 `detectors` 列表**：显式空列表会静默禁用检测但保留中间件在链中，是最坏组合。用 `enabled: false` 完全禁用而非空列表。
8. **可扩展性**：新 provider (文心、混元、Bedlock、自建网关) 通过实现 `SafetyTerminationDetector` 协议并经 `config.yaml: safety_finish_reason.detectors` 配置即可接入，无需改中间件代码。

## 与其他中间件的协作

- **LoopDetectionMiddleware**：同形抑 tool_calls 机制，共享 after-model 链。注册顺序确保 Safety 先看、先清理；Loop 基于清理后消息计数。
- **SubagentLimitMiddleware**：同写 `runtime.context["stop_reason"]` 机制 (safety_capped / subagent_limit_capped / loop_capped / token_capped)，让 Lead Worker 统一读取。
- **TerminalResponseMiddleware**：注册在本中间件之前——若 Safety 剥离 tool_calls 后 content 仍有可见文本，TerminalResponse 不干预；若 content 也为空，下一轮 TerminalResponse 可能兜底。
- **RunJournal (`__run_journal`)**：通过 `record_middleware` 持久化审计事件，供 `GET /api/threads/{id}/runs/{rid}/events` 等接口查询。

---

# 6. ClarificationMiddleware

## 概述

拦截 `ask_clarification` 工具调用，把模型想要问用户的问题格式化为可读 `ToolMessage` + 结构化 `human_input` payload，通过 `Command(goto=END)` 中断执行流，等待用户回复。支持在非交互渠道 (GitHub webhooks 等) 抑制澄清并指示 Agent 自主判断。

## 为什么需要这个中间件

### 场景痛点

Agent 需要向用户提问以澄清意图或获取缺失信息，但如果不打断执行流程，Agent 会在没有完整信息的情况下继续操作，得出错误结论。如果直接抛异常，又会导致整个 run 失败。对于非交互渠道（如 GitHub webhooks），人类无法实时回复，澄清调用会让 run 死锁。

### 为什么模型自身无法避免

LLM 没有自我中断的能力——它在一次生成中无法"暂停并等待用户回复"。模型能做的只是生成一条回复问用户，但无法控制后续流程不继续执行工具调用。同时，模型也无法判断当前是否处于非交互环境。

### 解决思路

在 `wrap_tool_call` 中拦截 `ask_clarification` 调用，格式化为可读消息 + 结构化 `human_input` payload，通过 `Command(goto=END)` 中断图执行等待用户回复；非交互渠道下返回普通 ToolMessage 让 Agent 自主判断继续。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/clarification_middleware.py` |
| 实现的钩子 | `wrap_tool_call` / `awrap_tool_call` |
| 持久化 | 无自身状态；写入 ToolMessage + artifact payload；通过 `Command(goto=END)` 终止图 |
| 配置依赖 | `Runtime.context.disable_clarification` (由非交互渠道设置)；必须最后注册 |

## 核心逻辑

### 拦截路径

`wrap_tool_call` / `awrap_tool_call` 检查工具名，非 `ask_clarification` 直接透传给 handler：

```python
def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[...]) -> ToolMessage | Command:
    if request.tool_call.get("name") != "ask_clarification":
        return handler(request)

    if self._is_disabled(request):
        return self._handle_disabled_clarification(request)

    return self._handle_clarification(request)
```

### 确定性消息 ID (重试不追加)

`_stable_message_id` 基于 `tool_call_id` 或 formatted_message 的 sha256 前缀，让重试的澄清调用**替换**而非**追加**：

```python
def _stable_message_id(self, tool_call_id: str, formatted_message: str) -> str:
    if tool_call_id:
        return f"clarification:{tool_call_id}"
    digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
    return f"clarification:{digest}"
```

### human_input payload 构建

`_build_human_input_payload` 构造结构化 payload，供前端 Human Input Card 渲染：

```python
payload: dict[str, Any] = {
    "version": 1,
    "kind": "human_input_request",
    "source": "ask_clarification",
    "request_id": request_id,
    "clarification_type": clarification_type,
    "question": str(args.get("question") or ""),
    "input_mode": "choice_with_other" if options else "free_text",
}
if tool_call_id:
    payload["tool_call_id"] = tool_call_id
if "context" in args:
    payload["context"] = None if context is None else str(context)
if options:
    payload["options"] = [
        {"id": f"option-{index}", "label": option, "value": option}
        for index, option in enumerate(options, 1)
    ]
```

- `input_mode`: 有 options 时是 `choice_with_other` (选项 + "其他")，无 options 是 `free_text`。
- `clarification_type`: 从 args 读取，默认 `missing_info`。

### options 归一化

`_normalize_options` 处理某些模型 (如 Qwen3-Max) 把数组参数序列化为 JSON 字符串的情况：

```python
def _normalize_options(self, raw_options: Any) -> list[str]:
    options = raw_options
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except (json.JSONDecodeError, TypeError):
            options = [options]
    if options is None:
        return []
    if not isinstance(options, list):
        options = [options]
    return [str(option) for option in options]
```

### 格式化可读消息

`_format_clarification_message` 根据 `clarification_type` 选 emoji 图标 (❓/🤔/🔀/⚠️/💡)，把 context / question / options 拼成多行可读文本。中英文都支持。

### Command(goto=END) 中断

`_handle_clarification` 返回 `Command(update={"messages": [tool_message]}, goto=END)`——这是 LangGraph 的中断原语，让图停在 END 节点，checkpoint 持久化，等待 resume：

```python
tool_message = ToolMessage(
    id=request_id,
    content=formatted_message,
    tool_call_id=tool_call_id,
    name="ask_clarification",
    artifact={"human_input": human_input_payload},
)
return Command(
    update={"messages": [tool_message]},
    goto=END,
)
```

关键点：
- **不追加额外 AIMessage**——前端直接检测并展示 `ask_clarification` 工具消息。
- **`artifact.human_input`**: 结构化 payload 作为 ToolMessage 的 artifact 字段，前端读取它渲染 Human Input Card；`ToolMessage.content` 是纯文本 fallback。
- **确定性 ID**：重试同一澄清调用 (相同 `tool_call_id`) 会用相同 `id`，LangGraph reducer 视为替换而非追加。

### 非交互渠道的抑制

`_is_disabled` 检查 `runtime.context.disable_clarification`，非交互渠道 (如 GitHub webhooks) 设置此 flag 因为澄清会让 run 死锁——人类只能通过后续 webhook 投递"回复"，而那时 agent 的 turn 早已结束：

```python
def _is_disabled(self, request: ToolCallRequest) -> bool:
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    if not context:
        return False
    return bool(context.get("disable_clarification"))
```

`_handle_disabled_clarification` 返回普通 `ToolMessage` (而非 `Command(goto=END)`)，让 agent 循环继续——agent 收到 tool 结果 "Clarification is disabled... Proceed with your best judgment..." 后再生成，理想情况下直接行动而非再问：

```python
return ToolMessage(
    id=self._stable_message_id(tool_call_id, "proceed-without-clarification"),
    content=(
        "Clarification is disabled in this context — the human is not present "
        "to answer synchronously. Do not ask for confirmation. Proceed with your "
        "best judgment, carry out the requested action, and state any assumptions "
        "you made in your final response."
    ),
    tool_call_id=tool_call_id,
    name="ask_clarification",
)
```

### scheduled-task 上下文

AGENTS.md 指出：scheduled background runs 设 `context.non_interactive=true`，Lead-agent 工具集**排除** `ask_clarification`——即调度触发的 run 根本不会绑定此工具。`non_interactive` 是 internal-only context key，只对 process-internal user (scheduler path) 合并，绝不从任意 HTTP/IM 客户端接受。

## 状态机图

```
            ┌──────────────────────────────────┐
            │ wrap_tool_call: 收到 ToolCallReq │
            └──────────────┬───────────────────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ name == "ask_clarification"?│── 否 ──▶ handler(request) 正常执行
              └──────────┬─────────────┘
                         │ 是
                         ▼
              ┌──────────────────────────┐
              │ disable_clarification set?│
              └──────┬──────────┬───────┘
                    是          否
                     │           │
                     ▼           ▼
       返回 ToolMessage       _handle_clarification:
       (content="...disabled       - 格式化可读 message
       proceed...")               - 构造 human_input payload
       agent 循环继续              - ToolMessage(artifact={"human_input":...})
                                 - 返回 Command(goto=END)
                                       │
                                       ▼
                            图终止于 END 节点
                            checkpoint 持久化
                            等待用户回复
                                       │
                                       ▼
                            用户回复 (hide_from_ui HumanMessage
                            + additional_kwargs.human_input_response)
                            → 新 run resume
                                       │
                                       ▼
                            RunJournal 持久化
                            (仅 allowlisted 的 ask_clarification
                             隐藏响应源作为 llm.human.input)
```

## 关键设计决策

1. **`wrap_tool_call` 而非 `after_model`**：澄清是工具调用，要在工具执行前拦截，让 `Command(goto=END)` 在 tools node 之前生效。
2. **必须最后注册**：因为这个中间件会短路工具执行，在任何 `on_tool_end` 触发之前就跳出。AGENTS.md 明确："RunJournal performs a root-run final reconciliation for allowlisted clarification `ToolMessage`s whose `tool_call_id` was produced by the current run, so human-input request cards remain recoverable from `run_events` after checkpoint compaction."
3. **确定性 ID**：重试的澄清调用用相同 `id`，LangGraph reducer 视为替换——避免在 checkpoint 历史里累积重复澄清卡片。
4. **双内容 (text + artifact)**：`ToolMessage.content` 是纯文本 fallback，`artifact.human_input` 是结构化 payload 供前端渲染——确保即使前端不解析 artifact 也能展示问题。
5. **`Command(goto=END)` 而非 `interrupt()`**：用图控制流原语自然终止，checkpoint 持久化后由外层 (Gateway resume / embedded client) 重新 invoke 推进。
6. **`disable_clarification` 抑制**：非交互渠道不能让 run 死锁等待，返回普通 ToolMessage 让 agent 自主继续。
7. **options 归一化**：处理 Qwen3-Max 等模型把数组参数序列化为 JSON 字符串的边界情况。
8. **scheduled-task 直接不绑定工具**：比 runtime 抑制更彻底——`non_interactive` 让工具集构造时就排除 `ask_clarification`。

## 与其他中间件的协作

- **RunJournal**：因本中间件可在 `on_tool_end` 之前短路，RunJournal 对 allowlisted 的澄清 ToolMessage 做 root-run final reconciliation，让 Human Input Card 在 checkpoint 压缩后仍可从 `run_events` 恢复。
- **Human Input Card 回复**：以 `hide_from_ui` `HumanMessage` + `additional_kwargs.human_input_response` 形式提交；RunJournal 只持久化 allowlisted 的隐藏响应源 (当前仅 `ask_clarification`) 作为 `llm.human.input`，在压缩后保留已答复卡片状态而不泄露通用内部隐藏上下文。
- **scheduled-task / non_interactive**：调度运行时工具集直接排除 `ask_clarification`，比依赖 runtime flag 更彻底。
- **(位置约束)**：必须最后注册，在任何自定义 `custom_middlewares` 之后。

---

# 辅助模块

## safety_termination_detectors.py

定义 `SafetyTerminationDetector` 协议、`SafetyTermination` dataclass 和三个内置检测器。

### `SafetyTermination` (frozen dataclass)

```python
@dataclass(frozen=True)
class SafetyTermination:
    detector: str           # 检测器名 (observability)
    reason_field: str       # 承载信号的元数据字段名 (finish_reason / stop_reason)
    reason_value: str       # 实际值 (content_filter / refusal / SAFETY ...)
    extras: dict[str, Any] = field(default_factory=dict)  # provider 特定元数据
```

### `SafetyTerminationDetector` (Protocol)

```python
@runtime_checkable
class SafetyTerminationDetector(Protocol):
    name: str
    def detect(self, message: AIMessage) -> SafetyTermination | None: ...
```

要求实现 side-effect free 且容忍缺失/异常类型元数据——检测器在每个 model response 上运行。

### `_get_metadata_value` 辅助

LangChain provider adapter 对停止信号的存放位置不统一——多数现代 adapter 用 `response_metadata`，但 legacy / passthrough 路径可能用 `additional_kwargs`。按顺序检查两者，只接受字符串值，避免在枚举/dict 上抛错：

```python
def _get_metadata_value(message: AIMessage, field_name: str) -> str | None:
    for container_name in ("response_metadata", "additional_kwargs"):
        container = getattr(message, container_name, None) or {}
        if not isinstance(container, dict):
            continue
        value = container.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None
```

### 三个内置检测器

| 检测器 | name | 触发字段 | 默认值集合 | extras |
|--------|------|---------|-----------|--------|
| `OpenAICompatibleContentFilterDetector` | `openai_compatible_content_filter` | `finish_reason` | `("content_filter",)` (可扩展 `sensitive` / `violation`) | Azure 的 `content_filter_results` |
| `AnthropicRefusalDetector` | `anthropic_refusal` | `stop_reason` | `("refusal",)` | 无 |
| `GeminiSafetyDetector` | `gemini_safety` | `finish_reason` (upper) | `SAFETY` / `BLOCKLIST` / `PROHIBITED_CONTENT` / `SPII` / `RECITATION` / `IMAGE_*` | `safety_ratings` |

`GeminiSafetyDetector` 的默认集合**故意排除** `STOP` / `MAX_TOKENS` / `MALFORMED_FUNCTION_CALL` / `UNEXPECTED_TOOL_CALL` 等——它们不是安全过滤，属于别的失效类别；让 observability 记录诚实分类。

`default_detectors()` 返回这三者的实例列表，用于 `SafetyFinishReasonMiddleware` 默认构造。

---

## _bounded_dict.py

`BoundedDict(OrderedDict)` 是 guard 中间件共享的有界字典实现。按插入顺序淘汰最旧条目，防止 Lead 长期实例上累积无界 `run_id` 条目导致内存泄漏：

```python
class BoundedDict(OrderedDict):
    def __init__(self, maxsize: int = 1000, *args: Any, **kwds: Any) -> None:
        self.maxsize = maxsize
        super().__init__(*args, **kwds)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key not in self:
            if len(self) >= self.maxsize:
                self.popitem(last=False)
        super().__setitem__(key, value)
```

**注意语义**：
- 只在插入**新 key** 时检查淘汰——已存在 key 的更新不会触发淘汰，避免意外丢掉活跃 run 的状态。
- 按**插入顺序**而非访问顺序淘汰 (LRU-insertion)——比 LRU-access 简单且足够 (每个 run_id 一次性写入)。
- `maxsize` 默认 1000，被 `TokenBudgetMiddleware` (5 个字典) 和 `LoopDetectionMiddleware` (`_stop_reason`) 共用。
- `TerminalResponseMiddleware` 也用 `BoundedDict(1000)` 存 `_retry_counts` / `_pending_prompts`，键是 `(thread_id, run_id)` 元组。

---

## tool_call_metadata.py

`clone_ai_message_with_tool_calls` 是 `SubagentLimitMiddleware`、`SafetyFinishReasonMiddleware` 共享的 AIMessage 克隆助手，确保结构化 `tool_calls`、raw provider payload (`additional_kwargs.tool_calls` / `function_call`)、`finish_reason` 三者保持同步：

```python
def clone_ai_message_with_tool_calls(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any | None = None,
) -> AIMessage:
    kept_ids = {tc["id"] for tc in tool_calls if isinstance(tc.get("id"), str) and tc["id"]}

    update: dict[str, Any] = {"tool_calls": tool_calls}
    if content is not None:
        update["content"] = content

    # 同步 additional_kwargs.tool_calls (raw provider payload)
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        synced_raw_tool_calls = [raw_tc for raw_tc in raw_tool_calls
                                 if _raw_tool_call_id(raw_tc) in kept_ids]
        if synced_raw_tool_calls:
            additional_kwargs["tool_calls"] = synced_raw_tool_calls
        else:
            additional_kwargs.pop("tool_calls", None)

    # 没保留任何 tool_call 时连 function_call 也清掉
    if not tool_calls:
        additional_kwargs.pop("function_call", None)

    update["additional_kwargs"] = additional_kwargs

    # finish_reason: tool_calls → stop (只在原值是 tool_calls 时改)
    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    if not tool_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata

    return message.model_copy(update=update)
```

**关键设计**：
- `kept_ids` 用保留的 tool_calls 的 id 集合过滤 raw payload——保留的 raw 条目必须与结构化条目对应。
- raw 全部过滤掉时直接 `pop("tool_calls")` 而非留空 list，避免下游看到空列表误判。
- `finish_reason` 改写只在"原值是 `tool_calls` 且新 tool_calls 为空"时触发——`content_filter` / `refusal` / `SAFETY` 原值保留 (这是 `SafetyFinishReasonMiddleware` 依赖的关键不变量)。
- 用 `message.model_copy(update=...)` 做 Pydantic v2 不可变克隆，不修改原对象。

被以下中间件使用：
- `SubagentLimitMiddleware._truncate_task_calls`：截断超额 task 调用后克隆带剩余 tool_calls 的 AIMessage。
- `SafetyFinishReasonMiddleware._build_suppressed_message`：剥离所有 tool_calls (传空 list) 并保留原 finish_reason。

---

# 总结：六中间件的安全护栏协同

| 维度 | 中间件 | 触发条件 | 响应动作 | stop_reason |
|------|--------|---------|---------|-------------|
| 子 Agent 委派 | SubagentLimitMiddleware | 单响应超并发 / 单 run 超总量 | 截断超额 task 工具调用 | `subagent_limit_capped` |
| 工具调用死循环 | LoopDetectionMiddleware | 哈希重复 ≥ hard_limit 或 类型频率 ≥ hard_limit | 剥离 tool_calls 强制终止 | `loop_capped` |
| Token 预算 | TokenBudgetMiddleware | 任一维度分数 ≥ hard_stop_threshold | 剥离 tool_calls 强制终止 | `token_capped` |
| 终端响应缺失 | TerminalResponseMiddleware | 工具后空响应 (一次重试后仍空) | jump_to model 重试 / 写 fallback | (写 `deerflow_error_fallback`) |
| Provider 安全截断 | SafetyFinishReasonMiddleware | provider 发出安全终止信号且带 tool_calls | 剥离 tool_calls + 用户解释 | `safety_capped` |
| 人机澄清 | ClarificationMiddleware | `ask_clarification` 工具调用 | `Command(goto=END)` 中断等待回复 | (无) |

**共同的 stop_reason 上报机制** (#4176)：四个 "capped" 中间件 (SubagentLimit / LoopDetection / TokenBudget / SafetyFinishReason) 都通过 `runtime.context["stop_reason"]` 写入，Lead Worker 统一读取并上报。LoopDetection 与 TokenBudget 还通过 `BoundedDict[run_id]` + `consume_stop_reason(run_id)` 鸭子类型接口让子 Agent executor 收集 cap 原因——`SubagentExecutor._aexecute` 用 `hasattr(mw, "consume_stop_reason")` 鸭子类型检测每个中间件，取第一个非 None 作为该 run 的 stop_reason，**新增 guard 不需要改 executor**。

**共同的 tool_calls 剥离机制**：LoopDetection / TokenBudget / SafetyFinishReason / SubagentLimit 都需要剥离 AIMessage 的 tool_calls。LoopDetection 用自己的 `_build_hard_stop_update`，TokenBudget 用 `_build_hard_stop_update`，SubagentLimit / SafetyFinishReason 共享 `clone_ai_message_with_tool_calls`。三者都把 `finish_reason: "tool_calls"` 改写为 `"stop"`，但 SafetyFinishReason 故意保留 `content_filter` / `refusal` / `SAFETY` 原值——`clone_ai_message_with_tool_calls` 的"只在原值是 tool_calls 时改写"不变量保证了这一点。

**共同的延迟注入模式**：LoopDetection / TokenBudget / TerminalResponse 都用 `after_model` (或 `after_model` + jump_to) 触发，但不在那一刻插入消息——而是把提醒加入队列，在 `wrap_model_call` 取出并作为 `HumanMessage` 追加到请求末尾。原因相同：`after_model` 紧跟 AIMessage(tool_calls) 之后但 ToolMessage 还没生成，任何插入的消息都会破坏 `assistant tool_calls → tool_messages` 配对，被 OpenAI/Moonshot 校验器拒绝。

**共同的 `BoundedDict` 防泄漏**：所有需要按 run_id 累积状态的中间件 (LoopDetection / TokenBudget / TerminalResponse) 都用 `BoundedDict(1000)` 防止 Lead 长期实例上累积无界条目。

**`run_id` 按存在性判断**：LoopDetection 与 TokenBudget 的 `_get_run_id` 都用 `"run_id" in ctx` 而非真值判断——正确处理嵌入式/TUI 分发子 Agent 的 `run_id=None` 合法情况，避免 stop_reason 丢失。

**链中位置约束**：
- `SubagentLimitMiddleware` 在 Lead 链第 27 位 (subagent_enabled 时)。
- `LoopDetectionMiddleware` 在第 28 位 (loop_detection.enabled 时)。
- `TokenBudgetMiddleware` 在第 29 位 (token_budget.enabled 时)。
- 自定义中间件在第 30 位。
- `TerminalResponseMiddleware` 在第 31 位。
- `SafetyFinishReasonMiddleware` 在第 32 位 (safety_finish_reason.enabled 时)——**故意在 TerminalResponse 之后**，让 LangChain 反向 after_model 派发使 Safety 先看到原始响应。
- `ClarificationMiddleware` 在第 33 位——**必须最后**，因为它会短路工具执行在 `on_tool_end` 之前跳出。

这六中间件共同构成 DeerFlow Agent 运行时的"安全守卫层"，与上游的输入消毒 / 工具结果消毒 / 沙箱 / guardrail / read-before-write 等前置守卫，以及 ToolProgressMiddleware / ToolErrorHandlingMiddleware 等工具质量守卫互补，形成纵深防御。
