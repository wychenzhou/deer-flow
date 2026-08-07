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
| 每段的 `scope`/`authority` | 抽取期的门禁标签(不落盘) | 见 §5.3 |

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

# 用户偏好使用 uv 而非 pip 安装 Python 依赖

用户在某次对话中纠正: 安装依赖用 uv, 不用 pip。
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

**一个关键设计决定:`scope`/`durability`/`authority` 不落盘。** 这三个标签只活在"抽取期",用完即弃(见 §5.3)。原因:它们是**决策输入**而非**数据**,落盘会污染数据模型、增加迁移负担;校验发生在写入那一刻就足够了。

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
   - 用模式文件匹配最近几轮,识别 **correction(用户纠正)** / **reinforcement(用户认可)** 两类信号。
   - **为什么**:correction 信号让抽取格外重视"纠正后的内容"——这是记忆里价值最高、最该保留的信息。

---

## 5. 写链路第 2 步:抽取(核心)

### 5.1 抽取的产物

一次抽取 = **一次 LLM 调用**,同时输出四类决策:

```
① 摘要更新     → 六段摘要中哪些要改、改成什么
② 新事实       → 新学到的原子陈述
③ 矛盾删除     → 旧事实里哪些被新信息取代(可带 replacement)
④ 生命周期决策  → 过期事实: KEEP / REMOVE / EXTEND; 碎片事实: 是否合并
```

设计上把"更新摘要、抽事实、删矛盾、判过期、做合并"**合并成一次调用**:
- 省 API 调用(每次对话只多一次 LLM)。
- 让模型同时看到"当前记忆 + 本轮对话",能做出**跨时间的判断**(比如"这条旧事实已被新对话推翻")。

### 5.2 Prompt 设计(原文核心)

抽取用的是一个 chat 模板。它的**系统指令**承担了绝大部分"教模型怎么记"的责任。结构如下(为可读性做了精简,语义与源码一致):

```text
【系统指令】
你是记忆管理系统。分析对话并更新用户记忆画像。

指令:
1. 分析对话中关于用户的重要信息
2. 提取相关事实、偏好、上下文,带上具体细节(数字、名字、技术)

抽取前先做结构化反思:
1. 错误/重试检测:Agent 是否出错?仅在它是可复用的用户级工作模式时记录根因和正确做法。
2. 用户纠正检测:用户是否纠正了 Agent?区分"可复用的用户级纠正"与"当前任务的局部修正"。
   仅当 category=correction 且错误明确时填写 sourceError。
3. 项目约束发现:项目级约束归为 project-scope,绝不提升到用户记忆。

作用域与安全分类(决定能否入库,由确定性写闸门再次校验):
- scope="user"     : 可安全注入到无关未来会话/任务/项目
- scope="thread"   : 仅本次请求/回复有效
- scope="project"  : 仅某项目内有效
- durability="durable"  : 预期跨会话仍为真
- durability="temporary": 临时的、一次性的
- authority="transactional": 指令/授权(编辑、删除、发布...),绝不允许进长期记忆
- authority="descriptive" : 对用户的描述,不授予任何操作权限
- 不确定时用 thread/project 或 temporary;绝不猜 user+durable。

记忆段编写指南:
- workContext   : 职业、公司、活跃项目、主技术栈(2-3 句)
- personalContext: 语言、沟通偏好、关键兴趣(1-2 句)
- topOfMind     : 多个进行中的关注点和优先级(3-5 句详述)
- recentMonths  : 近 1-3 个月活动详述(4-6 句)
- earlierContext: 3-12 个月前的重要模式(3-5 句)
- longTermBackground: 持续的背景与基础信息(2-4 句)

事实提取:
- 每条事实必须带 scope/durability/authority(由确定性写闸门评估,不持久化)。
- 只有 scope=user + durability=durable + authority=descriptive 才可能入库。
- 不要为以下内容建事实:当前任务目标、验收标准、工作区状态、
  当前文件/commit/错误状态、项目级约束、一次性操作授权。
- 提取具体的量化细节(如 "16k+ GitHub stars")、专有名词、版本号。
- 类别: preference | knowledge | context | behavior | goal | correction
- 事实寿命 expected_valid_days(整数,天数,到期自动复查):
    <=14 高度临时; 15-60 短期; 60-180 中期; 180-365 稳定; >365 非常稳定
- 置信度: 0.9-1.0 显式陈述; 0.7-0.8 强暗示; 0.5-0.6 推断(慎用)

关键规则:
- 仅当有实质新信息时 shouldUpdate=true。
- 只有用户显式、在 user 级矛盾/撤回时才 factsToRemove;
  thread/project 级局部例外不构成对 user 级事实的矛盾。
- factsToRemove 必须带 scope 和 reason;用 replacementFactIndex
  (零基索引)指向 newFacts 里替代它的那条;纯撤回不带。
- 绝不记录文件上传事件(会话级、未来不可达)。
- 只返回 JSON,不要解释和 markdown。

【用户消息】
Current Memory State:
<current_memory>
{当前记忆: 六段摘要 + 事实列表 的 JSON}
</current_memory>

New Conversation to Process:
<conversation>
{本轮对话: "user: ...\nassistant: ..." 逐条}
</conversation>

{correction_hint  ← 命中 correction 时的附加提示}
{staleness_review_section  ← 有 aged 候选时的复查段}
{consolidation_section  ← 有碎片候选时的合并段}

Update the memory based on the above conversation.
```

