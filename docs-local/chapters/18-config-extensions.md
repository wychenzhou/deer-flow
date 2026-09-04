# 18 · 配置体系与扩展机制:双层配置、热重载边界与五类扩展贡献点

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写

本章把 DeerFlow 的"配置 + 扩展"合起来讲——它们不是两件事,而是同一件事的两面:
**配置文件就是扩展机制的入口**,扩展机制的每一条缝都开在配置模型上。读懂本章,你
就同时拿到两把钥匙:

1. **配置怎么分层、哪部分能热改、哪部分必须重启**(`config.yaml` 的运行期字段与
   `STARTUP_ONLY_FIELDS` 注册表,`extensions_config.json` 的"API 可写"语义);
2. **第三方扩展从哪进来、能碰什么、按什么顺序被装进 agent**(`plugins:` 列表 →
   `load_extensions()` → 注册表 → `compose_with_extensions()` 单点注入 → 顺序不变量校验)。

一句话版本:

> 两个配置文件按**信任边界**分工:`config.yaml` 由 operator 独写(只读挂载、进程级解析),
> 描述引擎与基础设施;`extensions_config.json` 被 Gateway API 在运行期读写,只描述
> MCP 服务器与技能开关等"数据型"扩展物。真正会**执行第三方代码**的 Python 扩展
> (`plugins:`)被刻意锁死在 `config.yaml` 里、启动期装载一次、绝不放进 API 可写的文件。
> 配置的热改边界不是"整文件可热"或"整文件不可热",而是逐字段注册:per-run 字段下一条
> 消息生效,基础设施字段必须重启,权威清单在 `config/reload_boundary.py`。

---

## 1. 双层配置:两个文件、一条信任边界

仓库根目录躺着两个配置文件(均 gitignored,以 `*.example.*` 为模板拷贝):

| 文件 | 模板 | 谁写 | 挂载 | 解析器 |
|---|---|---|---|---|
| `config.yaml` | `config.example.yaml`(config_version 39) | operator 手工 / `make setup` | Docker 中 `:ro` 只读 | `AppConfig.from_file()`(YAML) |
| `extensions_config.json` | `extensions_config.example.json` | operator 手工 **+ Gateway API 运行期写** | Docker 中读写 | `ExtensionsConfig.from_file()`(JSON) |

这一分工不是文件格式偏好,而是**代码执行信任边界**:

- `extensions_config.json` 由 Gateway 的 HTTP 接口在运行期改写(`PUT`/`PATCH
  /api/mcp/config`、MCP 服务器启用开关、技能 enable/disable)。凡是"读了这个文件就会
  import 代码"的东西都不能放进来——否则一个能写配置的 API 就等价于远程代码执行。
- `plugins:`(Python 扩展列表)因此只能住在 `config.yaml`:`AppConfig.plugins` 的字段
  描述与 `reload_boundary.STARTUP_ONLY_FIELDS["plugins"]` 都明确写着——
  **导入 Python 入口点是 operator 控制的代码执行边界**,该列表只信 operator。
- 同样的理由,`config.yaml` 里的 `extensions.middlewares`(配置声明的中间件类路径)被
  middlewares/AGENTS.md 明确要求:Gateway 的技能/MCP 写接口**必须保留该字段但不新增
  写路径**(`to_file_dict()` 序列化时原样带过),因为类路径实例化任意代码。

两个文件的**解析优先级**完全同构(显式参数 > 环境变量 > 当前目录 > 仓库根/后端遗留
位置):

1. 显式 `config_path` / `config_path` 参数
2. `DEER_FLOW_CONFIG_PATH` / `DEER_FLOW_EXTENSIONS_CONFIG_PATH` 环境变量
3. 调用方项目根目录的 `config.yaml` / `extensions_config.json`(向后兼容还会找
   遗留的 `mcp_config.json`)
4. 后端目录与仓库根的旧位置(monorepo 兼容)

优先级 1–2 是 **operator 断言**:既然你指名要某个文件,文件不存在就必须
`FileNotFoundError` 响亮失败(坏挂载、拼错路径、生产配置被删都不该静默降级)。
只有纯搜索模式(3–4)找不到时,`config.yaml` 报错、`extensions_config.json` 返回
"空配置"——扩展本来就是可选项,不存在"从未配置过"不该是错误(唯一例外是
`deerflow.mcp.cache` 的陈旧检查:它把该异常就地吞掉当作"未配置",让运行期工具缓存
降级为服务最后已知可用的一批工具,而不是在每请求热路径上炸出去)。

### 1.1 两个模型在 AppConfig 里汇合

`AppConfig` 上有一个易被忽略的字段 `extensions: ExtensionsConfig`(第 234 行附近):
`from_file()` 在解析 YAML 之后、Pydantic 校验之前,会单独调用
`ExtensionsConfig.from_file()` 读取 JSON,**把两份配置合并进同一个 `extensions` 块**:

```python
yaml_extensions = config_data.get("extensions")          # config.yaml 里可选写 extensions:
extensions_config = ExtensionsConfig.from_file()         # 真正的主源:extensions_config.json
extensions_data = extensions_config.model_dump(by_alias=True)
if isinstance(yaml_extensions, Mapping):                 # YAML 显式声明的字段覆盖 JSON
    yaml_extensions_config = ExtensionsConfig.model_validate(yaml_extensions)
    extensions_data.update(yaml_extensions_config.model_dump(by_alias=True, exclude_unset=True))
config_data["extensions"] = extensions_data
```

规则是:**config.yaml 里显式写了的字段赢**——注释写明理由是"这些值属于主配置的热重载
契约"(见 §3)。实际效果:绝大部分部署根本不在 config.yaml 写 `extensions:`,JSON
文件原样上浮;只有 operator 想用 YAML 的注释能力钉死某项(比如强制某个 middleware
路径、临时覆盖某个技能开关)时才写,且会覆盖 API 写入的 JSON 值。

## 2. config.yaml:解析管线与 `$VAR` 递归展开

### 2.1 解析管线

`AppConfig.from_file()` 的完整处理顺序值得背下来,后面每一节都是它的展开:

```
resolve_config_path()            # §1 的优先级;找不到即 FileNotFoundError
yaml.safe_load()                 # or {}
_check_config_version()          # config_version 闸门(见 2.2)
resolve_env_variables()          # $VAR 递归展开(见 2.3)
_apply_database_defaults()       # database 段缺失时写入 CONFIG_FILE_DATABASE_DEFAULTS
ExtensionsConfig.from_file()     # 读取 JSON 并合并进 extensions 字段(见 1.1)
model_validate()                 # Pydantic 校验 + 钩子
_apply_singleton_configs()       # title/summarization/memory/... 单例配置同步
```

进 Pydantic 之前还有两个"宽容性"钩子值得一提:

- **`_drop_null_config_sections`**:注释掉某个顶层段落的全部条目(example 文件里
  `models:`、`memory:` 整段被注释就是这个形态)会让 PyYAML 解析出 `None`。该
  before-validator 把值为 `None` 的键全部丢弃,让字段落回 `default_factory`——否则
  文档化的 `cp config.example.yaml config.yaml` 首跑流程会直接炸出
  "Input should be a valid list" 的晦涩报错。没有默认值的必填段(`sandbox`)仍会报错,
  这符合直觉:没有可回退的东西。
- **`_build_name_indexes`**:校验后构建 `name -> ModelConfig/ToolConfig/ToolGroupConfig`
  三个 O(1) 索引(`get_tool_config` 每个社区工具调用要跑 2–3 次)。每次重载构造全新
  `AppConfig`,索引随之重建;重名取第一个(`setdefault`),保持旧 `next()` 语义。

### 2.2 `config_version`:版本闸门与 `make config-upgrade`

`config.example.yaml` 顶部有 `config_version`(当前 39)。启动时
`_check_config_version` 拿用户文件与 example 文件(沿 config.yaml 目录向上最多找 5 层)
对比:用户版本低就打 warning——"run `make config-upgrade`"。缺失 `config_version`
按 0(前版本化时代)处理;example 缺失也按 0。改 schema 的 commit 必须同步 bump
example 里的版本号,这是**文档纪律的机器化**:不 bump 就没有告警,operator 悄悄跑在
旧配置上。

### 2.3 `$VAR` 递归展开:两个模型,两种失败语义

两个配置文件都支持以 `$` 开头的字符串值引用环境变量,且都是**递归展开**(字符串、
dict 键值、list、tuple 逐层下沉)。注意语义差异——这是最容易踩的坑:

- **`AppConfig.resolve_env_variables`(config.yaml)**:找不到环境变量就
  `raise ValueError(f"Environment variable {name} not found ...")`。**严格**。
  配置文件里写 `$OPENAI_API_KEY` 而进程没导出它,直接启动失败。
- **`ExtensionsConfig.resolve_env_variables`(extensions_config.json)**:找不到就返回
  **空字符串**。**宽容**。注释写明理由:下游消费者(如 MCP 服务器)不该收到字面
  `$VAR` 令牌当真实环境值。代价是"拼错变量名"会静默变成空 secret——MCP 服务器
  启动时可能报认证错而不是配置错。

```yaml
# config.yaml —— 缺失 $VAR → ValueError,进程拒绝启动
models: [{name: gpt-4o, provider: {use: langchain_openai:ChatOpenAI, api_key: $OPENAI_API_KEY}}]
```

```json
// extensions_config.json —— 缺失 $VAR → 空串,服务器带空 token 起
{ "mcpServers": { "github": { "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" } } } }
```

另外,环境变量解析发生在 Pydantic 校验**之前**,所以模型字段仍只见解析后的明文

## 3. 热重载边界:per-run 字段 vs `STARTUP_ONLY_FIELDS`

### 3.1 为什么"热"、怎么"热"

背景是 issue #3144 的设计结论:Gateway 的请求依赖**每次请求**都走
`get_app_config()` 重新取配置,而不是把 `AppConfig` 缓存到 `app.state`。缓存层做了
三层失效判断:

```python
should_reload = (_app_config is None
                 or _app_config_path != resolved_path
                 or _app_config_signature != current_signature)
```

其中 `current_signature` 来自 `config/file_signature.py::get_config_signature()`
——**文件元数据 + 内容摘要**,而非裸 mtime。这保证在对象存储/网络挂载上 mtime 可能
僵死时,Gateway 与 LangGraph 侧的读取仍能与 config.yaml 的实际编辑对齐。命中重载就
`AppConfig.from_file()` 全量重建(构造期顺带刷新 §2.1 的索引与单例配置)。

于是**运行期字段**天然热生效:标题生成(`title.*`)、摘要(`summarization.*`)、记忆
(`memory.*`)、子代理(`subagents.*`)、验证(`verification.*`)、模型与工具列表里的
per-run 项(`models[*].max_tokens`、`tools[*]`)、agent 系统提示词……任何依赖在
请求/消息粒度重新读取配置的路径,下一次消息就吃到新值。lifespan 只用局部变量
`startup_config` 做一次性引导,再传给 `langgraph_runtime(app, startup_config)`。

### 3.2 `reload_boundary.py`:唯一的权威清单

"需要重启"的字段被集中登记在
`deerflow/config/reload_boundary.py::STARTUP_ONLY_FIELDS`(dict[str, reason]),而不是
散落在注释里。它的 docstring 讲清了注册表收录两类条目:

- **`AppConfig` 字段与显式注册的嵌套字段**——`format_field_description()` 为它们生成
  标准前缀 `"startup-only: "` + 理由,拼进对应 Pydantic `Field(description=...)`,
  所以 **IDE hover 直接看到"为什么重启、重启哪个子系统"**,无需跳转查表;
- **不属于 `AppConfig` schema 的顶层段**(`channels`、`channel_connections`)——schema
  层面无法标注,注册表是它们唯一的权威位置。

截止本 commit 的全部 18 个注册项:

| 字段路径 | 抓快照的位置(重启理由) |
|---|---|
| `plugins` | `load_extensions()` 在 `create_app()` 只跑一次,进程级中间件注册表不随 YAML 编辑重建 |
| `database` | `init_engine_from_config()` 在 `langgraph_runtime()` 启动期建一次 SQLAlchemy 引擎,连接池不重建 |
| `checkpointer` | `make_checkpointer()` 启动期绑定持久 checkpointer(含 SQLite WAL/busy_timeout) |
| `run_events` | `make_run_event_store()` 启动期选 memory/SQL 实现,冻结在 `app.state.run_events_config` |
| `agent_storage` | 启动期校验 backend 与 database.backend 一致;db 后端的同步引擎进程级缓存 |
| `stream_bridge` | `make_stream_bridge()` 构造 stream-bridge 单例一次 |
| `sandbox` | `get_sandbox_provider()` 缓存 provider 单例;换 `sandbox.use` 类路径只在下个进程生效 |
| `skills.container_path` | AIO/E2B provider 启动期归一化并锁定技能挂载根(沙箱身份、挂载、同步都依赖它) |
| `log_level` | `apply_logging_level()` 只在 app.py 启动期跑 |
| `logging` | `configure_logging()` 只在启动期装/卸 trace filter 与增强 formatter(只影响**日志输出**;trace id 本身无条件签发,`X-Trace-Id` 头恒在) |
| `channels` | `start_channel_service()` 启动期建一次 IM channel 客户端,不随 `channels.*` 重建 |
| `channel_connections` | 连接仓库与 channel worker 启动期接线;router 把合并后的 provider 配置缓存到 app.state |
| `scheduler` | `ScheduledTaskService` 启动期构造并启动,轮询器不重建(多实例参数要求所有 Pod 一起重启) |
| `mcp_tasks` | `McpTaskService` 启动期构造并启动,后台轮询器不重建 |
| `subagent_runtime` | 共享子代理准入控制器与隔离执行循环启动期配置一次 |
| `subagent_batches` | 持久化子代理批服务启动期构造,限额/租约/恢复被服务实例捕获 |
| `run_ownership` | `RunOwnershipConfig` 在 `langgraph_runtime()` 启动期捕获进 RunManager,心跳任务在那创建 |
| `dedupe_storage` | 入站去重存储(进程内/共享 Postgres)在 ChannelService 构造时定死 |

**两个方向的防漂移测试**(`tests/test_reload_boundary.py`)把注册表钉死:注册过的字段
必须带 `startup-only:` 前缀;任何 schema 里用了该前缀的字段必须在注册表里。新增需要
重启的字段 = 改注册表 + 让描述带前缀,缺一不可,CI 会抓。

一个值得注意的"例外中的例外":`scheduler.recursion_limit` 在 `scheduler` 段里,却**不**
是启动期捕获——`launch_scheduled_thread_run` 每次派发时从 `get_app_config()` 现读,
所以改 YAML 后下一个定时任务就跑新值,不用重启轮询器(重启理由里特意点名了这个区别)。

> **一句话判断法**:改完配置不确定热不热?看该字段描述有没有 `startup-only:` 前缀,
> 或直接 grep `STARTUP_ONLY_FIELDS`——注册表就是文档。

## 4. extensions_config.json:内容模型与"API 可写"语义

### 4.1 内容模型

`ExtensionsConfig`(config/extensions_config.py)是 Pydantic 模型,`extra="allow"`
容忍未来字段。schema 上的三个字段:

```python
class ExtensionsConfig(BaseModel):
    middlewares: list[str]      # "module.path:ClassName",resolve_class 实例化进 agent 链(§5)
    mcp_servers: dict[str, McpServerConfig]   # alias="mcpServers"
    skills: dict[str, SkillStateConfig]       # skill 名 → {enabled}
```

example 文件里出现但 schema 未声明(`mcpInterceptors` 等)的内容靠 `extra="allow"` 透传,
`to_file_dict()` 序列化时原样保留——这是"配置向前兼容"的默认姿势。

`McpServerConfig` 才是大头,一个服务器条目的完整能力(本 commit):

- **传输**:`type: stdio|sse|http`(兼容 MCP 官方 schema 的 `transport` 别名,二者并存时
  `type` 优先——model_validator `_accept_transport_alias`);`command/args/env`(stdio)与
  `url/headers`(sse/http);
- **凭据注入三件套**:静态 `headers`(仅启动期工具发现用)、`oauth`
  (`client_credentials`/`refresh_token` 客户端模式,含 refresh_skew_seconds 等细粒度参数)、
  `user_auth`(per-user 凭据映射,值支持 `$ENV_VAR`)、`headers_from_context`
  (per-request 凭据:只存 header 名与运行请求 `config.context.secrets` 的键名映射,
  不存任何秘密,因此可安全地从配置 API 明文返回);
- **行为**:`enabled`、`tool_name_prefix`(默认 true,防跨服务器撞名)、
  `tool_call_timeout`(单次调用;HTTP/SSE 的普通工具走传输级超时)、
  `session_init_timeout`(默认 `DEFAULT_MCP_SESSION_INIT_TIMEOUT`,防止挂死的服务器阻塞
  agent 构造或任务轮询)、`routing`(软提示,off/prefer + priority 0-100 自动夹紧 +
  keywords)、`tools`(按原始工具名覆盖 routing);
- **任务化**:`task_toolsets`——把 submit/status/cancel 三个原始 MCP 工具声明为一组
  "普通后台任务契约",交给独立的持久化任务运行时(McpTaskService)而不是 agent 循环。
  原始工具名跨 toolset/role 必须唯一,名字长度受 `MCP_TASK_SERVER_NAME_MAX_LENGTH`
  约束,校验不过直接 400。

### 4.2 HTTP 可写:原子写、双锁与 EBUSY 回退

这个文件与 config.yaml 的本质区别是**Gateway 在运行期写它**:`PUT`/`PATCH
/api/mcp/config`、MCP 启用开关、技能 enable/disable/install。写路径不是裸
`json.dump`,而是一套在 AGENTS.md 里被反复推敲的工程:

- **读-改-写全程持双锁**:进程内 `extensions_config_write_lock`(threading.Lock,必须在
  worker 线程内持有——asyncio.Lock 在取消时会提前释放,把锁让给第二个写者)+ 同目录
  侧车文件锁 `extensions_config_file_lock`(fcntl/msvcrt 的 `.extensions_config.json.lock`
  旁车文件,跨进程排他)。只锁最后的原子替换仍会丢更新,锁必须包住
  read→merge→write→reload 整个周期。
- **`atomic_write_extensions_config`**:同目录临时文件 + fsync + `os.replace`。但生产
  compose 把文件作为独立 bind mount 挂进来,Linux 拒绝 `rename()` 覆盖挂载点
  (EBUSY)——即使挂载可写。于是捕获 `EBUSY` 回退为**就地覆写**(非原子:写到一半崩溃
  会截断文件;每个目标只 warn 一次,之后降级 debug)。
- 因此生产 compose 把 `extensions_config.json` 挂成**读写**,`config.yaml` 保持
  `:ro`——"config.yaml 无人写、extensions 文件被 API 写"是部署层的分工镜像(§11)。

**热生效路径**:写接口完成后调用 `reload_extensions_config()`,更新进程级单例并让 MCP
工具缓存、技能缓存失效。**注意:没有文件监听器**。operator 直接手改 JSON 而不重启、
且 config.yaml 也没变时,不会触发任何重载(§3 的签名检查只盯 config.yaml);改动要等
下次 config.yaml 变更连带 `AppConfig.from_file()` 重读 JSON,或显式重启。

### 4.3 为什么 `plugins:` 必须留在 config.yaml

第 1 节已给原则,这里补齐机制:即使 `ExtensionsConfig` 有 `extra="allow"`,loader 的
`ExtensionSpec` 模型也是 `extra="forbid"`——`plugins:` 列表的每一项字段白名单化
(`enabled/name/package/use/config/required/table_prefix`),且该模型定义在 harness 的
`extensions/loader.py`,由 `config.yaml` 的 `AppConfig.plugins` 字段承载。两个文件在
schema 层面就分家了:JSON 文件里写 `plugins:` 是无效字段,loader 根本不会读它;而
配置带重复顶层 `plugins:` 键直接报错(而不是对着一块管理、Gateway 读另一块)。

## 5. 中间件的两条扩展路径:配置声明 vs 插件贡献

先厘清一个高频混淆点:**DeerFlow 的"扩展中间件"有两条完全不同的入口**,它们装配的
时机、位置、隔离级别都不同:

| | 配置声明中间件 | 插件贡献中间件 |
|---|---|---|
| 声明处 | `extensions.middlewares`(config.yaml 或 extensions_config.json) | Python 插件 `plugins:` → `registry.middlewares(...)` |
| 载体 | `"module.path:ClassName"` 字符串 | `MiddlewarePlacement`(类实例 + Placement + scope + order) |
| 实例化 | `resolve_class(path, AgentMiddleware)` → **零参构造**,每次 agent 构建都现造 | 插件 `install()` 期已造好;每次注入被 `IsolatedMiddleware` 包一层 |
| 类型限制 | 无参构造的 AgentMiddleware;lead/subagent 共用同一张列表 | 任意 AgentMiddleware + 声明式 Placement/scope |
| 位置 | 固定槽:built-ins/程序自定义之后、terminal/safety/clarification 尾巴之前(链位 31) | 由 Placement 锚点决定(MODEL_LOGICAL…TOOL_RAW,§7) |
| 隔离 | **无**——异常直接沿 LangChain 调用链炸(失败即 loud) | **有**——`IsolatedMiddleware` 包一层,观察失败降级诊断、调用放行(§7.3) |
| 信任注释 | "视为可信 operator 配置;路径实例化任意代码" | 安装即执行第三方代码,只信 operator 源 |

两条路径在中间件装配里先后执行:`build_middlewares()` 先
`load_configured_extension_middlewares(app_config)` 把配置声明的类路径实例化追加进链
(链位 31),函数尾部再调 `compose_with_extensions()` 把插件贡献按锚点注入(§7.2)。
配置声明路径"loud fail"是有意为之:middlewares/AGENTS.md 写明缺失包、非法类、坏模块
**必须在 agent 创建时大声失败**——这是 operator 亲手写的信任声明,静默跳过等于
带病上线。

## 6. extension-api:契约包与五类贡献点

### 6.1 包边界:为什么契约要独立成包

第三方扩展 import 的是 **`deerflow_extension_api`**(backend/packages/extension-api,
一个独立发布的小包),而不是 `deerflow`。`__init__.py` 的第一行注释是硬约束:

> "This package MUST NOT import `deerflow`."

理由是发布节奏:**扩展要能独立于宿主发布**。宿主(harness)每次改版都发布,扩展作者
不该被迫跟随;契约稳定在 `deerflow-extension-api` 里,扩展只依赖它,框架 import
(langchain/langgraph/fastapi)保持为扩展自己的直接依赖。宿主侧实现
(`deerflow.extensions.*`)与契约通过 Protocol + 结构类型对接。

契约版本号 `API_VERSION = "0.2.0"`。兼容窗口规则(`loader._compatible`):

- **0.x**:minor 可能 break,窗口 = 同 major.minor,补丁只增——`host >= declared`;
- **>=1.0**:同一 major 内契约只增不减,新 host 兼容旧扩展;**按更新 minor 写的扩展被拒**
  (它会够到宿主没实现的契约新增);
- 版本不可解析 → 拒绝(不放过)。

扩展安装入口用可选装饰器 `@extension(api="0.2.0", name="example")` 盖章
(`__deerflow_api__` 属性)。pip 依赖解析本是主兼容机制,这个章盖给 `--no-deps` 安装和
monorepo editable checkout 兜底——把深层的 AttributeError 转成启动期可操作的诊断:
"extension requires extension-api X, host provides Y. Install a matching version: pip
install 'deerflow-extension-api>=X,<Y'"。

### 6.2 `install(registry, config)` 与注册面

