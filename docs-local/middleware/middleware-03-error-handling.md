# 中间件深度解析（三）：错误处理与安全守卫中间件

本文件涵盖 DeerFlow harness 层四个关键的"容错与安全"中间件：`LLMErrorHandlingMiddleware`、`GuardrailMiddleware`、`SandboxAuditMiddleware`、`ToolErrorHandlingMiddleware`。它们共同构成 lead/subagent 运行链中的"失败恢复 + 工具调用守门人"子系统，是 DeerFlow 能够在上游 LLM 提供商抖动、沙箱命令越权、工具抛异常等场景下仍优雅结束一个 run 的关键。

阅读本篇前建议先阅读 `backend/AGENTS.md` 的 "Middleware Chain" 章节，了解这些中间件在整条链中的位置。下面每一节都按"概述 → 元信息 → 核心逻辑 → 关键设计决策 → 与其他中间件的协作"五段式展开。

---

# 1. LLMErrorHandlingMiddleware

## 概述

捕获 LLM 提供商调用过程中抛出的瞬时错误（限流、超时、连接中断、服务繁忙等），按指数退避策略重试；多次重试仍失败时熔断器跳开保护系统；最终把不可恢复的错误包装成一条带 `deerflow_error_fallback` 标记的 `AIMessage` 返回，让 LangGraph 图能干净终止而不是把异常向上抛炸整条 run。

## 为什么需要这个中间件

### 场景痛点

用户正在处理一个长对话，子代理已经跑了十几轮工具调用，累积了大量中间结果。此时 LLM 提供商因流量高峰返回 429 限流错误，或者网络闪断导致 `APIConnectionError`。如果没有本中间件，这个瞬态异常会直接向上传播，炸掉整条 run，之前十几轮的工作全部丢失，用户只能从头开始。

更糟糕的情况：模型在流式输出中途因 `StreamChunkTimeoutError` 卡死（常见于工具调用 payload 过大导致上游生成间隔超长），没有本中间件的话，这条消息永远收不到结尾，run 卡在"进行中"状态无法结束。

### 为什么模型自身无法避免

LLM 调用是基础设施层面的操作，发生在模型本身的"思维"之外。模型看不到 HTTP 连接的状态、API 的限流计数器、上游的负载情况，也完全没有能力发起重试。这些是部署环境的问题，不是模型行为的问题。即使模型"知道"可能被限流，它也没有任何手段来自动重试一次 API 调用。

### 解决思路

在 `wrap_model_call` 钩子中捕获所有 LLM 调用异常，用指数退避+熔断器自动恢复瞬态故障，不可恢复错误则包装成带标记的兜底消息，让 run 能正常结束。 

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py` |
| 实现的钩子 | `wrap_model_call` / `awrap_model_call`（同步与异步对称实现） |
| 持久化 | 内存（熔断器状态本进程内驻留，跨请求复用） |
| 配置依赖 | `AppConfig.circuit_breaker.failure_threshold`、`AppConfig.circuit_breaker.recovery_timeout_sec`；类属性 `retry_max_attempts=3`、`retry_base_delay_ms=1000`、`retry_cap_delay_ms=8000` |
| 状态变量 | `_circuit_failure_count`、`_circuit_open_until`、`_circuit_state`（`closed` / `open` / `half_open`）、`_circuit_probe_in_flight`，全部由 `threading.Lock` 保护 |
| 上下游位置 | 在 `_build_runtime_middlewares` 的 `tail` 段，位于 `DanglingToolCallMiddleware` 之后、`GuardrailMiddleware`/`SandboxAuditMiddleware`/`ToolErrorHandlingMiddleware` 之前；是 `wrap_model_call` 链路上负责"LLM 失败兜底"的最后一道 |

## 核心逻辑

### 1.1 错误分类（`_classify_error`）

入口先调用一组 helper（`_extract_error_detail` / `_extract_error_code` / `_extract_status_code`）从异常对象的多重属性中尽量榨取可读信息：

- `_extract_error_detail` 优先取 `str(exc).strip()`，空则回退 `exc.message`，再空则用类名；
- `_extract_error_code` 依次试 `exc.code`、`exc.error_code`，再深入 `exc.body["error"]["code"|"type"]`；
- `_extract_status_code` 依次试 `exc.status_code`、`exc.status`、`exc.response.status_code`。

随后按下表顺序匹配（**先 quota/auth，再 transient，最后 generic**），返回 `(retriable: bool, reason: str)`：

| 判定 | reason | retriable |
|------|--------|-----------|
| 文本/error code 命中 `_QUOTA_PATTERNS`（`insufficient_quota` / `billing` / `余额不足` …） | `quota` | False |
| 命中 `_AUTH_PATTERNS`（`unauthorized` / `invalid api key` / `未授权` …） | `auth` | False |
| 异常类名属于 `APITimeoutError`、`APIConnectionError`、`InternalServerError`、`ReadError`、`RemoteProtocolError`、`StreamChunkTimeoutError` | `transient` | True |
| 异常是 `IndexError`（见下方"空 generations 兜底"） | `transient` | True |
| HTTP 状态码 ∈ `_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}` | `transient` | True |
| 文本命中 `_BUSY_PATTERNS`（`server busy` / `服务繁忙` / `rate limit` …） | `busy` | True |
| 否则 | `generic` | False |

**空 generations 兜底**（源码注释重点强调）：上游某些提供商（如 Volces "coding" / `ark.cn-beijing.volces.com`）会返回 `200 OK` 但 `generations` 列表为空，`langchain_core.language_models.chat_models.ainvoke` 在访问 `llm_result.generations[0][0].message` 时抛 `IndexError: list index out of range`。这并非客户端 bug 而是上游瞬态故障，因此被显式归类为 `transient` 走重试路径。

### 1.2 重试预算覆盖（`_max_attempts_for`）

某些瞬态错误理论上可重试但代价高昂，通过 `_RETRY_BUDGET_OVERRIDES` 字典做 per-exception-class 覆盖：

```python
_RETRY_BUDGET_OVERRIDES: dict[str, int] = {
    "StreamChunkTimeoutError": 2,
}
```

注释解释：`StreamChunkTimeoutError` 是 `langchain-openai` 的流式块间隔看门狗，触发时上游已经停摆 `stream_chunk_timeout`（典型 120–240s），走满 3 次重试会堆积 6–12 分钟死寂。所以**只保留 1 次重试**（值 2 = 1 次首发 + 1 次重试），能捕获真正的瞬时 TCP 抖动，然后立刻失败。

键名是异常**类名字符串**而非类本身，注释明说是为了不引入对 `langchain-openai` 等可选依赖的 import-time 耦合。最终生效的 max 取 `min(override, self.retry_max_attempts)`，所以全局调小 retry_max_attempts 时覆盖值也会被压低。

### 1.3 退避时延（`_build_retry_delay_ms`）

```python
def _build_retry_delay_ms(self, attempt: int, exc: BaseException) -> int:
    retry_after = _extract_retry_after_ms(exc)
    if retry_after is not None:
        return retry_after
    backoff = self.retry_base_delay_ms * (2 ** max(0, attempt - 1))
    return min(backoff, self.retry_cap_delay_ms)
```

优先尊重服务端 `Retry-After` 头。`_extract_retry_after_ms` 按顺序尝试 4 种 header 写法（`retry-after-ms` / `Retry-After-Ms` / `retry-after` / `Retry-After`），含 `ms` 的按毫秒读，否则按秒换算；秒格式还可能是 HTTP date，用 `email.utils.parsedate_to_datetime` 兜底解析。

无服务端提示时用指数退避：`1000 * 2^(attempt-1)`，封顶 8000ms。`attempt` 从 1 起算，所以 1→1000ms、2→2000ms、3→4000ms（封顶前）。

### 1.4 熔断器（Circuit Breaker）

经典三态机，跨请求复用：

```
        失败累计达 threshold            recovery_timeout 到期
   closed ─────────────────────► open ──────────────────► half_open
       ▲                            │                        │
       │ 成功                        │ 探针失败               │ 探针成功
       └────────────────────────────┴──► open            └─► closed