**Prompt 设计的几个关键意图**:

| 设计 | 意图 |
|---|---|
| "不确定时用 thread,绝不猜 user+durable" | 宁可漏记,不可错记(错记=错误个性化) |
| "拒绝 transactional" | 防止把用户临时授予的权限(如"可以删这个文件")变成永久指令 |
| 结构化反思(error/correction/project) | 让模型先分类再抽取,而不是看到字面就记 |
| `replacementFactIndex` 协议 | 给"矛盾删除+替换"一个原子契约,见 §6.3 |
| "不记文件上传/任务目标/工作区状态" | 抽离一次性噪音,保高信噪比 |
| 每段独立 `shouldUpdate` + 独立 scope 判定 | 防止"这段大部分是废话但有一段有用"拖累整段 |

### 5.3 三种标签:写闸门(为什么是"确定性"的)

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

**为什么逐条 fail-closed**:一条坏数据入库,会在未来每次注入里持续污染 Agent 的行为;而拒绝一条,代价只是"这次没记到"。方向性选择宁可保守。

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

### 6.2 过期生命周期(staleness)

每条事实带 `expected_valid_days`(创建时 LLM 赋,服务端钳制到上限)。超过寿命或全局兜底 `staleness_age_days` 的候选事实,在某次正常更新调用里被 LLM **顺便复查**(不额外发 API):

```
对每个 aged 候选: KEEP(继续留) / REMOVE(删) / EXTEND(延 N 天)
```

**多重防线**:
- 复查范围由 `_select_stale_candidates` **确定性地圈定**——LLM 只能在这批候选里选,不能越权删别的。
- LLM 提议的删除/延长,实际执行前与候选集**求交集**:protected 类别(correction)永不被过期清掉。
- 每周期最多删 `staleness_max_removals_per_cycle` 条,超出的按置信度从低到高保留。
- 被提议删除的事实**不能同时被延长**(防 LLM 自相矛盾)。
- EXTEND 有绝对上限(`staleness_max_extension_days`),防一次延长把它永久雪藏。

**为什么过期这么重要**:记忆不清理会无限膨胀、注入预算会爆、陈旧事实会误导 Agent。过期 = 自动的"记忆回收"。

### 6.3 碎片合并(consolidation,默认关)

