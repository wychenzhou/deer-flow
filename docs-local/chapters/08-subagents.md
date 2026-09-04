# 第 8 章 Sub-Agent 总览与执行引擎

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写

## 8.0 本章导览

Sub-Agent(子代理)是 Lead Agent(主代理)委派出去、在自己的上下文里独立执行一段有界任务的**单次性代理实例**。本章不讨论"为什么要用子代理做研究/写代码"这类用法,而是拆解这套委派机制本身的骨架:

- **设计理念**:委派被定位为一种*优化手段*,而不是"任务复杂就派子代理"的默认反应(§8.1);
- **入口**:`task` 工具的参数、前置校验与"启动后台执行 + 轮询 + SSE 进度"的委派路径(§8.2);
- **执行引擎**:进程级持久隔离事件循环、进程级 FIFO 准入控制器、PENDING 注册与后台任务注册表、`SubagentResult` 契约(§8.3~§8.5);
- **身份拆分**:provider `tool_call_id` 与服务器 `execution_id` 刻意分离的双 ID 体系(§8.6);
- **类型注册**:内建 `general-purpose` / `bash`、`config.yaml custom_agents`、管理员托管定义与覆盖层(§8.7);
- **durable delegation ledger**:委派台账如何在消息流里被确定性提取、持久化并回灌给模型(§8.8);
- **验收检查**:一套"防自报(self-report)"的三层机制——报告契约、引用交叉核对、确定性验收清单(§8.9);
- **隔离边界**:子代理与父线程在沙箱、记忆、日志、技能上的共享与隔离(§8.10)。

> 旧书提示:老版本(backend/src/、skill.yaml、11 层中间件、后台线程池)的 sub-agent 章节已整体失效。新版把"身份"拆成了 provider 侧与服务器侧两个 ID;把"后台执行"从线程池改为**一个进程共享的持久隔离事件循环 + 协程准入队列**;新增了 durable delegation ledger 与三层验收检查。本文一律以 `backend/packages/harness/deerflow/` 下的当前实现为准。

---

## 8.1 设计理念:委派是优化,不是默认

### 8.1.1 收益导向的路由策略

DeerFlow 的委派哲学写死在三处并保持一致(有回归测试钉住):Lead Agent 的系统提示(`agents/lead_agent/prompt.py`)、`task` 工具的 docstring、两个内建角色的 description。核心论断只有一句:

> **启用 sub-agents 是把委派暴露为一种优化手段,而不是对复杂度的默认反应。**

主代理默认直接执行;只有当下面三类收益**明显超过**委派成本时才允许调用 `task`:

1. **并行延迟收益**——若干相互独立、互不重叠的任务并发执行能实质节省墙钟时间;
2. **专长收益**——某个子代理拥有直接路径上没有的专用工具、技能、模型或领域指令;
3. **上下文隔离收益**——一段有界但异常"吃上下文"的调查,放进独立上下文以免挤占父上下文。

而被明确写进否决清单的是:任务"复杂、多步、冗长、涉及大仓库"本身**不是**委派理由;跨代理存在输出依赖或共享可变状态(重叠文件、共享副作用)是并行分发的**硬否决**;需要用户交互或澄清的任务禁止委派(子代理也不持有 `ask_clarification`)。

### 8.1.2 成本必须进决策

路由提示要求主代理在每次决策里显式权衡成本,而不是只数收益:

- 多个上下文里**重复做同样的仓库发现**的成本;
- **协调、核验、综合**各子代理返回结果的成本;
- 任何父代理用直接工具就能**更便宜完成**的任务。

即使是一条有界的顺序链(任务 A 的产物是任务 B 的输入),也不允许拆成多个并行子代理;只有当"专长或上下文隔离收益明显胜出"时,才允许整条链放进**一个**子代理里顺序执行。并行范围必须互相独立且不重叠,主代理用最少的子代理数,并且每一批新委派都要重新评估(同时保留批内并行收益)。

### 8.1.3 一句话模型

`task()` 是一次**同步语义的工具调用**,底层是"后台启动一个在隔离事件循环上跑的协程 + 父侧每 5 秒轮询 + 通过自定义事件推送步骤进度"。子代理是一个**一次性、不续跑、无 checkpointer** 的 LangGraph 图(§8.4.3),做完就终结,结果以结构化 `ToolMessage` 回到父上下文——父代理再决定如何综合。

---

## 8.2 task 工具与委派路径

### 8.2.1 工具签名

`task` 定义在 `tools/builtins/task_tool.py`,用 LangChain 的 `@tool("task", parse_docstring=True)` 声明,参数为:

```python
async def task_tool(
    runtime: Runtime,
    prompt: str,                      # 交给子代理的任务描述
    subagent_type: str,               # general-purpose / bash / 自定义名
    tool_call_id: Annotated[str, InjectedToolCallId],  # provider 生成的调用 ID
    *,
    acceptance_criteria: list[str] | None = None,  # 验收清单(§8.9.3)
    description: str = "",            # 3-5 词短标签,仅用于日志/前端进度卡
) -> str | Command
```

- `description` 只是展示标签,**执行从不依赖它**;provider 没传时生命周期展示回退到 `prompt`。
- `acceptance_criteria` 是"模型提供的不可信数据"(最终可被用户影响),在 `_build_initial_state` 里被追加到子代理的 task `HumanMessage` 上(经过 `InputSanitizationMiddleware` 的中和与边界化),**绝不进 SystemMessage 通道**(§8.9)。
- 返回类型是 LangGraph `Command`:更新父线程消息流,插入一条结构化 `ToolMessage`(见 §8.2.4)。

### 8.2.2 前置校验

`task_tool` 进入正题前做四道检查(都在真正启动执行之前):

1. **能力目录过滤**:从 runtime metadata 里读 `allowed_subagents`(Custom Agent 的策略快照,`None` = 全放行、`[]` = 硬拒绝、list = 白名单),据此收敛可见的子代理名列表;列表为空时报 "none permitted by caller policy"。
2. **bash 沙箱门**:请求 `bash` 时先查 `is_host_bash_allowed()`;宿主机 bash 被禁用且没有隔离 shell 沙箱时直接返回 `LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE`——即使 registry 里还看得到这个名字,也要先给专用指引再报错。
3. **类型解析**:`get_subagent_config(subagent_type)` 找不到,或名字不在可用目录里时,报错并**列出全部可用类型**。
4. **技能白名单合并**:若父代理有 `available_skills`,子代理配置里的 `skills` 要取父策略与子配置的**交集**(`_merge_skill_allowlists`),防止子代理看到父代理都看不到的技能。

