# 中间件文档 07 — 视觉注入与工具路由/过滤/系统消息合并

本文档详细覆盖以下四个 DeerFlow Lead Agent（及子 Agent 复用）链中的中间件：

1. `ViewImageMiddleware` — 工具完成后将图片以 base64 形式注入回会话
2. `McpRoutingMiddleware` — 基于最新用户文本匹配，自动提升延迟加载的 MCP 工具 schema
3. `DeferredToolFilterMiddleware` — 在模型绑定阶段隐藏未提升的延迟工具 schema，并在调用阶段阻止未提升的工具调用
4. `SystemMessageCoalescingMiddleware` — 将多个 SystemMessage 合并为单个前置 SystemMessage，兼容严格后端

---

# 1. ViewImageMiddleware

## 概述

当 `view_image` 工具的所有调用都已被 `ToolNode` 完成后，在下次 LLM 调用之前把被查看图片的 base64 数据以 `HumanMessage` 形式注入到会话中，使多模态模型可以直接"看到"工具加载的图片，而无需用户再次提示。

## 为什么需要这个中间件

### 场景痛点

模型调用 `view_image` 工具后，工具返回的 `ToolMessage` 中只包含图片的元数据（路径、MIME 类型、大小），模型仍然"看不到"图片的像素内容。如果多模态模型需要基于图像内容做推理，用户必须手动重新上传图片或描述画面内容，导致交互割裂且效率低下。

### 为什么模型自身无法避免

模型输出的 `view_image` 是一个工具调用（tool_call），工具执行后的结果由 `ToolNode` 写入 `ToolMessage`。模型本身无权直接访问磁盘文件系统，也无法回溯原始上传过程中的二进制数据——它只能被动等待运行时以某种形式把图片内容重新送入消息列表。

### 解决思路

在 `view_image` 工具所有调用完成后，自动从磁盘读取图片并编码为 base64，以带 `image_url` 块的 `HumanMessage` 注入会话，让多模态模型在下一轮 LLM 调用时直接"看到"图片内容。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/view_image_middleware.py` |
| 实现的钩子 | `before_model`（同步） / `abefore_model`（异步） |
| 持久化 | State（`viewed_images` 元数据；base64 数据仅 Per-request，不持久化） |
| 配置依赖 | 仅当模型 `supports_vision=true` 时被装配进中间件链；`view_image` 工具由 `get_available_tools` 同步添加 |
| State Schema | `ViewImageMiddlewareState`（复用 `ThreadState`，保留 reducer 注解） |
| 大小限制 | `_MAX_IMAGE_BYTES = 20 * 1024 * 1024`（20 MB） |

## 核心逻辑

### 触发判定 `_should_inject_image_message`

注入必须同时满足四个条件，否则直接返回 `False`：

1. `state["messages"]` 非空。
2. 倒序找到最后一条 `AIMessage`。
3. 该 `AIMessage` 的 `tool_calls` 中至少有一个 `name == "view_image"`。
4. 该 AIMessage 中的**所有** tool_calls 都已经在它之后的 `ToolMessage` 中得到响应（`_all_tools_completed`）。
5. 还没有注入过图片详情消息（去重守卫，见下）。

```python
def _has_view_image_tool(self, message: AIMessage) -> bool:
    if not hasattr(message, "tool_calls") or not message.tool_calls:
        return False
    return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)
```

`_all_tools_completed` 的关键判定：

```python
tool_call_ids = {tc.get("id") for tc in assistant_msg.tool_calls if tc.get("id")}
assistant_idx = messages.index(assistant_msg)
completed_tool_ids = set()
for msg in messages[assistant_idx + 1:]:
    if isinstance(msg, ToolMessage) and msg.tool_call_id:
        completed_tool_ids.add(msg.tool_call_id)
return tool_call_ids.issubset(completed_tool_ids)
```

注意：它要求**所有** tool_call 都完成，不仅仅是 `view_image` 调用。这样防止同一条 AIMessage 中的其它工具（例如 `read_file`）尚未返回时，图片就被提前注入。

### 已注入去重

为了在多个连续 LLM 回合中不重复注入同一批图片，`_should_inject_image_message` 在最后一条 AIMessage 之后扫描 `HumanMessage`：

```python
for msg in messages[assistant_idx + 1:]:
    if isinstance(msg, HumanMessage):
        content_str = str(msg.content)
        if "Here are the images you've viewed" in content_str or \
           "Here are the details of the images you've viewed" in content_str:
            return False
