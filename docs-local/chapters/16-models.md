# 第 16 章　模型配置与适配层：从 config.yaml 到 LLM 调用

> 基于 DeerFlow 最新源码（本仓库 commit 11b339d6，2026-10，版本 2.2.0-dev）编写。
>
> 本章所有路径、类名、配置项均以当前源码为准。全章核心代码集中在 `backend/packages/harness/deerflow/models/`（模型工厂、provider 适配层，以及 `reasoning.py` 推理契约与 `request_admission.py` 请求准入两个新模块）、`backend/packages/harness/deerflow/config/model_config.py`（模型配置 schema）与仓库根 `config.example.yaml`（配置模板）。模型授权横跨 `backend/app/gateway/routers/models.py`、`backend/app/gateway/authz.py`、`backend/packages/harness/deerflow/agents/lead_agent/agent.py` 与 `backend/packages/harness/deerflow/authz/`（RBAC 与 skill/sandbox/plugin 授权）。

## 本章导航

DeerFlow 里"模型"不是一个对象，而是一条约五层深的链路：**config.yaml 的 `models:` 段（声明）→ `ModelConfig`（schema 校验，含可选的 `reasoning:` 契约与 `request_admission:` 准入）→ `create_chat_model` 工厂（合并元数据、归一化推理请求、注入适配参数、实例化）→ provider 适配器（补丁层，负责与各家 API 的方言打交道）→ 运行时解析与授权（`model:use` 决定这条链最终为谁服务）**。本章按这条链路逐层拆开。

| 小节 | 内容 | 对应权威依据 |
|------|------|--------------|
| 16.1 配置来源 | `models:` 平铺段、`ModelConfig` 元数据、`reasoning:` 契约与 `request_admission:` 准入、`$ENV` 凭据解析、端点键归一化；旧概念（providers/custom_providers/key_env/api_mode）校准 | `config/model_config.py`、`config/app_config.py`、`models/factory.py` |
| 16.2 模型名解析 | 默认模型（`models[0]`）、运行时 `model_name` 覆盖与回退 | `agents/lead_agent/agent.py:_resolve_model_name` |
| 16.3 工厂构造流程 | `create_chat_model` 的构造管线（legacy 与契约两条分支） | `models/factory.py`（全文 625 行） |
| 16.4 能力矩阵 | thinking 契约 / vision / reasoning_effort / context_window / stream_usage / RPM 准入 / 超时 | `factory.py`、`models/reasoning.py`、`models/request_admission.py` + `agent.py` |
| 16.5 适配补丁为什么 | 静默丢字段 → 要求逐字回显的 provider 拒收；`assistant_payload_replay` 共享回灌；patch 一句话表 | `models/patched_*.py`、`models/*_provider.py`、`models/assistant_payload_replay.py` |
| 16.6 配置示例 | deepseek / openrouter / 自定义端点 / 本地 vLLM 完整模型段 | `config.example.yaml` models 段注释块 |
| 16.7 模型授权 | `authorization` 段、Gateway 路由层 `model:use`、运行时 `_authorize_model_name` 与 fallback 扫描、内置 RBAC（model/tool/skill/sandbox/plugin 资源） | `config/authorization_config.py`、`app/gateway/routers/models.py`、`agent.py:_authorize_model_name`、`authz/rbac.py`、`authz/skill_filter.py`、`authz/plugin_authz.py` |
| 16.8 小结 | 概念 → 文件 → 关键位置速查表 | — |

---

## 16.1　模型配置来源：一份 `models:`，多家方言

### 16.1.1 顶层布局：`models:` 平铺段

2.2.0 的模型配置只有一个入口：仓库根 `config.example.yaml` 的顶层 `models:` 列表（复制为 gitignored 的 `config.yaml`）。每一项就是一条"模型档案"，`use` 字段声明由哪个 provider 类来实例化，其余字段声明模型元数据与构造参数。以模板中 DeepSeek 的注释示例为原型（`config.example.yaml` 约 490-506 行）：

```yaml
models:
  - name: deepseek-v4
    display_name: DeepSeek V4 (Thinking)
    use: deerflow.models.patched_deepseek:PatchedChatDeepSeek   # 类路径：模块:类
    model: deepseek-v4-pro                                      # 发给 provider 的模型 id
    api_key: $DEEPSEEK_API_KEY                                  # $前缀 → 环境变量
    timeout: 600.0
    max_retries: 2
    max_tokens: 8192                 # 单次调用输出上限（与 context_window 是两回事）
    context_window: 131072           # 总上下文容量（prompt + completion）
    supports_thinking: true
    supports_vision: false
    when_thinking_enabled:
      extra_body:
        thinking:
          type: enabled
    when_thinking_disabled:
      extra_body:
        thinking:
          type: disabled
```

**"配置来源"在 2.2.0 只有一个形状：`models:` 列表 + `use` 类路径。** 如果你手上的旧资料（2.0 早期结构或 v1 文档）里还有 `providers:` / `custom_providers:` 顶层段、`key_env` 键或 `api_mode` 键——在本仓库当前代码中**全部检索零命中**。它们的职责已被折叠进 `models:` 条目本身，对应关系如下，后面小节逐个展开：

| 旧概念 | 2.2.0 的真实对应物 | 证据 |
|--------|--------------------|------|
| "内置 provider" | 仓库自带、位于 `deerflow.models.*` 的适配器类（`PatchedChatDeepSeek`、`PatchedChatOpenAI`、`VllmChatModel`、`MindIEChatModel`、Claude/Codex 自定义类等），通过 `use:` 引用 | `models/` 目录 15 个 `.py` 文件 |
| `providers:`/`custom_providers:` 段 | 不存在顶层段；每个模型条目里的 `use` 类路径即 provider 声明，同一 provider 可被多个条目复用 | `config.example.yaml`（多个模型共用 `PatchedChatDeepSeek`/`ChatOpenAI`） |
| `key_env` | `api_key: $ENV_VAR`——配置加载期递归解析（见 16.1.2）；仓库中残留的 `api_key_env` 字样只出现在记忆后端（memory/backends/openviking）配置，与模型无关 | `config/app_config.py:resolve_env_variables` |
| `api_mode` | 没有该键；"走哪套协议"由 `use` 类路径决定，另有两个补丁性开关 `use_responses_api` / `output_version` 残留在 `ModelConfig` 上供 OpenAI-compatible 适配器消费 | `config/model_config.py:145-152`、`patched_minimax.py:156` |

校准提醒：**写配置时不要凭旧记忆找段名**——顶层只有 `models:`，段内没有 `provider:`/`key_env:`/`api_mode:` 这些键（`ModelConfig` 是 `extra="allow"`，写错的键不会在加载期报错，只会在请求期以诡异方式爆炸，见 16.3 第 16 步的防呆警告）。

### 16.1.2 `ModelConfig` schema：一条模型的元数据面

每条模型的字段由 `backend/packages/harness/deerflow/config/model_config.py`（269 行）的 `ModelConfig`（pydantic `BaseModel`）定义。它只声明**通用字段**，并故意设置 `model_config = ConfigDict(extra="allow")`（第 144 行）——provider 特有键（`api_base`、`extra_body`、`temperature`、`max_tokens`……）不逐一建模，靠 extra 放行后直通 provider 构造器。声明的字段分四类：

**身份与路由：**
- `name`（必填）：DeerFlow 内部唯一名，是 `get_model_config(name)`、运行时 `model_name`、授权目标用的标识符；
- `use`（必填）：provider 类的 `模块路径:类名`，交给反射解析（16.3 第 2 步）；
- `model`（必填）：真正发给 provider 的模型 id，如 `deepseek-v4-pro`、`google/gemini-2.5-flash-preview`；
- `display_name` / `description`：前端展示与列表接口输出用。

