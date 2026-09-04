# 第 2 章　仓库全景与技术栈

> 基于 DeerFlow 最新源码（本仓库 commit `2672e209`，2026-09）编写。

> **旧版说明**：本文融合两本旧书对应章节（hawkli 版"核心概念/项目结构"、coolclaws 版"仓库全景与技术栈"），但一切以重构后的**最新 monorepo 结构**为准。旧书基于数月前的 DeerFlow（`backend/src/` 单包布局、独立 LangGraph Server、`skill.yaml` 等），其中大量路径与概念在 2.x 已作废。凡冲突处，以本章为准；文末 2.13 给出了新旧目录对照表，供旧书读者迁移。

## 本章导航

| 想看什么 | 去哪一节 |
|---|---|
| 顶层目录树、每个目录干嘛的 | 2.2 |
| 版本号怎么对齐（lockstep） | 2.1 |
| harness / app 分层与依赖边界 | 2.3、2.4 |
| deerflow-harness 内部模块地图 | 2.4 |
| Gateway API 路由、IM channels | 2.5 |
| deerflow_extension_api 扩展契约 | 2.6 |
| frontend（Next.js）结构 | 2.7 |
| 服务拓扑、端口、Nginx 路由 | 2.8 |
| config.yaml / extensions_config.json 配置体系 | 2.9 |
| skills / contracts / scripts / 测试 | 2.10、2.11 |
| 技术栈总表与模型适配 | 2.12 |
| 旧书目录 → 新目录对照 | 2.13 |

## 2.1　仓库身份与版本 lockstep

DeerFlow 是 **bytedance/deer-flow** 开源的 **LangGraph-based AI super-agent system**（MIT License，Python ≥3.12 + Node.js ≥22）。README 标题自述为 *"**D**eep **E**xploration and **E**fficient **R**esearch **Flow**"*——一个编排 sub-agents、memory、sandbox 的"超级 Agent 框架"，能力靠可扩展的 skills 注入。

2.0 是**推倒重写的版本**：与 v1（纯 Deep Research 框架）不共享代码，v1 维护在 `main-1.x` 分支；本仓库是 2.x 主线。当前 release 版本为 **2.1.0**（2026-09 检出）。

**版本 lockstep 是仓库的硬约束**：一个发布版本必须在四个"版本源"中完全一致——

| 版本源 | 当前值（本检出） |
|---|---|
| `backend/pyproject.toml` → `[project] version`（应用包 `deer-flow`） | `2.1.0` |
| `backend/packages/harness/pyproject.toml` → `[project] version`（`deerflow-harness`） | `2.1.0` |
| `frontend/package.json` → `version` | `2.1.0` |
| `deploy/helm/deer-flow/Chart.yaml` → `version` + `appVersion` | `2.1.0` |

例外是 `deerflow-extension-api`：它是**独立的契约版本号**（当前 `0.2.0`），不与 release 版本对齐。harness 对它做**精确锁定**：

```toml
# backend/packages/harness/pyproject.toml
# Exact pin by design (extension-system version contract): the host pins
# the contract version it implements, extensions declare ranges. A range
# here would let pip resolve a newer contract package than this harness
# implements, making newer extensions look supported at runtime.
"deerflow-extension-api==0.2.0",
```

配套两个脚本维护 lockstep：

```bash
scripts/bump_version.sh <ver>     # 一次对齐四个版本源
scripts/verify_versions.sh <ver>  # 校验无漂移（推送 v* tag 时 CI 强制执行，
                                  # 任何源漂移都会阻塞全部发布）
```

详见 `RELEASING.md`。开发规范上还有几条仓库级公约：文档与代码同 PR 同步更新（用户侧改 README、架构侧改 AGENTS.md）；后端强制 TDD；`CLAUDE.md` 只是 `@AGENTS.md` 的导入壳，不许直接编辑。

## 2.2　顶层目录地图

仓库根（root `AGENTS.md` 的官方地图）：

```
deer-flow/
├── Makefile                        # 根编排：dev/start/stop、docker、setup、extension-*
├── config.example.yaml             # 模板 → 复制为 config.yaml（gitignored）
├── extensions_config.example.json  # 模板 → 复制为 extensions_config.json（gitignored）
├── backend/                        # Python 后端（见 2.3～2.6）
│   ├── Makefile                    # 后端命令：dev/gateway/test/lint/migrate-rev
│   ├── app/                        # FastAPI Gateway + IM channels（import: app.*）
│   ├── packages/
│   │   ├── extension-api/          # deerflow-extension-api（import: deerflow_extension_api.*）
│   │   └── harness/                # deerflow-harness（import: deerflow.*）
│   ├── extensions/sources/         # 本地安装 Python 扩展的可部署快照
│   ├── tests/  scripts/  docs/  samples/
│   └── langgraph.json              # LangGraph 图形配置
├── frontend/                       # Next.js（pnpm）——见 2.7
├── docker/                         # compose 文件、nginx、provisioner ——见 2.8
├── skills/                         # skills/public（入库）+ skills/custom（gitignored）——见 2.10
├── contracts/                      # 跨组件 JSON 契约（subagent 状态、skill 评审等）
├── examples/deerflow-extension-example/  # 参考扩展：演示全部 5 类贡献点
├── scripts/                        # 根编排脚本（check/configure/doctor/serve/nginx/docker/deploy/wizard…）
├── tests/                          # 根级测试（目前 tests/skills/：public skill 测试）
├── docs/                           # 横向文档、计划、设计笔记
├── deploy/helm/deer-flow/          # Helm Chart（生产 K8s 部署）
└── .github/                        # CI；skill-review-waivers.v1.json
```

