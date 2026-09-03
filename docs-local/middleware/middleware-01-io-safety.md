# I/O 安全中间件（处理逻辑）

本文件解析模型输入/输出通道上的安全边界：净化用户输入、限制工具输出体量、中和远程内容注入向量、修复历史中的悬挂工具调用。

| 序号 | 中间件 | 职责 |
|------|--------|------|
| 1 | `InputSanitizationMiddleware` | 中和用户消息中的提示注入控制标签 |
| 2 | `ToolOutputBudgetMiddleware` | 工具输出体量预算与外化 |
| 3 | `ToolResultSanitizationMiddleware` | 中和远程工具结果中的注入控制标签 |
| 4 | `DanglingToolCallMiddleware` | 修复悬挂工具调用与孤儿工具结果 |

辅助模块：

- `tool_output_synopsis.py` — 确定性的工具输出概要生成器（不调用 LLM，带 DoS 防护）
- `tool_call_metadata.py` — 保持 AIMessage 原始 provider 工具调用元数据同步的辅助函数

---

## 1. InputSanitizationMiddleware

**职责**：对进入模型前的"最后一条真实用户消息"做结构净化，中和提示注入。

**处理逻辑**：

- 从后往前找第一条"真实用户消息"：必须是 HumanMessage；`name == "summary"` 不算；`hide_from_ui` 且无有效用户响应也不算。空白消息原样返回，不插入标记。
- 把框架保留的 XML 标签（`<system-reminder>`、`<memory>`、`<think>`、`<analysis>`、`<role>` 等，以及系统提示声明的权威块、子代理提示块、常见注入词如 `system`/`instruction`/`override`/`ignore`/`prompt`）HTML 转义为字面文本——保留可读性但剥离结构语义。
- 对含边界 token（`--- BEGIN/END USER INPUT ---`）的内容做"边界中和"：把用户伪造的边界替换为中性标记（`[BEGIN USER INPUT]`/`[END USER INPUT]`）——既防"自抑制"（用户只键入 begin 就误判已包裹），也防"break-out"（在载荷中嵌入 end 提前结束）。
- 用纯文本边界包裹处理后的内容；幂等：严格 `startswith(begin) and endswith(end)` 判定已包裹，已包裹则只中和内部边界 token。
- 多模态 content 兼容字符串和块列表：合并所有文本块处理，但保留穿插的图片块（`[text,image,text]` 的图片不丢弃）。
- 净化后用 `ORIGINAL_USER_CONTENT_KEY` 保留净化前原始文本；first-writer-wins：已有合法字符串值就不动，只修复非法值。
- 全程 fail-open：除 `GraphBubbleUp` 外任何异常都吞掉并放行原 request——净化本身不当单点故障。
- 关键点：只作用于 per-request，从不写回 state、不修改原始 request；用 **Denylist** 而非 allowlist（框架把结构化标签声明为受信内部数据）。

**设计决策**：与 `ToolResultSanitizationMiddleware` 形成"两个不可信入口"的对称防御（用户输入 / 远程内容）。

---

## 2. ToolOutputBudgetMiddleware

**职责**：工具输出体量预算，防止超大输出撑爆模型上下文。

**处理逻辑**：

- 工具 handler 执行**之后**判定；先做廉价预检 `_needs_budget`：工具在豁免名单、或结果在 per-tool 阈值与 fallback 阈值内 → 直接放行，避免对小结果做线程卸载。
- 预检阈值与主逻辑镜像：取 per-tool 外化阈值与全局 fallback 的**最小值**，保证预检不假阴性。
- 超阈值后三层降级：① 外化到挂载型宿主路径（host bind-mount 到沙箱同路径）；② 外化到非挂载型远程 AIO 沙箱文件系统；③ 磁盘/沙箱都不可用时内联 head+tail 截断（不超 `max_chars`）。
- 外化路径带防护：拒绝绝对路径和含 `..` 的子目录；写宿主文件后校验路径不逃逸 storage 目录；远程 AIO 直接 `mkdir -p` + `write_file` 写进沙箱，用 `test -s` 显式验证落地（AIO 失败返回字符串而非抛异常），落地失败返回 None 走 fallback。
- 文件名只用 `uuid4().hex[:12]` 生成——`tool_call_id` 是不可信值，不进入文件系统路径。
- 预览与截断都在**行边界**对齐（end 向前 snap、start 向后 snap），避免切断行。
- `wrap_model_call` 阶段再扫历史 ToolMessage：超阈值的内联 fallback 截断，**不再重新外化**（tool-call 时已外化过）；用 `any()` 预扫描，全部不超就返回 None（避免每次模型调用重建长历史）。
- 结果形态兼容 `ToolMessage` 与带 `update.messages` 的 `Command`；无变更返回原对象便于 `is` 比较。
- 异步路径把磁盘/沙箱 IO 经 `asyncio.to_thread` 卸载到工作线程。

