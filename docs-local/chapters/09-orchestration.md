# 09 子代理并发、编排与容量治理

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写。
>
> 本章覆盖文件:`packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`、
> `agents/middlewares/delegation_ledger.py`、`agents/middlewares/durable_context_middleware.py`、
> `subagents/capacity.py`、`subagents/executor.py`、`subagents/batch_service.py`、
> `config/subagent_runtime_config.py`、`config/subagent_batches_config.py`、`config/subagents_config.py`、
> `runtime/runs/worker.py`、`tools/builtins/task_tool.py`、`agents/lead_agent/agent.py`、
> `agents/lead_agent/prompt.py`。可配合 `docs-local/middleware/middleware-08-safety-guards.md`(链位 27
> 及 `stop_reason` 家族)阅读。

先划清三个容易混为一谈的"限"——它们分属**三层不同的闸门**,本章逐一展开:

| 闸门 | 计费对象 | 生效点 | 谁执行 | 默认 |
|---|---|---|---|---|
| ① 模型层并发/总量 | 本次模型响应内 `task` 调用数 / 本 run 已入账委托数 | 每次模型输出后(`after_model`) | `SubagentLimitMiddleware`(lead 链位 27) | 并发 3 / run 总量 6 |
| ② 进程执行容量 | 同时占用的执行槽 | `SubagentExecutor` 真正跑模型之前 | `SubagentExecutionCapacity`(FIFO) | `max_running=3`、可排队 |
| ③ 持久批量上限 | 一个 batch 的持久行 | batch 提交 / 领取时 | `SubagentBatchService` + 配置 | `default_max_live_items=100` 等 |

旧书(coolclaws ch10 / hawkli ch07)把编排画成"per-response 线程池调度 + prompt 劝模型少开子代理":软约束
靠模型自觉,硬并发靠每次响应现场开线程池。这一版已彻底重构:**模型层两条硬预算由中间件强制**(不靠
prompt)、**进程层只有一个全局 FIFO 容量控制器**(不按响应建池)、**总量按 durable delegation ledger 以
`run_id` 计费**(跨 checkpoint、跨用户 turn 都算得清)。

---

## 1. 双层预算:per-response 并发 × per-run 总量

### 1.1 威胁模型:并发限制为什么拦不住长 run

假设只有"每次模型响应最多 N 个并行 `task` 调用"这一条限制(旧版思路)。一个长 lead run 的形态是
**反复的规划 checkpoint**:模型执行完一批工具,读结果,再规划下一批……每一个 checkpoint 都会产出一条新的
模型响应。于是模型可以每轮派发 N 个"合法尺寸"的子代理,checkpoint 循环 20 次就派了 20N 个——
并发限制被**时间上错开的合法批次**完全绕过,子代理总量只受 run 时长约束:

```
checkpoint 1: [task × 3]  ── 合法(≤ 3)
checkpoint 2: [task × 3]  ── 合法(≤ 3)
   ⋮ 每轮结果又变成长下文,模型继续"优化"
checkpoint K: [task × 3]  ── 总量失控
```

prompt 层的"尽量少派"只是概率性行为引导,不是保证——模型会为每轮新上下文找到"再派一轮"的理由。源码注释
原话:这种总量失控 "is more reliable than prompt-based limits"(比基于 prompt 的限制可靠)的原因,就是
中间件在**确定性计数**。

### 1.2 两条预算的语义与默认值

`SubagentLimitMiddleware`(构造签名默认 `max_concurrent=MAX_CONCURRENT_SUBAGENTS(=3)`、
`max_total=DEFAULT_MAX_TOTAL_SUBAGENTS(=6)`):

- **并发预算(max_concurrent)**:单条模型响应里最多保留前 N 个 `task` 工具调用,多出来的**整条丢弃**。
  注意是"保留前 N 个、丢掉后面的",不是把超出的排队延后——被丢的调用**不会执行**,其"工作已丢失"
  的事实必须让模型知道(见 §1.4 的硬警告措辞)。
- **总量预算(max_total)**:一次 lead run(一个 `run_id`)内允许入账的 `task` 委托总数,基于 durable
  delegation ledger 里**带当前 `run_id` 标签**的条目计数(§2)。总量预算独立于并发预算:
  `allowed = min(max_concurrent, max_total − 已入账数)`。

两个值都在构造时被 clamp 到硬安全区间,越界配置不报错而是被拉回:

```python
# config/subagents_config.py
MIN_CONCURRENT_SUBAGENT_CALLS = 1
MAX_CONCURRENT_SUBAGENT_CALLS = 64          # 并发 schema 安全上限
MIN_TOTAL_SUBAGENTS_PER_RUN = 1
MAX_TOTAL_SUBAGENTS_PER_RUN = 50            # 总量上限
DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN = 6

def clamp_subagent_concurrency(value, *, execution_capacity=None):
    upper = MAX_CONCURRENT_SUBAGENT_CALLS
    if execution_capacity is not None:
        upper = min(upper, max(MIN_CONCURRENT_SUBAGENT_CALLS, execution_capacity))
    return max(MIN_CONCURRENT_SUBAGENT_CALLS, min(upper, value))
```

注意并发 clamp 的第二重上限 `execution_capacity`:中间件广告的并发**永远不会超过进程真实能跑的执行槽数**
(§3 的 `max_running`)——否则模型按中间件允许值并行派 8 个,而进程只有 3 个槽,多出的 5 个只会排队/超时。

### 1.3 三层来源收敛到一个值

并发预算的解析链(`config/subagents_config.py::effective_subagent_concurrency`):

```
请求级 runtime 覆盖 max_concurrent_subagents(缺省 = subagent_runtime.max_running=3)
        ▼  clamp 到 [1, min(64, 进程执行容量 max_running)]
同一个值同时喂给:① lead 系统 prompt(软告知) ② SubagentLimitMiddleware(硬截断) ③ runtime config
```

总量预算同理:`config.configurable.max_total_subagents`(可选 per-run 覆盖,clamp 1-50)→ 缺省回落
`subagents.max_total_per_run`(默认 6)→ 同样 clamp。配置示例里写得很直白:"The default 6 allows two full
batches at the default concurrency of 3"——**默认 6 = 两个完整批次**,这就是编排默认的"地板价"。

**Hot-reload 边界**:并发值解析时对齐的是**启动时冻结**的 `subagent_runtime.max_running`,而
`subagent_runtime` 在 `reload_boundary.py::STARTUP_ONLY_FIELDS` 里是 restart-required。热加载**绝不允许**
prompt/中间件广告出超过已创建进程控制器的容量——改 `subagent_runtime` 要重启才生效。

### 1.4 中间件:after_model 的确定性截断

中间件挂在 lead 链位 27(`subagent_enabled` 时可选注册),实现 `after_model` / `aafter_model`,逻辑在
`_truncate_task_calls`:

```python
# subagent_limit_middleware.py::_truncate_task_calls(核心,略去防御分支)
task_indices = [i for i, tc in enumerate(last_msg.tool_calls) if tc.get("name") == "task"]
if not task_indices:
    return None
run_id = _runtime_run_id(runtime)                                    # runtime.context["run_id"]
prior = _count_prior_delegations(state["delegations"], run_id=run_id)  # 按 run_id 过滤的 ledger 计数
remaining_total = max(0, self.max_total - prior)
allowed_task_calls = min(self.max_concurrent, remaining_total)
if len(task_indices) <= allowed_task_calls:
    return None
indices_to_drop = set(task_indices[allowed_task_calls:])
truncated = [tc for i, tc in enumerate(last_msg.tool_calls) if i not in indices_to_drop]
if remaining_total == 0:                             # 总量已耗尽
    runtime.context["stop_reason"] = "subagent_limit_capped"   # §7
    content = _append_text(last_msg.content, _TOTAL_LIMIT_STOP_MSG)
updated = clone_ai_message_with_tool_calls(last_msg, truncated, content=content)
return {"messages": [updated]}                       # 同 id 替换,graph 状态就地更新
```

要点:

- **只数名字恰为 `task` 的调用**;`batch_task` 是另一套持久化模式(§5),不进这个 ledger、也不被改写。
- **计数来自 ledger 而非本响应**:`prior` = 状态里 `delegations` 通道中带当前 `run_id` 的已入账委托数。
  一条响应的可用额度 = min(并发限制, 总量余额)——总量约束是**跨 checkpoint 累计**的,不是本响应内自查。
- 截断后调用 `clone_ai_message_with_tool_calls`(**同 `id` 替换**,不是追加新消息):同步裁剪 provider 原始
  `tool_calls` 元数据(只留保留下来的 id)、全空时删 `function_call`,并把 `response_metadata.finish_reason`
  从 `tool_calls` 强制改成 `stop`——被剥光的响应不会带着"空工具调用"进入工具路由,而是纯文本收尾。
- **被丢弃调用的工作真的丢失**。prompt 因此带硬警告:"HARD LIMITS ARE NON-NEGOTIABLE: max {n} `task`
  calls per response, max {total} per run; excess calls are discarded and their work is lost."
