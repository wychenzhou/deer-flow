# 基础设施层中间件：线程目录 · 上传上下文 · 沙箱生命周期

本文件深度讲解三个「基础设施」中间件：它们不做拦截/改写，而是在 **agent 思考之前把运行环境准备好**——为 `(user, thread)` 建私有目录、把刚上传的文件变成模型可读的上下文、保证背后有一个身份对整条图可见的活沙箱。

| 链位 | 中间件 | 一句话职责 | 默认行为 | 它防住的坑 |
|-----|--------|-----------|---------|-----------|
| 4 | `ThreadDataMiddleware` | 为 `(user, thread)` 解析/创建目录并把路径写进 state | `lazy_init=True` 只算路径 | 多用户/线程文件串台、容器 UID 权限、目录穿越 |
| 5 | `UploadsMiddleware`（lead only） | 把**当前这轮**新上传文件以 `<current_uploads>` 块注入最新 HumanMessage | 每节最多列 10 个 | 上传污染记忆、注入面、上下文膨胀 |
| 6 | `SandboxMiddleware` | 沙箱获取/保留/释放，并把懒初始化产生的 `sandbox_id` 发布进 state | `lazy_init=True` 首用才获取 | 懒状态下游不可见、重复释放/跨 owner、provider 并发撕裂 |

三者共享两块地基：`Paths`（`config/paths.py`）与 `resolve_runtime_user_id()`（`runtime/user_context.py`）。先讲透它们，后面所有路径/桶的代码就都能读懂。

---
## 0. 共享地基：Paths 与身份解析
### 0.1 身份解析优先级：为什么不能只信 ContextVar

`resolve_runtime_user_id(runtime)` 是「谁是当前用户」的唯一事实源，按最权威到最兜底解析：

```
resolve_runtime_user_id(runtime):
 ① runtime.server_info.user.identity    LangGraph Agent Server 认证用户（服务端持有，不可伪造）
 ② config.configurable.langgraph_auth_* LangGraph Server 从 @auth.authenticate 写入的认证字段
 ③ runtime.context["user_id"]           Gateway 在请求入口注入；唯一能跨 ContextVar 丢失边界存活的通道
 ④ _current_user ContextVar             auth 中间件在请求入口 set；asyncio 子任务自动继承
 ⑤ "default"                            永不抛异常的最后兜底
```

关键认知：**ContextVar 只是第 4 顺位**。它本质是 task-local——后台任务（Feishu 频道线程、不 `copy_context` 的 worker 池、未来跨进程 driver）一旦脱离请求 task 就丢。`runtime.context` 存在的意义，就是给路径解析一条不依赖 Python 线程模型的权威通道。凡要**落盘用户数据**的代码（本组三个中间件、memory、自定义 Agent）都必须走这个函数，否则会出现「middleware 算 A 桶、工具却写进 B 桶」的串台。

归一化细节：LangGraph 允许任意字符串当 `user.identity`（最常见是邮箱），而目录要求 `[A-Za-z0-9_-]`。`make_safe_user_id()` 在认证边界把不安全字符替换成 `-`，**发生替换就追加 SHA-256 摘要后缀**——两个不同输入绝不共享存储桶。校验侧（`_validate_user_id`/`_validate_thread_id`）对含路径分隔符或 `.`/`..` 的 id 直接 `ValueError`，fail-closed，从根上杜绝目录穿越。

---

## 1. ThreadDataMiddleware：把「谁的文件放哪」钉死

**源码**：`agents/middlewares/thread_data_middleware.py`（约 120 行，本组最短，适合当入门范本）。

### 1.1 它解决什么问题

**坑一：多用户 / 多线程目录串台。** 所有线程共用一个 `workspace/`，用户 A 的对话就能读到上一个线程
甚至用户 B 留下的文件——在「agent 会在工作目录读写、并把产物当证据」的体系里这是灾难。隔离的最小单元
必须是 `(user_id, thread_id)` 二元组。

**坑二：各组件各拼各的路径 → 路径漂移。** 若沙箱工具、上传工具、委派各自算一遍目录，只要一处把
`threads` 写成 `thread`、漏掉 user 层级，模型看到的就是两个互相矛盾的文件系统。对策：**单一事实源**——
`before_agent` 把 `thread_data`（三个路径字符串）写进图 state，谁要用路径都去读它。

