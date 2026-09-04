# 11 · 记忆更新流水线:捕获 → 队列 → 提取 → 淘汰 → 注入回环

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写。
> 配套深文:`../memory-architecture-design.md`(自包含架构设计,含存储格式与并发细节);本章聚焦**代码级流水线**——每个环节在哪个文件、哪一行、按什么规则放行或丢弃,并沿一条记忆的完整旅程把中间件、队列、后端三者如何分工讲透。

## 0 全景:一条记忆的五站旅程

长期记忆的写入不是同步落盘,而是**异步、可丢、可重试**的。一次对话结束后,消息要依次穿过五个站点,才能变成未来对话里模型看到的一段 `<memory>`:

```
  写链路(后台、异步、best-effort)
  ┌────────────────────────────────────────────────────────────────────┐
  │                                                                    │
  ┌──────────┐   ①捕获      ②队列         ③提取(LLM)      ④闸门+落盘    │
  │ 对话结束  │ → MemoryMiddleware → MemoryUpdateQueue → MemoryUpdater → MemoryStorage
  │          │    (链 22)      (debounce 30s/背压)   (一次调用六决策)   (scope 闸门/乐观锁)
  └──────────┘                                                         │
       ▲                                                               ▼
       │  ⑤注入回环                                      memory.json + facts/*.md + FTS5 索引
  ┌──────────┐   读链路(同步、每次模型调用前)                            │
  │ 下一轮对话 │ ← DynamicContextMiddleware(#14) ← manager.get_context() │
  └──────────┘   冻结快照注入首条用户消息                                 │
                                                                        │
  紧急链:Summarization 压缩前 → memory_flush_hook → add_nowait(bypass)  ─┘
  └ 不经过这条链,被压缩删掉的消息就永远丢了(§7)
```

四个核心组件的分工可以用一句话概括:**中间件管"何时、为谁捕获/注入",后端管"捕获之后怎么做"**。`MemoryMiddleware` 只负责在对话结束时把原始消息整包交给管理器;过滤、校验、去抖、抽取、闸门、落盘全部发生在后端。这是因为记忆后端是可插拔的(`manager_class: deermem | mem0 | openviking | honcho | 自定义`),不同后端对"什么值得记"的策略不同,不能在 agent 层写死。

当前默认后端 **DeerMem** 的代码布局(都在 `backend/packages/harness/deerflow/` 下,下文路径省略此前缀):

| 组件 | 文件 | 职责 |
|---|---|---|
| `MemoryMiddleware` | `agents/middlewares/memory_middleware.py` | 对话结束后入队(链 22,极薄) |
| `DynamicContextMiddleware` | `agents/middlewares/dynamic_context_middleware.py` | 读链路注入(链 14) |
| `memory_flush_hook` | `agents/memory/summarization_hook.py` | 摘要压缩前紧急冲刷 |
| `MemoryManager` 契约 | `agents/memory/manager.py` | 后端中立接口 + 单例工厂 |
| `DeerMem` 后端 | `agents/memory/backends/deermem/deer_mem.py` | 过滤、队列、抽取、闸门的组装 |
| `message_processing.py` | `.../deermem/deermem/core/message_processing.py` | 消息过滤 + trivial 剔除 + 信号检测 |
| `queue.py` | `.../deermem/deermem/core/queue.py` | 去抖队列、合并、背压、flush |
| `updater.py` | `.../deermem/deermem/core/updater.py` | LLM 抽取、写闸门、水位、应用更新 |
| `eviction.py` | `.../deermem/deermem/core/eviction.py` | 容量淘汰策略(confidence / hybrid-v1) |
| `prompt.py` | `.../deermem/deermem/core/prompt.py` | 注入格式化 + 抽取对话裁剪 |

---

## 1 MemoryMiddleware(链 22):薄捕获器

### 1.1 挂载点与触发时机

在 lead-agent 的装配函数 `agents/lead_agent/agent.py::build_middlewares()` 里,`MemoryMiddleware` 在所有运行时中间件(1-13)和 lead-only 追加段中**紧跟 `TitleMiddleware` 之后**被 append,在 `agents/middlewares/AGENTS.md` 记载的中间件总表中编号为 **22**(同一张表里读链路的 `DynamicContextMiddleware` 是 14,`SummarizationMiddleware` 是 18——三者的相对位置决定了它们协作的时序)。注释写得很直白:

```
# TitleMiddleware generates title after first exchange
# MemoryMiddleware queues conversation for memory update (after TitleMiddleware)
```

它挂在 LangGraph 的 **`after_agent` / `aafter_agent`** 钩子上——即**每一轮 agent 执行完整结束之后**触发一次(注意:不是每次模型调用,而是一轮)。`TitleMiddleware` 在首轮后起标题,`MemoryMiddleware` 排在它后面,保证任何轮次结束时拿到的 `state["messages"]` 都是完整、已稳定的。

两个执行方法,同步/异步路径各一个:

```python
@override
def after_agent(self, state, runtime) -> dict | None:      # 同步路径
    add_args = self._resolve_add_args(state, runtime)
    if add_args is None:
        return None
    thread_id, messages, user_id, trace_id = add_args
    get_memory_manager().add(thread_id, messages, agent_name=self._agent_name,
                             user_id=user_id, trace_id=trace_id)
    return None

@override
async def aafter_agent(self, state, runtime) -> dict | None:  # 异步路径
    add_args = self._resolve_add_args(state, runtime)
    if add_args is None:
        return None
    manager = await asyncio.to_thread(get_memory_manager)   # 解析移出事件循环
    await manager.aadd(thread_id, messages, agent_name=self._agent_name,
                       user_id=user_id, trace_id=trace_id)
    return None
```

注意两个细节:钩子**永远返回 `None`**(不修改任何 state——记忆是旁路动作,失败也不得影响对话本身);异步路径用 `asyncio.to_thread` 把管理器解析挪出事件循环再调 `aadd`(网络型后端必须自己 offload)。

### 1.2 `_resolve_add_args`:五道前置门

