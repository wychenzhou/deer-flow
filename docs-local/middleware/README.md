# middleware — 中间件链深挖目录（总索引）

本目录是 DeerFlow harness **中间件链（链位 1–39）** 的中文源码深挖合集（8 篇）。
链装配基线见 `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md`。先看本索引知道
「哪个文件讲哪些中间件」，再进对应篇目看实现细节。

各篇顶部均有统一表格：**链位 | 中间件 | 一句话职责 | 主钩子 | 装配条件**，正文深挖不动。

## 链位总览（本篇一律采用 `agents/middlewares/AGENTS.md` 的 1–39 口径）

| 链位 | 中间件 | 一句话职责 | 详见 |
|---|---|---|---|
| 1 | InputSanitizationMiddleware | 净化用户输入，防伪造框架标签 | [01](middleware-01-io-safety.md) |
| （随 1 之后） | KnowledgeScopeMiddleware | 只暴露 Gateway 准入的执行范围，抹除消息里的范围/展示数据 | [01](middleware-01-io-safety.md) |
| 2 | ToolOutputBudgetMiddleware | 超预算工具结果外化/截断 | [01](middleware-01-io-safety.md) |
| 3 | ToolResultSanitizationMiddleware | 中和远程抓取内容的注入标签 | [01](middleware-01-io-safety.md) |
| 4 | PiiRedactionMiddleware | 用户消息 / 远程结果 PII 不可逆脱敏（可选，默认关） | [01](middleware-01-io-safety.md) |
| 5 | ThreadDataMiddleware | 为 `(user,thread)` 建私有目录 | [02](middleware-02-infrastructure.md) |
| 6 | UploadsMiddleware | 本轮上传注入 `<current_uploads>` | [02](middleware-02-infrastructure.md) |
| 7 | SandboxMiddleware | 沙箱获取/保留/释放 | [02](middleware-02-infrastructure.md) |
| 8 | DanglingToolCallMiddleware | 补配对 / 丢孤儿 / 修畸形调用 | [01](middleware-01-io-safety.md) |
| 9 | LLMErrorHandlingMiddleware | provider 失败归一化 + 重试/熔断 | [03](middleware-03-error-handling.md) |
| 10 | ArtifactResolutionMiddleware | 调用参数里的 artifact 句柄解析为真实引用（可选） | [03](middleware-03-error-handling.md) |
| 11 | Authorization / GuardrailMiddleware | 执行前（Layer 2）授权 + 外部 guardrail | [03](middleware-03-error-handling.md) |
| 12 | SandboxAuditMiddleware | bash 命令分级 block/warn/pass + 审计 | [03](middleware-03-error-handling.md) |
| 13 | ReadBeforeWriteMiddleware | 文件写入门（版本门） | [04](middleware-04-file-safety.md) |
| 14 | ToolProgressMiddleware | (thread,tool) 停滞状态机 | [04](middleware-04-file-safety.md) |
| 15 | ToolReceiptMiddleware + ToolErrorHandlingMiddleware | 结果打凭证 + 异常结构化 | [03](middleware-03-error-handling.md) |
| 16 | ArtifactCaptureMiddleware | 从工具结果捕获 artifact 引用入 state（可选） | [03](middleware-03-error-handling.md) |
| 17 | DynamicContextMiddleware | 日期/记忆一次性冻结注入 | [05](middleware-05-context-injection.md) |
| 18 | SkillActivationMiddleware | 斜杠技能激活 + 正文注入 + 密钥绑定 | [05](middleware-05-context-injection.md) |
| 19 | SkillToolPolicyMiddleware | 激活技能 allowed-tools 裁 schema/拦执行 | [05](middleware-05-context-injection.md) |
| 20 | DurableContextMiddleware | 委派/技能引用压缩前捕获并投影 | [05](middleware-05-context-injection.md) |
| 21 | DeerFlowSummarizationMiddleware | 上下文压缩（可选） | [06](middleware-06-conversation-management.md) |
| 22 | TodoMiddleware | 待办清单（plan mode，可选） | [06](middleware-06-conversation-management.md) |
| 23 | TokenUsageMiddleware | token 记录 + 子代理归因（可选） | [06](middleware-06-conversation-management.md) |
| 24 | TitleMiddleware | 线程自动标题 | [06](middleware-06-conversation-management.md) |
| 25 | MemoryMiddleware | 记忆异步抽取入队 | [06](middleware-06-conversation-management.md) |
| 26 | ViewImageMiddleware | 多模态图像临时注入（仅视觉模型） | [07](middleware-07-vision-routing.md) |
| 27 | McpRoutingMiddleware | 延迟 MCP 工具自动提升 | [07](middleware-07-vision-routing.md) |
| 28 | DeferredToolPromotionAuditMiddleware | 观测 tool_search Command 的最终提升结果 | [07](middleware-07-vision-routing.md) |
| 29 | DeferredToolFilterMiddleware | 延迟工具 schema 隐藏/拦截（可选） | [07](middleware-07-vision-routing.md) |
| 30 | SystemMessageCoalescingMiddleware | 合并 system 到开头 | [07](middleware-07-vision-routing.md) |
| 31 | SubagentLimitMiddleware | 子代理并发/总量截断（可选） | [08](middleware-08-safety-guards.md) |
| 32 | LoopDetectionMiddleware | 重复 tool_calls 死循环硬停（可选） | [08](middleware-08-safety-guards.md) |
| 33 | TokenBudgetMiddleware | 单 run token 预算硬停 | [08](middleware-08-safety-guards.md) |
| 34 | （自定义中间件插入点） | 代码注入点 | [08](middleware-08-safety-guards.md) |
| 35 | （扩展中间件插入点） | config 声明注入点 | [08](middleware-08-safety-guards.md) |
| 36 | TerminalResponseMiddleware | 空终态重试/兜底 | [08](middleware-08-safety-guards.md) |
| 37 | ModelLengthFinishReasonMiddleware | 长度截断记账（不改写） | [08](middleware-08-safety-guards.md) |
| 38 | SafetyFinishReasonMiddleware | 安全终止抑制工具（可选） | [08](middleware-08-safety-guards.md) |
| 39 | ClarificationMiddleware | 澄清中断等用户（必须最后） | [08](middleware-08-safety-guards.md) |