**坑三：容器 UID 不同导致 Permission denied。** 沙箱容器可能以宿主不同的 UID 运行，而
`Path.mkdir(mode=...)` 受进程 umask 影响**不保证**最终权限——所以 `ensure_thread_dirs` 在
`mkdir(parents=True, exist_ok=True)` 之后必须**显式 `chmod(0o777)`**，否则 Docker bind mount 一写就炸。

**坑四：每次执行都同步文件 IO 拖慢纯对话。** 很多 run 根本不落盘（直接回答就结束）。默认 `lazy_init=True` 让 `before_agent` **只计算路径字符串，不建目录**，mkdir 推迟给真正消费方（沙箱获取、上传落盘、工具首写）；`lazy_init=False`（eager）才同步建目录——用于必须立即 bind mount 的场景。
### 1.2 钩子与执行时机（链位置）

`before_agent`——只在 agent 节点开始、**模型调用之前**执行一次。链位是共享运行时基座第 4 位
（`tool_error_handling_middleware.py::_build_runtime_middlewares` 的 Layer 2 `thread_hooks` 第一项），
在 InputSanitization(1)/ToolOutputBudget(2)/ToolResultSanitization(3) 之后、Uploads(5) 与 Sandbox(6)
之前。**顺序即正确性**：目录必须先于沙箱获取（bind-mount provider 要求宿主目录已存在），路径必须先于
一切读它的下游（subagent 任务、`.tool-results` 外置）。
### 1.3 内部实现逻辑

```python
def before_agent(self, state, runtime):
    thread_id = runtime.context.get("thread_id")         # ① runtime 上下文优先
    if thread_id is None:                                # ② 无 runnable 上下文（直连图/单测）
        thread_id = get_config()["configurable"]["thread_id"]
    if thread_id is None:
        raise ValueError("Thread ID is required ...")    # fail-closed：没有 thread 就拒绝执行
    user_id = resolve_runtime_user_id(runtime)           # 身份解析见 §0.1（最差也是 "default"）
    if self._lazy_init:
        paths = self._get_thread_paths(thread_id, user_id)           # 只拼字符串
    else:
        paths = self._create_thread_directories(thread_id, user_id)  # ensure_thread_dirs: mkdir+chmod
    # 顺带给最新 HumanMessage 打溯源元数据（保 id/content，追加 run_id + ISO 时间戳）
    if last and isinstance(last, HumanMessage):
        last = HumanMessage(content=last.content, id=last.id, name=last.name or "user-input",
                            additional_kwargs={**last.additional_kwargs,
                                               "run_id": ..., "timestamp": now})
    return {"thread_data": paths, "messages": messages}
```

要点：thread_id 缺了就**抛异常**（地基缺失宁可 run 失败，不静默跳过）；`_get_thread_paths` 只发布 `workspace_path/uploads_path/outputs_path` 三个 key，但 eager 时实际建**四个**目录（多 `acp-workspace`，供 ACP 挂载 `/mnt/acp-workspace`，不进通用事实源）；溯源 stamp **保持 message id 不变**——messages channel 按 id 去重，原地换同 id 消息是幂等更新，`name` 缺失填 `"user-input"`。
### 1.4 目录结构示意

```
{data_dir}（默认 backend/.deer-flow）/
└── users/{user_id}/                        # middleware 恒有 user_id（最差 "default"）
    └── threads/{thread_id}/
        ├── user-data/                      # ⇄ 容器内 /mnt/user-data
        │   ├── workspace/                  #   workspace_path：agent 工作目录
        │   ├── uploads/                    #   uploads_path：前端上传落盘处
        │   └── outputs/                    #   outputs_path：制品 / .tool-results 外置
        ├── acp-workspace/                  # ⇄ /mnt/acp-workspace（eager 也建，不进 state）
        └── skills_view/                    # policy-scoped run 的 Agent skill 投影（见 §3）
```

legacy 布局 `{base}/threads/{thread_id}/...` 仅出现在显式传 `user_id=None` 的旧调用方；middleware 永远
走 users 布局——向后兼容由 `Paths.thread_dir` 内部消化。
### 1.5 与邻居的关系

