# 文件安全与停滞守卫：ReadBeforeWrite × ToolProgress（链位 11、12）

> 本篇拆解链上**相邻的两个可选中间件**：`ReadBeforeWriteMiddleware`（**文件写入门禁/版本门**，issue #3857）防「**盲写**」（基于过期内容改文件、破坏正确性）；`ToolProgressMiddleware`（**工具结果质量守卫/停滞状态机**，RFC #3177）防「**停滞**」（有输出但没新信息、烧 token 空转）。一个管文件一致性，一个管投入产出；都长在「工具调用」这条垂直切面上，共享同一套 `deerflow_tool_meta` 元数据语言，所以放在一起讲。
> 源码相对路径：`backend/packages/harness/deerflow/agents/middlewares/`；链装配基线见 [`agents/middlewares/AGENTS.md`](../../backend/packages/harness/deerflow/agents/middlewares/AGENTS.md) 与[目录索引](README.md)。

## 本文件覆盖的中间件

| 链位 | 中间件 | 一句话职责 | 主钩子 | 装配条件 |
|---|---|---|---|---|
| 11 | `ReadBeforeWriteMiddleware` | 写入门：读过的版本哈希匹配才放行写 | `wrap_tool_call` | `read_before_write.enabled`（默认开） |
| 12 | `ToolProgressMiddleware` | (thread,tool) 停滞状态机：warn → block | `wrap_tool_call` + `wrap_model_call` + `before_agent` | `tool_progress.enabled`（默认关） |

两个主角共享 `deerflow_tool_meta`（`middlewares/tool_result_meta.py`）——ToolProgress 能读懂结果质量，前提是每个结果都带结构化元数据。

---

## 0. 阅读地图:它们长在链的哪里

DeerFlow 的全部工具调用都会自外向内穿过一条**严格排序**的中间件链(顺序即正确性)。
`wrap_tool_call` 方向:装配列表第一个 = 最外层。本文两个主角位于运行时基础链
(shared runtime base,`tool_error_handling_middleware.py::_build_runtime_middlewares`)的
tail 段(位置标号沿用 AGENTS.md 的 11/12):

```
模型 tool_calls → 逐层穿过 wrap_tool_call(外 → 内):
  ToolReceiptMiddleware(13,receipts 记账,最外层) → Guardrail(9) → SandboxAudit(10)
  → ReadBeforeWriteMiddleware(11 ← 本文主角①:版本检查,可拦截)
  → ToolProgressMiddleware(12 ← 本文主角②:可拦截,执行后读 meta)
  → ToolErrorHandlingMiddleware(13:异常→错误 ToolMessage + stamp meta)
  → 实际工具(read_file / write_file / str_replace / web_search / task …)
```

装配代码(节选,顺序即外层→内层):

```python
tail.append(SandboxAuditMiddleware())
if app_config.read_before_write.enabled:      # 默认开
    tail.append(ReadBeforeWriteMiddleware())  # 11
if app_config.tool_progress.enabled:          # 默认关
    tail.append(ToolProgressMiddleware.from_config(...))  # 12
tail.append(ToolErrorHandlingMiddleware(...))             # 13,最内
```

两个主角都是**可选**中间件:`read_before_write.enabled` 默认 `True`(影响文件正确性,默认
护航),`tool_progress.enabled` 默认 `False`(成本守卫,默认不干预)。lead 与 subagent 复用
同一 runtime base(`build_subagent_runtime_middlewares`),门禁一致生效。

### 两个中间件的共同语言:`deerflow_tool_meta`

ToolProgress 能"读懂"结果质量,前提是每个工具结果都带结构化元数据
`additional_kwargs["deerflow_tool_meta"]`(定义在 `middlewares/tool_result_meta.py`):

```python
@dataclass(frozen=True, slots=True)
class ToolResultMeta:
    status: Literal["success", "error", "partial_success"]
    error_type: str | None   # auth/config/internal/rate_limited/transient/permission/
                             # no_results/not_found/unknown/blocked_by_progress_guard…
    recoverable_by_model: bool                  # 模型重试(换思路)能否修复?
    recommended_next_action: Literal["continue", "rewrite_query", "try_alternative",
                                     "summarize", "stop"]
    source: Literal["exception", "tool_return",   # 生产方:异常包装 / 工具自带错误文本 /
                    "content_analysis",           # 内容启发式(web_fetch 错误页等)/
                    "progress_middleware"]        # 本中间件自产
```

