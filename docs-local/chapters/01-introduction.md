# 第 1 章　DeerFlow 是什么，为什么重要

> 基于 DeerFlow 最新源码（本仓库 commit 2672e209，2026-09）编写。本仓库快照版本为 **2.1.0**（`backend/pyproject.toml` 与 `frontend/package.json` 同步锁定），MIT License。
>
> 本章所有路径、类名、端口、配置项均以当前源码为准。旧版图书基于 2026 年初的 2.0 早期结构写作（当时后端还是单层目录、无 harness/app 分层，技能声明方式与"11 层中间件"的说法均已过时），**本章已按最新代码重写校准**，请勿以旧书路径检索代码。

## 本章导航

本章回答三个问题：**DeerFlow 是什么**（定位与全栈形态）、**为什么重要**（时代背景与架构价值）、**为什么值得拿它做二次开发**（可定制面与代价）。读完本章你将掌握：

| 小节 | 内容 | 对应权威依据 |
|------|------|--------------|
| 1.1 定位 | Super Agent Harness、全栈组成（harness 框架 + App 网关 + Next.js 前端 + IM 通道） | 根 `AGENTS.md` → *What is DeerFlow* |
| 1.2 背景 | Long-horizon Agent 窗口期、v1→2.0 重写、社区用例 | `README.md` → *From Deep Research to Super Agent Harness* |
| 1.3 对比 | Harness vs Framework、与封闭 Agent 产品的差异 | README / 本书观点 |
| 1.4 架构 | Service Topology（四服务与端口）、Nginx 路由、monorepo 目录地图、Harness/App 分层 | 根 `AGENTS.md` + `backend/AGENTS.md` |
| 1.5 能力 | 沙箱执行、持久记忆、子代理委派、Skills/MCP、多平台 Gateway | `backend/AGENTS.md` 各子系统 |
| 1.6 技术栈 | 端到端技术栈对照表 | 根/backend/frontend `AGENTS.md` |
| 1.7 二次开发 | 分层可改点、开箱即用收益、边界与代价 | 全书论证基础 |
| 1.8 衔接 | 本书阅读路径与代码引用约定 | — |

**全书代码引用约定**：代码块标题中的路径均相对仓库根。旧书目录结构已整体迁移——例如 lead agent 提示词文件旧书写作单层目录中的 `agents/lead_agent/prompt.py`，当前真实路径是 `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`；主配置仍在仓库根，但模板文件叫 `config.example.yaml`（复制为 gitignored 的 `config.yaml`）；服务端口 2026（Nginx）/ 8001（Gateway）/ 3000（Frontend）维持不变，详见 1.4 节。

---

## 1.1　一句话定位：LangGraph-based Super Agent Harness

DeerFlow（**D**eep **E**xploration and **E**fficient **R**esearch **Flow）是字节跳动开源（bytedance/deer-flow，MIT）的 **LangGraph-based AI super-agent 系统**。仓库根 `AGENTS.md` 给出了权威定位：

> DeerFlow is a LangGraph-based AI super-agent system with a full-stack architecture. The backend runs a "super agent" with sandboxed execution, persistent memory, subagent delegation, and extensible tools (built-in, MCP, community), all per-thread isolated. The frontend is a Next.js chat UI. External IM platforms (Feishu, Slack, Telegram, Discord, DingTalk) bridge into the same agent through the Gateway.

拆开看，这句官方定位其实覆盖了四个层次，也就是当前版本的"全栈"含义：

1. **Agent 内核（harness 框架层）**：一个可发布的 Python 包 `deerflow-harness`（导入前缀 `deerflow.*`，位于 `backend/packages/harness/deerflow/`），内含 agent 编排（LangGraph 图）、中间件链、沙箱、记忆、子代理、技能、MCP 客户端、模型工厂与配置系统——**不依赖任何 Web 框架即可构建并运行 agent**。
2. **网关与渠道（app 应用层）**：未发布的 FastAPI 应用（导入前缀 `app.*`，位于 `backend/app/`），提供 REST API（`app/gateway/`）与 IM 通道（`app/channels/`），把同一套 agent 暴露成 HTTP 服务，并桥接 Feishu/Slack/Telegram/Discord/DingTalk 等外部消息平台。
3. **Web 前端**：`frontend/` 下的 Next.js 聊天界面（线程对话、流式输出、artifacts、todo/goal、子代理面板等）。
4. **执行沙箱**：Local / Docker(AIO) / Kubernetes provisioner / E2B 四种执行环境抽象，agent 的代码执行被隔离在每线程独立的目录与进程/容器边界内。

官方 README 的措辞更进一步，直接点明它**不是让你自己拼装的框架，而是 batteries-included 的运行时**：

> DeerFlow 2.0 is no longer a framework you wire together. It's a super agent harness — batteries included, fully extensible. Built on LangGraph and LangChain, it ships with everything an agent needs out of the box: a filesystem, memory, skills, sandbox-aware execution, and the ability to plan and spawn sub-agents for complex, multi-step tasks. Use it as-is. Or tear it apart and make it yours.

"Use it as-is. Or tear it apart and make it yours."——这句话是全书（尤其第 1.7 节"为什么选它做二次开发"）的总纲：DeerFlow 把"能直接跑"和"能拆开改"同时作为设计目标，而不是像黑盒产品那样只给一个 API。

### 版本演进速览

