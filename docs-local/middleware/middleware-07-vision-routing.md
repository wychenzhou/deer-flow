# 视觉注入与工具路由/过滤/系统消息合并（处理逻辑）

本文件覆盖 Lead Agent（及子 Agent 复用）链中的四个中间件：

| 中间件 | 职责 |
|--------|------|
| `ViewImageMiddleware` | 工具完成后把图片以 base64 注入回会话 |
| `McpRoutingMiddleware` | 基于最新用户文本匹配，自动提升延迟加载的 MCP 工具 schema |
| `DeferredToolFilterMiddleware` | 模型绑定阶段隐藏未提升的延迟工具 schema，调用阶段阻止未提升工具调用 |
| `SystemMessageCoalescingMiddleware` | 把多个 SystemMessage 合并为单个前置 SystemMessage，兼容严格后端 |

---

## 1. ViewImageMiddleware

**职责**：当 `view_image` 工具的所有调用都被 ToolNode 完成后，在下次 LLM 调用前把被查看图片的 base64 以 HumanMessage 注入，让多模态模型真正"看到"图片。

**处理逻辑**：

- 触发判定需同时满足：messages 非空；倒序找到最后一条 AIMessage；其 tool_calls 里至少有一个叫 `view_image`；该消息中**所有** tool_calls 都在其后有对应 ToolMessage（要求全部完成，防同条消息其它工具未返回时图片被提前注入）；尚未注入过图片详情（去重守卫）。
- **去重**：扫最后一条 AIMessage 之后的 HumanMessage，若内容出现 `"Here are the images you've viewed"` 或 `"Here are the details of the images you've viewed"` 魔法字符串则跳过注入（文案不可改，否则旧线程恢复会重复注入）。
- **内容构造**：state 里的 `viewed_images` 只保存轻量元数据 `{mime_type, actual_path, size}`；base64 每次按需从磁盘读取编码。构造为 text 块 + `image_url` 块（`data:<mime>;base64,<...>`）；`viewed_images` 为空返回 `"No images have been viewed."` 文本块。
- **读取防御**：文件不存在/不是文件返回 None；用 `current_size == expected_size`（严格相等而非 `<=`）做 TOCTOU 校验，文件在查看与注入之间被改或增长就跳过；>20MB 跳过；OSError 返回 None。
- **读取失败降级**：content 追加一条文本提示 `(file unavailable or changed on disk: <path>)`，仍给模型一条合法文本块。
- **注入**：以 `HumanMessage(content=..., additional_kwargs={"hide_from_ui": True})` 注入，对前端/IM 不可见。
- **异步 offload**：`abefore_model` 把磁盘读+base64 编码这类可能阻塞的操作 `to_thread`，防阻塞事件循环；同步路径直接调。
- **设计**：metadata 与 base64 分离（避免每个 checkpoint 持久化最多 20MB 副本）；用 HumanMessage 而非 ToolMessage（后者必须绑 tool_call_id，只有 HumanMessage 能带 image_url 块）；信任 `actual_path` 来自服务端写入路径（已校验虚拟根）。

---

## 2. McpRoutingMiddleware

**职责**：每次 LLM 调用前扫最新真实用户消息文本，按可序列化路由索引 `{tool_name: {priority, keywords}}` 关键字匹配，把命中的延迟 MCP 工具名写入 `state["promoted"]`，让后续过滤器不再隐藏其 schema。只写状态、不执行工具、不过滤 tool_call、不持有 BaseTool 对象。

**处理逻辑**：

- 索引归一化（防御性）：空工具名丢弃；priority 非整数默认 0；keywords 非 Sequence、是 str/bytes、为空则丢弃该项；单个 keyword 去空格后为空丢弃。
- 取**最新真实用户消息**：用 `is_real_user_message` 跳过 `hide_from_ui` 的框架注入 HumanMessage。
- **短路**：无 `catalog_hash` 或空索引直接返回空列表；无命中也返回空。
- **取原文**：用 `get_original_user_content_text` 从 `additional_kwargs.original_user_content` 取 sanitize 前保留的原始文本（避开被 sanitize 后的漏配）。
- **匹配**：双方 `casefold()` 后大小写不敏感子串匹配；一个工具任一 keyword 命中即入选，重复命中不叠加；命中后按 priority 高者优先、同 priority 按工具名升序，截取前 `top_k`（默认 3，clamp 到 1..5）。
- **状态写入**：命中的话写最小 payload `{catalog_hash, names}`；未命中返回 None，不触发任何 state 更新（避免无意义 checkpoint）。
- **作用域隔离**：promoted 经 `catalog_hash` 绑定当前 catalog，陈旧持久化 promoted 不泄漏到改名/漂移后的工具。
- **装配守卫**：`assert_mcp_routing_before_deferred_filter` 在装配时 fail-fast，若本中间件排在 `DeferredToolFilterMiddleware` 之后（路由 > 过滤）直接抛 RuntimeError。