```

去重依赖注入消息内的魔法字符串标记，所以**不要修改这些文案**，否则旧线程恢复时会重复注入。

### 内容构造 `_create_image_details_message`

该方法按需从磁盘读取图片并编码为 base64。状态中的 `viewed_images` 仅保存轻量元数据：

```python
viewed_images = state.get("viewed_images", {})  # {image_path: {mime_type, actual_path, size}}
```

构造的 content blocks 形如：

```python
[
    {"type": "text", "text": "Here are the images you've viewed:"},
    {"type": "text", "text": "\n- **<path>** (<mime>)"},
    {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}},
    ...
]
```

若 `viewed_images` 为空，返回的是 `[{"type": "text", "text": "No images have been viewed."}]`，使模型仍然得到一条合法的文本块而不是裸字符串数组。

### base64 注入条件 `_read_image_as_data_url`

读取磁盘文件的关键防御逻辑：

```python
@staticmethod
def _read_image_as_data_url(actual_path: str, mime_type: str, expected_size: int) -> str | None:
    try:
        file_path = Path(actual_path)
        if not file_path.exists() or not file_path.is_file():
            return None
        current_size = file_path.stat().st_size
        if current_size != expected_size:
            # 文件在 view 与 inject 之间被修改 — 跳过
            return None
        if current_size > _MAX_IMAGE_BYTES:
            return None
        with open(file_path, "rb") as f:
            image_bytes = f.read()
        base64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{base64_data}"
    except OSError:
        return None
```

三个守卫点：

1. **信任假设**：`actual_path` 来自服务端 `view_image_tool` 的写入路径，已经在写入时校验过虚拟根；客户端输入无法触及该字段。
2. **TOCTOU 防护**：重新比较 `current_size == expected_size`，防止文件在 view 与 inject 之间被替换或增长。注意是严格相等，不是 `<=`——任何变化都跳过。
3. **20MB 体积上限**：作为深度防御，与 `view_image_tool` 写入侧的硬限制保持一致。

读取失败时，content 中写入一条文本降级提示：

```python
content_blocks.append({"type": "text", "text": f"  (file unavailable or changed on disk: {actual_path})"})
```

### 注入动作

```python
human_msg = HumanMessage(content=image_content, additional_kwargs={"hide_from_ui": True})
return {"messages": [human_msg]}
```

`hide_from_ui=True` 让这条消息对前端聊天 UI 和 IM 通道不可见，避免重复展示工具结果。这与链中其它中间件注入的隐藏上下文消息保持一致（例如 `DurableContextMiddleware`、`DynamicContextMiddleware`）。

### 同步与异步差异

异步路径 `abefore_model` 把磁盘读取 + base64 编码这种可能阻塞的操作 offload 到线程：

```python
image_content = await asyncio.to_thread(self._create_image_details_message, state)
```

20MB 文件读取 + base64 编码可能耗时数百毫秒，直接跑在事件循环上会阻塞其它流。同步路径 `before_model` 直接调用，因为同步执行上下文已经不在事件循环上。

## 关键设计决策

**为什么 metadata 与 base64 分离？**
按 issue #4138 的设计，`viewed_images` 中只保存 `{mime_type, actual_path, size}` 之类的轻量字段。base64 数据每次按需从磁盘读取，不进入 checkpoint。否则每次 checkpoint 都会持久化一份高达 20MB 的 base64 副本，在多 checkpoint、长会话中成本爆炸。

**为什么用 HumanMessage 而不是 ToolMessage？**
ToolMessage 必须绑定到某个 `tool_call_id`，而这里需要的是把内容整体呈现给多模态模型作为上下文输入，HumanMessage 才能携带 `image_url` 类型的 content block。

**为什么要求所有 tool_call 完成？**
若只检查 `view_image` 自身，会导致同一条 AIMessage 中其它工具未完成时图片提前注入，模型可能在没有完整工具结果的情况下做推理。

**trade-off**：
- base64 注入体积大；虽然不持久化，但每次 LLM 调用都会重新读盘编码。若 `viewed_images` 中条目很多，每个条目都会触发一次磁盘读。
- 去重依赖文本字符串，脆弱但简单。若改写文案必须同步考虑旧 checkpoint 的兼容性。

## 与其他中间件的协作

- **`UploadsMiddleware`**：上传的图片先写入 `uploaded_files`，模型通过 `view_image` 工具加载后才进入 `viewed_images`，二者职责分离。
- **`DynamicContextMiddleware`** / **`DurableContextMiddleware`**：同样使用 `hide_from_ui=True` 注入隐藏上下文，约定一致。
- **`SystemMessageCoalescingMiddleware`**：本中间件注入的是 `HumanMessage`，不会被 SystemMessage 合并触及。
- **`DeferredToolFilterMiddleware`**：本中间件只读 `messages` 与 `viewed_images`，不修改 `request.tools`，互不干扰。
- 装配位置：Lead Agent 链中第 23 位，仅当 `model.supports_vision` 为真时附加。

---

# 2. McpRoutingMiddleware

## 概述

在每次 LLM 调用之前，扫描最新真实用户消息文本，根据可序列化的 MCP 路由索引（`{tool_name: {priority, keywords}}`）进行关键字匹配，把命中的延迟 MCP 工具名写入 `state["promoted"]`，从而让后续的 `DeferredToolFilterMiddleware` 不再隐藏这些工具的 schema。本中间件只写**最小的提升状态**，不执行任何工具、不持有 `BaseTool` 对象、不过滤 tool_call。

## 为什么需要这个中间件

### 场景痛点

当 MCP 工具数量庞大时（几十甚至上百个），所有工具的 schema 同时暴露给模型会导致工具选择质量下降、令牌消耗激增。`DeferredToolFilterMiddleware` 默认隐藏所有延迟工具的 schema，但用户每次使用都需要手动先调用 `tool_search` 来提升目标工具——即使意图已经在用户消息中明确表达（例如"帮我抓取 https://example.com"明显需要 `fetch_url` 工具）。

### 为什么模型自身无法避免

由于 schema 被过滤，模型根本不知道这些延迟工具的存在，自然也无法自行决定"应该先搜索再调用"。模型只能看到当前 `request.tools` 中可见的工具，对隐藏的工具一无所知。

### 解决思路

在每次 LLM 调用之前，自动扫描用户最新真实消息文本，通过预配置的关键字索引命中相关延迟工具，将其名称写入 `state["promoted"]`，使 `DeferredToolFilterMiddleware` 在 schema 过滤时放行这些工具。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/mcp_routing_middleware.py` |
| 实现的钩子 | `before_model` / `abefore_model` |
| 持久化 | State（`promoted` 字段，受 `merge_promoted` reducer 约束、按 `catalog_hash` 作用域） |
| 配置依赖 | `tool_search.enabled=true`、PR1 MCP routing metadata、`tool_search.auto_promote_top_k`（默认 3，clamp 到 1..5） |
| State Schema | `AgentState`（通用） |
| 装配顺序约束 | 必须在 `DeferredToolFilterMiddleware` 之前 |

