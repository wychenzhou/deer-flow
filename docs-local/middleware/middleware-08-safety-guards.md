# 安全守卫中间件（处理逻辑）

本文件解析六个核心"安全守卫类"中间件，构成 Lead Agent（及子 Agent）的运行时安全护栏：限制子 Agent 调用、打断工具调用死循环、约束 token 预算、保证终态响应可见、抑制被安全过滤截断的工具调用、以及人机交互式澄清。

| 中间件 | 职责 |
|--------|------|
| `SubagentLimitMiddleware` | 子 Agent 委派限额 |
| `LoopDetectionMiddleware` | 工具调用死循环守卫 |
| `TokenBudgetMiddleware` | token 预算守卫 |
| `TerminalResponseMiddleware` | 工具执行后空响应兜底 |
| `SafetyFinishReasonMiddleware` | provider 安全截断处理 |
| `ModelLengthFinishReasonMiddleware` | 记录 provider 长度截断为 run stop 原因 |
| `ClarificationMiddleware` | 人机澄清 |

辅助模块：`safety_termination_detectors.py`（provider 安全终止检测器）、`_bounded_dict.py`（有界字典）、`tool_call_metadata.py`（工具调用克隆/剥离助手）。

---

## 1. SubagentLimitMiddleware

**职责**：在一次响应或一个 run 中强制截断超出配额的 `task`（子 Agent 委派）调用，既防一次响应并发过多，也防单 run 反复分批委派绕过并发限制。

**处理逻辑**：

- 双维检测：单条 AIMessage 里 `task` 调用数、以及当前 run 在委派账本中的累计委派数。
- 并发解析：一次 min(per-run 请求, 启动冻结的 `subagent_runtime.max_running`, 安全上界 1–64)。
- 取"并发上限"与"剩余总配额"较小者作为允许保留的 task 数，超过的直接截断丢弃；用克隆助手重建 AIMessage 并保持**相同 id**——使 LangGraph 视为替换而非追加。
- **`max_total_per_run`** 数 durable delegation ledger 里**带当前 run_id** 的条目——反复 planning checkpoint 无法无限派发合法批量（默认 6，clamp 1–50）。
- **失败模式**：超总量时写 `stop_reason=subagent_limit_capped` + 可见告警 `[SUBAGENT LIMIT REACHED]`（提示用已有结果/直接执行简单工作/总结而非再委派）；**fail-restrictive**——run_id 缺失则按该线程全部历史计数（宁可更紧）。
- 未超限则不干预。

---

## 2. LoopDetectionMiddleware

**职责**：检测工具调用死循环，打破重复调用。

**处理逻辑**：

- **双层检测**：
  - **Layer1 哈希**（相同调用集）：对 tool_calls 做顺序无关哈希（name+稳定 key 排序），默认 `warn_threshold=3` 警告、`hard_limit=5` 硬停。
  - **Layer2 频率**（同工具类型换参）：单工具名滑窗频率计数，默认 30 警告、50 硬停，可 per-tool `tool_freq_overrides` 覆写。
- **哈希工具感知归一**：read_file 按 200 行分桶（防翻页误判）；write_file/str_replace 用**全参数哈希**（防迭代写入漏判）；其他工具抽关键字段（path/url/query/command/pattern/glob/cmd）。
- **警告注入**：把警告入队延迟到下一次 `wrap_model_call` 作为隐藏 HumanMessage 追加——不能在 after_model 直接插（否则破坏 assistant tool_calls 与 ToolMessage 配对、校验器拒绝）。警告一次后不再重复告警；`warn` 后若计数低于阈值则放行再警告。
- **硬停**：剥离全部 tool_calls、改写 `finish_reason=stop`、追加硬停文本，让 run 自然终止产出终答 **而不抛异常**；写 `stop_reason=loop_capped` 并保留到 run 返回后供子 Agent 执行器读取。
- **滑窗大小必须 ≥ 任何 hard_limit**（否则硬停分支不可达——若窗口 20 < hard 50，紧 burst 永远达不到硬停）；用 Counter 镜像队列使频次 O(1)。
- 频率窗口按"最大生效 hard"定尺寸（全局 + 每个 per-tool 覆写），warn 阈值不参与（warn ≤ hard 由合理配置保证，warn > hard 会先硬停，无害）。
- **跨度**：跨 run 保留 history（调用模式时间不变）——与新工具进度中间件的"每 run 清空"故意相反。
- **审计**：持久化 `middleware:loop_detection` 事件（warn 首现与硬停），带 `is_subagent`/`agent_id`，**不含工具参数/上下文/消息/参数哈希**。

