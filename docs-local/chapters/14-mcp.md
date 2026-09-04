# 14 · MCP 集成与延迟工具路由

> 基于 DeerFlow 最新源码（本仓库 commit 2672e209，2026-09）编写。
> 本章代码位于 `backend/packages/harness/deerflow/mcp/`、`config/extensions_config.py`、`tools/builtins/tool_search.py`
> 与 `agents/middlewares/{mcp_routing,deferred_tool_filter}_middleware.py`，顶层配置文件为 `extensions_config.json`
> （模板 `extensions_config.example.json`）。与旧书结构的关系：hawkli 第十一章（MCP Server 集成）+ coolclaws
> 第十六章（MCP 扩展）的合并重写，但全部事实以本仓库最新代码为准。

## 本章导览

这一章讲两件事，它们共享同一条“MCP 工具从配置到模型”的流水线：

1. **MCP 怎么接进来**：`extensions_config.json` 声明 server，`MultiServerMCPClient`
   做发现，工具被打上 `deerflow_mcp` tag 进入 agent 工具面（§1–§4）。
2. **接了之后怎么“省着用”**：MCP 服务器一多 schema 就爆炸——答案是**延迟工具路由**：
   默认不把 MCP 工具 schema 绑给模型，只在系统提示词里列名字
   （`<available-deferred-tools>`），模型想要哪个就调 `tool_search` 现场取回完整
   schema；`McpRoutingMiddleware`（链 24）还能按用户话里的关键词**自动提升**（promote）
   最相关的 top_k 个；`DeferredToolFilterMiddleware`（链 25）负责在所有提升发生**之前**
   把未提升的 schema 从模型绑定里藏掉（§5——本章核心）。末尾两节是 `mcp_tasks`
   持久化运行时（§7）与安全模型（§8）。

一句话版本：

> MCP 是“工具即子进程/远端服务”的生态接入协议；DeerFlow 把每个 server 的每个工具
> 变成一个 LangChain `BaseTool`，再按 `tool_search.enabled` 决定是**全量绑给模型**
> 还是**延迟到按需提升**。延迟路径上，“哪些 schema 模型可见”完全由线程状态里的
> `promoted`（带 `catalog_hash` 防漂移）决定，链 24 负责预判、链 25 负责执行隐藏。

---

## 1. MCP 是什么、为什么 DeerFlow 需要它

### 1.1 协议层的一句话

MCP（Model Context Protocol）把“给模型用工具”标准化成三层角色：**host**
（DeerFlow）持有一个 MCP **client**，通过 `stdio`（本地子进程）或 `http`/`sse`
（远端）与 **server** 通信，暴露 `tools/list`、`tools/call` 等能力。DeerFlow
不自研协议栈，站在 `langchain-mcp-adapters` 的 `MultiServerMCPClient` 之上做
多 server 管理，增强都在“适配层”和“调度层”。

### 1.2 为什么是“接入生态”而不是“自己写工具”

DeerFlow 的内建工具（bash、web 搜索、文件操作…）是写死的；而 MCP 生态里有
GitHub、PostgreSQL、Playwright、Slack 等成百上千个现成 server。接入 MCP 等于
一行配置拿到一个领域的完整工具面，不必等 DeerFlow 发版：

```jsonc
// extensions_config.example.json（节选）
"mcpServers": {
  "github": {
    "enabled": false,
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" },
    "tool_name_prefix": true,
    "session_init_timeout": 60,
    "tool_call_timeout": 60
  }
}
```

注意措辞：DeerFlow 是 **MCP client（host）**，不是 server——它**消费**别人的
MCP server（本章不是教你写 server 暴露给 Claude Code）。`mcp/tasks/` 的
`McpTaskDriver` 协议兼有“对接任意 server 长任务”的意味，但那是驱动抽象，
不是暴露自身为 server。

### 1.3 为什么必须“延迟”：token 账与工具路由的动机

接入生态的代价是数量：5 个 server × 每家 20 个工具 = 100 个 schema，全部
`bind_tools` 进每次模型请求，动辄几万 token 的纯开销，还会稀释模型对
真正该用的工具的注意力（工具选择退化）。DeerFlow 的解法分两档：

- `tool_search.enabled: false`（默认）——全量绑定，与内建工具一视同仁。
- `tool_search.enabled: true`——只有 **MCP 来源**工具被延迟：schema 不进模型
  请求，模型只见系统提示词里的名字清单；想用某工具就调 `tool_search` 取回完整
  schema 并**提升**（promote），提升后才绑给下一轮模型调用。