## 核心逻辑

### 构造与索引归一化

```python
def __init__(self, routing_index: McpRoutingIndex, catalog_hash: str | None, top_k: int) -> None:
    super().__init__()
    self._catalog_hash = catalog_hash
    self._top_k = clamp_auto_promote_top_k(top_k)
    self._routing_index = self._normalize_index(routing_index)
```

`_normalize_index` 对路由数据做**防御性再归一化**：

```python
@staticmethod
def _normalize_index(routing_index: McpRoutingIndex) -> dict[str, tuple[int, tuple[str, ...]]]:
    normalized: dict[str, tuple[int, tuple[str, ...]]] = {}
    for raw_name, raw_entry in routing_index.items():
        name = str(raw_name)
        if not name:
            continue
        try:
            priority = int(raw_entry.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        raw_keywords = raw_entry.get("keywords") or []
        if not isinstance(raw_keywords, Sequence) or isinstance(raw_keywords, (str, bytes)):
            raw_keywords = []
        keywords = tuple(keyword for keyword in (str(item).strip() for item in raw_keywords) if keyword)
        if not keywords:
            continue
        normalized[name] = (priority, keywords)
    return normalized
```

设计理由：本中间件被设计为可接受任意序列化路由数据，而不仅是 `tool_search._routing_priority` / `_routing_keywords` 的产物。在实践中它是 no-op，但任何一侧的强转规则变化时必须同步另一侧。

丢弃规则：

- 名字为空 → 丢弃
- `priority` 非整数 → 默认 0
- `keywords` 不是 Sequence、是 `str`/`bytes`、为空 → 丢弃该项
- 单个 keyword strip 后为空 → 丢弃

### 最新真实用户消息

```python
@staticmethod
def _latest_user_message(messages: list[Any]) -> HumanMessage | None:
    for message in reversed(messages):
        if is_real_user_message(message):
            return message
    return None
```

`is_real_user_message` 来自 `deerflow.utils.messages`，它会跳过 `hide_from_ui` 的框架注入 HumanMessage（如 ViewImage、DurableContext 等），保证匹配的是真实用户输入而非中间件塞进去的隐藏上下文。

### 关键字匹配与排序 `_matched_names`

```python
def _matched_names(self, state: Mapping[str, Any] | None) -> list[str]:
    if not self._catalog_hash or not self._routing_index:
        return []
    messages = list((state or {}).get("messages") or [])
    target = self._latest_user_message(messages)
    if target is None:
        return []

    text = get_original_user_content_text(target.content, target.additional_kwargs)
    if not text:
        return []

    haystack = text.casefold()
    matched: list[tuple[int, str]] = []
    for name, (priority, keywords) in self._routing_index.items():
        if any(keyword.casefold() in haystack for keyword in keywords):
            matched.append((priority, name))

    if not matched:
        return []

    matched.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in matched[: self._top_k]]
```

匹配规则要点：

1. **短路条件**：没有 `catalog_hash` 或空索引直接返回空列表。
2. **取原始用户文本**：`get_original_user_content_text` 从 `additional_kwargs.original_user_content` 取出 `InputSanitizationMiddleware` 保留的原始文本，避免被 sanitize 后的关键词漏配。
3. **大小写不敏感**：双方都 `casefold()`。
4. **关键字命中即记**：一个工具只要任一 keyword 命中即入选；重复命中不加分。
5. **排序**：`priority` 高者优先，相同 priority 按工具名升序字典序。
6. **截断到 top_k**：`top_k` 全局来自 `tool_search.auto_promote_top_k`（默认 3，clamp 1..5）。