Uploads(5)/Sandbox(6) 复用同一个 `resolve_runtime_user_id` + `Paths` 组合，算出的上传目录、bind-mount
源与 `thread_data` 严格一致；ThreadData 先跑等于先把「棋盘坐标」定下来。`thread_data` 是跨中间件公共
状态：Sandbox 的路径映射、ToolOutputBudget 的 `.tool-results` 外置、subagent 委派、制品收集都读它。
### 1.6 源码阅读指引

按序读：类 docstring（lazy/eager 模式）→ `_get_thread_paths`/`_create_thread_directories`（只差一次
`ensure_thread_dirs`）→ `before_agent` 主体（thread_id 双通道 → 身份 → 分支 → 溯源）。再跳
`config/paths.py::ensure_thread_dirs` 看 chmod 注释——最容易被忽略的坑。

---
## 2. UploadsMiddleware：只把「这一轮」的上传交给模型

**源码**：`agents/middlewares/uploads_middleware.py`（约 310 行）。**lead agent only**——链上第 5 位由
`include_uploads` 门控，subagent 装配时传 `include_uploads=False`。
### 2.1 它解决什么问题

**坑一：上传当普通上下文会污染记忆、触发提取。** 文件内容/元数据若混进用户消息正文，会被记忆提取、
总结管道当成「对话里说过的事实」固化——文件是**一次性工作素材**，不是长期记忆。注入必须只覆盖
**当前这轮**刚上传的文件，内容自成一块可识别的 `<current_uploads>` 区块，且不把大段文件内容写进消息
（只给路径+大纲+大小，让模型按需 `read_file`）。

**坑二：历史文件逐轮回灌 = 上下文无限膨胀。** 老版本每轮枚举整个 uploads 目录注入，多轮后每次模型调用
都背着所有历史文件名。重构后哲学：**历史文件不进上下文**，需要时用 `list_uploaded_files` 工具按需发现；
本中间件只处理最新 HumanMessage 里 `additional_kwargs["files"]`（前端上传后写入）声明的新文件。

**坑三：文件名是不可信输入。** 可能带路径分隔符（穿越）、是没传完的暂存文件（`.upload-*.part`）、可能
夹带 `<system-reminder>` 等框架标签（提示注入）。所有 user 来源字段（filename/path/大纲标题/预览）
进 prompt 前都过 `neutralize_untrusted_tags`，并做磁盘存在性检查（声明了但磁盘没有 = 被删/未落盘，
跳过并记日志）。
### 2.2 钩子与执行时机（链位置）

`before_agent`（同步）与 `abefore_agent`（异步图路径）。链位 5，紧跟 ThreadData(4)：此时目录坐标已定、
消息已被 InputSanitization(1) 消毒（`original_user_content` 已备份）、ThreadData 已打溯源 stamp——
本中间件在这条消息上**前置拼接** `<current_uploads>` 块，模型随后就看到。

- 前置条件：state 有 messages 且最后一条是 **HumanMessage**，否则返回 `{"uploaded_files": []}`
  不碰消息——续写轮（最后是 AIMessage/ToolMessage）不误触发。
- `abefore_agent` 不复制逻辑，而是 `run_in_executor(None, self.before_agent, state, runtime)` 把整段
  阻塞文件 IO（目录枚举、stat、读 sibling `.md`）卸载到工作线程；`run_in_executor` 拷贝当前 context，
  保证 LangGraph config 与 `_current_user` ContextVar 在 worker 里仍可读，runtime 显式传入以走
  `runtime.context["user_id"]` 权威通道。
### 2.3 内部实现逻辑

