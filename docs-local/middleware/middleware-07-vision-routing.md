# 视觉注入 · MCP 路由提升 · 延迟工具过滤 · 系统消息合并（链位 23、24、25、26）

> 本篇解析 Lead Agent（及子 Agent 复用）链末段的四个中间件。它们不参与沙箱/审计/执行，而是站在**模型请求边界**回答四个不同的问题：多模态模型怎么「看到」图片、海量 MCP 工具 schema 怎么不把上下文塞爆、「工具被提升」由谁说了算、严格后端为何拒绝「不在开头」的 SystemMessage。四个中间件的共同气质是 **per-request 化**：能不进 checkpoint 的 payload 一律不进（base64、合并后的 system 块），只写最小状态通道（`promoted`），并在每次模型调用前自清扫/自重建。
> 源码相对路径：`backend/packages/harness/deerflow/agents/middlewares/`；链装配基线见 [`agents/middlewares/AGENTS.md`](../../backend/packages/harness/deerflow/agents/middlewares/AGENTS.md) 与 `lead_agent/agent.py::build_middlewares`。

## 本文件覆盖的中间件

| 链位 | 中间件 | 一句话职责 | 主钩子 | 装配条件 |
|---|---|---|---|---|
| 23 | `ViewImageMiddleware` | 多模态图片临时注入，base64 不进 checkpoint | `wrap_model_call` | 模型 `supports_vision` |
| 24 | `McpRoutingMiddleware` | 从最新用户文本猜意图，提前提升延迟 MCP 工具 | `before_model` | `tool_search.enabled` 且 PR1 路由索引 |
| 25 | `DeferredToolFilterMiddleware` | 延迟工具 schema 隐藏 / 拦截绕过调用 | `wrap_model_call` + `wrap_tool_call` | `tool_search.enabled` 且构建期有延迟工具 |
| 26 | `SystemMessageCoalescingMiddleware` | 合并所有 SystemMessage 到开头唯一一块 | `wrap_model_call` | 恒装配（lead + subagent） |

装配位置：lead 链在 `agents/lead_agent/agent.py::build_middlewares`（Memory 之后、SubagentLimit 之前）依序 append；subagent 运行时在 `agents/middlewares/tool_error_handling_middleware.py::_build_runtime_middlewares` 镜像同一顺序。**钩子选择规律**：要写图状态就用 `before_model`（McpRouting 的产物就是 `promoted`）；要改最终 request payload 就用 `wrap_model_call`（ViewImage 的 HumanMessage、Coalescing 的合并块都只在请求里存活）。看一个中间件用什么钩子，先问"它的产物住在哪里"。

**贯穿全篇的三个概念**：
- **catalog_hash**：构建期由 MCP 延迟工具目录（deferred catalog）算出的身份哈希。凡是"延迟工具"都带它，把任何持久化的提升信息绑定到"当时那批工具"。目录改名/漂移后哈希变化，旧提升即刻作废——防陈旧 promotion 把改过名的工具暴露给模型。
- **promoted**：`ThreadState` 上唯一的跨轮持久化"提升"通道，形状 `{catalog_hash, names}`。写方有两个：`tool_search` 工具执行（按需发现后提升）与 `McpRoutingMiddleware`（按用户文本自动提升）。reducer `merge_promoted`：同 hash 并集去重保序、hash 变则整体替换、None/空保留原值。
- **服务端持有元数据键**（`_SERVER_OWNED_MESSAGE_METADATA_KEYS`）：`hide_from_ui`、`deerflow_view_image_context` 等标记由 Gateway 在入站请求剥除，客户端伪造不了。中间件识别"自己注入的消息"靠「保留 ID 前缀 + 服务端标记」双标识，而非内容嗅探。

---

## 1. ViewImageMiddleware：让多模态模型"看见"图片，但绝不让 base64 落进 checkpoint

### 1.1 它解决什么问题

`view_image` 工具（仅当模型 `supports_vision` 时注册）做了严格的读取校验：路径必须落在允许的虚拟根（uploads/outputs）下、按魔数检测真实 MIME、≤20MB、stat 与 read 之间文件变化即拒绝。但它返回给模型的 ToolMessage 只是一句 `"Successfully read image"`——**工具调用本身不携带图像数据**。图像必须由框架在"工具完成后、下一次模型调用前"以多模态内容块补进去，模型才真正看得到。

最直观的做法是 `before_model` 注入 + `after_model` 用 `RemoveMessage` 撤走。这条路踩过坑（issue #4267）：base64 先写进图状态、进 checkpoint；若模型调用中途 run 被打断，`after_model` 的撤走代码永不执行，**这条最多 20MB 的 base64 消息就永久滞留在线程历史里**，此后每次模型调用、每次恢复都会把它重发给 provider，直到线程被压缩——既浪费 token 又留下"用户看过哪张图"的持久痕迹。即使正常完成，每个 checkpoint 也要序列化一份完整 base64 副本（issue #4138）。

设计结论：**图像 payload 只在 `wrap_model_call` 内临时构造、随请求生灭，从不作为 state update 返回**。checkpoint 只存轻量元数据 `{mime_type, size, actual_path}`（由 `view_image` 工具写入 `state["viewed_images"]`，reducer `merge_viewed_images` 合并；空 dict 可整体清空）。