真正决定"要不要入队"的逻辑收敛在 `_resolve_add_args`,它只做**解析,不调管理器**,返回 `(thread_id, messages, user_id, trace_id)` 或 `None`:

1. **总开关**:`config = self._memory_config or get_memory_config()`;`not config.enabled` → 直接跳过。`memory.enabled: false` 时中间件整体空转。
2. **thread_id**:先取 `runtime.context["thread_id"]`,没有则退回 LangGraph 的 `get_config()["configurable"]["thread_id"]`;都没有 → 跳过(无线程上下文无从分桶)。
3. **消息非空**:`state.get("messages", [])` 为空 → 跳过。
4. **user_id 在入队时刻捕获**:`resolve_runtime_user_id(runtime)`。这是刻意为之——队列的 debounce 用 `threading.Timer` 在**另一个原生线程**触发,`ContextVar` 不跨原生线程传播,定时器触发时请求上下文早没了;不提前捕获,后台抽取就读不到用户身份、记忆会写错桶。
5. **trace_id 同步捕获**:`resolve_trace_id(runtime.context[DEERFLOW_TRACE_METADATA_KEY])`。同样的线程边界问题;trace_id 随 `ConversationContext` 一路带到抽取期的 LLM 调用,让记忆抽取的 trace span 与主对话对齐。

构造函数还有两个参数:`agent_name`(决定写入哪个事实桶,`None` = 默认桶 `__default__`,按 user/agent 双维隔离)与 `memory_config`(由装配方显式传入已解析的 `AppConfig.memory`,避免中间件内部再读一次全局单例)。

### 1.3 为什么"只捕获、不过滤"

`after_agent` 把**原始消息整包**交给 `manager.add()`——过滤到 Human/AI、去寒暄、剔除 `hide_from_ui`、检测信号全部不在中间件里做。这是与旧版(数月前 `backend/src` 时代把过滤写在中间件里)最大的结构性差异:**过滤下沉到了后端**。原因很实际:mem0/openviking/honcho 各自对"什么消息值得喂给记忆系统"的理解不同;agent 层不写死,换后端就不用改中间件。旧版书里那套 `MemoryMiddleware.after_agent` 里直接 `filter_messages_for_memory` + 判 `user_messages/assistant_messages` 的代码,现在原样搬进了 `DeerMem._prepare_update`(见 §2)。

### 1.4 tool 模式的特例

`memory.mode: middleware`(默认)时中间件无条件挂载。`mode: tool` 时**默认不挂**(模型自己通过 `memory_search`/`memory_add` 等四个工具决定记什么,`tools.py`);唯一的例外是**后端声明 `backend_requires_passive_writes_in_tool_mode` 为真**时(如 openviking:搜索靠工具、持久写仍依赖对话级抽取),中间件保留,形成"工具搜索 + 被动抽取"混合模式(`agent.py` 597-607 行的条件 append)。注意一个告警分支:`mode: tool` 但 `enabled: false` 会打出 warning——工具都不注册了,模式配置没意义。

---

## 2 入队前过滤:什么消息有资格进队列

`DeerMem.add()` / `add_nowait()` 的第一步都是 `_prepare_update(messages)`:

```python
def _prepare_update(self, messages):
    filtered = filter_messages_for_memory(messages, should_keep_hidden_message=self._config.should_keep_hidden_message)
    filtered = filter_trivial(filtered, patterns=self._trivial_patterns)
    user_messages = [m for m in filtered if getattr(m, "type", None) == "human"]
    assistant_messages = [m for m in filtered if getattr(m, "type", None) == "ai"]
    if not user_messages or not assistant_messages:
        return None                      # 无实质交互,不值得花一次 LLM 调用
    signals = detect_signals(filtered, patterns_dir=self._config.patterns_dir)
    return filtered, frozenset(signals)
```

四步过滤 + 一个交互门槛,全部在 `message_processing.py`,逐条看。

### 2.1 类型白名单 + tool_calls 剔除

`filter_messages_for_memory` 只保留 `type == "human"` 与 `type == "ai"` 两类;**`ToolMessage`(工具结果)、系统消息一律丢弃**——记忆抽取只需要"用户说了什么 + Agent 最终说了什么",中间的工具输出(搜索结果、代码执行回显)进入长期记忆既无意义又烧 token。

对 AI 消息还有一个关键规则:**带 `tool_calls` 的 AI 消息一律不要**。Agent 一轮里可能连续产生多条 AI 消息(思考 → 调工具 → 再思考 → 最终回复),只有**最终那条没有 tool_calls 的纯文本回复**才代表"Agent 说了什么"。实现上用一个 `skip_next_ai` 标志维持"某条 human 被剔除后,紧随其后那条 AI 也要跟着删"的成对纪律(见下)。

### 2.2 `hide_from_ui` 框架消息剔除 + 防自激

`human` 消息若带 `additional_kwargs["hide_from_ui"]`,说明它是**中间件注入的框架内部消息**,默认直接 `continue` 跳过。哪些消息带这个标记?`TodoMiddleware` 的 todo 提醒、`ViewImageMiddleware` 的注入载荷、`DynamicContextMiddleware` 注入的记忆块(`__memory`)与日期提醒(§8)——尤其最后一种:如果不禁掉,记忆抽取器会把自己上一轮注入的 `<memory>` 内容又抽回记忆里,**自我放大循环**。注释里明确点名:"p0 `__memory` payload could trigger a self-amplification loop"。

唯一的例外是**用户亲笔的澄清回答**:消息带 `human_input_response`(version 1 + kind + 非空 source/request_id/value)时视为真实内容予以保留——这是"用户在回答 Agent 的追问",值得记。判断既支持宿主注入的 `should_keep_hidden_message(additional_kwargs) -> bool` 钩子(生产环境由 deer-flow 注入,委托给权威实现 `read_human_input_response`),也内置了一个宿主无关的结构镜像 `_is_human_clarification_response`,让裸跑在测试/独立环境里的过滤逻辑同样正确。

### 2.3 trivial 寒暄过滤

