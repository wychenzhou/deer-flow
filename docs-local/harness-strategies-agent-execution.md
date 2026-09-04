# DeerFlow Harness 策略手册（问题 → 手段）

> **本文定位：策略地图，不是实现手册。**
> 它回答「某类问题，DeerFlow 用什么手段解决、为什么有效、长在哪」；
> 每个中间件的钩子细节、伪代码、流程图见
> `middleware/middleware-01-io-safety.md` ~ `middleware/middleware-08-safety-guards.md`（8 篇，按链位拆）。
> 逐中间件的实现细节本文不再展开，避免三遍重复。
>
> 全文字号约定：**坑** = 具体故障/威胁；**对策** = DeerFlow 的处理手段（可迁移）；`#NNNN` = 真实 issue 锚点。

---

## 0. 全文骨架：问题域 → 手段 → 载体

| # | 问题域 | 核心手段（可迁移） | 主要载体 | 深读 |
|---|--------|-------------------|----------|------|
| A | 模型 I/O 结构损坏 | 消毒 + 预算 + 修复悬挂：per-request 修补，checkpoint 保原貌 | InputSanitization / ToolResultSanitization / ToolOutputBudget / DanglingToolCall | 01 |
| B | 注入与越权 | 两个不可信入口对称消毒；可影响内容不获权威；双层授权门 fail-closed；命令按「位置」判 | 消毒链 + provenance / Guardrail / SandboxAudit / SkillToolPolicy | 01 / 03 / 05 |
| C | 上下文膨胀与污染 | 静态前缀缓存 + 动态后置注入；超限外化；精确保留最新请求；主线投影 | DynamicContext / ToolOutputBudget / Summarization / DurableContext | 05 / 01 / 06 |
| D | 失控与空转 | 终止三层护栏 + 纠偏（重锚定 vs 换招）分流；加法 stop_reason 收敛 | LoopDetection / ToolProgress / TokenBudget / SafetyFinishReason / Clarification | 08 / 04 |
| E | provider 不稳定与怪癖 | 错误分类 → 退避/熔断；异常结构化成 meta；特有字段回灌 | LLMErrorHandling / ToolErrorHandling / ToolReceipt / models 各 patch | 03 / 06 |
| F | 身份与隔离 | 单点身份解析；目录按 user/thread 分桶；沙箱 lease owner；子代理 id 拆分 | ThreadData / Sandbox / subagents | 02 |
| G | 运行时一致性 | 幂等键 + 部分唯一索引 + 租约心跳 + CAS；终态收尾纪律 | runtime/runs、checkpointer、scheduler | 02（沙箱）/ 08 |
| H | 可解释收尾 | 每个提前结束留原因；每次执行留凭证；验收对照证据 | stop_reason / deerflow_tool_meta / receipt / RunJournal | 03 / 08 |

---

## 1. 总纲：中间件链「顺序即正确性」

一条**严格排序**的中间件链同时管模型请求、模型响应、工具调用三个面。链顺序不是装饰，它就是正确性：
外层先看到消息、内层后看到；`wrap_model_call` 只改 per-request 载荷，`wrap_tool_call` 的短路结果会写进历史。
因此「哪个中间件包裹哪个」决定了修复是否在正确时机、以及被谁看到。

**三段装配**（35 个内置位，可选位按配置增减；30/31 是自定义/扩展插入点）：

```
lead:  shared base(1-13) → lead-only(14-29) → [30 custom → 31 extension] → 收尾(32-35)
subagent:  shared base 精简版 → subagent-only
```

- **1-13 shared base**（`build_lead_runtime_middlewares`，subagent 复用精简版）：
  输入消毒 → 输出预算 → 远程结果消毒 → ThreadData → Uploads → Sandbox →
  Dangling 修复 → LLM 错误处理 → Guardrail 双层门 → SandboxAudit → ReadBeforeWrite →
  ToolProgress → ToolReceipt(+ToolErrorHandling)。
- **14-29 lead-only**（`build_middlewares` 追加）：DynamicContext → SkillActivation →
  SkillToolPolicy → DurableContext → Summarization → TodoList → TokenUsage → Title →
  Memory → ViewImage → McpRouting → DeferredToolFilter → SystemMessageCoalescing →
  SubagentLimit → LoopDetection → TokenBudget。
