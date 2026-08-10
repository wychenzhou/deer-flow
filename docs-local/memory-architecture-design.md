# 长期记忆系统架构设计(参考 DeerMem)

> 版本:v1.0 · 状态:设计稿(基于 DeerFlow 的 DeerMem 实现提炼)
>
> 本文是一份**自包含的架构设计文档**:读者不需要事先了解 DeerFlow 的任何原理。
> 它回答三个问题:记忆系统要解决什么、怎么存储、怎么读怎么写。通篇以一个例子贯穿——
> 用户 A 在对话里纠正 Agent「用 uv 别用 pip」,看这条信息如何变成长期记忆,又如何被未来的对话用上。

---

## 1. 问题与设计目标

### 1.1 问题

LLM 是无状态的:每次对话,模型只看到当前上下文窗口里的内容。这意味着:

- 用户换了新会话,Agent 不知道用户叫张三、偏好 `uv`、是前端工程师。
- 用户每次都要重新自我介绍、重新纠正 Agent 犯过的错。
- Agent 在长对话里学到的"这个用户的口头禅、这个项目约定"在会话结束后全部丢失。

**长期记忆系统**的目标就是:把值得跨会话保留的信息抽出来、存起来,并在合适的时机放回上下文,让 Agent 表现得"记得这个用户"。

### 1.2 设计目标

| 目标 | 含义 | 具体手段 |
|---|---|---|
| **免打扰** | 用户不用手动"教"Agent 记什么 | 后台自动抽取,不打断对话 |
| **高信噪比** | 只记住跨会话仍成立的信息,不记一次性任务噪音 | scope/durability/authority 写闸门 |
| **可纠正** | 用户纠正过后,旧记忆能被替换,新纠正优先 | contradiction 删除 + correction 类别 + guaranteed 注入 |
| **不膨胀** | 记忆会过期、会合并,不会无限增长 | staleness 生命周期 + consolidation + max_facts |
| **隔离** | 不同用户、不同 Agent 的记忆互不串 | (user_id, agent_name) 双维分桶 |
| **可靠** | 并发写不丢数据、冲突不静默覆盖 | 乐观锁 + 原子写 + fail-loud |
| **可观测** | 知道每次 run 用了哪份记忆 | run-level memory identity(SHA-256) |

### 1.3 非目标

- 不保存完整对话原文(那属于"历史记录",不是记忆)。
- 不做向量数据库级语义检索(检索是可选的辅助,不是核心)。
- 不承诺 100% 抽取准确(LLM 抽取有损,靠闸门兜底)。

---

## 2. 总体架构

一条记忆的完整旅程,拆成**写链路**(对话 → 记忆文件)和**读链路**(记忆文件 → 上下文)。

```
                ┌────────────────── 写链路(异步, 后台) ──────────────────┐
                │                                                         │
 用户发消息 → Agent 回复 → MemoryMiddleware                               │
                              │ 捕获本轮全部消息 + user_id + agent_name    │
                              ▼                                           │
                         MemoryQueue(debounce 30s)                        │
                              │ (thread,user,agent) 去重合并               │
                              ▼                                           │
                        MemoryUpdater                                     │
                              │ ① 组装 prompt(当前记忆+本轮对话)           │
                              │ ② LLM 抽取 → JSON                         │
                              │ ③ 闸门校验 → change set                   │
                              ▼                                           │
                       MemoryStorage(文件)                                 │
                              │ 锁 + 乐观 revision + 原子写                │
                              ▼                                           │
                     memory.json + facts/*.md  +  FTS5 索引 ──────────────┤
                                                                          │
┌────────────────── 读链路(同步, 每条消息) ────────────────┐              │
│ DynamicContextMiddleware                                │              │
│   → MemoryManager.get_context(user,agent)               │              │
│   → 摘要 + 事实 → 预算裁剪 → <memory> 块                │              │
│   → 注入首条 HumanMessage                               │              │
└──────────────────────────────────────────────────────────┘              │
                                                                          │
  下次对话:用户 A 说"帮我装个库" → Agent 看到 <memory> 里的                │
  "[correction] 用 uv 不用 pip" → 直接给 uv 命令 ──────────────────────────┘
```

**四个核心组件**,职责单一:

| 组件 | 职责 | 类比 |
|---|---|---|
| `MemoryMiddleware` | 对话结束时的"捕获器":拿到消息,交给管理器 | 收银台的扫码枪 |
| `MemoryQueue` | 批量/去重/背压:把多次对话合并成一次抽取 | 缓冲池 |
| `MemoryUpdater` | 大脑:组织 prompt、调 LLM、做闸门校验、算 change set | 分析员 |
| `MemoryStorage` | 手脚:锁、乐观锁、原子写文件 | 档案室 |

> 记忆相关的两个中间件(`MemoryMiddleware` / `DynamicContextMiddleware`)与摘要前的紧急冲刷 hook,如何逐行落地、如何与这四个核心组件分工,见 **§11 记忆相关中间件实现详解**。

**为什么队列/异步**:
- LLM 抽取有延迟和成本,不能阻塞用户看到回复。
- 多个快速来回可以合并成一次抽取(debounce),省 token。
- 抽取失败不影响对话本身(降级为"这次不记,下次再试")。

---

## 3. 数据模型:存什么、字段什么含义

记忆被设计成**两种互补的形态**:

1. **共享摘要(叙事型)** —— 一段段文字,描述用户的整体画像。
2. **事实(原子型)** —— 一条条可独立增删改的陈述,带置信度/类别/寿命。

分开的原因:**摘要要"合成"**(把很多信息揉成几句话),**事实要"原子"**(单条可精确更新/删除/纠正)。混在一起会导致:改一条事实要重写整段文本,或删一段话把有用的细节一起丢了。

### 3.1 用户摘要文件 `memory.json`(每用户一个)

```
backend/.deer-flow/users/{user_id}/memory.json
```

```json
{
  "version": "1.0",
  "revision": 7,
  "lastUpdated": "2026-08-07T12:00:00Z",
  "user": {
    "workContext":     { "summary": "数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目", "updatedAt": "2026-08-01T10:00:00Z" },
    "personalContext": { "summary": "中文母语, 能读写英文; 偏好简洁沟通",                    "updatedAt": "2026-07-20T09:00:00Z" },
    "topOfMind":       { "summary": "正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移; 持续关注向量数据库", "updatedAt": "2026-08-07T12:00:00Z" }
  },
  "history": {
    "recentMonths":       { "summary": "近三个月: 从 pandas 转向 polars; ...", "updatedAt": "2026-08-07T12:00:00Z" },
    "earlierContext":     { "summary": "去年: 主导过数据管道重构; ...",         "updatedAt": "2026-05-01T08:00:00Z" },
    "longTermBackground": { "summary": "十年后端经验; 关注数据工程",            "updatedAt": "2026-01-01T08:00:00Z" }
  }
}
```

> 注:每个摘要段是 `{summary, updatedAt}` 对象(不是裸字符串)——`updatedAt` 记录该段最后一次更新,便于判断"这段多久没动了"。**磁盘上的 `memory.json` 不存事实**(`facts` 只在内存兼容文档形态里出现,持久化时被剥离;事实在独立文件里)。

**关键设计:该文件故意不存事实,也不存事实索引。**

| 字段 | 含义 | 为什么存在 |
|---|---|---|
| `revision` | 共享乐观锁版本 | 并发写时检测"别人改过了吗" |
| `user.workContext` | 职业/公司/主技术栈(2-3 句) | 回答"用户是谁" |
| `user.personalContext` | 语言/沟通偏好/兴趣(1-2 句) | 回答"用户怎么沟通" |
| `user.topOfMind` | **多个**当前关注点(3-5 句,最常更新) | 回答"用户最近在意什么",最影响个性化 |
| `history.recentMonths` | 近 1-3 个月活动详述 | 短期模式 |
| `history.earlierContext` | 3-12 个月前模式 | 中期模式 |
| `history.longTermBackground` | 根本性背景 | 长期稳定事实 |
| 每段的 `scope`/`authority` | 抽取期的门禁标签(不落盘) | 见 §5.4 |

**为什么 six 段**:把"现在做什么 / 最近做什么 / 一直是什么"分层,既避免一条"当前在做 X"被当成永久事实,又能保留历史脉络。`topOfMind` 被设计成最容易被更新、也最影响个性化。

### 3.2 事实文件 `facts/*.md`(每 Agent 每事实一个)

```
backend/.deer-flow/users/{user_id}/agents/{agent_name}/facts/{sha256(fact_id)[:2]}/{fact_id}.md
```

> 分片 `sha256(fact_id)[:2]`(2 位十六进制 → 最多 256 个子目录):当事实数量很大时,避免单个目录里挤满文件,保护文件系统。

单条事实内容:

```markdown
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
  type: extraction
  threadId: "thread-abc"
---
```

**每个字段的含义与设计意图**:

| 字段 | 含义 | 谁写 | 为什么重要 |
|---|---|---|---|
| `id` | 稳定唯一标识 | 系统生成 | 供 `factsToRemove`/`memory_update` 精确寻址 |
| `schemaVersion` | 格式版本 | 系统 | 将来迁移格式有据可依 |
| `content` | 事实正文 | LLM | 语义核心 |
| `category` | 类别:preference/knowledge/context/behavior/goal/correction | LLM | 决定注入优先级、是否豁免过期、如何渲染 |
| `confidence` | 置信度 0-1 | LLM | 排序竞争注入预算;低于阈值不写 |
| `createdAt`/`updatedAt` | 时间戳 | 系统 | 寿命计算、staleness 判断 |
| `revision` | **单事实乐观锁** | 系统 | 并发改同一条事实时防覆盖 |
| `expected_valid_days` | 预期寿命(天) | LLM | 到期进入 staleness 复查 |
| `source` | 来源元数据 | 系统/用户 | 追溯;`type: manual` 表示用户手写(高信任,受 [MANUAL] 保护) |

**"六类 category"各自的设计意图**:

| category | 语义 | 设计意图 |
|---|---|---|
| `preference` | 偏好/反感的工具、风格 | 最常影响后续行为 |
| `knowledge` | 专长、掌握的技术 | 回答"用户会什么" |
| `context` | 背景事实(职位、项目) | 用户画像底色 |
| `behavior` | 工作模式、沟通习惯 | 长期稳定 |
| `goal` | 目标/方向 | 指导长线协助 |
| `correction` | 显式纠正或 Agent 错误 | **特权类别**:guaranteed 注入 + staleness 豁免 |

**一个关键设计决定:`scope`/`durability`/`authority` 不落盘。** 这三个标签只活在"抽取期",用完即弃(定义与校验见 §5.4、§5.5)。原因:它们是**决策输入**而非**数据**,落盘会污染数据模型、增加迁移负担;校验发生在写入那一刻就足够了。

---

## 4. 写链路第 1 步:捕获与消息过滤

### 4.1 捕获时机

`MemoryMiddleware` 挂在 Agent **一轮对话结束后**(`after_agent`)。它拿到整轮的 `messages` 和运行时身份:

```
thread_id  ← runtime.context["thread_id"]  或  configurable.thread_id
user_id    ← resolve_runtime_user_id(runtime)   ← 在请求上下文还活着时捕获
agent_name ← 当前 Agent 名(默认 __default__)
```

### 4.2 消息过滤(为什么这么过滤)

进入队列前,`_prepare_update` 做四步:

1. **只留 `HumanMessage` 和 `AIMessage`**。
   - 工具结果(`ToolMessage`)、系统消息、隐藏内部消息全部丢弃。
   - **为什么**:记忆抽取只需要"用户说了什么 + Agent 最终说了什么"。工具调用的中间结果进入长期记忆毫无意义,还白白烧 token。
2. **去掉纯寒暄**:「好的」「知道了」「ok」这类无信息量消息跳过。
   - **为什么**:它们无法提供可抽取的信息;留着只会让"必须有来有回"的判断失真。
3. **必须有至少一条用户消息和一条助手消息**,否则整体跳过。
   - **为什么**:没有交互就没有新信息,不值得花一次 LLM 调用。
4. **信号检测** `detect_signals`。
   - 用模式文件匹配最近几轮,识别 **6 类信号**:`correction`(用户纠正)、`reinforcement`(用户认可)、`preference`、`identity`、`goal`、`decision`。
   - **为什么**:correction 信号让抽取格外重视"纠正后的内容"——这是记忆里价值最高、最该保留的信息。
   - **识别机制详解(正则模式、扫描窗口、如何变成 prompt 提示段)见 §5.2.1**。

> **信号 ≠ 事实类别。** 信号是"用户当前在做什么对话行为"(检测于消息层面);事实类别是"这条信息是什么性质"(抽取后用于存储)。二者名字部分重合,但不一一对应——比如 `reinforcement` 是信号,却**没有** `reinforcement` 这个 category;它只用于提升已有事实的置信度,不单独建类存储。完整映射见 §5.2.1。
>
> **信号的本质**:正则匹配到用户行为 → 转成 prompt 里的一句"软提示"(如"用户在纠正你,建议 confidence≥0.95") → 引导 LLM 在抽取时更敏感、更对准。信号本身不直接产生事实,只影响抽取策略。

---

## 5. 写链路第 2 步:抽取(核心)

一次抽取 = **一次 LLM 调用**。调用发生在 debounce 队列的 Timer 线程上(异步、后台,不阻塞用户看到回复)。LLM 同时看到**四样输入**,吐回**一份 JSON**,涵盖**六类决策**:

```
输入(组装者: MemoryUpdater._prepare_update_prompt)
  ① 当前记忆快照     → <current_memory>   见 §5.1
  ② 本轮对话(过滤后) → <conversation>     见 §4.2
  ③ 提示段           → correction_hint / staleness_review_section / consolidation_section  见 §5.2
  ④ 系统指令         → 教模型怎么记 + 输出 JSON 格式  见 §5.3

输出(一份 JSON,解析器: _parse_memory_update_response)
  ① 摘要更新     → user / history 六段中哪些改、改成什么
  ② 新事实       → newFacts[]
  ③ 矛盾删除     → factsToRemove[](可带 replacement)
  ④ 过期删除     → staleFactsToRemove[](KEEP 不输出)
  ⑤ 过期延长     → staleFactsToExtend[](保留但重校复查窗口)
  ⑥ 碎片合并     → factsToConsolidate[]
```

设计上把"更新摘要、抽事实、删矛盾、判过期、做合并"**合并成一次调用**:
- 省 API 调用(每次对话只多一次 LLM)。
- 让模型同时看到"当前记忆 + 本轮对话",能做出**跨时间的判断**(比如"这条旧事实已被新对话推翻")。