**能力元数据（不进模型构造器，见工厂的 exclude 集合）：**
- `supports_thinking`：是否支持思考模式；
- `supports_reasoning_effort`：是否支持 reasoning effort 参数；
- `reasoning`：**映射值时为声明式推理契约**（见下），布尔或字符串时保留旧式的原生 provider 值（如 `ChatOllama` 的 `reasoning: true`、gpt-oss 的 `reasoning: high`）；
- `supports_vision`：是否支持图像输入；
- `when_thinking_enabled` / `when_thinking_disabled`：开启/关闭思考时额外注入的构造参数（通常是一组 `extra_body`）；
- `thinking`：`when_thinking_enabled` 的"快捷方式"，语义等价于设置 `when_thinking_enabled["thinking"]`，两者同时给时在工厂里深合并；
- `context_window`：正整数的总上下文容量（prompt + completion），**与 `max_tokens`（单次输出上限）严格区分**（模板 132-142 行有专门注释）；
- `stream_chunk_timeout`：流式相邻 chunk 最大间隔秒数，`None` 时用工厂默认 240s（16.4）；
- `pricing`：`extra="allow"` 放行的展示性块（`currency`、`input_per_million` 等），只供 console 成本显示，**绝不允许进入 provider 请求**；
- `use_responses_api` / `output_version`：让仍用 `langchain_openai:ChatOpenAI` 的配置显式声明走 `/v1/responses` 相关行为、或声明结构化输出版本（`responses/v1`）的兼容性标记（`backend` config `AGENTS.md` 如此定义其动机）。二者不在工厂的 exclude 名单里，会随 settings 直通构造器——目标类声明同名字段就生效；DeerFlow 侧可见的实际消费是 `output_version`：`patched_minimax.py:156`、`vllm_provider.py:288` 在 `output_version == "v1"` 时把版本写进 chunk 的 `response_metadata`。模板另有一项 `use_previous_response_id`（默认 false，随 settings 透传给 `ChatOpenAI` 的 Responses 路径）：置 true 时只发本轮加 `previous_response_id`，而不重放全量历史——连锁上下文仍按 input token 计费，且任何客户端侧的历史改写（如被阻塞写负载的 elide）只有在重放历史时才生效（模板 322-327 行）。

**`reasoning:` 声明式契约（映射值，2.2.0 新增）：** 当 `reasoning:` 是一个映射（而非 `true`/字符串）时，`ReasoningCapabilities` 生效，`supports_thinking` / `supports_reasoning_effort` 两个布尔从中**派生**（见 16.4.1）。字段：`thinking`（`unsupported`/`optional`/`required`，默认 `unsupported`）、`on_disable_request`（`keep_enabled`/`reject`，仅 `required` 有意义）、`dialect`（`auto`/`openai_extra_body`/`anthropic`/`vllm_chat_template`/`ollama`/`none`，默认 `auto`）、`history`（`preserve`/`clear`）、`effort`（`ReasoningEffortCapabilities`：`values` 显示序词表、`default`、`aliases`（DeerFlow 通用值→provider 值映射）、`path` dotted 序列化路径，默认 `reasoning_effort`）。`_validate_reasoning_contract`（第 213-269 行）在加载期做交叉校验——`required` 不得配 `when_thinking_disabled`、`unsupported` 不得配任何 thinking 模板、两个旧布尔若被显式设置必须与派生值一致、`default`/`aliases` 必须落在 `values` 内、profile 与 thinking 模板里出现的 effort 值必须被契约接受——互相矛盾会在**启动期**就报错，而非请求期。

**请求准入（`request_admission`，不进构造器）：**
- `request_admission`：可选的 `RequestAdmissionConfig`——`requests_per_minute`（必填，>0，支持 `$VAR` 整数字面量替换）、`group`（可选的共享配额名，未给则按模型名分桶）、`max_wait_seconds`（默认 300）、`max_queue_size`（默认 256）。工厂在构造期把它翻译成一个进程内 `BaseRateLimiter`（见 16.4.4）。

### 16.1.3 凭据注入：`$` 环境变量解析 + CLI 凭据自动装载

配置里**永远不该出现明文密钥**。两条注入路径：

**(a) `api_key: $VAR`——`AppConfig.resolve_env_variables`（`config/app_config.py:556-575`）。** 配置加载期对整棵配置树做递归解析：任何**字符串**若以 `$` 开头，就当作环境变量名 `os.getenv` 取真实值；取不到直接抛 `ValueError(f"Environment variable {name} not found for config value {config}")`——**fail-fast，不会带着空密钥静默启动**。注意解析发生在字符串层，所以 `api_key: $DEEPSEEK_API_KEY` 是唯一推荐写法；也意味着不能用 `$` 表达字面量。

**(b) CLI 凭据自动装载——`models/credential_loader.py`（269 行）。** 面向两条"本地 CLI 即密钥源"的路径：Claude Code（`~/.claude/.credentials.json` 或 `CLAUDE_CODE_OAUTH_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` / `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_CREDENTIALS_PATH`）与 Codex CLI（`~/.codex/auth.json`，兼容新旧两种 token 嵌套结构，`CODEX_AUTH_PATH` 可覆盖）。OAuth token 以 `sk-ant-oat` 前缀识别，带过期时间检查（提前 60s 视为过期并告警）。装载结果喂给 `claude_provider.py`（`Authorization: Bearer`，须带 `anthropic-beta: oauth-2025-04-20,claude-code-20250219` 系列头）与 `openai_codex_provider.py`。用这两家时 `models:` 条目里可以不给 `api_key`，靠本地 CLI 登录态自动补。

### 16.1.4 端点键：`api_base` / `base_url` 两种方言与归一化

端点键是适配层最容易踩的坑，因为**不同基类认不同的键**：

- `langchain_deepseek.ChatDeepSeek`（及 `PatchedChatDeepSeek`）在 pydantic `model_fields` 里**自己声明了 `api_base`**，所以对它而言 `api_base` 是规范键（模板里 doubao/deepseek/kimi 条目都写 `api_base: https://...`）；
- 其余 `BaseChatOpenAI` 子类只继承 `openai_api_base`（别名 `base_url`）——**不认识 `api_base`**。

工厂里的 `_normalize_openai_base_url`（`factory.py:45-73`）专门治"用户把 `api_base` 误抄到 OpenAI 兼容模型上"：因为 `ModelConfig` 是 `extra="allow"`，错键不会在加载期被拦，会被转发进构造器、被 LangChain 转移进 `model_kwargs`、再在**每次请求期**被 OpenAI SDK 以 `unexpected keyword argument 'api_base'` 拒绝——而且真正的端点覆盖被静默丢弃。归一化逻辑：凡 `issubclass(model_class, BaseChatOpenAI)` 且该键不是类自己声明的，就把 `api_base` 改名为 `base_url`；若 `base_url`/`openai_api_base` 已存在则丢弃别名并告警（避免双重意图）。按基类家族而非类路径白名单判定，任何 OpenAI 兼容子类自动覆盖。

## 16.2　模型名解析：默认模型与运行时覆盖

进工厂之前，运行时先要把"这一轮用哪个模型"定下来。入口在 `agents/lead_agent/agent.py`：

- `_resolve_model_name`（`agent.py:226-240`）：`default_model_name = app_config.models[0].name`——**配置文件里第一条模型就是全系统默认模型**。调用方请求的 `model_name` 若能在配置中找到则原样返回；找不到就打日志 `Model 'xxx' not found in config; fallback to default model 'yyy'.` 并回退到默认。无任何模型时抛 `ValueError("No chat models are configured. ...")`。同名逻辑在 `create_chat_model(name=None)` 里再兜底一次：`name = config.models[0].name`（`factory.py:461-463`）。
- 运行时优先级由 `_resolve_runtime_option`（`agent.py:165-184`）定义：`request > agent config > default`，且用 `key in cfg` 而非 `cfg.get(key)` 判断，保证请求显式传 `thinking_enabled: false` 不会被吞掉（issue #4336 的语义）。
- 模型配置查找用 `AppConfig._build_name_indexes`（`app_config.py` model_validator）预建的 O(1) 索引，`get_model_config(name)` 在每次 agent 构建时被调用多次而无需线性扫描。

## 16.3　create_chat_model 工厂：归一化推理 + 双分支构造管线

## 16.3　create_chat_model 工厂：归一化推理 + 双分支构造管线

`create_chat_model`（`models/factory.py:432-625`）是"配置 → 可调用模型实例"的唯一官方入口，`models/__init__.py` 只导出它。签名：

```python
def create_chat_model(name=None, thinking_enabled=False, *,
                      app_config=None, attach_tracing=True,
                      model_overrides=None, **kwargs) -> BaseChatModel
```

