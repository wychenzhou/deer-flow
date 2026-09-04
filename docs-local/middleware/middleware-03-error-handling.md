# 错误处理与安全守卫中间件（深度解析）

> 本文是 `middleware` 系列的教学向深度文档，聚焦 shared runtime base 的五个中间件：
> `LLMErrorHandlingMiddleware`、`GuardrailMiddleware`（含 `Authorization` / `GuardrailAuthorizationAdapter` 双层门）、
> `SandboxAuditMiddleware`、`ToolReceiptMiddleware`、`ToolErrorHandlingMiddleware`。
>
> 阅读目标：搞懂「**一条工具调用从模型发出到结果回灌模型，中途会经过几道门、谁把异常变成结构化信号、
> 谁为每次执行打不可伪造的凭证**」。代码引用均为仓库相对路径（根为 `backend/packages/harness/deerflow/`）。
> 建议先读 `agents/middlewares/AGENTS.md` 的 "Middleware Chain"，再看本文实现细节。
---

## 0. 装配位置：shared runtime base 里的第 8~13 位
### 0.1 三段式装配与 tail 追加顺序

lead / subagent 共用的 shared runtime base 由 `agents/middlewares/tool_error_handling_middleware.py` 里的
`_build_runtime_middlewares()` 生成，经 `build_lead_runtime_middlewares()`（lead）与
`build_subagent_runtime_middlewares()`（subagent）暴露，按 `[*outer_wrappers, *thread_hooks, *tail]` 拼装：

- `outer_wrappers`（最外层 `wrap_model_call`）：`InputSanitizationMiddleware` → `ToolOutputBudgetMiddleware` → `ToolResultSanitizationMiddleware`；
- `thread_hooks`（`before_agent` 钩子）：`ThreadDataMiddleware` →（lead 才有）`UploadsMiddleware` → `SandboxMiddleware`；
- `tail`（本文主角，lead 全开时的 1-based 下标，`(opt)` 表示可选、条件追加）：

```
 7 DanglingToolCallMiddleware  (include_dangling_tool_call_patch=True)
 8 LLMErrorHandlingMiddleware   第 8 位：把 provider 调用失败恢复成 assistant 可读消息
 9 ToolReceiptMiddleware       (opt, verification.receipts_enabled 默认开) ── 最外层 wrap_tool_call
10 GuardrailMiddleware         (opt, authorization.enabled)      ← Layer 2：GuardrailAuthorizationAdapter
11 GuardrailMiddleware         (opt, guardrails.enabled+provider) ← 外部 GuardrailProvider
12 SandboxAuditMiddleware       bash 命令分级审计
13 ReadBeforeWriteMiddleware   (opt, read_before_write.enabled 默认开)
14 ToolProgressMiddleware      (opt, tool_progress.enabled)
15 ToolErrorHandlingMiddleware  最内层：异常 → 结构化 error ToolMessage
```

base 拼完后，lead-only 中间件（`DynamicContextMiddleware` 起约 18 个）由 `lead_agent/agent.py::build_middlewares()`
**追加在 base 之后**；最后在 `extensions/stack.py::compose_with_extensions()`（最外层 builder 末尾、扩展贡献合并完成后）
对整条链做 `assert_ordering` 校验——提前校验会让扩展悄悄反转不变式而不报错。

> **编号口径**：`agents/middlewares/AGENTS.md` 的条目编号（8 LLMErrorHandling、9 Authorization/Guardrail、10 SandboxAudit、
> 11 ReadBeforeWrite、12 ToolProgress、13 ToolReceipt+ToolErrorHandling）是**职能分组编号**而非严格下标：真实 append 顺序里
> `ToolReceiptMiddleware` 紧跟在 LLMErrorHandling 之后（第 9 个下标位），`ToolErrorHandlingMiddleware` 永远在链尾——
> 第 13 号条目实为「一对夹住 9~12 短路者的书挡」（receipt 在外、error handling 在内）。下文统一用**代码下标**，对应关系：
> 8=LLMErrorHandling，9=ToolReceipt，10/11=Authorization+Guardrail，12=SandboxAudit，13=ReadBeforeWrite，14=ToolProgress，15=ToolErrorHandling。

### 0.2 组合语义与两条硬不变式

LangChain 组合规则（代码注释原文："compose with first in list as outermost layer"）：**列表下标越小越靠外**，
`wrap_model_call` / `wrap_tool_call` 按「外层先执行、再调内层 handler」嵌套；`after_model` 类钩子按逆序分发。

由此推出本组的顺序契约（`extensions/ordering.py::core_ordering_constraints` 声明，装配末端 `assert_ordering`
强校验，违规直接 `RuntimeError` 并指认责任来源）：

```python
ToolProgressMiddleware  outer of ToolErrorHandlingMiddleware  # Progress 要在内层返回路径读到 meta
ToolReceiptMiddleware   outer of ToolErrorHandlingMiddleware  # Receipt 要用 meta.status 生成凭证
ToolReceiptMiddleware   outer of {Guardrail, SandboxAudit, ReadBeforeWrite, ToolProgress}
                        # ↑ 这四个都可能短路/重建 ToolMessage；receipt 不包住它们，账本就静默漏记
```

一条 `bash` 工具调用的完整进出路径（只画本组中间件，由上到下 = 由外到内）：

```
模型响应带 tool_calls
   ▼
[8  LLMErrorHandling]   重试/退避/熔断：provider 失败拦在这一层（9~15 感知不到调用失败过）
   ▼
[9  ToolReceipt]        最外层 wrap_tool_call：结果（含被短路者）返回时第一个打 receipt
   ▼
[10 Guardrail(authz)]   Layer 2 执行期授权：deny 在此短路，不进 11/12…
[11 Guardrail(外部)]    显式配置的 GuardrailProvider，仍评估每个调用（含 tool_search）
   ▼
[12 SandboxAudit]       只查 bash：block 短路；warn 放行但重建结果并附加警告
[13 ReadBeforeWrite]    (opt) 写门：blocked 短路（自 stamp meta）
[14 ToolProgress]       (opt) 结果质量状态机（读 15 打的 meta）
   ▼
[15 ToolErrorHandling]  最内层：真正执行工具；异常 → 结构化错误 + stamp deerflow_tool_meta
   ▼
ToolNode → 沙箱 handler
```