### 1.2 钩子与执行时机

- 链位 23（lead `agent.py` ~613；subagent `tool_error_handling_middleware.py` ~411），两者都用**解析后的运行时 model_name** 查模型配置，`supports_vision=True` 才 append——模型不支持视觉时整个机制不存在。
- 钩子 `wrap_model_call` / `awrap_model_call`：每次真实模型调用前运行，但多数时候是 no-op——只有"最近一条 AIMessage 调用了 `view_image` 且其全部 tool_calls 都已返回"才注入。工具执行与注入之间隔着 ToolNode 的一次完整往返，注入发生在**紧接着的那次**模型调用。

### 1.3 内部实现逻辑

`_inject(request)` 是唯一入口，两个关键机制：

**(a) 先清扫、后重建。** 本中间件"拥有"图片上下文、每次调用都从 `viewed_images` 重建，所以先把 request 里已存在的、属于它的消息全部摘掉，再决定是否补新的：

```python
_IMAGE_CONTEXT_MESSAGE_ID_PREFIX = "view-image-context:"            # 保留 ID 前缀
_IMAGE_CONTEXT_MESSAGE_MARKER_KEY = "deerflow_view_image_context"   # 服务端标记

def _is_image_context_message(message) -> bool:
    # 双标识缺一不可: HumanMessage + id 以保留前缀开头 + 标记为 True
    return (isinstance(message, HumanMessage)
            and bool(message.id) and message.id.startswith(_IMAGE_CONTEXT_MESSAGE_ID_PREFIX)
            and message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True)

messages = [m for m in request.messages if not self._is_image_context_message(m)]
```

清扫目标不是理论上的：**旧版 `before_model`/`after_model` 对**（#4267 之前）checkpoint 过的线程可能残留一条"写进了 state 但撤走代码没跑"的图片消息；不摘掉它，这段 base64 会跟着这条线程的每一次后续请求重发到永远。摘除后若判定无需注入，返回 `request.override(messages=清理后)`；无残留时原对象原样透传。

双标识为何必要：**客户端消息不可能被误删**。`deerflow_view_image_context` 是服务端持有的元数据键，Gateway 剥除调用方伪造值，任何用户消息都不满足第二个条件；而标记出现**之前**的旧 checkpoint 里、不带标记的残留注入消息（只有 ID 前缀）无法与用户自写文本区分——它们**保留不摘**，只靠魔法字符串去重避免重复注入。

**(b) 注入判定（`_should_inject_image_message`），五重收紧**：
1. messages 非空；
2. 倒序找到最后一条 AIMessage；
3. 它的 `tool_calls` 里有名字为 `view_image` 的；
4. **该消息的全部** tool_calls 在其后都有对应 ToolMessage（`_all_tools_completed` 用 id 集合 `issubset` 判定）——若同一轮还并行发了别的工具且未返回，图片不提前注入，防止模型在别的工具结果缺席时就被喂图；
5. 顺扫该 AIMessage 之后的 HumanMessage，内容含 `"Here are the images you've viewed"` 或 `"Here are the details of the images you've viewed"` 则跳过——针对无标记旧残留的去重守卫（魔法字符串是跨 checkpoint 版本契约，文案不可改）。

**(c) 内容构造与磁盘读取。** `_create_image_details_message` 读 `state["viewed_images"]`；为空时给一条合法文本块 `"No images have been viewed."`（保持 content 为 blocks 结构）。每个条目先追加文本行 `- **<image_path>** (<mime_type>)` 再读文件：

```python
def _read_image_as_data_url(actual_path, mime_type, expected_size) -> str | None:
    file_path = Path(actual_path)
    if not file_path.exists() or not file_path.is_file(): return None
    current_size = file_path.stat().st_size
    if current_size != expected_size: return None   # TOCTOU: 查看后被改/增长 → 跳过
    if current_size > _MAX_IMAGE_BYTES: return None # 20MB, 镜像工具侧写时上限
    image_bytes = f.read()                          # OSError → None
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
```

- **信任假设**：`actual_path` 由服务端 `view_image` 工具写入（写时已校验虚拟根与魔数），存于 LangGraph 控制的 state，客户端输入到不了该字段——读取范围可信。
- **TOCTOU 用 `==` 而非 `<=`**：size 变大说明磁盘上已不是当初校验过的那张图（工具侧是 stat 后整读校验），一律跳过；读失败降级为文本块 `(file unavailable or changed on disk: <path>)`，模型仍收到合法内容而非畸形请求。
- 最终消息：`HumanMessage(id=f"view-image-context:{uuid4().hex}", content=blocks, additional_kwargs={"hide_from_ui": True, "deerflow_view_image_context": True, **provenance_kwargs(ContentKind.IMAGE_PAYLOAD, "view_image")})`。`hide_from_ui` 让 UI/IM/记忆都看不到；provenance 戳 `IMAGE_PAYLOAD` 供审计归因。
- 异步路径把整段 `_inject` 用 `asyncio.to_thread` 卸载到线程池——最多 20MB 的磁盘读 + base64 编码会阻塞事件循环，不能在 Gateway 的 async 热路径上裸跑。

### 1.4 时序图