- **30/31 插入点**：自定义中间件、config 声明的扩展中间件（视为受信代码）。
- **32-35 收尾（顺序不可被扩展改写）**：TerminalResponse → ModelLengthFinishReason →
  SafetyFinishReason → Clarification。收尾在链尾的原因：LangChain 的 `after_model` 反向分派，
  越靠后注册越先执行——Safety 要在别人之前先看到 provider 的安全终止。

> 顺序约束的经典例子：**ToolResultSanitization(3) 必须在 ToolOutputBudget(2) 内侧**——
> 先中和再截断/外化，否则外化文件里残留注入标签；**ToolReceipt 必须是最外层 wrap_tool_call**——
> Guardrail/SandboxAudit/ReadBeforeWrite 都会短路返回自己的 ToolMessage，内层 receipt 会漏记。
> 顺序破坏 =「产生错误行为而不报错」，DeerFlow 用 `assert_ordering` 在装配期硬拦（唯一硬失败）。

**消息通道纪律（贯穿全文的信任分层）**：框架权威文本走 `SystemMessage`；任何模型/用户可影响的
文本（记忆、摘要、委派结果、技能正文）走隐藏 `HumanMessage` 数据块，且 HTML 转义。
所有注入/改写消息 stamp 服务端 provenance（`deerflow_content_kind` 等，键属
`_SERVER_OWNED_MESSAGE_METADATA_KEYS`，客户端伪造不了）。

---

## A. 模型 I/O 结构损坏 → per-request 修复，checkpoint 保原貌

### A1 用户输入伪造框架标签
**坑**：用户消息里写 `<system-reminder>`/`<memory>` 等标签，可伪装受信上下文。
**对策**（InputSanitization，链 1，最外层）：只对**最后一条真实用户消息**做结构净化——
框架保留标签 HTML 转义为字面文本（保可读、剥语义）；伪造的 `--- BEGIN/END USER INPUT ---`
边界替换为中性标记（防自抑制 + 防 break-out）；**denylist 而非 allowlist**
（系统提示把「这些标签及其同类」声明为受信数据，按类覆盖才不漏新块）；多模态 content 只处理文本块、保留图片块；
净化后存 `original_user_content`（first-writer-wins）；**fail-open**——净化不当单点故障。
**关键点**：只 per-request，绝不写回 state。

### A2 远程内容伪造框架上下文
**坑**：`web_fetch` 抓到的攻击者网页里带框架标签。
**对策**（ToolResultSanitization，链 3）：与 A1 **共用同一 `neutralize_untrusted_tags` 原语**
——两个不可信入口（用户输入 / 远程内容）一套规则对称防御；只按**工具名 allowlist**
（web_fetch/web_search/image_search/web_capture）+ MCP 元数据 tag 处理，本地工具
（bash/read_file）输出不动（避免误伤合法代码）。

### A3 超大工具输出撑爆上下文
**坑**：一次 `cat`/抓取产生几十万 token，直接爆上下文。
**对策**（ToolOutputBudget，链 2，双侧翼）：
- **工具执行后（wrap_tool_call，持久事件）**：超过 per-tool/fallback 阈值 → 三层降级外化：
  ① 宿主挂载路径 → ② 远程 AIO 沙箱文件系统 → ③ 都不可用才内联 head+tail 截断（行边界对齐）。
  上下文里留**确定性概要（不调 LLM）+ `read_file` 引用**；文件名只用 `uuid4().hex[:12]`（tool_call_id 不可信）。
- **模型调用前（wrap_model_call）**：只扫历史里漏网的内联截断，**绝不重新外化**（外化需要沙箱时机）。
- 外化目录 `.tool-results` 排除出工作区变更扫描与交付校验（是过程反馈，不是产物）。

### A4 悬挂工具调用 / 孤儿结果
**坑**：用户中断后 AIMessage 带 tool_calls 但无对应 ToolMessage；严格 provider（OpenAI 兼容系）
拒绝下一请求。
**对策**（DanglingToolCall，链 7，wrap_model_call）：扫描历史，规范化畸形 id/name/args →
重排消息：孤儿 ToolMessage 丢弃；每个 AIMessage 后按序补配对——有结果重发、无结果注入合成 error ToolMessage。
配对**保守**：全 open call 都有结果才用位置破平，否则宁可丢（"好过发明一个配对"）。
合成错误分三类文案（无效名 / 解析失败 / 一般中断），错误描述限 500 字符（#2894）。
**为什么 wrap_model_call 而不用 before_model+add_messages**：补丁必须插在悬挂消息**紧后方**保因果序，
reducer 追加到末尾会破坏顺序且写进 state。修复是每轮纯函数重放，checkpoint 保留事故原貌。