- `name=None` → 用 `models[0]`；
- `thinking_enabled`：请求思考模式；
- `model_overrides`：调用方（如自定义 agent 的 `temperature`/`max_tokens`）叠加在 profile 之上的采样覆盖，`None` 值忽略以免清掉 profile 值；**先于 thinking/Codex 变换应用**，保证 provider 特化归一化（如 Codex 丢 `max_tokens`）仍能管辖覆盖值；
- `attach_tracing=True`：直接给模型实例挂 Langfuse/LangSmith 回调。**在 LangGraph 图根已经接好 tracing 的调用方（`make_lead_agent`、图内 `TitleMiddleware`）必须传 `False`**，否则同一 LLM 调用产生重复 span、且 `session_id`/`user_id` 元数据因模型成为嵌套 observation 而被剥掉；
- `**kwargs`：调用方透传参数（典型是 `reasoning_effort`）。

构造管线（每步都能在 `factory.py` 找到对应行）。自 2.2.0 起，工厂在剥离元数据、叠加覆盖、合并 thinking 快捷字段之后，先**归一化推理请求**（第 8 步），再按 profile 是否声明 `reasoning:` 契约走进**两条互斥的 thinking/effort 分支**——契约分支与 legacy 分支的差异是理解本章的关键：

1. **取配置**：`config.get_model_config(name)`，查不到抛 `ValueError(f"Model {name} not found in config")`（465 行）。
2. **反射解析类**：`resolve_class(model_config.use, BaseChatModel)`（466 行，`deerflow/reflection/resolvers.py`）。import 失败时给出**可执行的安装提示**——`MODULE_TO_PACKAGE_HINTS` 把模块根映射到包名，报错形如 ``Missing dependency 'langchain_google_genai'. Install it with `uv add langchain-google-genai` (or `pip install ...`), then restart DeerFlow.``；路径里没有 `:` 或属性不存在也各有清晰报错。
3. **剥离元数据**（467 行）：`model_config.model_dump(exclude_none=True, exclude=set(_MODEL_METADATA_FIELDS))`。`_MODEL_METADATA_FIELDS`（17-41 行）共 **14 个键**，把**所有不进构造器**的键剔除：`use`/`name`/`display_name`/`description`、`supports_thinking`/`supports_reasoning_effort`/**`reasoning`**/`supports_vision`、`when_thinking_enabled`/`when_thinking_disabled`/`thinking`、`context_window`、`pricing`、**`request_admission`**（注释分别说明：pricing 若转发给 provider client，未知 kwarg 会被转进 completion 请求体；`reasoning` 与 `request_admission` 由归一化层消费，绝非 provider kwarg）。剩下的（`model`、`api_key`、`api_base`/`base_url`、`timeout`、`max_tokens`、`extra_body`……）就是要交给构造器的 settings。
4. **旧式 `reasoning` 原样回填**（468-474 行）：**只有** `reasoning` 是布尔或字符串时（`ChatOllama` 的 `reasoning: true`、gpt-oss 的 `reasoning: high`），工厂把它塞回 settings 当作原生 provider kwarg——上一步的 exclude 把它剔了，这一步补回。映射值不受影响（那是契约，不是 kwarg）。
5. **叠加 `model_overrides`**（481-482 行）：非 `None` 的覆盖键写入 settings。
6. **取出 `reasoning_effort` 并探测 Codex**（488-491 行）：`requested_reasoning_effort = kwargs.pop("reasoning_effort", None)`——注意是 `pop`，避免 kwargs 与 settings 同名冲突；`is_codex_model = issubclass(model_class, CodexChatModel)`。
7. **合并 thinking 快捷字段**（492-498 行）：`has_thinking_settings = (when_thinking_enabled is not None) or (thinking is not None)`；`thinking` 与 `when_thinking_enabled["thinking"]` 深合并后作为生效的 `effective_wte`。
8. **归一化推理契约**（504-516 行）：`contract = resolve_reasoning_contract(model_config)`（`models/reasoning.py`）——声明了 `reasoning:` 映射的 profile 得 `source="contract"`，否则从两个旧布尔派生 `source="legacy"`。随后 `resolved_reasoning = resolve_reasoning_request(contract, thinking_enabled=..., reasoning_effort=...)` 应用调用方请求：`unsupported` 模型强制关思考、`required` 模型强制开（除非契约声明 `on_disable_request: reject`，则抛 `ReasoningPolicyError` 被转成 `ValueError`）、effort 走词表/别名/default。任何 `adjustments` 都记一条 info 日志。
9. **分支 A —— legacy（无 `reasoning:` 映射，517-533 行）**：`_apply_legacy_thinking_settings`（196-241 行）完全保留历史行为。开启且模型不支持 → 直接 `ValueError("Model ... does not support thinking. Set supports_thinking to true ...")`（仅 legacy 硬报错）；开启且支持 → 把 `effective_wte` 深合并进 settings；关闭 → 三种禁用方言按序尝试：① 用户给了 `when_thinking_disabled` → 采用；② OpenAI 兼容网关形态（`extra_body.thinking.type`）→ 注入 `extra_body={"thinking": {"type": "disabled"}}` 并顺带 `reasoning_effort="minimal"`（**契约分支刻意丢弃这个合成值**）；③ vLLM/Qwen 形态（`extra_body.chat_template_kwargs` 含 `thinking`/`enable_thinking`）→ 注入对应键为 `False`（`_vllm_disable_chat_template_kwargs`）；④ Anthropic 原生形态（`thinking.type`）→ 注入 `thinking={"type": "disabled"}`。legacy 分支末尾若 `supports_reasoning_effort=False`，从 kwargs 与 settings 双双剔除 effort。
10. **分支 B —— 契约（声明了 `reasoning:`，534-544 行）**：`_apply_contract_thinking_settings`（244-290 行）用**已解析**的 `thinking_enabled`/effort 施工。`dialect` 为 `auto` 时用 `_infer_dialect`（156-174 行）从 `when_thinking_enabled` 模板反推方言；`enabled` 时先按方言生成 enable payload 再深合并模板（模板赢），`history` 有值时在 `openai_extra_body` 方言下追加 `clear_thinking`；`disabled` 时优先 `when_thinking_disabled`，否则用方言 disable payload；effort **最后**写在 `contract.effort.path`（默认 `reasoning_effort`，可为 `extra_body.thinking.effort` 等 dotted 路径），并**主动弹掉** `reasoning_effort` 通用键（除非契约就用这个路径）。
11. **端点键归一化 + chunk 超时默认**（548-549 行）：先跑 `_normalize_openai_base_url`（16.1.4），再跑 `_apply_stream_chunk_timeout_default`（见 16.4 超时小节）。
12. **Codex 特化**（552-563 行）：若是 `CodexChatModel`——`max_tokens`/`max_output_tokens` 会被 ChatGPT Codex 端点拒绝，直接弹掉；通用 effort 由 Codex 处理时（`codex_handles_generic_effort`）思考关闭 → `reasoning_effort="none"`；否则用调用方显式给的 `reasoning_effort`（合法集 `low/medium/high/xhigh`），没给则默认 `medium`。**thinking 在 Codex 上被翻译成 reasoning_effort 刻度**。
13. **MindIE 保守重试**（567-569 行）：`MindIEChatModel` 默认 `max_retries=1`，防止级联超时。
14. **`stream_usage` 默认 True**（576-578 行）：LangChain 的 `BaseChatOpenAI` **只在未设置自定义 base_url/api_base 时才默认 `stream_usage=True`**，所以走第三方端点（doubao、deepseek……）会静默丢失 usage 元数据；工厂在字段存在且用户未显式配置时强制补 `True`。
15. **`request_admission` 翻译成限流器**（589-599 行）：若 profile 声明了 `request_admission`，工厂调用 `get_request_admission(name, config)`（`models/request_admission.py`）拿到共享/专属的 `RequestAdmission`（`BaseRateLimiter` 子类），注入 `rate_limiter=`；同时把 SDK 内部 `max_retries` 归零（`max_retries = 0`），让每次重试都重新经过准入钩子。与自定义 `rate_limiter` 同时声明会直接报错。
16. **防呆警告**（334-381 行 `_warn_unknown_model_settings`，601 行调用）：`extra="allow"` 的另一面——配置里 `maxx_tokens` 这类 typo 键不会被 schema 拦下，LangChain OpenAI 客户端也不拒收，而是告警后转移进 `model_kwargs`、在每个请求里被 SDK 以 `unexpected keyword argument` 拒绝，极难反查。工厂在构造前把 settings 与类声明的 `model_fields`（含别名）比对，未知键**在模型构建期**就打出一条带建议的 warning（如 `Check for typos (e.g. 'maxx_tokens' -> 'max_tokens')`）。仅对 `BaseChatOpenAI` 家族生效（该家族才有 divert-and-crash 行为）；`ChatAnthropic` 走 `extra="ignore"` 会误报，故意不查。
17. **实例化 + context_window 翻译 + tracing**（605-625 行）：`model_class(**effective_model_settings)` 构造 → 若声明了 `context_window` 且调用方未给 profile，构造**后**合并进 profile（下节）→ `attach_tracing` 时挂回调。