`filter_trivial` 剔除纯应酬:**整条**用户消息(去首尾空白与 `。.,，!！?？;；` 等尾标后)用 `fullmatch` 匹配 `trivial.yaml` 里的模式(如「ok」「好的」「谢谢」「嗯」)——必须是**整条匹配**,含"ok"的正常句子绝不误伤。被剔除的寒暄 human 和紧随其后的 AI 回复一起删(`skip_next_ai` 纪律复用)。如果一轮对话全部是寒暄,过滤结果为空,调用方视为"不值得入队",省下一次抽取 LLM 调用。

### 2.4 上传事件剔除:session 级,不持久化

上传的文件列表是**会话级临时资源**,绝不能进长期记忆。两条防线:

1. **消息块剥离**:`UploadsMiddleware`(链 5,lead-only)会把本轮新上传的文件以 `<current_uploads>...</current_uploads>` 块 prepend 到最后一条用户消息的内容前(`uploads_middleware.py`);`filter_messages_for_memory` 用正则 `_UPLOAD_BLOCK_RE = re.compile(r"<current_uploads>[\s\S]*?</current_uploads>\n*", re.IGNORECASE)` 把整块删掉。若剥离后内容为空(纯上传轮),该 human 与其 AI 回复**整体跳过**——上传动作本身永不入队。
2. **提示词禁令**:抽取模板 `memory_update.chat.yaml` 的"不建事实"清单里明确列出**文件上传事件**——就算剥离失败漏了一条,模型也被指示不得把"用户传了 foo.pdf"记成事实。

为什么?上传路径只在当前 run 有效(`list_uploaded_files_tool` 明说"当前 run 上传的文件已列在 `<current_uploads>` 里,不再重复"),写进长期记忆,未来的会话会拿着过期路径去访问不存在的文件。旧版书里这个块叫 `<uploaded_files>` 且逻辑在中间件层;现版本已改为 `<current_uploads>` 并下沉到后端过滤——语义一致,位置和名字都变了。

### 2.5 有来有回门槛 + 信号检测

过滤后必须**至少各有一条 human 和一条 ai**,否则整体返回 `None` 不入队——没有交互就没有新信息。然后 `detect_signals` 在过滤后的**最近 6 条 human 消息**(`messages[-6:]`)上跑确定性正则,识别 6 类信号:`correction` / `reinforcement` / `preference` / `identity` / `goal` / `decision`。模式文件外置在 `core/message_patterns/*.yaml`(`patterns_dir` 可覆盖,加语言/业务短语不用改代码),正则全程**确定性匹配,不经过 LLM**。注意区分:**信号是"用户当前在做什么对话行为",事实类别是"这条信息是什么性质"**,名字部分重合但不是一一对应(`reinforcement` 是信号,却没有同名 category;它只用于给已有事实确认,§6.5)。

信号的第二个用途是**背压豁免**(§3.4)——这是它不只在 prompt 里当提示、还被提升为队列准入条件的原因。

---

## 3 队列:去抖、合并、背压、水位

过滤通过后,`DeerMem.add()` 把 `(messages, signals)` 交给 `MemoryUpdateQueue`(`deermem/core/queue.py`,472 行)。这是写链路的中枢,负责把"每次对话都触发"的写入需求压缩成"低频、批量、不丢高价值"的抽取调用。

### 3.1 入队对象与合并键

每个待处理单元是 `ConversationContext`:

```python
@dataclass
class ConversationContext:
    thread_id: str
    messages: list[Any]
    timestamp: datetime = ...            # UTC 入队时刻
    agent_name: str | None = None
    user_id: str | None = None           # 入队时捕获,跨 Timer 线程存活
    trace_id: str | None = None          # 同上,用于抽取 LLM 的 trace
    signals: frozenset[str] = ...
    bypass_watermark: bool = False       # 紧急冲刷标记(§3.5)
```

合并键 `queue_key = (thread_id, user_id, agent_name)`——**同一个线程、同一个用户、同一个 Agent 桶的多轮对话在去抖窗口内合并成一次抽取**;不同用户/不同 Agent 的更新互不干扰,天然符合存储的隔离模型。

### 3.2 去抖 Timer 语义

`add()` 持锁执行 `_enqueue_locked` 后调用 `_reset_timer()`:取消旧 Timer,新起一个 `threading.Timer(debounce_seconds, self._process_queue)`。`debounce_seconds` 默认 **30**(可配 1-300)。语义是**每次新入队都重置倒计时**:只要用户在 30 秒内持续对话,队列就不断合并、Timer 不断后移;用户停 30 秒后,一次性处理队列里所有 context。Timer 是 `daemon=True`——进程退出时未触发的任务直接丢失,这正是"纯内存 best-effort"设计的一部分(§3.6 有优雅停机的补救)。

处理循环 `_process_queue` 有几个并发要点:

- **单 worker 排空**:`_processing` 标志防止两个 Timer 同时排空。若处理中又有新 `add`,置 `_reprocess_pending`,当前 worker 的 finally 里在锁内检查该标志并 `_schedule_timer(0)` 续跑(锁内重排保证与并发的 `add` 不竞态)。
- **逐条间 0.5s 睡眠**:`time.sleep(0.5)` 限速,避免背靠背的 LLM 调用触发限流;停机 drain 路径可跳过。
- **成败计数**:`_process_queue` 结束时记一条 `"Memory update batch done: N succeeded, M failed"`——逐条失败的异常在循环内被吞掉(见 §5.3 的失败语义),没有这条汇总日志,运维只看到"Processing N"而不知道丢了几条。

### 3.3 同 key 合并与紧急共存

`_enqueue_locked` 在 `_items` 里找同 key 的现存 context:

- **找到 → 就地合并**:新 context 替换旧的,`signals` 取**并集**(任何一次更新上见过的信号都保留——信号是准入凭证,丢了就丢背压豁免权)。
- **没找到 → 追加**。
- **匹配键含 `bypass_watermark` 维度**:紧急冲刷(bypass=True)与普通更新(bypass=False)**即使同 key 也互不覆盖**、各自独立排队。为什么?紧急冲刷是"压缩前抢救快照",若它覆盖了同 key 待处理的普通更新,普通更新未抽取的对话尾部就丢了——而用户如果恰好在那之后停止对话,下一轮不会再补喂,那是真丢。

### 3.4 背压:queue_max_depth 与"重要记忆永远准入"

队列深度达到 `queue_max_depth`(默认 **1000**,0 = 不限)时,新到的**非信号普通更新**抛 `QueueFull`:

```python
max_depth = self._config.queue_max_depth
if max_depth > 0 and not bypass_watermark and not signals and existing is None \
        and len(self._items) >= max_depth:
    raise QueueFull(...)
```

豁免名单是三个"永远准入":**带任何信号的更新**(correction/reinforcement 等高价值对话——用户刚纠正过你,这条记忆如果被背压丢掉,代价远大于一次排队)、**紧急冲刷**(bypass,压缩前截图,丢了就永久丢)以及**同 key 合并**(合并不增加深度)。这正是"重要记忆永远准入"的实现:背压只淘汰低价值的普通更新。

`QueueFull` 抛到哪一层?`DeerMem.add()` 自己 `try/except QueueFull`,记 WARNING 后**吞掉**——背压降级为"本次更新跳过",绝不向上传播进 `MemoryMiddleware.after_agent` 打断 agent 的回合(同类中间件自保的同一套纪律)。且被拒的更新**下一轮对话还会重来**:中间件每轮都传整包对话,而水位不因"没入队"而前进(§3.5),所以这轮被拒 = 只是延迟,不是丢失。

### 3.5 水位(watermark):去重与失败不前进

入队只是第一步,真正喂给抽取 LLM 前还有**对话水位**去重。中间件每轮都传**整段对话历史**,若不做标记,每次抽取都会把三个月前的旧消息重抽一遍。`MemoryUpdater` 维护 `(thread_id, user_id, agent_name)` → 最后一条已抽取消息身份的水位表:

- 身份由 `_message_identity` 生成:`(id, mid)`(langgraph 消息 id)优先,无 id 则退回 `(type, content)`。**内容/身份基而不是索引基**——摘要压缩会删掉对话前部,索引水位会指错消息、静默跳过未抽取轮次;身份基水位在"前部被删"时找不到最后消息,安全地退化为全量重喂。
- `_feed_after_watermark`:水位还在消息列表里 → 只喂它**之后**的切片;水位消息已不在(被压缩删了/首次抽取)→ 喂全量。注释说得明白:"Re-feeding is safe over-extraction——它从不跳过一轮,而跳过才是唯一会丢事实的失败方向"。
- 水位表是**有界 LRU**(`watermark_max_keys`,默认 4096):超限丢最久未用的 key,该线程下一轮多抽一批(与重启同语义)。
- **成功才前进**:`_do_update_memory_sync_impl` 里 `if success and not bypass_watermark: watermark_set(...)`。抽取失败(LLM 报错、解析失败、闸门全拒)→ 水位不动 → 下轮重抽。这是"降级不丢"的最后一环。

**`bypass_watermark` 的语义**:紧急冲刷的 context 携带的只是"即将被删的消息子集",它**不读水位、不进全量喂送判断、成功后也不前进水位**——如果从子集长度推进水位,会把对话水位倒退到子集末尾(远早于真实最新消息),下一轮正常抽取会漏掉中间未抽取的轮次。紧急快照与正常更新因此互不干扰。

### 3.6 flush 族与优雅停机

- `flush()`:取消 Timer 立即同步处理(测试用)。
- `flush_nowait()`:`_schedule_timer(0)` 后台立即处理。
- `flush_sync(timeout)`:**优雅停机 drain**。队列纯内存 + Timer 是 daemon 线程,滚动部署/SIGTERM 时最后一次 Timer 触发前的待处理更新会丢;Gateway 停机流程在 `memory.shutdown_flush_timeout_seconds`(host 共享字段,默认 **30s**,1-300)预算内调它。它处理两个裸 `flush()` 会踩的竞态:① 先 join 正在排空、已把 context 拽出队列的 in-flight worker(否则 `flush` 看到 `_processing=True` 就空转返回,worker 随后被 exit 杀死、已拽出的 context 全丢);② 在 daemon 线程上跑 flush 并用 `Event.wait` 等——LLM 调用不可中断,timeout 是真实硬停,超时丢尾部(与"不 flush"同向失败,只是范围缩小到尾部)。配置注释特别提醒:30s 必须能塞进 K8s 的 `terminationGracePeriodSeconds`(与 channel/scheduler 的停机合在一起算),否则 drain 中途被 SIGKILL。每个待处理项一次 LLM 调用,IM 大批次场景要调高。
- `cancel_by_agent(...)`:删除/清空记忆前丢弃同 scope 待处理 context。`DeerMem.clear_memory()` 在清空前**和**清空后各 cancel 一次——防止 stale 的 debounce Timer 在清空过程中把旧事实又写回来。只动仍在 `_items` 里的 context;已被 worker 拽出、正在 LLM 调用中的不打断(那是 durable outbox 的职责,不是内存队列的)。`user_id=None` 只匹配遗留的无用户根,语义与存储一致。

---

## 4 提取:一次 LLM 调用,六类决策

