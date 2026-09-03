# 错误处理与安全守卫中间件（处理逻辑）

本文件涵盖四个"容错与安全"中间件：`LLMErrorHandlingMiddleware`、`GuardrailMiddleware`、`SandboxAuditMiddleware`、`ToolErrorHandlingMiddleware`。它们共同构成 lead/subagent 运行链中的"失败恢复 + 工具调用守门人"子系统，让 run 在上游抖动、命令越权、工具异常等场景下仍能优雅结束。

阅读前建议先看 `backend/AGENTS.md` 的 "Middleware Chain" 章节了解位置。每节按"职责 → 处理逻辑 → 关键设计决策"展开。

---

## 1. LLMErrorHandlingMiddleware

**职责**：捕获 LLM 调用瞬时错误，按指数退避重试；熔断器保护系统；不可恢复错误包装成带 `deerflow_error_fallback` 标记的兜底消息，让图干净终止。

**处理逻辑**：

- 先分类错误 → 返回 `(retriable, reason)`：`quota`/`auth` **不可重试**；`burst_rate` 在通用 429 之前判；`transient`/`busy`/`generic` 可重试。
- `IndexError` 也当归 transient——因为上游可能返回 `200 OK` 但带空 `generations` 列表（Volces 端点），接着 `list index out of range`。
- **有效重试预算** = `min(配置上限, 分class覆盖, 分reason覆盖)`：`StreamChunkTimeoutError` 上限 2（全 3 次会堆 6–12 分钟死寂），`burst_rate` 上限 2。UI 消息用有效预算，绝不承诺不会发生的重试。
- **退避**：优先尊重服务端 `Retry-After`（支持多种写法与毫秒/秒/HTTP date 格式）；无则 AWS 式去相关抖动 `randint(base, min(cap, max(base, seed*3)))`，从 reason 特定 base 播种，让一起失败的集群散开而非同步再峰；burst_rate 换更长的 `burst_retry_base_delay_ms`(5s)。
- 同步 `time.sleep`/异步 `asyncio.sleep` 睡眠都经**进程级并发 limiter** 释放并发槽（`asyncio.Semaphore` 绑单 loop、无法覆盖 lead+subagent+sync，故用 threading 原语）。
- **熔断器** closed→open→half-open 状态机；open 时 fast-fail 不真实调用；half-open 放行一次探针，成功关、失败再开；burst-rate 失败**释放探测但不记 fail**（防 slope 限流自己熔断变成自造的故障，#4290）。
- **只有"可重试且用尽预算"的错误才推进熔断计数**；quota/auth 不推进（防"凭证错"被误判成"服务挂了"）。
- `GraphBubbleUp`（控制流暂停/恢复）永远优先透传，不能被错误处理吞掉，否则破坏中断/恢复。
- 重试事件经流式 writer 发前端（UI 显示"正在重试"），且事件发送本身 try/except 包裹、绝不影响主流程。
- **兜底消息**：按原因分支生成不同文案（quota→额度/计费；auth→鉴权；流中断类→建议拆分请求；普通繁忙→建议等待续聊；其他→原始错误文本）；带 `deerflow_error_fallback` 标记，是下游判断"子代理是否真失败"的唯一权威信号——带标记才映射为失败并触发失败事件。

---

## 2. GuardrailMiddleware

**职责**：在工具调用真正执行前做授权决策，拒绝的调用不触达 handler。

**处理逻辑**：

- 每次工具调用前交给可插拔 provider 授权；拒绝 → 返回 `status="error"` 的消息让 agent 改路，不执行。
- 从运行时上下文安全汇聚授权请求（工具名、参数、agent 身份、thread、用户信息、是否子代理、授权属性等）；字段缺失回退默认。
- provider 正常返回：按决策放行或拒绝；拒绝消息只展示第一条原因，末尾固定引导"选择替代方式"。
- provider 抛 `GraphBubbleUp` → 透传。
- provider 抛其他异常 → 按 `fail_closed`（默认拒绝）或 `fail-open`（放行并记为 provider 错误）降级，两种都构造"评估器错误"原因。
- 审计 best-effort：journal 缺失跳过，落盘失败只记 debug，绝不改变工具执行结果；拒绝理由每条截断到 500 字符。
- 内置 Allowlist 区分"未配置（全允许）"与"显式空列表（全拒绝）"，避免空列表被误当未配置导致 fail-open。
- provider 是 Protocol 而非基类，外部实现无需 import 内部代码即可接入。

---

## 3. SandboxAuditMiddleware

**职责**：专门针对 `bash` 工具做命令安全审计（输入消毒 + 正则 + shlex 双重分析，分级 block/warn/pass，写结构化审计日志）。

**处理逻辑**：