- 总量耗尽(`remaining_total == 0`)时,被剥光的消息追加一段可见说明,让模型"用已收集到的子代理结果继续、
  直接做剩余简单活、或总结剩余工作"——见 §7 的完整措辞。

一个容易被忽略的细节:**并发截断发生在 `remaining_total > 0` 时是"静默丢"**(不追加说明、不盖戳),因为
run 还有预算、模型下次可以再派;只有**总量耗尽**才追加可见说明并盖 `subagent_limit_capped`。

---

## 2. durable delegation ledger:按 run_id 计费

总量预算的计数基座是 `ThreadState.delegations`——**随 checkpoint 持久化的委托账本**,不是进程内易失的
计数器。这保证 Gateway 重启、run 恢复、跨轮计费都一致。

### 2.1 通道与 reducer

`ThreadState` 的 `delegations: list[DelegationEntry]`(自定义 reducer `merge_delegations`,
`agents/thread_state.py`):

- 条目按 `id`(provider 工具调用 id)**同 id 最新版覆盖、首见顺序保留**;
- **terminal 状态永不被非 terminal 状态降级**(completed 不会被 in_progress 覆盖);
- 账本整体 **capped 到最近 50 条**(`_DELEGATION_LEDGER_MAX_ENTRIES = 50`);
- `DelegationEntry` 含可选 `run_id` 字段——这就是"这笔账记在哪个 run 头上"的键。

条目来源是消息流里的证据(不是中间件临时构造):`delegation_ledger.py::extract_delegations` 从
AIMessage 的 `task` 工具调用枚举出 `in_progress` 条目,再用配对 ToolMessage 里
`read_subagent_result_metadata` 读出的结构化元数据回填 `status`/`stop_reason`/`result_brief` 等。账本渲染
(`render_delegation_ledger`)以 `## Work already delegated` 段注入模型上下文,含状态指引——completed 条目
"do NOT delegate again; reuse this result",in_progress "already delegated; wait"。**账本同时是编排的
"记忆"与总量计费的"账"**,一份数据两个用途。

### 2.2 run_id 打标:谁把委托记在当前 run 头上

`DurableContextMiddleware`(`agents/middlewares/durable_context_middleware.py`)在每次模型调用前后把
当前消息区间的委托**打上当前 `run_id`**(`_with_run_id`):

- **只给新出现的委托 id 打标**;已存在于状态里的条目保留其原有 `run_id`(可能是更早 run 的)。
- "当前区间"怎么定?`_current_run_messages` 从后往前找**带当前 `run_id` 标签的 HumanMessage**(嵌入端
  `DeerFlowClient.stream()` 会给输入 HumanMessage 盖 `run_id`)作为本 run 起点;**Gateway resume 路径不会
  追加新 HumanMessage**,worker 就把 run 前 checkpoint 已有的 message id 集合放进 runtime context
  (`CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY`),capture 以此为边界——**绝不把旧 task 调用重标成当前 run**。

### 2.3 later user turn 重新计费(计费单位是 run,不是 thread)

这是整套设计的点睛之处:**预算按 `run_id` 重置,而一个 thread 跨多个 run**。每次用户发新消息 = 新 run =
新 `run_id`:

```
turn 1 (run_id=A): 派了 4 个子代理  →  A 名下 ledger 4 条
turn 2 (run_id=B): 还能派几个?       →  B 名下 ledger 0 条 → 满额 6,重新计费
```

`_count_prior_delegations` 的过滤条件是 `entry["run_id"] == run_id`(见 subagent_limit_middleware.py
第 87-88 行),所以**历史 run 的委托不消耗新 run 的预算**。用户追问时模型重新拥有完整一批额度,而不是被
上一轮干掉的配额永久锁死——这正是"durable ledger 按 run_id 计数"相对"按 thread 累计"的关键差异。

**没有 run_id 时 fail-restrictive**:自定义 graph 集成没提供 runtime context 时,中间件打 warning 并把
**整条 thread 的委托都算作已用**(且空 run_id 过滤退化成只按 id 去重)——宁可收紧也不漏放。

---

## 3. 执行容量:SubagentExecutionCapacity(FIFO)

模型层预算管"**能不能派**",进程容量管"**同时有几个真在跑**"。它是 process-wide 的——所有并发 lead
run、所有并行子代理共享同一份;旧书里"每次响应现场开线程池"的模型在这里不存在。

### 3.1 设计:running 计数 + FIFO waiters

`subagents/capacity.py::SubagentExecutionCapacity`:

```python
class SubagentExecutionCapacity:
    def __init__(self, config: SubagentRuntimeConfig) -> None:
        self._lock = asyncio.Lock()
        self._running = 0                                   # 占用执行槽数
        self._waiters: deque[asyncio.Future[None]] = deque()  # FIFO 排队者
```

执行槽是**进程本地 async 信号量 + FIFO 队列**的组合,由配置(`subagent_runtime`,startup-only)决定:

| 配置项 | 默认 | 语义 |
|---|---|---|
| `max_running` | 3 | 同时执行的 native 子代理上限(1-64,也是并发 clamp 的 `execution_capacity`) |
| `max_queued` | 64 | 等待槽位的排队上限(0-10000) |
| `admission_policy` | `queue` | 池满时:`queue` 排队等待 / `reject` 立即拒绝 |
| `queue_timeout_seconds` | 300 | 排队最长等待,超时 = 准入失败 |

### 3.2 排队不占线程:与线程池的本质差异

旧编排为每个并发子代理**占一个线程/协程配额**,排队中也要先占资源;这里是**排队者只是一堆 asyncio
future,零 OS 线程、零 running 槽**——源码 docstring 直说 "queued work never owns a thread",AGENTS 同样
强调 "Waiters hold no scheduler thread"。整条执行链路跑在一个**进程级持久独立事件循环**
(`executor.py::_get_isolated_subagent_loop`,专用 daemon 线程)上,同步 `execute()` 与 `execute_async()`
都把协程 submit 到该 loop——容量控制器、共享 async 客户端都绑定在这个长寿 loop 上,而不是 per-call
起新 loop。

获取/释放(`slot()` 异步上下文管理器):

```python
async def _acquire(self):
    async with self._lock:
        if self._running < self._config.max_running:   # 有空槽:直接占用
            self._running += 1
            return
        queued = sum(not c.done() for c in self._waiters)
        if self._config.admission_policy == "reject" or queued >= self._config.max_queued:
            raise SubagentCapacityRejected(...)         # 立即拒绝
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)                    # 排队
    try:
        await asyncio.wait_for(waiter, timeout=self._config.queue_timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # 从队列移除自己;若 release 恰在超时瞬间转移了槽位,则先归还再报超时
        ...
        raise SubagentCapacityTimeout(...)
```

释放是**槽位 FIFO 交接**,不是简单 `running -= 1`:

```python
def _release_locked(self):
    while self._waiters:
        waiter = self._waiters.popleft()
        if waiter.done():
            continue
        waiter.set_result(None)   # 把现有槽直接转给队首;running 计数保持 flat
        return
    if self._running <= 0:
        raise RuntimeError("Subagent execution capacity released without an owner")
    self._running -= 1            # 无排队者才真正还槽
```

细节值得记:**running 数在交接过程中保持不变**(槽直接移到队首等待者),队列空了才递减;被取消/超时的
等待者必须在锁内把自己从 deque 移除(或归还已转移的槽),否则 `_waiters` 残留脏 future——`snapshot()`
因此允许把"刚超时还没自我移除"的 waiter 也算排队(只会更保守,绝不乐观)。

### 3.3 准入失败发生在模型执行之前

```python
# SubagentExecutor._aexecute
capacity = self.execution_capacity or get_subagent_execution_capacity()
try:
    async with capacity.slot():
        result.status = RUNNING; result.started_at = _utcnow()
        return await self._aexecute_admitted(task, result)   # 真正跑 agent
except SubagentCapacityError as exc:
    result.try_set_terminal(FAILED, error=str(exc), admission_failure=True)
    return result
```

- **排队/被拒 → `SubagentCapacityRejected` / `SubagentCapacityTimeout`**,结果直接 `FAILED` +
  `admission_failure=True` 终结,**一次模型调用都没发生**("occurs before model execution")。父模型收到
  工具失败消息,可改小批次重试。
- 准入失败不算子代理真正"跑挂了":对 durable batch(§5)意味着**退还 lease、不消耗 item 的 attempt
  配额**;对普通 `task` 省下整份模型/工具预算。
- 取消与超时路径都穿过 `slot()` 的 finally → `_release()`,**排队与槽位所有权不会因异常泄漏**。

### 3.4 生命周期与绑定的三条纪律

1. **启动时冻结**:Gateway/嵌入端启动调用 `configure_subagent_execution_capacity(config)` 安装快照,
   控制器**惰性创建**并绑定当前事件循环;同一配置重复安装是 no-op(防多入口重置活队列);运行中(有
   running/queued)重配直接 `RuntimeError`。