- **v1（2025-05 起）**：Deep Research 专用框架，接收研究问题→自动搜索→生成报告。
- **2.0（2026-02-28）**：彻底重写，与 v1 **零代码共用**，发布当天登顶 GitHub Trending #1（README 顶部徽章区有官方记录）；v1 冻结在 `1.x` 分支继续维护。
- **当前快照（2.1.0，2026-09）**：本仓库 commit `2672e209`，即本书编写基准。

为什么 2.0 敢"推倒重来"？答案在下一节。

---

## 1.2　为什么重要：Long-horizon Agent 的窗口期

### 1.2.1 社区把 Deep Research 用成了"第一个 Skill"

v1 的定位很窄，但社区用脚投票做了 DeerFlow 团队没预料到的事：

- 有人拿它做**数据分析**，丢 CSV 让 agent 自动建模；
- 有人做 **PPT 生成**，一句话产出完整演示文稿；
- 有人搭**内容工厂**，批量生成产品文案与 SEO 文章；
- 有人做**运维巡检**，定时检查服务器并生成报告。

这些用例的共同结论是：Deep Research 只是 DeerFlow 能做的第一个技能，DeerFlow 的本质是 **Harness**——一个给 agent 提供运行时基础设施的平台。于是团队在 2026 年春节决定从头重写（详见根 `README.md` 的 *From Deep Research to Super Agent Harness* 一节）。

### 1.2.2 Long-horizon Agent 的三个特征

DeerFlow 瞄准的是 **Long-horizon Agent**——运行时间从分钟到小时、需要自主决策、最终产出"初稿级"交付物的任务。三个特征缺一不可：

| 特征 | 含义 | DeerFlow 的对应设计 |
|------|------|--------------------|
| **运行时间长** | 不是一问一答，而是持续执行分钟到小时级任务（一份 10–30 分钟的市场调研） | 每线程隔离的 checkpoint/run 生命周期（`packages/harness/deerflow/runtime/`）、上下文压缩与摘要中间件、`recursion_limit` 默认 1000 |
| **自主决策** | Lead Agent 不是按预设流程跑的状态机，而是动态拆解、动态委派 | LangGraph 图 + 中间件链 + `task` 委派工具 + 计划模式（TodoList），见第 5 章 |
| **产出"初稿"** | 目标是可继续修改的交付物（报告/网站/幻灯片/代码），不是一句话答案 | 文件工具 + 沙箱文件系统 + artifacts 前端卡片，输出落在线程的 `user-data/outputs` |

### 1.2.3 为什么"现在"才成立

正如 LangChain 创始人 Harrison Chase 的判断：Long-horizon Agent 之所以今天才真正 work，是因为三件事同时成熟——**模型推理能力**（reasoning 足够强，能支撑长链规划）、**工具生态**（MCP 等标准让 agent 能调用几乎任何服务）、**上下文工程**（context engineering 让 agent 在长任务中不"忘事"）。DeerFlow 恰好诞生在这三者的交集上，并在 2.x 把上下文工程做成了中间件链上的显式组件（`agents/middlewares/` 下的 summarization、memory、token budget、dynamic context 等），这是它区别于早期研究框架的本质升级。

> **阅读提示**：对中间件链与上下文工程的源码级剖析，见本仓库 `docs-local/middleware/`（fork 自有文档）与本书第 5、10 章。

---

## 1.3　DeerFlow vs Framework vs 封闭 Agent 产品

### 1.3.1 Harness 不是 Framework

很多人把 DeerFlow 与 LangChain、LlamaIndex 这类 Framework 相提并论，但设计哲学不同：

| 维度 | Framework（框架） | Harness（运行时平台） |
|------|-------------------|----------------------|
| 设计理念 | 提供积木，自己搭 | Batteries included，开箱即用 |
| 执行环境 | 开发者自己搞定 | 自带沙箱、文件系统、进程隔离 |
| 决策权 | 开发者写流程 | Agent 自主规划、拆解、执行 |
| 扩展方式 | 插件/中间件 | Skills + MCP + Python 扩展 + 子代理 |
| Opinionated 程度 | 低（灵活但胶水代码多） | 高（约定优于配置，但层可替换） |

DeerFlow 是 **opinionated** 的：它预设了 Lead Agent 委派子代理的模式、预设了沙箱执行环境、预设了 Skills 的加载方式。你可以改，但不改也能直接跑。注意一个易被忽略的层次差别：DeerFlow **本身构建在 LangChain/LangGraph 之上**，所以"Framework vs DeerFlow"不是替代关系，而是**地基 vs 房子**的关系——LangGraph 提供图执行与 checkpoint 原语，DeerFlow 提供面向产品的完整运行时。

### 1.3.2 与封闭 Agent 产品的差异

| 产品 | 定位 | 交互方式 | 开源/自托管 |
|------|------|---------|------------|
| **DeerFlow** | Super Agent Harness | 任务驱动，产出文件/网站/报告 | 是（MIT，可自托管） |
| OpenAI Operator | 浏览器自动化 Agent | 操控浏览器完成任务 | 否 |
| Manus | 通用 AI Agent | 云端运行、虚拟桌面 | 否 |
| Claude Computer Use | 计算机控制能力 | 操控鼠标键盘 | API（非产品） |

核心差异：DeerFlow 是**开源的、可自托管的、面向开发者的**。你可以在自己的服务器上跑它、用自己的模型、扩展自己的技能与渠道；它不是一个封闭产品，而是一个可以拆开改的平台——这正是本书（一份面向"要改它的人"的深度说明书）存在的前提。