**实例化走查**：拿 16.1.1 的 legacy `deepseek-v4`（`thinking_enabled=True`）过一遍，看 settings 从声明到构造器发生了什么：

1. exclude 剔除 `name/display_name/description/supports_*/reasoning/when_thinking_*/thinking/context_window/pricing/request_admission`；
2. 剩 `model=deepseek-v4-pro`、`api_key`（已解析为真实值）、`api_base`、`timeout=600.0`、`max_retries=2`、`max_tokens=8192`；
3. 第 8 步契约派生出 legacy（无 `reasoning:` 映射），第 9 步开启分支把 `when_thinking_enabled` 深合并进来 → 新增 `extra_body={"thinking": {"type": "enabled"}}`；
4. `_declares_api_base(PatchedChatDeepSeek)` 为真（类自己声明了 `api_base` 字段）→ 端点键保持 `api_base` 原样、不被改名；
5. 因是 `BaseChatOpenAI` 子类 → 注入 `stream_chunk_timeout=240.0`；`stream_usage` 字段存在且未配置 → 注入 `True`；
6. `context_window` 不进构造器，实例化后合并进 `instance.profile["max_input_tokens"]`；
7. 最终构造器收到约 9 个 kwarg，全部是 provider 类声明的合法字段——这正是第 16 步警告存在的原因：任何多余键都意味着请求期才会爆炸。

## 16.4　能力矩阵：推理契约 / thinking / vision / reasoning_effort / context_window / RPM 准入 / 流式用量 / 超时

### 16.4.1 各能力字段的"声明 → 消费"回路

| 能力 | schema 字段 | 工厂行为 | 下游消费点 |
|------|-------------|----------|------------|
| 思考模式 | `supports_thinking` / `reasoning.thinking` + `when_thinking_enabled/disabled` + `thinking` 快捷 | legacy：`thinking_enabled=True` 且支持 → 注入 enable 设置；关闭 → 按方言注入 disable 设置；**请求开启但不支持 → 抛 `ValueError`**。契约：由 `resolve_reasoning_request` 先归一化（`unsupported` 关、`required` 开、`reject` 抛错），再走方言 payload | `agent.py:1074-1080`：lead 装配用同一 `resolve_reasoning_request` 归一化，`thinking_unsupported` 时打警告并降为 `False`（宁可关掉也不报错）；运行时默认值 `_resolve_runtime_option(..., default=True)` |
| 推理契约 | `reasoning:`（映射）→ `ReasoningCapabilities` | `resolve_reasoning_contract` 归一化；`source="contract"` 时走 `_apply_contract_thinking_settings`（见 16.3 分支 B） | `models/reasoning.py`：`ReasoningContract`/`resolve_reasoning_request`/`reasoning_capabilities_payload`（后者投影给 Gateway `/api/models` 与 `DeerFlowClient`） |
| reasoning effort | `supports_reasoning_effort` / `reasoning.effort` | legacy：False → 从 kwargs/settings 双剔除；True → 透传（Codex 上被特化为 none/刻度映射）。契约：走词表/别名/default，写在 `effort.path` | 前端 low/medium/high 选择经 `**kwargs` 进工厂；`agent.py:1302` 自定义 agent 构建时显式传 `reasoning_effort` |
| RPM 准入 | `request_admission` | 工厂把它翻译成 `RequestAdmission`（`BaseRateLimiter`）注入 `rate_limiter=`，并把 SDK `max_retries` 归零 | `models/request_admission.py`：进程内 FIFO 均匀间隔限流，按 `group` 或模型名分桶，`max_wait_seconds`/`max_queue_size` 兜底 |
| 视觉输入 | `supports_vision` | **完全不参与构造**（纯元数据，被 exclude） | `agent.py:696`：`ViewImageMiddleware` 只在 `model_config.supports_vision` 为真时挂载——用解析后的运行时 `model_name` 反查配置，避免陈旧值；subagent 侧同理 |
| 上下文窗口 | `context_window` | exclude 出构造；**构造后**合并进 profile（16.4.2） | UI 实时 "% context used" 指示器；`summarization.trigger: [{type: fraction}]` 的阈值解析（profile["max_input_tokens"]） |
| 计费 | `pricing`（extra） | exclude 出构造，绝不进请求体 | console 成本显示、run 级成本估算（`token_usage_by_model`） |
| 流式 chunk 超时 | `stream_chunk_timeout` | OpenAI 兼容家族缺省注入 240s | 流式 chunk 间隔守卫 |

### 16.4.2 `context_window` → langchain profile：构造后合并而非构造时传入

`translate_context_window` 逻辑（`factory.py:580-617`）解决一个真实痛点（issue #3103）：第三方 OpenAI 兼容模型（doubao、deepseek、自建网关……）的 SDK **没有自带模型 profile**，而 `SummarizationMiddleware` 的 fraction 触发要从 `profile["max_input_tokens"]` 解析阈值。做法是**先构造、后合并**（`translate_context_window` 标志在 587 行算好，实例化后 609-617 行执行）：

```python
inferred_profile = getattr(model_instance, "profile", None)
model_instance.profile = {**(inferred_profile or {}), "max_input_tokens": model_config.context_window}
```

为什么不把 `profile` 当构造参数传？那样会**整体替换** provider 推断出的 profile（tool_calling、structured_output、io 能力、输出上限等元数据全丢），只剩这一个键。构造后合并则保留推断值、只让 operator 显式声明的窗口赢得 `max_input_tokens`。`profile` 是 `BaseChatModel` 的元数据字段（`exclude=True`），不会进请求体；显式 profile（调用方或 `model_overrides` 给的）永不被打。

### 16.4.3 超时谱系：一个 240 秒的故事

`_apply_stream_chunk_timeout_default`（`factory.py:395-429`）+ `_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS = 240.0`（392 行）针对的是流式场景的"思考暂停"：langchain-openai 自带 120 秒 chunk-gap 上限，而 DeepSeek-R1、Doubao-thinking、GPT-5 这类推理模型**首个 chunk 合法地要 90~150 秒**——默认值直接设 240s 让流式层很少误触；真卡死时 `LLMErrorHandlingMiddleware` 还有 budget=2 的重试兜底。两个细节：

- **为什么是 `issubclass` 判定而非类名单**：`stream_chunk_timeout` 是 `BaseChatOpenAI` 的字段，`ChatOpenAI`、`PatchedChatOpenAI`、`VllmChatModel`、`MindIEChatModel`、`PatchedChatDeepSeek`、`PatchedChatMiMo`、`PatchedChatStepFun`、`PatchedChatMiniMax` 全部继承。issue #3189 报在 `mimo-v2.5`（`PatchedChatMiMo`），而原修复（#3195）只匹配了 `ChatOpenAI`/`PatchedChatOpenAI`——子类不仅保留 120s 激进默认，**用户显式配置的 `stream_chunk_timeout` 还被静默丢弃**。按家族判定让所有子类一次到位。
- **非 OpenAI 兼容类直接弹掉该键**（424-425 行）：`ChatAnthropic` 声明 `extra="ignore"` 会静默丢、其他 OpenAI 风格客户端会转移进 `model_kwargs` 在请求期炸——与其让用户意图丢失，不如主动剔除。