2. **loop 绑定**:控制器不能跨事件循环移动("cannot move event loops while executions are active");只有
   完全空闲时才允许为新 loop 重建。直接异步消费者(嵌入端/测试)换 loop 后可合法重建——生产同步/后台
   路径始终共享那个持久独立 loop。
3. **显式注入**:`create_deerflow_agent(..., subagent_runtime=runtime)` 的直接调用方可传 caller-owned
   `SubagentRuntime`,让 `task` 工具、batch 工具、中间件限制与 `SubagentExecutor` **共享同一个控制器**,
   不读全局 YAML;跨多个 graph 复用同一实例 = 共享同一容量边界。

---

## 4. 超时与取消

子代理是后台执行 + 父侧轮询的形态,超时与取消分布在执行器、轮询器、运行 worker 三层。

### 4.1 执行超时:asyncio.wait_for

每个子代理的有效超时 = `subagents.timeout_seconds`(默认 **1800s = 30 分钟**),可用
`subagents.agents.<name>.timeout_seconds` 逐代理覆盖(`get_timeout_for`);自定义 agent 默认自带
`timeout_seconds=900`。内置 `general-purpose` `max_turns=150`、`bash` `max_turns=60`(逐代理可覆盖)。

`execute_async` 把执行包在 `asyncio.wait_for(..., timeout=...)` 里;同步 `execute()` 的隔离 loop 路径同理
(`future.result(timeout=...)` 超时即 `cancel_event.set()` + `future.cancel()`)——两种入口共享同一协作机制:

```python
try:
    return await asyncio.wait_for(self._aexecute(task, result), timeout=self.config.timeout_seconds)
except TimeoutError:
    result.cancel_event.set()                       # 通知执行体协作停止
    result.try_set_terminal(TIMED_OUT, error=f"Execution timed out after {self.config.timeout_seconds} seconds", ...)
    return result
except asyncio.CancelledError:
    result.cancel_event.set()
    result.try_set_terminal(CANCELLED, error="Cancelled by user", ...)
    return result
```

### 4.2 协作式取消:cancel_event 在流边界检查

子代理内部不做强杀,而是**协作取消**:`SubagentResult.cancel_event` 是一个 `threading.Event`;
`_aexecute_admitted` 的 `astream` 循环在**每个 chunk 边界**检查它:

```python
if result.cancel_event.is_set():
    result.try_set_terminal(CANCELLED, error="Cancelled by user",
                            token_usage_records=collector.snapshot_records(), ...)
    return result
```

代码注释明确:**长工具调用(单次迭代内部)不会立刻被打断,要等到下一个 chunk 产出**。父侧 5 秒轮询,
取消传播最坏延迟 = 一个 chunk 周期;注册表清理 `cleanup_background_task` 只收 terminal 条目,取消请求走
`request_cancel_background_task`。`try_set_terminal` 保证**恰好一次**终态写入——后台超时与执行 worker
竞争写同一 result 时,先到者胜,迟到者不得改状态/载荷。

### 4.3 父 run 中止与批量的取消

- **用户取消整个 run**:worker 的 `record.abort_event` 置位 → 流循环中断 →
  `_finish_cancellation(record.abort_action)`(rollback 回滚到 run 前 checkpoint 或 `interrupted`);
  消息里的孤儿 task 调用由 `DanglingToolCallMiddleware` 补占位 ToolMessage,graph 不至于卡死。
- **task 轮询器被取消**(父 run 中止/节点取消):`task_tool` 的 `CancelledError` 分支先
  `request_cancel_background_task`,再等短 grace 期让终态(含最终 token 用量)落账后 re-raise——"节点
  必须以 interrupted 结束,而不是 failed 工具调用";异常退出通用分支同样先请求取消,再
  `_schedule_deferred_subagent_cleanup` 把清理钉到进程持有的子代理 loop(存活于 `asyncio.run()` 拆除)。
- **durable batch 的用户取消**:`cancel_batch` 立即把每个非终态 item 终态化并清 lease(§5.2),fencing
  任何迟到的 worker 完成写。

### 4.4 轮询安全网:polling_timed_out

`task_tool` 每 5 秒读一次 `_background_tasks` 注册表,上限 `max_poll_count = (config.timeout_seconds +
60) // 5`。若后台任务卡死而执行超时兜底没生效(理论上是执行超时先触发),轮询超时是第二道网:发
`task_timed_out` 事件、请求协作取消、安排 deferred cleanup、返回 `status="polling_timed_out"` 的失败
工具消息。与 `timed_out` 的差别:执行器**从未**到达终态(可能仍卡在深层模型/工具调用里),是父侧轮询
先放弃。

