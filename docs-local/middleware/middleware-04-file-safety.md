# 中间件文档 04 — 文件安全

本文档详细解析 DeerFlow 中间件链中两个与文件安全 / 工具结果质量相关的中间件：

- `ReadBeforeWriteMiddleware`（位置 11）：文件写入门禁，强制"先读后写"的版本门
- `ToolProgressMiddleware`（位置 12）：工具结果质量守卫，基于状态机检测工具调用停滞与重复

两者在中间件链中位置相邻且互为配合：`ReadBeforeWriteMiddleware` 处于 `ToolProgressMiddleware` 的外层，使得被门禁阻塞的写操作不会进入 `ToolProgressMiddleware` 的状态机计数，避免一次合理的"拒绝"被误判为工具故障。

---

# 1. ReadBeforeWriteMiddleware

## 概述

`ReadBeforeWriteMiddleware` 是一个版本门：当 agent 想要修改一个已存在的文件时，必须先在本次会话上下文中（`state["messages"]` 里）通过 `read_file` 读取过该文件的当前版本。门禁通过对比"读取时记录的 sha256 内容哈希"和"当前文件内容的 sha256"来判定读取是否仍然有效；任何写入都会改变文件哈希、自动失效此前的读标记，从而强制连续两次修改之间必须重新读文件。

## 为什么需要这个中间件

### 场景痛点

Agent 在一个长任务中连续 5 次追加同一段报告到 `report.md`，导致文件末尾有 5 份重复内容（issue #3857）。更常见的情况是：agent 在一次 `write_file` 成功后，下一个 tool call 又再次写入同一文件，却没有先读取当前版本——两次写入之间丢失了第一次写入的结果，或者产生重复。在并发场景下，同一 turn 中两个 `write_file` 都基于同一份过期的读标记通过门禁，各自落地后造成最终文件内容混乱。

### 为什么模型自身无法避免

长上下文中的模型会遗忘自己之前写过什么，尤其是当中间夹杂了大量中间工具结果后，模型更容易丢失对文件状态的精确记忆。此外，模型倾向于重复使用曾经成功的工具调用模式——如果第一轮"读→写"成功让任务前进了一小步，模型在下一轮很可能直接调用 `write_file` 而不先 `read_file`，因为它"记得"自己刚读过且文件内容没变。模型没有对文件系统的持久化感知，无法在两次工具调用之间自动跟踪文件的哈希状态。

### 解决思路

通过拦截写操作前的文件读取标记（sha256 内容哈希）对比，强制 agent 在每次修改文件前必须重新读取当前版本，任何写入自动失效此前所有读标记。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py` |
| 实现的钩子 | `wrap_tool_call`、`awrap_tool_call` |
| 持久化 | 状态写在消息的 `additional_kwargs` 上（`state["messages"]`）；每进程的锁命名空间为 `WeakValueDictionary` |
| 配置依赖 | `config.yaml -> read_before_write.enabled`（默认开启），中间件按需挂载 |
| 作用范围 | 工具调用 `read_file`、`write_file`、`str_replace` |
| 装配位置 | 中间件链位置 11，位于 `ToolProgressMiddleware` 与 `ToolErrorHandlingMiddleware` 之外层 |
| 背景动因 | Issue #3857：lead agent 反复把同一份报告小节追加 5 次的"只追加、不读回"故障 |

## 核心逻辑

### 不变量（模块 docstring 摘录）

- **工具保持无状态**：读标记（`sha256`）仅盖在 `read_file` 的 `ToolMessage.additional_kwargs` 上，门禁的状态完全活在 `state["messages"]`。
- **Summarization 删除读结果 = 删除标记**：当压缩上下文丢掉了读文件内容，门禁永远不可能通过，因为读标记和读结果同生共死。
- **写入永不刷新标记**：每次成功写入都会改变文件哈希，因此使此前的所有读都自动失效，迫使下一次修改前重新读。
- **门禁检查与工具执行按 `(scope, path)` 串行**：LangGraph 在一个 `AIMessage` 中并发执行多个 tool call，若没有临界区，同一 turn 内两个写入可能都基于同一个过期的标记通过，再各自落地。同一把锁也覆盖 `read_file` + 标记盖戳，确保盖的哈希就是模型实际看到的版本的哈希。
- **Fail-open（失败开放）**：若门禁自己无法 inspect 文件（sandbox 抖动、二进制内容、或 AIO/E2B 这种把读取失败当作 `"Error: ..."` 字符串返回的 sandbox），则放行让工具自身去产生错误。

### 钩子分发

`wrap_tool_call` / `awrap_tool_call` 先按工具名分流：

```python
_READ_TOOLS = frozenset({"read_file"})
_GATED_WRITE_TOOLS = frozenset({"write_file", "str_replace"})