---

## 1.4　全栈架构：Service Topology 与仓库地图

本节内容**全部对照当前仓库权威文档**：根 `AGENTS.md`（monorepo 总览）与 `backend/AGENTS.md`（后端深度）。

### 1.4.1 四服务拓扑

一条 `make dev`（本地开发）或 Docker Compose（生产）命令拉起四个协作服务：

| Service | Port | Role |
|---------|------|------|
| **Nginx** | `2026` | 统一反向代理入口——浏览器打开的就是它 |
| **Gateway API** | `8001` | FastAPI REST API + 内嵌 LangGraph 兼容 agent 运行时 |
| **Frontend** | `3000` | Next.js Web 界面 |
| **Provisioner** | `8002` | 可选——仅当沙箱配置为 provisioner/K8s 模式时启动 |

（表源：根 `AGENTS.md` → *Service Topology*。）

端口细节值得注意，因为它们同时是安全边界：

- 两个 compose 文件都以 `"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"` 发布入口——**默认只绑回环地址**，与 README 的部署模型一致；裸 `"${PORT}:2026"` 会绑到 `0.0.0.0`。
- Nginx 在容器内对 IPv4+IPv6 都监听 `default_server`；Gateway 在容器内故意绑 `0.0.0.0:8001`——两者都是容器内部行为，**对外只有 Nginx 的 2026 一个面**，Gateway 的 8001 刻意不发布。新增任何对外端口都必须显式给出绑定地址（`backend/tests/test_compose_default_bind_host.py` 对两个 compose 文件的每个服务都钉死了这一约束）。
- 根目录 `PORT` 只影响 Docker 入口；本地编排把 Next.js 钉在 3000，防止加载 `.env` 让 `make dev` 等到错误的端口。

### 1.4.2 Nginx 的唯一入口与路由规则

Nginx 是唯一公网入口，职责三路分流：

```
浏览器 / 外部客户端
        │
        ▼
   Nginx :2026  ── 唯一对外入口（HTML/文本压缩；SSE/字体/图片/音视频不压缩）
        │
        ├── /api/langgraph/*   → 代理到 Gateway 内嵌 LangGraph 运行时(8001)，
        │                       并重写为 Gateway 原生 /api/* 路由
        ├── /api/*（其余）      → 直连 Gateway REST 路由组(8001)
        └── /（非 API）         → Frontend(3000)
```

要点：**LangGraph 兼容的运行时以 `/api/langgraph/*` 对外暴露，但内部实际就是 Gateway 的原生路由**——这是"同一套 agent 同时服务 Web、SDK、IM 三端"的关键机制（详见 `backend/AGENTS.md` 与后端 `app/gateway/` 的 LangGraph 兼容层）。

### 1.4.3 monorepo 目录地图

根 `AGENTS.md` 给出了当前仓库（2.x）的真实布局，摘录如下——**请对比旧书目录，路径已全部变化**：

```
deer-flow/
├── Makefile                        # 根编排：dev/start/stop、docker、setup（整栈入口）
├── config.example.yaml             # 模板 → 复制为 config.yaml（gitignored，仓库根）
├── extensions_config.example.json  # 模板 → extensions_config.json（gitignored）：MCP servers + skills
├── backend/                        # Python 后端 —— 见 backend/AGENTS.md
│   ├── extensions/sources/         # 本地安装的 Python 扩展的可部署快照
│   ├── packages/extension-api/     # deerflow-extension-api（导入 deerflow_extension_api.*）——公开扩展契约
│   ├── packages/harness/           # deerflow-harness（导入 deerflow.*）——agent 框架（可发布包）
│   └── app/                        # FastAPI Gateway + IM 通道（导入 app.*）
├── frontend/                       # Next.js 前端（pnpm）——见 frontend/AGENTS.md
├── docker/                         # docker-compose 文件、nginx 配置、provisioner
├── skills/                         # Agent 技能：public/（提交入库）、custom/（gitignored）
├── contracts/                      # 跨组件 JSON 契约（子代理状态、技能评审等）
├── examples/deerflow-extension-example/  # 演示全部五类扩展贡献点的参考扩展
├── scripts/                        # Makefile 调用的根级编排脚本
├── tests/                          # 根级测试（当前为 skills 公共技能测试）
└── docs/                           # 横切文档与设计笔记
```

关键变化点（对比旧书）：

- **后端目录已重构，不再有旧书那种 backend 单层布局**：框架代码收敛为可发布包 `backend/packages/harness/`（内部再按子系统分目录，见 1.4.4），应用代码在 `backend/app/`，公共扩展契约独立成包 `backend/packages/extension-api/`。
- **配置在仓库根**：`config.example.yaml` → `config.yaml`（主应用配置）；`extensions_config.example.json` → `extensions_config.json`（MCP servers + skills）。两个真实文件都 gitignored，且**可通过 Gateway API 在运行时编辑**（`config.yaml` 生产挂载只读 `:ro`，`extensions_config.json` 因运行时写入而挂载读写——这是有测试钉死的生产约束）。
- **技能目录在仓库根**：`skills/public/`（提交）+ `skills/custom/`（本地）；受管集成技能包放在全局 `.deer-flow/integrations/skills/{provider}/`（如 lark-cli），每个用户的启用状态与凭据仍按用户隔离。

### 1.4.4 Harness / App 分层与依赖纪律

后端最重要的结构决策是**分层 + 单向依赖**（`backend/AGENTS.md` → *Harness / App Split*）：

