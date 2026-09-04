# 第 3 章　快速上手：从零跑起一个 DeerFlow

> 基于 DeerFlow 最新源码（本仓库 commit `2672e209`，2026-09）编写。本仓库快照版本为 **2.1.0**（`backend/pyproject.toml` 与 `frontend/package.json` 同步锁定），MIT License。

> **旧版说明**：本章融合两本旧书对应内容（hawkli 版第 1 章 §1.6「环境准备」与附录 B「贡献指南」、coolclaws 版第 3 章「快速上手」），但一切以重构后的**最新仓库**为准校准——旧书基于数月前的 DeerFlow（`backend/src/` 目录、独立 LangGraph Server 进程、`mcp_config.json`、skill.yaml 等），其中大量路径、命令与配置项在 2.x 已作废。凡冲突处，以本章为准。

DeerFlow 2.0 于 2026 年 2 月发布后登上 GitHub Trending，是一次**从零重写**——与 v1 零代码共用（v1 仍在 `1.x` 分支维护）。本章带你完成「克隆 → 配置 → 启动 → 验证」全流程，并给出最常用的配置项速查。全程约 10～15 分钟即可跑起第一个对话。

## 3.1　启动路径与服务拓扑总览

DeerFlow 有四种启动方式，端口与入口完全统一：

| 场景 | 命令 | 说明 |
|------|------|------|
| 本地开发（改代码） | `make dev` | 本机跑 Gateway + Frontend + Nginx 三个进程，热重载 |
| 本地开发（后台） | `make dev-daemon` | 同上，后台运行，`make stop` 停止 |
| Docker 开发（体验推荐） | `make docker-start` | `docker/docker-compose-dev.yaml`，源码挂载 + 热重载 |
| Docker 生产 | `make up` | 构建镜像启动，`make down` 停止，适合长期服务 |

一个 `make dev` / Docker 栈实际跑四个协作服务（根 AGENTS.md 的 service topology）：

| 服务 | 端口 | 角色 |
|------|------|------|
| **Nginx** | `2026` | 统一反向代理入口——浏览器只开这一个 |
| **Gateway API** | `8001` | FastAPI REST API + **内嵌** LangGraph 兼容 agent runtime |
| **Frontend** | `3000` | Next.js Web 界面 |
| **Provisioner** | `8002` | 可选——仅当沙箱配置为 provisioner/K8s 模式时启动 |

> **与旧版最大的架构差异**：v2 不再有独立的 LangGraph Server 进程，agent runtime 直接内嵌在 Gateway 里（`RunManager` + `run_agent()` + `StreamBridge`）。`make dev` 本地模式只有 Gateway、Frontend、Nginx 三个进程；Provisioner 只在 Docker + provisioner 沙箱模式下作为容器出现。

Nginx 的路由规则（理解它，排错就成功一半）：

```
/               → Frontend (3000)   Web UI
/api/langgraph/* → Gateway 内嵌 runtime（重写为 /api/*）
/api/*          → Gateway REST API (8001)
```

## 3.2　前置依赖

### 3.2.1　依赖清单

本地开发模式（`make dev`）需要以下工具，`make check` 会逐项验证：

| 工具 | 版本要求 | 用途 | 安装提示 |
|------|---------|------|---------|
| Node.js | 22+ | 前端构建 | https://nodejs.org/ 或 nvm |
| pnpm | 跟随 `frontend/package.json` 的 `packageManager` 锁定 | 前端依赖 | 直接装 pnpm，或由 Corepack 提供（见下） |
| uv | 最新 | Python 依赖/运行 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| nginx | 1.x 即可 | 本地反向代理 | macOS：`brew install nginx`；Ubuntu：`sudo apt install nginx` |
| Python | 3.12+（由 uv 管理） | 后端 | 无需手动装，`uv sync` 会按 `pyproject.toml` 解析 |
| Docker + Compose | Compose **v2.24+** | 仅容器沙箱 / Docker 模式需要 | `docker compose version` 自查 |

