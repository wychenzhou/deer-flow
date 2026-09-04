# DeerFlow 深度说明书(基于最新代码)

> **基于 DeerFlow 最新源码编写**:本仓库 commit `2672e209`(2026-09,上游 main ~2.1.0)
>
> 本书融合两本社区旧书的结构与素材——[hawkli-1994/deerflow-book](https://github.com/hawkli-1994/deerflow-book) 与 [coolclaws/deerflow-book](https://github.com/coolclaws/deerflow-book)——
> 但全部内容已按当前代码逐章校准:旧版目录(`backend/src/`、11 层中间件、`skill.yaml`、旧配置字段)一律作废,
> 以 `backend/packages/harness/`、35 链位中间件、`SKILL.md`、双层配置等新结构为准。
>
> 配套深文:`../middleware/middleware-01-io-safety.md` ~ `middleware-08-safety-guards.md`
> (8 篇逐中间件深度文档,本书第 6、7 章与其互为表里)。

## 目录

### 第一部分 · 认知与上手

| 章 | 文件 | 一句话主线 |
|----|------|-----------|
| 1 | [01-introduction.md](01-introduction.md) | DeerFlow 是什么、全栈拓扑、为什么选它做二次开发 |
| 2 | [02-repo-overview.md](02-repo-overview.md) | monorepo 全景:harness / app / extension-api 三层与边界 |
| 3 | [03-quick-start.md](03-quick-start.md) | 从零跑起:依赖、配置三步走、make dev / Docker、验证 |

### 第二部分 · 核心引擎

| 章 | 文件 | 一句话主线 |
|----|------|-----------|
| 4 | [04-langgraph-engine.md](04-langgraph-engine.md) | LangGraph 引擎:ThreadState、图、checkpointer、run 生命周期、流式 |
| 5 | [05-lead-agent.md](05-lead-agent.md) | Lead Agent 装配:ABI 入口、prompt 静态化、运行时 configurable |
| 6 | [06-middleware-pipeline.md](06-middleware-pipeline.md) | 中间件总纲:钩子机制、三段装配、35 链位职责地图 |
| 7 | [07-context-engineering.md](07-context-engineering.md) | 上下文工程:前缀缓存、三层内存治理、主线保持、信任分层 |

### 第三部分 · 子代理与记忆

| 章 | 文件 | 一句话主线 |
|----|------|-----------|
| 8 | [08-subagents.md](08-subagents.md) | Sub-Agent 总览与执行引擎:隔离 loop、身份拆分、委托台账 |
| 9 | [09-orchestration.md](09-orchestration.md) | 并发编排与容量治理:双层预算、FIFO、batch_task |
| 10 | [10-memory-architecture.md](10-memory-architecture.md) | 长期记忆架构:Fact 模型、可插拔后端、注入 |
| 11 | [11-memory-pipeline.md](11-memory-pipeline.md) | 记忆更新流水线:捕获→队列→提取→淘汰→注入回环 |

### 第四部分 · 执行与能力

| 章 | 文件 | 一句话主线 |
|----|------|-----------|
| 12 | [12-sandbox.md](12-sandbox.md) | Sandbox 抽象与五 Provider:隔离边界、租约、路径契约 |
| 13 | [13-tools.md](13-tools.md) | 工具系统:注册、内置工具族、执行链、可发现性 |
| 14 | [14-mcp.md](14-mcp.md) | MCP 集成:延迟工具路由、stdio 会话池、长任务 |
| 15 | [15-skills.md](15-skills.md) | Skills 体系:SKILL.md、激活、授权边界、实战 |

### 第五部分 · 模型、服务与扩展

| 章 | 文件 | 一句话主线 |
|----|------|-----------|
| 16 | [16-models.md](16-models.md) | 模型配置与适配:工厂、能力矩阵、provider 补丁 |
| 17 | [17-gateway.md](17-gateway.md) | Gateway API 与 IM 渠道:REST、SSE、trace、鉴权 |
| 18 | [18-config-extensions.md](18-config-extensions.md) | 配置体系与扩展:双层配置、热重载边界、五类扩展点 |

### 附录

| 附录 | 文件 | 内容 |
|------|------|------|
| A | [appendix-a-config.md](appendix-a-config.md) | config.yaml / extensions_config.json 逐节字段速查 |
| B | [appendix-b-reading-path.md](appendix-b-reading-path.md) | 按角色读、按目标查的阅读路径 |
| C | [appendix-c-glossary.md](appendix-c-glossary.md) | 术语表(120+ 条,带章节出处) |

## 建议阅读方式

- **想先懂全貌**:1 → 2 → 4 → 5 → 6 → 附录 B
- **搞中间件**:第 6 章总纲 + `../middleware/` 8 篇深文交替读
- **搞二次开发(写 Skill/工具/扩展)**:15 → 13 → 14 → 18
- **按需查询**:附录 B §3 的「想搞懂 X → 读哪里」索引表