```
Model ──tool_calls:[view_image]──▶ ToolNode ──▶ view_image 工具
                                                 ├─ 校验虚拟根 / 魔数 / ≤20MB
                                                 ├─ state["viewed_images"] += {path:{mime,size,actual_path}}
                                                 └─ ToolMessage: "Successfully read image"  ← 不含图像!
Model ◀───────── ToolMessage ─────── ToolNode

┌─ 下一次模型调用 (wrap_model_call) ────────────────────────────────┐
│ 1. 清扫: 摘掉「ID前缀+服务端标记」双命中的遗留图片消息             │
│ 2. 判定: 最后 AIMessage 含 view_image 且全部 tool_calls 已返回     │
│ 3. 构造: 文本块 + data:<mime>;base64,… 块 (按需读盘, 失败给文本降级)│
│ 4. append: HumanMessage(hide_from_ui, 双标识, provenance)         │
└────────────────────────────────────────────────────────────────────┘
Provider ◀── […, HumanMessage(base64 image_url)]   ← 模型终于"看见"
            该消息不进 checkpoint; 返回的 ModelResponse 也不带它
```

### 1.5 与邻居的关系
- 上游 **`view_image` 工具**：分工是"写时校验存元数据"对"读时重建 payload"。工具侧 20MB 上限被中间件常量镜像重查，因为中间件不能信任"查看与注入之间文件没被改过"。
- 下游 UI/记忆链路：`hide_from_ui=True` 的消息被 UI 渲染与记忆提取统一跳过。
- 与 McpRouting/Coalescing 不同：它没有状态写入、没有装配断言——纯请求级、无跨中间件依赖，唯一"邻居契约"是自己的双标识 + 魔法字符串去重（跨 checkpoint 版本）。

### 1.6 设计权衡
- **per-request 注入 vs 状态通道**：换来 checkpoint 干净、中断不残留；代价是每次模型调用重新读盘 + base64 编码（最多 20MB）、以及"同图多轮未清空会重复注入"。缓解：读取放 `asyncio.to_thread`、魔法字符串去重、reducer 提供清空语义（`viewed_images={}`）。
- **双标识 vs 单标识**：单靠 ID 前缀分不清"本中间件旧版残留"与"用户恰好以该前缀开头的消息"；单靠标记则标记发明前写入的旧 checkpoint 扫不掉。双标识 + 魔法字符串去重是兼容旧 checkpoint 的最小方案——**宁可旧残留多活一条，也不冒删用户消息的险**。
- **为什么是 HumanMessage 而非 ToolMessage**：ToolMessage 必须绑定 `tool_call_id`（是对某次调用的应答，语义不成立）；LangChain 多模态 `image_url` 内容块也只有 HumanMessage 能携带。
- **20MB 是上限不是目标**：真正防视觉 token 爆炸的是 TokenBudget/Summarization 层；本中间件只管"该给就给、不该给别给"。

### 1.7 源码阅读指引
`agents/middlewares/view_image_middleware.py`（297 行）。按 `wrap_model_call/awrap_model_call → _inject → _should_inject_image_message → _create_image_details_message → _read_image_as_data_url` 自上而下读；文件头常量区（`_IMAGE_CONTEXT_*`、`_MAX_IMAGE_BYTES`）就是本中间件全部的"外部契约"。配对读 `tools/builtins/view_image_tool.py`（写侧校验）与 `thread_state.py::merge_viewed_images`（空 dict 清空语义）。回归关注：中断恢复不重发 base64、旧 checkpoint 残留被清扫、并发工具未完成不提前注入。

---

## 2. McpRoutingMiddleware：从最新用户文本猜意图，把该用的延迟 MCP 工具"提前提升"

### 2.1 它解决什么问题

`tool_search.enabled` 时，MCP 工具全部进入**延迟状态**：ToolNode 仍持有可执行实例（调用路由需要），但 schema 不进 `bind_tools`——模型得先用 `tool_search` 发现并"提升"某个工具，其 schema 才可见、调用才放行。MCP 工具目录动辄几十上百个，schema 全量进上下文既塞爆 prompt 又拖慢首 token。

`tool_search` 是被动发现：模型得先想起有这回事、构造查询、等结果、再决定调用。对"服务器描述里带明确意图关键词"的工具（PR1 路由元数据：`routing.mode="prefer"` + `keywords`），框架可以在**模型开口之前**就替它提升好候选——这是自动路由（PR2）。

`McpRoutingMiddleware` 是自动路由的写入端：每次模型调用前扫描**最新一条真实用户消息**的文本，命中索引就把工具名写进 `state["promoted"]`。它**只写状态**：不持有 `BaseTool`、不执行任何工具、不过滤任何 tool_call（那是 DeferredToolFilter 的事）——刻意缩成一个纯数据消费者/生产者，构造时只收"序列化的路由索引"。