“延迟”判定是**来源无关**的：只要工具带着 `deerflow_mcp` tag（加载时打的）
就可延迟，不限于固定前缀。它与技能的延迟发现（`skills.deferred_discovery` →
`describe_skill`）是**两套独立开关、两条独立装配**：技能是目录元数据、
工具是函数 schema，别混。

---

## 2. 配置：mcpServers 与 extensions_config.json

### 2.1 文件、解析优先级与热更新

MCP server 和技能共用顶层 `extensions_config.json`（仓库根，gitignored；
模板见 `extensions_config.example.json`），顶层四个键：
`middlewares`（扩展中间件类路径）、`mcpInterceptors`（鉴权拦截器工厂路径）、
`mcpServers`、`skills`。解析优先级与 `config.yaml` 同构：

1. 显式 `config_path` 参数；
2. `DEER_FLOW_EXTENSIONS_CONFIG_PATH` 环境变量；
3. 当前目录的 `extensions_config.json`；
4. 父目录（项目根）的 `extensions_config.json`。

两点关键语义（`config/extensions_config.py` + `config/AGENTS.md`）：

- **显式路径是 operator 断言**：显式 `config_path`/环境变量指向的文件不存在即抛
  `FileNotFoundError`（配置即声明）；只有搜索模式（3–4）找不到才返回“未配置”。
  MCP 缓存的陈旧检查是唯一例外：文件中途消失按“未配置”处理，缓存继续服务
  最后已知可用工具，不在请求热路径上抛异常。
- **`$` 开头的字符串按环境变量解析**（`$GITHUB_TOKEN`），密钥留在进程环境里。

热更新走 Gateway API：`PUT /api/mcp/config`（整体替换、整包校验）或
`PATCH /api/mcp/config`（只改一个 server 的 `enabled`）。写盘是进程内锁 +
sidecar 文件锁双重持有的 read-modify-write，经
`atomic_write_extensions_config`（同目录临时文件 + fsync + `os.replace`）
落盘，失败时旧配置原封不动。

### 2.2 一个 server 的字段地图（McpServerConfig）

`config/extensions_config.py` 的 `McpServerConfig` 定义了全部字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用 |
| `type` | `"stdio"` | 传输：`stdio`/`sse`/`http`；也接受 MCP 官方 schema 的 `transport` 别名（两者并存时 `type` 优先，见 `normalize_mcp_transport_alias`） |
| `command`/`args`/`env` | — | stdio 专用：子进程命令、参数、环境 |
| `url`/`headers` | — | sse/http 专用：端点与静态头 |
| `oauth` | `null` | sse/http：令牌端点流（`client_credentials`/`refresh_token`），自动刷新 |
| `user_auth` | `null` | sse/http：DeerFlow 用户 id → 凭据头值映射 |
| `headers_from_context` | `null` | sse/http：把 run 请求 `config.context.secrets` 的键映射为头 |
| `description` | `""` | 给人看的说明 |
| `routing` | `mode:"off"` | 软路由偏好：`mode`、`priority`(0–100，越界钳制)、`keywords` |
| `tools` | `{}` | 按**原始工具名**的逐工具覆盖（当前只有 `routing` 一种） |
| `tool_name_prefix` | `true` | 见 §3 |
| `session_init_timeout` | `60.0` | server 拉起超时（发现 = 子进程启动 + initialize + tools/list），`null` = 不限 |
| `tool_call_timeout` | `null` | 单次 stdio 调用与**所有传输**上的持久任务调用的超时；普通 http/sse 工具走传输层超时，配置了也只告警忽略 |
| `task_toolsets` | `[]` | 长任务契约，见 §7 |

`extra="allow"`：未知键不报错，向前兼容外部配置工具生成的 schema。

### 2.3 两栖加载：build_server_params

`mcp/client.py::build_server_params` 把 `McpServerConfig` 翻译成
`MultiServerMCPClient` 的参数字典：`stdio` 必须有 `command`（否则抛错），
`args`/`env` 可选带上；`sse`/`http` 必须有 `url`，静态 `headers` 的每个值
要先过 `mcp/headers.py::illegal_header_value_reason` 校验——传输层拒收的值
（换行、首尾空白、非 ASCII）在配置期就拒绝该 server 并记录原因，而不是等到
h11 把完整头值渲染进模型可见的异常（§8）。其它 transport 类型直接抛
`unsupported transport type`。