---

## 3. TokenBudgetMiddleware

**职责**：一个 run 内 token 消耗管控（输入/输出分别管控）。

**处理逻辑**：

- 累计每次模型调用的 `usage_metadata`，用**差值追踪**：比较当前值与已记录值的增量，只把新增 token 累加；正确处理子代理 token 被回溯合并时的归因（合并前为 0、合并后 diff 为 0，不重复计算）。
- 算 total/input/output 三维分数并取最高者。
- **警告**：任一维度超 `warn_threshold`（默认 0.8）且本 run 未告警过 → 入队一次预算警告延迟注入（每 run 只发一次）。
- **硬停**：任一维度超 `hard_stop_threshold`（1.0）→ 剥离 tool_calls、改写 `finish_reason=stop`、追加指明哪个维度超限的文本；不 raise（让 run 自然完成终答），写 `stop_reason=token_capped`。
- **per-run 隔离**：`before_agent` 把历史所有先前 run 的 AIMessage 标为已见，本 run 从 0 预算；run_id 按**存在性**判断处理嵌入式子代理的 run_id=None；stop_reason 用有界字典防泄漏。

---

## 4. TerminalResponseMiddleware

**职责**：工具执行完成后模型返回空 AIMessage（无可见文本、无 tool_calls）时兜底，防"静默成功"。

**处理逻辑**：

- **检测**（同时满足）：最后一条是 AIMessage；无可见内容；无 tool_call 意图或错误；当前 turn 存在工具结果（只看真实用户消息之后的 ToolMessage，跳过隐藏消息；调度/内部调用无真实用户消息时不干预）。
- **首次空响应**：删除这条空消息并 `jump_to model` 重新调度；下一次模型调用注入隐藏恢复提示（指导模型基于已有结果产出简洁最终响应、不要再调工具）；重试按 run 一次性预算，避免无限循环。
- **结果**：重试后仍有可见文本或 tool_call 则正常继续；重试后仍空 → 用面向用户的错误 fallback 文本替换消息，并打上失败完成标记，让 run 以明确错误而非"静默成功"结束。

---

## 5. SafetyFinishReasonMiddleware

**职责**：provider 因安全原因中途截断响应但仍返回半截 tool_calls 时，抑制工具执行。

**处理逻辑**：

- **检测器顺序调用**：OpenAI 兼容 `content_filter`；Anthropic `refusal`（用 stop_reason）；Gemini `SAFETY`/`BLOCKLIST`/`PROHIBITED_CONTENT`/`SPII`/`RECITATION`/`IMAGE_*`（大写枚举）。**故意排除** `STOP`/`MAX_TOKENS`（长度非安全）、`MALFORMED_FUNCTION_CALL`（协议错误另归类）。读取 `response_metadata` 与 `additional_kwargs` 两个来源（adapter 放置不一致），只接受字符串值防 Pydantic enum 崩溃。
- **只在这些信号携带 tool_calls 时才干预**，否则放行让部分文本自然到达用户；检测器异常被捕获记不匹配，**绝不中断 run**。
- **处理**：用克隆助手剥离全部结构化 tool_calls 与 raw payload，但**保留原 finish_reason**（让下游继续看到真实 provider 原因），追加面向用户解释。
- **结果**：写 `stop_reason=safety_capped`；发 SSE 自定义事件（best-effort）；持久化审计事件——**只记录被抑制的工具名/数量/id，不记录参数**（那正是被过滤的内容）。
- **注册在 LoopDetection 之后**：Safety 先看到原始响应先清理，Loop 再基于清理后的消息计数——两个共享 `clone_ai_message_with_tool_calls` 但触发不同。
- **边界**：`from_config` 拒绝空 detectors 列表（否则静默禁用但保留在链中），用 `enabled:false` 完全禁用。