```
latest HumanMessage.additional_kwargs["files"] = [ {filename,size,path,status}, ... ]  ← 前端写入
   │ _files_from_kwargs() 四道过滤：①非 dict 跳过 ②filename 空、或含路径分隔符 → 穿越，剔除
   │   ③ is_upload_staging_file()（.upload-*.part）→ 未完成上传，剔除
   │   ④ uploads_dir 可用时 (uploads_dir/filename).is_file() 落盘检查，缺文件剔除
   ▼
new_files（虚拟路径 /mnt/user-data/uploads/{filename} + size + extension）
   │ 无新文件 → 返回 {"uploaded_files": []}（顺带清上一轮残留，防 list_uploaded_files 误排除）
   ▼
context_files = new_files[:10]；omitted = new_files[10:]     # 每节独立上限，默认 10
   │ 每个 context 文件: extract_outline_for_file(物理路径)
   │   = 读上传管道 markitdown 转出的同名 {stem}.md：
   │     有标题结构 → outline[{title,line}]（上限 50 条，截断加 {"truncated":True} 哨兵）
   │     无结构     → preview（前 5 行非空文本）
   ▼
_create_files_message() → <current_uploads> 块
   │   条目: filename(大小)/Path/大纲(L 行号，引导 read_file 按行读)或预览
   │   omitted>0 → “N more file(s) omitted” + 省略文件类型直方图 + glob/grep 指引
   │   尾部固定指引: 先 read_file/grep/glob，最后才回退 web search
   ▼
拼回最新 HumanMessage（保 id+name+additional_kwargs）:
   string 内容 → f"{block}\n\n{原content}"
   list(多模态) → [ {type:"text",text:block}, *原块 ]          # 图片块原样保留
   additional_kwargs[ORIGINAL_USER_CONTENT_KEY] 非字符串时用 message_content_to_text 回填
   ▼
return {"uploaded_files": new_files, "messages": messages}
```

设计细节：

- **cap 是「每节」的**：当前消息段与历史段各有上限防独占预算。当前实现 `new_files` **全量**进 state，
  只有进 prompt 的前 10 条带大纲。
- **`uploaded_files` 状态字段**只记「当前这轮」新文件，供 `list_uploaded_files` 区分「正在上下文里」
  与「已历史、应当列出」——所以每轮都重写/清空它，否则上一轮文件会被 list 工具当成当前文件排除。
- **保留原 message id**：消息 channel 按 id 去重，id 不变则整链（含 summarization 保真逻辑）不会把
  这条消息复制一份。
- **不重复注入**：只有 `files` 非空且过滤后仍有有效文件才注入；老消息的 files 元数据即使还在，
  也因存在性/当前消息检查自然失效。省略文件的类型直方图本身也再过一次 `neutralize_untrusted_tags`。
### 2.4 与邻居的关系

- **依赖 ThreadData(4) 地基**：存在性检查的 `uploads_dir` 由 `Paths.sandbox_uploads_dir` +
  `resolve_runtime_user_id` 算出——同源同桶，绝不会检查别人的目录。
- **依赖 InputSanitization(1) 产物**：正文读取优先 `original_user_content`（消毒前备份），因为
  sanitize 可能改写文件名/路径字符，读当前 content 会拿到被改写的版本。
- **lead-only 原因**：上传是对 **lead 对话**的输入。subagent 也注入会把同一批文件灌进每个子代理上下文
  （token 浪费 + 归属混乱）；subagent 只需经 `task` 拿到委派文本，需要文件时共享 lead 沙箱自己读。
- **与 Sandbox(6) 不互相依赖**，但顺序保证注入发生在沙箱获取前，模型「看到文件」与「拿到能读文件的
  沙箱」不跨轮。
### 2.5 源码阅读指引

按序读：模块 docstring（先看「历史上传不再注入」——这是本中间件区别于老版本的灵魂）→
`_files_from_kwargs`（四道过滤）→ `_select_files_for_context`（cap 语义）→
`_create_files_message`/`_format_file_entry`（块结构+消毒点）→ `before_agent` 主体（拼接分支）→
`abefore_agent`（executor 卸载）。再跳 `utils/file_outline.py` 看 outline 三种标题识别与 preview
回退、`uploads/manager.py` 看 `.upload-*.part` 暂存命名。

---
## 3. SandboxMiddleware：沙箱的获取、可见化与归还

**源码**：`sandbox/middleware.py`（约 520 行，本组最复杂）。依赖：`sandbox/lease.py`（执行租约）、
`sandbox/overwrite.py`（fork 恢复识别）、`sandbox/sandbox_provider.py`（provider 单例）、
`agents/thread_state.py::merge_sandbox`（reducer）。
### 3.1 它解决什么问题

