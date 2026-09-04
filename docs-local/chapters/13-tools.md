# 13 · 工具系统:注册、内置工具与执行链

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写。
>
> 本章代码位于 `backend/packages/harness/deerflow/tools/`(装配)、
> `tools/builtins/`(13 个内置工具源文件)、`sandbox/tools.py`(文件/bash 执行族)、
> `community/*/tools.py`(搜索/抓取/浏览器等可插拔族),以及
> `agents/middlewares/` 里负责"把工具调用变成 ToolMessage"的那组中间件。
> 中间件机制总纲见第 06 章,错误恢复细节深链
> [`../middleware/middleware-03-error-handling.md`](../middleware/middleware-03-error-handling.md)。

## 0. 阅读地图与一句话结论

工具系统回答四个问题:**注册**(工具从哪来)、**内置清单**(有什么)、
**执行链**(调用如何变成 ToolMessage)、**可发现性**(模型怎么知道该调哪个)。

> 一句话结论:DeerFlow **没有运行时工具注册中心**——每次装配 agent 时由
> `get_available_tools()` 把 config 声明源 + 代码内建 + MCP 缓存 + ACP 四路
> 工具合成为一份最终列表,按 **name 去重**后直接交给 `create_agent(tools=...)`;
> "模型可见哪些 schema"在编译期定死,却可在请求期被两层机制再过滤
> (DeferredToolFilterMiddleware 隐藏未 promote 的 MCP schema、SkillToolPolicyMiddleware
> 按激活技能裁剪);真正执行时,每个工具调用穿过一条
> `wrap_tool_call` 洋葱链,产出带 `deerflow_tool_meta` 元数据的 ToolMessage,
> 而最外层的收据中间件再为它盖一枚确定性 receipt。

深链与兄弟章节:

- [第 06 章 中间件管道总纲](../chapters/06-middleware-pipeline.md) —— 35 链位语义地图;本章第 4 节只讲"工具执行"这一段洋葱的语义顺序。
- [中间件深链 03:错误处理](../middleware/middleware-03-error-handling.md) —— ToolErrorHandlingMiddleware 异常→错误 ToolMessage、`deerflow_tool_meta` 分类规则的完整状态机。
- 第 07 章(上下文工程)的信任分层、第 08 章(子代理)的 `task` 工具,与本章互相引用。

---

## 1. 全景:一次工具调用的生命周期

先给全链路,后面各节展开。以 Lead Agent 的一次完整工具回合为例:

```
模型输出 AIMessage{tool_calls:[{id,name,args}...]}
  └→ after_model 阶段:模型想调工具
       └→ LangGraph ToolNode:为每个 tool_call 执行工具
            └→ wrap_tool_call 洋葱(外层→内层):
                 ToolOutputBudget → ToolResultSanitization → SandboxMiddleware
                 → ToolReceipt(夹心 #13a)→ Guardrail/授权 → SandboxAudit
                 → ReadBeforeWrite → ToolProgress → ToolErrorHandling(夹心 #13b)
                 → 沙箱授权门 + 懒初始化 → 工具函数本体
       └→ 结果以 ToolMessage 写回 state(messages 通道)
  └→ 回到 model:带着全部 ToolMessage 再生成下一段
```

工具"返回结果"有三种形态,对应三种代码写法:

1. **返回字符串**(sandbox 文件族、社区工具、多数第三方工具)——由 LangGraph
   ToolNode 自动包成 `ToolMessage(content=..., tool_call_id=..., name=...)`。
2. **返回 `Command(update={...})`**(框架工具的主流写法:task、tool_search、
   present_files、view_image、ask_clarification、describe_skill)——工具**自己**
   把要落 state 的 `ToolMessage` 写进 `update["messages"]`,还可顺带更新
   `ThreadState` 的非消息通道(`promoted`、`viewed_images` 等),甚至用
   `Command(goto=...)` 改图控制流(ask_clarification 打断到 END)。
3. **抛异常**——被最内层 ToolErrorHandlingMiddleware 捕获,转成
   `status="error"` 的友好 ToolMessage,run 不崩,模型拿到错误继续决策。

三种形态在进入消息历史前,都会经过同一套元数据盖章(§4.3)。