### 4.5 轮次轴:max_turns 与 GraphRecursionError

子代理 `run_config.recursion_limit = max_turns`,耗尽时 `agent.astream` 抛 `GraphRecursionError`;
`_aexecute` 的专用 except 分支(先于通用 `except Exception`)从最后流出的 chunk 抢救部分成果:
**有可用部分输出 → `completed` + `stop_reason="turn_capped"`;没有 → `failed` + `turn_capped`**——父模型
能区分"预算耗尽"与"子代理坏了",不必解析结果文本。子代理侧 `stop_reason` 经 ToolMessage 元数据随结果
上账,并作为 `subagent_stop_reason` 展现在委托账本条目里(§7 同族)。

---

## 5. batch_task:持久批量模式

普通 `task` 是一次性后台执行,结果只活在当前 run 的 ToolMessage 与内存注册表里——进程一崩,进行中的
子代理就没了。**`batch_task` 是显式的持久批量模式**:提交即写库(batch + item 行),后台服务按 lease
领取执行,崩溃后由 lease 过期恢复。它是**仅由显式工具进入的模式**,绝不会因为 prompt 大而自动启用;
`subagent_batches.enabled=false` 默认关闭(开启会显著增加模型用量,且要求 `database.backend` 为
sqlite/postgres)。

### 5.1 与普通 task 的三点边界

1. **不占普通 run 的 ledger 配额**:`batch_task` 的持久行不计入 `max_total_per_run`,也不被
   `SubagentLimitMiddleware` 截断/改写——它有自己的持久化维度(§5.3)。
2. **不受 lead 上下文大小的隐式绑架**:大结果留在 owner-scoped 的 API/JSONL 导出里,不整段灌回 lead
   context;`batch_status` 只回传有界预览。
3. **复用同一执行容量**:批量 item 最终仍走 `SubagentExecutor` + §3 的进程槽,不会为 batch 另开执行池。

### 5.2 行模型、lease 领取与恢复

`SubagentBatchService`(`subagents/batch_service.py`)是 Gateway(或显式直接 runtime)启动的后台任务:

- 提交(`submit`)校验 `1 ≤ total ≤ max_items_per_batch`(默认 5000),批量写入 batch/item 行;
- 轮询器每 `poll_interval_seconds`(默认 1.0s)跑一轮 `run_once`:
  `available = max(0, max_running − 本进程在飞 item 数)`,有额度才 `claim_items`——
  以 `lease_owner = hostname:uuid` 原子领取到期/未领取的 item,每个 item 带 `lease_seconds=120` 租约;
- `_execute_item` → `SubagentExecutor`(同一共享容量控制器)→ 有界结果入库;
- **崩溃恢复**:worker 死在租约内,lease 过期后其他(或重启后的)领取者接管该 item;
- **attempt 语义**:队列拒绝/排队超时发生在模型执行之前 → **退还 lease、不消耗该 item 的尝试次数**;
  真实执行失败与 lease 过期才消耗重试预算(`max_attempts=3`);用户取消 → 非终态 item 立即终态化 + 清
  lease,fencing 迟到完成。

### 5.3 三重维度:total / live / running

`subagent_batches` 的持久限额故意拆成三个独立维度(`subagent_batches_config.py`,均有 per-batch 硬上限):

| 配置项 | 默认 | 维度 |
|---|---|---|
| `max_items_per_batch` | 5000 | **total**:一个 batch 允许的持久 item 总数 |
| `default_max_live_items` | 100 | **live**:一个 batch 同时处于 queued/running 的待办数(可被 `batch_task` 参数覆盖,上限 `max_live_items_per_batch=1000`) |
| `default_max_running_items` | 3 | **running**:一个 batch 同时真正占用执行槽的 item 数(≤ `max_running_items_per_batch=64`,且必须 ≤ live) |
| `max_attempts` | 3 | 单 item 重试预算 |
| `max_result_chars` / `result_preview_max_chars` | 100_000 / 2_000 | 存储结果 / 回传预览的长度边界 |

模型校验器强制 `running ≤ live ≤ 每批上限`。三个维度对应三种资源:提交总量(防刷库)、待办积压(防
海量排队)、真实执行(与 §3 进程容量对齐)。工具面:`batch_task`(提交)、`batch_status`(进度,增量轮询)、
`cancel_batch`(取消)——仅在启动期 SQL-backed 提交器就绪时注册进工具集。

---

## 6. 编排示例:分批并发