工具装配时,子代理**拿不到 `task`**(`subagent_enabled=False`,防递归嵌套),也拿不到上传工具(`include_upload_tool=False`——子代理有独立 ThreadState,`runtime.state["uploaded_files"]` 不存在,当前 run 的文件排除逻辑不成立)。工具组继承父代理的 `tool_groups`,模型名经 `resolve_subagent_model_name` 解析(`inherit` → 用父模型;无父模型时回退到配置的第一个模型)。

### 8.2.3 委派路径全景

```text
[父代理模型回合]
   └─ task 工具被调用
        ├─ 前置校验(目录/沙箱/类型/技能)              task_tool.py
        ├─ 装配 SubagentExecutor(配置快照、父上下文身份) 
        ├─ executor.execute_async(prompt, task_id=tool_call_id)
        │     ├─ 生成服务器 execution_id(uuid4 全量)   ← registry 主键
        │     ├─ SubagentResult 以 PENDING 注册进 _background_tasks
        │     └─ 协程提交到持久隔离事件循环(带超时)      executor.py
        └─ 轮询循环(每 5s,最多 (timeout+60)//5 次)
              ├─ aemit_custom_event: task_started / task_running(每步)
              ├─ 读到终态后: 报告 usage → 发终态事件 → 清理注册表
              ├─ 完成分支: 引用核对 + 验收清单(§8.9)
              └─ 返回 Command(ToolMessage + metadata)
```

注意一个关键取舍:**轮询由工具自己(在父运行的事件循环上)做,不让 LLM 去轮询**。LLM 只发起一次调用,拿到一条最终 `ToolMessage`。

### 8.2.4 事件与结果形态

执行期间,`task_tool` 通过 `aemit_custom_event`(LangGraph `StreamWriter`)发六类 `task_*` 自定义事件,`task_id` 一律用 **provider 的 `tool_call_id`**(前端卡片、SSE 关联都靠它):

| 事件 | 载荷要点 |
|------|---------|
| `task_started` | `task_id`、`description`(或 prompt)、**解析后的有效模型名** |
| `task_running` | 每个新 AI 消息一条,`message_index` 1 基、`total_messages`、累积 usage 快照 |
| `task_completed` | `result`、usage、模型名 |
| `task_failed` | `error`、usage |
| `task_timed_out` | `error`、usage |
| `task_cancelled` | `error`、usage |

usage 快照由子代理内部的 `SubagentTokenCollector` 在每个 LLM 响应后发布到共享 `SubagentResult`,下一次 `task_running` 带上它——前端折叠卡可以整体替换而不必重算父 run 总量。终态 ToolMessage 的 `additional_kwargs` 同时携带结构化元数据(`subagent_status`、`subagent_stop_reason`、`subagent_error`、`subagent_result_brief`/`_sha256`、`subagent_model_name`、`subagent_token_usage`、`subagent_tool_receipts`、`subagent_receipt_verdict`、`subagent_acceptance_verdict`),由 `subagents/status_contract.py` 统一读写,枚举值被 `contracts/subagent_status_contract.json` 跨 Python/TS 钉住。

工具的终态映射:后台执行状态 `COMPLETED/FAILED/CANCELLED/TIMED_OUT` → 线缆状态 `completed/failed/cancelled/timed_out`,外加工具层自己的 `polling_timed_out`(轮询预算耗尽的安全网)。文本内容经 `format_subagent_result_message` 生成(带上 stop_reason 人类可读标注),完成且带验收清单时再拼 `render_acceptance_section`。

---

## 8.3 执行引擎(一):持久隔离事件循环

### 8.3.1 为什么需要"隔离事件循环"

`task` 是同步语义,但被调用时父代理很可能正跑在某个事件循环里(Gateway/嵌入式都在 async 环境),直接 `await` 会把父循环阻塞到子代理跑完(默认最长 30 分钟)。早期方案是"每次执行起一个独立线程 + 短命事件循环",代价是共享的异步客户端(HTTP/SQL 连接池等)被绑定到会被立刻关闭的短命 loop 上,反复重建。

新实现(commit 2672e209)采用**一个进程级、长期存活、跑在专用 daemon 线程上的事件循环**:

```python
# subagents/executor.py
def _run_isolated_subagent_loop(loop, started_event):
    asyncio.set_event_loop(loop)
    loop.call_soon(started_event.set)
    loop.run_forever()          # daemon 线程 "subagent-persistent-loop",永不返回

def _get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    # 懒创建;已存在且 usable 则复用
```

- 线程名 `subagent-persistent-loop`,daemon=True,进程退出时由 `atexit` 注册的 `_shutdown_isolated_subagent_loop()` 优雅停闭(先 `call_soon_threadsafe(loop.stop)` 再 join 1 秒、再 close;超时只告警不硬关)。
- 同步 `execute()` 路径(以及 `execute_async()` 的提交)都通过 `asyncio.run_coroutine_threadsafe(coro, loop)` 把协程投到这个持久 loop 上,自己 `future.result(timeout=config.timeout_seconds)` 阻塞等待。
- 这样做之后,**同一个进程的所有子代理共享一套异步基础设施**(准入控制器、沙箱客户端、事件存储),没有"每条执行一个 loop"的开销与资源泄漏。

### 8.3.2 ContextVar 拷贝:隔离 loop 不是空白 Context

跨线程换 loop,`contextvars.Context` 不会自动跟着跑。`_copy_isolated_subagent_context()` 显式拷贝父上下文,只做一处修剪:**删除标记了 `deerflow_loop_bound` 的回调处理器**,典型代表是父运行专属的 `RunJournal`(它拥有父 loop 上的任务和 SQL 连接池)。

为什么必须拷贝而不是传空白 Context:LangGraph 把当前 runnable config 存在 ContextVar 里,子图要**继承父的 checkpoint namespace**(否则子代理的 AI/tool 帧会串进父的 `messages` 流);用户身份、追踪上下文、metadata、tag、以及框架的流回调也要存活。但如果让 `RunJournal` 整只跨过 loop 边界,父循环和子循环会**重复记账**,并触发 `Future attached to a different loop`。LangGraph 会把继承回调与子 run 的显式回调(子代理的 token collector、tracing 回调)合并,所以修掉 loop-bound handler 不会丢掉子代理的 token 帧。

### 8.3.3 反向穿越:清理与终态 usage 报告

loop 边界有双向流量:

- **父 → 子**:上述 `run_coroutine_threadsafe` 提交执行。
- **子 → 父**:两个刻意保留的窄通道。
  1. **loop 检测审计记录**:`task_tool` 捕获父 loop,构造 `_ParentLoopMiddlewareRecorderProxy` 交给 `SubagentExecutor`;子代理 loop 上的 loop-detection 中间件通过该代理把日志 append `call_soon_threadsafe` 回父 loop 上的 `RunJournal`,避免从子 loop 直接调父 run 的事件存储。工具返回前 `aclose()` 代理:先围栏(fence)迟到的子事件,再让已接受的 append 全部落地。
  2. **终态 usage 报告(延迟清理路径)**:当轮询异常退出、需要把清理任务钉在持久 loop 上(`run_on_isolated_subagent_loop()`)时,`_deliver_final_usage_report` 把最终 usage 记录**送回父 loop** 上的 `RunJournal`(`call_soon_threadsafe`)。方向不可反:journal 的累加器是无锁读改写字段,`get_completion_data()` 会遍历 `_tokens_by_model`,跨线程写会丢 token 更新或破坏迭代。若捕获父 loop 时它已关闭(`asyncio.run` 同步收尾),报告按设计丢弃并记 info 日志——run 已持久化完成数据,计数器无人再读。