---

## 2. 注册机制:没有全局 registry,只有"每次装配的合成"

`tools/AGENTS.md` 开宗明义:`get_available_tools(groups, include_mcp, model_name,
subagent_enabled)` 是**唯一合成点**。仓库里没有 `registry.register(...)` 式 API、
没有 toolsets 概念——"工具存在"由三件事共同决定:配置文件写了什么、代码在哪个
运行时条件下追加了什么、agent 装配时传入了什么开关。工具的生命周期是**装配期
一次性决议**,之后每个线程的每次 run 复用同一份工具列表(agent 按 config 缓存),
直到配置变化触发重建。

### 2.1 声明源 A:config.yaml 的 `tools[]`(主声明源)

`config.example.yaml` 里 `tools:` 是一张显式清单,每条是一个
`ToolConfig`(`config/tool_config.py`),只有三个必填字段 + 任意扩展字段:

```yaml
tool_groups:            # 逻辑分组,供组织与访问控制
  - name: web
  - name: file:read
  - name: file:write
  - name: bash
  - name: browser
  - name: knowledge

tools:
  - name: web_search        # 工具对外唯一名(模型看到的名字)
    group: web              # 归属分组
    use: deerflow.community.ddg_search.tools:web_search_tool   # 模块:属性 路径
    max_results: 5          # extra="allow",每个工具自取的自定义字段
```

- `use` 是 `module:attribute` 字符串,装配时经 `deerflow.reflection.resolve_variable(
  cfg.use, BaseTool)` **动态导入**并校验为 `BaseTool`——这就是"插件化"的底层:
  任何人写一个返回 `BaseTool` 的模块,配一行 YAML 即注册。
- 自定义字段(如 `max_results`、`timeout`、`base_url`)不进 schema,由工具实现
  在运行时经 `ToolRuntime` 配置读取(`sandbox/tools.py::_get_tool_config_int` 等
  helper),`group` 用于过滤,`name` 用于对模型展示。
- `tool_groups[]` 纯声明分组;自定义 agent(SOUL.md/config.yaml 里配置了
  `tool_groups`)装配时把该列表作为 `groups=` 传进 `get_available_tools`,只加载
  命中分组的 config 工具;默认 agent 不传,加载全部。

### 2.2 声明源 B:代码内建——固定三件套 + 条件追加

`tools/tools.py` 顶部的 `BUILTIN_TOOLS` 固定三件(永远在,除非被 `non_interactive`
剔除或去重挤掉):

| 工具 | 条件 | 一句话职责 |
|---|---|---|
| `present_files` | 恒在 | 把线程 outputs 目录里的产物文件"呈现"给用户(仅 `/mnt/user-data/outputs` 前缀,虚拟路径经 `resolve_runtime_user_id` 解析) |
| `ask_clarification` | 恒在;`non_interactive` 剔除 | 请求人工澄清,ClarificationMiddleware 拦截,产出 Human Input Card 并打断回合 |
| `review_skill_package` | 恒在 | 只读技能质量审查器(见 skills/public/skill-reviewer) |

其余内建工具全部是**条件追加**(代码即注册表,顺序敏感):

```python
if is_mcp_task_runtime_available():      builtin_tools += [list_background_tasks, cancel_background_task]
if include_upload_tool:                  builtin_tools.append(list_uploaded_files)      # 子代理装配传 False
if skill_evolution.enabled:              builtin_tools.append(skill_manage_tool)        # tools/skill_manage_tool.py
if subagent_enabled:                     builtin_tools += [task_tool, ...]
if is_subagent_batch_runtime_available():builtin_tools += [batch_task, batch_status, cancel_batch]
if model_config.supports_vision:         builtin_tools.append(view_image_tool)
```

外加两个不在列表里、按场景单挂的:`setup_agent`(仅 `is_bootstrap=True` 的引导
agent)、`update_agent`(仅自定义 agent 且**非 webhook 渠道**——webhook 由任意
外部评论者触发,不能把自改 `tool_groups`/SOUL.md 的工具暴露给它)。`task` 工具在
`create_deerflow_agent` 直接集成路径下会被**克隆并绑定**调用方的
`SubagentRuntime`,保持原名原 schema 不变。