---

## 3. DeferredToolFilterMiddleware

**职责**：`tool_search` 启用时，MCP 工具仍注册在 ToolNode 供执行路由，但 schema 不应出现在 `bind_tools` 列表里，直到被提升。本中间件在模型绑定阶段隐藏未提升延迟工具的 schema，并在调用阶段阻止未提升工具的调用。

**处理逻辑**：

- 构造：`deferred_names` 为不可变 `frozenset`；`catalog_hash` 与 McpRouting 共享；装配时固化（fail-closed），故意不用 ContextVar（避免运行期动态漂移）。
- **已提升集合**：读 `state["promoted"]`，必须 `catalog_hash` 匹配才返回其 names；不匹配（陈旧 promoted、catalog 漂移、跨线程复用）返回空集，所有延迟工具保持隐藏。
- **应隐藏集合** = 延迟集合 − 已提升集合。
- **Schema 过滤**：延迟集合为空直接返回原 request（快路径零开销）；当前无隐藏工具也直接返回；用 `request.override(tools=active)` 返回新对象、不改原对象；只影响 model-bound schema，ToolNode 仍持有全部工具。
- **调用阻止（双保险）**：即使 schema 被隐藏，模型仍可能靠 few-shot/历史记录尝试调用未提升工具 → 不进入 handler，直接返回 error 状态 ToolMessage，提示应先调用 `tool_search` 再重试（若工具不在隐藏集合则放行执行）。
- `catalog_hash` 校验放在每次读 `_promoted` 时而非装配时：`state["promoted"]` 可能来自旧 checkpoint，每次重新校验确保漂移后的同名工具不会获得 schema 可见性。

---

## 4. SystemMessageCoalescingMiddleware

**职责**：把每次请求里的多个 SystemMessage（`request.system_message` 字段 + `request.messages` 列表）合并为单个前置 SystemMessage——针对严格 OpenAI 兼容后端（vLLM/SGLang/Qwen/Anthropic）拒绝非前置/非连续 system 的 provider 无关修复层。

**处理逻辑**：

- **no-op 优化**：若 `request.messages` 中没有 SystemMessage，直接返回 None、原 request 透传——此时只剩唯一静态 system prompt，不必合并，避免改内容指纹破坏 prefix-cache 命中。
- **合并顺序**：先放 `request.system_message`（静态系统 prompt），再按 `request.messages` 中出现的顺序追加所有 SystemMessage。
- **`dynamic_context_reminder` 去重**：合并前若有多条 DynamicContextMiddleware 注入的日期/元数据 reminder，只保留最后一条（最新日期），丢弃更早的——避免跨午夜相邻日期块语义冲突，让模型只看到最新日期。
- **合并输出**：保留首条 SystemMessage 的 `id`（通常是静态 system_prompt 的 id，下游靠其识别前置 system 消息）；`additional_kwargs` 用 `dict.update` 合并（后写覆盖先，`hide_from_ui`、`dynamic_context_reminder` 等标记保留）；内容 `\n\n` 连接，`_flatten_content` 兼容 str 与 list-of-blocks。
- 最后 `request.override(system_message=merged, messages=non_system)`，messages 不再含 SystemMessage。
- 用 `wrap_model_call` 而非 `before_model`：需操作 payload 里分离的 system_message+messages（这是 model-call handler 内部才扁平化为 `[system_message, *messages]` 的领域）；**不改 checkpoint 中持久化的 `state["messages"]`**，保证所有扫历史做标记的中间件（memory builder、journal、summarization、is_dynamic_context_reminder）继续正常。