要点：

- **pnpm 不强制全局安装**。`make check` / `make install` / `make dev` 优先使用直接的 `pnpm` 可执行文件，找不到时自动回退到 `corepack pnpm`（从 `frontend/` 运行，遵循 `packageManager` 锁定的版本）。因此只装 Node 22+ 即可被 check 接受。
- **Docker 不是 `make dev` 的前置条件**——本地模式不碰 Docker。只有你要用「Docker 一键启动」或「Docker 容器沙箱」时才需要 Docker + Compose v2.24+（`docker-compose-dev.yaml` 用到了较新的可选 `env_file` 长语法，老版本 Compose 解析失败）。
- `make check` 的输出会提示你后续命令：通过后按顺序执行 `make config`/`make setup` → `make install` → `make doctor` → `make dev`。

### 3.2.2　系统建议配置

| 部署目标 | 起步 | 推荐 | 备注 |
|---------|------|------|------|
| 本地 `make dev` | 4 vCPU / 8 GB / 20 GB SSD | 8 vCPU / 16 GB | 一个开发者 + 少量 hosted API 会话够用；**2 vCPU / 4 GB 通常不够** |
| Docker 开发 | 4 vCPU / 8 GB / 25 GB SSD | 8 vCPU / 16 GB | 镜像构建、bind mount、沙箱容器更吃资源 |
| 长期服务 `make up` | 8 vCPU / 16 GB / 40 GB SSD | 16 vCPU / 32 GB | 多用户、多 agent 并发、报告生成 |

- 以上只含 DeerFlow 自身；若还要本地跑 LLM，另行估算。
- 持久化服务器推荐 **Linux + Docker**；macOS / Windows 适合当开发评估环境。
- Windows 用户跑本地流程必须用 **Git Bash**（脚本依赖 Git for Windows 的 `cygpath` 等工具；原生 cmd/PowerShell 与 WSL 都不保证支持）。
- Linux 上 Docker 报 `permission denied … /var/run/docker.sock` 时，把自己加入 `docker` 组并重新登录（详见 CONTRIBUTING.md）。

## 3.3　克隆与目录

```bash
git clone https://github.com/bytedance/deer-flow.git
cd deer-flow
```

2.0 的仓库布局与 v1（`backend/src/`）完全不同，新结构（根 AGENTS.md repository map）：

```
deer-flow/
├── Makefile                        # 根编排：dev/start/stop、docker、setup、check、doctor
├── config.example.yaml             # 模板 → 复制为 config.yaml（gitignored）
├── extensions_config.example.json  # 模板 → 复制为 extensions_config.json（gitignored）
├── .env.example                    # 模板 → 复制为 .env（密钥，gitignored）
├── backend/                        # Python 后端
│   ├── packages/harness/           # deerflow-harness 框架包（import: deerflow.*）
│   │   └── deerflow/               #   agents/ sandbox/ subagents/ tools/ models/ mcp/
│   │                               #   skills/ community/ config/ extensions/ …
│   ├── packages/extension-api/     # 公开扩展契约（import: deerflow_extension_api.*）
│   ├── app/                        # FastAPI Gateway + IM 渠道（import: app.*）
│   └── Makefile                    # 后端单独命令（dev/gateway/test/lint/migrate-rev）
├── frontend/                       # Next.js 前端（pnpm）
├── docker/                         # docker-compose 文件、nginx 配置、provisioner
├── skills/
│   ├── public/                     # 内置技能（入库）
│   └── custom/                     # 自定义技能（gitignored）
├── scripts/                        # 根编排脚本（serve/docker/deploy/check/configure/doctor/setup_wizard）
├── docs/                           # 跨模块文档与设计笔记
└── tests/                          # 根级测试
```