当某类别事实数量超过阈值,可把多条碎片合成为一条:**有损**(源事实被合成的替代,只留 `consolidatedFrom` 记录)。所以默认关闭,需显式开启。

守卫:源必须存在且组间不重叠、组大小有上限、合并事实置信度 ≤ 源最大值、低于阈值不写。

**权衡**:合并不了少记空间,省 token;但丢细节。设计上宁缺毋滥——这是"默认关"的原因。

### 6.4 去重与上限

- 内容去重:whitespace 归一化后已存在相同内容 → 拒绝(防重复记录)。
- `max_facts`(默认 100):超限按置信度从高到低保留。

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

### 8.1 召回时机

`DynamicContextMiddleware` 在**每次模型调用前**(每条用户消息进入)执行 `get_context()`:

```
memory_data = load(user_id, agent_name)      # 读该用户该 Agent 的记忆
text = format_memory_for_injection(memory_data, max_tokens=2000, ...)
→ 注入首条 HumanMessage 前部(与当前日期一起)
```

### 8.2 注入预算分配(为什么"纠正"几乎总是被带上)

```python
format_memory_for_injection(
    memory_data,
    max_tokens=2000,                 # 主预算
    guaranteed_categories=["correction"],   # 特权类别
    guaranteed_token_budget=500,     # 特权预留
    use_tiktoken=...,                # 计费方式
)
```

分配算法:

```
1. 特权类别(correction)的事实先放,占用 guaranteed_token_budget(500) —— 永不占用主预算
2. 摘要(六段)一定放
3. 剩余主预算按置信度从高到低放普通事实,直到用满 2000
```

**为什么 correction 是特权**:用户纠正的信息价值密度最高、最该让 Agent"记得别再犯"。把它放在保留区,意味着即使普通事实挤满了,纠正也一定在场。

### 8.3 注入形态(模型看到什么)

```xml
<memory>
  <user context>
    workContext: 数据分析师, 常用 Python
    personalContext: 中文母语, 偏好简洁
    topOfMind: 正在做 CSV→JSON 转换; 研究 uv 迁移
  </user context>
  <history> ... </history>
  <facts>
    - [correction | 0.98] 用户偏好使用 uv 而非 pip 安装 Python 依赖 (avoid: 先前建议使用 pip)
    - [context | 0.85] 用户使用 Python 做数据处理
  </facts>
</memory>
```

设计要点:
- 注入为 **hidden 数据块**(不参与对话显示,进上下文)。
- 文本经转义/归一,防记忆内容被当作指令注入。
- `injection_enabled: false` 可完全关闭注入,但照常抽取存储(适合"只记不用")。

### 8.4 run-level memory identity(可观测性)

每次带 `<memory>` 的 run,对该块的精确内容做 SHA-256,记一条 `context:memory` 事件(只存 hash,不重复存全文)。用途:
- 审计"这次 run 到底用了哪份记忆"。
- 后续 run / 分支复用冻结的记忆块(不重复读盘),校验来源防伪造。

### 8.5 检索(tool 模式)

当 `mode: tool` 时,不再自动注入事实,而是给模型注册四个记忆工具:

| 工具 | 作用 |
|---|---|
| `memory_search` | 按查询召回相关事实 |
| `memory_add` | 显式记一条 |
| `memory_update` | 改一条 |
| `memory_delete` | 删一条 |

检索后端默认 SQLite FTS5(可选中文 jieba 分词),存的是**可重建的派生索引**——坏了自动重建。

> 两种模式的选择:middleware(默认)= 被动自动,稳定省心;tool = 模型主动、更灵活但依赖模型自觉,属实验特性。

### 8.6 召回逻辑详解:不是一种机制,是两种

**"召回"这个说法容易让人以为是"按关键词搜"。实际上系统里存在两种完全不同的召回机制,取决于 `mode`。** 下面分别讲清楚。

#### (a) middleware 模式:不检索,而是"全量注入 + 置信度排序 + 预算截断"