`build_servers_config` 只遍历 `get_enabled_mcp_servers()`；单个 server 配置
失败只丢自己（log error），不拖垮其它 server——单点故障隔离原则贯穿整个
MCP 子系统。

### 2.4 加载与缓存：懒初始化 + 内容签名失效

`mcp/cache.py` 维护进程级工具缓存：

- **懒初始化**：`get_cached_mcp_tools()` 首次调用（或陈旧后）才真正
  `get_mcp_tools()`；已在运行的事件循环里则另起线程跑 `asyncio.run`
  （兼容 LangGraph Studio 等无启动钩子的场景）。
- **失效不是比 mtime**：缓存记录“解析出的配置路径 + `(mtime, size, sha256)`
  内容签名”（`config/file_signature.py`，与主 `config.yaml` 共用同一 helper），
  用**内容相等性**而非 mtime `>` 比较——覆盖同秒编辑、mtime 回退（git checkout、
  `cp -p`、对象存储/网络挂载）与配置文件切换；Gateway 另一进程写盘后，多 worker
  部署也能在下一次调用感知并重建。
- 重置缓存时同步关闭会话池（`close_all_sync`），子进程与连接随旧配置回收。

`get_mcp_tools()`（`mcp/tools.py`）是整条加载链的主体：

1. 每次现读 `ExtensionsConfig.from_file()`（不走进程缓存——Gateway 是独立进程，
   必须保证读盘最新）→ `validate_mcp_task_config_snapshot` → `build_servers_config`；
2. 拉起 OAuth 初始头（发现阶段用静态头 + OAuth，见 §6）与工具拦截器；
3. `MultiServerMCPClient(servers_config, tool_interceptors=..., tool_name_prefix=True)`；
4. **逐 server 独立发现**（`asyncio.gather`），每个发现套 `session_init_timeout`
   超时——挂死的 stdio server 在 `wait_for` 取消时由适配器逐级清理进程树，
   不留孤儿进程；超时/失败只跳过该 server 并 warning；
5. 对所有产出的工具统一做**边界校验与打 tag**（§3.3），stdio 的包一层
   会话池包装（§4），最后给没有同步入口的工具补 `make_sync_tool_wrapper`
   （DeerFlowClient 同步流需要）。

---

## 3. 工具名前缀与来源路由

### 3.1 前缀防碰撞

两个 MCP server 都暴露 `search` 时，模型侧裸名会冲突。默认
`tool_name_prefix: true` 由 `MultiServerMCPClient` 在发现期统一加
`<server_name>_` 前缀，改名后工具名全局唯一，模型看到的名字就是路由键。
若某 server 工具名自带稳定命名空间（如统一 `xxx_*`），operator 可设
`tool_name_prefix: false`——此时该 server 改走
`langchain_mcp_adapters.tools.load_mcp_tools(..., tool_name_prefix=False)`
裸名发现。**前缀是展示层约定，不是路由依据**（见下）。

### 3.2 按“产出 server”路由，绝不按名字猜

`get_mcp_tools()` 用 `zip(servers_config.keys(), tools_by_server)` 把每个工具
钉死到**实际产出它的那个 server** 再决定包装与分组。为什么不扫名字？
`tool_name_prefix: false` 的 server 无前缀；即使有前缀，server 名互为前缀时
（`web` vs `web_scraper`，`web_scraper_search`.startswith(`web_`)）扫描会路由错。
会话池分桶、stdio 路径改写、`deerflow_mcp_source` tag 里的
`server_name`/`transport`，全部来自这份精确的来源分组。

### 3.3 加载边界：名字白名单、tag 与 routing 元数据

发现完成后每个工具依次过三关（`tools.py` 940 行主体）：

1. **名字校验**：MCP 工具名来自外部（可能敌意）server，必须匹配
   `^[A-Za-z0-9_-]+$` 才保留，否则 drop 并告警——工具名只应是函数标识符；
   被延迟（tool_search）的工具永远不进 `bind_tools`，提供方 API 的名字校验在
   它们身上不会执行，它们只活在系统提示词字符串里，伪造名字就能冒充框架结构（§8）。
2. **打 MCP tag**：`tools/mcp_metadata.py::tag_mcp_tool` 写入
   `metadata["deerflow_mcp"]=True` 与 `deerflow_mcp_source={server_name, transport}`。
   这个 tag 是整个系统的“我是 MCP 工具”判据（`is_mcp_tool()`）：延迟装配、
   来源路由、ToolResultSanitization 的远程内容中和都读它。`get_available_tools()`
   拿到缓存工具后还会再补一次 tag（幂等）。