要点：

- **monorepo 取向层在根 `AGENTS.md`**：它只做全局地图与跨切面公约，深度内容下沉到 `backend/AGENTS.md`（harness/app 拆分、中间件链、sandbox、MCP、skills、memory、IM、持久化、配置、测试布局）和 `frontend/AGENTS.md`（App Router、线程/流式数据流、命令）。各子系统还有自己的 `AGENTS.md`（如 `backend/packages/harness/deerflow/extensions/AGENTS.md`、`mcp/AGENTS.md`、`runtime/AGENTS.md`），遵循"就近文件"原则。
- **运行时配置在仓库根**，而不是 backend 下——`config.yaml`（应用主配置）与 `extensions_config.json`（MCP servers + skills 开关）都被 gitignore，可通过 Gateway API 运行时改写。
- **一个 `make dev` 跑通全栈**：Nginx(2026) + Gateway(8001) + Frontend(3000)（+ 可选的 Provisioner 8002、compose 内的 redis）。详见 2.8。

## 2.3　backend：三个 Python 包与 harness/app 分层

这是与旧书差异最大的地方。旧版是 `backend/src/` 单一大包；现在 backend 拆成**三个发行单元**：

| 包 | 目录 | import 前缀 | 是否发布 | 角色 |
|---|---|---|---|---|
| `deer-flow`（应用） | `backend/app/` | `app.*` | 否 | FastAPI Gateway + IM channels + 后台服务，**不可复用** |
| `deerflow-harness` | `backend/packages/harness/deerflow/` | `deerflow.*` | 是 | 可发布的 Agent 框架：编排、工具、sandbox、models、MCP、skills、config |
| `deerflow-extension-api` | `backend/packages/extension-api/deerflow_extension_api/` | `deerflow_extension_api.*` | 是 | 宿主无关的**公共扩展契约**（2.6） |

应用包 `deer-flow` 直接声明两个依赖，尽管 harness 已传递引入：

```toml
# backend/pyproject.toml
dependencies = [
    "deerflow-harness",
    # Direct dependency on purpose ... the app declares them rather than
    # relying on the harness to keep supplying it.
    "deerflow-extension-api",
    "fastapi>=0.115.0", "httpx>=0.28.0", "python-multipart>=0.0.31",
    "sse-starlette>=2.1.0", "starlette>=1.3.1,<2", "uvicorn[standard]>=0.34.0",
    "lark-oapi>=1.4.0", "slack-sdk>=3.33.0", "python-telegram-bot>=21.0",
    "wecom-aibot-python-sdk>=0.1.6", "dingtalk-stream>=0.24.3",
    "langgraph-sdk>=0.1.51", "bcrypt>=4.0.0", "pyjwt>=2.13.0",
    "email-validator>=2.0.0", "e2b-code-interpreter>=2.8.1",
    ...
]
```

### harness / app 依赖方向（本书最重要的架构红线）

> **依赖规则：App 可以 import deerflow，deerflow 永远不许 import app。**

这条边界不是口头约定，而是被测试锁死的：

```python
# backend/tests/test_harness_boundary.py（CI 强制执行）
"""Boundary check: harness layer must not import from app layer.
The deerflow-harness package (packages/harness/deerflow/) is a standalone,
publishable agent framework. It must never depend on the app layer (app/)."""
BANNED_PREFIXES = ("app.",)
# …AST 扫描 harness 下所有 *.py，发现 from app. / import app. 即失败
```

因此 harness 是**可独立发布、可被他人直接 pip 使用**的框架（`deerflow-harness` 发布名）；app 是套在它外面的"产品壳"。阅读源码时判断归属只需看 import：

```python
# harness 内部
from deerflow.agents import make_lead_agent
from deerflow.models import create_chat_model

# app 内部
from app.gateway.app import app
from app.channels.service import start_channel_service

# App → Harness（合法）
from deerflow.config import get_app_config

# Harness → App（禁止，test_harness_boundary.py 会拦下）
# from app.gateway.routers.uploads import ...   # ← CI 直接失败
```

另一条 import 卫生规则：`deerflow.agents`、`deerflow.subagents` 等包根是**懒入口**（lazy），内部模块若只需轻量类型/配置/注册表，应 import 具体子模块，避免拉起整个工具图或子代理执行器。

### backend/ 目录其余内容