**坑一：懒初始化产生的 `sandbox_id` 对下游图步骤不可见。** LangGraph 的 channel reducer 只捡拾「经
图边写回」的状态更新。懒获取发生在**工具执行中**：`ensure_sandbox_initialized()`（`sandbox/tools.py`）
拿到沙箱后直接改 `runtime.state["sandbox"] = {"sandbox_id": ...}`——工具运行时本地的一次性变更，
**不触发 reducer**，同一步骤后面的工具（ToolOutputBudget 外置、subagent 的 `task_tool` 共享）看到的
state 没有 sandbox。对策见 §3.4：`wrap_tool_call` 前后 diff，一旦发现 id 从无到有就包装成
`Command(update=...)` 走正式图边发布。

**坑二：共享沙箱的重复释放 / 跨 owner 释放。** lead 与多个 subagent 并发跑在同一 thread 沙箱上：
A 先结束若直接 `provider.release()`，B 正在用的沙箱就被停掉（进 warm pool 甚至销毁）。对策是**进程内
执行租约**（`lease.py`）：每个并发执行在 `runtime.context` 分到一个一次性 owner id（`agent:<uuid>`，
server-owned key），lease manager 按 owner 记账，**只有最后一个持有者才真正执行 provider.release
（把远端沙箱停进 warm pool）**。

**坑三：fork 恢复的沙箱被误当成本人的。** delta checkpoint 模式下 rollback/regenerate 以 `Overwrite`
包装形式把父线程 sandbox 状态回放到子执行——直接 release 会**把父线程的 warm 沙箱驱逐掉**。
`unwrap_sandbox()` 解开包装并返回 `fork_restored=True`，释放路径见到就跳过；工具侧以非释放 holder
（`release_on_last=False`）借用，只做 fence 与 scope 清理。

**坑四：checkpoint 里的旧沙箱在策略变更后存活。** 线程之前用共享 skill 视图，现在 run 被限定到某 Agent
的 skill 策略；若不处理，下游工具会经 checkpoint 复用旧共享沙箱，绕过新的文件系统隔离。对策：
policy-scoped run（有 projection）在 `before_agent` **强制抢先获取**，旧共享视图沙箱无法经 checkpoint
存活；状态里已有不同 id 时用 `Overwrite` 整体替换（绕过 merge reducer 的冲突报错）。

**坑五：provider 单例被并发初始化/撕裂。** `_default_sandbox_provider` 可达于多个 OS 线程（主事件循环
+ Feishu 频道线程各跑各的 loop），裸 check-then-create 会双重初始化。对策：`_provider_lock` 双层检查
+ 锁外回调（见 §3.3-c）——锁只护引用交换，插件回调（`__init__/reset/shutdown/动态 import`）在锁外跑，
否则非重入锁被重入的 provider 自死锁，慢 teardown 也会堵住所有并发 `get()`。
### 3.2 钩子与执行时机（链位置 + 生命周期状态机）

钩子全集：`before_agent`/`abefore_agent`（获取/保留）、`after_agent`/`aafter_agent`（释放）、
`wrap_tool_call`/`awrap_tool_call`（diff 发布）。链位 6 = Layer 2 `thread_hooks` 最后一项，排在
ThreadData(4)/Uploads(5) 之后：eager 获取要求线程目录已存在（bind mount），且获取发生在模型/tool
执行之前。**after 钩子按注册序反序执行**——整链收尾时 Sandbox 第一个释放，先停执行资源、再让上层
中间件处理后事。

```
lazy 模式（默认，共享视图）                        eager / policy-scoped 模式
─────────────────────────────                    ─────────────────────────────
before_agent: 只解析身份/投影，不碰 provider、      before_agent:
  不建 owner ◄── 纯对话 run 到此结束，零沙箱资源       ├─ ensure_sandbox_lease_owner()→agent:<uuid>
                                                    ├─ 授权门 authorize("sandbox","execute")
                                                        │ 拒绝&共享视图 → 跳过获取（留给 lazy gate）
                                                        │ 拒绝&policy-scoped → raise（旧沙箱不许存活）
第一次沙箱工具调用                                    ├─ acquire / lease-manager.acquire
  ensure_sandbox_initialized()                       ├─ context["sandbox_id"]=id
  ├─ authorize（task-local 单次决策）                 ├─ projection?→sync_agent_skills()
  ├─ acquire; runtime.state["sandbox"]=id ◄─┐        │     失败→release+pop(context)+raise（回滚）
  └─ 返回 Sandbox                           │        └─ state.sandbox={id}（替换旧 id 用 Overwrite）
                                            │   已有 state.sandbox_id？→ lease.retain()（幂等借用）
wrap_tool_call: prev=None→执行→curr=id 出现◄┘    然后 context["sandbox_id"]=retained_id
  → Command(update={sandbox,messages}) 发布
  → 后续图步骤/下游中间件可见沙箱 id
after_agent（简化）:
  ├─ fork_restored（Overwrite 包装）→ 不释放 ◄─ 父线程的沙箱，放了就驱逐
  ├─ 有 owner → lease-manager.release(owner)   ── 仅最后一个持有者停远端
  ├─ 无 owner → provider.release(sandbox_id)；无 state 但有 context["sandbox_id"] → 同上
  └─ 都没有 → 不动资源（state 里 id 保留供审计）
```
### 3.3 内部实现逻辑