3. **路由元数据落点**：此刻还能拿到 `source_name` 与**去掉前缀后的原始名**
   （`_raw_mcp_tool_name` 只剥适配器加的前缀；关闭前缀的 server 即使自带
   `<server_name>_` 开头也不误剥）。`resolve_effective_mcp_routing(server_cfg, original_name)`
   按**原始名**合并 server 级 `routing` 与 `tools.<original_name>.routing` 覆盖
   （覆盖只更新显式设置的键），`mode != "off"` 时把结果写入
   `metadata["deerflow_mcp_routing"]`。前缀不影响配置寻址：例子里
   `postgres.tools.query.routing` 配的是原始名 `query`，不是 `postgres_query`。

---

## 4. stdio 会话池：有状态 server 的命脉

### 4.1 为什么需要池

适配器默认每次调用新建会话——对 Playwright 这类有状态 server 意味着浏览器
状态（开过的页、填过的表单）随调用结束蒸发。`session_pool.py` 维护
`(server_name, scope_key)` 键控的持久会话池。比“要不要池”更硬的约束是
**生命周期纪律**：MCP `ClientSession` 基于 anyio task group，取消作用域必须在
**进入它的同一个 task** 退出，否则 `RuntimeError: Attempted to exit cancel
scope in a different task`；同步包装路径（`make_sync_tool_wrapper`）每次调用
开新 `asyncio.run` 循环，跨调用退出必然炸（issue #3379）。因此每个池化会话由
专属 `_run_session` owner task 持有：`__aenter__` 后把活会话交给调用方、自己
等 close 事件；所有关闭路径只**发信号**，`__aexit__` 永远由 owner task 执行，
池满按 LRU 逐出。

### 4.2 scope = (server, user:thread)，cwd/TMPDIR 钉进线程工作区

池键不是裸 thread_id：`scope_key = f"{user_id}:{thread_id}"`——文件系统隔离是
按 `(user_id, thread_id)` 的，两个用户若线程 id 碰撞，裸 thread_id 会让它们
共享一个有状态会话（跨用户状态泄露）。

stdio 子进程额外做两件钉扎（http/sse 无本地文件系统，整段跳过）：

- `cwd` 默认钉到该线程的 sandbox 工作区（operator 显式配了 `cwd` 则尊重）；
- `TMPDIR`/`TMP`/`TEMP` 默认钉到 `workspace/.mcp/tmp/`（`MCP_TMP_SUBDIR`，
  `.mcp` 是 DeerFlow 内部保留命名空间：产物可被工具返回、但被排除出
  workspace 变更摘要，与 `.git`/`node_modules` 同级处理）。

效果：Playwright 截图这类文件落进线程挂载的 user-data 树内，随后
`_convert_call_tool_result` 做**确定性路径翻译**（`/mnt/user-data/...` 虚拟路径）
时才有资格被改写；调用前后对工作区做轻量文件快照 diff，自由文本里的“裸文件名”
（`Saved as page-2026.yml`）只有能唯一对应本次调用产生的文件时才改写，
绝不瞎猜；user-data 树之外的路径一律不动。

### 4.3 断线恢复与超时

收到 MCP SDK 显式 `Connection closed` 或 anyio 关闭流错误时，只逐出
`(server_name, user_id:thread_id)` 这一个会话——且仅当池里注册的还是**失败的
那个 ClientSession**（旧并发调用的迟到错误不能逐出新替身）；失败调用照常报错、
不自动重放，下次重试自然新建子进程。协议超时、`isError=true` 结果、拦截器失败
都不驱逐健康会话。初始化超时（`session_init_timeout`，默认 60s）由
`asyncio.wait_for` 包住 `pool.get_session`，超时走 owner task 清理路径，不留
半吊子会话。

---

## 5. 延迟工具路由：schema 隐藏与两级提升（本章核心）

### 5.1 装配：谁被延迟、谁进 ToolNode

`tools/builtins/tool_search.py` 提供三个构建原语。`build_deferred_tool_setup`
从候选工具里筛出 `is_mcp_tool(t)` 的集合；空集（未启用 tool_search，或启用
但没有任何 MCP 工具幸存）返回空 setup `(None, frozenset(), None)`。非空则：

```python
# tool_search.py
catalog = DeferredToolCatalog(tuple(deferred))          # 不可变、可搜索
return DeferredToolSetup(build_tool_search_tool(catalog), catalog.names, catalog.hash)
```