```
backend/
├── langgraph.json            # LangGraph Studio 图形配置（见 2.5）
├── packages/{harness,extension-api}/
├── app/                      # gateway + channels + mcp_tasks/scheduler/subagent_batches
├── extensions/sources/       # `deerflow extensions install` 落地的扩展快照
├── tests/                    # pytest 套件；tests/blocking_io 子目录由 test-blocking-io 单独把关
├── scripts/benchmark/        # 独立可复现的后端基准（如 deermem_eviction、concurrency）
├── docs/                     # 后端文档（CONFIGURATION.md/ARCHITECTURE.md/API.md/…）
├── samples/                  # 示例（other_agent_demo 等）
└── Makefile                  # install/dev/gateway/test/test-live/test-blocking-io/lint/format/migrate-rev
```

`make migrate-rev MSG="..."` 自动生成 Alembic 迁移——持久化层（SQLAlchemy + Alembic）位于 harness 的 `deerflow/persistence/`。

## 2.4　deerflow-harness 内部模块地图

harness 包（`backend/packages/harness/deerflow/`）是 Agent 框架本体，模块如下（与 backend/AGENTS.md 的目录地图一致）：

```
deerflow/
├── agents/           # LangGraph Agent 系统
│   ├── lead_agent/   #   主 Agent（factory + system prompt）
│   ├── middlewares/  #   中间件链（40+ 个文件：memory、summarization、sandbox、
│   │                 #   todo、token_budget、tool_error_handling、uploads、view_image、
│   │                 #   loop_detection、read_before_write、safety_finish_reason、
│   │                 #   subagent_limit、clarification、mcp_routing、input_sanitization…）
│   ├── memory/       #   记忆抽取、队列、prompt（DeerMem 长期记忆）
│   ├── factory.py    #   create_deerflow_agent() SDK 级入口
│   ├── features.py   #   RuntimeFeatures 声明
│   └── thread_state.py  # ThreadState schema
├── runtime/          # RunManager + run_agent() + StreamBridge；runs/ store/ checkpointer/
│                     # events/ stream_bridge/ checkpoint_cache/ journal/ goal/ context_compaction
├── sandbox/          # Sandbox 抽象、local 实现、sandbox 工具、生命周期中间件
├── subagents/        # 子代理：registry.py / executor.py / builtins/（general-purpose、bash）
├── tools/            # 工具：builtins/（present_file、ask_clarification、view_image、
│                     # review_skill_package、task/background_tasks/batch_task、tool_search…）+ 搜索注册
├── mcp/              # MCP 集成（client、tools、cache、McpTaskService 长任务运行时）
├── skills/           # Skill 发现、加载、解析（SKILL.md）、安装、校验、安全扫描
├── extensions/       # Python 插件：loader、registry、placement、isolation、CLI、gateway 桥
├── models/           # 模型工厂与供应商适配（thinking/vision 支持）
├── config/           # 配置系统：每块配置一个 _config.py（app/model/sandbox/tool/memory/…）
├── community/        # 社区/三方能力：搜索（tavily、ddg、brave、searxng、serper、exa、firecrawl、
│                     # jina、tenki…）、沙箱（aio_sandbox、e2b_sandbox、opensandbox）、browser_automation…
├── persistence/      # SQLAlchemy 引擎、models、Alembic migrations（agents/thread_meta/run/
│                     # scheduled_tasks/mcp_tasks/user/…）
├── guardrails/  authz/  tracing/  # 护栏、授权、可观测（Langfuse/monocle 工厂）
├── scheduler/        # 后台定时任务调度（scheduler.* 配置；多实例走 lease 恢复）
├── uploads/          # 上传文件管理（文档转换 markitdown）
├── integrations/     # 托管的一方集成安装器（如 Lark CLI skill pack）
├── workspace_changes/  # run 级改动文件汇总/差异
├── client.py         # 嵌入式 Python 客户端 DeerFlowClient
├── tui/              # 终端工作台（`deerflow` 控制台脚本入口，textual，可选 extra）
└── reflection/ utils/ constants.py logging_config.py trace_context.py …
```

要点：

- **`agents/middlewares/` 是横切关注点集中地**（不是旧书说的"11 层固定链"——现在文件数远超于此，实际装配顺序由 factory 与中间件各自声明决定，读代码时以 `agents/middlewares/AGENTS.md` 与 factory 为准）。
- **记忆（DeerMem）在 `agents/memory/`**，2.x 不依赖 embedding/向量库，由 LLM 抽取画像/facts 为 JSON 持久化并注入 prompt。
- **`runtime/` 是整个运行时的核心**：`make dev`、Docker dev、生产三种模式共用同一条执行路径——`RunManager` + `run_agent()` + `StreamBridge`，不存在"Gateway 一套、Docker 另一套"的双轨执行栈。
- harness 通过 `[project.scripts] deerflow = "deerflow.tui.cli:main"` 提供同名 CLI（子命令含 `extensions` 管理）；可选 extras：`postgres`、`redis`、`monocle`、`browser`、`memory-zh`、`tui` 等，让核心安装保持精简。

## 2.5　app 层：Gateway API、IM Channels 与后台服务

