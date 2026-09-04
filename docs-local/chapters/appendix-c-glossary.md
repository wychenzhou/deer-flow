# 附录 C　术语表

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写

本表收录全书核心术语,按主题分为 13 组、共 90+ 条。每条给出**一句话定义**与**出现章节/源码位置**,方便按需回查。

**阅读约定**
- "第 N 章"指向 `docs-local/chapters/0N-*.md` 对应章;小节号为该章自身标题编号。
- "middleware-0X"指向 `docs-local/middleware/` 下的中间件深度解析文档;middleware-07 等按其标题首行所述链位区间(23–26)理解。
- "链位"是第 6 章定义的 35 链位地图中的装配位号,注意**链位号 ≠ 物理注册序**(见 §C.3)。
- 源码路径均相对仓库根,形如 `backend/packages/harness/deerflow/...`(import 名 `deerflow.*`)。

## C.1　架构与分层

- **DeerFlow** —— 一个 LangGraph-based 的 AI Super Agent 系统:monorepo 由 backend(harness + App)、frontend、docker、skills、contracts 组成,以"沙箱执行 + 持久记忆 + 子代理委派 + 可扩展工具(Skills/MCP/Python 扩展)"五件套加 Gateway 多平台接入为能力面。
  出处:第 1 章 §1.1、§1.4 | 仓库根 `AGENTS.md`

- **Harness(deerflow-harness 包)** —— 位于 `backend/packages/harness/` 的 agent 框架层,import 名 `deerflow.*`,实现运行模型、中间件管道、沙箱、记忆、子代理、扩展系统等全部"引擎"能力,不依赖任何 App 层代码。
  出处:第 2 章 §2.3、§2.4 | `backend/packages/harness/`

- **App(backend/app 层)** —— import 名 `app.*` 的应用外壳:FastAPI Gateway(`app/gateway/`)、IM Channels(`app/channels/`)与后台服务,负责 HTTP 面、渠道接入与运维装配。
  出处:第 2 章 §2.5 | `backend/app/`

- **harness/app 依赖纪律** —— 全书最重要架构红线:**只允许 App → Harness**;`backend/tests/test_harness_boundary.py` 用 AST 扫描 harness 下所有 `*.py`,发现 `from app.` / `import app.` 即让 CI 失败。
  出处:第 1 章 §1.4.4、第 2 章 §2.3、第 18 章 §9

- **deerflow-extension-api(契约包)** —— `backend/packages/extension-api/` 下、import 名 `deerflow_extension_api.*` 的公共扩展契约包;宿主对其版本**精确 pin**,扩展声明兼容区间,防止新扩展在宿主不支持时被误判可用。
  出处:第 2 章 §2.6、第 18 章 §6、§9

- **四服务拓扑 / Service Topology** —— `make dev`/Docker 拉起 Nginx(2026,唯一公网入口)、Gateway API(8001)、Frontend(3000)与可选的 Provisioner(8002,仅沙箱配 provisioner/K8s 模式时)四个服务。
  出处:第 1 章 §1.4.1、第 2 章 §2.8、第 3 章 §3.1

- **monorepo / 仓库地图** —— backend、frontend、docker、skills、contracts、scripts、docs 的分层布局;配置模板 `config.example.yaml` 与 `extensions_config.example.json` 位于仓库根,真实文件均被 gitignore。
  出处:第 1 章 §1.4.3、第 2 章 §2.2

- **版本 lockstep** —— 宿主(harness)与契约包(extension-api)的版本必须同步演进;宿主 pin 死自己实现的契约版本,扩展以 range 声明,避免 pip 解析出"宿主并未实现"的新契约。
  出处:第 2 章 §2.1、第 18 章 §9

## C.2　运行模型:LangGraph 引擎、线程与 Run

- **AgentState** —— 图中状态基类,规定 `messages` 通道的消息形态规约,是 ThreadState 的祖先。
  出处:第 4 章 §4.1.1

- **ThreadState** —— 运行期线程状态类(继承 AgentState),扩展了记忆/委派/提升等自定义字段,并为多个字段声明自定义 reducer;是"一次对话线程"在状态层面的全部可见数据。
  出处:第 4 章 §4.1.2、第 5 章 §5.4 | `backend/packages/harness/deerflow/agents/thread_state.py`

- **reducer(自定义 reducer)** —— 挂在状态字段上的合并函数,定义同名字段多节点写入时如何归并(如委派记账的按 run_id 去重追加);LangGraph 每次把新值"reduce"进旧值。
  出处:第 4 章 §4.1.2、第 5 章 §5.4.2