- `DeferredToolCatalog`：工具元组 + `names` + `hash` + `search(query)`，
  纯搜索零变更（`frozen=True` 但**不加 slots**——`@cached_property` 要写 `__dict__`）。
- `catalog.hash`：工具按名排序后 `{name, openai_schema}` JSON 的 SHA-256 前 16 位
  ——目录唯一指纹。**不变量**：`tool_search_tool is None ⟺ deferred_names 空 ⟺ hash None`。
- `assemble_deferred_tools`（lead/subagent/embedded 三路共用）装配期 **fail-closed**：
  启用且有 MCP 候选、却没恢复出延迟集 → `RuntimeError`，拒绝裸绑 MCP schema。

装配完成后：`tool_search` **追加进 agent 工具列表**（ToolNode 里永远可调），
deferred 工具**留在 ToolNode 但被摘出模型绑定**。系统提示词渲染
`<available-deferred-tools>`：一行一个、HTML 转义的名字——名字来自外部
server，转义防止伪造名字闭合标签冒充框架结构。

### 5.2 链 25 · DeferredToolFilterMiddleware：隐藏的执行者

`agents/middlewares/deferred_tool_filter_middleware.py`——全链第 **25** 位
（lead-only 段，紧随链 24）。它构造时只收 `(deferred_names, catalog_hash)`
（build 期闭包，无 ContextVar），每次模型调用从**图状态**读提升记录：

```python
def _promoted(self, state) -> set[str]:
    promoted = (state or {}).get("promoted")
    if promoted and promoted.get("catalog_hash") == self._catalog_hash:
        return set(promoted.get("names") or [])
    return set()            # hash 对不上 → 视为零提升

def _hidden(self, state) -> set[str]:
    return set(self._deferred) - self._promoted(state)
```

两条防线：

1. **`wrap_model_call` 藏 schema**：`_filter_tools` 把 `request.tools` 里仍在
   hidden 集合的工具剥掉再交给下游 handler。ToolNode 保留全部工具（含
   deferred）用于执行路由——**模型不可见 ≠ 运行时不存在**。
2. **`wrap_tool_call` 拦直接调用**：模型即使没 schema 也可能凭
   `<available-deferred-tools>` 里的名字幻觉调用。对 hidden 名直接构造一个
   error `ToolMessage`（不是抛异常，run 继续）：
   `Error: Tool '<name>' is deferred and has not been promoted yet. Call tool_search first to expose and promote this tool's schema, then retry.`

`hide` 取自**当轮请求的 state 快照**，同一轮内 `tool_search` 提升后、后续模型
调用立即可见；它还实现 `release_policy_parameters()` 向观察者声明
`deferred_names`/`catalog_hash`/`promotion_scope:"graph_state_catalog_hash"`。

### 5.3 promoted 状态与 catalog_hash 防漂移

`ThreadState.promoted` 是 `{catalog_hash, names: []}`（`agents/thread_state.py`
的 `PromotedTools`），reducer `merge_promoted` 规则：

- 新值为空/None → 保留旧值（本节点没碰提升）；
- **`catalog_hash` 变了 → 整体替换、丢弃旧 names**——防的正是漂移：线程
  checkpoint 里持久着一个旧目录的裸名，若目录（工具增删/改名/schema 变了）
  漂移后仍按名放行，会暴露一个语义完全不同的工具；
- hash 相同 → 名字**并集去重保序**（`tool_search` 的多轮累积与链 24 的
  自动提升可以叠加）。

提升记录落在图状态、随 checkpoint 跨轮持久——状态可能比工具目录活得久，
所以必须带 hash scope；链 25 的 `_promoted` 与 reducer 用的是**同一把钥匙**
（构造期 catalog_hash == 状态里 catalog_hash）。

### 5.4 tool_search 工具本体：查询语法与 Command 提升

`build_tool_search_tool` 把 catalog 闭包进一个 `@tool`，入参带
`InjectedToolCallId`。三种查询形态：

- `"select:Read,Edit"` —— 点名精确取（**不设上限**：点名却静默丢子集=要了没给；
  仅排名模式有 `MAX_RESULTS=5` 上限）；
- `"notebook jupyter"` —— 正则/关键词搜索：名字或描述命中、名字命中权重 2，
  取前 5；**非法正则退化为字面量匹配**（查询来自模型，不平衡括号不该炸工具）；