app 层（`backend/app/`，import 前缀 `app.*`）不可发布、只依赖 harness，负责把框架变成可服务的产品。

### Gateway（`app/gateway/`）——端口 8001

FastAPI 应用入口 `gateway/app.py`，路由按域拆到 `routers/`：

```text
app/gateway/routers/
├── models.py            # GET/POST /api/models        模型列表与配置
├── threads.py           # /api/threads/{thread_id}    Thread 数据清理等
├── thread_runs.py       # /api/threads/{id}/runs      Thread 级 Run 生命周期
├── runs.py              # /api/runs                   无状态 Run 执行
├── uploads.py           # /api/threads/{id}/uploads   文件上传
├── artifacts.py         # /api/threads/{id}/artifacts 产物服务
├── suggestions.py       # /api/threads/{id}/suggestions  后续建议
├── memory.py            # /api/memory                 全局记忆
├── mcp.py  mcp_tasks.py # /api/mcp、MCP 长任务          MCP server 管理
├── skills.py            # /api/skills                 Skill 注册/状态/上传安装
├── agents.py            # /api/agents                 自定义 Agent 配置
├── subagents.py  subagent_batches.py                  # 子代理目录/批处理
├── scheduled_tasks.py   # /api/scheduled-tasks        定时任务
├── channels.py  channel_connections.py                # IM 渠道管理
├── auth.py  features.py  feedback.py  browser.py
├── github_webhooks.py  integrations.py  console.py  input_polish.py
└── assistants_compat.py # LangGraph 兼容辅助路由
```

Gateway 目录下还有 auth 体系（`auth_middleware.py`、`csrf_middleware.py`、`internal_auth.py`、`langgraph_auth.py`、`authz.py`）与中间件（`trace_middleware.py`、`context_usage.py` 等），这些是**旧书完全没有**的新增面。

### 嵌入的 LangGraph 兼容运行时

旧书里是"独立的 LangGraph Server（端口 2024）"，现在**不存在独立进程**——agent 运行时直接嵌入 Gateway：

```jsonc
// backend/langgraph.json —— LangGraph Studio / 兼容层的图形声明
{
  "python_version": "3.12",
  "dependencies": ["."],
  "graphs": { "lead_agent": "deerflow.agents:make_lead_agent" },
  "auth": { "path": "./app/gateway/langgraph_auth.py:auth" },
  "http": { "app": "./app/gateway/langgraph_studio.py:langgraph_app" },
  "checkpointer": {
    "path": "./packages/harness/deerflow/runtime/checkpointer/async_provider.py:make_checkpointer"
  }
}
```

对外暴露为 `/api/langgraph/*` 的 LangGraph SDK 兼容面：nginx 把 `/api/langgraph/xxx` 重写为 `/api/xxx` 转发给 Gateway 原生路由（见 2.8）。想用 LangGraph Studio 调试也可以（可选），但正常使用**不需要** Studio——Gateway 自己实现了兼容层。

### IM Channels（`app/channels/`）

```
app/channels/
├── base.py            # ChannelBase 抽象
├── service.py         # start_channel_service：channel 生命周期总管
├── manager.py  run_policy.py  message_bus.py  store.py
├── feishu.py  slack.py  telegram.py  discord.py
├── dingtalk.py  wechat.py  wecom.py  github.py
├── buzz.py  buzz_nostr.py    # Buzz（Nostr 协议层）
└── feishu_run_policy.py  buzz_run_policy.py …
```

channel 的接入地址由环境变量给出（`DEER_FLOW_CHANNELS_LANGGRAPH_URL` / `DEER_FLOW_CHANNELS_GATEWAY_URL`），compose 里默认指向 `http://gateway:8001/api` 与 `http://gateway:8001`。

### 后台服务（backend/app/ 平级目录）

Gateway 之外 app 层还挂了三个"旁路服务"（均由 harness 里对应子系统支撑）：

- `app/scheduler/service.py` —— 定时任务调度器（`config.yaml → scheduler.enabled` 开关；默认单实例，`scheduler.multi_instance=true` 走 lease 恢复）；
- `app/mcp_tasks/service.py` —— MCP 长任务持久运行时（`McpTaskService`，lease 恢复、取消 fencing）；
- `app/subagent_batches/service.py` —— 子代理批处理。

它们共享同一条 run 生命周期，**不引入平行执行栈**——调度器只决定"何时跑"，实际执行仍走 Gateway 的 run 路径。

## 2.6　deerflow-extension-api：公共扩展契约

`backend/packages/extension-api/deerflow_extension_api/` 是一个**宿主无关**的纯契约包——第三方扩展 import 它来声明自己能贡献什么，而不 import 任何 `deerflow.*` 或 `app.*`（那是实现细节）：

```
deerflow_extension_api/
├── __init__.py  contracts.py   # 契约类型与版本
├── assembly.py                 # 扩展组装（贡献点聚合）
├── auth.py                     # 鉴权/主体解析（extension principal）
├── compaction.py               # 上下文压缩契约
├── placement.py                # 放置策略
├── provenance.py               # 溯源（provenance key 集合）
├── release.py                  # 版本/发布契约
├── runtime_bridge.py           # 运行时桥
└── state.py                    # 状态访问（ExtensionData.scope_id 等）
```

