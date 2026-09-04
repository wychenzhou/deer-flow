# 附录 A · 配置速查:config.yaml 与 extensions_config.json 逐节字段参考

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写

第 18 章讲配置体系的**机制**(双层文件、解析优先级、热重载边界、扩展装载);本附录反过来,做**字段级速查**——每个顶层节、每个字段的默认值 + 一句话含义。字段名一律以仓库根目录 `config.example.yaml`(config_version 39)与 `extensions_config.example.json` 的实际内容为准。

**读法约定**:

- 「默认」= 模板文件里写的值;整节被注释的(如 `subagents:`、`channels:`),其中字段默认值写在该节注释里,同样列出。
- 所有字符串值支持环境变量引用:`api_key: $OPENAI_API_KEY` 运行时解析;不要把明文密钥写进文件。
- `use:` 路径格式 `package.module:class/var`(与第 18 章一致),经 `resolve_variable`/`resolve_class` 反射加载。
- 标记 ⚠ 的是高频踩坑点。
- **旧素材命名对照**:早期书稿里的 `model/agent/terminal/display` 等顶层节在本版并不存在——模型在 `models:`,Agent 行为分散在 `subagent_runtime:`/`subagents:`/`verification:`/`skills:` 等节,「终端/沙箱」概念在 `sandbox:` + `tools:` 的 bash/文件工具 + `uploads:`,前端展示相关在 `suggestions:`/`input_polish:`/`title:`/`token_usage:`。

## A.0 文件定位、解析与环境变量

| 字段/变量 | 默认 | 含义 |
|---|---|---|
| `config_version` | `39` | schema 版本号,启动时与 example 比对,不一致告警;缺省视为 0。schema 变更后 bump,用 `make config-upgrade` 合并新字段进本地 config.yaml ⚠ |
| `log_level` | `info` | deerflow 模块日志级别(debug/info/warning/error);重启生效 |
| `logging.enhance.enabled` | `false` | 是否让日志记录携带 `trace_id` 字段(仅影响日志输出格式;trace id 本身永远签发并返回在 `X-Trace-Id` 头)⚠ 重启生效 |
| `logging.enhance.format` | `text` | 增强日志的 trace_id 呈现格式 |
| `DEER_FLOW_PROJECT_ROOT` | 仓库根 | 显式指定项目根(默认 config.yaml 所在处) |
| `DEER_FLOW_CONFIG_PATH` / `DEER_FLOW_EXTENSIONS_CONFIG_PATH` | — | 指向具体配置文件;显式指定后文件缺失会 `FileNotFoundError` 响亮失败,不静默降级 |
| `DEER_FLOW_HOME` | `.deer-flow`(项目根下) | 可写运行态数据目录(与 `database.sqlite_dir` 相关) |
| `DEER_FLOW_SKILLS_PATH` | — | `skills.path` 缺省时的技能目录覆盖 |
| `GATEWAY_WORKERS` | 1 | Gateway worker 数;>1 时需配 `run_ownership`、Redis stream bridge 等 |
| `DATABASE_URL` / `UV_EXTRAS` 等 | — | Postgres URL 与 uv extras(见 database 节) |

解析优先级(两个文件同构):显式 `config_path` 参数 > 对应 `DEER_FLOW_*_CONFIG_PATH` 环境变量 > 当前目录 > 仓库根(向后兼容后端旧位置;extensions 还会找遗留 `mcp_config.json`)。热改边界权威清单在 `backend/packages/harness/deerflow/config/reload_boundary.py::STARTUP_ONLY_FIELDS`。

顶层节总览(35 个活动节;★= 重启生效,★热 = 该节内部分字段热):

| 顶层节 | 一句话 | | 顶层节 | 一句话 |
|---|---|---|---|---|
| `config_version` | schema 版本 | | `loop_detection` | 重复工具调用熔断 |
| `log_level`★ | 日志级别 | | `read_before_write` | 写前必读闸门 |
| `logging`★ | trace_id 日志增强 | | `safety_finish_reason` | 拦截安全终止响应 |
| `token_usage` | 用量采集/展示 | | `uploads` | 上传限额与转换 |
| `token_budget` | 单次 run token 硬顶 | | `sandbox`★ | 沙箱 provider |
| `max_recursion_limit` | 递归上限钳制 | | `subagent_runtime`★ | 子代理进程准入 |
| `models` | LLM 模型列表 | | `subagent_batches`★ | 持久化批量调度 |
| `tool_groups` | 工具分组 | | `verification`★热 | 工具收据/裁决 |
| `tools`★热 | 工具清单 | | `skills`(container_path★) | 技能路径/延迟发现 |
| `tool_search` | MCP 延迟加载 | | `skill_scan` | 确定性安全检查 |
| `tool_output` | 大输出外置 | | `skill_evolution` | 技能自进化 |
| `suggestions`★热 | 追问建议 | | `title`★热 | 会话标题 |
| `input_polish`★热 | 输入润色 | | `summarization`★热 | 上下文压缩 |
| `memory`★热 | 长期记忆 | | `run_ownership`★ | 多 worker 运行归属 |
| `agents_api` | Custom Agent API 开关 | | `scheduler`★ | 定时任务调度器 |
| `database`★ | 统一持久化后端 | | `mcp_tasks`★ | 长任务 MCP 运行时 |
| `run_events`★ | 运行事件存储 | | `authorization`★热 | RBAC 授权 |
| `agent_storage`★ | 自定义 Agent 定义存储 | | — | — |

## A.1 models(模型列表)

模板中全部为注释示例,`models:` 实际为空——**首启必须至少加一条**。通用字段:

| 字段 | 默认 | 含义 |
|---|---|---|
| `name` | 必填 | 内部唯一标识,被 `subagents.agents.*.model`、`summarization.model_name` 等引用 |
| `display_name` | — | 前端展示名 |
| `use` | 必填 | 聊天模型类路径(见下) |
| `model` | 必填 | 发给 provider 的 API 模型名 |
| `api_base` / `base_url` | — | 自定义端点(Ollama 原生用 `base_url` 且**不要** `/v1` 后缀⚠) |
| `api_key` | — | 密钥,建议 `$ENV_VAR` 形式;google 系用 `gemini_api_key` |
| `timeout` / `request_timeout` / `default_request_timeout` | — | 请求超时秒(各 provider 类接受的键名不同) |
| `max_retries` | — | 失败重试次数 |
| `max_tokens` | — | **单次调用输出上限**(传给 provider)⚠ 不是上下文总量 |
| `context_window` | — | 提示+补全总容量,驱动 UI「% context used」与 fraction 触发器;三方 OpenAI 兼容模型无内置 profile,不声明则百分比不渲染/触发器丢弃 ⚠ |
| `temperature` / `top_p` | — | 采样参数(MiniMax 要求 temperature ∈ (0.0, 1.0]⚠) |
| `supports_thinking` | false | 是否支持思考模式;Anthropic 等**必须显式 true**,否则即使 UI 开了思考也静默回退非思考⚠ |
| `supports_vision` | false | 是否支持图像输入(`view_image` 工具) |
| `supports_reasoning_effort` | false | 是否支持推理力度调节 |
| `stream_usage` / `cumulative_stream_usage` | — | 流式用量;vLLM 端点在每个 chunk 报累计用量时才开后者 |
| `use_responses_api` / `output_version` | — | 用 OpenAI `/v1/responses`(仍配 `langchain_openai:ChatOpenAI`) |
| `when_thinking_enabled` / `when_thinking_disabled` | — | 思考开/关时分别合并的请求体片段(常写 `extra_body.thinking.type`;Anthropic 在 enabled 分支写 `budget_tokens`,必填、min 1024、须 < max_tokens ⚠) |
| `pricing` | — | 可选成本展示:{`currency`(全模型统一币种,混用禁用成本报告), `input_per_million`, `output_per_million`, `input_cache_hit_per_million`(缺省按 miss 价)} |
| `read_timeout`/`connect_timeout`/`write_timeout`/`pool_timeout` | — | MindIE 等高级网络设置 |

`use` 适配器速记(选型即安全):

- `langchain_openai:ChatOpenAI` —— OpenAI 与 OpenAI 兼容网关(OpenRouter/Novita/Atlas 等)通用;普通模型够用。
- `deerflow.models.patched_deepseek:PatchedChatDeepSeek` —— DeepSeek/豆包/GLM-CP/Kimi 等返回 `reasoning_content` 的推理模型(多轮工具调用需重放)。
- `deerflow.models.patched_openai:PatchedChatOpenAI` —— Gemini 经 OpenAI 兼容网关且开思考(保留 tool-call `thought_signature`)。
- `deerflow.models.patched_mimo:PatchedChatMiMo` / `patched_stepfun` / `patched_minimax` —— 各自厂商的推理内容重放 + 消息净化适配器(MiMo 重放 `reasoning_content`;MiniMax 剥离 DeerFlow 附加的 `name` 字段否则报 2013)。
- `langchain_anthropic:ChatAnthropic` —— Claude 扩展思考;`langchain_google_genai:ChatGoogleGenerativeAI` —— Gemini 原生 SDK(无思考);`langchain_ollama:ChatOllama` —— Ollama **原生** `/api/chat`(推理保真,`reasoning: true` 传 `think`,base_url 无 `/v1`);`deerflow.models.vllm_provider:VllmChatModel` / `mindie_provider:MindIEChatModel` —— vLLM/MindIE 自托管。

## A.2 工具体系:tool_groups / tools / tool_search / tool_output

`tool_groups`:默认六组 `web`、`file:read`、`file:write`、`bash`、`browser`、`knowledge`——只声明分组名,是访问控制的组织单位。

`tools[]` 每条:字段 `name`(唯一,重名覆盖⚠)、`group`、`use`(变量路径),其余字段原样透传给工具工厂。模板默认启用:

- `web_search`(DuckDuckGo,免 key,`max_results: 5`,`backend/region/safesearch` 可选)、`web_fetch`(Jina AI reader,`timeout: 10`)、`image_search`(DuckDuckGo)、文件工具 `ls`/`read_file`/`glob`(max_results 200)/`grep`(max_results 100)/`write_file`/`str_replace`、`bash`。
- `bash` 仅在隔离 shell 沙箱或 `sandbox.allow_host_bash: true` 时激活。

**web_search 可选 provider**(同名替换默认项,注释在模板中):`searxng`(自托管,`base_url`)、`serper`/`serply`/`brave`/`tavily`/`exa`/`firecrawl`/`groundroute`/`fastcrw`(云 key)、`infoquest`(`search_time_range`,-1 关时间过滤)、`tencent_wsa`(`mode`: 0 网页/1 VR/2 混合)。**web_fetch 可选**:`browserless`、`crawl4ai`(≥0.8.7 防 RCE、≥0.9 强制 token)、`exa`、`infoquest`、`firecrawl`、`groundroute`、`fastcrw`;⚠ 任一时刻只能启用一个 web_fetch provider。`web_capture`(Browserless 截图 artifact)与 `browser_*` 系列(browser_navigate/snapshot/click/type/get_text/back/screenshot/close,stateful Playwright,每线程一个浏览器)。