meta 的**生产地是内层 ToolErrorHandling**(每个结果返回时 `normalize_tool_result` 归一化),
消费地是外层 ToolProgress——所以 ToolProgress **必须坐在 ToolErrorHandling 外层**。
分类由关键词规则表 `_ERROR_RULES` + 内容启发式完成(见第 2.3 节映射表)。

---

## 1. ReadBeforeWriteMiddleware:最外层写门(链第 11 位)

### 1.1 它解决什么问题:盲写

issue #3857 的原始故障:主 agent 交付报告时"append-only,从不读回"——同一份报告段落被
**追加了五次**,且模型完全不知道。典型形态三连:**重复 append**(同段内容追加 N 次)、
**覆盖已有文件**(拿过期记忆写掉模型没看过的当前版本,写头 ≠ 读头,见 §9.2「先读状态再
改写」)、**`str_replace` 过期替换**(基于旧内容做精确替换,替换点早已漂移)。
ReadBeforeWrite 的答案不是"让模型更小心",而是把"先读后写"变成**不可绕过的版本门**:
想改一个已存在的文件,会话上下文里必须先有一次 `read_file` 读到的内容哈希 == 文件当前
哈希,否则写直接被拦。

### 1.2 钩子与执行时机

- **只挂 `wrap_tool_call` / `awrap_tool_call`**(同步/异步两份对称实现)。它没有
  `wrap_model_call`、没有 `before_agent`——纯工具面中间件,状态不需要 run 边界管理。
- 关注的工具只有三个,其余原样透传 `handler(request)`:

```python
_READ_TOOLS       = frozenset({"read_file"})
_GATED_WRITE_TOOLS = frozenset({"write_file", "str_replace"})
```

- 同一个 `wrap_tool_call` 同时服务两条分支,且**共用同一把锁、同一个沙箱授权 scope**:
  - `read_file` 命中 → 先让内层执行读,返回后 `_attach_read_mark` 给结果盖读标记;
  - `write_file` / `str_replace` 命中 → 先做 `_check_write_gate` 版本检查,被拦直接短路
    返回,放行才调用内层 handler。
- 链位置 11 = 坐在 ToolProgress(12)/ToolErrorHandling(13) **外层**。含义见 §1.6:
  被拦的写**根本不进入内层**,连 ToolProgress 的槽都不占。

### 1.3 内部实现:mark 的存与验

**mark 是什么**:`read_file` 成功返回后,中间件把"该文件当前完整内容的 sha256"盖到这条
`ToolMessage.additional_kwargs["deerflow_read_mark"]` 上:

```python
READ_MARK_KEY = "deerflow_read_mark"
message.additional_kwargs[READ_MARK_KEY] = {
    "path": posixpath.normpath(path),   # 规范化路径(统一 ../ 等写法)
    "hash": hashlib.sha256(disk_content.encode()).hexdigest(),
}
```

**关键:哈希不解析工具返回的文本,而是盖戳时再读一次真实磁盘。** 因为内容可能被外层
ToolOutputBudget 截断/外置、工具文本可能不是原样,只有"读之后、写之前"二次读盘才能保证
mark 哈希的正是**模型屏幕上那一版**。因此 `read_file` 的执行与盖戳被放进同一个临界区,
"读到 → 盖章"原子完成(§1.4)。

**门怎么验**(`_check_write_gate`,约等于真实代码):

```python
def _check_write_gate(request) -> ToolMessage | None:
    current = self._content_reader(request.runtime, path)   # 读磁盘当前内容
    if self._latest_mark_hash(state, norm_path) == _content_hash(current):
        return None      # 最新 mark 哈希 == 当前哈希 → 放行
    return ToolMessage(content=_BLOCK_MESSAGE.format(tool_name=..., path=path),
                       status="error")   # 不匹配 → 拦,提示先重读
```

`_latest_mark_hash` **倒序扫描 `state["messages"]`**,取路径匹配的最新一条 `ToolMessage`
上的 mark(同文件多读几次,只有最新那次算数)。路径经 `posixpath.normpath` 规范化,沙箱内
路径统一按 POSIX 处理。