> **三者的共同纪律（域 A 灵魂）**：per-request 修改 = 每次模型调用前的纯函数，
> 从不污染 checkpoint；checkpoint 是事故现场，重放永远能复现。

---

## B. 注入与越权 → 谁的内容谁负责 + 双层门

### B1 「可影响内容不获权威」（OWASP LLM01）
**坑**：把记忆/网页内容拼进 system prompt → 拿到 system 级指令权，且破坏前缀缓存。
**对策**（贯穿全框架的信任分层）：
- 系统提示**完全静态**；记忆/摘要/委派结果一律走隐藏 HumanMessage 数据块 + HTML 转义；
- 服务端 provenance 键防伪造（见 §1）；
- 每条机制**诚实标注边界**：SkillToolPolicy 是「行为 scoping 不是安全边界」、
  SandboxAudit 是「纵深防御不是安全边界」——隔离边界在沙箱，不在中间件。

### B2 越权调用工具
**坑**：模型（或被注入的上下文）调用角色不该调的工具。
**对策**（GuardrailMiddleware，链 9/10/11，双层门，均 fail-closed）：
- **Layer 1（装配期）能力过滤**：按角色裁剪工具 schema（模型根本看不见）；
- **Layer 2（执行期）**：pre-tool-call 对每个调用复核（`GuardrailAuthorizationAdapter`
  复用 Layer 1 同一 provider 实例）；guardrails.enabled 时再叠加外部 GuardrailProvider。
- 每个决策发布中性 `AuthorizationOutcome` 到 per-run runtime context（`__authorization_outcome` 键，
  调用方构造时剥伪造）。

### B3 命令注入：按「位置」判，不按「有没有 $()」
**坑**：`$(curl url)` 出现在**命令位**（`$(curl url)`、`| sh`、`eval`、`-c`、`<<<`）会执行远端内容；
出现在**值位**（`x=$(curl url)`、`echo $(curl url)`）只是捕获输出。
**对策**（SandboxAudit，链 12）：整命令 Pass 1（位置盲区：eval/source 参数、解释器 code-string flag、
here-string）→ `_split_compound_command` 按子命令 Pass 2（命令位 vs 值位锚定匹配）；
heredoc 体是数据（记录 `<<EOF` 头、body 原样消费）。**定位声明**：这是审计与纵深，沙箱才是边界。

### B4 路径穿越（纵深）
**坑**：`../../etc`、裸 `/`、`file://` URL 逃逸。
**对策**：tools 辅助层多重校验（读写 surface 拒绝穿越；写限 `VIRTUAL_PATH_PREFIX/`/skills/mounts；
`cd` 拒绝穿越；命令替换里根绝对路径拒绝）+ 沙箱契约层 `download_file` 穿越抛
`PermissionError`、读失败抛 `OSError`，底层重抛**保留原始路径**隐藏宿主真实路径。

### B5 秘钥泄漏
**坑**：沙箱命令继承宿主 env 泄漏 `*KEY*/*SECRET*/*TOKEN*/*PASS*`（#3861）。
**对策**：`build_sandbox_env` 先剥离敏感 env + `SSH_AUTH_SOCK`，再叠加 per-call 请求秘钥
（键名 `^[A-Za-z_][A-Za-z0-9_]*$` 校验，且从命令字符串剥离）。

---

## C. 上下文：前缀缓存 + 三层内存治理

**总原则**：上下文里的每一段都要回答「哪来的、可信吗、什么时候过期」；代价分三层治理——
**放得下就放（注入）、放不下就外化/压缩（预算）、不该留就防（污染门）**。

### C1 前缀缓存优先：静态 system + 后置注入
**坑**：把日期/记忆拼进 system prompt → 每个用户/每轮不同前缀，缓存全废。
**对策**（DynamicContext，链 14）：system prompt 完全静态；日期/记忆以 `<system-reminder>`
**一次性**注入到首条真实用户消息前（frozen-snapshot：同 ID 原位冻结首条消息，
ID-swap `{id}__memory`/`{id}__user` 附着最新用户消息）；跨零点只发轻量仅日期纠正消息；
日期用 `reminder_date` 存 `additional_kwargs` 结构化检测（不靠 regex 扫内容）。