- **delta 模式(DeltaThreadState / DeltaChannel)** —— 消息通道只保留增量(delta)的线程模式:payload 更小、免去全量历史合并,但代价是**不能 fork**(worker 必须线性化续跑)。
  出处:第 4 章 §4.1.3、§4.4.3

- **Checkpointer** —— LangGraph 的线程检查点组件,负责把每步状态落盘/落库;DeerFlow 借此实现 thread 续跑与恢复,是"有状态 run"的地基。
  出处:第 4 章 §4.4 | 对照表见 `docs-local/checkpoint-tables.md`

- **CheckpointStateAccessor** —— 运行时提供的"线程态唯一访问咽喉":所有对 checkpointer 状态的读写都必须经它,防止中间件绕过纪律直改状态。
  出处:第 4 章 §4.4.2

- **langgraph.json** —— 向 LangGraph Server 注册可部署 agent 的清单文件,声明 entry/图的装配入口与模型配置。
  出处:第 4 章 §4.3、第 5 章 §5.1.1

- **create_agent(抽象图构建)** —— 内部图的构建入口:DeerFlow 用抽象声明组装图而非手写节点/边;真实执行由 LangGraph 引擎驱动,开发者不直接碰图拓扑。
  出处:第 4 章 §4.2.1

- **assembly descriptor(装配描述符)** —— 一次装配的"可观测身份":记录模型、中间件、链位等装配事实的结构化描述,便于调试与可观测,而非仅靠源码反推。
  出处:第 4 章 §4.2.2、第 5 章 §5.8 | `backend/packages/harness/deerflow/agents/assembly_descriptor.py`

- **run_agent** —— 两条运行入口(Gateway / DeerFlowClient)最终汇入的 worker 主循环:负责逐 turn 驱动模型与中间件、维护运行时上下文、处理 stop_reason 与收尾。
  出处:第 4 章 §4.5.3 | `backend/packages/harness/deerflow/runtime/runs/worker.py`

- **RunManager** —— Run 生命周期管家:记账、准入、幂等与并发治理;每次 run 的创建、放行与记账都先经它,是"每线程每 run 一次"语义的强制点。
  出处:第 4 章 §4.5.2、第 17 章 §5 | `backend/packages/harness/deerflow/runtime/runs/manager.py`

- **StreamBridge** —— 流式桥:把内部(异步)事件流接到对外通道(LangGraph 兼容 / SSE / 客户端回调),负责流控与背压。
  出处:第 4 章 §4.6、第 17 章 §5 | `backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py`

- **stream_mode / SSE** —— 对外流式协议:内部把多种 stream_mode 归一化,再按 SSE 帧格式经 Nginx 推给前端;Nginx 刻意不压缩 SSE 以保实时性。
  出处:第 4 章 §4.6、第 2 章 §2.8

- **双路径(客户端直连 vs Gateway)** —— 同一份嵌入运行时可被两条路径驱动:进程内 `DeerFlowClient` 直连,或经 Gateway 的 LangGraph 兼容 HTTP 接口;二者在 §4.7 被对比,差异落在多任务策略与审计上。
  出处:第 4 章 §4.7、第 17 章 §9

- **运行时 configurable(运行选项)** —— 每次 run 可覆盖的运行时参数集(模型、流模式、超时等),经 ThreadState 的 configurable 通道注入,是"per-run 可热改"边界的一部分。
  出处:第 5 章 §5.5、第 18 章 §3

## C.3　中间件体系与钩子

- **AgentMiddleware / 中间件** —— 扩展与横切逻辑的基本单元:继承中间件基类、实现若干钩子,由装配器按 35 链位地图排进管道;中间件是 DeerFlow 最大的定制面。
  出处:第 6 章 §1、§7 | `backend/packages/harness/deerflow/agents/middlewares/`

- **状态钩子 vs 包裹钩子** —— 钩子两大家族:状态钩子(read/write 型,读写图状态如 before_agent、after_model)与包裹钩子(wrap_* 型,包住模型调用/工具调用、返回迭代器流);先分清拦的是什么再谈顺序。
  出处:第 6 章 §1.1

- **35 链位地图** —— 全书唯一需要对照的装配总表:35 个链位覆盖共享基座、lead-only 段与扩展并入段;链位号是"职责槽位",与物理注册序存在系统性偏差。
  出处:第 6 章 §2.3、§3、§4

