# 12 · Sandbox 抽象与实现:代码执行的隔离边界与 Provider 生态

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写
> 代码引用根:`backend/packages/harness/deerflow/`(下文省去前缀);远程 Provider 在
> `community/{aio_sandbox,opensandbox,boxlite,tenki}/`。模块内权威说明先读
> `sandbox/AGENTS.md`(104 行,接口/Provider/虚拟路径/工具四段)与 harness 级
> `AGENTS.md` 的 "Sandbox System" 一节。

Sandbox 是 DeerFlow 里**唯一会真正执行任意代码**的子系统:模型产出的 bash 命令、
文件读写、结构化搜索最终都落在这里。本章讲清四件事——这个抽象为什么长这样(§1)、
统一接口长什么样(§2)、Provider 与工厂如何把"隔离"变成可插拔策略(§3)、
以及执行期状态机(中间件 + 租约)如何决定"谁拿沙箱、谁放沙箱"(§4)。
§5–§8 分别讲身份令牌、路径契约、密钥剥离与五个 Provider;§9 用一句话把
命令审计和路径纵深指到对应深文。

---

## 1. 设计目标:代码执行是核心能力,所以隔离是安全底线

DeerFlow 的定位不是"能聊天的 RAG",而是**超级 Agent**:模型可以在一个真实
shell 里装依赖、跑测试、起服务、改文件,再用读回的结果决定下一步。代码执行
不是旁路能力,而是产品核心——这带来一个残酷推论:**模型输出本身不可信**,
每一条命令都必须按"可能被注入、可能逃逸、可能删库"来对待。

三个设计目标:

1. **代码执行 = 核心能力,不能被阉割成"只读工具"**。bash / read_file / write_file /
   grep / glob 必须齐全,而且要能支撑真实工作流(装包、跑进程、看输出)。这决定了
   统一接口的形态:一个能执行命令、能读写任意文件系统路径的 `Sandbox`,而不是
   一组"安全白名单工具"。
2. **隔离是产品承诺,不是部署选项**。每个用户 × 每个 thread 的执行环境互不可见
   (per-thread 隔离);AIO 等远程 Provider 用独立容器/微 VM,连内核、网络、
   磁盘都分开。隔离强度因 Provider 而异——从 Local 的"无 OS 边界"到 BoxLite
   的独立内核——所以隔离能力必须**声明式暴露**(见 §3 的 provider 能力位),
   中间件据此 fail closed。
3. **宿主(Gateway)不裸奔**。即便用 LocalSandbox(直接在宿主上起 bash 子进程),
   也不能让平台密钥跟着环境变量流进子进程(§7 密钥剥离),不能让命令读写宿主
   任意路径(§6 路径映射 + 穿越检查),不能让它无限挂起(超时 + 进程组击杀),
   命令还要过分级审计(§9)。"沙箱弱"不等于"宿主裸奔",纵深每一层都独立生效。

一句话:统一接口让**上层的 Agent 逻辑与下层的隔离强度解耦**——同一套
工具、中间件、租约代码,换一个 Provider 配置就从"宿主 bash + 路径映射"
升级成"独立内核微 VM",模型看到的文件系统契约完全不变。

---

## 2. Sandbox 统一接口:一次定义,五个实现

抽象基类 `Sandbox`(`sandbox/sandbox.py`,ABC)定义全部能力。它有九个方法:

| 方法 | 签名要点 | 语义 |
|---|---|---|
| `execute_command` | `(command, env=None, timeout=None) -> str` | 在沙箱里执行 bash,返回标准/错误输出 |
| `execute_command_in_scope` | `(command, *, scope_id=None)` | 可选执行域内执行(见下) |
| `release_command_scope` | `(scope_id)` | 释放某个执行域的 shell 状态 |
| `read_file` | `(path, start_line=None, end_line=None) -> str` | 读文件(可限行区间,1 起) |
| `write_file` | `(path, content, append=False)` | 写/追加文本 |
| `download_file` | `(path) -> bytes` | 取二进制字节 |
| `update_file` | `(path, content: bytes)` | 用二进制覆盖 |
| `list_dir` | `(path, max_depth=2) -> list[str]` | 列目录(树状,默认 2 层) |
| `glob` | `(path, pattern, *, include_dirs=False, max_results=200) -> (list[str], bool)` | 按 glob 找路径,返回"是否截断" |
| `grep` | `(path, pattern, *, glob=None, literal=False, case_sensitive=False, max_results=100) -> (list[GrepMatch], bool)` | 单文件或目录树搜索,同样有界 |

关键设计点:

- **`env` 是请求级密钥通道**。`execute_command` 的 `env` 参数注入**本次调用**
  的临时环境变量(如 skill 脚本要用的短期端用户 token、`git push` 的 GitHub App
  installation token),不写进 prompt、不进 tool 参数、不进命令字符串。抽象层
  用 `_validate_extra_env` 强制 key 必须是合法 POSIX 变量名
  (`^[A-Za-z_][A-Za-z0-9_]*$`),这是纵深防御:今天没有任何实现把 key 拼进
  shell 字符串(Local 走 `subprocess.run(env=...)`、AIO 走 `bash.exec` 的结构化
  `env` 字段),但未来若出现 shell-splicing 实现,非法 key 会先在这里 `ValueError`
  拦住,而不是变成注入。
- **`persistent_shell_sessions` 三态声明,默认 fail closed**。`None`(默认)表示
  实现**没有声明**自己的会话语义——自定义 Provider 靠类路径加载,可能复用持久
  shell,所以"沉默"必须按"可能有脏状态"处理;只有显式 `False` 才可信为
  "每次都是干净 shell"。所有官方实现都显式声明:AIO 是 `True`(一条持久会话
  链,export/cwd/函数跨调用存活),Local/OpenSandbox/BoxLite/Tenki 都是 `False`
  (每次 fresh exec)。消费方(如验收清单的 `tests_passed` 匹配器)对 `True` 与
  `None` 一视同仁,把录得的 bash 证据降级为 UNVERIFIED——命令文本证明不了
  环境干净。
- **scope 钩子是"加法",不破坏第三方子类**。`execute_command_in_scope` /
  `release_command_scope` 是基类上的**默认透传实现**:没有服务端 shell 会话的
  Provider(以及所有自定义子类)直接继承 `execute_command`,不需要实现任何新东西;
  会话感知的 Provider(AIO)用 `scope_id` 给**并发执行的 agent** 各发一条有序的
  服务端 shell 会话,同一 scope 内串行、不同 scope 互不污染 shell 状态
  (见 §4/§8)。
- **异常契约在 docstring 里钉死**。`download_file` 明确:路径穿越或落在虚拟前缀
  之外要抛 `PermissionError`;文件不存在/不可读要抛 `OSError`——**本地和远程
  实现必须统一**,调用方只处理一个异常类型。`read_file` 等对越界路径同理
  (§6 展开)。

### 2.1 这个接口"刻意缺什么"

没有 `mkdir`、`rm`、`mv` 之类的原语——因为 bash 全都能做;没有"安全的写文件
门"——那是中间件层(ReadBeforeWrite)与审计层的职责,不属于传输层。接口只回答
"对某个路径能做什么",不回答"该不该做"。这是分层原则:sandbox 模块管
**传输与隔离语义**,策略(授权、审计、读前写)全部在它外面的中间件与 authz 层。

---

## 3. Provider 模式与工厂:隔离是插拔策略

`Sandbox` 是"一个活环境",`SandboxProvider`(`sandbox/sandbox_provider.py`)
是"环境的生命周期工厂"。两者是经典 Provider 模式:上层只依赖抽象,真正执行
隔离的类由配置决定。

### 3.1 生命周期:`acquire → get → release`

```python
class SandboxProvider(ABC):
    uses_thread_data_mounts: bool = False
    needs_upload_permission_adjustment: bool = True
    supports_agent_skill_isolation: bool = False   # 能力位,见下

    def acquire(self, thread_id=None, *, user_id=None) -> str      # 拿环境,返回 id
    async def acquire_async(self, thread_id=None, *, user_id=None) -> str
    def get(self, sandbox_id: str) -> Sandbox | None               # 按 id 取活实例
    def release(self, sandbox_id: str) -> None                     # 释放/销毁
    def reset(self) -> None                                        # 清缓存状态
    def sync_agent_skills(self, sandbox_id, *, thread_id, user_id, projection)  # 默认 no-op
```

- **异步路径**:`acquire_async` 基类实现是 `asyncio.to_thread(self.acquire, ...)`。
  Docker 创建、容器发现、跨进程锁、就绪轮询、释放全是阻塞 IO,必须滚出事件循环;
  AIO/E2B 等重 Provider 还覆盖成带序列化的版本(§5)。
- **能力位声明**:`supports_agent_skill_isolation=True` 表示该 Provider 能在整个
  Agent 工具面强制 lead Agent 的显式技能策略。bind-mount 型 Provider(目录挂载
  即生效)继承 no-op 的 `sync_agent_skills`;上传型 Provider(E2B、部分远端)
  覆盖它把"线程技能投影"上传进沙箱。宿主型 Provider 只要启用的 shell 能绕过
  路径映射就必须报 `False`——**显式策略 + 不支持隔离的 Provider 组合,中间件在
  acquire 之前就 fail closed 抛 `SandboxRuntimeError`**,绝不让策略静默失效。
- **release 不一定是销毁**:远程 Provider 把"释放"解释成"**进 warm pool 待命**"
  (§8),下次同一用户/线程 acquire 直接复用,省掉冷启动。

