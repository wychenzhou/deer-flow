# 基础设施层中间件详解

本文档详细解析 DeerFlow 中三个基础设施层中间件的实现、数据流与设计决策。这三个中间件负责在 agent 执行前/执行期间为其准备好运行所需的环境资源：线程目录、上传文件上下文以及沙箱生命周期。

- **ThreadDataMiddleware** — 为每个 thread 准备目录结构并将路径写入 state
- **UploadsMiddleware** — 扫描上传目录并把 `<uploaded_files>` 上下文拼接到最新 HumanMessage
- **SandboxMiddleware** — 沙箱的懒获取/释放，并通过 `wrap_tool_call` 持久化懒初始化结果

它们都位于 `packages/harness/deerflow/` 下，共享 `Paths`（路径解析）与 `get_effective_user_id()` / `resolve_runtime_user_id()`（用户身份解析）这两个跨中间件的公共依赖。

---

# 1. ThreadDataMiddleware

## 概述

为每个线程（thread）按需计算或创建其私有的 workspace/uploads/outputs 目录路径，并将这些路径写入 graph state 的 `thread_data` 字段，供后续的沙箱工具与上传中间件使用。

## 为什么需要这个中间件

### 场景痛点

没有这个中间件，每个需要写磁盘的工具（sandbox bash、文件上传、输出制品）都无法保证工作目录存在。第一次尝试在 sandbox 里写文件时会失败，因为目录尚未创建。更糟的是，各个消费者会自行猜测路径计算方法，导致同一次对话中不同工具看到的路径不一致——sandbox bash 用路径 A，文件上传用路径 B，输出制品用路径 C，"文件未找到"的错误层出不穷。

### 为什么模型自身无法避免

LLM 只能操作抽象路径字符串（如 `/mnt/user-data/workspace/`），无法计算宿主机侧的文件系统路径，也不会创建目录或设置权限（如 `chmod 0o777`）。即便在 prompt 中要求模型 `mkdir -p`，它也不知道正确的宿主机根路径——那是一个部署环境相关的 runtime 信息，应归属基础设施层而非模型推理。

### 解决思路

在 agent 执行前计算或创建好线程私有的目录结构，将所有路径写入 graph state，让下游消费者从同一个"事实源"读取一致的路径。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/thread_data_middleware.py` |
| 实现的钩子 | `before_agent` |
| 持久化 | State (`thread_data` 字段，会被 checkpointer 持久化) |
| 配置依赖 | `Paths(base_dir)`（来自 `deerflow.config.paths`），`lazy_init` 构造参数 |
| State Schema | `ThreadDataMiddlewareState`（与 `ThreadState` schema 兼容） |

## 核心逻辑

### 构造与依赖

```python
class ThreadDataMiddleware(AgentMiddleware[ThreadDataMiddlewareState]):
    state_schema = ThreadDataMiddlewareState

    def __init__(self, base_dir: str | None = None, lazy_init: bool = True):
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()
        self._lazy_init = lazy_init
```

- `base_dir` 可选；为 `None` 时走全局 `get_paths()`，读取 `DEER_FLOW_CONFIG_PATH` 或默认 `.deer-flow/` 目录。
- `lazy_init=True`（默认）只计算路径，**不真正创建目录**；`lazy_init=False` 则在 `before_agent` 里同步 `mkdir -p`。

### State Schema

```python
class ThreadDataMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""
    thread_data: NotRequired[ThreadDataState | None]
```

`ThreadDataState` 是 `TypedDict`，包含三个可空字符串字段：`workspace_path`、`uploads_path`、`outputs_path`。由于使用 `NotRequired`，与主 `ThreadState` schema 兼容，不会破坏既有 checkpoint。

### `before_agent` 钩子

```python
@override
def before_agent(self, state, runtime: Runtime) -> dict | None:
    context = runtime.context or {}
    thread_id = context.get("thread_id")
    if thread_id is None:
        config = get_config()
        thread_id = config.get("configurable", {}).get("thread_id")

    if thread_id is None:
        raise ValueError("Thread ID is required in runtime context or config.configurable")

    user_id = get_effective_user_id()

    if self._lazy_init:
        paths = self._get_thread_paths(thread_id, user_id=user_id)
    else:
        paths = self._create_thread_directories(thread_id, user_id=user_id)
        logger.debug("Created thread data directories for thread %s", thread_id)
    ...
```

**处理流程**：

1. **获取 `thread_id`**：优先从 `runtime.context["thread_id"]`（Gateway 路径注入），否则回退到 `get_config()["configurable"]["thread_id"]`（LangGraph RunnableConfig 路径）。两条都拿不到时**抛 `ValueError`**，fail-closed，不静默跳过。
2. **解析 `user_id`**：调用 `get_effective_user_id()`。该函数读取 `_current_user` ContextVar，未设置时返回 `DEFAULT_USER_ID = "default"`（无认证模式下的兜底），**永不抛异常**——这是路径解析的刻意设计，因为始终需要一个合法的用户桶来放目录。
3. **计算/创建路径**：
   - `lazy_init=True`：调用 `_get_thread_paths` 只算路径字符串（见下），不触碰文件系统。
   - `lazy_init=False`：调用 `_create_thread_directories`，内部走 `Paths.ensure_thread_dirs`，会 `mkdir(parents=True, exist_ok=True)` 并显式 `chmod(0o777)` 四个目录（workspace/uploads/outputs/acp-workspace）。`chmod` 是刻意为之：Docker 沙箱容器可能以不同的 UID 运行，默认 `mkdir(mode=...)` 受 umask 影响可能给容器造成 "Permission denied"，显式 `chmod` 才能保证容器可写。
4. **为最新 HumanMessage 打溯源元数据**：

```python
messages = list(state.get("messages", []))
last_message = messages[-1] if messages else None