### 2.2 钩子与执行时机
- 链位 24（lead `agent.py` ~619；subagent 同序），在 DeferredToolFilter(25) **之前**——顺序是正确性的一部分（见 2.5 装配断言）。
- 钩子 `before_model` / `abefore_model`：它的产物是要落进图状态的 `promoted` 更新，是典型"状态型"中间件，与 ViewImage/Coalescing 的"payload 型"形成对照。
- 装配条件（`tools/builtins/tool_search.py::build_mcp_routing_middleware`）：`deferred_setup.catalog_hash` 非 None、deferred_names 非空、且延迟工具中至少有一个带 `routing.mode="prefer"` 与非空 `keywords`；任一不满足返回 None（整个中间件不装配）。构造时**一次性**把 `BaseTool.metadata` 的路由信息压平成 `{tool_name: {"priority": int, "keywords": [str,…]}}`，此后中间件与工具对象完全解耦。
- `top_k` 取全局 `tool_search.auto_promote_top_k`（默认 3，Field 校验 clamp 到 1..5）；工具级 `routing.auto_promote_top_k` 被忽略（log debug 提示）。

### 2.3 内部实现逻辑

构造时先做防御性归一化（`_normalize_index`）——本中间件接受**任意序列化路由数据**，不只 builder 的输出：空工具名丢弃；`priority` 非整数（TypeError/ValueError）默认 0；`keywords` 必须是 Sequence 且**不是** str/bytes（字符串会被当单字符序列迭代）；逐词 strip 后为空则整项丢弃。

每次模型调用（`_matched_names`）：

```python
if not self._catalog_hash or not self._routing_index:   # 短路 1: 无目录或无索引
    return []
messages = list((state or {}).get("messages") or [])
target = self._latest_user_message(messages)             # 倒序找最新"真实"用户消息
if target is None: return []
text = get_original_user_content_text(target.content, target.additional_kwargs)
if not text: return []
haystack = text.casefold()
matched = [(priority, name) for name, (priority, keywords) in self._routing_index.items()
           if any(kw.casefold() in haystack for kw in keywords)]
if not matched: return []
matched.sort(key=lambda it: (-it[0], it[1]))             # priority 高者优先, 同级按名升序
return [name for _, name in matched[: self._top_k]]      # 截前 top_k (默认 3)
```

- **"最新真实用户消息"**：`is_real_user_message` 过滤框架注入的隐藏 HumanMessage（`hide_from_ui`、summarization 标记等）——路由意图只能来自用户本人，注入上下文里的图片说明/数据块不应触发工具提升。
- **取原文而非 sanitize 后文本**：`get_original_user_content_text` 优先读 `additional_kwargs["original_user_content"]`（InputSanitizationMiddleware 消毒前保留的原文），避开消毒改写造成的漏配；没有才退回 content 文本。
- **匹配语义**：双方 `casefold()` 后做**大小写不敏感子串包含**；一个工具任一 keyword 命中即入选，重复命中不叠加；匹配对象是索引 keyword，不是工具描述。
- **状态写入（`_state_update`）**：命中才返回最小 payload；**未命中返回 None、不触发任何 state 更新**——避免无意义 checkpoint 写入，也保证"这次没命中"不会清掉 `tool_search` 先前提升的结果（reducer 语义：None/空保留原值）：

```python
return {"promoted": {"catalog_hash": self._catalog_hash, "names": names}}
```

落地靠 `merge_promoted`：同 `catalog_hash` 与已有提升并集去重（与 `tool_search` 的提升互不覆盖），`catalog_hash` 变化（目录漂移）则整体替换。**catalog_hash 是作用域键**：陈旧、持久化自上一次目录形态的 `promoted` 绝不会泄漏到改名/漂移后的工具上。

### 2.4 时序图

```
用户消息(原文) ──▶ McpRouting.before_model
                   ├─ is_real_user_message 定位最新真实用户消息
                   ├─ casefold 子串匹配 routing_index 的 keywords
                   └─ 命中 → state update {promoted:{catalog_hash, names[≤top_k]}}
                               │ (merge_promoted: 同 hash 并集 / hash 变整体替换)
                               ▼
                   DeferredToolFilter.wrap_model_call  ← 同一次模型调用读到新 promoted
                   └─ 隐藏集合 = deferred − promoted → 命中工具 schema 可见
                               ▼
                        模型直接调用新 schema（不必先 tool_search）
```

### 2.5 与邻居的关系（装配断言）
- **必须在 DeferredToolFilter(25) 之前**。`wrap_model_call` 逐层外包、排前的是外层；`before_model` 的派发发生在 wrap 链贴近真实模型调用的内层。若 filter 包在 routing 外层，filter 计算隐藏集合时 routing 这次的 `promoted` 还没写入，**自动提升当轮失效、要等下一次模型调用才生效**——行为错误且无任何报错。`assert_mcp_routing_before_deferred_filter`（装配后立即调用）把这种静默失效变成 fail-fast 的 `RuntimeError`；lead 与 subagent 两个 builder 在 append DeferredToolFilter 后都跑它。
- 与 **`tool_search` 工具**是互补双写方：tool_search 是"模型主动发现后提升"（执行时写 `promoted` 并返回 schema）；McpRouting 是"模型开口前替它提升"。二者经同一个 reducer 合并，互不覆盖。
- 与 **InputSanitizationMiddleware**：路由读它保留的 `original_user_content`——相隔 20+ 个位置却共享同一个 server-owned 契约键，是"原文保留"跨层复用的范例。
- 它**不消费** `promoted`（读方是 DeferredToolFilter），也不感知模型是否真用了被提升的工具——提升只是"给机会"，用不用由模型决定。