打包扩展通过 PEP 621 entry point 声明入口(`deerflow.extensions` 组),宿主用
`resolve_variable` 解析:

```toml
[project.entry-points."deerflow.extensions"]
example = "deerflow_extension_example:install"
```

入口签名固定为 `install(registry: ExtensionRegistry, config: Mapping[str, Any])`。
扩展看到的 `ExtensionRegistry` 是**只写、结构化最小**的契约(宿主实现另有 attribution、
rollback、build 等宿主机,刻意不进契约),七个注册方法全部带默认实现(后续加方法保持
additive):

```python
registry.middlewares(contributor)            # → 贡献 AgentMiddleware(带 Placement)
registry.task_lifecycle(contributor)         # → on_task_start / on_task_stop
registry.system_model_observer(observer)     # → on_system_model_call(系统侧模型调用)
registry.agent_assembly_observer(observer)   # → 观察 agent 装配产物(描述符)
registry.context_compaction_observer(observer)  # → 观察上下文压缩事件
registry.service(service)                    # → ExtensionService: start/stop
registry.routers((router,))                  # → FastAPI APIRouter(启动期已构造好)
```

仓库文档(根 AGENTS.md 与参考扩展 README)把贡献点归纳为**五类**:
**middleware / task lifecycle / system-model observer / Gateway service / FastAPI
HTTP router**,`examples/deerflow-extension-example/` 每个都给了刻意做小的实现。
`agent_assembly_observer` 与 `context_compaction_observer` 是注册面上真实存在、但
尚在参考扩展之外的另两个观察口(assembly 观察 agent 图装配、compaction 观察上下文
压缩),写扩展时以 `deerflow_extension_api.contracts.ExtensionRegistry` 的实际签名为准。

五类的语义要点:

1. **Middleware**(`MiddlewareContributor.contribute_middlewares(app_store, ctx)`):
   返回 `MiddlewarePlacement` 序列,每个含 `middleware`、`placement`(五个 Placement 之一)、
   `scope`(AgentScope.LEAD/SUBAGENT/BOTH,位标志)、`order`(int)。宿主在注入时做全部
   类型校验(必须是 AgentMiddleware、scope/placement 必须合法、order 必须是 int 非 bool),
   非法的条目只记诊断、跳过,不拖垮整个插件(§7.2)。
2. **Task lifecycle**(`TaskLifecycleContributor`):`on_task_start/on_task_stop` 收到
   `TaskInfo`(task_id/run_id/thread_id/kind=lead|subagent/parent_task_id/agent_name/
   resumed)与 `TaskOutcome`(completed/aborted/failed)。lead 的 task_id 恒等于 run_id
   (含 continuation),`notify.py` 提供 `lead_task_id` 与 lead/subagent 两侧的结果分类器。
3. **System-model observer**(`SystemModelCallObserver.on_system_model_call`):观察
   **不被中间件模型钩子覆盖的系统自有模型调用**——goal 评估、记忆抽取、标题生成、
   摘要(SystemOperationKind.GOAL/MEMORY/TITLE/SUMMARIZATION)。`SystemModelRequest` 是
   调用前只读快照(messages 被 `__post_init__` 归一化成 tuple,防标题/摘要传单条 prompt
   字符串时被当字符序列遍历);`SystemModelResult` 是成败快照(response/error/
   duration_ms)。
4. **Gateway service**(`ExtensionService`):`start(deps)` / `stop()`。`deps` 是
   `ExtensionRuntimeDeps`——宿主基础设施就绪后才绑定:`app_store`、`policy`
   (HostPolicySnapshot,宿主强制的预算/限额投影,刻意不暴露 AppConfig 以免把扩展钉死在
   harness 发布节奏上)、`session_factory`。宿主按注册顺序 start、逆序 stop(每项独立
   30s 预算,超时/取消/异常都 fail-open 记诊断);启动被取消或失败的项仍登记为
   "已尝试",关机时同样拥有 stop()。
5. **FastAPI router**(`registry.routers(...)`):路由对象在 install 期**急切构造**,宿主
   挂载前做严格预检(`include_contributed_routers` + `_router_routes`):不得带
   on_startup/on_shutdown 或自定义 lifespan(生命周期请注册成 service);不得含
   WebSocket 路由(宿主还没法做认证与 Origin 检查);不得含 Starlette Mount;**不得进入
   宿主保留命名空间**——预检用编译期路径模板推演每条路由能否触达公开前缀(/health、
   /docs、/openapi.json、OAuth 回调……)或 CSRF 豁免路径,能触达就拒挂(安全边界,fail
   closed);与已挂路由(含宿主路由)路径重叠且被宿主覆盖也拒挂(fail-open 的 shadow 检测)。
   成功挂载的路由跑在宿主 `AuthMiddleware` 之后:到这里的请求都已会话认证;"已登录"
   与"是管理员"仍是两回事——路由内用 `deerflow_extension_api.auth` 的
   `resolve_principal(request)` / `require_admin(request)`,读取宿主装在 app.state 的
   resolver,拿到的是身份的中性投影而非宿主的 auth 上下文。

### 6.3 作用域存储:`ExtensionData`

所有回调的第一个参数是 `app_store`(应用级)或 `app_store + task_store`(任务级),类型
`ExtensionData`:按 **type 而不是字符串** 作键的线程安全小仓库——两个扩展不会撞键。
宿主为 app 与每个 task 各建一个实例,作用域结束即丢弃,回调每次下发当前作用域的实例
而非让扩展持有句柄,因此扩展永远不需要陈旧句柄检查。task_store 在运行期通过
`task_store_from_runtime(runtime)` 从 `EXTENSION_TASK_STORE_KEY` 取(中间件在工具调用
里够不到任务级 store 时用)。

## 7. 扩展顺序:从声明到注入的确定性

middleware stack 是 position-sensitive 的,所以顺序是这套系统的第一公民;而顺序
不变量被破坏是唯一硬失败——不像缺失一次观察,它产生**无报错的错误行为**(§7.5)。

### 7.1 加载顺序:config 列表顺序,显式且可复现