模板里还能看到**两种超时命名**并存的证据：`PatchedChatDeepSeek` 条目写 `timeout: 600.0`，`ChatOpenAI` 条目写 `request_timeout: 600.0`——因为 `use` 指向的类各自声明不同的字段，这再次说明"配置键合法性取决于 `use` 类"。MindIE 条目则是 `read_timeout: 900.0` / `connect_timeout: 30.0` 等四元组（它因工具调用流式缺陷走 mock-streaming，需要 15 分钟级读超时，`mindie_provider` 内部自行归一化超时）。

### 16.4.4 RPM 准入：`request_admission` 如何给共享账号排队

2.2.0 新增的 `request_admission` 解决"多个模型条目共用一个 provider 账号、共享 RPM 配额"的限流问题（`models/request_admission.py`）：

- **均匀间隔而非令牌桶**：`RequestAdmission` 是 `BaseRateLimiter` 子类，每 `60 / requests_per_minute` 秒放行一次；空闲期**不累积突发额度**（`_next = now + interval` 每次重置），对 provider 的 burst-slope 限流最友好。
- **FIFO 公平 + 有界队列**：等待者以 ticket 入队，队满（> `max_queue_size`）抛 `AdmissionError`，超 `max_wait_seconds` 也抛——本地准入失败发生在发起上游请求**之前**。同步 `acquire` 与异步 `aacquire` 共享同一把 threading.Lock 与同一张 ticket 队列，跨事件循环也安全（不持有调用方 loop 的引用，靠短轮询睡眠支持取消）。
- **按组共享**：`group` 相同时多个模型条目复用**同一个**限流器实例（`get_request_admission` 的 `_registry` 以 `("group", name)` 或 `("model", name)` 为键）；进程内配置冲突且未重启会显式报错。
- **重试语义**：工厂注入 `rate_limiter=` 的同时把 `max_retries` 归零（16.3 第 15 步），让每次 SDK 内部重试都重新过一遍准入，而不是绕过限流。

### 16.4.5 thinking 的两道闸门：装配期降级 vs 工厂期报错

思考开关在两条代码路径上有**故意不对称**的行为，理解它才能读懂线上日志（2.2.0 起两条路径都先经过同一 `resolve_reasoning_request` 归一化，差异只在"不支持"时的处理）：

- **装配路径（lead agent，`agent.py:1049-1080`）**：先算运行时 thinking 值——请求显式值 > 自定义 agent 配置 > 默认 `True`；随后用 `resolve_reasoning_request(contract, ...)` 归一化，若命中 `thinking_unsupported` 只**打 warning 并降为 `False`**（`resolved_reasoning.thinking_enabled`）。也就是说走正常装配，一个不支持思考的模型永远不会带着 `thinking_enabled=True` 进工厂——**宁可关掉思考，也不让运行失败**。
- **工厂路径（`create_chat_model` 直接调用方）**：`factory.py:508-509` 在 legacy profile 上 `thinking_enabled=True` 且 `supports_thinking=False` 时**抛 `ValueError`**；契约 profile 则在 `on_disable_request: reject` 时抛 `ReasoningPolicyError`（转成 `ValueError`）。这条硬错误只对绕过装配层的直接调用方生效（记忆更新、标题生成、独立工具脚本等），逼它们在代码层面显式确认模型能力，而不是静默接受一个开不起来的开关。

两条路径合起来：**面向用户的请求会被优雅降级，面向开发者的误用会立刻报错**。另外注意运行时默认值是 `True`（`default=True`）——现代模型默认开思考，模板里大多数 thinking 模型条目也因此配套写了 `when_thinking_enabled`/`when_thinking_disabled` 两个形态，确保开关在两个方向都有确定的注入内容。

### 16.4.6 `extra_body` 合并不是覆盖：禁用形态的叠写语义

thinking 开关里所有"注入形态"的操作都经过 `_deep_merge_dicts`（`factory.py:44-56`，递归合并、不修改入参）——因为 `extra_body` 里可能已经躺着用户配置的其它键（如 StepFun 的 `reasoning_format: deepseek-style`、MiniMax 的 `reasoning_split: true`）。若是整块替换，开思考时 `when_thinking_enabled` 会连带抹掉这些与思考并列的请求级开关；递归合并保证 `thinking.type` 或 `chat_template_kwargs.enable_thinking` 只覆盖自己那一支，其余键原样保留。禁用分支里顺带注入的 `reasoning_effort="minimal"`（235 行，**仅 legacy 路径**）同理是"最小推理"的保守值：模型声明了 `supports_reasoning_effort` 就原样收到它，没声明的则被剔除——两条路都不会把 effort 误发给不认它的端点。契约路径刻意不合成 `minimal`，改由 `reasoning.effort.default` 决定背景调用的力度。

## 16.5　provider 适配补丁：为什么存在，修什么

### 16.5.1 根源：静默丢弃字段 → 要求逐字回显的 provider 拒收

LangChain 的 OpenAI 客户端为通用性只序列化**标准字段**（assistant 消息的 `content`、tool_calls 的 `id/type/function`）。各家思考模型返回的非标字段（DeepSeek/StepFun/MiMo 的 `reasoning_content`、Gemini 网关的 `thought_signature`、vLLM 的 `reasoning`、MiniMax 的 `reasoning_details`）被放进 `AIMessage.additional_kwargs` 只在本轮存活，下一轮组请求体时**被静默丢弃**。

对普通对话这顶多"看不见思考过程"；但对思考模型的多轮工具调用，服务端把历史推理内容当作**必须逐字回显**的输入（推理参与采样/计费/防重放），缺失即**拒收**而非忽略——真实报错如 Gemini 网关的：

```
Unable to submit request because function call `<tool>` in the N. content
block is missing a `thought_signature`.
```

DeerFlow 是重度多轮工具调用 agent，历史里每条 assistant 消息都可能带 tool_calls，所以"丢弃"几乎必然升级为"请求 400"。补丁层的修法统一而克制：**子类覆写 `_get_request_payload`，在发送前把字段从原始 `AIMessage` 放回序列化 payload，不动 LangChain 其余管线**。

### 16.5.2 `assistant_payload_replay.py`：共享的"回灌"匹配引擎

`models/assistant_payload_replay.py`（118 行）把最容易出错的部分——**"序列化后的第 N 条 assistant payload 对应原始消息里的哪条 AIMessage"**——收敛成一份共享实现，各 patch 只负责传入"恢复哪个字段"的回调：

- `restore_assistant_payloads(payload_messages, original_messages, restore)`：两条路径。**快速路径**——payload 数与原始消息数一致，逐条 zip、`role == "assistant"` 且原对象是 `AIMessage` 就调用 restore；**慢速路径**——序列化可能丢消息/重排（历史被裁剪等），先抽离所有 AIMessage 与 assistant payload，做匹配。
- 匹配策略 `_match_ai_message`：先按**签名**找唯一命中——`(content 的稳定 JSON repr, "|".join(tool_call_ids))` 构成签名，`content` 为空且无 tool_calls 的消息签名为 `None` 不参与；签名命中多条/零条时按 payload 序号**从该序号向前扫描**找下一个未占用的 AIMessage（保持位置偏向又能在消息被丢时恢复），不回绕到更早下标。
- 字段恢复回调：`restore_reasoning_content`（把 `additional_kwargs["reasoning_content"]` 写回 payload）与通用 `restore_additional_kwargs_field`。`AssistantPayloadRestorer = Callable[[dict, AIMessage], None]` 是各 patch 的实现契约。

### 16.5.3 各 patch 一句话表（真实文件，`models/` 目录）