- `"+slack send"` —— `+` 后首个 token 必须出现在**名字**里，剩余 token 参与
  排名（`+bare` 无第二名则返回全部 `slack*` 命中）。

命中即返回一个 `Command` 做两件事：完整 schema（`convert_to_openai_function`
JSON）进 ToolMessage 作为搜索结果；命中名单写进 `promoted:{catalog_hash,names}`
状态更新。模型读 schema、下一轮即可调用；未命中返回 `No tools found matching:...`
且 names 为空（空 names 在 `merge_promoted` 里视为“没动”，不清既有提升）。

### 5.5 链 24 · McpRoutingMiddleware：意图匹配自动提升

延迟解决了 token 账，却把负担推给模型：它得记得先 `tool_search` 再调用。
PR1（路由提示）让提示词出现 `<mcp_routing_hints>`（渲染于 `lead_agent/prompt.py`）：
只收 `mode:"prefer"` 且带 keywords 的条目、按 priority 降序；工具正被延迟时，
文案明确写“use `tool_search` to fetch `X`, then prefer that MCP tool”——不让
模型直接调一个 schema 已被藏掉的工具。

PR2 更进一步——`agents/middlewares/mcp_routing_middleware.py`（链 **24**，
`tool_search.enabled` 且路由索引非空时安装，由
`tool_search.build_mcp_routing_middleware` 构建），在 `before_model` 里**替模型
做预判**：

```python
for name, (priority, keywords) in self._routing_index.items():
    if any(keyword.casefold() in haystack for keyword in keywords):
        matched.append((priority, name))
matched.sort(key=lambda item: (-item[0], item[1]))   # 优先级降序，平局按名字
return [name for _, name in matched[: self._top_k]]
```

关键约束（docstring 与注释逐条钉死）：

- 只匹配**最新一条真实 HumanMessage**（净化前原文，排除工具结果回填的伪用户消息）；
- 命中即写 `promoted` 状态更新、交给链 25 生效；**不持有工具对象、不执行工具、
  不过滤调用**——职责只有“写最小提升状态”；
- 数量上限走全局 `tool_search.auto_promote_top_k`（默认 3，钳制 1..5）；
- 构造时只收**序列化扁平路由索引**且 `_normalize_index` 防御性重解析——构建器
  可摸 `BaseTool.metadata`，运行时中间件永远不碰工具对象；
- **顺序不变量**：`assert_mcp_routing_before_deferred_filter` 装配期断言链 24
  先于链 25——自动提升若跑在 schema 过滤之后，这轮就用不上，白提升。

链 24/25 在**子代理**同样装配（`build_subagent_runtime_middlewares` +
`SubagentExecutor`），只是 deferred 集是启动期技能策略过滤后的子集，各自
catalog/hash 独立。

### 5.6 一次完整的时间线

把链 24/25、tool_search 与状态 reducer 串起来（`tool_search.enabled=true`、
有 `routing.mode="prefer"` 的 server、用户说“帮我查一下订单表”）：

```
用户: 查一下订单表
 ├─ before_model 链24: 路由索引命中 postgres_query("订单表") → 写 promoted
 │    {hash, names:[postgres_query]} → merge_promoted: hash 一致则并集
 ├─ wrap_model_call 链25: hidden = deferred − promoted = ∅ → schema 全放行
 ├─ 模型调用 postgres_query(...)
 ├─ 模型想用另一个未提升的 github_* → wrap_tool_call 链25: 返回
 │    "deferred and has not been promoted yet" error ToolMessage
 ├─ 模型: tool_search("+github repo") → ToolMessage 载完整 schema
 │    + Command 写 promoted {hash, names:[github_*]} → 下一轮 bind 可见
 └─ 模型调用 github_* 成功
```

一个线程内提升是累积的（同 hash 并集），但**跨目录漂移即清零**（hash 变 →
整体替换）——旧线程 checkpoint 绝不会让改名后的工具裸奔。

---

## 6. 远程凭据：oauth / user_auth / headers_from_context

sse/http server 的鉴权有三个注入源（stdio 只吃 env/args，相关配置 warning
跳过），优先级：**静态 `headers` < `oauth` < `user_auth` <
`headers_from_context`**（注册顺序 = 覆盖次序，越后越靠内、胜出）。

- `oauth`：`client_credentials`/`refresh_token` 两种流，`OAuthTokenManager`
  自动刷新并注入 `Authorization`；渲染出的头值必须先过
  `illegal_header_value_reason`——拒收即 fail-closed，h11 不会把令牌回显进
  模型可见的工具错误。