- **Harness**（`packages/harness/deerflow/`）：可发布的 agent 框架包 `deerflow-harness`，导入前缀 `deerflow.*`。包含 agent 编排、工具、沙箱、模型、MCP、技能、配置——构建与运行 agent 所需的一切。**它不 import FastAPI 的 app 层**，因此可以脱离 Web 独立使用（`deerflow/client.py` 的 `DeerFlowClient` 与 `deerflow.agents.create_deerflow_agent` 工厂就是面向"嵌入式集成"的出口）。
- **App**（`app/`）：未发布的应用代码，导入前缀 `app.*`。包含 FastAPI Gateway（`app/gateway/`）与 IM 通道（`app/channels/`）。
- **依赖规则**：App 可以 import deerflow，**deerflow 永不 import app**。这条边界由 CI 中的 `tests/test_harness_boundary.py` 强制执行。

```python
# 允许：App → Harness
from deerflow.agents import make_lead_agent
from deerflow.config import get_app_config

# 允许：App 内部
from app.gateway.app import app
from app.channels.service import start_channel_service

# 禁止：Harness → App（test_harness_boundary.py 会让 CI 失败）
# from app.gateway.routers.uploads import ...
```

Harness 内部同样按子系统拆目录（`backend/AGENTS.md` 目录地图，摘录核心）：

```
backend/packages/harness/deerflow/
├── agents/            # LangGraph agent 系统
│   ├── lead_agent/    # 主 agent（factory + 运行时组装的 system prompt）
│   ├── middlewares/   # 中间件组件（内存/技能激活/摘要/令牌预算/…）
│   ├── memory/        # 记忆抽取、队列、提示词；backends/（DeerMem/mem0/honcho/…）
│   └── thread_state.py# ThreadState schema
├── sandbox/           # 沙箱执行系统（sandbox.py 抽象 + local/ + tools.py 等）
├── subagents/         # 子代理委派（builtins/、executor.py、registry.py）
├── tools/builtins/    # 内置工具（present_file、ask_clarification、view_image、task、…）
├── mcp/               # MCP 集成（tools、cache、client、mcp_tasks 持久任务）
├── integrations/      # 受管一等集成安装器（如 Lark CLI 技能包）
├── extensions/        # Python 插件加载器/注册表/隔离
├── models/            # 模型工厂（thinking/vision 支持，多 provider）
├── skills/            # 技能发现、加载、解析
├── config/            # 配置系统（app/model/sandbox/tool 等）
├── community/         # 社区工具（web search/fetch、图像搜索、AIO 沙箱）
├── runtime/           # RunManager + run_agent() + StreamBridge（运行期）
├── persistence/       # SQLite/Postgres、Alembic 迁移、channel_connections、scheduler 表
├── scheduler/         # 后台定时任务调度器（非交互运行）
├── tui/               # 终端工作台（Terminal Workbench，CLI/TUI）
├── authz/ guardrails/ tracing/ uploads/ utils/ …   # 授权、护栏、追踪、上传等横切件
└── client.py          # 内嵌 Python 客户端（DeerFlowClient）

backend/app/
├── gateway/           # FastAPI Gateway（app.py + routers/ + authz/ + langgraph 兼容层）
└── channels/          # IM 平台集成（feishu/slack/telegram/discord/dingtalk/github/buzz…）
```

> 目录是常青的"活地图"：每个关键子系统都带自己的 `AGENTS.md`（如 `agents/AGENTS.md`、`sandbox/AGENTS.md`、`mcp/AGENTS.md`、`extensions/AGENTS.md`），读源码前先读最近邻的 `AGENTS.md` 是本书推荐的最高效路径。

### 1.4.5 运行期：RunManager + run_agent() + StreamBridge

`make dev`、Docker dev、生产三种模式跑的是**同一套运行时**：Gateway 进程内嵌 agent runtime，核心是 `RunManager` + `run_agent()` + `StreamBridge`（`packages/harness/deerflow/runtime/`）。对外，Nginx 把 `/api/langgraph/*` 重写为 Gateway 原生 `/api/*` 路由，于是：

- **Web 前端**用 LangGraph SDK（`@langchain/langgraph-sdk`）走 `/api/langgraph/*` 流式对话（`messages-tuple` 流 + `values` 快照）；
- **IM 通道**（`app/channels/`）同样通过 `langgraph-sdk` HTTP 客户端回流 Gateway——**与前端是同一个入口、同一套线程/运行管理**，通道只做消息协议转换（详见 1.5.6）；
- 流式协议上有精细工程：`write_file`/`str_replace` 参数增量被分批推送；`stream_subgraphs` 时子图帧保留命名空间（`values|<ns>`，LangGraph Platform 风格），避免被当作根帧覆盖整线程视图（#4399）。

### 1.4.6 启动与部署矩阵

| | 本地前台 | 本地守护 | Docker Dev | Docker Prod |
|---|---|---|---|---|
| Dev | `make dev` / `./scripts/serve.sh --dev` | `make dev-daemon` | `make docker-start` | — |
| Prod | `make start` | `make start-daemon` | — | `make up`（`./scripts/deploy.sh`） |
| 停 | `make stop` | 同上 | `make docker-stop` | `make down` |