### 3.2 工厂:动态类解析 + 双检锁单例

Provider 由配置决定:`config.sandbox.use` 是一个**类路径字符串**(如
`deerflow.community.aio_sandbox:AioSandboxProvider`),模块级工厂
`get_sandbox_provider()` 负责把它变成进程内单例:

1. **快路径**:`with _provider_lock:` 读一次全局 `_default_sandbox_provider`,
   非空即返回——单次加锁读保证并发 reset/shutdown 不会在检查与返回之间把全局
   置空,让调用者拿到 `None` 或撕裂实例。
2. **冷启动**:在锁**外**执行 `resolve_class(config.sandbox.use, SandboxProvider)`
   动态 import + `cls(**kwargs)` 构造。锁外执行是刻意的:`resolve_class` 与构造
   器都是插件代码,可能很慢、甚至可能重入这些生命周期函数——在不可重入的
   `threading.Lock` 里跑它们会自死锁,还会让并发的 `get()` 在慢 teardown 期间
   全部阻塞。
3. **回锁裁决安装竞赛**:重新加锁,若全局仍为 `None` 则安装自己并返回;若已被
   别的线程抢先(winner 在同锁下读取,必然活),则在锁外**拆除刚建的孤儿**——
   带副作用构造器的 Provider(如 AIO 会起 idle-checker 线程)调用其 `shutdown()`
   防泄漏(#3721),然后返回 winner。

配套的四个管理函数:**`reset_sandbox_provider()`**(换配置/测试用;锁内摘引用,
锁外跑 provider 的 `reset()` 回调并 `discard_sandbox_lease_manager(provider)`)、
**`shutdown_sandbox_provider()`**(应用退出;锁内摘引用、锁外跑 `shutdown()`,
完整释放所有沙箱)、**`set_sandbox_provider()`**(注入 mock/自定义实例;被替换的
旧实例不 shutdown,归属调用者)、以及 provider 自身的 `reset()` 钩子(Local 用它
清模块级 `_singleton` 与 LRU,否则换挂载配置不生效)。

> 锁纪律的精髓:**锁只护引用交换,回调一律在锁外**。注释里原话——
> "provider callbacks ... may be slow or, worse, re-enter these lifecycle functions"。

---

## 4. SandboxMiddleware:谁拿沙箱、谁放沙箱

拿到/放掉一个沙箱不是"调用一次 acquire/release"这么简单——同一时刻可能有一个
lead、多个并发 subagent、一个 Gateway 请求、一次渠道上传在共用**同一个**线程
沙箱(它们共享 thread_id)。谁负责最终 release、谁只是借用、fork 恢复的 checkpoint
该怎么处理,全由 `SandboxMiddleware`(`sandbox/middleware.py`,挂在 shared runtime
base 的 thread_hooks 段)+ `SandboxLeaseManager`(`sandbox/lease.py`)协调。

### 4.1 获取(before_agent)

中间件在 `before_agent`/`abefore_agent`(每 run 一次)按这条链决策:

1. 从 `runtime.context` 取 `thread_id`;取不到(无上下文的直接调用)就退回基类
   行为。
2. `resolve_runtime_user_id(runtime)` 解析用户;`ensure_sandbox_lease_owner
   (runtime.context)` 为本次执行**铸造一个临时 owner id**(形如
   `agent:{uuid4}`,写入 context;已有则复用)。
3. 技能投影:若本中间件 `owns_agent_skill_projection`(lead 才 True;**subagent
   构造时显式传 False**,因为 subagent 共享 lead 的线程沙箱与文件系统视图,
   重建投影会拓宽/收窄所有并发 agent 共享的目录)且有显式技能集,则准备线程
   级投影。
4. **lazy 与 eager 的分叉**:默认 `lazy_init=True` 且无策略投影时,直接跳过——
   沙箱到**第一个真正碰沙箱的工具调用**才经 `ensure_sandbox_initialized*`
   (tools.py)懒获取。只有两种情形 eager:策略投影存在(必须抢在旧共享视图
   checkpoint 复活前换沙箱),或 `lazy_init=False`。懒路径必须这样设计:只回答
   问题、返回 terminal Command 的 run,不该在 after_agent 被图旁路时留下一个
   没用的 owner。
5. **授权门(eager 版)**:`authorize_sandbox_execution*`(authz 层,sandbox:execute,
   RFC #4063 Phase 3)先跑。拒绝时共享视图 run 跳过 eager、把拒绝延迟到第一个
   沙箱工具(与懒路径的 `SandboxAuthorizationError`→友好 `ToolMessage`
   "sandbox execution is not permitted for your role" 语义一致);**策略投影 run
   直接 raise**——旧的共享沙箱绝不能被下游工具复用。
6. 经 `SandboxLeaseManager.acquire(owner_id, thread_id, user_id=...)` 拿沙箱
   (带 owner 绑定),写 `runtime.context["sandbox_id"]`,并把 id 通过 state 更新
   落进 `sandbox.sandbox_id` 通道。同步失败时按 owner 有无回滚 release。
7. **已有沙箱的复用路径**:state 里已有 `sandbox_id` 且不是 fork 恢复的,走
   `retain` —— 用本次 owner 对这个已存在沙箱做**幂等保留绑定**,不重新 acquire,
   省掉冷启动也避免换容器。

### 4.2 发布(after_agent)与 lease owner 语义

`after_agent`/`aafter_agent`(每 run 一次,图出口)是发布点,但发布**不等于**
调 `provider.release`:

- 读 state 里的沙箱;`unwrap_sandbox()`(`sandbox/overwrite.py`)负责剥掉
  `langgraph.types.Overwrite` 包装——fork 恢复的 checkpoint 可能把 sandbox 通道
  以 replace 语义的 `Overwrite` 形态重放,直接 `sandbox["sandbox_id"]` 会崩。
- **`fork_restored=True` 时绝不释放**:包装值重放的是**父线程**的沙箱状态,
  在此释放会把父线程的 warm 沙箱逐出(日志原话:"Not releasing fork-restored
  sandbox")。`_retain_existing_sandbox` 同理:fork 恢复的沙箱不做 retain 绑定。
- 正常路径:读 context 里的 owner id,有则 `SandboxLeaseManager.release(owner_id)`,
  无(直接工具调用者)才裸调 `provider.release(sandbox_id)`。

### 4.3 SandboxLeaseManager:一个客户端,多个 holder

进程内租约表(`sandbox/lease.py`)解决"并发 lead/subagent/请求/上传共用同一
provider 客户端"时的归属问题。结构:`_bindings_by_owner`(owner_id →
`_LeaseBinding{sandbox_id, thread_key, release_on_last}`)、
`_owners_by_sandbox`(sandbox_id → owner 集合)、`_release_pending_by_sandbox`。
按 `(user_id, thread_id)` 键用 `AcquireSerializer` 串行化生命周期迁移;元数据锁
与 provider IO 分离(慢 IO 不占锁)。关键语义:

- **普通 owner(lead/独立 subagent 执行)= `release_on_last=True`**。它走完整个
  graph/tool/请求批次后 release,若自己是最后一个 holder,就执行挂起的
  `provider.release`——对远程 Provider 即"进 warm pool 待命"。
- **借用 holder(fork 恢复的 child、上传同步)= `release_on_last=False`**。它们
  只为本次操作"围住"客户端(防止它被中途 park/回收)并负责自己 scope 的清理,
  但**不请求 park**;此前普通 owner 的 release 请求会挂在
  `_release_pending_by_sandbox` 上等所有借用者离开。缺失的 fork 客户端由新普通
  owner 替换。
- **owner 单调性**:owner 的绑定在 thread 身份间不可迁移(换 (user_id, thread_id)
  抛 RuntimeError);借用者可升级为普通 owner(之后真 acquire 时把
  `release_on_last` 翻 True),普通 owner 不会降级。
- **幂等与取消安全**:重复 release 无害;acquire 被取消时"排水"底层
  `to_thread` 任务并在持锁窗口内回滚未绑定的 acquire,失败只记日志、不吞原始
  取消——取消永远无法打断 acquire/rollback/release 对账,也无法让沙箱 IO 活得
  比它的 holder 久。
- **外层围栏重复幂等 release**:完整批次排空后,`release_sandbox_execution_lease*`
  在 run 的外层生命周期围栏再放一次(防 after_agent 被绕过);**per-tool 的
  terminal `Command` 包装绝不 release**,因为同一步的兄弟处理器可能还在跑。
- **server-owned 身份**:lease/scope 上下文键由 Gateway/worker 清洗调用者值,
  只有内部 subagent 路径能赋 task id——调用方无法伪造 owner 蹭别人的沙箱。
- 管理器按 **provider 对象身份**(非 hash/等值)注册,自定义不可哈希 Provider
  依然有效。

### 4.4 工具调用包装:懒获取的 id 要能持久化

tools.py 的 `ensure_sandbox_initialized*` 直接改 `runtime.state["sandbox"]`,
但该突变只活在当前工具调用内,LangGraph 的 channel reducer 看不见——后续步骤
(如 ToolOutputBudgetMiddleware、subagent 的 task_tool)就拿不到 id。因此
`wrap_tool_call` 在 handler 前后快照 state 对比 sandbox_id,发现新 id 就把
`ToolMessage` 包成 `Command(update={"sandbox": {...}, "messages": [msg]})`(或并进
已有 dict update,保留 goto/graph/resume 等字段),让沙箱 id 正式落图。

---

## 5. 隔离令牌与身份:远程资源的确定性寻址

远程沙箱(容器/VM)是**跨进程、跨实例**的资源,必须能按 (user_id, thread_id)
确定性寻址,否则每次 Gateway 重启、每次并发 acquire 都会造出新容器、旧容器成
孤儿。`sandbox/identity.py` 给出全仓共享的派生式:

```python
def derive_sandbox_scope_token(*, user_id: str, thread_id: str) -> str:
    # sha256(f"{user_id}:{thread_id}").hexdigest()[:16]  —— 16 位小写 hex
```

**兼容性契约**:这是持久身份边界——AIO、E2B、BoxLite、Tenki、OpenSandbox 都靠
这个 token 定位已有容器/VM。改任何属性(分隔符、编码、摘要、大小写、截断长度)
都是破坏性迁移:存量远程资源会全部找不到、冷启动。签名故意 keyword-only(旧
helper 是 `(thread_id, user_id)` 位置参数,反序,两个 str 静默互换太容易)。
`is_sandbox_scope_token` 只验形状(截断哈希不可逆)。

配套的**获取序列化器** `AcquireSerializer`(`sandbox/acquire_serialization.py`,
RFC #4741)把"同一键的并发 acquire/release 迁移"变成临界区:引用计数 + 有界增长
的 per-key `threading.Lock` 表、专用有界 executor(异步等待绝不碰事件循环或默认
executor)、由 worker 拥有的取消清理(不依赖被取消的事件循环任务恢复执行)、
`close()` 幂等(Provider `shutdown()`/`reset()` 调用)。键的粒度因 Provider 而异:
AIO 按 `(user_id, thread_id)`,E2B 按 `(user_id, thread_id, skills_root)`,
BoxLite/Tenki/OpenSandbox 按派生 id;`thread_id=None` 的获取(随机 UUID)绕过
序列化器。

身份之外还有**命令作用域身份** `sandbox_command_scope`:subagent owner 同时充当
`scope_id`(context 键由服务端写),AIO 给每个 scope 一条有序持久 shell 会话、
`ErrorObservation` 后轮换会话、lease 释放时清理会话;注册表身份在每次 scope
锁等待后**重新校验**,防止排队的命令复活已释放的会话;带 env 的命令一律走全新
`bash.exec` 会话,密钥不跨调用残留。

---

## 6. 路径契约:一个虚拟前缀,两种落地方式

模型与工具层**只见一套虚拟路径**,Provider 负责翻译。这是整个文件系统安全的
核心契约。

### 6.1 虚拟前缀与物理布局

```text
Agent 看到:  /mnt/user-data/{workspace, uploads, outputs}   ← VIRTUAL_PATH_PREFIX = "/mnt/user-data"
              /mnt/acp-workspace                              (ACP 工作区)
              /mnt/skills                                     (显式策略下的技能视图,§6.3)

物理落点:    backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/{...}
技能原始树:  deer-flow/skills/ + 受管集成存储(全局);投影视图在 .deer-flow/skills_view/ 下
```

- **AIO**:这些目录**以相同的虚拟路径 volume-mount 进容器**,容器原生接受
  `/mnt/user-data/...`,零翻译。
- **Local**:宿主无容器,`LocalSandboxProvider` 在 acquire 时按 thread 构建
  `PathMapping` 表,把 `/mnt/user-data/{workspace,uploads,outputs}` 与
  `/mnt/acp-workspace` 指到该线程宿主目录,于是公开 `Sandbox` API 与 AIO **统一
  遵守同一个 `/mnt/user-data` 契约**。线程级沙箱 id 形如
  `local:{user_id}:{thread_id}`(`acquire(None)` 退化为固定 id `"local"` 的通用
  单例,给无 thread 上下文的旧调用/脚本);线程沙箱按 LRU 缓存(默认 256,`threading.Lock`
  守护),`get()` 命中会 `move_to_end` 提升活跃线程,防负载下被逐出。

### 6.2 翻译与穿越检查

LocalSandbox 每次 IO 走 `_resolve_path_with_mapping`:先 `_find_path_mapping`
找最长匹配映射,算出相对路径后 `realpath` 拼宿主根,再用
`commonpath([local_root, resolved]) == local_root` 做**包含性检查**;逃逸即
```python
raise PermissionError(errno.EACCES, "Access denied: path escapes mounted directory", path_str)
```
(`download_file` 的 docstring 还额外要求:路径不落在 `VIRTUAL_PATH_PREFIX` 下
同样 `PermissionError`)。映射可带 `read_only`,只读挂载上的写会被拒。对称地,
**输出方向**有反向翻译:命令输出里的宿主真实路径被 `_reverse_resolve_paths_in_output`
替换回虚拟路径(不能把 `ubuntu` 之类的宿主用户名漏给模型);`execute_command`
先把命令串里的虚拟路径经 `_command_pattern` 替换成宿主路径再执行(带 env 白名单
保护,避免误伤字面量)。tools.py 层的 `replace_virtual_path*` 是**纵深防御第二层**
(兼路径校验)——Local 的 provider 挂载表才是 acquire 时身份与可见性的唯一权威,
工具层解析只覆盖 `/mnt/user-data/...`,skills/ACP/自定义挂载在工具层保持虚拟。

> Local 的路径映射**不是安全边界**:启用的宿主 bash 子进程完全可以绕过
> `PathMapping` 用规范路径直读宿主文件。所以 Local 的
> `supports_agent_skill_isolation` 是**动态属性**(宿主 bash 启用即 False),
> 显式 Agent 技能策略在宿主 bash 开启时 fail closed。

### 6.3 skills mount:共享视图 vs 线程投影

技能文件系统的三种形态(细节在技能/上下文章节,这里只讲挂载语义):

- **无显式策略(共享视图)**:沙箱暴露分类挂载——public/custom/legacy/
  integrations 各自指向 .deer-flow 下的技能视图目录(零拷贝共享)。
- **lead Agent 显式策略(线程投影)**:中间件 eager 构建
  `threads/{thread_id}/skills_view/` 下的**相干线程视图**,Local 用**一个
  `/mnt/skills` 根映射**替换四个分类映射,结构化文件工具只过一个受管边界;
  AIO 用四个线程投影分类挂载 + **不同的确定性沙箱身份**,防止复用旧的共享挂载
  容器;上传型 Provider(E2B 等)在 acquire 后 `sync_agent_skills` 严格清空受管
  目录再上传签名投影。
- subagent **不是投影 owner**:复用 lead 的线程文件系统视图,绝不用自己的
  发现/激活策略重建它(§4.1 的 `owns_agent_skill_projection=False`)。

---

## 7. 密钥剥离:宿主的秘密不进沙箱

skill 脚本是沙箱子进程。若子进程默认继承 Gateway 整个 `os.environ`,那平台凭据
(OPENAI_API_KEY、tracing 密钥、各社区 Provider key……)全在脚本面前——§2 的
"请求级密钥注入"就毫无意义:脚本直接读继承变量即可。`sandbox/env_policy.py`
因此让 `execute_command` **不再继承完整环境**:

- `build_sandbox_env()` 先按**大写化后的变量名**做大小写不敏感通配剥离:
  `*KEY*` / `*SECRET*` / `*TOKEN*` / `*PASS*` / `*CREDENTIAL*` / `*DSN*`
  (模式集对齐 codex 的默认排除,但 codex 默认关闭、DeerFlow 默认开启——
  安全优先)。`*PASS*` 有意覆盖 `PASSWORD`/`DB_PASS`/`SMTP_PASS`/`MYSQL_PASS`
  乃至 `*_ASKPASS` 凭据助手(指向会交出凭据的程序,同属泄漏类)。
- 再补一张"名字里没有 KEY/SECRET/TOKEN 但常规嵌密码"的精确名单
  (`MYSQL_PWD`、`REDISCLI_AUTH`、`REDIS_AUTH`、`PGSERVICEFILE`……);`*URL*`
  整体拦截被有意避免,会误伤技能合法要读的服务 URL。
- 良性变量(`PATH`、`HOME`、`LANG`、`VIRTUAL_ENV`、`PYTHONPATH`……)原样保留。
- 剥离之后,**请求级密钥(env=)再叠上去**——注入胜出,显式声明的需要(required
  secrets)才能拿到;脚本真需要被剥掉的某个名字,就通过 required-secrets 声明,
  由调用方在 `context.secrets` 提供。

Local 把它并进宿主子进程环境;AIO 用全新 `bash.exec(env=...)` 会话,保证密钥
不留在持久会话里。

---

## 8. 各 Provider 一览

五个官方实现,隔离强度从"零 OS 边界"到"独立内核"。选择即取舍:Local 零冷启动
但无隔离;AIO 默认 Docker 平衡;BoxLite/Tenki 微 VM 更强隔离;OpenSandbox 托管
云盒。共享的 warm-pool 生命周期(`community/warm_pool_lifecycle.py` 的
`WarmPoolLifecycleMixin`)统一了远程 Provider 的"释放进池、按需复用":公共默认
`DEFAULT_IDLE_TIMEOUT=600`、`IDLE_CHECK_INTERVAL=60`、`DEFAULT_REPLICAS=3`,
mixin 拥有 idle-checker 线程、warm 过期、最老淘汰、副本计数与软上限日志;
Provider 自留 active 注册表、创建/发现、健康检查与销毁钩子 `_destroy_warm_entry`。
配置位:`sandbox.use`(类路径)、`sandbox.replicas`、`sandbox.idle_timeout`。

### 8.1 LocalSandboxProvider — 宿主直跑,零隔离边界

`sandbox/local/local_sandbox_provider.py` + `local_sandbox.py`。**隔离原理**:无
容器、无 VM——直接在当前主机起 bash 子进程。安全靠三层替代:**路径映射 + 穿越
检查**(§6.2,逃逸即 PermissionError)、**环境剥离**(§7)、**命令级围栏**:
命令跑在独立进程组,wall-clock 超时(`sandbox.bash_command_timeout`,默认 600s),
超时杀整个 POSIX 进程组/Windows 进程树并提示模型把长进程放后台;POSIX 下
`server &` 立即返回,未被重定向的后台输出被排空但不落匿名临时文件;stdin 给
/dev/null(读 stdin 立即 EOF);`persistent_shell_sessions = False`(每次 fresh
子进程)。**关键坑**:它不是文件系统安全边界(§6.2 的宿主 bash 旁路),显式技能
策略 + 宿主 bash 会 fail closed;线程沙箱 LRU 256,配置/挂载变更后必须
`reset_sandbox_provider()`(release 是 no-op,缓存靠 acquire 的 LRU 淘汰与 reset
清理,`_agent_written_paths` 要在 eviction 间存活以支持反向解析)。

### 8.2 AioSandboxProvider — Docker 默认主力(community/aio_sandbox/)

**隔离原理**:连接运行中的 agent-infra "all-in-one-sandbox" Docker 容器,走 HTTP
API(`agent_sandbox` SDK);用户/线程目录 volume-mount 进同一虚拟路径。沙箱 id =
`derive_sandbox_scope_token`;**跨实例容器所有权**经可插拔 lease store
(`sandbox.ownership.type` = memory|redis,多实例自动推断 redis)——lease 回答
"**谁可以回收这个容器**"而非"谁能用",`own:`/`del:` 双状态让销毁窗口安全(#4206
教训:无状态 take 会让并发 Gateway 把彼此正在用的容器停掉)。**关键坑**:持久
shell 会话(`persistent_shell_sessions=True`)导致 export/cwd 跨命令存活,证据
不可信;并发 subagent 必须走 scope 会话否则互相污染 shell 状态(#1433/#5128)——
每个 scope 一条有序会话、ErrorObservation 后轮换、lease 释放即清理;带 env 的
命令需要 `bash.exec` API,镜像 < 1.9.x 会 404——此时 fail fast 提示升级镜像
(≥1.9.3,建议 1.11.0),而不是让模型空重试(#3921);`get()` 只能做内存查询
(碰 ownership store 就是事件循环上的阻塞 IO);backend 健康检查失败按 unknown
处理(不误杀),本地发现不了的可疑容器不收养、落回创建。

### 8.3 OpenSandboxProvider — 托管云盒(community/opensandbox/)

**隔离原理**:把 DeerFlow 沙箱跑在 OpenSandbox(opensandbox-group 的托管平台)上,
经同步 SDK `opensandbox.sync.SandboxSync` 直连;**每次调用都是全新 `run_command`
执行**,`persistent_shell_sessions=False`,无 shell 状态残留——隔离在远端平台,
本地零虚拟化成本。实现:`OpenSandboxProvider(WarmPoolLifecycleMixin, SandboxProvider)`
带 `_warm_pool` 时间戳字典与 `_sandboxes` active 注册表;id 走派生 token;默认
`default_command_timeout=600`,构造期校验 timeout 必须为正。**关键坑**:远端盒子
会话可能随时死亡——沙箱适配器带 `on_terminal_failure(sandbox_id, reason)`
回调,SDK 报"会话没了"时经 Provider 逐出该 warm/active 条目,下次 acquire 重新
创建;release 不是销毁而是按 idle 进 warm pool(§3.1),`replicas`/`idle_timeout`
由共享 mixin 执行。资源在云端,延迟与配额受平台约束,断网/平台抖动表现为
terminal failure。

### 8.4 BoxliteProvider — 本地微 VM,独立内核(community/boxlite/)

**隔离原理**:每个沙箱 = 一个 BoxLite 微 VM——无守护进程的 OCI 原生 VM、自带
内核(libkrun/KVM on Linux、Hypervisor.framework on macOS)。动机直指 AIO
Docker 的资源/冷启动痛点(#3439/#3213)。`deerflow-harness[boxlite]` 可选依赖,
仅在选中此 Provider 时懒加载。**关键坑**:BoxLite handle **loop-affine**——Provider
在守护线程上持有一条**私有 asyncio 事件循环**,同步 `Sandbox` 调用经
`run_coroutine_threadsafe` 桥上去;带 timeout 的健康检查把超时同时穿透 BoxLite
`exec(timeout=...)` 与私有循环的 `.result(timeout)`,挂死的 VM 无法无限占住
per-thread acquire 锁。每次都是 fresh `sh -lc`(`persistent_shell_sessions=False`);
盒名由 (user_id, thread_id) 确定性派生、进 warm pool 后**只允许同一 user/thread
回收**;`TERMINAL_ERROR_MARKERS`("vsock"/"disconnected"/"no such box"/"box has
been stopped"……)识别死盒并逐出;`replicas` 上限超了只逐 warm VM 不碰 active;
`reset()` 是有意轻量的注册表清空(不关盒、不停 idle reaper、不关私有循环),
完整 teardown 走 `shutdown()`。

### 8.5 TenkiSandboxProvider — 云微 VM,免运维(community/tenki/)

**隔离原理**:每个沙箱 = Tenki(tenki.cloud)云微 VM,从库存基础镜像创建,
**无守护进程、无本地虚拟化**——云托管替代 AIO(容器)与 BoxLite(本地虚拟化)。
SDK 同步(`tenki_sandbox`,可选 extra),适配器直接调、无需事件循环桥。
`deerflow-harness[tenki]` 懒加载。**关键坑**:文件传输走 Tenki 原生
`sandbox.fs` API(`read_text`/`read_stream`/`write_stream`/`mkdir`/`stat`)——
二进制安全、流式,**无 base64/shell 中转**;只有目录/内容**搜索**(list_dir/glob/
grep)才 shell 到 busybox 可移植 `find`/`grep`,用共享的 `deerflow.sandbox.search`
解析(与 E2B 同源)。沙箱以**非特权 `tenki` 用户**运行,DeerFlow 的 `/mnt/user-data`
前缀在 bootstrap 时重映射到可写 HOME 下(`_resolve_path`)并尽力 `sudo` 符号链接;
盒名 = `sha256(user_id:thread_id)[:16]`(64 位,与 E2B 一致),warm pool 只按此 id
键控(无全种子回退)。终端会话错误(具名 SDK 错误 + `ConnectionError`/
`BrokenPipeError`/`EOFError`)经 `_invalidate_sandbox` 逐出死微 VM;
**跨进程孤儿对账是 follow-up**——今天只有单进程 warm pool。

---

## 9. 命令审计与路径纵深:一句话指路

bash 命令的分级审计(block/warn/pass)、每次调用的结构化审计落盘、与
ToolReceipt 的先后关系,以及错误 ToolMessage 的恢复语义,属于
`SandboxAuditMiddleware`——详见 `docs-local/middleware/middleware-03-error-handling.md`
(命令分级与审计在 §"SandboxAuditMiddleware")与 harness `AGENTS.md` 的
"Middleware Chain";路径穿越、读前写、宿主路径漏出等文件纵深见
`middleware-04-file-safety.md` 与 `sandbox/tools.py` 的
`replace_virtual_path*` 第二层防线——本章只到"路径契约与隔离边界"为止,
文件工具内部的纵深归文件安全章。

---

## 附:本章速查

- 接口 `Sandbox`(sandbox/sandbox.py):9 个方法 + scope 透传钩子 +
  `persistent_shell_sessions` 三态声明;env 键过 POSIX 校验。
- 工厂 `get_sandbox_provider()`:双检锁单例,动态类解析与构造在锁外,
  孤儿实例 shutdown 回收;reset/shutdown 摘引用在锁内、回调在锁外。
- 中间件 `SandboxMiddleware`:lazy 默认;eager 只发生在策略投影或显式配置;
  authz 拒绝→共享视图跳过、策略视图 raise;fork_restored 的 Overwrite 包装
  **不 retain、不 release**;懒获取的 id 经 wrap_tool_call 的 Command 更新落图。
- 租约 `SandboxLeaseManager`:普通 owner release_on_last,借用 holder(上传/
  fork)不请求 park;最后 holder 执行 provider.release(远程=进 warm pool);
  release 幂等、取消安全、外层围栏重复释放。
- 身份 `derive_sandbox_scope_token`:sha256("{user}:{thread}")[:16],改不得;
  `AcquireSerializer` 串行化同键生命周期迁移。
- 路径:`/mnt/user-data/{workspace,uploads,outputs}` + `/mnt/acp-workspace`
  (+ 策略下 `/mnt/skills`);Local 映射翻译 + 逃逸 PermissionError,输出反向
  掩码;不是安全边界。
- 密钥:`env_policy.build_sandbox_env` 默认剥离 \*KEY\*/\*SECRET\*/\*TOKEN\*/
  \*PASS\*/\*CREDENTIAL\*/\*DSN\* + 精确名单,再叠请求级 env。
- Provider:Local(宿主 bash、LRU 256、无边界)/ AIO(Docker + scope 会话 +
  ownership store + bash.exec 镜像门槛)/ OpenSandbox(托管、每次 fresh)/
  BoxLite(微 VM、私有事件循环桥)/ Tenki(云微 VM、原生 fs API、非特权用户);
  远程四个共享 WarmPoolLifecycleMixin(idle 600s、replicas 3)。