| 文件 / 类 | 基类 | 补丁动机（一句话） | 回灌/整形内容 | 对应 config 示例 |
|-----------|------|--------------------|----------------|------------------|
| `patched_deepseek.py` `PatchedChatDeepSeek` | `langchain_deepseek.ChatDeepSeek` | 思考模式下 API 要求**每条**历史 assistant 消息都带 `reasoning_content`，原类存进 `additional_kwargs` 却不再回送 | `reasoning_content` | deepseek-v4、doubao（ark）、kimi/moonshot、glm coding plan |
| `patched_openai.py` `PatchedChatOpenAI` | `langchain_openai.ChatOpenAI` | Gemini 思考经 OpenAI 兼容网关时 tool_calls 上的 `thought_signature` 必须逐字回显，标准序列化静默丢弃 → 400 `INVALID_ARGUMENT` | tool_calls 上的 `thought_signature` | gemini-2.5-pro + 自定义网关、atlascloud qwen3-thinking |
| `patched_stepfun.py` `PatchedChatStepFun` | `ChatOpenAI` | StepFun 返回 `reasoning`（或 deepseek-style 的 `reasoning_content`）双别名，标准类全丢；流式、全量、多轮回灌三条路径都捕获 | `reasoning`/`reasoning_content` | step-3.7-flash |
| `patched_mimo.py` `PatchedChatMiMo` | `ChatOpenAI` | 小米 MiMo 思考模式要求历史消息回灌 `reasoning_content`，一旦 tool_calls 进历史就 400（issue #3189 同源） | `reasoning_content` | mimo-v2.5 / mimo-v2.5-pro |
| `patched_minimax.py` `PatchedChatMiniMax` | `ChatOpenAI` | MiniMax 在 `extra_body.reasoning_split=true` 下返回结构化 `reasoning_details`，标准类忽略 → 保留请求开关并把字段映射成 DeerFlow 已认识的 `additional_kwargs.reasoning_content` | `reasoning_details` → `reasoning_content` | MiniMax-M3 / M2.7（含 highspeed 变体） |
| `vllm_provider.py` `VllmChatModel` | `ChatOpenAI` | vLLM 0.19 把 `reasoning` 当一等字段，需在非流式、流式 delta、多轮请求三条路径都保留（vLLM 期待前序 reasoning 被回显）；另有 `cumulative_stream_usage`（默认 false）把每 chunk 重复上报的累计用量按 completion id 转增量 | `reasoning` + usage 快照→增量 | qwen3-32b-vllm（`--reasoning-parser` 需服务端配合） |
| `mindie_provider.py` `MindIEChatModel` | 自研 | MindIE chat template 解析不了 LangChain 原生 `tool_calls`/`ToolMessage` 或产生 0-token 生成 → `_fix_messages` 把多模态 list 内容展平为字符串、把 tool 消息转成模型期待的 XML 文本；mock-streaming + 保守超时 + `max_retries=1` | 消息整形（非回灌） | Qwen3-Coder-480B MindIE（`read_timeout: 900.0`） |
| `claude_provider.py` | `ChatAnthropic` | 双鉴权模式：标准 `x-api-key` 或 Claude Code OAuth `Authorization: Bearer`（`sk-ant-oat` 前缀识别，须 `anthropic-beta: oauth-2025-04-20,claude-code-20250219` beta 头）；prompt caching + interleaved thinking | 鉴权/请求头 | claude-sonnet-4（本地 Claude Code 登录态） |
| `openai_codex_provider.py` `CodexChatModel` | 自研 | 走 ChatGPT Codex **Responses API**（`chatgpt.com/backend-api/codex/responses`，与 Codex CLI 同端点），Codex OAuth token 自动装载；端点强制流式；**拒绝 `max_tokens`**（工厂在 16.3 第 12 步弹掉）；指数退避重试 | Responses 格式适配 | codex（thinking→reasoning_effort 刻度） |

上表所有适配器的共性写法：子类覆写 `_get_request_payload`（DeepSeek 系）或同时覆写响应解析路径（StepFun/MiMo/vLLM 连流式 chunk 一起抓），需要回灌历史字段的调用 `restore_assistant_payloads(...)` 并传入各自的 restorer。**选择哪个 patch 纯粹是配置行为**——`use:` 指向哪个类，工厂就用哪个适配器，其余字段（端点、密钥、thinking 开关）写法几乎不变，这让同一个模型族在不同网关间切换只改三五行。

### 16.5.4 补丁层的内部纪律与边界

补丁不是随便 monkey-patch，有几条肉眼可见的约束（以 `patched_deepseek.py` 为最小样本，全文 102 行）：

- **覆写面最小**：只覆写 `_get_request_payload`，且先 `self._convert_input(input_).to_messages()` 拿到**序列化前的原始消息**，再 `super()._get_request_payload(...)` 拿基础 payload，最后对 `payload["messages"]` 里每条 assistant 消息做恢复——把补丁钉死在"发送前一刻"这一处，LangChain 的转换、批处理、重试逻辑一概不动。
- **可序列化契约**：补丁类必须向 LangChain 声明自己可被序列化——`is_lc_serializable() -> True` 与 `lc_secrets`（把 `api_key` 映射回环境变量名 `DEEPSEEK_API_KEY`，这样 `model.dump()` 到别处反序列化时密钥仍从环境取，不落盘）。
- **回灌字段逐家不同、机制共享**：每家只传一个"恢复什么"的回调（`restore_reasoning_content` 或通用 `restore_additional_kwargs_field("thought_signature")`），"哪条 payload 对应哪条 AIMessage"的易错匹配逻辑只写一次在 `assistant_payload_replay.py`。
- **回归测试成建制**：每个适配器都有同名测试（`backend/tests/test_patched_deepseek.py`、`test_patched_mimo.py`、`test_patched_minimax.py`、`test_patched_stepfun.py`、`test_assistant_payload_replay.py`、`test_codex_provider.py`、`test_claude_provider_oauth_billing.py` 等），外加工厂级 `test_model_factory.py`、`test_model_config.py`、授权级 `test_models_authorization.py`。改适配器时这些是防线。

**边界（代价）**：补丁与具体 LangChain 版本的私有方法签名耦合（如 `_get_request_payload` 的 `stop` 关键字），升级 langchain-openai/langchain-deepseek 大版本时可能需同步适配；且它只解决"序列化丢字段"，不解决 provider 的其它方言（鉴权方式、流式契约、消息模板）——那些落在独立 provider 文件里（Claude 的 OAuth、Codex 的 Responses 强制流式、MindIE 的消息整形与 mock-streaming）。判断一个模型要不要补丁的实用标准：**它开思考模式后是否要求历史 assistant 消息逐字回显非标字段**——要，就用对应 patch；纯普通模型直接用官方类即可（16.6.2）。

## 16.6　配置示例：三段可直接套用的模型段

### 16.6.1 DeepSeek 官方端点（自带思考开关，模板 490-506 行同型）

```yaml
models:
  - name: deepseek-v4
    display_name: DeepSeek V4 (Thinking)
    use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
    model: deepseek-v4-pro
    api_base: https://api.deepseek.com/v1   # ChatDeepSeek 自声明 api_base
    api_key: $DEEPSEEK_API_KEY
    timeout: 600.0
    max_retries: 2
    max_tokens: 8192
    context_window: 131072
    supports_thinking: true
    supports_vision: false
    when_thinking_enabled:
      extra_body:
        thinking:
          type: enabled
    when_thinking_disabled:
      extra_body:
        thinking:
          type: disabled
```

要点：thinking 开关通过 `extra_body.thinking.type` 以 OpenAI 兼容网关形态注入（16.3 第 9 步 legacy 禁用分支 ② 能识别并自动生成禁用形态）；`supports_vision: false` 保证视觉中间件不挂载；`context_window` 驱动 UI 百分比与 summarization fraction 阈值。**同一条目结构可平移到任何走 `PatchedChatDeepSeek` 的端点**（火山 ark、moonshot、coding plan 网关），只改 `model`/`api_base`/`api_key`。

### 16.6.2 OpenRouter / 任意 OpenAI 兼容聚合网关（模板 712-721 行同型）

```yaml
  - name: openrouter-gemini-2.5-flash
    display_name: Gemini 2.5 Flash (OpenRouter)
    use: langchain_openai:ChatOpenAI          # 纯 OpenAI 兼容，无需补丁
    model: google/gemini-2.5-flash-preview
    api_key: $OPENROUTER_API_KEY
    base_url: https://openrouter.ai/api/v1     # BaseChatOpenAI 认 base_url
    request_timeout: 600.0
    max_retries: 2
    max_tokens: 8192
    temperature: 0.7
```