### C2 压缩不能删「现在在问什么」
**坑**：summarization 把最新用户请求卷进 summary → 模型答错 turn；删掉已注入的记忆提醒 →
下轮模型失忆；首轮长分析若把 cutoff 前移会变成 no-op。
**对策**（Summarization，链 18）：按**精确消息 ID** 保留最新真实用户请求（`is_real_user_message`）；
按 tag 救回 `<system-reminder>` 日期 + `__memory` peer；**故意不救** stale ID-swap `__user` peer
（那是跨轮 prompt 污染源）；已有摘要以 `name="summary"` 混进 token 计数防摘要无限累积；
fraction 触发在模型无 max_input_tokens profile 时**丢弃该子句**而非让 agent 构建失败（#3103）。

### C3 主线投影：压缩删不掉的「任务账本」
**坑**：委派太多、历史被压缩后，模型视角里「我本来要干什么」消失。
**对策**（DurableContext，链 17，在 Summarization 之前捕获）：把 `task` 委派摘要
（completed/in-progress/结果）与已加载技能引用（只 name/path/description，不存正文）投影进**每次请求**；
权威规则走 SystemMessage，不可信字段走 HumanMessage 数据块——压缩产物不能被伪装成系统指令。

### C4 记忆防污染（写入侧）
**坑**：框架内部消息（提醒/图片注入）当事实入库 → 自放大循环；纯确认对话批量触发 LLM；
「上传了文件」被当持久事实 → 未来去找不存在的文件。
**对策**（Memory 系，域见 middleware-06 与 §F 原则）：
- **提取过滤**：只取 human + 无 tool_calls 的 ai；跳过 `hide_from_ui` 消息；
  `filter_trivial` fullmatch 丢「ok/好的/谢谢」；
- **上传是 session 级**：提取/格式化/持久化三层剥离上传提及；
- **确定性门**：prompt 之外有代码级门——`scope/durability/authority` 三字段 + 置信度阈值 +
  read-check-write 临界区去重；
- **写入队列**：按 `(thread,user,agent)` 合并去抖；`queue_max_depth` 后只拒绝普通更新，
  **signal 与 emergency 永远准入**（重要记忆永不丢弃）；
- **淘汰**：hybrid 加权（confidence/confirmation/access）+ correction 保留名额（10%），
  防止把用户刚重申的事实逐出（#4641/#4789）。

---

## D. 失控与空转：终止(硬保证) + 纠偏(概率引导) 分流

> 两类问题症状像（run 不结题），对策方向**相反**，先分类：
> **脱主线（跑题）** → 重新锚定目标任务；**卡住不动（无效重复）** → 敦促换招/收尾。
> **终止是硬保证，纠偏是行为引导**——先纠偏、纠不动再终止。

### D1 终止：三层护栏 + 收尾语义（详见 middleware-08）
| 层 | 中间件 | 探测 | 触发行为 |
|---|---|---|---|
| ① 调用模式 | LoopDetection(28) | 重复相同 tool_calls（哈希+滑窗，≥3 warn / ≥5 硬停） | warn 注入 `[LOOP DETECTED]`；硬停剥 tool_calls 强制纯文本终答 |
| ② 结果质量 | ToolProgress(12) | 工具反复返回「无新信息」（Jaccard 判重 + 错误分类状态机） | 只封该工具：ACTIVE→WARNED→BLOCKED，注入换招 hint |
| ③ 预算 | TokenBudget(29) | per-run token | warn 注入；超硬阈值剥 tool_calls 强制终答 |

**收尾语义（32-35，反向分派先跑 Safety）**：provider 安全终止（content_filter/refusal/Gemini
SAFETY）仍带残缺 tool_calls → **剥离 tool_calls 保留真实 finish_reason**（SafetyFinishReason）；
长度截断但有内容 → 只记 `model_length_capped` 不改写（ModelLengthFinishReason）；工具执行后
provider 返回**空终态** → 隐藏恢复提示重试一次，再空 → checkpoint 换成可见 error fallback
（TerminalResponse，防「伪成功」）；`ask_clarification` → 写 human_input 卡 + `goto=END` 暂停
（Clarification，必须最后，scheduled 非交互 run 丢弃该工具）。

**所有提前结束汇入加法 `stop_reason`**（`loop_capped/token_capped/turn_capped/safety_capped/
subagent_limit_capped/model_length_capped`）——不加新 status 枚举，旧前端忽略可选字段，
跨语言契约向后兼容；硬停**不 raise**（子代理看到 raw 崩溃），剥调用 + 自然终止 + 标记原因。