`load_extensions(specs)` 按 `plugins:` 列表顺序逐个 resolve+install——列表顺序即
注册顺序,注册顺序即注入顺序的 tiebreaker。每个 spec 的处理管线:

1. `table_prefix` 无条件注册进 alembic 过滤(禁用或后失败的 spec 也注册——它点名的表
   可能已在库里,误伤方向是"多注册一个前缀"而非"漏一个");与宿主表名冲突的前缀
   **总是中止启动**(它污染进程期内共享的 alembic 过滤器,`required:false` 也扛不住);
2. `enabled: false` → 跳过(不 resolve 不 import);
3. `resolve_variable(spec.use)` → 不可解析/不可调用 → 诊断 + (required 则抛
   `ExtensionLoadError`);
4. 读 `__deerflow_api__` 标记并做版本兼容检查(§6.1)——不兼容给 pip 安装提示;
5. `registry.mark()` 记录各桶长度 → `attributed_to(spec.use)` 块内跑
   `install(registry, _frozen_config(spec.config))` → 失败则 **位置回滚**
   (`rollback_to(mark)`,而非按 source 删——两个 spec 可以合法地共享同一 `use` 不同
   config,按 source 删会误伤先成功注册的同名实例);
6. 成功则记录 loaded_sources,最后打一条 "Extensions loaded: x/y (…)" 的 info 日志——
   没有它,"全部成功"与"plugins 块宿主根本没读"无法区分。

失败语义:**默认 fail-open**——坏扩展带诊断跳过,Gateway 照样起;`required: true`
(ExtensionManager 安装时显式 opt-in,见 §10)翻转成 fail-closed——缺失会改变行为而非
仅可观测性的扩展必须中止启动。全程 attribution:每个 Diagnostic、每次 provenance、
每个顺序错误的报错都带 `source`(即该 spec 的 use 字符串),"谁干的"永远可查。

### 7.2 锚点:语义 Placement → 物理索引的唯一翻译器

`extensions/anchors.py` 的 docstring 是设计总纲:

> "This is the only module that knows the shape of DeerFlow's middleware stack.
> Restructuring the stack means updating the anchor table here; extensions, which
> declare only what they need to observe, stay untouched."

五个 `Placement` 声明的是**语义保证**而非结构位置("我要观察工具的原始返回"而不是
"把我放第 3 层")——中间件在列表里占一个索引,但该索引只在它实际参与的钩子链上有
意义,所以按"轴 × 端点"声明消除了歧义,也让宿主保有重构堆栈的自由:

| Placement | 语义保证(锚点实现) |
|---|---|
| `MODEL_LOGICAL` | 模型轴外端:重试与错误处理之外,一次逻辑决策只触发一次(锚:LLMErrorHandlingMiddleware 之外) |
| `MODEL_PHYSICAL` | 模型轴内端:每个请求变换中间件之内,一次物理 provider 调用触发一次,重试会重入(锚链:SafetyFinishReason 之后 & TerminalResponse 之后的最内 … 逐级回退到 innermost) |
| `TOOL_VISIBLE` | 工具轴外端:截断、净化、错误包装之外,观察模型最终看到的(锚:outermost) |
| `TOOL_RAW` | 工具轴内端:紧贴真实 callable 边界,观察工具处理前的原始返回(锚:Clarification 之外的最内,回退 innermost) |
| `STANDARD` | 无前后处理要求;与其它 STANDARD 的相对顺序不保证(锚:LLMErrorHandling 之外,回退 innermost) |

锚点表 `PLACEMENT_ANCHORS`(extensions/stack.py)是**惰性解析**的 `_AnchorTable`:
import 时不碰任何中间件模块(避免循环依赖),首次取用才填充。主锚点失败(比如
ClarificationMiddleware 因配置没装)时沿 `PlacementAnchor` 的 fallback 链逐级下探;
`resolve()` 返回 `(index, used_primary_rule)`,**没走主规则就报 warning 诊断**——
"该 placement 的观察语义可能与文档保证不同",静默降级的位置变更比报错更危险。
SUBAGENT scope 有专属覆盖:MODEL_PHYSICAL 在子代理里先尝试 SystemMessageCoalescing
Middleware 之内再落回主链。

注入本身(`inject_middlewares`):先按 `(order, 注册序)` 稳定排序;再**从最内开始插**
(每次 insert 都会把后续索引后移,倒着插保证先算好的锚点仍有效);同一目标索引的两个
贡献按 priority 倒序处理——先插低优先级,让高优先级的贡献最终落在更外层。每个实例
被 `IsolatedMiddleware` 包装,名字按 `extension:{source}:{inner}:{priority}` 去重
(LangChain 要求全栈名字唯一,名字同时是 trace 身份与 before/after 钩子的图节点 ID)。
返回 provenance map:`{最终索引: 扩展 source}`——顺序校验拿它点名责任人。

### 7.3 单点注入:`compose_with_extensions`

堆栈在两个嵌套点装配(`build_lead_runtime_middlewares()` 出基座,
`build_middlewares()` 再追加 ~18 个 lead-only 中间件,全部在基座**内层**),而
MODEL_PHYSICAL 落在第二组——所以扩展注入**必须**在完整列表组装完成后进行,绝不能
塞进基座构造器。`compose_with_extensions` 就是那唯一的收尾点:

```python
def compose_with_extensions(middlewares, scope, ctx, extensions=None):
    # 1) 无中间件贡献者:仍跑 assert_ordering(空 provenance)后原样返回——校验不缺席
    # 2) 有贡献者但 ctx 为 None → ValueError(扩展需要 AgentBuildContext 决策)
    # 3) inject_middlewares(锚点表[scope], scope, ctx, ...) → 诊断上报 app.state
    # 4) assert_ordering(result, provenance)   ← 硬校验,违反即 RuntimeError
```