```

- `_check_circuit()`：`open` 且未到期 → 返回 True（fast-fail）；`open` 到期 → 转 `half_open` 并放行一次探针；`half_open` 且已有探针在飞 → fast-fail；否则放行。
- `_record_success()`：任何非 `closed` 或失败计数非零时记 info 日志，全部重置回 `closed`。
- `_record_failure()`：`half_open` 状态下失败直接回 `open` 并重置探针；`closed` 状态下递增计数，到达 `failure_threshold` 时跳 `open`。
- `_release_half_open_probe()`：当探针被"既非成功也非失败"地消费掉时（如 `GraphBubbleUp` 控制流信号或不可重试错误）调用，避免 half_open 永远 fast-fail。

熔断打开时直接返回兜底 `AIMessage`：

```python
if self._check_circuit():
    return self._build_error_fallback_message(
        self._build_circuit_breaker_message(),
        error_type="CircuitBreakerOpen",
        reason="circuit_open",
        detail="LLM circuit breaker is open",
    )
```

### 1.5 主循环（`wrap_model_call` / `awrap_model_call`）

两个钩子实现对称，唯一差异是 `time.sleep` vs `await asyncio.sleep`。以异步版为例：

```python
attempt = 1
while True:
    try:
        response = await handler(request)
        self._record_success()
        return response
    except GraphBubbleUp:
        # 保留 LangGraph 控制流信号（interrupt/pause/resume）
        self._release_half_open_probe()
        raise
    except Exception as exc:
        retriable, reason = self._classify_error(exc)
        max_attempts = self._max_attempts_for(exc)
        if retriable and attempt < max_attempts:
            wait_ms = self._build_retry_delay_ms(attempt, exc)
            logger.warning("Transient LLM error on attempt %d/%d; retrying in %dms: %s", ...)
            self._emit_retry_event(attempt, wait_ms, reason)
            await asyncio.sleep(wait_ms / 1000)
            attempt += 1
            continue
        logger.warning("LLM call failed after %d attempt(s): %s", ...)
        if retriable:
            self._record_failure()
        else:
            self._release_half_open_probe()
        return self._build_user_fallback_message(exc, reason)
```

要点：

1. `GraphBubbleUp` 永远优先透传 —— LangGraph 的 `interrupt`/`resume`/`pause` 控制流信号不能被错误处理吃掉，否则会破坏 HITL 流程。释放探针再 raise。
2. 重试前通过 `_emit_retry_event` 用 `langgraph.config.get_stream_writer()` 向前端流式发一条 `{"type": "llm_retry", "attempt": n, "max_attempts": M, "wait_ms": w, "reason": r, "message": ...}` 自定义事件，UI 上能看到"正在重试"提示。事件发送本身用 `try/except` 包裹并降级为 debug 日志，**不能**反向影响重试主流程。
3. 不可重试错误（如 quota/auth）**不**调用 `_record_failure`（否则会污染熔断器，把"凭证错了"误判成"提供商挂了"），只释放探针。
4. 可重试但用尽预算的错误才推进熔断器计数。

### 1.6 用户兜底消息（`_build_user_fallback_message`）

按 `reason` 分支生成不同的人类可读文案：

- `quota` → "out of quota / billing unavailable / usage restricted"
- `auth` → "authentication or access is invalid"
- `busy` / `transient` 且异常类属于 `_STREAM_DROP_EXCEPTIONS`（目前只含 `StreamChunkTimeoutError`）→ **特殊的流中断提示**，建议用户"拆分请求、缩短输出再重试"而非干等。源码注释指出这类失败的典型根因是单次工具调用 payload 太大，模型在上游花太久序列化 JSON 参数，导致流式块间隔超时。
- 普通 `busy`/`transient` → "temporarily unavailable after multiple retries, please wait and continue"。
- 其他 → 原始 detail 文本。

所有兜底都包装成 `AIMessage` 并在 `additional_kwargs` 里写入：

```python
{
    "deerflow_error_fallback": True,
    "error_type": type(exc).__name__,
    "error_reason": reason,
    "error_detail": detail,
}
```

`deerflow_error_fallback=True` 这个标记是下游 `SubagentExecutor` 判定"子代理是否真失败"的**唯一权威信号**：只有带这个标记的兜底 AIMessage 才会被映射成 `SubagentStatus.FAILED` 并触发 `task_failed` 事件；单纯看起来像错误的自然语言文本不会被误判为失败协议（详见 `backend/AGENTS.md` Subagent System 的 "Handled LLM failures" 段）。

## 关键设计决策

1. **类名字符串而非类引用**：`_RETRY_BUDGET_OVERRIDES` 和 `_STREAM_DROP_EXCEPTIONS` 用类名作键，避免在 harness 层硬依赖 `langchain-openai`。这让 harness 可以在没有装 `langchain-openai` 的环境里被导入（如纯本地模型或 vLLM 部署）。
2. **熔断器跨请求驻留但本进程内**：用 `threading.Lock` 而非 `asyncio.Lock`，因为同步和异步两条钩子共用同一份熔断状态。代价是同步路径在 `time.sleep` 期间持锁外的逻辑依然能被异步路径推进，但锁本身只保护计数读写，临界区极短。
3. **不可重试错误不推进熔断**：防止"用户凭证错"这种永久故障把熔断器烧穿，导致恢复后仍被 fast-fail。
4. **`IndexError` 当瞬态**：这是个看似很脏的 hack 但有充分注释 —— 上游空 `generations` 不是客户端 bug，重试是比崩整条 run 更好的选择。
5. **`StreamChunkTimeoutError` 特殊化**：既压低重试预算（避免 6-12 分钟死寂），又用差异化用户文案（建议拆分而非重试），是对真实故障模式的精细化运营。
6. **兜底永远是 `AIMessage` 而非 raise**：让 LangGraph 图能正常走到 END 节点，checkpoint 写入正常终态，run 状态机能干净收尾。这是 DeerFlow "失败也是正常 run"哲学的核心。

## 与其他中间件的协作

- **`SubagentExecutor`**（`packages/harness/deerflow/subagents/executor.py`）：subagent 运行链里也挂了本中间件（通过 `build_subagent_runtime_middlewares` 共享 `_build_runtime_middlewares` 的 tail）。Executor 在 terminalization 时检查 AIMessage 的 `additional_kwargs.deerflow_error_fallback` 标记，把它映射成 `SubagentStatus.FAILED` + `subagent_error` 事件。
- **`InputSanitizationMiddleware`**：在 `wrap_model_call` 链上是**外层**，所以本中间件看到的是已消毒的 messages。
- **`TerminalResponseMiddleware`**（lead 链的 tail 之后）：处理"空 terminal AIMessage"恢复；本中间件的兜底 AIMessage 不属于"空响应"，不会被它二次处理。
- **`DanglingToolCallMiddleware`**：位于本中间件之前（tail 段更外层），负责补齐丢失的 tool_call response，让本中间件看到的 model call 请求总是结构合法的。

---

# 2. GuardrailMiddleware

## 概述

在每次工具调用真正执行之前，把它交给可插拔的 `GuardrailProvider` 做授权决策；被拒绝的调用不会触达 handler，而是返回一条 `status="error"` 的 `ToolMessage` 让 agent 自行改路；provider 自身抛异常时按 `fail_closed` 策略决定是阻断还是放行。

## 为什么需要这个中间件

### 场景痛点

假设攻击者通过 prompt 注入让 agent 调用 `write_file` 覆写 `/etc/passwd`，或者诱导 agent 执行 `bash` 删除生产数据库备份。如果没有 GuardrailMiddleware，这些工具调用会畅通无阻地抵达各自的 handler，造成不可逆的数据损坏。

即使不是恶意场景，一个经过了多轮上下文积累的 agent 也可能"忘记"了最初的授权边界，调用某个只应在特定条件下使用的敏感工具（如 `update_agent` 或 `setup_agent`）。没有任何权限检查的纯 LLM 调用链无法阻止这种越权行为。

### 为什么模型自身无法避免

LLM 本质上是一个概率模型，其行为受到 prompt 质量、上下文长度、训练数据分布等多重因素影响。安全策略不应该建立在"模型会乖乖听话"的假设上——prompt 注入、jailbreak、上下文污染都是已知攻击面。安全边界必须由可信的、不可绕过的代码层来执行，而不是依赖模型的自律。

### 解决思路

在 `wrap_tool_call` 钩子中截获每次工具调用，交给可插拔的授权 provider 做决策，拒绝的调用直接返回错误消息，绝不执行。 

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/guardrails/middleware.py` |
| 实现的钩子 | `wrap_tool_call` / `awrap_tool_call`（同步与异步对称） |
| 持久化 | Per-request + 可选 RunJournal 审计落盘（best-effort，不阻塞执行） |
| 配置依赖 | `AppConfig.guardrails.enabled` / `.provider` / `.fail_closed` / `.passport`（在 `_build_runtime_middlewares` 里条件挂载） |
| 协作模块 | `deerflow.guardrails.provider`（Protocol + 数据类）、`deerflow.guardrails.builtin.AllowlistProvider`、`deerflow.authz.principal.normalize_authz_attributes` |
| 上下游位置 | 在 `tail` 段位于 `SandboxAuditMiddleware` 之前、`LLMErrorHandlingMiddleware` 之后；仅当 `guardrails.enabled and guardrails.provider` 才挂载 |