生产 Compose 用镜像预构建的 Python 环境（`uv run --no-sync`），Gateway 提供真实 `/health` 探针，`make up` 会等探针通过才打印成功横幅。另有 Helm charts（`deploy/helm/deer-flow/`），版本号与 `backend/pyproject.toml`、`frontend/package.json` 三方锁步（`scripts/verify_versions.sh` 在 CI 强制）。首次使用顺序：`make config` → `make install` → `make dev`（`make dev` 不会替你生成配置文件）。

---

## 1.5　核心能力拆解（五件套 + Gateway）

### 1.5.1 沙箱执行（Sandbox）

DeerFlow 不只"说"，它有自己的"电脑"（README 原话：*DeerFlow doesn't just talk about doing things. It has its own computer.*）。沙箱子系统位于 `packages/harness/deerflow/sandbox/`：`sandbox.py` 定义抽象接口，`tools.py` 提供 `bash`、`ls`、`read_file`/`write_file`/`str_replace` 等文件工具，`middleware.py` 管理沙箱生命周期。

四种执行环境按 `config.yaml → sandbox.use` 切换：

| Provider | 配置类 | 隔离级别 | 适用 |
|----------|--------|---------|------|
| **Local**（默认） | `deerflow.sandbox.local:LocalSandboxProvider` | 宿主机 + 受管工具路径边界；**宿主 bash 默认禁用** | 可信单用户本地工作流 |
| **AIO / Docker** | `deerflow.community.aio_sandbox:AioSandboxProvider` | 容器（all-in-one sandbox 镜像，x86_64/arm64） | 需要真文件系统边界的场景 |
| **Kubernetes provisioner** | `docker/provisioner/`（8002） | 容器（hostPath/PVC） | 多机/生产集群 |
| **E2B** | E2B 云 VM | 远程 VM（warm pool、replicas/burst、可选 Redis 容量所有权） | 云上弹性执行 |

每线程隔离是硬设计：上传、文件、产物都落在按用户/线程分桶的目录（`users/{user_id}/threads/{thread_id}/user-data/{uploads,outputs}`），技能在沙箱内以只读投影挂载（`/mnt/skills`，随启用状态实时更新），受管集成的 CLI 运行时挂 `/mnt/integrations/...`。注意 README 的诚实边界：**Local 提供的是"受管工具路径边界"而非宿主机隔离**；要"外壳可执行 + 文件边界可强制"的组合，请用 Docker/AIO、K8s provisioner 或 E2B。旧书描述的"本地→Docker→K8s 按需切换"在 2.x 已发展为"多 Provider + 每线程隔离 + 技能投影"的完整模型。

### 1.5.2 持久记忆与上下文工程

> *Most agents forget everything the moment a conversation ends. DeerFlow remembers.*（README）

- **记忆后端**：默认本地后端 **DeerMem**（`agents/memory/backends/`，含容量驱逐策略评测 `backend/scripts/benchmark/deermem_eviction/`）；可选 `mem0`（托管/自托管 API）、`honcho`（服务端构建 user-model 记忆）、`openviking`（稳定 Session 捕获）。跨会话累积用户画像、偏好与知识，重复事实在写入时去重，全部本地存储、用户可控。
- **中间件驱动**：记忆不是散落的工具调用，而是中间件链上的显式环节——`agents/middlewares/memory_middleware.py`、`summarization_middleware.py`、`token_budget_middleware.py`、`dynamic_context_middleware.py`（回忆的记忆作为隐藏上下文注入，不进 checkpoint）等。上下文工程由此成为可配置、可插拔的管线，而非 prompt 技巧。
- **计划模式**：`config.configurable.is_plan_mode` 打开 TodoList 中间件，`write_todos` 工具跟踪多步任务，一个 in_progress + 实时更新。
- **上下文压缩**：`summarization` 配置支持 tokens/messages/max-input 比例三种触发方式；手动压缩走 `POST /api/threads/{id}/compact`。

### 1.5.3 子代理委派（Sub-Agents）

Lead Agent 可以按需派生子代理——每个子代理拥有**独立的上下文、工具集与终止条件**，在后台执行并回报结构化结果，由 Lead Agent 校验与综合。关键设计（README *Sub-Agents* + `backend/AGENTS.md`）：

- **委派是优化手段，不是复杂请求的默认响应**：只有"真实并行延迟收益 / 专业能力 / 上下文隔离"占优时才派生；互相依赖的作用域与重叠副作用不进并行派发（`max_concurrent_subagents: 1` 时并行引导被禁用）。
- **内置两种子代理**：`subagents/builtins/general_purpose.py` 与 `bash_agent.py`；可在 **Settings → Subagents** 增删改管理员级 worker 定义，Custom Agent 可白名单/黑名单选择。
- **后台执行引擎**：`subagents/executor.py`（`execute_async()` 生成服务端 `execution_id`）+ `registry.py`；身份刻意拆分——provider 的 `tool_call_id` 是消息/SSE/前端卡片的关联键（对外契约 `ExtensionData.scope_id` → `SubagentResult.external_task_id`），服务端 `execution_id` 才是注册表与轮询/取消/超时的所有权键（provider ID 跨 run 不全局唯一，绝不能当所有权键）。
- **批量委派**：`batch_task` 持久化批量执行（大集合存 SQL、独立 total/live/running 上限、重启恢复、线程级 Web 面板与 owner 限定 JSONL 导出）。
- **容量护栏**：`subagents.max_total_per_run`、`max_concurrent_subagents` 等由中间件（`subagent_limit_middleware.py`）强制；还有可选的确定性回执层（`verification.receipts_enabled`，默认开）。
- 子代理的内部 AI/工具消息**留在委派子图内**，不进父聊天流；长任务子代理在启用摘要时自行压缩历史。