一个"错位"值得注意：第 9 位 ToolReceipt 的 `wrap_model_call` 是第 8 位 LLMErrorHandling 的**内层**——每次重试都会
重新渲染一遍 ledger（ledger 从消息流派生、天然幂等）；LLM 调用彻底失败时 handler 抛异常，引用快照步骤不执行，
但兜底消息本身不含引用，无影响。
---

## 1. LLMErrorHandlingMiddleware（第 8 位）

### 它解决什么问题

模型调用是 run 的心脏，也是外部依赖里最不稳的一环。没有这层处理时：

- provider 返回 `500/502/503/504/429/408/409/425`、连接被掐断（`RemoteProtocolError`）、流式分块超过
  `stream_chunk_timeout` 停顿（`StreamChunkTimeoutError`）……任何一个都会让**这一步模型调用直接抛异常**、
  整条 run 被打断，用户看到的是原始堆栈而不是可行动的提示；
- 某些端点会 `200 OK` 但带空 `generations` 列表（Volces），LangChain 随后抛 `IndexError: list index out of range`
  ——这其实是上游瞬时抖动，却被当成客户端 bug；
- 限流分两种：通用 `429`（瞬时）与 **burst-rate**（`limit_burst_rate`，按请求速率的*斜率*限流，早高峰 0→满载
  的几分钟内最容易触发）。前者重试即可，后者重试是在给正在被掐的斜率**火上浇油**；
- 一批请求同时失败时，若都按固定指数退避（`base * 2^n`）重试，会**同步再峰**、重新触发限流。

### 钩子与执行时机

`wrap_model_call` / `awrap_model_call`。它是 shared base 里最内层的模型调用包裹者（第 8 位）：外层是
InputSanitization / ToolOutputBudget / ToolResultSanitization，所以它**看到的永远是消毒后的消息**；内层是
ToolReceipt 的 ledger 渲染与真实模型调用。位置含义：provider 层面的失败在这里就被消化，下游阶段（工具执行、
receipt、进度状态机）不需要知道模型调用失败过——除非失败到要返回兜底消息。

### 内部实现逻辑：五段流水

失败处理 = **分类 → 定预算 → 算退避 → 限并发 → 熔断**，最后用兜底消息收尾。

**① 错误分类**（`_classify_error` → `(retriable, reason)`，先判 quota/auth 再判 burst_rate，最后才是通用类）：

```python
quota/auth（消息与 error code 模式匹配）        → 不可重试      # 凭证/额度错，重试无意义
burst_rate（limit_burst_rate / "rate increased too
  quickly" / "请求速率增长过快"…）              → 可重试×专用策略 # 必须在通用 429 之前判定！
APITimeoutError / APIConnectionError / InternalServerError /
  ReadError / RemoteProtocolError / StreamChunkTimeoutError    → transient
isinstance(IndexError)                          → transient      # Volces 200+空 generations
status_code ∈ {408,409,425,429,500,502,503,504} → transient
busy 文案（"server busy"/"overloaded"/"负载较高"…）→ busy（可重试）
其余                                            → generic（不可重试）
```

**② 有效重试预算** = `min(retry_max_attempts, 分异常类覆盖, 分 reason 覆盖)`。类名覆盖
`{StreamChunkTimeoutError: 2}`（上游已卡 120–240s，全 3 次会堆 6–12 分钟死寂，只留 1 次"廉价重连"）；
reason 覆盖 `{burst_rate: 2}`（重试 burst 是在给被限的斜率加需求）。**UI / 事件里的 `attempt/max_attempts`
必须用有效预算**——绝不向用户承诺不会发生的重试。

**③ 退避**：先尊重服务端 `Retry-After`（`retry-after-ms`/`Retry-After` 等头，毫秒/秒/HTTP date 三种格式，
解析失败回退计算），有则原样遵守不加抖动；否则 AWS 式**去相关抖动**：

```python
high = min(cap, max(base, seed * 3))   # 先 clamp 窗口再取样，避免 ~70% 堆在 cap 上
delay = random.randint(base, high)     # seed = 上次 delay，首次用 reason 专属 base
```

burst_rate 换用更长的 `burst_retry_base_delay_ms=5000`（普通 base=1000、cap=8000）——否则首次重试窗口坍缩成
`randint(5000, 5000)`，整个集群在同一秒再撞限流；用 5000 播种则 `randint(5000, 8000)`，一起失败的集群散开。

**④ 进程级并发 limiter**：单次尝试外包给基于 `threading` 原语的全进程 limiter（`_ProcessWideLimiter`，
`max_concurrent_llm_calls`，默认 0=关闭）。为什么不用 `asyncio.Semaphore`？它绑定第一个使用的 event loop——
lead 主 loop、subagent 隔离 loop（`subagents/executor.py`）、sync 路径是**三个 loop**，per-loop 上限挡不住扇出。
cap **启动即冻结**（首个 `__init__` 解析，之后一律 no-op），消除热重载改 cap 的下调竞态；退避睡眠在
`release()` 之后进行，**不占并发槽**。

**⑤ 熔断器**（closed → open → half-open，每实例一份、thread-lock 保护）：open 期 fast-fail 返回兜底消息不真实调用；
到点转 half-open 只放行**一个探针**，成功 `_record_success` 回 closed，失败再 open 一个 `recovery_timeout`。
关键细节：**只有「可重试且已用尽预算」的错误才 `_record_failure()`**——quota/auth 不计 fail（凭证错 ≠ 服务挂）；
burst_rate 失败只释放探针不记 fail（否则 slope 限流会把自己熔断成 #4290 想防的自造故障）。`GraphBubbleUp`
（暂停/恢复控制流）永远 re-raise 并释放探针，绝不能被吞。

**⑥ 兜底消息**：预算用尽或不可重试 → 返回带标记的 AIMessage：

```python
AIMessage(content=按 reason 分发的文案, additional_kwargs={
    "deerflow_error_fallback": True,   # ← 下游判断"子代理是否真失败"的唯一权威信号
    "error_type": 异常类名, "error_reason": reason, "error_detail": detail,
})
```

文案按原因分支：quota→额度/计费；auth→凭证；流中断类（`_STREAM_DROP_EXCEPTIONS`）→建议**拆分请求/缩短输出**
（根因通常是单个超大 tool-call 序列化拖死上游流）；普通繁忙→等待后续聊；其余→原始错误文本。每次重试经流式
writer 发 `llm_retry` 事件（前端显示"正在重试 1/2"），事件发送本身 try/except 包裹、绝不影响主流程。