### 8.3.4 为什么清理任务必须钉在持久 loop

`asyncio.run()` 在退出时会取消调用方 loop 上的所有任务。如果延迟清理是 `asyncio.create_task` 挂在轮询器的短命 loop 上,会在真正执行前被取消。因此清理协程用 `run_on_isolated_subagent_loop()` 投到持久 loop——它比轮询器的 loop 活得久。清理任务只持有**解析好的 usage recorder + 各 ID + 父 loop 引用**,绝不持有整个 `runtime`(那会连带钉住父 run 的 journal 与事件存储,长达整个轮询预算窗口)。`_deferred_cleanup_tasks` 集合持有强引用防止任务被 GC;注册表条目状态持续不可读时用 `force_cleanup_background_task` 兜底强删(§8.5)。

---

## 8.4 执行引擎(二):准入控制与执行本体

### 8.4.1 进程级 FIFO 准入控制器

`subagents/capacity.py` 实现 `SubagentExecutionCapacity`——一个进程共享、**FIFO** 的异步容量控制器:

```python
class SubagentExecutionCapacity:
    async def slot(self):   # async context manager: 先 _acquire 后 _release
```

- `_running` 计数;满了就进 `_waiters`(双端队列,FIFO),**排队者不占任何线程/调度器**——等待者是挂在 future 上的协程;
- 配置来自 `subagent_runtime`(startup-only 快照):`max_running=3`(1~64)、`max_queued=64`、`admission_policy="queue" | "reject"`、`queue_timeout_seconds=300`;
- 满员且 `admission_policy=="reject"`(或队列达上限)直接抛 `SubagentCapacityRejected`;排队超时抛 `SubagentCapacityTimeout`;两者在 `_aexecute` 里被捕获,结果以 `FAILED + admission_failure=True` 终态化——**没进模型、没消耗任何执行资源**;
- 槽位转移用"把释放的槽转给队首等待者,`_running` 保持平坦"的算法(`_release_locked`),取消/超时的等待者自我摘除;若摘除瞬间槽已转给它,则它先替转让人释放槽再报超时——不泄漏槽位。

并发度解析只做一次,三方取最小:每 run 请求值(`max_concurrent_subagents`)→ 启动冻结的 `subagent_runtime.max_running` → 模式安全天花板(1~64),结果同时给 Lead prompt 与 `SubagentLimitMiddleware` 用。**热重载不允许任何一层宣称超出已建控制器能力的并发**;改启动值必须重启。

### 8.4.2 执行本体 `_aexecute` 的状态机

```python
async def _aexecute(self, task, result_holder=None) -> SubagentResult:
    # 1. 补 PENDING → RUNNING(带 started_at),在容量槽内执行
    async with capacity.slot():
        result.status = RUNNING
        return await self._aexecute_admitted(task, result)
    # 2. 准入失败 → FAILED + admission_failure=True
```

`_aexecute_admitted` 是真正的执行体,要点:

- **执行上下文**:`sandbox_lease_owner_id = f"subagent:{result.task_id}"`,连同 `run_id`、`user_id/user_role/oauth_*`、`authz_attributes`、`is_internal`、`channel_user_id`、扩展 task store 一起写进传给 `agent.astream(..., context=...)` 的 runtime context,并打 `context["is_subagent"] = True`——下游中间件/沙箱据此区分主代理与子代理;
- **图装配**:`_create_agent()` 用 `create_agent(state_schema=ThreadState, checkpointer=False)`,中间件走 `build_subagent_runtime_middlewares`(§8.10.1);`run_config` 的 `recursion_limit = config.max_turns`,显式回调只放子代理专属的 `SubagentTokenCollector` + tracing 回调——**不放进任何 checkpoint 坐标键**(`thread_id/checkpoint_ns/...`),让 LangGraph 从拷贝来的父 ContextVar 继承坐标,保持子图命名空间(§8.10.4);
- **流式主循环**:`async for chunk in agent.astream(state, config=run_config, context=context, stream_mode="values")`,每个 chunk 做四件事——(a) 先保留 `final_state = chunk`(让在途工具结果先于协作式取消被收割);(b) `update_tool_receipts` / `update_bash_executions` 累积验收证据;(c) 检查 `result.cancel_event` 实现**协作式取消**;(d) `capture_new_step_messages` 增量捕获步骤消息(见下);
- **终态判定**:流结束后扫描最后一条 AI 消息——带 `deerflow_error_fallback` 标记(LLMErrorHandlingMiddleware 产物)则映射为 `FAILED`,否则 `_extract_final_result` 抽取结果文本(找不到可用文本返回哨兵 `"No response generated"`)。

### 8.4.3 一次性执行:checkpointer=False 与坐标系继承

子代理图编译时 **`checkpointer=False`**,绝不继承父 run 的 checkpointer——子代理是一次性的,从不 resume(也没有 checkpoint 表行)。同时 `_aexecute` 刻意在子 `RunnableConfig` 里省略 `thread_id/checkpoint_ns/checkpoint_id/checkpoint_map`:LangGraph 必须从拷贝的父 ContextVar 继承这些坐标,委托图才能保持**非根子图命名空间**;在 LangGraph 1.2.6+ 上,哪怕显式补回与父相同的 `thread_id`,也会开一条新的根 lineage,并可能把子代理的 AI/tool 帧路由进父 `messages` 流。DeerFlow 业务组件(沙箱、中间件、归因)仍然通过 `runtime.context["thread_id"]` 拿到父 thread_id——这是受推荐的查找路径。

### 8.4.4 步骤捕获与事件持久化