> **路径校准（旧书读者必看）**：v1 / 早期 2.0 资料里的 `src.models.*`、`src.community.*`、`src.sandbox.*` 类路径，现已全部带 `deerflow.` 前缀（如 `deerflow.community.ddg_search.tools:web_search_tool`）；`mcp_config.json` 已被 `extensions_config.json` 取代（加载器按 `extensions_config.json` → 遗留 `mcp_config.json` 的顺序查找）；后端包根从 `backend/src` 移到 `backend/packages/harness/deerflow`；`config.yaml` 顶层也不再需要 `middlewares:` 之类的旧结构。

## 3.4　配置文件三步走

DeerFlow 的本地配置是**仓库根目录的三份 gitignored 文件** + 可选的 `frontend/.env`：

| 文件 | 来源模板 | 内容 |
|------|---------|------|
| `config.yaml` | `config.example.yaml` | 主配置：模型、工具、沙箱、记忆、数据库、鉴权… |
| `extensions_config.json` | `extensions_config.example.json` | MCP 服务器 + 技能（运行时可由 Gateway API 改写） |
| `.env` | `.env.example` | API 密钥与部署级环境变量（启动时被 `serve.sh` source） |
| `frontend/.env` | `frontend/.env.example` | 前端直连后端时的覆盖项（**默认注释掉即可，走 nginx**） |

### 3.4.1　方式一：交互向导 `make setup`（推荐）

```bash
make setup
```

向导交互完成三件事（约 2 分钟）：

1. 选择 LLM 提供方并填 key；
2. 可选配置 Web 搜索提供方（也可先跳过）；
3. 选择执行/安全偏好（沙箱模式、bash 访问、文件写工具）。

它生成一份**最小可用 `config.yaml`**，把密钥写入 `.env`，并从模板生成 `frontend/.env`。之后随时可跑：

```bash
make doctor    # 校验依赖、配置、模型提供方连通性，给出修复提示
```

### 3.4.2　方式二：手动拷贝全量模板 `make config`

想直接编辑完整模板就执行：

```bash
make config
```

`scripts/configure.py` 只做三件事（任一配置文件已存在则**中止**，防止覆盖）：

1. `config.example.yaml` → `config.yaml`
2. `.env.example` → `.env`
3. `frontend/.env.example` → `frontend/.env`

> 注意 `make config` **不会**生成 `extensions_config.json`——该文件缺失时以空默认加载，不影响启动；需要 MCP 服务器时再手动 `cp extensions_config.example.json extensions_config.json`。

### 3.4.3　`config.yaml` 顶层结构

全量模板约 2800 行、绝大多数为注释示例。顶层键一览（行号对应本仓库 `config.example.yaml`，`config_version: 39`）：

```
config_version: 39      # 升级检测；make config-upgrade 合并新字段
log_level: info         # debug/info/warning/error
token_usage:            # 用量统计（enabled: true）
token_budget:           # 单次 run 硬预算（默认关闭）
max_recursion_limit: 1000
models:                 # ★ LLM 模型列表，第一个为默认模型（模板里全是注释示例，需自行启用）
tool_groups:            # web / file:read / file:write / bash / browser / knowledge
tools:                  # ★ 工具开关与 provider 选择
tool_search:            # 延迟加载工具
uploads:                # 文件上传（转换、大小限制）
sandbox:                # ★ 沙箱模式（见 3.7）
subagent_runtime:       # 子智能体上限（如 max_total_per_run）
skills:                 # skills 路径、容器挂载点、延迟发现
summarization:          # 上下文自动摘要（默认开启）
memory:                 # 长期记忆（默认开启）
database:               # sqlite（默认）/ postgres；共享给 checkpointer/Store/应用数据
run_events:             # run 事件存储：memory（默认）/ db / jsonl
scheduler:              # 定时任务（默认关闭）
authorization:          # 登录鉴权（enabled: false 默认免登录）
plugins:                # Python 扩展包（仅可信来源，操作者手动管理）
```

所有字段值都支持环境变量引用：`api_key: $OPENAI_API_KEY`。运行时可调的环境变量：