**a) eager 获取的完整决策树（`before_agent`，异步版结构镜像）**

```python
def before_agent(self, state, runtime):
    thread_id = runtime.context.get("thread_id")
    if thread_id is None:
        return super().before_agent(state, runtime)    # 无 thread：无沙箱可隔离
    user_id = resolve_runtime_user_id(runtime)
    projection = self._prepare_agent_skill_projection(thread_id, user_id=user_id)
    owner_id = ensure_sandbox_lease_owner(runtime.context)  # 生成一次 owner: agent:<uuid>

    if self._lazy_init and projection is None:
        # 共享视图+lazy：不提前绑 owner——只回答/只发终止 Command 的 run
        # 不该留下从未被工具用过的 owner，一切推迟到第一个沙箱工具。
        return super().before_agent(state, runtime)

    existing = self._read_sandbox_id_from_state(state)
    if existing is None or projection is not None:     # 没有，或策略要求全新
        try: authorize_sandbox_execution(context=..., app_config=...)
        except SandboxAuthorizationError:
            if projection is not None: raise           # 显式策略：宁可 abort
            return None                                # 共享视图：延迟到 lazy gate
        sandbox_id = self._acquire_sandbox(thread_id, user_id=user_id, owner_id=owner_id)
        runtime.context["sandbox_id"] = sandbox_id
        if projection is not None:                     # 同步技能投影到新沙箱
            try: provider.sync_agent_skills(sandbox_id, ..., projection=projection)
            except BaseException:
                self._release(...); pop(context); raise  # 失败回滚，不留半初始化沙箱
        if existing == sandbox_id: return super().before_agent(state, runtime)
        if existing is not None:
            return {"sandbox": Overwrite({"sandbox_id": sandbox_id})}  # 替换旧共享沙箱
        return {"sandbox": {"sandbox_id": sandbox_id}}                  # 首写走 merge reducer
    retained = self._retain_existing_sandbox(state, thread_id=..., user_id=..., owner_id=...)
    if retained: runtime.context["sandbox_id"] = retained
    return super().before_agent(state, runtime)
```

- `_prepare_agent_skill_projection` 只在 `owns_agent_skill_projection=True`（lead）时真正建投影；
  subagent 传 `False`——它们**共享 lead 的 thread 沙箱**，绝不能用自己更窄的发现列表重写共享的
  skills 视图（会同时改到所有并发使用者）。provider 不支持 skill 隔离却要投影 → `SandboxRuntimeError`
  fail-closed。
- 共享视图+eager 被 `authorize` 拒绝时**不抛**（抛了就是 run 级图错误而非 RFC §9 的友好 ToolMessage），
  改为跳过获取、把拒绝推迟到第一个沙箱工具的 lazy gate——两条路径语义统一。

**b) 释放的「不误伤」规则（`after_agent`）**

```python
sandbox, fork_restored = unwrap_sandbox(state.get("sandbox"))   # Overwrite 包装 = fork 回放
if sandbox is not None:
    if fork_restored: log("Not releasing fork-restored …"); return None
    owner = sandbox_lease_owner(runtime.context)                 # 只读，不新建
    (lease_manager.release(owner) if owner else provider.release(sandbox_id))
    return None                     # 不写 state：merge_sandbox 的 new=None 分支保留旧值供审计
if runtime.context.get("sandbox_id"): ...同上释放外部注入的 id...
return super().after_agent(state, runtime)
```