## 核心逻辑

### 2.1 上下文与请求构造

```python
@staticmethod
def _resolve_context(request: ToolCallRequest) -> dict:
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None) if runtime is not None else None
    return context if isinstance(context, dict) else {}
```

从 `ToolCallRequest.runtime.context` 安全取出运行时上下文字典（可能为 None 或非 dict，统一回退到 `{}`）。这个 context 是 Gateway 在 run 启动时通过 `runtime.context` 注入的，承载 `thread_id` / `user_id` / `user_role` / `is_subagent` / `run_id` / `channel_user_id` / `is_internal` / `authz_attributes` 等身份与运行级元数据。

```python
def _build_request(self, request: ToolCallRequest, context: dict) -> GuardrailRequest:
    return GuardrailRequest(
        tool_name=str(request.tool_call.get("name", "")),
        tool_input=request.tool_call.get("args", {}),
        agent_id=self.passport,
        thread_id=context.get("thread_id"),
        is_subagent=bool(context.get("is_subagent")),
        timestamp=datetime.now(UTC).isoformat(),
        user_id=context.get("user_id"),
        user_role=context.get("user_role"),
        oauth_provider=context.get("oauth_provider"),
        oauth_id=context.get("oauth_id"),
        run_id=context.get("run_id"),
        tool_call_id=request.tool_call.get("id"),
        channel_user_id=context.get("channel_user_id"),
        is_internal=context.get("is_internal") is True,
        authz_attributes=normalize_authz_attributes(context.get("authz_attributes")),
    )
```

把分散在 context 里的字段汇聚成 `GuardrailRequest` 数据类（`deerflow.guardrails.provider.GuardrailRequest`）。注意：

- `agent_id` 用构造期传入的 `self.passport`，而非 `thread_id`，这是可插拔授权 RFC 里"agent 身份证书"概念的最小落地（Phase 1A 只透传，未做强绑定）。
- `is_internal` 用 `is True` 严格布尔判断，与 Gateway 的"server-owned `request.state.auth_source`"对齐，杜绝客户端伪造。
- `authz_attributes` 通过 `normalize_authz_attributes` 做 copy-on-read 规范化，保证 provider 拿到的是结构化属性而非自由 dict。

### 2.2 主决策流（同步版）

```python
context = self._resolve_context(request)
gr = self._build_request(request, context)
try:
    decision = self.provider.evaluate(gr)
except GraphBubbleUp:
    raise
except Exception:
    logger.exception("Guardrail provider error (sync)")
    if self.fail_closed:
        decision = GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-closed)")])
        self._record_guardrail_event(context, gr, decision, action="deny_tool_call", provider_error=True)
        return self._build_denied_message(request, decision)
    else:
        decision = GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-open)")])
        self._record_guardrail_event(context, gr, decision, action="allow_tool_call_after_provider_error", provider_error=True)
        return handler(request)
if not decision.allow:
    logger.warning("Guardrail denied: tool=%s policy=%s code=%s", ...)
    self._record_guardrail_event(context, gr, decision, action="deny_tool_call", provider_error=False)
    return self._build_denied_message(request, decision)
return handler(request)
```

三个分支：

1. **Provider 正常返回**：按 `decision.allow` 决定放行或拒绝。
2. **Provider 抛 `GraphBubbleUp`**：透传，保留 LangGraph 控制流信号。
3. **Provider 抛其他异常**：按 `fail_closed` 决定。`True`（默认）→ 拒绝；`False` → 放行并记 `allow_tool_call_after_provider_error` 审计事件。两种降级都构造 `oap.evaluator_error` reason 并走审计。

异步版用 `await self.provider.aevaluate(gr)` 和 `await handler(request)`，逻辑完全镜像。

### 2.3 拒绝消息构造

```python
def _build_denied_message(self, request: ToolCallRequest, decision: GuardrailDecision) -> ToolMessage:
    tool_name = str(request.tool_call.get("name", "unknown_tool"))
    tool_call_id = str(request.tool_call.get("id", "missing_id"))
    reason_text = decision.reasons[0].message if decision.reasons else "blocked by guardrail policy"
    reason_code = decision.reasons[0].code if decision.reasons else "oap.denied"
    return ToolMessage(
        content=f"Guardrail denied: tool '{tool_name}' was blocked ({reason_code}). Reason: {reason_text}. Choose an alternative approach.",
        tool_call_id=tool_call_id,
        name=tool_name,
        status="error",
    )
```

要点：

- 永远取 `reasons[0]`（如果有多个 reason，只展示第一个给模型看，避免消息过长）。
- 兜底 reason code 是 `oap.denied`，兜底 message 是 "blocked by guardrail policy"。
- `status="error"` 让下游 `normalize_tool_message` 会把它分类为 `error/tool_return` 并打 `deerflow_tool_meta`，ToolProgressMiddleware 等下游消费者能识别这是被拒工具结果。
- 消息末尾固定带 "Choose an alternative approach."，引导模型改路。

### 2.4 审计落盘（`_record_guardrail_event`）

```python
journal = context.get("__run_journal")
if journal is None:
    return
```

遵循 DeerFlow "optional Journal" 模式：审计是 best-effort，绝不能改变工具执行行为。`__run_journal` 是 run 级 RunJournal 实例，只在主 runtime 注入；embedded/subagent 执行可能没有，直接跳过。

落盘内容是结构化 changes：

```python
changes = {
    "tool_name": guardrail_request.tool_name,
    "tool_call_id": guardrail_request.tool_call_id,
    "agent_id": guardrail_request.agent_id,
    "is_subagent": guardrail_request.is_subagent,
    "user_role": guardrail_request.user_role,
    "allow": decision.allow,
    "policy_id": decision.policy_id,
    "reason_codes": reason_codes,
    "reason_messages": reason_messages,  # 每条截断到 _REASON_MESSAGE_LIMIT=500 字符
    "fail_closed": self.fail_closed,
    "provider_error": provider_error,
}
journal.record_middleware(tag="guardrail", name=type(self).__name__, hook="wrap_tool_call", action=action, changes=changes)
```

`reason_messages` 每条预截断到 500 字符，防止超长拒绝理由撑爆 run_events 行。`journal.record_middleware` 本身可能抛异常（存储故障），整个 try 块用 `except Exception: logger.debug(...)` 兜底，绝不向主流程传播。

### 2.5 内置 Provider（`AllowlistProvider`）

`packages/harness/deerflow/guardrails/builtin.py` 提供零依赖的最简 provider：

