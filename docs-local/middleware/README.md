# middleware — 中间件链深挖目录（总索引）

本目录是 DeerFlow harness **中间件链（链位 1–35）** 的中文源码深挖合集（8 篇）。
链装配基线见 `backend/packages/harness/deerflow/agents/middlewares/AGENTS.md`。先看本索引知道
「哪个文件讲哪些中间件」，再进对应篇目看实现细节。

各篇顶部均有统一表格：**链位 | 中间件 | 一句话职责 | 主钩子 | 装配条件**，正文深挖不动。

## 链位总览（本篇一律采用 `agents/middlewares/AGENTS.md` 的 1–35 口径）

| 链位 | 中间件 | 一句话职责 | 详见 |
|---|---|---|---|
| 1 | InputSanitizationMiddleware | 净化用户输入，防伪造框架标签 | [01](middleware-01-io-safety.md) |
| 2 | ToolOutputBudgetMiddleware | 超预算工具结果外化/截断 | [01](middleware-01-io-safety.md) |
| 3 | ToolResultSanitizationMiddleware | 中和远程抓取内容的注入标签 | [01](middleware-01-io-safety.md) |
| 4 | ThreadDataMiddleware | 为 `(user,thread)` 建私有目录 | [02](middleware-02-infrastructure.md) |
| 5 | UploadsMiddleware | 本轮上传注入 `<current_uploads>` | [02](middleware-02-infrastructure.md) |
| 6 | SandboxMiddleware | 沙箱获取/保留/释放 | [02](middleware-02-infrastructure.md) |
| 7 | DanglingToolCallMiddleware | 补配对 / 丢孤儿 / 修畸形调用 | [01](middleware-01-io-safety.md) |
| 8 | LLMErrorHandlingMiddleware | provider 失败归一化 + 重试/熔断 | [03](middleware-03-error-handling.md) |
| 9 | Authorization / GuardrailMiddleware | 执行前（Layer 2）授权 + 外部 guardrail | [03](middleware-03-error-handling.md) |
| 10 | SandboxAuditMiddleware | bash 命令分级 block/warn/pass + 审计 | [03](middleware-03-error-handling.md) |
| 11 | ReadBeforeWriteMiddleware | 文件写入门（版本门） | [04](middleware-04-file-safety.md) |
| 12 | ToolProgressMiddleware | (thread,tool) 停滞状态机 | [04](middleware-04-file-safety.md) |
| 13 | ToolReceiptMiddleware + ToolErrorHandlingMiddleware | 结果打凭证 + 异常结构化 | [03](middleware-03-error-handling.md) |
| 14 | DynamicContextMiddleware | 日期/记忆一次性冻结注入 | [05](middleware-05-context-injection.md) |
| 15 | SkillActivationMiddleware | 斜杠技能激活 + 正文注入 + 密钥绑定 | [05](middleware-05-context-injection.md) |
| 16 | SkillToolPolicyMiddleware | 激活技能 allowed-tools 裁 schema/拦执行 | [05](middleware-05-context-injection.md) |
| 17 | DurableContextMiddleware | 委派/技能引用压缩前捕获并投影 | [05](middleware-05-context-injection.md) |
| 18 | DeerFlowSummarizationMiddleware | 上下文压缩 | [06](middleware-06-conversation-management.md) |
| 19 | TodoListMiddleware | 待办清单（plan mode） | [06](middleware-06-conversation-management.md) |
| 20 | TokenUsageMiddleware | token 记录 + 子代理归因 | [06](middleware-06-conversation-management.md) |
| 21 | TitleMiddleware | 线程自动标题 | [06](middleware-06-conversation-management.md) |
| 22 | MemoryMiddleware | 记忆异步抽取入队 | [06](middleware-06-conversation-management.md) |
| 23 | ViewImageMiddleware | 多模态图像临时注入 | [07](middleware-07-vision-routing.md) |
| 24 | McpRoutingMiddleware | 延迟 MCP 工具自动提升 | [07](middleware-07-vision-routing.md) |
| 25 | DeferredToolFilterMiddleware | 延迟工具 schema 隐藏/拦截 | [07](middleware-07-vision-routing.md) |
| 26 | SystemMessageCoalescingMiddleware | 合并 system 到开头 | [07](middleware-07-vision-routing.md) |
| 27 | SubagentLimitMiddleware | 子代理并发/总量截断 | [08](middleware-08-safety-guards.md) |
| 28 | LoopDetectionMiddleware | 重复 tool_calls 死循环硬停 | [08](middleware-08-safety-guards.md) |
| 29 | TokenBudgetMiddleware | 单 run token 预算硬停 | [08](middleware-08-safety-guards.md) |
| 30 | （自定义中间件插入点） | 代码注入点 | [08](middleware-08-safety-guards.md) |
| 31 | （扩展中间件插入点） | config 声明注入点 | [08](middleware-08-safety-guards.md) |
| 32 | TerminalResponseMiddleware | 空终态重试/兜底 | [08](middleware-08-safety-guards.md) |
| 33 | ModelLengthFinishReasonMiddleware | 长度截断记账（不改写） | [08](middleware-08-safety-guards.md) |
| 34 | SafetyFinishReasonMiddleware | 安全终止抑制工具 | [08](middleware-08-safety-guards.md) |
| 35 | ClarificationMiddleware | 澄清中断等用户（必须最后） | [08](middleware-08-safety-guards.md) |

## 篇目 → 链位

| 篇目 | 链位 | 主题 |
|---|---|---|
| [01](middleware-01-io-safety.md) | 1, 2, 3, 7 | I/O 安全：净化 / 预算 / 配对 |
| [02](middleware-02-infrastructure.md) | 4, 5, 6 | 基础设施：目录 / 上传 / 沙箱 |
| [03](middleware-03-error-handling.md) | 8, 9, 10, 13 | 错误处理 + 授权 / 审计 / 凭证 |
| [04](middleware-04-file-safety.md) | 11, 12 | 文件写门 + 停滞守卫 |
| [05](middleware-05-context-injection.md) | 14, 15, 16, 17 | 上下文注入：动态日期 / 技能 / 持久上下文 |
| [06](middleware-06-conversation-management.md) | 18, 19, 20, 21, 22 | 对话生命周期：压缩 / 待办 / 归因 / 标题 / 记忆 |
| [07](middleware-07-vision-routing.md) | 23, 24, 25, 26 | 视觉注入 + MCP 路由 + 延迟工具 + system 合并 |
| [08](middleware-08-safety-guards.md) | 27–29, 32–35 | 终止性与收尾：限额 / 死循环 / 预算 / 兜底 |

> **链位口径**：统一采用 `agents/middlewares/AGENTS.md` 的 1–35。各篇顶部块与正文一致，不再有「职能分组
> 编号 vs 代码下标」两套号。需注意一组**成对编号**：第 9 位 = **Authorization / GuardrailMiddleware**（授权 + 外部
> guardrail 同属第 9 位，授权在外、外部在内）；第 13 位 = **ToolReceiptMiddleware + ToolErrorHandlingMiddleware**
> （一组，receipt 最外层、error-handling 最内层，恰好夹住 9~12 的短路者）。其余 8、10、11、12 一一对应。