两处调用点:lead 装配(`agents/lead_agent/agent.py` 的 `build_middlewares` 尾,
AgentScope.LEAD + 构造好的 `AgentBuildContext(agent_name, model_name, policy)`)、
子代理装配(`tool_error_handling_middleware.py` 的子代理构建器,AgentScope.SUBAGENT)。
`LoadedExtensions` 上的 `has_middleware_contributors` 预计算标志让零扩展路径零开销
短路——而构造 `AgentBuildContext` 需要 `project_host_policy()` 投影宿主策略,所以
短路不只是性能优化,还免去了无扩展时投影的系统调用。

### 7.4 隔离:`IsolatedMiddleware` 解包

插件中间件在 LangChain 调用链内执行,未处理异常会**杀死用户的 run**。因此每个贡献
被 `extensions/isolation.py::IsolatedMiddleware` 包裹,观察失败降级为诊断、调用放行
(fail-open;未来若出现"决策型"拦截贡献需 fail-closed,必须显式退出该包装)。工程细节:

- **镜像完整接口,不只四个 wrap 钩子**:LangChain 靠类级身份检查发现能力
  (`m.__class__.before_model is not AgentMiddleware.before_model`),工具/state_schema/
  transformers 走实例属性。包装器按内层中间件实际实现的钩子集**缓存生成专属子类**
  (同钩子组合共享一个子类),让 LangChain 在包装器上看到与内层一致的接口;
- **同步/异步配对补全**:LangChain 把 sync/async wrap 对当一个能力,任一侧存在就接两条
  执行路径——内层只实现一侧时,包装器补静默直通的另一侧,否则基类在隔离生效前就
  `NotImplementedError` 了;
- **下游 handler 追踪**:pre-handler 失败只调一次 handler、post-handler 失败返回已捕获
  结果、handler 自身失败仍归图的错误策略——隔离恢复**绝不引入第二次模型请求或工具
  副作用**;内层不调 handler 或调两次都算错误上报;
- `GraphBubbleUp`(LangGraph 结构化控制流)原样穿透,不做 fail-open 吞并;
- **生命周期钩子没有 handler 可回退**:观察失败 = 不应用状态更新;
- `IsolatedMiddleware.inner` 属性暴露被包对象——顺序校验与测试都用它**解包**后再做
  isinstance 判断。

### 7.5 `assert_ordering`:声明式不变量

`extensions/ordering.py` 用约束声明取代手写索引比较。扩展合并**先于**校验执行,贡献
无法溜过不变量;违规错误直接点名扩展。核心约束(`core_ordering_constraints()`,
惰性解析避免 extensions/ 反向依赖 agents/)三条:

- `ToolProgressMiddleware` 必须在 `ToolErrorHandlingMiddleware` **之外**(它读后者盖的
  `deerflow_tool_meta`);
- `ToolReceiptMiddleware` 必须在 `ToolErrorHandlingMiddleware` 之外(收据读状态戳);
- `ToolReceiptMiddleware` 必须在每个**短路器**之外——Guardrail、SandboxAudit、
  ReadBeforeWrite、ToolProgress 都可能不调下游 handler 直接返回/重建 ToolMessage,
  收据必须包住它们,否则账本静默缺口。

校验用"两侧都缺席则跳过"(no-op)语义:约束只在两个类型都实际在场时执行。解包
IsolatedMiddleware 后取类型,违规时列出违规索引并归因:`provenance` 里有名字就点名
扩展,没有就是 core middleware order。

## 8. 反射:`resolve_variable` / `resolve_class`

`deerflow/reflection/` 是整个配置体系引用类路径的统一出口,只有两个函数:

- **`resolve_variable(path, expected_type=None)`**:`rsplit(":", 1)` 拆
  `module.path:variable_name`,import 模块后取属性;可选 isinstance 校验。import 失败时
  生成**可操作的依赖提示**(`langchain_google_genai` → "Install it with `uv add
  langchain-google-genai`")——提示映射表 `MODULE_TO_PACKAGE_HINTS` 覆盖常见 provider 包。
- **`resolve_class(path, base_class=None)`**:在 `resolve_variable` 之上要求结果是 type,
  且(给了 base_class 时)必须是其子类。

配置里的 `use:` 字段全是它的客户:**模型的 `use`(langchain_openai:ChatOpenAI)、
工具路径、沙箱 provider 的 `sandbox.use`、guardrail provider、配置声明中间件的
`extensions.middlewares` 条目、以及扩展入口的 `plugins[].use`**。统一出口意味着所有
失败共享同一种报错方言——"缺依赖该装什么包"这类问题全仓库只实现一次。

## 9. harness/app 边界与版本 lockstep

扩展机制横跨两层代码,先把边界说清(backend/AGENTS.md 的 Harness/App Split):

- **Harness**(`packages/harness/deerflow/`,发布名 `deerflow-harness`,import 前缀
  `deerflow.*`):可发布的 agent 框架包。agent 编排、工具、沙箱、模型、MCP、技能、
  config——**包括整个 `extensions/` 加载/注册/注入/隔离子系统**都在这一层;
- **App**(`backend/app/`,import 前缀 `app.*`):不发布的应用程序代码。FastAPI Gateway
  与 IM channel 集成。`app/gateway/app.py` 调 `load_extensions()`、把贡献路由挂到
  app.router(`include_contributed_routers`)、在 lifespan 里 start/stop 扩展服务;
- **依赖方向硬约束**:App import deerflow 合法,deerflow import app **非法**——由
  `tests/test_harness_boundary.py` 在 CI 钉死。这也是 extensions/ 与 agents/middlewares
  之间刻意保持的**单向**依赖:anchors/ordering 对中间件类型的引用全部推迟到函数体内,
  避免在 import 期把依赖指回 agents/。

**版本 lockstep 是"同仓同位",不是版本号对齐**:harness 与 app 在同一个 monorepo、同
一个 commit 里发布与部署,不存在两套版本号可以漂移——`config.example.yaml` 的 schema
演进、`STARTUP_ONLY_FIELDS` 的注册、`compose_with_extensions` 的锚点表,全部随仓库
原子变更。真正需要独立版本管理的是**契约**,而它被拆成了独立的
`deerflow-extension-api`(API_VERSION = "0.2.0",§6.1):参考扩展在 pyproject 里声明
`deerflow-extension-api>=0.2,<0.3`——宿主与扩展之间唯一允许漂移、且被显式管理的版本
关系就是这一条,运行时由 `__deerflow_api__` 标记 + loader 兼容窗口兜底。

## 10. 运维面:operator CLI 与部署

### 10.1 `deerflow extensions` 命令

Python 扩展的安装/启停由 `extensions/cli.py` 的 operator CLI 承担(从 `deerflow`
console script 派发),只暴露五个面:`install SOURCE [--yes] [--required]`、`list`、
`enable NAME`、`disable NAME`、`remove NAME`。根 Makefile 的 `make extension-*` 只是
包装。核心事实:

- **安装 = 受控的 uv 事务**:`uv add --project <backend> --group extensions
  --no-workspace --no-sync -- <source>`(要求 uv ≥0.8.0,仓库 Docker 路径钉 0.11.1),
  更新专用 `[dependency-groups].extensions` 与 uv.lock,发现恰好一个 packaging
  entry point,插入/收养一条托管 `plugins:` 记录(name/package/use/enabled/required/
  config)。新记录 `required: false`;`--required` 是显式 opt-in——坏 wheel、缺原生库、
  快照被删都会变成只能靠 shell 访问恢复的启动中止;
- **源限制**:本地目录安装是**快照拷贝**(`backend/extensions/sources/<distribution>/`,
  非 editable),远程只收 HTTPS,git 源必须 public Git-over-HTTPS(SSH 被拒:镜像构建器
  不转发宿主 SSH 凭据);安装校验通过才动 uv,失败则回滚 pyproject/uv.lock 并重同步
  环境;
- **每次变更都需重启 Gateway**(`plugins` 在 STARTUP_ONLY_FIELDS 注册表里),操作全程
  持跨进程 `.deer-flow/extension-manager.lock`。

### 10.2 部署:一个文件读、一个文件写

生产 compose(docker/docker-compose.yaml)与 Helm(deploy/helm/deer-flow)把 §1 的信任
分工做成了**挂载语义**:

- **docker compose(一句话)**:`config.yaml` 以 `:ro` 只读挂载进容器,`extensions_config.json`
  以读写方式挂载(注释原话:"Writable on purpose: the Gateway edits this file at
  runtime…config.yaml above stays read-only because no API writes it"),两个文件都通过
  `DEER_FLOW_CONFIG_PATH` / `DEER_FLOW_EXTENSIONS_CONFIG_PATH` 环境变量指向容器内路径;
  也正因为文件是独立 bind mount 点,`atomic_write_extensions_config` 的 EBUSY 回退
  (§4.2)在 compose 部署下是**每次 API 写都会走的常态路径**。
- **Helm(一句话)**:config.yaml 来自 ConfigMap 以 subPath 只读挂载;extensions_config.json
  由 init 容器把 ConfigMap 种子 `cp` 进**可写的 home 卷目录**
  (`/app/backend/.deer-flow/extensions-config/extensions_config.json`,PVC 或
  pod-local emptyDir),此后 API 的运行期写入落在持久卷上,不会写回只读 ConfigMap。
- 两者共通:**插件(`plugins:`)没有热路径,任何 add/enable/disable/remove 都要滚动重启
  Gateway**;MCP/技能类改动走 API 即写即 reload,不重启。

## 11. 决策速查与常见坑

**判断一道配置改动怎么生效:**

1. 字段在 `STARTUP_ONLY_FIELDS`?→ 重启(顺带读它的 reason,知道重启哪个子系统)。
2. 否则是 config.yaml 里 per-run 字段?→ 下一条消息生效(signature 检测,无需手动 reload)。
3. 是 extensions_config.json 里的 MCP/技能?→ 走 API 写(自动 reload);手改文件要等 config.yaml 连带变更或重启。
4. 是 `plugins:` / `extensions.middlewares`?→ `plugins` 重启才装载;声明式中间件下一次 agent 构建即实例化(仍属 trusted-operator 代码,改动前想清楚)。

**高频坑位:**

- `config.yaml` 里 `$VAR` 缺失 → **启动失败**;extensions_config.json 里缺失 → **空串**
  (两类文件失败语义不同,别用同一套 CI 检查)。
- 把 `plugins:` 写进 extensions_config.json → loader 永远读不到,而且该文件任何 API
  写接口都不会替你保留它(可能被整块覆盖)。
- 锚点主规则未命中只 warn 不报错——日志里见到 "placement … fell back to a
  secondary anchor" 说明宿主堆栈结构变了,观察语义可能已经漂移。
- `required: false` 是默认:装了但起不来的扩展只留一条诊断,不会拦 Gateway——怀疑
  "扩展没生效"先看 `app.state.extension_diagnostics`(宿主的运行期诊断桶,上限 1000 条,
  顺序错误、注入失败、路由拒挂都进这里)。
- 扩展路由挂在宿主 AuthMiddleware 之后,但"已登录 ≠ 管理员":需要管理权限的路由必须
  在 handler 里 `require_admin(request)`,不能只靠网关。
- 手改 extensions_config.json 后既不调 API 又不改 config.yaml → 进程内旧值继续服役,
  没有文件监听器会救你。

**本章关系图(读到这里你应该能默写):**

```
config.yaml ──plugins:──► load_extensions()(启动期一次)──► LoadedExtensions(不可变快照)
   │  ▲                            attributed/rollback/Diagnostic      │
   │  └─ resolve_env_variables / resolve_class / AppConfig             ├─ middlewares → compose_with_extensions
extensions_config.json ──► ExtensionsConfig ──merge──► AppConfig.extensions  │   → anchors → inject → Isolated → assert_ordering
   │  (API 可写:/api/mcp/config、技能开关,写后 reload)                  ├─ task_lifecycle / system_model_observer
   ▼                                                                    │   → notify → ExtensionData(app/task)
MCP 服务器 / 技能 / 声明式中间件                                       ├─ services → start(注册序)/ stop(逆序,30s/项)
                                                                        └─ routers → include_contributed_routers(预检后挂载)
```