```python
class AllowlistProvider:
    name = "allowlist"

    def __init__(self, *, allowed_tools: list[str] | None = None, denied_tools: list[str] | None = None):
        self._allowed = set(allowed_tools) if allowed_tools is not None else None
        self._denied = set(denied_tools) if denied_tools else set()

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        if self._allowed is not None and request.tool_name not in self._allowed:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", ...)])
        if request.tool_name in self._denied:
            return GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.tool_not_allowed", ...)])
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.allowed")])

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        return self.evaluate(request)
```

注释强调一个细节：**显式区分"未配置 allowlist"（`None` → 全允许）与"显式空 allowlist"（`[] → 全拒绝**）。如果用 truthiness 测试 `if not allowed_tools`，`[]` 会被当 `None` 处理而 fail open，让本应"全拒"的部署变成"全允许"。

### 2.6 Provider Protocol

`packages/harness/deerflow/guardrails/provider.py` 定义了 `@runtime_checkable` 的 `GuardrailProvider` Protocol，要求实现 `name: str` 属性、`evaluate(request) -> GuardrailDecision` 和 `aevaluate(request) -> GuardrailDecision`。任何满足这个签名的类都能作为 provider，通过 `config.yaml -> guardrails.provider.use` 用类路径加载（与 models/tools/sandbox 共用 `deerflow.reflection.resolve_variable` 机制），不强求继承基类。

数据类三件套：

- `GuardrailRequest`：上下文 + 工具调用信息（含 `tool_name`、`tool_input`、`tool_call_id`、`agent_id`、`thread_id`、`is_subagent`、`user_id`、`user_role`、`oauth_provider`/`oauth_id`、`run_id`、`channel_user_id`、`is_internal`、`authz_attributes`）。
- `GuardrailReason`：`code` + `message`，对齐 OAP reason 对象。
- `GuardrailDecision`：`allow` + `reasons: list[GuardrailReason]` + `policy_id` + `metadata`。

## 关键设计决策

1. **`fail_closed` 默认 True**：授权系统默认应该"拒绝"而非"放行"，这是安全态势的默认值。需要显式 opt-in 才能转 fail-open。
2. **Protocol 而非基类**：让外部 OAP 实现（如 `aport-agent-guardrails`）无需 import deerflow 内部代码就能接入，降低耦合。
3. **审计是 best-effort**：`_record_guardrail_event` 的失败不能改变工具执行结果，否则审计基础设施故障会变成拒绝/放行决策的一部分，破坏授权语义。
4. **reason 取第一条**：模型看到的拒绝消息只有一条 reason，避免超长消息污染上下文。完整 reason list 仍落审计。
5. **`is_internal`/`authz_attributes`/`channel_user_id` 从 runtime.context 读**：这些是 Gateway 在 `build_run_config` 阶段写入 `runtime.context`（**不**进 `configurable`，因为后者会被 checkpoint 持久化），是授权信任链的源头。
6. **`oap.evaluator_error` reason code**：provider 异常时构造的 reason 不是 `oap.denied`（不是策略拒绝），让审计能区分"策略说 no"与"评估器自身故障"。

## 与其他中间件的协作

- **`LLMErrorHandlingMiddleware`**：本中间件在 `tail` 段位于其后。本中间件产生的 `ToolMessage(status="error")` 会流经 `SandboxAuditMiddleware`（如果工具名是 `bash`，audit 仍会跑）和 `ToolErrorHandlingMiddleware`（会通过 `normalize_tool_result` 给它打 `deerflow_tool_meta`）。
- **`SandboxAuditMiddleware`**：另一个 `wrap_tool_call` 守门人，但只对 `bash` 工具有效；两者互不感知，分别独立判定。
- **`ToolErrorHandlingMiddleware`**：在 `tail` 段最内层（outer→inner 顺序），本中间件拒绝后返回的 `ToolMessage` 会被它 normalize，下游 `ToolProgressMiddleware` 能正确识别 `error/auth`/`error/tool_return` 类别。
- **`build_run_config`（Gateway 层）**：runtime.context 的 `is_internal`/`authz_attributes`/`channel_user_id` 由 Gateway 在请求入口写入，是本中间件信任链的源头（参见 `backend/AGENTS.md` 的 "Authorization identity plumbing"）。
- **`SubagentExecutor`**：subagent 链也通过 `build_subagent_runtime_middlewares` 共享 `_build_runtime_middlewares`，所以子代理的工具调用同样受 Guardrail 守护；`is_subagent=True` 通过 context 透传给 provider。

---

# 3. SandboxAuditMiddleware

## 概述

专门针对 `bash` 工具调用的安全审计中间件：先用正则 + shlex 双重分析给命令分级（`block` / `warn` / `pass`），高风险命令直接拦截不执行并返回 error `ToolMessage`，中风险命令照常执行但在结果末尾追加警告文本，所有 bash 调用（无论等级）都写一条结构化 JSON 审计日志到 `gateway.log`。

## 为什么需要这个中间件

### 场景痛点

一个 agent 在调试过程中被 prompt 注入，模型输出了一条 `bash` 命令：`rm -rf /`。如果没有本中间件，这条命令会原封不动通过 sandbox 工具的 `execute_command` 执行，清除整个容器内的文件系统。即便有 sandbox 隔离，这也会导致正在运行的开发环境不可用，丢失所有线程的工作区文件。

更隐蔽的场景：模型生成了 `safe;rm -rf /home` 这样的复合命令（外表看起来安全，";"后面才是破坏操作），或者 `base64 -d <<<xxxxx | bash` 这样的远程代码执行链。纯粹依赖模型不犯错是不现实的。

### 为什么模型自身无法避免

模型在生成 bash 命令时，无法"感知"即将执行的命令在系统层面的实际影响。一个看似无害的 `chmod 777 sensitive_file` 或 `pip install malicious_package`，模型缺乏安全上下文来判断其破坏性。更关键的是，模型的训练数据中包含大量 `rm -rf /` 之类的系统管理命令示例，模型只是在"完成任务"，并不理解它在当前上下文中的危害。

### 解决思路

在 `wrap_tool_call` 钩子中专门拦截 `bash` 工具，用正则+shlex 双重分析对命令分级，高风险命令直接阻断、中风险命令追加警告、所有调用写审计日志。 

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` |
| 实现的钩子 | `wrap_tool_call` / `awrap_tool_call`（同步与异步对称） |
| 持久化 | 无状态持久化（纯 per-request 计算）；审计日志通过 stdlib `logging` 写到 `gateway.log` |
| 配置依赖 | 无显式配置（始终挂载，在 `_build_runtime_middlewares` tail 段无条件 append）；类属性 `_MAX_COMMAND_LENGTH=10_000`、`_AUDIT_COMMAND_LIMIT=200` |
| 协作模块 | 无外部模块依赖，纯 Python stdlib（`re`/`shlex`/`json`/`datetime`） |
| 上下游位置 | `tail` 段位于 `GuardrailMiddleware` 之后、`ReadBeforeWriteMiddleware`/`ToolProgressMiddleware`/`ToolErrorHandlingMiddleware` 之前 |
| 状态模式 | `state_schema = ThreadState`（显式声明，但实际只用 `request.runtime` 取 `thread_id`，未读写其他 state 字段） |

## 核心逻辑

### 3.1 高风险模式清单（`_HIGH_RISK_PATTERNS`）

15 条预编译正则，覆盖典型破坏性命令族：