释放后 state 里的 `sandbox.sandbox_id` **故意保留**（reducer `new is None → return existing`）——留给
observability/审计；provider 的 release 幂等，下次 `ensure_sandbox_initialized` 重新分配。有 owner 时
走 lease manager：一次 release 只撤自己的 holder，**最后持有者才真正 park 远端沙箱**。

**c) provider 单例的双层检查（`sandbox_provider.py`）**

```python
def get_sandbox_provider():
    with _provider_lock:                    # 快速路径：读+返回同一把锁，防 reset 竞态交出 None
        if _default_sandbox_provider is not None:
            return _default_sandbox_provider
    config = get_app_config()               # 冷启动：解析+构造在【锁外】——插件代码
    cls = resolve_class(config.sandbox.use, SandboxProvider)   # 动态 import
    provider = cls(**kwargs)                # 构造器可有副作用（AIO 起 idle-checker 线程）
    with _provider_lock:                    # 第二层检查：安装竞争
        if _default_sandbox_provider is None:
            _default_sandbox_provider = provider; return provider
        winner = _default_sandbox_provider
    provider.shutdown()                     # 输掉竞争：锁外销毁孤儿实例，防泄漏（#3721）
    return winner
```

锁**只护引用交换**；`__init__/reset()/shutdown()/resolve_class()` 这些插件回调全部锁外执行——非重入锁
一旦在回调里重入（provider 自己调 `get_sandbox_provider`）就自死锁，慢 teardown 也会让并发 `get()` 排队。
### 3.4 `wrap_tool_call`：把懒初始化结果发布进图状态

```python
def wrap_tool_call(self, request, handler):
    prev = self._read_sandbox_id_from_request(request)   # 读 request.runtime.state
    result = handler(request)          # ← ensure_sandbox_initialized 可能在此刻新建沙箱
    if prev is not None: return result # 之前就有：非本次懒初始化，原样放行
    curr = self._read_sandbox_id_from_request(request)
    if curr is None: return result     # 没新建（工具没碰沙箱）
    return self._attach_sandbox_update(result, curr)     # 发布正式状态更新
```

`_attach_sandbox_update` 按 result 形态三路处理（绝不静默丢数据）：

```
ToolMessage        → Command(update={"sandbox": {"sandbox_id": id}, "messages": [msg]})
Command(dict)      → dataclasses.replace(cmd, update={**原字段, "sandbox": ...})
                     # 保留 messages/goto/graph/resume 等既有字段
Command(非 dict/None)→ 原样返回        # update 形态未知时不动，避免在未知 shape 上丢数据
```

state 侧 `merge_sandbox` reducer（`thread_state.py`）与之配对：

```python
def merge_sandbox(existing, new):
    if new is None: return existing      # after_agent 的 None → 保留旧值（审计）
    if existing is None: return new
    if existing["sandbox_id"] == new["sandbox_id"]: return existing   # 幂等：多工具同一步懒初始化
    raise ValueError(f"Conflicting sandbox state updates: …")  # 冲突=生命周期/隔离 bug，fail-closed
```

两个写路径的分工：懒初始化走 `wrap_tool_call` 的 **merge（幂等，同 id 可重复发布）**；eager 路径替换
旧沙箱走 **`Overwrite`**（跳过 reducer 冲突检查——那是**故意的替换**，不是并发冲突）。`fork_restored`
沙箱在 eager 路径的 retain 也被跳过（`unwrap_sandbox` 返回 True 时 `_retain_existing_sandbox` 直接
返回 None）——回放状态不允许被本 run 当成自己的资源绑定。
### 3.5 与邻居的关系（ThreadData → Uploads → Sandbox 为什么是这个顺序）

1. **ThreadData(4) 先建坐标系**：`thread_data` 路径是 provider bind-mount 的宿主源，eager 下目录必须
   先于沙箱存在；它是全链的「棋盘」。
2. **Uploads(5) 先铺上下文**：让模型在同一次调用里既看到 `<current_uploads>` 又拿到能读这些文件的
   沙箱，认知不跨轮。
3. **Sandbox(6) 殿后**：它是执行资源，只在消息/目录就绪后才有意义；after 钩子逆序最先释放，保证整个
   run 生命周期内沙箱可用。