### 与邻居的关系

- **对内（第 9 位 ToolReceipt）**：每次重试都重新走一遍内层 ledger 渲染——ledger 派生自消息流，幂等无副作用；
  调用成功才轮到 ToolReceipt 打引用快照；
- **对模型调用链**：provider 原始错误**从不进入模型可见上下文**——模型只看到"重试后成功"或干净的中文兜底消息；
- 兜底消息的 `deerflow_error_fallback` 标记是 run 收尾的重要信号：worker / 子代理执行器据此把 run 判为失败
  （并忽略历史 checkpoint 里残留的旧 fallback，见 `worker.py::_extract_llm_error_fallback_message`）。

### 设计权衡

- **宁可多睡不可同步再峰**：去相关抖动牺牲单次最优等待，换取整群失败时的错峰；
- **熔断只对"真·持续故障"记账**：把 quota/auth/burst_rate 排除在熔断计数外，防止"凭证过期/限流策略"这类可恢复
  的人为状态把整个 provider 打成"已死"，导致全系统 fast-fail；
- **有效预算 ≤ 配置上限是特性**：配置是操作员天花板，代码只允许**收紧**；UI 说 1/2 就真只有 2 次尝试；
- 兜底是"assistant-facing"的：不抛异常、不中止 run，让 run 以**可解释的失败**收尾（子代理场景再映射为 failed）。

### 源码阅读指引

`agents/middlewares/llm_error_handling_middleware.py`：顶部常量（`_RETRIABLE_STATUS_CODES`/`_QUOTA/_AUTH/_BURST/_BUSY_PATTERNS`
含中文文案/`_RETRY_BUDGET_OVERRIDES`/`_REASON_RETRY_BUDGETS`/`_STREAM_DROP_EXCEPTIONS`）；`_ProcessWideLimiter`
与 `_apply_configured_cap`（跨 loop 许可证、取消交接、启动冻结）；`LLMErrorHandlingMiddleware` 的
`_classify_error`/`_max_attempts_for`/`_build_retry_delay_ms`/`_check_circuit`/`_record_success`/`_record_failure`/
`_release_half_open_probe`/`wrap_model_call`/`awrap_model_call`；模块尾部 `_extract_status_code`/`_extract_error_code`/
`_extract_retry_after_ms`/`_extract_error_detail`。
---

## 2. GuardrailMiddleware（第 10/11 位：Authorization / GuardrailAuthorizationAdapter 双层门）

### 它解决什么问题

模型会调它不该调的：`rm -rf`、读别人的文件、调未授权的 MCP 工具、在受限上下文里委派 `task`……越权要防两层：

- **Layer 1（装配期能力过滤）**：`AuthorizationProvider.filter_resources(...)` 在工具装配阶段把不可见工具从模型绑定的
  schema 里**过滤掉**（看不见就不存在）。但它只在 build 期生效、按工具名粗粒度决策，管不了"工具可见但本次调用参数越权"
  （同一把 `bash`，参数里命令不同危险程度不同）；
- **Layer 2（执行期检查）**：工具真正执行**前**，对 `(principal, resource="tool", action="call", target=工具名, 上下文)`
  再问一次 provider。

同时还有**外部 guardrail**：`guardrails.enabled` 时显式配置的 `GuardrailProvider`（Protocol 接口——任何类实现
`evaluate/aevaluate` 即可接入，无需继承内部基类）。DeerFlow 把两者塞进**同一个 `GuardrailMiddleware` 类**、
以不同 provider 参数实例化——deny 审计、fail-closed 降级、同步/异步、错误 ToolMessage 只实现一次。

### 双层门全貌与装配

```
          ┌─────────────── Layer 1（装配期，无中间件）───────────────┐
 build 期  │ AuthorizationProvider.filter_resources → 过滤工具 schema  │
          └──────────────────────────────────────────────────────────┘
                      ▼  （同一 provider 实例贯穿两层）
          ┌─────────────── Layer 2（执行期 = GuardrailMiddleware）─────┐
 工具调用   │ GuardrailRequest → GuardrailAuthorizationAdapter          │
          │   └─ 复用 AuthorizationProvider.authorize(tool/call)       │
          │ 外部 GuardrailProvider（guardrails.enabled，追加在内层）      │
          └───────────────┬────────────────────────────────────────────┘
          ┌───────────────┼──────────────────┐
          ▼               ▼                  ▼
       allow           deny             provider 抛异常
          │         error ToolMessage     ├─ fail_closed=True  → deny(oap.evaluator_error)
          │         （只展示第一条原因）      └─ fail_closed=False → allow + 审计"provider error"
          ▼
    handler(request) 继续向下
```

装配（`_build_runtime_middlewares` 的 tail 追加）：

```python
if authorization_config.enabled is True:          # 授权先追加 → 处于外层
    provider = authorization_provider or resolve_authorization_provider(authorization_config)
    tail.append(GuardrailMiddleware(
        GuardrailAuthorizationAdapter(provider,
            default_role=authorization_config.default_role,
            infrastructure_tool_names=authorization_infrastructure_tool_names),
        fail_closed=authorization_config.fail_closed))
if guardrails_config.enabled and guardrails_config.provider:   # 外部 guardrail 后追加 → 内层
    tail.append(GuardrailMiddleware(resolve_variable(provider_cls)(**kwargs),
                                    fail_closed=guardrails_config.fail_closed,
                                    passport=guardrails_config.passport))
```

**授权在外层**意味着：授权先 deny 时，外部策略调用根本不会发生（省一次不必要调用）。`GuardrailAuthorizationAdapter`
（`authz/adapter.py`）不是新中间件，而是把 `AuthorizationProvider` **伪装成 GuardrailProvider**：`evaluate(gr)` →
`build_principal_from_context` 构造 Principal（与 Layer 1 共用同一身份构建：`default_role` 兜底、`authz_attributes`
规范化）→ `provider.authorize(AuthzRequest(resource="tool", action="call", target=工具名, context={thread_id, run_id,
tool_call_id, tool_input, is_subagent, agent_id, timestamp}))` → `AuthzDecision` 转回 `GuardrailDecision`。
provider 异常**故意向上抛**给 GuardrailMiddleware 统一 fail-closed（适配器里 catch 会复制逻辑并漂移）。
一个例外豁免：当前 build 存在具体的 deferred 工具设置时，生成的 `tool_search` 在 `infrastructure_tool_names` 里
（其 catalog 已被 Layer 1 过滤过），适配器直接放行（`authz.infrastructure_tool`）；**没有** deferred 设置的普通同名
工具不享受豁免，外部 GuardrailProvider 则仍评估每个调用（含 `tool_search`）。

