# 文件安全中间件（处理逻辑）

本文件解析两个文件安全/工具结果质量中间件，链上相邻且互为配合：

- `ReadBeforeWriteMiddleware`（位置 11）：文件写入门禁，强制"先读后写"的版本门
- `ToolProgressMiddleware`（位置 12）：工具结果质量守卫，状态机检测工具停滞后与重复

`ReadBeforeWriteMiddleware` 在 `ToolProgressMiddleware` 的外层，使被门禁阻塞的写操作不会进入进度状态机计数，避免一次合理的"拒绝"被误判为工具故障。

---

## 1. ReadBeforeWriteMiddleware

**职责**：版本门——agent 想修改已存在文件，必须先在本会话上下文（`state["messages"]`）里通过 `read_file` 读过该文件当前版本。

**处理逻辑**：

- 判据：读时记录的 sha256 内容哈希 vs 当前文件 sha256；一致才放行。
- 触发：`write_file`/`str_replace` 一命中，先在锁内解析路径、检查门禁；`read_file` 命中则先执行读取、再给结果盖读标记；两路共用同一把 `(scope, norm_path)` 锁。
- 锁隔离：每进程一个 `WeakValueDictionary` 存 `threading.Lock`，key 为 `(scope, norm_path)`；scope 优先 `thread_id`，否则回退 `sandbox_id`，都没有用 `global`，让无关 agent 互不阻塞。
- 路径规范化：`posixpath.normpath` 统一，`/a/b/../c` 与 `/a/c` 视为同一路径。
- **判定通过**：最新读标记哈希 == 当前文件哈希（读到的就是当前版本）。
- **判定不通过**：哈希不同（读的是旧版本 / 文件被写或外部改过）、或根本没有读标记（文件存在但本次会话没读过）→ 返回 error 状态 ToolMessage，提示先重读再写。
- 拦截提示明确"任意写入都会失效此前读标记，每次修改前都要重读"，并提示可只读相关区段（如追加前读约 30 行）。
- 拦截结果绕开 `ToolErrorHandlingMiddleware`，所以主动调 `normalize_tool_result` 盖 `deerflow_tool_meta`（标 `recoverable_by_model=True`），保证内层 `ToolProgressMiddleware` 仍正确分类、不误触 warn。
- 读标记只盖在 `read_file` 成功的 `ToolMessage.additional_kwargs` 上，状态完全活在 `state["messages"]`；盖的哈希不是从工具返回内容解析，而是**再读一次真实磁盘**，确保是"读之后、写之前"的当前版本。
- **Fail-open 场景**：path 缺失/非字符串直接放行；`_content_reader` 抛 `FileNotFoundError` 视为新建文件放行；抛其它异常记 warning 放行；返回内容以 `Error:` 开头（AIO/E2B 风格）记 debug 放行且不盖标记；读取结果为 error 状态不盖标记。
- **设计原则**：
  - 标记放 messages 而非独立 channel（让 summarization 删读结果时顺带删标记）。
  - **写入永不刷新标记**——否则 agent 可连续写多次而不再读，正是 #3857 原始故障。
  - 把"门禁检查+工具执行"与"读+盖戳"都放进同一临界区，防止同一 turn 并发写都拿过期标记各自落地。
  - 与 `sandbox/file_operation_lock.py` 是同模式但命名空间独立的另一把锁。

---

## 2. ToolProgressMiddleware

**职责**：基于状态机跟踪每个工具在 `(thread_id, tool_name)` 维度上连续"无新信息"调用的次数，检测停滞与重复（RFC #3177）。

**状态机**（ACTIVE → WARNED → BLOCKED）：

- **先判 BLOCKED**：工具调用进来先查是否已 blocked，是则直接返回 `[TOOL_BLOCKED] ...` 错误消息（标 `recoverable_by_model=True`、`recommended_next_action="summarize"`），不执行 handler。
- 推进：先把 problem 计数 +1（保证各分支计数一致）；对 success 结果才算 Jaccard 词集，error/partial_success 一律视为 problem。
- **立即 BLOCKED**：`recoverable_by_model=False` 且 `recommended_next_action="stop"`（auth/config/internal）首次出现即硬阻塞——模型重试无意义。
- **升级 BLOCKED**：`recoverable_by_model=False` 且 `action≠stop`（rate_limited/transient）且连续 problem ≥ `stagnation_threshold + warn_escalation`。
- **WARNED（终端）**：`recoverable_by_model=True`（no_results/not_found/permission/Jaccard 重复的 success）且计数达升级阈值 → 保持 WARNED 并重复注入提示，**不硬阻塞**（避免阻止合理的换参重试）。
- **WARNED（过渡）**：计数达 `stagnation_threshold` 但未达升级阈值 → 注入提示。
- **好结果重置**：consecutive_problems 归零、phase 回 active，并把本次词集加入 `recent_word_sets`（只保留最近 3 个）。

**Jaccard 判重**：内容截断到 8192 字符，取长度 ≥3 的单词做词集；词集小于 `min_words`（默认 10）不参与比较；与最近 3 个词集任一 Jaccard ≥ 阈值（默认 0.8）视为 near-duplicate。

**提示注入**：工具执行产生的 hint 不直接进当前消息流，而是排入 `(thread_id, run_id)` 的 pending 队列（单 run 上限 3 条），在下次模型调用前经 `wrap_model_call` 抽出、`dict.fromkeys` 保序去重后以 `HumanMessage(name="progress_hint")` 追加。

**run 边界**：`before_agent` 每次新 run 清掉非当前 run_id 的过期 pending，并无条件把该线程所有工具状态重置回 active（清零计数、block_reason、recent_word_sets）——因为 rate_limited/transient 是时间相关错误，根因可能在新 run 已消失。

**LRU**：`_phase_states` 是 OrderedDict，最大跟踪 100 线程；驱逐最老线程同步清其 pending；读路径 `_get_block_reason` 故意不 `move_to_end`（避免 blocked 线程长期挤占位置）。

**豁免工具**：`ask_clarification`、`write_todos`、`present_files`、`task` 跳过状态机——这些工具的"无新信息"不代表停滞。