- `user_auth`：一个共享 server 服务多租户，按 `user_id` 映射凭据头值
  （支持 `$ENV_VAR`）。默认 `on_missing:"deny"`：未映射用户或解析为空 →
  `ToolException`；`"passthrough"` 则退回静态头。静态 `headers` 仅供启动发现。
- `headers_from_context`：请求级密钥（`config.context.secrets`），多租户网关
  每个 run 自己带 key。块里只存**键名不存凭据**，因此 API 可明文返回、PUT
  原样往返；解析出的值同样必须过传输层校验。

三条铁律：注入值一律走 `apply_header_overrides` **大小写不敏感覆盖**（否则静态
`authorization` 与注入的 `Authorization` 双写，单值读取时静默颠倒优先级）；
`headers_from_context.headers` 配置期拒绝同一头的两种拼写；未映射密钥默认 deny
（静默放行 = 拿发现凭据的权限替租户调远端）。拦截器内读 run 上下文用
`request.runtime`（LangGraph 注入的 `ToolRuntime`），不用
`langgraph.config.get_config()["context"]`——工具调用内该键恒为 None。

---

## 7. 长任务：mcp_tasks 持久化运行时

MCP 工具若全是秒回还好；一遇到“提交报表生成任务，5 分钟后来取”就得回答：
远端任务 id 放哪、谁来轮询、Gateway 重启怎么办。答案是**不让 Agent 循环碰这些**
——独立 `McpTaskService` + SQL 持久化（`mcp_tasks` 表；`mcp_tasks.enabled`
默认 false、需 sqlite/postgres 后端，内存后端使服务不可用；startup-only，
磁盘改了要重启才生效）。

### 7.1 task_toolsets：提交可见、轮询藏起

`mcpServers.<server>.task_toolsets` 显式绑定原始 submit/status/cancel 工具名
（配置期校验：一个原始工具名只能占一个角色，`submit`≠`status`≠`cancel`）。
`_configure_task_tools_for_server` 在加载期做三件事：**status/cancel 从模型
视野里消失**；submit 换成 `_make_background_submit_tool` 包装——返回的
`StructuredTool` 保留原名/schema，docstring 追加“Submitted as durable
background task…”，调用后**只回本地 `task_id`**（远端句柄由服务持有）；
缺失绑定在加载期抛 `McpTaskConfigurationError`（fail-fast，工具面与后台调用
不允许分叉）。运行时可用时，管理工具 `list_background_tasks`/
`cancel_background_task` 一并挂出。

### 7.2 持久行、租约与轮询

`persistence/mcp_tasks/model.py` 的 `mcp_tasks` 行是一次完整的状态机：
规范化状态（`submitted`/`working`/`input_required`/`completed`/`failed`/
`cancelled`）、远端句柄映射、结果（错误截断 4000 字符、`result_artifact`/
`input_required` 限 64 KiB 且必须合法 JSON，超限按**永久协议失败**处理而非
截断改变语义）、三套独立进度字段（poll/cancel/notification 各有
`next_*_at` 与 lease 列），`uq_mcp_tasks_user_server_remote` 保证
(user, server, remote_task_id) 唯一——重复提交直接以冲突浮出，不去取消
已登记的合法任务。远端 id/任务名 ≤255、server 名 ≤128，对齐 SQL schema。

`McpTaskService` 轮询器按时认领到期行：写 `lease_owner` + `lease_expires_at`
（`lease_seconds` 默认 120），**过期租约即重启恢复机制**——结果只在 worker 仍
持有未过期租约时落库，迟到结果必须丢弃（owner token 匹配也没用）。cancel/poll
都带 fence：首个取消请求封掉在飞轮询租约、重复取消保住活跃取消租约（不会并发
双 cancel）；`input_required` 降速（60s），且**用户目前无法把答案提交回远端**
（协议单行道）。其它配置：`poll_interval_seconds:5`、`max_concurrent_polls:8`、
`tracking_degraded_after_errors:3`。Gateway 关闭会取消轮询器——挂死的外部
状态调用不能阻塞进程退出。

驱动侧（`mcp/tasks/ordinary.py`）：只信 MCP `structuredContent`，远端
`running`→`working`；`error_code=task_not_found` 或畸形结构 = 永久失败；
`isError=true` 的状态调用是**可重试**的调用失败（首段文本留作有界诊断），
真正的远端失败必须出现在正常结果里。`task_tool_caller.py` 为 stdio 恢复
`(server_name, user_id:thread_id)` 会话作用域；http/sse 调用保持临时，
初始化走 `session_init_timeout`、任务调用走 `tool_call_timeout`，支持在
Agent run 之外做 server 级 OAuth 刷新。