### 2.3 声明源 C:MCP 缓存工具与 ACP

- MCP 工具在应用启动时经 `initialize_mcp_tools()` 初始化,进程内缓存
  (`mcp/cache.py`,以"配置路径 + 内容签名"作失效键);`get_available_tools` 每次
  装配都**重读磁盘上的 `extensions_config.json`**(`ExtensionsConfig.from_file()`)
  以拾取 Gateway API 在另一进程的改动,再取缓存。取出的每个工具打上
  `deerflow_mcp` metadata 标记(`tools/mcp_metadata.py`,单一事实源),可选携带
  server/transport 来源与 `deerflow_mcp_routing` 路由元数据。
- ACP(Agent Client Protocol)工具 `invoke_acp_agent`:仅当 `config.yaml` 的
  `acp_agents` 非空时按配置**现场构建**(`build_invoke_acp_agent_tool`),每个外部
  agent 一个入口。

### 2.4 合成与去重:`get_available_tools` 的 201 行

装配顺序 `config 工具 → builtins → MCP → ACP`,其间夹三道闸:

1. **host bash 守卫**:`is_host_bash_allowed(config)` 为假(本地沙箱默认)时,凡
   `group=="bash"` 或 `use=="deerflow.sandbox.tools:bash_tool"` 的 config 工具
   一律不加载——本地文件系统不是隔离边界,主机 bash 默认不给模型。
2. **sync 适配**:`_ensure_sync_invocable_tool` 给 async-only 工具挂
   `make_sync_tool_wrapper`(tools/sync.py),使同步调用方(embedded client/TUI)也能
   执行;wrapper 在一个共享线程池(`tool-sync`)里跑协程并转发 RunnableConfig。
3. **按 name 去重**:`seen_names` 集合保留**先到者**——即 config 装载工具优先于
   内建,内建优先于 MCP,ACP 垫底;重复者记 warning 丢弃(issue #1803:重名会让
   模型收到拼接 schema、路由却认另一个名)。去重前还会核对每个 config 条目的
   `cfg.name` 与工具对象自带 `.name` 是否一致,不一致以 `.name` 为准并告警。

返回值即 `create_agent(tools=final_tools, ...)` 的输入——**没有第二次注册**。
内存工具与 describe_skill 等"迟到工具"由 agent 装配点在授权过滤后补挂
(`late_tools`,见 §6)。

---

## 3. 内置工具族清单(`tools/builtins/` 实况)

`ls tools/builtins` 共 13 个源文件(其中 `__init__.py` 是导出面)。按文件逐一给
一句话定位;括号内注明绑定条件,不注者为无条件或已见 §2.2:

| 源文件 | 工具 | 一句话定位 |
|---|---|---|
| `present_file_tool.py` | `present_files` | 呈现 outputs 产物,路径校验只认 `/mnt/user-data/outputs/*`(虚拟/宿主双形态归一);面向 Web UI 走 artifact |
| `clarification_tool.py` | `ask_clarification` | 请求澄清:`missing_info/ambiguous_requirement/approach_choice/risk_confirmation/suggestion` 五类,v2 协议支持 `fields` 结构化表单卡;`return_direct=True` |
| `task_tool.py` | `task` | 委派子代理(§8 章主角):`prompt + subagent_type + acceptance_criteria + description`;bash 子代理仅在允许 host bash 或隔离 shell 沙箱时可用;结果自带 `[rN]` receipt 引文与验收清单 |
| `batch_task_tool.py` | `batch_task` / `batch_status` / `cancel_batch` | 显式**持久化**批量提交/进度/取消(仅装有 SQL 批量调度器时);大批结果落 owner 作用域导出,不进主上下文 |
| `background_tasks_tool.py` | `list_background_tasks` / `cancel_background_task` | 查询/取消后台子代理轮询任务(仅 MCP 任务运行时在位时) |
| `list_uploaded_files_tool.py` | `list_uploaded_files` | 列出当前线程 uploads 的已上传文件;子代理装配显式排除(它们没有独立 ThreadState) |
| `review_skill_package_tool.py` | `review_skill_package` | 只读审查一个技能包质量,产出 tag 中性化报告 |
| `view_image_tool.py` | `view_image` | 读图(仅模型 `supports_vision` 时绑定):校验 `/mnt/user-data/{workspace,uploads,outputs}` 前缀、magic bytes、≤20MB,state 只存轻量元数据,base64 由 ViewImageMiddleware 按需注入下一次请求 |
| `setup_agent_tool.py` | `setup_agent` | 仅引导(bootstrap)agent:持久化新自定义 agent 的 SOUL.md/config.yaml |
| `update_agent_tool.py` | `update_agent` | 仅自定义 agent 常态对话:原地自更新 SOUL.md/config.yaml(部分更新 + 原子写) |
| `tool_search.py` | `tool_search` | **延迟发现工具本身**(§6.2):查询不可变 `DeferredToolCatalog`,命中即 promote 进 `ThreadState.promoted` |
| `invoke_acp_agent_tool.py` | `invoke_acp_agent` | 调用外部 ACP 兼容 agent(config `acp_agents`);per-thread ACP 工作区挂 `/mnt/acp-workspace`(只读) |
| `__init__.py` | — | 导出面:与上表工具一一对应(`tool_search`/`invoke_acp_agent` 因需闭包/配置,不进静态导出) |