- `rm -[^\s]*r[^\s]*\s+(/\*?|~/?\*?|/home\b|/root\b)\s*$`：递归删除根/家目录
- `dd\s+if=`：块设备覆写
- `mkfs`：文件系统格式化
- `cat\s+/etc/shadow`：读取 shadow 密码文件
- `>+\s*/etc/`：重定向覆写 /etc 下文件
- `\|\s*(ba)?sh\b`：管道把外部内容喂给 sh/bash（典型 `curl ... | bash`）
- `[`$]\(?\s*(curl|wget|bash|sh|python|ruby|perl|base64)`：命令替换执行远端脚本
- `base64\s+.*-d.*\|`：base64 解码后管道执行
- `>+\s*(/usr/bin/|/bin/|/sbin/)`：覆写系统二进制
- `>+\s*~/?\.(bashrc|profile|zshrc|bash_profile)`：覆写 shell 启动文件
- `/proc/[^/]+/environ`：泄露其他进程环境变量
- `\b(LD_PRELOAD|LD_LIBRARY_PATH)\s*=`：动态链接器劫持
- `/dev/tcp/`：bash 内建网络（绕过工具白名单）
- fork bomb 两式：`:(){ :|:& };:` 与 `while true; do bash & done`

### 3.2 中风险模式清单（`_MEDIUM_RISK_PATTERNS`）

5 条：

- `chmod\s+777`：世界可写
- `pip3?\s+install`、`apt(-get)?\s+install`：包安装（可能引入恶意依赖）
- `\b(sudo|su)\b`：在 Docker root 下是 no-op，但警告让 LLM 知情
- `\bPATH\s*=`：PATH 修改是长攻击链

### 3.3 复合命令拆分（`_split_compound_command`）

这是整个中间件最精巧的部分。问题：`safe;rm -rf /` 或 `rm -rf /&&echo ok` 这种**无空格分隔符**的复合命令如果简单按 `;`/`&&` 拆分会错过。解法是手写一个状态机扫描器，跟踪单双引号和转义状态：

```python
while index < len(command):
    char = command[index]
    if escaping:
        current.append(char); escaping = False; index += 1; continue
    if char == "\\" and not in_single_quote:
        current.append(char); escaping = True; index += 1; continue
    if char == "'" and not in_double_quote:
        in_single_quote = not in_single_quote; ...
    if char == '"' and not in_single_quote:
        in_double_quote = not in_double_quote; ...
    if not in_single_quote and not in_double_quote:
        if command.startswith("&&", index) or command.startswith("||", index):
            # 切分，跳过 2 字符
        if char == ";":
            # 切分，跳过 1 字符
    current.append(char); index += 1
```

**关键边界处理**：如果扫描结束时 `in_single_quote or in_double_quote or escaping` 为真（未闭合引号或悬挂转义），**返回原命令不拆分**（fail-closed）。源码注释解释：未闭合引号下盲目拆分可能丢掉部分命令，更安全的做法是把整个串交给分类器（它大概率会 block 或 warn）。

### 3.4 两遍分类（`_classify_command`）

```python
def _classify_command(command: str) -> str:
    # Pass 1: 整条命令对高风险正则扫描（捕获多语句结构模式）
    normalized = " ".join(command.split())
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(normalized):
            return "block"

    # Pass 2: 拆分复合命令，逐子命令分类，最严重判决胜
    sub_commands = _split_compound_command(command)
    worst = "pass"
    for sub in sub_commands:
        verdict = _classify_single_command(sub)
        if verdict == "block":
            return "block"  # 短路：不可能更糟
        if verdict == "warn":
            worst = "warn"
    return worst
```

**为什么需要两遍**：fork bomb `:(){ :|:& };:` 或 `while true; do bash & done` 这类**结构性攻击跨多个 shell 语句**，如果在 `;` 处拆分，每个子片段单独看都不触发高风险模式（`:(){` 单看无害），会漏报。Pass 1 先对整串扫描高风险正则，Pass 2 再做子命令级别分类。

`_classify_single_command` 内部还会做一次 shlex token 重排：

```python
normalized = " ".join(command.split())
for pattern in _HIGH_RISK_PATTERNS:
    if pattern.search(normalized): return "block"
try:
    tokens = shlex.split(command)
    joined = " ".join(tokens)
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(joined): return "block"
except ValueError:
    # heredoc 等多行形式 shlex 解析不了，原始扫描已覆盖
    pass
```

用 shlex 重排是为了消除引号差异（`"rm" -rf /` vs `rm -rf /`），让正则更稳。shlex 失败时降级到原始扫描结果。

### 3.5 输入消毒（`_validate_input`）

在分类之前还有一道前置门：

```python
_MAX_COMMAND_LENGTH = 10_000

def _validate_input(self, command: str) -> str | None:
    if not command.strip():
        return "empty command"
    if len(command) > self._MAX_COMMAND_LENGTH:
        return "command too long"
    if "\x00" in command:
        return "null byte detected"
    return None
```

注释：正常 bash 命令很少超过几百字符，10_000 远超任何合法用途但只是 Linux `ARG_MAX` 的零头。超过此长度几乎一定是 payload 注入或 base64 攻击串。空命令和含 null 字节的命令同样直接拒绝。

### 3.6 主流程（`_pre_process` + `wrap_tool_call`）

`_pre_process` 把所有预处理聚合成一次返回（命令、thread_id、verdict、reject_reason）：

```python
def _pre_process(self, request):
    args = request.tool_call.get("args", {})
    raw_command = args.get("command")
    command = raw_command if isinstance(raw_command, str) else ""
    thread_id = self._get_thread_id(request)

    # ① 输入消毒 — 在正则分析前拒绝畸形输入
    reject_reason = self._validate_input(command)
    if reject_reason:
        self._write_audit(thread_id, command, "block", truncate=True)
        logger.warning("[SandboxAudit] INVALID INPUT thread=%s reason=%s", thread_id, reject_reason)
        return command, thread_id, "block", reject_reason

    # ② 分类
    verdict = _classify_command(command)

    # ③ 审计日志
    self._write_audit(thread_id, command, verdict)

    if verdict == "block":
        logger.warning("[SandboxAudit] BLOCKED thread=%s cmd=%r", thread_id, command)
    elif verdict == "warn":
        logger.warning("[SandboxAudit] WARN (medium-risk) thread=%s cmd=%r", thread_id, command)

    return command, thread_id, verdict, None
```

注意**输入消毒失败也写一条 `verdict="block"` 审计**（`truncate=True` 表示超长命令在日志里截断到 200 字符），保证审计完整性。

钩子主体非常薄：

```python
@override
def wrap_tool_call(self, request, handler):
    if request.tool_call.get("name") != "bash":
        return handler(request)

    command, _, verdict, reject_reason = self._pre_process(request)
    if verdict == "block":
        reason = reject_reason or "security violation detected"
        return self._build_block_message(request, reason)
    result = handler(request)
    if verdict == "warn":
        result = self._append_warn_to_result(result, command)
    return result
```

异步版用 `await handler(request)`，其余完全一致。

要点：
- **只对 `bash` 工具有效**，其他工具（`read_file`/`write_file`/`str_replace`/MCP 工具）直接 `handler(request)` 透传。
- Block 时**不调用 handler**，直接返回 error `ToolMessage`，不让危险命令触达 sandbox。
- Warn 时**照常执行**，只在结果末尾追加警告文本，让 LLM 在下一轮看到。

### 3.7 警告追加（`_append_warn_to_result`）

```python
def _append_warn_to_result(self, result, command):
    if not isinstance(result, ToolMessage):
        return result
    warning = f"\n\n⚠️ Warning: `{command}` is a medium-risk command that may modify the runtime environment."
    if isinstance(result.content, list):
        new_content = list(result.content) + [{"type": "text", "text": warning}]
    else:
        new_content = str(result.content) + warning
    return ToolMessage(content=new_content, tool_call_id=..., name=..., status=...)
```

兼容 `content` 是 list（多模态格式）和 str 两种情况：list 模式追加一个 `{"type": "text", "text": ...}` dict，str 模式直接字符串拼接。返回新的 `ToolMessage` 而非原地修改，保持不可变风格。

### 3.8 审计日志（`_write_audit`）

```python
_AUDIT_COMMAND_LIMIT = 200

def _write_audit(self, thread_id, command, verdict, *, truncate=False):
    audited_command = command
    if truncate and len(command) > self._AUDIT_COMMAND_LIMIT:
        audited_command = f"{command[: self._AUDIT_COMMAND_LIMIT]}... ({len(command)} chars)"
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "thread_id": thread_id or "unknown",
        "command": audited_command,
        "verdict": verdict,
    }
    logger.info("[SandboxAudit] %s", json.dumps(record, ensure_ascii=False))
```

`ensure_ascii=False` 让中文/emoji 等字符原样写入日志而非转义。`truncate=True` 仅在输入消毒失败时启用（因为畸形命令可能极长，会撑爆日志行）；正常分类路径不截断，便于事后取证。

### 3.9 阻断消息构造

```python
def _build_block_message(self, request, reason):
    tool_call_id = str(request.tool_call.get("id") or "missing_id")
    return ToolMessage(
        content=f"Command blocked: {reason}. Please use a safer alternative approach.",
        tool_call_id=tool_call_id,
        name="bash",
        status="error",
    )
```

固定引导模型使用"safer alternative approach"。`status="error"` 让下游 `ToolErrorHandlingMiddleware.normalize_tool_result` 给它打 `deerflow_tool_meta`（分类大概率落到 `error_type=permission` / `recoverable_by_model=True` / `recommended_next_action=try_alternative`，与消息文案语义对齐）。

## 关键设计决策

1. **只对 `bash` 生效**：文件操作工具（`read_file`/`write_file`/`str_replace`）由 `ReadBeforeWriteMiddleware` 等专门守门人负责；本中间件聚焦 shell 命令这一最高风险面。
2. **正则 + shlex 双重分析**：正则覆盖结构性模式（fork bomb 等），shlex 消除引号差异。两者互补，任一命中即 block。
3. **两遍分类**：Pass 1 整串扫描捕获跨语句攻击，Pass 2 拆分后子命令分类捕获 `safe;rm -rf /` 这类隐蔽复合命令。注释明确警告"splitting them on `;` would destroy the pattern context"。
4. **未闭合引号 fail-closed**：`_split_compound_command` 检测到未闭合引号/转义时返回整串不拆分，让分类器大概率 block/warn，比"丢一半命令"安全。
5. **Block 不执行 vs Warn 照常执行**：高风险是确定性威胁（数据毁灭/提权），必须阻断；中风险是潜在风险（包安装、PATH 改动），让模型知情即可。
6. **无状态**：本中间件不维护任何跨请求状态，天然线程/协程安全。
7. **审计日志走 stdlib logging 而非 RunJournal**：与 `GuardrailMiddleware` 不同，本中间件不依赖 `__run_journal`，审计直接进 `gateway.log`，部署侧用标准日志管道（ELK/Loki 等）就能消费。
8. **`_MAX_COMMAND_LENGTH=10_000` 前置截断**：在正则前先拒绝超长命令，既防 ReDoS 又防 base64 payload 注入。

## 与其他中间件的协作

- **`ToolErrorHandlingMiddleware`**：本中间件返回的 block `ToolMessage(status="error")` 会被它 `normalize_tool_result` 打 `deerflow_tool_meta`，再被 `ToolProgressMiddleware`（如果启用）分类为 "可恢复，建议 try_alternative"。
- **`SandboxMiddleware`**（thread_hooks 段）：负责 sandbox 生命周期获取，比本中间件更靠外层；本中间件运行时 sandbox 已就绪，但因为 block 路径不调 handler，危险命令根本不会触达 sandbox。
- **`GuardrailMiddleware`**：同样是 `wrap_tool_call` 守门人但更通用（任何工具）。两者都跑，互相不知道对方决策。如果 Guardrail 拒绝了 `bash` 调用，本中间件根本不会被调用（因为 Guardrail 在 `tail` 段更靠外？实际看构造顺序：`tail` 顺序是 `DanglingToolCall` → `LLMErrorHandling` → `Guardrail` → `SandboxAudit` → ... → `ToolErrorHandling`，所以 Guardrail 在外层，先于本中间件决策）。
- **`bash_tool`**（`packages/harness/deerflow/sandbox/tools.py`）：真正的 handler，本中间件放行后才会执行；bash 工具自身还有 `sandbox.bash_command_timeout` 等运行时保护，本中间件是入口层防御。

---

# 4. ToolErrorHandlingMiddleware

## 概述

工具调用执行最内层的"异常转消息"中间件：捕获 handler 抛出的任何异常（除 `GraphBubbleUp` 控制流信号），把它转换成结构化的 error `ToolMessage` 让 run 继续而非崩；同时对所有正常/异常结果统一打 `deerflow_tool_meta` 元数据，对 skill 文件读取结果打 `skill_context_entry` 元数据，是下游 `ToolProgressMiddleware` 和 `DurableContextMiddleware` 的唯一元数据生产者。

## 为什么需要这个中间件

### 场景痛点

一个 agent 调用 `read_file` 读取不存在的路径，或者 `bash` 命令因为权限不足抛出 `PermissionError`，又或者 MCP 工具因远端服务挂掉返回一个非标准化异常。如果没有本中间件，这些异常会层层上抛，最终以未捕获异常的形式炸掉整个 run，所有对话历史、中间结果、checkpoint 陷入一个不完整的终态。

更麻烦的场景：子代理在后台执行 `task` 工具时，内部某个工具调用报错。如果这个异常直接传播到父 agent，父 agent 看到的是一条异常而非结构化的子代理结果，无法判断子代理到底失败了还是被截断了。

### 为什么模型自身无法避免

工具执行失败是运行时问题——文件不存在、网络不通、权限不足——这些因素发生在模型做出"调用工具"决策之后，模型无法在调用前预知。且工具 handler 由代码执行，代码会抛异常，异常是编程语言层面的机制，模型根本看不到原始异常；如果没有本中间件做转译，模型连"发生了什么"都无从得知。

### 解决思路

在 `wrap_tool_call` 最内层捕获所有非控制流异常，转换成带结构化元数据的 error `ToolMessage`，让 run 能继续走到 END 节点，同时通过 `deerflow_tool_meta` 让下游中间件和模型能理解失败原因。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` |
| 实现的钩子 | `wrap_tool_call` / `awrap_tool_call`（同步与异步对称） |
| 持久化 | 无（per-request 计算，元数据写入 `ToolMessage.additional_kwargs`，随消息进 checkpoint） |
| 配置依赖 | `AppConfig.summarization.skill_file_read_tool_names`、`AppConfig.skills.container_path`（用于 skill 元数据 stamping）；可选（`app_config=None` 时用 `DEFAULT_SKILL_FILE_READ_TOOL_NAMES` 与 `DEFAULT_SKILLS_CONTAINER_PATH`） |
| 协作模块 | `tool_result_meta.py`（`normalize_tool_result`/`stamp_exception_meta`/`ToolResultMeta`）、`skill_context.py`（`build_skill_entry_metadata_from_read`/`SKILL_CONTEXT_ENTRY_KEY`）、`subagents.status_contract`（`format_subagent_result_message`/`make_subagent_additional_kwargs`） |
| 上下游位置 | 在 `_build_runtime_middlewares` tail 段**最内层**（最后 append）；被 `ToolProgressMiddleware`、`ReadBeforeWriteMiddleware`、`SandboxAuditMiddleware`、`GuardrailMiddleware` 全部包裹；元数据被所有这些外层消费者读取 |

## 核心逻辑

### 4.1 主流程（`wrap_tool_call` / `awrap_tool_call`）

```python
@override
def wrap_tool_call(self, request, handler):
    try:
        result = handler(request)
    except GraphBubbleUp:
        # 保留 LangGraph 控制流信号
        raise
    except Exception as exc:
        logger.exception("Tool execution failed (sync): name=%s id=%s", ...)
        return self._build_error_message(request, exc)
    return normalize_tool_result(self._maybe_stamp(result, request))
```

异步版用 `await handler(request)`，其余完全一致。

三段式：

1. **`handler(request)` 执行**：让真正的工具逻辑跑起来。
2. **`GraphBubbleUp` 透传**：LangGraph 的 `interrupt`/`resume`/`pause` 控制流信号绝不能被吞，否则 HITL 流程会卡死。这是 DeerFlow 所有中间件的一致约定。
3. **其他异常转消息**：用 `logger.exception` 记带堆栈的 error 日志（含 tool name + tool_call_id），返回 `_build_error_message` 构造的 error `ToolMessage`。
4. **正常路径打元数据**：`normalize_tool_result(self._maybe_stamp(result, request))` —— 先 stamp skill read 元数据（如果是 skill 文件读取），再 normalize 打 `deerflow_tool_meta`。

### 4.2 错误消息构造（`_build_error_message`）

```python
def _build_error_message(self, request, exc):
    tool_name = str(request.tool_call.get("name") or "unknown_tool")
    tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
    detail = str(exc).strip() or exc.__class__.__name__
    if len(detail) > 500:
        detail = detail[:497] + "..."

    content = f"Error: Tool '{tool_name}' failed with {exc.__class__.__name__}: {detail}. {_RECOVERY_HINT}"
    message = ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=tool_name,
        status="error",
    )
    # 本中间件是异常包装器的生产者，所以 task 失败也带结构化元数据
    structured_error = f"{exc.__class__.__name__}: {detail}"
    message = _stamp_task_exception_status(message, tool_name=tool_name, error=structured_error)
    return stamp_exception_meta(message, structured_error)
```

要点：

- **detail 截断到 500 字符**（`[:497] + "..."`），防超长异常消息撑爆模型上下文。
- **`_RECOVERY_HINT`** 固定尾巴："Continue with available context, or choose an alternative tool."，引导模型自救。
- **三重 stamping 链**：
  1. `_stamp_task_exception_status`：如果工具是 `task`（子代理委派），给它附上 subagent status contract 结构化元数据；
  2. `stamp_exception_meta`：用 `tool_result_meta` 模块给所有异常包装打 `deerflow_tool_meta`，`source="exception"`，分类信息来自异常文本。

### 4.3 task 工具特殊 stamping（`_stamp_task_exception_status`）

```python
_TASK_TOOL_NAME = "task"

def _stamp_task_exception_message(message: ToolMessage, *, tool_name: str, error: str) -> ToolMessage:
    if tool_name != _TASK_TOOL_NAME:
        return message
    content, metadata_error = format_subagent_result_message("failed", error=error)
    if not content.endswith((".", "!", "?")):
        content += "."
    message.content = f"{content} {_RECOVERY_HINT}"
    existing = dict(message.additional_kwargs or {})
    existing.update(make_subagent_additional_kwargs("failed", error=metadata_error))
    message.additional_kwargs = existing
    return message
```

子代理委派工具（`task`）的异常需要带 `subagent_status`/`subagent_error` 结构化字段（对齐 `contracts/subagent_status_contract.json`），让前端 subtask 卡片能正确显示"failed"状态。这里复用 `format_subagent_result_message` + `make_subagent_additional_kwargs` 这对 helper，保证异常路径与正常路径产出的 task `ToolMessage` 在结构上等价（注释："task failures raised before task_tool can build its own Command still carry the same structured metadata"）。

### 4.4 异常元数据 stamping（`stamp_exception_meta`）

来自 `packages/harness/deerflow/agents/middlewares/tool_result_meta.py`：

```python
def stamp_exception_meta(msg: ToolMessage, exc_info: str) -> ToolMessage:
    attrs = _classify_error_text(exc_info)
    updated_kwargs = dict(msg.additional_kwargs or {})
    updated_kwargs[TOOL_META_KEY] = _make_meta(status="error", source="exception", **attrs)
    msg.additional_kwargs = updated_kwargs
    return msg
```

**总是覆盖**已有的 `deerflow_tool_meta`（与 `normalize_tool_message` 的"已有则保留"语义相反）。源码注释："Exception-derived classification is more authoritative than a tool's own return-time stamp." —— 异常分类比工具自己返回时打的标记更权威，所以强制覆盖。

`_classify_error_text` 用 `_ERROR_RULES` 表按关键字匹配，给出 `(error_type, recoverable_by_model, recommended_next_action)` 三元组：

| 关键字 | error_type | recoverable | next_action |
|--------|-----------|-------------|-------------|
| 401/403/unauthorized/invalid api key | auth | False | stop |
| rate limit/rate_limit | rate_limited | False | summarize |
| timeout/connection/network error | transient | False | try_alternative |
| not configured/missing required/disabled | config | False | stop |
| permission denied/path traversal/forbidden | permission | True | try_alternative |
| no results found/no content found | no_results | True | rewrite_query |
| not found/no such file/404 | not_found | True | rewrite_query |
| unexpected error/internal error/500 | internal | False | stop |
| （未匹配） | unknown | True | try_alternative |

**关键细节**：纯数字关键字（401/403/404/500）用 `\b` 词边界正则匹配，防止"took 500ms"这种无关数字被误判为 internal error。这部分逻辑由 `_match_keyword` + `_NUMERIC_KW_RE`（在模块加载时预编译）实现。

### 4.5 正常结果 normalize（`normalize_tool_result` → `normalize_tool_message`）

```python
def normalize_tool_result(result):
    if isinstance(result, ToolMessage):
        return normalize_tool_message(result)
    return result
```

`normalize_tool_message` 是更复杂的分类器，按多种信号判定 `status`：

1. **已有 `deerflow_tool_meta` 则直接返回**（保留上游 stamp，例如 `ReadBeforeWriteMiddleware` 阻断写入时已自己打过 stamp）。
2. **`status="error"` 但内容不以 "Error:" 开头**：先尝试 JSON 提取 `error` 字段（`_extract_json_error_text`），命中则按 error 文本分类；否则判断是否是"无 error 键的 JSON dict" —— 是则当 unknown 不强行分类（防止 `{"user_id": 401}` 这种字段值被误命中 "401" → auth）；否则按原始文本分类。
3. **内容以 "Error:" 开头**：按去掉前缀后的文本分类。
4. **JSON 含 `error` 字段**：按 error 字段值分类。
5. **命中 `_PARTIAL_MARKERS`**（"partial results"/"truncated"/"no results found"/...）：`partial_success` + `recommended_next_action="rewrite_query"`。
6. **其他**：`success`。

JSON `error` 字段的"语义零值"处理（`_SEMANTIC_ZERO_ERROR_STRINGS = {"none", "null", "false", "no", "ok", "success", "n/a", ""}`）：很多工具在成功时返回 `{"error": "none", "results": [...]}`，这种不能被误判为 error。源码注释："This prevents tools that return `{"error": "none", "results": [...]}` on success from being misclassified as errors."

### 4.6 skill 文件读取 stamping（`_stamp_skill_read_metadata`）

```python
def _stamp_skill_read_metadata(self, message, request, *, tool_name):
    if tool_name not in self._skill_read_tool_names:
        return message
    if getattr(message, "status", "success") == "error":
        return message
    content = message.content if isinstance(message.content, str) else None
    if content is None:
        return message
    path = _tool_call_path(request.tool_call)
    if path is None:
        return message
    entry = build_skill_entry_metadata_from_read(path, content, skills_root=self._skills_root)
    if entry is None:
        return message
    existing = dict(message.additional_kwargs or {})
    existing[SKILL_CONTEXT_ENTRY_KEY] = dict(entry)
    message.additional_kwargs = existing
    return message
```

只对配置的 skill 读取工具（`skill_file_read_tool_names`，默认是 `read_file` 等）生效，且仅在成功（非 error）且 content 是字符串时才尝试 stamp。

`build_skill_entry_metadata_from_read`（来自 `skill_context.py`）做的事：

1. 把传入的 path 规范化（`posixpath.normpath`），验证它落在 `skills_root` 之下；
2. 验证 basename 是 `SKILL.md`；
3. 验证 content 不是 "Error:" 开头（失败结果不 stamp）；
4. 用 `_FRONT_MATTER_RE` 正则提取 YAML frontmatter，用 `yaml.safe_load` 解析，取出 `description` 字段，空白归一化并截断到 `_SKILL_DESCRIPTION_MAX_CHARS`；
5. 返回 `{"path": normalized_path, "description": ...}`。

这个 `skill_context_entry` 元数据是 `DurableContextMiddleware` 在 summarization 压缩 messages 后**重建** `ThreadState.skill_context` 的关键线索：它会扫描所有 `ToolMessage` 的 `additional_kwargs[SKILL_CONTEXT_ENTRY_KEY]`，把已加载的 skill 引用重新提取出来塞回 state，让 skill 在压缩后仍可见。`skill_context.py` 里的 `extract_skills` 函数就是干这件事的，它依赖本中间件预先 stamp 的元数据，源码里还有 mismatch 检测：

```python
if metadata["path"] != expected_path:
    logger.warning("mismatched skill read metadata: tool_call_id=%s expected_path=%s metadata_path=%s", ...)
    continue
```

### 4.7 `_maybe_stamp` 桥接

```python
def _maybe_stamp(self, result, request):
    if not isinstance(result, ToolMessage):
        return result
    tool_name = str(request.tool_call.get("name") or "")
    return self._stamp_skill_read_metadata(result, request, tool_name=tool_name)
```

只对 `ToolMessage`（不是 `Command`）做 skill stamping，再交给 `normalize_tool_result` 统一 normalize。`Command` 透传（LangGraph 控制流命令）。

### 4.8 构造期配置解析

```python
def __init__(self, *, app_config: AppConfig | None = None) -> None:
    super().__init__()
    self._app_config = app_config
    if app_config is None:
        self._skill_read_tool_names = frozenset(DEFAULT_SKILL_FILE_READ_TOOL_NAMES)
        self._skills_root = DEFAULT_SKILLS_CONTAINER_PATH
    else:
        self._skill_read_tool_names = frozenset(app_config.summarization.skill_file_read_tool_names)
        self._skills_root = app_config.skills.container_path
```

支持无 config 构造（用默认值），方便测试与 embedded 场景。

## 关键设计决策

1. **最内层位置**：注释明确"ToolProgressMiddleware must be outer (lower index) so its wrap_tool_call handler chain includes ToolErrorHandlingMiddleware (inner), which stamps `deerflow_tool_meta` on every result before ToolProgressMiddleware reads it"。本中间件是元数据的生产者，必须被所有需要元数据的消费者包裹。`_build_runtime_middlewares` 末尾还有显式的位置断言：

   ```python
   if _ToolProgressMiddleware is not None:
       _progress_idx = next((i for i, m in enumerate(middlewares) if isinstance(m, _ToolProgressMiddleware)), None)
       _error_idx = next((i for i, m in enumerate(middlewares) if isinstance(m, ToolErrorHandlingMiddleware)), None)
       if _progress_idx is not None and _error_idx is not None and _progress_idx > _error_idx:
           raise RuntimeError(f"ToolProgressMiddleware must be outer (index {_progress_idx}) of ToolErrorHandlingMiddleware (index {_error_idx}) — check middleware append order")
   ```

   用 `isinstance` 而非 `type().__name__`，子类和重命名都覆盖。Fail-fast 在构建期暴露顺序错误，不在运行时静默 noop。

2. **`GraphBubbleUp` 透传**：与 `LLMErrorHandlingMiddleware`、`GuardrailMiddleware` 一致的全局约定，保留 LangGraph HITL 控制流信号。

3. **异常 stamp 覆盖正常 stamp**：`stamp_exception_meta` 总是覆盖 `deerflow_tool_meta`，因为异常分类更权威。防止工具先打了 "success" 然后又抛异常的奇怪场景下，下游消费者误读 success 标记。

4. **数字关键字词边界**：`\b401\b`、`\b500\b` 等预编译正则防止"took 500ms"被误判 internal error。这种细节体现生产环境打磨程度。

5. **JSON error 字段语义零值处理**：`{"error": "none", ...}` 不算 error。让常见工具约定（成功时也带 error 字段）不会被误分类。

6. **task 异常路径与正常路径结构对齐**：通过 `_stamp_task_exception_status` 复用 `format_subagent_result_message` + `make_subagent_additional_kwargs`，异常路径产出的 task ToolMessage 与正常路径产出的在 `additional_kwargs` 结构上等价，前端 subtask 卡片无需区分两种来源。

7. **detail 截断到 500 字符**：与 `GuardrailMiddleware._REASON_MESSAGE_LIMIT` 一致的 500 字符上限，防超长异常消息撑爆模型上下文和 run_events 行。

8. **skill stamping 只对成功结果**：error 结果不 stamp skill 元数据，因为 skill 加载失败的话根本没有"已加载的 skill"需要 durable 恢复。

9. **`app_config` 可选**：让中间件在测试和 embedded 场景下能无 config 构造，降低使用门槛。

## 与其他中间件的协作

- **`ToolProgressMiddleware`**（外层）：读取本中间件打的 `deerflow_tool_meta` 中的 `recoverable_by_model`/`recommended_next_action` 字段，做 stagnation 检测（连续 N 次 "no new info" 则警告/阻断）。本中间件是它的元数据生产者。
- **`DurableContextMiddleware`**（lead 链更外层，在 summarization 之前）：通过 `skill_context.extract_skills` 扫描所有 `ToolMessage.additional_kwargs[SKILL_CONTEXT_ENTRY_KEY]`，在 summarization 压缩 messages 前把已加载 skill 引用抢救回 `ThreadState.skill_context`。本中间件是它的元数据生产者。
- **`ReadBeforeWriteMiddleware`**（外层）：阻断写入时自己调用 `normalize_tool_result` 直接 stamp `deerflow_tool_meta`（`recoverable_by_model=True`），所以本中间件看到的是已带 stamp 的 ToolMessage，`normalize_tool_message` 走"已有则保留"分支直接返回。
- **`SandboxAuditMiddleware`**（外层）：对 `bash` 工具的 block/warn 结果也是 ToolMessage，流经本中间件时被 normalize（block 结果分类大概率落到 permission/try_alternative）。
- **`GuardrailMiddleware`**（外层）：拒绝时返回的 `ToolMessage(status="error")` 同样被本中间件 normalize 打 `deerflow_tool_meta`，让 ToolProgressMiddleware 能识别为"被拒工具结果"。
- **`SubagentExecutor`**：subagent 链也挂本中间件（共享 `_build_runtime_middlewares`）；子代理内部工具调用的异常同样被转成 error ToolMessage，不会炸掉 subagent run。子代理的 task 工具调用（如果有嵌套）会复用 `_stamp_task_exception_status` 保持协议一致。
- **`DeerFlowSummarizationMiddleware`**：summarization 压缩 messages 时依赖本中间件 stamp 的 `skill_context_entry` 元数据来恢复 `ThreadState.skill_context`，否则压缩后已加载的 skill 引用会丢失。

---

# 附录：四个中间件在 `tail` 段的相对顺序

根据 `packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py::_build_runtime_middlewares` 的 append 顺序，`tail` 段（post-processing append-only middlewares）从外到内依次是：

1. `DanglingToolCallMiddleware`（条件挂载）
2. `LLMErrorHandlingMiddleware`
3. `GuardrailMiddleware`（条件挂载，`guardrails.enabled`）
4. `SandboxAuditMiddleware`
5. `ReadBeforeWriteMiddleware`（条件挂载，`read_before_write.enabled`）
6. `ToolProgressMiddleware`（条件挂载，`tool_progress.enabled`）
7. `ToolErrorHandlingMiddleware`（**最内层**）

"外层" = `wrap_tool_call` 链上更早执行（pre-handler 阶段先跑，post-handler 阶段后跑）；"内层" = 更贴近真正的工具 handler。`ToolErrorHandlingMiddleware` 最内意味着它直接包裹 handler，是异常和元数据 stamping 的"最后一道关卡"，所有外层中间件看到的结果都已经被它处理过。

这条链上的分工清晰：

- **Guardrail/SandboxAudit/ReadBeforeWrite**：决策类（是否允许执行）
- **ToolProgress**：监测类（结果是否带来新信息）
- **ToolErrorHandling**：兜底类（异常转消息 + 元数据 stamping）

四者通过 `deerflow_tool_meta` 这套结构化协议协作，避免每个中间件都自己解析 ToolMessage.content 文本，这是 DeerFlow 中间件链工程化程度的体现。