if name in _GATED_WRITE_TOOLS:
    # 1. 解析 path
    # 2. 取 (scope, norm_path) 锁
    # 3. 在锁内执行 _check_write_gate：
    #    - 若返回非 None（被阻塞），用 normalize_tool_result 包装为 deerflow_tool_meta 标记结果
    #    - 否则调用 handler(request) 执行真实写
if name in _READ_TOOLS:
    # 1. 解析 path
    # 2. 取同一把 (scope, norm_path) 锁
    # 3. 在锁内：先 handler(request) 执行读取
    #    再 _attach_read_mark 盖戳
return handler(request)
```

异步版本中一个关键细节：由于 `threading.Lock` 可能由其它线程释放（先 acquire 再在事件循环线程 release），代码用 `asyncio.to_thread(lock.acquire)` 把 acquire 委托到 worker 线程，并在 `finally` 中直接 `lock.release()`：

```python
lock = self._lock_for(request, path)
await asyncio.to_thread(lock.acquire)
try:
    blocked = await asyncio.to_thread(self._check_write_gate, request)
    if blocked is not None:
        return normalize_tool_result(blocked)
    return await handler(request)
finally:
    lock.release()
```

### 锁策略

每进程一个 `WeakValueDictionary[tuple[str, str], threading.Lock]`，key 是 `(scope, norm_path)`：

```python
_GATE_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.Lock] = weakref.WeakValueDictionary()
_GATE_LOCKS_GUARD = threading.Lock()

def _get_gate_lock(scope: str, norm_path: str) -> threading.Lock:
    key = (scope, norm_path)
    with _GATE_LOCKS_GUARD:
        lock = _GATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _GATE_LOCKS[key] = lock
        return lock
```

- `WeakValueDictionary` 让没有引用的锁能被 GC，避免无限增长。
- 与 `sandbox/file_operation_lock.py` 同样的弱引用模式，但**命名空间独立**：工具内部的文件锁只保护 mutation，本锁还覆盖授权决策阶段。

`_lock_scope` 把锁按 thread 或 sandbox 隔离，避免无关 agent 互斥：

```python
@staticmethod
def _lock_scope(request: ToolCallRequest) -> str:
    context = getattr(request.runtime, "context", None)
    if isinstance(context, dict):
        thread_id = context.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    state = request.state
    if isinstance(state, dict):
        sandbox_state = state.get("sandbox")
        if isinstance(sandbox_state, dict):
            sandbox_id = sandbox_state.get("sandbox_id")
            if isinstance(sandbox_id, str) and sandbox_id:
                return sandbox_id
    return "global"
```

### 哈希与标记

内容哈希用 sha256，路径用 `posixpath.normpath` 标准化（保证 `/a/b/../c` 与 `/a/c` 视作同一路径）：

```python
def _normalize_mark_path(path: str) -> str:
    return posixpath.normpath(path)

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

READ_MARK_KEY = "deerflow_read_mark"
```

读标记的形态是 `ToolMessage.additional_kwargs["deerflow_read_mark"] = {"path": norm_path, "hash": sha256_hex}`。

### 写门禁判定 `_check_write_gate`

```python
def _check_write_gate(self, request: ToolCallRequest) -> ToolMessage | None:
    tool_call = request.tool_call
    path = self._requested_path(request)
    if path is None:
        return None
    try:
        current = self._content_reader(request.runtime, path)
    except FileNotFoundError:
        # write_file 创建文件；str_replace 自报错误
        return None
    except Exception:
        logger.warning("...; allowing the write (fail-open)", path, exc_info=True)
        return None
    if current.startswith(_UNINSPECTABLE_CONTENT_PREFIX):
        # AIO/E2B 风格 sandbox：missing 与 unreadable 不可区分，fail open
        logger.debug("...; allowing the write (fail-open)", path)
        return None
    norm_path = _normalize_mark_path(path)
    if self._latest_mark_hash(request.state, norm_path) == _content_hash(current):
        return None
    tool_name = str(tool_call.get("name", "write"))
    return ToolMessage(
        content=_BLOCK_MESSAGE.format(tool_name=tool_name, path=path),
        tool_call_id=str(tool_call.get("id", "")),
        name=tool_name,
        status="error",
    )
```

`_BLOCK_MESSAGE` 模板既给出可操作指引，强调"任意写都会失效此前读，因此每次修改前都要重读"，并提示可只读相关区段（例如追加前读最后约 30 行即可）。

### 最新标记查找 `_latest_mark_hash`

`reversed(messages)` 从最新到最旧扫描，找到第一个属于目标 `norm_path` 的 `deerflow_read_mark`：

```python
@staticmethod
def _latest_mark_hash(state: Any, norm_path: str) -> str | None:
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return None
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        mark = (message.additional_kwargs or {}).get(READ_MARK_KEY)
        if isinstance(mark, dict) and mark.get("path") == norm_path:
            mark_hash = mark.get("hash")
            return mark_hash if isinstance(mark_hash, str) else None
    return None