加载链路在 harness 的 `deerflow/extensions/`（loader/registry/placement/isolation/manager/CLI）。**第三方扩展的来源是 `config.yaml` 顶层的 `plugins:` 列表**——刻意由运维控制（该列表会导致 import 代码，所以不放进 API 可写的 `extensions_config.json`）。打包扩展可贡献五类东西：

1. **middleware**（进中间件链）
2. **task lifecycle**（任务生命周期钩子）
3. **system-model observers**
4. **Gateway services**
5. **FastAPI HTTP routers**

参考实现见 `examples/deerflow-extension-example/`（独立可发布包，五个贡献点齐全，含测试）。管理命令：

```bash
deerflow extensions install SOURCE=...   # 或根 Makefile 包装：make extension-install SOURCE=...
make extension-list / extension-enable NAME=… / extension-disable NAME=… / extension-remove NAME=…
```

每次变更都要求重启 Gateway；build hooks 与扩展代码以 Gateway 权限执行，所以只允许可信来源（扩展管理器事务、接受源形态、锁纪律见 `deerflow/extensions/AGENTS.md`）。安装的扩展由 `dependency-groups.extensions` 记账，落地快照在 `backend/extensions/sources/`。

## 2.7　frontend：Next.js 前端

```text
frontend/
├── src/
│   ├── app/           # Next.js App Router
│   ├── components/    # workspace/（聊天页）、landing/、ui/（Shadcn，自动生成勿改）、
│   │                  # ai-elements/（Vercel AI SDK 元素，自动生成）、docs/
│   ├── core/          # 业务核心：threads/ api/（LangGraph SDK 单例）agents/ subagents/
│   │                  # auth/ artifacts/ channels/ integrations/ i18n/(en-US,zh-CN)
│   │                  # skills/ memory/ mcp/ models/ settings/ suggestions/ tasks/
│   │                  # todos/ tools/ voice-input/ input-polish/ workspace-changes/ …
│   ├── hooks/  lib/  content/  styles/  typings/
├── tests/             # tests/unit/（Rstest）+ tests/e2e/（Playwright）
├── package.json  pnpm-lock.yaml  pnpm-workspace.yaml
├── next.config.js  playwright.config.ts  rstest.config.ts
└── Makefile
```

技术栈（来自 frontend/AGENTS.md）：**Next.js 16、React 19、TypeScript 5.8、Tailwind CSS 4、pnpm 10.26.2**（Node 22+）。核心依赖：`@langchain/langgraph-sdk`（^1.5.3，agent 编排与流式）、`@langchain/core`、`@tanstack/react-query`（服务端状态）；UI 组件从 Shadcn/MagicUI/React Bits/Vercel AI SDK 注册表生成。

主要路由：`/`（landing）、`/workspace/chats/[thread_id]`（认证聊天）、`/workspace/agents/*`（自定义 Agent）、`/showcase/[thread_id]`（免登录只读演示）、`/artifacts/view`（独立产物渲染窗）、`/blog`、`(auth)/{login,setup,auth/callback}`、`/[lang]/docs`、`/api/*` 路由处理器。

命令速记：`pnpm dev`（默认 Webpack；`DEER_FLOW_DEV_BUNDLER=turbo` 可选）、`pnpm check` = ESLint + `tsc --noEmit`（提交前必跑）、`pnpm test` = Rstest 单测、`pnpm test:e2e` = Playwright、`pnpm build/start`。单测按 `tests/unit/` 镜像 `src/` 布局；`*.dom.test.*` 走 happy-dom，其余走 node（快约 3 倍）。

与后端连通的 env：`NEXT_PUBLIC_BACKEND_BASE_URL`、`NEXT_PUBLIC_LANGGRAPH_BASE_URL`——标准 `make dev`/Docker 流程**留空即可**，由 nginx 的 `/api/langgraph/*` 前缀接驳 Gateway。

## 2.8　运行时拓扑：服务、端口与 Nginx 路由

一个 `make dev` / Docker 栈跑四个协作服务（外加 compose 里的 redis）：

| 服务 | 端口 | 角色 |
|---|---|---|
| **Nginx** | `2026` | 统一反向代理入口——浏览器只开这个 |
| **Gateway API** | `8001` | FastAPI REST + 内嵌 LangGraph 兼容 agent 运行时 |
| **Frontend** | `3000` | Next.js Web 界面 |
| **Provisioner** | `8002` | 可选——仅 sandbox 配 provisioner/K8s 模式时启动 |

Nginx 路由（`docker/nginx/nginx.conf`，本地版见 `nginx.local.conf` / `nginx.docker.conf`）：