- **wrap_model_call** —— 包裹模型调用的钩子:在模型被真正调用时包一层生成器,可注入/改写/截断/统计模型输出流;技能激活、token 预算等中间件都依赖它。
  出处:第 6 章 §1.2、middleware-05/06 | `skill_activation_middleware.py`、`token_budget_middleware.py`

- **wrap_tool_call** —— 包裹每次工具调用的钩子,负责三件事:决定调用**是否持久**(结果进不进状态)、能否**短路**(不真执行直接给结果)、以及返回 **Command**(截断当前执行流)。
  出处:第 6 章 §1.2、第 13 章 §4

- **after_model(反向分派)** —— 模型输出后的状态钩子;**最晚注册的最先看见模型输出**,与直觉相反;委派截断、记忆排队等下游逻辑靠这个次序保证"先看见、先决策"。
  出处:第 6 章 §1.3

- **before_agent** —— 每 run 恰好触发一次的状态钩子,只应做"初始化即应持久"的事(如建线程目录),不做逐 turn 的事。
  出处:第 6 章 §1.4

- **deerflow_tool_meta** —— 工具执行结果的状态元数据键:由 ToolErrorHandlingMiddleware 在执行后"盖章"(status 为 success / error / partial_success 等),后续 ToolReceipt、ToolProgress 等中间件读取它生成收据与进度,是工具链上中间件间通信的约定字段。
  出处:第 6 章 §1.2、middleware-03 | `tool_error_handling_middleware.py`、`tool_receipt.py`

- **ToolErrorHandlingMiddleware(盖章/错误处理)** —— 工具调用出错时统一兜底:把错误规整进 deerflow_tool_meta、决定重试还是短路,并处理"阻塞中的工具槽位"等边界。
  出处:middleware-03、第 6 章 §3

- **ToolReceiptMiddleware** —— 每次工具调用后生成结构化"收据"(status 取自 deerflow_tool_meta),保证模型看到的是规整的执行回执而非裸错误。
  出处:middleware-01、第 13 章 §4 | `agents/middlewares/tool_receipt.py`

- **ToolProgressMiddleware** —— 长工具调用的进度可见性:记录/上报工具当前进度,并承担停滞守卫;与 ReadBeforeWrite 组成"文件安全 + 停滞"防线。
  出处:middleware-04 | `agents/middlewares/tool_progress_middleware.py`

- **ReadBeforeWriteMiddleware** —— 文件写安全防线:写文件前要求先读(或显式覆盖确认),防止"没看过就覆盖"的破坏性写。
  出处:middleware-04 | `agents/middlewares/read_before_write_middleware.py`

- **链位 30/31 与收尾 32–35** —— 30/31 是扩展中间件的唯一合法并入点;32–35 是收尾段(含链 35 Clarification 人机核对点),对扩展"不可改写"——保证信任边界与终止性不被后插的代码绕过。
  出处:第 6 章 §2.4、§6

## C.4　上下文工程与信任分层

- **前缀缓存(prefix caching)** —— 让模型服务端缓存"相同前缀"的 KV 计算以省时省钱;DeerFlow 的系统提示词被**静态化**、动态内容一律**后置注入**,就是为了最大化前缀复用。
  出处:第 5 章 §5.6.3、第 7 章 §2、第 10 章 §8

- **静态 system prompt** —— 完全静态、不夹记忆与日期的主提示词;动态内容由 DynamicContextMiddleware 以 `<system-reminder>` 形式注入第一条 HumanMessage,使主提示词跨用户跨会话逐字节一致。
  出处:第 5 章 §5.6、第 7 章 §2.1 | `dynamic_context_middleware.py`

- **动态后置注入(注入点纪律)** —— 一切动态内容(记忆、日期、图片、待办)只允许出现在用户消息之后的注入点,不许改写前缀区;违反即"打散前缀"反模式。
  出处:第 7 章 §2.2、§2.5

- **SystemMessageCoalescing(链 26)** —— 前缀"最后一公里整形":把多条 system 消息合并/归并,避免注入过程把前缀弄脏;发生在链 26。
  出处:第 7 章 §2.4、middleware-07 | `system_message_coalescing_middleware.py`

- **ViewImage(链 23)** —— 视觉内容注入中间件:把图片以最重负载方式注入,同时寿命最短——用完即从上下文清出,避免图片 token 拖垮后续轮次。
  出处:第 7 章 §2.3、middleware-07 | `view_image_middleware.py`