把三层串起来的典型编排,参数取默认(`max_concurrent=3`、`max_total=6`、进程 `max_running=3`,`6 = 两个
完整批次`),任务 = 一次需要 8 个独立文件调查的深度研究 run:

```
checkpoint 1  模型发 task×4 → allowed=min(3,6−0)=3 → 前 3 保留,第 4 个静默丢弃
              3 个 task 并发执行:进程 3 槽全占;完成一个,_release 把槽转给队首(无队)
              3 个 ToolMessage 回填账本(completed + 摘要)→ A 名下 3 条
checkpoint 2  模型读账本("已完成 3 项,还剩 5 项")→ 发 task×3
              allowed=min(3,6−3)=3 → 全保留 → 账本 A 名下 6 条
checkpoint 3  模型又发 task×2(还剩 2 项)→ prior=6 → remaining=0 → allowed=0
              两条全剥 → content 追加 [SUBAGENT LIMIT REACHED] 说明
              → runtime.context["stop_reason"]="subagent_limit_capped"
              → finish_reason 强制 stop,纯文本收尾;run 记录 success + stop_reason
```

几个编排层的行为观察:

- **并发截断保前丢后**:checkpoint 1 的第 4 个调用被丢,但模型到 checkpoint 2 才发现(没有工具结果回来)
  ——这就是 prompt 硬警告"excess calls are discarded"存在的原因。
- **总量截断发生在"合法批次也会被拒"时**:checkpoint 3 的 2 个调用尺寸合法,但**总量余额为 0**,一样
  全剥。模型此时只能:用已收集的 6 份结果继续、把剩下 2 项直接自己做、或总结成"未完成清单"。
- **每批之间自然串行**:模型必须先读回 ToolMessage 才能规划下一批——总量预算正是沿这条 checkpoint 节奏累计的。
- **若把并发调成 1**:prompt 会移除"并行/多批收益"引导,只允许"专家能力或上下文隔离"类实质收益派发
  (lead 链与 built-in role 同步,测试钉在 `test_subagent_routing_prompt.py`)。
- **分批数 > 预算时的建议**:规划期就把批次数压到 `ceil(总工作量 / min(并发, 余额))` 内;run 级覆盖
  `max_total_subagents`(上限 50)只影响本次 run,不污染后续 turn(§2.3)。

---

## 7. stop_reason = subagent_limit_capped

### 7.1 它是什么、何时盖

