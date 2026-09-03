# DeerFlow Harness 策略全览：如何保证 Agent 正确、安全、可控地执行

> 本文档系统梳理 DeerFlow 的 `harness` 层（`backend/packages/harness/deerflow/`）为保证 Agent 在
> **正确性 / 安全性 / 稳定性 / 可审计性** 上不出错而采用的全部机制。每一节都按
> **「坑（pitfall）→ 对策（mechanism）」** 的组织方式展开——这些正是 agent 开发者在实际踩坑后被
> 沉淀下来的工程解法。代码引用均为仓库相对路径。
>
> 参考依据：`backend/packages/harness/deerflow/` 下各子系统的 `AGENTS.md` 与具体实现，及
> `backend/AGENTS.md`、`backend/docs/CONFIGURATION.md`、`backend/docs/RUN_EVENT_STREAM.md`。

---

## 目录

1. [什么叫 Harness，以及它解决的几类问题](#1-什么叫-harness)
2. [中间件链：单点防护的完整序列](#2-中间件链)
3. [Sandbox 体系：执行隔离](#3-sandbox-体系)
4. [模型适配层：跨 Provider 一致性](#4-模型适配层)
5. [Memory 子系统：长期记忆的正确性](#5-memory-子系统)
6. [Subagents：子代理委派与容量控制](#6-subagents)
7. [Runtime：运行生命周期与流式契约](#7-runtime)
8. [Skills / MCP / Config / 扩展：装配与信任边界](#8-skills--mcp--config--扩展)
9. [跨切面设计模式：可复用的「已解决的坑」清单](#9-跨切面设计模式)
10. [总览表：对视频速查](#10-总览表)

---

## 1. 什么叫 Harness

**Harness（马具/线束）** 在 agent 框架中指的是「夹在 **LLM 模型调用** 与 **后果** 之间的那一层控制面」。
它不负责「模型有多聪明」，而负责「**当模型失控或环境异常时，系统不会崩、不会越权、不会无限烧钱、
不会污染长期状态，且能给出可解释的结果**」。

DeerFlow 的 harness 是 `deerflow-harness` 包（import 前缀 `deerflow.*`），与 `app.*`（Gateway 应用层）
有严格单向依赖边界（见 §8-12）。它解决的问题可归为六类：

| 类别 | 典型坑 | 代表机制 |
|---|---|---|
| **终止性** | 模型陷入死循环、无限烧钱 | 递归上限 clamp、token 预算、循环检测 |
| **正确性** | 工具调用畸形、消息序列破坏、离线数据串台 | 消息配对校验、序列重放、状态机 |
| **安全性** | 提示注入、越权、路径穿越、秘钥泄露 | 消毒中间件、鉴权、沙箱、秘钥 env 注入 |
| **稳定性** | 超时、限流、连接中断、分片竞态 | 重试/退避/熔断、幂等锁、幂等键 |
| **一致性** | 多实例/多 worker 竞争、checkpoint 竞态 | 进程锁、DB 部分唯一索引、租约心跳 |
| **可审计** | 无法判断 run 是怎么结束的、证据不可信 | stop_reason、run journal、验收检查 |

---

## 2. 中间件链

DeerFlow 的核心手段是 **一条严格排序的中间件链**。中间件既可以改写入站的 ModelRequest、改写模型响应、
也可以拦截/改写工具调用。**顺序即正确性**（§8-11 详述）。

### 每个中间件解决了什么坑

按装配顺序（见 `agents/middlewares/AGENTS.md`；编号即链上位置）：

1. **InputSanitizationMiddleware** — 最外层。坑：用户输入里的 `<system-reminder>` 等框架标签可伪造上下文/注入指令。对策：净化 `original_user_content`，保留原始内容供下游识别，服务端持有 provenance 键防伪造。
2. **ToolOutputBudgetMiddleware** — 坑：超大工具输出撑爆上下文。对策：超过 `externalize_min_chars` 外置到 `.tool-results` 目录，上下文只留紧凑摘要 + `read_file` 引用；该目录被排除出「工作区变更扫描」与「交付校验」。
3. **ToolResultSanitizationMiddleware** — 坑：远程内容（web_fetch/web_search 等）里带框架标签，可伪造可信上下文。对策：按工具名 allowlist 中和标签，本地工具输出不动。
4. **ThreadDataMiddleware** — 坑：多用户/多线程文件串台。对策：按 `user/{user_id}/threads/{thread_id}/user-data` 建目录，从 runtime 解析身份。
5. **UploadsMiddleware** — 注入上传文件列表。坑：把上传当成普通上下文会污染记忆（见 §5-6）。
6. **SandboxMiddleware** — 沙箱获取生命周期（见 §3）。
7. **DanglingToolCallMiddleware** — 坑：用户中断后，AIMessage 的 tool_calls 无对应 ToolMessage，严格 provider 拒绝下一请求。对策：注入占位 ToolMessage；非法 tool_call 名/参数消毒。
8. **LLMErrorHandlingMiddleware** — 见 §4-4 与 §9。
9. **Authorization / GuardrailMiddleware** — 坑：越权调用工具。对策：双层门（第一层能力过滤 + 第二层执行检查），fail-closed，发布 `AuthorizationOutcome`。
10. **SandboxAuditMiddleware** — 命令审计。坑：`$(curl url)` 这种命令位置的命令替换会执行远端内容。对策：按**位置**（命令位 vs 值位）判定，而非仅看 `$(` 存在；对 `eval`/`-c`/`<<<` 等盲区做全命令匹配。明确「是纵深防御与审计，不是安全边界」。
11. **ReadBeforeWriteMiddleware** — 坑：盲写导致重复 append/覆盖。对策：`read_file` stamp 内容哈希；`write_file`（append/覆盖已有）/`str_replace` 默认被拦截，除非最新 mark 与该路径当前哈希一致。
12. **ToolProgressMiddleware** — 见 §9-2（结果质量状态机）。
13. **ToolReceiptMiddleware + ToolErrorHandlingMiddleware** — 坑：工具抛异常会让 run 中止；短路的 ToolMessage 会漏记 ledger。对策：异常转错误 ToolMessage；给每个结果 stamp `deerflow_tool_meta`（status/error_type/recoverable_by_model/recommended_next_action/source）；receipt 是 `wrap_tool_call` 最外层，避免被短路结果漏记。
14. **DynamicContextMiddleware** — 见 §5-4（前缀缓存 + 记忆注入）。
15. **SkillActivationMiddleware** — 见 §8-1。
16. **SkillToolPolicyMiddleware** — 见 §8-1。
17. **DurableContextMiddleware** — 坑：summarization 压缩后，委派摘要/技能引用丢失。对策：投影受任务委派摘要与技能引用到每次请求；静态规则用 SystemMessage，不可信字段（summary/skill 描述）用 HumanMessage 数据块。
18. **SummarizationMiddleware** — 见 §5-7。
19. **TodoListMiddleware** — Plan 模式，`write_todos` 任务追踪。
20. **TokenUsageMiddleware** — 记录 token 用量；子代理用量从终态 `ToolMessage.additional_kwargs` 按消息位置读取，避免重入重复累加。
21. **TitleMiddleware** — 首轮后自动生成标题。
22. **MemoryMiddleware** — 见 §5-1。
23. **ViewImageMiddleware** — 坑：图像 base64 写进 checkpoint 会在中断后残留重发。对策：只在 `wrap_model_call` 内临时 append，绝不进图状态；自带保留 ID 前缀 + 服务端标记用于自清扫。
24. **McpRoutingMiddleware** — 见 §8-2。
25. **DeferredToolFilterMiddleware** — 见 §8-2。
26. **SystemMessageCoalescingMiddleware** — 坑：vLLM/SGLang/Anthropic 拒绝非开头 SystemMessage。对策：所有 SystemMessage 合并为单个开头的 SystemMessage（仅 per-request payload，checkpoint 不变）。
27. **SubagentLimitMiddleware** — 见 §6-5。
28. **LoopDetectionMiddleware** — **死循环防护（调用模式层）**，见 §9-1。
29. **TokenBudgetMiddleware** — 预算硬停，见 §9-1。
30+. 自定义中间件（`custom_middlewares` 与 config-declared `extensions.middlewares`）→ **TerminalResponseMiddleware** → **ModelLengthFinishReasonMiddleware** → **SafetyFinishReasonMiddleware** → **ClarificationMiddleware**。

**SafetyFinishReasonMiddleware**（34）— 坑：provider 因安全原因终止（`content_filter`/`stop_reason=refusal`/Gemini SAFETY），但响应仍带残缺 tool_calls，LangChain 会把它当「去执行」，导致工具被截断参数执行、修复、再被过滤的循环。对策：检测器识别后**剥离 tool_calls**，保留真实 finish_reason；空内容则回填可见说明，避免严格 provider 拒收。

**ClarificationMiddleware**（35，必须最后）— 坑：歧义时盲目推进。对策：拦截 `ask_clarification`，写 `human_input` 卡并 `Command(goto=END)` 暂停等用户；丢弃同批 sibling 调用。**注意**：scheduled 非交互 run 会通过 `context.non_interactive` **丢弃**该工具（见 AGENTS.md）。

---

## 3. Sandbox 体系

### 3.1 契约层保证（`sandbox/sandbox.py`, `sandbox_provider.py`）

- **统一抽象**：`Sandbox` 声明 `execute_command/read_file/download_file/list_dir/write_file/glob/grep/update_file` + 两个可选 scope 钩子（默认透传，第三方 provider 仍合法）。
- **坑：请求级秘钥注入/拼入命令**。对策：`execute_command` 接受 per-call `env` dict，用 `^[A-Za-z_][A-Za-z0-9_]*$` 校验键名，从命令字符串中剥离秘钥。
- **坑：shell 状态污染证明**。对策：`persistent_shell_sessions` 三态——`None` 未声明 ⇒ 证据按 `UNVERIFIED` 处理；只有显式 `False` 才被信任为干净 shell。每个实现都要声明。
- **坑：沙箱越权/路径穿越**。对策：`download_file` 契约强制「穿越抛 `PermissionError`、读失败抛 `OSError`」，调用方只处理一种类型；底层重新抛时**保留原始路径**隐藏宿主真实路径。
- **坑：provider 单例在多线程被并发初始化/撕裂**。对策：`_provider_lock` 双层检查；**插件回调（`__init__/reset/shutdown/resolve_class`）在锁外执行**——否则非重入锁会自死锁；竞争时丢弃孤儿并 `shutdown`（#3721）。

### 3.2 沙箱中间件（`sandbox/middleware.py`）

- **坑：lazy init 状态对下游图步骤不可见**（channel reducer 不捡拾）。对策：`wrap_tool_call` 前后 diff sandbox id，通过 `Command(update=...)` 发布。
- **坑：共享沙箱的重复释放 / 跨 owner 释放**。对策：lease 管理器允许 lead/subagent 各自持本地租约，仅最后一个持有者停掉远端；`fork_restored` 状态明确不释放。
- **坑：checkpoint 里的旧沙箱在策略变更后存活**。对策：policy-scoped run 抢先获取，旧的共享视图沙箱无法经 checkpoint 存活。
- **鉴权门（RFC #4063）**：获取前后都跑 `authorize_sandbox_execution`；共享视图 run 被拒绝时延迟到 lazy gate，policy-scoped run 直接中止；拒绝走 `SandboxAuthorizationError` → 友好 ToolMessage，「sandbox not permitted for your role」。

### 3.3 隔离令牌（`sandbox/identity.py`）

- `derive_sandbox_scope_token(*, user_id, thread_id)` = `sha256("user:thread")` 截 16 位小写 hex。
- **坑：ID 碰撞跨租户串台**。对策：AIO/BoxLite 在截断 ID 已被别的租户占用时抛 `SandboxIdentityCollisionError`（fail-closed）。
- **坑：哈希是断裂迁移边界**——改分隔符/编码/大小写会孤儿化所有远端资源；从 8→16 位需一次冷启动。五个 provider 全走它。
- policy-scoped / custom-root 各有独立的沙箱 ID 域（`_policy_scoped_sandbox_id` / `_custom_root_sandbox_id`），杜绝「一个 skills root 建的容器被复用给另一个」。

### 3.4 各 Provider 的特定保证

**Local（`local/`，宿主 bash 风险最大）**
- `supports_agent_skill_isolation` **动态**：`not is_host_bash_allowed()`；配置不可读时 fail-closed 为 `False`（绝不把一个宿主进程 provider 意外变成隔离边界）。`is_host_bash_allowed` 只在「宿主本地 provider + `sandbox.allow_host_bash: true`」时才为 true。
- 路径包含：`os.path.commonpath` 对 `local_root` 校验，逃逸抛 `PermissionError`；只读 mount 在 `write_file/update_file` 强制。
- **坑：宿主路径泄漏到输出**。对策：反向前向解析返回 `/mnt/...`；修复了 Windows 上硬编码 `/` 匹配不到反斜杠嵌套路径导致泄漏原始宿主路径的 bug。
- **坑：秘钥泄漏**。对策：`build_sandbox_env` 从继承的 `os.environ` 剥离 `*KEY*/*SECRET*/*TOKEN*/*PASS*/*CREDENTIAL*/*DSN*` 及 `SSH_AUTH_SOCK`，再叠加请求秘钥（#3861）。
- 高基数线程路径 LRU 缓存 256；扫描直接走 per-thread root，不为每线程编译 regex（避免 Python 全局 regex 缓存驻留）。

**AIO（`community/aio_sandbox/`，容器+Docker/K8s）**
- `uses_thread_data_mounts` 默认按 backend 探测；`thread_data_mounts` 可显式覆盖。误设 true 会让上传对沙箱不可见。
- **坑：无主容器被同级收割**。对策：`_publish_ownership` **fail-closed**——不发布所有权就不交出容器；`_register_created_sandbox` 回滚刚创建的容器。
- **坑：Redis 多实例时，某 Redis 重启驱逐所有 live 沙箱**。对策：`_adoptable_after_grace` 要求无主容器跨完整 TTL 才可收养；`del:` 拆除状态用 `_held_teardown_lease` 续期。
- 事件循环纪律：`get()` 纯内存查找，绝不碰 store；阻塞步（文件锁/发现/所有权发布）下放 serializer executor。
- Lark CLI 配置目录**只读**挂载 + 内嵌可写 `config/locks`——防被攻破的 agent 篡改 `appSecret`。

**OpenSandbox（`community/opensandbox/`）**
- 热池：释放进入进程内 `_warm_pool`，仅同 scope 经 `ping()` 活性检查可回收。
- **坑：长命令缩短 renewal 时间窗**。对策：`renewal_timeout = max(sandbox_timeout, sdk_timeout+30s)`，`_operation_lock` 防止后续短文件操作覆盖长命令的时间窗。
- **坑：命令路径 404 与文件路径 404 语义不同**。对策：command 404（execd 没了）/410/不健康会话/传输断 → 终端失败，驱逐死客户端；file 404 仍是普通缺失文件。
- 路径穿越：`_resolve_path` 要求绝对路径，拒绝任何 `..`；下载限制在 `VIRTUAL_PATH_PREFIX`。

**BoxLite（微 VM，`boxlite/`）**
- async-native，box 是 loop-affine：provider 自持一个 daemon 线程私有 asyncio loop，`run_coroutine_threadsafe` 统一下放；sync 绿门拒绝在 async 环境内运行。
- **坑：挂起 VM 卡死 acquire 锁**。对策：健康检查 `exec(timeout=...)` + `.result(timeout)` 双重短超时。
- 下载前缀限制 + `wc -c` 先量再读（100MB 上限）。

**Tenki（云端微 VM，`tenki/`）**
- **坑：`/mnt` 为 root 所有、进程是非特权 tenki 用户**。对策：`_resolve_path` 把 `/mnt/user-data` 重映射到可写 HOME；bootstrap 用 `sudo -n` symlink（密码式 sudoers 快速失败而非阻塞）。
- `max_duration` 默认 4h（Tenki 本身约 30min 生命周期），避免对话中途丢状态。

**共享 WarmPool 生命周期（`community/warm_pool_lifecycle.py`）**
- `DEFAULT_IDLE_TIMEOUT=600`、`IDLE_CHECK_INTERVAL=60`、`DEFAULT_REPLICAS=3`；空闲到期淘汰、最老 warm 优先逐出。

### 3.5 获取序列化 / 阻塞 IO 约束（`sandbox/acquire_serialization.py`）

- **坑：锁表无限增长**。对策：`AcquireSerializer` 引用计数，`refs==0 and 未锁` 时 pop 条目。
- **坑：async 等待把事件循环/默认 executor 卡死**。对策：阻塞 `threading.Lock.acquire` 放到**有界专用 executor**（`min(32, cpu+4)`）。
- **坑：取消的 waiter 泄漏已获取的锁**。对策：`_AsyncAcquire` 做 race-safe 所有权交接；worker 自释放并清理，不依赖取消方事件循环跑 done-callback。
- **坑：worker 里 ContextVars 丢失**（`to_thread` 拷贝但 `run_in_executor` 不拷贝）。对策：`run_on_executor` 显式 `contextvars.copy_context()`，使 trace id 在 worker 内可读。

### 3.6 Mount 与 `/mnt/user-data` 引导

- 所有 provider 物化同一虚拟前缀 `/mnt/user-data/{workspace,uploads,outputs}`、`/mnt/skills`（物理 `users/{user_id}/threads/{thread_id}/user-data/...`）。
- 保留虚拟前缀 `/mnt/user-data`、`/mnt/acp-workspace` 对自定义 mount 强制；重复/冲突 mount 跳过，缺失宿主路径报 ERROR 并给 Docker 指引（#3244）。
- 不 bind-mount 的沙箱在 create 后 `mkdir -p` 引导，失败显式销毁远端。

### 3.7 路径穿越防护（tools 层纵深）

- tools 辅助层多重校验：读写 surface 拒绝穿越；写局限在 `VIRTUAL_PATH_PREFIX/`、skills、ACP、配置的 mount；`cd`/工作目录穿越拒绝；命令替换里的根绝对路径与裸 `/` 拒绝；bash 里 `file://` URL 拒绝。
- **Provisioner canonical-root 保留**：`_normalize_skills_container_path` 拒绝非绝对/非根、冗余 `.`/`..`，以及与平台 mount（`/mnt/user-data`、`/mnt/acp-workspace`、`/mnt/integrations/lark-cli`）重叠。

**明确边界**：LocalSandbox **不是**宿主文件系统安全边界；宿主 bash 开启时，`supports_agent_skill_isolation` 动态 fail-closed。

---

## 4. 模型适配层

### 4.1 各 Patch 存在的「为什么」（核心坑）

所有 reasoning 字段 patch 的**共同根因**：LangChain 基础 adapter 只序列化「标准」assistant 消息字段，静默丢弃 provider 特有字段；**要求这些字段在每条历史 assistant 消息上逐字回显**的 provider 会拒绝下一次请求。

| Adapter | Provider 怪癖 | 对策 |
|---|---|---|
| `PatchedChatDeepSeek` | 推理模型要求 `reasoning_content` 在**所有**历史 assistant 消息上；`ChatDeepSeek` 存进 `additional_kwargs` 却不再下发 | `_get_request_payload` 捕获转换前消息，`restore_assistant_payloads` 回灌 |
| `PatchedChatOpenAI` | Gemini 通过 OpenAI 兼容网关需要每个 tool-call 对象带 `thought_signature` 回显；base 只序列化 id/type/function | 按 id（退化按位置）匹配，复制 `thought_signature`/`thoughtSignature` |
| `PatchedChatMiMo` | 返回 `reasoning_content`，要求回放；多轮 tool-call 后 400 | 三处 override：request 回放 + 流式/非流式捕获 reasoning |
| `PatchedChatMiniMax` | ① 结构化 `reasoning_details`（`reasoning_split=true`）；② **要求每条 user 消息 `name` 一致**，否则 2013。DeerFlow 中间件给 user 消息打的 provenance name（`user-input`/`summary`/`loop_warning`）会导致不一致 | `extra_body["reasoning_split"]=True` + `_strip_user_message_names`（pop 所有 role==user 的 `name`） |
| `PatchedChatStepFun` | 返回 `reasoning`/`reasoning_content`（deepseek-style），base 忽略 | 流式+非流式捕获 + request 回灌 |
| `VllmChatModel` | vLLM 0.19 丢失非标准 `reasoning`，破坏交错 thinking/tool-call | 三条路径保存 `reasoning`/`reasoning_content` |
| `MindIEChatModel` | chat template 解析不了 LangChain 原生 tool_calls（0-token）、硬编码 XML tool_calls、stream+tools 丢 `choices`、过度转义换行 | `_fix_messages`（tool_calls → XML、tool 结果 HTML 转义**防逃逸**）、流式回退为全量生成+模拟流、解码转义换行、超时归一化 |
| `ClaudeChatModel` | Claude Code OAuth token 需 `Authorization: Bearer`（非 x-api-key）+ beta header + OAuth 仅 4 个 `cache_control` | 检测 OAuth、换 `auth_token`、禁用 prompt caching（OAuth）、重试退避 |
| `CodexChatModel` | Codex CLI OAuth 走 Responses API（非 chat）、要流式、拒收 `max_tokens` | 全自定义 `BaseChatModel`，[Responses 格式转换 + 流式合并 + 非法 tool 参数 → `invalid_tool_call` + 429/500 重试] |

**共享回灌机制**（`models/assistant_payload_replay.py`）：`restore_assistant_payloads` 用签名匹配 `(content, tool_call_ids)` + 位置回退扫描——因为序列化可能丢弃/重排消息，**纯位置 zip 在 provider 剥离某些消息时会崩**。每个 patch 只提供「要回灌哪个字段」。

**工具剥离原语**（`agent/middlewares/tool_call_metadata.py`）：`clone_ai_message_with_tool_calls` 在剥离 tool_calls 时同步 `additional_kwargs["tool_calls"]`，`function_call` 空则 pop，`finish_reason` **仅在 tool_calls→stop** 时改写——保证 Safety 兄弟中间件看到的是 provider **真实**的原因。

### 4.2 能力矩阵（thinking/vision/reasoning_effort）

- `supports_thinking`：不支持时 `thinking_enabled` 直接 `ValueError`。
- `supports_vision`：gating `ViewImageMiddleware`（只有声明了才注入 vision 工具/消息）。
- `supports_reasoning_effort`：false 时工厂从 kwargs 与 config **剥离** `reasoning_effort`。
- `when_thinking_enabled/disabled`：以 `extra_body` 注入；禁用路径有四条归一化优先级——显式 `when_thinking_disabled` → OpenAI 兼容网关（`extra_body.thinking.type: disabled` + `reasoning_effort: minimal`）→ vLLM/Qwen（`chat_template_kwargs.enable_thinking: False`）→ 原生 Anthropic（构造参数 `thinking: disabled`）。

### 4.3 工厂 `create_chat_model`（`models/factory.py`）

- `model_dump` 剥掉 `use/name/display_name/context_window/pricing` 等**展示性元数据**——否则未知 kwargs 被塞进 `model_kwargs`，在请求时才崩。
- `api_base→base_url` 别名、`_warn_unknown_model_settings` 走 build 期警告——因为 `ModelConfig` 是 `extra="allow"`，坏键 config 加载时不报，只在请求时经 OpenAI SDK「unexpected keyword argument」崩。
- **context_window → LangChain profile**：**构造后**合并进 `max_input_tokens`，而非经构造器传入（会**替换**掉整套推断的 profile：tool_calling/structured_output/io/output）。这是第三方 OpenAI 兼容模型能做 `summarization fraction trigger` 的原因（#3103）。
- `stream_usage` 默认 true（否则第三方 `base_url` 端点丢失流式用量）；`stream_chunk_timeout` 默认 **240s**（langchain 的 120s 对推理模型首 chunk 90-150s 太激进）。
- Codex pop `max_tokens`；MindIE 强制 `max_retries≤1`。
- `attach_tracing=False` 传参：已在 graph 根接线 tracing 的调用方必须传，否则 LLM 调用发重复 span、session/user 元数据被剥（模型变成嵌套观察）。
- **模型鉴权**：运行时 `_authorize_model_name` 校验 `model:use`；deny 时**扫描可见但通过 `use` 的候选**回退（自定义 provider 可能允许 `list` 但拒绝 `use`）。

### 4.4 错误处理（`llm_error_handling_middleware.py`）

- **重试分类**：`quota`/`auth` 不可重试；`burst_rate` 在通用 429 之前判；`IndexError` 当 transient（Volces 端点 `200 OK` 带空 `generations` 列表会 `list index out of range`）。
- **有效预算 = min(config 上限, 分class 覆盖, 分reason 覆盖)**——`StreamChunkTimeoutError` 上限 2、`burst_rate` 上限 2。
- **退避：AWS 式去相关抖动** `randint(base, min(cap, max(base, seed*3)))`，尊重 provider `Retry-After`；同步/异步睡眠都经进程级 limiter（**backoff 睡眠释放并发槽**）。
- **熔断**：closed→open→half-open 状态机；**burst-rate 失败释放探测但不记 fail**（避免 slope 限流把自己熔断，自己制造故障，#4290）。
- **进程级并发上限**：用 threading 原语（`asyncio.Semaphore` 绑定单一 loop，无法覆盖 lead+subagent+sync）**启动即冻结**。
- **优雅收尾**：不可重试/耗尽的失败返回可读 `AIMessage` + 观测字段；`GraphBubbleUp`（暂停/恢复）原样 re-raise。

### 4.5 Safety 终止检测器（`safety_termination_detectors.py`）

- 三个检测器都从 `response_metadata` **和** `additional_kwargs` 读（LangChain adapter 放哪不一致）；只接受字符串值防 Pydantic enum/dict 崩溃。
- OpenAI `content_filter`（可扩展 `finish_reasons=` 支持中文 provider 的 `sensitive`）；Anthropic `stop_reason=refusal`；Gemini `SAFETY/BLOCKLIST/PROHIBITED_CONTENT/SPII/RECITATION/IMAGE_*`——**故意排除** `MAX_TOKENS`（长度，非安全）、`LANGUAGE/NO_IMAGE`（能力不匹配）、`MALFORMED_FUNCTION_CALL/UNEXPECTED_TOOL_CALL`（协议错误，另归类）。
- 检测器异常被吞（bug 不破坏 run）；`from_config` 拒绝**显式空列表**（静默禁用是最大风险）。

### 4.6 Token 用量元数据

- `stream_usage=True` 默认（第三方便丢失流式用量）；vLLM `cumulative_stream_usage` 把累计快照转 per-chunk 增量（仅当有稳定 completion id、按 id 隔离交织流、软上限 1024）；Codex/MiniMax 各有自己的 `usage_metadata` 构建。
- `TokenUsageMiddleware`：子代理终态用量从当前 run 的 ToolMessage 读并 stamp `subagent_token_usage_attributed=true`，checkpoint 重放不能重复累加。

---

## 5. Memory 子系统

### 5.1 提取过滤（`backends/deermem/core/message_processing.py`）

- 只取 `human` 与 **无 tool_calls 的 `ai`**（工具调用片段不等于事实）；要求至少一个 human + 一个 assistant 才入队。
- **坑：框架内部消息污染记忆 / 自放大循环**。对策：跳过 `hide_from_ui` 的中间件消息（Todo 提醒、ViewImage、`__memory` 自我保护块）；例外：用户真实回答（`human_input_response`）。
- **坑：纯确认无意义对话批量触发 LLM**。对策：`filter_trivial` 用 `fullmatch` 丢「ok/好的/谢谢」及其后续 AI 回复；含实质内容的不丢。

### 5.2 队列与去重（`core/queue.py`）

- 内嵌去抖队列，按 `(thread_id, user_id, agent_name)` 键**合并**（同一线程多次 turn collapse 成一个 pending）。
- **背压**：`queue_max_depth` 后只拒绝普通更新；**signal 与 emergency（bypass）永远准入**——「重要记忆永不丢弃」。
- **坑：summarization flush 覆盖普通更新尾部**。对策：bypass 的 match key 带 `bypass_watermark`，不会顶掉正常 pending。
- **坑：过期 timer 复活已删 agent**。对策：`cancel_by_agent` 丢弃已清作用域的 pending。
- **坑：滚动发布丢队列**。对策：`flush_sync` 在 drain 前 join 已运行的 worker；`shutdown_flush`。
- **坑：失败更新被跳过**。对策：watermark 不前进，下轮重试。

### 5.3 DeerMem 淘汰（`core/eviction.py`, `updater.py`）

- `select_facts_for_capacity` 一个函数、两个策略：`confidence`（默认，完全保留历史排序）与 `hybrid-v1`（`confidence*0.65 + confirmation*0.25 + access*0.10`，各有半衰期）。
- **坑：纯置信度会逐出刚被用户重申/修正但置信度低的事实**（#4641 缺陷，#4789 修复）。对策：hybrid 加权 + **correction 保留名额**（`ceil(max_facts*0.10)` 给 `category=="correction"`）。
- **坑：非有限/越界 confidence 崩溃或主导**。对策：`_bounded_number`、`_coerce_source_confidence`。
- **坑：逐出刚加的事实却报「已存」**。对策：cap 逐出时 `create_fact` 返回 `fact_id=None`，调用方报「cap reached」。
- **意义**：`max_facts`（默认 100）防上下文膨胀——无限事实会淹没 `<memory>` 注入块与每次提取的 prompt。
- 一个策略贯穿**所有写路径**（自动提取/手动 create/import）；shadow mode 只算不生效，供观测。

### 5.4 DynamicContextMiddleware（前缀缓存 + 记忆注入）

- **系统 prompt 完全静态**（最大前缀缓存复用）；memory/日期以 `<system-reminder>` **一次性**注入，之后冻结首条消息（同 ID）——每条新 turn 前缀可命中缓存。
- **坑：用户可影响内容不能获得 system 权威（OWASP LLM01）**。对策：memory 用 **HumanMessage**（不是 SystemMessage），ID-swap `{id}__memory` / `{id}__user`。
- **坑：晚注入污染旧 prompt**（把记忆 append 到**上一条**用户消息会把旧首条提前，模型答错 turn）。对策：ID-swap 使 reminder 附着到**最新**用户消息。
- **坑：依赖 regex 扫记忆判断日期变化不可靠**。对策：`reminder_date` 存 `additional_kwargs`，结构权威检测；跨零点另发轻量 SystemMessage。
- 注入限时 5s 下放线程，tiktoken 冷下载时优雅降级。

### 5.5 Prompt 设计与确定性门

- prompt 强制每条事实/摘要带 `scope/durability/authority`，只有 `scope=user, durability=durable, authority=descriptive` 可入库；`authority=transactional`（指令/授权/许可）永不变长期记忆。
- **确定性门**（不止 prompt 防模型滑边）：`_fact_scope_gate_reason`/`_summary_scope_gate_reason`/`_removal_scope_gate_reason` 拒收；置信度阈值；按内容规范化键去重（在 read-check-write 临界区内做，防并发重复存同事实）。
- **坑：HTML 标签逃逸（prompt 注入）**。对策：记忆值/事实/总结/会话 turn 在嵌入 `<current_memory>`/`<memory>`/`<conversation>` 前全部 HTML 转义，长消息 head+tail 交替截断。
- **坑：token 预算溢出时截掉连操作者想保的事实**。对策：`format_memory_for_injection` 结构感知截断，Facts 块作受保护后缀，`guaranteed_categories` 免逐出。
- **坑：移除操作误删用户记忆**。对策：task/project 级矛盾不能删用户记忆；成对移除要求其替代品过所有门并在 trim 后仍在。

### 5.6 上传过滤

三层独立剔除：提取时剥 `<current_uploads>` 块（只有上传块的无实 human turn 丢弃 + skip 后续 AI）；conversation 格式化时剥标签；持久化后 `_strip_upload_mentions_from_memory` 剥描述上传**事件**的句子（刻意窄——"用户会处理 CSV 文件"这种合法事实保留）。
- **坑**：「上传是 session 级，持久化上传事件会让 agent 未来去找不存在的文件」。

### 5.7 记忆 ↔ 总结（压缩不掉已取的事实）

- **坑：总结掉的 turn 进不了记忆**。对策：`SummarizationMiddleware.compact_state` 只在**替换 summary 存在后、消息移除前**触发 pre-summarization 钩子（summary 无法物化则重复提取双倍）。`memory_flush_hook` → `add_nowait` → **bypass_watermark 路径**（一次性快照，不读/不推进 conversation watermark，避免子集长度倒推造成漏取）。
- **坑：压缩删掉已注入的 `<memory>` reminder**。对策：`_preserve_dynamic_context_reminders` 按 tag 救回日期 SystemMessage + `__memory` peer + 最新真实用户请求；未 tagging 的 `__user` peer 不救（让历史 prompt 仍可压缩）。

---

## 6. Subagents

### 6.1 背景执行（`subagents/executor.py`）

- 单一长活 daemon 线程事件循环 `_isolated_subagent_loop`（避免 per-execution 建/关 loop 而悬挂持有 loop 资源的 async client）。
- **坑：`asyncio.create_task` 在短命调用方 loop 上会被取消**（如 sync `asyncio.run` 包装）。对策：持久 loop + `run_on_isolated_subagent_loop`。
- 注册 PENDING 在拷贝 parent Context **之前**；提交失败回滚刚注册的条目（防 PENDING 僵尸）。`_submit_to_isolated_loop_in_context` 在构造 coroutine 前解析 loop，rejected coroutine `.close()` 防「never awaited」。

### 6.2 身份拆分（核心坑）

```
provider tool_call_id   → SubagentResult.external_task_id   ← ToolMessage/task_* SSE/card/ExtensionData.scope_id 关联
server execution_id     → SubagentResult.task_id             ← 进程内 registry/polling/cancel/timeout/cleanup
```
- **坑：provider tool_call_id 跨父 run 会重复**，不能做 registry 所有权键——否则一个 run 的 cancel/cleanup 影响另一个。对策：server 生成 uuid 做注册表键；executor 闭包保留自己的 `SubagentResult` 而非透过可变 registry 重新解析所有权（race 安全）；token 用量从**当前 run 的 ToolMessage** 归属，绝不经进程级 provider-id 缓存。
- `task_id`（`SubagentConfig`）是**子代理名**键，与 run 标识无关。

### 6.3 Registry（`subagents/registry.py`）

- 解析顺序：built-in → config `custom_agents` → 启用的 admin-managed → per-agent `agents.<name>` 覆盖；managed 与 built-in/config 冲突时**退出运行时发现**（operator 优先级高于持久化集）。
- `max_turns`/`timeout` 分层：per-agent 覆盖 > 全局默认（仅 built-in）> config 自身值；全局默认**不**覆盖 custom agent 自己的值。
- `get_available_subagent_names` 在宿主 bash 禁用时隐藏 `bash`。

### 6.4 隔离

- 每个被接纳的子代理带 `sandbox_lease_owner_id = f"subagent:{task_id}"`，装进 runtime context；沙箱中间件保留该 execution 于 lead 线程的活动 provider client，**一个子代理结束不能关闭沙箱**而兄弟还在跑（#5128）。AIO 的 command scope 选出一个显式持久 shell 会话——独立 scope 并发、子代理内保持 shell 状态有序。release 在 `finally` 幂等重复（异常/协作取消/超时都不泄漏租约）。
- **坑：把整个 RunJournal 传入子代理事件循环**。对策：只有窄代理 `_ParentLoopMiddlewareRecorderProxy` 跨 loop——`record_middleware` 经 `call_soon_threadsafe` 调回 owner loop 追加，loop 已关静默丢弃；`aclose` 用 `yield` 围住迟到的子事件。**让 record hook 成为唯一跨界的东西**。

### 6.5 限额（`subagent_limit_middleware.py`, `capacity.py`）

- **坑：长 lead run 在每个 checkpoint 反复派发合法批量绕过并发**。对策：`max_total_per_run` 数**durable delegation ledger 里带当前 run_id 的条目**；`allowed = min(max_concurrent, max_total - prior)`；耗尽时 stamp `stop_reason=subagent_limit_capped` 并附加可见 `[SUBAGENT LIMIT REACHED]`。
- 并发解析为一次 min(per-run 请求, 启动冻结的 `max_running`, 安全上界 1–64)。
- `SubagentExecutionCapacity` FIFO：`reject` 或队列满 → 拒绝；否则 enqueue + `wait_for(timeout=queue_timeout_seconds)`；**queued 从不占线程**。

### 6.6 stop_reason（加法字段，不用新枚举）

- 三条轴提前结束，都汇入**加法 `stop_reason`**（非新 status，避免破坏 v1 消费方）：`turn_capped`（`recursion_limit==max_turns` 抛 `GraphRecursionError`）、`token_capped`（TokenBudget 剥 tool_calls 强制终答不抛）、`loop_capped`（LoopDetection 同）。
- `_consume_guard_stop_reason` duck-typed 遍历所有 `hasattr(mw, "consume_stop_reason")` 的中间件；`SubagentResult.stop_reason` 经 `status_contract.py` 渲染「Task Succeeded (capped: ...)」。
- **坑：max_turns 时部分产出被丢**。对策：可用部分恢复为 `completed + stop_reason`；不可用才 `failed + stop_reason`。
- 为什么加法不枚举：`status_contract.py` —— 新 status 会破坏 v1 消费方；可选字段被旧前端忽略，保持跨语言契约向后兼容。

### 6.7 内建/自定义 agent

- `general-purpose`：`tools=None`（继承全部），`disallowed_tools=["task","ask_clarification","present_files"]`（防嵌套/递归/澄清），`max_turns=150`。
- `bash`：仅沙箱工具，同样禁 `task/ask_clarification/present_files`，`max_turns=60`。
- 自定义（`custom_agents`）：`description/system_prompt/tools(disallowed_tools)/skills/model(max_turns/timeout_seconds)`，`disallowed_tools` 默认同内建。

### 6.8 已解决的坑（子代理侧）

- `GraphRecursionError` 被掩蔽/部分失败：恢复部分流式成果（`_extract_final_result`），区分「LLM fallback 标记」与「真部分输出」（`_extract_llm_error_fallback`，只扫尾部因子代理共享 parent thread_id、LangGraph 重放全历史）。
- 重复 `stream_mode="values"` 重放 O(n²)：seen-id 集合 + 尾游标 O(1)。
- 多 tool-call turn 丢掉了除最后一个之外的所有 ToolMessage：`capture_new_step_messages` 走新 append 尾部而非 `messages[-1]`。
- 总结压缩重置 step-capture 游标。
- 终态竞态（后台 timeout vs worker）：首个终态转移胜（`try_set_terminal`），receipt/bash 证据在终结前同锁提交。
- 沙箱租约在 cancel/timeout/异常时泄漏。

---

## 7. Runtime

### 7.1 run 生命周期（`runtime/runs/manager.py`）

- **坑：本地冲突在 store 插入成功后失败 → 泄漏 pending 行**。对策：`_admit_thread_operation` 把本地 inflight 检查、DB 持久化、内存注册放**同一把 `asyncio.Lock`** 临界区（锁序不变量：store 插入不可能在本地 ConflictError 将触发时成功）。
- `multitask_strategy=reject|interrupt|rollback`：checkpoint 写 inflight 时 interrupt/rollback 也返回失败；interrupt/rollback 的 claim+insert 在同一事务，`IntegrityError` 重试 3 次（防 worker 在 SELECT FOR UPDATE 与 INSERT 之间插队）。
- **幂等键**：内存扫 `idempotency_key` 命中返回已有记录并标记 `idempotency_reused`（复用调用方**不**挂第二个 worker）；DB 层 `uq_runs_idempotency_key` 唯一索引 + `reuse_idempotent_run`。
- `try_start` 启动屏障：serialize 被多个 worker 路径认领的 run；检查 abort/status 两次。
- **同日/后台调度**：`wait_for_prior_finalizing`、`has_later_run`、`has_later_started_run`。
- cancel 路由（local vs peer）+ 租约接管：错过心跳 → `claim_for_takeover` 标 `error`；有效租约 → 持久 `request_cancel`。

### 7.2 run worker（`worker.py`）

- **坑：rollback/interrupt 的收尾**。对策：`_finish_cancellation`——rollback 标 `error "Rolled back by user"` 并**恢复 run 前 checkpoint**；interrupt 标 `interrupted`；都先置 `finalizing=True` 让同线程替换阻塞。
- **坑：中断首轮跑没标题**。对策：`_ensure_interrupted_title` 只在「无更早 finalizing 且无更晚 started run」时写；幂等（checkpoint 已有标题则短路）；3 次重试 stale-snapshot CAS。
- **坑：终态 run 比它的 receipt 后死（崩溃窗口）**。对策：flush journal → 持久化幂等 `run.delivery` receipt → **之后**才持久化终态。
- **stop_reason**：worker 读 `runtime.context["stop_reason"]`（`loop_capped/token_capped/safety_capped/subagent_limit_capped/model_length_capped`）并透传 set_status；`_orphan_recovery_observed_after_heartbeat` 用它作为心跳后合成 END 的信号。
- **坑：编辑回放/增量恢复的 checkpoint 竞态**。对策：edit-replay 非成功时恢复 run 前 checkpoint；`_linearize_delta_checkpoint_resume` 把 delta fork 转线性 head 写（共享 parent 的 pending_writes 会重放被抛弃 sibling 的答案，#4458）。
- **坑：上一次 run 的 LLM error-fallback 污染下次**。对策：`_extract_llm_error_fallback_message` 忽略 `pre_existing_message_ids`。

### 7.3 StreamBridge（SSE）

- `StreamEvent` 带单调 `id` 作 SSE `id:`（`Last-Event-ID` 重连）。
- **坑（#4399）：子代理的 `values` 快照以 bare `values` 发布，替换了 SDK 客户的整线程视图；其 token 块刷屏父消息流**。对策：`_publish_stream_item` 对命名空间帧提前 return（在 root-only 消费者之前），只发 `values|<ns>`；subgraph 事件用 `mode|ns1|ns2` 命名。
- **坑：文件体每个 token 流式，浏览器重复解析增长 JSON（O(n²)）**。对策：`_LargeFileToolChunkBatcher` 只对 `str_replace/write_file` 批 32 个、只在客户也订阅 `values` 时启用、只在 root 帧上 flush——messages-only 消费者保持原 per-chunk 契约。
- **坑：Redis XREAD 不能参与 pipeline / 保留下水位**。对策：`_read_retained_snapshot` 原子非阻塞 xrange/xrevrange/xread；游标低于保留下水位 → StreamGap；race-proof `Last-Event-ID`。

### 7.4 RunJournal（审计）

- **坑：并发 fire-and-forget flush 争同一 SQLite 文件**。对策：`_flush_sync` 若已有 flush in-flight 跳过；失败批前端重排队；仅在成功写时 bump `feed_generation`（读侧信号）。
- **坑：中间件自己回答了 tool call（short-circuit）→ on_tool_end 不触发 → reload 丢结果（#4666）**。对策：`_reconcile_final_tool_messages` 三条件界定（用户可见、属于本 run 的 lead agent、尚未持久化）。
- **坑：把用户数据落盘**。对策：`record_memory_context` 只存 `content_sha256`；`middleware:loop_detection` 不持久化工具参数/上下文/消息/哈希；hidden human 只允许 `ask_clarification`；trace category 截断；metadata 秘钥 redact。
- **坑：checkpoint 分支重放丢弃整支**。对策：`build_checkpoint_history_seed_events`/`build_branch_history_seed_events` 按 turn `run_id={prefix}-{n}` 分组，重生成一条继承答案只丢该 turn（#4458）。

### 7.5 checkpoint 竞态 / `reserve_checkpoint_write`

- `reserve_checkpoint_write` = `goal_thread_lock` + `run_manager.reserve_thread_operation(kind=checkpoint_write)`——创建短命持久 pending 行，用**同一部分唯一索引**同时关闭 run-admission 与跨 worker checkpoint 写竞态。用于手动压缩、`POST /threads/{id}/state`、goal 变更路由。
- 部分唯一索引 `uq_runs_thread_active`：`thread_id UNIQUE WHERE status IN ('pending','running')`，在 ORM `__table_args__` 定义（空库 create_all 也拿到）;migration 先取消重复再建索引。
- per-thread worker checkpoint 锁 `_checkpoint_thread_lock` + `persist_run_history_metadata` 3 次重试 CAS（读后写前检查 checkpoint 身份）。

### 7.6 recursion-limit clamp

- **坑：客户端给任意大 recursion_limit → 单一 run 无限 super-step → 成本失控/DoS**。对策：`_clamp_recursion_limit` 拒绝非整/bool/非正（回退默认 100），有效值 clamp 到 `max_recursion_limit`（默认 1000）；在 client-config 透传**之后**应用（覆盖客户端所发）。调度器在 dispatch 前先预 clamp，`build_run_config` 再 clamp（纵深）。

### 7.7 run_events DB backend + run_ownership heartbeat

- **坑：`max(seq)` 与 INSERT 交错 → seq 冲突**。对策：Postgres `pg_advisory_xact_lock(hashtext(thread_id))`（`SELECT max() FOR UPDATE` 无法锁聚合），其余方言 `with_for_update()`；`put_if_absent` 同一 advisory lock（幂等终态 receipt 路径）。`uq_events_thread_seq` 最后防线。
- **坑：两 worker 都以为拥有同一 run**。对策：heartbeat 每 `lease_seconds/3` 续租，仅在成功持久续租后推进 `lease_expires_at`（deadline fail-closed）；`_mark_ownership_lost` fence 本 worker；`reconcile_orphaned_inflight_runs` 用 `claim_for_takeover` 接管过期租约，**claim 原子胜出后**才写 zero-delivery receipt。

### 7.8 调度器多实例（`scheduler.multi_instance`）

- **坑：不安全组合**。对策：启动门拒绝——`multi_instance=true` 要求 Postgres + `run_events.backend=db` + `run_ownership.heartbeat_enabled=true` 否则 `SystemExit`；`GATEWAY_WORKERS>1` 但未开 multi-instance 也拒绝（每个 worker 各起一个 scheduler）。
- lease-aware 恢复 vs 单实例 `recover_expired_launch_claims`；共享 Postgres advisory budget 使 `max_concurrent_runs` 成为全局 cap。
- 调度器分发走**正常 run 路径**（`launch_scheduled_thread_run` + 单活跃 occurrence 唯一索引 `uq_scheduled_task_run_active`）。

### 7.9 关停 drain

- `RunManager.shutdown` 用 per-thread `abort_event` fence 运行，**在 checkpointer 池关闭前** drain（PoolClosed workaround，#3373），只对未自行落定的 run 标 `interrupted`；drain 有界于 `timeout`（信号重入死锁的前提）。

---

## 8. Skills / MCP / Config / 扩展

### 8.1 Skills

- **坑：嵌套目录的 SKILL.md 被外层覆盖**。对策：`skills_by_name` 仅按解析名建键，嵌套 SKILL.md 永不注册（目录是包边界）。
- **坑：default-vs-explicit enabled 状态**。对策：解析默认 enabled，每次加载从 extensions_config 真值再合并（CUSTOM 未配置默认启；PUBLIC/LEGACY/INTEGRATION 按类别门控）。
- **坑：畸形 frontmatter 杀死整个加载**。对策：`parse_skill_file` 宽 exception → None；缺字段/非字符串 → None；name/description strip 后复检；YAML 错误行内提示。
- **坑：模型 regex 崩**。对策：deferred-discovery catalog 的 regex 降级为字面量，`MAX_RESULTS=5`。
- **坑：UTF-8**——所有读 `encoding="utf-8"`；canonical_hash/sha256 按 utf-8 编码。
- **坑：operator 管理的外部树被穿透**。对策：一级目录 symlink 允许，symlinked SKILL.md 或更深逃逸拒绝。
- **SkillActivation**：严格 `/skill-name task`，匹配失败直接返回 `AIMessage(content=...)`（模型看到原因而非陷入重试循环）；只激活**最新**真实用户消息；同一 slash 一次激活后写 run-key 防每模型调用重复读盘。隐藏上下文只在 per-call `request.override` 注入（`hide_from_ui`，稳定 `__slash_activation` id），**绝不进 checkpoint**；body XML 转义防技能自身注入标签。
- **SkillToolPolicy（关键安全边界语义）**：启用技能 ≠ 授权（`_active_policy` 无 slash 且无 skill_context 时返回空 → 基线工具集不动）；读第二个技能**不能**扩大 slash 技能的权限（slash 优先且立即返回）；`skill_context` 无法作为 widen 路径（策略是**差值**），`allowed_tool_names_for_skills` 只在**无任何**技能声明 `allowed-tools` 时返回 None（legacy allow-all），一旦有声明即为上限；`task` **不**框架豁免（受限技能不能绕政策委派）；`tool_search` 结果里被拒名被移除 schema。
- **坑：被拒技能绕过**。对策：fail-closed——registry 加载失败或活跃引用无可用技能 → 退到 framework-safe-only；按**规范容器文件路径**解析（非 name/存储路径），防 confused deputy（custom 技能可 shadow 同名 public）。
- **坑：审阅目标不能激活**。对策：`review_skill_package` 标 `review_subject_entry` 而非 `skill_context_entry`，不绑定 required-secrets、不应用 allowed-tools；再审阅内容 tag-neutralized，全文在 ToolMessage.artifact，模型可见的是紧凑 JSON；本地目标限 cwd/tmp/skills root 且必须是 `.skill` 包或含根 SKILL.md 的目录；语义工件 80k 上限。
- **skill-creator**：每次 create/edit/patch/write 都跑 `validate_skill_markdown_content` + 静态扫描 fail-closed 阻断（CRITICAL）；SkillScan 原生确定性扫描器纯 sync 函数，`enforce_static_scan` 按 `skill_scan.enabled` kill-switch。
- **config**：`skills.path` 解析顺序（显式 → env → project-root `/skills`（if dir）→ legacy → 稳定默认），`container_path` **restart-only**（reload_boundary 标出）；enabled 列表在 extensions_config.json 的 `skills` map。

### 8.2 MCP

- **坑：同秒编辑、回退 mtime（git checkout/cp -p/tar/object-store）、mtime 相等或更老的 config 切换都不可见**。对策：`get_cached_mcp_tools` 用 `(mtime,size)` **签名 + 内容签名差值** 探测陈旧，而非 `mtime >`。
- **坑：删除显式 config 路径让热路径 crash**。对策：`_resolve_config_path` 只对那座特定的 `FileNotFoundError` 按「unconfigured」fail-soft（last-known-good），真实调用方仍 loud。
- per-server `tool_name_prefix` 防碰撞；但 routing/session scope 按产生 server+transport 而非可见前缀。
- **Durable task runtime**：`McpTaskRow` 持久身份/租约/恢复/幂等字段；**陈旧结果不可写**——只在 worker 持有未过期租约时应用状态，过期/取消后即使 owner-token 匹配也丢弃；`mcp_task_session_scope_key = user_id:thread_id` 跨 run 边界保持 session 作用域——持久 `submit` 在 Agent run 内 inline 等待（秘钥仍可达），status/cancel 在 run 后。
- **DeferredToolFilter + McpRouting + tool_search**：延迟 MCP schema 隐藏于 `bind_tools`，ToolNode 仍持有用于路由；**坑：陈旧持久化 promotion 暴露漂移工具**——promotion 只在 `promoted["catalog_hash"] == _catalog_hash` 时读；未促销调用被阻止并提示先 `tool_search`；McpRouting 只写最小 `promoted` 更新、匹配 `casefold` 文本、按 `auto_promote_top_k` clamp 1–5；`assert_mcp_routing_before_deferred_filter` 保证顺序（routing 在 filter 之前）。

### 8.3 Config / 持久化 / 反射 / 扩展

- **config 系统**：`config_version` 欠账检测 + `make config-upgrade` 指引；解析顺序（显式 → `DEER_FLOW_CONFIG_PATH` → 项目 config.yaml → legacy）；`$VAR` 递归引用（config.yaml 缺失变量**提升**、extensions_config 存 `""` 占位防字面 `$VAR` 到 MCP server）；`_drop_null_config_sections` 把 present-but-null（如 `models:`）按缺省处理（`cp config.example.yaml config.yaml` 首跑不崩）。
- **config.yaml vs extensions_config.json 拆分**：`plugins`（config.yaml，仅启动，加载任意扩展包）是**不同信任层**，刻意放在 API 可写文件之外；`extensions`（MCP server + skills + config-declared middleware，HTTP 可写的 json）；合并单项：config.yaml 的扩展字段优先，但 json 是运行时源；`extensions.middlewares` 只信操作员，**不得**有 API 写路径。
- **reload 边界**：`config/reload_boundary.py` 单一事实源，`STARTUP_ONLY_PREFIX` 注入 Pydantic `Field(description=...)`（IDE hover 可见），双向漂移测试钉死两侧；故意不在 `database`/`postgres_schema` 热改时重置 checkpointer/store（ORM engine 启动建一次，永不重建）。
- **BoundedDict / LRU**：`BoundedDict(OrderedDict)` 默认 maxsize 1000，被 TokenBudget 与 LoopDetection 共享以相同方式 cap 长活中间件状态。MCP tools 缓存进程全局、stale 即整体 reset；`skill_manage_tool` 的 per-(user,skill) asyncio locks 用 `WeakValueDictionary` 防累积。
- **反射**（`resolve_class`/`resolve_variable`）：`module:Class` 单一语法；`:` 缺失/rtrim 失败 → 带示例的 `ImportError`；坏模块 → 带 `uv add`/`pip install` 提示；错类型 → `ValueError`；`resolve_class` 双重校验是类且是 base 子类。configured extension middleware 解析失败在 agent 创建时 loud 失败（不退化为空工具集）。
- **扩展顺序（为何顺序即正确性）**：`assert_ordering` 唯一硬失败——错误顺序「产生错误行为而无报错」。约束全在 `wrap_tool_call` 嵌套：ToolProgress outer of ToolErrorHandling；ToolReceipt **最外**（在 stamping step 与**每个 short-circuiter** 之上，short-circuit ToolMessage 不空窗 receipt ledger）。`compose_with_extensions` 在**最外层 builder 末尾**一次注入（18 个 lead 专属中间件都在 base 之后且是它的 inner，提前注入会把扩展排到它们上面）。`IsolatedMiddleware` 解包使包裹者按其内层类型比较；`core_ordering_constraints` `@cache` 惰性 import 避循环。
- **harness/app 边界**：`test_harness_boundary.py` 静态 AST 扫描 `packages/harness/deerflow` 下所有 `.py`，出现 `import app.`/`from app.` 即失败——harness 可发布/独立，绝不依赖 app 层（方向 App→Harness allowed）。这使如 `skills/AGENTS.md` 禁止 review core 导入 `app.*` 或执行目标脚本。
- **migrations / 版本 lockstep / 校验**：ORM 变更必配 alembic revision；Gateway 启动 `alembic upgrade head`；legacy 分支的 `create_all` 限于 `_BASELINE_TABLE_NAMES` 且被 guard test 钉住；`safe_add_column/safe_drop_column` 幂等；extension 自有表经 `include_object` + `register_configured_extension_table_prefixes`（读 config.yaml，不 import 扩展代码）**排除在 host alembic 视图外**。版本 lockstep：`backend/pyproject.toml` ↔ `frontend/package.json` ↔ `deploy/helm/.../Chart.yaml`（`verify_versions.sh` 阻断）；harness 把 `deerflow_extension_api` 契约版本精确 pin 到 host↔extension 兼容。
- **验收检查（anti-self-report 门）**：`subagents/acceptance_checks.py`——可判定的判据（`file exists/non-empty`、`file_written`、`tests_passed`）**在代码里对照记录的证据**检查，其他一律 `UNVERIFIED`，绝不静默通过；措辞刻意用 `checked/holds` 把「通过/已验证」留给运行时硬门；持久化 verdict 读回时结构再校验。

---

## 9. 跨切面设计模式

这些是从全部机制中提炼出的**可复用模式**——任何 agent 框架都会撞上同样的坑。

### 9.1 终止性：三层护栏

> 本节讲的是**终止**（把失控的 run 掐断）。真正"把 agent 拉回正轨再继续"的**纠偏**在 §9.7，侧重点是「脱主线」与「卡住不动」两大类，两者不可混为一谈。初学者请先读 §9.7.0 分清「跑题 vs 卡住」再回头读本节。

| 层 | 键 | 触发 | 行为 |
|---|---|---|---|
| 调用模式 | `LoopDetectionMiddleware` | 每次模型响应后哈希 tool_calls | warn 注入 `[LOOP DETECTED]`（HumanMessage，放在所有 ToolMessage 之后保 pairing）；hard_limit → 剥离 tool_calls 强制终答，`stop_reason=loop_capped` |
| 结果质量 | `ToolProgressMiddleware` | 工具执行后 | `(thread,tool)` 状态机 ACTIVE→WARNED→BLOCKED；Jaccard 词集判「重复返回同一内容」；只封该工具；`recoverable_by_model` 决定 WARNED 是否终态 |
| 预算 | `TokenBudgetMiddleware` | 每 run token | warn 注入，`hard_stop_threshold` 剥 tool_calls 强制终答，`token_capped` |

**分工**：LoopDetection 是「调用模式」护栏（模型响应后、工具执行前，看 AIMessage 的 tool_calls 签名）；ToolProgress 是「结果质量」护栏（工具执行后、看返回内容）；两者互补不竞争，可同时注入 hint；LoopDetection 硬停时不发 wrap_tool_call，ToolProgress 不触发，无双停。跨 run 作用域**刻意相反**：LoopDetection 保留 history（调用模式时间不变），ToolProgress 每次 run 清空（rate_limited/transient 是 time-bound，残留计数会误封）。

**坑（已解）**：不能把框架词插进 AIMessage（会污染下游 Memory 消费者），也不能在 tool_calls 与其响应之间插消息（OpenAI/Moonshot 拒收、Anthropic 禁 mid-stream SystemMessage）——所以警告在 `wrap_model_call` 注入、排在所有 ToolMessage 之后。硬停**不 raise**——抛异常会让子代理看到 raw 崩溃；改为剥离 tool_calls + 自然终止，并写 `consume_stop_reason` 供调用方区分「loop-capped completion」与「干净完成」。

### 9.2 「回归头/写头读头」一致性：凡是「先读状态再改写」都要防竞态

- 记忆去重在 read-check-write 临界区；capacity 淘汰用同一策略贯穿所有写路径；`create_fact` cap 逐出返回 `fact_id=None`。
- `persist_run_history_metadata` 3 重试 CAS——写前 re-read head，checkpoint 身份变了就拒写。
- `_linearize_delta_checkpoint_resume` 把 delta fork 转线性 head。
- 调度器恢复必须**reconstruct run_id/started_at/error state** 后才释放短 launch claim；launch/failure/timeout bookkeeping 在**parent-first 事务**里改 occurrence 与 parent task，防 peer 在两次写之间认领已释放的 task。

### 9.3 「要么一次性、要么可重试」：幂等与失败收敛

- run admission 用持久部分唯一索引 + 局部唯一约束，**fail-closed**（拿不到所有权就不交）。
- MCP task `put_if_absent` 同一 advisory lock；`uq_events_thread_seq` 唯一约束兜底。
- 调度器单活跃 occurrence 索引 `uq_scheduled_task_run_active`；重复触发器 coalesce 到同一 active 行。
- 终态 receipt 先于终态持久化（防「终态 run 比 receipt 后死」）；首个终态转移胜（`try_set_terminal`，防后台 timeout vs worker 竞态）。
- 删除/覆盖前**先读目标**、报告结果要如实（验证失败就说失败）。

### 9.4 「可影响内容不获权威」：提示注入防御

- memory 用 HumanMessage 而非 SystemMessage（OWASP LLM01）。
- 远端内容 tag 中和（ToolResultSanitization）；记忆/会话/事实在嵌入点 HTML 转义。
- SkillToolPolicy 明确「best-effort scoping，**不是**安全边界」；SandboxAudit「纵深防御，不是安全边界」；MCP stdio 校验「纵深，不是信任边界」——框架诚实标注每条机制的边界。
- `<system-reminder>` / 框架标签由服务端 provenance 键持有，客户端无法伪造入站消息。

### 9.5 「跨 loop / 跨线程不得共享有环对象」

- RunJournal 是 `deerflow_loop_bound`（owner loop + loop-bound SQL pool）；子代理只收到窄 record 代理。
- `asyncio.Semaphore` 绑定单 loop，无法覆盖 lead+subagent+sync → 用 threading 原语。
- BoxLite 的 box 是 loop-affine → 私有 loop + `run_coroutine_threadsafe`。
- 专用有界 executor 承载阻塞锁（不占默认 executor / 事件循环）。

### 9.6 「持久化即审计，审计即证据」：可解释收尾

- 所有提前结束都落到**加法 `stop_reason`**，子代理/ledger/UI 都能读出是 `loop_capped`/`token_capped`/`turn_capped`/`safety_capped`/`subagent_limit_capped`/`model_length_capped`。
- `deerflow_tool_meta` 把 status/error_type/recoverable_by_model/recommended_next_action 结构性 stamp 到每个工具结果（tool_receipt 记录每个工具调用：工具名/状态/参数与输出哈希/字节数/时间戳）。
- 验收检查代码级对照记录证据，`UNVERIFIED` 不静默通过。
- `persistent_shell_sessions` 三态：未声明 shell 状态 ⇒ 证据按 `UNVERIFIED`。

---

### 9.7 纠偏：Agent 脱主线 / 卡住不动时的重新对齐

> **为什么单独成节**：前面 §9.1 讲的"三层护栏"负责**终止**（掐断、强制收尾），但它们**不负责"把 agent 拉回正轨再继续跑"**。这两件事经常被混为一谈，但对初学者是完全不同的两类问题，解法也不同。本小节把**纠偏（correction / re-centering）**单独讲透。

#### 9.7.0 先分清两类"出问题"

**问题 A：脱主线（direction drift / 跑题）**——agent 忘了最初的目标，转去做别的事，或答错了 turn。
**问题 B：卡住不动（stagnation / 无效重复）**——agent 没跑题，但反复做同一件"不产生新信息"的事，原地打转。

两类问题症状相似（都表现为"run 不结题、用户等不到答案"），但**对策方向相反**：
- 治 A 靠**重新锚定到目标任务**（把目标、最近用户请求、已完成步骤重新喂回上下文）。
- 治 B 靠**敦促换策略 / 总结收尾**（告诉它"别撞墙了，换招"）。

下面每条都标注它治的是 A 还是 B。

#### 9.7.1 问题 A（脱主线）的纠偏

**(A1) 系统提示设计——让"不跑题"从源头开始**
- 位置：`deerflow/agents/lead_agent/prompt.py`。主系统提示是**完全静态**的（§5-4 前缀缓存），它在设计上就让 agent：按步骤走、遇到歧义先问清楚、做完总结自己完成了什么。
- 坑：动态内容混进 system prompt 会破坏前缀缓存，且让"可影响内容"拿到 system 级权威（注入面）。所以它坚持"静态 system + 动态 context 分开"。

**(A2) DurableContextMiddleware——把任务主线投影回每一次模型请求**
- 位置：`deerflow/agents/middlewares/durable_context_middleware.py`（链上第 17 位）。
- 坑：任务委派太多、历史被 Summarization 压缩后，**主线目标从模型视角消失了**，模型对着删减后的历史"忘事"。
- 对策：把 `task` 委派的**摘要**（completed / in-progress / 结果）和**已加载的技能引用**投影进每次请求，压缩也删不掉。静态规则用 SystemMessage，不可信字段（summary、技能描述）用 HumanMessage 数据块——防止把压缩产物伪造成系统指令。
- 治 A 的核心手段：**即使历史被压缩，模型仍能看到"我本来要干什么、已经派了什么、得到了什么"**。

**(A3) SummarizationMiddleware——压缩时保住"最新用户请求"与"已注入的记忆提醒"**
- 位置：`deerflow/agents/middlewares/summarization_middleware.py`。
- 坑：压缩时若把最新用户请求也卷进 summary，模型会**答错 turn**（回答的对象变成了旧问题）。
- 对策：`compact_state` 用**精确消息 ID** 保留最新真实用户请求；`_preserve_dynamic_context_reminders` 按 tag 救回 `<system-reminder>`（日期 + `__memory` peer）。不救未 tagging 的 `__user` peer（历史 prompt 仍可压缩）。
- 治 A 的另一半：**保证模型始终能看到"用户现在到底在问什么"**。

**(A4) ClarificationMiddleware——唯一真正回到用户意图的手段**
- 位置：`deerflow/agents/middlewares/clarification_middleware.py`（链上**必须最后**，第 35 位）。
- 坑：歧义时 agent 自己猜，猜错方向浪费大量调用。
- 对策：`ask_clarification` 工具 → 写 `human_input` 卡 → `Command(goto=END)` **暂停**，等用户回答再继续。`after_model` 会丢掉同批 sibling 调用（避免用户没回答时它们先跑）。`disable_clarification` 时保留 sibling。
- ⚠️ **重要限制**：scheduled 非交互 run 通过 `context.non_interactive=true` **丢弃**这个工具（`ask_clarification` 不在工具集里）。所以**自动任务里 A4 退化为不可用，只能靠 A2/A3**。
- 治 A 的"人机核对点"。

**(A5) TodoListMiddleware（Plan 模式）——用任务清单锚定步骤**
- 位置：`deerflow/agents/middlewares/todo_middleware.py`（可选，`is_plan_mode`）。
- 对策：`write_todos` 工具维护任务清单，配合 A2 让 agent 明确"我在第几步、下一步是什么"。
- 治 A 的"结构化锚点"。

**(A6) ToolReceipt / 验收证据——用事实强制 grounded**
- 位置：`subagents/acceptance_checks.py`（§8-13）、`verification.receipts_enabled`。
- 对策：可判定判据（`file exists`、`tests_passed`）在代码里**对照记录的证据**检查，其余一率 `UNVERIFIED`，绝不静默通过。逼 agent 引用实际执行过的证据（receipt ledger r1..rN），减少"东拉西扯、答非所问"。
- 治 A 的"可判据化"。

#### 9.7.2 问题 B（卡住不动）的纠偏

**(B1) ToolProgressMiddleware 的 `[PROGRESS HINT]`——敦促换策略**
- 位置：`deerflow/agents/middlewares/tool_progress_middleware.py`（链上第 12 位，RFC #3177）。
- 坑：某个工具反复返回"没结果/没找到/被限流/又是同一份内容"，agent 还一直用它，是**无效重复**。
- 对策：按 `(thread, tool)` 状态机（§2 item 12、§9-4）在 WARNED 时注入 hint，带 `recommended_next_action`：
  - `rewrite_query` → "Try rephrasing your search query with different keywords"
  - `try_alternative` → "Consider using a different tool or strategy"
  - `summarize` → "Consider summarizing your current findings and moving forward"
  - `stop` → "Do not retry this operation — it is not recoverable"
- 治 B 的第一道纠偏：**换招，而不是继续撞**。

**(B2) LoopDetectionMiddleware 的 warn——敦促"总结收尾"**
- 位置：`deerflow/agents/middlewares/loop_detection_middleware.py`（链上第 28 位）。
- 坑：模型反复发**一模一样的 tool_calls**（无论结果如何）。
- 对策：同一调用集 ≥ `warn_threshold`（默认3）时注入：
  > `[LOOP DETECTED] You are repeating the same tool calls. Stop calling tools and produce your final answer now. If you cannot complete the task, summarize what you accomplished so far.`
- 治 B + 一部分治 A（它让 agent 从"原地打转"转为"总结已完成的推进"）。

**(B3) TerminalResponseMiddleware——空响应自救**
- 位置：`deerflow/agents/middlewares/terminal_response_middleware.py`（第 32 位）。
- 坑：工具执行完 provider 返回**空** terminal AIMessage，run 会以"伪成功"结束。
- 对策：注入一次隐藏恢复提示并重试；第二次仍空 → 在 checkpoint 里替换成可见错误标记，run 以**error** 结束而非静默成功。
- 治 B 的边缘：**防止卡在"空响应"上**。

#### 9.7.3 纠偏与终止怎么协同（不打架）

- **时机不同**：纠偏在 run 中间（注入提示/暂停问人），终止在 run 将失控时（剥 tool_calls 强制收尾）。
- **优先级**：先纠偏、纠不动再终止。纠偏是渐进的（warn 多次才 hard），终止是最终兜底。
- **消息注入的同一临界点**：LoopDetection 的 warn 和 ToolProgress 的 hint 都走 `wrap_model_call`，都能在**同一次模型调用**里注入，模型同时看到两组提示，可一起推理；两者不读对方内部状态，互不冲突。
- **若已硬停**：LoopDetection 硬停会剥 tool_calls，`wrap_tool_call` 不再发出，ToolProgress 自然不触发——没有"双重终止"。
- **若只封工具**：ToolProgress BLOCK 只返回该工具的 `[TOOL_BLOCKED]` 错误 ToolMessage，模型仍会发起那个 tool call，被 LoopDetection 继续追踪——两者各管各的独立状态。

#### 9.7.4 已知局限（诚实标注——这决定了纠偏的天花板）

1. **没有"语义级跑题检测"**：系统**不判断**"你是不是跑题了"。它靠"引导提示 + 终止兜底 + 人工确认点"让 agent 自己校准，而不是主动发现跑题。这是本小节最重要的一个事实。
2. **非交互 run 里 A4 不可用**：`context.non_interactive=true` 丢 `ask_clarification`，自动任务只能靠 A2/A3 的"上下文重锚定"。
3. **best-effort，非安全边界**：SkillToolPolicyMiddleware 明确"是行为 scoping 而非硬安全边界"（bash cat 等替代加载不捕获）；SandboxAudit "是纵深防御不是安全边界"。纠偏同理——它是行为引导，不是可靠保证。
4. **hint 只在能拿到 `deerflow_tool_meta` 的工具上触发**：ToolProgress 依赖 ToolErrorHandling 给结果 stamp meta；某些短路径/外部 MCP 结果的 meta 可能缺失（会告警但降级）。
5. **纠偏是概率性的**：模型可能忽略注入的提示继续走。真正的硬保证只有"终止"层。

#### 9.7.5 一句话总结

> **防**：静态上限（recursion/token/子代理数）保证必然终止 + 动态双检（调用模式哈希、结果质量状态机）。
> **纠**：注入"换策略/总结推进"提示（B1/B2）、把目标和最近请求持续投影回上下文（A2/A3）、`ask_clarification` 暂停问人（A4）、plan 任务清单（A5）、可判据证据（A6）。
> **止**：剥 tool_calls 强制终答并标记原因（§9.1）。
> **局限**：没有语义跑题检测；纠偏是行为引导而非保证。

---

## 10. 总览表

| 子系统 | 核心坑 | 核心机制 |
|---|---|---|
| 中间件链 | 消息序列破坏/工具调用畸形 | 严格排序 + 每个中间件独立守卫 + 顺序即正确性 |
| 终止性 | 死循环、无限烧钱 | recursion clamp / token 预算 / 循环检测双检 |
| 正确性 | 工具结果重复、数据串台 | ToolProgress 状态机 + 沙箱隔离令牌 |
| 安全性 | 注入、越权、路径穿越、秘钥泄漏 | 消毒链 + 双层鉴权 + 沙箱/路径守卫 + 秘钥 env 注入 |
| 稳定性 | 超时、限流、分片竞态 | 重试/退避/熔断 + 幂等锁/键 + 部分唯一索引 |
| 一致性 | 多实例竞争、checkpoint 竞态 | 进程锁 + 持久唯一索引 + 租约心跳 + CAS |
| 模型一致性 | 各 provider 特有字段静默丢失 | 每种 patch + 共享 payload 回灌 + 能力矩阵 gating |
| 纠偏（§9.7） | 脱主线、卡住不动 | 上下文重锚定（DurableContext/Summarization）+ 提示换策略（ToolProgress/Loop warn）+ ask_clarification 暂停 + plan 清单 + 可判据证据 |
| 记忆正确性 | 框架噪音、上行过滤、上下文膨胀 | 提取过滤 + 队列合并/背压 + DeerMem 淘汰 |
| 子代理 | 身份冲突、容量失控、证据污染 | 身份拆分 + 限额 + stop_reason 加法字段 |
| Runtime | run 竞态、SSE 命名空间、checkpoint | admission 锁 + `values\|ns` 命名 + reserve_checkpoint_write |
| Skills | 授权语义、注入、包边界 | 严格激活语法 + 差值策略 + fail-closed + registry 路径解析 |
| MCP | 陈旧/漂移、session 作用域 | 签名探测 + durable task 租约 |
| Config | 信任分层、反射失败、版本漂移 | config.yaml/json 分层 + 反射 loud + 版本 lockstep |
| 扩展 | 顺序破坏 | assert_ordering + 单一注入点 + harness/app 边界测试 |
| 审计 | 无法解释 run 如何结束 | stop_reason + tool_meta + receipt + 验收检查 + run journal |

---

### 结语

DeerFlow 的 harness 不是单个「护栏」，而是**一条由约 35 个中间件 + 契约层沙箱 + 模型适配补丁 + 记忆优化 + 子代理控制器 + 运行期竞态防护共同织成的网**。每个机制背后都对应一个真实踩过的坑（正文用 `#NNNN` 注了 issue 号），这解释了为什么「顺序、原子性、幂等性、fail-closed、诚实标注边界」反复出现——它们是 agent 工程里比「让模型更聪明」更难的、决定系统能否交付的部分。