### 7.3 通知：幂等 run + dead-letter

任务进入 `input_required`/终态后停止轮询，转 `notification_status=pending`，
由 `launch_mcp_task_notification_run` 发起**幂等的 Agent 通知 run**：
`dispatch_attempt`（幂等键）与投递失败计数**分开记**，退避封顶指数、
**5 次失败 → dead_letter**；目标线程已删/不匹配 → 立即 dead-letter（绝不
重建或回收）；run 成功才算 delivered。信任边界刻意收口：通知指令是受信框架
文本，序列化后的远端事件按非受信数据——一个带 `<system-reminder>` 的任务
事件不能冒充框架上下文。

---

## 8. 安全模型

1. **只信 operator 配置。** `extensions_config.json` 能表达任意子进程启动
   （`command`/`args`/`env`）、任意远程 URL+头、任意 import 路径
   （`mcpInterceptors`、`middlewares`）。它**不是安全边界，是 operator 声明**；
   真正的边界是 operator 的机器权限 + 远端可达性——所以扩展中间件路径标注
   “trusted operator config”，`plugins:` 只放顶层 `config.yaml`（operator 写）、
   刻意不进 API 可写的 `extensions_config.json`。
2. **API 是受信输入的反面。** `PUT`/`PATCH /api/mcp/config` 是**不可信输入**，
   注册 stdio server 过 `_validate_mcp_update_request`：一句话总结——
   **只许指名道姓地让 `npx`/`uvx` 拉一个显式包名，不许带任何会执行代码的
   `args` 标志（`-c`/`-e`…）或无条件代码注入的 `env` 变量
   （`PYTHONPATH`/`PYTHONHOME`…）**；命令必须来自白名单
   （`{npx, uvx}` + `DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST`），含路径分隔符/
   空白/shell 元字符即拒，`npx`/`uvx` 还按各自 option 区域与 arity 精确切分。
   但这是**纵深防御不是信任边界**：`npx` 本就为拉取并执行远端包而生，管理员
   仍可指向自己发布的恶意包，修复之道不是把黑名单加长。
3. **模型可见即泄露面。** 工具名只许 `[A-Za-z0-9_-]`（防伪造名字在延迟列表
   伪造提示词结构）；凭据/头值过 `illegal_header_value_reason` fail-closed——
   换行会把完整值渲染进 h11 异常、再经 ToolErrorHandlingMiddleware 进
   prompt/checkpoint/trace，所以含 CRLF/首尾空白/非 ASCII 的凭据一律拒收且
   不回显；`user_auth`/`headers_from_context` 缺映射默认 deny。
4. **远程内容默认不可信。** MCP 工具结果在 `ToolResultSanitizationMiddleware`
   （链 3）的远程内容名单里——按 `deerflow_mcp` tag 判定而非工具名，服务器把
   抓取工具叫 `fetch_url` 也逃不掉中和（详见上下文工程章）。

---

## 小结与阅读路线

- 配置与加载：`extensions_config.json` → `config/extensions_config.py` →
  `mcp/{client,cache,tools}.py`；热更新靠“路径+内容签名”失效、单 server 故障隔离。
- 命名：`tool_name_prefix` 防碰撞只是展示层，来源路由永远按产出 server；有状态
  靠 stdio 会话池（scope=(server, user:thread)、owner-task 生命周期、
  cwd/TMPDIR 钉进线程工作区）。
- **延迟路由**：`tool_search.enabled` → 链 24 预判提升（意图匹配 top_k）→
  链 25 执行隐藏（未提升 schema 不绑、直接调用被拦）；`promoted` 带
  `catalog_hash` 防漂移。链位全景见第 06 章；子代理侧挂同名镜像（各自 hash）。
- 长任务与安全：`mcp_tasks` 独立持久运行时（租约/幂等通知/dead-letter）；
  信任边界始终是 operator 配置与管理员身份。

想要再往深挖：链位地图与中间件机制看第 06 章，MCP 结果的净化与信任分层看
第 07 章，子代理复用看第 08 章；代码侧从
`backend/packages/harness/deerflow/mcp/AGENTS.md` 进入——它是这一子系统的
权威契约文档。