### 钩子与执行时机

`wrap_tool_call` / `awrap_tool_call`，在调用内层 handler **之前**做决策。决策流（同步/异步同构）：

```python
gr = GuardrailRequest(tool_name, tool_input, agent_id=passport, thread_id, user_id, user_role,
                      is_subagent, channel_user_id, is_internal, authz_attributes, …)  # 字段缺失回退默认
try:
    decision = provider.evaluate(gr)                    # 或 aevaluate
except GraphBubbleUp:
    raise                                                # 控制流优先透传
except Exception:
    decision = deny(oap.evaluator_error)  if fail_closed else allow(oap.evaluator_error)
    audit(action=…deny_tool_call / allow_tool_call_after_provider_error, provider_error=True)
    put_authorization_outcome(...)
    return denied_message 或 handler(request)
put_authorization_outcome(context, tool_call_id, …)     # 每条决策路径都发布
return denied_message if not decision.allow else handler(request)
```

- **deny 的 ToolMessage**：`status="error"`，文案只展示 `reasons[0]` 第一条（code + message），末尾固定引导
  `Choose an alternative approach.`——让模型**改路**而非硬撞；
- **审计（best-effort）**：`context["__run_journal"]` 存在才写 `record_middleware(tag="middleware:guardrail", …)`；
  journal 缺失跳过（embedded/subagent 无 journal），落盘异常只记 warning——审计**绝不改变工具执行结果**；
  拒绝理由每条截断到 500 字符；
- **AuthorizationOutcome**（`authz/outcome.py`）：每条决策路径都把 `AuthorizationOutcome(decision, policy_id,
  policy_version, reason_codes)` 发布进 per-run runtime context，键为 `__authorization_outcome` 下的 `tool_call_id`。
  `__` 前缀是服务端保留约定：Gateway `build_run_config` 剥掉调用方伪造的同名键。消费方 `pop_authorization_outcome`
  取走即删，双方只共享这一个契约模块；内部 store 上限 500 条（无观察者时防无限增长，FIFO 淘汰最旧）；
- **策略身份**：`policy_id/policy_version` 从 provider 属性解析，缺省回退 `name/version`；outcome 路径刻意不调
  `release_policy_parameters()`（那还会算 provider 参数，浪费）。

### 与邻居的关系

授权（10）在外部 guardrail（11）之外（授权先 deny、外部调用不发生）；两者都在 SandboxAudit（12）之外
（授权不过，命令审计看不到这条命令）；两者都在 ToolReceipt（9）**之内**——deny 短路产生的 `status="error"`
ToolMessage **没有** `deerflow_tool_meta`（GuardrailMiddleware 不调 `normalize_tool_result`），但它穿过第 9 位时
被最外层 ToolReceipt 兜住：`make_tool_receipt` 回退 `message.status="error"`，账本不缺这条记录
（这正是 receipt 必须最外层的原因之一）。

### 设计权衡

- **fail-closed 是默认**：provider 挂了宁可拦死（`oap.evaluator_error`）也不放行；`fail_open` 是显式选择，
  放行同时**留审计**，让"评估器出错时发生了什么"可查；
- **同一 provider 实例贯穿 Layer1/Layer2**：装配期过滤 catalog 的 provider 与执行期检查的是同一个对象，
  策略不在两层间漂移；主体验证也共用，`default_role` 语义一致；
- **授权身份布线独立于开关**：无论 `authorization.enabled` 与否，Principal 构造 / `is_internal` 溯源 /
  `authz_attributes` 拷贝照常进行（Gateway 剥客户端伪造、`build_principal_from_context` 是唯一共享构建器）；
- **单条 reason + 固定引导**：拒绝消息不把全部策略细节倒给模型——给一条可行动的原因，避免策略细节成为新注入面。

### 源码阅读指引

`guardrails/middleware.py`（`GuardrailMiddleware` 全部决策/审计/outcome 逻辑）；`authz/adapter.py`
（`GuardrailAuthorizationAdapter`：`_infrastructure_decision`/`_to_authz`/`_to_guardrail`/`evaluate`/`aevaluate`）；
`authz/outcome.py`（契约与 `__authorization_outcome` 键）；`authz/principal.py`（`build_principal_from_context`）；
`guardrails/provider.py`（Protocol 与数据结构）；设计文档 `docs/GUARDRAILS.md` 与
`docs/plans/2026-07-10-pluggable-authorization-rfc.md`。
---

## 3. SandboxAuditMiddleware（第 12 位）

### 它解决什么问题

沙箱负责**隔离**（进程在容器/VM 里、文件受限），但隔离 ≠ 安全：模型（或被提示注入诱导的模型）仍可能让 bash
执行危险动作——递归删根/家目录、`dd if=` 覆写磁盘、`mkfs` 格式化、读 `/etc/shadow`、覆写 `/usr/bin` 与 shell
启动文件、`base64 -d | sh`、`/dev/tcp` 内联网络（绕过工具 allowlist）、`LD_PRELOAD` 劫持、fork bomb……以及最阴险的
一类：**命令替换执行远端内容**——`$(curl url)` 把下载来的字节当命令执行。本中间件在沙箱里再放一道**行为守门人**：
命令分级 block/warn/pass，并把每次 bash 调用落结构化审计日志。

### 钩子与执行时机

`wrap_tool_call` / `awrap_tool_call`，但**只对 `bash` 工具生效**，其余工具直接 `handler(request)` 透传。
位于授权门（10/11）之内（先过身份授权再看命令内容），位于 ToolReceipt（9）之内（短路/重建结果由外层兜底记账）。

### 命令位 vs 值位（本中间件的灵魂）

命令替换 `$(...)` / `` `...` `` / `$(<...)` 本身不可怕——`x=$(curl url)` 只是把输出**捕获进变量**（值位）。
可怕的是替换结果**成为将要执行的命令**（命令位）。判定依据是**位置**而非 `$(` 是否存在（issue #4611 之前的宽松
规则连日常输出捕获都误杀）：