### 2.6 设计权衡
- **只写最小状态**：命中才写、payload 只有 `{catalog_hash, names}`，每轮无意义的 checkpoint 差分被消除；反面是路由无法"取消提升"——取消只能靠 hash 变化或 tool_search 结果覆盖。
- **只匹配最新真实用户消息**：旧轮次文本不参与，避免"用户上一轮提过某工具、这一轮无关请求也被喂满 schema"——这是延迟工具带宽预算的纪律。
- **top_k 全局 clamp 1..5**：默认 3 是"够用不泛滥"的启发式——太少则命中工具漏在外，太多则退化回全量注入。故意忽略工具级 `auto_promote_top_k`，防单工具声明把每轮注入量顶到不受控。
- **序列化索引隔离工具对象**：构造后不引用 `BaseTool`，路由数据可序列化、可测试、无对象生命周期泄漏；代价是索引装配时固化——运行期改 MCP 路由元数据需重建 agent。

### 2.7 源码阅读指引
`agents/middlewares/mcp_routing_middleware.py`（137 行）+ 两个外挂：`tools/builtins/tool_search.py::build_mcp_routing_middleware`（读 metadata、压平索引、决定是否装配）与 `config/tool_search_config.py::clamp_auto_promote_top_k`（1..5 边界、默认 3）。`deerflow/utils/messages.py` 的 `is_real_user_message` / `get_original_user_content_text` 决定"哪条消息、哪段文本"参与匹配。文件末尾的 `assert_mcp_routing_before_deferred_filter` 是理解顺序约束的钥匙，读装配测试时从它入手。

---

## 3. DeferredToolFilterMiddleware：schema 的"门卫"——隐藏与拦截双保险

### 3.1 它解决什么问题

延迟工具机制的核心矛盾：**同一个工具名，ToolNode 需要（执行路由），模型不需要（发现前看到 schema）**。不延迟的话，几十个 MCP 工具的描述随每次模型调用进入 prompt：占上下文、拖慢首 token、稀释模型对少量活跃工具的注意力。

DeferredToolFilter 是该机制的**读端执行者**：模型绑定阶段从 `request.tools` 摘掉仍未提升的延迟工具 schema；调用阶段拦住模型"绕过发现直接调用"的漏网请求。它和 McpRouting 是同一枚硬币的两面——一个写 `promoted`，一个按 `promoted` 决定藏什么。

### 3.2 钩子与执行时机
- 链位 25，`tool_search.enabled` 且构建期 `deferred_setup.deferred_names` 非空才装配（lead `agent.py` ~625、subagent `tool_error_handling_middleware.py` ~420）；空/None setup 是纯 no-op。
- 双钩子：`wrap_model_call`（每模型调用裁 schema）+ `wrap_tool_call`（每工具调用拦未提升者），各配 sync/async 四件套。
- **构造期注入、fail-closed**：`deferred_names: frozenset` 与 `catalog_hash` 在 `__init__` 一次固化（来自 `DeferredToolSetup`），**故意不用 ContextVar**——运行期动态漂移会让"该藏谁"不可预测；集合在装配一刻钉死，目录变化靠重建 agent 生效。

### 3.3 内部实现逻辑

提升集合的读取最微妙——**每次读都重新校验 catalog_hash**，而非装配时校验一次：

```python
def _promoted(self, state) -> set[str]:
    promoted = (state or {}).get("promoted")
    if promoted and promoted.get("catalog_hash") == self._catalog_hash:
        return set(promoted.get("names") or [])
    return set()          # hash 不匹配 → 视为零提升, 所有延迟工具保持隐藏

def _hidden(self, state) -> set[str]:
    return set(self._deferred) - self._promoted(state)
```

为什么每次读校验：`state["promoted"]` 是**从旧 checkpoint 恢复的持久化数据**，可能属于上一次目录形态（改名/漂移前）。装配时只验一次的话，运行中恢复的旧 `promoted` 会把漂移后的同名工具暴露出来。每次读校验保证：hash 不匹配（陈旧 promoted、catalog 漂移、跨线程复用）一律返回空集、全部隐藏——**fail-closed**。

```python
def _filter_tools(self, request: ModelRequest) -> ModelRequest:
    if not self._deferred:          # 快路径 1: 没延迟工具 → 原样返回, 零开销
        return request
    hide = self._hidden(request.state)
    if not hide:                    # 快路径 2: 当前没有要藏的 → 原样返回
        return request
    active = [t for t in request.tools if getattr(t, "name", None) not in hide]
    return request.override(tools=active)   # 返回新对象, 绝不改原 request
```

`request.override(tools=active)` 只影响 **model-bound schema**；ToolNode 手里的完整工具集原封不动——藏的是"模型可见性"，执行路由永远可用。

**调用阶段双保险（`_blocked_tool_message`）**：schema 藏得住，但模型仍可能靠 few-shot 示范、历史消息里残留的工具调用格式、甚至幻觉拼出未提升工具名来调用。执行前再拦一次：

```python
name = str(request.tool_call.get("name") or "")
if not name or name not in self._hidden(request.state):
    return None                       # 不在隐藏集合 → 放行给 handler
return ToolMessage(
    content="Error: Tool '<name>' is deferred and has not been promoted yet. "
            "Call tool_search first to expose and promote this tool's schema, then retry.",
    tool_call_id=..., name=name, status="error",
)
```