```

"最新标记"语义配合"写入不刷新标记"的不变量，构成版本比较的核心：

- 若最新标记哈希 == 当前文件哈希 → 通过，允许写。
- 若哈希不同（说明此前读的是更老的版本，或文件已被某次写/外部修改改过）→ 阻塞，要求重读。
- 若没有标记 → 阻塞（针对已存在文件）。

### 标记盖戳 `_attach_read_mark`

仅在 `read_file` 成功执行后调用：

```python
def _attach_read_mark(self, request: ToolCallRequest, result: ToolMessage | Command) -> None:
    path = self._requested_path(request)
    if path is None:
        return
    message = self._extract_tool_message(result)
    if message is None or message.status == "error":
        return
    try:
        content = self._content_reader(request.runtime, path)
    except Exception:
        logger.debug("read-before-write mark skipped for %r: file not hashable", path, exc_info=True)
        return
    if content.startswith(_UNINSPECTABLE_CONTENT_PREFIX):
        logger.debug("read-before-write mark skipped for %r: error-string read channel", path)
        return
    message.additional_kwargs[READ_MARK_KEY] = {
        "path": _normalize_mark_path(path),
        "hash": _content_hash(content),
    }
```

注意：盖的哈希**不是**从 `result.content` 解析，而是再调用一次 `self._content_reader`，确保哈希基于"读取之后、写之前"的真实当前内容。失败（FileNotFoundError、二进制、`Error:` 前缀字符串）一律跳过盖戳，保持 fail-open。

`_extract_tool_message` 支持两种返回类型：直接的 `ToolMessage` 或 `Command`（其中可能包含多条 messages，取最后一条 `ToolMessage`）。

### 阻塞结果为什么走 `normalize_tool_result`

被阻塞的写不会经过 `ToolErrorHandlingMiddleware`，因此 `deerflow_tool_meta` 元数据不会被它自动盖戳。门禁直接调用 `normalize_tool_result(blocked)`：

```python
if blocked is not None:
    # Stamp deerflow_tool_meta so ToolProgressMiddleware can classify
    # the blocked write even though it bypasses ToolErrorHandlingMiddleware.
    return normalize_tool_result(blocked)