- **三层内存治理(注入 / 外化 / 压缩)** —— 上下文不是仓库而是预算:记忆动态注入(注入层)、文件系统作第二级内存(外化层)、摘要压缩(压缩层)三层配合,是第 7 章主线的操作模型。
  出处:第 7 章 §3

- **主线保持(压缩保护)** —— 压缩时精确保住三样东西:最新真实用户请求、按 tag(不按内容)保护的记忆提醒、以及 durable 投影;防止压缩把任务主线"移走"。
  出处:第 7 章 §4

- **durable 投影(durable projection)** —— 压缩删不掉的东西:在上下文中保留不可压缩的权威内容(如契约、待办投影),以对抗摘要的信息损失。
  出处:第 7 章 §4.4、middleware-05(DurableContext)

- **Summarization(链 18)** —— 压缩层主体:总结旧消息、按摘要协议生成"摘要即一级公民"的产物,并触发 pre-summarization flush 抢救记忆。
  出处:第 7 章 §3.4、第 11 章 §6、middleware-06

- **信任分层 / 通道即信任声明** —— system 通道、user 通道、工具结果承载不同信任等级;只有受信来源能进 system 通道,untrusted 内容的边界框画在更外层。
  出处:第 7 章 §5.1

- **权威契约(防指令前缀)** —— 给注入的数据块加"权威来源标签 + 定界 + 转义"的包裹协议(含 HTML 转义防御 XML 块逃逸),防止数据内容冒充系统指令。
  出处:第 7 章 §5.2、§5.3

- **provenance(溯源/留痕)** —— 双义术语:上下文侧,每条注入内容留下"谁在何时注入"的 provenance 痕迹(第 7 章 §5.4);扩展侧,ExtensionRegistry 以 `attributed_to(source)` 记录每个扩展贡献的来源字符串,供诊断与溯源。
  出处:第 7 章 §5.4、第 18 章 §6 | `extensions/registry.py`

- **ClarificationMiddleware(链 35)** —— 人机核对点:在 run 收尾前,对歧义/高风险请求向用户发起澄清而非直接执行;位于链 35,属于不可被扩展改写的收尾段。
  出处:第 6 章 §6、第 7 章 §6 | `clarification_middleware.py`

## C.5　子代理:委派、执行与编排

- **task 工具** —— 委派入口:lead agent 调用它把一段任务交给子代理执行;带前置校验、验收标准与身份拆分,是"委派是优化,不是默认"哲学的落地。
  出处:第 8 章 §8.2 | `backend/packages/harness/deerflow/tools/builtins/task_tool.py`

- **acceptance check / acceptance_criteria(验收检查)** —— 委派时附带的客观可验收标准;子代理完成后由确定性代码(而非自述)检查可判定的部分(如 `file:<path> exists`),超界项降级为 UNVERIFIED 而非误判通过。
  出处:第 8 章 §8.2、第 9 章 §1.4 | `subagents/acceptance_checks.py`

- **SubagentExecutor** —— 子代理执行引擎主体:`_aexecute` 状态机管理从准入到终态的每一步,负责步骤捕获、事件持久化、usage 报告与清理。
  出处:第 8 章 §8.4 | `backend/packages/harness/deerflow/subagents/executor.py`

- **隔离事件循环(isolated event loop)** —— 子代理跑在独立的事件循环上,避免子代理的取消/异常污染主循环;跨 loop 调用经 ContextVar 拷贝与"反向穿越清理"桥接。
  出处:第 8 章 §8.3

- **ContextVar 拷贝** —— 子代理进入隔离 loop 时,父上下文的 ContextVar 被显式拷贝继承,保证 trace_id、身份等隐式上下文不丢;但清理任务必须钉在持久 loop 上执行。
  出处:第 8 章 §8.3.2、§8.3.4

- **SubagentExecutionCapacity(FIFO 准入)** —— 进程级执行容量控制器:running 计数 + FIFO waiters;排队不占线程,准入失败发生在模型执行**之前**。
  出处:第 9 章 §3 | `backend/packages/harness/deerflow/subagents/capacity.py`

- **双层预算(per-response 并发 × per-run 总量)** —— 第一层限制单次响应内并发子代理数,第二层限制一次 run 累计委派总数;后者由 after_model 的确定性截断强制,防"并发限制拦不住长 run"。
  出处:第 9 章 §1