---

## 6. ClarificationMiddleware

**职责**：模型调用 `ask_clarification` 时拦截，暂停等用户回复。**必须最后注册**（因它在 on_tool_end 之前短路工具执行）。

**处理逻辑**：

- `wrap_tool_call` 检查工具名，非此工具直接透传；检查是否处于非交互渠道。
- 拦截后用**确定性 ID**（基于 tool_call_id 或消息哈希）构造 ToolMessage + 结构化 `human_input` payload（含问题、选项、输入模式），通过 `Command(goto=END)` 中断执行流，checkpoint 持久化等待用户回复；重试同一澄清用相同 ID，LangGraph 视为替换而非追加。
- `after_model` 丢掉同批 sibling 调用（防用户未回答时它们先跑）；`disable_clarification` 时保留 sibling。
- **结果**：交互渠道 → 图停在 END 等待回复，前端渲染 Human Input Card（content 是纯文本回退、artifact 是结构化 payload）；非交互渠道（设置 disable_clarification）→ 返回普通 ToolMessage 指示 agent 自主判断继续，避免 run 死锁；scheduled 任务更彻底——**工具组装阶段就排除该工具**。

---

## 7. ModelLengthFinishReasonMiddleware

**职责**：把 provider 因输出预算耗尽而截断的响应（`finish_reason='length'` / `stop_reason=max_tokens`）记录为 run 的 `stop_reason=model_length_capped`——保留可见内容供审计，但不把"被截断"当成干净完成。

**处理逻辑**：

- **边界刻意窄**（#4271）：
  - 只在**最后一条 AIMessage** 被 provider 长度信号截断且**带可见内容**时才标记 run 级原因；
  - **从不改写 assistant 内容**，也**不把 XML 形文本重新解析成工具调用**；
  - 忽略仍带工具调用意图、畸形工具调用元数据、或无可见内容的响应——只有"终态 assistant 响应 + 可见内容"才可能被标记。
- **检测**：`after_model`/`aafter_model` 先取最后一条 AIMessage；`_has_tool_call_intent_or_error` 检查 `tool_calls`/`invalid_tool_calls`/raw `tool_calls`/`function_call`，有则返回 None（放行给正常工具处理路径决策）；`_has_visible_content` 检查有非空文本；然后顺序调用长度检测器（`ModelLengthTerminationDetector`），命中即终止。
- **检测器**：来自 `model_length_termination_detectors.py` 的默认集合，匹配 `finish_reason=length`/`MAX_TOKENS`、或 `stop_reason=max_tokens`；并读取 `response_metadata` 与 `additional_kwargs` 两个来源。
- **打标**：命中则 `runtime.context["stop_reason"] = "model_length_capped"`——已有更早的 cap 原因（跨隐藏延续 turn）则保留原值不覆盖；附带 thread_id/run_id/message_id/detector/reason_field/reason_value 的 INFO 日志。
- **与 SafetyFinishReason 分工**：SafetyFinishReason 处理"安全截断"（content_filter/refusal/SAFETY），本中间件处理"长度截断"——两者都只记录 stop_reason，都不改写内容，都不抛异常。检测器异常被吞（记录不匹配），绝不中断 run。
- **与安全检查的区别**：`MAX_TOKENS` 被安全检测器**故意排除**（长度非安全，见 §5 SafetyFinishReason），由本中间件单独负责。