### 5.1 输入①:当前记忆快照 —— 是什么、为什么不大

`<current_memory>` 里装的是**该 `(user, agent)` 桶当前完整记忆文档的 JSON 序列化**(`json.dumps(memory_data)`),即 §3 数据模型的**内存视图**:六段摘要 + 该 Agent 的事实列表。它**不是全库、不是所有用户、也不是原始对话历史**。

```json
{
  "revision": 7,
  "user": {
    "workContext":     { "summary": "数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目", "updatedAt": "2026-08-01T10:00:00Z" },
    "personalContext": { "summary": "中文母语, 能读写英文; 偏好简洁沟通",                    "updatedAt": "2026-07-20T09:00:00Z" },
    "topOfMind":       { "summary": "正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移",            "updatedAt": "2026-08-07T12:00:00Z" }
  },
  "history": {
    "recentMonths":       { "summary": "近三个月: 从 pandas 转向 polars; ...", "updatedAt": "2026-08-07T12:00:00Z" },
    "earlierContext":     { "summary": "去年: 主导过数据管道重构; ...",         "updatedAt": "2026-05-01T08:00:00Z" },
    "longTermBackground": { "summary": "十年后端经验; 关注数据工程",            "updatedAt": "2026-01-01T08:00:00Z" }
  },
  "facts": [
    { "id": "fact_uv",  "content": "用户偏好使用 uv 而非 pip 安装 Python 依赖", "category": "correction", "confidence": 0.98, "expected_valid_days": 365 },
    { "id": "fact_ctx", "content": "用户是后端工程师, 主用 Python",              "category": "context",    "confidence": 0.95, "expected_valid_days": 365 }
  ]
}
```

**上下文不会很大,因为有四道天然约束**:

| 约束 | 机制 | 效果 |
|---|---|---|
| **单桶** | 只序列化 `(user, agent)` 一个桶的记忆,不跨用户/Agent | 数据量 ≈ 一个人的画像,而非全系统 |
| **事实有上限** | `max_facts`(默认 100)按置信度截断;每条事实就是一句话 | 事实列表最多 ~100 行短文 |
| **摘要被 prompt 约束句数** | §5.3.1 的编写指南限定每段 2-6 句 | 六段摘要总计不过几段话 |
| **对话也被截断** | `format_conversation_for_update`:每条超过 1000 字符的消息只保留头 500 + 尾 500,中间 `...[truncated]...`;且只保留过滤后的 user+final-AI | 输入的对白被限制在可读规模 |

另外两点:
- **在后台线程跑**:抽取走 debounce 队列的 Timer 线程(§2),即使这段上下文不小,也不阻塞用户看到回复。
- **注入侧另有预算**:抽取 prompt 的 `current_memory`,与"模型实际看到的 `<memory>` 注入块"是两回事;注入块由 `format_memory_for_injection` 按 `max_injection_tokens`(默认 2000)预算裁剪(§8.2),两者互不干扰。