- **stop_reason** —— run/子代理的终止原因标记(存于运行时上下文):loop/token/safety/model_length/subagent_limit_capped 等共用同一通道,worker 收尾时把它写入 run 记录供审计与重试决策。
  出处:第 8 章 §8.4.5、第 9 章 §7 | `runtime/runs/worker.py`

- **subagent_limit_capped** —— 当子代理总委托量撞上 per-run 预算时,由截断中间件盖上的 stop_reason;被截断的剩余 task 调用不会执行。
  出处:第 9 章 §7、middleware-06

- **durable delegation ledger(持久委派账本)** —— ThreadState 中的持久通道 + reducer,按 **run_id 记账**:每次委托被盖在当前 run 头上,later user turn 重新计费;计费单位是 run 不是 thread,即使线程跨多轮也防超发。
  出处:第 9 章 §2

- **身份拆分(provider tool_call_id → external_task_id,server uuid → task_id)** —— 一次委派同时持有多重身份:模型侧 tool_call_id、对外 external_task_id、服务侧 uuid→task_id;拆分使取消、追踪与记账互不混淆。
  出处:第 8 章 §8.6

- **SubagentResult** —— 可跨线程竞争的一次性终态对象:多个等待方(轮询方、清理方、取消方)对它只能消费一次,防双重收尾。
  出处:第 8 章 §8.5.3

- **execute_async / PENDING 注册表** —— 把子代理提交为后台任务:注册 → 提交 → 遗忘,状态先置 PENDING,后台执行器接手;注册表提供取消语义。
  出处:第 8 章 §8.5.1、§8.5.2 | `subagents/registry.py`

- **batch_task** —— 持久批量委派模式:与单次 task 的三点边界(可恢复、按行模型、三重维度),行级 lease 领取与恢复保证断点续跑。
  出处:第 9 章 §5 | `subagents/batch_runtime.py`、`batch_service.py`

## C.6　长期记忆

- **Fact** —— 跨会话记忆的基本单位:一条"值得保留的用户事实",以 markdown 落盘(如 `facts/ab/fact_uv.md`),由确定性闸门把关入库。
  出处:第 10 章 §4、第 11 章 §5

- **MemoryManager** —— 存储后端抽象的三层契约(读写/召回/生命周期),支持可插拔后端发现;默认本地后端为 DeerMem。
  出处:第 10 章 §3 | `agents/memory/manager.py`

- **DeerMem(默认后端)** —— 本地文件型记忆后端:事实按用户/agent 分目录存储、以 hash 前缀分片,管理并发与淘汰。
  出处:第 10 章 §4 | `agents/memory/backends/deermem/deer_mem.py`

- **确定性闸门(scope=user + durable + descriptive)** —— 提取结果入库前的硬门槛:作用域为 user、且 durable 且 descriptive 的事实才落盘;LLM 只管提,入库与否由确定性代码把关。
  出处:第 11 章 §5

- **MemoryMiddleware(链 22)** —— 薄捕获器:每轮对话后把符合条件的消息丢进记忆更新队列(排在 TitleMiddleware 之后),不自己碰 LLM。
  出处:第 11 章 §1 | `agents/middlewares/memory_middleware.py`

- **pre-summarization flush** —— 压缩发生前把仍值得留的消息"抢救"进记忆队列,避免消息在摘要中丢失。
  出处:第 11 章 §6

- **淘汰(eviction)** —— 三套互补的确定性淘汰机制(容量/时效/重要性等),全部由代码把关而非 LLM 裁量。
  出处:第 10 章 §6

- **冻结快照(frozen snapshot)** —— 动态注入的记忆在注入瞬间被冻结成快照,整轮不变——前缀缓存友好是"设计出来"的,不是碰巧。
  出处:第 10 章 §8、第 11 章 §7

## C.7　Sandbox 与代码执行

- **Sandbox(统一接口)** —— 代码执行的隔离边界抽象:一次定义接口、多种 Provider 实现,隔离是"插拔策略"而非写死的实现。
  出处:第 12 章 §2 | `backend/packages/harness/deerflow/sandbox/sandbox.py`

- **SandboxProvider** —— Provider 抽象与工厂:本地子进程、Docker、远程沙箱服务等实现都实现同一契约;沙箱模式的三种选择在配置层完成。
  出处:第 12 章 §3、第 3 章 §3.7 | `sandbox/sandbox_provider.py`

