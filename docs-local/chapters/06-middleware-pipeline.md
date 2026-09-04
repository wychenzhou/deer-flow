# 06 · 中间件管道总纲:35 链位地图与装配机制

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写
> 配套深文:`../middleware/middleware-01-io-safety.md` ~ `middleware-08-safety-guards.md`(8 篇,逐中间件展开)

本章是「中间件」主题的**总纲章**:只回答三件事——中间件机制是什么(§1)、
35 个链位怎么装出来的(§2)、以及一张可直接查的**职责地图**(§4)。至于每个
中间件内部的伪代码、状态机、决策细节,**一律交给 8 篇深文**,本章不重复深挖;
§3 给出"如何按需读那 8 篇"的路线。

先给结论,一句话版本:

> 一个"DeerFlow 模型调用"是**一次洋葱式嵌套**:35 个链位(语义槽位)分成
> 共享基座(1–13)+ lead-only 追加段(14–35)两段装配,可选位按配置决定装不装,
> 扩展在最后一步并入并经受顺序不变量校验。链位号是**语义契约**,物理装配序
> 与它的唯一偏差是 #13 的"夹心"结构——见 §2.3。

---

## 1. 机制先行:中间件到底长在哪

DeerFlow 的 agent 本体是 LangGraph 图,每个中间件是
`langchain.agents.middleware.AgentMiddleware[AgentState]` 的子类(仓库全部 30+
中间件无一例外)。"中间件"不是一个插件概念,而是**在 agent 循环的固定缝隙上
打孔**的一组钩子。钩子有两类、两组方法、每个方法都有同步/异步两版(异步加 `a`
前缀:`abefore_model`、`awrap_tool_call`…)。

```
before_agent                          ← 每 run 一次(图入口节点)
  └→ before_model                     ← 每轮模型调用前(独立图节点)
       └→ [wrap_model_call 洋葱]      ← 包裹"这一次"模型请求
            └→ model(provider)
       └→ after_model                 ← 每轮模型调用后(独立图节点,逆装配序!)
            ├→ 模型想调工具? → [wrap_tool_call 洋葱] → ToolMessage 落 state
            └→ 该收手?      → 跳工具节点 / 跳回 model / 跳出到 after_agent
  └→ after_agent                      ← 每 run 一次(图出口节点)
```

(middleware-01 §0 把这张图讲得最透,含"选钩子的两把尺子",是全系列的机制总起,
卡住时先回去读它。)

### 1.1 状态钩子 vs 包裹钩子:先分清楚你拦的是什么

| 类别 | 签名 | 语义 | 落不落 checkpoint |
|---|---|---|---|
| 状态钩子 | `before_model(state, runtime) -> dict \| None` 等 | 编译成**独立图节点**,可读写 state 全貌、返回的 dict 按 channel 并入 | **落**——持久、跨步骤存活、被后续节点可见 |
| 包裹钩子 | `wrap_model_call(request, handler) -> ModelResponse` | **嵌套在模型/工具节点内部**的调用链,`handler` 由你决定何时调、调几次、带什么载荷去 | 取决于你改的对象(见下) |

**包裹钩子的两条铁律:**

1. **装配顺序 = 外层→内层,先装配者最外**。最外层先看到原始请求,它的改写是
   内层(含 LLMErrorHandling 的重试)共享的"干净视图"。这就是为什么
   `InputSanitizationMiddleware` 必须是全链第 1 位——净化之后的每条消息,
   之后的每个中间件(包括可能重试多次的 LLM 错误处理)看到的都是干净版本。
2. **改请求侧 = 只活这一次模型调用;改响应/结果侧 = 持久事件**。

`wrap_model_call` 的请求侧(`request.override(messages=...)`)改的是**发给模型
的那一份载荷,永不写回图状态**——base64 图片、SKILL.md 正文、当轮净化视图这些
"想临时给模型看、绝不想留在历史/被重复发送"的东西,只有这道缝做得到
(ViewImage、SkillActivation、InputSanitization、DanglingToolCall、
SystemMessageCoalescing 都在这里)。而 `wrap_model_call` 返回的模型消息、
`wrap_tool_call` 返回的 `ToolMessage`,与正常产出一样经 `add_messages` 落进
state——**持久**。差异的本质是"改造对象",不是中间件的意图。

### 1.2 wrap_tool_call:持久、短路、Command 三件事

`wrap_tool_call(request, handler)` 包裹**每一次工具执行**,是唯一能在"工具真的
跑起来之前"插一脚的地方。返回值类型 `ToolMessage | Command`,由此获得三种能力:

```python
def wrap_tool_call(self, request: ToolCallRequest, handler) -> ToolMessage | Command:
    # 1) 放行:调 handler 执行工具,再改写/打标结果(ToolReceipt/SandboxAudit)
    result = handler(request)                     # handler = 内层链 + 真正执行
    return self._stamp(result)                    # 改写结果 → 持久

    # 2) 短路:不调 handler,自造 ToolMessage 顶替(Guardrail 拒绝、
    #    ReadBeforeWrite 拦写、ToolErrorHandling 异常转错误消息)
    return ToolMessage(content="Error: ...", tool_call_id=..., status="error")

    # 3) Command:带图路由权的中断/更新(Clarification 用 Command(goto=END))
    return Command(goto=END)                      # 打断工具循环,整 run 收尾
```

注意 `wrap_tool_call` 虽然长在"通道"上,其返回值却作为工具节点输出照常落历史
——它同时是通道拦截器与持久写手;而且**一个中间件可以既装 wrap_tool_call 又装
wrap_model_call**(ToolReceipt 就是这样:结果侧打戳 + 模型侧渲染隐藏收据账本)。