另有三个"同族不同目录"的延迟发现/演化工具:`describe_skill`(`skills/describe.py`,
技能延迟发现,§6.3)、`skill_manage`(`tools/skill_manage_tool.py`,技能演化开关打开
才绑定)、内存读写工具(`agents/memory/tools.py`,`memory.enabled` 时经
`_append_memory_tools_without_name_conflicts` 追加)。

### 3.1 第二族:文件与 bash(经 config 注册的沙箱执行族)

读/写/执行类工具**不在 builtins 目录**,而在 `sandbox/tools.py`
(`ls`/`read_file`/`glob`/`grep`/`write_file`/`str_replace`/`bash` 七个执行函数
文件顶部定义,实际由 config 的 `use: deerflow.sandbox.tools:*_tool` 条目注册)。
它们是抽象 `Sandbox`(`sandbox/sandbox.py`:execute_command/read_file/write_file/
list_dir/glob/grep)之上的模型可见壳:

| config 条目 | 分组 | 一句话定位 |
|---|---|---|
| `ls` | file:read | 列目录(宿主/沙箱路径映射,虚拟 `/mnt/user-data` 语义) |
| `read_file` | file:read | 读文件,支持行区间;ReadBeforeWrite 用它盖章内容哈希 |
| `glob` | file:read | 模式找文件,`max_results` 截断(默认 200) |
| `grep` | file:read | 单文件或目录树文本检索(默认上限 100) |
| `write_file` | file:write | 创建/覆写文件(需 ReadBeforeWrite 门) |
| `str_replace` | file:write | 精确串替换编辑(需 ReadBeforeWrite 门) |
| `bash` | bash | 执行 shell 命令;沙箱化(容器/远程)或 `sandbox.allow_host_bash: true` 才实际暴露 |

这些工具共用 `sandbox/tools.py` 一整层**路径/命令防线**:虚拟路径解析与掩码
(`mask_local_paths_in_output` 防止把宿主绝对路径漏给模型)、`..` 穿越拒绝、
bash 命令的 host 路径白名单与命令位置审计(配合 SandboxAuditMiddleware)、ACL
级校验。执行前统一过沙箱授权门 `ensure_sandbox_initialized`
(`authz/sandbox_authz.py` 的 `sandbox:execute` 授权)。

### 3.2 第三族:搜索 / 抓取 / 浏览器 / 图像(community,可插拔)

`community/` 下 24 个子包,每个都是一个 `config.yaml` 条目族,同名单工具互斥
(`web_search` 只能开一个 provider)。`config.example.yaml` 的默认激活三件:

- `web_search`(默认 ddg_search,DuckDuckGo 免 key;可换 tavily/brave/serper/
  serply/exa/searxng/firecrawl/groundroute/fastcrw/tencent_wsa/infoquest)
- `web_fetch`(默认 jina_ai reader;可换 browserless/crawl4ai/exa/firecrawl/
  groundroute/fastcrw/infoquest;SSRF 守卫 `allow_private_addresses` 默认关)