### 状态写入 `_stateUpdate`

```python
def _state_update(self, state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    names = self._matched_names(state)
    if not names:
        return None
    logger.debug(
        "McpRoutingMiddleware auto-promoted %d deferred tool schema(s) catalog=%s names=%s",
        len(names), (self._catalog_hash or "")[:8], names,
    )
    return {
        "promoted": {
            "catalog_hash": self._catalog_hash,
            "names": names,
        }
    }
```

关键点：

- 只写最小 payload：`catalog_hash` + 命中的工具名列表。
- **没有匹配则返回 `None`**，不触发任何状态更新，避免无意义的 checkpoint 写入。
- 提升状态是**作用域隔离的**：通过 `catalog_hash` 绑定到当前 catalog；陈旧的持久化 `promoted` 不会泄漏到改名/漂移后的工具（由 `merge_promoted` reducer 与 `DeferredToolFilterMiddleware` 共同保证）。

### 钩子实现

```python
@override
def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    return self._state_update(state)

@override
async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    return self._state_update(state)
```

同步与异步走完全相同的纯函数路径；CPU 成本低（关键词子串匹配），不需要 `to_thread`。

### 装配顺序守卫

模块底部导出了一个断言函数：

```python
def assert_mcp_routing_before_deferred_filter(middlewares: Sequence[AgentMiddleware]) -> None:
    """Fail fast if auto-promote would run after deferred schema filtering."""
    from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

    routing_idx = next((idx for idx, m in enumerate(middlewares) if isinstance(m, McpRoutingMiddleware)), None)
    filter_idx = next((idx for idx, m in enumerate(middlewares) if isinstance(m, DeferredToolFilterMiddleware)), None)
    if routing_idx is not None and filter_idx is not None and routing_idx > filter_idx:
        raise RuntimeError(
            f"McpRoutingMiddleware must be installed before DeferredToolFilterMiddleware "
            f"(routing index {routing_idx}, deferred filter index {filter_idx})"
        )
```

如果顺序颠倒，提升写入会发生在过滤之后，模型仍然看不到 schema，行为反直觉且难以调试。装配时 fail-fast 是必要的。

## 关键设计决策

**为什么只写状态、不过滤 schema？**
职责分离：路由中间件只决定"哪些工具应该被提升"，过滤中间件负责"隐藏未提升的 schema"。这样过滤逻辑可以被复用于其它提升路径（例如 `tool_search` 工具显式调用），且路由匹配逻辑变化不会影响过滤的执行路径。

**为什么用关键字而不是嵌入向量召回？**
PR1 阶段采用最简单的可序列化元数据（`priority` + `keywords`），无需向量索引与额外存储。子串匹配的召回率高、可解释、易于通过 `extensions_config.json -> mcpServers.<server>.routing` 与 `tools.<tool>.routing` 配置。

**为什么用 `catalog_hash` 作用域？**
防止跨版本或 catalog 漂移导致工具名被误用。例如：用户重新配置 MCP 后，旧名字"fetch_url"对应的工具可能变成了另一个实现；只有 `catalog_hash` 一致的 `promoted` 才会被接受。

**trade-off**：
- 子串匹配会带来假阳性（例如用户消息里出现"create"关键字可能误提升名为 `create_xxx` 的延迟工具）。但被提升只是让 schema 可见，不会强制执行；模型若不调用该工具就没有副作用。
- `top_k` 截断可能漏掉真正相关的工具，但默认 3 已足以覆盖典型场景；运维可以调高到 5。

## 与其他中间件的协作

- **必须先于 `DeferredToolFilterMiddleware` 装配**：由 `assert_mcp_routing_before_deferred_filter` 强制。
- **`DeferredToolFilterMiddleware`**：消费 `state["promoted"]`，在 `wrap_model_call` 中把已提升的 schema 放回 `request.tools`、未提升的继续隐藏。
- **`InputSanitizationMiddleware`**：保留 `additional_kwargs.original_user_content`，本中间件通过 `get_original_user_content_text` 读取真实用户输入，绕开 sanitize。
- **`SkillToolPolicyMiddleware`**：即使 schema 被 auto-promote 可见，仍然要经过 skill policy 的最终过滤；auto-promote 不会绕过权限边界。
- **`tool_search` 工具**：另一条显式提升路径；二者写入同一个 `state["promoted"]`，由 `merge_promoted` reducer 合并。
- 装配位置：Lead Agent 链中第 24 位，仅当 `tool_search.enabled` 且 PR1 路由元数据匹配到延迟工具时附加；子 Agent 通过 `build_subagent_runtime_middlewares` 复用。

---

# 3. DeferredToolFilterMiddleware

## 概述

在 `tool_search` 启用时，MCP 工具仍然被注册到 `ToolNode` 以便执行路由，但它们的 schema 不应该出现在 `bind_tools` 列表中，直到模型通过 `tool_search` 或 `McpRoutingMiddleware` 提升它们。本中间件在 `wrap_model_call` 阶段把仍然延迟的工具从 `request.tools` 中移除，并在 `wrap_tool_call` 阶段阻止对未提升工具的调用。