### 1.3 after_model 反向分派:最晚注册的最先看见模型输出

`after_model`/`aafter_model` 被 LangChain 编译成图节点时**刻意从列表尾部倒着
连边**:最后注册的中间件最先看到模型输出,其 state 更新先落地,前面的中间件
看到的是"已被它改写过的 state"。

这是全书最容易踩的暗坑,也是收尾段(32–35)设计的全部依据。真实例子:lead 链
注册顺序是 TerminalResponse(32)→ModelLength(33)→Safety(34)→Clarification(35),
而**实际动作顺序恰好反过来**:Clarification 最先看到原始输出(剥掉与
ask_clarification 同批的 sibling),接着 Safety 检测 `content_filter` 之类的
安全终止并剥掉 tool_calls,**然后** LoopDetection/TokenBudget 才在"干净消息"上
记账,TerminalResponse 最后兜底判空。若把 Safety 注册到 27 之前,它看到的将是
尚未被剥 tool_calls 的失控响应——顺序就是正确性(§1.5)。

### 1.4 before_agent:每 run 一次,只做"初始化即应持久"的事

`before_agent` 在整个 agent 节点执行前跑一次(不是每次模型调用),典型用途:
ThreadData 建线程目录、Uploads 注入本轮上传清单、Sandbox 获取沙箱并把
`sandbox_id` 写进 state、DynamicContext 把日期提醒写进首条消息(因此持久化,
后续轮次无需重注)。对称地,`after_agent` 做 run 级收尾:Sandbox release、
Memory 把整轮消息排队给异步提取。

### 1.5 顺序即正确性:机制层面的三个推论

1. **改写先于读取**:ToolResultSanitization(3)必须坐在 ToolOutputBudget(2)
   **内层**——先中和原始输出,再由预算截断;"先净化后预算"而不是反过来。
2. **发布先于消费**:SkillActivation(15)必须紧贴 SkillToolPolicy(16)之前——
   15 在 wrap 里把 slash 激活源写进 run context,16 同一轮立刻读它决定工具裁剪;
   DurableContext(17)必须在 Summarization(18)之前——"委派账本在压缩前捕获";
   McpRouting(24)必须在 DeferredToolFilter(25)之前,代码里还有一条装配期断言
   `assert_mcp_routing_before_deferred_filter` 直接拦错。
3. **数据依赖由程序校验**:`deerflow/extensions/ordering.py` 把关键不变量声明为
   数据(ToolReceipt 必须在 Guardrail/SandboxAudit/ReadBeforeWrite/ToolProgress
   外层——否则短路结果永远拿不到收据、账本静默缺口;ToolProgress 必须在
   ToolErrorHandling 外层——它要读后者戳的 `deerflow_tool_meta`),在扩展并入
   之后的装配末尾**一次性校验**:违反即构建期 `RuntimeError` 并点名责任方,
   绝不运行期悄悄错序。

**消息溯源(provenance)**:凡注入/改写消息的中间件,统一用
`deerflow_extension_api.provenance` 的键(`deerflow_content_kind` /
`deerflow_producer_kind` / 可选 `deerflow_producer_entity_id`)经
`provenance_kwargs()` 打戳到 `additional_kwargs`;三个键都在
`_SERVER_OWNED_MESSAGE_METADATA_KEYS` 里,入站消息无法伪造。打到模型调用边界
时消息已与普通消息无异,"是谁产的"这个事实必须在生产现场记录。当前打戳方:
DynamicContext、DurableContext、SystemMessageCoalescing、ViewImage、
SkillActivation。工具结果改写链另有
`additional_kwargs["deerflow_tool_transforms"]` 轨迹(`tool_transform_meta.py::append_tool_transform`),
按应用顺序记录 raw→visible 的每次变换,供观察者按事实分类而非嗅探输出措辞。

---

## 2. 三段装配:链是怎么"装"出来的

35 个链位不是一张平铺的配置表,而是**三个函数、按严格顺序、跨两个文件**拼出来
的。装配代码只有三处,背下来就掌握了整个管道:

| 段 | 函数 | 文件位置 | 产出 |
|---|---|---|---|
| ① 共享基座 | `_build_runtime_middlewares` | `agents/middlewares/tool_error_handling_middleware.py`(L161) | 链位 1–13 的实例列表 |
| ①′ lead 入口 | `build_lead_runtime_middlewares` | 同文件(L318) | 基座 + lead 参数(含 Uploads、receipt 渲染模式) |
| ①″ subagent 入口 | `build_subagent_runtime_middlewares` | 同文件(L343) | 基座变体 + subagent 追加段 |
| ② lead-only 追加 | `build_middlewares` | `agents/lead_agent/agent.py`(L457) | 在基座上继续 append 链位 14–35 |
| ③ 扩展并入 + 校验 | `compose_with_extensions` | `extensions/stack.py` | 按 Placement 锚点插入扩展贡献,`assert_ordering` 终检 |

### 2.1 ① 共享基座:三个内部清单

`_build_runtime_middlewares` 内部按职责分成三个清单再拼接
(`middlewares = [*outer_wrappers, *thread_hooks, *tail]`):