要点：普通（非思考）模型直接用官方 `langchain_openai:ChatOpenAI` 即可——不需要 DeerFlow 补丁，因为无非标字段需要回灌。同型写法覆盖 Novita、Atlas Cloud 等一切 OpenAI 兼容网关。**若该网关上的模型开思考模式并回传推理字段**（如 Atlas Cloud 的 Qwen3 `*-thinking`），模板明示应换 `deerflow.models.patched_openai:PatchedChatOpenAI`（或相应 patch）——这正是 16.5 那张表的使用场景。

### 16.6.3 自定义/本地端点：vLLM 0.19（模板 769-783 行同型）

```yaml
  - name: qwen3-32b-vllm
    display_name: Qwen3 32B (vLLM)
    use: deerflow.models.vllm_provider:VllmChatModel
    model: Qwen/Qwen3-32B
    api_key: $VLLM_API_KEY                    # 本地服务也走统一 $ENV 约定
    base_url: http://localhost:8000/v1
    cumulative_stream_usage: true             # 仅当端点每 chunk 重复累计用量时开
    request_timeout: 600.0
    max_retries: 2
    max_tokens: 8192
    supports_thinking: true
    when_thinking_enabled:
      extra_body:
        chat_template_kwargs:
          enable_thinking: true               # Qwen 系用 template kwargs 切思考
```

要点：本地 vLLM 也是 OpenAI 兼容端点，但 thinking 开关走 `chat_template_kwargs.enable_thinking`（工厂第 9 步 legacy 禁用分支 ③ 与 `_vllm_disable_chat_template_kwargs` 能自动生成 `enable_thinking: false` 的禁用形态）；服务端需按模型要求以 `--reasoning-parser <parser>` 启动。本地推理同样建议给 `context_window`，否则 fraction summarization 触发会带警告降级。

### 16.6.4 同一条目内易混字段速查

| 字段 | 含义 | 与谁的差异 |
|------|------|------------|
| `max_tokens` | 单次调用**输出**上限，发给 provider | vs `context_window`：后者是总容量元数据，不进请求体（工厂 exclude） |
| `context_window` | 总上下文容量（prompt+completion），驱动 UI 百分比 + profile 合并 | vs `max_tokens` |
| `stream_chunk_timeout` | 流式相邻 chunk 最大间隔；不设 → OpenAI 兼容家族默认 240s | 只对 `BaseChatOpenAI` 子类生效，其它类该键被剔除 |
| `request_timeout` / `timeout` / `read_timeout` 等 | 各自的 HTTP 超时，**合法键取决于 `use` 类**（ChatOpenAI 认 `request_timeout`，ChatDeepSeek 认 `timeout`，MindIE 认四元组） | 写错键不报加载错，靠工厂 16.3 第 16 步告警 |
| `api_base` / `base_url` | 端点键两种方言，见 16.1.4 | `ChatDeepSeek` 认 `api_base`；其余 OpenAI 兼容认 `base_url`（误抄会被自动归一化） |

### 16.6.5 声明式推理契约（模板 263-297 行同型）

需要"永远开思考 + 自定义 effort 词表 + 明确方言"的模型用 `reasoning:` 映射声明契约。模板里 Z.AI GLM-5.3-Flash 的例子：

```yaml
  - name: glm-5.3-flash
    display_name: GLM-5.3-Flash
    use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
    model: glm-5.3-flash
    api_base: https://api.z.ai/api/paas/v4
    api_key: $ZAI_API_KEY
    timeout: 600.0
    max_retries: 2
    temperature: 1.0
    top_p: 0.95
    max_tokens: 131072
    context_window: 1000000
    supports_vision: true
    stream_usage: false
    reasoning:
      thinking: required          # 不可关思考:required 支路永不注入 disable payload
      dialect: openai_extra_body  # 显式方言(省略则 auto)
      history: clear              # 压缩后不要求逐字回放推理
      effort:
        values: [low, high, max]  # provider 自己的词表
        default: high             # 背景调用(摘要/标题/子代理)的默认力度
        aliases:
          minimal: low            # DeerFlow 通用值 → provider 值
          medium: high
    extra_body:
      tool_stream: true
```

要点：`required` 让前景与背景调用都强制开思考；`aliases` 把 DeerFlow 的 `minimal`/`medium` 预设映射进 provider 词表；`default: high` 保证背景调用落在可控力度；`history: clear` 令 `openai_extra_body` 方言追加 `clear_thinking: true`。矛盾组合（如 `required` + `when_thinking_disabled`、`default` 不在 `values` 内、profile/template 里出现契约不认的 effort 值）会在**启动期**就报错。无 `reasoning:` 的 profile 行为完全不变（legacy 支路）。

## 16.7　模型授权：model:use 与 fallback 扫描

授权开关是配置里的 `authorization:` 段（`config.example.yaml` 约 3183 行起，默认 `enabled: false`；schema 在 `config/authorization_config.py`）：`enabled`、`fail_closed`（provider 出错或身份无法解析时是否放行，默认 true=阻断）、`default_role`（user_role 为空时的兜底角色，默认 `user`）、`provider`（`use` 类路径 + `config` 字典）。内置 provider 是 `deerflow.authz.rbac:RbacAuthorizationProvider`，策略写在 provider.config 的 `roles:` 下：

```yaml
authorization:
  enabled: true
  fail_closed: true
  default_role: user
  provider:
    use: deerflow.authz.rbac:RbacAuthorizationProvider
    config:
      roles:
        admin:
          models: {allow: "*"}
          skills: {allow: "*"}          # 技能可见性也受 RBAC 约束
        user:
          models: {allow: "*"}
          skills: {allow: "*"}
        guest:
          models: {allow: ["gpt-4o-mini"]}   # guest 角色只能用白名单模型
          skills: {allow: []}                # 不列出任何技能
```

RBAC 实现要点（`authz/rbac.py`）：资源类型到配置键有显式映射 `_RESOURCE_POLICY_KEYS`（`{"tool": "tools", "model": "models", "skill": "skills", "sandbox": "sandbox", "mcp_server": "mcp_servers", "route": "routes", "plugin_action": "plugin_actions", "plugin_management": "plugin_management"}`，防止 `AuthzRequest.resource` 单数与配置键复数错位）；策略在构造期编译成不可变结构，**deny 恒胜 allow**；角色/策略缺失抛 `ValueError` 而非静默放行，把最终决定权交给上层的 `fail_closed`。授权判定走 `deerflow.authz.provider` 的 `AuthzRequest(principal, resource, action, target)` / `AuthzDecision` 协议，principal 由 `build_principal_from_context` 统一构建（内部调用方 `system_role=internal` 被弹掉、落入 `default_role`）。

**授权已从"只有 model/tool"扩展到多类资源。** 除 `model:use`（本节主线）与工具授权（`authz/tool_filter.py`）外，2.2.0 起同一套 RBAC 还覆盖：

- **`skill:list/use`**（`authz/skill_filter.py::resolve_skill_authorization`）：`GET /api/skills`、`/custom`、`/{name}` 的可见性过滤走 `skills` 策略键；技能**管理**端点（install/edit/export）仍恒为 admin-gated，不受角色策略放宽；
- **`sandbox:execute`**（`authz/sandbox_authz.py::authorize_sandbox_execution`）：沙箱使用前先查 `sandbox` 键，deny 时抛 `SandboxAuthorizationError` 并转成友好 ToolMessage（"sandbox not permitted for your role"），与其他路径共享同一 `build_principal_from_context` 身份源；
- **`plugin_action`/`plugin_management`**（`authz/plugin_authz.py`）：分别约束"调用已注册的插件后端 action（`POST /api/plugins/{ns}/actions/{name}`）"与"企业级插件管理路由（`require_plugin_management`）"，target 形如 `<namespace>/<action-name>` 或 `<namespace>/permissions.read|write`。

**model:use 在两层执行，语义一致但职责不同：**