```text
浏览器 ──► nginx :2026
            ├── /api/langgraph/*  ── rewrite ^/api/langgraph/(.*) /api/$1 break ──► Gateway :8001
            │      （LangGraph SDK 兼容面 → Gateway 原生 /api/* 路由）
            ├── /api/*            ────────────────────────────────────────────────► Gateway :8001
            │      （/api/models、/api/memory、/api/mcp、/api/skills、/api/agents、
            │        /api/threads…、/docs、/redoc、/openapi.json、/health）
            ├── /api/sandboxes    ────────────────────────────────────────────────► Provisioner :8002
            └── /*                 ────────────────────────────────────────────────► Frontend :3000
```

代理层压缩 HTML 与文本资产，**刻意不压缩** SSE、字体、图片、音视频；SSE 流式需要 location 级的 proxy 设置（buffering 关闭）。

安全默认值：两个 compose 文件都用 `"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"` 发布入口——**默认只绑回环**；裸 `${PORT}:2026` 会绑 0.0.0.0，不符合文档部署模型。根 `PORT` 只控制 Docker 入口；本地编排固定 Next.js 3000。Gateway 在容器内绑 `0.0.0.0:8001` 是有意的（容器内部面，不发布）。`backend/tests/test_compose_default_bind_host.py` 对两个 compose 的每个服务锁死该约定。

启动模式矩阵（根 Makefile / scripts）：

| | Local Foreground | Local Daemon | Docker Dev | Docker Prod |
|---|---|---|---|---|
| **Dev** | `make dev`（`./scripts/serve.sh --dev`） | `make dev-daemon` | `make docker-start`（`./scripts/docker.sh start`） | — |
| **Prod** | `make start`（`--prod`） | `make start-daemon` | — | `make up`（`./scripts/deploy.sh`） |

首次使用顺序：`make config`（生成两个 gitignored 配置文件）→ `make install` → `make dev`。`make dev` **不会**代生成配置，缺 `config.yaml` 服务拒绝启动。

docker/ 目录：`docker-compose.yaml`（生产）、`docker-compose-dev.yaml`、`docker-compose.cli-auth.yaml`、`docker-compose.dood.yaml`、`docker-compose.openviking.yaml`、`nginx/`、`provisioner/`（K8s 沙箱 provisioner 镜像）、`dev-entrypoint.sh`、`lark-cli-broker/`、`lark-cli-init/`。生产 compose 里 `config.yaml` 只读挂载（`:ro`），而 `extensions_config.json` 必须读写挂载——Gateway 运行时会写它（PUT/PATCH `/api/mcp/config`、MCP 开关、skill 更新）；Helm 则先把 ConfigMap 种子拷进可写的 home-volume。

## 2.9　配置体系

**配置文件在仓库根，全部 gitignored**，运行时可通过 Gateway API 编辑：

| 文件 | 内容 | 来源模板 |
|---|---|---|
| `config.yaml` | 应用主配置（约 2836 行的 example 模板，`config_version: 39`） | `config.example.yaml` |
| `extensions_config.json` | MCP servers + skills 启用状态 + MCP 拦截器 | `extensions_config.example.json` |

```yaml
# config.example.yaml（节选顶层块；当前 schema 版本 config_version: 39）
log_level: info
max_recursion_limit: 1000
models:            # 模型定义列表（use 指向 "package.module:class"）
tools:             # 工具开关与分组（tool_groups、tool_search、tool_output…）
sandbox:           # 沙箱模式（local/provisioner…）与挂载
skills:            # skills 路径、扫描、校验（skill_scan 另有块）
memory:            # DeerMem 记忆系统
summarization:     # 上下文摘要策略
uploads:  title:  suggestions:  input_polish:
loop_detection:  read_before_write:  safety_finish_reason:
token_usage:  token_budget:  verification:
subagent_runtime:  subagent_batches:  agents_api:
database:  run_events:  agent_storage:  run_ownership:  scheduler:  mcp_tasks:
authorization:     # 授权规则（含 plugins 之外的鉴权配置）
```

顶层块多达四十余个，每个块在 harness `deerflow/config/` 里有一个 `xxx_config.py` 一一对应（`sandbox_config.py`、`model_config.py`、`scheduler_config.py`……）。schema 变更时 `config_version` 递增，用 `make config-upgrade` 把新字段合并进本地 `config.yaml`（`scripts/config-upgrade.sh`）。

取值规则：所有字段值都支持环境变量引用（`api_key: $OPENAI_API_KEY`）；路径解析支持 `DEER_FLOW_PROJECT_ROOT`（默认仓库根）、`DEER_FLOW_CONFIG_PATH`（指向特定配置文件）、`DEER_FLOW_HOME`（运行时数据目录，默认 `<root>/.deer-flow`）。

`extensions_config.json` 的结构（模板即真源）：

```jsonc
// extensions_config.example.json
{
  "middlewares": [],               // Python 扩展中间件
  "mcpInterceptors": ["my_package.mcp.auth:build_auth_interceptor"],
  "mcpServers": {                  // 每台 MCP：enabled/type(stdio|http)/command/args/env/
      "github": { "enabled": false, "type": "stdio", "command": "npx", /* … */ },
      "postgres": { /* …含 routing:{mode,priority,keywords} 工具路由权重… */ }
  },
  "skills": {}                     // skills 启用状态（Gateway 运行时写入）
}
```