```python
outer_wrappers:  # 链位 1-3 —— 最外层 wrap_model_call(净化/预算/中和)
    InputSanitizationMiddleware()          # 1 必须先装配 = 最外
    ToolOutputBudgetMiddleware             # 2
    ToolResultSanitizationMiddleware()     # 3 坐在 2 内层:先中和后截断
thread_hooks:    # 链位 4-6 —— before_agent 一次性钩子(线程数据)
    ThreadDataMiddleware(lazy_init=...)    # 4
    UploadsMiddleware()                    # 5 仅 include_uploads=True(lead)
    SandboxMiddleware(...)                 # 6
tail:            # 链位 7-13 —— 后处理/工具边界层(append 序 = 物理序!)
    DanglingToolCallMiddleware()           # 7  (include_dangling_tool_call_patch)
    LLMErrorHandlingMiddleware(...)        # 8
    ToolReceiptMiddleware(...)             # 13·外 ← 物理上紧跟 8 之后!
    GuardrailMiddleware(authz 适配)        # 9·外 (authorization.enabled)
    GuardrailMiddleware(显式 provider)     # 9·内 (guardrails.enabled)
    SandboxAuditMiddleware()               # 10
    ReadBeforeWriteMiddleware()            # 11 (read_before_write.enabled)
    ToolProgressMiddleware(...)            # 12 (tool_progress.enabled)
    ToolErrorHandlingMiddleware(...)       # 13·内 ← 最内层,执行+抓异常
```

### 2.2 ② lead-only 追加段

`build_middlewares` 拿到基座后按固定顺序继续 `append`(代码里每段都有注释解释
"为什么必须在谁之后")。顺序即:**14–17 上下文四件套 → 18 压缩 → 19 待办 →
20 用量 → 21 标题 → 22 记忆 → 23 视觉 → 24/25 MCP 路由与过滤 → 26 系统消息合并
→ 27 子代理限额 → 28 循环检测 → 29 预算硬停 → 30 自定义 → 31 配置扩展 →
32–35 收尾四件套**。文件头还有一段"顺序注释"总结了硬约束:
ThreadData 在 Sandbox 之前(先有 thread_id)、Summarization 要早(先减上下
文)、Title/Memory/ViewImage/Clarification 的相对位置理由。

### 2.3 链位号 vs 物理序:全书唯一需要记住的偏差

对比 §2.1 的两个注释与速查表编号,你会看到**链位号 ≠ 物理 append 序号**:

- **#9 是一个语义槽,物理上是 0~2 个 GuardrailMiddleware 实例**(authorization
  适配器 + 显式 guardrail provider,授权永远在显式 guardrail 外层先判);
- **#13 是一个"夹心槽"**:语义上把"工具账本"的两端合成一位——**最外层**的
  ToolReceipt(物理紧跟 #8,因为任何会短路/重建结果的门卫都必须被它包住,否则
  账本静默缺口)与**最内层**的 ToolErrorHandling(物理队尾,负责真正执行工具并
  把异常变成 error ToolMessage)。中间夹着的 #9/#10/#11/#12 全是"可以不执行
  工具就返回结果"的短路者。

这就是"35 链位"的正确读法:**35 是文档化的语义槽位上限,不是实例数**。#9
(0–2 个 Guardrail 实例)与 #13(2 个中间件的夹心)在计数时对折为一位,加上
可选位按开关装配、30/31 是 0..N 的插入点——真实实例数随配置在约 25(全关 +
零插入)到 35+(全开 + 插入)之间浮动。链位号是**全书统一口径**(AGENTS.md 与
8 篇深文共用),物理位置以代码为准。

### 2.4 ③ 扩展并入:为什么必须在最外层 builder 的末尾

扩展中间件(plugin 贡献)不是 append 进列表,而是按 **Placement 锚点**插入
(`extensions/stack.py` 的锚点表):

| Placement | 锚点 | 含义 |
|---|---|---|
| `MODEL_LOGICAL` | `LLMErrorHandling` 外层 | 一个逻辑决策 = 一次事件,即使内层重试多次 |
| `MODEL_PHYSICAL` | Safety 之后、Clarification 之前 | 观察"最终请求",但不越过 Clarification 改变"最终请求"的定义 |
| `TOOL_VISIBLE` | 最外层 | 看到所有工具可见性改写 |
| `TOOL_RAW` | Clarification 外层 + 最内 | 尽量贴近真实工具可调用;Clarification 是唯一例外(它短路工具环) |
| `STANDARD` | LLMErrorHandling 外层 + 最内 | 通用默认 |

代码注释点明设计:**扩展注入只能在最终列表组装完成后执行一次**——若在基座
builder 里做,MODEL_PHYSICAL 贡献会落在 lead-only 的 ~18 个中间件**之上**,
改变观察者对"最终请求"的定义。并入后 `assert_ordering` 立即跑不变量校验
(§1.5-3),扩展贡献想悄悄反转某个不变量会在装配期失败并**点名该扩展**。

### 2.5 subagent 链:复用基座,换一段 lead-only

`build_subagent_runtime_middlewares` 复用同一基座,差异全部是**参数 + 追加段
替换**:

- 基座参数:`include_uploads=False`(子代理不注入上传)、
  `receipts_render_mode="always"`(引用在子代理语境产生——不渲染账本就无引用)、
  `owns_agent_skill_projection=False`(子代理不重建线程级 skill 投影);