`_process_queue` 排到某个 context 时调用 `MemoryUpdater.update_memory(...)`,最终落到 `_do_update_memory_sync_impl`——一次**同步 `model.invoke()`**。为什么同步?抽取跑在 Timer/executor 工作线程上,用同步 HTTP 走与 lead agent 完全隔离的连接池,规避跨事件循环连接复用 bug(issue #2615)。若 `update_memory` 恰好被事件循环里的调用方触发(如从 LangGraph 节点直调),会 offload 到进程级 `_SYNC_MEMORY_UPDATER_EXECUTOR`。

### 4.1 prompt 的四路输入

`_prepare_update_prompt` 组装,模型同时看到:

1. **当前记忆快照 `<current_memory>`**:该 `(user, agent)` 桶完整记忆文档的 JSON(`json.dumps`),即六段摘要 + 该 Agent 事实列表。序列化前 `_escape_memory_for_prompt` 把**每个字符串叶子做 `html.escape`**——`json.dumps` 只转义引号和反斜杠,`<` `>` `&` 原样保留,若某条用户可影响的事实内容是 `</current_memory><evil>...`,就能逃出块边界伪造权威区(issue #4044);逐叶子转义不会破坏 JSON 结构(转义发生在序列化前,`json.dumps` 会重新加引号)。id/时间戳等受控字段不含这些字符,转义是空操作。
2. **本轮对话 `<conversation>`**:过滤后的 user+final-AI 消息,经 `format_conversation_for_update` 渲染——**每条消息超过 1000 字符只保留头 500 + 尾 500**,中间 `...[truncated]...`;**转义在截断之后**做,保证边界不会劈开转义序列、`</memory>` 之类不能借截断潜入。
3. **条件提示段**:信号提示 `correction_hint`(名字是历史遗留,现在承载全部 6 类信号:命中 correction 时提示"记录为 confidence ≥ 0.95 的 correction 类事实,且仅当它是可复用的用户级偏好;对当前任务/文件的纠正是 thread/project 级,不得入库")、`staleness_review_section`(过期复查候选,§6.3)、`consolidation_section`(碎片合并,默认关闭)。三者只有条件满足才渲染非空,且**都是"代码先圈定候选、LLM 只能在候选里选"**——模型没有越权删/合的窗口。
4. **静态系统指令**:`core/prompts/memory_update.chat.yaml` 的 system 消息**完全静态**(无变量),每次渲染逐字节相同 → 前缀缓存友好。它教模型"怎么记":六段摘要的编写指南、结构化反思(错误检测/纠正检测/项目约束发现)、事实提取准则(**不建事实**:当前任务目标、验收标准、工作区状态、文件/commit 状态、项目级约束、一次性授权、文件上传事件)、每条事实/摘要必须带 `scope`/`durability`/`authority` 三标签、输出 JSON schema。

模板可用 `backend_config.prompts_dir` 覆盖,甚至按 Agent 分子目录(`{prompts_dir}/{agent_name}/memory_update.chat.yaml`)。**自定义模板必须包含三分类字段**,否则抽取写路径 fail-closed(§6.1)——旧模板导致抽取整体拒绝,并靠拒绝计数与高拒绝率告警暴露。

### 4.2 输出:六类决策 + 容错解析 + "只删不写"禁令

模型返回一份 JSON,涵盖六类决策:摘要更新(`user`/`history` 六段,各带 `shouldUpdate`)、新事实 `newFacts[]`、矛盾删除 `factsToRemove[]`(可带 `replacementFactIndex` 指向替代品)、过期删除 `staleFactsToRemove[]`(KEEP 不输出)、过期延长 `staleFactsToExtend[]`、碎片合并 `factsToConsolidate[]`。把"更新摘要、抽事实、删矛盾、判过期、做合并"压进**一次调用**,既省 API,又让模型同时看到"当前记忆 + 本轮对话"做跨时间判断(如"这条旧事实已被新对话推翻")。

消费管线三段逐层收紧(`updater.py`):

```
① _parse_memory_update_response   容错 JSON 提取
   模型可能包 thinking/散文/markdown fence → 逐个 '{' raw_decode,
   找第一个含 user+history+newFacts 三个顶层键的合法 JSON 对象
② _normalize_memory_update_data   规范化
   逐条校验 content/category/confidence/expected_valid_days/scope/durability/authority,
   畸形条目丢弃;但 factsToRemove 存在而 newFacts 全部畸形 → 整体抛错
③ _apply_updates                  闸门 → 去重 → max_facts 裁剪 → 冲突删除 → 合并
```

第②步的安全规则值得单独强调:**绝不执行"只删不写"**——模型说"删掉旧事实 X"却给不出合法的新事实,整个更新中止而不是把 X 删了。第③步的逐条闸门见 §5。

### 4.3 失败语义:吞掉、不前进、下次再抽

`update_memory` 的整个主体包在 try/except 里,任何失败(LLM 报错、解析失败、存储冲突)返回 `False` 并记日志,**从不向上抛**——队列循环也因此能继续处理下一个 context(否则一条坏更新会拖垮整批)。失败的后果只是"这次没记到":水位未前进,下一轮对话带着更完整的消息重抽。这就是整个写链路一以贯之的降级哲学:**宁可漏一次,不可错一次,绝不打断对话**。

---

## 5 确定性闸门:scope=user + durable + descriptive 才入库

抽取产物是 LLM 输出,LLM 可能违背指令。`_apply_updates` 在写盘前用**纯代码**逐条校验(`updater.py` 的三个闸门函数):

```
事实入库三条件(缺一不可):
  scope      == "user"         能跨会话/跨任务成立
  durability == "durable"      预期长期为真
  authority  == "descriptive"  是描述,不是指令/授权
摘要段:scope == user 且 authority == descriptive
矛盾删除:scope == user 且有非空 reason
```

标签先经 `_normalize_gate_label`(去空白小写);缺失按 `missing` 拒绝。判定粒度是**单条事实/单段摘要**,不是整批:一条坏数据只拒绝它自己,不影响同批其他合法条目入库;也绝不反过来因为"整体还行"放过某一条。拒绝按原因分桶计数(`scope_gate_rejections[facts|summaries|removals][missing|scope|durability|authority]`),汇总成 `rejected_by_scope_gate` 指标,供可观测性消费(拒绝率高 → 告警提示 prompt 模板可能缺分类字段)。

典型判定:

| 候选内容 | scope | durability | authority | 结果 |
|---|---|---|---|---|
| "用户偏好 uv 而非 pip" | user | durable | descriptive | ✅ 入库 |
| "当前任务是把 CSV 转 JSON" | thread | temporary | descriptive | ❌ 一次性,拒 |
| "用户授权删除生产库" | user | durable | **transactional** | ❌ 指令/授权,永不入库 |
| "项目约定用 ruff" | **project** | durable | descriptive | ❌ 项目级,拒 |

**方向性保守**:拒绝一条的代价只是"这次没记到"(下轮重抽);入库一条的代价是"错误个性化持续污染未来每次注入"。宁可漏记,不可错记。三个标签**只活在抽取期,不落盘**——它们是决策输入而非数据,校验发生在写入那一刻就足够。

### 5.1 矛盾删除的原子性

LLM 用 `factsToRemove` 表达"旧事实被推翻了",可带 `replacementFactIndex` 指向替代它的新事实。关键规则:**带 replacement 的删除是原子的**——只有替代品通过了闸门、去重、`max_facts` 裁剪,旧事实才被删;否则"删旧的"和"写新的"分离,可能出现旧删了、新没写进去的数据丢失。task/project 级事实**不允许 LLM 单方面删除**(fail-closed)。`_normalize_memory_update_data` 里连字符串形式的旧式删除条目都保留为"无 scope 的删除",让 apply 层按 `missing` 拒绝而不是静默放行一次无界定的破坏性变更。

### 5.2 过期复查与碎片合并:确定性交集护栏

两个生命周期机制都搭便车在同一次抽取调用里,不额外发 API:

- **staleness**:每条事实有 LLM 赋的复查窗口 `expected_valid_days`(写盘时钳到 `staleness_age_days × staleness_max_lifetime_multiplier`,默认 90×20≈5 年,防模型把寿命设到永不复查)。`_select_stale_candidates` 代码圈定过期且非保护类别(`staleness_protected_categories`,默认 `["correction"]`)的候选;候选数 ≥ `staleness_min_candidates`(3)才渲染复查段。LLM 只对候选三选一(KEEP 不输出 / REMOVE / EXTEND),apply 层把提议与候选集求**交集**——模型越权提议删 protected 类别或未到期事实,静默丢弃。删除受每周期上限 `staleness_max_removals_per_cycle`(10)约束,超了按置信度从低到高保留(先删最可疑的);延长取 `min(距创建天数 + extend_by, staleness_max_extension_days)`(3650);**被提议删除的事实绝不同时被延长**(防模型自相矛盾)。护栏的执行独立于 `staleness_review_enabled` 开关——开关关掉只是模型看不到候选段,apply 层保护照常。
- **consolidation**(默认 `consolidation_enabled: false`,有损故默认关):同类别碎片 ≥ `consolidation_min_facts`(8)才圈组;源必须存在、组间不重叠、每组合并上限 `consolidation_max_sources`(8)、每组决策上限 `consolidation_max_groups_per_cycle`(3)。

### 5.3 容量淘汰:max_facts 与两种策略

记忆是只增不减的仓库,注入预算却固定(`max_injection_tokens` 2000)。`max_facts`(默认 **100**)超限时按 `eviction.py` 的确定性策略裁剪——所有写入路径(自动抽取、工具 CRUD、导入)共用同一套:

- **`confidence`(默认)**:保留置信度最高的事实,淘汰即永久删除(仅留 metadata 审计)。
- **`hybrid-v1`(opt-in)**:综合三维得分——置信度(权重 **0.65**)+ 显式确认新鲜度(**0.25**,半衰期 90 天)+ 查询驱动的访问热度(**0.10**,半衰期 30 天),三者权重和必须精确为 1.0(config 构造时校验)。另为 correction 保留插槽(`eviction_correction_reserved_fraction` 0.10 / 上限 10 个)——纠正类事实永不因普通容量竞争被挤掉。确认新鲜度 `_confirmation_freshness` 有确认时间戳(`lastConfirmedAt`)时按时间衰减,否则退回"创建时间 × 0.5"(创建是弱于显式确认的证据)。
- **shadow 模式**(`fact_eviction_shadow_enabled`):confidence 策略执行时并行计算 hybrid-v1,记录分歧到 metadata-only 审计,不实际改变淘汰。
- **访问热度只增于检索**:`search()` 命中返回结果时才 `record_fact_accesses`(仅 hybrid 跟踪开启时);**prompt 注入和 `get_context()` 不增加热度**——被动读到不算"用户主动需要"。审计与 usage 侧车数据在 agent 的 `.metadata/` 目录下,**不改写** canonical Markdown 的时间戳与 revision;用户删除/清空必须连带清理侧车数据。

### 5.4 reinforcement:确定性确认门

用户说"对,就这样"不能只靠 LLM 自觉升级事实。确认机制(`_apply_updates` 开头)刻意设计成**两级**:

1. **确定性门(批级,代码)**:`detect_signals` 证明"最近 6 条过滤后消息里有一条 human 消息命中了 reinforcement 模式"——只证明存在确认行为,不证明确认的是哪条事实。
2. **绑定(模型)**:LLM 输出 `factsToReinforce[{id, scope, reason}]`,把信号绑定到**已存在的具体事实 id**——必须 `scope == user` 且带非空 reason,否则不算数。

只有当 hybrid 跟踪开启(`fact_eviction_policy == hybrid-v1` 或 shadow)且 `reinforcement` 信号在场时才应用:`lastConfirmedAt = now`、`confirmationCount += 1`,供 hybrid 的确认新鲜度消费。绑定失败最多是"没确认上",绝不会无中生有地造出确认。

---

## 6 pre-summarization flush:压缩前把消息抢救进队列

`SummarizationMiddleware`(链 18)压缩上下文时会把旧消息从 state 里删掉——**没有抢救钩子,这些对话就永远丢失**,而它们本该是记忆抽取的输入。`memory_flush_hook`(`agents/memory/summarization_hook.py`,28 行)就是为此存在的:

```python
def memory_flush_hook(event: SummarizationEvent) -> None:
    if not get_memory_config().enabled or not event.thread_id:
        return
    user_id = resolve_runtime_user_id(event.runtime)
    get_memory_manager().add_nowait(
        event.thread_id,
        list(event.messages_to_summarize),   # 即将被删的消息子集
        agent_name=event.agent_name,
        user_id=user_id,
    )
```

注册位置:`summarization_middleware.py` 的 `create_summarization_middleware` 里,当 `memory.enabled 且 not skip_memory_flush` 时把 `memory_flush_hook` append 进摘要中间件的 **hook 列表**;摘要中间件在真正压缩前遍历 hook 列表、构造 `SummarizationEvent(thread_id, agent_name, messages_to_summarize, runtime, ...)` 逐个触发。三个关键行为:

1. **走 `add_nowait` 而非 `add`**:`add_nowait` 用 `bypass_watermark=True` 入队并立即 `_schedule_timer(0)` 处理。等常规 30s 去抖,消息可能已被压缩删掉,必须马上抽。
2. **绕过水位**:紧急快照是"删前一次性截图",按它自己的长度推进水位会**倒退**对话水位、让下轮正常抽取把已删内容当新内容重抽;`bypass_watermark` 让快照既不全量比对也不推进水位(§3.5)。
3. **与普通更新共存**:队列匹配键含 `bypass_watermark` 维度,紧急冲刷不覆盖同 key 待处理的普通更新(§3.3)。

**子代理双保险**:`build_subagent_runtime_middlewares`(在 `tool_error_handling_middleware.py` 装配)强制 `skip_memory_flush=True`,摘要工厂因此不挂这个 hook。原因与子代理不挂 `MemoryMiddleware` 同源:**子代理与父线程共享同一个 `thread_id`**,若子代理内部压缩也冲刷记忆,会把子代理的中转对话污染进父线程的持久记忆。两处防护(子代理无捕获中间件 + 无冲刷钩子)互为冗余。

**压缩不掉已取事实的另一半保障**:即便冲刷钩子没挂(如摘要被禁),已经被正常抽取、水位前进过的消息也不受影响——事实已落盘,删对话只丢"还没轮到抽取的尾部",而那一部分正是冲刷钩子抢救的对象。

---

## 7 读链路与注入回环:DynamicContext 怎么把记忆放回去

写链路产出的记忆,靠 `DynamicContextMiddleware`(链 **14**,lead-only 链里第一个被 append 的)在**每次模型调用前**放回上下文。它是记忆读链路最复杂的中间件,两条安全红线贯穿始终。

### 7.1 读取路径与 <memory> 包裹

`before_agent` → `_inject` → `_build_full_reminder(runtime)` → `_get_memory_context(agent_name, app_config)`(`lead_agent/prompt.py`):

1. 门:`not config.enabled or not config.injection_enabled` → 返回空。`injection_enabled: false` 时"只记不用"——照常抽取存储,但不注入。
2. `get_memory_manager().get_context(user_id, agent_name)`(DeerMem 实现):读该 `(user, agent)` 桶完整记忆文档 → `format_memory_for_injection(...)` 格式化 + 预算裁剪 → 返回正文。
3. 调用侧包 `<memory>\n{正文}\n</memory>`。
4. **失败策略**:读记忆的任何异常默认**吞掉返回空串**(fail-open)——记忆读不出来,对话照常跑,只是这次没有记忆;只有配置 `backend_config.failure_policy.read: fail_closed` 且异常是 `MemoryManagerError` 时才 re-raise(有记忆系统强依赖的部署可选)。

### 7.2 预算:双池结构与安全截断

`format_memory_for_injection`(`deermem/core/prompt.py`)做三件事:**摘要六段无条件全注 → 事实按 confidence 严格降序 → 预算内贪心**。预算分双池:

| 预算池 | 默认 | 用途 | 超支行为 |
|---|---|---|---|
| 特权预留 `guaranteed_token_budget` | 500 | `guaranteed_categories`(默认仅 `correction`) | 超支向上溢出主预算,自身绝不截断 |
| 主预算 `max_tokens` | 2000 | 摘要 + 普通事实 | 按置信度贪心,满即停 |

correction 必须是特权类——"别再犯同样的错"若因预算满被漏掉,代价远大于少带一条普通偏好。填充顺序:guaranteed 先占预留区(≤500 不碰主预算,>500 溢出挤占)→ 摘要 → 普通事实按置信度降序。**安全截断规则**把 `Facts:` 块当受保护后缀,溢出只从尾部裁 `User Context`/`History`,绝不裁 Facts(尤其 guaranteed 行);correction 多时总输出上限被抬到 `max_tokens + guaranteed_actual_usage`,保护 guaranteed 行不被截断误删。计费默认 `tiktoken`(精确,首次可能触发 BPE 下载),可切 `char`(离线 CJK 感知估算)。

**注入文本同样做 HTML 转义**(`<` `>` `&`),与 §4.1 的 `_escape_memory_for_prompt` 同源防护——防记忆内容里的 `</memory>` 逃出块边界被当成指令(issue #4097 同族)。

### 7.3 ID-swap 注入与冻结快照

记忆不进 system prompt(保持系统提示词完全静态 → 前缀缓存命中),而是作为隐藏消息在**首条用户消息之前**注入,整个会话只注入一次、随后冻结:

```
第一轮:  [SystemMessage(日期) id=原始用户消息id]
          [HumanMessage(<memory>…) id={原始id}__memory]   ← 仅当有记忆
          [HumanMessage(真正用户输入) id={原始id}__user]
后续轮:  原样复用 → 前缀缓存每次命中
```

技巧是 **ID 交换**:`SystemMessage` 取走原始用户消息的 id(占它的位置/序号),记忆块与真实输入用 `__memory`/`__user` 后缀 id 跟在后面;LangGraph 的 `add_messages` reducer 对同 id 消息就地覆盖,净效果是每轮用户输入前挂着同一组冻结提醒,消息列表结构不再变化。跨午夜只更新日期、不重注记忆(`reminder_date` 结构化字段只挂 SystemMessage,反向扫描优先读它;旧 checkpoint 才解析 content,且正则只跑在 SystemMessage 上——记忆 HumanMessage 是用户可影响的,绝不对它做内容解析,堵死"在记忆里伪造 `<current_date>`"的日期欺骗)。

**安全红线**:框架数据(日期)进 `SystemMessage`(框架权威);记忆内容用户可影响,**只能进 `HumanMessage`(role=user),永不携带系统权限**(OWASP LLM01,issue #3630)——否则用户可影响的记忆放进 system role 就获得了超越输入的指令权。docstring 明确写着 "memory stays at role:user"。防递归的 ID 交换红线:注入目标用 `endswith("__user")` 拒绝上一轮产物,防 `__user__user__user` 幽灵重放。异步路径整体 `asyncio.to_thread` offload 并套 **5 秒硬超时**——`_inject` 里有同步文件 IO 和可能的 tiktoken 下载,不 offload 会卡死所有并发 HTTP handler;超时则本次不注入(优雅降级),已冻结在 checkpoint 里的旧记忆仍在。

**注入回环的闭合与自激防护**:本轮的 `<memory>` 是 DynamicContext 注入的隐藏 HumanMessage(`hide_from_ui` + `dynamic_context_reminder` 标记);对话结束 MemoryMiddleware 捕获的正是含这些隐藏消息的完整列表——若不加过滤,抽取器会把"自己上轮注入的记忆"再抽一遍,形成自我放大。环的闭合处就是 §2.2 的 `hide_from_ui` 剔除:p0 `__memory` 消息被过滤函数直接跳过(只保留 `{id}__memory` 里 `id` 那部分真实用户输入的副本 `__user` 消息),记忆内容永远不会回到记忆里。**guardrails 因此是双层的**:写回时 scope=user+durable+descriptive 三标签确定性闸门(§5)只允许"用户画像级描述"入库;入队时框架消息剔除保证抽取器看到的对话不含系统自产内容。

### 7.4 run-level 记忆身份

`before_agent` 后调 `_record_effective_memory`:`_effective_memory_message` 在两条路径里定位"本 run 真正生效的记忆块"(本次注入产生的 `__memory` 消息,或经 `CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY`——worker 在 run 开始前记录的消息 id 集合——定位 checkpoint 里延续下来的冻结记忆),对它做 SHA-256,记一条 `context:memory` run 事件(`content_sha256`,不存全文)。Gateway 从不受信输入剥掉 `dynamic_context_reminder`/`hide_from_ui` 标记,用户伪造不了带这些标记的记忆消息。运维拿 hash 跨 run 比对"这次用的记忆与上次是否同一份"。

---

## 8 关键参数速查

| 配置项 | 默认 | 含义 |
|---|---|---|
| `memory.enabled` | true | 总开关(调用点门) |
| `memory.mode` | `middleware` | 被动抽取 / `tool` 模型直管,互斥 |
| `memory.injection_enabled` | true | 是否注入 `<memory>`(可"只记不用") |
| `memory.shutdown_flush_timeout_seconds` | 30 | 优雅停机 drain 预算(host 共享) |
| `memory.manager_class` | `deermem` | 后端选择(fail-fast,不静默降级) |
| `backend_config.debounce_seconds` | 30 | 去抖窗口(1-300) |
| `backend_config.queue_max_depth` | 1000 | 背压上限;0=不限;信号/紧急永远准入 |
| `backend_config.watermark_max_keys` | 4096 | 对话水位 LRU 上限 |
| `backend_config.max_facts` | 100 | 事实容量上限(10-500) |
| `backend_config.fact_confidence_threshold` | 0.7 | 置信度写入门槛 |
| `backend_config.fact_eviction_policy` | `confidence` | 容量淘汰策略(`hybrid-v1` opt-in) |
| `backend_config.staleness_*` | 90d/3/10/… | 过期复查窗口、触发数、周期删除上限等 |
| `backend_config.consolidation_enabled` | false | 碎片合并(有损,默认关) |
| `backend_config.guaranteed_categories` / `guaranteed_token_budget` | `["correction"]` / 500 | 注入特权池 |
| `backend_config.max_injection_tokens` | 2000 | 注入主预算 |
| `backend_config.model.model` | 空 | 抽取 LLM;空 = 用宿主默认模型 |

> 注:`debounce_seconds`/`max_facts`/`staleness_*` 等 DeerMem 私有字段历史上是 `memory:` 顶层键,现已归入 `memory.backend_config`;旧配置加载时会自动迁移并告警(见 `config/memory_config.py` 的 `_LEGACY_DEERMEM_FIELDS`)。

---

## 小结

把整条流水线压缩成一句话:**`MemoryMiddleware`(链 22)在每轮对话结束后把原始消息交给管理器;DeerMem 在入队前做四重过滤(类型白名单 + `hide_from_ui` 剔除 + trivial 剔除 + 上传块剥离)并要求"有来有回";`MemoryUpdateQueue` 按 `(thread,user,agent)` 去抖合并,`queue_max_depth` 背压只拒普通更新、信号与紧急冲刷永远准入;`MemoryUpdater` 在 Timer 线程上用一次同步 LLM 调用产出六类决策的 JSON;apply 层以 scope=user+durable+descriptive 的确定性闸门逐条 fail-closed,再经 staleness/consolidation/容量淘汰的确定性交集护栏;`SummarizationMiddleware` 压缩前通过 `memory_flush_hook`(add_nowait + bypass_watermark)抢救即将被删的消息;`DynamicContextMiddleware`(链 14)在每次模型调用前把格式化好的 `<memory>` 以 role=user 的隐藏消息冻结注入首条用户输入前——闭环。

写链路处处是"降级不丢":队列失败下轮重抽、水位失败不前进、背压只延迟不丢失、冲刷与普通更新共存。读链路处处是"宁缺毋滥":三标签闸门拒绝一切非用户级画像内容、注入只带得下预算内的最高置信度事实、correction 永远有特权席位、读失败默认空上下文也不拖垮对话。这两个原则——**异步写、保守读**——就是 DeerFlow 记忆系统所有设计的底层逻辑。