| 变量 | 作用 |
|------|------|
| `DEER_FLOW_PROJECT_ROOT` | 显式指定项目根（配置文件默认在根目录） |
| `DEER_FLOW_CONFIG_PATH` | 指向某个具体配置文件 |
| `DEER_FLOW_HOME` | 运行时状态目录（默认 `.deer-flow`；本地 `make dev` 固定为 `backend/.deer-flow`） |
| `DEER_FLOW_SKILLS_PATH` | 技能目录（默认项目根 `skills/`） |

### 3.4.4　`extensions_config.json`——MCP 与技能

这是 v2 的「扩展配置」，管理三类东西：

- `mcpServers`：MCP 服务器（stdio 如 GitHub/Postgres，或 http 如 OpenViking），每个带 `enabled` 开关、`env` 环境变量、会话/工具超时、`tool_name_prefix`、路由偏好等；
- `skills`：技能级覆盖；
- `middlewares` / `mcpInterceptors`：扩展注入点（示例文件里是占位，生产一般留空）。

```bash
cp extensions_config.example.json extensions_config.json
```

几个要点：

- 该文件**运行时会被 Gateway 改写**（`PUT`/`PATCH /api/mcp/config`、MCP 启用开关、技能更新都会写回），所以生产 compose 对它是**可写挂载**，而 `config.yaml` 是只读挂载；
- 旧的 `mcp_config.json` 已被取代（加载器按 `extensions_config.json` → 遗留 `mcp_config.json` 的顺序查找）；
- 需要 `npx` 启动的 stdio MCP（如 GitHub server）要求 **Gateway 进程**环境里有 Node/npx——Docker 模式下请确认镜像内已具备，而不是只在宿主机装了。

### 3.4.5　API 密钥：`.env`

密钥推荐放仓库根的 `.env`（`make dev` 时 `serve.sh` 会把它 source 进环境；`.env` 已在 .gitignore）。`make setup` 向导会自动写入；手动编辑则参考 `.env.example`——其中搜索类密钥（`TAVILY_API_KEY`、`JINA_API_KEY`、`SERPER_API_KEY`、`INFOQUEST_API_KEY` 等）默认就露在外面，各家模型密钥（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`VOLCENGINE_API_KEY`、`GEMINI_API_KEY`…）放在 Optional 注释里，用到哪个就打开哪行：

```bash
OPENAI_API_KEY=sk-…
DEEPSEEK_API_KEY=…
TAVILY_API_KEY=tvly-…       # 可选：第三方搜索
JINA_API_KEY=…              # 可选：提升 web_fetch 抓取限额
INFOQUEST_API_KEY=…         # 可选：BytePlus 搜索
```

`config.yaml` 里不要写明文，一律 `api_key: $VAR_NAME` 引用。

## 3.5　配置第一个模型

模型在 `config.yaml` 的 `models:` 列表里声明，**第一个条目就是默认模型**。注意模板里所有模型示例都是注释状态，`make config` 之后至少要启用/添加一个条目才能聊天。字段语义（摘录自模板注释）：

- `use`：类路径 `包.模块:类`，例如 OpenAI 系用 `langchain_openai:ChatOpenAI`，Anthropic 用 `langchain_anthropic:ChatAnthropic`，豆包/DeepSeek/Kimi 用 `deerflow.models.patched_deepseek:PatchedChatDeepSeek`（保留多轮 thinking 字段）；
- `api_key: $XXX`（环境变量）或 `gemini_api_key: $XXX`（Gemini 原生 SDK 用单独字段）；
- `api_base` / `base_url`：自建或兼容网关地址（如 `https://openrouter.ai/api/v1` 配 `langchain_openai:ChatOpenAI`）；
- `max_tokens`：**单次调用输出上限**（传给 provider）；
- `context_window`：模型总上下文容量，驱动 UI 的「已用上下文 %」与 fraction 型摘要触发——第三方兼容模型没有内置档案，不设则百分比不显示；
- `supports_vision` / `supports_thinking`：声明能力，决定 `view_image` 工具与 thinking 开关是否生效；
- `when_thinking_enabled` / `when_thinking_disabled`：开/关 thinking 时附加给 provider 的参数（Claude 必须带 `thinking.budget_tokens`，且须小于 `max_tokens`）。