- 追加段换血:不装 DynamicContext(14)而装轻量 `SubagentDateContextMiddleware`
  (只注入日期,无记忆查找/ID-swap——子代理一次性图不需要 lead 那套生命周期);
  SkillActivation(15)/SkillToolPolicy(16)同 pair 复用(白名单限 discovery);
  DurableContext(17)+ Summarization(18,`skip_memory_flush=True`,否则子代理
  内部轮次会写进**父线程**记忆);ViewImage/McpRouting/DeferredToolFilter 按
  同样条件装配;LoopDetection(28)/TokenBudget(29,默认开,`subagents.token_budget`)
  镜像 lead(子代理无 task 工具,只有工具循环启发式可能触发);
  Safety(34)照装;最后同样是 configured-extensions + compose_with_extensions
  (AgentScope.SUBAGENT 锚点,MODEL_PHYSICAL 锚在 Coalescing 之后)。

> 一句总结:中间件系统的"三段式"——**同一基座 + 按 scope 换追加段 + 扩展末段
> 锚点并入**——让 lead、subagent、bootstrap agent 共享同一套安全/质量护栏,
> 差异被收敛到参数与追加列表,而不是复制粘贴整条链。

---

## 3. 如何阅读 middleware-01 .. 08

8 篇深文每篇 400–700 行,共约 4300 行,**不要从头通读到尾**。它们按"职责带"
分组,与链位号的对应关系如下(也是你查深文的地图):

| 深文 | 覆盖链位 | 职责带 | 什么时候该读 |
|---|---|---|---|
| `middleware-01-io-safety.md` | 1、2、3(+ 7) | I/O 通道防线:进模型的、出模型的字节 | 关心注入/伪造/超大输出/悬挂调用 |
| `middleware-02-infrastructure.md` | 4、5、6 | 线程目录、上传上下文、沙箱生命周期 | 想懂"文件落在哪、沙箱何时生何时灭" |
| `middleware-03-error-handling.md` | 8、9、10、13 | 错误归一、授权/护栏门、沙箱审计、工具账本 | 关心"工具调用为什么没执行/失败了还可见" |
| `middleware-04-file-safety.md` | 11、12 | 读改写门 + 停滞状态机 | 关心文件写安全与"工具空转" |
| `middleware-05-context-injection.md` | 14、15、16、17 | 注入即改写:日期/技能/持久上下文 | 关心"模型凭什么知道今天几号、会用什么技能" |
| `middleware-06-conversation-management.md` | 18、19、20、21、22 | 对话生命周期:压缩/待办/用量/标题/记忆 | 关心上下文变短、标题、记忆入库 |
| `middleware-07-vision-routing.md` | 23、24、25、26 | 视觉注入、MCP 延迟工具路由与过滤、系统消息合并 | 关心图片、tool_search 机制 |
| `middleware-08-safety-guards.md` | 27、28、29、30、31、32、33、34、35 | 终止性与收尾:限额/循环/预算/空回复/澄清 | 关心 run 为什么停、怎么停、扩展插哪 |

三条推荐路径:

1. **机制入门**:先读本章 §1,再读 `middleware-01` 的 §0("先建立心智模型")——
   那是全系列钩子体系讲得最透的一节;其余 7 篇的 0.x 节各自是"本篇主角的心智
   模型",按需回翻。
2. **按链位顺序通读一遍地图** → `01 → 02 → 03 → 04`(共享基座 1–13,和链序
   平行,注意 01 还含 #7、03 讲 #8–13)→ `05 → 06 → 07`(lead-only 上下文带,
   14–26)→ `08`(27–35 一网打尽,且 §0.2/§0.3 是全系列对"收尾语义与插入点"
   最权威的论述,本章 §6 即其浓缩)。
3. **按需深挖**:遇到具体中间件的行为疑问,用本章 §4 速查表的深链直接跳到对应
   深文的对应小节——每篇的目录结构高度统一:「解决什么问题 → 钩子与执行时机/
   链位置 → 伪代码/图 → 与邻居的协作 → 键与常量速查」,定位成本很低。

**深文里看什么、不看什么**:深文是"为什么"——状态机转移表、命令分类的位置
盲区、ID-swap 的午夜纠正、receipt 的 2,000 字符预算与 r1..rN 渲染……这些细节
本章刻意不重复;每篇标注的**源码行号指引**(如 `agent.py` ~613、`tool_error_
handling_middleware.py` ~411)是"读代码时的路标"。注意:部分深文写作时对个别
链位用了**物理编号**(如 doc-03 把 ToolErrorHandling 称"第 15 位")——遇到
数字对不上时以本章 §4 的语义链位号为准,物理序见 §2.1。

---

## 4. 35 链位职责速查表

> 装配条件列:(恒)= 无条件装配;(可选:xxx)= 条件成立才装配,开关见 §5。
> 「#13」为夹心槽,表内拆成两行展示其两端,链位仍记 13。