- 输入消毒先行：空命令、超长命令（>10000 字符）、含空字节的命令直接 block；消毒失败也写一条 block 审计。
- **高风险**（递归删根/家目录、磁盘覆写、格式化、读 shadow 密码、覆写系统文件与二进制、curl 管道 bash、base64 解码执行、进程环境泄露、链接器劫持、fork bomb 等）→ 直接拦截不执行，返回 error 消息并引导更安全方式。
- **中风险**（chmod 777、包安装、sudo/su、PATH 修改）→ 照常执行，但在结果末尾追加警告文本让模型知情。
- 复合命令按分号/`&&`/`||` 拆分逐个判断，取最严重判决；但若引号未闭合或存在悬挂转义，返回整串不拆分（fail-closed）。
- **两遍分类**：先整串扫高风险模式（捕获跨语句的 fork bomb），再拆分逐子命令判级（捕获 `safe;rm -rf /` 这类隐蔽复合命令）。
- 用 shlex 重排 token 消除引号差异（如 `"rm" -rf /`），解析失败降级到原始扫描。
- 只对 `bash` 工具生效，其他工具直接透传。

**边界**：明确"是纵深防御与审计，不是安全边界"（真正隔离靠沙箱）。

---

## 4. ToolErrorHandlingMiddleware

**职责**：最内层包裹 handler——正常结果统一打 `deerflow_tool_meta` 元数据；异常（除控制流）转结构化的 error 消息，让 run 继续而非崩。

**处理逻辑**：

- 捕获任意异常 → 记录带堆栈日志（含工具名+调用 id）→ 返回 error 消息，detail 截断到 500 字符并带固定恢复提示。
- `GraphBubbleUp` 透传，保留中断/恢复控制流。
- **异常分类覆盖正常标记**：异常派生分类比工具自身返回的分类更权威，强制覆盖已有元数据。
- 关键字分类给出三元组 `(错误类型, 是否可恢复, 建议动作)`（鉴权、限流、瞬时网络、配置缺失、权限、无结果、文件不存在、内部错误等）；纯数字关键字用词边界匹配，避免 "took 500ms" 误判为内部错误。
- 正常结果正常化：已有元数据保留；`Error:` 开头按文本分类；JSON 里 `error` 字段按值分类；语义零值（none/null/no/ok/success 等）不算错误；命中部分结果标记归为部分成功并建议改写查询。
- `task` 子代理委派工具异常时附 subagent 状态结构化元数据，使异常路径与正常路径产物结构等价，前端卡片能正确显示 failed 状态。
- skill 文件读取成功才落 `skill_context_entry` 元数据（校验路径落在 skills 根下、basename 是 `SKILL.md`、内容非 `Error:` 开头，并提取 YAML 描述），供压缩后重建 skill 上下文；error 结果不 stamp。
- **构建期校验**中间件顺序：进度中间件必须在最内层元数据中间件之外，否则构建期直接抛错（fail-fast）。

---

## 5. ToolReceiptMiddleware

**职责**：为每次工具调用打确定性凭证（receipt），并把凭证账本渲染给模型——零 LLM 的 provenance 层，供最终报告引用实际执行证据（citation verification 的基础）。

**处理逻辑**：

- **打凭证**：`wrap_tool_call` 在 handler 返回后给匹配的 ToolMessage 打 `deerflow_tool_receipt`（`make_tool_receipt`：工具名、状态、参数与输出哈希、字节数、时间戳）。直接 `ToolMessage` 与 `Command.update.messages` 两种结果都覆盖，按 `tool_call_id` 匹配消息。
- **顺序契约（build 期强制）**：本中间件是 `wrap_tool_call` 的**最外层**——Guardrail/SandboxAudit/ReadBeforeWrite/ToolProgress 可能短路或重建结果，内层 receipt 层会静默漏记账本。短路消息要么自打 meta，要么在 `make_tool_receipt` 回退到 `message.status`。
- **账本注入**：每次模型调用从在途消息提取凭证（`extract_tool_receipts`）渲染成隐藏 HumanMessage 追加，**从不写回 state**——像 DurableContextMiddleware 一样派生。
- **引用账本快照**：`_stamp_citing_ledger` 把本次渲染的凭证子集写回模型输出的 AIMessage `additional_kwargs[TOOL_RECEIPT_LEDGER_KEY]`——运行时所有、总是覆盖，provider 输出无法伪造"它对照的那份账本"。
- **render_mode 控制 token 成本**：
  - `always`：每次模型调用都渲染账本（子代理链——引用在那里产生，无账本则子代理无法引用、Layer 1 失效）。
  - `delegation_only`：只在消息流含已完成的子代理结果时渲染（主链——唯一需要引用上下文的地方），避免普通对话回合的常驻 token 税；作用域限定当前 turn（只数最新真实用户消息之后的消息），否则一次委派会让账本在后续每个普通回合都渲染。
- **失败处理**：打凭证失败只记 warning（绝不阻塞工具执行）——但系统性 stamp 失败必须可见，否则账本不完整、引用会撒谎。
- **凭证键是 runtime-owned**：总是覆盖、绝不清空已存在值——否则工具可伪造自己的"证据"并被当 runtime 凭证渲染。

> 配套模块：`receipt_verification.py` 负责校验模型生成的引用 id/锚点是否落在该轮凭证子里（子代理终态引用解析）；`tool_receipt.py` 提供凭证模型与渲染。安全终止/长度截断类结果也会被 receipt 记录。