## 篇目 → 链位

| 篇目 | 链位 | 主题 |
|---|---|---|
| [01](middleware-01-io-safety.md) | 1, 2, 3, 4, 8（+ 1 之后的 KnowledgeScope） | I/O 安全：净化 / 预算 / 配对 / PII 脱敏 |
| [02](middleware-02-infrastructure.md) | 5, 6, 7 | 基础设施：目录 / 上传 / 沙箱 |
| [03](middleware-03-error-handling.md) | 9, 10, 11, 12, 15, 16 | 错误处理 + 授权 / 审计 / 凭证 / artifact |
| [04](middleware-04-file-safety.md) | 13, 14 | 文件写门 + 停滞守卫 |
| [05](middleware-05-context-injection.md) | 17, 18, 19, 20 | 上下文注入：动态日期 / 技能 / 持久上下文 |
| [06](middleware-06-conversation-management.md) | 21, 22, 23, 24, 25 | 对话生命周期：压缩 / 待办 / 归因 / 标题 / 记忆 |
| [07](middleware-07-vision-routing.md) | 26, 27, 28, 29, 30 | 视觉注入 + MCP 路由 + 延迟工具 + system 合并 |
| [08](middleware-08-safety-guards.md) | 31, 32, 33, 36, 37, 38, 39 | 终止性与收尾：限额 / 死循环 / 预算 / 兜底 |

> **链位口径**：统一采用 `agents/middlewares/AGENTS.md` 的 1–39。`KnowledgeScopeMiddleware` **不占独立链位**，
> 紧跟在第 1 位 `InputSanitizationMiddleware` 之后。需注意一组**成对编号**：第 11 位 = **Authorization /
> GuardrailMiddleware**（授权 + 外部 guardrail 同属第 11 位，授权在外、外部在内）；第 15 位 = **ToolReceiptMiddleware
> + ToolErrorHandlingMiddleware**（一组，receipt 最外层、error-handling 最内层，恰好夹住 11~14 的短路者）。
> 其余 9、10、12、13、14、16 一一对应。
>
> **一则编号特例**：第 28 位 `DeferredToolPromotionAuditMiddleware` 是**职能分组编号**——它在物理装配上位置更靠前
> （紧随第 18 位 `SkillActivationMiddleware`、第 19 位 `SkillToolPolicyMiddleware` 之前），但按 AGENTS.md 的
> 延迟工具族（27 McpRouting / 28 Audit / 29 Filter）统一编号；详见 [05](middleware-05-context-injection.md) 与
> [07](middleware-07-vision-routing.md)。