```

这保证 `ToolProgressMiddleware` 仍能正确分类这个被阻塞的结果（元数据中 `recoverable_by_model=True`），不会因为结果缺少元数据而误触 warn 警告。

### Fail-open 场景汇总

| 场景 | 行为 |
|------|------|
| `path` 参数缺失 / 非字符串 | 直接放行 `handler(request)` |
| `_content_reader` 抛 `FileNotFoundError` | 视为新文件创建，放行 |
| `_content_reader` 抛其它异常 | 记 warning，放行（fail-open） |
| 返回内容以 `"Error:"` 开头（AIO/E2B 错误字符串 channel） | 记 debug，放行（fail-open），不盖标记 |
| 读 `result.status == "error"` | 不盖标记（让工具自身的错误被模型看见） |
| `content_reader` 在盖戳阶段失败 | 跳过盖戳，保持 fail-open |

## 关键设计决策

### 1. 状态放在 messages 而非单独的 state channel
设计目标是让"读结果被 summarization 删除时，标记也一起被删除"。如果把标记放在独立的 state 字段里，summarization 必须额外感知这层耦合，否则会出现"内容已不在上下文，但门禁仍认为已读"的故障。

### 2. 写入永不刷新标记
若写后自动盖新标记，模型就可以连续写多次而无需中间读一次——这是 issue #3857 的原始故障模式。强制"每次修改前必读"避免了重复追加。

### 3. 串行化门禁检查与工具执行
单 turn 内 LangGraph 并发执行多个 tool call。若只锁住 mutation，两个并发写都会基于同一过期标记通过门禁、然后各自落地，产生丢失更新。把门禁 check 也放在同一把锁里，把"读+盖戳"也放在同一把锁里，构成完整的临界区。

### 4. Fail-open 而非 fail-closed
sandbox 可能短暂不可用；二进制内容、AIO/E2B 错误字符串 channel 都是不可区分"missing"与"unreadable"的场景。fail-closed 会让正常文件创建也被拒绝，模型陷入死循环；fail-open 让工具自身去报真实的错误（如 `str_replace` 找不到子串时自报）。

### 5. 阻塞结果主动 stamp `deerflow_tool_meta`
被阻塞的写不会进入 `ToolErrorHandlingMiddleware`，元数据不会被自动盖。主动 `normalize_tool_result` 保证 `ToolProgressMiddleware` 仍能正确分类（标记为 `recoverable_by_model=True`），不会触发"元数据缺失"的 warning。

## 与其他中间件的协作

- **ToolProgressMiddleware**（位置 12）：处于本中间件内层。被门禁阻塞的写不进入其状态机计数，因此一次合理的"拒绝写"不会被误算成工具停滞。
- **ToolErrorHandlingMiddleware**（位置 13）：处于本中间件内层。正常工具异常仍走它盖戳 `deerflow_tool_meta`；本中间件通过 `normalize_tool_result` 主动盖戳，与它形成一致协议。
- **SummarizationMiddleware**（位置 18）：删除读结果 = 删除标记。设计上保证压缩后门禁不会假阳性通过。
- **sandbox/file_operation_lock.py**：另一把同模式但命名空间独立的锁，只覆盖 mutation 本身；本锁覆盖 authorization + mutation 整个阶段。
- **SandboxMiddleware**（位置 6）：产出 `state["sandbox"].sandbox_id`，作为本中间件 `_lock_scope` 的回退 scope（无 `thread_id` 时按 sandbox 隔离锁）。

---

# 2. ToolProgressMiddleware

## 概述

`ToolProgressMiddleware` 是一个基于状态机的"工具停滞守卫"（RFC #3177）。它在每次工具调用结果返回后，检查 `deerflow_tool_meta` 元数据与（对 success 结果的）Jaccard 相似度，按 `(thread_id, tool_name)` 维度跟踪连续"无新信息"调用次数；当达到阈值时进入 `WARNED` 状态注入提示，进一步恶化则进入 `BLOCKED` 状态硬阻塞该工具，避免模型继续浪费 API 调用。

## 为什么需要这个中间件

### 场景痛点

Agent 在调试一个 bug 时连续调用 6 次 `web_search` 搜索几乎相同的关键词变体，每次的搜索结果摘要大同小异，白白消耗 6 次 API 调用和数千 token 后才放弃。另一个常见场景是 agent 反复调用 `read_file` 读取同一个不会自动变化的日志文件，期望看到不同的内容——模型在一个循环中不断重试同一个工具，输出相同的结果而不自知。

### 为什么模型自身无法避免

模型缺乏对工具输出内容的"语义记忆"——它看不到两次 `web_search` 返回的摘要本质上是对同一组信息的重排，也无法判断 3 次 `read_file` 读取同一文件的结果是否真的带来了新信息。Jaccard 相似度计算对模型来说是黑盒之外的逻辑，模型无法靠自己的推理来检测这种"工具停滞"状况。长上下文场景下，第一次搜索的结果可能已经被压缩或遗忘，模型会在没有意识到重复的情况下发起新一轮搜索。

### 解决思路

基于状态机跟踪每个工具的连续"无进展"调用次数，利用 Jaccard 相似度检测重复的成功结果，先注入提示引导模型改变策略，必要时硬阻塞该工具以节省资源。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/tool_progress_middleware.py` |
| 实现的钩子 | `wrap_tool_call`、`awrap_tool_call`、`wrap_model_call`、`awrap_model_call`、`before_agent`、`abefore_agent` |
| 持久化 | Per-request 内存（`OrderedDict` LRU + `dict` pending 队列），不写入 state |
| 配置依赖 | `config.yaml -> tool_progress.enabled`（按需挂载）；参数由 `ToolProgressConfig` 驱动 |
| 装配位置 | 中间件链位置 12，`ToolErrorHandlingMiddleware` 外层 |
| 协作模式 | 结果质量守卫（per-tool BLOCK），与 `LoopDetectionMiddleware`（call-pattern 守卫，whole-turn stop）互补 |

## 核心逻辑

### 数据结构

```python
@dataclass(slots=True)
class ToolPhaseState:
    """Per (thread_id, tool_name) tracking state."""
    phase: Literal["active", "warned", "blocked"] = "active"
    consecutive_problems: int = 0
    block_reason: str | None = None
    # 不可变 tuple：避免 dataclasses.replace() 后旧/新状态意外共享可变列表
    recent_word_sets: tuple[frozenset[str], ...] = field(default_factory=tuple)
```

中间件实例字段：

```python
self._lock = threading.Lock()  # 关键 section 是短小的纯内存操作，不用 asyncio.Lock
self._phase_states: OrderedDict[str, dict[str, ToolPhaseState]] = OrderedDict()  # LRU
self._pending: dict[tuple[str, str], list[str]] = defaultdict(list)  # (thread_id, run_id) → hints
```

为什么用 `threading.Lock` 而不是 `asyncio.Lock`：临界区是短小的字典操作无 I/O，事件循环停滞风险可忽略；同时 subagent executor 线程池走 sync `wrap_tool_call` 路径，asyncio.Lock 无法保护。这与 `LoopDetectionMiddleware` 的模式一致。

### 工具调用包装：先查 BLOCKED 再执行

`wrap_tool_call` / `awrap_tool_call` 分流：