`subagents/step_events.py::capture_new_step_messages` 遍历每个 `values` chunk **新追加的尾部**(不是只看 `messages[-1]`)——一个回合发多个工具调用时,`ToolNode` 会在一个 super-step 里追加多条 `ToolMessage`,只看末条会丢掉除最后外全部工具输出(#3779)。每步的文本与工具调用 args 都按 `SUBAGENT_STEP_MAX_CHARS` 截断并打 `truncated` 标记,防止大 payload 撑爆行宽。

持久化侧:`runtime/runs/worker.py::_SubagentEventBuffer` 把父 stream 上的 `task_*` 事件批量写入 `RunEventStore`,落成 `subagent.start` / `subagent.step` / `subagent.end`(`category="subagent"`,metadata 里带 task_id)。写用 `put_batch`(终态事件、累计 25 条、worker finally 三处冲刷),因为 `put()` 是低频接口(每次调用持 per-thread 咨询锁),而深层子代理(`max_turns=150`)在热流上会发几百步。独立 category 让这些记录不进 `list_messages`(线程消息流),`list_events` 可查;`list_events` 支持 `task_id` 过滤 + `after_seq` 游标,前端卡片按需回填某一次委派的完整步骤,不被 run 级 limit 截断尾部。

### 8.4.5 三步护栏与 stop_reason

三个独立坐标轴可以提前结束子代理,且都用**一个加性字段** `stop_reason` 表面原因,而不是新造状态枚举(#3875 Phase 2):

| 轴 | 机制 | 触发后 |
|----|------|--------|
| 回合轴 | `run_config.recursion_limit = max_turns`,耗尽抛 `GraphRecursionError`,`_aexecute` 专捕 | 有可用部分结果 → `completed + turn_capped`;否则 `failed + turn_capped` |
| token 轴 | `TokenBudgetMiddleware`(默认 `max_tokens` 与 `summarization.enabled` 耦合:开 1M / 关 2M,warn 0.7,硬停 1.0;用户设值永远优先) | 硬停**不抛异常**:剥掉在途回合的 tool_calls、强制 `finish_reason="stop"`,自然完成 → `completed + token_capped` |
| 循环轴 | `LoopDetectionMiddleware`:重复相同工具调用集、或某工具类型高频变化参数 | 同样剥 tool_calls 强制收尾 → `+ loop_capped` |

`_aexecute` 通过 `hasattr(m, "consume_stop_reason")` **鸭子类型**收集所有带该方法的中间件(每个按 run_id 提供一次性取值),取第一个非空原因。加性字段意味着:**被 cap 但有可用输出的 run 保持 `completed`,部分成果随 `result` 存活**,父代理能区分"正常收工"与"被预算掐断",而无需解析文本。终态 ToolMessage 的 `subagent_stop_reason` 让前端/日志看到同一原因。capped 文本标注示例:"Task Succeeded (capped: token budget). Result: …"。

LLM 调用失败本身也走干净路径:`LLMErrorHandlingMiddleware` 把 provider/模型异常转成带 `deerflow_error_fallback=true` 标记的 `AIMessage`,图正常终结;但"图干净终结 ≠ 任务成功"——`_aexecute` 终结时只看标记,把这种消息映射成 `FAILED` 并发出 `task_failed` 与结构化 `subagent_error`。**只有标记是权威的**,看起来像错误但没有标记的散文仍然是正常完成结果。

---

## 8.5 执行引擎(三):PENDING 注册与后台任务注册表

### 8.5.1 execute_async:注册 → 提交 → 遗忘

```python
def execute_async(self, task: str, task_id: str | None = None) -> str:
    execution_id = str(uuid.uuid4())          # 全量 uuid:服务器主键
    result = SubagentResult(task_id=execution_id,
                            external_task_id=task_id,   # provider tool_call_id
                            trace_id=self.trace_id,
                            status=SubagentStatus.PENDING)
    # 先拷贝父上下文(拷贝本身可能失败;失败就不能注册,否则留下 PENDING 僵尸)
    parent_context = _copy_isolated_subagent_context()
    with _background_tasks_lock:
        _background_tasks[execution_id] = result     # ← PENDING 注册点
    # run_with_timeout: asyncio.wait_for(_aexecute, timeout) 包一层
    # TimeoutError→TIMED_OUT / CancelledError→CANCELLED / Exception→FAILED
    execution_future = _submit_to_isolated_loop_in_context(parent_context, run_with_timeout)
    _background_futures[execution_id] = execution_future
    execution_future.add_done_callback(forget_future)
    return execution_id
```

要点:

- **PENDING 注册先于提交**:注册在 ContextVar 拷贝**之后**、提交**之前**。提交若失败(持久 loop 没起来),必须立刻把刚注册的条目弹掉——调用方永远拿不到 execution_id 来轮询,而 `cleanup_background_task` 拒绝清理非终态条目,不弹就是僵尸;
- **超时/取消由包装协程兜底**:`asyncio.wait_for(..., timeout=config.timeout_seconds)` 触发后置 `cancel_event` 并 `try_set_terminal(TIMED_OUT/CANCELLED)`——因为子代理线程/协程无法被强杀(`Future.cancel()` 做不到),只能协作式停;
- 注册表是一对进程级 dict:`_background_tasks[execution_id] -> SubagentResult`(可轮询的状态对象)与 `_background_futures`(底层 future,future 完成即被 forget 回调摘除,防止内存堆积)。

### 8.5.2 注册表 API 与取消语义

模块级函数(`executor.py` 尾部)构成完整生命周期:

| 函数 | 语义 |
|------|------|
| `get_background_task_result(execution_id)` | 读状态对象(task_tool 轮询循环每 5s 调它) |
| `request_cancel_background_task(execution_id)` | 置 `cancel_event` + `future.cancel()`(注意锁序:先取锁读、出锁后 cancel,因为 cancel 会同步触发持同一锁的 forget 回调) |
| `cleanup_background_task(execution_id)` | **只删终态条目**(COMPLETED/FAILED/TIMED_OUT/CANCELLED 或 completed_at 非空),避免与仍在更新的后台执行竞态 |
| `force_cleanup_background_task(execution_id)` | 无条件强删——给"条目在但状态对象已不可读"的中断展开路径兜底;此时协作取消早已请求,泄漏条目比丢条目更糟,子代理线程自己还握着 result 引用,之后 `try_set_terminal` 无害 |

取消是**协作式**的:`_aexecute` 只在 `agent.astream()` 的迭代边界检查 `cancel_event`,正在执行的长工具调用要等下一个 chunk 才能响应。`SubagentResult.cancel_event` 是个 `threading.Event`——超时路径(持久 loop 上的 `wait_for`)与用户取消路径可以跨线程竞争同一个 holder。

### 8.5.3 SubagentResult:可跨线程竞争的一次性终态

`SubagentResult` 是注册表条目、轮询对象、终态载荷三位一体:

```python
@dataclass
class SubagentResult:
    task_id: str                 # 服务器生成,拥有本次执行(注册表主键)
    external_task_id: str | None # provider 关联 ID,可跨父 run 重复(§8.6)
    trace_id: str                # 日志关联
    status: SubagentStatus       # PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/TIMED_OUT
    result: str | None           # 完成后的最终文本
    error: str | None
    stop_reason: str | None      # token_capped/turn_capped/loop_capped(加性)
    token_usage_records: list    # collector 快照
    tool_receipts: list | None   # 收割的子代理工具收据(RFC #4651 PR2)
    bash_executions: list | None # 验收用 bash 执行证据(RFC #4651 PR4)
    cancel_event: threading.Event
```

它的核心不变量由 `try_set_terminal(status, ...)` 保证:**终态只能设置一次**。后台超时/取消与执行 worker 可能竞争同一个 holder,第一个到达的终态转换赢,迟到的终态写不得改写状态或载荷——所有读写都在 `_state_lock`(threading.Lock)下进行。`update_token_usage_records` / `update_tool_receipts` / `update_bash_executions` 则是运行中发布方法,只在非终态时生效(终态后拒绝再写)。

> 命名提醒:`SubagentResult.task_id` 就是 execute_async 返回的 `execution_id`(全量 uuid)。sync 路径 `_aexecute` 内部创建的 result 用 `str(uuid.uuid4())[:8]` 短 id,只活在同步调用栈内,不进注册表。

---

## 8.6 身份拆分:provider tool_call_id → external_task_id,server uuid → task_id

### 8.6.1 为什么要拆

一个父 run 里每次 `task` 工具调用,provider(OpenAI/DeepSeek 等)会生成一个 `tool_call_id`,它同时是 `ToolMessage` 回填的槽位键。乍看用 `tool_call_id` 当后台任务主键最自然——但它有一个致命缺陷:**provider 的 tool_call_id 不是跨父 run 全局唯一的**。并发父 run(多个线程/多用户)完全可能各自拿到重复的 ID,拿它当进程级注册表的所有权键会互相踩踏。

于是后端把身份刻意拆成两层:

| 维度 | provider `tool_call_id` | 服务器 `execution_id`(uuid4 全量) |
|------|------------------------|----------------------------------|
| 生成方 | 模型服务商 | `SubagentExecutor.execute_async()` |
| 存到 | `SubagentResult.external_task_id` | `SubagentResult.task_id` |
| 用在哪 | `ToolMessage` 回填、`task_*` SSE 事件、持久化生命周期事件、前端任务卡、`ExtensionData.scope_id` 公共契约 | **进程级注册表主键**、轮询、取消、超时、清理 |
| 唯一性 | 仅单父 run 内保证 | 进程内全局唯一 |

### 8.6.2 两条纪律

1. **provider ID 永不成为 registry 所有权键**:execute_async 的签名故意是 `task_id: str | None = None`(external),docstring 明说 "It is never used as the process-wide background registry key because provider tool-call IDs can repeat across concurrent parent runs"。scheduler 闭包等长期持有者**自己保留 `SubagentResult` 引用**,而不是事后穿过易变的注册表重新解析所有权。
2. **usage 归因不经过进程级 provider-ID 缓存**:子代理终态 token usage 放在当前 run 的 `ToolMessage.additional_kwargs` 里,从消息状态归因;绝不维护"provider id → usage"的进程全局缓存。

### 8.6.3 第三、第四个身份

日志与追踪还叠加两层:

- **`trace_id`**:每个 `SubagentExecutor` 构造时生成(`uuid4()[:8]` 短标签,可被父 runtime metadata 覆盖),写进所有日志前缀 `[trace=...]`,把父与子代理日志串成一条线;
- **`deerflow_trace_id`**:请求级关联 ID(`X-Trace-Id`),从父 runtime context 继承(resolve 顺序:显式传入 → 环境值),同步进子代理 run 的 Langfuse 元数据,让"一次用户请求 → 主 run → 若干子代理 run"形成一棵可追溯的树。

---

## 8.7 Registry:内建 / 自定义 / 托管类型

### 8.7.1 内建两个角色

`subagents/builtins/` 定义两个内建 `SubagentConfig`:

| 类型 | 定位 | 工具 | max_turns |
|------|------|------|-----------|
| `general-purpose` | 有界探索与行动的通才 | `tools=None`(继承父全部可用工具,再减掉拒绝集) | 150 |
| `bash` | 命令执行专才 | 仅沙箱工具 `["bash","ls","read_file","write_file","str_replace"]` | 60 |

两者共同的 `disallowed_tools`:`["task", "ask_clarification", "present_files"]`——禁止递归委派、禁止打断用户要澄清、禁止直接向用户展示文件。bash 子代理在宿主机 bash 被禁用时从可见目录里被剔除(`get_available_subagent_names` 联动 `is_host_bash_allowed`)。模型默认 `inherit`(继承父模型),全局 `subagents.max_turns` / `subagents.timeout_seconds`(默认 1800s=30 分钟)只对**内建**生效;自定义类型用自己的配置值。

### 8.7.2 解析顺序与覆盖

`subagents/registry.py::get_subagent_config(name)` 的四层解析(注释明说对齐 Codex 的配置分层):

1. **内建**:`BUILTIN_SUBAGENTS`(general-purpose、bash);
2. **`config.yaml custom_agents`**(operator 控制的本地自定义);
3. **管理员托管定义**:`ManagedSubagentDefinition`(经 `agent_storage.backend` 持久化,与 Custom Agent 定义同一存储后端;按 1 秒签名 TTL 缓存);
4. **`subagents.agents.<name>` 逐代理覆盖**:timeout、max_turns、model、skills——只显式覆盖;全局默认不覆盖自定义代理自己的值。

覆盖的优先级细节:timeout/max_turns 为"逐代理覆盖 > 全局默认(仅内建) > 配置自身值";model/skills 只有逐代理覆盖。名字冲突时,**内建与 config.yaml 定义优先**,同名托管定义"仍持久化(Settings UI 可见)但被排除出运行时"。`list_subagents` / `get_subagent_names` 支持 `allowed_subagents` 白名单过滤;Custom Agent 的 `allowed_subagents` 在装配时被**快照进 run metadata**,提示发现与 `task` 执行两侧都用同一快照过滤——工具内部绝不重读可变的代理配置来推调用方策略(防 TOCTOU)。

### 8.7.3 直接调用方的 SubagentRuntime

直接 `create_deerflow_agent` 的 SDK 集成方不走进程全局 YAML,而是显式传入 `SubagentRuntime`(`subagents/runtime.py`):它把一个 `SubagentExecutionCapacity`(真实执行上限)、可选 batch worker/仓库、`max_total_per_run` 与可选 `app_config` 快照绑成一个对象,同应用的多个图共享同一执行天花板。绑定的 `task` 工具用 ContextVar 把该 runtime 的 capacity 传进 executor(`bind_task_tool`),绑定的 batch 工具只解析该 runtime 自己的 submitter,绝不 fall through 到别的应用的进程全局 submitter。

---

## 8.8 Durable Delegation Ledger:持久委派台账

### 8.8.1 是什么、为什么持久

旧版没有委派台账:父代理对"这轮 run 已经派过几次子代理、每次结果如何"只能靠上下文里的消息硬翻。新版的 **delegation ledger** 是 `ThreadState` 的 `delegations` 字段——一组结构化的 `DelegationEntry`,随 LangGraph checkpoint 一起按 `agent_storage.backend` **持久化**,并且(关键)在后续回合被渲染回模型上下文(§8.8.3)。

```python
class DelegationEntry(TypedDict):
    id: str                    # = provider tool_call_id
    run_id: NotRequired[str]   # 打上发起 run 的标签(per-run 预算计数用)
    description: str
    subagent_type: str
    status: str                # in_progress / 终态(completed/failed/cancelled/timed_out)
    result_brief: NotRequired[str]   # 确定性头尾截断(2000 字符上限,非 LLM 摘要)
    result_sha256: NotRequired[str]
    result_ref: NotRequired[str]     # 完整结果的 ToolMessage id
    stop_reason: NotRequired[str]
    receipt_verdict: NotRequired[dict]    # RFC #4651 PR2 引用核对结论
    acceptance_verdict: NotRequired[dict] # RFC #4651 PR4 验收清单结论
    created_at: str
```

### 8.8.2 确定性提取与归并

`agents/middlewares/delegation_ledger.py::extract_delegations(messages)` 从消息流里**确定性**枚举委派:

- 扫所有 `AIMessage.tool_calls`,凡名字是 `task` 的:以 `tool_call_id` 为 id 建 `in_progress` 条目(描述取 `description` 否则 `prompt`,截 200 字符);
- 再扫所有 `ToolMessage`,按 `tool_call_id` 配对,从 `additional_kwargs` 的 `subagent_status` 等结构化字段读终态并回填(status、stop_reason、receipt/acceptance verdict、result_brief+sha256+result_ref)。

归并器 `merge_delegations`(ThreadState reducer):同 id 以最新版本替换且保持首见顺序;`in_progress` 可被升级,**终态永不被非终态降级**;台账上限 50 条。这保证断点续跑(checkpoint 恢复)后父代理仍看得到历史委派。

持久化的时机与边界(durable-context 捕获):每次 `before_agent` 把**当前 run 边界内新产生/变化的条目**追加进 state——靠 runtime context 里的 `run_id` 与新消息边界识别"哪些是这次 run 的委派"(Gateway resume 路径不允许追加新 HumanMessage,worker 把 run 前 checkpoint 的消息 id 集暴露进 context 作为边界)。**历史 run 的委派不消耗新 run 的预算**(§8.8.4)。

### 8.8.3 回灌模型:台账进 durable context

`DurableContextMiddleware` 在每个回合把台账渲染成 `<durable_context_data>` 里的一节 `render_delegation_ledger(...)`(字符预算 6000,条目结果简报截 120 字符),让父代理持续看到"已派出哪些子代理、状态如何、为何结束(cap reason)、引用核对与验收结论如何"。渲染前会**重新校验**收据/验收 verdict 的形态,丢弃畸形值——持久化台账是"不可信 durable context",不能把脏值带进提示。citation 引用核对只发生在子代理上下文内(子代理里没有父台账),所以子代理链上 `receipts_render_mode="always"`,否则 Layer 1 会惰性失效。

### 8.8.4 双重限额:并发 + run 总预算

`SubagentLimitMiddleware`(仅装在主代理链上)用台账做两层硬性限制:

- **并发**:单次模型响应里 `task` 调用数超过 `max_concurrent`(min(请求值,进程容量,schema 1-64))时,保留前 N 个、**丢弃其余**,并替换 AIMessage(同 id 触发替换);
- **run 总预算**:默认 `max_total_per_run=6`(schema 1-50,运行时 `max_total_subagents` 覆盖并 clamp 到同范围)。中间件对台账里 `run_id == 当前 run` 的条目计数——**同一个 run 里反复用小批量绕过并发限制的企图被拦住**,而历史 run 的委派不占新 run 预算。

没有剩余额度时的行为:全部 `task` 调用被剥掉、同步 provider 原始 tool-call 元数据、强制 `finish_reason="stop"`,并在消息末尾追加可见的 "[SUBAGENT LIMIT REACHED] …" 说明,让代理用已收集的结果继续综合;同时 `runtime.context["stop_reason"] = "subagent_limit_capped"`(与 token/turn/loop cap 同一加性通道)。若 runtime 没有 `run_id`,中间件**fail-restrictive**:把线程全部历史委派都算作已用,并打警告。

显式 `batch_task` 走的持久批处理(durable batch 行、lease 式 `SubagentBatchService`)是独立预算域,**不消耗也不放宽**这条普通 run 台账:其 total/live/running 上限在 `subagent_batches` 配置下;批模式只由显式工具选择,从不按 prompt 大小推断。

---

## 8.9 验收检查:anti-self-report 三层防线

RFC #4651 的核心立场,直接写进了 `task` 工具 docstring 的第一句:

> **Reading the result (subagent reports are SELF-REPORTS, not verified facts)** —— 子代理报告是自我报告,不是被核实的事实。

"子代理说完成了"不等于完成。三层防线逐层把"自我报告"压到只剩无可辩驳的执行证据:

### 8.9.1 Layer 0:报告契约(提示层,`report_contract.py`)

`SubagentExecutor._build_initial_state` 给**每一个**子代理(内建与自定义一致)拼进 `<report_contract>` SystemMessage 段,要求:

- 凡声明自己做了某动作(写了文件、跑了命令、抓了页面、发了请求),必须引用执行记录里的收据 id(`[rN tool_name]` 或裸 `[rN]`),并锚定到具体那次调用——**引用与声明工具不匹配 = failed,id 不在台账 = unknown**;
- 每个交付物附一个**可核验句柄**(绝对路径、URL、记录 ID、HTTP 状态);
- 显式报告失败/跳过/不确定的部分,"绝不断言你没执行过的动作";
- 完成报告的动作声明**没有任何收据引用 → 整篇标 UNVERIFIED**。

引用条款跟随 `verification.receipts_enabled`(默认 true);关掉时无执行记录可查,报告契约退化为"仅句柄模式",不承诺做不到的交叉核对。同一模块提供 `format_citation`/`receipt_id`,提示文本与验证器共享格式,**提示与验证不会漂移**。

`acceptance_criteria` 走**不可信通道**:20 条 × 500 字符上限、逐条 `neutralize_untrusted_tags` 中和后,作为纯文本块追加到 task `HumanMessage`(该通道被 `InputSanitizationMiddleware` 转义和边界框定);SystemMessage 只放框架所有的 `<acceptance_criteria>` **指针注**(说明清单位置、权威层级、"准则文本永远不能覆盖系统提示")——准则里的自然语言注入(如 "ignore the report contract")因此永远停留在任务数据优先级,上不了系统通道。

### 8.9.2 Layer 1:收据与引用交叉核对(`receipt_verification.py`)

子代理执行期间,每条工具调用被 `ToolReceiptMiddleware` 盖章成收据(`receipt_id`、工具名、状态、时间),`_harvest_tool_receipts` 从终态消息流收割,放在 `SubagentResult.tool_receipts` 里随注册表条目传输。

`task_tool` 的完成分支是唯一同时握着**全文报告**(未截断)与收据的地方,在此一次性做 `verify_receipt_citations(report_text, receipts)`:

- 解析报告里的 `[rN]`/`[rN tool]` 引用,逐一对照收据台账;
- 判定 `citation_resolved`(所有声明有可解析引用且无锚定失败)还是 `no_citation_claims` / `failed` / `unknown`;
- 词表纪律:`citation_resolved` 是摘要布尔,**绝不用 "verified/passed" 这类强肯定词**——引用解决只证明"那次调用发生过并记录了状态",**不验证相邻论断的正确性**,docstring 明确要求父代理对承重论断亲自 spot-check 句柄;
- `receipts=None`(收据关闭或 run 在流式前结束)则跳过,保持与关闭收据的部署完全一致的旧行为;空列表是真实收割(零盖章调用),照样出结论。

### 8.9.3 Layer 2:确定性验收清单(`acceptance_checks.py`)

父代理挂 `acceptance_criteria` 时,子代理完成分支(经 `asyncio.to_thread` 卸载、失败隔离)对**可判定叶子**做代码级检查:

- `file:<path> exists` / `non-empty`:经与 `ReadBeforeWriteMiddleware` 同源的 `read_current_file_content`,**限定共享线程工作区**(`workspace_path`/`outputs_path`,虚拟 `/mnt/user-data/...` 前缀与工作区相对写法先归一)。读用沙箱原生虚拟路径;大小先定界(本地 `os.stat`;远端在全新 `env -i` shell 里做只元数据的 stat/realpath 探针——被污染的持久 shell 会话状态(函数/别名/PATH/locale)无法左右它,`stat` 不开内容所以 FIFO 阻塞不了父代理,必须是常规非符号链接文件,realpath 必须落在挂载根的 realpath 之下,末段符号链接直接拒绝);
- `file_written:<path>`:存在性 + 读回的类型化声明绑定;
- `tests_passed:<command>`:必须锚定到**某条具体的已记录 bash 执行**(executor 从流式 chunk 累积进 `bash_executions`,按 `tool_call_id` 归并、有界缓存,摘要压缩旧消息也抹不掉)且状态成功、输出尾带测试汇总形态;命令匹配做 shell 结构解析(操作符分段、可执行名、参数),`echo` 参数里"提了一句"不能锚定;
- 其余任何措辞:**`checked=False`,渲染 UNVERIFIED,绝不静默通过**。

词表分层是硬纪律:叶子布尔是 **`checked`/`holds`**,永远不是 `satisfied`/`verified`/`passed`——强肯定词只留给未来真正的运行时硬门,模型永远不会把"确定性执行证据"和"任务验收"混为一谈。渲染出的清单拼在模型可见的结果文本里,结构化 verdict 走 metadata 进台账;`holds` 叶子是执行证据,不担保交付物内容正确。

三层合起来的效果:子代理的最终报告在父上下文里带着"引用核对结论 + 验收清单结论"出现,自称"我写好了 report.md"旁边就贴着 `[r3 write_file]` 的引用核对结果和 `file:../outputs/report.md non-empty → holds` 的代码级检查结果。

---

## 8.10 与父线程的隔离边界

子代理不是父代理的克隆:它在**共享**(沙箱文件域、thread_id、身份)与**隔离**(上下文、记忆、消息流、生命周期)之间有七条明确的线。

### 8.10.1 中间件链:共享底座 + 子代理专属收尾

`build_subagent_runtime_middlewares`(在 `agents/middlewares/tool_error_handling_middleware.py`)复用主代理共享底座(`_build_runtime_middlewares` 的子集):InputSanitization → ToolOutputBudget → ToolResultSanitization → ThreadData → Sandbox → DanglingToolCallPatch → LLMErrorHandling → Guardrail/Authorization → SandboxAudit → SkillActivation → SkillToolPolicy 等;随后追加子代理专属件:ViewImage(模型支持时)、MCP 路由/延迟工具过滤、`LoopDetectionMiddleware`、`TokenBudgetMiddleware`(按 `agent_name` 解析逐代理 token_budget)、`SubagentDateContextMiddleware`(#4781,一次性注入框架所有的 `<current_date>`,不读记忆配置、不继承主代理冻结对话/午夜生命周期)、`DurableContextMiddleware`(skip_memory_flush=True,§8.10.3)、可选的 summarization 件、最内层 `SystemMessageCoalescingMiddleware`(把多个 SystemMessage 合并成一个领头的——严格 provider 不接受"assistant-first"或重复 system 消息)。**不装** `SubagentLimitMiddleware` 与 lead-only 件——子代理没有 `task`,不需要限额;任务卡也解释过原因。

### 8.10.2 沙箱:共享文件域,独立执行租约

- **共享**:executor 从父 runtime state 接收 `sandbox_state`/`thread_data`,子代理在**同一个线程工作区**里干活(虚拟路径 `/mnt/user-data/{uploads,workspace,outputs}`),验收检查也按这个共享域判 `file:` 叶子;上传不共享(独立 ThreadState 无 `uploaded_files`);
- **独立执行租约**(#5128):每个被准入的子代理 run 带任务派生的 `sandbox_lease_owner_id = "subagent:{task_id}"` 与同值 `sandbox_command_scope_id`。沙箱中间件把这次执行保留在**父线程的活动 provider client** 上——一个子代理收尾不能关掉沙箱而兄弟姐妹还在跑;最后一个持有者才做 pending 的 provider 释放。AIO 沙箱下,命令作用域为每个子代理选一条**显式持久 shell 会话**,不同作用域可并发,单子代理内 shell 状态保序。同步沙箱工具体经 `asyncio.to_thread` 卸载时被 shield 并在重复取消间排干,被取消的 worker 既不能重新接纳已释放的 owner,也不能在子代理终态化之后继续跑。中间件做正常释放,`SubagentExecutor` 在 `finally` 里幂等补一次——异常、协作取消、超时展开路径都漏不掉租约或会话。

### 8.10.3 记忆:不污染父线程的持久记忆

主代理开记忆时,`memory_flush_hook` 会把压缩前的消息冲刷进以 `thread_id` 为键的持久记忆。子代理与父**共享 `thread_id`**,若不跳过,子代理内部回合会污染父线程的持久记忆——所以子代理的 durable-context 装配以 **`skip_memory_flush=True`** 调用。压缩产生的摘要写进子代理**自己的** `ThreadState.summary_text`(不占消息流),由 durable-context wrapper 作为受保护的人话数据投影进下一次请求;deep 子代理(`max_turns=150`)keep 策略只留 assistant 工具调用+工具结果时,没有这段注入,严格 provider 会拒绝以 assistant/tool 历史开头的请求。

### 8.10.4 消息流与 checkpoint:继承坐标系,不继承线程

- 子代理图**不带 checkpointer**、一次性不续跑(§8.4.3);
- `run_config` 里故意省略 checkpoint 坐标键,靠拷贝的父 ContextVar 继承**非根子图命名空间**——这是 stream 隔离的契约:子代理的 `values` 快照如果冒充裸根帧发布,SDK 客户端会看到整条线程视图被替换(#4399);根级消费者(文件块批处理、子代理事件持久化、LLM 错误回退检测)忽略带命名空间的帧;
- 子代理的业务上下文(thread_id 等)走 `runtime.context`,这条路径与坐标继承互不干扰;
- 初始状态只有一条合并的 SystemMessage(角色提示 + 报告契约 + 技能索引/延迟工具提示)+ 一条 task HumanMessage(+ 验收准则)——**不继承父消息历史**,这正是"上下文隔离收益"的机制来源。

### 8.10.5 技能:父身份解析,父策略收敛

子代理按配置装载技能,存储解析走 `get_or_new_user_skill_storage(user_id)`(父 runtime 身份;无身份才回退 `DEFAULT_USER_ID`),让自定义技能遮蔽/可见性与主代理一致;可用技能集合再与父策略求交(§8.2.2)。技能正文与 allowed-tools 声明在激活/加载后才生效(`SkillActivationMiddleware` + `SkillToolPolicyMiddleware`),与主代理同一套激活+策略对。

### 8.10.6 日志与追踪:短 trace_id 标签 + 共享 deerflow_trace_id

子代理日志用自己 8 字符的 `trace_id` 前缀(`[trace=...] Subagent {name} ...`),与父 run 日志通过同一个 `deerflow_trace_id`(请求级,进 Langfuse 元数据)连成树;token 归因从消息状态走,不设进程全局缓存(§8.6)。父 run 的 `RunJournal` 是 `deerflow_loop_bound` 的,**不跨隔离 loop**;loop 检测审计与终态 usage 经 §8.3.3 的窄通道回父 loop。被调度器复用的线程绝不带上次的 trace 绑定——绑定是每次 run 一个作用域。

### 8.10.7 提示通道:谁的话进哪个通道

最后是信息论意义上的隔离:**框架权威文本**(报告契约、验收准则指针、系统指令)只走系统通道;**模型/用户可影响的文本**(委派 prompt、验收准则值、工具结果)只走不可信通道(task HumanMessage,被转义与边界框定)。准则里的自然语言注入永远不能获得系统通道优先级——这既是验收检查能成立的前提,也是"子代理无法被准则文本越权改写自身系统提示"的保证。

---

## 8.11 配置与文件速查

```yaml
# config.yaml(相关段示意,精确 schema 见 config/ 下各模块)
subagents:
  timeout_seconds: 1800        # 内建子代理默认超时(30 分钟)
  max_turns: null              # 内建 max_turns 全局覆盖(默认 general-purpose=150, bash=60)
  max_total_per_run: 6         # run 总委派预算(schema 1-50)
  token_budget: {...}          # max_tokens 默认与 summarization.enabled 耦合(1M/2M),warn 0.7,硬停 1.0
  custom_agents:               # 自定义类型:name → {description, system_prompt, tools, skills, model, max_turns, timeout_seconds}
    ...
  agents:                      # 逐代理覆盖:timeout/max_turns/model/skills
    general-purpose: {...}
subagent_runtime:              # startup-only 进程容量
  max_running: 3               # 并发上限(1-64)
  max_queued: 64
  admission_policy: queue      # queue | reject
  queue_timeout_seconds: 300
verification:
  receipts_enabled: true       # 开关 Layer 0/1(收据引用契约 + 交叉核对)
```

| 关注点 | 文件 |
|--------|------|
| 执行器 / 结果 / 后台注册表 / 隔离 loop | `subagents/executor.py` |
| 准入控制 | `subagents/capacity.py` |
| 类型注册与解析 | `subagents/registry.py`、`subagents/builtins/`、`persistence/managed_subagents.py` |
| 线缆契约 | `subagents/status_contract.py`(+`contracts/subagent_status_contract.json`) |
| 委派台账 | `agents/middlewares/delegation_ledger.py`、`agents/thread_state.py` |
| 限额中间件 | `agents/middlewares/subagent_limit_middleware.py` |
| 报告契约 / 验收 | `subagents/report_contract.py`、`subagents/acceptance_checks.py`、`agents/middlewares/receipt_verification.py`、`agents/middlewares/tool_receipt.py` |
| 工具入口 / 批处理 | `tools/builtins/task_tool.py`、`tools/builtins/batch_task_tool.py`、`subagents/batch_service.py` |
| 子代理中间件装配 | `agents/middlewares/tool_error_handling_middleware.py::build_subagent_runtime_middlewares` |
| 事件持久化 | `runtime/runs/worker.py::_SubagentEventBuffer`、`subagents/step_events.py` |
| 直接 SDK 运行时 | `subagents/runtime.py` |

测试锚点(改动这些语义时必须回归):`tests/test_subagent_executor.py`(含 `TestSubagentCheckpointLineage`)、`tests/test_subagent_routing_prompt.py`、`tests/test_subagent_prompt_security.py`、`tests/test_lead_agent_prompt.py`、`tests/test_worker_trace_binding.py` 与各 `test_subagent_*` 契约测试。

---

## 8.12 小结

- **委派是优化**:提示、工具、角色三处口径一致,只允许"并行延迟 / 专长 / 上下文隔离"三类收益明显盖过成本的委派,依赖与共享状态是硬否决。
- **执行是异步注册 + 轮询**:服务器 `execution_id`(uuid4)注册 PENDING 进进程注册表,协程跑在**持久隔离事件循环**上,受进程级 FIFO 准入控制;父侧每 5 秒轮询,步骤经 `task_running` 事件流出,终结后清理。
- **身份刻意拆分**:provider `tool_call_id`(external)管关联,服务器 uuid 管所有权——因为 provider ID 跨父 run 可重复。
- **台账持久且回灌**:delegation ledger 随 checkpoint 持久、每回合渲染回模型,支撑 per-run 总预算的代码级强制。
- **报告按自报对待**:报告契约要求引用收据 + 可核验句柄,完成分支做引用交叉核对与确定性验收清单;词表纪律保证"执行证据"永远不等于"任务验收"。
- **隔离有七条线**:中间件链、沙箱租约、记忆冲刷、消息流/checkpoint、技能、日志、提示通道——子代理在共享文件域里干活,却碰不到父的消息历史、持久记忆与生命周期。