拦下的调用**不进 handler**（不执行），返回 `status="error"` 的 ToolMessage，提示先 `tool_search` 再重试——模型看到错误会自行纠正，而不是静默失败或触发重试风暴。

### 3.4 时序图

```
┌────────────── 模型绑定阶段 ─────────────────────────────────┐
request.tools ─▶ wrap_model_call ─▶ _hidden(state) = deferred − promoted
 (全部工具)                          │ hash 匹配? 是→names / 否→∅
                                    ├─ 快路径: 无延迟工具/无隐藏 → 原样透传
                                    └─ override(tools=active)  ← ToolNode 工具集不动
└──────────────────────────────────────────────────────────────┘
┌────────────── 调用阶段 (双保险) ─────────────────────────────┐
model 的 tool_calls ─▶ wrap_tool_call ─▶ name ∈ _hidden(state)?
                    ├─ 是 → error ToolMessage("Call tool_search first … retry") [不进 handler]
                    └─ 否 → 放行 handler (ToolNode 执行路由)
└──────────────────────────────────────────────────────────────┘
```

### 3.5 与邻居的关系
- **读 McpRouting(24)/tool_search 写的 `promoted`**：自动路由在 `before_model` 写的提升、工具发现写的提升，都经 `merge_promoted` 落进 `ThreadState.promoted`，本中间件在同一模型调用读取生效；装配断言保证写方永远先于读方（见 §2.5）。
- 与 **SkillToolPolicyMiddleware** 分工：技能策略裁"已激活技能的 allowed-tools"（行为 scoping）；本中间件裁"延迟 MCP 工具的发现前可见性"（带宽/注意力管理）。两者互不读对方状态，在模型绑定阶段各自过滤一遍。
- 与 **Authorization Layer 1**：延迟目录在授权过滤之后装配（`assemble_deferred_tools`），ToolNode 持有的延迟工具已经过授权；本中间件的隐藏/拦截是机制完整性的门卫，不是安全边界。

### 3.6 设计权衡
- **构造期固化 vs 运行期动态**：frozenset + 构造注入换确定性（同一 catalog 的所有模型调用看到同一隐藏策略），代价是 MCP 目录变更必须重建 agent——与缓存/目录失效模型一致，属预期。
- **hash 每次读校验 vs 装配校验一次**：读路径多一次字典比较，换来的保证是：checkpoint 里的 `promoted` 与当前目录不符时**最坏多藏（fail-closed）、绝不多露**——泄漏方向永远不被允许。
- **快路径优先**：`deferred` 为空、`hidden` 为空都原样透传；已装配但目录恰好全提升时也只付一次 set 差集的开销。
- **error ToolMessage 而非 raise**：让模型看见"为什么被拒、下一步做什么"（tool_search → retry），把越界调用变成可学习信号；raise 会被外层 LLMErrorHandling 当成框架故障。
- **双保险的必要性**：schema 过滤只约束"模型看到的工具清单"，管不住模型从历史/few-shot 里捡回旧工具名——执行侧必须再拦一次。两条防线共用同一个 `_hidden(state)`，不会出现"藏了 A 却放行 A"的不一致。

### 3.7 源码阅读指引
`agents/middlewares/deferred_tool_filter_middleware.py`（119 行）：先读 `_promoted/_hidden`（作用域语义），再读 `_filter_tools`（绑定阶段）与 `_blocked_tool_message`（执行阶段）。配对读 `thread_state.py::merge_promoted`（reducer）与 `tools/builtins/tool_search.py`（`promoted` 的另一写方 + `DeferredToolSetup` 来源）。`release_policy_parameters`（deferred_names 排序、catalog_hash、`promotion_scope="graph_state_catalog_hash"`）声明了它的行为策略。

---

## 4. SystemMessageCoalescingMiddleware：把散落的 SystemMessage 收拢成开头的唯一一块

### 4.1 它解决什么问题

官方 OpenAI API 容忍会话中段出现 system 消息，但**严格后端不行**：vLLM、SGLang、Qwen 与 Anthropic 会拒绝非开头、或多个不连续的 SystemMessage（报错形如 `"System message must be at the beginning"` / `"Received multiple non-consecutive system messages"`）。

DeerFlow 的 lead 链天然积累多条 SystemMessage，根源在 **DynamicContextMiddleware 的 ID-swap 注入**：框架权威的日期/元数据提醒不能伪装成用户输入（OWASP LLM01），它把首/末条 HumanMessage 原位替换成三元组、三元组头是一条 SystemMessage 提醒——消息列表中间于是出现 system。**跨午夜**时还会在最新用户消息前追加第二条"仅日期"的轻量纠正提醒（不重写首条，保护前缀缓存），同一线程可能同时存在两条日期提醒。另外 `create_agent` 把静态 system_prompt 单独放在 `request.system_message` 字段，只在 model-call handler 内部、最后一刻才扁平化成 `[system_message, *messages]`。

`SystemMessageCoalescingMiddleware` 是 **provider 无关的统一修复层**：每次请求把 `request.system_message` + `request.messages` 里所有 SystemMessage 合并成一条、放在开头。此前 Claude 提供商在 `claude_provider._coalesce_system_messages` 里做过 per-provider 修补；现在上移为对所有后端一视同仁的中间件——一处修复、处处受益。