`subagent_limit_capped` 是 lead run 侧 `stop_reason` 家族的一员(#4176),与 loop/token/safety 等 cap
并列。它由 `SubagentLimitMiddleware` 在**一次截断恰好因总量耗尽发生**时(§1.4 的
`remaining_total == 0`)写入 `runtime.context["stop_reason"]`。三个前提缺一不可:

1. 模型本响应**又发了 `task` 调用**(否则中间件根本不动作);
2. 该 run 的 ledger 入账数已 == `max_total`(remaining 为 0);
3. 截断真实发生(所有 task 调用被剥)。

`stop_reason` 是**运行时上下文通道**(`runtime.context` dict),不是 graph state——它不进 checkpoint,
由 worker 在 run 结束时读出。同一通道上 worker 注释列了完整映射:
`loop_detection → loop_capped`、`token_budget → token_capped`、`safety_finish_reason → safety_capped`、
`subagent_limit → subagent_limit_capped`、`model_length_finish_reason → model_length_capped`。

### 7.2 worker 的消费语义

`runtime/runs/worker.py` 收尾段(step 8):

```python
# runtime/runs/worker.py 收尾(step 8):守卫在 runtime.context 盖的 stop_reason
# (subagent_limit -> "subagent_limit_capped",与 loop/token/safety/model_length 同通道)写到 run 记录
stop_reason = runtime_context.get("stop_reason") if runtime_context is not None else None
cancel_action = await run_manager.set_status_if_not_cancelled(
    run_id,
    RunStatus.error if delivery_error else RunStatus.success,   # ← 仍是 success
    error=delivery_error, stop_reason=stop_reason, ...)
```

要点:

- **状态仍是 `success`**(除非交付校验失败),`stop_reason` 作为附加字段落在 run 记录上,随流推给客户端
  (`record.stop_reason`)。语义 = "run 正常完成,但中途撞上了委托总量上限",不是失败。
- **清理时机不对称**:worker 在**每个用户可见 turn 开始前**清一次 `runtime.context["stop_reason"]`
  (step 7 注释:"Clear any stale stop_reason before the first (user-visible) turn only");而隐藏的 goal
  延续 turn **保留** cap 原因——"用户在可见 turn 撞 cap 就是 capped,即使后续隐藏评估 turn 干净跑完"
  (#4176 review)。这也与 §2.3 呼应:下一个用户 turn = 新 run = 新预算 + 清过的 stop_reason。

### 7.3 与同族 capped 的差异

| stop_reason | 守卫 | 触发 | 行为特征 |
|---|---|---|---|
| `loop_capped` | LoopDetectionMiddleware(28) | 重复工具调用/单工具频率爆炸 | 剥 tool_calls 强制终答 |
| `token_capped` | TokenBudgetMiddleware(29) | 子代理 run token 超预算 | 剥 tool_calls 强制终答 |
| `subagent_limit_capped` | SubagentLimitMiddleware(27) | run 委托总量耗尽后模型仍发 `task` | **剥掉的是 task 调用**,不是全部工具意图;content 追加可见说明 |
| `safety_capped` | SafetyFinishReasonMiddleware(34) | provider 声明安全截断 | 剥残缺 tool_calls/回填 |
| `model_length_capped` | ModelLengthFinishReasonMiddleware(33) | provider 长度截断 | 纯记账,不改消息 |

两个特殊点值得写死:

1. 其他 capped(loop/token/safety/model_length)是"**把整个响应收成终答**"(失控的是模型整体行为);limit
   版本剥的只是 `task` 意图——同一响应里并存的**其他工具调用照常执行**。run 往往在剥光后的纯文本 turn
   结束,内容带着给模型的收尾指令。
2. 它**不是**子代理侧 `stop_reason`(`turn_capped`/`token_capped`/`loop_capped` 经子代理结果 metadata
   上账)——两个通道同名不同域:`subagent_limit_capped` 只出现在 **lead run 记录**上。

### 7.4 客户端/调用方怎么读

- SSE 终帧与 `/wait` 的 run 终态:status + `stop_reason` 字段;前端/调用方看到
  `status=success, stop_reason=subagent_limit_capped` 即知"委托配额耗尽但结果可用"。
- 触发时的可见正文(`_TOTAL_LIMIT_STOP_MSG`,追加在被剥消息的 content 里):

  > [SUBAGENT LIMIT REACHED] The subagent delegation limit for this run has been reached. Continue
  > using the subagent results already collected, execute remaining simple work directly, or
  > summarize the remaining work instead of launching more subagents.

- 委托账本里的 6 条 terminal 结果都在,模型可凭账本摘要继续合成——**预算耗尽不等于成果丢失**,这正是
  ledger 既当账本又当"成果索引"的设计红利。

---

## 8. 配置速查与旧版对照

| 键 | 默认 | clamp/合法域 | 重启? | 作用 |
|---|---|---|---|---|
| `config.configurable.max_concurrent_subagents` | —(= `max_running` 3) | 1-64,且 ≤ 执行容量 | 否 | 单响应并发(喂 prompt + 中间件) |
| `config.configurable.max_total_subagents` | `subagents.max_total_per_run` 6 | 1-50 | 否 | 单 run 委托总量覆盖 |
| `subagents.max_total_per_run` | 6 | 1-50 | 否(hot-reload) | run 总量默认 |
| `subagents.timeout_seconds` | 1800 | ≥1 | 否 | 内置子代理默认执行超时 |
| `subagents.agents.<name>.*` | — | — | 否 | 逐代理 timeout/max_turns/model/skills/token_budget |
| `subagent_runtime.*` | 3 / 64 / queue / 300 | 见 §3.1 | **是** | 进程执行容量 FIFO |
| `subagent_batches.*` | 关 | 见 §5.3 | **是** | 持久批量维度与恢复 |

**与旧书的差异**(一句话版):旧编排 = 每次响应一个线程池 + prompt 软约束,池间互不相让、总量无硬账;
新编排 = 进程级单一 FIFO 容量控制器(排队零线程)+ 中间件硬截断(并发)+ ledger 按 `run_id` 计费(总量),
batch 另走持久 lease 调度。默认(3/6/3)让轻量部署开箱即用,放大旋钮也有序:先升 `subagent_runtime.max_running`,
再升并发请求值,最后调 `max_total_per_run`——**任何一层都不应单独超过其下层的能力**。

> 延伸阅读:`docs-local/middleware/middleware-08-safety-guards.md`(链位 27-35 与 stop_reason 家族)、
> `middleware-03-error-handling.md`(工具失败语义)、子代理 executor 的 step 事件与结果持久化见
> harness AGENTS "Subagent System" 段。