**(a) Gateway 路由层——可见性与显式查询（`backend/app/gateway/routers/models.py`）。** `resolve_model_authorization`（`app/gateway/authz.py:368`）走与路由授权同一套"缓存 provider + internal 角色 + principal"路径；provider 实例缓存按**配置对象 id + `model_dump()` 签名**双键管理（`_get_cached_route_provider`，`authz.py:209`），`get_app_config()` 热重载出新对象时先比对签名，内容没变就复用 provider，避免每请求一次昂贵的 dump。两条路由：
- `GET /api/models`（列表）：`provider.filter_resources(principal, "model", 全部模型名)` 过滤出该角色可见集合——RBAC 下等价于 allow 名单成员，自定义 provider 则完全由它定义 `list` 语义；返回值类型不符（非 `list[str]`）按 provider 错误处理。provider 出错：`fail_closed` → 空列表，`fail_open` → 全量。
- `GET /api/models/{name}`（详情）：`provider.authorize(AuthzRequest(resource="model", action="use", target=name))`，拒绝返回 **403 而非 404**——模型存在、角色无权，语义上要区分开（路由 docstring 明说 deny→403）。

**(b) 运行时层——每次 agent 构建的真实选择（`agents/lead_agent/agent.py:_authorize_model_name`，241-335 行）。** 在 `_resolve_model_name` 之后、`create_chat_model` 之前执行（装配路径 ~1064 行、`DeerFlowClient._ensure_agent` 亦调用）。行为树：

1. `authorization.enabled != true` → 原样返回（no-op）；
2. `authorize(model, use, 目标模型)` 通过 → 原样返回；
3. **deny 或 provider 出错** → 进入 fallback 扫描：取 `filter_resources` 可见名单，**逐个跳过被拒模型本身、对每个候选再单独 `authorize("model","use")`**——因为自定义 provider 可能 `list` 允许而 `use` 拒绝（middleware AGENTS.md 原文："a custom provider may allow `list` while denying `use`"），只 filter 不 verify 会把一个不可用的模型选上来；RBAC provider 忽略 action，两步退化为"取第一个可见名"；
4. 找到可用候选 → 打 warning（`Model 'xxx' is not authorized for the current role; fallback to 'yyy'.`）并返回候选；
5. 扫描失败：`fail_closed` → `raise ValueError("No models are authorized for the current role.")`（与"未配置模型"的既有报错契约一致）；`fail_open` → 打 warning 后**返回原模型名**。

设计哲学写在 docstring 里，引自授权 RFC §9：**"fall back to an allowed default, not error, to avoid breaking runs"**——拒绝一个已解析的模型不该让整次运行崩掉，优雅降级到授权模型；只有 fail-closed 且确实一个可用模型都没有时才以明确错误终止。Gateway 层与运行时层重复执行同一 `model:use` 检查并非冗余：路由层管"用户能看到/查到什么"，运行时层管"这一轮实际用哪个模型"，后者拦截的是自定义 agent 默认模型或请求参数里夹带的越权模型名。

### 16.7.1 fail-closed / fail-open 语义总表

授权配置里 `fail_closed`（默认 **true**）决定"provider 出错或身份无法解析时"的行为——它和"deny"是两件事：deny 是策略的明确回答，fail_* 是系统**无法回答**时的兜底。model:use 链路各环节的兜底行为：

| 环节 | 具体失败点 | `fail_closed: true`（默认） | `fail_closed: false` |
|------|-----------|---------------------------|----------------------|
| Gateway `GET /models` | provider 解析/过滤抛错 | 返回**空列表**（什么模型都不可见） | 返回全量模型 |
| Gateway `GET /models/{name}` | provider 解析/`authorize` 抛错 | **403**（不可用） | 放行（200） |
| 运行时 `_authorize_model_name` | provider 解析/`authorize`/`filter_resources` 抛错 | **`ValueError`**（整次构建失败） | 返回原模型名（带 warning） |
| 运行时 fallback 扫描 | 候选全部不可用 | **`ValueError`**（"No models are authorized..."） | 返回原模型名（带 warning） |

注意授权**关闭时**（`enabled: false`，出厂默认）所有环节直接短路：路由层返回全量、运行时原样放行——与"开启 + fail_open"的语义不同，前者根本不存在 provider，后者是 provider 出错时的宽容。调试时看日志措辞即可区分：fail-open 路径会打 `Authorization provider failed ...` / `fail_open allows` 警告，关闭路径则完全静默。

### 16.7.2 身份来源与内部调用方

`model:use` 的 principal 与工具授权的 principal 来自同一条构建链 `build_principal_from_context`（定义在 `authz/principal.py`，请求侧由 `app/gateway/authz.py:_route_authz_context` 喂上下文）：`user_id` + `user_role` + `oauth_provider/oauth_id` + `is_internal`。关键细节是**内部调用方不享受特权**——IM 通道 worker、调度器这类 `system_role="internal"` 的调用方会被弹成 `None` 角色，从而落入 `authorization.default_role`（默认 `user`）接受同样策略约束：`model:use` 面前没有"后台免检"。前端/IM 用户各按自己的角色拿模型清单，管理员角色在 RBAC 示例里配 `models: {allow: "*"}` 才全量可见。

## 16.8　小结：概念 → 文件速查

| 概念 | 真实文件 | 关键位置 |
|------|----------|----------|
| 模型配置 schema | `backend/packages/harness/deerflow/config/model_config.py` | `ModelConfig`，`extra="allow"`（144 行）；`ReasoningCapabilities`（104-129）、`RequestAdmissionConfig`（27-34）、`_validate_reasoning_contract`（213-269） |
| 配置加载与 `$ENV` 解析 | `backend/packages/harness/deerflow/config/app_config.py` | `resolve_env_variables`（613 行）、`_build_name_indexes`（638 行） |
| 推理契约归一化 | `backend/packages/harness/deerflow/models/reasoning.py` | `resolve_reasoning_contract`（105 行）、`resolve_reasoning_request`（142 行）、`reasoning_capabilities_payload`（190 行） |
| RPM 准入 | `backend/packages/harness/deerflow/models/request_admission.py` | `RequestAdmission`（23 行）、`get_request_admission`（108 行） |
| 模型工厂 | `backend/packages/harness/deerflow/models/factory.py` | `create_chat_model`（432 行）、legacy 分支（196-241）、契约分支（244-290）、Codex（552-563）、stream_usage（576-578）、request_admission（589-599）、context_window 合并（609-617）、240s 默认（392） |
| 反射解析 + 安装提示 | `backend/packages/harness/deerflow/reflection/resolvers.py` | `resolve_class`（73 行） |
| 回灌共享引擎 | `backend/packages/harness/deerflow/models/assistant_payload_replay.py` | `restore_assistant_payloads`（20 行）、签名匹配（48 行） |
| provider 补丁 | `models/patched_deepseek.py`、`patched_openai.py`、`patched_stepfun.py`、`patched_mimo.py`、`patched_minimax.py`、`vllm_provider.py`、`mindie_provider.py`、`claude_provider.py`、`openai_codex_provider.py` | 每个文件模块 docstring 即动机陈述 |
| CLI 凭据装载 | `models/credential_loader.py` | `load_claude_code_credential`（193 行）、`load_codex_cli_credential`（242 行） |
| 运行时模型名解析 + 授权 | `agents/lead_agent/agent.py` | `_resolve_model_name`（226）、`_authorize_model_name`（241）、推理归一化（1074-1080）、vision 装配（696）、`create_chat_model` 调用（1151/1302） |
| Gateway 模型路由 + 授权 | `backend/app/gateway/routers/models.py`、`backend/app/gateway/authz.py` | `list_models` filter、`get_model` 403、`resolve_model_authorization`（368）、provider 缓存（209） |
| 授权配置与 RBAC | `config/authorization_config.py`、`authz/rbac.py`、`authz/skill_filter.py`、`authz/sandbox_authz.py`、`authz/plugin_authz.py` | `_RESOURCE_POLICY_KEYS`（26-42）、deny wins、skill/sandbox/plugin 授权 |

读完整章应能回答三个问题：**一条模型配置从 `models:` 到真实 HTTP 请求经过了哪些净化与合并**（16.3 的双分支构造管线：先归一化推理请求，再按 legacy/契约分流）；**为什么大部分模型要套 DeerFlow 的 patch 而不是直接用官方类**（16.5：多轮工具调用 + 思考模型的逐字回显契约，静默丢字段 = 请求被拒）；**谁来决定"哪个模型、给谁用"**（16.7：配置顺序决定默认，授权策略决定可用性，deny 之后还有一条优雅降级的 fallback 扫描）。