if last_message and isinstance(last_message, HumanMessage):
    messages[-1] = HumanMessage(
        content=last_message.content,
        id=last_message.id,
        name=last_message.name or "user-input",
        additional_kwargs={
            **last_message.additional_kwargs,
            "run_id": context.get("run_id"),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
```

   保留 `id` / `content` / `additional_kwargs`，再**追加** `run_id`（来自 `runtime.context`）和 ISO 8601 UTC 时间戳到 `additional_kwargs`。`name` 缺失时填默认值 `"user-input"`。这条信息下游会被 tracing、记忆抽取、上传中间件等读取作为溯源锚点。

5. **返回 state 更新**：

```python
return {
    "thread_data": {**paths},
    "messages": messages,
}
```

   `paths` 字典含三个键：`workspace_path`、`uploads_path`、`outputs_path`，值为字符串形式的宿主路径（见 `_get_thread_paths`）。

### 路径解析细节

```python
def _get_thread_paths(self, thread_id, user_id=None) -> dict[str, str]:
    return {
        "workspace_path": str(self._paths.sandbox_work_dir(thread_id, user_id=user_id)),
        "uploads_path":   str(self._paths.sandbox_uploads_dir(thread_id, user_id=user_id)),
        "outputs_path":   str(self._paths.sandbox_outputs_dir(thread_id, user_id=user_id)),
    }
```

底层 `Paths.sandbox_work_dir` 实现为：

```python
def sandbox_work_dir(self, thread_id, *, user_id=None) -> Path:
    return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "workspace"

def thread_dir(self, thread_id, *, user_id=None) -> Path:
    if user_id is not None:
        return self.user_dir(user_id) / "threads" / _validate_thread_id(thread_id)
    return self.base_dir / "threads" / _validate_thread_id(thread_id)
```

- 有 `user_id`：路径形如 `{base_dir}/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`
- 无 `user_id`：路径形如 `{base_dir}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`（向后兼容）

`_validate_thread_id` / `_validate_user_id` 会拒绝包含路径分隔符或 `..` 的值，防目录穿越。

## 关键设计决策

### 1. lazy_init 默认为 True 的取舍

真正的目录创建交给**消费端**（如 `SandboxProvider.acquire` 或 `UploadsMiddleware` 扫描时）按需 `mkdir`，而不是在每次 agent 调用前都做一遍文件系统操作。

**Trade-off**：
- 优点：避免每次 `before_agent` 都做同步文件系统 IO，减少热路径开销；多中间件不需要重复 `mkdir`。
- 缺点：目录的实际创建被分散到多个调用点，对调试不直观；如果下游忘了 `mkdir`，可能到第一次写入时才失败。

### 2. user_id 回退到 "default"

`get_effective_user_id()` 在无 ContextVar 时返回 `"default"` 而非 `None`。这意味着无认证模式下所有数据落在一个共享的 `users/default/` 桶里——既能正常工作，又能在后续开启认证时通过 `scripts/migrate_user_isolation.py` 把 legacy 数据迁到具体用户桶下。

### 3. 兼容 ThreadState schema 而不强制使用

`ThreadDataMiddlewareState` 只声明 `thread_data`，但标注 "Compatible with `ThreadState` schema"。这样中间件既能在测试中以最小 schema 独立运行，又能在生产里与完整 `ThreadState` 的其它字段共存，reducer 不会冲突（`thread_data` 是 `NotRequired` 的 `LastValue` 通道）。

### 4. 为何把 run_id/timestamp 写到最后一条 HumanMessage

这是整个 run 的溯源锚点：`run_id` 让后续 tracing、记忆抽取、上传中间件都能对齐到当前运行；`timestamp` 给审计和日志提供时间基准。写在 `additional_kwargs` 而非 `content` 里，确保不会污染模型可见的 prompt。

## 与其他中间件的协作

- **UploadsMiddleware**：直接消费 `thread_data.uploads_path` 对应的 `Paths.sandbox_uploads_dir`，本中间件负责把路径算好放进 state。
- **SandboxMiddleware**：沙箱 provider 在 `acquire(thread_id)` 时会创建 workspace 目录；本中间件 lazy 模式下不创建，但 state 里的路径已是最终宿主路径，沙箱工具可直接拿来翻译虚拟路径 `/mnt/user-data/...`。
- **DynamicContextMiddleware / MemoryMiddleware**：读取最新 HumanMessage 的 `additional_kwargs.run_id` / `timestamp` 作为溯源。
- **ACP 工具**：`Paths.ensure_thread_dirs` 会顺带创建 `acp-workspace` 目录，供 ACP 代理容器卷挂载为 `/mnt/acp-workspace`。

---

# 2. UploadsMiddleware

## 概述

扫描线程的上传目录，把当前消息新上传的文件与历史已上传文件构造成 `<uploaded_files>` 上下文块，前置拼接到最新 HumanMessage 的 content 上，让模型知道有哪些文件可用、它们的路径与文档大纲。

## 为什么需要这个中间件

### 场景痛点

没有这个中间件，用户上传了文件（PDF、图片、代码），模型却完全不知道哪些文件可用、它们在 sandbox 里的路径是什么、文档内容概要是什么。模型只能盲目地猜测文件名、自己 `ls /mnt/user-data/uploads/` 遍历目录，浪费多次工具调用且输出不一致。更严重的是，模型无法区分"这条消息新上传的文件"和"历史对话中已上传的文件"，导致重复引用或遗漏。

### 为什么模型自身无法避免

模型是被动的：它只能响应工具调用的返回结果，不能主动扫描文件系统。让模型用 `ls` + `read_file` 逐个探索上传目录，既浪费 token 又无法做"文件名与用户问题相关性排序"——那是信息检索的活儿，不是文本生成的范畴。此外，文档大纲的抽取需要对 PDF/PPT/Excel 做格式转换（markitdown），这对模型本身不现实。

### 解决思路

在 `before_agent` 中扫描上传目录，区分新文件与历史文件，按查询相关性排序，抽取文档大纲，将结构化的 `<uploaded_files>` 上下文块注入到最新 HumanMessage 中，让模型在首次应答前就拥有完整的文件上下文。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/agents/middlewares/uploads_middleware.py` |
| 实现的钩子 | `before_agent`（同步）+ `abefore_agent`（异步） |
| 持久化 | State (`uploaded_files` 字段 + 修改 `messages` 最新一条)；checkpointer 会持久化 |
| 配置依赖 | `Paths(base_dir)`；`max_files_per_context_section` 构造参数（默认 10） |
| State Schema | `UploadsMiddlewareState`（声明 `uploaded_files: NotRequired[list[dict] | None]`） |

## 核心逻辑

### 构造与模块常量

```python
_OUTLINE_PREVIEW_LINES = 5
_MAX_FILES_PER_CONTEXT_SECTION = 10
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+")

class UploadsMiddleware(AgentMiddleware[UploadsMiddlewareState]):
    state_schema = UploadsMiddlewareState

    def __init__(self, base_dir=None, *, max_files_per_context_section=_MAX_FILES_PER_CONTEXT_SECTION):
        if max_files_per_context_section < 1:
            raise ValueError("max_files_per_context_section must be at least 1")
        self._paths = Paths(base_dir) if base_dir else get_paths()
        self._max_files_per_context_section = max_files_per_context_section
```

`max_files_per_context_section` 是**每个上下文段落**的文件数上限，分别作用于"当前消息新上传"与"历史上传"两段。超出部分进入 `omitted_*` 列表，生成"还有 N 个文件未列出"的提示并引导模型使用 `glob`/`grep`。

### `before_agent` 主流程

```python
@override
def before_agent(self, state, runtime: Runtime) -> dict | None:
    messages = list(state.get("messages", []))
    if not messages:
        return None

    last_message_index = len(messages) - 1
    last_message = messages[last_message_index]

    if not isinstance(last_message, HumanMessage):
        return None
    ...
```

**前置条件**：state 里必须有 messages，且最后一条必须是 `HumanMessage`。否则直接返回 `None`，不修改任何 state——这意味着 agent 续写、纯工具回复等场景不会被误触发。

#### 1. 解析 uploads 目录与 query 文本

```python
thread_id = (runtime.context or {}).get("thread_id")
if thread_id is None:
    try:
        from langgraph.config import get_config
        thread_id = get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        pass  # get_config() 在单元测试等无 runnable 上下文时会抛
uploads_dir = self._paths.sandbox_uploads_dir(thread_id, user_id=get_effective_user_id()) if thread_id else None

query_text = get_original_user_content_text(last_message.content, last_message.additional_kwargs)
```

- `get_original_user_content_text` 优先读 `additional_kwargs["original_user_content"]`（`InputSanitizationMiddleware` 在 sanitize 之前保存的原始文本），否则 fallback 到当前 `content` 的文本。**使用原始文本而非 sanitize 后的版本**，因为 sanitize 可能改写文件名/路径字符，而这里要做 query 匹配。
- `get_config()` 在非 runnable 上下文（如单元测试直接调用 `before_agent`）会抛 `RuntimeError`，被吞掉——`thread_id` 留作 `None`，后续 `uploads_dir=None`，跳过文件系统检查。

#### 2. 抽取当前消息的新文件

```python
new_files = self._files_from_kwargs(last_message, uploads_dir) or []
context_new_files, omitted_new_files = self._select_files_for_context(new_files, query_text)
```

`_files_from_kwargs` 从 `last_message.additional_kwargs["files"]` 读取前端上传后塞入的元数据：

```python
kwargs_files = (message.additional_kwargs or {}).get("files")
if not isinstance(kwargs_files, list) or not kwargs_files:
    return None

files = []
for f in kwargs_files:
    if not isinstance(f, dict):
        continue
    filename = f.get("filename") or ""
    if not filename or Path(filename).name != filename or is_upload_staging_file(filename):
        continue
    if uploads_dir is not None and not (uploads_dir / filename).is_file():
        continue
    files.append({
        "filename": filename,
        "size": int(f.get("size") or 0),
        "path": f"/mnt/user-data/uploads/{filename}",   # 虚拟路径，沙箱内可见
        "extension": Path(filename).suffix,
    })
return files if files else None
```

**关键过滤条件**：
- `Path(filename).name != filename` → 拒绝带路径分隔符的 filename（防目录穿越）。
- `is_upload_staging_file(filename)` → 排除 Gateway 上传过程中残留的 `.upload-*.part` 暂存文件（前缀+后缀双匹配）。
- `uploads_dir / filename` 存在性检查 → 跳过已被删除/未落盘的条目，避免给模型列出"幻影文件"。仅在 `uploads_dir` 已解析时执行。

每个文件输出 dict 的 `path` 是**虚拟路径** `/mnt/user-data/uploads/...`——这是沙箱内的稳定契约，无论 `LocalSandbox`（host 翻译）还是 `AioSandbox`（容器卷挂载）都接受此路径。

#### 3. 扫描历史文件

```python
new_filenames = {f["filename"] for f in new_files}
historical_candidates: list[dict] = []
if uploads_dir and uploads_dir.exists():
    for file_path in sorted(uploads_dir.iterdir()):
        if is_upload_staging_file(file_path.name):
            continue
        if file_path.is_file() and file_path.name not in new_filenames:
            stat = file_path.stat()
            historical_candidates.append({
                "filename": file_path.name,
                "size": stat.st_size,
                "path": f"/mnt/user-data/uploads/{file_path.name}",
                "extension": file_path.suffix,
                "_mtime": stat.st_mtime,      # 仅供排序，后续 pop 掉
                "_host_path": file_path,       # 仅供抽 outline，后续 pop 掉
            })
```

- 排除暂存文件与当前消息新文件，避免重复。
- `sorted(uploads_dir.iterdir())` 让结果稳定可复现（便于测试）。
- `_mtime` / `_host_path` 是**内部字段**（前缀下划线约定），稍后会从对外可见的字典里 pop 掉，不污染 state。

#### 4. 按查询相关性排序、截断

```python
historical_files, omitted_historical_files = self._select_files_for_context(
    historical_candidates, query_text, recency_key="_mtime",
)
```

`_select_files_for_context` 是核心排序/截断逻辑：

```python
def _select_files_for_context(self, files, query_text, *, recency_key=None):
    ranked: list[tuple[tuple, dict]] = []
    for index, file in enumerate(files):
        selected_file = dict(file)
        match_strength = _query_match_strength(selected_file, query_text)
        query_match = match_strength > 0
        if query_match:
            selected_file["selection_reason"] = "query_match"

        if recency_key:
            sort_key = (-match_strength, -float(selected_file.get(recency_key) or 0), selected_file["filename"])
        else:
            sort_key = (-match_strength, index)
        ranked.append((sort_key, selected_file))

    ranked.sort(key=lambda item: item[0])
    selected = [file for _, file in ranked[: self._max_files_per_context_section]]
    omitted   = [file for _, file in ranked[self._max_files_per_context_section :]]
    return selected, omitted
```

排序键（取负让大的靠前）：
- **第一关键字 `-match_strength`**：与 query 匹配度高的排最前。`_query_match_strength` 给出三档：
  - `3`：filename 或 stem（去扩展名）完整出现在 query 里。
  - `2`：stem 拆 token 后，有长度 ≥ 3 的 token 出现在 query 里（如 `report` 匹配 `quarterly_report.pdf`）。
  - `1`：扩展名作为单词边界出现在 query 里（如 query 里写了 "pdf"）。
  - `0`：完全不匹配。
- **第二关键字**：有 `recency_key` 时按 `-mtime`（最近修改优先），否则按原 index（保持原顺序）。
- **第三关键字**：`filename` 字典序，避免 tie 时顺序不稳定。

匹配上的文件会被打上 `selection_reason="query_match"`，格式化时会输出"Selected because: matched the current query."

#### 5. 抽取文档大纲（outline）

```python
for file in historical_files:
    file_path = file.pop("_host_path")
    file.pop("_mtime", None)
    outline, preview = _extract_outline_for_file(file_path)
    file["outline"] = outline
    file["outline_preview"] = preview

if uploads_dir:
    new_files_by_name = {file["filename"]: file for file in new_files}
    for file in context_new_files:
        phys_path = uploads_dir / file["filename"]
        outline, preview = _extract_outline_for_file(phys_path)
        file["outline"] = outline
        file["outline_preview"] = preview
        if original_file := new_files_by_name.get(file["filename"]):
            original_file["outline"] = outline
            original_file["outline_preview"] = preview
```

`_extract_outline_for_file` 的核心逻辑：

```python
def _extract_outline_for_file(file_path: Path) -> tuple[list[dict], list[str]]:
    md_path = file_path.with_suffix(".md")
    if not md_path.is_file():
        return [], []

    outline = extract_outline(md_path)
    if outline:
        return outline, []

    # outline 为空 → 读前 5 行非空行作为内容预览
    preview: list[str] = []
    try:
        with md_path.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    preview.append(stripped)
                if len(preview) >= _OUTLINE_PREVIEW_LINES:
                    break
    except Exception:
        logger.debug("Failed to read preview lines from %s", md_path, exc_info=True)
    return [], preview
```

**设计要点**：
- 上传管道（Gateway `/api/threads/{id}/uploads`）会调用 `markitdown` 把 PDF/PPT/Excel/Word 转换成同名 `.md` 放在 uploads 目录旁边。这里查找的就是这个 sibling `.md` 文件。
- `extract_outline` 解析 pymupdf4llm 三种标题风格：标准 `#`、纯粗体（SEC filing 常用）、分拆粗体（学术论文常用）。
- 大纲非空时，preview 返回空；大纲为空时返回前 5 行非空文本作为"内容锚点"，让模型在无结构标题时仍有起点。
- 新文件和历史文件都会抽 outline；新文件额外把 outline 写回到 `new_files` 列表（state 里持久化的那份），让前端和后续运行也能复用。

#### 6. 生成 `<uploaded_files>` 消息文本

```python
if not context_new_files and not historical_files:
    return None
...
files_message = self._create_files_message(
    context_new_files, historical_files,
    omitted_new_files=omitted_new_files,
    omitted_historical_files=omitted_historical_files,
)
```

如果当前消息新文件列表和历史文件列表**都为空**，直接返回 `None`，不动 state——避免在没有任何上传的对话里产生无意义 prompt 噪声。

`_create_files_message` 生成的文本结构（简化示例）：

```
<uploaded_files>
The following files were uploaded in this message:

- report.pdf (245.3 KB)
  Path: /mnt/user-data/uploads/report.pdf
  Selected because: matched the current query.
  Document outline (use `read_file` with line ranges to read sections):
    L1: Quarterly Revenue Report
    L12: Methodology
    ...
- data.csv (8.2 KB)
  Path: /mnt/user-data/uploads/data.csv
  No structural headings detected. Document begins with:
    > Region,Revenue,Q1 2024
    > North,$1.2M,...
  Use `grep` to search for keywords ...

The following files were uploaded in previous messages and are still available:
...
... (3 more historical file(s) omitted from this context.)
  Omitted file types: 2 .pdf, 1 .xlsx
  Use `glob(pattern='**/*', path='/mnt/user-data/uploads/')` to list all uploads.
  Use `grep(pattern='keyword', path='/mnt/user-data/uploads/')` to search across uploads.

To work with these files:
- Read from the file first — use the outline line numbers and `read_file` to locate relevant sections.
- Use `grep` to search for keywords when you are not sure which section to look at ...
- Use `glob` to find files by name pattern ...
- Only fall back to web search if the file content is clearly insufficient to answer the question.
</uploaded_files>
```

**关键细节**：
- size 格式化：`< 1024 KB` 显示 KB，否则显示 MB。
- outline 截断时会在末尾加 sentinel `{"truncated": True}`，格式化时只显示非 truncated 的条目，并追加"showing first N headings; use `read_file` to explore further"。
- 没有大纲但有 preview 时，输出"No structural headings detected. Document begins with:" + `> ` 前缀的几行内容。
- 没有大纲也没有 preview 时，输出"Use `grep` ..."引导。
- omitted 列表的类型用 `Counter` 聚合，如"2 .pdf, 1 .xlsx"。
- 最后一段操作指引是固定的——教模型先 `read_file` / `grep` / `glob`，最后才回退 web search。

#### 7. 拼接到最新 HumanMessage

```python
original_content = last_message.content
additional_kwargs = dict(last_message.additional_kwargs or {})
original_user_content = additional_kwargs.get(ORIGINAL_USER_CONTENT_KEY)
if not isinstance(original_user_content, str):
    if ORIGINAL_USER_CONTENT_KEY in additional_kwargs:
        logger.warning(
            "UploadsMiddleware replaced non-string %s metadata: type=%s",
            ORIGINAL_USER_CONTENT_KEY, type(original_user_content).__name__,
        )
    additional_kwargs[ORIGINAL_USER_CONTENT_KEY] = message_content_to_text(original_content)
```

**`ORIGINAL_USER_CONTENT_KEY = "original_user_content"`** 是溯源契约：sanitize 之前保留的用户原始文本。若已存在且是字符串就保留（first-writer-wins 语义）；若是非字符串值（旧数据/异常数据）则覆盖并 warning。

接下来按 content 类型分支处理：

```python
if isinstance(original_content, str):
    updated_content = f"{files_message}\n\n{original_content}"
elif isinstance(original_content, list):
    files_block = {"type": "text", "text": f"{files_message}\n\n"}
    updated_content = [files_block, *original_content]
else:
    updated_content = original_content
```

- 字符串：简单前置拼接。
- list（多模态，如带图片）：把 `<uploaded_files>` 包装成一个 text block 放到最前，**保留所有原 block**（图片等）。
- 其它类型：原样不动（防御性）。

最后构造新消息并返回：

```python
updated_message = HumanMessage(
    content=updated_content,
    id=last_message.id,             # 保留 id，保证 dedup 通道稳定
    name=last_message.name,
    additional_kwargs=additional_kwargs,  # 含 files 元数据 + 原始 content 备份
)
messages[last_message_index] = updated_message

return {"uploaded_files": new_files, "messages": messages}
```

- 保留 `id` 让 LangGraph 的 `AddMessages` reducer 能识别为对同一条消息的更新，而不是新增。
- `additional_kwargs` 保留 `files` 元数据，前端可以从流式消息里读取结构化文件信息（路径、大小）用于 UI 展示。
- `uploaded_files` 写到 state 的同时，**只回写新文件**（不含历史），避免历史文件清单无限膨胀。

### `abefore_agent` 异步钩子

```python
@override
async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
    return await run_in_executor(None, self.before_agent, state, runtime)
```

**关键设计**：`before_agent` 是同步函数，里面做了**阻塞文件系统 IO**（目录枚举、`stat`、读取 `.md` 大纲）。当 graph 在 async 模式下运行时，LangGraph 默认会把同步 `before_agent` 直接放到事件循环上执行，从而阻塞整个循环。

`run_in_executor(None, ...)` 把整段同步逻辑扔到默认 `ThreadPoolExecutor` 的 worker 线程上执行。`run_in_executor` 会**复制当前 ContextVar context**，所以 `_current_user`（`get_effective_user_id()` 读的那个）在 worker 线程里仍然可读——这是 DeerFlow 整套 user_id 上下文传播能在异步 + 线程池混合模式下工作的关键一环。

这个 offload 被 `tests/blocking_io/test_uploads_middleware.py` 锁定为回归测试（Blockbuster 会检测 `app.*` 与 `deerflow.*` 在事件循环上的同步阻塞 IO）。

## 关键设计决策

### 1. 新文件 vs 历史文件的双段处理

把当前消息上传和历史已上传分成两段，各有独立的 `max_files_per_context_section` 上限和 omitted 提示。这避免了"新上传 5 个文件挤掉所有历史文件"或"历史文件太多把新文件挤到后面"两类问题——新文件永远在第一段展示。

### 2. query 相关性排序

历史文件可能很多，盲目塞给模型会浪费 token 并分散注意力。按 query 匹配度排序 + 三档匹配（filename/stem/token/extension）让最可能被引用的文件出现在 prompt 最显眼位置。`selection_reason="query_match"` 还会显式告诉模型"为什么选了它"，减少模型自己猜测的成本。

### 3. sibling `.md` outline 而非重新解析原文件

上传时 Gateway 已经把 PDF/PPT/Excel/Word 转成同名 `.md` 放在 uploads 目录旁边。这里直接读 `.md` 抽大纲，避免在热路径上重复跑 `markitdown`（毫秒级 → 秒级）。`.md` 不存在或没大纲时退化到前 5 行预览，保证无结构文档仍有起点。

### 4. first-writer-wins 的 `original_user_content`

`ORIGINAL_USER_CONTENT_KEY` 是一个跨中间件契约：`InputSanitizationMiddleware` 在 sanitize 前先写原始文本，`UploadsMiddleware` 只在非字符串时覆盖并 warning。这样多个中间件可以串行修改 `content` 而**原始文本不丢失**——对记忆抽取、loop 检测、title 生成都需要真实用户文本。

### 5. 虚拟路径 `/mnt/user-data/uploads/...` 而非宿主路径

发给模型的 `path` 字段永远是虚拟路径。模型在沙箱内调 `read_file`/`grep`/`glob` 用的就是这些路径，工具层负责翻译到宿主或容器挂载点。这让 prompt 不依赖部署模式（local/AIO/Boxlite），可移植。

## 与其他中间件的协作

- **ThreadDataMiddleware**：依赖它把 `thread_data.uploads_path` 算好（虽然 `UploadsMiddleware` 自己也通过 `Paths.sandbox_uploads_dir` 重新解析，但 state 里的值可供其他消费者使用）。
- **InputSanitizationMiddleware**：在 `UploadsMiddleware` 之前运行（outer），写 `original_user_content`。`UploadsMiddleware` 读取它做 query 匹配，并在自己修改 content 时保留它。
- **SandboxMiddleware**：上传中间件生成的虚拟路径 `/mnt/user-data/uploads/...` 由沙箱 provider 在 `acquire` 时通过 `PathMapping` 翻译，或由 AIO 容器卷挂载到同一虚拟路径。
- **SkillActivationMiddleware / DynamicContextMiddleware**：这些中间件也修改最新 HumanMessage 的 content；和 Uploads 一起组成 content 前置/附加块链。由于 Uploads 只在最后一条 HumanMessage 上加 `<uploaded_files>` 块，其它中间件加 `<system-reminder>`/`<skill>` 块时不冲突——只要都保留原始 block 列表即可。
- **MemoryMiddleware**：读 `original_user_content` 抽取记忆时，Uploads 已修改的 content 不会污染，因为它优先读 `additional_kwargs` 里的原始文本。

---

# 3. SandboxMiddleware

## 概述

管理沙箱的生命周期：在 agent 启动或第一次工具调用时获取（acquire）沙箱、把 `sandbox_id` 存入 state，在 agent 结束时释放（release）。通过 `wrap_tool_call` 钩子捕获沙箱工具的**懒初始化副作用**，并用 `Command(update=...)` 把结果持久化到 graph state。

## 为什么需要这个中间件

### 场景痛点

没有这个中间件，每个 sandbox 工具（bash、read_file、write_file）都需要各自获取和释放沙箱——更糟的是可能没有人释放，Docker 容器或 Boxlite VM 一路泄漏直到系统资源耗尽。并发工具调用可能为同一个线程创建多个沙箱，破坏文件隔离。沙箱的 `sandbox_id` 被某个工具获取后，下游消费者（`ToolOutputBudgetMiddleware`、`ReadBeforeWriteMiddleware`、审计日志）完全不知道它——因为它只存在于工具调用期间的本地可变对象里，LangGraph 的 channel reducer 看不到这个变更。

### 为什么模型自身无法避免

LLM 一次只发出一个工具调用，没有资源生命周期管理的概念——它不知道何时应该在第一次 bash 前 acquire 沙箱，也不知道何时应该在最后一次工具后 release。让模型管理沙箱生命周期既不可靠（可能忘记 release），又浪费 token 去操心基础设施问题。sandbox_id 需要被持久化到 graph state 供多个下游中间件消费，这是模型无法也无需参与的。

### 解决思路

用 `before_agent`/`after_agent` 在正确时机 acquire/release 沙箱（支持 eager 和 lazy 两种模式），并用 `wrap_tool_call` 对比每次工具调用前后的 sandbox_id 变化，通过 `Command(update=...)` 将懒初始化获取的 sandbox_id 持久化到 graph state。

## 元信息

| 属性 | 值 |
|------|-----|
| 文件路径 | `packages/harness/deerflow/sandbox/middleware.py` |
| 实现的钩子 | `before_agent` / `abefore_agent` / `after_agent` / `aafter_agent` / `wrap_tool_call` / `awrap_tool_call` |
| 持久化 | State (`sandbox` 字段，使用 `merge_sandbox` reducer)；`Command(update=...)` 在工具调用返回时 |
| 配置依赖 | `lazy_init` 构造参数；`get_sandbox_provider()`（全局 provider） |
| State Schema | `SandboxMiddlewareState`（声明 `sandbox: SandboxStateField` + `thread_data: NotRequired[ThreadDataState | None]`） |

## 核心逻辑

### State Schema 与 reducer

```python
class SandboxMiddlewareState(AgentState):
    sandbox: SandboxStateField
    thread_data: NotRequired[ThreadDataState | None]
```

`SandboxStateField = Annotated[NotRequired[SandboxState | None], merge_sandbox]`。`merge_sandbox` 是关键 reducer：

```python
def merge_sandbox(existing, new) -> SandboxState | None:
    if new is None:
        return existing
    if existing is None:
        return new
    existing_id = existing.get("sandbox_id")
    new_id = new.get("sandbox_id")
    if existing_id == new_id:
        return existing
    raise ValueError(f"Conflicting sandbox state updates: {existing_id!r} != {new_id!r}")
```

**幂等写入 + 冲突 fail-closed**：多个工具可能在同一 graph step 懒初始化沙箱并都 emit `Command(update={"sandbox": ...})`。只要 id 一致就接受；id 不一致就抛异常——因为这通常意味着生命周期/隔离 bug，静默选一个会导致后续工具用错沙箱。

### 构造

```python
def __init__(self, lazy_init: bool = True):
    super().__init__()
    self._lazy_init = lazy_init
```

`lazy_init=True`（默认）：不在 `before_agent` 里 acquire；把 acquire 推迟到**第一次工具调用**（由沙箱工具自身的 `ensure_sandbox_initialized` 触发）。

### `before_agent` / `abefore_agent`

```python
@override
def before_agent(self, state, runtime: Runtime) -> dict | None:
    if self._lazy_init:
        return super().before_agent(state, runtime)

    # Eager 初始化（旧行为）
    if "sandbox" not in state or state["sandbox"] is None:
        thread_id = (runtime.context or {}).get("thread_id")
        if thread_id is None:
            return super().before_agent(state, runtime)
        sandbox_id = self._acquire_sandbox(thread_id, user_id=resolve_runtime_user_id(runtime))
        logger.info(f"Assigned sandbox {sandbox_id} to thread {thread_id}")
        return {"sandbox": {"sandbox_id": sandbox_id}}
    return super().before_agent(state, runtime)
```

- **lazy_init=True**：直接调 `super().before_agent()` 返回 `None`，不动 state。沙箱留给工具层 acquire。
- **lazy_init=False（eager）**：
  - 仅当 state 里还没有 sandbox 时才 acquire（幂等）。
  - `thread_id` 缺失时不 acquire（调 super）——因为沙箱 provider 的 `acquire(thread_id)` 依赖 thread_id 做隔离，没有 thread_id 不能瞎分配。
  - 调用同步 `_acquire_sandbox(thread_id, user_id=resolve_runtime_user_id(runtime))`。

`abefore_agent` 镜像同步逻辑，但用 `_acquire_sandbox_async`（调用 provider 的 `acquire_async`），让 Docker/Boxlite 沙箱的创建/发现/轮询等阻塞操作不占事件循环：

```python
@override
async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
    if self._lazy_init:
        return await super().abefore_agent(state, runtime)
    if "sandbox" not in state or state["sandbox"] is None:
        thread_id = (runtime.context or {}).get("thread_id")
        if thread_id is None:
            return await super().abefore_agent(state, runtime)
        sandbox_id = await self._acquire_sandbox_async(thread_id, user_id=resolve_runtime_user_id(runtime))
        logger.info(f"Assigned sandbox {sandbox_id} to thread {thread_id}")
        return {"sandbox": {"sandbox_id": sandbox_id}}
    return await super().abefore_agent(state, runtime)
```

`resolve_runtime_user_id(runtime)` 是工具/中间件层的用户身份解析"单一真相源"，解析顺序：`runtime.context["user_id"]`（Gateway 注入）→ ContextVar → `"default"`。

### `after_agent` / `aafter_agent`

```python
@override
def after_agent(self, state, runtime: Runtime) -> dict | None:
    sandbox = state.get("sandbox")
    if sandbox is not None:
        sandbox_id = sandbox["sandbox_id"]
        logger.info(f"Releasing sandbox {sandbox_id}")
        get_sandbox_provider().release(sandbox_id)
        return None

    if (runtime.context or {}).get("sandbox_id") is not None:
        sandbox_id = runtime.context.get("sandbox_id")
        logger.info(f"Releasing sandbox {sandbox_id} from context")
        get_sandbox_provider().release(sandbox_id)
        return None

    return super().after_agent(state, runtime)
```

**释放优先级**：
1. 先看 state 里的 `sandbox.sandbox_id`——这是本中间件 / 工具调用 acquire 的。
2. state 没有就看 `runtime.context["sandbox_id"]`——这是外部预先注入的沙箱 id（比如测试或自定义运行路径通过 context 传进来的）。
3. 都没有就调 super 返回 `None`，不动任何资源。

`aafter_agent` 镜像，但用 `_release_sandbox_async`：

```python
async def _release_sandbox_async(self, sandbox_id: str) -> None:
    await asyncio.to_thread(get_sandbox_provider().release, sandbox_id)
```

用 `asyncio.to_thread` 把同步 `provider.release` 扔到线程池，避免在事件循环上做阻塞 IO（沙箱释放可能涉及 Docker container stop、文件清理等）。

**关键细节**：`after_agent` 返回 `None` 而非 `{"sandbox": None}`。这意味着 state 里的 `sandbox_id` 在 agent 调用结束后**仍然保留**（reducer 的 `new=None` 分支 `return existing`）。沙箱本身已被 release（provider 端清理），但 id 留在 state 里供 observability 和日志使用。这是刻意的——provider 的 `release` 是幂等的，下次 acquire 会重新分配新 id。

### `wrap_tool_call` / `awrap_tool_call`——懒初始化的持久化

这是整个中间件最巧妙的部分。源码注释：

```python
# ------------------------------------------------------------------
# Tool-call wrappers: persist lazily-acquired sandbox state into the
# graph state via Command(update=...).
#
# Background:
#   ``ensure_sandbox_initialized*`` in ``deerflow.sandbox.tools`` mutates
#   ``runtime.state["sandbox"]`` directly. That mutation is local to the
#   current tool invocation and is NOT picked up by LangGraph's channel
#   reducer, so subsequent graph steps (and downstream consumers such as
#   ``ToolOutputBudgetMiddleware`` and the sub-agent ``task_tool``)
#   cannot observe the sandbox id. Wrapping the tool call lets us detect
#   a fresh lazy init by diffing the state snapshot before/after the
#   handler and emit a proper state update via ``Command``.
# ------------------------------------------------------------------
```

**问题背景**：沙箱工具（`bash`/`read_file`/`write_file` 等）在执行前会调用 `ensure_sandbox_initialized`，它直接修改 `runtime.state["sandbox"]`。但 `runtime.state` 是工具调用期间的**本地可变对象**，LangGraph 的 channel reducer 看不到这个变更，所以下一个 graph step、`ToolOutputBudgetMiddleware`、子代理 `task_tool` 都不知道沙箱已经被分配。

**解决方案**：用 `wrap_tool_call` 包装所有工具调用，**前后对比** `runtime.state["sandbox"].sandbox_id`：

```python
@staticmethod
def _read_sandbox_id_from_state(state: object) -> str | None:
    if not isinstance(state, dict):
        return None
    sandbox_state = state.get("sandbox")
    if not isinstance(sandbox_state, dict):
        return None
    sandbox_id = sandbox_state.get("sandbox_id")
    return sandbox_id if isinstance(sandbox_id, str) else None

@staticmethod
def _read_sandbox_id_from_request(request: ToolCallRequest) -> str | None:
    runtime = request.runtime
    if runtime is None or runtime.state is None:
        return None
    return SandboxMiddleware._read_sandbox_id_from_state(runtime.state)

@override
def wrap_tool_call(self, request, handler) -> ToolMessage | Command:
    prev_sandbox_id = self._read_sandbox_id_from_request(request)
    result = handler(request)
    if prev_sandbox_id is not None:
        return result
    curr_sandbox_id = self._read_sandbox_id_from_request(request)
    if curr_sandbox_id is None:
        return result
    return self._attach_sandbox_update(result, curr_sandbox_id)
```

**逻辑**：
1. 执行 handler 前读一次 `sandbox_id`（`prev`）。
2. 执行 handler（工具本体），期间 `ensure_sandbox_initialized` 可能 acquire 新沙箱并写入 `runtime.state["sandbox"]`。
3. 执行后读一次 `sandbox_id`（`curr`）。
4. 如果 `prev` 非 None：说明执行前已有沙箱，工具内部没新建，无需更新 state，直接返回 result。
5. 如果 `prev` 是 None 但 `curr` 非 None：**这是新建沙箱的情况**，需要把新 id 持久化到 graph state。
6. 调 `_attach_sandbox_update(result, curr_sandbox_id)` 把 update 合并进 result。

### `_attach_sandbox_update`——合并 update 到 ToolMessage / Command

```python
@staticmethod
def _attach_sandbox_update(result: ToolMessage | Command, sandbox_id: str) -> ToolMessage | Command:
    sandbox_update = {"sandbox": {"sandbox_id": sandbox_id}}

    if isinstance(result, ToolMessage):
        return Command(update={**sandbox_update, "messages": [result]})

    existing_update = result.update
    if isinstance(existing_update, dict):
        merged_update = {**existing_update, **sandbox_update}
        return dc_replace(result, update=merged_update)
    return result
```

**三种情况**：
- **返回的是 ToolMessage**：包装成 `Command(update={"sandbox": ..., "messages": [msg]})`。LangGraph 接到 Command 会把 update 应用到 channels——`sandbox` 走 `merge_sandbox` reducer，`messages` 走 `AddMessages` reducer。
- **返回的是 Command 且 update 是 dict**：用 `dataclasses.replace` 创建一个新 Command，update 字段合并 `sandbox` key 进去，**保留**所有原字段（`messages`/`goto`/`graph`/`resume` 等）。
- **返回的是 Command 但 update 非 dict / None**：**原样返回**，不动。注释说明这是为了"避免在未知 update shape 上静默丢数据"——如果工具返回了一个奇怪形态的 Command，宁可丢失 sandbox update 也不要破坏工具本身的语义。

`awrap_tool_call` 镜像同步版本，区别是 `await handler(request)`。

## 关键设计决策

### 1. 默认 lazy_init=True 的原因

源码注释：

> Lifecycle Management:
> - With lazy_init=True (default): Sandbox is acquired on first tool call
> - With lazy_init=False: Sandbox is acquired on first agent invocation (before_agent)
> - Sandbox is reused across multiple turns within the same thread
> - Sandbox is NOT released after each agent call to avoid wasteful recreation
> - Cleanup happens at application shutdown via SandboxProvider.shutdown()

**Trade-off**：
- lazy：agent 只做对话（没有工具调用）时不分配沙箱，省资源。Docker/Boxlite 沙箱创建可能要秒级，懒分配把成本分摊到真正需要的工具调用。
- eager：`before_agent` 里同步 acquire，agent 启动前就有沙箱。适用于确定会用工具、希望首工具调用零延迟的场景。

默认 lazy 的另一个考量：**scheduled task / 非交互运行**（`context.non_interactive=true`）在 plan-only 或纯对话场景下不需要沙箱，eager 模式会浪费。

### 2. `merge_sandbox` 的 fail-closed 设计

多个工具并发 acquire 沙箱理论上应该返回同一个 id（沙箱 per-thread 缓存），reducer 检测到不同 id 直接抛异常。这比"静默选一个"更安全——如果真出现了不同 id，意味着沙箱隔离边界被破坏，后续工具可能读到错误的文件系统状态。抛异常让 bug 立即可见，而不是埋下数据串扰的雷。

### 3. `wrap_tool_call` 而非修改 `ensure_sandbox_initialized`

可以在 `ensure_sandbox_initialized` 里直接 `Command(update=...)`，但那需要每个工具都返回 Command 而非 ToolMessage，破坏工具协议的统一性。把持久化逻辑放到中间件的 `wrap_tool_call` 里，**对所有沙箱工具透明**——`bash`/`read_file`/`write_file`/`str_replace` 等不用知道 sandbox state 如何持久化。

### 4. `after_agent` 释放但保留 state id

释放 provider 资源（Docker container / Boxlite VM）但不擦除 state 里的 `sandbox_id`。这样：
- 下一次 acquire 会重新分配（provider 端是幂等的）。
- 历史日志、tracing、UI 都能看到这次 run 用过哪个沙箱。
- 如果 reducer 把 `sandbox_id` 擦成 None，反而丢失了审计信息。

### 5. 通过 `asyncio.to_thread` 而非 `run_in_executor` 释放沙箱

`UploadsMiddleware.abefore_agent` 用 `run_in_executor`（LangChain 的工具），这里用 `asyncio.to_thread`（标准库）。两者效果类似——都把同步函数扔到线程池。`asyncio.to_thread` 更轻、更标准，且不需要 LangChain 依赖。

### 6. `_attach_sandbox_update` 对未知 Command shape 原样返回

注释明确：

> Command with non-dict / None update -> leave it untouched to
> avoid silent data loss on unknown update shapes.

未来可能有新形态的 Command（例如 update 是 Pydantic model 或其它结构）。与其猜测如何合并，不如放弃 sandbox update——下次工具调用还会再 acquire（provider 的 active map 里有），最坏情况是重复 acquire 一次。

## 与其他中间件的协作

- **ThreadDataMiddleware**：`SandboxMiddlewareState` 也声明 `thread_data: NotRequired[ThreadDataState | None]`，和 `ThreadDataMiddleware` 共享 schema。沙箱 provider `acquire(thread_id)` 内部会调用 `Paths.ensure_thread_dirs` 创建 workspace/uploads/outputs 目录——这就是 lazy_init=True 模式下目录真正被创建的地方。
- **UploadsMiddleware**：Uploads 生成的虚拟路径 `/mnt/user-data/uploads/...` 依赖沙箱 provider 把虚拟路径翻译到宿主或容器挂载点。`LocalSandboxProvider.acquire` 时构造 `PathMapping`，AIO 容器直接卷挂载。
- **ToolOutputBudgetMiddleware**：注释明确提到它"cannot observe the sandbox id"——`wrap_tool_call` 的修复让 `sandbox_id` 能被下游消费者读到，`ToolOutputBudgetMiddleware` 在工具输出超限时可以正确归属到具体沙箱。
- **SubagentExecutor / task_tool**：子代理通过 `task` 工具启动时也会 acquire 自己的沙箱。`wrap_tool_call` 的持久化让 lead agent 的 state 能看到子代理创建的 sandbox id（如果子代理复用父 sandbox 则不变）。子代理 graph 编译时 `checkpointer=False`，但其 `runtime.context` 仍携带 `thread_id`，沙箱隔离边界由 provider 保证。
- **SandboxAuditMiddleware**：审计沙箱操作前需要知道当前 sandbox id。`wrap_tool_call` 保证在审计中间件运行前 state 里的 sandbox id 已被持久化（如果新建过）。
- **ReadBeforeWriteMiddleware**：依赖 `sandbox_id` 对同一路径的读写 gate 做同 sandbox 序列化（`(sandbox.id, path)` 范围）。`wrap_tool_call` 让它读到正确的 sandbox id。
- **SandboxProvider.shutdown()**：应用关闭时统一清理所有 active + warm 沙箱。中间件的 `after_agent` 只 release 当前 run 的沙箱，provider 的 warm pool 仍持有已释放的沙箱供下次快速 reuse。