**被拦的提示** `_BLOCK_MESSAGE` 直说因果并给出低成本出路:"任何写入都会使此前的读失效,
每次修改前都要重读;分段读(比如 append 前读最后 ~30 行)就够"——引导模型做**最小必要
重读**,而不是把整个大文件重新读一遍。

一次完整的"读-改-读-改"时序:

```
模型            RBW 门                                  磁盘
 │  read_file   ──lock(path)──▶ [内层执行读] ──▶ hash(二次读盘)──▶ stamp mark
 │               └───────────── 返回读内容 + mark(读到的就是当前版) ──▶ 模型
 │  write_file  ──lock(path)──▶ 磁盘当前 hash ?= 最新 mark hash
 │                               ├─ 相等 → 放行,执行写(写后磁盘 hash 已变)
 │                               └─ 不等 → error ToolMessage("先重读再写")
 │                                        (此路根本不进内层 TP/TEH)
 ▼  模型重读 → 盖新 mark → 再写 → 新 hash == 新 mark → 放行
```

### 1.4 临界区:(thread, path) 级串行化

**坑**:LangGraph 会把同一个 AIMessage 里的多个 tool_calls **并发执行**。若门禁检查与工具
执行不串行,同轮两个写可以**同时**拿着同一个过期 mark 通过检查、再各自落地——双双基于
过期版本写,门形同虚设。

**对策**:`_GATE_LOCKS`(进程级 `weakref.WeakValueDictionary`)按 `(scope, norm_path)` 存
`threading.Lock`。scope 解析优先级:`runtime.context["thread_id"]`(非空 str)→ 每个 thread
一把锁;否则 `state["sandbox"]["sandbox_id"]` → 按沙箱;再否则 `"global"`。无关 agent 之间
零争用;同 (thread, path) 的写则被完全串行——第二个写拿到锁时磁盘哈希已变,会被门拦下。
**这把锁同时覆盖 `read_file`+盖戳**,保证 mark 与模型所见版本一致。

几个实现细节值得细读:

- 锁表用 `WeakValueDictionary` + 独立 guard 锁:锁没人持有时自动被回收,防进程内无限增长;
  防"检查后、插入前"两线程都 miss 而各建一把锁(双检锁模式)。
- async 路径用 `asyncio.to_thread(lock.acquire)` 取锁、事件循环线程上 `release()`——
  `threading.Lock` 无 owner 概念,跨线程释放合法(注释明说这是有意的)。
- 与 `sandbox/file_operation_lock.py` 是**同一模式、独立命名空间**:工具内部的文件锁只
  守护 mutation 那一段;这里这把锁还覆盖**前置的授权与读检查**,职责更宽。

### 1.5 Fail-open 与 Fail-closed 的边界

门禁自己**读不到文件内容**时怎么办?源码把"无法检查"当成"放行 + 让工具自己出错",
而不是拦死——逐条过:

| 情况 | 行为 | 理由 |
|---|---|---|
| `path` 参数缺失 / 非字符串 | 放行 | 门不适用于该调用形态 |
| 读盘抛 `FileNotFoundError` | 放行 | `write_file` 是**新建**文件,无需读;`str_replace` 会自己报错 |
| 读盘抛其它异常(沙箱抖动等) | `logger.warning` + 放行 | 门是质量门不是安全边界,误拦比误放贵 |
| 读到的内容以 `"Error:"` 开头 | `debug` + 放行,**且不盖 mark** | AIO/E2B 把读失败(含文件缺失)变成错误字符串而非异常,"缺失"与"不可读"在此通道上不可区分 → 让创建继续、让工具自己报真实错误 |
| `read_file` 结果是 error 状态 | 不盖 mark | 读到的是错误不是内容 |
| 沙箱授权拒绝(`SandboxAuthorizationError`) | **不 fail-open**,转 error ToolMessage | 授权是安全边界,必须 fail-closed;不能把授权失败伪装成"文件不存在"放过去 |

除最后一行外全部 fail-open,并且 `_UNINSPECTABLE_CONTENT_PREFIX = "Error:"` 单独成常量,
注释解释得清楚:门禁失败时**宁可让工具自己产生真实错误,也不能让门禁替工具撒谎**。