---

## 3. ToolResultSanitizationMiddleware

**职责**：中和远程工具结果（web 内容）里的注入控制标签。

**处理逻辑**：

- 工具执行之后判定；仅当工具名落在硬编码 allowlist（`web_fetch`/`web_search`/`image_capture`/`web_capture`）才处理；判定依据是 `tool_call["name"]`，所以工具成功或失败都判定。
- 复用与用户输入相同的 `neutralize_untrusted_tags` 原语，把攻击者网页伪造的框架标签转义为字面文本；本地工具（`bash`/`read_file` 等）输出保持原样，避免误伤合法代码/日志。
- content 兼容字符串和块列表；文本块逐个中和，图片块原样透传；`ToolMessage` 与 `Command(update.messages)` 两种结果都处理。
- 用名字 allowlist 而非 `fetch`/`search`/`crawl` 子串启发式（会误伤 `file_search` 等本地工具）。
- **已知局限**：MCP 以任意名暴露的远程内容工具不被覆盖，应靠注册元数据标记而非名字猜测。`web_capture` 被纳入是因为 Browserless 截图工具带进来的 `X-Response-Status`（攻击者可控远程内容）也要中和。
- **关键顺序**：必须先由本中间件中和，再由预算中间件截断/外化——否则外化的文件里会残留注入标签。

---

## 4. DanglingToolCallMiddleware

**职责**：修复历史消息中悬挂的工具调用与孤儿工具结果，保证严格 provider 的 tool_call 配对校验。

**处理逻辑**：

- 在 `wrap_model_call` 阶段扫历史，修两类结构问题：
  - **悬挂调用**（AIMessage 有 tool_calls 但无对应 ToolMessage）
  - **孤儿结果**（ToolMessage 存在但对应 AIMessage 已丢）
- 先规范化所有畸形 tool_call id：`None`/空 id 替换为位置派生的合成 id（`deerflow_synthetic_tool_call_{msg_index}_{source}_{position}`），使配对 pass 与模型消息无需共享状态即可同意。
- 收集所有合法 ToolMessage 按 `tool_call_id` 分桶，收集所有 AIMessage 的 tool_call id 集合。
- 重排消息：孤儿 ToolMessage 静默丢弃；每个 AIMessage 之后按 tool_call 序检查有无匹配 ToolMessage，有则重发、无则注入合成 error ToolMessage。
- **位置配对是保守的**：仅当 turn 内所有 open call 都有结果（`positional=True`）才用位置破平；若缺失结果（即被打断），返回 None 交给孤儿 pass 丢弃——"好过发明一个配对"。
- name 匹配宽松：仅当 call 和 result 都有合法 name 且不同才排除配对；缺失 name 永不矛盾。
- 规范化 tool_call name：空/非字符串 name 替换为 `unknown_tool`；tool_calls 从三个来源抽取（结构化字段、raw payload、invalid_tool_calls），raw 仅在结构化与 invalid 都为空时才 relabel（避免给被遮蔽的 raw 铸造 id 引入孤儿）。
- 合成错误消息专门分支：无效 name → 提示改用可用工具名；解析失败的工具（含 `write_file` 携带巨大 payload 的 #2894 workaround，错误描述限 500 字符避免回显大块内容）→ 专门恢复指引；一般中断 → "工具调用被中断"。
- 参数规范化：dict args 转合法 JSON 对象字符串（`allow_nan=False`），非法则置 `{}`，防 OpenAI 兼容 replay 400。
- 只 per-request 修改，不动 checkpoint state；用 `wrap_model_call` 而非 `before_model + add_messages`（后者把补丁追加到列表末尾会破坏因果序）。

---

## 辅助模块

**`tool_output_synopsis.py`**：确定性（不调 LLM）概要生成器，带 DoS 防护（5MB 字节硬上限、YAML alias 500K 上限、XML 用 defusedxml）。形态检测顺序：空 → 超大 → 二进制 → JSON → XML → TSV → CSV → YAML → 代码 → 文本。CSV/TSV 需 ≥5 行同宽且表头形似标识符防误判；YAML 拒绝日志行。

**`tool_call_metadata.py`**：`clone_ai_message_with_tool_calls` 在被截断 tool_calls 时保持 structured 字段与 raw payload 同步，并把 `finish_reason` 从 `tool_calls` 修正为 `stop`——避免触发 Dangling 中间件不必要的修复。