- `image_search`(默认 ddg;可换 serper/brave/infoquest)——模型生成前找参考图

浏览器族 `browser_automation`(`browser_navigate/snapshot/click/type/get_text/back/
screenshot/close`,group: browser)是有状态 agentic 浏览器,`[ref]` 数字索引寻址,
与只读 web_fetch/web_capture 互补;沙箱 provider 族(`aio_sandbox`/`e2b_sandbox`/
`boxlite`/`tenki`/`opensandbox`)提供隔离执行后端;`ragflow` 是知识库检索。全部
community 工具结果视作**远程内容**,进上下文前被 ToolResultSanitizationMiddleware
中和框架标签(§4.2)。

---

## 4. 工具执行链:从 tool_calls 到 ToolMessage

### 4.1 绑定期:模型看到什么,由"装配 + 请求期过滤"双重决定

`create_agent(model, tools=final_tools, middleware=...)`(langchain)把最终工具表
交给 agent:模型请求携带的 function schema = 工具表(去掉 deferred 隐藏集)。
但"模型可见性"不是纯编译期:`DeferredToolFilterMiddleware` 在每个模型调用前按
`ThreadState.promoted`(catalog_hash 作用域)决定哪些 MCP schema 出现在请求里;
`SkillToolPolicyMiddleware` 按激活技能的 `allowed-tools` 再裁剪。执行侧同一把锁:
模型即使喊出一个未授权/未 promote 的工具名,执行也会被拦截(ToolNode 找不到即
invalid tool call,或授权门拒绝)。

### 4.2 执行期:wrap_tool_call 洋葱(外层 → 内层)