**沙箱授权 scope 的共享**:写分支在 `with sandbox_authorization_scope(request.runtime):`
内依次完成"pre-write 检查 → 工具体 → (读分支)post-read hashing",三段组合调用共享
**一次** sync/async provider 决策,而不是每段都向授权层要一次结论。

### 1.6 与邻居的关系

- **在 ToolProgress(12) / ToolErrorHandling(13) 外层**,这是位置上的核心设计:被拦的写
  **直接返回、根本不调用内层 handler**,所以 ToolProgress 的 `wrap_tool_call` 不会为这次
  调用执行——**一次"合理拒绝"不消耗 ToolProgress 的停滞计数,也不会被误判为工具故障**。
- 既然短路绕过了内层 ToolErrorHandling 的 `normalize_tool_result`,RBW 在自己产的拦截
  ToolMessage 上**主动 `normalize_tool_result` 盖 `deerflow_tool_meta`**。为什么必须盖:
  最外层 ToolReceipt 与其它消费者依赖 meta 记账/分类;内容以 `Error:` 开头会被归一化为
  `error_type=unknown, recoverable_by_model=True`(可恢复、建议换策略)——恰好不会触发
  ToolProgress 的硬封路径(见 §2.3 的 recoverable 规则)。
- 对授权失败同理会 `stamp_exception_meta`(source=`exception`),保持"任何返回都带 meta"
  的不变量。

### 1.7 设计权衡

- **mark 活在 messages 上,而不是独立 state channel**:summarization 压缩掉读结果时,mark
  随消息一起消失 → 门**自动失效**,读内容不在上下文里门就不可能通过(强一致:模型必须
  "看得见"它读过的证据才能写)。代价:一次压缩后想改文件就得重新读。
- **写入永不刷新 mark**:写成功后文件哈希已变,此前所有 mark 自动作废 → 连续两次修改之间
  必然夹一次重读。这正是 #3857 原始故障(连续盲 append)的根治点。代价:同文件的
  "读-改-读-改"多花一次读——用分段读缓解。
- **哈希靠二次读盘而非解析返回内容**:不信任工具文本、不受输出预算截断影响;代价是每次
  read_file 多一次 IO(沙箱内可忽略)。
- **fail-open 而非 fail-closed**:把"门"定位成心智一致性护栏,真正的安全边界在沙箱与授权
  层(§3)。误拦会卡死完全合法的工作流,误放最多浪费一次写;拦截本身会立刻被模型看到并
  纠正。
- **门检查与工具执行同一临界区**:正确性(防同轮双写踩 stale mark)优先于吞吐。

### 1.8 源码阅读指引(`middlewares/read_before_write_middleware.py`,共 ~307 行)

| 行区 | 内容 |
|---|---|
| 1–26 | 模块 docstring:设计不变量(工具无状态、mark 挂消息、写不刷 mark、同锁串行、fail-open),**必读** |
| 52–67 | 常量:`READ_MARK_KEY`、工具集、`"Error:"` 前缀、`_BLOCK_MESSAGE` 文案 |
| 73–84 | `_get_gate_lock`:WeakValueDictionary 锁表 + guard 锁双检 |
| 102–181 | `wrap_tool_call` / `awrap_tool_call`:写分支(检查/拦截)与读分支(执行/盖戳) |
| 183–195 | `_authorization_error_result`:授权失败 → error ToolMessage + exception meta |
| 197–217 | `_lock_for` / `_lock_scope`:锁的 (thread→sandbox→global) 命名空间解析 |
| 221–251 | `_check_write_gate`:门核心——读盘、比对最新 mark、产拦截消息 |
| 253–273 | `_latest_mark_hash`:倒序扫 messages 找最新同路径 mark |
| 277–307 | `_attach_read_mark` / `_extract_tool_message`:成功读 → 二次读盘盖戳;兼容 Command 包装 |

---

## 2. ToolProgressMiddleware:结果质量状态机(链第 12 位)

### 2.1 它解决什么问题:停滞 ≠ 死循环

一个工具反复返回 `no results`、每次抓回同一页内容、或连续 `rate_limited`,模型每次都
"有输出"——但**没有任何新信息**,token 和 API 调用继续烧。这就是**停滞(stagnation)**:
"有输出但没新信息"。