### 4.2 钩子与执行时机
- 链位 26，lead 与 subagent builder 都**无条件 append**（`agent.py` ~637、`tool_error_handling_middleware.py` ~575）——四者中唯一没有装配条件的中间件。
- 钩子 `wrap_model_call` / `awrap_model_call`，**刻意不用 `before_model`**：合并对象是"最终 request payload 里分离的 `system_message` 字段 + `messages` 列表"——这是 model-call handler 内部才扁平化的领域；`before_model` 只能看到图状态。用 wrap 让它在扁平化发生前、对最终载荷动手。
- subagent 场景：builder 把**仅日期的上下文中间件（SubagentDateContextMiddleware）紧贴放在本合并器之前**，于是子代理的内建 prompt 与隐藏日期提醒仍以"一个开头的 system 块"抵达 provider。

### 4.3 内部实现逻辑

```python
def _coalesce_request(request: ModelRequest) -> ModelRequest | None:
    in_msg_systems = [m for m in request.messages if isinstance(m, SystemMessage)]
    if not in_msg_systems:
        return None            # messages 里没有 SystemMessage → 原请求透传 (no-op)

    parts = []
    if request.system_message is not None:
        parts.append(request.system_message)      # 1) 静态 system prompt 打头
    parts.extend(in_msg_systems)                  # 2) messages 里的 system, 按出现顺序

    # 跨午夜去重: 多条 dynamic_context_reminder 只留最后一条 (最新日期)
    reminder_indices = [i for i, p in enumerate(parts) if is_dynamic_context_reminder(p)]
    if len(reminder_indices) > 1:
        keep_last = reminder_indices[-1]
        parts = [p for i, p in enumerate(parts) if i not in reminder_indices[:-1] or i == keep_last]

    first = parts[0]
    merged_kwargs = {}
    for p in parts:
        merged_kwargs.update(p.additional_kwargs or {})   # hide_from_ui / reminder 标记保留
    merged_kwargs.update(provenance_kwargs(MIDDLEWARE_INJECTION, "system_coalescing"))
    merged = SystemMessage(
        content="\n\n".join(_flatten_content(p.content) for p in parts),  # 兼容 str 与 list-of-blocks
        id=first.id,                                      # 保留首条 id (通常是静态 prompt 的 id)
        additional_kwargs=merged_kwargs,
    )
    non_system = [m for m in request.messages if not isinstance(m, SystemMessage)]
    return request.override(system_message=merged, messages=non_system)
```

关键决策逐条拆：
- **no-op 优化**：`request.messages` 里没有 SystemMessage 时返回 None（原 request 透传）——此时只剩唯一静态 system prompt、本来就在开头，**合并反而会改变内容指纹、打碎 prefix-cache 命中**。零变化是默认态。
- **合并顺序**：静态 `system_message` 第一（承载 base prompt 身份），再按消息顺序接续所有动态 system。
- **跨午夜只留最新 reminder**：去重依据是 `is_dynamic_context_reminder(p)`——按 `additional_kwargs` 里的结构化标记识别（DynamicContext 写 `dynamic_context_reminder` 键），**不是正则扫内容**，用户消息里手写的 `<current_date>` 不会被误判。保留最后一条（最新日期）：若不丢弃，合并后两段相邻且矛盾的日期块之间已无任何隔开它们的轮次，模型只能猜哪条有效。
- **保留首条 `id`**：下游"按开头 system 消息的 id 识别前置系统提示"的消费者不受合并影响。
- **`additional_kwargs` 用 `dict.update` 后写覆盖先**：reminder 的 `hide_from_ui`、`dynamic_context_reminder` 等标记保留到合并块上（下游按标记扫描的中间件继续工作），并追加本中间件 provenance 戳（`MIDDLEWARE_INJECTION` / `system_coalescing`，AGENTS.md stamping 名单成员）。
- **`_flatten_content`**：兼容 str 与多模态 list-of-blocks 两种 content 形态（DeerFlow 的 SystemMessage 恒为纯字符串，属防御性兜底），块间 `\n\n` 连接。
- **checkpoint 不变**：合并只发生在 request 上；持久化的 `state["messages"]` 原样保留——所有扫历史做标记的中间件（memory builder、journal、summarization、`is_dynamic_context_reminder` 检测）看到的仍是注入前的结构。与 ViewImage 共用第一原则：**该留在 checkpoint 的结构一个字节都不动**。

### 4.4 时序图

```
checkpoint/state (永不变):
  messages = [H(user)①, AI, Tool, H(user)②<三元组>…, AI(view_image)…]
               三元组头 = SystemMessage(reminder 日期1)     ← DynamicContext ID-swap
  跨午夜后: 尾部附近另有一条 SystemMessage(reminder 日期2)  ← 第二条日期提醒
  (静态 system_prompt 单独在 request.system_message 字段)

        │ wrap_model_call (扁平化之前)
        ▼
 request.system_message(静态) ─┐
 messages 里的 SystemMessage ① ─┼─▶ parts ─ 去重: 只留最后一条 reminder (日期2)
 messages 里的 SystemMessage ② ─┘          │
                                           ▼
   merged SystemMessage(id=静态prompt的id, content=块1\n\n块2…, kwargs 并集 + provenance)
     ─▶ override(system_message=merged, messages=去 system)
                                           ▼
        handler 扁平化: [merged, …non_system]  → 严格后端: 唯一 system 在开头 ✅
```