4. **owner 差异**：lead 拥有线程物理 skill 投影（`owns_agent_skill_projection=True`）并可 eager 获取；
   delegated subagent / prompt-only bootstrap agent 是 **non-owner**——`False` 让它们绝不重建共享
   视图，也不强制 eager 获取。并发时 lease 层保证只有最后持有者 park 远端沙箱。
### 3.6 源码阅读指引

按序读 `middleware.py`：类 docstring（生命周期四句话）→ `_prepare_agent_skill_projection` +
`_require_projection_support`（subagent 为何不建投影）→ `before_agent` 决策树 →
`_retain_existing_sandbox`/`after_agent`（fork_restored 与 lease 释放）→ `wrap_tool_call` +
`_attach_sandbox_update`（Command 三路合并）。然后：`sandbox/overwrite.py`（20 行）看
`unwrap_sandbox` 为何返回 `(value, fork_restored)`；`sandbox/tools.py::ensure_sandbox_initialized`
看懒获取现场（`runtime.state["sandbox"]` 直改、fork 借用 `reuse_or_acquire(release_on_last=not
fork_restored)`、authorize 单次决策）；`agents/thread_state.py::merge_sandbox` 看 reducer；
`sandbox/lease.py` 看 `acquire/retain/release`（`release_on_last` 语义）与 owner 的「建 vs 读」分工
（`ensure_sandbox_lease_owner`/`sandbox_lease_owner`）；`sandbox/sandbox_provider.py` 看双层检查注释。

---
## 4. 协作时序与关键设计决策

一条 run 的时序（lazy 共享视图，最常见路径）：

```
run 开始
  ├─ InputSanitization(1)… 净化消息、备份 original_user_content
  ├─ ThreadData(4).before_agent   thread_id 校验 → 身份解析 → thread_data 路径 → 消息溯源 stamp
  ├─ Uploads(5).before_agent      files 过滤 → <current_uploads> 前置拼接 → uploaded_files 状态
  ├─ Sandbox(6).before_agent      lazy+共享视图 → 零操作（不建 owner、不碰 provider）
  ├─ 模型调用 → 决定调工具
  │    └─ 首个沙箱工具: ensure_sandbox_initialized → authorize → acquire
  │         → runtime.state["sandbox"]=id（本地）
  │         → wrap_tool_call 前后 diff → Command(update) 发布 ◄─ 关键一步
  ├─ 下游工具/中间件（ToolOutputBudget 外置、task 委派共享等）读到 sandbox_id
  └─ after_agent(逆序): Sandbox(6) 释放（fork_restored 跳过；owner lease 记账）
```

**关键设计决策**：

1. **路径与身份做成「单一事实源」而非各自解析**：ThreadData 把 `thread_data` 写进 state，下游统一读它；
   身份统一走 `resolve_runtime_user_id` 五级解析（server auth > context > ContextVar > default），
   杜绝「middleware 算 A 桶、工具写 B 桶」，并以 `[A-Za-z0-9_-]` 校验 + 穿越拒绝 fail-closed 兜底。
2. **默认全 lazy，把「可能用不到」的初始化推迟到第一次真实消费**：目录只由消费方 mkdir，沙箱只在第一个
   沙箱工具调用时 acquire，纯对话 run 连 owner 都不绑定；eager 模式留给 bind-mount / policy-scoped
   场景显式开启。
3. **沙箱身份经「wrap_tool_call 前后 diff + Command 发布」补上 LangGraph 可见性缺口**：reducer 捡不到
   工具内部对 `runtime.state` 的直改，就在图边正式补发；`merge_sandbox` 幂等收口同一步骤多次发布、
   对 id 冲突 fail-closed，替换旧沙箱则显式用 `Overwrite` 表达「故意换」。
4. **共享沙箱用进程内 owner 租约管理而非裸 acquire/release**：并发 lead/subagent 各持本地租约，只有
   最后持有者 park 远端；`fork_restored`（父线程状态回放）一律不释放、只借用（`release_on_last=False`），
   从机制上消灭重复释放与跨 owner 释放。
5. **provider 单例双层检查 + 插件回调锁外执行**：锁只护引用交换，构造/重置/销毁/动态 import 全在锁外，
   输掉安装竞争的孤儿实例被 `shutdown()` 回收（#3721）——并发初始化既不撕裂也不自死锁。