停滞与死循环是**两个不同层的故障**(§9.1):死循环是调用模式问题——模型不断**发出同一批
tool_calls**、不看结果,由链尾的 LoopDetectionMiddleware 硬停整轮;停滞是结果质量问题——
工具执行了、但**结果不再带来新信息**,由本文主角 ToolProgress 按工具细粒度处理(先 warn、
后封**这一个工具**,其余工具照常)。

ToolProgress 用 `(thread_id, tool_name)` 维度的状态机跟踪"连续无新信息"的次数,把工具的
结果分门别类(带 meta 的结构化信号,RFC #3177):该 warn 的先 warn 让模型换思路,该封的
硬封不执行。

### 2.2 钩子与执行时机

一个中间件挂了**三类钩子**,对应它的三个职责面:

```python
wrap_tool_call / awrap_tool_call   # ① 执行前:已 BLOCKED 则直接拦截,不跑工具
                                   # ② 执行后:_update_state_from_result 推进状态机
wrap_model_call / awrap_model_call # ③ 模型调用前:把排队的 hint 注入请求
before_agent / abefore_agent       # ④ 新 run 开始:清过期 pending + 重置线程状态
```

- 豁免工具(默认 `{ask_clarification, write_todos, present_files, task}`)直接透传——这些
  工具的"无新信息"不代表停滞(比如 `write_todos` 每轮更新同一份清单、`task` 返回子代理
  结果本就可能相似)。
- 读结果 meta:从 `ToolMessage.additional_kwargs["deerflow_tool_meta"]` 解析(兼容
  Command 包装,按 `tool_call_id` 定位)。**meta 缺失且非豁免工具 → 打 warning**——这句
  warning 是装配错位的探针("verify ToolProgressMiddleware is outer of
  ToolErrorHandlingMiddleware"):如果哪天有人把装配顺序调反,这里会立刻报警。

### 2.3 状态机:三类错误,三条路径

每 `(thread_id, tool_name)` 一份 `ToolPhaseState`:

```python
@dataclass(slots=True)
class ToolPhaseState:
    phase: Literal["active", "warned", "blocked"] = "active"
    consecutive_problems: int = 0
    block_reason: str | None = None
    recent_word_sets: tuple[frozenset[str], ...] = ()   # 最近 3 次成功结果的词集(见 2.4)
```

```
        ┌──────────────── 好结果:清零计数 + 词集入窗(尾3)────────────────┐
        ▼                                                              │
     ACTIVE ──连续 problem 达 stagnation_threshold(默认 3)──▶ WARNED ──再达 warn_escalation_count(默认 2,即第 5 次)──▶ BLOCKED
        ▲                                                              │
        └──────────────── 好结果(任何阶段都允许回到 ACTIVE)───────────────┘
```
特殊路径:
① 首现即封:`recoverable_by_model=False` 且 action=`stop`(auth/config/internal)第一次出现就
   phase=blocked——重试无意义,一次都别浪费。
② WARNED 终态:`recoverable_by_model=True`(no_results/not_found/permission/未知/Jaccard 重复
   success)达升级阈值后仍停 WARNED、每次再犯重注入 hint——不硬封,给模型换参重试的空间。
③ 升级硬封:`recoverable_by_model=False` 且 action≠stop(rate_limited/transient)第 5 次连续
   problem → phase=blocked。模型重试救不了,硬封省 API。

**计数纪律**:进入判定前先把 `consecutive_problems += 1`,再走所有分支——保证任何出口
计数一致,工具失败后计数永远不可能是 0。

**BLOCKED 是终态**:`_assess_and_transition` 开头有守卫——若一个 blocked 状态流进来(并发
竞态等极端情况),原样返回、不涨计数、不降级。正常流程中根本到不了这里,因为
`wrap_tool_call` 在**执行前**就查 `_get_block_reason`,已封工具的直接产 `[TOOL_BLOCKED]`
错误消息返回,handler(工具本体)根本不跑:

```python
# 已封工具的每一次再调用(不执行工具,零消耗):
ToolMessage(content=f"[TOOL_BLOCKED] {block_reason}", status="error",
            additional_kwargs={TOOL_META_KEY: {
                "status": "error",
                "error_type": "blocked_by_progress_guard",
                "recoverable_by_model": True,          # 模型仍可换思路
                "recommended_next_action": "summarize",
                "source": "progress_middleware",
            }})
```

`block_reason` 按 error_type 给人类可读文案(如 `rate_limited` →
"Repeated rate-limiting — summarize current findings and proceed.")。

**三类错误的分类映射**(分类在 meta 生产端 `tool_result_meta.py` 的关键词规则表完成):

| meta.error_type | 触发证据(关键词/内容) | recoverable | action | TP 走向 |
|---|---|---|---|---|
| `no_results` | "no results found" 等 | ✅ True | rewrite_query | WARNED(终态),重复注入 hint |
| `not_found` | 404 / "no such file" / "does not exist" | ✅ True | rewrite_query | WARNED(终态) |
| `permission` | permission/access denied、路径穿越 | ✅ True | try_alternative | WARNED(终态) |
| `unknown` | 其它无法归类的错误 | ✅ True | try_alternative | WARNED(终态) |
| `success` + Jaccard 重复 | 词集相似度 ≥ 0.8(见 2.4) | ✅ True | (continue) | WARNED(终态),提示换词 |
| `partial_success` | "partial results"/"truncated" 等标记 | ✅ True | rewrite_query | 一律算 problem(不跑 Jaccard) |
| `rate_limited` | "rate limit" | ❌ False | summarize | WARNED → 第 5 次 BLOCKED |
| `transient` | timeout/connection/network/temporarily unavailable | ❌ False | try_alternative | WARNED → 第 5 次 BLOCKED |
| `auth` / `config` / `internal` | 401/unauthorized;not configured;500/internal error | ❌ False | **stop** | **首现即 BLOCKED** |

两个值得注意的"隐藏分类":web_fetch 抓回的 HTTP 错误页(如 `# 404 Not Found` 标题)在
传输层是成功的,但被"错误页外壳"分析归类为错误(issue #4273);工具自带 `Error:` 前缀文本
与结构化 `subagent_status=failed` 结果也各有专门通道——总之**分类不看 status 一个字段**。

**好结果的定义**:`status == success` **且** Jaccard 不比中最近 3 个词集 → 清零计数、回
active、新词集入窗。error/partial_success 天然是 problem,连 Jaccard 都不用算(省一次
O(n) 正则)。

### 2.4 Jaccard 判重:怎么知道"又是同样的内容"

`word_set`:内容截断到 `_MAX_CONTENT_FOR_WORDSET = 8192` 字符,小写,取长度 ≥ 3 的单词
(`\b\w{3,}\b`)。截断是因为对超大结果(网页)做全量正则代价高——判重是启发式,不是保证,
丢尾部可接受。

```python
def is_near_duplicate(current, recent, threshold=0.8, min_words=10) -> bool:
    if len(current) < min_words: return False            # 太短的集合不参与(噪声大)
    for prev in recent[-3:]:                             # 只比最近 3 次
        if len(prev) < min_words: continue
        union = len(current | prev)
        if union == 0: continue                          # 双空集不判重
        if len(current & prev) / union >= threshold: return True
    return False
```

- 窗口只保留**最近 3 个成功词集**——内容在演化(哪怕慢),新旧混比只会误伤。
- `min_words=10` 双端门槛:内容太短(如一行 "OK")相似度毫无意义,直接放行。

### 2.5 WARNED 提示的重注入:pending 队列 + wrap_model_call

**hint 长什么样**(`_format_hint`):`[PROGRESS HINT]` 基础句 + 按
`recommended_next_action` 的尾缀。例:`no_results` → "Your search returned no results.
Try rephrasing your search query with different keywords or approach.";
`success`+重复 → "The tool is returning duplicate results. Try rephrasing your query…"。

**为什么不能直接插进消息流**:在 tool_calls 与其响应之间插消息会破坏 pairing(OpenAI/
Moonshot 拒收、Anthropic 禁 mid-stream SystemMessage),插 AIMessage 会污染下游 Memory
消费者(§9.1 已解之坑)。所以 hint 先排进进程内 `_pending[(thread_id, run_id)]` 队列
(单 run 上限 `_MAX_PENDING_PER_RUN = 3`),到**下一次模型调用前**由 `wrap_model_call`
抽出:

```python
def _augment_request(request: ModelRequest) -> ModelRequest:
    hints = self._drain_pending(request.runtime)         # pop 队列
    if not hints: return request
    deduped = list(dict.fromkeys(hints))                 # 保序去重(多次同样 problem 只提示一次)
    return request.override(messages=[
        *request.messages,
        HumanMessage(content="\n\n".join(deduped), name="progress_hint"),
    ])
```

注入发生在**模型请求的最后一帧**(所有 ToolMessage 之后),且只改本次 request,**不写
checkpoint**——模型看到提示、历史状态不被污染。一次模型调用里多条 hint 合并成一条
`HumanMessage(name="progress_hint")`。`inject_assessment` 配置可关掉注入(状态机照常跟踪)。

WARNED 是"过渡"还是"终态"决定了 hint 的注入频率:

```python
if new_count >= stagnation_threshold + warn_escalation:   # ≥5
    if meta.recoverable_by_model:   → 停在 WARNED,重注入 hint(每次再犯都再提示)
    else:                           → BLOCKED + reason(硬封)
elif new_count >= stagnation_threshold:                   # ≥3
    → WARNED + hint(第一次提示)
else:
    → 只涨计数,不出声
```

**run 边界**:`before_agent` 做两件事——① 清掉本线程**其它 run_id** 的过期 pending(本轮
没来得及注入的 hint 不会漏进下一轮);② 把该线程**所有**工具状态无条件重置回 active
(清零计数/block_reason/词集)。理由写得很透:rate_limited/transient 是 **time-bound** 错误,
根因可能在新 run 已经消失,带着旧计数会误封本可成功的调用。这与 LoopDetection **刻意相反**
(见 §2.6)——两种守卫的作用域策略是它们分工的一部分,不是疏忽。

### 2.6 与 LoopDetectionMiddleware 的分工

| | ToolProgressMiddleware | LoopDetectionMiddleware |
|---|---|---|
| 触发时机 | **工具执行后**(看返回的 meta/内容) | **模型响应后、工具执行前**(看 AIMessage 的 tool_calls 签名) |
| 粒度 | 细:**封具体一个工具**,其它工具照常 | 粗:**剥掉全部 tool_calls,硬停整轮**(stop_reason=loop_capped) |
| 判据 | 结果质量:连续无新信息 | 调用模式:重复相同 tool_calls(哈希签名) |
| 跨 run | 每次 run 清空(结果质量是 time-bound 的) | 保留 `_history`(调用模式时间不变) |
| 产物 | `[TOOL_BLOCKED]` error ToolMessage | `[LOOP DETECTED]` hint → 硬停兜底 |

两者**互补不竞争**:可以同一次模型调用里各自注入一组 HumanMessage 提示,模型同时看到两
组信号自行权衡;互不读取对方的内部状态。关键的不变量:

- LoopDetection 硬停时**不再发出任何 wrap_tool_call**,ToolProgress 自然不触发——**没有
  双停**;
- ToolProgress 封了一个工具后,模型若再调它,得到的是即时 `[TOOL_BLOCKED]`(不执行);
  模型仍可自由调用其它工具,LoopDetection 照常跟踪整个调用模式。

### 2.7 邻居与链内互动

- **ToolErrorHandling(13) 在它内层**:异常被转成错误 ToolMessage 并 stamp meta(异常路径
  用 `stamp_exception_meta`,覆盖工具自带 stamp,因为"异常分类比工具自己的 stamp 更权威")
  后,ToolProgress 才能读到结构化的分类。这是它必须坐在 TEH 外层的唯一原因。
- **ReadBeforeWrite(11) 在它外层**:被 RBW 拦下的写直接短路,**根本到不了 ToolProgress 的
  wrap**——合理拒绝不占停滞槽,不会被累计成"工具老失败"。RBW 自 stamp 的
  `recoverable_by_model=True` meta 也保证即使进入分类路径也走 WARNED 而非硬封。
- **ToolReceipt(最外层)**:ToolProgress 自产的 `[TOOL_BLOCKED]` 消息带完整 meta,receipts
  记账不缺口。

### 2.8 并发与内存治理

- **一把 `threading.Lock` 而非 asyncio.Lock**:临界区全是毫秒级内存 dict 操作、无 IO,
  不阻塞事件循环;且 asyncio.Lock 管不到子代理执行器线程池走的 **sync `wrap_tool_call`**
  路径(那需要两把锁)。与 LoopDetection 同一模式。
- **LRU 状态表**:`_phase_states` 是 `OrderedDict`,默认最多跟踪 100 个线程
  (`max_tracked_threads`);写路径 `_get_state` 里 `move_to_end` 刷新热度,驱逐最老线程时
  **同步清掉它的 pending**——否则 pending 表会无限膨胀。
- **读路径不 bump 热度**:`_get_block_reason` 故意不 `move_to_end`——若已封的线程每次被
  查询都刷新热度,会永久占着 LRU 槽,健康线程进不来。
- **幽灵 pending 守卫**:`_queue_assessment` 先确认 thread 还在 `_phase_states` 里才入队,
  防止给刚被 LRU 逐出的线程留下永远无人清理的 pending。
- **`recent_word_sets` 用不可变 tuple[ frozenset ]**:problem 路径的 `replace()` 省略该字段
  时,不会让新旧状态对象共享同一个可变 list(否则 `.append()` 会静默串改两侧状态)。

### 2.9 源码阅读指引(`middlewares/tool_progress_middleware.py`,共 ~598 行)

| 行区 | 内容 |
|---|---|
| 1–47 | 模块 docstring:**架构图 + 状态机三分支 + 与 LoopDetection 分工**,必读 |
| 74–93 | 常量(`_MAX_PENDING_PER_RUN=3`、词集 8192 上限)与 `ToolPhaseState` |
| 100–127 | `word_set` / `is_near_duplicate`:Jaccard 判重 |
| 152–205 | `_parse_tool_meta`(schema 漂移容错)与 hint/block_reason 文案表 |
| 215–255 | `__init__` 参数默认值 + `from_config`;`threading.Lock` 的选择理由(234–238) |
| 276–300 | `_get_state`(LRU 写路径+驱逐)/ `_get_block_reason`(读路径不 bump) |
| 302–317 | `_make_blocked_message`:`[TOOL_BLOCKED]` 的 meta 结构 |
| 319–367 | `_update_state_from_result`:meta 缺失探针、状态推进、阶段切换日志 |
| 372–435 | `_assess_and_transition`:**状态机核心**,逐分支读 |
| 437–463 | pending 队列:`_queue_assessment` / `_drain_pending` / `_clear_stale_pending` |
| 465–499 | `_reset_run_states`:run 边界无条件重置 + **与 LoopDetection 作用域相反的完整论证** |
| 504–548 | `wrap_tool_call`:先拦 BLOCKED,再执行后更新 |
| 553–598 | `_augment_request`/`wrap_model_call`(注入 hint)与 `before_agent` |

辅助:meta 生产端在 `middlewares/tool_result_meta.py`(`ToolResultMeta` 34–40;
关键词规则表 43–82;`normalize_tool_message` 分类优先级 254–318);配置在
`config/tool_progress_config.py`、`config/read_before_write_config.py`。

---

## 3. 合起来看:一条"先正确、后有效"的防线

把两个中间件拼回几条典型轨迹,看它们如何接力又不越界:

```
① 模型想改文件 F(没读过当前版)
     RBW(11):拦,error ToolMessage + meta(unknown/可恢复)   【正确性门:版本不对】
     → 模型 read_file F → RBW 盖 mark → 再写 → RBW 放行 → TP(12) 正常计数
② 反复搜没结果的话题(每次都换了词但都 no_results)
     TP(12):第 3 次 WARNED + hint;第 5 次仍 recoverable → 停在 WARNED 重注入,不硬封
             —— 换词重试是合法的,封死会扼杀模型自救
③ 对同一 URL 连续抓取(内容一模一样)
     TP(12):success 但 Jaccard 判重 → WARNED"返回重复结果"
④ 反复调一个密钥失效的工具
     TP(12):auth/stop 首现即 BLOCKED,后续调用直接 [TOOL_BLOCKED],零执行
```

**一句话总结**:ReadBeforeWrite 保证"**要写的文件,模型真的看过当前版**"(正确性,宁可拦);
ToolProgress 保证"**工具还在产生新信息,才值得继续调它**"(有效性,先提醒后封禁)。
前者管文件和模型之间的"读写一致性",后者管 token 与结果之间的"投入产出比";它们和
LoopDetection(调用模式)、TokenBudget(预算)一起构成 §9.1 的四层终止性护栏,各守一层、
位置分明。