通用易错参数:⚠ `allow_private_addresses`/`allow_unguarded_cdp`(SSRF 守卫,生产保持 false;`cdp_url` 只能本地可信浏览器)、browser 工具多 worker 时会话驻留单个 worker 内存(`GATEWAY_WORKERS=1`)、browser_navigate 的 `timeout_ms`/`viewport_*`/`headless` 被全组共享。

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `tool_search` | `enabled` | false | MCP 工具不进上下文,只列名 + 运行时 `tool_search` 发现(省 context、多 MCP 大工具集时更准) |
| | `auto_promote_top_k` | 3 | PR1 路由元数据每轮最多自动提升的延迟工具 schema 数(1..5) |
| `tool_output` | `enabled` | true | 超大工具输出保护 |
| | `externalize_min_chars` | 12000 | 超过即落盘 + 类型化摘要 + 文件引用,模型用 read_file 读全文 |
| | `preview_head_chars` / `preview_tail_chars` | 2000 / 1000 | 摘要内嵌原始 head/tail 采样 |
| | `fallback_max_chars` | 30000 | 无法落盘时 head+tail 截断阈值 |
| | `fallback_head_chars` / `fallback_tail_chars` | 8000 / 3000 | 截断保留量 |
| | `storage_subdir` | `.tool-results` | 外置文件子目录 |
| | `exempt_tools` | [read_file, read_file_tool] | 豁免工具,防「外置→读→再外置」死循环⚠ |
| | `tool_overrides` | 注释 | 按工具覆盖阈值(如 bash: 20000) |
| `loop_detection` | `enabled` | true | 重复相同工具调用循环检测 |
| | `warn_threshold` / `hard_limit` | 3 / 5 | 连续相同工具调用先警告后硬停(window_size 20 内) |
| | `window_size` / `max_tracked_threads` | 20 / 100 | 检测窗口 / 追踪线程数上限 |
| | `tool_freq_warn` / `tool_freq_hard_limit` | 30 / 50 | 单类型工具频率阈值;`tool_freq_overrides.<tool>:{warn,hard_limit}` 可按工具放宽(如批量流水线里的 bash) |
| `read_before_write` | `enabled` | true | 未先读当前版本禁止 write_file 覆盖/append 与 str_replace;每次写使旧读失效——防长任务盲目重复追加(issue #3857) |
| `safety_finish_reason` | `enabled` | true | 拦截 provider 因安全原因终止但仍带 tool_calls 的响应(截断的 tool_calls 不可执行,issue #3028);`detectors` 缺省内置 OpenAI content_filter / Anthropic refusal / Gemini SAFETY 族,可整体覆盖 |
| `tool_progress` | 整节注释 | disabled | (RFC #3177)(thread, tool) 级停滞检测;`stagnation_threshold: 3`→WARNED、`warn_escalation_count: 2`→BLOCKED,`jaccard_similarity_threshold: 0.8` 判近似重复;默认关闭 |

## A.3 成本护栏:token_usage / token_budget / max_recursion_limit / circuit_breaker / llm_call

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `token_usage` | `enabled` | true | 每次模型调用记录 input/output/total 并在 workspace UI 展示(provider 返回时) |
| `token_budget` | `enabled` | false | 单次 run 硬性 token 预算开关 ⚠ 防跑飞成本 |
| | `max_tokens` | 200000 | 每次 run 总限额(输入+输出) |
| | `max_input_tokens` / `max_output_tokens` | null | 可选的单向独立限额 |
| | `warn_threshold` | 0.8 | 越过 80% 预算时向 agent 注入上下文警告 |
| | `hard_stop_threshold` | 1.0 | 越过后剥离 tool_calls、强制立即产出最终答案 |
| `max_recursion_limit` | — | 1000 | LangGraph super-step 上限;客户端提交值高于此被钳制(防 DoS/跑飞成本);非法/非正值回退服务端默认 100 |
| `circuit_breaker` | 整节注释 | — | LLM 调用熔断:`failure_threshold: 5` 连续失败开路,`recovery_timeout_sec: 60` 后试探恢复 |
| `llm_call` | 整节注释 | — | 进程级并发 LLM 上限:`max_concurrent_calls: 0`(0=不限制;限 burst-rate 429 的斜率;⚠ 启动期冻结需重启生效);重试 `retry_max_attempts: 3`、`retry_base_delay_ms: 1000`、`retry_cap_delay_ms: 8000`、burst 专用 `burst_retry_base_delay_ms: 5000`(provider 发 Retry-After 时忽略) |

## A.4 交互小件:suggestions / input_polish / title

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `suggestions` | `enabled` | true | 回复末尾自动生成追问建议 |
| | `max_suggestions` | 3 | 最多几条 |
| `input_polish` | `enabled` | true | 输入框草稿可重写润色 |
| | `max_chars` | 4000 | `/api/input-polish` 接受的草稿最大长度 |
| | `model_name` | null | 润色专用快速低价模型;null = 用默认聊天模型 |
| `title` | `enabled` | true | 自动会话标题 |
| | `max_words` / `max_chars` | 6 / 60 | 标题长度上限 |
| | `model_name` | null | null = 快速本地回退;填模型名走 LLM 生成 |

## A.5 uploads(上传限制)与 sandbox(沙箱)

`uploads`(应用层限制,Gateway 执行并下发给前端):

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_files` | 10 | 单次上传文件数上限 |
| `max_file_size` | 52428800(50 MiB) | 单文件上限 |
| `max_total_size` | 104857600(100 MiB) | 单次总量上限 |
| `auto_convert_documents` | false | 是否在宿主机侧自动做 Office/PDF→文本转换 ⚠ host 侧解析器风险,非完全可信来源保持 false |
| `pdf_converter` | auto | PDF→Markdown 转换器:auto(优先 pymupdf4llm,回退 MarkItDown)/ pymupdf4llm / markitdown |

`sandbox` 模板默认是 **Option 1 Local(本机直执行)**,字段:

| 字段 | 默认 | 含义 |
|---|---|---|
| `use` | `deerflow.sandbox.local:LocalSandboxProvider` | provider 类路径;换容器/微VM 沙箱改这里 |
| `allow_host_bash` | false | ⚠ Local 不是安全隔离边界,宿主 bash 默认禁;仅完全可信单用户本地开 |
| `mounts` | — | `[{host_path, container_path, read_only}]`;⚠ Docker 模式(compose)还需在 services.gateway.volumes 同步 bind-mount(issue #3244) |
| `bash_output_max_chars` | 20000 | bash 输出中段截断(头+尾,错误可能在任意位置) |
| `read_file_output_max_chars` / `ls_output_max_chars` | 50000 / 20000 | 头截断(内容前置);0=不截断 |
| `bash_command_timeout` | 600 | 单条 bash 墙钟秒上限,超时杀整组;常驻进程请后台化并重定向输出 |

**其余 5 种 provider(注释模板,选一替换 use)**:Option 2 AIO 容器(`deerflow.community.aio_sandbox:AioSandboxProvider`,macOS 自动 Apple Container/Docker):`image` ⚠ 默认镜像 `:latest` 冻结在旧 digest(缺 /v1/bash/* 路由),**显式 pin 1.11.0**;`port: 8080`、`replicas: 3`(LRU 淘汰)、`container_prefix: deer-flow-sandbox`、`thread_data_mounts`(省略=自动探测共享挂载)、`environment`($VAR 从宿主解析)、`ownership: {type: memory|redis, ...}` ⚠ 多 Gateway 实例/多 worker 共享容器后端必须 `type: redis`(compose 已设 DEER_FLOW_STREAM_BRIDGE_REDIS_URL 时自动推断);redis 归属 **fail-closed**。Option 3 BoxLite 微VM(`boxlite:BoxliteProvider`,`image: python:3.12-slim`、`memory_mib`/`cpus`、`replicas: 3`、`idle_timeout: 600`、`health_check_skip_seconds: 0.0`)。Option 4 Provisioner/K8s(AIO + `provisioner_url` + `provisioner_api_key`;Pod 镜像用 provisioner 的 SANDBOX_IMAGE 而非 sandbox.image)。Option 5 Tenki 云微VM(⚠ `project_id` 已随 Tenki 1.x 移除,用 `workspace_id`;`max_duration: 14400`、`sticky` 等)。Option 6 OpenSandbox 远程(`domain`/`protocol`(localhost 用 http,远程必须 https)/`use_server_proxy`/`sandbox_timeout` 等)。

## A.6 子代理:subagent_runtime / subagents / subagent_batches / verification / acp_agents

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `subagent_runtime` | `max_running` | 3 | 进程级并发执行槽(普通 task 与批量共享);⚠ 重启生效 |
| | `max_queued` | 64 | 异步等待队列上限(排队不占调度线程) |
| | `admission_policy` | queue | 满员策略:queue 排队 / reject 拒绝 |
| | `queue_timeout_seconds` | 300 | 排队超时 |
| `subagents`(整节注释,默认即注释值) | `timeout_seconds` | 1800 | 内置子代理默认超时(自定义 agent 默认 900) |
| | `max_turns` | 注释 | 全局最大轮数覆盖;内置默认 general-purpose=150、bash=60 |
| | `max_total_per_run` | 6 | 单次 lead run 委托总数背停(1..50;允许两次默认并发 3 的整批);请求可临时 `max_total_subagents` 覆盖 |
| | `token_budget` | 注释 | 子代理单 run token 顶:`{enabled, max_tokens: 2000000, warn_threshold: 0.7}`;触顶时结果盖 `subagent_stop_reason=token_capped`;per-agent 覆盖优先 |
| | `agents.<name>` | — | per-agent 覆盖:`timeout_seconds`/`max_turns`/`token_budget`/`model`(须匹配 models: 中的 name)/`skills`(发现/激活白名单,默认全部) |
| | `custom_agents.<name>` | — | 自定义子代理类型:`description`/`system_prompt`/`tools`(白名单,null=继承全部)/`skills`/`model`(inherit 继承父)/`max_turns`/`timeout_seconds`;经 `task` 工具可用 |
| `subagent_batches` | `enabled` | false | 持久化原生子代理批量 ⚠ 开启会显著增加模型用量,且要求 database.backend = sqlite/postgres |
| | `poll_interval_seconds` / `lease_seconds` | 1 / 120 | 轮询间隔 / 领取租约 |
| | `max_items_per_batch` | 5000 | 一批持久化 item 总数上限 |
| | `default_max_live_items` / `max_live_items_per_batch` | 100 / 1000 | 存活(pending)item 默认/单批上限 |
| | `default_max_running_items` / `max_running_items_per_batch` | 3 / 64 | 真实执行槽默认/单批上限 |
| | `max_attempts` | 3 | 失败重试次数 |
| | `max_result_chars` / `result_preview_max_chars` | 100000 / 2000 | 结果存储上限 / 超限保留的文本预览 |
| `verification` | `receipts_enabled` | true | 给工具结果盖确定性收据并注入上下文,供最终报告引用(可关) |
| | `receipts_render_mode` | `"delegation_only"` | 主链账本渲染:仅处理子代理结果时渲染(子代理链恒渲染) |
| | `judge_enabled` / `judge_model_name` | false / null | 验收标准审查裁决器,默认关闭 |
| `acp_agents`(整节注释) | `<name>` | — | 外部 ACP agent 配置:`command`+`args`(mcode 原生 ACP;claude/codex CLI 需 ACP 适配器如 `@zed-industries/claude-agent-acp`/`codex-acp`)、`description`、`model: null`、`auto_approve_permissions: false`⚠、`timeout_seconds: 1800`、`env`($VAR);供内置 `invoke_acp_agent` 工具使用 |

## A.7 技能:skills / skill_scan / skill_evolution

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `skills` | `path` | 项目根 `skills/` | 宿主技能目录(相对项目根或绝对);可由 `DEER_FLOW_SKILLS_PATH` 覆盖 |
| | `container_path` | `/mnt/skills` | 沙箱容器内挂载点 ⚠ AIO/provisioner/E2B 要求规范绝对非根路径且不与平台保留挂载重叠;改后**重启 Gateway**(快照身份/挂载/同步) |
| | `deferred_discovery` | false | true 时系统提示只放 `<skill_index>`(仅名),agent 经 `describe_skill` 按需取详情——技能多时保 prefix-cache 友好 |
| `skill_scan` | `enabled` | true | 安装/更新技能前确定性安全分析(嵌套归档、密钥模式等);归档路径穿越/符号链接/可执行二进制/大小与条目数限制及 LLM 扫描器无条件运行 |
| `skill_evolution` | `enabled` | false | 允许 agent 自主在 `skills/custom/` 建/改技能 ⚠ |
| | `moderation_model_name` | null | 安全扫描模型(null = 默认模型) |
| | `security_fail_closed` | true | 裁决模型不可用:true 全禁写;false 仅放行非可执行内容(可执行永远禁) |

## A.8 summarization(上下文压缩)

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | true | 自动压缩开关 |
| `model_name` | null | null = 用 run 实际所用模型压缩(**不是 models[0]**⚠);指定模型失败时回退 run 自身模型,压缩不会因坏 provider 失效 |
| `trigger` | `[{type: tokens, value: 32000}]` | 触发条件,**任一满足即压缩**(OR);可选 `type: messages`、`type: fraction`(如 0.8)⚠ fraction 阈值从「压缩所用模型」声明的 `context_window` 解析,没声明则该子句带警告丢弃、剩余绝对子句照常工作 |
| `keep` | `{type: messages, value: 10}` | 压缩后保留的近期历史;也可 `type: tokens` 或 `type: fraction`(占模型 max input 比例) |
| `trim_tokens_to_summarize` | 15564 | 准备压缩时最多保留的 token 数(null = 不裁剪,超长会话不建议) |
| `summary_prompt` | null | 自定义压缩提示词(null = 默认 LangChain prompt) |
| `skill_file_read_tool_names` | [read_file, read, view, cat] | 视为「读 SKILL.md」的工具名:被读的技能引用写入 durable skill_context 通道并在压缩后以名/路径/描述提醒重注入(旧 `preserve_recent_skill_*` 设置已弃用);`[]` 关闭捕获 |

## A.9 memory(长期记忆)

顶层字段:

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | true | 记忆机制总开关(调用点门) |
| `injection_enabled` | true | 是否注入系统提示(调用点门) |
| `shutdown_flush_timeout_seconds` | 30.0 | Gateway 优雅停机排空待处理记忆更新的硬预算(每项一次 LLM 调用;须 < K8s terminationGracePeriodSeconds) |
| `manager_class` | deermem | 后端选择:注册名(deermem/mem0/noop/openviking/honcho)或 MemoryManager 子类点分路径 |
| `mode` | middleware | middleware = 每轮被动后台抽取;tool = 实验性,模型直接调 memory_search/add/update/delete(mem0 等在 tool 模式仍保留被动写) |
| `backend_config` | 见下 | 后端私有配置 dict,原样透传——**各后端键完全不同,注意键在 backend_config 下而非顶层**⚠ |

`backend_config`(deermem 私有键,示例文件实际写出):

| 字段 | 默认 | 含义 |
|---|---|---|
| `storage_path` | `""` | 空 = deer-flow base_dir(工厂注入绝对 runtime_home);非空 = 数据根目录(每用户 `{storage_path}/users/{uid}/memory.json`) |
| `storage_class` | file | file 或点分 MemoryStorage 类路径;非法持久后端启动即败 |
| `strict_user_scope` | false | 认证部署且全调用方传 user_id 后置 true |
| `manifest_filename` | memory.json | 用户级清单文件(只含版本/revision/时间与历史,不含 facts) |
| `file_lock_timeout_seconds` | 10 | 跨进程 advisory 锁超时 |
| `retrieval_adapter` | fts5 | 检索适配器;空 = 禁用检索,或点分 RetrievalPort 工厂 |
| `debounce_seconds` | 30 | 处理排队更新的防抖等待 |
| `queue_max_depth` | 1000 | 待处理项背压上限;0 = 无限;满时拒绝普通更新、signal 更新永远准入 |
| `model` | 空 | 抽取用 LLM:{provider, model, api_key, base_url, temperature};**全省略 = 无抽取**(非 LLM 操作仍可用,更新会报错) |
| `max_facts` | 100 | 事实存储上限 |
| `fact_confidence_threshold` | 0.7 | 事实入库最低置信 |
| `fact_eviction_policy` | confidence | 容量淘汰:confidence(纯置信排序)/ hybrid-v1(置信 65% + 明示确认新鲜度 25% + 查询热度 10%);`fact_eviction_shadow_enabled: false` 可开影子审计 |
| `max_injection_tokens` | 2000 | 记忆注入 token 预算 |
| `token_counting` | tiktoken | 注入预算计数:tiktoken(准,⚠ 首次使用可能联网下载 BPE)或 char(免网、CJK 感知) |
| `guaranteed_categories` / `guaranteed_token_budget` | [correction] / 500 | 绕过常规预算、走保留额度的强制注入类别(如「别用 pip 用 uv」类纠正) |
| `staleness_review_enabled` | true | 过期事实老化评审(随正常 memory-update 同一次 LLM 调用,KEEP/REMOVE/EXTEND) |
| `staleness_age_days` / `staleness_min_candidates` / `staleness_max_removals_per_cycle` | 90 / 3 / 10 | 老化阈值 / 最少候选 / 单轮最多删除 |
| `staleness_protected_categories` | [correction] | 豁免类别 |
| `staleness_max_lifetime_multiplier` | 20.0 | 新建事实 LLM 自估 expected_valid_days 的上限乘子(90×20≈5 年) |
| `staleness_max_extension_days` | 3650 | EXTEND 后 expected_valid_days 绝对顶(≈10 年) |
| `consolidation_enabled` | false | 事实合并(有损,源事实被合成事实取代)⚠ 默认关 |
| `patterns_dir` / `prompts_dir` | `""` | 覆盖内置信号检测 patterns 与抽取 prompt 模板的目录(空 = 内置) |

OpenViking 适配(参考注释):`base_url`、`owner_user_id`、`api_key_env`、`timeout_seconds`、`default_peer_id`、`startup_policy: fail_fast`、`failure_policy: {read: fail_open, write: log_and_drop}`、`retrieval: {top_k: 8, score_threshold, max_injection_chars, content_mode, injection_query}`;Docker 访问宿主实例用 `http://host.docker.internal:1933` + `allow_insecure_http`。Honcho:`base_url`、`workspace_prefix: deerflow-u-`、`assistant_peer`。

## A.10 存储:database / checkpointer / run_events / agent_storage / dedupe_storage

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `database` | `backend` | sqlite | 统一后端驱动 LangGraph checkpointer + Store + DeerFlow 应用数据;memory(不持久)/ sqlite(单机)/ postgres(生产多节点) |
| | `sqlite_dir` | `.deer-flow/data` | sqlite 单文件 deerflow.db(WAL)位置;整节省略时默认 sqlite + 该目录 |
| | `postgres_url` | $DATABASE_URL | 从 .env 引 `DATABASE_URL`;驱动:本地 `make dev` 自动探测并 `--extra postgres`,或设 `UV_EXTRAS=postgres` |
| | `postgres_schema` | — | ORM + checkpointer/store 表放指定 schema(仅对新表生效;迁移存量表要连 alembic_version 一起挪,漏了会重播迁移⚠) |
| | `pool_recycle` / `command_timeout` | 300 / 30 | ORM 连接回收 / 命令超时 |
| | `checkpoint_channel_mode` | full | full = 全量消息 checkpoint;delta = 消息走 DeltaChannel(全进程须一致) |
| | `checkpoint_delta.snapshot_frequency` | 10 | delta 模式下每 N 次 per-step 写做一次全量快照 |
| | `checkpoint_graph_cache.accessor_graph_max` | 64 | 编译图缓存上限(热改) |
| `checkpointer`(⚠ 已弃用) | `type` / `connection_string` | — | 旧独立配置;与 database 并存时 **checkpointer 优先**管 LangGraph checkpointer+Store,database 只管应用数据;建议删掉改用 database |
| `run_events` | `backend` | memory | 运行事件存储:memory(重启丢)/ db(ORM,生产)/ jsonl(追加式单机) |
| | `max_trace_content` | 10240 | db 后端 trace 内容截断(字节) |
| | `track_token_usage` | true | 把 token 用量累计进 RunRow |
| `agent_storage` | `backend` | file | 自定义 Agent 定义(config.yaml+SOUL.md)与部署级 managed subagent 定义存哪:file(每用户文件 + managed-subagents/ JSON)/ db(SQL,多节点可见,要求 database 为 sqlite/postgres);换 db 用 `backend/scripts/migrate_agents_to_db.py` 迁移 |
| `dedupe_storage`(整节注释) | `backend` | auto | 入站 webhook 去重状态:auto(Postgres 优先,否则进程内)/ memory / postgres;多副本部署必须 postgres |

## A.11 运行治理:scheduler / mcp_tasks / run_ownership / stream_bridge

| 节 | 字段 | 默认 | 含义 |
|---|---|---|---|
| `scheduler` | `enabled` | false | 定时/周期(cron)agent 运行后台调度器主开关 |
| | `multi_instance` | false | 跨 Gateway 实例租约恢复 ⚠ true 要求共享 Postgres + `run_ownership.heartbeat_enabled` + `run_events.backend: db`,否则启动拒绝 |
| | `poll_interval_seconds` / `lease_seconds` | 5 / 120 | 扫描间隔 / 崩溃进程任务可回收延迟 |
| | `max_concurrent_runs` | 3 | launching/running 全局并发顶(跨实例,Postgres advisory 锁) |
| | `queue_timeout_seconds` | 3600 | 持久队列最长等待,超时该次发生标记失败 |
| | `min_once_delay_seconds` | 60 | 一次性任务创建时的最小未来偏移 |
| | `recursion_limit` | 1000 | 调度 run 的 super-step 上限(与 Web UI 一致),超 `max_recursion_limit` 被钳制;此项无需重启、dispatch 时读取 |
| `mcp_tasks` | `enabled` | false | 普通 submit/status/cancel MCP 任务工具集的持久运行时(配套 extensions_config.json 里每服务器的 `task_toolsets`) |
| | `poll_interval_seconds` / `lease_seconds` | 5 / 120 | 轮询与租约 |
| | `max_concurrent_polls` | 8 | 单 worker 每次扫描最多发起的 status 调用 |
| | `max_poll_backoff_seconds` | 300 | 瞬时错误指数退避上限 |
| | `input_required_poll_interval_seconds` | 60 | 等待用户输入时的最小轮询间隔 |
| | `tracking_degraded_after_errors` | 3 | 连续错误数,超过后查询 API 报 tracking_degraded |
| | `max_result_bytes` / `result_preview_max_chars` | 65536 / 2000 | 完整 JSON 结果存储上限 / 超限文本预览 |
| `run_ownership` | `lease_seconds` | 30 | 运行租约秒数(心跳每 lease/3 续);renew 失败则本机取消运行 |
| | `grace_seconds` | 10 | 过期后回收孤儿运行的额外宽限 = 跨 worker 时钟偏移预算 ⚠ 多 worker 时钟须 NTP 同步(领先 >grace 的 peer 可能误回收活 run) |
| | `heartbeat_enabled` | false | `GATEWAY_WORKERS > 1` 时置 true |
| `stream_bridge`(整节注释) | `type` | memory | SSE 事件桥:memory(单进程)/ redis(Docker/multi-worker;compose 已自动设 DEER_FLOW_STREAM_BRIDGE_REDIS_URL) |
| | `queue_maxsize` | 256 | 每 run 保留的数据事件数,更早的重连收 SSE `gap` |
| | `heartbeat_interval_seconds` | 15 | 心跳间隔(max 86400) |
| | `stream_ttl_seconds` / `recovered_stream_cleanup_delay_seconds` / `max_connections` | 86400 / 60 / — | redis 滚动 TTL / 孤儿流清理延迟 / 连接池顶 |
| | — | — | ⚠ redis 桥 fail-hard:mid-run Redis 故障直接失败该 run,无自动降级 |

## A.12 渠道与身份:channels / channel_connections / auth / agents_api

`channels`(整节注释;全部 IM 走**出站**连接,无需公网 IP):共享字段 `langgraph_url`(默认 http://localhost:8001/api,Docker 用服务名 gateway)、`gateway_url`、`inbound_queue_maxsize: 1000`、`max_concurrency: 5`、`shutdown_grace_period_seconds: 3`、`session:{assistant_id: lead_agent, config:{recursion_limit: 100}, context:{thinking_enabled, is_plan_mode, subagent_enabled}}`。各渠道最小字段:feishu(app_id/app_secret/domain 中 or 国际)、slack(bot_token xoxb + app_token xapp,allowed_users)、telegram(bot_token, allowed_users, `rich_messages: false`)、wechat(bot_token/ilink_bot_id + qrcode_login_enabled: true 引导、各类大小/扩展名限制、每渠道 session 与 per-user 覆盖)、wecom(bot_id/bot_secret)、dingtalk(client_id/secret/card_template_id)、discord(bot_token/allowed_guilds/mention_only/allowed_channels/thread_mode)、buzz(relay_url/private_key;⚠ **DENY-BY-DEFAULT**:空 allowed_users = 谁都不能触发)。

`channel_connections`(整节注释):`enabled: false`、`require_bound_identity: true`(关掉会让未绑定外部 IM 用户开 thread/run)、各渠道 enabled 开关。

`auth`(整节注释):`local.allow_registration: false`(⚠ 默认开放注册——SSO 部署务必关;`/initialize` 建首个 admin 不受此限)、`max_login_attempts: 5` / `lockout_seconds: 300`(每 IP 登录节流,热读生效);`oidc.enabled: true` + `frontend_base_url` + `providers.<name>:{display_name, issuer, client_id, client_secret, redirect_uri, scopes, token_endpoint_auth_method, auto_create_users: true, require_verified_email: true, allowed_email_domains, admin_emails, pkce_enabled: true, nonce_enabled: true}`。

`agents_api`: `enabled: false` —— 是否开放 custom-agent SOUL/USER.md 的 HTTP 管理 ⚠ 仅在可信认证管理边界后开启。

## A.13 授权与守卫:authorization / guardrails / 注解块

`authorization`(活动节,默认 **禁用**):

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | false | 细粒度资源授权(RBAC 等)总开关;关 = 每个认证用户可访问一切(注释示例块里写 true 仅作演示 ⚠ 实际默认值以活动键 `enabled: false` 为准) |
| `fail_closed` | true | provider 出错 / 身份无法解析时拦截 |
| `default_role` | user | user_role 为空时应用的角色;内置 RBAC 需要该角色存在于下方 roles |
| `provider.use` | `deerflow.authz.rbac:RbacAuthorizationProvider` | 授权实现 |
| `provider.config.roles.<role>` | admin/user/guest 示例 | 每角色四类资源的 allow/deny 策略:`tools`、`routes`(如 threads:read/runs:read)、`models`、`sandbox`;已知角色对某资源无策略 = 不受限;guest 示例禁 sandbox 执行 + 只放 web_search |

`guardrails`(整节注释):工具调用前预执行授权,三选一:内置 `AllowlistProvider`(`config.denied_tools: ["bash","write_file"]`)、OAP passport provider(`aport_guardrails.providers.generic:OAPGuardrailProvider`)、自定义 provider(任意含 evaluate/aevaluate 的类)。

**扩展注解块**(都住在 config.yaml,都是 operator 信任边界 ⚠ 变更即重启):
`extensions.middlewares` —— 配置声明的 AgentMiddleware 类路径列表(留注释则 extensions_config.json 的 middlewares 为唯一来源);`plugins:` —— Python 扩展列表,每条 `{name, package, use: module:install, enabled, required(加载失败是否中止启动), table_prefix(扩展自有的表名前缀,防 alembic autogenerate 误删), config}`;用 `make extension-install/list/enable/disable/remove` 管理,安装器同步改 backend/pyproject.toml + uv.lock;SSH Git 源被拒;本地目录拷贝快照到 backend/extensions/sources。Monocle 链路追踪走环境变量(`MONOCLE_TRACING`/`MONOCLE_EXPORTERS`/`OKAHU_API_KEY`),不在 YAML 配置。

## A.14 extensions_config.json(MCP 服务器 + 技能开关 + 中间件)

文件由 Gateway API 运行期改写(`PUT`/`PATCH /api/mcp/config`、MCP 启用开关、技能更新),生产 compose 将其挂载为**读写**,config.yaml 保持 `:ro`。顶层键:

```jsonc
{
  "middlewares": [],          // AgentMiddleware 类路径列表(load 进 lead 链)
  "mcpInterceptors": [],      // "module.path:build_factory" 字符串列表(自定义 MCP 拦截器)
  "mcpServers": { /* 名 → 配置 */ },
  "skills": {}                // 技能名 → { "enabled": true }
}
```

`mcpServers.<server>` 字段:

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | true | 该服务器是否启用(可用例全 false 占位) |
| `type` | stdio | 传输:stdio / sse / http;官方 schema 的 `transport` 拼写亦接受(别名,type 优先)⚠ |
| `command` + `args` + `env` | — | stdio:启动命令与参数;`env` 值支持 `$VAR` 宿主解析 |
| `url` + `headers` | — | sse/http:端点与静态头(仅启动发现用) |
| `tool_name_prefix` | true | 发现工具是否加服务器名前缀防跨服务器重名 |
| `session_init_timeout` | 60 | 服务器拉起超时(子进程 + initialize + tools/list);注释掉可无超时 |
| `tool_call_timeout` | null | 单次工具调用超时(stdio 与所有传输的 task 调用);HTTP/SSE 普通工具走传输级超时 |
| `description` | `""` | 该 MCP 服务器提供什么(给模型看) |
| `routing` | `{mode: off, priority: 0, keywords: []}` | PR1 软路由提示:`mode` off/prefer、`priority` 高者先渲染(0–100 钳制)、`keywords` 关键词;例中 postgres 用 prefer+50 |
| `tools.<tool名>` | — | per-tool 覆盖:`{routing: {...}}`(合并覆盖服务器级 routing) |
| `oauth` | — | HTTP/SSE OAuth:`{enabled, token_url, grant_type: client_credentials|refresh_token, client_id/secret, refresh_token, scope, audience, token_field: access_token, token_type_field, expires_in_field, default_token_type: Bearer, refresh_skew_seconds: 60, extra_token_params}` |
| `user_auth` | — | 每用户凭据注入:`{enabled, header: Authorization, users: {deerflow用户id: "Bearer <token>"}, on_missing: deny|passthrough}`;fail-closed,未映射用户报 ToolException;静态 headers 只用于发现 |
| `headers_from_context` | — | 每请求凭据注入:`{enabled, headers: {头名: context.secrets 键}, on_missing}`;不存凭据只存映射,可明文返回 API;与 user_auth/oauth 并存时优先级 static headers < oauth < user_auth < headers_from_context |
| `task_toolsets` | [] | 普通长任务工具组:`[{name, submit_tool, status_tool, cancel_tool}]`(工具名 = 服务器原始名,无前缀);⚠ 同一原始工具名不得占两个 toolsets/角色;需要 A.11 `mcp_tasks.enabled` 与 db 持久化,否则启动失败 |

`mcpInterceptors` 与「启用 task_toolsets 的服务器」的运行时绑定在 Gateway 启动快照冻结,热漂移会在工具发现前明确失败;`middlewares` 走中间件链(与 `config.yaml` 的 `extensions.middlewares` 二选一为源)。配置文件读写由双重锁(进程内 + 侧车 advisory 锁)串行化,原子写失败(EBUSY)时回退非原子覆盖。

## A.15 ⚠ 高频易错清单(汇总)

1. `max_tokens` 是**单次输出上限**,`context_window` 才是总容量;fraction 触发器没声明 context_window 会整句被丢弃(带警告)。
2. Anthropic 开思考:`supports_thinking: true` + `when_thinking_enabled.thinking.budget_tokens`(≥1024 且 < max_tokens),缺一不可。
3. Ollama 用 `langchain_ollama:ChatOllama` 且 base_url 无 `/v1`;OpenAI 兼容端点会丢 reasoning_content。
4. MiMo/StepFun/MiniMax/Gemini-网关 的推理模型务必用对应 `patched_*` 适配器,否则多轮工具调用报错或思考内容丢失。
5. MiniMax `temperature` 必须 ∈ (0.0, 1.0];M2.x 永远思考,别配 disabled。
6. 多个同 `name` 工具互相覆盖;web_fetch 系一次只启用一个 provider。
7. `allow_host_bash`/`allow_private_addresses`/`allow_unguarded_cdp` 默认都该保持 false(SSRF/逃逸边界)。
8. browser 工具组多 worker 会丢会话:`GATEWAY_WORKERS=1`。
9. 改 schema 后 bump `config_version` 并跑 `make config-upgrade`,别手抄旧文件。
10. `subagent_batches`、`scheduler`、`mcp_tasks` 开启前先确认持久化后端(database/run_events)够格;多实例另需 redis/ownership/heartbeat。
11. 沙箱 AIO 默认 `:latest` 是旧 digest,显式 pin 版本;多实例共享容器后端必配 `ownership.type: redis`。
12. 记忆 `token_counting: tiktoken` 首次可能联网;受限网络用 `char`。改 `manager_class` 时 backend_config 整套键随后端换。
13. `uploads.auto_convert_documents` 默认 false 是有意为之(宿主解析器风险)。
14. `channels.buzz` 空 allowed_users = 全员拒绝(与其他渠道相反)。
15. `plugins`/`extensions.middlewares`/`mcpInterceptors` 都执行代码——只信 operator 源;每次改动重启生效。
16. 两个配置文件解析都「显式指定则缺失即错」;Docker 下把可变配置文件放在目录 bind-mount 而非单文件挂载(编辑器替换文件会让单文件挂载失效)。