Anthropic Claude 最小示例（取自模板）：

```yaml
models:
  - name: claude-sonnet-4
    display_name: Claude Sonnet 4
    use: langchain_anthropic:ChatAnthropic
    model: claude-sonnet-4-20250514
    api_key: $ANTHROPIC_API_KEY
    default_request_timeout: 600.0
    max_retries: 2
    max_tokens: 16000
    context_window: 200000
    supports_vision: true
    supports_thinking: true
    when_thinking_enabled:
      thinking:
        type: enabled
        budget_tokens: 4096
    when_thinking_disabled:
      thinking:
        type: disabled
```

OpenAI 兼容网关（OpenRouter 等；`use_responses_api: true` 可切 `/v1/responses`）：

```yaml
models:
  - name: openrouter-gemini-2.5-flash
    display_name: Gemini 2.5 Flash (OpenRouter)
    use: langchain_openai:ChatOpenAI
    model: google/gemini-2.5-flash-preview
    api_key: $OPENROUTER_API_KEY
    base_url: https://openrouter.ai/api/v1
```

**只配一个模型就能聊天**，但推荐至少 2～3 个（默认 + 便宜的备用），便于出问题时切换。

## 3.6　默认启用的工具与搜索

`config.example.yaml` 的 `tools:` 段**默认启用**了以下条目（开箱即用，大部分无需 key）：

| 工具 | 实现 | 需要 key? |
|------|------|----------|
| `web_search` | DuckDuckGo（DDG） | 否 |
| `web_fetch` | Jina AI Reader | 否（可配 `JINA_API_KEY` 提额） |
| `image_search` | DuckDuckGo 图片 | 否 |
| `ls` / `read_file` / `glob` / `grep` | 沙箱文件读取 | 否 |
| `write_file` / `str_replace` | 沙箱文件写入 | 否 |
| `bash` | 沙箱 shell | 仅当容器沙箱或 `allow_host_bash: true` 时激活 |

> **与旧书的差异**：早期版本默认用 Tavily 搜索（要 key）；现在默认是 **DDG，零配置**。Tavily / Serper / SearXNG / Brave / InfoQuest / 腾讯 WSA 等都作为同名的备选 provider 以注释形式给出——换用时要**注释掉默认条目再取消注释目标条目**（模板对同名工具的要求是「每次只启用一个条目」；`web_fetch` 同理）。

常见切换示例（把默认 DDG 那几行注释掉，启用下面这段并在 `.env` 配 key）：

```yaml
  - name: web_search
    group: web
    use: deerflow.community.tavily.tools:web_search_tool
    max_results: 5
```

## 3.7　沙箱模式：三种选择

沙箱决定 Agent 执行 bash、读写文件的隔离边界，是 v2 最核心的基础设施。`config.example.yaml` 默认：

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false   # ★ 宿主 bash 默认关闭！
```

| 模式 | 配置 | 说明 |
|------|------|------|
| Local（默认） | `deerflow.sandbox.local:LocalSandboxProvider` | 直接在 Gateway 进程所在机器执行。**不是安全隔离边界**：宿主 shell 默认禁用，需单用户可信环境显式开 `allow_host_bash: true` |
| Docker / AIO（推荐） | `deerflow.community.aio_sandbox:AioSandboxProvider` | 每个会话一个隔离容器（Docker / macOS Apple Container），镜像默认 `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest`，注释建议显式 pin 如 `1.11.0`（multi-arch）；`replicas` 默认 3、`port` 默认 8080、容器名前缀 `deer-flow-sandbox` |
| Provisioner / K8s（生产） | 同 AIO provider + `provisioner_url: http://provisioner:8002` | 通过 provisioner 服务在 K8s 里按会话起 Pod，多用户高并发首选 |

