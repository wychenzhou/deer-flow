# 基础设施层中间件（处理逻辑）

本文件解析三个基础设施中间件，它们在 agent 执行前/期间准备好运行所需环境：线程目录、上传文件上下文、沙箱生命周期。

| 中间件 | 职责 |
|--------|------|
| `ThreadDataMiddleware` | 为每个 thread 准备目录结构并把路径写入 state |
| `UploadsMiddleware` | 扫描上传目录，把 `<uploaded_files>` 上下文拼接到最新 HumanMessage |
| `SandboxMiddleware` | 沙箱懒获取/释放，并通过 `wrap_tool_call` 持久化懒初始化结果 |

共享依赖：`Paths`（路径解析）、`get_effective_user_id()` / `resolve_runtime_user_id()`（用户身份解析）。

---

## 1. ThreadDataMiddleware

**职责**：为每个线程计算或创建私有 workspace/uploads/outputs 目录路径，写入 state 的 `thread_data` 字段，供沙箱/上传中间件共用同一事实源（避免不同工具看到不一致路径）。

**处理逻辑**：

- 取 `thread_id`：优先 `runtime.context["thread_id"]`，否则回退 `config["configurable"]["thread_id"]`；两者都没有则抛 `ValueError`（fail-closed，不静默跳过）。
- 解析 `user_id`（`get_effective_user_id()`）：读 ContextVar，无则回退 `"default"`，永不抛异常——始终需要合法用户桶来放目录。
- `lazy_init=True`（默认）只计算路径字符串，不真正建目录；目录创建交给消费端（沙箱 acquire、上传扫描时按需 mkdir），避免每次 before_agent 做同步文件 IO。
- `lazy_init=False` 同步 `mkdir -p` 并显式 `chmod 0o777` 四个目录（workspace/uploads/outputs/acp-workspace）——Docker 容器可能以不同 UID 运行，受 umask 影响会 Permission denied。
- 为最新 HumanMessage 打溯源元数据：保留 id/content/additional_kwargs，追加 `run_id` 与 ISO 时间戳；`name` 缺失填 `user-input`。
- 路径形态：有 user_id 时 `{base}/users/{user_id}/threads/{thread_id}/user-data/...`，无则 `{base}/threads/{thread_id}/user-data/...`（向后兼容）。
- 校验拒绝含路径分隔符或 `..` 的 thread_id/user_id（防目录穿越）。

---

## 2. UploadsMiddleware

**职责**：把上传文件列表注入最新用户消息，让 agent 感知上下文。

**处理逻辑**：

- 前置条件：state 有 messages 且最后一条是 HumanMessage，否则返回 None 不碰 state（续写/纯工具回复不误触发）。
- 解析 uploads 目录与 query 文本：`thread_id` 取不到时（单元测试等无 runnable 上下文）`uploads_dir` 置 None，跳过文件系统检查。
- query 文本优先读 `original_user_content`（净化前保存），否则 fallback 当前 content——因为 sanitize 可能改写文件名/路径字符。
- 抽取当前消息新文件：从 `additional_kwargs["files"]` 读元数据，过滤掉带路径分隔符的 filename（防穿越）、暂存文件（`.upload-*.part`）、以及被删除/未落盘的条目（存在性检查）。
- 扫描历史文件：枚举 uploads 目录，排除暂存文件与新文件名，用 `sorted(iterdir())` 保证稳定可复现；`_mtime`/`_host_path` 是内部字段，处理完 pop 掉不污染 state。
- 按 query 相关性三档排序：filename 完整出现=3、token 长度≥3 匹配=2、扩展名单词边界=1、不匹配=0；第二关键字按 mtime 或原 index，第三按 filename 字典序；匹配的打 `selection_reason="query_match"`。
- 截断：当前消息段与历史段各有独立 `max_files_per_context_section` 上限（默认 10），超出进 omitted 列表，提示"还有 N 个文件未列出"并引导 `glob`/`grep`。
- 抽取文档大纲：查同名 `.md` sibling（上传管道已用 markitdown 转换）；大纲非空则用，为空则读前 5 行非空文本作预览；新文件把 outline 回写到 state 持久化的那份。
- 生成 `<uploaded_files>` 块：仅有新文件或历史文件任一非空才生成，两者都空返回 None；列举文件（路径、大小、选择原因、大纲/预览），尾部固定操作指引（先 read_file/grep/glob，最后才回退 web search）。
- 拼接到最新 HumanMessage：字符串直接前置拼接；多模态列表则把一个 text block 放最前、保留所有原块（图片等）。
- 保留原 `id`（让 dedup 通道稳定）、保留 `additional_kwargs` 的 raw content 备份；`uploaded_files` 只含新文件（历史清单不无限膨胀）。
- 异步钩子 `abefore_agent` 用 `run_in_executor` 把整段阻塞文件 IO 卸载到工作线程（`run_in_executor` 复制 ContextVar，保证 `_current_user` 在 worker 线程可读）。

---

## 3. SandboxMiddleware

**职责**：管理沙箱 acquire/release；懒初始化，并把懒初始化产生的 sandbox_id 持久化到 state。

**处理逻辑**：

- 生命周期：`before_agent`/`abefore_agent` 获取，`after_agent`/`aafter_agent` 释放，`wrap_tool_call`/`awrap_tool_call` 捕获懒初始化副作用并持久化 `sandbox_id`。
- `lazy_init=True`（默认）：不在 before_agent acquire，推迟到第一次工具调用（由沙箱工具的 `ensure_sandbox_initialized` 触发），纯对话场景不分配沙箱省资源。
- `lazy_init=False`（eager）：仅当 state 还没有 `sandbox_id` 时才 acquire（幂等）；`thread_id` 缺失时不 acquire（`acquire(thread_id)` 依赖它做隔离）；异步版本调 `acquire_async` 让阻塞操作不占事件循环。
- 释放优先级：先看 state 里 `sandbox.sandbox_id`，没有则看 `runtime.context["sandbox_id"]`（外部注入），都没有则不动资源。
- 释放后 state 里的 `sandbox_id` 保留（`after_agent` 返回 None，reducer 的 new=None 分支返回 existing），留给 observability/审计；provider 的 release 幂等，下次 get 会重新分配。
- `wrap_tool_call` 持久化懒初始化：执行 handler 前读一次 `sandbox_id`（prev），执行后读一次（curr）；prev 非 None 说明已有沙箱、直接返回 result；prev 是 None 且 curr 非 None 说明刚新建，需把 id 持久化到 state。
- 持久化方式：`ToolMessage` 包装成 `Command(update={sandbox, messages:[msg]})`；`Command` 且 update 是 dict 用 `dataclasses.replace` 合并 sandbox key 并保留原字段；`Command` 且 update 非 dict/None 原样返回，防止在未知 shape 上静默丢数据。
- `merge_sandbox` reducer 幂等写入 + 冲突 fail-closed：id 一致接受，id 不一致抛异常（通常是生命周期/隔离 bug，静默选一个会导致后续用错沙箱）。