```python
tool_name = str(request.tool_call.get("name", ""))
if not tool_name or tool_name in self._exempt_tools:
    return handler(request)
runtime = getattr(request, "runtime", None)
if runtime is None:
    return handler(request)
block_reason = self._get_block_reason(runtime, tool_name)
if block_reason:
    logger.info("tool_progress: %s/%s call intercepted (blocked): %s",
                self._thread_id(runtime), tool_name, block_reason)
    return self._make_blocked_message(request, tool_name, block_reason)
return self._update_state_from_result(handler(request), tool_name, runtime)
```

被 BLOCKED 的工具直接返回一个 `[TOOL_BLOCKED] ...` 错误 `ToolMessage`，且元数据标 `recoverable_by_model=True`、`recommended_next_action="summarize"`：

```python
def _make_blocked_message(self, request, tool_name, block_reason) -> ToolMessage:
    return ToolMessage(
        content=f"[TOOL_BLOCKED] {block_reason}",
        tool_call_id=str(request.tool_call.get("id", "")),
        name=tool_name,
        status="error",
        additional_kwargs={
            TOOL_META_KEY: {
                "status": "error",
                "error_type": "blocked_by_progress_guard",
                "recoverable_by_model": True,
                "recommended_next_action": "summarize",
                "source": "progress_middleware",
            }
        },
    )
```

### 状态机推进 `_update_state_from_result`

```python
def _update_state_from_result(self, result, tool_name, runtime):
    if not isinstance(result, ToolMessage):
        return result
    meta = _parse_tool_meta((result.additional_kwargs or {}).get(TOOL_META_KEY))
    if meta is None:
        if tool_name not in self._exempt_tools:
            logger.warning(
                "tool_progress: deerflow_tool_meta missing for non-exempt tool %s — "
                "verify ToolProgressMiddleware is outer of ToolErrorHandlingMiddleware",
                tool_name)
        return result
    content = _message_content_str(result)
    thread_id = self._thread_id(runtime)
    with self._lock:
        state = self._get_state(thread_id, tool_name)
        new_state, hint = self._assess_and_transition(state, meta, content)
        self._set_state(thread_id, tool_name, new_state)
    # 日志记录 phase 转换...
    if hint and self._inject_assessment:
        self._queue_assessment(runtime, hint)
    return result
```

如果 `meta is None` 且工具不在豁免列表中，会发出 warning，提示装配顺序错误（本中间件必须在 `ToolErrorHandlingMiddleware` 外层）。

### 状态机核心 `_assess_and_transition`

完整代码：

```python
def _assess_and_transition(self, state, meta, content) -> tuple[ToolPhaseState, str | None]:
    # 防御：blocked 是终态，本函数理论上 unreachable（wrap_tool_call 已拦截）
    if state.phase == "blocked":
        return state, None

    # 先把本次记一次 problem，保证所有退出路径 consecutive_problems 一致
    new_count = state.consecutive_problems + 1

    # 不可恢复 + action=stop → 立即 BLOCKED
    if not meta.recoverable_by_model and meta.recommended_next_action == "stop":
        return replace(state, phase="blocked",
                       consecutive_problems=new_count,
                       block_reason=_block_reason(meta)), None

    # 只对 success 结果算 word_set（error/partial_success 一定是 problem，没必要算 Jaccard）
    ws = word_set(content) if meta.status == "success" else frozenset()
    is_problem = meta.status in ("error", "partial_success") or (
        meta.status == "success" and is_near_duplicate(
            ws, state.recent_word_sets, self._jaccard_threshold, self._min_words))

    if not is_problem:
        # 好结果：重置计数，回 ACTIVE
        new_recent = (*state.recent_word_sets, ws)[-3:]
        return replace(state, consecutive_problems=0, phase="active",
                       recent_word_sets=new_recent), None

    hint: str | None = None

    if new_count >= self._stagnation_threshold + self._warn_escalation:
        if meta.recoverable_by_model:
            # 模型可通过改变策略修复 → 保持 WARNED，重复注入提示
            hint = _format_hint(meta)
            new_state = replace(state, consecutive_problems=new_count, phase="warned")
        else:
            # 模型无法通过重试修复 → BLOCKED
            reason = _block_reason(meta)
            new_state = replace(state, consecutive_problems=new_count,
                                phase="blocked", block_reason=reason)
    elif new_count >= self._stagnation_threshold:
        hint = _format_hint(meta)
        new_state = replace(state, consecutive_problems=new_count, phase="warned")
    else:
        new_state = replace(state, consecutive_problems=new_count)

    return new_state, hint
```

关键分支说明：