> 防注入细节:序列化前 `_escape_memory_for_prompt` 会把每个字符串叶子的 `<` `>` `&` 转义,防止记忆内容里的 `</current_memory><evil>` 逃出块边界、向抽取模型伪造权威区(issue #4044)。

### 5.2 输入②:三个提示段 —— 信号、过期复查、碎片合并

这三个变量都是**条件性**的:只有对应条件满足才渲染非空字符串。先讲清楚 §5.2.2 / §5.2.3 这两个"生命周期机制"**为什么存在**,否则"aged 候选、碎片候选、复查窗口"这些名字没有落点:

> **为什么需要"过期复查 + 碎片合并"。** 记忆是一个只增不减的仓库,放任不管会出三个问题:
> 1. **无限膨胀**:事实无限累积,而注入预算是固定的(`max_injection_tokens`,§8.2)——旧事实会挤掉新事实的预算。
> 2. **陈旧误导**:已经不再为真的旧事实(比如纠正之后还留着"用户用 pip")会持续带偏未来会话。
> 3. **碎片噪音**:同一方面被反复记成许多条小事实,重复又费 token。
>
> **过期复查**回答"记忆怎么变旧、怎么淘汰";**碎片合并**回答"记忆怎么去重、怎么变精炼"。两者都**搭便车**在正常抽取的同一次 LLM 调用里执行(不额外发 API),而且都是**代码先圈定候选、LLM 只能在候选里选**——模型没有越权删/合的窗口。

#### 5.2.1 correction / reinforcement 信号怎么识别

信号是**确定性正则匹配**,不是 LLM 判断。`detect_signals` 只扫描**最近 6 条 HumanMessage**(`messages[-6:]`),用模式文件做 `regex.search`:

```
correction.yaml(命中 → 用户纠正)            reinforcement.yaml(命中 → 用户认可)
  \bthat('s| is) (wrong|incorrect)\b          \byes[,.]?\s+(exactly|perfect|that('s| is) (right|correct|it))\b
  \byou misunderstood\b                       \bperfect(?:[.!?]|$)\b
  \btry again\b / \bredo\b                    \bexactly\s+(right|correct)\b
  不对 / 你理解错了 / 重试 / 重新来            \bthat('s| is) (exactly )?(right|correct|what i (wanted|needed|meant))\b
  换一种 / 改用                                对，就是这样 / 完全正确 / 就是这个意思 / 正是我想要的 / 继续保持
```

(完整清单在 `core/message_patterns/correction.yaml` 与 `reinforcement.yaml`;可用 `patterns_dir` 覆盖,加语言/业务短语不用改代码。注意"你说的对/没错"这类中文确认**刻意不在默认 reinforcement 里**——`对` 会误伤"对不起/对方",详见文件注释。)

识别出的信号(现在是 **6 类**:`correction` / `reinforcement` / `preference` / `identity` / `goal` / `decision`)被 `_build_signal_hints` 转成**软提示** `correction_hint`(名字是历史遗留,现在承载全部信号提示)塞进 prompt——比如命中 correction 时提示"记录为 confidence ≥ 0.95 的 correction 类事实,且仅当它是可复用的用户级偏好;对当前任务文件/方向的纠正是 thread/project 级,不得入库"。

**信号 → 事实类别的映射**(不是一一对应):

| 信号 | 含义 | 通常映射到 Category | 说明 |
|---|---|---|---|
| `correction` | 用户在纠正 Agent | `correction` | 直接对应;特权类别 |
| `reinforcement` | 用户在认可/确认 | *(不新建类别)* | 只提升已有事实的 confidence,或验证 preference/behavior |
| `preference` | 用户表达偏好/反感 | `preference` | 直接对应 |
| `identity` | 用户透露身份/背景 | `context` / `knowledge` | "我是后端工程师"→`context`;"我精通 Python"→`knowledge` |
| `goal` | 用户表达目标/方向 | `goal` | 直接对应 |
| `decision` | 用户做选择/决定 | `preference` / `goal` | "我决定用 uv"→`preference`;"我要完成迁移"→`goal` |

**一句话本质**:信号 = "检测用户行为 → 转成 prompt 里的软提示 → 引导 LLM 按正确策略抽取并归类"。它不直接产生事实,只影响抽取策略(如 correction 要求 confidence≥0.95;reinforcement 要求验证而非新建)。

**信号只是提示,不是裁决**:它提高模型对"纠正/认可"内容的敏感度,但最终能不能入库,由 §5.5 的**确定性写闸门**逐条判定。信号还有第二个用途——**背压豁免**:带信号的更新在队列满时也总是被允许入队(§4 队列设计),保证高价值记忆不被丢。

#### 5.2.2 staleness_review_section:过期复查

**为什么有"复查窗口"**:每条事实创建时,LLM 都要估计"这条事实大概多少天后会变得不再准确",这个估计值就叫它的**复查窗口**。窗口到期**只意味着"该重新看一眼这条事实了",绝不意味着"它死了"**——年龄大不等于错误(母语、核心技能的事实可以无限成立)。所以窗口是一个"到期提醒",不是判决;真正的判决(留/删/延)在复查那一刻,由 LLM 结合当前记忆和本轮对话来做。

**为什么每条事实单独一个窗口,而不是全局统一一个年龄**:事实的易变度差别巨大——

| 事实类型 | 实际寿命 | 例子 |
|---|---|---|
| 非常稳定 | 数年 | 母语、核心技能、性格、长期职业背景 |
| 稳定 | 半年到一年 | 当前角色、常用技术栈、偏好 |
| 短期 | 几天到几周 | "正在迁移到 uv"、"当前实验" |
| 高度临时 | 一天内 | 当前 bug、今天的关注点 |

一个全局阈值对这两类都不合适:阈值太短会把稳定事实提前送审(浪费复查次数和 token),阈值太长会让易变事实长期误导(危险)。所以**窗口由 LLM 创建时逐条赋值**(§5.3.2 的档位表),系统只负责"到期提醒 + 复查"。

**复查窗口的实际取值**(代码 `_effective_fact_staleness_age`):

```
effective_age(复查窗口, 天) =
  expected_valid_days   若该事实创建时 LLM 赋了值(写盘时已被钳制到上限,见下)
  staleness_age_days(默认 90)   否则 —— 兜底: 老版本事实或 LLM 没给值
```

**完整判定流程(五步)** —— 注意第 ①、② 步是代码决定"查不查、查哪些",第 ③ 步才是 LLM,第 ④、⑤ 步又是代码决定"提议能否生效":

```
① 圈定候选(代码, 确定性)   _select_stale_candidates
   对每条既有事实:
     effective_age = expected_valid_days(若有) 否则 staleness_age_days(90)
     createdAt + effective_age < now        ← 已过自己的复查窗口
     且类别不受保护(correction 直接被排除)  ← 才进候选

② 触发阈值(代码)
   候选数 >= staleness_min_candidates(3) → 生成复查段塞进 prompt
   不足 → 本轮不复查, 攒够再说

③ LLM 三选一(每个候选, 只填删/延, KEEP 不填)
   KEEP   → 不输出任何条目
   REMOVE → staleFactsToRemove[{id, reason}]
   EXTEND → staleFactsToExtend[{id, extend_by_days, reason}]

④ 交集校验(代码, 防越权)
   实际删除集 = LLM 提议删除的 id ∩ 候选集
   实际延长集 = LLM 提议延长的 id ∩ 候选集
   → 越权提议(protected 类别 / 根本没到期的)被静默丢弃

⑤ 应用规则(代码)
   删除: 每周期上限 staleness_max_removals_per_cycle(10);
         超了按置信度从低到高保留(先删最"可疑"的)
   延长: 新窗口 = min(距创建天数 + extend_by_days, staleness_max_extension_days);
         被提议删除的事实绝不同时被延长(防 LLM 自相矛盾)
```

**完整示例**(假设今天是 2026-08-08,该桶记忆里 4 条事实):

| id | content | category | confidence | createdAt | expected_valid_days |
|---|---|---|---|---|---|
| F1 | 用户使用 pip | context | 0.60 | 2026-01-01 | 60 |
| F2 | 用户是后端工程师 | context | 0.95 | 2025-01-01 | 365 |
| F3 | 用户偏好 uv | correction | 0.98 | 2026-08-07 | 365 |
| F4 | 用户用 polars 做数据处理 | knowledge | 0.70 | 2026-02-01 | 90 |

- **① 圈定**:F1 窗口 60 天 → 2026-03-02 已过 ✓;F2 窗口 365 天 → 2026-01-01 已过 ✓;F3 是 correction → 受保护,排除 ✗;F4 窗口 90 天 → 2026-05-02 已过 ✓。候选 = {F1, F2, F4}。
- **② 触发**:候选数 3 ≥ 3 → 生成复查段:

```
<stale_facts>
- [F1 | context    | 0.60 | 2026-01-01 | valid:60d ] "用户使用 pip"
- [F2 | context    | 0.95 | 2025-01-01 | valid:365d] "用户是后端工程师"
- [F4 | knowledge  | 0.70 | 2026-02-01 | valid:90d ] "用户用 polars 做数据处理"
</stale_facts>
```

每行末尾的 `valid:Nd` 是该事实的复查窗口,让模型校准保守度:`valid:30d` 说明当初就视为易变,`valid:365d` 说明当初视为稳定。

- **③ LLM 判定**(示例):F1 → REMOVE(用户已纠正为 uv,pip 过时);F2 → KEEP(仍是后端工程师);F4 → EXTEND(仍成立,但 valid:90d 低估了稳定性):

```json
"staleFactsToRemove": [ { "id": "F1", "reason": "用户已纠正为 uv, pip 事实过时" } ],
"staleFactsToExtend":  [ { "id": "F4", "extend_by_days": 365, "reason": "polars 是长期技术栈, 原窗口过短" } ]
```

- **④ 交集校验**:删除 {F1} ∩ {F1,F2,F4} = {F1} ✓;延长 {F4} ∩ 候选 = {F4} ✓。若模型越权提议删 F3(受保护类别),{F3} ∩ 候选 = ∅ → 丢弃,F3 安然无恙。
- **⑤ 应用**:F1 被删;F4 新窗口 = min(距创建约 188 天 + 365, `staleness_max_extension_days`) = 553 天;F2 保持不动。

**这个例子浓缩了三个关键设计**:

| 现象 | 设计意图 |
|---|---|
| F2 已过窗口却被 KEEP | 窗口只是"到期提醒",不是判决;年龄大 ≠ 错误 |
| F3 是纠正类,根本不在候选里 | 保护类别在圈定时就被排除;即使 LLM 越权提议,第 ④ 步交集也会滤掉它 |
| 判定与执行之间隔着四道确定性护栏 | 交集校验、每周期上限、防自相矛盾、EXTEND 绝对上限——全是代码,与模型行为无关 |

**护栏与开关解耦**:第 ④、⑤ 步的交集校验、每周期上限、防自相矛盾是 apply 层的确定性代码,**即使 `staleness_review_enabled` 被关闭也照常执行**——此时模型拿不到候选段、正常不会提议,等同空转。保护独立于模型行为,也独立于功能开关。

**各变量含义**:

| 变量 | 默认 | 含义 |
|---|---|---|
| `expected_valid_days` | LLM 创建时赋 | **该事实自己的复查窗口**(天)。LLM 按档位表赋值,写盘时被钳制到上限(见下) |
| `staleness_age_days` | 90 | **全局兜底复查年龄**:没有 `expected_valid_days` 的事实(老版本 / 模型没给值)拿它当窗口 |
| `staleness_max_lifetime_multiplier` | 20.0 | **创建时钳制系数**:新事实的 `expected_valid_days` 被钳到 ≤ `staleness_age_days × 20`(90×20=1800 天≈5 年),防止模型给一个"永不复查"的寿命 |
| `staleness_min_candidates` | 3 | **触发阈值**:aged 候选不足这条就不启动复查——1-2 条的复查不值得花一次提示词开销,攒够再查 |
| `staleness_max_removals_per_cycle` | 10 | 单轮最多删条数;LLM 提议超额时按置信度从低到高保留(先删最可疑的) |
| `staleness_max_extension_days` | 3650 | **延长绝对上限**:第 ⑤ 步里新窗口被钳到 ≤ 它(默认 10 年),防一次延长把事实永久雪藏 |
| `staleness_protected_categories` | `["correction"]` | 受保护类别:correction 是显式用户反馈,永不因过期被自动清掉 |
| `createdAt` | 系统写 | 事实创建时间,过期判定的时间基准 |

#### 5.2.3 consolidation_section:碎片合并

**为什么有碎片合并**:同一方面的信息,会被反复记成许多条**小事实(碎片)**。比如用户多次聊到数据工作,就攒下 `fact_a`"用户用 Python"、`fact_b`"用户写过 pandas 脚本"、`fact_c`"用户关注 polars"……十条各自一句话。后果是:

- 每条都要占用注入预算(§8.2 的 `max_injection_tokens`);
- 内容彼此重叠,读起来重复、噪音大。

合并 = 把同一方面的多条碎片**合成一条更丰富的陈述**,只保留一份,保留所有关键细节。**但合并是有损的**:源事实被删除,只留 `consolidatedFrom` 记录来源 id——原始逐条细节不再单独存在。所以**默认关闭**(`consolidation_enabled: false`),需要显式开启。

**完整判定流程(五步)** —— 与过期复查同一结构:①、② 是代码圈定候选,③ 才是 LLM,④、⑤ 又是代码把关:

```
① 圈定候选(代码, 确定性)   _select_consolidation_candidates
   按 category 分组事实;
   某类别条数 >= consolidation_min_facts(8) → 候选组
   protected 类别(correction)排除(与过期复查同一套保护)

② 展示(代码)
   候选组按条数从多到少排序, 取前 consolidation_max_groups_per_cycle(3) 组
   每组最多展示 consolidation_max_sources(8) 条

③ LLM 判定(每个候选组, 只填合并, SKIP 不填)
   CONSOLIDATE → factsToConsolidate[{sourceIds, consolidated}]
   SKIP        → 不输出

④ 执行守卫(代码, 全确定性)
   对每个 sourceIds:
     · 全部真实存在于事实索引      ← 源必须存在
     · 全部属于候选组              ← 不能合并保护类别/非候选事实
     · 未被本周期其他合并消费      ← 组间不重叠
     · 2 ≤ 条数 ≤ max_sources     ← 组大小
   对每个 consolidated:
     · 过同一套 scope/durability/authority 闸门
     · confidence = min(LLM 给的, 源事实最大置信度)   ← 不能凭空拔高
     · 结果置信度 ≥ 阈值才写

⑤ 落盘
   源事实被删, 新合成事实带 consolidatedFrom=[源 ids]  (审计用, 不含原文)
   createdAt 继承最新源事实     ← 过期时钟反映真实信息年龄, 不是合成时刻
   expected_valid_days 按"最早复查期限"继承  ← 易变细节不会被稳定事实带偏
```

**完整示例**(knowledge 类别攒了 9 条,达到阈值):

```
F1 用户用 Python        F4 用户做过数据管道    F7 用户做过 ETL
F2 用户写过 pandas      F5 用户懂 SQL          F8 用户写过数据可视化
F3 用户关注 polars      F6 用户了解分布式计算   F9 用户用 ClickHouse
```

- **① 圈定**:knowledge 9 条 ≥ 8 → 候选组;correction 等其他类别不管多少条都不参与。
- **② 展示**:9 条里最多展示 8 条(取前 `consolidation_max_sources`)。
- **③ LLM 判定**(示例):CONSOLIDATE,`sourceIds=["F1","F2","F3","F4","F5"]`,`consolidated="用户有多年 Python 数据栈经验: pandas/polars 做数据处理, 懂 SQL, 做过数据管道"`。
- **④ 守卫**:5 个源都在事实索引、都属于 knowledge 候选组、都未被消费、2≤5≤8 ✓;置信度:源最大 0.85,LLM 说 0.9 → 钳到 **0.85** ✓。
- **⑤ 落盘**:F1-F5 被删,新增一条带 `consolidatedFrom=["F1","F2","F3","F4","F5"]` 的合成事实;F6-F9 保持不动。

再看两个"被拦下"的场景:若模型在**另一个**合并决策里又用 F3 —— 它已被第一组消费 → **拒绝**;若把 correction 类事实 F10 也塞进 sourceIds —— 它不在候选组 → **拒绝**。合并是"宁缺毋滥",第 ④ 步把每一次越权都挡在写盘之前。

**各变量含义**:

| 变量 | 默认 | 含义 |
|---|---|---|
| `consolidation_enabled` | false | 总开关。默认关,因为合并有损(源事实被删,只剩 `consolidatedFrom` id) |
| `consolidation_min_facts` | 8 | **触发阈值**:某类别至少攒够几条才值得合并——低于它,展示这批碎片的提示词开销不划算 |
| `consolidation_max_groups_per_cycle` | 3 | 单轮最多让模型处理几个候选组,防止过度合并 |
| `consolidation_max_sources` | 8 | 每组最多展示多少条源事实,防止一次把太多细节揉没 |
| `sourceIds` | 模型在决策里填 | 合并决策指认的源事实 id 列表 |
| `consolidated` | 模型在决策里填 | 合成后的替代事实:`content/category/confidence/scope/durability/authority` |
| `consolidatedFrom` | 系统落盘时写 | 记在合并结果上的来源 id 列表(仅 id,不含原文),用于审计 |

**一个开关全程关死**:`consolidation_enabled` 为 `false` 时,提示段不生成(模型看不到合并候选),apply 层的整个合并处理块也被跳过——从提示到落盘整条路径都不存在。这与过期复查不同(staleness 的 apply 层护栏即使开关关闭也照常执行),合并是**被一个开关整条关死**。

### 5.3 输入③:系统指令 —— 教模型"怎么记"

`memory_update.chat.yaml` 是 chat 模板,`system` 消息是**完全静态**的(没有变量,只有字面 `{{ }}` JSON 花括号),每次调用渲染结果逐字节相同——与 lead agent 的静态系统提示词一样,天然前缀缓存友好。它承担了绝大部分"教模型怎么记"的职责,分四块:

#### 5.3.1 六段摘要的编写指南

| 段 | 定位 | 句数/篇幅 | 时间窗 | 示例(来自模板原文) |
|---|---|---|---|---|
| `workContext` | 职业、公司、活跃项目、主技术栈 | 2-3 句 | 当前 | "Core contributor, project names with metrics (16k+ stars), technical stack" |
| `personalContext` | 语言、沟通偏好、关键兴趣 | 1-2 句 | 当前 | "Bilingual capabilities, specific interest areas, expertise domains" |
| `topOfMind` | **多个**进行中的关注点和优先级 | 3-5 句详述 | 最近、更新最频繁 | "Primary project work, parallel technical investigations, ongoing learning/tracking"——专门强调"捕捉若干个**并发**的主题,不是一个任务" |
| `recentMonths` | 近期活动详述 | 4-6 句或 1-2 段 | 近 1-3 个月 | 探索过的技术、做过的项目、解决的问题、展现的兴趣 |
| `earlierContext` | 略早但仍相关的重要模式 | 3-5 句或 1 段 | 3-12 个月前 | 过去的项目、学习历程、已形成的模式 |
| `longTermBackground` | 持续的根本背景 | 2-4 句 | 一直如此 | 核心专长、长期兴趣、根本工作方式 |

模板里的 **"What Goes Where"** 再做一遍分工确认:`topOfMind` 明确是"最近在意的 3-5 个并发主题(主工作、副线探索、学习/关注兴趣)",并注明**更新最频繁**;`recentMonths` 详述近期技术探索;`longTermBackground` 只放不变的事实。还有两条**演化规则**:**更新 topOfMind 时整合新主题、移除已完成/放弃的**(保持 3-5 个活跃主题);**历史段按时间窗做时间线整合**,而不是覆盖。

#### 5.3.2 结构化反思 + 事实提取

抽取前,系统指令要求模型先做**结构化反思**,把对话分类后再决定记什么:

| 反思项 | 问什么 | 记什么 / 不记什么 |
|---|---|---|
| 错误/重试检测 | Agent 是否出错、需要重试、产出错误? | 仅在它是**可复用的用户级工作模式**时,记录根因和正确做法 |
| 用户纠正检测 | 用户是否纠正了 Agent 的方向/理解/输出? | 区分"可复用的用户级纠正"与"对当前任务事实/文件的修正";仅当 category=correction 且错误明确时才填 `sourceError` |
| 项目约束发现 | 本对话是否发现了项目级约束? | 归为 project-scope,**绝不提升**到用户记忆 |

事实提取的指南(模板原文语义):

- **类别**:`preference`(偏好/反感) `knowledge`(专长/掌握) `context`(背景事实) `behavior`(工作模式) `goal`(目标) `correction`(显式纠正,含正确做法)
- **寿命 `expected_valid_days`**:`<=14` 高度临时(当前 bug/今天的事);`15-60` 短期(进行中的实验);`60-180` 中期(当前角色/技术栈);`180-365` 稳定(职业背景);`>365` 非常稳定(核心技能/母语)。超过服务端上限(`staleness_age_days × staleness_max_lifetime_multiplier`)的数值写盘时被静默下调
- **置信度**:`0.9-1.0` 显式陈述("我在做 X");`0.7-0.8` 从行为/讨论强暗示;`0.5-0.6` 推断(慎用,仅用于清晰模式)
- **不建事实**:当前任务目标、验收标准、工作区状态、当前文件/commit/错误状态、项目级约束、一次性操作授权、文件上传事件
- **每条事实/每段摘要都必须带 `scope` / `durability` / `authority`**(定义见 §5.4;评估由确定性写闸门做,不持久化)

#### 5.3.3 输出格式:一份 JSON,六类决策

模板的 **"Output Format (JSON)"** 段定义了模型必须返回的**完整 JSON schema**——这就是 §5 开头六类决策的落点。文档旧版把它省略了,造成"只知道让模型记,不知道它返回什么":

```json
{
  "user": {
    "workContext":     { "summary": "...", "shouldUpdate": true,  "scope": "user", "authority": "descriptive" },
    "personalContext": { "summary": "...", "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "topOfMind":       { "summary": "...", "shouldUpdate": true,  "scope": "user", "authority": "descriptive" }
  },
  "history": {
    "recentMonths":       { "summary": "...", "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "earlierContext":     { "summary": "...", "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "longTermBackground": { "summary": "...", "shouldUpdate": false, "scope": "user", "authority": "descriptive" }
  },
  "newFacts": [
    { "content": "...", "category": "preference|knowledge|context|behavior|goal|correction",
      "confidence": 0.9, "expected_valid_days": 90,
      "scope": "user|thread|project", "durability": "durable|temporary", "authority": "descriptive|transactional" }
  ],
  "factsToRemove": [
    { "id": "fact_xyz", "scope": "user|thread|project", "reason": "explicit user-level contradiction", "replacementFactIndex": 0 }
  ],
  "staleFactsToRemove": [ { "id": "fact_xyz", "reason": "brief explanation" } ],
  "staleFactsToExtend":  [ { "id": "fact_xyz", "extend_by_days": 365, "reason": "brief explanation" } ],
  "factsToConsolidate": [
    { "sourceIds": ["fact_a", "fact_b"],
      "consolidated": { "content": "synthesized fact", "category": "knowledge", "confidence": 0.9,
                        "scope": "user", "durability": "durable", "authority": "descriptive" } }
  ]
}
```

**一个贯穿始终的完整示例**(接附录的用户 A / uv 纠正):

```json
{
  "user": {
    "workContext":     { "summary": "数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目", "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "topOfMind":       { "summary": "正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移",                "shouldUpdate": true,  "scope": "user", "authority": "descriptive" },
    "personalContext": { "summary": "中文母语, 能读写英文; 偏好简洁沟通",                        "shouldUpdate": false, "scope": "user", "authority": "descriptive" }
  },
  "history": {
    "recentMonths":       { "summary": "近三个月: 从 pandas 转向 polars; ...", "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "earlierContext":     { "summary": "去年: 主导过数据管道重构; ...",         "shouldUpdate": false, "scope": "user", "authority": "descriptive" },
    "longTermBackground": { "summary": "十年后端经验; 关注数据工程",            "shouldUpdate": false, "scope": "user", "authority": "descriptive" }
  },
  "newFacts": [
    { "content": "用户偏好使用 uv 而非 pip 安装 Python 依赖", "category": "correction",
      "confidence": 0.98, "expected_valid_days": 365,
      "scope": "user", "durability": "durable", "authority": "descriptive",
      "sourceError": "先前建议使用 pip install" }
  ],
  "factsToRemove": [
    { "id": "fact_z", "scope": "user", "reason": "用户显式纠正: 不再使用 pip", "replacementFactIndex": 0 }
  ],
  "staleFactsToRemove": [],
  "staleFactsToExtend": [],
  "factsToConsolidate": []
}
```

**输出被怎么消费**(三段管线,逐层收紧):

```
① 容错 JSON 提取   _parse_memory_update_response
   模型可能包 thinking/散文/markdown fence; 解析器逐个 '{' 尝试 raw_decode,
   找到第一个含 user+history+newFacts 三个顶层键的合法 JSON 对象。
② 规范化           _normalize_memory_update_data
   把模型产物转成 apply 层认识的形状: 逐条校验 content/category/confidence/
   expected_valid_days/scope/durability/authority, 丢弃畸形条目。
   关键安全规则: factsToRemove 存在而 newFacts 全部畸形 → 整体抛错(绝不执行"只删不写")。
③ 应用             _apply_updates
   闸门 → 去重 → max_facts 裁剪 → 冲突删除 → 合并, 见 §5.5 与 §6。
```

> 模板存放在 `core/prompts/memory_update.chat.yaml`(chat 格式,`system`+`user` 两条消息);可用 `backend_config.prompts_dir` 覆盖,支持按 Agent 分子目录(`{prompts_dir}/{agent_name}/memory_update.chat.yaml`)。§5.2 的两个 text 模板是 `staleness_review.yaml` / `consolidation.yaml`。

### 5.4 字段速查(这些字段从哪来、什么意思)

| 字段 | 含义 | 谁写 | 在哪校验 | 示例 |
|---|---|---|---|---|
| `scope` | 信息适用范围:`user` 跨会话/跨任务 / `thread` 仅本次 / `project` 仅某项目 | LLM 抽取 | 写闸门(§5.5)要求 `user` | `"scope": "user"` |
| `durability` | 是否预期跨会话仍为真:`durable` / `temporary` | LLM 抽取 | 写闸门要求 `durable` | `"durability": "durable"` |
| `authority` | 描述性 vs 授权性:`descriptive`(描述)/ `transactional`(指令/授权) | LLM 抽取 | 写闸门要求 `descriptive`;transactional 永不入库 | `"authority": "descriptive"` |
| `shouldUpdate` | 该段摘要是否有**实质新信息** | LLM 抽取 | apply 层:false 或缺失 → 跳过该段 | `"shouldUpdate": true` |
| `summary` | 摘要正文(六段之一) | LLM 抽取 | 该段 scope/authority 过闸门才写入 | 见 §5.3.1 |
| `category` | 事实类别(六选一) | LLM 抽取 | 决定注入优先级、staleness/consolidation 豁免 | `"category": "correction"` |
| `confidence` | 置信度 0-1 | LLM 抽取 | 低于 `fact_confidence_threshold`(默认 0.7)不入库 | `"confidence": 0.98` |
| `expected_valid_days` | 预期寿命(天),到期进复查 | LLM 抽取 | 写盘时钳制到 ≤ `staleness_age_days × staleness_max_lifetime_multiplier` | `"expected_valid_days": 365` |
| `sourceError` | correction 类事实记录"之前错在哪" | LLM 抽取(仅 correction) | 渲染成 `(avoid: ...)`;非 correction 不带 | `"sourceError": "先前建议 pip"` |
| `replacementFactIndex` | `factsToRemove` 条目的零基索引 → `newFacts` 里替代它的那条 | LLM 抽取 | apply 层:替代品存活才允许删旧(§6.1) | `"replacementFactIndex": 0` |
| `newFacts[]` | 新事实列表 | LLM 抽取 | 逐条闸门 + 去重 + `max_facts` 裁剪 | 见 §5.3.3 |
| `factsToRemove[]` | 矛盾删除列表(id + scope + reason) | LLM 抽取 | 删除闸门:scope=user 且有 reason(§6.1) | 见 §5.3.3 |
| `staleFactsToRemove[]` / `staleFactsToExtend[]` | 过期复查:删 / 延 | LLM 在复查段判 | 与候选集求交集(§5.2.2) | `{"id":"...","extend_by_days":365}` |
| `factsToConsolidate[]` | 碎片合并决策 | LLM 在合并段判 | 源存在/不重叠/置信度守卫(§5.2.3) | 见 §5.3.3 |

**配置字段(系统定义,不是 LLM 产出)**:`staleness_age_days`(90)、`fact_confidence_threshold`(0.7)、`max_facts`(100)、`staleness_min_candidates`(3)、`staleness_max_removals_per_cycle`(10)、`staleness_protected_categories`(`["correction"]`)、`consolidation_min_facts`(8)等——这些"为什么这么设计、每个默认值什么意思",已分别在 **§5.2.2(过期复查变量表)** 和 **§5.2.3(碎片合并变量表)** 逐条解释。

### 5.5 写闸门:确定性 + 逐条 fail-closed

抽取的产物是 LLM 输出,LLM 可能违背指令。所以系统在**写盘之前**用**纯代码**再做一次校验(不依赖 LLM 自觉):

```
事实入库三条件(缺一不可,均为确定性判断):
  scope      == "user"        ← 能跨会话/跨任务成立
  durability == "durable"     ← 预期长期为真
  authority  == "descriptive" ← 是描述,不是指令
```

示例判断:

| 候选内容 | scope | durability | authority | 结果 |
|---|---|---|---|---|
| "用户偏好 uv 而非 pip" | user | durable | descriptive | ✅ 入库 |
| "当前任务是把 CSV 转 JSON" | thread | temporary | descriptive | ❌ 拒(一次性) |
| "用户说可以删除生产库" | user | durable | **transactional** | ❌ 拒(指令/授权) |
| "项目约定用 ruff" | **project** | durable | descriptive | ❌ 拒(项目级) |

**"逐条 fail-closed" 是什么**:

- **fail-closed = 默认拒绝**:校验不通过就拒绝,而不是默认放行。系统绝不因为"模型看起来没问题"就跳过校验。
- **逐条 = 判定粒度是单条事实 / 单段摘要,不是整批**:一条坏数据只拒绝它自己,**不影响同批其他合法事实入库**;也绝不反过来——**不会因为"整体还行"就放过某一条**。每个判定按原因(missing / scope / durability / authority)分桶计数(`scope_gate_rejections`),拒绝率可通过 `rejected_by_scope_gate` 指标观测。
- **方向性保守**:拒绝一条的代价只是"这次没记到"(下一轮对话会重抽,见 §2 的降级语义);入库一条的代价却是"错误个性化持续污染未来每次注入"。所以**宁可漏记,不可错记**。
- 摘要段、事实、删除条目各有独立闸门函数(`_summary_scope_gate_reason` / `_fact_scope_gate_reason` / `_removal_scope_gate_reason`),规则同源、按类型分别执行。

到这里,抽取这一步就完整了:模型拿到"当前记忆 + 对话 + 提示段 + 系统指令",返回一份 JSON;解析器容错提取、规范化,然后交给 **§6 的决策层**做矛盾删除、去重与上限的确定性守卫,最后 **§7 的存储层**做乐观锁 + 原子落盘。

---

## 6. 写链路第 3 步:冲突与决策

### 6.1 语义冲突(新旧矛盾)

LLM 用 `factsToRemove` 表达"这条旧事实被推翻了",可带 `replacementFactIndex` 指向替代它的新事实。

**处理规则(关键)**:
- 删除条目必须 `scope=user` 且有 `reason`,否则拒绝。
- **带 replacement 的删除是原子的**:只有替代它的新事实**通过了闸门、去重、max_facts 裁剪**后,旧事实才被删。
  - 为什么要这样:如果"删旧的"和"写新的"分离,可能出现"旧删了,新却没写进去"——数据丢失。把两者绑成一个原子决策,要么都成,要么都不动。
- task/project 级事实不允许 LLM 单方面删除(fail-closed)。

**示例**:旧事实 `fact_z("用户用 pip")`,新对话纠正后:
```json
"factsToRemove": [ { "id": "fact_z", "scope": "user",
                     "reason": "用户显式纠正: 不再使用 pip",
                     "replacementFactIndex": 0 } ],
"newFacts": [ { "content": "用户偏好使用 uv 而非 pip", "category": "correction", ... } ]
```
→ 只有当 `newFacts[0]` 通过闸门后,`fact_z` 才被删除。

---

## 7. 写链路第 4 步:并发与持久化

### 7.1 为什么用文件,而不是数据库

单机场景下,文件方案有清晰优势:每条事实一个文件 → 原子增删、可 git diff、可人工编辑、坏一个不影响其他、零依赖。用乐观锁解决并发。

### 7.2 乐观锁协议

```
写入流程:
  1. 取用户级锁(进程内 RLock + 跨进程文件锁 fcntl/flock 或 msvcrt)
  2. 读 memory.json 当前 revision(共享版本)
  3. 校验每个目标事实的 per-fact revision 仍满足 precondition
     ("该事实不存在" 或 "revision == 我读到的值")
  4. 写:memory.json(revision+1) + 每个事实的 .md(per-fact revision+1)
  5. 冲突处理:
     ├─ MemoryManifestRevisionConflict / MemoryFactRevisionConflict
     ├─ 若满足 disjoint-rebase(只动事实不动摘要,且每个事实 precondition 仍满足)
     │    → 最多重试 2 次(rebase)
     └─ 否则 → 抛错(fail-loud),Gateway 映射为 HTTP 409
```

**双 revision 的设计**:
- **共享 revision**(memory.json)保护"摘要"这个共享资源:任何写都要基于最新版本。
- **per-fact revision**(每个 .md)保护单个事实:两条并发更新各改一条不同的事实,可以安全 rebase。

### 7.3 原子性与可恢复性

- 原子替换:写临时文件 → `os.replace()`(POSIX 下再 fsync 父目录)。
- 可恢复日志:目标文件写入走 journal,中途崩溃可恢复。
- **fail-loud 原则**:memory 是持久状态,冲突绝不静默回退到别的后端或静默丢弃——报错让上层(HTTP 409)明确知道。

---

## 8. 读链路:召回与注入

> **先给结论,避免被"召回"二字带偏。** 系统里只有**两种召回机制**,由 `memory.mode` 决定:
> - **middleware(默认):不是关键词、也不是向量**。`get_context()` 把该 `(user, agent)` 桶的**全部**记忆读出来,按置信度排序,在 token 预算内"能带多少带多少"——**根本不检索**(§8.2)。
> - **tool(实验):才是真正的检索**。模型主动调 `memory_search(query)`,走 SQLite **FTS5 全文索引**(关键词/全文检索,§8.4)。
>
> 一句话:**middleware = "记得多少带多少"(置信度截断);tool = "需要时按关键词查"(FTS5)。**

### 8.1 召回时机

`DynamicContextMiddleware` 在**每次模型调用前**(每条用户消息进入)执行读链路,经 `_get_memory_context` 调用 `get_context()`:

```
get_context(user_id, agent_name)                       # DeerMem 实现
  → get_memory_data(agent_name, user_id)               # ① 读该 (user, agent) 桶的完整记忆文档
  → format_memory_for_injection(memory_data, ...)      # ② 格式化 + 预算裁剪(§8.2)
  → 返回正文文本(不含 <memory> 包裹)

_get_memory_context(...)                               # lead_agent/prompt.py 调用侧
  → 用 <memory>...</memory> 包一层
  → DynamicContextMiddleware 注入首条用户消息前(§11.3 的 ID 交换)
```

**为什么 read 是"全量读 + 格式化"而不是"查询"**:注入路径要快、要确定,不能引入额外的一次 LLM 调用或检索依赖——置信度已经是"这条信息有多可靠"的最好代理(见 §8.2 设计理由)。

### 8.2 middleware 模式:全量召回 + 置信度排序 + 预算截断

#### 8.2.1 无查询词,只有"预算内择优"

默认模式下,**根本没有查询词、没有向量、没有语义排序**。`format_memory_for_injection` 做三件事:

```
① 摘要(六段): 无条件全部注入
② 事实排序:   按 confidence 从高到低(严格顺序)
③ 预算截断:   guaranteed 类别先放 → 剩余主预算按序贪心, 遇到放不下的第一条就停
```

被截断的事实**不查、不用**——每次注入重新读盘重新算,记忆文件本身不受影响。

#### 8.2.2 预算分配:双预算结构与裁减规则

##### 双预算结构

注入采用**主预算 + 特权预留**的双池设计,避免高价值事实在预算竞争中被挤掉:

| 预算池 | 默认值 | 用途 | 超支行为 |
|---|---|---|---|
| **特权预留** (`guaranteed_token_budget`) | 500 tokens | `guaranteed_categories` 专属(默认仅 `correction`) | 超支部分**向上溢出**到主预算,且自身绝不被截断 |
| **主预算** (`max_tokens`) | 2000 tokens | 摘要(六段) + 普通事实 | 按置信度贪心填充,满即停 |

> **为什么 correction 必须是特权**:用户纠正的信息价值密度最高——"别再犯同样的错"如果因预算满了而被漏掉,代价远大于少带一条普通偏好。预留区保证它**无论主预算多满都一定在场**。

##### 填充顺序(三步)

```
Step 1: guaranteed 事实(correction) → 先占特权预留(500)
        └─ 实际用量 ≤ 500: 只花预留,不碰主预算
        └─ 实际用量 > 500: 超出部分向上挤占主预算
Step 2: 摘要(六段) → 无条件全部进入主预算
Step 3: 普通事实 → 按 confidence 从高到低依次进主预算,直到主预算用尽
```

##### 上限的两种场景

| 场景 | guaranteed 用量 | 总输出上限 | 说明 |
|---|---|---|---|
| **常态** | ≤ 500(预留内) | `max_tokens` (2000) | guaranteed 行只是**替换**了同等长度的普通事实位置,总上限不变 |
| **correction 很多** | > 500(溢出) | `max_tokens + guaranteed_actual_usage` | 上限被**抬高**以保护 guaranteed 行不被安全截断误删;普通事实相应被挤占 |

> 例:若 correction 事实实际占 600 tokens,主预算只剩 2000-100=1900 给摘要+普通事实;但总输出上限被抬到 2600,保证那 600 tokens 的 correction 完整保留。

##### 安全截断规则

预算溢出时,裁剪器遵循**"Facts 优先,尾部裁摘要"**:

- `Facts` 块被视为**受保护后缀**——溢出时只从尾部裁剪 `User Context` / `History`,**绝不裁 Facts**(尤其是 guaranteed 行)。
- 如果主预算连摘要都装不下,先压缩 `History` 段,再压缩 `User Context` 段,`Facts` 始终最后才被触碰。
- 这条规则与上限抬高机制配合:guaranteed 行即使在极端溢出场景下也至少有"预留 + 主预算剩余"双重保护。

##### 计费方式

```python
format_memory_for_injection(
    memory_data,
    max_tokens=2000,                        # 主预算
    guaranteed_categories=["correction"],   # 哪些类别进特权预留
    guaranteed_token_budget=500,            # 特权预留额度
    use_tiktoken=True,                      # True = tiktoken 精确计费; False = CJK 感知的字符估算(离线)
)
```

- `use_tiktoken=True` 时依赖 tiktoken 的 BPE 编码,首次调用可能触发编码下载(网络阻塞,最坏约 26 分钟;已用 5 秒超时兜底,见 §11.3.3)。
- `use_tiktoken=False` 时采用离线字符估算,不碰网络,适合启动期或 tiktoken 不可用的场景。

#### 8.2.3 完整示例(一个具体记忆 → 注入出的文本)

假设该桶记忆是 §5.1 那份(六段摘要 + correction + 若干普通事实),预算 2000。`format_memory_for_injection` 的产出(即 `get_context` 的返回值)是:

```text
User Context:
- Work: 数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目
- Personal: 中文母语, 能读写英文; 偏好简洁沟通
- Current Focus: 正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移

History:
- Recent: 近三个月: 从 pandas 转向 polars; ...
- Earlier: 去年: 主导过数据管道重构; ...
- Background: 十年后端经验; 关注数据工程

Facts:
- [correction | 0.98] 用户偏好使用 uv 而非 pip 安装 Python 依赖 (avoid: 先前建议使用 pip)
- [context    | 0.95] 用户是后端工程师, 主用 Python
- [knowledge  | 0.85] 用户用 polars 做数据处理
- [preference | 0.78] 用户偏好简洁的 CLI 输出
```

(`_select_fact_lines` 的贪心过程:correction 行先进 guaranteed 预留区;普通事实按置信度 0.95 → 0.85 → 0.78 依次进主预算,遇到放不下的第一条就停。)

### 8.3 注入形态(模型看到的完整样子)

`get_context()` 返回的是**正文**;真正进上下文的,还要在调用侧包一层 `<memory>`,并由 `DynamicContextMiddleware` 注入到首条用户消息前:

```xml
<memory>
User Context:
- Work: 数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目
- Personal: 中文母语, 能读写英文; 偏好简洁沟通
- Current Focus: 正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移

History:
- Recent: 近三个月: 从 pandas 转向 polars; ...
- Earlier: 去年: 主导过数据管道重构; ...
- Background: 十年后端经验; 关注数据工程

Facts:
- [correction | 0.98] 用户偏好使用 uv 而非 pip 安装 Python 依赖 (avoid: 先前建议使用 pip)
- [context    | 0.95] 用户是后端工程师, 主用 Python
</memory>
```

**结构规则**:

| 块 | 内容 | 对应数据 |
|---|---|---|
| `User Context` | `Work` / `Personal` / `Current Focus` 三行 | `memory.json` 的 `user.workContext` / `personalContext` / `topOfMind` |
| `History` | `Recent` / `Earlier` / `Background` 三行 | `history.recentMonths` / `earlierContext` / `longTermBackground` |
| `Facts` | 逐行事实,预算内按置信度从高到低 | 该 Agent 的事实桶 |

**事实行格式**(`prompt.py::_format_fact_line`):

```
- [correction | 0.98] 内容 (avoid: 之前错在哪)   ← correction 且带 sourceError
- [context    | 0.95] 内容                        ← 普通事实
```

**设计要点**:
- 注入为 **hidden 数据块**(`hide_from_ui`,不参与对话显示,只进上下文)。
- 所有文本经 **html 转义**(`<` `>` `&`),防记忆内容里的 `</memory>` 逃出块边界、被当成指令(与 §5.1 的 `_escape_memory_for_prompt` 同源防护)。
- `injection_enabled: false` 可完全关闭注入,但照常抽取存储(适合"只记不用")。
- **tool 模式下只注入摘要**:`get_context` 把 `agent_name` 置空,`Facts` 块为空,事实靠 `memory_search` 按需取回(§8.4)——避免"自动注入一遍 + 检索又返回一遍"的重复。

### 8.4 tool 模式:`memory_search` 才是真正的检索

当 `mode: tool` 时,不再自动注入事实,而是给模型注册四个记忆工具:

| 工具 | 作用 |
|---|---|
| `memory_search` | **按查询召回相关事实**(真正的关键词/全文检索) |
| `memory_add` | 显式记一条 |
| `memory_update` | 改一条 |
| `memory_delete` | 删一条 |

**`memory_search(query, top_k=5)` 的完整流程**(`retrieval.py` + `search_facts`):

```
① 分词
   中文 → jieba(可选 extra `memory-zh`;未装则退化为 unicode/空白切分)
   英文 → 空白切分
② 构造 FTS5 查询
   - 若 query 是 FTS 高级语法(AND / OR / NOT / 短语 "..." / 前缀 tok*) → 直接透传
   - 否则(自然语言)→ 拆 token → "tok1" OR "tok2" OR ...
     (每个 token 加引号、转义标点 → 自然语言里的标点不会被当成 FTS 运算符)
③ 打分排序
   SELECT ..., bm25(memory_fts) AS score
   FROM memory_fts MATCH ?
   ORDER BY bm25_score LIMIT top_k*2
   再叠加:
   - confidence 权重(高置信度事实加分)
   - 时间衰减(新近创建的事实加权;bm25 的 k1 置 0 时,排名退化为纯 confidence×权重)
④ 兜底
   FTS5 抛错(如语法不合法)→ 退回子串匹配(substring),保证搜索不崩
```

**SQLite 里到底存了什么?——不是聊天记录。** 记忆系统用到的唯一 SQLite 库是 `.retrieval/<scope>.sqlite`,里面只有一张 FTS5 虚拟表 `memory_fts`,**每一行对应一条事实**(不是一条聊天消息):

```
CREATE VIRTUAL TABLE memory_fts USING fts5(
  doc_id UNINDEXED,        # 事实 id
  content,                 # 事实正文(分词后, 供检索匹配)
  raw_content UNINDEXED,   # 原始正文
  category / confidence / created_at / source UNINDEXED,   # 元数据
  scope_user / scope_agent UNINDEXED,                      # 按 (user, agent) 隔离检索
  fact_json UNINDEXED      # 事实快照(JSON)
)
```

- **它是派生的、可重建的**:权威数据在 `facts/*.md`,这张表只是为 bm25 检索准备的**副本**——索引坏了自动删了重建,丢了从 `.md` 重新灌一遍。它绝不是事实的"另一个存储"。
- **聊天记录根本不在记忆系统里**:§1.3 非目标明文"不保存完整对话原文"。对话历史由 **LangGraph checkpointer + run_events** 持久化(按 `database` 配置可落在 SQLite / Postgres / memory),那是"对话历史"系统,与记忆是两套东西,互不读写。

> **别混淆:同一个进程里其实有"两个 SQLite",用途完全不同。**

| SQLite 库 | 位置 / 配置 | 存什么 | 归属 |
|---|---|---|---|
| 记忆检索索引 | `backend/.deer-flow/users/{user_id}/.retrieval/<scope>.sqlite` | 事实的 FTS5 派生行(**一行一事实**,§A.6) | 记忆系统(§8.4) |
| 对话历史 / 状态 | `database.backend: sqlite` 的 LangGraph checkpointer + run_events | 消息、checkpoint、run 事件(**即"聊天记录"**) | 对话持久化系统,与记忆无关 |

两者唯一共同点只是"都用 SQLite 文件",**数据完全不相干**:记忆索引可随时删了重建,删它不影响任何聊天历史;反之 `database` 库的损坏/清空也不影响记忆。

**两种模式对照**:

| 维度 | middleware(默认) | tool(实验) |
|---|---|---|
| 触发 | 每次 run 开始,自动 | 模型主动调 `memory_search` |
| 方法 | **无检索**:全量读 + 置信度排序 | **FTS5 全文检索**(jieba/空白分词) |
| 打分 | confidence(截断靠 token 预算) | bm25 + confidence + 时间衰减 |
| 关键词 | ❌ 无关 | ✅ 核心 |
| 注入内容 | 摘要 + 选中事实(预算内) | 只注入摘要,事实靠检索取回 |
| 失败兜底 | 注入失败 → 空上下文(不打断对话) | FTS 失败 → 子串匹配 |

**两个常见误解**:

1. **"记忆会不会被关键词检索"——默认模式不会。** 它不搜,只是"按置信度把能带的都带上"。没有语义排序,也不是 RAG 式向量检索。
2. **"FTS5 索引是干嘛的"——它服务 `memory_search` 和未来的按需召回。** middleware 模式的注入完全不依赖它(**索引坏了不影响注入,只影响搜索**)。索引存的是可重建的派生数据,坏了自动重建。

### 8.5 run-level memory identity(可观测性)

每次带 `<memory>` 的 run,对该块的精确内容做 SHA-256,记一条 `context:memory` 事件(只存 hash,不重复存全文)。用途:
- 审计"这次 run 到底用了哪份记忆"。
- 后续 run / 分支复用冻结的记忆块(不重复读盘),校验来源防伪造。

(注入侧的落点见 §11.3.4。)

---

## 9. 隔离模型

| 维度 | 隔离机制 | 说明 |
|---|---|---|
| 用户 | `users/{user_id}/` 目录 + 每用户锁 | 用户之间物理隔离;`strict_user_scope` 开启后强制带 user_id |
| Agent | `agents/{agent_name}/facts/` | 同一用户下不同 Agent 记忆互不串 |
| Thread | **不隔离**(仅作去重 key) | 设计上记忆就是要跨会话共享;线程不参与存储隔离 |

一个直接推论:换自定义 Agent = 换记忆桶;开认证 = 每个登录用户一份记忆。

---

---

## 10. 扩展点(如何接入自己的存储)

整个记忆系统对"换实现"是**分层开放的**:从"换掉整套记忆逻辑"到"只换一条 prompt",有 6 个粒度递减的扩展点。改得越深,收益越大,但要遵守的契约也越多。先看总览:

| 扩展点 | 配置入口 | 需要写什么 | 粒度 |
|---|---|---|---|
| **换后端** | `memory.manager_class` | 新 `backends/<name>/` 文件夹(一个 `MemoryManager` 子类) | 整套记忆逻辑(存储+抽取+检索) |
| **换存储类** | `memory.backend_config.storage_class` | 一个 `MemoryStorage` 子类 | 只换持久化,保留 LLM 抽取/队列 |
| **换检索适配器** | `memory.backend_config.retrieval_adapter` | 一个 `RetrievalPort` 实现 | 只换"按关键词查事实" |
| **自定义抽取模板** | `memory.backend_config.prompts_dir` | YAML prompt 文件 | 只换"模型怎么记" |
| **自定义信号模式** | `memory.backend_config.patterns_dir` | YAML 正则文件 | 只换"识别哪些信号" |
| **观测钩子** | 程序注入 `callbacks` / `extraction_callback` | 一个类或函数 | 只加可观测性,不改行为 |

> 一条通用原则贯穿全章:**memory 是持久状态,任何解析/接入失败都必须 fail-loud**——绝不允许"配置写错了但静默回退到默认后端"这种悄悄把数据写进错误存储的行为。

---

### 10.1 换后端:接入一个完整的记忆系统

这是最深、也最常见的扩展点:把整套记忆系统换成你自己的(mem0、OpenViking、向量库、自建服务……)。**所有现有的读写链路的中间件一行都不用改**——它们只通过 `MemoryManager` 这个抽象契约通信(见 §11.5 的分工边界)。

#### 10.1.1 契约:`MemoryManager` 接口

后端必须实现的是 pydantic `BaseModel` 子类(不是裸 ABC),所以它**免费获得字段校验和序列化**;同时 `ModelMetaclass` 派生自 `ABCMeta`,没实现的抽象方法在实例化时就抛 `TypeError`——记忆是持久状态,缺了 `add`/`get_context` 的后端是严重 bug,必须在构造期抓住,而不是运行时才炸。

方法分三层:

| 层 | 方法 | 必须? | 说明 |
|---|---|---|---|
| **Tier-1 抽象** | `add(thread_id, messages, ...)` / `get_context(user_id, ...)` | **必须** | 写(入队抽取)+ 读(返回注入文本)。这是后端的根本职责 |
| **Tier-2 管理** | `add_nowait` / `search` / `get_memory` / `clear_memory` / `import_memory` / `export_memory` / `shutdown_flush` | 可继承默认 | 默认抛 `NotImplementedError` 或空实现;`add_nowait` 默认委托 `add`,`shutdown_flush` 默认 `True`(无缓冲就没得排空) |
| **Tier-3 可选** | `warm` / `reload_memory` / `create_fact` / `delete_fact` / `update_fact` | 可继承默认 | 启动预热、手动 reload、事实 CRUD;只覆盖你支持的那些 |

构造入口是**类方法** `from_config(backend_config, *, mode, **host_hooks)`,由工厂调用而不是直接 `cls(...)`——这样每个后端自己决定怎么组装依赖、消费哪些 host hook。

两个**类级声明**(ClassVar,不是字段):

| 声明 | 含义 |
|---|---|
| `supports_search: ClassVar[bool]` | 是否实现了 `search()`。默认 `False`;实现了就必须设 `True`,否则实例化时不变式校验直接抛错(`mode="tool"` 强制要求 search 支持) |
| `requires_passive_writes_in_tool_mode: ClassVar[bool]` | `mode: tool` 下是否仍需要 `MemoryMiddleware` 被动抽取。默认 `False`(工具模式完全模型主导);设 `True` 的后端(如 openviking:搜索靠工具、持久写靠对话抽取)在工具模式下会**保留** `MemoryMiddleware`,形成"工具搜索 + 被动抽取"混合模式(§11.2) |

**契约的三个刻意中立点**(想清楚再实现,别照搬 DeerMem 的假设):

- `get_context` 返回**注入就绪的纯文本**,格式由后端自己定——DeerMem 做"全量读 + 置信度裁剪",别的后端可以自己做检索 + 格式化。格式**不是契约的一部分**。
- `add` 收到的是**原始对话消息**,过滤/信号检测是后端的私有事务——DeerMem 在 `message_processing.py` 里做,别的后端按自己的策略来。
- **不假设"事实"模型**——后端完全可以不存 fact,只要 `get_context` 能返回文本、`add` 能更新记忆。

#### 10.1.2 drop-in 发现机制 + fail-loud

不需要改任何注册表。工厂在 `backends/` 目录下**自动扫描**:每个有 `__init__.py` 且暴露 `MANAGER_CLASS` 属性的子文件夹,就以**文件夹名**注册(文件夹名 == 后端名 == `manager_class` 配置值)。

`manager_class` 的解析顺序:

```
① 已注册的短名        deermem / mem0 / openviking / noop(或你新加的)
② dotted 路径          "pkg.mod:Cls" 或 "pkg.mod.Cls"  → 任意包里的 MemoryManager 子类
③ 都解析不到           → raise ValueError(fail-loud)
```

第 ③ 条是**刻意为之**:`manager_class` 拼错/导入失败时,绝不静默回退到 deermem——否则用户的写入会悄悄落到错误的存储,这是静默的数据完整性地雷。解析发生在启动期(急着预热),让运维当场修配置,而不是跑起来才发现写错地方。

#### 10.1.3 可移植性黄金规则

后端收到 host 的全部信息,只通过**两个通道**:

```
① MemoryManager 方法参数      user_id / agent_name / thread_id / messages / ...
② backend_config dict         传给 __init__ 的字段(你自己的 knob:model、vector_store、阈值...)
```

**整个后端文件夹里只允许一行 `from deerflow`**——就是导入 ABC 契约那行 `from deerflow.agents.memory.manager import MemoryManager`。其他一切(deer-flow 路径 helper、配置单例、模型工厂)**都不许 import**;存储根目录从 `backend_config["storage_path"]` 读(host 注入),自己绝不调用路径解析。

为什么这么严:这一行就是后端和 host 的唯一耦合点。想把它移植到另一个 agent,只改这一行导入即可。任何多余的 `from deerflow.xxx` 都会让"换后端"变成"改宿主代码"。

#### 10.1.4 动手步骤(六步,参照 noop 模板)

`backends/noop/` 是一个刻意写全的**空实现模板**(存什么、读什么都返回空)。照抄它:

```text
① 复制 backends/noop/ → backends/<yourname>/
② config.py    声明你的配置字段 + from_backend_config():从 backend_config dict 解析,
                只读已知键;storage_path 从这里取,绝不 import deer-flow 路径 helper
③ <yourname>_manager.py   重命名类;把依赖(storage/llm/queue/连接)声明成
                PrivateAttr;model_post_init 里用 self.backend_config 解析成你的 config;
                实现 Tier-1 的 add / get_context(必须)
④ (可选)覆盖 Tier-3 hooks  你支持 create_fact/delete_fact/update_fact/search/warm 就覆盖,
                不支持就继承默认(callers 捕 NotImplementedError)
⑤ __init__.py     设置 MANAGER_CLASS = YourManager
⑥ config.yaml     memory.manager_class: <yourname>
```

`backend_config` 由 `memory.backend_config` 传给后端,host 还会额外注入 `storage_path`(默认状态目录)。你自己的 knob 也放这里:

```yaml
memory:
  enabled: true
  manager_class: myredis
  backend_config:
    storage_path: .deer-flow          # host 注入默认,也可自己写
    redis_url: redis://localhost:6379 # ← 你自己的 knob,config.py 里解析
    max_facts: 100
```

#### 10.1.5 最小示例:一个"Redis 后端"的骨架

```python
# backends/myredis/config.py
class MyRedisConfig:
    def __init__(self, *, storage_path: str, redis_url: str):
        self.storage_path = storage_path
        self.redis_url = redis_url

    @classmethod
    def from_backend_config(cls, backend_config: dict | None) -> "MyRedisConfig":
        cfg = dict(backend_config or {})
        return cls(
            storage_path=str(cfg.get("storage_path") or ""),
            redis_url=str(cfg.get("redis_url", "redis://localhost:6379")),
        )
```

```python
# backends/myredis/myredis_manager.py
from deerflow.agents.memory.manager import MemoryManager   # ← 唯一允许的 from deerflow

class MyRedisManager(MemoryManager):
    _config = PrivateAttr(default=None)
    _client = PrivateAttr(default=None)

    def model_post_init(self, __context) -> None:
        self._config = MyRedisConfig.from_backend_config(self.backend_config)
        self._client = redis.Redis.from_url(self._config.redis_url)

    @classmethod
    def from_config(cls, backend_config, *, mode="middleware", **host_hooks):
        return cls(backend_config=backend_config, mode=mode)

    def add(self, thread_id, messages, *, agent_name=None, user_id=None, trace_id=None):
        # ① 过滤出 user + final-AI(这是你的私有事务)
        # ② 调你自己的抽取 LLM → 写入 Redis(按 (user, agent) 桶隔离)
        ...

    def get_context(self, user_id, *, agent_name=None, thread_id=None):
        # 读 Redis 里该桶的记忆,格式化(任何格式都行)返回注入文本
        ...
```

**网络后端的异步义务**:如果后端要发起网络调用,必须覆盖 `aadd` / `aget_context` / `asearch`(基类默认是**同步委托**,没有并发收益)。`MemoryMiddleware.aafter_agent` 调 `aadd`,而它是 `asyncio.to_thread(get_memory_manager)` 解析完再调的——但网络 IO 本身必须由你的 `a*` 方法自己 offload(参考 `backends/openviking/` 的 async 入口,它把同步 HTTP 和文件 IO 都挪出了事件循环)。

---

### 10.2 换存储类 / 检索适配器:只换"手脚"

如果你**认可 DeerMem 的抽取/队列/闸门逻辑**,只想把"档案室"从文件换成别的,那就不要换后端——只换下面两层:

#### 10.2.1 `MemoryStorage` 契约

`memory.backend_config.storage_class` 指向一个 `MemoryStorage` 子类。抽象方法三个:

| 方法 | 语义 |
|---|---|
| `load(agent_name, *, user_id)` | 读该 `(user, agent)` 桶的完整记忆文档(兼容形态,含 `facts` 列表) |
| `reload(agent_name, *, user_id)` | 丢弃缓存,强制重读(绕过内存缓存) |
| `save(memory_data, agent_name, *, user_id, expected_revision)` | 写回;`expected_revision` 是乐观锁版本,冲突时返回 `False` 或抛错 |

可选的增量路径(强烈建议覆盖,否则写整份文档):

- `apply_changes(change_set, **scope)` —— 应用一个**变更集**(只改增量),原子的、可回滚的;这是 DeerMem 文件存储的精髓:摘要和每条事实分开落盘,改一条不动整份。
- `clear_all(user_id)` / `close()` —— 清空该用户全部桶 / 释放资源。

> 换存储类 ≠ 换后端:你仍然需要 `MemoryManager` 里的 `add`/`get_context`(它们把"对话 → 抽取 → 调 storage"串起来)。`storage_class` 只是把 DeerMem 的存储依赖替换掉;如果连抽取/闸门逻辑也要换,那应该走 §10.1 换后端。

#### 10.2.2 `RetrievalPort` 契约(检索适配器)

`memory.backend_config.retrieval_adapter` 指向一个 `RetrievalPort` 实现(默认 `fts5`,空字符串关闭)。这是存储层**对外接检索的插槽**:存储层在写完事实后**释放锁再通知**它 upsert/remove(§A.6),搜索时把 `search()` 委托给它。

```python
class RetrievalPort(Protocol):
    def upsert(self, fact: dict, *, scope: dict, path: str) -> None: ...
    def remove(self, fact_id: str, *, scope: dict) -> None: ...
    def search(self, query: str, *, scopes: list[dict], top_k: int,
               mode: str, filters: dict | None) -> list[dict]: ...
    def clear(self, *, scopes: list[dict] | None = None) -> None: ...
    def rebuild(self, records: list[tuple], *, scopes: list[dict] | None) -> None: ...
```

**关键设计**:检索索引是**派生的、可重建的**——权威数据在 `facts/*.md`,索引坏了删了重建,丢了从事实文件重新灌(§8.4)。所以自定义检索适配器只负责"给个能搜的视图",永远不拥有权威数据。

---

### 10.3 自定义抽取模板:只换"怎么记"

`memory.backend_config.prompts_dir` 指向一个目录,可覆盖四份 prompt(§5.2、§5.3):

```
{prompts_dir}/
├── memory_update.chat.yaml    # 主抽取模板(system + user 两条消息,§5.3)
├── staleness_review.yaml      # 过期复查的 text 模板(§5.2.2)
├── consolidation.yaml         # 碎片合并的 text 模板(§5.2.3)
└── fact_extraction.yaml       # 事实提取
```

支持**按 Agent 分子目录**:`{prompts_dir}/{agent_name}/memory_update.chat.yaml` 只覆盖特定 Agent,没配的那层回退到上一级。

**改动这个扩展点有一个必须遵守的契约**:模板输出的 JSON 必须继续带 `scope`/`durability`/`authority` 分类字段(§5.4)——因为写闸门(§5.5)是 fail-closed 的,模板里没了这些字段,**每一次抽取写都会被闸门拒掉**,表现为 `rejected_by_scope_gate` 指标暴涨、记忆永远写不进去。自定模板前先确认:你的模板产出是否仍携带这三个标签。

---

### 10.4 自定义信号模式:只换"识别什么"

`memory.backend_config.patterns_dir` 覆盖两个信号检测的正则文件(§5.2.1):

```
{patterns_dir}/
├── correction.yaml       # 识别"用户纠正"
└── reinforcement.yaml    # 识别"用户认可"
```

文件里是 `regex.search` 用的模式清单。加语言/业务短语**不用改任何代码**——把新短语写进 YAML 即可。两个真实注意点(来自 §5.2.1 的注释):

- 中文 `对` 这类确认**刻意不在默认 reinforcement 里**——`对` 会误伤"对不起/对方"。你自己加模式时留意单字正则的误伤。
- `patterns_dir` 一旦显式设置,**两个文件都必须存在**(否则启动即 fail-loud,不静默回退到内置模式)。

---

### 10.5 观测钩子:只加可观测性

两个钩子,都是**程序注入**(来自 factory 的 `host_hooks`,不是 YAML 字段):

| 钩子 | 时机 | 用途 | 默认 |
|---|---|---|---|
| `callbacks`(一个 `MemoryCallbacks`) | **每次 LLM 调用前** | 往 `invoke_config` 里塞 trace 元数据,让记忆抽取的 span 与主对话 trace 对齐(§11.2 的 trace_id 落点) | `LangfuseMemoryCallbacks`(langfuse 未启用则 no-op) |
| `extraction_callback(payload)` | **每次抽取后** | token 用量、接受/拒绝统计 | host 默认:打日志 + 拒绝率 >60% 时报警 |

`extraction_callback` 收到的 `payload` 稳定字段(对接自己的监控时按这些键读):

```
facts_extracted / facts_passed_confidence / rejected_low_confidence /
rejected_by_scope_gate / scope_gate_rejections / thread_id / model_name /
token_usage / success
```

`scope_gate_rejections` 是**按原因分桶**的(`missing`/`scope`/`durability`/`authority`),拒绝率过高说明抽取 prompt 或阈值退化——这正是在 prompt 或模板改坏时(§10.3)第一时间暴露问题的信号。替换成自己的回调时:异常永不向上抛(DeerMem 侧已包了 try)。

---

### 10.6 检查清单

接入完成后,对照这张表自检:

| 检查项 | 为什么 |
|---|---|
| `manager_class` 拼写错误是否在启动时报错,而非静默回退? | fail-loud 防数据写错存储(§10.1.2) |
| 后端文件夹里除 ABC 契约外,是否还有别的 `from deerflow`? | 移植性黄金规则(§10.1.3) |
| 网络后端是否覆盖了 `aadd` / `aget_context`? | 避免阻塞事件循环(§10.1.5) |
| `mode: tool` 下 `supports_search` 是否与 `search()` 一致? | 不变式校验,不一致在构造期抛错 |
| 自定模板后,`rejected_by_scope_gate` 是否仍接近 0? | 模板丢了 scope/durability/authority 会让闸门拒掉一切(§10.3) |
| `patterns_dir` 里两个文件都在吗? | 显式设置后缺一个就启动失败 |
| `extraction_callback` 的拒绝率是否在观测范围内? | 监控 prompt/阈值退化(§10.5) |

---

## 11. 记忆相关中间件实现详解(代码级)

前面各节从"设计意图"讲了记忆系统;这一节落到**实际代码**,把参与记忆读写的三个 LangGraph 中间件组件逐个拆开,回答"它们各自在哪一行做了什么、为什么这么做"。对应代码:

| 组件 | 文件 | 挂载点 | 职责 | 触发时机 |
|---|---|---|---|---|
| `MemoryMiddleware` | `agents/middlewares/memory_middleware.py` | lead-agent 链,**TitleMiddleware 之后** | 写链路**捕获器**:把本轮对话交给记忆管理器 | `after_agent` / `aafter_agent`(每轮对话结束后) |
| `DynamicContextMiddleware` | `agents/middlewares/dynamic_context_middleware.py` | lead-agent 链,**基础链之后第一个 lead-only 中间件** | 读链路**注入器**:把记忆 + 当前日期注入首条用户消息前 | `before_agent` / `abefore_agent`(每次模型调用前) |
| `memory_flush_hook` | `agents/memory/summarization_hook.py` | **挂进 `SummarizationMiddleware`** 的 hook 列表 | 摘要压缩前的**紧急冲刷**:把即将被删的消息抢先入队 | 摘要中间件触发压缩那一刻 |

三者合起来就是完整的读写闭环:**flush hook 和 MemoryMiddleware 走写链路, DynamicContextMiddleware 走读链路**。

### 11.1 中间件在链中的位置

```
基础运行时链(所有 agent 共享, build_lead_runtime_middlewares)
  InputSanitization → ToolOutputBudget → ThreadData → ... → MemoryManager 无关
┌────────────────────────── lead-only 追加(build_middlewares) ──────────────────────────┐
│                                                                                        │
│  ① DynamicContextMiddleware     ← 读链路: 每轮注入 <memory> + 日期                       │
│  ② SkillActivation / SkillToolPolicy / DurableContext                                  │
│  ③ SummarizationMiddleware(+memory_flush_hook)   ← 压缩前紧急冲刷写链路                  │
│  ④ TitleMiddleware                                                                    │
│  ⑤ MemoryMiddleware            ← 写链路: 对话结束后入队                                 │
│  ⑥ ViewImage / ... / 安全类 / Clarification(最后)                                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

关键观察:

- **`MemoryMiddleware` 紧跟在 `TitleMiddleware` 之后**(`agent.py::build_middlewares`)。标题中间件在首轮完成后自动起标题;记忆中间件在它之后,保证任何轮次结束时都能拿到完整、已稳定的消息列表入队。
- **`DynamicContextMiddleware` 是整个 lead-only 链最靠前的注入者**,  (见 11.3),与静态系统提示词配合最大化前缀缓存命中。
- **子代理不挂 `MemoryMiddleware`,也不挂 `DynamicContextMiddleware`**(它们只出现在 lead-agent 的 `build_middlewares` 里)。子代理与父线程共享同一个 `thread_id`,若子代理也写记忆,会把内部中转的每轮对话污染进**父线程**的持久记忆——这正是摘要路径必须 `skip_memory_flush=True` 的原因(见 11.4)。

### 11.2 `MemoryMiddleware`:写链路捕获器

这是一个**极薄**的中间件,严格遵守"中间件只捕获、后端才过滤"的分工。核心是 `_resolve_add_args` 加两个执行方法:

```python
class MemoryMiddleware(AgentMiddleware[MemoryMiddlewareState]):
    def __init__(self, agent_name=None, *, memory_config=None): ...

    def _resolve_add_args(self, state, runtime) -> tuple[str, list, str, str | None] | None:
        # ① 总开关
        config = self._memory_config or get_memory_config()
        if not config.enabled:
            return None
        # ② thread_id: 先取 runtime.context,再退回 config.configurable
        thread_id = runtime.context.get("thread_id") or get_config()["configurable"]["thread_id"]
        if not thread_id:
            return None
        # ③ 消息: 必须有内容
        messages = state.get("messages", [])
        if not messages:
            return None
        # ④ user_id 在入队时刻就捕获(resolve_runtime_user_id)
        user_id = resolve_runtime_user_id(runtime)
        # ⑤ trace_id 从 runtime.context → config.metadata → 全局 ContextVar 逐级取
        ...
        return thread_id, messages, user_id, trace_id

    def after_agent(self, state, runtime):          # 同步路径
        get_memory_manager().add(thread_id, messages, agent_name=..., user_id=..., trace_id=...)

    async def aafter_agent(self, state, runtime):   # 异步路径
        manager = await asyncio.to_thread(get_memory_manager)
        await manager.aadd(thread_id, messages, agent_name=..., user_id=..., trace_id=...)
```

四个设计要点:

| 要点 | 为什么 |
|---|---|
| **只捕获、不过滤** | `after_agent` 把**原始消息列表**整包交给 `manager.add()`;过滤到 HumanMessage/AIMessage、去寒暄、要求一用户一助手、检测 correction/reinforcement 全部在**后端**的 `message_processing.py::filter_messages_for_memory` + `detect_signals` 里做(见 §4.2,即 `_prepare_update` 对应的真实实现)。中间件保持薄,是因为这些逻辑对不同后端(mem0/openviking)策略不同,不该在 agent 层写死 |
| **user_id 入队即捕获** | `MemoryMiddleware` 用 `resolve_runtime_user_id(runtime)` 在 `after_agent` 里就把 user_id 取好,存入 `ConversationContext`。原因:队列的 debounce 用 `threading.Timer` 在**另一个线程**触发,`ContextVar` 不跨原生线程传播——不提前捕获,定时器触发时就读不到用户身份 |
| **trace_id 同步捕获** | 同样的线程边界问题,`trace_id` 也必须在请求上下文还活着时取好,随 `ConversationContext` 一起传给抽取期的 LLM 调用,让记忆抽取的 langfuse span 与主对话 trace 对齐 |
| **同步/异步双实现** | LangGraph 的同步路径走 `after_agent`,异步路径走 `aafter_agent`;异步路径用 `asyncio.to_thread(get_memory_manager)` 把管理器解析移出事件循环,再调后端的 `aadd`(网络后端必须在 `a*` 方法里自己 offload) |

**`agent_name` 与 `memory_config`**:`agent_name` 决定写入哪个事实桶(`None` = 默认 `__default__`);`memory_config` 让调用方(lead agent 组装链时)显式传入已解析的 `AppConfig.memory`,避免中间件内部再读一次全局单例。

**tool 模式下的特例**:`memory.mode: tool` 时默认不挂 `MemoryMiddleware`(模型通过 `memory_add` 等工具主动写)。但若后端声明 `requires_passive_writes_in_tool_mode = True`(如 openviking:搜索靠工具、持久写仍依赖对话级抽取),则**保留** `MemoryMiddleware`,形成"工具搜索 + 被动抽取"混合模式。

### 11.3 `DynamicContextMiddleware`:读链路注入器

这是记忆读链路最复杂的一个中间件,负责把记忆和日期注入上下文,同时坚守两条安全红线(见下)。它挂在 `before_agent`,在**每次模型调用前**执行 `_inject`。

#### 11.3.1 冻结快照模式 + 前缀缓存

系统提示词保持**完全静态**(记忆不写进 system prompt),所有动态内容通过一个隐藏的 `SystemMessage` + 隐藏 `HumanMessage` 在**首条用户消息之前**注入,并且**只注入一次、整个会话冻结**:

```
第一轮:  [SystemMessage(日期) id=原始用户消息id]
          [HumanMessage(<memory>…) id={原始id}__memory]   ← 仅当有记忆
          [HumanMessage(真正的用户输入) id={原始id}__user]
后续轮:  原样复用,不再改动 → 前缀缓存每次都能命中
```

这套 **ID 交换(ID-swap)** 是核心技巧,由 `_make_reminder_and_user_messages` 一手完成:

```python
stable_id = original.id or str(uuid.uuid4())          # ① 取原始用户消息的 id 作稳定 id

reminder_kwargs = {"hide_from_ui": True, "dynamic_context_reminder": True}
if reminder_date is not None:
    reminder_kwargs["reminder_date"] = reminder_date   # ② 结构化日期,只挂在 SystemMessage 上

messages.append(SystemMessage(content=reminder, id=stable_id, additional_kwargs=reminder_kwargs))
if memory_content:
    messages.append(HumanMessage(content=memory,  id=f"{stable_id}__memory",
        additional_kwargs={"hide_from_ui": True, "dynamic_context_reminder": True}))
messages.append(HumanMessage(content=original.content, id=f"{stable_id}__user",
        name=original.name, additional_kwargs=original.additional_kwargs))
```

**它怎么"就地替换"**:LangGraph 的 `add_messages` reducer 对**相同 id 的消息就地覆盖**。`SystemMessage` 取走了原始用户消息的 id,所以在消息列表里它**占据原始消息的位置/序号**;记忆块和真实用户输入用新 id(`__memory` / `__user` 后缀)追加在它后面。净效果是:每轮的用户输入前永远挂着同一组冻结的提醒,原始消息"没挪过窝"。

**为什么必须用 SystemMessage 而非 HumanMessage 承载提醒**:提醒里有两类来源截然不同的数据,必须分角色——

| 消息 | 内容 | ID | 角色 | 为什么 |
|---|---|---|---|---|
| `SystemMessage` | `<system-reminder><current_date>…</current_date></system-reminder>` | **原始用户消息 id** | 框架权威 | 日期是框架数据,由系统生成,可安全放进 system role;占原始 id 实现就地替换 |
| `HumanMessage`(可选) | `<memory>…</memory>` | `{原始id}__memory` | **role=user** | 记忆内容用户可影响,绝不能获得系统权限(安全红线 1) |
| `HumanMessage` | 真正的用户输入 | `{原始id}__user` | role=user | 被替换后的真实消息,`name`/`additional_kwargs` 原样保留 |

**`additional_kwargs` 三个标记的职责分工**(它们才是中间件的"私有协议"):

| 标记 | 挂在谁身上 | 用途 |
|---|---|---|
| `hide_from_ui: True` | 三条注入消息 | 不参与对话显示,只进上下文 |
| `dynamic_context_reminder: True` | SystemMessage + 记忆 HumanMessage | `is_dynamic_context_reminder` 靠它识别注入消息(**不用内容匹配**,用户消息里写 `<system-reminder>` 也不会被误认) |
| `reminder_date: str` | **只挂 SystemMessage** | 权威日期(结构化、不可被用户内容伪造);记忆 HumanMessage 故意不携带——见 §11.3.2 日期权威来源 |

**安全红线 1(OWASP LLM01,issue #3630)**:框架数据(日期)和用户可影响的数据(记忆)**分属不同角色**。日期进 `SystemMessage`(框架权威);记忆进 `HumanMessage`(role=user),**永远不携带系统权限**——否则用户可影响的记忆内容一旦放进 system role,就获得了超越输入的指令权。同理,系统上下文也绝不冒充用户输入。这条在 `_build_full_reminder` 的 docstring 里写得很明确:"memory stays at role:user"。

**安全红线 2(防递归 ID 交换)**:`_is_user_injection_target` 拒绝任何 id 以 `__user` 结尾的消息(用 `endswith`,不是子串 `in`——避免误伤 id 中间恰好含 `__user` 的情况)。否则上一轮生成的 `{id}__user` 会被当成新的注入目标,产生 `id__user__user__user…` 的无限后缀膨胀和幽灵消息重放。`_is_user_injection_target` 的完整拒绝清单:

```
❌ 不是 HumanMessage
❌ 已经是 dynamic-context reminder(id 以 __memory 结尾 或 带该标记)
❌ name == "summary"(摘要消息,不注入)
❌ id 以 __user 结尾        ← 安全红线 2
✅ 其余 HumanMessage 都可作为注入目标
```

#### 11.3.2 三种分支:首轮 / 同日 / 跨午夜

`_inject` 用**反向扫描 + 一个状态机**决定做什么。先读"上次注入的日期",再和今天比:

```python
last_date = _last_injected_date(messages)          # 反向扫描 messages
current_date = datetime.now().strftime("%Y-%m-%d, %A")

if last_date is None:                              # ── 首轮 ──
    first_idx = next(i for i, m in enumerate(messages) if _is_user_injection_target(m))
    return _make_reminder_and_user_messages(messages[first_idx],
             *_build_full_reminder(runtime), reminder_date=current_date)
if last_date == current_date:                      # ── 同日 ──
    return None                                    # 什么都不做,前缀缓存命中
# ── 跨午夜 ──
last_human_idx = next((i for i in reversed(range(len(messages))) if _is_user_injection_target(messages[i])))
return _make_reminder_and_user_messages(messages[last_human_idx],
         _build_date_update_reminder(), reminder_date=current_date)   # 无 memory_content
```

| 分支 | 触发 | 注入什么 | 目标消息 |
|---|---|---|---|
| **首轮** | 历史里没有注入过的日期(`last_date is None`) | **完整提醒**:日期 + 记忆 | **第一条**可注入的 HumanMessage |
| **同日** | `last_date == current_date` | 什么都不做(返回 `None`) | — |
| **跨午夜** | 日期变了 | **轻量日期更新**:只有日期,不含记忆 | **最后一条**可注入的 HumanMessage |

三个关键设计:

- **首轮找"第一个",跨午夜找"最后一个"**:首轮要插在最前(记忆是会话级快照,必须放在开头);跨午夜只修日期,插到当前轮的用户消息前即可,不打扰已冻结的会话前缀。
- **跨午夜不重复注入记忆**:记忆块是会话冻结的,`last_date` 已经非 None 说明记忆早已注入过;真正需要修正的只有日期。日期更新也**持久化**(带 `reminder_date`),让新一天后续轮次看到一致的历史、不再重复注入。
- **日期权威来源**:`_last_injected_date` 反向扫描时,优先读 `SystemMessage.additional_kwargs["reminder_date"]`(结构化、不可被用户内容伪造);仅对**旧 checkpoint**(在 `reminder_date` 字段出现前写入的)退回解析 content,且**正则 `_DATE_RE` 只跑在 `SystemMessage` 上**——记忆 `HumanMessage` 是用户可影响的,绝不对它做内容解析。这一条堵死了"用户在记忆内容里伪造 `<current_date>`"的日期欺骗洞(与 §11.3.1 安全红线 1 同源)。

#### 11.3.3 异步路径:offload + 超时兜底

`abefore_agent` 把 `_inject` 用 `asyncio.to_thread` 挪出事件循环,并套上 **`_INJECT_TIMEOUT_SECONDS = 5.0`** 硬超时:

```python
try:
    result = await asyncio.wait_for(
        asyncio.to_thread(self._inject, state, runtime),
        timeout=_INJECT_TIMEOUT_SECONDS,
    )
except TimeoutError:
    logger.warning("injection timed out (%.1fs); skipping new memory/date injection", _INJECT_TIMEOUT_SECONDS)
    self._record_effective_memory(state, None, runtime)
    return None
self._record_effective_memory(state, result, runtime)
return result
```

**为什么必须 offload**:`_inject` 内部有两件可能长时间阻塞的事——① 同步加载 `memory.json`(文件 IO);② 首次调用 tiktoken BPE 编码可能**触发编码下载**(网络阻塞,最坏能到 OS TCP 超时 ~26 分钟,issue #3402)。若不加 offload,这条 `before_agent` 路径会卡死**所有并发 HTTP handler**(认证、SSE 心跳),整个网关都跟着停。

**超时之后发生了什么**:这次**不注入新的动态上下文**(`None` 返回 = 本次不加提醒/记忆),请求优雅降级,而不是把网关拖挂。关键语义:**已冻结在 checkpoint 里的旧记忆仍然在**——那是对上一轮生效的冻结快照,不因本次超时而丢失,只是"不更新"而已。超时路径同样会走 `_record_effective_memory(state, None, runtime)`,保证每次 run 的记忆身份记录不缺失(见 §11.3.4)。

#### 11.3.4 每次 run 的记忆身份(接 §8.5)

`before_agent` / `abefore_agent` 在 `_inject` 之后都调用 `_record_effective_memory`——它的职责是定位"本 run 真正生效的记忆块",对它做 SHA-256,记一条 `context:memory` run 事件。

**定位生效记忆块的两条路径**(`_effective_memory_message`):

```
路径 ① 本次注入产生:
   update 是 dict 且有 messages → 在其中找 id 以 __memory 结尾
   + 是 dynamic-context reminder + content 是 str 的 HumanMessage

路径 ② checkpoint 里复用(跨 run 延续):
   runtime.context 读 CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY
   (本次 run 开始前就已存在的消息 id 集合)
   → 在 state 里找 id ∈ pre_existing_ids 且以 __memory 结尾的 HumanMessage
```

为什么路径 ② 只信"run 前就存在的 id":**Gateway 会从不受信任的输入里剥掉动态上下文的标记**(`dynamic_context_reminder` / `hide_from_ui`),所以用户无法伪造一条带这些标记的消息、再借用已知消息 ID 冒充记忆来源。路径 ① 覆盖"本 run 注入的新记忆",路径 ② 覆盖"延续下来的冻结记忆",两条合起来保证**每次 run 都能唯一确定它实际生效的那份记忆**。

找到后执行:

```python
journal.record_memory_context(
    content_sha256=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
)
```

**只存 hash,不存全文**——`run_events` 里只有 `content_sha256` 一个字段,完整记忆文本留在 checkpoint,不重复落一份到事件流。生产消费方是 `GET /api/threads/{thread_id}/runs/{run_id}/events?event_types=context:memory`,运维拿 hash 跨 run 比对"这次用的记忆和上次是不是同一份"。

### 11.4 `memory_flush_hook`:摘要压缩前的紧急冲刷

摘要中间件在把旧消息压缩掉之前,会先触发 hook 列表;`memory_flush_hook` 就是其中一环:

```python
def memory_flush_hook(event: SummarizationEvent) -> None:
    if not get_memory_config().enabled or not event.thread_id:
        return
    user_id = resolve_runtime_user_id(event.runtime)
    get_memory_manager().add_nowait(
        event.thread_id,
        list(event.messages_to_summarize),
        agent_name=event.agent_name,
        user_id=user_id,
    )
```

为什么需要它:**没有它,摘要一旦把消息从 state 里删掉,这些对话就永远丢失**——它们本该是记忆抽取的输入。所以压缩前必须"抢救式"把即将被删的消息塞进队列。

三个关键行为:

| 行为 | 机制 | 为什么 |
|---|---|---|
| **走 `add_nowait` 而非 `add`** | `add_nowait` 立即调度(debounce=0),`bypass_watermark=True` | 等常规 30s debounce,消息可能已经被删了;必须马上入队 |
| **绕过索引水位** | `bypass_watermark=True` | 紧急快照是"删前一次性截图",其自身长度如果计入水位会**倒退**对话水位(下次正常抽取会把已经压缩掉的内容当成新内容重抽);绕过它,快照与正常更新互不干扰 |
| **与待处理正常更新共存** | 队列按 `(thread,user,agent)` + `bypass_watermark` 做匹配 key | 紧急冲刷**不覆盖**同 key 的待处理正常更新——覆盖会丢掉正常更新未抽取的尾部(若用户随后停止对话,那个尾部不会被下一轮补喂) |

**子代理路径 `skip_memory_flush=True`**:`_create_summarization_middleware` 只在 `memory.enabled and not skip_memory_flush` 时挂上这个 hook。子代理构建链(`build_subagent_runtime_middlewares`)强制传 `skip_memory_flush=True`,因为子代理与父线程共享 `thread_id`——不跳过的话,子代理内部压缩也会把它的中转对话冲刷进**父线程**的持久记忆(与 §11.1 里子代理不挂 `MemoryMiddleware` 是同一层防护,双保险)。

### 11.5 分工边界:中间件 vs 后端

一句话总结:**中间件管"何时、为谁捕获/注入",后端管"捕获到之后怎么做"**。

| 维度 | 中间件层(`agents/middlewares/`) | 后端层(`agents/memory/backends/<name>/`) |
|---|---|---|
| 读 | `DynamicContextMiddleware` 决定"注入到哪个消息、是否跨午夜" | `manager.get_context()` 决定"格式化什么、按置信度/预算截断什么"(见 §8) |
| 写 | `MemoryMiddleware` / `memory_flush_hook` 决定"何时捕获、身份是谁" | `filter_messages_for_memory` + `detect_signals` + `MemoryUpdateQueue`(debounce) + LLM 抽取 + 闸门 + 落盘(见 §4–§7) |
| 错误处理 | 注入失败 → 空上下文,不打断对话(`_get_memory_context` 捕获 `MemoryManagerError`,除非 `failure_policy.read: fail_closed`) | 队列抽取失败 → 本次不记,水位不前进,下次对话重抽(降级不丢原则,见 §2) |

这也是为什么写自定义后端时只动 `backends/`,两个中间件一行都不用改——它们与后端通过 `MemoryManager` 抽象契约(`add` / `get_context` / `add_nowait` / `aadd`)通信(见 §10)。

---

## 附录:一个贯穿始终的例子,完整走一遍

**对话**:A 说「帮我写个 Python 脚本,把 CSV 转 JSON」→ Agent 给出用 `pip install pandas` 的脚本 → A 纠正「用 uv 别用 pip」→ Agent 重写。

1. **捕获**(阶段 4):MemoryMiddleware 拿到 4 条消息(user, ai, user, ai)+ user=A。
2. **过滤**:保留 4 条;detect_signals 命中 `{correction}`。
3. **入队**:key=(thread1, A, __default__),30s 后触发。
4. **组装 prompt**:当前记忆(含旧事实 `fact_z: A 用 pip`)+ 对话 + correction_hint + 可能的 staleness 段。
5. **抽取**:LLM 返回 §6.1 的 JSON —— `newFacts[0]="用 uv"`(correction, user, durable, descriptive)、`factsToRemove=[fact_z → replacement 0]`。
6. **闸门**:`newFacts[0]` 三标签全过 → 接受;任务类内容被 scope gate 拒。
7. **决策**:因 `replacementFactIndex=0` 且替代品存活 → 原子删除 `fact_z`,写入 `fact_uv`。
8. **落盘**:`memory.json` revision+1;新增 `facts/ab/fact_uv.md`;`fact_z.md` 删除;FTS5 索引更新。
9. **下次对话**:A 在新会话说「帮我装个库」。DynamicContext 注入 `<memory>`,其中 correction 事实在 guaranteed 预算里必然在场。Agent 读到「用户偏好 uv 而非 pip」→ 直接给 `uv pip install ...`。
10. **循环**:这轮对话又进入下一轮抽取,`topOfMind` 更新为"正在迁移到 uv"。

---

---

# 附录 A:记忆数据存储格式与示例(完整)

## A.1 完整目录树(单个用户)

```
backend/.deer-flow/
└── users/{user_id}/
    ├── memory.json                    ← 用户共享摘要 + 共享乐观锁 revision
    ├── .memory.journal.json           ← (临时)多文件操作事务日志,操作成功后删除
    ├── .retrieval/                    ← (可选, retrieval_adapter=fts5 时) SQLite FTS5 派生索引
    │   └── <scope>.sqlite / ...       ← 只存可重建的派生数据,坏了自动重建
    └── agents/{agent_name}/           ← 每个 Agent 一个目录
        ├── config.yaml                ← 自定义 Agent 定义(与记忆并存,仅自定义 Agent 有)
        ├── SOUL.md                    ← 自定义 Agent 人设(仅自定义 Agent 有)
        └── facts/                     ← 该 Agent 的事实桶
            └── {sha256(fact_id)[:2]}/ ← 分片前缀(2 位十六进制, 最多 256 个目录)
                └── {fact_id}.md       ← 单条事实
```

**要点**:
- **没有 per-agent 的 `memory.json`** —— 摘要全局一份(按用户),事实按 Agent 分桶。
- 默认 Agent 用的是保留名 `__default__` 桶(在自定义 Agent 名语法之外,不会撞)。
- 无认证部署下 `user_id = "default"`,所有会话写进同一个用户桶。

## A.2 `memory.json` 完整示例(真实 schema)

```json
{
  "version": "1.0",
  "revision": 7,
  "lastUpdated": "2026-08-07T12:00:00Z",
  "user": {
    "workContext":     { "summary": "数据分析师, 主用 Python; 核心贡献过一个 16k star 开源项目", "updatedAt": "2026-08-01T10:00:00Z" },
    "personalContext": { "summary": "中文母语, 能读写英文; 偏好简洁沟通",                    "updatedAt": "2026-07-20T09:00:00Z" },
    "topOfMind":       { "summary": "正在做 CSV→JSON 转换脚本; 并行研究 uv 迁移; 持续关注向量数据库", "updatedAt": "2026-08-07T12:00:00Z" }
  },
  "history": {
    "recentMonths":       { "summary": "近三个月: 从 pandas 转向 polars; ...", "updatedAt": "2026-08-07T12:00:00Z" },
    "earlierContext":     { "summary": "去年: 主导过数据管道重构; ...",         "updatedAt": "2026-05-01T08:00:00Z" },
    "longTermBackground": { "summary": "十年后端经验; 关注数据工程",            "updatedAt": "2026-01-01T08:00:00Z" }
  }
}
```

**字段含义表**:

| 字段 | 类型 | 含义 | 谁写 | 用途 |
|---|---|---|---|---|
| `version` | string | 文档格式版本(`"1.0"`) | 系统 | 迁移判断 |
| `revision` | int | 共享乐观锁版本,每次写 +1 | 系统 | 并发冲突检测(§7.2) |
| `lastUpdated` | ISO-8601 | 文档最后更新时间 | 系统 | 审计 |
| `user.*.summary` | string | 各段摘要文本 | LLM | 注入画像 |
| `user.*.updatedAt` | ISO-8601 | 该段最后更新时间 | 系统 | 判断"多久没更新" |
| `history.*.summary` | string | 各历史段文本 | LLM | 注入历史脉络 |
| `history.*.updatedAt` | ISO-8601 | 该段最后更新时间 | 系统 | 同上 |

**注意**:磁盘上的 `memory.json` **刻意不存 `facts` 字段**;`facts` 只在内存的"兼容文档形态"里出现(由系统把摘要 + 事实文件合并成完整视图)。这样共享摘要文件保持小、稳定、不与事实变更耦合。

## A.3 事实 Markdown:两种典型示例

### A.3.1 自动抽取的事实(LLM 产出)

```
backend/.deer-flow/users/default/agents/__default__/facts/ab/fact_uv.md
```

```markdown
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
  type: extraction
  threadId: "thread-abc"
---

# 用户偏好使用 uv 而非 pip 安装 Python 依赖

用户在某次对话中纠正: 安装依赖用 uv, 不用 pip。
```

### A.3.2 用户手写的事实(source.type=manual)

手动通过 `/api/memory` 导入或人工编辑的事实,`source.type=manual`:

```markdown
---
id: manual_001
schemaVersion: 2
content: 用户所在团队统一用 uv 作为包管理器
category: context
confidence: 1.0
createdAt: "2026-08-01T09:00:00Z"
updatedAt: "2026-08-01T09:00:00Z"
revision: 1
expected_valid_days: 365
source:
  type: manual
  user: "user-01"
---

# 用户所在团队统一用 uv 作为包管理器

由管理员手动导入。
```

> **`manual` 是特权来源**:抽取阶段会给这类事实打 `[MANUAL]` 标记,并告诉 LLM"除非新对话是显式、无歧义的纠正,否则保持原样"——防止自动抽取把人工维护的高信任事实改坏。

**字段含义表(补充 §3.2)**:

| 字段 | 类型 | 含义 | 谁写 | 备注 |
|---|---|---|---|---|
| `id` | string | 唯一标识,合法字符 `[A-Za-z0-9_-]` | 系统 | 供删除/更新寻址 |
| `schemaVersion` | int | 事实格式版本 | 系统 | `2` 表示 Markdown 事实格式 |
| `content` | string | 事实正文 | LLM/手动 | 语义核心 |
| `category` | enum | preference/knowledge/context/behavior/goal/correction | LLM | 注入优先级 + staleness 豁免 |
| `confidence` | float 0-1 | 置信度 | LLM | 排序竞争注入预算;低于阈值不写 |
| `createdAt`/`updatedAt` | ISO-8601 | 创建/最近更新 | 系统 | 寿命与 staleness 计算 |
| `revision` | int | 单事实乐观锁 | 系统 | 并发改同一条时防覆盖 |
| `expected_valid_days` | int | 预期寿命(天),到期进复查 | LLM | staleness 生命周期 |
| `source.type` | enum | extraction / manual | 系统 | manual = 高信任受保护 |
| `source.threadId` | string | 来源线程(仅 extraction) | 系统 | 溯源 |

## A.4 事实分片(Sharding)

```
{fact_id}.md → 存放于 facts/{sha256(fact_id)[:2]}/{fact_id}.md
```

- 例:`fact_uv` → 计算 `sha256("fact_uv")`,取前 2 位十六进制字符作为子目录名(示意,实际值以计算为准)。
- **为什么分片**:单 Agent 事实可能成千上万;不分片会把几千个文件塞进一个目录,文件系统性能骤降(目录项线性扫描)。256 个桶摊薄后每个目录几百个文件是健康的。

## A.5 多文件事务日志 `.memory.journal.json`

一次写操作常涉及 **1 个 memory.json + 多个事实 .md**(比如一次更新改了摘要又增删了 3 条事实)。为了让"多文件写入"崩溃后可恢复,写前先落一份日志:

```json
{
  "operationId": "op_8f3a...",
  "state": "prepared",                // prepared → committed, 崩溃后据此决定回滚/恢复
  "expectedRevision": 6,              // 操作基于的共享 revision
  "agentName": "__default__",         // 涉及哪个 Agent 桶
  "factIds": ["fact_uv", "fact_z"],
  "oldEntries": {                     // 被覆盖前的旧内容(回滚用)
    "fact_uv": "...旧 front matter...",
    "fact_z":  "...旧内容..."
  }
}
```

- 操作**成功后删除**日志;操作中途崩溃,下次启动 `_recover_journal` 读到日志 → 用 `oldEntries` 回滚到一致状态。
- 日志名固定 `.memory.journal.json`,放在 memory.json 同目录。

## A.6 检索索引 `.retrieval/`

当 `retrieval_adapter: fts5`(默认),存储层维护一个 **SQLite FTS5** 派生索引:

```
backend/.deer-flow/users/{user_id}/.retrieval/
└── <scope>.sqlite          # 每 (user, agent) 一个 scope 库/表
```

- 存的是**可重建的派生数据**(事实的 FTS5 行),不是权威数据。
- 事实写入/删除后,存储层在**释放锁之后**通知索引 upsert/remove。
- 索引损坏 → 删除重建一次;仍失败 → 退回子串检索(`substring_fallback`)。
- 启动时 `warm_retrieval()` 后台全量重建,就绪后 `memory_search` 有索引可用。

> 中文场景可选 `memory-zh` extra 启用 jieba 分词;否则用 SQLite unicode 分词 + 子串兜底。

## A.7 迁移与备份文件

| 文件 | 何时出现 | 内容 | 处理 |
|---|---|---|---|
| `memory.json.v1.bak` | v1(内嵌事实)→ v2(Markdown)迁移时 | 迁移前的完整旧文件 | 迁移后保留,可回滚 |
| `{agent}/facts/` | v1→v2 迁移时按 Agent 生成 | 从旧 JSON 拆出的单条事实 .md | 迁移产物 |
| `.memory.journal.json` | 每次多文件写操作 | 事务日志 | 成功后删除 |

迁移规则:任何**破坏性 v2 写**之前,旧 JSON 必须已备份成 `.v1.bak`;备份缺失或与预期不符 → 中止,不改 v1 数据。冲突(摘要撞车)保留源文件并 fail-loud。

## A.8 一个用户的记忆"进化史"(before / after)

**对话 1**:用户 A 说「我是后端工程师,常用 Python」。

- `memory.json`(新建):
  ```json
  { "version": "1.0", "revision": 1, "lastUpdated": "...",
    "user": { "workContext": { "summary": "后端工程师, 常用 Python", "updatedAt": "..." }, ... },
    "history": { ... } }
  ```
- `facts/ab/fact_ctx.md`:
  ```markdown
  content: 用户是后端工程师, 主用 Python
  category: context, confidence: 0.95, expected_valid_days: 365
  ```

**对话 2**:A 纠正「用 uv 别用 pip」(此前已记过"用 pip")。

- 新增 `facts/ab/fact_uv.md`:`category: correction, confidence: 0.98`。
- 删除 `facts/zz/fact_pip.md`(旧"用 pip"事实,经 `factsToRemove` + replacement 原子删除)。
- `memory.json`: `revision → 2`,`topOfMind.summary` 更新为「正在迁移到 uv」,`lastUpdated` 更新。

**对话 3**(90 天后):`fact_ctx` 进入 staleness 复查 → LLM 判 EXTEND → `expected_valid_days` 调大,`revision` +1。

**观察**:摘要文件"长不大"但内容随对话演进;事实文件"只增删改单条",彼此隔离,坏一条不影响其他。

---

*本文对应代码:`backend/packages/harness/deerflow/agents/memory/`。字段/策略均与当前实现一致;若代码演进,以代码为准。*