### 1.5.4 Skills 渐进加载与工具体系

- **Skill 是什么**：一个结构化能力模块，核心是一个 `SKILL.md`（定义工作流、最佳实践、资源引用）。仓库根 `skills/public/` 提交内置技能（研究、报告、幻灯片、网页、图像/视频生成等），`custom/` 放自定义技能；可通过 Gateway 安装 `.skill` 归档。
- **渐进加载**：技能只有在任务需要时才加载（`skill_activation_middleware.py`），不是一次性灌进上下文——这保护了 token 预算，也让 token 敏感模型可用。用户可用 `/skill-name` 前缀对单轮显式激活某个已启用技能。
- **策略与信任边界**：技能可声明 `allowed-tools` 策略，激活后同时过滤"模型可见的工具 schema"与"工具执行"；但要记住 README 的明确警告——这是 best-effort 行为限定而非硬安全边界（通过另一工具读取技能指令不被捕获）。
- **质量治理**：内置只读技能评审器 `skills/public/skill-reviewer/`（用 harness 层 `review_skill_package` 工具），CI 强制公共技能变更过评审（waivers 机制见根 `AGENTS.md`）；`skill-creator` CLI 负责新技能脚手架。
- **工具体系**：内置工具（`tools/builtins/`：`present_file_tool`、`clarification_tool`、`view_image_tool`、`task_tool`、`batch_task_tool`、`tool_search`（延迟发现）、`invoke_acp_agent_tool`、`list_uploaded_files_tool`、`background_tasks_tool` 等）+ MCP 工具 + 社区工具（`community/`：web search/fetch/scrape、图像搜索、AIO 沙箱）+ 技能包工具，**全部按线程隔离**注册。

### 1.5.5 MCP 与 Python 扩展

- **MCP**：`packages/harness/deerflow/mcp/`（tools、cache、client）提供 Model Context Protocol 客户端；server 配置放 `extensions_config.json`，由 Gateway API（`PUT/PATCH /api/mcp/config`）运行时管理。长时 MCP 操作走独立持久任务运行时（`McpTaskService` + `mcp_tasks`，租约式恢复），不进 agent 主循环。
- **Python 扩展**：顶层 `config.yaml → plugins:` 列表（操作者控制、刻意不放 API 可写的 `extensions_config.json`）加载的第三方扩展可贡献**五类**东西：middleware、task lifecycle、system-model observers、Gateway services、FastAPI HTTP routers——参考实现见 `examples/deerflow-extension-example/`；公共契约独立成包 `backend/packages/extension-api/`（`deerflow_extension_api.*`）。管理命令：`deerflow extensions install/list/enable/disable/remove`（或根 `make extension-*`）。
- **受管集成**：`integrations/` 提供一等集成安装器，如 Lark/Feishu CLI 技能包（admin 安装一次 → 全局共享、每用户独立启用与 OAuth 凭据），是"技能作为产品分发"的样板。

### 1.5.6 多平台 Gateway：REST + LangGraph 兼容 + IM

Gateway（`backend/app/gateway/`）是 HTTP 面，路由组覆盖：threads、runs、models、memory、skills、uploads、artifacts、agents、subagents、scheduled_tasks、mcp、mcp_tasks、channels、channel_connections、integrations、suggestions、input_polish、auth、browser、github_webhooks、assistants_compat、console、feedback、subagent_batches 等（`app/gateway/routers/`）。它同时提供 **LangGraph 兼容层**（`langgraph_studio.py`、`assistants_compat.py`、`langgraph_auth.py`），让 LangGraph SDK/Studio 生态的客户端可以直接接入。

IM 通道系统（`backend/app/channels/`，其 `AGENTS.md` 是最权威说明）：

- **桥接架构**：通道与 Gateway 之间走 `langgraph-sdk` HTTP 客户端（与前端一致），内部客户端注入进程内 internal auth + CSRF 对，线程在服务端统一创建管理。
- **官方渠道**：Feishu/Lark、Slack、Telegram、Discord、DingTalk（`backend/AGENTS.md` 列出的正式集），另有 GitHub（webhook 驱动，出站仅日志，agent 用沙箱内 `gh` CLI 主动回写）与 Buzz（Nostr relay，需要 `buzz` extra；NIP-42 认证 + pubkey 白名单，流式回帖用 kind-40003 原地编辑）。本快照 `app/channels/` 目录下还可见 `wechat.py`/`wecom.py` 等实现文件——是否启用以 `config.yaml` 配置与文档为准。
- **消息流**：平台消息 → 通道实现 → `MessageBus.publish_inbound()` → `ChannelManager._dispatch_loop()` → 查/建线程 → `runs.stream()`/`runs.wait()`/`runs.create()`（按渠道策略选择增量更新、一次性回复或 fire-and-forget 长任务）→ 累积 AI 文本 → 出站。流式出站采用**白名单而非黑名单**（只发布 assistant 类型消息），防止隐藏模型上下文（`<memory>`、`<durable_context_data>` 等）泄漏到 IM。
- **命令面**：`/new`、`/status`、`/models`、`/memory`、`/goal`、`/help` 等由 manager 本地处理或查 Gateway API。
- 多用户与授权：`authz/`、`personal_access_tokens`、`auth/`（含 CSRF、internal auth、github OAuth 等），每用户的通道连接与凭据 SQL 持久化（`persistence/channel_connections`）。