### D2 纠偏（A 脱主线 / B 卡住，详见 middleware-04/05/06/08）
- **治 A（重新锚定）**：DurableContext 投影任务主线（C3）+ Summarization 保最新请求（C2）
  + Clarification 暂停问人（唯一人机核对点）+ TodoList plan 清单 + 验收证据 grounded；
  局限：scheduled run 里 Clarification 不可用，只能靠前两者。
- **治 B（换招/收尾）**：ToolProgress `[PROGRESS HINT]`（带 recommended_next_action：
  rewrite_query/try_alternative/summarize/stop）+ LoopDetection warn（"Stop calling tools and
  produce your final answer"）+ TerminalResponse 空响应自救。
- **协同**：两守卫 warn/hint 都在 `wrap_model_call` 注入且排在所有 ToolMessage 后
  （不能插在 tool_calls 与其响应之间——OpenAI/Moonshot 拒收）；互不读对方状态；
  LoopDetection 硬停后不发 wrap_tool_call，ToolProgress 自然不触发，无双停。
- **诚实局限**：没有语义级跑题检测；hint 依赖 `deerflow_tool_meta`（短路径/外部 MCP 可能缺失）；
  纠偏是概率性的，真正的硬保证只有终止层。

---

## E. Provider 不稳定与怪癖 → 分类、结构化、回灌

### E1 LLM 调用失败：分类 → 退避/熔断（LLMErrorHandling，链 8）
- **分类**：quota/auth 不可重试；burst_rate 在通用 429 前判；`IndexError` 当 transient
  （Volces 端点 200 带空列表）；有效预算 = min(配置上限, 分 class, 分 reason)；
- **退避**：AWS 式去相关抖动，尊重 provider `Retry-After`；backoff 睡眠经进程级 limiter
  （睡眠释放并发槽）；
- **熔断**：closed→open→half-open；**burst-rate 失败释放探测但不记 fail**（防 slope 限流
  把自己熔断，自己制造故障，#4290）；
- **进程级并发上限用 threading 原语**（asyncio.Semaphore 绑单 loop，盖不住 lead+subagent+sync）；
- **优雅收尾**：耗尽返回可读 AIMessage + 观测字段，`GraphBubbleUp` 原样 re-raise。

### E2 工具异常 → 结构化 meta（ToolErrorHandling + ToolReceipt，链 13）
- 异常 → 错误 ToolMessage + stamp `deerflow_tool_meta`（status/error_type/
  recoverable_by_model/recommended_next_action/source）——下游 ToolProgress 状态机靠它分类；
- **ToolReceipt 是最外层 wrap_tool_call**：短路结果不空窗 ledger；stamp 确定性凭证
  （工具名/状态/参数与输出哈希/字节数/时间戳）；模型可见的是紧凑 receipt 列表（r1..rN，
  2000 字符预算，最新优先 + 遗漏标记）。

### E3 Provider 特有字段静默丢失 → 回灌补丁（models/）
**坑（共同根因）**：LangChain 基础 adapter 只序列化标准字段，静默丢弃 provider 特有字段；
**要求这些字段在每条历史 assistant 消息逐字回显**的 provider（DeepSeek `reasoning_content`、
Gemini `thought_signature`、MiniMax `reasoning_details`、vLLM `reasoning`…）拒绝下一次请求。
**对策**：每个 provider 一个 patch（只声明「回灌哪个字段」）+ 共享
`restore_assistant_payloads`（按 `(content, tool_call_ids)` 签名匹配 + 位置回退——纯位置 zip
在 provider 剥离消息时会崩）。能力矩阵 gating：`supports_thinking/vision/reasoning_effort`
不支持就把对应开关/字段在工厂剥离。

---

## F. 身份与隔离 → 单点解析 + 桶一致 + lease owner

- **目录分桶**（ThreadData，链 4）：`users/{user_id}/threads/{thread_id}/user-data/{workspace,
  uploads,outputs}`；身份一律走 `resolve_runtime_user_id`（server auth → langgraph auth →
  runtime.context → ContextVar → default）——杜绝「middleware 算 A 桶、工具写 B 桶」串台；
- **沙箱 lease owner**（Sandbox，链 6）：lead 与各子代理各持本地租约，**仅最后一个持有者停掉远端**；
  `fork_restored` 明确不释放；子代理 `sandbox_lease_owner_id=subagent:{task_id}`，一个子代理结束
  不能关沙箱而兄弟还在跑（#5128）；release 在 finally 幂等；
- **沙箱 id 隔离令牌**：`sha256(user:thread)` 截 16 位；ID 碰撞抛 `SandboxIdentityCollisionError`
  （fail-closed）；policy-scoped/custom-root 各自独立 id 域；