快速选择：**开发调试用 Local（不开宿主 bash），常规体验用 Docker/AIO，多用户生产用 Provisioner**。

首次使用容器沙箱前拉镜像（本地模式可跳过）：

```bash
make setup-sandbox        # 本地模式预拉 AIO 沙箱镜像
# Docker 部署则用 make docker-init，同样只拉镜像不启动服务
```

> 路径校准：旧资料里的 `src.sandbox.local:LocalSandboxProvider`、`src.community.aio_sandbox:AioSandboxProvider` 现在分别是 `deerflow.sandbox.local:…` 与 `deerflow.community.aio_sandbox:…`，照抄旧路径会启动失败。

## 3.8　启动方式一：本地开发 `make dev`

```bash
make check          # 1. 校验 Node 22+ / pnpm / uv / nginx
make install        # 2. 安装依赖（backend uv sync --locked + frontend pnpm install + pre-commit）
make setup-sandbox  # 3. （可选）若用容器沙箱则预拉镜像
make dev            # 4. 启动
```

`make dev`（即 `scripts/serve.sh --dev`）内部依次做：

1. **配置预检**：找不到 `config.yaml`（或 `backend/config.yaml`、`DEER_FLOW_CONFIG_PATH` 指向的文件）直接报错退出，提示先 `make setup`/`make config`；
2. **自动 `config-upgrade`**：把 `config.example.yaml` 里缺失的新字段递归合并进 `config.yaml`（先备份 `config.yaml.bak`）——所以旧配置升级代码后无需手动处理；
3. **同步依赖**：`uv sync --locked --quiet --all-packages`（自动带上 `UV_EXTRAS` 或从 config 探测的 extras，如 postgres/redis）+ 前端 `pnpm install`；
4. 依次启动三个服务并等待端口就绪：

| 服务 | 命令 | 端口 | 就绪超时 |
|------|------|------|---------|
| Gateway | `cd backend && uv run --no-sync uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001`（dev 加 `--reload`，reload 监听 `*.yaml` 与 `.env`，排除 `backend/.deer-flow` 与 `backend/sandbox`） | 8001 | 30s |
| Frontend | `pnpm run dev`（Next.js dev server，端口被固定为 3000） | 3000 | 300s |
| Nginx | `nginx -c docker/nginx/nginx.local.conf` | 2026 | 10s |

启动成功横幅：

```
==========================================
  ✓ DeerFlow is running!  [DEV (Gateway runtime, hot-reload enabled)]
==========================================
  🌐 http://localhost:2026
  📋 Logs: logs/{gateway,frontend,nginx}.log
```

- **访问入口**：http://localhost:2026（推荐，同源无 CORS 问题）。直接访问 8001/3000 也可以，但跨源场景要设 `GATEWAY_CORS_ORIGINS`；
- **日志**：仓库根 `logs/{gateway,frontend,nginx}.log`（前台模式各服务终端也有输出；前端报错在浏览器 console，后端 traceback 在 Gateway 终端）；
- **停止**：前台 `Ctrl+C`（会触发清理）；后台（`make dev-daemon`）用 `make stop`；再启动前若端口被占，先 `make stop`（`serve.sh` 也会先回收本仓库占用的 8001/3000/2026 再启动）；
- **热重载**：dev 模式 uvicorn 带 `--reload`，且 reload 规则包含 `*.yaml` 与 `.env`——开发期改 `config.yaml` 会自动生效；少数「冻结项」（如 `database.checkpoint_channel_mode`、`database.checkpoint_delta.snapshot_frequency`）例外，改了要重启进程；
- **生产本地模式**：`make start` / `make start-daemon`（先 `next build`，可用 `SKIP_FRONTEND_BUILD=1` 复用上次构建）；
- 想用 LangGraph Studio 调试图：在 `backend/` 下 `uv run langgraph dev --allow-blocking`（仅开发调试用，不适用于生产）。

## 3.9　启动方式二：Docker（推荐体验）