注意一个安全细节：`plugins:`（会 import 代码的 Python 扩展）放在 `config.yaml`，**刻意不进** API 可写的 `extensions_config.json`；后者只装"声明式"内容（MCP 定义、skills 开关）。写 `extensions_config.json` 时有进程内锁 + sidecar 文件锁双层保护，且对 Docker 挂载点做了 `EBUSY` 回退（`atomic_write_extensions_config`），相关行为有专门测试钉住。

## 2.10　skills 与 contracts

### skills 目录

```
skills/
├── public/      # 入库的公共 skills（当前 25 个，随仓库发版）
└── custom/      # 用户私有 skills（.gitignore 排除，不提交）
```

`public/` 现有（节选）：`bootstrap`（引导）、`deep-research`、`data-analysis`、`ppt-generation`、`chart-visualization`、`image-generation`、`video-generation`、`music-generation`、`web-design-guidelines`、`frontend-design`、`consulting-analysis`、`academic-paper-review`、`systematic-literature-review`、`newsletter-generation`、`podcast-generation`、`claude-to-deerflow`、`github-deep-research`、`surprise-me`、`find-skills`、**`skill-creator`**（写 skill 的 skill）、**`skill-reviewer`**（内置只读质量评审器）等。仓库根 `skills-lock.json` 记录从 GitHub 引入的外部 skill 及其 SHA-256：

```jsonc
// skills-lock.json
{ "version": 1, "skills": {
    "wechat-article-extractor": {
      "source": "freestylefly/wechat-article-extractor-skill",
      "sourceType": "github", "skillPath": "SKILL.md",
      "computedHash": "ba5583…d74c"
    } } }
```

解析、加载、校验在 harness `deerflow/skills/`（loader/parser/manager/validation/security_scanner）；公共 skill 变更走 CI 质量评审（`scripts/review_changed_public_skills.py` + `.github/skill-review-waivers.v1.json`，详见 contracts）。托管集成 skill 包全局放在 `.deer-flow/integrations/skills/{provider}/`，集成凭据与启用状态仍按用户隔离。**Skill 与 Tool 的区别不变**：Skill 是复合能力（SKILL.md 的 prompt + 若干 tool 的编排），Tool 是原子操作；新增自定义能力优先在 `skills/custom/` 造 skill，不要改核心代码。

### contracts 目录（跨语言/跨组件契约）

```
contracts/
├── subagent_status_contract.json      # 子代理状态枚举（completed/failed/cancelled/
│                                      # timed_out/polling_timed_out + v2 stop_reason）
├── slash_skill_contract.json
├── run_event_stream_contract.json     # SSE 事件流契约
└── skill_review/                      # skill 评审 JSON Schema（v1）
    ├── package_snapshot.v1.schema.json
    ├── review_facts.v1.schema.json
    ├── review_report.v1.schema.json
    └── waiver_manifest.v1.schema.json
```

它们不是文档摆设——前后端、工具链与 CI 都按这些 JSON 校验数据。例：

```jsonc
// contracts/subagent_status_contract.json
{
  "version": 2,
  "valid_status_values": ["completed", "failed", "cancelled", "timed_out", "polling_timed_out"],
  "valid_stop_reason_values": ["token_capped", "turn_capped", "loop_capped"]
}
```

## 2.11　scripts、测试与质量门禁

根 `scripts/`（每支被根 Makefile 调用的编排脚本）：

```text
serve.sh        # 本地前台/守护进程启停（dev/prod）
docker.sh       # Docker 开发环境启停（docker-start/stop/logs）
deploy.sh       # 生产 Docker 栈（make up/down）
nginx.sh        # 本地 nginx 启停
setup_wizard.py / configure.py / doctor.py   # 安装向导 / 配置生成 / 环境体检
support_bundle.py      # 脱敏故障包 + AI issue 草稿
check.py / check.sh    # 依赖体检（Node/pnpm/uv/nginx…）
verify_versions.sh / bump_version.sh          # 版本 lockstep（见 2.1）
config-upgrade.sh      # config.yaml 增量升级
review_changed_public_skills.py               # public skill 变更评审
detect_blocking_io_static.py / detect_thread_boundaries.py   # 执行边界体检
pnpm.py                # host 侧统一 pnpm runner（corepack 回退）
```

测试分层（backend 三档）：

```bash
cd backend && make test               # 默认离线套件（pytest，排除 live/blocking-io）
cd backend && make test-blocking-io   # 严格阻塞 I/O 套件（Blockbuster 门禁）
cd backend && make test-live          # 显式 live 集成测试（真 API，需凭据）
cd backend && make lint && make format  # ruff lint / ruff format（CI 强制 format --check）
```

代码风格：Python 3.12+、双引号、ruff、行宽 240；前端 prettier + eslint。仓库级测试也在 `tests/skills/`（public skill 测试）。另外注意 `backend/scripts/benchmark/` 有可复现基准（deermem_eviction、concurrency），要求外部数据集按不可变 revision + SHA-256 钉住。