- **子代理身份拆分**（subagents）：provider `tool_call_id` 跨 run 会重复，不能做 registry 所有权键；
  server 生成 uuid 做 `task_id`，外部关联走 `external_task_id`——cancel/cleanup 永不串 run；
- **loop-bound 对象不跨界**：RunJournal 带 owner loop + loop-bound SQL pool，子代理只拿窄 record
  代理（`call_soon_threadsafe` 调回 owner loop）；asyncio.Semaphore 绑单 loop → 用 threading。

---

## G. 运行时一致性 → 幂等键 + 唯一索引 + 租约心跳 + CAS

- **run admission**：本地 inflight + DB 持久化 + 内存注册放**同一把锁**临界区；
  DB 层部分唯一索引 `uq_runs_thread_active`（thread_id UNIQUE WHERE status IN
  ('pending','running')）——同一线程同时只有一个活跃 run；
- **幂等键**：`idempotency_key` 命中返回已有记录（不挂第二个 worker）；DB 唯一索引兜底；
- **checkpoint 写竞态**：`reserve_checkpoint_write` = goal_thread_lock + 短命持久 pending 行；
  per-thread worker checkpoint 锁 + 3 次重试 CAS（写前 re-read head，身份变则拒写）；
  中断恢复走 `_linearize_delta_checkpoint_resume` 把 delta fork 转线性 head（#4458）；
- **多实例**：heartbeat 每 `lease_seconds/3` 续租、成功才推进 deadline（fail-closed）；
  `claim_for_takeover` 原子胜出后才写 zero-delivery receipt；`multi_instance` 启动门拒绝不安全组合；
- **终态纪律**：flush journal → 持久化 receipt → **之后**才持久化终态（防「终态比 receipt 后死」）；
  首个终态转移胜（`try_set_terminal`）；interrupt/rollback 收尾先置 `finalizing` 再恢复 checkpoint；
  中断首轮没标题 → worker finalizing 阶段写本地回退标题（双门防覆盖更新的 run）；
- **recursion clamp**：客户端任意大 recursion_limit → clamp 到 `max_recursion_limit`(默认1000)
  在 client-config 透传后应用（覆盖客户端），调度器预 clamp + build_run_config 再 clamp（纵深）。

---

## H. 可解释收尾 → 原因 + 凭证 + 对照证据

- **为什么结束**：`stop_reason` 加法字段（D1）+ RunJournal 审计事件
  （loop_detection 不持久化工具参数/内容/哈希；memory 上下文只存 `content_sha256`——审计不等于抄家）；
- **做了什么**：`deerflow_tool_meta` + receipt（E2）；
- **做没做成**：验收检查（acceptance_checks）——可判定判据（file exists/tests_passed）
  **在代码里对照记录的证据**检查，其余一律 `UNVERIFIED` 不静默通过；
  `persistent_shell_sessions` 三态：未声明 shell 状态 ⇒ 证据按 UNVERIFIED（没证据就不装看见）。

---

## 附录：35 个链位速查

1 InputSanitization · 2 ToolOutputBudget · 3 ToolResultSanitization · 4 ThreadData ·
5 Uploads · 6 Sandbox · 7 DanglingToolCall · 8 LLMErrorHandling ·
9/10/11 Guardrail(Authorization) + SandboxAudit 邻近 · 12 ToolProgress ·
13 ToolReceipt+ToolErrorHandling · 14 DynamicContext · 15 SkillActivation ·
16 SkillToolPolicy · 17 DurableContext · 18 Summarization · 19 TodoList ·
20 TokenUsage · 21 Title · 22 Memory · 23 ViewImage · 24 McpRouting ·
25 DeferredToolFilter · 26 SystemMessageCoalescing · 27 SubagentLimit ·
28 LoopDetection · 29 TokenBudget · 30 custom · 31 configured extensions ·
32 TerminalResponse · 33 ModelLengthFinishReason · 34 SafetyFinishReason ·
35 Clarification（必须最后）

> 结语：DeerFlow 的 harness = 约 35 个中间件 + 契约层沙箱 + 模型适配补丁 + 记忆门 + 运行时竞态防护。
> 每个手段背后都是真实踩过的坑（`#NNNN`）。反复出现的不是「让模型更聪明」，而是
> **顺序、原子性、幂等、fail-closed、诚实标注边界**——这才是 agent 工程里决定能否交付的部分。