## 为什么需要这个中间件

### 场景痛点

当几十上百个 MCP 工具的 schema 全部绑定到模型时，工具选择的准确率严重下降（模型容易混淆相似工具），同时每次 LLM 调用的令牌消耗暴涨。更严重的是，模型可能随意调用未授权的工具，造成不可预期的后果。

### 为什么模型自身无法避免

模型的工具调用机制没有"忽略"或"延迟加载"的概念——所有绑定到 `bind_tools` 的工具 schema 对模型同等可见，模型有权调用其中任何一个。在 schema 层面设防是运行时独有的能力，模型无法自行裁剪其工具列表。

### 解决思路

在 `wrap_model_call` 层过滤掉未提升的延迟工具 schema，同时在 `wrap_tool_call` 层拦截对未提升工具的调用并返回错误提示，形成 schema 可见性与执行权限的双重防线。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py` |
| 实现的钩子 | `wrap_model_call` / `awrap_model_call` / `wrap_tool_call` / `awrap_tool_call` |
| 持久化 | 读 State（`promoted`，hash-scoped）；构造时注入 `deferred_names` + `catalog_hash` |
| 配置依赖 | `tool_search.enabled=true`；延迟工具集与 catalog_hash 在装配时计算 |
| State Schema | `AgentState` |

## 核心逻辑

### 构造

```python
def __init__(self, deferred_names: frozenset[str], catalog_hash: str | None):
    super().__init__()
    self._deferred = deferred_names
    self._catalog_hash = catalog_hash
```

- `deferred_names` 是一个**不可变**的 `frozenset[str]`，在装配时由 `assemble_deferred_tools` 一次性计算（fail-closed）。
- `catalog_hash` 与 `McpRoutingMiddleware` 共享同一个值，保证提升状态的作用域一致。

不使用 ContextVar，故意把延迟集合与 catalog_hash 在装配时固化，避免运行期动态漂移。

### 已提升集合 `_promoted`

```python
def _promoted(self, state) -> set[str]:
    promoted = (state or {}).get("promoted")
    if promoted and promoted.get("catalog_hash") == self._catalog_hash:
        return set(promoted.get("names") or [])
    return set()
```

关键守卫：**`catalog_hash` 必须匹配**。如果不匹配（陈旧的持久化 `promoted`、catalog 漂移、跨线程复用等），返回空集，所有延迟工具都保持隐藏。这是 hash-scoped promotion 的核心保证。

### 应该隐藏的集合 `_hidden`

```python
def _hidden(self, state) -> set[str]:
    return set(self._deferred) - self._promoted(state)
```

`延迟集合 - 已提升集合 = 当前应隐藏集合`。

### Schema 过滤 `_filter_tools`

```python
def _filter_tools(self, request: ModelRequest) -> ModelRequest:
    if not self._deferred:
        return request
    hide = self._hidden(request.state)
    if not hide:
        return request
    active = [t for t in request.tools if getattr(t, "name", None) not in hide]
    if len(active) < len(request.tools):
        logger.debug("Filtered %d deferred tool schema(s) from model binding",
                     len(request.tools) - len(active))
    return request.override(tools=active)
```

要点：

1. **快路径**：若 `self._deferred` 为空（没有延迟工具），直接返回原 request，零开销。
2. **快路径 2**：若当前没有需要隐藏的工具（全部已提升），返回原 request。
3. **使用 `request.override(tools=active)`**：不修改原 request 对象，返回新对象，符合 LangChain 中间件不可变约定。
4. **`ToolNode` 仍持有全部工具**：本中间件只过滤 model-bound schema，不影响 ToolNode 的执行路由能力。

### 工具调用阻止 `_blocked_tool_message`

```python
def _blocked_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
    if not self._deferred:
        return None
    name = str(request.tool_call.get("name") or "")
    if not name or name not in self._hidden(request.state):
        return None
    tool_call_id = str(request.tool_call.get("id") or "missing_tool_call_id")
    return ToolMessage(
        content=(f"Error: Tool '{name}' is deferred and has not been promoted yet. "
                 f"Call tool_search first to expose and promote this tool's schema, then retry."),
        tool_call_id=tool_call_id,
        name=name,
        status="error",
    )
```

防御层：即使 schema 被隐藏，模型仍可能（通过 few-shot、历史记录、记忆）尝试调用未提升的工具。此时不进入 `handler`，直接返回一个错误 `ToolMessage`，让模型知道应该先调用 `tool_search`。

### 钩子实现

```python
@override
def wrap_model_call(self, request, handler):
    return handler(self._filter_tools(request))

@override
def wrap_tool_call(self, request, handler):
    blocked = self._blocked_tool_message(request)
    if blocked is not None:
        return blocked
    return handler(request)

@override
async def awrap_model_call(self, request, handler):
    return await handler(self._filter_tools(request))