### 1.5.7 模型接入、前端与可观测（速览）

- **模型工厂**（`packages/harness/deerflow/models/`）：统一 `create_chat_model`，支持 thinking/reasoning-effort 与 vision；provider 不止 OpenAI 系——`langchain_*` 全家桶、OpenRouter、vLLM（`deerflow.models.vllm_provider:VllmChatModel`，含 `chat_template_kwargs.enable_thinking` 推理切换）、Codex CLI / Claude Code OAuth（CLI-backed provider）、MiniMax Code 等 ACP agent（`acp_agents:` + `invoke_acp_agent`）；`supports_thinking`/`supports_vision` 驱动能力探测。
- **前端**：Next.js 16 + React 19 + TS + Tailwind（详见 1.6）。线程化对话、流式渲染、artifacts、todos、线程级 `/goal`、自定义 agent、子代理/批量任务面板、定时任务页（`/workspace/scheduled-tasks`）、记忆与事实编辑页、MCP/技能/模型设置、IM 连接管理、受管集成（Lark）状态页、输入润色与语音输入、en-US/zh-CN 双语。
- **可观测**：LangSmith / Langfuse / Monocle 三选追踪（README *Advanced*），另有 harness `tracing/` 与 Gateway `trace_middleware.py`、`make support-bundle`（脱敏诊断包）与 `make doctor`。
- **上传**：`POST /api/threads/{thread_id}/uploads`，PDF/PPT/Excel/Word 经 `markitdown` 自动转文本，线程隔离落盘、原子写入。

---

## 1.6　技术栈一览

下表综合根/backend/frontend 三份 `AGENTS.md` 与源码声明，是 2.x 的真实栈（旧书 1.3 节表格已过期）：

| 层 | 技术 | 版本/说明（以当前源码为准） |
|----|------|---------------------------|
| Agent 运行时 | LangGraph + LangChain | 图执行、checkpoint、流式；`backend/langgraph.json` 供 LangGraph Studio |
| 后端语言 | Python | 3.12+，类型注解，ruff（lint+format，行宽 240） |
| 包/工具链 | uv | `uv run`、`--no-sync` 生产模式 |
| Web 框架 | FastAPI | Gateway（`app/gateway/`），Uvicorn |
| 前端 | Next.js 16 / React 19 / TypeScript 5.8 / Tailwind 4 | pnpm（10.26.2 钉版），Webpack 默认/Turbopack 可选；LangGraph SDK（`@langchain/langgraph-sdk` ^1.5.3）、TanStack Query、Shadcn UI 等 |
| 持久化 | SQLite（默认）↔ PostgreSQL | SQLAlchemy + Alembic（`make migrate-rev`）；checkpoint/memory/run/scheduler 等表；多实例需 Postgres |
| 执行沙箱 | Local / Docker(AIO) / K8s provisioner / E2B | `sandbox.use` 切换；all-in-one-sandbox 容器镜像 |
| 消息/流 | SSE（`messages-tuple` + `values`）、WebSocket、IM 长连接 | Nginx 层不压缩 SSE |
| IM 协议 | 各平台 SDK（Feishu/Slack/Telegram/Discord/DingTalk/GitHub/Buzz…） | `app/channels/` |
| 反向代理 | Nginx | :2026 唯一入口；`/api/langgraph/*` 重写 |
| 编排/部署 | Makefile + Docker Compose + Helm（`deploy/helm/deer-flow/`） | 四服务拓扑；`scripts/` 编排 |
| 测试 | pytest（后端，TDD 强制）+ Blockbuster 阻塞 IO 门禁；前端 Rstest/Playwright | `make test` / `make test-blocking-io` / `make test-live` |
| 追踪 | LangSmith / Langfuse / Monocle | 可配置 |

---

## 1.7　为什么选择 DeerFlow 做二次开发

### 1.7.1 五个可改层，边界清晰

DeerFlow 的"Harness"哲学意味着它交付执行骨架，而不是锁死行为。以当前代码为准，二次开发的切入点与仓库里的真实扩展点一一对应：

| 定制层 | 改什么 | 当前真实扩展点 |
|--------|--------|---------------|
| **Skill 层** | 添加能力单元 | 写 `SKILL.md` + 资源放 `skills/public|custom/`；`.skill` 归档经 Gateway 安装；`allowed-tools` 策略 |
| **Agent 层** | 定制行为/prompt/编排 | 换/包装 Lead Agent（`agents/lead_agent/`）、加中间件（`agents/middlewares/`，插进 LangGraph 链）、Custom Agent 定义 |
| **Memory 层** | 接企业知识库/个性化记忆 | 换 memory backend（`agents/memory/backends/` 的 DeerMem/mem0/honcho 为范例） |
| **Sandbox 层** | 适配私有化执行环境 | 实现 `sandbox.py` 抽象的新 Provider（`sandbox/local` 与 E2B 为范例），`sandbox.use` 切换 |
| **Channel 层** | 对接内部 IM/业务系统 | 实现 `app/channels/base.py` 的 `Channel` 子类（feishu/slack/telegram 为范例）；或 Python 扩展贡献五类点 |
| **整体** | 深度平台化 | Python `plugins:` 扩展（middleware/task lifecycle/observers/Gateway services/FastAPI routers）；或直接用 `create_deerflow_agent` + `DeerFlowClient` 把 harness 嵌进自己的应用，完全不要 Gateway/前端 |