1. **blocked 终态防御**：理论上 `wrap_tool_call` 在执行 handler 前已拦截 blocked 工具，本分支是为并发竞争定义良性行为——避免一次可恢复错误结果把 phase 静默降级回 warned。
2. **先递增 `consecutive_problems`**：无论后续走哪条分支，problem 计数都已 +1，保证所有退出路径状态一致。
3. **立即 BLOCKED 路径**：`recoverable_by_model=False` 且 `recommended_next_action="stop"`（auth/config/internal）首次发生就 BLOCK，因为模型无法通过重试修复。
4. **Jaccard 只对 success 算**：error / partial_success 一定是 problem，没必要浪费 O(n) 正则。
5. **好结果重置**：`consecutive_problems=0`，phase 回 `active`，并把本次 word_set 加入 `recent_word_sets`（只保留最近 3 个）。
6. **达到 `stagnation_threshold + warn_escalation`**：
   - `recoverable_by_model=True`（no_results / not_found / permission / Jaccard 重复 success）→ WARNED 是终态，重复注入 hint，不 BLOCK（避免阻止合理的换参数重试）。
   - `recoverable_by_model=False` 且 `action≠stop`（rate_limited / transient）→ BLOCKED。
7. **达到 `stagnation_threshold` 但未到升级阈值** → WARNED，注入 hint。

### Jaccard 相似度

```python
_MAX_CONTENT_FOR_WORDSET = 8192

def word_set(content: str) -> frozenset[str]:
    """Extract lowercase words of length >= 3 for Jaccard similarity.
    Content is capped at _MAX_CONTENT_FOR_WORDSET chars to bound memory and CPU cost on
    large tool results (e.g. web pages). Tail content beyond the cap is omitted from the set.
    """
    return frozenset(re.findall(r"\b\w{3,}\b", content[:_MAX_CONTENT_FOR_WORDSET].lower()))

def is_near_duplicate(current, recent, threshold, min_words) -> bool:
    """Return True if current is similar to any of the last 3 recent word sets."""
    if len(current) < min_words:
        return False
    for prev in recent[-3:]:
        if len(prev) < min_words:
            continue
        union = len(current | prev)
        if union == 0:
            continue
        if len(current & prev) / union >= threshold:
            return True
    return False
```

设计要点：

- 取长度 ≥ 3 的单词，避免常见停用词"a/the/is"污染相似度。
- 内容截断到 8192 字符后再算词集，O(n) 正则成本有上限；尾部被丢弃是可以接受的，因为 duplicate-detection 是启发式而非保证。
- `min_words`（默认 10）下限：词集太小（短结果）不参与相似度比较，避免误报。
- 与最近 3 个 word_set 中任一个 Jaccard ≥ `jaccard_threshold`（默认 0.8）即视为 near-duplicate。

### 状态机图

```
                       ┌─────────────────────────────────────────────────────┐
                       │                                                     │
                       │   首次出现  recoverable_by_model=False, action=stop   │
                       │   (auth / config / internal)                         │
                       │                                                     │
                       ▼                                                     │
                  ┌─────────┐                                                │
                  │ BLOCKED │ ◄──────────────────────────────────────────────┘
                  └─────────┘  (终态，仅 by reset_run_states 解除)
                       ▲
                       │
                       │ recoverable_by_model=False, action≠stop
                       │ (rate_limited / transient)
                       │ 连续 problem 数 ≥ stagnation_threshold + warn_escalation
                       │
  ┌──────────┐    │      ┌─────────┐
  │          │    │      │         │
  │ ACTIVE   │────┼─────►│ WARNED  │
  │ (默认)   │    │      │ (终态或 │
  │          │    │      │  过渡)  │
  └──────────┘    │      └─────────┘
        ▲         │           │
        │         │           │ recoverable_by_model=True
        │         │           │ (no_results / not_found / permission / Jaccard-duplicate success)
        │         │           │ 连续 problem 数 ≥ stagnation_threshold + warn_escalation
        │         │           │ → 重复注入 hint，保持 WARNED
        │         │           ▼
        │         │      ┌─────────┐
        │         │      │ WARNED  │
        │         │      │ (终态)  │
        │         │      └─────────┘
        │         │           │
        │         │           │ recoverable_by_model=False, action≠stop
        │         │           │ 连续 problem 数 ≥ stagnation_threshold + warn_escalation
        │         │           ▼
        │         └──────► BLOCKED
        │
        │
        │ 好结果（status=success 且非 Jaccard 重复；或其它非 problem 状态）
        │ consecutive_problems=0, recent_word_sets 追加 ws 保留最近 3 个
        │
        └─────────── (回 ACTIVE，重置计数)
```

转换矩阵：

| 当前 phase | 触发条件 | 新 phase | hint |
|------------|----------|----------|------|
| ACTIVE/WARNED | 首次 `recoverable=False, action=stop` | BLOCKED | 无 |
| ACTIVE/WARNED | `recoverable=False, action≠stop` 且 `new_count ≥ stagnation+warn_escalation` | BLOCKED | 无 |
| ACTIVE | `new_count ≥ stagnation` 且未达升级阈值 | WARNED | 有 |
| WARNED | `recoverable=True` 且 `new_count ≥ stagnation+warn_escalation` | WARNED（终态） | 有（每次 problem 重复注入） |
| ACTIVE/WARNED | 好结果（非 problem） | ACTIVE | 无 |
| BLOCKED | 任何 | BLOCKED（防御性 no-op） | 无 |