- **SandboxMiddleware** —— 管沙箱生命周期的中间件:谁(哪个 run)拿沙箱、何时归还,绑定线程目录与 lease 语义。
  出处:第 12 章 §4 | `sandbox/middleware.py`

- **sandbox lease(SandboxClientLease)** —— 执行作用域的沙箱租约:进程内沙箱客户端按租约发放,**最后一个 execution lease 才能调用 provider.release**;上传等场景用 `release_on_last=False` 显式延长。
  出处:第 12 章 §4、middleware-02 | `sandbox/lease.py`

- **密钥剥离(secret stripping)** —— 沙箱镜像/运行时建立时剥离宿主的密钥环境,宿主的秘密不进沙箱;工具结果回传侧另有脱敏防线。
  出处:第 12 章 §7 | `sandbox/env_policy.py`

- **路径契约(虚拟前缀)** —— 线程工作区以虚拟前缀(如 `/mnt/user-data/...`)统一寻址:本地实现映射到线程目录,远程实现经 token 落地;写路径先过 `ReadBeforeWrite` 语义。
  出处:第 12 章 §6、middleware-02 | `sandbox/path_patterns.py`

- **隔离令牌与身份** —— 远程资源的确定性寻址:沙箱内资源经隔离令牌标识,保证"同一线程的资源可被确定性找回",是跨 Provider 一致性的关键。
  出处:第 12 章 §5 | `sandbox/identity.py`

## C.8　工具系统

- **工具注册(每次装配的合成)** —— 没有全局 registry:每次 agent 装配时把内置工具、MCP 工具、Skills 工具、扩展工具**合成**成该次运行的工具集;name 是唯一身份。
  出处:第 13 章 §2、§5

- **工具执行链(tool_calls → ToolMessage)** —— 一次工具调用的生命周期:模型产出 tool_calls → 经 wrap_tool_call 链 → 执行/短路 → 盖章 deerflow_tool_meta → 生成 ToolMessage 回填状态。
  出处:第 13 章 §4、第 6 章 §1.2

- **可发现性(discoverability)** —— 模型"怎么知道有这个东西、长什么样":工具说明(description/schema)如何进上下文、何时隐藏(与延迟工具路由联动),是第 13 章 §6 的主题。
  出处:第 13 章 §6、第 14 章 §5

- **内置工具族** —— `tools/builtins/` 下随 harness 分发的工具(task、batch_task、tool_search 等),与 MCP/Skills 工具同池装配、同链执行。
  出处:第 13 章 §3 | `tools/builtins/`

## C.9　MCP 与延迟工具路由

- **MCP(Model Context Protocol)** —— Anthropic 推出的工具/上下文开放协议;DeerFlow 经 extensions_config.json 的 `mcpServers` 声明外部 MCP server,并把其工具并入本池。
  出处:第 14 章 §1、§2、第 3 章 §3.4.4

- **stdio 会话池** —— 对有状态 MCP server 的 stdio 连接池:复用长活会话是"有状态 server 的命脉",避免每次工具调用重开进程。
  出处:第 14 章 §4

- **工具名前缀与来源路由** —— 按来源给工具命名/打标并路由回对应 server,保证"哪个 server 的工具"可辨识、可隔离。
  出处:第 14 章 §3

- **延迟工具路由(deferred tools)** —— 本章核心:大量 MCP 工具的 schema **不预先**绑给模型,而是延迟到模型显式查询/被关键词命中时才提升(promote),从而省 token、防 schema 过载。
  出处:第 14 章 §5

- **DeferredToolCatalog** —— 延迟工具目录:持有候选工具集,提供 names/hash/search;`catalog_hash` 是该目录内容的指纹。
  出处:第 14 章 §5 | `tools/builtins/tool_search.py`

- **catalog_hash** —— 延迟目录的内容指纹,写进提升记录(promoted)作**防漂移 scope**:若目录已变(哈希不匹配),旧提升记录整体失效,防止把已不存在的工具 schema 误暴露。
  出处:第 14 章 §5、第 6 章 §5 | `tool_search.py`、`deferred_tool_filter_middleware.py`

- **promoted(提升记录)** —— 线程状态里"本轮已提升哪些工具"的记录:`{"catalog_hash": …, "names": […]}`;哪些 schema 对模型可见完全由它决定。
  出处:第 14 章 §5

- **tool_search** —— 延迟路由的查询工具:模型不知道有什么工具时调用它按关键词检索目录,命中即提升并把工具绑给下一轮模型调用。
  出处:第 14 章 §5、第 13 章 §6 | `tools/builtins/tool_search.py`