@override
async def awrap_tool_call(self, request, handler):
    blocked = self._blocked_tool_message(request)
    if blocked is not None:
        return blocked
    return await handler(request)
```

同步与异步路径对称。`wrap_model_call` 通过 `_filter_tools` 修改 request 后透传给 handler；`wrap_tool_call` 通过 `_blocked_tool_message` 判定是否短路返回错误。

## 关键设计决策

**为什么延迟集合在装配时固化？**
运行期 catalog 漂移会让"哪些工具是延迟的"变得不确定。固化在装配时保证一次 run 内的延迟集合稳定；若 MCP 配置变化，下次 run 重新装配即可。这与 MCP 缓存的 content-signature 失效检查配合，保证新 run 看到最新 catalog。

**为什么需要 `wrap_tool_call` 阻止？**
仅过滤 schema 不够：模型可能"记住"了历史回合看到的工具，或通过 few-shot 仿造 tool_call。必须在执行侧也设防，双保险。

**为什么 `catalog_hash` 检查放在 `_promoted` 而不是装配时？**
`catalog_hash` 在装配时已经固化在中间件实例上，但 `state["promoted"]` 可能来自历史 checkpoint（旧 catalog）。每次读 state 都重新校验 hash，确保陈旧提升状态不会让漂移后的同名工具获得 schema 可见性。

**trade-off**：
- 阻止错误消息依赖模型理解并调用 `tool_search`；弱模型可能陷入循环。但这是 tool_search 设计的必然代价。
- `deferred_names` 集合是静态的；若运行期 MCP 工具集变化（例如热加载），需要重建 agent。Gateway 的 MCP 配置 API 已经处理了 agent 失效逻辑。

## 与其他中间件的协作

- **必须在 `McpRoutingMiddleware` 之后装配**：见前节 `assert_mcp_routing_before_deferred_filter`。
- **`McpRoutingMiddleware`**：写入 `state["promoted"]`，本中间件读取。
- **`tool_search` 工具**：另一条提升路径；调用 `tool_search` 也会写入 `state["promoted"]`。
- **`SkillToolPolicyMiddleware`**：在它之后运行；即使延迟工具被提升，skill policy 仍然可能进一步过滤。两个过滤层叠加，权限不绕过。
- **`ToolNode`**：ToolNode 仍然能看到所有延迟工具以执行路由，本中间件只过滤 model-bound schema。
- 装配位置：Lead Agent 链中第 25 位；子 Agent 通过 `build_subagent_runtime_middlewares` 复用，每个 task run 拿到独立的 `ThreadState`，提升状态按 run 隔离。

---

# 4. SystemMessageCoalescingMiddleware

## 概述

把每次 LLM 调用请求中可能出现的多个 `SystemMessage`（来自 `request.system_message` 字段与 `request.messages` 列表）合并为单个前置 `SystemMessage`，通过 `request.system_message` 字段输出。这是针对严格 OpenAI 兼容后端（vLLM、SGLang、Qwen）和 Anthropic 的 provider 无关修复层，它们会拒绝非前置或非连续的 SystemMessage。

## 为什么需要这个中间件

### 场景痛点

多个中间件（`DynamicContextMiddleware`、`DurableContextMiddleware`）各自向请求中注入 `SystemMessage`，导致 `request.messages` 中出现多个分散的 SystemMessage。严格后端（vLLM、SGLang、Qwen、Anthropic）强制要求 SystemMessage 必须是消息列表的首条且不能中断，否则返回 HTTP 400 错误（如 `"duplicate system message"` 或 `"system message must be at the beginning"`）。

### 为什么模型自身无法避免

消息结构的组装由运行时在 LLM 调用之前完成，模型对 `request.system_message` 和 `request.messages` 的分隔以及 provider 侧的扁平化逻辑完全不可见。模型无法控制多个中间件注入 SystemMessage 的顺序与位置。

### 解决思路

在 `wrap_model_call` 阶段，将 `request.system_message` 与 `request.messages` 中所有 SystemMessage 合并为单个前置 SystemMessage 并通过 `request.system_message` 字段输出，使 provider 只收到一个前置的 system 块。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/system_message_coalescing_middleware.py` |
| 实现的钩子 | `wrap_model_call` / `awrap_model_call` |
| 持久化 | Per-request（只修改 request payload，**不**修改 checkpoint 中的 `state["messages"]`） |
| 配置依赖 | 无显式配置；装配为 Lead Agent 链第 26 位（固定）。子 Agent 链也在最后附加，保证 `DurableContextMiddleware` 注入的 SystemMessage 被合并 |
| State Schema | `AgentState` |

## 核心逻辑

### 触发与短路 `_coalesce_request`

```python
def _coalesce_request(request: ModelRequest) -> ModelRequest | None:
    in_msg_systems = [m for m in request.messages if isinstance(m, SystemMessage)]
    if not in_msg_systems:
        return None
    ...
```

**关键 no-op 优化**：如果 `request.messages` 中没有任何 `SystemMessage`，直接返回 `None`，调用方让原 request 透传：