### 提示注入：`wrap_model_call`

工具执行后产生的 hint 不直接注入到当前消息流，而是排入 `(thread_id, run_id)` 维度的 pending 队列，在下次模型调用前通过 `wrap_model_call` 抽出并追加为 `HumanMessage`：

```python
_MAX_PENDING_PER_RUN = 3

def _queue_assessment(self, runtime, text: str) -> None:
    key = self._pending_key(runtime)
    thread_id = key[0]
    with self._lock:
        # 防止为已被 LRU 驱逐的 thread 创建幽灵 pending 项
        if thread_id not in self._phase_states:
            return
        queue = self._pending[key]
        if len(queue) < _MAX_PENDING_PER_RUN:
            queue.append(text)

def _drain_pending(self, runtime) -> list[str]:
    key = self._pending_key(runtime)
    with self._lock:
        return self._pending.pop(key, [])

def _augment_request(self, request: ModelRequest) -> ModelRequest:
    hints = self._drain_pending(request.runtime)
    if not hints:
        return request
    deduped = list(dict.fromkeys(hints))
    new_messages = [
        *request.messages,
        HumanMessage(content="\n\n".join(deduped), name="progress_hint"),
    ]
    return request.override(messages=new_messages)
```

`dict.fromkeys` 做保序去重，避免相同 hint 在一次模型调用前被重复追加。`HumanMessage.name="progress_hint"` 让模型和其它中间件能识别该提示的来源。

### Run 边界与重置策略

`before_agent` 在每次新 agent run 开始时做两件事：

```python
@override
def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
    self._clear_stale_pending(runtime)
    self._reset_run_states(runtime)
    return None
```

`_clear_stale_pending`：清掉非当前 `run_id` 的 pending hints，避免上一 run 残留提示污染本 run。

`_reset_run_states`：无条件把该 thread 下所有工具状态重置回 `active`，清零 `consecutive_problems`、`block_reason`、`recent_word_sets`：

```python
for tool_name, tool_state in list(thread_tools.items()):
    thread_tools[tool_name] = replace(
        tool_state,
        phase="active",
        consecutive_problems=0,
        block_reason=None,
        recent_word_sets=(),
    )
```

设计理由（docstring 原文）：

- `rate_limited` / `transient` 等错误是**时间相关**的：用户两次回合之间根因可能已消失，把陈旧计数带进新 run 会在本可成功的调用上误判 BLOCKED。
- `LoopDetectionMiddleware` 采取相反策略（跨 run 保留 `_history`），因为**调用模式循环是时间无关的**：模型反复发出同样 tool_calls 与 run 起始时刻无关。两个中间件因守卫的故障模式不同，跨 run 策略也故意不同。

### LRU 与 pending 队列的耦合

`_get_state` 在插入新 thread 时会驱逐最老的 thread，同时清掉被驱逐 thread 的 pending 项：

```python
def _get_state(self, thread_id: str, tool_name: str) -> ToolPhaseState:
    if thread_id not in self._phase_states:
        self._phase_states[thread_id] = {}
        while len(self._phase_states) > self._max_tracked_threads:
            evicted_thread, _ = self._phase_states.popitem(last=False)
            # 同步清除被驱逐 thread 的 pending，避免无限增长
            for key in [k for k in self._pending if k[0] == evicted_thread]:
                del self._pending[key]
    self._phase_states.move_to_end(thread_id)
    return self._phase_states[thread_id].get(tool_name, ToolPhaseState())
```

`_get_block_reason` 做只读检查时**故意不调用 `move_to_end`**：若读路径也 bump recency，blocked thread 会永远在 LRU 中保持热度，挤占健康 active thread 的位置。Recency 只在写路径更新。

### 豁免工具

```python
self._exempt_tools: set[str] = exempt_tools if exempt_tools is not None else {
    "ask_clarification", "write_todos", "present_files", "task"
}
```

豁免工具的语义是"结构性无意义去跟踪"：`ask_clarification` 是人机交互、`write_todos` 是计划更新、`present_files` 是文件呈现、`task` 是子代理委派。这些工具的"无新信息"不代表工具停滞，因此跳过状态机。

### from_config 工厂

```python
@classmethod
def from_config(cls, config: ToolProgressConfig) -> ToolProgressMiddleware:
    return cls(
        stagnation_threshold=config.stagnation_threshold,
        warn_escalation_count=config.warn_escalation_count,
        inject_assessment=config.inject_assessment,
        jaccard_threshold=config.jaccard_similarity_threshold,
        min_words=config.min_word_count_for_similarity,
        exempt_tools=set(config.exempt_tools),
        max_tracked_threads=config.max_tracked_threads,
    )
```