```
$(curl url)           命令位 → 下载内容被执行            → block
x=$(curl url)         值位   → 输出被捕获进变量           → pass
echo $(curl url)      值位   → 输出当参数传给 echo        → pass
FOO=1 $(curl url)     ⚠️ 前缀赋值+空格 → 仍是命令位       → block（赋值分支要求赋值与替换间有空白；x=… 无空白 → 值位）
env/nohup/exec/sudo/doas/time/command/builtin $(curl)     → 命令位（exec 包装器后仍是命令位置）
echo hi\n$(curl url)  未加引号的换行 ≡ ';' → 第二条是命令位 → block（否则逃过锚定规则）
```

实现是**两条锚定正则**（`_HIGH_RISK_COMMAND_POSITION_PATTERNS`，`^` 锚在**单个子命令**上、绝不对整串匹配）：

```python
_COMMAND_POSITION_PREFIX = r"(?:(?:env|command|builtin|exec|nohup|time|sudo|doas)\s+|\w+=\S*\s+){0,8}"
# ^前缀后跟 $( risky-exe 或 ` risky-exe；risky-exe ∈ curl|wget|bash|sh|python|ruby|perl|base64（\b 词边界）
```

而三类**位置盲区**（替换出现在哪都会被执行，因为输入在任何位置都是执行上下文）必须做**整命令匹配**（Pass 1）：

- `eval` / `source` 的参数：`eval $(curl url)`；
- 解释器的 **code-string 旗标**：`bash -c`、`python -c`、`perl/ruby/node -e`、`perl/node -p`、`php -r`——旗标收到的
  内容会被执行，`xargs sh -c "$(curl url)"` 这类"藏在参数里的执行"也在其列；
- here-string `<<<`（经 stdin 送到同一位置）。

三者共享**同一个替换 opener** `_RISKY_SUBSTITUTION`——它同时覆盖 `$(…`、`<(…` 与反引号三种拼写（共享 opener
保证反引号拼写 `` eval `curl u` `` 不会从只写 `$(` 的规则下漏掉）。

### 内部实现逻辑

流水线：**输入消毒 → Pass 1 整串高危扫描 → Pass 2 拆分逐子命令判级 → 审计 → 路由**。

```python
① _validate_input：空命令 / 长度 > 10_000（远超合法用途、远小于 ARG_MAX，几乎必是注入载荷）/
   含 \x00 空字节 → block（拒绝原因入审计，命令截 200 字符落日志）
② Pass 1：整串（空白归一化后）跑 _HIGH_RISK_PATTERNS——捕获跨语句结构攻击：
   `:(){ :|:& };:`（fork bomb）、`while true; do bash & done`、`| (ba)?sh`、`base64 .* -d |`、
   `> /usr/bin|/bin|/sbin`、`> ~/.bashrc`、`/proc/*/environ`、`LD_PRELOAD|LD_LIBRARY_PATH =`、
   `/dev/tcp/`、rm -rf 根/家、dd if=、mkfs、cat /etc/shadow… → 命中即 block
③ Pass 2：_split_compound_command(command, split_pipes=True) 拆子命令，逐个 _classify_single_command
   （命令位锚定正则在这里才 match），最严重判决胜出（block 短路）
④ 审计：logger.info("[SandboxAudit] {json:{timestamp, thread_id, command, verdict}}") —— 每次都写
⑤ 路由：block → error ToolMessage（不进 handler）；warn → 执行后重建结果附加 ⚠️ 警告；pass → 执行
```

`_classify_single_command` 对单个子命令：高危锚定规则 → **shlex 重排 token 后复查**（消除引号差异：
`"rm" -rf /` 重排成 `rm -rf /` 命中；`shlex.split` 抛 `ValueError`——heredoc 等多行形态——则回退原始扫描）→
中危规则（`chmod 777`/`pip install`/`apt install`/`sudo|su`/`PATH=`——放行但警告，让模型知情）。

**`_split_compound_command` 是启发式 shell 扫描器，不是 shell parser**，几条关键纪律：

- 引号感知扫描原始串：`safe;rm -rf /`、`rm -rf /&&echo ok` 这类无空格粘连运算符也能拆开；引号内运算符忽略；
- **未闭合引号 / 悬挂转义 → 整串原样返回**（fail-closed：宁可整串分类，也不静默丢片段）；
- 未加引号的**换行等价于 `;`** 拆分——否则 `echo hi\n$(curl url)` 逃过锚定规则；
- **heredoc body 是数据**：头部（`<<EOF`/`<<-EOF`/`<<'EOF'`/`<<"EOF"`/`<<\EOF`）被记录，body 从换行处**逐字消费**
  到分隔符行——body 里一行 `$(curl url)` 不会提升成命令位；`<<<` 用 lookahead+lookbehind 双重排除（它无 body，
  否则 `<<< "text"` 会被读成 heredoc+分隔符）；`$(( ))`/`(( ))` 里的 `<<` 是位移不是重定向（追踪算术深度，避免
  幽灵 heredoc 吞掉命令剩余部分）；
- 默认**不按 `|` 拆**（管道是一条逻辑命令；跨管道规则 `| sh`、`base64 -d | …` 由 Pass 1 整串扫描兜住），但命令位
  检测需要 `split_pipes=True`——管道后的第一个词是新的命令位置（`echo hi | $(curl url)`）。

### 与邻居的关系

- **对 ToolReceipt（9）**：block 短路消息与 warn 的**结果重建**都不会带内层 `deerflow_tool_meta`——
  `_append_warn_to_result` 用 content/tool_call_id/name/status 构造**全新 ToolMessage**，丢弃了 ToolErrorHandling（15）
  刚 stamp 的 additional_kwargs。正因为 receipt 最外，这类结果仍记账（status 回退 `message.status`）；账本只在
  "短路发生在 receipt 之外"时才会漏——而那是被 ordering 约束禁止的；
- **对授权门（10/11）**：审计在授权之后，先有身份结论再看命令内容，职责不重叠；
- **对 ToolErrorHandling（15）**：block 是"决策"不是"故障"，由审计自己产出消息；warn 后真正执行的命令若抛异常，
  仍由内层 15 转成结构化错误。

### 设计权衡

- **边界诚实标注：纵深防御与审计，不是安全边界**。真正的隔离边界是沙箱本身；SandboxAudit 只提高"危险命令被执行"
  的门槛并留下结构化记录，模型换一种未收录写法时沙箱仍是最后防线。"启发式而非 shell parser"意味着可能误放
  （未收录模式）也可能误拦（fail-closed 方向），部署时不能把它当完整安全方案；
- **block/warn/pass 三级而非二值**：中危动作（包安装、sudo、chmod 777、PATH 修改）在许多任务里是正当需求，
  一刀切毁可用性；warn 把判断权交还模型并在**结果里留痕**（复盘可见它被警告过）；
- **两遍扫描缺一不可**：Pass 1 保结构攻击上下文（拆开 `;` 会毁掉 fork bomb 模式），Pass 2 抓隐蔽复合命令
  （`safe;rm -rf /` 拆开后第二段才现形）；
- 命令过长/空字节等**输入消毒拒绝**直接 block 并写审计——这些形态几乎不可能是合法 bash。

### 源码阅读指引

`agents/middlewares/sandbox_audit_middleware.py`：正则常量区（`_RISKY_SUBSTITUTION_EXECUTABLES`/`_RISKY_SUBSTITUTION`/
`_CODE_STRING_INTERPRETERS`/`_LEADING_FLAGS`/`_HIGH_RISK_PATTERNS`/`_COMMAND_POSITION_PREFIX`/
`_HIGH_RISK_COMMAND_POSITION_PATTERNS`/`_MEDIUM_RISK_PATTERNS`/`_HEREDOC_HEADER`）；纯函数区
（`_consume_heredoc_bodies`/`_split_compound_command`/`_matches_high_risk`/`_classify_single_command`/
`_classify_command`）；中间件类（`_write_audit`/`_append_warn_to_result`/`_validate_input`/`_pre_process`/
`wrap_tool_call`/`awrap_tool_call`）。
---

## 4. ToolErrorHandlingMiddleware（第 15 位，最内层）

### 它解决什么问题

工具 handler 是任意第三方代码（MCP server、社区工具、沙箱命令），会抛**任何**异常：文件不存在、权限不足、
网络断、SDK 不兼容……没有这层包裹，一个异常会**直接中止整条 run**。即使工具"正常返回"，失败信号也五花八门：
有的 `status="error"`，有的正文写 `Error: ...`，有的返回 JSON `{"error": ...}`，有的把 HTTP 错误页当成功页返回，
有的静默返回空。下游（ToolProgress 停滞检测、ToolReceipt 凭证、前端卡片、子代理失败渲染）**不能靠嗅探文本**判断
结果好坏——它们需要统一的结构化信号。

### 钩子与执行时机

`wrap_tool_call` / `awrap_tool_call`，**链上最后一个中间件**（最内层，紧贴 ToolNode）。它看到的是**最原始**的
handler 返回（还没被 ToolProgress/RBW 重写）；它 stamp 的 `deerflow_tool_meta` 先被 14 位 ToolProgress 读到
（构建期由 ordering 约束保证），再被 9 位 ToolReceipt 用来生成凭证 status。

### 内部实现逻辑

**① 异常路径**：

```python
try:
    result = handler(request)
except GraphBubbleUp:
    raise                       # 控制流（interrupt/pause/resume）永远透传
except Exception as exc:
    logger.exception(...)       # 带工具名 + tool_call_id 的完整堆栈
    return _build_error_message(request, exc)   # 见下

detail = str(exc).strip() 或类名；>500 字符截到 497+"..."
content = f"Error: Tool '{tool_name}' failed with {ExcName}: {detail}. "
          f"{_RECOVERY_HINT}"   # _RECOVERY_HINT = "Continue with available context, or choose an alternative tool."
ToolMessage(content=content, tool_call_id=..., name=tool_name, status="error")
```

若工具是 `task`（子代理委派），`_stamp_task_exception_status` 会把正文格式化成 `format_subagent_result_message("failed", error)`
的规范形态（补标点、附 `make_subagent_additional_kwargs("failed", …)` 结构化元数据）——**异常路径与正常路径产物结构等价**，
前端卡片能正确渲染 failed。最后 `stamp_exception_meta(message, structured_error)`：**总是覆盖**已有 meta——
异常派生的分类比工具自身返回时 stamp 的分类更权威。

**② 正常路径**：`normalize_tool_result(...)` 给结果 stamp meta（已有则保留；`Command.update.messages` 按
`tool_call_id` 匹配要 stamp 的那条）；顺带 `_stamp_skill_read_metadata`：命中 skill 文件读取工具且结果成功时，校验
路径落在 skills 根、basename 是 `SKILL.md`、内容非 `Error:` 开头，才 stamp `skill_context_entry`（含 YAML description），
供压缩后重建 skill 上下文——error 结果不 stamp。

**③ 统一元数据形态**（`agents/middlewares/tool_result_meta.py`）：

```json
additional_kwargs["deerflow_tool_meta"] = {
  "status": "success" | "error" | "partial_success",
  "error_type": "auth" | "rate_limited" | "transient" | "config" | "permission"
               | "no_results" | "not_found" | "internal" | "unknown",
  "recoverable_by_model": true | false,        # 模型换个方式能否自救
  "recommended_next_action": "continue" | "rewrite_query" | "try_alternative" | "summarize" | "stop",
  "source": "exception" | "tool_return" | "content_analysis" | "progress_middleware"
}
```

**④ 文本 → 分类**（`normalize_tool_message`，仅当 meta 缺失时执行，判定优先级从上到下）：

1. `additional_kwargs.subagent_status` ∈ {failed, cancelled, timed_out, polling_timed_out} → 用 `subagent_error`
   （或正文）分类为 error——结构化字段优先于正文嗅探，正文不以 `Error:` 开头的 subagent 失败不再被误标 success；
2. `message.status == "error"` 且正文非 `Error:` 开头 → 先试 JSON `error` 字段取**字段值**分类（避免别的字段恰好含
   关键词如 `{"user_id": 401}` 误中 401 规则）；无 error 键的合法 JSON 对象不按裸文本分类；非 JSON 才整串分类；
3. 正文 `Error:` 前缀 → 剥前缀后分类；
4. 正文整体是 JSON 且带 error 字段 → 按字段值分类；`"none"/"null"/"false"/""` 等**语义零值**不算错误；
5. `web_fetch` 结果首行标题形如 HTTP 错误状态行（`404 Not Found`、IIS `404 - File or directory not found.`、
   Cloudflare……）→ 按 `_ERROR_SHELL_PHRASES` 映射分类（source=content_analysis）——fetch 传输层成功但拿到错误页，
   不能算 success（issue #4273）；标题按等值匹配而非子串（"404 Ways to Cook Rice" 幸存为 success）；
6. 命中部分结果标记（"partial results"/"no results found"/"truncated"…）→ `partial_success` + rewrite_query；
7. 否则 → `success`。

关键词规则表（`_ERROR_RULES`）给出 `(error_type, recoverable_by_model, recommended_next_action)` 三元组：
auth→(false, stop)、rate_limited→(false, summarize)、transient→(false, try_alternative)、config→(false, stop)、
permission→(true, try_alternative)、no_results→(true, rewrite_query)、not_found→(true, rewrite_query)、
internal→(false, stop)；未知→(true, try_alternative)。**纯数字关键词（401/403/404/500…）用 `\b` 词边界正则**
——防止 "took 500ms" 里的 500 误判成 internal error。

### 与邻居的关系

- **向上游供应 meta**：ToolProgress（14）在 `_update_state_from_result` 读它判停滞；ToolReceipt（9）的
  `make_tool_receipt` 用 `meta.status` 作凭证状态（缺失才回退 `message.status`）；
- **对短路消息**：Guardrail deny / SandboxAudit block 不经过本中间件，故不自 stamp meta——短路者是"决策者"，
  语义由 `message.status` 表达，外层的 receipt 用回退状态兜底（SandboxAudit 的 warn 重建甚至会丢弃本中间件已打的
  meta，这是有意的分工而非 bug）；
- **构建期顺序被硬校验**：ToolProgress 与 ToolReceipt 都必须在它外层（都要读它打的 meta）；ordering.py 违反即
  装配期 `RuntimeError`（fail-fast，不悄悄错序）。

### 设计权衡

- **"异常覆盖正常"的不对称权威**：异常路径 `stamp_exception_meta` 无条件覆盖已有 meta（异常是更强的信号），
  正常路径 `normalize_tool_message` 保留已有 meta（工具自己的 stamp 可信）；
- **分类宁可宽容不可误杀**：`no_results/not_found/permission` 都 `recoverable_by_model=True`，让 ToolProgress
  只提示不封禁；只有 auth/config/internal 这类"重试无望"才 `stop`；
- 500 字符截断 + 固定恢复引导：错误详情是模型上下文而非日志——完整堆栈留在 `logger.exception`。

### 源码阅读指引

`agents/middlewares/tool_error_handling_middleware.py`（中间件类 + `_build_runtime_middlewares` 装配本体）；
`agents/middlewares/tool_result_meta.py`（`ToolResultMeta`/`_ERROR_RULES`/`_NUMERIC_KW_RE`/
`_SEMANTIC_ZERO_ERROR_STRINGS`/`_extract_json_error_text`/`_classify_error_shell`/`stamp_exception_meta`/
`normalize_tool_message`/`normalize_tool_result`）；`agents/middlewares/skill_context.py`
（`build_skill_entry_metadata_from_read`）；`subagents/status_contract.py`。
---

## 5. ToolReceiptMiddleware（第 9 位，最外层 wrap_tool_call）

### 它解决什么问题

AI 报告的**引用必须落到真实执行过的证据上**（citation verification 的地基），而证据需要一条零 LLM 的 provenance 层：
每次工具调用生成**确定性凭证**（工具名、状态、参数/输出哈希、字节数、时间戳），并把凭证账本渲染给模型，让它用
`[r3 write_file]` 这样的 id 引用"我做过的事"。没有这层时：

- 模型声称"我创建了 report.md"，却没有任何可机器核对的执行记录；
- Guardrail deny / SandboxAudit block / RBW 拦截这类**短路 ToolMessage** 不经过正常执行路径，内层 receipt 会
  **静默漏记**它们——账本空洞，之后的引用验证就会撒谎；
- 工具可以在自己的结果里伪造 additional_kwargs 里的"证据"。

### 钩子与执行时机

- **`wrap_tool_call` / `awrap_tool_call`：shared base 里最外层**（下标 9，先于 10~15 全部短路者）。注意它必须
  **同时**包住 Guardrail/SandboxAudit/RBW/ToolProgress 四个可能短路或重建结果的中间件（ordering.py 逐条声明强校验）；
- **`wrap_model_call` / `awrap_model_call`**：每次模型调用前从在途消息提取凭证、渲染成隐藏 HumanMessage 注入
  （从不写回 state——与 DurableContextMiddleware 同构的派生数据）；模型响应返回后把"本次渲染的凭证子集"快照
  写回 AIMessage（`TOOL_RECEIPT_LEDGER_KEY`）。

### 内部实现逻辑

**① 打凭证**（`_stamp` → `_stamp_message` → `make_tool_receipt`）：

```python
kwargs[TOOL_RECEIPT_KEY] = {
  "tool_call_id": str(tool_call.id),       "tool_name": str(tool_call.name),
  "status": meta.status or message.status,    # deerflow_tool_meta 优先；短路消息回退 message.status
  "args_sha256":   sha256(json.dumps(args,  sort_keys=True, default=str))[:16],
  "output_sha256": sha256(content)[:16],      # 捕获于 stamping 时刻（消毒/截断改写之前）
  "output_bytes":  len(content.encode("utf-8")),
  "created_at":    datetime.now(UTC).isoformat(),
}
```

直接返回的 `ToolMessage` 与 `Command.update.messages` 两种结果形态都覆盖——后者按 `tool_call_id` 匹配要打的那条
（委派的 `task`、`present_file`、`view_image`、`tool_search` 结果多以 Command 带出）。**凭证键是 runtime-owned**：
总是覆盖、绝不保留已存在值——否则工具可伪造"证据"并被当成 runtime 凭证渲染。打凭证失败只记 warning（绝不阻塞
工具执行），但系统性失败必须可见——账本不全，引用就撒谎。

**② 账本渲染**：

```python
receipts = extract_tool_receipts(messages)   # 只收结构合法 receipt（is_valid_receipt），按消息序编显示 id r1..rN
ledger, rendered = render_tool_receipts_with_snapshot(receipts, max_chars=2000)
if ledger: 注入隐藏 HumanMessage(insert_after_leading_system_messages, hide_from_ui=True)
```

渲染文本以三行头开场：标题、引用指令（"Cite receipt ids (e.g. `[r1 write_file]`) in your final report …"）、以及
一句**反自动偏差声明**（"Execution evidence only — receipts record that a call happened and its status; they do
not validate claim correctness"）——账本永远声明自己的证据边界，模型不会把"执行过"读成"做对了"。
**2000 字符预算超限时保留最新凭证**（按时间顺序、id 不变），并插入省略标记：

```
## Tool receipts (execution record)
…（标题 + 引用指令 + 证据边界声明，见上文 ②）
- ... older receipts omitted (context budget)
- [r27] write_file status=success args_sha256=… output_sha256=… bytes=…
- [r28] task       status=success args_sha256=… output_sha256=… bytes=…
```

渲染同时返回**保留子集**，`_stamp_citing_ledger` 随后把它**原样**写回本次响应 AIMessage 的
`additional_kwargs[TOOL_RECEIPT_LEDGER_KEY]`——运行时所有、总是覆盖，provider 输出无法伪造"它对照的那份账本"。

**③ render_mode 控制 token 成本**：

- `always`：每次模型调用都渲染（**subagent 链**——引用在子代理上下文产生，无账本则无法引用、验证层失效）；
- `delegation_only`（**lead 链默认**，可配 `verification.receipts_render_mode`）：仅当消息流出现**已完成子代理结果**
  （ToolMessage 带 `subagent_status`）才渲染。作用域限定**当前 turn**（只数最新真实用户消息之后的消息，无真实
  用户消息则全流在范围内），否则一次委派会让账本在后续每个普通回合常驻。

**④ 消费侧配套**（`tool_receipt.py`）：`extract_citing_turn_receipts` 从最近一个带快照的 AIMessage 取子集并**严格校验**
——id 必须 `r`+正整数、连续递增、可从任意起点开始（`r24`–`r30` 合法，不要求从 r1 起），供子代理终态引用校验解析
"citing turn 当时可见的证据"（summarization 压缩重编号后依然成立）；`parse_citations` 解析 `[rN]`/`[rN tool]`，
数字先限长（>10 位视为畸形输入跳过，防 int() 转换炸弹），按 `(id, anchor)` 精确对去重。

### 与邻居的关系（为什么它必须是最外层）

- **对短路者（10~13）**：Guardrail deny、SandboxAudit block/warn 重建、RBW blocked、ToolProgress BLOCK 都可能
  **不调用内层 handler** 就自行产出 ToolMessage。receipt 注册在它们内层时这些结果根本到不了 receipt——账本漏记；
  注册在外层则每条短路结果都被 `_stamp` 兜住（无 meta 回退 `message.status`），账本无空洞——这是 AGENTS.md 与
  ordering.py 反复强调的第一顺序契约；
- **对 ToolErrorHandling（15）**：正常结果在内层返回路径上已 stamp meta，打凭证直接取 `meta.status`——凭证与停滞
  检测读的是**同一个**结构化信号；
- **对 LLMErrorHandling（8）**：8 号是它模型调用侧的外层，重试会重新走 ledger 渲染（派生、幂等，无副作用）。

### 设计权衡

- **凭证从消息流派生而非独立存储**：渲染与收割永远一致，无第二份状态要同步；
- **receipt 是执行真相的"新鲜度戳"而非可复算指纹**：内容在打凭证后被外层消毒/截断改写、压缩后只剩净化文本——
  `output_sha256` 校验的是"当时捕获的字节"，不是对持久化消息重算；
- **显示 id 位置化 + 快照**：r1..rN 按 append-only 消息列表分配（turn 内稳定），压缩重编号后旧引用可能漂移——
  解法不是让 id 永恒，而是**每轮引用都对照该轮快照**（Layer 2 校验）；
- **token 税换证据**：`delegation_only` 把常驻开销限制在真正产生引用的回合，`always` 留给引用主产地的子代理链。

### 源码阅读指引

`agents/middlewares/tool_receipt_middleware.py`（`_stamp`/`_should_render`/`_inject`/`_prepare_model_call`/
`_stamp_citing_ledger`）；`agents/middlewares/tool_receipt.py`（`make_tool_receipt`/`extract_tool_receipts`/
`extract_citing_turn_receipts`/`is_valid_receipt`/`render_tool_receipts_with_snapshot`/`CITATION_RE`/`format_citation`/
`parse_citations`）；`agents/middlewares/receipt_verification.py`（引用校验）；`extensions/ordering.py`（顺序契约）。
---

## 6. 设计权衡速查表
| 中间件 | 链位 | 核心权衡 | 边界声明 |
|---|---|---|---|
| LLMErrorHandling | 8 | 去相关抖动 vs 同步再峰；熔断只记真故障 | 让 run 以可解释失败收尾，不吞控制流 |
| GuardrailMiddleware | 10/11 | fail-closed（默认）vs fail-open；授权先于外部 guardrail | 只拦"执行前"；工具装配过滤归 Layer 1 |
| SandboxAudit | 12 | block/warn/pass 三级 vs 二值；命令位/值位按位置判 | **纵深防御与审计，不是安全边界**（沙箱才是） |
| ToolErrorHandling | 15（最内） | 异常分类覆盖正常；关键词表宁宽勿杀 | 异常 ≠ run 中止，统一成结构化 meta |
| ToolReceipt | 9（最外） | 账本 token 税 vs 可核验证据；派生不持久化 | 记录"发生过+状态"，不背书"做对了" |

## 7. 阅读顺序与延伸

1. 先读 `agents/middlewares/AGENTS.md` "Middleware Chain" 全文（本组对应 8~13 条目）；
2. 装配实证：`agents/middlewares/tool_error_handling_middleware.py::_build_runtime_middlewares` 的 tail 追加段（本文 §0 下标即出自此处）；
3. 顺序契约：`extensions/ordering.py` + `extensions/stack.py::compose_with_extensions`（扩展注入后校验、指认责任方）；
4. 机制背景 issue/设计：SandboxAudit `#4611`、LLM 熔断 `#4290`、错误页 `#4273`、授权 RFC `docs/plans/2026-07-10-pluggable-authorization-rfc.md`；
5. 下游消费者：`tool_progress_middleware.py`（读 meta 的停滞状态机）、`receipt_verification.py`（引用校验）、`runtime/runs/worker.py`（读 `deerflow_error_fallback` 判失败）。