```python
@staticmethod
def _maybe_coalesce(request: ModelRequest) -> ModelRequest:
    coalesced = _coalesce_request(request)
    if coalesced is None:
        return request
    return coalesced
```

为什么这是重要的优化？当 `request.messages` 中只有 `request.system_message`（静态系统 prompt）这一个 SystemMessage 时，它已经是唯一的、前置的，不需要合并。透传让 prefix-cache 命中率不被破坏（合并会改变内容指纹，使缓存失效）。

### 合并逻辑

```python
# Merge system_message (if any) + all in-messages SystemMessages.
parts: list[SystemMessage] = []
if request.system_message is not None:
    parts.append(request.system_message)
parts.extend(in_msg_systems)
```

顺序：先 `request.system_message`（静态系统 prompt），然后按 `request.messages` 中出现顺序追加所有 SystemMessage。

典型场景：`DynamicContextMiddleware` 在最新 HumanMessage 之前插入了一个 `<system-reminder>` SystemMessage；`DurableContextMiddleware` 注入了 `authority_contract` SystemMessage；午夜跨日时还会注入一条日期更新 SystemMessage。

### dynamic_context_reminder 去重

```python
# Deduplicate dynamic_context_reminder SystemMessages: only keep the last
# one (most recent date), drop earlier reminders.
reminder_indices = [i for i, p in enumerate(parts) if is_dynamic_context_reminder(p)]
if len(reminder_indices) > 1:
    keep_last = reminder_indices[-1]
    parts = [p for i, p in enumerate(parts) if i not in reminder_indices[:-1] or i == keep_last]
```

`is_dynamic_context_reminder` 来自 `dynamic_context_middleware`，识别由该中间件注入的日期/元数据提醒。

**为什么只保留最后一条？** 午夜跨日时，`DynamicContextMiddleware` 会注入新的日期 SystemMessage，但旧的 reminder 仍可能在历史中。合并后两条相邻的 `<current_date>` 块会自相矛盾，且原本分隔它们的中间回合在合并后已经消失，模型失去时间锚点。保留最新日期是最安全的选择——模型应只看到最新日期。

### 合并输出

```python
first = parts[0]
merged_kwargs: dict = {}
for p in parts:
    merged_kwargs.update(p.additional_kwargs or {})
merged = SystemMessage(
    content="\n\n".join(_flatten_content(p.content) for p in parts),
    id=first.id,
    additional_kwargs=merged_kwargs,
)

non_system = [m for m in request.messages if not isinstance(m, SystemMessage)]
return request.override(system_message=merged, messages=non_system)
```

关键设计：

1. **保留首条 SystemMessage 的 `id`**：通常是静态 `system_prompt` 的 id。下游消费者（如 `is_dynamic_context_reminder` 之外的标记检查器）依赖该 id 识别前置 system 消息。
2. **合并 `additional_kwargs`**：后写入者覆盖前写入者。`hide_from_ui`、`dynamic_context_reminder` 等标记保留到合并块上。
3. **内容用 `\n\n` 连接**：保持可读性；`_flatten_content` 处理 str 与 list-of-blocks 两种 content 形态（虽然 DeerFlow 的 SystemMessage 总是 str，但 helper 保证健壮性）。
4. **`request.override(system_message=merged, messages=non_system)`**：合并后的 SystemMessage 通过 `system_message` 字段输出；`messages` 中不再有任何 SystemMessage，由 model-call handler 在最后一步前置拼接。

### `_flatten_content` 健壮性

```python
def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)
```

DeerFlow 的 SystemMessage 内容总是 plain string，但 helper 处理多模态 list-of-blocks 形态以防万一。

### 钩子实现

```python
@override
def wrap_model_call(self, request, handler):
    return handler(self._maybe_coalesce(request))

@override
async def awrap_model_call(self, request, handler):
    return await handler(self._maybe_coalesce(request))
```

### 为什么用 `wrap_model_call` 而不是 `before_model`

文档注释里解释得很清楚：

> Uses `wrap_model_call` (not `before_agent`) so the merge runs on the final request payload — where `system_message` and `messages` are still separate fields — and never touches the persisted `state["messages"]`.

`before_model`/`abefore_model` 是修改 `state["messages"]` 的入口；而本中间件需要操作 **request payload**（`request.system_message` + `request.messages`），这是 `wrap_model_call` 才能看到的领域。同时，`request.system_message` 与 `request.messages` 在 model-call handler 内部才被扁平化为 `[system_message, *messages]`，所以本中间件必须在 handler 之前合并。

### 不修改 checkpoint 的意义

> Touches the per-request payload only (checkpoint state unchanged)

这是关键设计：合并只是请求时的视图变换，checkpoint 中的 `state["messages"]` 保持原样。所有扫描历史靠标记工作的中间件（memory builder、journal、summarization、`is_dynamic_context_reminder` 检测）都继续工作。

## 关键设计决策

**为什么 provider 无关，而不是在每个 provider 内修？**
原注释明确：