LangChain 的 `AgentMiddleware.wrap_tool_call`/`awrap_tool_call` 提供"包裹下一次
调用"的原语:每个中间件拿到 `ToolCallRequest` 和一个 `handler`,可选择先处理后
放行、拒绝、改写结果。DeerFlow 在装配时把中间件列表按"外层 → 内层"语义排好,
工具调用从外往里穿过,结果从里往外逐层返回。与工具执行直接相关的各层(链位号
是第 06 章 35 槽地图的语义号,物理装配的 #13 夹心见 ch06 §2.3):

1. **ToolOutputBudgetMiddleware**(槽 2,最外层)——结果超预算则**外化**到磁盘
   (`.tool-results/`),上下文只留"类型化摘要 + read_file 引用"。
2. **ToolResultSanitizationMiddleware**(槽 3)——对**远程内容**工具结果
   (web_fetch/web_search/image_search/web_capture 及一切带 `deerflow_mcp` 标的
   工具)中和 `<system-reminder>` 一类框架标签;本地工具输出(bash/read_file)
   原样放行。改写层在返回路径上往 `additional_kwargs["deerflow_tool_transforms"]`
   追加声明条目(`append_tool_transform(kind, by, version)`,有序,最后一条即最终
   可见字节的制造者)。
3. **SandboxMiddleware**(槽 6)——懒获取的沙箱把 `sandbox_id` 以
   `Command.update` 附着到本工具结果上,使 state 落库(已有 sandbox_id 则不动)。
4. **ToolReceiptMiddleware**(槽 13a,9–13 段最外层)——给每个直接 ToolMessage
   及 `Command.update.messages` 里的 ToolMessage(含 task/present_files/
   view_image/tool_search 的自写消息)盖确定性 receipt;短路结果若未自盖章则回退
   `message.status`(§4.3)。
5. **GuardrailMiddleware / 授权适配**(槽 9)——Layer 2 执行前授权
   (`authorization.enabled`)与显式 guardrail provider;deny 转错误 ToolMessage。
6. **SandboxAuditMiddleware**(槽 10)——审计/分类 sandbox 命令(命令位置审计:
   `$(curl url)` 在命令位拦截、在值位放行);中危结果重建。防注入第二道,隔离边界
   仍是沙箱本身。
7. **ReadBeforeWriteMiddleware**(槽 11,默认开)——写前读门:`read_file` 在结果上
   盖内容哈希,`write_file` 覆写/追加与 `str_replace` 前校验最新哈希;无匹配则
   直接以 `recoverable_by_model=True` 的错误 ToolMessage 短路(消息即标记,摘要
   压缩天然使门失效,逼模型重读)。
8. **ToolProgressMiddleware**(槽 12,可选)——停滞守卫,包在 ToolErrorHandling
   外面,保证它读到的是已盖章 `deerflow_tool_meta` 的结果;按三类错误
   (可恢复/瞬态/立即)推进 ACTIVE→WARNED→BLOCKED 状态机。
9. **ToolErrorHandlingMiddleware**(槽 13b,最内层)——`GraphBubbleUp`(interrupt 等
   控制流)原样上抛;其余异常转 `status="error"` 的友好 ToolMessage("Error: Tool
   'x' failed with … Continue with available context…");**正常结果也经
   `normalize_tool_result` 盖章** —— 它是 `deerflow_tool_meta` 的生产者(§4.3)。
10. **工具本体**——最内 handler 先走沙箱授权门 + `ensure_sandbox_initialized`
    懒初始化,再执行工具函数;普通字符串返回被 ToolNode 包成 ToolMessage。

**注意分工**:槽 9–13 这段是"工具执行核心",各门可能短路或自产 ToolMessage,
所以 receipt 物理上排在授权/审计/写前读/停滞**之前**(最外层);而 budget/
sanitization 两个改写层更靠外,receipt 盖章发生在它们改写之前——receipt 记录的是
**执行真相**(原始返回值),改写后可见字节以 `deerflow_tool_transforms` 轨迹另行
声明。每个门的状态机细节请深链 middleware-03。

### 4.3 ToolMessage 上的三层元数据契约

落在消息历史里的每个 ToolMessage,`additional_kwargs` 可能带三层服务端元数据
(均为 `_SERVER_OWNED_*`,调用方不可伪造):

1. **`deerflow_tool_meta`**(`tool_result_meta.py`)——最内层 ToolErrorHandling
   统一盖的**结构化状态**:`status`(success/error/partial_success)、`error_type`
   (auth/rate_limited/transient/config/permission/no_results/not_found/internal/
   unknown,由 `_ERROR_RULES` 关键词规则推断)、`recoverable_by_model`、
   `recommended_next_action`(continue/rewrite_query/try_alternative/summarize/
   stop)。下游(ToolProgress、日志、审计)读这个键而非解析文案。
2. **`deerflow_tool_receipt`**(`tool_receipt.py`)——ToolReceiptMiddleware 盖的
   **确定性收据**:工具名、status、args/output 哈希(sha256 截 16)、字节数、
   时间戳;展示 id 按消息位置给 `r1..rN`(append-only 才稳定)。模型请求前从消息
   状态**派生**隐藏收据账本,超 2000 字符预算保新弃旧并留省略标记;task 子代理
   报告里的 `[rN]`/`[r2 write_file]` 引文即由该账本校验(receipt_verification,
   零 LLM 层,是子代理自报的交叉证据,§8 章展开)。
3. **`deerflow_tool_transforms`**(`tool_transform_meta.py`)——改写层在
   raw→visible 之间追加的**变换轨迹**(budget 外化、sanitization 中和等),
   有序、声明式,供观察者按事实分类而非嗅探输出措辞。

一句话浓缩:**内层执行真相(ToolErrorHandling 盖 meta)→ 外层事实记录(receipt)→
更外层改写(sanitization/budget,轨迹进 transforms)**——三层各司其职,互不覆盖。

### 4.4 错误路径与特殊流

- **异常 → 错误 ToolMessage**:ToolErrorHandling 兜底,内容限长 500 字符,错误被
  分类盖章后模型可在下一轮重试或换路(middleware-03 全展开)。
- **无效工具名/悬挂调用**:模型喊出不存在的工具、或被中断留下的 tool_call,由
  DanglingToolCallMiddleware 注入占位 ToolMessage,并把原始 provider 载荷存
  `additional_kwargs["tool_calls"]`(严格 provider 拒绝畸形参数时也能继续)。
- **打断/澄清**:`ask_clarification` 由 ClarificationMiddleware 在**执行前**
  拦截(它注册在链尾、最贴近模型侧),`Command(goto=END)` 中断回合等用户;
  `disable_clarification` 场景保留同轮兄弟调用。
- **循环/预算**:LoopDetection 与 TokenBudget 在 after_model 段工作,与工具执行
  正交(见 ch06)。

---

## 5. 命名与冲突:name 是唯一身份

工具名是**对模型的契约**:schema 绑定、ToolNode 路由、receipt 归属、技能策略的
`allowed-tools` 声明全部按 name。DeerFlow 用一套"先到先得 + 显式告警"的纪律:

1. **合成期去重**(tools.py §2.4):config > builtins > MCP > ACP;重名者 warning
   丢弃。这是 #1803 的根治——曾经模型拿到两个同名工具拼出来的 schema,运行时却
   只认其中一个,产生 "not a valid tool"。
2. **config name ≠ 工具 .name**:告警并以工具的 `.name` 为准绑定(配置只管
   分组/参数,真名在实现里)。
3. **内存工具名冲突**:`_append_memory_tools_without_name_conflicts`——内存工具
   与既有工具重名就跳过该内存工具,**不**动既有工具(避免误伤用户配置的 MCP
   工具)。
4. **技能策略的规范拼写**:SKILL.md `allowed-tools` 里 `Bash`/`WebFetch`/
   `Read`/`Write`/`Edit` 等可移植拼写映射到 `bash`/`web_fetch`/`read_file`/
   `write_file`/`str_replace` 等真实运行时名;未知标量名原样保留——命名有一层
   "规范别名"而不只是字符串巧合。
5. **外部名字不可信**:deferred 工具名渲染进 `<available-deferred-tools>` 前做
   HTML 转义,防止 MCP 服务器用精心构造的名字关闭标签块伪造框架指令。
6. **委派边界**:`create_deerflow_agent` 注入克隆工具时保持原名原 schema,让
   "用户工具去重"在模型契约层面稳定;webhook 渠道整体不给 `update_agent`
   (自改配置面只属于操作者可信入口)。

运维建议:给 MCP 服务器起名/选工具时先查默认工具表(bash、write_file、
web_search、present_files 等),同义工具(如第二个 web_fetch provider)只会被去重
丢弃并刷 warning 日志。

---

## 6. 可发现性:模型怎么知道"有这个东西、长什么样"

可发现性分**两条轴线**:工具侧与技能侧;每侧又分"全量注入(legacy) vs 延迟发现
(deferred)"两档。核心权衡:上下文占用 vs 首次调用的正确率。

### 6.1 基线:全量注入

`tool_search.enabled: false` 且 `skills.deferred_discovery: false` 时,一切朴素:
所有 config + builtin + MCP 工具 schema 全量进模型请求(几十上百个 MCP 工具时
每轮都烧 token 且选择准确率下降),技能以全元数据 `<available_skills>` 块注入
系统提示词。这是默认档,也是延迟发现要优化的对象。

### 6.2 tool_search:工具的延迟发现(默认关,`tool_search.enabled: true`)

打开后,装配走 `assemble_deferred_tools`(`tools/builtins/tool_search.py`),
**fail-closed**:

- 候选工具里凡带 `deerflow_mcp` 标的 MCP 工具全部**推迟**:收进不可变
  `DeferredToolCatalog`(工具元组 + `names` 快照 + 内容签名 `catalog.hash`,
  按 name/schema 排序哈希截 16 位),**不再绑定给模型**。
- 系统提示词只渲染 `<available-deferred-tools>` **名单**(名字 + HTML 转义,
  一行一个),模型知道"有这些工具,但没有 schema"。
- 追加 `tool_search` 工具本身:模型用三种查询取 schema——`"notebook jupyter"`
  正则关键词检索(名称命中权重 2、描述命中 1,上限 `MAX_RESULTS=5`);
  `"select:Read,Edit"` 点名精确取(不限量);`"+slack send"` 强制名字含 slack 再
  按其余词排序。查询是模型自由文本,非法正则降级为字面匹配,绝不抛错。
- 命中即 **promote**:`tool_search` 返回 `Command(update={promoted: {catalog_hash,
  names}, messages: [ToolMessage(schema JSON)]})`——schema 以 ToolMessage 形式落
  消息历史,同时把 promote 名单写进 `ThreadState.promoted`(按 catalog_hash 作用域,
  `merge_promoted` reducer 管理,防旧 catalog 的残留 promotion 污染新装配)。
- **请求期隐藏**:`DeferredToolFilterMiddleware` 在每个模型调用前,凡不在当前
  promoted 集合里的 deferred MCP schema 一律不出现在请求里;模型若绕过 tool_search
  直接喊名,会得到 invalid-tool 错误并被告知先 fetch。
- **自动提升(PR2)**:MCP 服务器在 `extensions_config.json` 里声明
  `routing.mode: "prefer"` + keywords 时,`build_mcp_routing_middleware` 把路由索
  引序列化进 `McpRoutingMiddleware`;它读最新一条 HumanMessage,命中关键词就先把
  匹配的 schema **自动 promote**(`tool_search.auto_promote_top_k`,默认 3,夹在
  1–5),并在提示词里渲染 `<mcp_routing_hints>`("用户请求涉及 X 时:先用
  tool_search 取 Y,再优先用 Y")。该中间件只改 promoted 状态,绝不执行工具。
- **装配失败即拒绝**:enabled 且有 MCP 候选却没能恢复 deferred 集,直接 raise
  ——宁可构建失败,也不悄悄把全量 MCP schema 绑给模型。

### 6.3 describe_skill:技能的延迟发现(默认关,`skills.deferred_discovery: true`)

技能侧同构实现(`skills/describe.py`,与 tool_search **互为镜像**:同一套查询语法、
同样的 `Command + ToolMessage` 返回形态、同样降级语义,但**不写 state**——技能
不需要 promote,metadata 到手即可 `read_file` 读 SKILL.md 本体):

- 系统提示词的 `<available_skills>` 全元数据块换成精简 `<skill_index>`(只列名);
- 追加 `describe_skill` 工具,返回命中技能的描述/allowed-tools/文件位置等
  结构化 metadata,模型据此决定是否 `read_file` 加载 SKILL.md;
- 开关独立于 `tool_search.enabled`;两套延迟发现可同时开(工具 MCP 侧 + 技能侧)。

### 6.4 框架工具在策略下的地位

授权与技能策略把 `tool_search`、`describe_skill`、`review_skill_package` 视为
**框架级发现基础设施**:restrictive 策略下它们保持可见,可揭示/提升 metadata;
但被延迟的业务工具要真正可见 schema、可执行,仍必须过激活技能的 `allowed-tools`
声明(策略中间件在请求期实时裁决)——延迟发现只解决"知道有什么",不授予执行权。
同理,`_NON_INTERACTIVE_DISABLED_TOOL_NAMES`(`ask_clarification`)等按运行语境
裁剪的名单,与发现机制正交。

---

## 7. 收束:从工具系统看整条链

把本章四问串起来就是完整心智模型:

```
声明(config tools[] + 条件 builtins + MCP/ACP)          ← 注册(§2)
  → get_available_tools 合成 + 按 name 去重              ← 唯一合成点(§2.4)
  → 装配期:deferred 分拣 / describe_skill / 授权 Layer1  ← 可见性第一次定稿(§6)
  → create_agent(tools, middleware):schema 绑定          ← 模型看到的工具表
  → 请求期:DeferredToolFilter / SkillToolPolicy 再过滤    ← 可见性第二次定稿(§6.4)
  → 执行期:wrap_tool_call 洋葱 → 沙箱门 → 工具函数        ← 执行链(§4.2)
  → ToolMessage + deerflow_tool_meta + receipt + transforms(§4.3)
  → 模型带着全部结果与隐藏收据账本进入下一轮
```

工具系统因此是**声明式 + 编译期收敛 + 请求期过滤 + 执行期元数据化**的组合:没有
中心 registry 就没有运行时注册竞态;name 去重与 deferred 机制解决大规模工具生态
的上下文与冲突问题;执行链上每一层门都只在自己该在的位置上,用 ToolMessage 承载
结构化事实(meta/receipt/transforms),让模型、日志、审计与子代理报告校验共享同一
份"执行真相"。

下一站:中间件深链 03 看错误 ToolMessage 的完整状态机;第 08 章看 `task` 工具如何
把 receipt 引文变成可验证的委派账本。