配置字段在 `deerflow.config.tool_progress_config.ToolProgressConfig` 中定义，对应 `config.yaml -> tool_progress.*`。

## 关键设计决策

### 1. 三类错误的差异化处理
- **`recoverable_by_model=True`**（no_results / not_found / permission / Jaccard 重复 success）：WARNED 是终态。模型收到 hint 后应改变策略，硬 BLOCK 会阻止合理的"换参数重试"。
- **`recoverable_by_model=False, action≠stop`**（rate_limited / transient）：WARNED → BLOCKED。模型无法通过重试修复，硬 BLOCK 节省 API 调用。
- **`recoverable_by_model=False, action=stop`**（auth / config / internal）：首次即 BLOCKED。任何重试都无意义。

### 2. 阻塞粒度 = 单工具
只 BLOCK 触发停滞的具体工具名，其它工具仍正常工作。这与 `LoopDetectionMiddleware` 的"whole-turn stop"形成粒度互补。

### 3. 提示延后到模型调用前注入
工具执行后立即注入 hint 会破坏当前 turn 的消息流；排入 pending 队列，在下次 `wrap_model_call` 前一次性去重追加，既保证模型一定看见，又不污染当前 turn。

### 4. 跨 run 无条件重置
时间相关错误（rate_limited / transient）根因可能在新 run 已消失，带陈旧计数会误判。即便 BLOCKED 工具也会被重置——如果根因持续，它会在新 run 中立即重新 BLOCK，没有信息损失。

### 5. 不可变 `recent_word_sets`
用 `tuple[frozenset[str], ...]` 而非 `list`，保证 `dataclasses.replace()` 不共享可变结构，避免旧/新状态对象通过 `.append()` 静默交叉污染。

### 6. Jaccard 仅对 success 算
error / partial_success 一定是 problem，没必要浪费 O(n) 正则。`word_set(content)[:8192]` 上限保证大结果（如网页）的成本有界。

### 7. 元数据缺失时发出 warning
若非豁免工具的结果缺少 `deerflow_tool_meta`，说明装配顺序错误（本中间件必须在 `ToolErrorHandlingMiddleware` 内层之外）。warning 不抛异常，避免单工具故障中断整个 run。

## 与其他中间件的协作

- **ToolErrorHandlingMiddleware**（位置 13，内层）：本中间件处于其外层，确保工具结果已盖 `deerflow_tool_meta`。若装配反了，本中间件读不到 meta 会发 warning。
- **ReadBeforeWriteMiddleware**（位置 11，外层）：被门禁阻塞的写已带 `deerflow_tool_meta`（通过 `normalize_tool_result` 主动盖戳），本中间件能正确分类为 `recoverable_by_model=True`，不会误触 warn 计数。
- **LoopDetectionMiddleware**（位置 28）：
  - 本中间件是**结果质量守卫**，工具执行后触发，BLOCK 单工具。
  - LoopDetection 是**调用模式守卫**，模型响应后触发，whole-turn 停止。
  - 两者可在同一次模型调用中同时注入 HumanMessage hint 而互不冲突。
  - 若 LoopDetection 硬停（剥离 tool_calls），则没有 `wrap_tool_call` 被触发，本中间件不会触发——无双停。
  - 两者跨 run 策略故意相反（本中间件重置，LoopDetection 保留）。
- **InputSanitizationMiddleware**（位置 1，最外层）：本中间件注入的 `HumanMessage(name="progress_hint")` 在其内层，理论上应被其感知；但 progress_hint 是受信任的服务器侧消息，不被当作用户输入。
- **LLMErrorHandlingMiddleware**（位置 8）：把模型异常转为可恢复 assistant-facing 错误，避免本中间件因模型异常而误触状态机。

## 附录：配置参数对照

| 构造参数 | 默认值 | 对应 `ToolProgressConfig` 字段 |
|----------|--------|-------------------------------|
| `stagnation_threshold` | 3 | `stagnation_threshold` |
| `warn_escalation_count` | 2 | `warn_escalation_count` |
| `inject_assessment` | True | `inject_assessment` |
| `jaccard_threshold` | 0.8 | `jaccard_similarity_threshold` |
| `min_words` | 10 | `min_word_count_for_similarity` |
| `exempt_tools` | `{ask_clarification, write_todos, present_files, task}` | `exempt_tools` |
| `max_tracked_threads` | 100 | `max_tracked_threads` |

模块级常量：

| 常量 | 值 | 含义 |
|------|----|----|
| `_MAX_PENDING_PER_RUN` | 3 | 单次 run 累积 hint 上限，防爆炸 |
| `_MAX_CONTENT_FOR_WORDSET` | 8192 | Jaccard 计算的内容截断长度，O(n) 正则成本有界 |