> Mirrors the per-request coalescing already done for Claude in `claude_provider._coalesce_system_messages` but at a provider-agnostic layer so every backend benefits from a single fix instead of per-provider patches.

Claude provider 已经有过类似的合并逻辑；但在 provider 层修意味着每个 provider（vLLM、SGLang、Qwen、Anthropic、OpenAI 兼容）都要打补丁。放在中间件层，一个修复覆盖所有后端。

**为什么静态 system prompt 不直接放 messages？**
`create_agent` 把静态 system_prompt 保留在 `request.system_message` 字段，只在 model-call handler 内部扁平化。这是 LangChain >= 1.2.15 的设计，目的是让 prefix-cache 命中率不被破坏（system prompt 在请求间稳定）。本中间件必须在合并时同时考虑这两个来源。

**为什么只保留最后一条 reminder 而不是全部合并？**
午夜间跨日注入的 reminder 块中包含 `<current_date>` 之类的 contradictory 语义。合并后它们相邻但语义冲突，模型可能困惑。保留最新日期让模型只看到当前状态。

**trade-off**：
- 合并后内容指纹变化，prefix-cache 命中可能下降。但 no-op 优化（无 in-messages SystemMessage 时直接透传）保留了典型情况下的缓存命中率。
- `additional_kwargs` 合并用 `dict.update`，后写入者覆盖前写入者；若两个 SystemMessage 都有同名 key，后者胜出。这在 DeerFlow 的当前中间件装配顺序下是安全的（`DynamicContextMiddleware` 在 `DurableContextMiddleware` 之前），但未来新增 SystemMessage 注入中间件时需要注意顺序。

## 与其他中间件的协作

- **`DynamicContextMiddleware`**：注入 `<system-reminder>` SystemMessage 作为最新 HumanMessage 之前的 triplet 首元素。本中间件把它合并进 leading system block。
- **`DurableContextMiddleware`**：注入 `authority_contract` SystemMessage。本中间件把它合并进 leading system block。
- **`is_dynamic_context_reminder`**：被本中间件复用，识别需要去重的 reminder 块。
- **`SummarizationMiddleware`**（子 Agent 链）：在它之后装配；子 Agent 的 `DurableContextMiddleware` + `DeerFlowSummarizationMiddleware` 都可能产生 SystemMessage，本中间件合并所有。
- 装配位置：
  - Lead Agent 链第 26 位，固定（非 optional）。
  - 子 Agent 链中通过 `build_subagent_runtime_middlewares` 最后附加（innermost），合并 `DurableContextMiddleware` 注入的 authority_contract SystemMessage，避免 strict 后端返回 duplicate-system 400（issue #4040）。

---

# 横向总结：四个中间件如何协同

| 维度 | ViewImage | McpRouting | DeferredToolFilter | SystemMessageCoalescing |
|------|-----------|------------|---------------------|--------------------------|
| 钩子 | before_model | before_model | wrap_model_call + wrap_tool_call | wrap_model_call |
| 改 State | 写 messages | 写 promoted | 只读 promoted | 不改 State（只改 request） |
| 装配顺序 | 23（vision） | 24（routing） | 25（filter） | 26（coalesce，固定） |
| 配置 gate | supports_vision | tool_search.enabled | tool_search.enabled | 无（始终启用） |

数据流视角：

1. 用户上传图片 → `view_image` 工具执行 → 写入 `viewed_images` metadata。
2. `ViewImageMiddleware` 在下次 LLM 调用前注入 base64 `HumanMessage`（带 `hide_from_ui`）。
3. 同一回合，若用户消息中提到 MCP 工具的关键字 → `McpRoutingMiddleware` 写入 `state["promoted"]`。
4. `DeferredToolFilterMiddleware` 在 `wrap_model_call` 中读取 `promoted`，把已提升的 MCP schema 放回 `request.tools`。
5. `DurableContextMiddleware` 与 `DynamicContextMiddleware` 可能注入 SystemMessage。
6. `SystemMessageCoalescingMiddleware` 在 `wrap_model_call` 中合并所有 SystemMessage 为单个前置块，通过 `request.system_message` 输出，`request.messages` 中不再有 SystemMessage。
7. model-call handler 扁平化 `[system_message, *messages]` 后发给 provider。

安全与稳定性保证：

- ViewImage：信任路径来自服务端，TOCTOU 校验，20MB 硬上限。
- McpRouting：catalog_hash 作用域，匹配仅看真实用户文本，top_k 截断。
- DeferredToolFilter：catalog_hash 双向校验，schema 过滤 + 调用阻止双保险。
- SystemMessageCoalescing：no-op 优化保留 prefix-cache，不修改 checkpoint，dynamic_context_reminder 去重避免午夜跨日矛盾。

四个中间件共同实现了 DeerFlow 的"视觉注入 + 工具延迟发现与路由 + 系统消息合规"三大运行期关注点，各自职责单一、边界清晰，通过 `state["promoted"]`、`state["viewed_images"]`、`request.override()` 等契约协作。