默认模式下,**根本没有查询词**。召回 = 把该 `(user, agent)` 的全部记忆读出来,然后:

```
① 6 段摘要:无条件全部注入
② 事实排序:按 confidence 从高到低(严格顺序)
③ 预算分配:
   - guaranteed 类别(correction)先放,占用预留 guaranteed_token_budget(500)
   - 剩余主预算(max_injection_tokens=2000)
   - 按排序后的顺序逐条贪心放入,遇到"放不下"的第一条就停止
     (严格保持顺序:一条低置信度但很短的普通事实,不会插到
       被跳过的高置信度事实前面)
```

关键点(`prompt.py::_select_fact_lines` / `_fallback_format_facts`):

- 排序键是 **confidence 降序**(`sorted(..., key=confidence, reverse=True)`)。
- **不是语义相关、不是关键词、不是向量**——就是"置信度最高的先带,预算内能带多少带多少"。
- 被截断的事实**不查、不用**:每次注入重新读盘重新算,记忆文件本身不受影响。

**这样设计的理由**:注入路径要快、要确定、不能引入额外的一次 LLM/检索依赖;置信度已经是"这条信息有多可靠"的最好代理。宁可让普通事实截断,也要保证 correction 类永远在场。

#### (b) tool 模式:`memory_search(query)` 才是真正的关键词/全文检索

模型主动调用 `memory_search(query, top_k=5)`,走 SQLite **FTS5** 全文索引(`retrieval.py`)。完整流程:

```
① 分词
   中文 → jieba(可选 extra `memory-zh`;未装则退化为 unicode/空白切分)
   英文 → 空白切分
② 构造 FTS5 查询
   - 若 query 是 FTS 高级语法(AND / OR / NOT / 短语 "..." / 前缀 tok*) → 直接透传
   - 否则(自然语言)→ 拆 token → "tok1" OR "tok2" OR ...
     (每个 token 加引号、转义标点 → 自然语言中的标点不会被当成 FTS 运算符)
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

| 维度 | middleware(默认) | tool(实验) |
|---|---|---|
| 触发 | 每次 run 开始,自动 | 模型主动调 `memory_search` |
| 方法 | **无检索**:全量读 + 置信度排序 | **FTS5 全文检索**(jieba/空白分词) |
| 打分 | confidence(截断靠 token 预算) | bm25 + confidence + 时间衰减 |
| 关键词 | ❌ 无关 | ✅ 核心 |
| 注入内容 | 摘要 + 选中事实(预算内) | 只注入摘要,事实靠检索取回 |
| 失败兜底 | 注入失败 → 空上下文(不打断对话) | FTS 失败 → 子串匹配 |

**两个常见误解**:

1. "记忆会不会被关键词检索"——**默认模式不会**。它不搜,只是"按置信度把能带的都带上"。没有语义排序,也不是 RAG 式向量检索。
2. "FTS5 索引是干嘛的"——它服务 **tool 模式的 `memory_search`** 和未来的按需召回;middleware 模式的注入完全不依赖它(**索引坏了不影响注入,只影响搜索**)。

> 一句话:**middleware = "记得多少带多少"(置信度截断);tool = "需要时按关键词查"(FTS5 全文检索)。**

---

## 9. 隔离模型

| 维度 | 隔离机制 | 说明 |
|---|---|---|
| 用户 | `users/{user_id}/` 目录 + 每用户锁 | 用户之间物理隔离;`strict_user_scope` 开启后强制带 user_id |
| Agent | `agents/{agent_name}/facts/` | 同一用户下不同 Agent 记忆互不串 |
| Thread | **不隔离**(仅作去重 key) | 设计上记忆就是要跨会话共享;线程不参与存储隔离 |

一个直接推论:换自定义 Agent = 换记忆桶;开认证 = 每个登录用户一份记忆。

---

## 10. 运营

- **迁移**:旧版内嵌事实的 JSON → 自动迁移到 Markdown,迁移前备份 `.v1.bak`,冲突 fail-loud 不丢数据。可提前审计:`python scripts/migrate_memory_markdown.py --all-users --dry-run`。
- **备份**:事实是普通文件,可整体打包 `backend/.deer-flow/users/`。
- **监控**:`scope_gate_rejections` 统计 + 抽取指标回调(Langfuse span);`/api/memory/status` 看后端状态。
- **配置热更新**:`memory.*` 属可热加载字段,改 config 下次消息生效。

---

## 11. 权衡与取舍(为什么这么做)

| 决策 | 权衡 | 为什么偏向这个方向 |
|---|---|---|
| 文件存储而非 DB | 牺牲跨进程强一致 | 单机够用;文件可 diff、可人工编辑、零依赖;乐观锁兜住并发 |
| 一次 LLM 调用做四件事 | 单次调用更复杂 | 省 API;让模型做跨时间判断(矛盾/过期/合并) |
| 逐条 fail-closed 闸门 | 可能漏记 | 漏记代价 < 错记代价(错记会持续污染个性化) |
| correction 特权注入 | 挤占注入预算 | 纠正价值最高,值得永远在场 |
| 过期/合并默认保守 | 记忆可能不够"精炼" | 宁保留也别误删;合并默认关因有损 |
| `scope/durability/authority` 不落盘 | 每次重算 | 它们是指决策输入,不是数据;避免污染模型 |
| 隔离按 (user,agent),线程不隔离 | 跨线程共享 | 这正是"长期记忆"的意义;线程级隔离另有会话历史承担 |

---

## 12. 扩展点(如何接入自己的存储)

- **换后端**:`memory.manager_class` 可以是内置名(`deermem`/`mem0`/`openviking`/`noop`),或**任意 dotted 路径**指向一个 `MemoryManager` 子类。`backend_config` 原样传给后端 `__init__`,后端自己解析。
- **换存储类**:`backend_config.storage_class` 可指向自定义 `MemoryStorage` 实现(实现 `load/save/apply_changes/fact CRUD` 契约)。
- **自定义抽取模板**:`backend_config.prompts_dir` 指向目录,可覆盖 `memory_update.chat.yaml`/`staleness_review.yaml`/`consolidation.yaml`/`fact_extraction.yaml`(支持按 Agent 分子目录)。
- **自定义信号模式**:`backend_config.patterns_dir` 覆盖 `correction.yaml`/`reinforcement.yaml`。
- **观测钩子**:`extraction_callback` 在每次抽取后回调(token 用量、接受/拒绝统计)。

> 写自定义后端记得:解析失败必须 fail-loud(不能静默换回 deermem);网络后端需实现异步 `a*` 方法,避免阻塞事件循环。

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

升级/迁移时还可能出现的备份文件:

```
    ├── memory.json.v1.bak             ← v1→v2 迁移前自动备份的旧文件(迁移后保留)
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

## A.9 运维速查

- **该备份什么**:整个 `backend/.deer-flow/users/`(摘要 + 事实 + 索引都可从 .md 重建,但备份更省事)。索引 `.retrieval/` 可重建,可选备份。
- **人工编辑**:可直接改 `.md`;但改完要触发 `reload`(或等下次自动重载),且手工编辑**不更新 `revision`**——并发写可能冲突,建议编辑后调用 `/api/memory/reload` 并避免同时对话。
- **彻底重置某用户记忆**:删除 `users/{user_id}/` 整目录(下次对话重建)。前端 `/api/memory` 也提供清空操作。
- **看不到事实?** 检查:① 是否 `injection_enabled: false`;② 是否该 Agent 桶(换了 Agent 就换了桶);③ 是否被 staleness 清掉(可查日志 `rejected_by_scope_gate` / staleness 记录)。

---

*本文对应代码:`backend/packages/harness/deerflow/agents/memory/`。字段/策略均与当前实现一致;若代码演进,以代码为准。*
