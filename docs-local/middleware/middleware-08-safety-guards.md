# 安全守卫与终止性中间件(深度教学)

本文深度拆解 Lead Agent(及子 Agent)运行时中负责**终止性(termination)**与**收尾语义**的七个中间件, 即链位置 27–35 的 `SubagentLimitMiddleware`、`LoopDetectionMiddleware`、`TokenBudgetMiddleware`、`TerminalResponseMiddleware`、`ModelLengthFinishReasonMiddleware`、`SafetyFinishReasonMiddleware`、`ClarificationMiddleware`, 以及支撑它们的辅助模块(`safety_termination_detectors.py`、`model_length_termination_detectors.py`、`_bounded_dict.py`、`tool_call_metadata.py`)。

本文是 `docs-local/harness-strategies-agent-execution.md` §9.1「终止性:三层护栏」的逐文件展开。阅读前提:先弄懂两件事——**链上顺序即正确性**(`agents/middlewares/AGENTS.md` "Middleware Chain" 章节),以及 **after_model 钩子的反向分派**(§0.3,那是理解 32→35 收尾顺序的钥匙)。正文用真实 issue 号标注"坑"的出处(#3875 / #4176 / #3028 / #4393 / #4271 / #4027),便于追溯动机。

---

## 0. 终止性:三层护栏(总览)

> 先划清概念:本节讲的是**终止**——把"失控 / 半途而废 / 空转"的 run 掐断或如实收尾。"把 agent 拉回正轨再继续"的**纠偏**
> (ToolProgress 提示、上下文重锚定等)在 harness-strategies §9.7,与终止是两类问题:**纠偏是概率性的行为引导,终止才是硬保证。**

### 0.1 三个威胁面,三层护栏

一个 run 可能以三种坏方式"结束",每层护栏对应一种:

| 层 | 中间件(链位) | 探测什么 | 触发时机 | 行为 |
|---|---|---|---|---|
| ① 主动硬停:调用模式 | `LoopDetectionMiddleware`(28) | 模型**重复发出同一组 tool_calls** / 单工具调用频率爆炸 | 每次模型响应后(after_model) | 先 warn 注入;超硬限则**剥离 tool_calls 强制终答**,`stop_reason=loop_capped` |
| ① 主动硬停:预算 | `TokenBudgetMiddleware`(29) | 单 run token 用量(输入/输出/总量)超阈值 | 每次模型响应后 | warn 注入;超硬限剥 tool_calls 强制终答,`token_capped` |
| ② 终止语义:识别 provider 终止信号 | `SafetyFinishReasonMiddleware`(34) + `ModelLengthFinishReasonMiddleware`(33) | provider 已用 `finish_reason`/`stop_reason` 声明"安全截断 / 长度截断" | after_model,反向分派中先于 ① 看到原始响应 | Safety 剥残缺 tool_calls / 回填空响应,`safety_capped`;ModelLength 纯记账,`model_length_capped`;都不抛异常、保留真实原因 |
| ③ 兜底:什么都没发生 | `TerminalResponseMiddleware`(32) | 工具执行后模型返回**空终态 AIMessage**(无文本、无工具意图) | after_model(收尾段最后触发) | 隐藏恢复提示重试一次;仍空 → 可见错误 fallback 标记,run 以 **error** 结束而非"静默成功" |

- **① 主动掐断**:系统自己判断"再这样下去没完了"(重复调用、超预算),不等 provider,自己剥 tool_calls 让 run 自然产出终答,**不抛异常**——抛异常会让子代理执行器/worker 看到 raw 崩溃,"带着被迫终答结束"才是可控的。
- **② 如实记账 + 防残骸**:provider 中途给出终止信号而响应本身是残的(半截 tool_calls、或全空)。系统不重试、不改写,只**防止残骸被当成干净的、可执行的成功**。
- **③ 最后防线**:①② 都没触发、provider 也没给信号,但 turn 就是空手而归——绝不允许"看似成功、实则什么都没说"。

### 0.2 链位置:27–35 与自定义/扩展插入点

Lead 链由两段拼成:共享基础段(1–26,`tool_error_handling_middleware.py::_build_runtime_middlewares` → `build_lead_runtime_middlewares`,子代理经 `build_subagent_runtime_middlewares` 复用大部分),再在 `lead_agent/agent.py::build_middlewares` 追加 lead-only 段:

```
26 SystemMessageCoalescing
27 SubagentLimitMiddleware        (可选, subagent_enabled)
28 LoopDetectionMiddleware        (可选, loop_detection.enabled)
29 TokenBudgetMiddleware          (可选, token_budget.enabled)
30 自定义中间件 custom_middlewares      ← 程序化注入点
31 扩展中间件 extensions.middlewares   ← config 声明注入点(reflection resolve_class)
32 TerminalResponseMiddleware         ← 收尾段开始(语义上不可被扩展改写)
33 ModelLengthFinishReasonMiddleware
34 SafetyFinishReasonMiddleware    (可选, safety_finish_reason.enabled,默认开)
35 ClarificationMiddleware         ← 必须最后
```

30/31 两个插入点的含义:

- 用户中间件只能插在 30(代码显式传 `custom_middlewares`)或 31(config.yaml / extensions_config.json 的 `module.path:ClassName`,经 `deerflow.reflection.resolve_class` 加载,缺失/非法在装配期 loud fail;该路径实例化任意代码,视为可信操作者输入)。
- 它们**必须落在 Loop/Token 两个守卫之后、收尾段 32–35 之前**:若在 Loop/Token 之前,其 after_model 会先看到"尚未被剥 tool_calls"的失控响应;收尾段 35 之后**没有任何注册位**,Clarification 的 `Command(goto=END)` 中断语义因此不可被扩展绕过。subagent 收到同一份扩展中间件列表,同样置于其 safety tail 之前。
- ### 0.3 after_model 反向分派:收尾顺序为什么是 TerminalResponse → ModelLength → Safety → Clarification

LangChain factory 把 with-after_model 的中间件按**注册列表逆序**接边(`add_edge("model", middleware_w_after_model[-1])` 后从后往前走):
**最后注册的中间件最先看到模型输出**,其 state 更新先落地,后面的中间件看到的是已更新的 state。注册顺序 32→33→34→35,所以 after_model 的**实际动作顺序**是:

```
模型输出 AIMessage
   ▼ (35 Clarification 最先看到原始输出)
 35 Clarification   若 ask_clarification 与 sibling 同批 → 先剥 sibling,
                    只留澄清调用交给工具路由                    (§7)
   ▼ (34 Safety 第二个)
 34 SafetyFinish    安全终止且带 tool_calls → 剥离;空内容 → 回填说明
                    (此时 Clarification 已剥过 sibling)          (§6)
   ▼ (33 ModelLength 第三个)
 33 ModelLength     终态 + 可见内容 + 长度信号 → 记 stop_reason, 不改消息 (§5)
   ▼ (32 TerminalResponse 最后)
 32 TerminalResponse此时检查"是否空终态": Safety 已回填 → 放行不误重试;
                    真仍空 → RemoveMessage + jump_to=model 重试一次  (§4)
   ▼
 …(29 Token / 28 Loop / 27 Limit / 30-31 自定义·扩展 最后跑, 基于已清理的消息)
```

这个顺序是**正确性**,不是实现细节:

1. **Clarification 最先剥 sibling**:若它最后跑,同批 `bash`/`write_file` 已作为合法 tool_calls 进入工具路由——用户还没回答它们就先执行;且 langchain 的 `return_direct` 路由要求"末条 AIMessage 的全部 tool_calls 都是 return_direct"才走 END,混合批次会绕回模型。
2. **Safety 先于 Loop/Token**:两者共享同一套剥离机制但触发不同(见 `safety_finish_reason_middleware.py` 模块 docstring)。Safety 先清掉安全截断带来的**残缺 tool_calls**,Loop 再基于**清理后**的消息计数——被过滤的半截 `write_file` 不会数进死循环哈希;TokenBudget 按 message id 差值计 usage,与内容改写无关,不受影响。
3. **Safety 先于 TerminalResponse(最微妙的一条)**:Safety 的"空响应回填"(#4393)把安全拒绝产生的空 assistant 消息补成可见说明。若 Terminal 先跑,会先 `RemoveMessage+jump_to=model` 去重试一个"因安全而故意空"的响应;回填后 Terminal 看到有内容即放行,run 干净结束。
4. **ModelLength 只记账不改写**,只要保证在 Terminal 判定空态之前已跑即可;放 33 让"安全→长度→空态"的语义顺序自然。
5. **Clarification 必须最后注册,还因它的 wrap_tool_call 短路**:拦截 `ask_clarification` 后 handler 根本不执行,直接 `Command(goto=END)` 中断;后置任何 wrap 层都可能在这条中断路径上再插入执行,破坏"暂停等用户"的原子性。

### 0.4 加法 `stop_reason`:给 run 的"死因"记账

所有提前结束都要能被调用方**结构性读出**,而非解析结果文本。设计是**加法字段**,两处落点:

```
runtime.context["stop_reason"]   ← 落点一:worker 直接读(不需要中间件实例引用, #4176)
中间件实例._stop_reason[run_id]  ← 落点二:BoundedDict, 经 consume_stop_reason(run_id) pop
```

| 值 | 谁写 | 语义 |
|---|---|---|
| `subagent_limit_capped` | SubagentLimit | 本 run 子代理总委派额度耗尽,多余 task 被剥 |
| `loop_capped` | LoopDetection | 重复工具调用超硬限,强制终答 |
| `token_capped` | TokenBudget | 单 run token 超硬限,强制终答 |
| `safety_capped` | SafetyFinishReason | provider 安全终止,工具调用被抑制 |
| `model_length_capped` | ModelLengthFinishReason | provider 长度截断(只记,不改) |

- **为什么是加法**:`stop_reason` 字段最初不存在,逐个守卫按需加字符串即可,无需维护一个"所有结束原因"的中央枚举;下游(worker、子代理执行器、ledger、UI)字符串比对,未知值天然放行。代码注释反复强调这使 capped 完成可与"干净完成"区分(#3875 Phase 2)。
- **子代理链路**:`SubagentExecutor` 用 `hasattr(m, "consume_stop_reason")` 收集所有守卫(鸭子类型),run 返回后逐个 pop,把 `loop_capped`/`token_capped` **带回给 lead 的 ledger**(`subagents/executor.py::_consume_guard_stop_reason`)。
- **BoundedDict(1000) + pop 消费**(`_bounded_dict.py`,一个插入即淘汰最老项的 OrderedDict):lead 的守卫实例**跨很多 run 常驻**,无上限会逐 run 泄漏;锁内写防并发 Gateway 线程撕裂。
- **run_id 按"存在性"取值**(`_get_run_id`):嵌入式/CLI 分派的子代理 run_id 合法为 `None`(键在、值 None)。若用真值判断会把 None 塌缩成 `"default"` 占位,导致硬停写进 `"default"`、执行器按 `None` 查询,**静默丢失 loop_capped**。两处守卫都 `"run_id" in ctx` 判存在,查不到退回 `str(id(runtime))`。

### 0.5 三个贯穿全章的模式

1. **警告延迟注入**:检测在 after_model,但**绝不**在那里插消息——工具节点还没跑,插入会落在 assistant(tool_calls) 与其 ToolMessage 响应**之间**,OpenAI/Moonshot 拒收(`tool_call_ids did not have response messages`),Anthropic 禁 mid-stream SystemMessage;也不能改写 AIMessage 把框架词塞进模型嘴里(污染 Memory 消费者)。正确姿势:after_model 只**入队**(按 thread/run 键),`wrap_model_call` 时以 HumanMessage **追加在全部消息之后**(此时上轮 ToolMessage 已齐,配对完整)。
2. **硬停三件套(不 raise)**:剥 `tool_calls` → 同步清 `additional_kwargs` 的 raw `tool_calls`/`function_call` → `response_metadata.finish_reason` 由 `tool_calls` 改 `"stop"` → content 后追加可见强制终答文本。改写后 AIMessage 不再要求配对 ToolMessage,run 自然走完收尾,调用方看到"正常完成"而非崩溃。
3. **审计 best-effort**:RunJournal `record_middleware` 全程 try/except,失败只 warning 绝不打断 run;记录**刻意不含工具参数、消息正文、参数哈希**(那正是被审计/被过滤的内容本身)。

---

## 1. SubagentLimitMiddleware——子代理委派限额(链 27)

文件:`agents/middlewares/subagent_limit_middleware.py`(181 行)

### 它解决什么问题

- **单次响应并发失控**:一次响应生成 20 个 `task` 调用,执行容量只有 3,其余排队到超时。
- **分批绕过并发限制**:单次并发限 3,lead 却能在 planning checkpoint 反复"再派 3 个",一个 run 无限分批。
- prompt 层限制不可靠(模型会忽略),需要**执行层硬截断**。

### 钩子与执行时机 / 链位置

- 只实现 `after_model`/`aafter_model`(趁 tool_calls 还在 AIMessage 上、工具执行前)。
- 链位置 27,**可选**(`subagent_enabled` 才装配)。
- `max_concurrent`:装配前已解析为 `min(per-run 请求, 启动冻结的 subagent_runtime.max_running, 安全上界 1–64)`,并与 lead prompt 共享同一值(热更新不得让任何一层宣传超过已建进程控制器);构造函数再 `_clamp_subagent_limit` 兜底到 [1,64]。
- `max_total`:默认 `DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN=6`,clamp [1,50];运行时 `max_total_subagents` 可覆盖,同范围。

### 内部实现逻辑

```python
def _truncate_task_calls(state, runtime):
    last = state.messages[-1]
    if last.type != "ai" or not last.tool_calls: return None
    task_idx = [i for i,tc in enumerate(last.tool_calls) if tc["name"]=="task"]
    if not task_idx: return None

    run_id = runtime.context.get("run_id")            # 可能为 None
    if run_id is None:
        warn("no run_id; counting ALL thread delegations as prior usage")  # fail-restrictive

    prior = count_prior_delegations(state["delegations"], run_id=run_id)  # 当前 run 账本条数(id 去重)
    remaining_total = max(0, self.max_total - prior)
    allowed = min(self.max_concurrent, remaining_total)   # 双维取小, 先到的约束生效

    if len(task_idx) <= allowed: return None             # 未超限: 零干预

    drop = task_idx[allowed:]                            # 截掉多出来的 task
    kept  = [tc for i,tc in enumerate(last.tool_calls) if i not in set(drop)]

    if remaining_total == 0:                             # 只有总预算耗尽才是"终点"
        runtime.context["stop_reason"] = "subagent_limit_capped"
        content = append_text(last.content, _TOTAL_LIMIT_STOP_MSG)
    else:
        content = None                                   # 只剥并发超额, 不打扰内容
    return {"messages": [clone_ai_message_with_tool_calls(last, kept, content=content)]}
```

关键事实:

- **账本按 run_id tag**:委派 ledger 条目捕获时带 `run_id`。同一 run 反复经过 planning checkpoint,`prior` 持续累计——**分批绕过失效**;同一 thread 的**后续 user turn 是全新 run**,预算重置(fresh run budget)。"总量限制落在 run 粒度而非 thread 粒度"正是设计语义。
- **fail-restrictive**:run_id 缺失(自定义图集成未传)→ 按该线程**全部历史委派**计数,宁可更紧,并打 warning。
- **只对普通 `task` 计数**:显式 durable `batch_task` 是独立模式,有自己持久化的 total/live/running 限制,不被改写成普通 ledger 条目、也不消耗普通 run 额度。
- **两种截断的面孔**:额度未耗尽只是"少派几个",模型带保留调用继续;`remaining_total==0` 时追加可见告警(`[SUBAGENT LIMIT REACHED]`,提示用已有结果/直接执行/总结,别再委派)并 stamp `subagent_limit_capped`。
- 截断由 `clone_ai_message_with_tool_calls` 同步清理 raw provider tool-call 元数据并把 `finish_reason` 改为 `"stop"`(无保留 tool_calls 时)。

### ASCII 流程图

```
after_model(last AIMessage)
   │
   ├─ 非 ai / 无 tool_calls / 无 task ─────────────► 放行(None)
   ▼
run_id; prior = 当前 run 账本条数(无 run_id → 全线程历史, 更紧)
remaining_total = max(0, max_total - prior); allowed = min(max_concurrent, remaining_total)
   │
   ├─ len(task) <= allowed ────────────────────────► 放行(None)
   ▼
截掉 task_idx[allowed:] 之后的 task
   ├─ remaining_total == 0 ─► stamp subagent_limit_capped
   │                          + content 追加 [SUBAGENT LIMIT REACHED] 可见告警
   ▼
clone(同 id 替换) → {"messages":[…]} → 模型带剩余调用继续 / 被迫终答
```

### 与邻居的关系

- 位于收尾段前最早的 lead-only 守卫;剥掉 task 后,LoopDetection(28) 计数的 tool_calls 已不含被截断的 task,TokenBudget(29) 也不会为从未执行的调用计 token。
- 被截断的 task 从未执行 → 无 receipt、不进 ledger;账本只反映**真实捕获**的委派,`_count_prior_delegations` 按 delegation id 去重,防同一委派在状态多帧里被重复计。
### 设计权衡

- **并发/总量双维取 `min`**:单次响应既不能超并发(容量),也不能让 run 累计破总量;先到者生效,语义无歧义。
- **同消息 id 重建替换** vs 删除重发:直接改末条 AIMessage,避免"删了再让模型重出"的额外调用;同 id 走 reducer 替换路径,历史不留"幽灵 task"。
- **总预算耗尽才带告警文本**:并发截断是常规整形(模型还要继续干活),塞可见告警污染上下文;无额度可派、需要模型改变策略时才注入说明。
- **总量放 ledger 而非中间件内存**:checkpoint/恢复后账本仍在,跨节点一致;代价是每次 after_model 扫一次有界 ledger。

### 源码阅读指引

`subagent_limit_middleware.py`:重点读 `_truncate_task_calls`(双维计算)、`_count_prior_delegations`(run_id 过滤 + id 去重)、`_TOTAL_LIMIT_STOP_MSG`。
---

## 2. LoopDetectionMiddleware——死循环防护:调用模式层(链 28)

文件:`agents/middlewares/loop_detection_middleware.py`(842 行,七个里最复杂)

### 它解决什么问题

P0 安全:模型陷入"调工具→看结果→再调同一工具"的死循环直到 recursion limit 杀 run。检测是**调用模式**维度:不看工具结果内容,只看模型发出的 **tool_calls 签名**。

### 钩子与执行时机 / 链位置

- `after_model`/`aafter_model`:检测 + 入队警告或硬停。
- `wrap_model_call`/`awrap_model_call`:把入队警告追加到下一次请求消息尾部。
- `before_agent`:清本线程**其他 run** 遗留的 pending 警告;`after_agent`:清**本 run** 残余 pending 警告(警告是瞬态的,run 结束未发出即弃,不跨调用残留)。
- 链位置 28,**可选**(`loop_detection.enabled`)。**跨 run 保留 `_history`**(调用模式时间不变;与 ToolProgress 的"每 run 清空"故意相反,§0.1 注)。

### 内部实现逻辑

**双层检测**(`_track_and_check` → `_LoopDecision{action: warn|hard_stop, detection_layer: identical_call_set|tool_frequency, tool_names, count, threshold}`):

- **Layer 1 哈希层——相同调用集**:每条 tool_calls 归一出稳定 key,`_hash_tool_calls` 对 (name,key) 列表**排序后** md5[:12]——顺序无关,同一组调用换顺序仍是同一哈希。默认 `warn_threshold=3`、`hard_limit=5`;每线程滑窗 `window_size=20`,LRU 上限 100 线程。
- **Layer 2 频率层——同工具类型、换参数**:单工具名在滑窗内频率,默认 warn 30 / hard 50,可 `tool_freq_overrides` per-tool 覆写(如 batch 流水线放开 `bash`)。专治"换参数连续 read_file 40 个文件"这类哈希层抓不到的漂移循环。

**工具感知的稳定 key**(`_stable_tool_key`,防误判):

```python
read_file         → "path:{start//200}-{end//200}"    # 200 行分桶: 翻页读不算重复
write_file/str_replace → json.dumps(全参数)           # 内容敏感: 同一路径迭代写入,
                        # 若只取 path, 不同 payload 的合法迭代会被折叠成"重复"误停
其余工具          → 抽关键字段 path/url/query/command/pattern/glob/cmd
                        # 无关键字段 → provider 给的 fallback key, 或全参数 JSON
```

**频率窗口尺寸的隐蔽约束**:Layer 2 的 deque 长度 `_tool_freq_window = max(window_size, tool_freq_hard_limit, 各 override 的 hard)` —— **窗口必须 ≥ 任何生效 hard**,否则计数到窗口长就随 popleft 衰减,紧 burst 永远到不了硬停(窗口 20 < hard 50 ⇒ 硬停分支是死代码)。warn 阈值不参与:合理配置 warn ≤ hard 已被覆盖,warn > hard 的错配会先硬停,无害。配套 Counter 镜像 deque(append 自增、popleft 自减),频率查询 O(1)——单个 override 把窗口撑到 1000 时,全扫窗口每次 O(1000)。

**警告路径**:首现才告警——`_warned[thread]` 记录已告警哈希,计数衰减出窗口即清除、可再告警;频率层 `_tool_freq_warned` 同构。警告入队 `_pending_warnings[(thread,run)]`(每 run 上限 4 条),下一次 `wrap_model_call` 以 `HumanMessage(name="loop_warning")` 追加消息末尾(§0.5 模式 1)。

**硬停路径**(与 TokenBudget 对称,#3875 Phase 2):

```python
# 1) stamp stop_reason 两处
self._stop_reason[run_id] = "loop_capped"      # 实例内 BoundedDict(锁内写)
runtime.context["stop_reason"] = "loop_capped" # worker 可直接读
# 2) 剥 tool_calls + 清 raw metadata + finish_reason→stop + 追加硬停文本
stripped = last.model_copy(update=_build_hard_stop_update(last, content))
return {"messages": [stripped]}                # 3) 替换消息, run 自然终止, 不 raise
```

**审计**(`_record_audit_event`):持久化 `middleware:loop_detection`,只在**状态转移**时记(每哈希首 warn、每工具频率首 warn、每次硬停),带 `is_subagent`/`agent_id`/`detection_layer`/`tool_names`/`count`/`threshold`,**无参数/正文/结果/参数哈希**。recorder 从 `runtime.context[LOOP_DETECTION_RECORDER_CONTEXT_KEY]` 取——普通 task 子代理只收到窄的 loop-safe 记录代理,forward 到父 run journal,**绝不把 loop-bound 的 RunJournal 塞进子代理的独立事件循环**;无则退回 `__run_journal`。

### ASCII 状态机

```
after_model: 取 last AIMessage 的 tool_calls
  │
  ├─ 无 tool_calls ───────────────► 放行
  ▼ Layer 1: call_hash 入本线程滑窗(20)
  ├─ count >= hard(5) ───────────► 硬停: 剥调用+清raw+finish→stop+文本
  │                                 stamp loop_capped(两处) + 审计(转移才记)
  ├─ count >= warn(3) 且 首次 ────► 入队警告 → wrap_model_call 注入
  ▼ Layer 2: 逐工具名入频率 deque(≥最大hard) + Counter O(1)
  ├─ freq >= eff_hard ────────────► 硬停(文案指名工具名与次数)
  ├─ freq >= eff_warn 且 未告警 ──► 入队警告; 计数衰减 < warn 后可再告警
  └─ 未触发 ──────────────────────► 放行
```

### 与邻居的关系

- **与 ToolProgressMiddleware(12)分工**:调用模式 vs 结果质量——Loop 在模型响应后看 tool_calls 签名、硬停整轮;ToolProgress 在工具执行后按 (thread,tool) 状态机只封"不产生新信息"的单工具。两者可同时注入提示、互不读对方状态;Loop 硬停后 wrap_tool_call 不再发出,ToolProgress 自然不触发,**无双停**(harness-strategies §9.7.3)。
- **与 SafetyFinishReason(34)**:共享 `clone_ai_message_with_tool_calls` 剥离机制但触发不同;注册序 Safety 在 Loop 后 → 反向分派 Safety 先清理、Loop 后计数(§0.3 第 2 条)。
### 设计权衡

- **加法 stop_reason 不改协议**:`loop_capped` 只是一个字符串,执行器 pop 即可区分"loop-capped completion"与"干净完成",无需给消息结构加字段(§0.4)。
- **硬停清 raw metadata 而非只清结构化字段**:`tool_calls=[]` 只是 langchain 视图;raw payload 还在 `additional_kwargs["tool_calls"]`/`["function_call"]`、`response_metadata` 里各有一份——只清一处,严格 provider 下一请求仍会看到无配对响应的工具调用。三处同清,强制终答消息才真正序列化成"纯文本 assistant"。
- **strip 而非 raise**:剥调用让 run 以被迫终答自然完成,调用方无需异常特判;代价是"完成"可能是假的——由 `loop_capped` 记账把假完成戳穿。
- **警告延迟注入而非直接插**:§0.5 模式 1;入队状态瞬态,不跨调用。
### 源码阅读指引

按序读 `loop_detection_middleware.py`:模块 docstring(#3875 Phase 2 动机)→ `_normalize_tool_call_args`/`_stable_tool_key`/`_hash_tool_calls`(key 设计)→ `_track_and_check`(双层检测与窗口纪律,含 "MUST be at least as long as the largest hard limit" 注释)→ `_apply`(warn/hard 分叉)→ `_build_hard_stop_update`(三处同清)→ `_get_run_id`(注释讲了一个真实丢原因 bug)→ `consume_stop_reason`/`_record_audit_event` → `_augment_request`。 配套:`_bounded_dict.py`(31 行)、`audit_context.py`、`runtime/events/catalog.py::MIDDLEWARE_LOOP_DETECTION_TAG`。

---

## 3. TokenBudgetMiddleware——预算硬停(链 29)

文件:`agents/middlewares/token_budget_middleware.py`(317 行)

### 它解决什么问题

死循环的另一半成因:模型不重复调用,但单 run 内 token 无限烧(长分析、大输出、多轮子代理回溯合并)——LoopDetection 看不见预算;"烧穿预算后结束"也必须区别于干净完成被呈现给调用方。

### 钩子与执行时机 / 链位置

- `before_agent`:把历史上**先前所有 run** 的 AIMessage 用量标为已见 → 本 run 从 0 预算起算(**per-run 隔离**)。
- `after_model`/`aafter_model`:差值累计 + 阈值判定(警告入队 / 硬停改写)。
- `wrap_model_call`:注入预算警告(`HumanMessage(name="budget_warning")`)。
- `after_agent`:清本 run 的 warned/pending/seen/cumulative(**不清 `_stop_reason`**,留给执行器 pop)。
- 链位置 29,**可选**(`token_budget.enabled`,默认关)。

### 内部实现逻辑

config(`config/token_budget_config.py`):`max_tokens` 必填(默认 200000)、`max_input_tokens`/`max_output_tokens` 可选维度,`warn_threshold=0.8`、`hard_stop_threshold=1.0`,model_validator 保证 hard ≥ warn。

**差值追踪**(核心算法):每个 AIMessage 的 `usage_metadata` 会因**子代理用量回溯合并**而事后变大。按 message id 记 `seen[msg.id]=(in,out)`,每次 after_model 只累**增量**:

```python
for msg in messages:                       # 扫全部历史 AIMessage
    prev_in, prev_out = seen.get(msg.id, (0, 0))
    diff_in  = max(0, msg.usage.input_tokens  - prev_in)
    diff_out = max(0, msg.usage.output_tokens - prev_out)
    if diff: accum.… += diff; seen[msg.id] = 现值
```

- 合并前 seen 为 (0,0) → 首次出现按全量计一次;合并后或 checkpoint 重放 diff=0 → 不重复计。**每个 message id 的用量至多计一次**,天然抗回溯合并与重入。
- 三维分数 `used/limit` 取最高者(恒含 total,input/output 仅配置了对应 limit 才参与),触发维度记入 `trigger_reason`。
- `frac_max >= hard_stop_threshold(1.0)` → 硬停:stamp `token_capped`(BoundedDict + runtime.context),`_build_hard_stop_update` 追加 `[TOKEN BUDGET EXCEEDED]`(指明维度、用量/上限),剥 tool_calls/raw 元数据/finish_reason→stop,不 raise。
- `>= warn_threshold(0.8)` 且本 run 未告警 → `_warned[run_id]=True`,入队一条(每 run 一次);下轮注入 `[TOKEN BUDGET WARNING] … Wrap up your current work…`。
### ASCII 状态机

```
before_agent: 现有历史 AIMessage 全标 seen ─► 本 run 预算从 0 开始
after_model:
  差值累计(每 msg.id 至多计一次; 回溯合并/重放 diff=0 不重复)
  frac_max = max(used/limit 各维); 触发维度 = argmax
    ├─ frac_max >= 1.0(hard) ─► stamp token_capped(两处)
    │                           剥调用+清raw+finish→stop+EXCEEDED 文本 → 自然终答
    ├─ frac_max >= 0.8(warn) 且 未告警 ─► _warned[run]=True, 入队 1 条
    │                                     → 下次 wrap_model_call 注入 WARNING
    └─ 否则 ──────────────────► 放行
after_agent: 清 warned/pending/seen/cumulative; _stop_reason 留给 consume_stop_reason
```

### 与邻居的关系

- **与 LoopDetection(28)**:预算维度与调用模式维度互为补集(重复 vs 超支),硬停动作逐字对称;执行器 `_consume_guard_stop_reason` 收集**所有**暴露 `consume_stop_reason` 的守卫逐个 pop,先非 None 者胜。
- **与 TokenUsageMiddleware(20)**:TokenUsage 负责记录与归因(把子代理用量写回 dispatching AIMessage 的 usage_metadata);TokenBudget 只消费这些数字——差值追踪专为兼容"事后回溯合并"而设计。
### 设计权衡

- **差值 vs 全量重算**:全量重算无法区分"重放后数字变大是新增还是重复";差值把每个 msg.id 的计量变单调一次,重入安全。
- **run 边界清零(before_agent 快照)**:否则长线程的旧 run 预算泄漏进新 turn;代价是每次 run 起点一次 O(历史) 快照。
- **BoundedDict + 锁 + pop 消费**:同 §0.4。
- **阈值进 config**:上限因模型/任务而异;schema 校验(ge/le、hard≥warn)把错配挡在装配期。

### 源码阅读指引

`token_budget_middleware.py`:docstring → `_get_run_id` → `before_agent`(快照)→ `_apply`(差值累计与三维判定,注意 `usage_accum.total <= 0` 早退)→ `_build_hard_stop_update`(与 Loop 的同名函数对比,几乎同构)→ `consume_stop_reason` → `_drain_pending_warnings`/`_inject_warnings`。
---

## 4. TerminalResponseMiddleware——空终态兜底(链 32)

文件:`agents/middlewares/terminal_response_middleware.py`(223 行)

### 它解决什么问题

工具执行完后,provider 返回**空的终态 AIMessage**(无可见文本、无 tool_calls、无错误)。若不处理,run 以"成功"结束但用户什么都没收到——**静默成功**;且空 assistant 消息被持久化后,严格 OpenAI 兼容 provider(Moonshot/Kimi 等)**下一次请求直接拒收**(`message with role 'assistant' must not be empty`),毒化整个 thread(#4393 的兄弟问题,§6 的空内容回填是另一半解)。

### 钩子与执行时机 / 链位置

- `after_model`/`aafter_model`,带 `@hook_config(can_jump_to=["model"])`(允许返回 `jump_to` 重调度 model 节点)。
- `wrap_model_call`:注入隐藏恢复提示。
- `before_agent`/`after_agent`:清理 per-(thread,run) 状态。
- 链位置 32:收尾段**最先注册**(反向分派里**最后触发**,§0.3)——它是兜底,必须看到 Safety 回填等前面所有改写之后的最终状态。
### 内部实现逻辑

三个检测闸门(全满足才干预):

```python
last = messages[-1]
if not isinstance(last, AIMessage): return None
if _has_visible_content(last):           return None   # 有文本: 正常
if _has_tool_call_intent_or_error(last): return None   # 有工具意图: 交给路由
#   工具意图 = tool_calls / invalid_tool_calls / raw tool_calls / function_call
#              / finish_reason in {"tool_calls","function_call"}
if not _tool_result_in_current_turn(messages): return None
#   最后一条真实(非 hide_from_ui)HumanMessage 之后必须存在 ToolMessage;
#   没有任何真实用户消息 → 不干预
```

**重试预算:每 run 一次**(不是每空消息一次),键含 `run_attempt_id` 回退:

```python
if retry_count == 0:
    retry_counts[key] = 1; pending_prompts[key] = True
    return {"messages": [RemoveMessage(id=last.id)], "jump_to": "model"}
    # 删掉这条空消息(恢复成功则历史/未来上下文无垃圾), 重新调度 model
# 第二次仍空:
return {"messages": [last.model_copy(update={
    "content": _FALLBACK_CONTENT,   # 用户可读错误文案
    "additional_kwargs": {**last.additional_kwargs,
        "deerflow_error_fallback": True,
        "error_reason": "Model returned an empty terminal response after one retry"},
})]}
```

- 下一次模型调用经 `wrap_model_call` 注入隐藏恢复提示(`name="terminal_response_recovery"`,`hide_from_ui=True`):"你上一条响应是空的,请基于已有工具结果产出简洁的、用户可见的最终响应,除非必要不要再调工具"。
- **budget 不刷新**:重试后若又调工具、又空,`retry_count` 已是 1 → 直接 fallback——不会形成"空→重试→工具→空→重试"的无限循环(注释:"once per run, not once per empty message")。
- `deerflow_error_fallback` 标记是下游**判定子代理/run 真失败的权威信号**:worker 与 `_extract_final_result` 见标记即映射为 error,不把 fallback 文案当正常回答。

### ASCII 流程图

```
after_model(last)
  ├─ 有可见文本 / 有工具意图 / turn 无工具结果 ─► 放行(不干预)
  ▼ (工具执行后的空 AIMessage, retry_count=0)
删除空消息 + jump_to=model
  ▼ 下一次模型调用: wrap_model_call 注入隐藏恢复提示(hide_from_ui)
重试响应
  ├─ 有可见文本或工具意图 ──────────────► 正常继续(空消息已删, 无残留)
  ├─ 再次为空(retry_count=1) ───────────► content=fallback 文案
  │                                       + deerflow_error_fallback=True
  │                                       → worker 判 error, 非静默成功
  └─ 重试后又调工具且最终空 ──────────────► 同左(预算不刷新, 不无限重试)
```

### 与邻居的关系

- **与 SafetyFinishReason(34)的顺序耦合**(§0.3 第 3 条):Safety 先回填空内容 → Terminal 后判"有内容"放行,不会对"因安全而故意空"的响应做无谓重试;顺序颠倒则 Terminal 先 RemoveMessage+重试一个本质是安全终止的响应。
- **与 Clarification(35)**:澄清走 `Command(goto=END)` 中断等用户,不产生"工具执行后空终态";且 Clarification 先剥 sibling,Terminal 处理的集合已不含被弃调用。
- **覆盖关系**:ModelLength(33) 记录"长度截断但有内容";Terminal 处理"截断到一点内容都没有"的极端情形(provider 连信号都没给,纯空)——合起来保证**有内容的截断可审计、无内容的空手有兜底**。

### 设计权衡

- **RemoveMessage + jump_to=model vs 原地重试**:删空消息再重调度,恢复成功则历史干净;`jump_to` 经 `hook_config` 声明,图只允许跳到 model。
- **每 run 一次预算**:budget 耗尽标志持久于 BoundedDict,跨"工具→空→重试→工具"回合不重置——防无限循环的关键,宁可在罕见长任务里少一次补救。
- **fallback 标记优先于文案**:文案面向用户、标记面向系统,下游只信标记(§0.4 同哲学)。
- **只对"有真实用户消息的 turn"干预**:scheduled/内部调用没有同步用户在等,兜底语义不同,不硬套交互式规则(#4027)。

### 源码阅读指引

`terminal_response_middleware.py`:docstring → `_has_visible_content`/`_has_tool_call_intent_or_error`/`_tool_result_in_current_turn`(三个闸门,与 ModelLength/Safety 的同名函数对比差异)→ `_apply`(budget 与两分支)→ `_RECOVERY_PROMPT`/`_FALLBACK_CONTENT` → `_augment_request`(隐藏注入)。
---

## 5. ModelLengthFinishReasonMiddleware——长度截断记账(链 33)

文件:`agents/middlewares/model_length_finish_reason_middleware.py`(130 行,最小)

### 它解决什么问题

provider 因**输出预算耗尽**停止生成(`finish_reason='length'` / `stop_reason='max_tokens'` / Gemini `MAX_TOKENS`),但响应带着可见内容。DeerFlow 应当**保留内容供审计**,却**不能把被截断的响应当成干净完成**——否则下游以为模型答完了,其实它没写完(#4271)。

### 钩子与执行时机 / 链位置

- 只实现 `after_model`/`aafter_model`;**纯观察**:命中只写 `runtime.context["stop_reason"]` + INFO 日志,永不返回 state 更新(不改任何消息)。
- 链位置 33,注册在 Safety(34) 之前 → 反向分派中 ModelLength 在 Safety **之后**看到消息(先剥离/回填,后记账),在 TerminalResponse(32) 之前看到消息(先 stamp,后判空)。
### 内部实现逻辑

```python
last = messages[-1]
if not isinstance(last, AIMessage):            return None
if _has_tool_call_intent_or_error(last):      return None  # 放行给正常工具处理路径
    # tool_calls / invalid_tool_calls / raw tool_calls / function_call(无 finish_reason 检查)
if not _has_visible_content(last):            return None  # 空内容不算"截断但有货"
termination = self._detect(last)              # 顺序跑长度检测器
if termination is None:                        return None
if isinstance(ctx, dict) and "stop_reason" not in ctx:   # 只写首次, 不覆盖更早 cap
    ctx["stop_reason"] = "model_length_capped"            # (跨隐藏延续 turn 保留原值)
logger.info(... detector / reason_field / reason_value / stamped ...)
return None   # ← 关键: 任何情况都不产生 state update
```

**检测器**(`model_length_termination_detectors.py`,与 Safety 同构的策略接口):

| 检测器 | 字段 | 默认命中值 |
|---|---|---|
| `OpenAICompatibleLengthDetector` | `finish_reason` | `length` |
| `AnthropicMaxTokensDetector` | `stop_reason` | `max_tokens` |
| `GeminiMaxTokensDetector` | `finish_reason` | `MAX_TOKENS` |

都经 `_get_metadata_value` 按 `response_metadata` → `additional_kwargs` 顺序读(adapter 放置不一致),**只收非空字符串**(Pydantic enum/dict 忽略,绝不 raise);检测器异常吞掉记 no-match,绝不中断 run。

### ASCII 流程图

```
after_model(last)
  ├─ 非 AIMessage / 有工具意图 / 无可见内容 ─► 不标记(留给正常工具路径/其他中间件)
  ▼ 终态 + 可见内容
顺序跑长度检测器 (length / max_tokens / MAX_TOKENS)
  ├─ 未命中 ───────────────────────────► 放行
  └─ 命中 ──► "stop_reason" 不在 ctx 才写 model_length_capped(保留更早 cap)
              + INFO 日志(thread/run/message_id/detector/字段/值)
              → 无 state 更新, 内容原样保留
```

### 与邻居的关系

- **与 SafetyFinishReason 分工(§0.1 第②层两半)**:Safety 管安全截断(content_filter/refusal/SAFETY),ModelLength 管长度截断——都不抛异常,保留可见内容。`MAX_TOKENS` 被 Gemini **安全**检测器**故意排除**(`safety_termination_detectors.py` 注释:"output length truncation, not safety … expose separately"),正由本中间件单独负责——**同一信号在两类检测器里只归属一类**,不双重记账。
- **与 TerminalResponse(32)**:ModelLength 处理"截断但有内容"(可审计的次优完成),Terminal 处理"真·空"(重试/兜底);反向分派 ModelLength 先 stamp,Terminal 后判空,互不冲突。
- **同名闸门更窄**:`_has_tool_call_intent_or_error` 在此**不含** finish_reason 检查(长度信号本就来自 finish_reason,不能自指);Terminal 版含(要排除 tool_calls finish_reason)。

### 设计权衡

- **只记账不改写**:截断的内容是模型真实产出,审计价值高于一切;改写引入"谁动了我的输出"的信任问题。代价是结尾戛然而止——由 UI/下游依据 `model_length_capped` 提示续问。
- **不覆盖已有 stop_reason**:跨隐藏延续 turn 可能已带更早 cap(如 loop_capped),新检测只补空不顶替——最早死因优先。
- **不重解析文本 tool-call-like envelope**:截断可能让模型在正文写 `<tool_call>…</tool_call>` 文本,框架**绝不**把文本当调用执行(注入与误执行面);要做就重发请求,不做就明说没做完。
### 源码阅读指引

`model_length_finish_reason_middleware.py`(130 行):docstring(#4271)→ `_has_tool_call_intent_or_error`/`_has_visible_content`(与 terminal_response_middleware.py 同名函数对比)→ `_apply`(stamp 的"不覆盖"分支)。
---

## 6. SafetyFinishReasonMiddleware——安全终止处理(链 34)

文件:`agents/middlewares/safety_finish_reason_middleware.py`(449 行)

### 它解决什么问题

provider 因**安全原因**中途终止(`finish_reason='content_filter'`、Anthropic `stop_reason='refusal'`、Gemini `SAFETY`…),但响应带着**半截 tool_calls**——如 `write_file` 参数写到一半被过滤。LangChain 工具路由只看 `tool_calls` 非空就"去执行":残缺参数被执行,agent 看到写坏的半截文件、试图修复、再被过滤,**三方循环**(#3028);全空响应被持久化则毒化 thread(#4393)。

### 钩子与执行时机 / 链位置

- 只实现 `after_model`/`aafter_model`。
- 链位置 34,**可选**(`safety_finish_reason.enabled`,默认 true),注册在 TerminalResponse(32)/ModelLength(33)/custom(30)/configured-extensions(31) 之后——反向分派中**先于它们与 LoopDetection 看到原始模型输出**(模块 docstring 与 §0.3)。
### 内部实现逻辑

**检测器**(`safety_termination_detectors.py`):

| 检测器 | 字段 | 默认命中值 | 备注 |
|---|---|---|---|
| `OpenAICompatibleContentFilterDetector` | `finish_reason` | `content_filter` | 覆盖 OpenAI/Azure/Moonshot/DeepSeek/Mistral/vLLM/Qwen 兼容网关;Azure `content_filter_results` 进 extras;可经 `finish_reasons=` 扩展(如国产网关 `sensitive`/`violation`) |
| `AnthropicRefusalDetector` | `stop_reason` | `refusal` | |
| `GeminiSafetyDetector` | `finish_reason` | `SAFETY`/`BLOCKLIST`/`PROHIBITED_CONTENT`/`SPII`/`RECITATION`/`IMAGE_SAFETY`/`IMAGE_PROHIBITED_CONTENT`/`IMAGE_RECITATION`(大写) | **故意排除** `STOP`(正常)、`MAX_TOKENS`(长度非安全,归 ModelLength)、`MALFORMED_FUNCTION_CALL`/`UNEXPECTED_TOOL_CALL`(协议错误另类)、`LANGUAGE`/`NO_IMAGE`(能力不匹配)、`OTHER`/`FINISH_REASON_UNSPECIFIED`(过宽,opt-in);`safety_ratings` 进 extras |

读取规则同 ModelLength:两处 metadata 都查、只收字符串;检测器异常吞掉记 no-match,**绝不中断 run**。

**只在两种情形干预**(`_prepare_intervention` 先闸后检):

```python
tool_calls = list(last.tool_calls or [])
content_is_blank = not message_content_to_text(last.content or "").strip()
if not tool_calls and not content_is_blank: return None   # 有可见文本无工具: 放行,
                                                          # 部分答案自然到达用户
termination = self._detect(last)                           # 未命中 → 放行
# 模式A 带 tool_calls → 全剥(残缺参数不可执行)
# 模式B 全空无工具   → 回填可见说明(防空 assistant 毒化 thread)
patched = self._build_suppressed_message(last, termination)
ctx["stop_reason"] = "safety_capped"                       # 与 loop/token 并列(#4176)
```

`_build_suppressed_message`:

- 用 `clone_ai_message_with_tool_calls(message, [])` **一次清掉**结构化 tool_calls、raw `additional_kwargs.tool_calls`、`function_call`;该助手只把 `finish_reason=="tool_calls"` 改 `"stop"`,而安全场景的 `content_filter`/`refusal`/`SAFETY` **原样保留**——下游 SSE/转换器继续看到**真实 provider 原因**。
- content 追加面向用户解释:带工具调用用 `_USER_FACING_MESSAGE`(工具被抑制、参数可能截断不安全);空内容用 `_USER_FACING_EMPTY_MESSAGE`(**不声称抑制了工具**,因为本来就没有)。
- `additional_kwargs["safety_termination"]` stash 可观测记录:`detector`/`reason_field`/`reason_value`/`suppressed_tool_call_count`/`suppressed_tool_call_names`/`extras`。

**可观测性双通道**(best-effort,失败只 debug/warning):

- SSE 自定义事件 `type="safety_termination"`(经 `emit_custom_event`)——通知 Web UI 收起已流出的 "tool starting…" 占位。
- 持久化 `middleware:safety_termination`(`journal.record_middleware`):记 detector/字段/值/**被抑制工具的名字、数量、id**/message_id——**绝不记工具参数**,那正是被过滤的内容,记下来等于绕过过滤器(#3028 review)。

`from_config` 边界:detectors 为 `None` → 内置三件套;显式**空列表 → 抛 ValueError**——空列表会"静默禁用但留在链里",两头不讨好;关闭请用 `enabled: false` 整体摘除。自定义 detector 经 `deerflow.reflection.resolve_variable` 加载,`isinstance` 校验 Protocol 形状。

### ASCII 决策流

```
after_model(last AIMessage)
  │
  ├─ 无 tool_calls 且 有可见内容 ────────► 放行(部分答案照常送达用户)
  ▼
detect(): 依次跑 detector (content_filter / refusal / SAFETY…)
  ├─ 无命中 ────────────────────────────► 放行
  ▼ 命中
  ├─ 模式A 带 tool_calls ──► clone(msg, []) 剥全部调用 + raw 元数据
  │                           finish_reason 保留(安全终止是事实, 留给下游)
  │                           content 追加 "工具调用被抑制…" 说明
  ├─ 模式B 全空无工具 ─────► content 回填 "provider 安全终止, 无内容…"
  │                           (防严格 provider 拒收空 assistant → thread 毒化)
  ▼
stamp safety_capped(runtime.context) + additional_kwargs.safety_termination
+ SSE 事件(UI 收起占位) + 审计事件(不含参数) → 返回替换消息
```

### 与邻居的关系

- **与 LoopDetection**:同一剥离机制、不同触发——Safety 先看到原始响应并清理,被安全抑制的消息**不计入** Loop 调用哈希(否则半截调用被数成"重复",安全截断被误判成死循环);注册顺序保证此点(§0.3 第 2 条)。
- **与 ModelLength**:MAX_TOKENS 类信号被安全检测器排除、单独归 ModelLength,互不抢注(§5"与邻居的关系")。
- **与 TerminalResponse**:Safety 空内容回填先跑,Terminal 后跑 → 不重试"故意的空"(§0.3 第 3 条、§4)。
### 设计权衡

- **消息层剥调用、语义层留原因**:残缺 tool_calls 必须剥(否则被执行);`finish_reason` 是**事实**,保留给下游判断"为什么剥"——执行安全与可观测性分居两个字段。
- **只对安全终止干预**:有可见文本但无工具调用的安全终止直接放行——部分答案仍有价值,重写是越权。
- **审计不含参数**:抑制工具时把参数写进审计 = 把 provider 拒绝的内容换个地方落盘;名字/数量/id 足够回答"今天哪些 run 被安全抑制了"(单条 SQL,不 join 消息体)。
- **空响应回填但不撒谎**:回填文案区分"抑制了 N 个工具调用"与"provider 返回空"两个版本,可观测字段与用户文案都如实。
- **空 detector 列表拒绝装配**:`enabled:false` 是唯一合法关闭方式,防"看似在链上实则失效"的假安全感。

### 源码阅读指引

`safety_finish_reason_middleware.py` 模块 docstring(#3028/#4393 因果链)→ `_prepare_intervention`(闸门与两模式,注意 `last.content or ""` 对 `model_copy(update={"content": None})` 的防御)→ `_build_suppressed_message` → `_detect` → `_emit_event`/`_record_audit_event` → `from_config`。
配套:`safety_termination_detectors.py`(237 行,detector 排除集注释极详尽);`config/safety_finish_reason_config.py`;`tool_call_metadata.py`(50 行,被 Safety/SubagentLimit/Clarification/Loop 共享的克隆助手)。

---

## 7. ClarificationMiddleware——人机澄清(链 35,必须最后)

文件:`agents/middlewares/clarification_middleware.py`(605 行)

### 它解决什么问题

任务有歧义时,与其让 agent 猜(猜错浪费大量调用),不如**停下来问用户**。旧方案把 `ask_clarification` 当普通工具、问完继续跑;正确语义是**中断执行流、持久化等待用户回答**,回复后再恢复。它是 harness-strategies §9.7 A4——纠偏里唯一真正回到用户意图的手段。

### 钩子与执行时机 / 链位置

- `wrap_tool_call`/`awrap_tool_call`:在工具执行**之前**拦截 `ask_clarification`;非此工具直接透传 handler。
- `after_model`/`aafter_model`:`_drop_parallel_non_clarification_tools`,丢掉同批 sibling 调用。
- 链位置 35:**必须最后注册**——它在 wrap_tool_call 层短路工具执行(直接 `Command(goto=END)`,handler 不跑),任何后置层都破坏中断原子性;反向 after_model 分派中它**最先**看到原始响应、先剥 sibling(§0.3)。
### 内部实现逻辑

**step 0 —— after_model 剥 sibling**(`_drop_parallel_non_clarification_tools`):

provider 常批量发工具调用。若 `ask_clarification` 与 `bash`/`write_file` 同批而只拦澄清,sibling 会在用户回答**之前**执行;且 `return_direct` 路由要求末条 AIMessage 的**全部** tool_calls 都 return_direct 才 goto END——混合批次会绕回模型。

```python
if self._clarification_disabled(runtime): return None   # 禁用时保留 sibling(见下)
clarification = [tc in tool_calls         if name == "ask_clarification"]
invalid       = [tc in invalid_tool_calls if name == "ask_clarification"]
if not clarification and not invalid: return None        # 无澄清: 不管
siblings = [tc in tool_calls if name != "ask_clarification"]
if not siblings: return None                             # 只有澄清: 不管
# 关键: 畸形 ask_clarification 落在 invalid_tool_calls 也算停止信号
# → 剥掉 siblings(Anthropic tool_use 内容块按 id/name 同步过滤), 只留澄清;
#   无剩余 tool_calls 时路由到 END
```

**step 1 —— wrap_tool_call 拦截**(`_handle_clarification`):

```python
args = tool_call.args
fields = self._normalize_fields(args.get("fields"))     # 只归一化一次
formatted = self._format_clarification_message(args, fields=fields)
request_id = _stable_message_id(tool_call_id, formatted)
# 确定性 ID: "clarification:{tool_call_id}" 或 sha256(格式化消息)[:16]
#  → 重试同一澄清用相同 id, LangGraph 视为替换而非追加(不重复渲染卡片)
payload = self._build_human_input_payload(args, tool_call_id, request_id, fields=fields)
tool_msg = ToolMessage(id=request_id, content=formatted,    # 纯文本回退
                       tool_call_id=tool_call_id,
                       name="ask_clarification",
                       artifact={"human_input": payload})   # 结构化载荷
return Command(update={"messages": [tool_msg]}, goto=END)   # 中断执行流
```

- **双通道**:`content` 是任何前端都能渲染的纯文本;**`artifact.human_input`** 是结构化载荷(问题、clarification_type、input_mode、选项/表单字段)。
- **协议版本化**:legacy 模式(`free_text`/`choice_with_other`)保持 `version: 1`;v2 `form` 模式(来自 `fields`)是 `version: 2`——老前端见 version 2 拒绝该载荷、降级用纯文本 content。**回复协议不变**(v1 `text`/`option`):表单卡提交可读文本摘要 `response_kind:"text"`,journal 持久化无需新字段。
- **v2 form 字段归一化是原子的**(`_normalize_fields`):结构性损坏 → 整个表单降级 legacy,绝不允许"卡片看似完整实则缺业务字段":

```
硬上限: MAX_FORM_FIELDS=16 / MAX_FIELD_OPTIONS=24 / MAX_FIELD_TEXT_CHARS=200 /
        MAX_FORM_SERIALIZED_BYTES=16_384(UTF-8 总字节; 单条上限合起来仍可能让
        纯文本回退超 IM 通道限制: Slack 40k 字符/条、Feishu ~30KB/卡)
原子失败(任一 → 整表 []): 条目非 dict / name 缺失空 / name ∈ _RESERVED_FIELD_NAMES
  (JS Object.prototype 成员: __proto__/constructor/prototype/toString/hasOwnProperty/…)
  / name 重复或超长 / 字段数>16 / 选项数>24 / 选项超长 / 序列化超 16KB
局部降级(不拖垮整表): 未知 type(含 unhashable 的 type:[]/{}——先 isinstance
  探测再查表, 防 TypeError)→ "text"; select/multi_select 无选项 → "text"
选项: 裁剪/去重/去空(前端拒空 label); XML 残留标签先剥(_XML_TAG_RE);
       dict/list 容器按源序递归拍平出标量叶
checkbox: 布尔归一(模型可能给 "true"/1), 默认 no; 其上 required = 同意语义
```

- `_format_clarification_message`:按 `clarification_type` 选图标(❓/🤔/🔀/⚠️/💡),context 作背景、question 为主体,fields 或 options 编号列出(带 required 标注、multi_select 注明可多选),结尾引导填值。`clarification_type: []` 这类畸形 JSON 先 `str()` 再查表(防 dict key TypeError)。

**step 2 —— 非交互渠道(`disable_clarification`)**:webhook/批处理等场景人类无法同步回答,澄清会**死锁 run**。`runtime.context["disable_clarification"]` 置位时:

- `_handle_disabled_clarification` 返回**普通 ToolMessage**(非 END 中断):"澄清被禁用、人类不在场,用最佳判断继续执行并声明假设"——agent 收到它作为工具结果继续生成,**循环不终止**;
- `_drop_parallel_non_clarification_tools` 跳过:保留 sibling 照常执行;
- scheduled 任务更彻底:工具**组装阶段就排除** `ask_clarification`,根本不进工具集。

### ASCII 状态机

```
after_model(AIMessage)
  ├─ disable_clarification ──────────────► 保留 sibling, 放行(不剥)
  ├─ 无澄清调用 ─────────────────────────► 放行
  └─ 澄清 + sibling ──► 剥 sibling(畸形澄清也算停止信号)
                        → 只留澄清 → 交工具节点
wrap_tool_call(每个调用)
  ├─ 非 ask_clarification ───────────────► handler 透传(正常执行)
  └─ ask_clarification
       ├─ disable_clarification ──► 普通 ToolMessage "按最佳判断继续"
       │                             → 循环继续(不 END, 防 run 死锁)
       └─ 交互 ──► fields 原子归一化 → 纯文本 content
                   + artifact.human_input(v1/v2 版本化)
                   + 确定性 id 的 ToolMessage
                   → Command(goto=END): 图停在 END, checkpoint 持久化等用户
                     (重试同 id 替换; 老前端对 version 2 拒收降级纯文本)
```

### 与邻居的关系

- **为什么必须最后**(§0.3 第 1、5 条):① 反向 after_model 中它先看到原始输出才能剥 sibling——注册更早则它最后才跑,sibling 已进工具路由;② wrap_tool_call 短路 + `goto=END` 的中断必须在链末端,后面不能有层再改写/插入。
- **与 Safety/ModelLength/Terminal**:澄清不是终止信号——它不触发 Safety;中断后也没有"工具执行后空终态"(没有工具被执行)。顺序上 Clarification 剥完 sibling,后面中间件处理的集合才是用户真正会看到的动作。
### 设计权衡

- **确定性消息 id**:澄清是幂等操作——重复/重试不得生成第二张卡;tool_call_id(无则内容哈希)的稳定 id 走 reducer 替换路径。
- **content/artifact 双通道 + 版本号**:可读回退与结构化载荷解耦;版本号让新旧前端/渠道**按能力降级**,后端不猜客户端。
- **原子降级 vs 逐字段容错**:结构性错误(重复名/超限/保留名)整表降级,因为"渲染出缺字段的完整表单"比"没有表单"更糟;良性问题(未知类型、空选项 select)局部降级——错误分级编码了"哪些是业务完整性、哪些是模型毛刺"。
- **禁用时保留 sibling**:非交互 run 把澄清转成"继续"信号,若还剥 sibling,agent 会丢掉本可推进的动作——保留 sibling 让它带着澄清结果一起干完。
- **归一化放中间件而非工具 schema**:`ask_clarification` 在 wrap_tool_call 层被短路,**工具参数的类型校验从未发生**——中间件是唯一且最后的校验点,所以归一化必须既严格(原子)又健壮(吞掉一切畸形 JSON 形状)。

### 源码阅读指引

`clarification_middleware.py` 按数据流读:`_drop_parallel_non_clarification_tools`(invalid_tool_calls 也算停止信号)→ `_handle_clarification`/`_stable_message_id` → `_normalize_fields`(原子校验,全文件最值得精读)→ `_normalize_options`/`_flatten_dict_option_values`/`_normalize_bool`(畸形输入防御)→ `_build_human_input_payload`(v1/v2 分派)→ `_format_clarification_message` → `_handle_disabled_clarification`/`_clarification_disabled` → `wrap_tool_call`/`awrap_tool_call`。
---

## 附:收尾段全景一图

```
模型响应 → after_model 反向分派(最后注册的先跑)
35 Clarification  剥 sibling, 只留澄清 → (澄清则短路到 END 等用户)
34 SafetyFinish    安全终止? 剥残缺 tool_calls / 回填空内容 → safety_capped
33 ModelLength     长度截断且有内容? → 记 model_length_capped(不改写)
32 TerminalResponse仍空终态? → 隐藏恢复提示重试一次 → 再空则 error fallback
29 TokenBudget     超预算? → 剥调用终答 + token_capped
28 LoopDetection   重复调用? → 剥调用终答 + loop_capped
27 SubagentLimit   超额 task? → 截断 + subagent_limit_capped
                      ↓ 正常路径
                   工具节点(wrap_tool_call) → ToolMessage → 下一轮模型调用
                      ↓ wrap_model_call(注册顺序)
                   注入本轮的 loop/budget/terminal 延迟警告(消息尾部, 配对完好)
```

三个主动/语义守卫与兜底各管一个威胁面(§0.1);所有 capped 都落到加法 `stop_reason`(§0.4); 自定义与扩展中间件只能插在 30/31,收尾段 32–35 的顺序不可被扩展改写(§0.2–0.3)。 读源码时若困惑"为什么它在这里",回到 §0.3 的反向分派图——DeerFlow 中间件链的顺序不是装饰,它就是正确性本身。