要求 Docker Engine + **Compose v2.24+**。开发模式（`docker/docker-compose-dev.yaml`）源码挂载 + 热重载，与 `make dev` 体验一致：

```bash
make docker-init     # 拉取沙箱镜像（仅容器沙箱需要；local 模式自动跳过）
make docker-start    # 启动
```

`docker-start` 行为（`scripts/docker.sh`）：

- 缺 `config.yaml` 时会**自动从 example 复制**并提示你补 key 和模型；
- 按 `config.yaml` 的 `sandbox.use` **模式感知**决定服务集合：
  - `LocalSandboxProvider`（默认）→ 容器 `redis frontend gateway nginx`（Nginx 发布 `127.0.0.1:2026`）；
  - `AioSandboxProvider`（无 `provisioner_url`）→ 额外叠加 `docker-compose.dood.yaml`，把 Docker socket 挂进 Gateway 供 AIO 沙箱建容器；
  - AioSandboxProvider + `provisioner_url` → 额外启动 `provisioner`（端口 8002）；
- compose 文件把入口发布为 `"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"`——**默认只绑回环地址**，安全模型是「本地可信环境」；要对外暴露须显式设 `BIND_HOST=0.0.0.0` 并自行加 TLS/防火墙（见 `.env.example` 注释）；
- Docker 构建默认走上游 uv 镜像源，受限网络可先导出 `UV_INDEX_URL`（如清华 PyPI 镜像）与 `NPM_REGISTRY`（如 npmmirror）再执行。

日常操作：

```bash
make docker-logs             # 全部
make docker-logs-frontend    # 仅前端
make docker-logs-gateway     # 仅 Gateway
make docker-logs-redis       # 仅 Redis
make docker-stop             # 停止
```

生产（本地构建镜像、挂载运行时配置与数据）：

```bash
make up      # 构建 + 启动；等待 Gateway /health 就绪才报告成功，失败会打印容器状态与近期日志
make down    # 停止并删除容器
```

## 3.10　验证：浏览器与健康检查

1. 浏览器打开 **http://localhost:2026**；
2. **默认免登录**：`config.yaml` 里 `authorization.enabled` 默认为 `false`，直接进入工作区；若你开了鉴权，首次通过 `POST /api/v1/auth/initialize` 创建管理员（该接口与 `allow_registration` 无关、只要还没有 admin 就可用，防止把自己锁在外面），之后用户可经 `POST /api/v1/auth/register` 自助注册（`auth.local.allow_registration` 默认 true）；
3. 页面右上选择模型（确保已配置且密钥可连通），输入第一个任务试试：

> 帮我调研 2026 年大模型推理优化方向，梳理主要开源项目、性能对比与社区活跃度，输出一份结构化报告。

4. 观察 UI 实时流：Lead Agent 分析 → 按需加载技能 → （若启用）子智能体并行调研 → web_search/web_fetch 取证 → 汇总写盘（产物整理到输出目录并由终端卡片呈现）→ 完成后可查看报告文件；
5. 健康检查与日志：

```bash
curl -s http://localhost:8001/health    # 本地模式直接探 Gateway，期望 {"status":"healthy","service":"deer-flow-gateway"}
tail -f logs/gateway.log                # 本地模式
# Docker 模式：make docker-logs-gateway，看到 "Application startup complete" 即就绪
```

常见启动失败速查：

| 现象 | 原因 | 处理 |
|------|------|------|
| 2026 打不开 | Nginx 未起/端口占用 | 看 `logs/nginx.log`；`make stop` 后重试；`nginx -v` 确认已装 |
| 页面 502 | Gateway 没起来 | `logs/gateway.log` 末尾；模型/密钥配置错误常在此暴露 |
| 聊天报 provider 401/404 | key 没填/模型 id 错 | 补 `.env` 并重启；`make doctor` 验证连通性 |
| 发消息无模型可选 | `models:` 为空 | 模板里模型全是注释，至少启用一个非注释条目（见 3.5） |
| 改了配置不生效 | 属「冻结项」 | `checkpoint_channel_mode` 等需重启 Gateway 的项，见 3.8 |