- **McpRoutingMiddleware(链 24)** —— 预判式提升:按用户话语关键词把最相关的 top_k 个工具**自动提升**(promote)。
  出处:第 14 章 §5、middleware-07 | `agents/middlewares/mcp_routing_middleware.py`

- **DeferredToolFilterMiddleware(链 25)** —— schema 隐藏的执行者:在所有提升发生**之前**,把未提升工具的 schema 从模型绑定里藏掉,保证模型只看见"已提升 ∪ 常驻"工具。
  出处:第 14 章 §5、middleware-07 | `agents/middlewares/deferred_tool_filter_middleware.py`

- **两级提升(两级 promote)** —— 提升分两路:链 24 关键词自动提升(top_k)+ tool_search 显式查询提升;二者共用同一份 promoted 状态与 catalog_hash 校验。
  出处:第 14 章 §5

- **mcp_tasks(长任务运行时)** —— 对耗时长跑的 MCP 任务提供持久化运行时(任务注册、进度、结果回收),避免一次请求的生命周期杀死长任务。
  出处:第 14 章 §7

## C.10　Skills 体系

- **Skill** —— DeerFlow 的扩展单元:一个带 `SKILL.md` 的目录(说明、frontmatter、可引用资源),表示"模型可渐进加载的技能包"。
  出处:第 15 章 §1、§2

- **SKILL.md** —— 技能描述文件:YAML frontmatter(name、description、触发条件等)+ Markdown 正文;description 前段常被约定为自含的触发句。
  出处:第 15 章 §1

- **渐进加载与 discovery** —— 技能默认不全部进上下文:按名字/描述被发现(discovery),需要时才加载全文;"技能什么时候被看见"由发现机制决定。
  出处:第 15 章 §3

- **激活(显式斜杠 + 自主加载)** —— 技能经两条路激活:用户显式斜杠命令(链 15 SkillActivationMiddleware)或模型按任务自主加载;激活后才把工具/内容绑给模型。
  出处:第 15 章 §4 | `agents/middlewares/skill_activation_middleware.py`

- **SkillToolPolicy(链 16)** —— 授权边界:控制"已激活技能的工具有没有权限跑、能跑什么",是 Skills 与工具执行之间的闸门。
  出处:第 15 章 §5 | `agents/middlewares/skill_tool_policy_middleware.py`

- **SkillScan(安全静态扫描)** —— 技能安装/加载前的静态安全扫描(frontmatter 容错、危险内容检测),配合安装通道管理把住"技能即代码"的安全口。
  出处:第 15 章 §6 | `skills/security_static_scanner.py`

- **目录模型(public / custom / 集成包)** —— 技能分仓:仓库内 `skills/public/`(提交)、`skills/custom/`(gitignore)与全局集成技能包;信任边界随目录不同而不同。
  出处:第 15 章 §2、第 2 章 §2.10

## C.11　模型配置与适配

- **models: 配置段** —— config.yaml 中以 provider 方言组织的模型清单(一家多家方言);运行时按名字解析到具体 provider 配置。
  出处:第 16 章 §16.1、§16.2

- **create_chat_model(工厂)** —— 把模型配置变成可调用 chat model 的 12 步构造管线:方言解析、能力对齐、适配补丁、超时与流式选项,全部收敛于此。
  出处:第 16 章 §16.3、第 5 章 §5.7

- **能力矩阵(thinking / vision / reasoning_effort …)** —— 每个模型的声明能力(思考模式、视觉、reasoning_effort、context_window、流式用量、超时);框架按矩阵决定"能干什么、怎么调用"。
  出处:第 16 章 §16.4

- **provider 适配补丁** —— 对各家方言(OpenAI/Anthropic/DeepSeek 等)不一致处的修补层,保证上游语义(如流式用法统计)在各 provider 上对齐。
  出处:第 16 章 §16.5

- **model:use 与 fallback 扫描** —— 模型授权:配置中声明哪些角色(如 main/vision/thinking)可用哪些模型;主模型失败时按授权范围扫描 fallback。
  出处:第 16 章 §16.7

## C.12　Gateway、客户端与 IM 渠道

- **Gateway** —— FastAPI 应用壳(Gateway API,端口 8001):装配中间件栈、lifespan 与 routers,对外提供 threads/runs/models/console 等 REST 路由,并内嵌一份 LangGraph 兼容运行时。
  出处:第 17 章 §1–§3、第 2 章 §2.5 | `backend/app/gateway/`