### 4.5 与邻居的关系
- **DynamicContextMiddleware**：Coalescing 为它产生的"非开头 SystemMessage"善后——ID-swap 三元组与跨午夜追加都会制造严格后端的硬伤；两者经 `is_dynamic_context_reminder`（结构化标记而非内容嗅探）协作，去重语义与 DynamicContext 的日期注入同源。
- **装配位置 26**：在 DynamicContext(14)、DurableContext(17) 等所有可能注入 system 的中间件**之后** append，合并时能看到全部来源；子代理的日期中间件被刻意放在它紧邻之前（AGENTS.md），使"prompt + 日期提醒"以单个 system 块抵达。
- 与 **DurableContextMiddleware** 的差异：DurableContext 把静态权威规则注入为 SystemMessage、把不可信数据注入为 HumanMessage——可信部分走 system 通道、会被本中间件收进合并块；不可信部分留在 HumanMessage，保持"用户可影响内容不获系统权威"。

### 4.6 设计权衡
- **per-request 合并 vs 改 checkpoint**：合并只作用于请求载荷，换来三件事——checkpoint 结构对所有历史扫描者保持稳定、旧线程恢复无需迁移、合并逻辑天然幂等（每次从原始状态重算）。代价是每次模型调用多一次过滤 + 字符串拼接（廉价操作，可忽略）。
- **no-op 保 prefix-cache**：唯一静态 system 时零改动是刻意的性能设计——`content` 指纹不变才能命中 provider KV 缓存；"能不动就不动"是这里的最高优先级。
- **只留最新 reminder**：宁可丢弃旧日期（模型看到的必须是唯一、最新的时间锚点），也不保留矛盾信息；与 DynamicContext"同一天不重写首条、跨午夜轻量纠正"的策略呼应。
- **id 保留 = 身份保留**：合并块取首条 id（静态 prompt），下游"按 id 认 system 头"的代码零迁移——"合并内容但不动身份"的最小侵入。
- **provider 无关 vs per-provider 修补**：上移成中间件后 claude provider 的内联修补成为冗余（docstring 注明 mirrors）；一处修复覆盖所有后端，代价是每条请求多一层 wrap——被 no-op 快路径抵消。

### 4.7 源码阅读指引
`agents/middlewares/system_message_coalescing_middleware.py`（160 行）：`_flatten_content`（内容形态兜底）→ `_coalesce_request`（唯一算法，纯函数、易测）→ `_maybe_coalesce`/wrap 钩子（薄壳）。配对读 `agents/middlewares/dynamic_context_middleware.py::is_dynamic_context_reminder`（去重的判定依据）与 DynamicContext 的 ID-swap/跨午夜注入逻辑（系统消息的"生产者"）；对照读 claude provider 的 `_coalesce_system_messages`（被取代的 per-provider 修补）。

---

## 5. 四个中间件的共同设计主线（读完源码后的复盘）

1. **钩子跟着写目标走**：要落图状态（McpRouting → `promoted`）用 `before_model`；要改最终请求载荷（ViewImage 的图片消息、Coalescing 的合并块、DeferredToolFilter 的 schema 裁剪）用 `wrap_model_call`。
2. **checkpoint 最小化**：ViewImage 的 20MB base64 不进 state；Coalescing 的合并结果不回写 messages；McpRouting 只在命中时写 `{catalog_hash, names}` 三字段。中断恢复、前缀缓存、历史扫描中间件是这三条决策的共同受益者——**凡是能每请求重建的东西，都不值得持久化**。
3. **识别靠"保留前缀 + 服务端标记"双标识，不靠内容嗅探**：ViewImage 用它自清扫旧残留；Coalescing 靠 `dynamic_context_reminder` 结构化键去重；McpRouting 靠 `is_real_user_message` 排除注入消息。可伪造的内容从不作数，服务端持有的键才是事实。
4. **fail-closed 与 fail-fast 并用**：DeferredToolFilter 的 hash 每次读校验（陈旧即全藏）；`assert_mcp_routing_before_deferred_filter` 装配时把"顺序错了但不报错"变成 RuntimeError。隐藏方向宁多不少，顺序错误宁炸不静默。
5. **catalog_hash 是贯穿 24/25 的作用域键**：promotion 是持久化状态、工具目录却会漂移——把"谁被提升"绑定到"当时那批工具"的身份上，改名/增删后旧提升自动失效。这是"持久化数据必须携带自己的身份/有效期"的范例。

**源码阅读顺序建议**：先 `thread_state.py`（`merge_viewed_images` / `merge_promoted` 两个 reducer 的语义），再按链序 23→26 各读一个文件；最后回到 `agents/lead_agent/agent.py::build_middlewares` 的 append 段落（含装配条件与断言），把"何时存在、在谁之前"钉进记忆。每个文件的 `release_policy_parameters` / provenance 戳是"该中间件对外承诺了什么行为"的声明式摘要，值得最后回看。
