# 10 长期记忆架构:跨会话 Fact、可插拔后端与动态注入

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写。
>
> 本章覆盖文件:`agents/memory/`(manager.py、tools.py、summarization_hook.py、backends/*)、
> `agents/middlewares/memory_middleware.py`、`agents/middlewares/dynamic_context_middleware.py`、
> `agents/middlewares/summarization_middleware.py`(仅 hook 挂载点)、`config/memory_config.py`、
> `backends/deermem/deermem/`(config/core/{storage,retrieval,updater,message_processing,eviction,prompt,queue}.py)。
> 仓库内已有一份 1767 行的自包含深度设计稿 `docs-local/memory-architecture-design.md`(下文简称
> **《设计稿》**),本章是它的**代码校准版导读**:给出骨架、关键决策与真实路径,细节逐节链向设计稿。

> 旧书两章(hawkli ch09 / coolclaws ch11)基于数月前的结构写作(单层 `MemoryManager` + JSON 文档、
> 无后端抽象、无容量淘汰、无 ID 交换注入),全部作废。当前版本的三个**结构性事实**先立在这里:

1. **后端可插拔**:记忆系统是 `MemoryManager` 抽象契约 + `backends/` 目录自动发现。默认本地后端
   `deermem`,另有 `mem0` / `honcho` / `openviking` 远程适配器与 `noop` 空实现模板。
2. **事实是"文件级原子"的 Markdown**:一条事实 = 一个 `.md` 文件(YAML front matter),不是 JSON
   大文档里的一个对象。摘要与事实分储、`scope/durability/authority` 只活在抽取期不落盘。
3. **读写链路完全异步分离**:写 = 对话结束后的 debounce 队列 + 一次 LLM 抽取 + 确定性写闸门;
   读 = 每条消息前 `DynamicContextMiddleware` 按 token 预算"能带多少带多少",默认**根本不检索**。

---

## 1. 设计目标:把"值得跨会话保留的"抽出来

LLM 无状态,`messages` 历史随会话消亡。长期记忆要解决的是:**换会话后 Agent 仍"记得这个用户"**——
不再要求用户重新自我介绍、不再重复犯已被纠正过的错。仓库设计稿把目标收敛为七条(§1.2):

| 目标 | 手段 |
|---|---|
| 免打扰 | 后台自动抽取,`memory.mode: middleware` 默认被动捕获,用户不用教 Agent 记什么 |
| 高信噪比 | `scope/durability/authority` 三字段确定性写闸门,只收跨会话仍成立的描述性事实 |
| 可纠正 | contradiction 删除 + `correction` 特权类别 + guaranteed 注入,纠正内容永远在场 |
| 不膨胀 | 事实容量上限 `max_facts`(默认 100)+ 淘汰策略 + staleness 复查 + 碎片合并(默认关) |
| 隔离 | `(user_id, agent_name)` 双维分桶,不同用户/不同 Agent 记忆互不串 |
| 可靠 | 用户级锁 + 双乐观锁 revision + 原子写 + fail-loud(冲突绝不静默覆盖) |
| 可观测 | 每次带 `<memory>` 的 run 记录记忆块 SHA-256(`context:memory` 事件) |

**非目标(与旧书的根本差异)**:不存对话原文(那是 LangGraph checkpointer + run_events 的"历史"
系统);不做向量语义检索(默认注入无查询词,检索只是 tool 模式的可选辅助);不承诺 100% 抽取准确
(LLM 有损,靠闸门兜底,宁漏勿错)。`memory.injection_enabled: false` 可"只记不用"。

---

## 2. 总体架构:一次 LLM 抽取、四种组件、三个挂载点

一条记忆的旅程分写/读两条链路:

```
用户消息 → Agent 回复 ──► MemoryMiddleware (after_agent 捕获)         读链路(每条消息前)
                            │ MemoryQueue: debounce 10s + 去重          DynamicContextMiddleware
                            ▼                                          (before_agent, lead-only 链最前)
                         MemoryUpdater ─ ① 当前记忆快照+本轮对话        │ get_context(user, agent)
                            │         ② LLM 一次调用 → 六类决策 JSON      ▼
                            │         ③ 确定性写闸门 → change set      <memory> 块注入首条用户消息前
                            ▼                                          (摘要 + 置信度排序事实,预算裁剪)
                         MemoryStorage (FileMemoryStorage: 锁+乐观锁+原子写)
                            ▼
      memory.json(摘要) + agents/{name}/facts/*.md + .retrieval/ FTS5 索引
```

四个核心组件职责单一:**Middleware 只捕获、Queue 只缓冲去重、Updater 只组织抽取与闸门、Storage
只落盘**。为何写链路必须异步:一次抽取 = 一次 LLM 调用,有延迟有成本,不能阻塞用户看到回复;
多个快速来回经 debounce 合并成一次抽取省 token;抽取失败降级为"这次不记下次再试",不影响对话。

三个记忆相关的 LangGraph 中间件(挂载点见仓库 `agents/middlewares/AGENTS.md` 与设计稿 §11.1):

| 组件 | 文件 | 挂载点 | 职责 |
|---|---|---|---|
| `MemoryMiddleware` | `agents/middlewares/memory_middleware.py` | lead 链 `TitleMiddleware` 之后 | 写链路捕获器,`after_agent`/`aafter_agent` 每轮结束把整包消息交给 `manager.add()` |
| `DynamicContextMiddleware` | `agents/middlewares/dynamic_context_middleware.py` | lead-only 链**第一个**(基础链之后) | 读链路注入器,`before_agent` 每次模型调用前注入记忆 + 当前日期(§9) |
| `memory_flush_hook` | `agents/memory/summarization_hook.py` | `SummarizationMiddleware` 的 hook 列表 | 摘要压缩**前**的紧急冲刷:把即将被压掉的消息抢先入队 |

关键边界:**子代理不挂这三个中间件**——子代理与父线程共享 `thread_id`,若它也写记忆会把内部
中转对话污染进父线程的持久记忆(这也是摘要路径 `skip_memory_flush` 的原因)。

---

## 3. 存储后端抽象:`MemoryManager` 三层契约与可插拔发现

### 3.1 Host 共享配置:刻意做薄

`config/memory_config.py` 的 `MemoryConfig` **只持有六个 host 共享字段**,DeerMem 的私有旋钮
全部藏在 `backend_config` dict 里——这是"后端可换"的前提:共享 schema 越薄,后端越可移植。

```yaml
# config.yaml(根)
memory:
  enabled: true
  mode: middleware            # middleware(默认被动) | tool(模型主动调工具)
  injection_enabled: true     # false = 只记不用:照常抽取存储,不注入
  shutdown_flush_timeout_seconds: 30   # 优雅停机排空缓冲的硬时限(1-300s)
  manager_class: deermem      # deermem | mem0 | honcho | openviking | noop | dotted path
  backend_config: { }         # 逐字传给后端自解析(DeerMem 解析成 DeerMemConfig)
```

旧版散在 `memory:` 顶层的 DeerMem 私有字段(旧书照抄的那批:`max_facts`、`max_injection_tokens`、
`staleness_*`……)在加载时被**自动迁移进 `backend_config`**,升级不会静默把自定义值重置回默认。
`mode: tool` 同时要求后端 `supports_search=True`,否则构造期不变式校验直接抛错。

### 3.2 `MemoryManager`:pydantic BaseModel + ABC 语义

契约在 `agents/memory/manager.py`。它是 **pydantic `BaseModel` 子类**(免费获得字段校验与序列化),
而 `ModelMetaclass` 派生自 `ABCMeta`,未实现的抽象方法在**实例化时**抛 `TypeError`——记忆是持久
状态,缺 `add`/`get_context` 的后端是严重 bug,必须在构造期抓住。方法分三层:

| 层 | 方法 | 说明 |
|---|---|---|
| **Tier-1(abstract)** | `add(thread_id, messages, ...)` / `get_context(user_id, ...)` | 写(入队抽取)+ 读(返回**注入就绪的纯文本**)。后端的根本职责,必须实现 |
| **Tier-2(管理,带默认)** | `add_nowait` / `search` / `get_memory` / `clear_memory` / `import_memory` / `export_memory` / `shutdown_flush` | 默认委托或抛 `NotImplementedError`;支持的才覆盖 |
| **Tier-3(可选 hook)** | `warm` / `reload_memory` / `create_fact` / `delete_fact` / `update_fact` / `on_pre_compress` / `on_turn_start` | 启动预热、事实 CRUD、摘要压缩前回调 |

构造入口是**类方法 `from_config(backend_config, *, mode, **host_hooks)`**,由工厂调用而非直接
`cls(...)`,让每个后端自己决定依赖组装。两个**类级声明**(ClassVar):

- `supports_search: ClassVar[bool]` —— 实现了 `search()` 必须置 True,`mode: tool` 强制要求。
- `requires_passive_writes_in_tool_mode: ClassVar[bool]` —— 置 True 的后端(如 openviking)在
  tool 模式下**保留 `MemoryMiddleware`**,形成"工具搜索 + 被动抽取"混合模式。

错误类型同样是公共契约:`MemoryManagerError` 基类派生 `MemoryConflictError` /
`MemoryCorruptionError`;DeerMem 把存储层冲突转换为公共类型,Gateway 映射 **409 / 稳定 500**。

契约的三个**刻意中立点**:① `get_context` 返回的文本格式由后端自定(DeerMem 做"全量读 + 置信度
裁剪",别的后端可以自己检索 + 格式化,格式不是契约);② `add` 收到的是**原始对话消息**,过滤/信号
检测是后端私有事务;③ **不假设"事实"模型**——后端完全可以不存 fact,只要能读写文本。

### 3.3 drop-in 发现 + fail-loud

不需要注册表:工厂扫描 `agents/memory/backends/` 目录,每个有 `__init__.py` 且暴露
`MANAGER_CLASS` 的子文件夹以**文件夹名**注册。`manager_class` 解析顺序:
短名(`deermem`/`noop`)→ dotted path(`pkg.mod:Cls`,任意包里的 `MemoryManager` 子类)→
都失败则 `raise ValueError`。**最后一条是刻意的**:拼错/导入失败绝不静默回退 deermem——否则写入会
悄悄落进错误的存储。解析发生在启动期(急着预热),让运维当场修配置。

### 3.4 可移植性黄金规则:整个后端只允许一行 `from deerflow`

```python
from deerflow.agents.memory.manager import MemoryManager   # 唯一允许的 from deerflow
```

后端收到 host 信息只有两个通道:方法参数(user_id/agent_name/thread_id/messages)与
`backend_config` dict(自己的旋钮:模型、向量库、阈值……)。存储根目录从
`backend_config["storage_path"]` 读(host 注入),绝不自己 import deer-flow 路径 helper。这一行
导入就是后端与宿主唯一耦合点——想移植到别的 agent,只改这一行。完整动手步骤(六步,参照
`backends/noop/` 空实现模板)见 `agents/memory/backends/README.md`。

### 3.5 四个现成后端

| 后端 | 目录 | 定位 |
|---|---|---|
| `deermem` | `backends/deermem/` | **默认**。DeerFlow 自研:结构化 Fact(front matter Markdown)+ 本地 JSON/FTS5,**零外部依赖**,单机/单用户默认选择 |
| `mem0` | `backends/mem0/` | 可选 HTTP 适配器。**无本地 LLM 抽取**——mem0 服务端自己做抽取/遗忘;DeerFlow 只做身份解析与失败策略(`failure_policy`:启动 fail_fast/tolerate、读 fail_open/fail_closed、写 log_and_drop/raise)。未知配置键拒绝(fail-fast) |
| `honcho` | `backends/honcho/` | 可选远程 Honcho(用户-模型记忆,表征由 Honcho 服务端 deriver 构建,无本地 LLM 调用)。**每 resolved `user_id` 建一个 workspace**,user id 映射到 Honcho 的 `^[a-zA-Z0-9_-]{1,64}$` 文法(`sanitize_id`);缺用户 fail-closed 到无记忆;默认读失败记日志返回空,`failure_policy.read: fail_closed` 才重抛 |
| `openviking` | `backends/openviking/` | 可选远程,**用官方 `langchain-openviking` 包**,保持 middleware 模式。一条 API key 绑定一个配置的 owner,其他 owner 在远程访问前拒绝。DeerFlow 拥有捕获时机/召回 query/transcript 游标,包拥有传输与 Session commit;一个 thread 映射一个稳定 Session,hash-only 游标存在 `{storage_path}/openviking/sessions/` 下 |
| `noop` | `backends/noop/` | 刻意写全的**空实现模板**——存什么读什么都返回空。新后端照抄它(文档见其 manager docstring 与 backends/README.md) |

mem0/honcho/openviking 的远程细节属于适配器层,架构上只讲到这里——它们共享同一 `MemoryManager`
契约,换后端 = 改 `manager_class` 一行 + 重启(manager 是进程级单例,不热加载),中间件一行不改。
各适配器自己的 README(`backends/<name>/README.md`)是权威细节源。

---

## 4. 默认本地后端 DeerMem:文件布局、事实模型与并发

### 4.1 目录布局

```text
{base_dir}/users/{user_id}/memory.json
{base_dir}/users/{user_id}/agents/{agent_name}/facts/{sha256(fact_id)[:2]}/{fact_id}.md
```

- **`memory.json` 只存共享摘要、revision、时间戳,永不存事实或事实索引**(事实是独立文件)。
  存储层刻意不把事实做成"一个 JSON 大文档",换来:单条原子增删改、坏一条不影响其他、可人工
  编辑、可 git diff;分片 `sha256(fact_id)[:2]` 最多 256 个子目录,防单目录文件过多。
- 缺省 `agent_name` 映射到保留名 `__default__`;旧版无用户根与 `lead-agent` 桶是只读回退数据,
  正常读取自动迁移。
- **两个 SQLite 别混淆**:`.retrieval/<scope>.sqlite`(FTS5 事实索引,派生可重建)属于记忆系统;
  对话历史/checkpoint 是 `database.backend` 的 LangGraph checkpointer + run_events,两套互不读写。
- 用户隔离 = `users/{user_id}/` 目录 + 每用户锁;开启认证后每个登录用户一份记忆;无认证模式
  `DEFAULT_USER_ID = "default"`。自定义 Agent 文件共享每用户 agent 目录 → **换自定义 Agent
  = 换记忆桶**。thread **不隔离**(仅作去重 key)——记忆本就设计为跨会话共享。

### 4.2 事实 Fact:一条 `.md` = 一个原子单元

```markdown
# backend/.deer-flow/users/{user_id}/agents/{agent}/facts/ab/fact_uv.md
---
id: fact_uv
schemaVersion: 2
content: 用户偏好使用 uv 而非 pip 安装 Python 依赖
category: correction
confidence: 0.98
createdAt: "2026-08-07T12:00:00Z"
updatedAt: "2026-08-07T12:00:00Z"
revision: 1
expected_valid_days: 365
source:
  type: extraction          # extraction | manual(用户手写,高信任)
  threadId: "thread-abc"
confirmationCount: 2        # 确定性确认次数(hybrid-v1 用)
lastConfirmedAt: "..."      # 最近一次确定性确认时间
---

用户偏好使用 uv 而非 pip 安装 Python 依赖
```

**六类 category 与设计意图**(事实注入优先级、staleness/consolidation 豁免都由它决定):

| category | 语义 | 设计意图 |
|---|---|---|
| `preference` | 偏好/反感的工具、风格 | 最常影响后续行为 |
| `knowledge` | 专长、掌握的技术 | 回答"用户会什么" |
| `context` | 背景事实(职位、项目) | 画像底色 |
| `behavior` | 工作模式、沟通习惯 | 长期稳定 |
| `goal` | 目标/方向 | 指导长线协助 |
| `correction` | 显式纠正或 Agent 错误(含 `sourceError` 记"之前错在哪") | **特权类别**:guaranteed 注入 + staleness/consolidation/eviction 三重豁免 |

### 4.3 分类三字段:`scope` / `durability` / `authority`——只活在抽取期

这是"Fact 模型"最反直觉的一点:**三字段不落盘**。它们由 LLM 在抽取 JSON 里逐条标注,只用于
确定性写闸门的一次性校验,用完即弃(设计稿 §5.4 的理由:它们是**决策输入**而非数据,落盘会污染
数据模型、增加迁移负担)。

| 字段 | 取值 | 含义 |
|---|---|---|
| `scope` | `user` / `thread` / `project` | 信息适用范围:跨会话跨任务 / 仅本次对话 / 仅某项目 |
| `durability` | `durable` / `temporary` | 是否预期跨会话仍为真 |
| `authority` | `descriptive` / `transactional` | 描述性陈述 vs 指令/一次性授权 |

### 4.4 并发与持久化:双 revision 乐观锁 + fail-loud

`core/storage.py`:`FileMemoryStorage` 是权威存储 + 检索适配器的持有者。写入协议(设计稿 §7):

```
1. 取用户级锁(进程内 RLock + 跨进程文件锁)
2. 读 memory.json 当前 revision(共享版本)
3. 校验每个目标事实的 per-fact revision 仍满足 precondition
4. 写:memory.json(revision+1) + 每个事实 .md(per-fact revision+1),走 recovery journal
5. 冲突:MemoryManifestRevisionConflict / MemoryFactRevisionConflict
   ├─ disjoint-rebase(只动事实不动摘要,且每个事实 precondition 仍满足)→ 最多重试 2 次
   └─ 否则 → fail-loud 抛错,Gateway 映射 HTTP 409
```

**双 revision** 的设计:共享 revision 保护 memory.json 摘要这个共享资源;per-fact revision 保护
单条事实——两条并发更新各改一条不同事实时可以安全 rebase,互不阻塞。POSIX 原子替换 `os.replace()`
后 fsync 父目录;目标文件写入走 journal,中途崩溃可恢复。`apply_changes()` 只返回 `complete: false`
的事实增量——**绝不把部分缓存伪装成完整记忆文档**。`core/storage.py` 里错误按类型分
`MemoryStorageCorruption`/两个 `MemoryRevisionConflict` 子类,上层不要用文本匹配异常。

迁移:legacy JSON → Markdown 的 v1-to-v2 是应用运行期**单向**迁移,先写校验过的
`{manifest}.v1.bak` 再动手,备份缺失/不匹配则中止;可主动跑
`PYTHONPATH=. python scripts/migrate_memory_markdown.py --all-users --dry-run`(backend/ 下)。

---

## 5. 写链路:捕获 → 过滤 → 队列 → 抽取 → 闸门 → 落盘

### 5.1 捕获:`MemoryMiddleware` 薄如纸

`after_agent` 把**原始消息整包**交给 `manager.add()`,自己不做任何过滤。四个要点:

1. **只捕获、不过滤**:过滤(Human/AI、去寒暄、信号检测)全部在后端 `message_processing.py`
   里做——不同后端策略不同,不该写死在 agent 层。
2. **user_id 入队即捕获**:`resolve_runtime_user_id(runtime)` 在 `after_agent` 里取好存入
   `ConversationContext`。原因:debounce 用 `threading.Timer` 在**另一个线程**触发,`ContextVar`
   不跨原生线程传播——不提前捕获,定时器触发时就丢了用户身份。
3. **trace_id 同步捕获**:同样的线程边界问题;让记忆抽取的 Langfuse span 与主对话 trace 对齐。
4. 同步 `after_agent` / 异步 `aafter_agent` 双实现,异步路径用 `asyncio.to_thread` 把管理器解析
   移出事件循环,再调后端 `aadd`(网络后端必须自己 offload)。

### 5.2 消息过滤四步(后端私有,`core/message_processing.py`)

`filter_messages_for_memory` + `filter_trivial` + `detect_signals`,四步:

1. **只留 `HumanMessage` / `AIMessage`**——`ToolMessage`、系统、隐藏内部消息全丢(工具中间结果
   进长期记忆毫无意义还烧 token)。
2. **去掉纯寒暄**("好的""知道了")——无信息量,留着会让"必须有来有回"判断失真。
3. **至少一条 user + 一条 assistant**,否则整体跳过——没有交互不值得一次 LLM 调用。
4. **`detect_signals`**:确定性正则扫描**最近 6 条 HumanMessage**(`messages[-6:]`),模式文件在
   `core/message_patterns/{correction,reinforcement,preference,identity,goal,decision,trivial}.yaml`。

**信号 ≠ 事实类别。** 信号是"用户当前的对话行为"(消息层检测),事实类别是"这条信息的性质"
(存储分类),名字部分重合但不一一对应——如 `reinforcement` 是信号却**没有** reinforcement 这个
category,它只提升已有事实的 confidence。信号的作用是转成 prompt 里的**软提示**
(`_build_signal_hints`):命中 correction 时提示"记 confidence≥0.95 的 correction 类事实,且仅当是
可复用的用户级偏好;对当前任务文件/方向的纠正是 thread/project 级,不得入库"。**信号只是提示
不是裁决**,最终能否入库由确定性写闸门逐条判定。信号还有第二个用途——**背压豁免**:带信号的
更新在队列满时也总是被允许入队,保证高价值记忆不被丢。

### 5.3 队列与抽取:一次 LLM 调用,六类决策

debounce(默认 10s)到期后在 Timer 线程上执行一次抽取。模型同时看到四样输入:① 当前
`(user, agent)` 桶记忆快照(JSON,序列化前把 `<` `>` `&` 转义,防 `</current_memory>` 逃出块边界
伪造权威区,issue #4044);② 过滤后的本轮对话(单条超 1000 字符截头尾 500);③ 条件提示段
(correction_hint / staleness_review_section / consolidation_section);④ 静态系统指令
(`core/prompts/memory_update.chat.yaml`)。输出**一份 JSON,六类决策**:

```json
{
  "user":    { "workContext": {...}, "personalContext": {...}, "topOfMind": {...} },
  "history": { "recentMonths": {...}, "earlierContext": {...}, "longTermBackground": {...} },
  "newFacts": [ { "content": "...", "category": "preference|knowledge|context|behavior|goal|correction",
                  "confidence": 0.9, "expected_valid_days": 90,
                  "scope": "user|thread|project", "durability": "durable|temporary",
                  "authority": "descriptive|transactional" } ],
  "factsToRemove":       [ { "id": "fact_xyz", "scope": "user", "reason": "...", "replacementFactIndex": 0 } ],
  "staleFactsToRemove":  [ { "id": "fact_xyz", "reason": "..." } ],
  "staleFactsToExtend":  [ { "id": "fact_xyz", "extend_by_days": 365, "reason": "..." } ],
  "factsToConsolidate":  [ { "sourceIds": ["fact_a"], "consolidated": { "content": "...", ... } } ]
}
```

**系统指令完全静态**(`system` 消息没有变量,每次渲染逐字节相同)——与 lead agent 静态系统提示词
同理,对提供商前缀缓存友好。注意:`fact_extraction.yaml` 在代码里标注 dormant(未接任何运行时调用,
`deer_mem.py:147`),现行主模板是 `memory_update.chat.yaml`;可用 `prompts_dir` 按 Agent 分子目录
覆盖,自定义目录必须含同样的分类字段,旧模板会让抽取写 fail-closed。

**寿命档位**:`expected_valid_days` 由 LLM 按档位赋值(≤14 高度临时 / 15-60 短期 / 60-180 中期 /
180-365 稳定 / >365 非常稳定),写盘被钳制到 ≤ `staleness_age_days × staleness_max_lifetime_multiplier`
(默认 90×20=1800 天),防止模型给一个"永不复查"的寿命。置信度档位:0.9-1.0 显式陈述、
0.7-0.8 行为强暗示、0.5-0.6 推断(慎用)。

**输出消费三段管线**(`core/updater.py`):① `_parse_memory_update_response` 容错提取(模型可能包
thinking/散文/fence,逐个 `{` 试 `raw_decode`,找含 user+history+newFacts 三顶层键的合法 JSON);
② `_normalize_memory_update_data` 逐条校验、丢畸形条目——**关键安全规则:factsToRemove 存在而
newFacts 全部畸形 → 整体抛错(绝不执行"只删不写")**;③ `_apply_updates` 闸门 → 去重 → max_facts
裁剪 → 冲突删除 → 合并。

### 5.4 确定性写闸门:逐条 fail-closed

LLM 可能违背指令,所以写盘前用**纯代码**校验(不依赖模型自觉),`updater.py` 里三个同源闸门函数
`_fact_scope_gate_reason` / `_summary_scope_gate_reason` / `_removal_scope_gate_reason`:

```
事实入库三条件(缺一不可):
  scope      == "user"        ← 能跨会话/跨任务成立
  durability == "durable"     ← 预期长期为真
  authority  == "descriptive" ← 是描述,不是指令
```

| 候选内容 | scope | durability | authority | 结果 |
|---|---|---|---|---|
| "用户偏好 uv 而非 pip" | user | durable | descriptive | ✅ 入库 |
| "当前任务是把 CSV 转 JSON" | thread | temporary | descriptive | ❌ 拒(一次性) |
| "用户说可以删除生产库" | user | durable | transactional | ❌ 拒(指令/授权) |
| "项目约定用 ruff" | project | durable | descriptive | ❌ 拒(项目级) |

**fail-closed = 默认拒绝**,绝不因为"模型看起来没问题"就放行;**逐条 = 一条坏数据只拒绝它自己,
不影响同批其他合法事实**,也绝不反过来"整体还行就放过某条"。每个判定按原因(missing / scope /
durability / authority)分桶计数,拒绝率经 `rejected_by_scope_gate` 指标暴露,高拒绝率有告警。
方向性保守:拒绝的代价只是"这次没记到"(下轮重抽);入库的代价是"错误个性化持续污染未来每次注入"
——**宁漏勿错**。摘要段也必须 user-scoped + descriptive;缺失标签拒绝该项但不中断其他无关更新。

### 5.5 冲突删除是原子的

LLM 用 `factsToRemove` 表达"旧事实被推翻",可带 `replacementFactIndex` 指向替代它的 newFacts
条目。规则:**只有替代的新事实通过闸门、去重、max_facts 裁剪之后,旧事实才被删**——把"删旧的 +
写新的"绑成一个原子决策,避免"旧删了、新没写进去"的数据丢失。task/project 级事实不允许 LLM
单方面删除(fail-closed)。

---

## 6. 淘汰策略:三套互补机制,全由确定性代码把关

记忆是只增不减的仓库,放任不管会膨胀、会误导。DeerFlow 用三套机制,结构一致:**代码先圈定候选,
LLM 只能在候选里选,apply 层再上确定性护栏**——模型没有越权删/合的窗口。

### 6.1 容量淘汰 `deermem/core/eviction.py`(每写必算)

所有自动、手动、工具、导入路径都走同一份容量逻辑——`max_facts` 上限(默认 100)下的**确定性、
可解释**选择。模块 docstring 直言:"Deterministic, explainable capacity policies"。

```python
select_facts_for_capacity(
    facts, *, max_facts, policy,
    confidence_weight=0.65, confirmation_weight=0.25, access_weight=0.10,
    confirmation_half_life_days=90, access_half_life_days=30,
    correction_reserved_fraction=0.10, correction_reserved_max=10,
) -> FactEvictionDecision   # kept / evicted / scores / policy / reserved_correction_slots
```

- **`confidence`(默认)**:按 confidence 排序,精确复刻历史行为。
- **`hybrid-v1`(opt-in)**:三个有界信号加权——`confidence`(0.65)、**确认新鲜度**(0.25,按
  `lastConfirmedAt` 指数衰减,半衰期 90 天;无显式确认时把创建时间当弱证据 ×0.5)、**访问热度**
  (0.10,`accessHeat × log1p 归一化`,半衰期 30 天)。权重必须合计 1.0,否则构造期 `ValueError`。
- **correction 保留名额**:hybrid-v1 下先为 correction 事实预留 `min(max_facts × 0.10, 10, 实际
  correction 数)` 个名额,**未用名额立即归还普通竞争**——只保留"最少必要"的 correction,不无脑
  全保。
- **shadow 影子模式**(`fact_eviction_shadow_enabled: false` 默认关):confidence 策略裁减时**并行
  计算** hybrid-v1,把两策略的分歧写进 metadata-only 审计(`eviction_audit_max_entries=200` 上限,
  0 禁用),方便灰度观察再切换。被淘汰者只留元数据记录 `EvictedFact`(id/category/score/components),
  不污染规范数据。

**热度的确定性确认门**:只有**确定性的消息处理**能确认事实(不是 LLM 自说自话)——updater 的
`factsToReinforce` 输出只提供"事实绑定",确定性闸门要求最近六条被过滤后的批量消息里存在匹配的
human 消息,且**不要求再做一次信号→事实的匹配**(信号正则如 reinforcement 只是提示,不直接确认)。
确认更新 `lastConfirmedAt` / `confirmationCount`(畸形值重置)。**搜索只对返回的事实增加 access
heat;prompt 注入与 `get_context()` 不增加**——注入是"读盘格式化",不是"访问"。

### 6.2 过期复查 staleness(随抽取搭便车)

窗口到期**只是"该重新看一眼"的提醒,不是判决**——年龄大不等于错误(母语、核心技能可以无限成立)。
完整五步(设计稿 §5.2.2,代码 `_select_stale_candidates` / `_build_staleness_section`):

```
① 圈定候选(代码):createdAt + effective_age < now 且类别不受保护 → 候选
   effective_age = expected_valid_days(有则用之)否则 staleness_age_days(默认 90)
   protected(correction)直接排除
② 触发阈值(代码):候选 >= staleness_min_candidates(3) 才生成复查段塞 prompt,攒够再查
③ LLM 三选一:KEEP(不输出)/ REMOVE(staleFactsToRemove)/ EXTEND(staleFactsToExtend)
④ 交集校验(代码):实际删除/延长集 = LLM 提议 ∩ 候选集 → 越权提议(protected/未到期)静默丢弃
⑤ 应用规则(代码):删除每周期上限 staleness_max_removals_per_cycle(10),超了按置信度从低到高保留;
   延长钳到 staleness_max_extension_days(3650);被提议删除的绝不同时延长(防自相矛盾)
```

护栏(④⑤)是 apply 层确定性代码,**即使 `staleness_review_enabled` 关闭也照常执行**——此时模型
拿不到候选段、不会提议,等同空转。保护独立于模型行为也独立于功能开关。

### 6.3 碎片合并 consolidation(默认关,一开关整条关死)

同一方面被反复记成许多条小事实(碎片)→ 每条占注入预算、内容重叠。合并 = 多源合成一条更丰富的
陈述,源被删只留 `consolidatedFrom` id——**有损**,所以 `consolidation_enabled: false` 默认关
(设计稿 §5.2.3)。执行守卫全确定性:源必须真实存在、都属于候选组(按 category 分组,条数 ≥
`consolidation_min_facts` 8 才触发)、组间不重叠、2 ≤ 条数 ≤ `max_sources` 8;合成事实过同一套
三字段闸门,`confidence = min(LLM 给的, 源最大置信度)`——**不能凭空拔高**;`createdAt` 继承最新
源、复查期限继承最早源。开关关闭时从提示到落盘整条路径不存在(与 staleness 不同,不是"护栏仍在"）。

---

## 7. 读链路:两种召回机制,由 `memory.mode` 决定

**先给结论避免被"召回"带偏**:系统只有两种召回机制(设计稿 §8):

- **middleware(默认):不是关键词、也不是向量**。`get_context()` 把该 `(user, agent)` 桶的**全部**
  记忆读出来,摘要全带、事实按 confidence 排序,在 token 预算内"能带多少带多少"——**根本不检索**。
- **tool(实验):才是真正的检索**。模型主动调 `memory_search(query)`,走 SQLite **FTS5 全文索引**。

一句话:middleware = "记得多少带多少"(置信度截断);tool = "需要时按关键词查"(FTS5)。

### 7.1 middleware 注入:双预算结构 + 特权预留

`DynamicContextMiddleware` 每次模型调用前经 `_get_memory_context` → `get_context()`(DeerMem 实现
= `get_memory_data` + `format_memory_for_injection`)读盘格式化。**为什么 read 是"全量读 + 格式化"
而不是查询**:注入路径要快、要确定,不能引入额外 LLM 调用或检索依赖——confidence 已是"这条信息
有多可靠"的最好代理。注入预算双池:

| 预算池 | 默认 | 用途 | 超支行为 |
|---|---|---|---|
| 特权预留 `guaranteed_token_budget` | 500 tokens | `guaranteed_categories` 专属(默认仅 `correction`) | 溢出部分**向上挤占主预算**,自身绝不被截断 |
| 主预算 `max_tokens` | 2000 tokens | 六段摘要 + 普通事实 | 按 confidence 贪心填充,满即停 |

填充三步:guaranteed(correction)事实先进预留区(超 500 则溢出主预算,总上限被抬到
`2000 + guaranteed 实际用量`,保护特权行不被截断)→ 六段摘要无条件进主预算 → 普通事实按
confidence 从高到低贪心进主预算,遇放不下的第一条就停。裁剪时 **Facts 块是受保护后缀,溢出只裁
尾部 User Context / History**(先 History 后 User Context,Facts 最后才被碰)。计数默认 tiktoken
(`token_counting: tiktoken`,懒加载编码,加载失败降级字符估算并冷却 600s;`char` 模式彻底离线,
适合无网络环境)。渲染形态:

```xml
<memory>
User Context:
- Work: 数据分析师, 主用 Python; ...
- Personal: 中文母语, 能读写英文; ...
- Current Focus: 正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移
History:
- Recent: ...  - Earlier: ...  - Background: ...
Facts:
- [correction | 0.98] 用户偏好使用 uv 而非 pip 安装 Python 依赖 (avoid: 先前建议使用 pip)
- [context    | 0.95] 用户是后端工程师, 主用 Python
</memory>
```

### 7.2 tool 模式:四个记忆工具与 FTS5

`agents/memory/tools.py` 注册 `memory_search` / `memory_add` / `memory_update` / `memory_delete`。
tool 模式注入**只含共享摘要**(`agent_name` 置空 → Facts 块为空),事实靠 `memory_search` 按需取回,
避免"自动注入一遍 + 检索又返回一遍"的重复;`memory_add` 等走 Tier-3 `create_fact`(不支持则返回
错误 JSON)。**tool 模式 CRUD 不经过抽取闸门**——人(模型)显式写,信任模型。

`memory_search(query, top_k)`(DeerMem `core/retrieval.py`):中文 jieba 分词(仅 `memory-zh`
extra;未装退化 unicode/空白切分),英文空白切分;query 是 FTS 高级语法(AND/OR/NOT/短语/前缀)则
透传,否则拆 token 加引号转义再 OR;`bm25(memory_fts)` 打分后再叠 confidence 权重 + 时间衰减;
FTS 抛错退回子串匹配保证不崩。SQLite 里只有一张 FTS5 虚拟表 `memory_fts`,**一行 = 一条事实**
(不是一条聊天消息),存 `doc_id/content/raw_content/category/confidence/created_at/source/
scope_user/scope_agent/fact_json`。索引是**派生的可重建副本**(权威在 `.md`),坏了自动删了重建;
`retrieval_adapter: fts5` 默认,空值选子串兜底;中文分词只有 `memory-zh` extra 才启用 jieba。
Gateway 启动后台调度 `DeerMem.warm_retrieval()` 不阻塞就绪,关停先等 1s 预热再按
`shutdown_flush_timeout_seconds` 全预算冲刷规范记忆。

---

## 8. 动态注入的冻结快照:前缀缓存友好是设计出来的

`DynamicContextMiddleware`(lead-only 链**第一个**注入者,`before_agent`)的注入手法是本章最值得
抄的工程细节(设计稿 §11.3):**系统提示词保持完全静态(记忆不进 system prompt)**,所有动态内容
通过"隐藏 SystemMessage + 隐藏 HumanMessage"在**首条用户消息之前**注入,并且**只注入一次、整个
会话冻结**:

```
第一轮:  [SystemMessage(日期提醒)           id = 原始用户消息 id]
          [HumanMessage(<memory>…)           id = {原始id}__memory]   仅当有记忆
          [HumanMessage(真正的用户输入)      id = {原始id}__user]
后续轮:   原样复用不再改动 → 前缀缓存每次都能命中
```

核心技巧是 **ID 交换(ID-swap)**,由 `_make_reminder_and_user_messages` 一手完成:

```python
stable_id = original.id or str(uuid.uuid4())          # ① 原始用户消息 id 作稳定 id
reminder_kwargs = {"hide_from_ui": True, "dynamic_context_reminder": True}
if reminder_date is not None:
    reminder_kwargs["reminder_date"] = reminder_date   # ② 结构化日期只挂 SystemMessage
messages.append(SystemMessage(content=reminder, id=stable_id, additional_kwargs=reminder_kwargs))
if memory_content:
    messages.append(HumanMessage(content=memory, id=f"{stable_id}__memory",
        additional_kwargs={"hide_from_ui": True, "dynamic_context_reminder": True}))
messages.append(HumanMessage(content=original.content, id=f"{stable_id}__user",
    name=original.name, additional_kwargs=original.additional_kwargs))
```

**"就地替换"的机制**:LangGraph `add_messages` reducer 对相同 id 的消息就地覆盖——`SystemMessage`
取走原始用户消息的 id,在列表里占据原始位置;记忆块与真实输入以 `__memory`/`__user` 后缀追加。
净效果:每轮用户输入前永远挂着**同一组冻结的提醒**,原始消息"没挪过窝",后续轮次零改动 →
provider 前缀缓存每次命中。三个 `additional_kwargs` 标记是中间件私有协议:`hide_from_ui`(注入
消息不参与对话显示)、`dynamic_context_reminder`(靠标记识别注入消息,**不用内容匹配**——用户消息
里写 `<system-reminder>` 不会被误认)、`reminder_date`(只挂 SystemMessage)。

**为什么必须 SystemMessage 承载日期提醒、HumanMessage 承载记忆**:日期是框架数据可安全进 system
role;记忆内容**用户可影响,绝不能获得系统权限**(安全红线)——`<memory>` 块即使被注入也以
role=user 出现。所有文本经 HTML 转义防 `</memory>` 逃逸。`additional_kwargs` 上另有 provenance
键(`deerflow_content_kind` 等,见 `agents/middlewares/AGENTS.md`),DynamicContext 的注入消息由
`deerflow_content_kind: dynamic_context_memory` 等标记,且这些键在 `_SERVER_OWNED_MESSAGE_METADATA_KEYS`
里,调用方无法伪造。Gateway 输入处理会剥掉伪造的 dynamic-context 标记;**只有当前
`DynamicContextMiddleware` 的输出能建立首次 run 的记忆身份**。

---

## 9. 摘要与生命周期:六段叙事 + 冲刷 hook

共享摘要是**叙事型互补形态**(与原子事实互补:摘要要合成、事实要原子,混在一起改一条要重写整段):
`memory.json` 六段各是 `{summary, updatedAt}` 对象,`updatedAt` 记录最后更新,便于判断"这段多久
没动了"。设计意图(设计稿 §5.3.1):把"现在做什么 / 最近做什么 / 一直是什么"分层,`topOfMind`
设计成最常更新也最影响个性化(3-5 个**并发**主题,更新时整合新主题、移除已完成项);`recentMonths`
近 1-3 个月活动;`longTermBackground` 只放不变事实;历史段按时间窗做时间线整合而非覆盖。摘要更新
同样过三字段闸门(必须 user + descriptive),`shouldUpdate: false` 的段在 apply 层跳过。

`summarization_hook.py` 的 `memory_flush_hook` 挂在 `SummarizationMiddleware` 的 hook 列表:上下文
压缩会删除旧消息,而这些消息可能还没入记忆队列——压缩前 hook 把它们**抢先入队**,保证摘要
压缩不造成记忆捕获缺口。

---

## 10. 配置速查与运维要点

Host 共享字段见 §3.1;DeerMem 私有旋钮默认值(`backends/deermem/deermem/config.py`,校验上限都在
字段上,如 `max_facts` 限 10-500):

| 旋钮 | 默认 | 含义 |
|---|---|---|
| `debounce_seconds` | 10 | 队列合并窗口 |
| `max_facts` | 100 | 每桶事实容量上限(容量淘汰的 `max_facts`) |
| `fact_confidence_threshold` | 0.7 | 新事实入库置信度下限 |
| `max_injection_tokens` / `guaranteed_token_budget` | 2000 / 500 | 注入双预算(§7.1) |
| `guaranteed_categories` | `["correction"]` | 特权预留类别 |
| `token_counting` | `tiktoken` | `char` = 离线字符估算 |
| `retrieval_adapter` | `fts5` | 空值 = 子串兜底 |
| `fact_eviction_policy` | `confidence` | `hybrid-v1` opt-in;shadow 影子模式默认关 |
| `staleness_review_enabled` / `staleness_age_days` | 开 / 90 | 复查开关 / 兜底窗口(§6.2) |
| `staleness_min_candidates` / `staleness_max_removals_per_cycle` | 3 / 10 | 复查触发与每周期删除上限 |
| `staleness_protected_categories` | `["correction"]` | 过期豁免类别 |
| `staleness_max_lifetime_multiplier` / `staleness_max_extension_days` | 20.0 / 3650 | 寿命钳制系数 / 延长上限 |
| `consolidation_enabled` | false | 碎片合并整条开关(§6.3) |
| `consolidation_min_facts` / `max_sources` | 8 / 8 | 触发阈值 / 单组源上限 |
| `watermark_max_keys` | 4096 | 会话水位缓存软上限(0 = 无界;丢一条水位 → 下轮可能重抽一批) |

运维纪律:记忆 manager 是**进程级单例**,改后端/配置需重启(无热加载);停机排空预算
`shutdown_flush_timeout_seconds`(30s,1-300)必须装进 K8s `terminationGracePeriodSeconds`
(连同 retrieval 预热 1s);DeerMem 升级前先停服快照存储根;`memory.json` 的 out-of-band 人工编辑
需 `reload_memory` 让缓存感知。测试锚点:`backend/tests/test_memory_updater.py`(闸门/规范化)、
`backend/tests/test_<backend>_memory_backend.py`(各后端)。

---

## 11. 深度阅读路径

- **《设计稿》全本** `docs-local/memory-architecture-design.md`——自包含、带贯穿例子(用户 A
  纠正"用 uv 别用 pip")。建议顺序:§3 数据模型 → §4-7 写链路 → §8 读链路 → §10 扩展点 →
  §11 中间件代码级 → 附录(目录树/真实 schema/迁移)。
- **`agents/memory/AGENTS.md`**——本模块的权威边界说明(身份解析、存储契约、迁移、检索、
  抽取安全、容量、远程后端、token 计数),比本章更细的约束都在这里。
- **`agents/memory/backends/README.md`**——换后端/加后端的操作手册(noop 六步模板)。
- **`config/memory_config.py` + `backends/deermem/deermem/config.py`**——共享与私有配置的分界,
  理解"为什么这样分层"的第一手材料。相邻章节:记忆中间件的 lead 链位见本书第 06 章(中间件管线)
  /第 07 章(上下文工程);`<memory>` 注入块的 run-level SHA-256 审计与摘要压缩,见第 05 章。

**一句话总结本章**:DeerFlow 的长期记忆 = 可插拔后端契约(`MemoryManager` 三层 + 目录发现 +
fail-loud)× 确定性写闸门(scope/durability/authority + 原子删除 + 双 revision)× 可解释淘汰
(staleness 复查 + confidence/hybrid-v1 + correction 特权)× 前缀缓存友好的冻结注入(静态 system
prompt + ID-swap)。旧书时代"一个 JSON 文件 + 一个中间件"已被完全重构——中间件变薄、后端变厚、
模型越权窗口被确定性代码收窄到零。