### 1.7.2 开箱即用的收益（选它的"省"）

1. **Batteries included**：沙箱、记忆、技能系统、子代理编排、多模型接入、IM 桥接、定时任务、授权/护栏——`make dev` 一条命令整栈可跑，不用从零拼 LangGraph 胶水。
2. **单一模型配置管理多 provider**：thinking/vision 探测、CLI-backed provider（Codex/Claude OAuth）、vLLM 本地推理都收敛在 `config.yaml → models:` 一份配置里。
3. **LangGraph 兼容层白嫖生态**：SDK 客户端、Studio 调试、`/api/langgraph/*` 协议，前端与 IM 通道共用一套线程/运行语义。
4. **架构纪律被测试钉死**：harness/app 单向依赖（`test_harness_boundary.py`）、端口绑定（`test_compose_default_bind_host.py`）、TDD 强制——意味着 fork 下去不容易烂尾。
5. **MIT + 活跃社区 + 官方重写背书**：2.0 登顶 Trending 后迭代极快（本快照 2.1.0），且团队自身把"可扩展"当一等公民（官方 README 原话 *tear it apart and make it yours*）。

### 1.7.3 诚实提醒：选它的"代价"

- **快迭代 = 配置/API 漂移**：2.x 仍在快速演进，`config.yaml` 键与路由可能在版本间变动；二次开发务必锁版本/锁 commit（本书即以 commit `2672e209` 为基准），升级前读 `CHANGELOG.md`。
- **Opinionated 默认**：预设的 Lead Agent 委派模式、技能渐进加载等约定，绕过时需要理解中间件链（本书第 5 章）。
- **安全边界要靠部署兑现**：默认回环绑定（127.0.0.1:2026）、Local 沙箱非硬隔离、技能 `allowed-tools` 是 best-effort——对外暴露或处理不可信任务前，读根 `README.md` 的 ⚠️ Security Notice，并按文档启用 Docker/AIO、K8s provisioner 或 E2B。
- **规模化要上 Postgres**：多 Gateway 实例（定时任务多实例、E2B Redis 容量所有权、调度器租约恢复）要求共享 Postgres + 相应开关（`scheduler.multi_instance`、`run_ownership.heartbeat_enabled`、`run_events.backend=db`），默认 SQLite 面向单机。

> **选型结论（本书立场）**：如果你的诉求是"快速给团队/客户交付一个可控、可自托管、可深度定制、能接 IM 的长时任务 agent"，DeerFlow 的 Harness + 全栈形态是当前开源选项里少有的"整机交付、分层可拆"组合；如果你的诉求只是"在代码里调几个 LLM 工具函数"，那用 LangChain 直连更轻，DeerFlow 的运行时设施反而是负担。本书的读者默认属于前者。

---

## 1.8　与后续章节的衔接

本书按"机制（怎么工作）→ 平台（怎么用起来）→ 改法（怎么改它）"三层展开。第 1 章是定位层；后续章节将依序深入：

1. **核心机制层**：agent 与中间件链（本快照的中间件已重构，旧书"11 层"名单已过时，以 `agents/middlewares/` 当前清单为准）→ 运行时/checkpoint/流式 → 记忆 → 沙箱 → 子代理 → 技能 → MCP/扩展 → 上下文工程。
2. **平台层**：Gateway REST/路由与 LangGraph 兼容层 → IM 通道逐渠道剖析 → 前端数据流 → 配置系统与部署。
3. **二次开发实战**：自定义 skill、自研中间件/工具、扩展新 Sandbox Provider、对接新 IM 渠道、Python 插件五类贡献点、基于 `create_deerflow_agent` 的嵌入式集成。

**代码导航惯例**（本书通用）：先读目标目录的 `AGENTS.md` → 再读 `AGENTS.md` 点名的核心文件 → 用 `backend/tests/` 里的同名测试反推行为契约。全书路径一律以当前仓库为准，不再出现旧书式的过期路径。

## 本章小结

- **DeerFlow 是一个 LangGraph-based 全栈 super-agent 系统**：可发布框架包 `deerflow-harness` + FastAPI App（Gateway/IM）+ Next.js 前端 + 多沙箱执行，四服务（Nginx:2026 / Gateway:8001 / Frontend:3000 / Provisioner:8002 可选）一条 `make dev`/Compose 拉起。
- **它不是 Framework，是 Harness**：batteries included 且 opinionated，但每一层都可替换；它构建在 LangChain/LangGraph 之上，与框架是"地基 vs 房子"关系。
- **它瞄准 Long-horizon Agent**：长时间运行、自主决策、产出初稿级交付物；v1→2.0 的彻底重写源于社区把 Deep Research 用成了"第一个 Skill"。
- **核心能力五件套 + 多平台 Gateway**：沙箱执行（Local/Docker/K8s/E2B 每线程隔离）、持久记忆（DeerMem 默认 + mem0/honcho/openviking 可选）、子代理委派（内置两种 + 批量 + 容量护栏）、Skills 渐进加载 + MCP + Python 扩展、IM 渠道桥接（Web 与 IM 共用 LangGraph 兼容入口）。
- **二次开发的价值在于分层可改 + 边界被测试钉死**；代价是快迭代下的配置漂移与需要部署兑现的安全边界——这正是本书以固定 commit 为基准、逐层剖析的原因。

> **下一步**：阅读第二章，进入核心机制——LangGraph 图如何被组装、中间件链如何工作。