## 2.12　技术栈一览

| 层 | 选型 |
|---|---|
| 编排引擎 | **LangGraph**（`langgraph>=1.2.9,<1.3`，2.x 硬上限；checkpointer SQLite/Postgres，Alembic 迁移） |
| 模型抽象 | LangChain：`langchain>=1.3` + anthropic/openai/deepseek/google-genai/openviking 供应商；`deerflow/models/` 提供 thinking/vision 支持与 `patched_*` 修补（deepseek/minimax/mimo/stepfun/openai…）与专用 provider（vllm/mindie/openai_codex/claude） |
| API 层 | FastAPI + uvicorn + sse-starlette（SSE 流式）、LangGraph SDK 兼容面、内嵌运行时（RunManager/StreamBridge） |
| Agent 运行时 | 主 Agent `make_lead_agent` + 40+ 中间件链 + 子代理（general-purpose/bash）+ MCP 客户端 + sandbox |
| 沙箱 | `agent-sandbox>=0.0.30` 抽象；local 实现；AIO/E2B/OpenSandbox 远程；K8s provisioner 可选 |
| 搜索/社区 | Tavily、DDG（ddgs）、Brave、SearXNG、Serper、Exa、Firecrawl、Jina、InfoQuest、Tenki… |
| 持久化 | SQLAlchemy 2（asyncio）+ aiosqlite/Postgres + Alembic；LangGraph checkpointer/Store 共用同一 DB |
| 可观测 | Langfuse 工厂 + monocle（可选 tracing extra） |
| IM | Feishu(Lark oapi)、Slack、Telegram、Discord、DingTalk、WeChat、WeCom、GitHub、Buzz(Nostr) |
| 前端 | Next.js 16 / React 19 / TypeScript 5.8 / Tailwind 4 / pnpm 10.26.2；LangGraph JS SDK + TanStack Query；Rstest + Playwright |
| 部署 | Docker compose（本地/dev/prod）、Helm Chart（`deploy/helm/deer-flow`）、nginx 统一入口 |
| Python 工具链 | uv（锁文件 + `uv run --no-sync`）、ruff、pytest、pre-commit、blockbuster（阻塞 I/O 门禁） |

## 2.13　旧书目录对照（backend/src → 新布局）

旧书读者请把下列映射记在脑子里——旧路径在 2.x **全部作废**：

| 旧书里的路径/概念 | 现在的位置 |
|---|---|
| `backend/src/agents/` | `backend/packages/harness/deerflow/agents/` |
| `backend/src/gateway/` | `backend/app/gateway/`（import 前缀从 `src.gateway` 变 `app.gateway`） |
| `backend/src/channels/` | `backend/app/channels/` |
| `backend/src/community/`、`sandbox/`、`mcp/`、`skills/`、`models/`、`subagents/`、`tools/`、`config/`、`client.py` | `backend/packages/harness/deerflow/` 同名子目录（`deerflow.*` import） |
| 独立的 **LangGraph Server（端口 2024）** | 取消——运行时嵌入 Gateway（8001），nginx 暴露 `/api/langgraph/*` 兼容面 |
| `backend/src/reflection/`（旧"反思机制"） | `deerflow/reflection/`：动态模块加载（resolve_variable/resolve_class），非"自我反思" |
| `skill.yaml` | 已废弃——skill 元数据在 `SKILL.md` frontmatter |
| 单一 `backend/pyproject.toml` 大包 | 三包：`deer-flow`(app) + `deerflow-harness` + `deerflow-extension-api`，边界由 `test_harness_boundary.py` 钉死 |
| `.env` 承载全部密钥 | `.env` + 配置里 `$ENV_VAR` 引用（模板 `config.example.yaml` / `.env.example`） |

## 小结

- **仓库分层**：根 `AGENTS.md` 管地图与公约；backend 三包（app 壳 / harness 框架 / extension-api 契约），**依赖只许 app → harness，由 `test_harness_boundary.py` 强制**——这是理解全书的钥匙。
- **一个运行时**：`make dev`、Docker、生产共用 Gateway 内嵌的 `RunManager + run_agent() + StreamBridge`；对外只开 nginx 一个口（2026），`/api/langgraph/*` 是 LangGraph SDK 兼容面。
- **配置在根**：`config.yaml`（主配置，`config_version: 39`）+ `extensions_config.json`（MCP/skills，运行时 API 可写）；版本号 lockstep 2.1.0 由 `verify_versions.sh` 把关。
- **扩展走契约**：第三方用 `deerflow_extension_api.*` 声明贡献（middleware/任务生命周期/observers/Gateway 服务/HTTP 路由），来源在 `config.yaml plugins:`，装完重启 Gateway。

**下一步**：进入 backend 的骨架——阅读第 3 章，看 Gateway 如何把 HTTP/IM 请求送进 harness 运行时，以及 LangGraph 兼容层如何落地。