- **Nginx 唯一入口** —— 容器栈的单一公网面(2026):代理 `/api/langgraph/*` 到内嵌 LangGraph 运行时(重写为原生 `/api/*`),其余 `/api/*` 直通 Gateway;压缩 HTML 而刻意不压 SSE。
  出处:第 2 章 §2.8、第 17 章 §1

- **内嵌 LangGraph 兼容运行时** —— Gateway 内直接嵌入的运行时面:以 RunManager + StreamBridge 为核心,使外部 LangGraph 生态客户端能按 LangGraph 语义驱动 DeerFlow。
  出处:第 17 章 §5、第 4 章 §4.8

- **无状态/有状态双 run 入口** —— 无状态(单次请求,不续线程)与有状态(thread 续跑)两类 run 入口,配合不同的多任务策略;是 LangGraph 兼容面与原生 REST 的差异点。
  出处:第 17 章 §6

- **RunEventStore** —— 事件与审计存储:run 生命周期事件(含 stop_reason、用量、委派)被持久化,支撑回放与审计。
  出处:第 17 章 §7

- **X-Trace-Id** —— 贯穿所有入口的追踪 ID:一次请求从 REST/IM/LangGraph 兼容面进入,凭同一 trace id 串起子代理、沙箱与记忆写链路。
  出处:第 17 章 §8

- **DeerFlowClient** —— harness 提供的进程内客户端(`deerflow/client.py`):不经 HTTP,直接在进程内驱动嵌入运行时,与 Gateway 路径共用 run_agent。
  出处:第 4 章 §4.7、第 17 章 §9 | `backend/packages/harness/deerflow/client.py`

- **IM Channels** —— 飞书、Slack、Telegram、Discord、钉钉等外部 IM 的传输适配层:把渠道消息归一化成同一份 agent 服务输入,回投结果走渠道能力。
  出处:第 17 章 §10、第 2 章 §2.5 | `backend/app/channels/`

## C.13　配置体系与扩展机制

- **双层配置** —— `config.yaml`(信任边界内的主配置,含 `plugins:` 列表,operator 控制、代码导入面)+ `extensions_config.json`(MCP 与技能,"API 可写")两个文件、一条信任边界。
  出处:第 18 章 §1、第 3 章 §3.4

- **config.yaml 解析管线与 `$VAR` 递归展开** —— 配置值中的 `$VAR` 被递归展开为环境变量;缺失变量 → ValueError,进程拒绝启动,保证配置在启动期自证完整。
  出处:第 18 章 §2

- **热重载边界 / STARTUP_ONLY_FIELDS** —— 只有 per-run 字段能随请求热改;启动期字段被列为 STARTUP_ONLY_FIELDS,运行中改动不生效——边界由 `reload_boundary` 显式声明而非约定。
  出处:第 18 章 §3 | `backend/packages/harness/deerflow/config/reload_boundary.py`

- **extensions_config.json(内容模型)** —— MCP server 与技能的可写配置源:结构声明、来源路由与启用状态都在这里,经 Gateway API 可改;但顶层 `plugins:` 列表刻意留在 config.yaml 中由 operator 独控。
  出处:第 18 章 §4、第 14 章 §2

- **五类扩展贡献点** —— 打包扩展可贡献的五类东西:中间件、task lifecycle、system-model observer、Gateway services、FastAPI HTTP routers;参考实现见 `examples/deerflow-extension-example/`。
  出处:第 18 章 §6、第 2 章 §2.6

- **resolve_variable / resolve_class(反射)** —— 配置与扩展装配期的反射原语:按名字解析出变量或类,供中间件声明式注入与扩展顺序的确定性展开。
  出处:第 18 章 §8

- **operator CLI(deerflow extensions …)** —— 运维命令面:`deerflow extensions install/list/enable/disable/remove`(或 `make extension-*`);每次变更都要求 Gateway 重启,因为扩展代码以 Gateway 权限执行,只信任 operator 来源。
  出处:第 18 章 §10、第 2 章 §2.5

- **插件中间件装配(配置声明 vs 插件贡献)** —— 中间件的两条扩展路径:配置声明式(改配置即插入)与插件贡献式(扩展在装配期并入);前者轻、后者重,并入点都锁定在链位 30/31。
  出处:第 18 章 §5、第 6 章 §2.4