## 3.11　常用配置项速查

| 配置（均在 `config.yaml`） | 默认 | 说明 |
|------|------|------|
| `config_version` | 39 | schema 版本；`make config-upgrade` 自动合并缺失字段并备份 `.yaml.bak` |
| `log_level` | info | deerflow 模块日志级别 |
| `models[0]` | —（模板全注释） | 列表第一个为默认模型 |
| `tools:` | DDG 搜索 + Jina 抓取 + 文件工具 + bash | 同名工具只能启用一个 provider |
| `sandbox.use` / `allow_host_bash` | local / false | 沙箱 provider；宿主 bash 默认关 |
| `skills.path` / `container_path` | 项目根 `skills/` / 沙箱内挂载点 | 技能目录与挂载点；`deferred_discovery: true` 可改按需发现技能 |
| `summarization.enabled` | true | 长对话自动摘要（tokens/messages/fraction 触发） |
| `token_budget.enabled` | false | 单 run 硬预算（`max_tokens` 默认 200000），超限强制收尾 |
| `database.backend` | sqlite | sqlite（默认，`sqlite_dir: .deer-flow/data`）/ postgres；共享给 checkpointer/Store/应用数据 |
| `run_events.backend` | memory | 事件持久化：memory / db / jsonl；生产建议 db |
| `authorization.enabled` | false | 关闭=本地免登录；开启后走 `/api/v1/auth/initialize` + `/api/v1/auth/*` |
| `scheduler` | enabled: false | 定时任务；多 Gateway 实例要 Postgres + `run_ownership.heartbeat_enabled` + `run_events.backend: db` |

`.env` 里值得知道的部署级变量：

| 变量 | 说明 |
|------|------|
| `PORT` / `BIND_HOST` | **仅 Docker ingress**：Nginx 发布端口（默认 2026）与绑定地址（默认 127.0.0.1）；不影响本地 `make dev` 的端口 |
| `GATEWAY_CORS_ORIGINS` | 拆分源/端口转发部署时的浏览器 CORS 白名单（逗号分隔精确 origin） |
| `UV_INDEX_URL` / `NPM_REGISTRY` | 受限网络的镜像源加速（构建期） |
| `LANGSMITH_*` / `LANGFUSE_*` | 链路追踪开关与端点 |
| `DATABASE_URL` | 仅 `database.backend: postgres` 时需要 |
| `GATEWAY_ENABLE_DOCS=false` | 生产关闭 Swagger/ReDoc/OpenAPI |
| `GATEWAY_WORKERS` | 生产默认 1；多 worker 需要 Postgres + Redis stream bridge 等配套（见 README） |

## 小结

- **两条推荐路径**：本地开发 `make check && make install && make dev`；一键体验 `make docker-init && make docker-start`；生产 `make up`。入口统一为 http://localhost:2026（Nginx），后端 8001、前端 3000。
- **配置三件套**：`make setup`（推荐向导）或 `make config`（全量模板）→ `config.yaml` 配模型/沙箱/工具 → `.env` 放密钥；MCP/技能另配 `extensions_config.json`（可运行时经 API 修改）。
- **开箱即用项**：默认 DDG 搜索免 key；沙箱默认 Local 但**宿主 bash 关闭**；模板里模型条目全是注释，记得至少启用一个；用 Docker/AIO 沙箱要先 `make setup-sandbox` 或 `make docker-init`。
- **迭代提示**：DeerFlow 处于快速迭代期，配置 schema 会随 `config_version` 演进——升级代码后跑一次 `make config-upgrade`（或直接 `make dev`，它会自动合并），别手动照抄旧配置。
- **下一步**：跑通后建议接着读第 4 章（仓库结构深潜）与第 5 章（Lead Agent / Agent 核心），理解内嵌 runtime 与中间件链；要动手写技能可参考旧书第 12 章对应主题（docs-local 目录下后续会补齐自定义技能章节）。