| 链位 | 中间件 | 装配 | 钩子要点 | 一句话职责 | 深链 |
|---|---|---|---|---|---|
| 1 | `InputSanitizationMiddleware` | 恒 | `wrap_model_call` 最外 | 净化用户消息中的框架标签/边界,`original_user_content` 溯源;让所有内层(含重试)看到干净输入 | [middleware-01](../middleware/middleware-01-io-safety.md) |
| 2 | `ToolOutputBudgetMiddleware` | 恒 | `wrap_tool_call` + `wrap_model_call` | 超大工具输出外化到 `.tool-results`(typed synopsis + read_file 引用),模型侧兜底截历史巨文 | [middleware-01](../middleware/middleware-01-io-safety.md) |
| 3 | `ToolResultSanitizationMiddleware` | 恒 | `wrap_tool_call` | 中和远程内容工具结果(web_fetch/web_search/image_search/web_capture + 一切 MCP 工具)的框架/注入标签 | [middleware-01](../middleware/middleware-01-io-safety.md) |
| 4 | `ThreadDataMiddleware` | 恒 | `before_agent` | 建用户/线程目录(user-data/{workspace,uploads,outputs}),身份经 `resolve_runtime_user_id` 解析 | [middleware-02](../middleware/middleware-02-infrastructure.md) |
| 5 | `UploadsMiddleware` | lead 恒;subagent 无 | `before_agent` | 把"本轮新上传"的文件清单注入对话,不泄露历史上传 | [middleware-02](../middleware/middleware-02-infrastructure.md) |
| 6 | `SandboxMiddleware` | 恒 | `before/after_agent` | 获取沙箱存 `sandbox_id`、run 末归还;持有线程物理 skill 投影(子代理非 owner) | [middleware-02](../middleware/middleware-02-infrastructure.md) |
| 7 | `DanglingToolCallMiddleware` | 恒(lead/subagent) | `wrap_model_call` | 为无响应的 tool_calls 注入占位 ToolMessage;净化畸形调用,防严格 provider 400 | [middleware-01](../middleware/middleware-01-io-safety.md) |
| 8 | `LLMErrorHandlingMiddleware` | 恒 | `wrap_model_call` | provider/模型调用失败归一为可恢复的助手可见错误,含重试与限流 | [middleware-03](../middleware/middleware-03-error-handling.md) |
| 9 | `GuardrailMiddleware`(授权适配器 +/或显式 provider,0–2 实例) | 可选:`authorization.enabled` / `guardrails.enabled` | `wrap_tool_call` 执行期门 | 执行前双重放行闸:授权先判(可省一次外部调用),护栏后判(含 tool_search);fail-closed、发布 `AuthorizationOutcome` | [middleware-03](../middleware/middleware-03-error-handling.md) |
| 10 | `SandboxAuditMiddleware` | 恒 | `wrap_tool_call` | 沙箱 shell/文件操作命令分类审计;防注入是 defense-in-depth,**不是安全边界**(沙箱才是) | [middleware-03](../middleware/middleware-03-error-handling.md) |
| 11 | `ReadBeforeWriteMiddleware` | 可选:`read_before_write.enabled`(默认开) | `wrap_tool_call` | 读改写门(#3857):未读/已过期内容禁止 write/str_replace;被拦结果打 `recoverable_by_model=True` | [middleware-04](../middleware/middleware-04-file-safety.md) |
| 12 | `ToolProgressMiddleware` | 可选:`tool_progress.enabled` | `wrap_tool_call` | 结果质量停滞状态机(ACTIVE→WARNED→BLOCKED),按三类错误分级;与 LoopDetection 分工 | [middleware-04](../middleware/middleware-04-file-safety.md) |
| 13(外) | `ToolReceiptMiddleware` | 可选:`verification.receipts_enabled`(默认开) | `wrap_tool_call` 最外 + `wrap_model_call` | 工具收据账本:确定性溯源打戳;短路结果也上账;模型前渲染隐藏账本 r1..rN(2K 字符预算) | [middleware-03](../middleware/middleware-03-error-handling.md) |
| 13(内) | `ToolErrorHandlingMiddleware` | 恒 | `wrap_tool_call` 最内 | 真正执行工具;异常转 error ToolMessage + 戳 `deerflow_tool_meta`,让 run 可继续 | [middleware-03](../middleware/middleware-03-error-handling.md) |
| 14 | `DynamicContextMiddleware` | 恒(lead);subagent 换 `SubagentDateContextMiddleware` | `before_agent` | 日期(+可选记忆)以 `<system-reminder>` 注入首条 HumanMessage,系统提示保持静态供 prefix-cache 复用 | [middleware-05](../middleware/middleware-05-context-injection.md) |
| 15 | `SkillActivationMiddleware` | 恒(lead/subagent) | `wrap_model_call` | 检测 `/skill` 严格语法,注入 SKILL.md 正文为隐藏当轮上下文;与 16 共享 owner-token 互相认证 | [middleware-05](../middleware/middleware-05-context-injection.md) |
| 16 | `SkillToolPolicyMiddleware` | 恒(lead/subagent) | `wrap_model_call` + `wrap_tool_call` | 激活后按 allowed-tools 裁 schema(模型不可见)+ 阻断越权执行;`task` 不豁免 | [middleware-05](../middleware/middleware-05-context-injection.md) |
| 17 | `DurableContextMiddleware` | 恒(lead/subagent) | `before/after_model` + `wrap_model_call` | 委派账本/技能引用/压缩摘要投影为持久上下文;权威走 SystemMessage、不可信值走隐藏 HumanMessage 数据块 | [middleware-05](../middleware/middleware-05-context-injection.md) |
| 18 | `DeerFlowSummarizationMiddleware` | 可选:`summarization.enabled` | `before_model` | 逼近 token 上限时压缩早期消息;保住最新真实用户请求;摘要写 `summary_text` 通道 | [middleware-06](../middleware/middleware-06-conversation-management.md) |
| 19 | `TodoMiddleware` | 可选:runtime `is_plan_mode` | 多钩子 | 待办清单 `write_todos`;防压缩丢待办、防带未完成待办草草收场 | [middleware-06](../middleware/middleware-06-conversation-management.md) |
| 20 | `TokenUsageMiddleware` | 可选:`token_usage.enabled` | `after_model` | 每次模型调用用量记账;子代理用量按消息位次归因回派发消息(幂等打标) | [middleware-06](../middleware/middleware-06-conversation-management.md) |
| 21 | `TitleMiddleware` | 恒装配(内部按 `title.enabled`) | `after_model` | 首轮完整交换后自动起线程标题;同步路径只做本地回退,异步路径才调 LLM | [middleware-06](../middleware/middleware-06-conversation-management.md) |
| 22 | `MemoryMiddleware` | 非 tool-mode 恒装;tool-mode 视后端 | `after_agent` | 整轮结束后把(user + 最终 AI)消息投进记忆队列,异步提取 | [middleware-06](../middleware/middleware-06-conversation-management.md) |
| 23 | `ViewImageMiddleware` | 可选:模型 `supports_vision` | `wrap_model_call` | base64 图片以隐藏消息只活在本次请求,checkpoint 只留 `viewed_images` 轻元数据;自清残留 | [middleware-07](../middleware/middleware-07-vision-routing.md) |
| 24 | `McpRoutingMiddleware` | 可选:`tool_search.enabled` 且有路由索引 | `before_model` | 按最新真实用户消息意图,提前提升匹配的 deferred MCP schema(默认 top-3,只提升不执行) | [middleware-07](../middleware/middleware-07-vision-routing.md) |
| 25 | `DeferredToolFilterMiddleware` | 可选:`tool_search.enabled` 且有 deferred 集 | `wrap_model_call` + `wrap_tool_call` | 隐藏 deferred(MCP)工具 schema,直到 tool_search/路由提升(读 `promoted`,hash 作用域) | [middleware-07](../middleware/middleware-07-vision-routing.md) |
| 26 | `SystemMessageCoalescingMiddleware` | 恒 | `wrap_model_call` | 把每条 SystemMessage 合并成请求开头唯一一块;修严格后端(vLLM/SGLang/Qwen/Anthropic)拒收非开头系统消息 | [middleware-07](../middleware/middleware-07-vision-routing.md) |
| 27 | `SubagentLimitMiddleware` | 可选:runtime `subagent_enabled` | `wrap_tool_call` | 截断超额 `task` 调用:每响应并发上限 + 每 run 委派总量上限(读持久委派账本按 run_id 计数) | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 28 | `LoopDetectionMiddleware` | 可选:`loop_detection.enabled` | `after_model` | 调用模式层死循环防护:重复相同 tool_calls → 剥调用、强制文本收尾、戳 `loop_capped` | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 29 | `TokenBudgetMiddleware` | 可选:`token_budget.enabled` | `after_model` + `wrap_model_call` | 每 run token 预算硬停,戳 `token_capped`(与 loop 对称的 stop-reason 通道) | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 30 | 自定义中间件 `custom_middlewares=[...]` | 程序注入 | 自定义 | **代码注入点**:`build_middlewares(custom_middlewares=[...])` 显式传入的实例列表,落在 27–29 之后、收尾段之前 | [middleware-08 §0.2](../middleware/middleware-08-safety-guards.md) |
| 31 | 配置扩展中间件 `extensions.middlewares` | 可选:config 声明 | 自定义 | **配置注入点**:`module.path:ClassName`(零参构造)经 `resolve_class` 加载;缺失/非法装配期 loud fail;subagent 同列表 | [middleware-08 §0.2](../middleware/middleware-08-safety-guards.md) |
| 32 | `TerminalResponseMiddleware` | 恒 | `wrap_model_call` | 空终态兜底:工具执行后返回空 AIMessage → 注入隐藏恢复提示重试一次;仍空则落可见错误回退 | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 33 | `ModelLengthFinishReasonMiddleware` | 恒 | `after_model` | 长度封顶记账:检测 finish_reason=length/max_tokens → 戳 `model_length_capped`,内容原样保留 | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 34 | `SafetyFinishReasonMiddleware` | 可选:`safety_finish_reason.enabled`(默认开) | `after_model` + `wrap_tool_call` | provider 安全终止(content_filter 等)时抑制工具执行——反向分派中**最先**看到原始输出 | [middleware-08](../middleware/middleware-08-safety-guards.md) |
| 35 | `ClarificationMiddleware` | 恒(必须最后) | `wrap_tool_call` + `after_model` | 拦截 `ask_clarification`:`Command(goto=END)` 中断问人,剥同批 sibling;表单 v1/v2 校验原子化 | [middleware-08](../middleware/middleware-08-safety-guards.md) |

---

## 5. 可选位与开关:一条链,多种长度

下面的开关**全部在装配期读取**,改配置后需重启/重建 agent 才生效;运行时
configurable(`config.configurable`)只有两处(`is_plan_mode`、
`subagent_enabled` 及其限额)会影响装配。

| 链位 | 装配条件 | 默认 | 备注 |
|---|---|---|---|
| 9 | `authorization.enabled` / `guardrails.enabled`(+ provider) | 关 | 授权适配器与显式护栏相互独立,0–2 实例 |
| 11 | `read_before_write.enabled` | 开 | 最外层写门;装在 ToolProgress 之外免得拦写消耗停滞槽 |
| 12 | `tool_progress.enabled` | 开 | 结果质量状态机 |
| 13(外) | `verification.receipts_enabled`;渲染模式 `verification.receipts_render_mode` | 开 / `delegation_only`(lead) | 打戳恒开,渲染模式 lead 只渲染委派结果、subagent `always` |
| 18 | `summarization.enabled` | 视部署 | lead 与 subagent 读同一开关,不会漂移 |
| 19 | runtime `config.configurable.is_plan_mode` | 关 | 唯一"按请求"可变的装配位 |
| 20 | `token_usage.enabled` | 关 | |
| 21 | 恒装配,`title.enabled` 内部判定 | 开 | 装配期无法关闭,但可经 title 配置关闭行为 |
| 22 | 非 tool-mode 恒装;`memory.mode=tool` 时仅当后端要求被动写入 | 视 memory 配置 | 子代理链永远不装(与父线程共享 thread_id) |
| 23 | 运行时模型 `supports_vision` | 模型相关 | 装配读的是**解析后**的 model_name,非旧 config |
| 24/25 | `tool_search.enabled` 且构建期 deferred 集非空 | 关 | 24 依赖 25 存在;25 空集时纯 no-op |
| 27 | runtime `subagent_enabled` | 关 | 限额另有 runtime `max_concurrent_subagents` / `max_total_subagents` |
| 28 | `loop_detection.enabled` | 视部署 | lead 与 subagent 均受控 |
| 29 | `token_budget.enabled` | lead 默认关;subagent 默认开 | subagent 默认上限与 `summarization.enabled` 耦合(1M/2M),用户显式值永远优先 |
| 30/31 | 恒为 0..N 个插入实例 | 无 | 见 §6 |
| 34 | `safety_finish_reason.enabled` | 开 | |

**关闭某中间件的调试法**:所有可选位都能用对应开关"摘除"后重跑复现实验
(§8)。基座里四个恒装的非可选位(#1/#2/#3/#4 等)没有开关——这是有意的:
它们是"正确性基座",想关只能临时改装配代码(改 `_build_runtime_middlewares`
的清单),不要在生产配置里找不存在的开关。

---

## 6. 扩展插入点 30/31 与收尾 32–35:为什么"不可改写"

全链只有**两个**合法的用户中间件插入点,都在 27–29 之后、32 之前:

- **#30 程序化**:`build_middlewares(..., custom_middlewares=[...])` ——
  代码里显式传入的实例列表(embedded `DeerFlowClient` 走同一条链,入口一致);
- **#31 配置化**:`config.yaml` / `extensions_config.json` 的
  `extensions.middlewares: ["module.path:ClassName", ...]` —— 零参构造、
  必须是 `AgentMiddleware` 子类、缺失包/非法类/坏模块在 **agent 创建期** loud
  fail。**信任边界**:该路径实例化任意代码,视为可信操作者输入;Gateway 的
  skill/MCP 写接口刻意不为它开写路径。子代理收到同一份列表,置于其 safety
  tail 之前。

为什么必须落在 27–29 之后、32–35 之前?两条硬理由:

1. **不得早于 Loop(28)/Token(29)**:若插在它们之前,你的 `after_model` 会先
   看到"尚未被剥 tool_calls"的失控响应——死循环/超预算的硬停语义被你架空了。
2. **不得晚于 Clarification(35),且 32–35 之间没有注册位**:收尾段是一个
   "反向分派顺序被精确调过"的闭环——Clarification 的中断原子性
   (`Command(goto=END)` 短路工具环)要求它物理最后;任何在它之后注册的东西都会
   先于它看到模型输出、可能放行它已经剥掉的 sibling。所以 32–35 是**语义上不
   可被扩展改写**的:TerminalResponse(32)注册最早(反向分派最后触发,兜底要看
   到 Safety 等所有改写后的**最终**状态)、ModelLength(33)记账、Safety(34)反向
   最先剥安全终止的调用、Clarification(35)收口问人。想观察"最终请求"的扩展用
   `MODEL_PHYSICAL` 锚点(落在 Safety 之后、Clarification 之前),想观察"原始
   工具结果"用 `TOOL_RAW`(§2.4)——**锚点机制就是给扩展的、受控的"伪插入点"**,
   与 30/31 的裸插入互为补充。

---

## 7. 自定义中间件写法

### 7.1 骨架:继承 `AgentMiddleware`,实现你需要的钩子

所有钩子签名以 langchain 1.3.x 为准(仓库锁定 1.3.14 / langchain_core 1.4.9):

```python
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage, Command
from deerflow.agents.thread_state import ThreadState   # 各中间件的 State 泛型

class MyMiddleware(AgentMiddleware[ThreadState]):
    """示例:每次模型请求前,在载荷末尾追加一行只读日期提醒(不落 checkpoint)。"""
    def wrap_model_call(self, request, handler):
        # request.override(messages=...) 只改"发给模型这一份"
        extra = SystemMessage(content="<reminder>本地时区为 UTC+8</reminder>")
        request = request.override(messages=[*request.messages, extra])
        return handler(request)          # 记得把 handler 链下去!
    async def awrap_model_call(self, request, handler):
        extra = SystemMessage(content="<reminder>本地时区为 UTC+8</reminder>")
        return await handler(request.override(messages=[*request.messages, extra]))
```

三个最常见的坑:

1. **同步/异步要成对或自知**:`invoke`/`stream` 走同步钩子,`ainvoke`/`astream`
   走异步钩子;只实现一版,另一语境会抛 `NotImplementedError`。仓库中间件全部
   两版都写。
2. **改了请求就一定要调 `handler`**(除非你故意短路);忘了调 = 模型永远收不到
   请求,整个 run 卡死。
3. **想持久化就写 state / 返回结果,不要试图把东西塞进 `request.messages`
   期待它留下**——那是瞬时通道(§1.1)。

想"每次工具调用前拦一下",用 `wrap_tool_call`;想"本轮结束后记一笔账",用
`after_model`(记得它的反向分派);想"run 开始时建个目录",用 `before_agent`。
选钩子的完整两把尺子见 middleware-01 §0.2。

### 7.2 注入与验证

```python
# 程序化注入(#30)—— 与 make_lead_agent / DeerFlowClient 同一入口
from deerflow.agents.lead_agent.agent import build_middlewares
chain = build_middlewares(config, model_name,
                          custom_middlewares=[MyMiddleware()])
```

配置化注入(#31):

```yaml
# config.yaml
extensions:
  middlewares:
    - "my_pkg.my_module:MyMiddleware"   # 零参构造,AgentMiddleware 子类
```

装配 tip:扩展贡献方还可以实现 `release_policy_parameters() -> dict`
(duck-typed,无基类)——凡"配置会改变 agent 行为"的中间件都该声明,供
`collect_release_policies()` 汇总、`assembly_descriptor` 描述(长文本用
`canonical_hash` 而不内嵌,声明是身份不是副本)。

---

## 8. 调试技巧

**1. 先看见链,再谈调试。** agent 工厂产出的 `LeadAgentAssembly.descriptor`
由 `agents/assembly_descriptor.py::build_assembly_descriptor()` 构建,按最终
顺序逐条描述中间件:实现了 `release_policy_parameters()` 的用其声明,否则
`describe_middleware` 探测字段(model/provider/routing_index/detectors…)。
**日志/可观测里先确认你以为是第 N 位的中间件真的在第 N 位**——可选位关闭时
链会缩短,链位号不变但物理邻居会变。

**2. 关掉嫌疑中间件做二分。** 任何"装配条件"是可选位的中间件(§5 表),都能
用对应配置开关摘除后重跑,对比行为差异(例如怀疑写门误伤 →
`read_before_write.enabled: false`;怀疑死循环误杀 →
`loop_detection.enabled: false`)。怀疑对象是基座恒装位(#1/2/3/4…)时,没有
开关——**不要在生产配置里找**;临时改 `_build_runtime_middlewares` 的清单
复现,验证后改回,并跑 `make test`(顺序不变量有单测覆盖)。

**3. 顺序错误会 loud fail,别自己猜。** `assert_ordering` 在校验失败时抛出
`RuntimeError: Middleware ordering constraint violated: X must be outer of Y ...
Contributed by: <扩展名>`——先读这条消息,它已经点名了谁该为错序负责;
`assert_mcp_routing_before_deferred_filter` 是另一处装配期断言。改装配代码后
若想确认,直接构造 `build_lead_runtime_middlewares()` 看返回列表顺序即可。

**4. 区分三类"没执行"的根因。** 一个工具调用没跑,先查它是被哪一层拦的:
(a) 模型侧没发出调用(Safety 剥了 tool_calls / TokenBudget 硬停 / LoopDetection
硬停 → 看 stop_reason: `safety_terminated` / `token_capped` / `loop_capped`);
(b) wrap_tool_call 短路(#9 拒绝 / #11 拦写 → 看返回的 ToolMessage.status 与
`deerflow_tool_meta`);(c) 执行了但异常被 #13(内)转成 error ToolMessage
(content 以 `Error: Tool 'xxx' failed with ...` 开头)。短路消息会自戳
`deerflow_tool_meta` 或回退到 `message.status`;正常结果由 ToolErrorHandling
在**内层返回路径**上打戳,外层 ToolReceipt 才能据此记账——这也是
#13"夹心"结构存在的意义。

**5. after_model 行为"看起来反了"是常态。** 调试 32–35 或任何多 after_model
中间件交互时,永远先把"注册顺序"倒过来推:最后注册的最先执行(§1.3)。
Safety(34)想看到原始输出,所以它**注册得最晚**(只早于 Clarification);
TerminalResponse(32)要看最终状态,所以它**注册得最早**。反直觉,但这就是设计。

**6. subagent 行为异常先查归属。** 子代理链复用基座但参数不同:无 Uploads、
receipt 渲染 `always`、`owns_agent_skill_projection=False`、Summarization 带
`skip_memory_flush=True`、TokenBudget 默认开(上限还与 summarization 开关
耦合)。"为什么子代理的表现和 lead 不一样"——八成是这些参数差异,不是 bug。

---

## 附:源码索引速查

| 想看什么 | 去哪看 |
|---|---|
| 35 链位权威语义(含可选位注释) | `agents/middlewares/AGENTS.md`(中间件链一节) |
| 基座装配(1–13,物理序) | `agents/middlewares/tool_error_handling_middleware.py::_build_runtime_middlewares`(L161) |
| lead 基座入口 | 同文件 `build_lead_runtime_middlewares`(L318) |
| subagent 装配(基座变体 + 追加段) | 同文件 `build_subagent_runtime_middlewares`(L343) |
| lead-only 追加段(14–35) | `agents/lead_agent/agent.py::build_middlewares`(L457,文件头 L447 起有顺序注释) |
| 顺序不变量声明与校验 | `extensions/ordering.py::core_ordering_constraints` / `assert_ordering` |
| 扩展 Placement 锚点 | `extensions/stack.py::_anchors`(MODEL_LOGICAL / MODEL_PHYSICAL / TOOL_VISIBLE / TOOL_RAW / STANDARD) |
| 配置扩展中间件加载(#31) | `agents/middlewares/configured_extensions.py::load_configured_extension_middlewares` |
| 链的描述/可观测 | `agents/assembly_descriptor.py::build_assembly_descriptor` / `describe_middleware` |
| provenance 键 | `deerflow_extension_api.provenance`(`provenance_kwargs()`);工具变换轨迹见 `agents/middlewares/tool_transform_meta.py` |
| 单个中间件源码 | `agents/middlewares/<name>_middleware.py`(与深文行号指引对照) |
