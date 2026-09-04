# 17 Gateway API 与 IM 渠道

> 基于 DeerFlow 最新源码(本仓库 commit 2672e209,2026-09)编写。
>
> 本章覆盖文件:`backend/app/gateway/app.py`、`routers/`(threads、thread_runs、runs、models、
> console、channels、channel_connections、auth、mcp、memory、skills、agents、scheduled_tasks、
> subagents、subagent_batches、uploads、artifacts、feedback 等)、`trace_middleware.py`、
> `auth_middleware.py`、`csrf_middleware.py`、`authz.py`、`auth/pat.py`、`internal_auth.py`、
> `langgraph_auth.py`、`deps.py`、`services.py`、`backend/app/channels/`(manager、service、
> message_bus、store、run_policy、base 及各平台实现)、`packages/harness/deerflow/client.py`、
> `runtime/stream_bridge/`、`runtime/runs/worker.py`、`runtime/events/`、`docker/nginx/nginx.conf`。
> 素材书 coolclaws ch19(fastapi-gateway)+ ch20(im-channels),以当前代码为准校准。

旧书里,"Gateway"是一层薄薄的 HTTP 胶水:后端照搬 LangGraph Platform 的 `langgraph-api`
独立服务,把 `graph.astream()` 的裸事件流转发出去;IM 渠道则各自维护一个"轮询 agent"循环,
自己拼 prompt、自己存 session。这一版把两者都**推翻重做**了:**LangGraph 兼容运行时被直接
嵌进 FastAPI 进程**(`RunManager` + `run_agent()` + `StreamBridge`,见
`packages/harness/deerflow/runtime/`),Gateway 不再代理任何外部 agent 服务;IM 渠道也不再
自己当 agent,而是退化成**纯传输适配层**——通过和浏览器同款的 `langgraph-sdk` HTTP 客户端
把消息交给 Gateway,由服务端统一管理 thread/run 生命周期。两条主线在本章合并讲述。

---

## 1. Gateway 是什么:拓扑与内外两条入口

### 1.1 四个服务、一个入口

`make dev`(或 Docker 栈)一共起四个协作进程:

| 服务 | 端口 | 角色 |
|---|---|---|
| **Nginx** | `2026` | 唯一对外入口(反向代理),serve 前端 + 转发 API |
| **Gateway API** | `8001` | FastAPI REST API + **内嵌 LangGraph 兼容 agent 运行时** |
| **Frontend** | `3000` | Next.js 前端(容器内,不直接对外) |
| **Provisioner** | `8002` | 可选,仅沙箱配置为 provisioner/K8s 模式时启动 |

`docker-compose` 把入口发布为 `"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"`——**默认只绑
loopback**;Gateway 在容器内有意绑 `0.0.0.0:8001` 但**不发布**,8001 整个外部不可达。生产部署
(Nginx 直连)同样只有 2026 一个暴露面,`backend/tests/test_compose_default_bind_host.py` 把
这一点钉死在两个 compose 文件里。

### 1.2 Nginx 路由:一条 rewrite 拆出两条语义路径

Nginx 配置(`docker/nginx/nginx.conf`)是**路径级**路由,核心只有三块:

```
/api/langgraph/  → rewrite ^/api/langgraph/(.*) → /api/$1 → proxy_pass gateway:8001
/api/*(其余)      → 直接 proxy_pass gateway:8001(浏览器同样经此)
/ (非 API)        → proxy_pass frontend:3000
```

其中唯一"非平凡"的一块是 `/api/langgraph/*`:**先把前缀剥掉再转发**。于是同一个 FastAPI
进程同时服务两类客户端:

- **原生 REST 路径 `/api/*`**:前端、DeerFlowClient 等价方法、运维脚本直接打。
- **LangGraph SDK 路径 `/api/langgraph/*`**:任何把 `base_url` 指向
  `/api/langgraph` 的官方 `langgraph-sdk` 客户端(以及 IM 渠道内部的 `ChannelManager`),
  发出的 `POST /threads`、`POST /threads/{id}/runs` 等请求,经 rewrite 后落在 Gateway
  原生 routers 上——**线程、运行、checkpoint 全部由 Gateway 服务端创建与管理**,SDK 只是
  一个传输协议。

前端环境变量:`NEXT_PUBLIC_LANGGRAPH_BASE_URL` 默认 `/api/langgraph`(经 nginx),
`NEXT_PUBLIC_BACKEND_BASE_URL` 默认空(同源)。换句话说,**浏览器侧走的就是 LangGraph SDK
协议**;SDK 兼容不是"给外部用户的赠品",而是前端与 IM 渠道共用的第一公民路径。

Streaming 相关的代理细节:`proxy_buffering off`、`X-Accel-Buffering no`、
`proxy_read_timeout 600s`(长 run 不因 600s 超时断流)、`client_max_body_size 20M` +
`proxy_request_buffering off`(长粘贴 prompt 不被 nginx 1m 默认上限拦下,issue #3952);
`gzip` 只压缩文本资产,**SSE/字体/图片/音视频显式不压缩**(延迟优先)。

Nginx 之外还有一个极少人注意的兼容入口:`backend/langgraph.json` 声明
`auth.path = ./app/gateway/langgraph_auth.py:auth` 与
`http.app = ./app/gateway/langgraph_studio.py:langgraph_app`,让**独立 LangGraph
Studio / 直接 LangGraph Server 兼容模式**也能跑同一套 graph(`make_lead_agent`)、同一套
checkpointer(`runtime/checkpointer/async_provider.py`)与同一套 JWT/CSRF 认证规则。默认
Gateway 内嵌运行时**不加载**这个模块;它是为外部 LangGraph 工具链保留的第二启动形态。

---

## 2. 应用装配:middleware 栈、lifespan 与 routers 清单

`create_app()`(`app/gateway/app.py`)是唯一装配点,`app = create_app()` 供 uvicorn 导入。

### 2.1 Middleware 栈(自外向内)

```
TraceMiddleware      # 最外:每请求绑定 X-Trace-Id,response start 时写回(见 §8)
CORSMiddleware       # 仅当配置了 GATEWAY_CORS_ORIGINS 才挂(nginx 同源默认不需要)
CSRFMiddleware       # Double-Submit Cookie,state-changing 请求校验
AuthMiddleware       # 最内:fail-closed 认证网关,拒绝未认证访问非公开路径
```

- **AuthMiddleware 是兜底安全网**:`/health`、`/docs`、`/openapi.json`、`/api/webhooks/`(自证
  身份)等少量 `_PUBLIC_PATH_PREFIXES` 之外,未认证请求一律拒绝。它把 `request.state.user` /
  `request.state.auth` 先于任何 router 写好。
- **CSRF 与 CORS 共享同一套 origin 配置源**(`get_configured_cors_origins()`,
  `CORS_EXPOSED_HEADERS`),避免两套策略漂移。`CORS_EXPOSED_HEADERS` 必须暴露 `Content-Location`
  与 `X-Trace-Id`——run-creating 路由把 run id 放在 `Content-Location` 里,该头不在 CORS
  safelist,不暴露则浏览器 JS 读不到,`useStream` 的 `onCreated` 与 thread-gated 操作全部失效。
- 扩展(plugins)可额外贡献 middleware 与 FastAPI router;**扩展路由最后 include**,保证宿主
  路由(含 `/health`)优先;与宿主路由确定冲突(definite shadow)会被带诊断信息地拒绝。

### 2.2 Lifespan:startup 顺序即架构顺序

`lifespan()` 的启动序列值得按序读,因为它就是"运行时依赖图":

1. 读 `get_app_config()`(仅一次性 bootstrap;请求期配置一律走 `deps.get_config()`
   **热重载**,config.yaml 编辑不重启即生效);
2. 公共 skill projection、Monocle tracing(可选,失败只记日志不挡启动);
3. 后台重建记忆检索索引(不延迟就绪);
4. tiktoken 编码缓存预热(限 5s,失败降级字符计数);
5. 清理遗留 `.upload-*.part` 暂存文件;
6. `async with langgraph_runtime(app, startup_config)`:在此单例化 **StreamBridge、
   checkpointer、store、run-event store、run repository**(`deps.py`,restart-required);
7. admin bootstrap(首启无 admin → 提示访问 `/setup`;有 admin → 跑一次
   "no-auth → with-auth" orphan thread 迁移,把无 `user_id` 的 LangGraph store 行归到
   admin 名下);
8. `start_channel_service(...)`:启动 IM 渠道(见 §9),并把 `app.state.stream_bridge` 以
   零参闭包 `get_stream_bridge=` 传进去(GitHub follow-up watcher 靠它订阅 run 流);
9. `ScheduledTaskService` / `McpTaskService` / `SubagentBatchService`(按配置分别启动)。

关闭顺序与之镜像:先停渠道(限 5s,`shutdown_grace` 之外还要容忍超时),再停调度/MCP/batch
worker,最后 `langgraph_runtime` 退出时 drain 在飞 run 与内存 flush——每个 shutdown hook
都有 `_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0` 上界,防 uvicorn reload 反复向卡死的 worker 发信号。

### 2.3 Routers 清单(以 app/gateway/app.py 实际 include 为准)

| Router 模块 | 前缀 | 关键端点 |
|---|---|---|
| `models` | `/api/models` | `GET /` 列模型;`GET /{name}` 详情 |
| `features` | `/api/features` | `GET /` UI 能力开关 |
| `console` | `/api/console` | `GET /stats` `/runs` `/usage` 跨线程只读运营数据 |
| `mcp` | `/api/mcp` | `GET/PUT/PATCH /config`、`POST /config/servers`…(扩展配置热写) |
| `mcp_tasks` | `/api/threads/{id}/mcp-tasks` | durable MCP 任务查询/取消 |
| `subagent_batches` | `/api/threads/{id}/subagent-batches` | batch 查询/暂停/续跑/取消/retry/results.jsonl |
| `memory` | `/api/memory` | 全局记忆 CRUD + 搜索 |
| `skills` | `/api/skills` | 列表/启停/安装(`/install/upload` admin-only,100MiB 上限)/reload |
| `integrations` | `/api/integrations` | Lark/Feishu 托管 skill pack 安装与授权流转 |
| `artifacts` | `/api/threads/{id}/artifacts` | 产物读取/改写/打包 |
| `browser` | `/api/threads/{id}/browser` | 浏览器会话 + `ws /browser/stream` |
| `uploads` | `/api/threads/{id}/uploads` | 多文件上传(含 limits/list/删除) |
| `threads` | `/api/threads` | 见 §3.1 |
| `scheduled_tasks` | `/api/scheduled-tasks` | 任务 CRUD/pause/resume/trigger/runs |
| `agents` | `/api/agents` | 自定义 agent 管理 + `/api/agents.json` 类目录 |
| `subagents` | `/api/subagents` | admin 托管 worker CRUD |
| `suggestions` | `/api/threads/{id}/suggestions` | 追问建议 |
| `input_polish` | `/api/input-polish` | 输入润色 |
| `channel_connections` | `/api/channels` | 用户级连接:providers/connections/connect/runtime-config |
| `channels` | `/api/channels` | `GET /` 状态、`POST /{name}/restart` |
| `assistants_compat` | `/api/assistants` | LangGraph Platform assistants 兼容 stub(search/get/graph/schemas) |
| `auth` | `/api/v1/auth` | login/register/logout/me/pats/OIDC/setup |
| `feedback` | `/api/threads/{id}/runs/{rid}/feedback` | run 反馈 CRUD + stats |
| `thread_runs` | `/api/threads` | LangGraph Platform 兼容 runs 生命周期(§3.2) |
| `runs` | `/api/runs` | 无状态 runs:`POST /stream` `/wait`、`GET /{rid}/messages` |
| `github_webhooks` | `/api/webhooks/github` | 仅 `GITHUB_WEBHOOK_SECRET` 配置时挂载(fail-closed) |
| 扩展贡献 | — | `include_contributed_routers`(最后) |
| `/health` | app 级 | `{"status":"healthy","service":"deer-flow-gateway"}` |

> 注意两个"同名不同物":`threads` router 管 thread 文件系统/生命周期数据,`thread_runs`
> router 才是 LangGraph 兼容的 runs 生命周期;`channels`(运维状态)与
> `channel_connections`(用户级连接)分属两个文件、两个 prefix 语义。

---

## 3. 主要路由:threads / runs / models / console

### 3.1 Threads(`threads.py`)

LangGraph 兼容核心 + DeerFlow 扩展并存:

- 集合:`POST /api/threads`(创建,`metadata.user_id` 写入归属)、`POST /search`、
  `GET/PATCH /{thread_id}`、`DELETE /{thread_id}`;
- 生命周期数据:`POST /{id}/branches`、`POST /{id}/history`(返回可达分支历史)、
  `GET/POST /{id}/state`(手工读写 checkpoint)、`POST /{id}/compact`(手工压缩,
  复用 `DeerFlowSummarizationMiddleware` + 共享的 `reserve_checkpoint_write()` 边界);
- `GET/PUT/DELETE /{id}/goal`:目标持久化,`/goal` 命令在 IM 侧就是先 PUT 再当一轮 chat 投喂;
- 文件系统数据(thread-local):上传、产物、浏览器会话都以 `/{thread_id}` 为隔离根。

所有 thread 读取都带 `owner_check`:Gateway 把归属存为 LangGraph store 元数据里的
`metadata.user_id`,读时按当前用户过滤,写时注入——与 LangGraph Server 的 owner 语义一致。

### 3.2 Runs:一条 LangGraph Platform 兼容的完整生命周期(`thread_runs.py`)

这是"内嵌运行时"对外的核心面。路由清单即协议面:

- `POST /{thread_id}/runs` — 创建 run(标准 LangGraph Platform 语义,支持
  `multitask_strategy`、`checkpoint`/`checkpoint_id`、`if_not_exists` 等);返回 `RunResponse`,
  并在响应头 `Content-Location` 里带上 run id;
- `POST /{thread_id}/runs/stream` — 创建并立即 SSE 流式返回;
- `GET/POST /{thread_id}/runs/{run_id}/stream` — join 已存在 run 的流;GET 上允许
  `?action=interrupt|rollback&wait=0|1`(先取消再观察);
- `POST /{thread_id}/runs/{run_id}/cancel` — 取消;
- `POST /{thread_id}/runs/{run_id}/join` — 只读观察(见 §6 的
  `apply_on_disconnect=False`);
- `GET /{thread_id}/runs`、`GET /{thread_id}/runs/{run_id}` — 列表/详情;
- `GET /{thread_id}/messages`、`GET /{thread_id}/messages/page`、
  `GET /{thread_id}/runs/{run_id}/messages` — 消息读取(分页、按 run);
- `GET /{thread_id}/token-usage` — 线程累计 token;
- `GET /{thread_id}/runs/{run_id}/events`、`.../workspace-changes` — 审计事件(§7);
- `POST /{thread_id}/runs/regenerate/prepare`、`/edit-regenerate/prepare` — 重生成
  的准备阶段(定位源 checkpoint、校验 lineage,delta 模式下不能 fork 则线性化 resume);
- `POST /{thread_id}/runs/{run_id}/artifacts/archive` — 产物打包;
- `GET /{thread_id}/runs/{run_id}/feedback` 家族在 `feedback.py`。

无线程纯跑(stateless):`runs.py` 的 `POST /api/runs/stream` 与 `/wait`——不要求线程已存在,
`if_not_exists="create"` 时按需建线程。IM 的 fire-and-forget 渠道与 MCP 通知 run 走的就是
这条无状态入口。

### 3.3 Models(`models.py`)+ 模型授权

`GET /api/models` 从 `config.yaml` 的 `models` 列表返回脱敏元数据(`name`/`model`/
`display_name`/`supports_thinking`/`supports_reasoning_effort`),并带 `token_usage.enabled`。

当 `authorization.enabled=true` 时,该路由对每个已认证调用者做**模型级过滤**:
`resolve_model_authorization()` 解析缓存的 `AuthorizationProvider`,构造与
`resolve_route_permissions` 完全一致的 Principal,再 `provider.filter_resources(principal,
"model", names)` 只返回该角色可 `list` 的模型。provider 异常按 `fail_closed` 退化为空列表
或全量列表。route 侧另有第二道闸:run 请求携带 `model_name` 时,`services.start_run`
先查 `app_config.get_model_config(model_name)` 允许名单(未配置 → 400),`run_agent` 内部
`_authorize_model_name` 兜底(embedded/DeerFlowClient 同源),彻底杜绝"指定一个没配过的模型"
绕过计费与策略的可能。

### 3.4 Console(`console.py`):运营面直查

`GET /api/console/stats`(runs/threads/agents/tokens/cost 头计数)、`/runs`(带线程标题的
分页 run 历史,per-run cost)、`/usage`(零填充日 token 序列 + per-model 拆账)。它直接查询
`runs`/`threads_meta` 作为报表层,需要 SQL 后端(`database.backend: memory` → 503)。成本估算
读取可选的 `models[*].pricing`(`currency`/`input_per_million`/`output_per_million`/
`input_cache_hit_per_million`),且**缓存感知**:`RunJournal` 把 prompt-cache 命中单独记桶,
按命中价计费(未配置命中价 → 按未命中价计,保守上界);多币种混用则禁用成本列,避免聚合出
假数字。

---

## 4. 认证与授权

### 4.1 三层凭据

Gateway 认三种凭据,优先级与语义各不相同:

| 凭据 | 载体 | 说明 |
|---|---|---|
| **会话 Cookie** | `access_token` cookie(HttpOnly JWT) | 浏览器主路径;login 接受 `remember_me`,但 Gateway **从不存密码**(只存 hash)。`SessionCookiePolicy` 只在 HTTPS/受信转发 HTTPS、或 localhost 直连 HTTP 时才持久化 HttpOnly cookie |
| **PAT(Personal Access Token)** | `Authorization: Bearer dfp_...` | 程序化 API 的"API key"(§4.2)。**失效的 Bearer 直接 401,不回退 cookie**——这让 CSRF 中间件对 Bearer 请求的跳过是安全的 |
| **内部认证头** | `X-DeerFlow-Internal-Token` + `X-DeerFlow-Owner-User-Id` | 进程内受信调用(IM 渠道 worker、scheduler、MCP 通知),§9 详述 |

CSRF:Double-Submit Cookie(`csrf_token` cookie 与 `X-CSRF-Token` 头配对,`secrets.compare_digest`
比较),作用于所有 state-changing 方法;session 建立时与 CSRF cookie 一起按同一 `max_age`
过期(re-issue 成对)。登出清全部 cookie 并抑制登出响应的 CSRF 再发。

用户来源:`POST /api/v1/auth/register`、`POST /login/local`、OIDC(`GET /auth/providers`,
`/auth/oauth/{provider}`,回调解码)三种。**首启无任何账号**:`_ensure_admin_user` 检测
admin 数为 0 时不建号,打日志提示访问 `/setup`(`/api/v1/auth/setup-status` →
`POST /api/v1/auth/initialize` 建首个 admin)。后续启动则执行一次性的 orphan thread
迁移(no-auth → with-auth 升级路径)。

### 4.2 PAT:dfp_ 令牌、scopes 与默认拒绝的路由白名单

PAT 是 v1 唯一程序化凭据(`auth/pat.py`):

- 形态:`dfp_` + base62(32 CSPRNG bytes)(定宽 base62,高位补零,长度确定);
  创建响应**只显示一次**,库中只存 SHA-256 摘要;校验 = digest 索引查找 + 常数时间重比较,
  所有失败统一返回同一 401(响应不可当 oracle)。`last_used_at` 每 300s 节流写一次。
- **Scope 集合恰好是路由权限串**:`threads:read/write/delete` + `runs:create/read/cancel`
  (`PAT_ALLOWED_SCOPES`)。PAT 只能**收窄**其拥有者的权限,不能放大。
- **PAT 路由白名单是默认拒绝的**:`_PAT_ROUTE_RULES` 用 (method, regex) 显式列出 threads/
  runs 生命周期子路由——collection 规则、`goal`、`state`、`compact|history|branches`、
  runs 各子路由、stateless `/api/runs/(stream|wait)` 等。**未列入的已认证路由(哪怕持 admin
  的 PAT)一律 403**,这封死了"只拿一个读 scope 的 PAT 却能删记忆/建 agent/改渠道配置"的
  缺口;runs 子路由刻意不用 `runs(/.*)?` 通配,新增子路由默认拒绝直到显式列入。
- admin PAT 不携带 admin 能力(`ExtensionPrincipal` 里 `is_pat` 会压制 `is_admin` 与
  `admin` role 两个信号)。
- PAT 管理(`POST/GET/DELETE /api/v1/auth/pats`)与改密要求 session auth。

### 4.3 路由权限模型:`resource:action` + owner_check

`authz.py` 定义了 DeerFlow 版 LangGraph Auth 风格装饰器:

```python
@router.get("/{thread_id}")
@require_auth
@require_permission("threads", "read", owner_check=True)
async def get_thread(thread_id: str, request: Request): ...
```

权限全集:`threads:read/write/delete`、`runs:create/read/cancel`(外加工具侧的
`sandbox:execute` 与模型侧的 model 过滤,见 §3.3)。关键行为:

- `owner_check=True`:除权限外还校验资源归属当前用户(thread 的 `metadata.user_id`),
  管理员也不跨用户。
- `authorization.enabled=true` 时,每个已认证请求的权限由外部 `AuthorizationProvider`
  (config 的 `authorization` 段解析,provider 按配置签名缓存、热重载安全)逐条评估;
  `fail_closed=true` 时 provider 故障 → 该权限直接拒绝,反之回退全量。
- **内部调用者**(`system_role="internal"`)不是真实 RBAC 角色:构造 Principal 时把它
  pop 掉,落入 `default_role`——IM worker、scheduler 与普通用户走同一条授权策略。
- `runs:cancel` 会从多个入口泄漏:除了专门的 cancel 路由,join-stream 的
  `?action=interrupt|rollback` 与 run 创建的 `multitask_strategy=interrupt|rollback`
  同样终止活动 run。`require_cancel_permission_if()` 在这类"参数条件性取消"上补查,
  只读 PAT 无法借道取消。
- 授权是**热重载**的(`get_app_config().authorization`),provider 实例按 config 对象
  id + 内容签名缓存,只有内容真正变化才重新解析。

LangGraph Server 兼容模式(`langgraph_auth.py`)复用同一套 JWT/CSRF 规则:`@auth.authenticate`
校验 cookie → decode JWT → DB 查用户 → `token_version` 匹配;`@auth.on` 对写注入
`user_id` 元数据、对读按 `user_id` 过滤,保证外部 Studio 也看到与 Gateway 一致的线程隔离。

---

## 5. LangGraph 兼容的"内嵌运行时":RunManager + StreamBridge

Gateway 的 agent 运行不在请求协程里,而在**后台 asyncio.Task**(`runtime/runs/worker.py` 的
`run_agent()`),通过 `RunManager`(run 记录注册表、multitask 策略、断连语义)与
`StreamBridge`(生产/消费解耦,对齐 LangGraph Platform 的 Queue + StreamManager)把事件
送到 HTTP SSE 端点。

### 5.1 StreamBridge 抽象

`runtime/stream_bridge/base.py` 定义最小协议:

```python
@dataclass(frozen=True)
class StreamEvent:
    id: str      # 单调递增事件 id(直接用作 SSE id: 字段,支持 Last-Event-ID 重连)
    event: str   # SSE 事件名:metadata / values / messages / messages-tuple / error / end ...
    data: Any    # JSON 可序列化 payload

HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL       = StreamEvent(id="", event="__end__", data=None)
type StreamItem = StreamEvent | StreamGap
```

- `publish(run_id, event, data)` / `publish_end(run_id)`(生产者,worker 侧);
- `subscribe(run_id, *, last_event_id=None, heartbeat_interval=None)` → 异步迭代器(消费者
  侧):空闲超 `heartbeat_interval`(默认取启动配置 `stream_bridge.heartbeat_interval_seconds`)
  产出心跳;**落后于保留历史 → 产出 `StreamGap` 并停止**,绝不把"部分重放"伪装成完整流;
- `cleanup(run_id, *, delay)` — 生产端结束后的资源释放,`delay>0` 给迟到的订阅者留窗口。

实现两种:`memory.py`(进程内 ring buffer,支持 `Last-Event-ID` 断点重放,解析事件 id 到
本地序列;未知/落后 id 明确报 Gap 而非谎称完整;内存实现 `supports_cross_process=False`)
与 `redis.py`(跨 worker 的 Stream 后端,`supports_cross_process=True`,把大段重放批量化)。
`store_only` 的 run 记录(本 worker 不活动的跨进程 run)在无跨进程桥时 join 返回 409。

### 5.2 事件名:`values|ns` 命名与根帧纪律

worker 把 LangGraph `stream_mode` 名 1:1 映射为 SSE 事件名(`_lg_mode_to_sse_event`,
`"messages"` 就是 `"messages"`,`messages-tuple` 仅在客户端显式请求时使用;`events` 模式被
拒绝——它需要 `astream_events`,无法与 `values` 快照并存)。流式订阅可选
`stream_subgraphs`;开启时 subgraph 帧带 namespace,`_compose_sse_event()` 把事件名拼成
LangGraph Platform 风格的 `values|<ns>`(`"|".join((sse_event, *namespace))`,SDK 端
`event.split("|")[1:]` 还原)。关键纪律(issue #4399):**委派 subagent 继承父 checkpoint
namespace,若把它伪造成裸 `values`,会整体替换 SDK 客户端的线程视图**——所以 subgraph 帧
保留 namespace;根消费者(文件工具 chunk 批处理器、subagent 事件持久化、LLM 错误回退检测)
**忽略带 namespace 的帧**;Web 前端不请求 subgraph 流,子任务进度走根 namespace 的
`task_*` 自定义事件。

worker 侧事件面:`metadata`(run 元数据,含 trace id)、`values`(节点级完整 state 快照,
可选而非批处理前提)、`messages`(LLM token delta)、`error`(LLM provider 重试耗尽,带
fallback 文案)、`end`。`write_file`/`str_replace` 参数 delta 以**有界批次**发
(多模式 `messages-tuple` 消费者);单模式消息消费者保留逐 chunk 原契约。

### 5.3 SSE 消费端(`services.py::sse_consumer`)

`StreamingResponse(media_type="text/event-stream")`,响应头
`Cache-Control: no-cache` + `X-Accel-Buffering: no`。消费循环:

1. 读请求头 `Last-Event-ID`,原样传给 `bridge.subscribe(record.run_id, last_event_id=...)`;
2. 心跳 → `": heartbeat\n\n"` 注释帧(防代理/浏览器超时断开);
3. `END_SENTINEL` → `end` 帧收尾;
4. `StreamGap` → 发 `gap` 事件(`code: stream_replay_gap`,携带 requested/
   earliest/latest event id 与 `recovery: reload_durable_state`),然后停止——不假装续上;
5. 其余帧 → `format_sse(entry.event, entry.data, event_id=entry.id)`,SSE 行格式带
   `id:` / `event:` / `data:`。

**on_disconnect 语义只在创建者端点生效**(`apply_on_disconnect=True`):
`multitask_strategy`/`on_disconnect` 取 `cancel` 时客户端断连即取消后台 task,
`continue` 时 run 继续、事件丢弃。**join/observer 流传
`apply_on_disconnect=False`**:观察者断连绝不触发取消——否则一个只持 `runs:read` 的凭据
只要断开连接就能取消 run。GET join 的 `?action=interrupt|rollback` 在查 run 之前就被
`_reject_get_stream_action` 挡掉(SameSite=Lax 下跨站顶级导航仍带 cookie,不能把 GET 变
成取消通道)。浏览器重连 = 重新带 `Last-Event-ID` subscribe;事件 id 单调递增由桥维护。

---

## 6. 无状态/有状态双 run 入口与多任务策略

一条 run 的生命周期状态机由 `RunManager` 持有:创建时注册 `RunRecord`(含 `store_only`)、
结束/清理时注销;`GET /{thread_id}/runs/{run_id}/join` 与 `stream_existing_run` 通过
`run_mgr.get(run_id)` 校验 run 确实存在且属于该线程再订阅。创建端(stream 或 wait)支持
`multitask_strategy`:默认 `reject`(线程忙 → 409 `ConflictError`),另有 `interrupt`/
`rollback`(终止旧 run 再跑,`?action=` 与 `multitask_strategy` 都要求 `runs:cancel`)与
`enqueue`。IM 渠道的 `THREAD_BUSY_MESSAGE` 正是 409 的用户侧翻译。

wait 端点(`POST .../runs/wait`、stateless `/api/runs/wait`)复用同一桥的订阅直到 `end`,
返回最终 state。所有端点经 `services.py::start_run` 统一入口:它做 run 元数据脱敏
(`redact_config_secrets` 擦掉 `runs.kwargs_json` 里的 secrets)、server 端 stamp
`run_id`/trace id、把内部 run 的 owner 解析正确,并负责幂等键(调度/MCP 通知重试复用同一
durable run)。

---

## 7. 事件与审计:RunEventStore

SSE 是瞬时的;**审计面在 `RunEventStore`**(`runtime/events/store/`,后端 db/jsonl/memory,
`run_events.backend` 配置;多实例可靠性语义要求 db)。持久化事件是**服务端挑选过的**固定
定义集(`runtime/events/catalog.py`):journal 类(run 生命周期/LLM 记账)、subagent 类
(`task_*` 生命周期,`SubagentResult.external_task_id` 关联)、workspace-changes 类——
类别/类型有长度上限校验,事件在 worker 里**按批缓冲落库**(#3779,低频率 DB 写),消息经
`_MessageSeqStamper` 打线程内递增 `seq`。

读取面:`GET /api/threads/{id}/runs/{rid}/events`(debug/audit),支持
`event_types`(逗号分隔)、`task_id` + `after_seq` 组合分页——subtask 卡片能翻单个子代理
的步骤而不被 run 级 `limit` 截尾(#3779);返回前对 `metadata` 做 secret 擦除。另外
`GET .../workspace-changes` 列出该 run 的沙箱文件变更事件。控制台(`/api/console/*`)是
跨线程的只读运营视图(要求 SQL 后端),run 历史、token 用量与成本都在那里聚合。

---

## 8. Trace:`X-Trace-Id` 贯穿所有入口

`TraceMiddleware`(`trace_middleware.py`)故意**无条件挂载**:trace id 必须在每条路径上存在,
让下游(worker 的 run 元数据、委派 subagent、后台记忆线程)只读一个 ContextVar,而不是到处
"if 可能有 trace id"。要点:

- 读入站 `X-Trace-Id`(有则沿用,无则生成),`request_trace_context()` 包住整请求;
  **HTTP 入口刻意不继承**——伪造头不得回退到上一请求的 id;
- **`http.response.start` 时写回响应头**(不是响应结束时):覆盖 SSE 等流式响应且不消费 body;
  `X-Trace-Id` 在 `CORS_EXPOSED_HEADERS` 里(非 safelist,split-origin 客户端要显式暴露);
- 未处理异常由本中间件自己发一个带头的 500(它比 `ServerErrorMiddleware` 更靠外,那个
  500 不带任何用户中间件)→ 用户最需要关联日志的那条响应也有 id;该 500 是 CORS-opaque
  的,刻意不修(在 CORSMiddleware 外复刻 origin 白名单会让两套策略漂移);
- `logging.enhance.enabled` 只决定日志记录是否打印该字段,不影响 id/header/run 元数据,
  所以本中间件不读 AppConfig、不卷入"logging 需重启"的契约。

**ContextVar 是唯一事实源**,HTTP 之外的每个入口都要自己绑:`ScheduledTaskService` 派发、
`launch_mcp_task_notification_run`、IM 渠道 `ChannelManager._worker_loop`
(**每条 inbound 消息一个 scope**,worker task 长驻复用,scope 必须随消息关闭不能泄漏到
下一条)、`DeerFlowClient.stream()`(逐 `next()` 绑,绝不在 yield 间持有——sync generator
共享调用方 context)。`worker._bind_trace_id` 把它 stamp 进 runtime context 与
`config["metadata"]`,`start_run` stamp 进 run 记录;**客户端塞的 `deerflow_trace_id`
(body.metadata / body.config.context)一律替换**,否则持久化的 run 会与响应头和日志不一致。
线程元数据刻意不含 trace key——一个线程横跨多次 run。崩溃恢复的调度 launch 复用原 run
而不重 stamp(记录保留第一次的 id,重试日志用新 id),这是接受的偏离。

---

## 9. DeerFlowClient:内嵌双路径对比

`packages/harness/deerflow/client.py` 提供**同一进程内、无 HTTP** 的编程入口,返回类型与
Gateway API response schema 对齐(consumer 代码在两种模式下可互换)。两条流式路径**刻意
并行不共享**(docs/STREAMING.md 逐条否决过三种合并方案):

| 维度 | Gateway 路径 | DeerFlowClient 路径 |
|---|---|---|
| 入口 | FastAPI `/runs/stream` 等端点 | `DeerFlowClient.chat()/stream()` |
| 执行模型 | async + `agent.astream()` | sync generator + `agent.stream()` |
| 事件传输 | StreamBridge + `sse_consumer` | 直接 `yield` |
| 序列化 | 纯 JSON dict(LangGraph Platform wire 格式) | 原生 LangChain 对象 |
| 消费者 | 前端 `useStream`、IM 渠道、LangGraph SDK | Jupyter、脚本、测试 |
| 生命周期 | RunManager:run_id、断连语义、multitask、heartbeat | 每次 `stream()` 轻量 run_id,函数返回即结束 |
| 断连恢复 | `Last-Event-ID` 重连 | 不需要 |

核心语义:`chat()` 同步累积各 message-id 的 delta 返回最终 AI 文本;`stream()` 订阅
`stream_mode=["values","messages","custom"]` 产出 `StreamEvent(type, data)`:
`values` = 节点级完整 state 快照(已由 messages 模式送过的 AI 文本**不重复合成**)、
`messages-tuple` = 按 id concat 的 delta 或一次性 tool call/result、`custom` = 显式
`StreamWriter` 事件(同时经 callback 双发为 `astream_events(v2)` 的 `on_custom_event`)、
`end` = 收尾(携带按 message id 计一次的累计 usage)。每个 `stream()` 维护
`seen_ids/streamed_ids/counted_usage_ids` 三组 id,分别守"同 id 不重放 / 不重复合成 /
usage 只计一次"三个独立不变式。内部直接复用 `make_lead_agent` 同款
`create_agent()` + `build_middlewares()`;`list_models()`/`get_skill()`/`set_goal()` 等
Gateway 等价方法一应俱全,`reset_agent()` 在记忆/技能变更后强制重建 agent。

---

## 10. IM 渠道:从传输适配到统一 agent 服务

### 10.1 架构总览

IM 渠道(`app/channels/`)把外部平台接进同一个 agent。与旧书最大的不同:**渠道不再创建或
维护任何 agent 会话**,只做消息搬运——平台实现 → `MessageBus` → `ChannelManager` worker
池 → `langgraph-sdk` HTTP 客户端 → Gateway(线程/run 全在服务端)。渠道与前端共享同一条
LangGraph SDK 协议路径,天然获得线程隔离、checkpoint、记忆、工具执行的全部能力。

支持平台:Feishu(飞书)、Slack、Telegram、Discord、DingTalk、WeChat、WeCom、Buzz(Nostr
relay,需 `buzz` extra)以及 **GitHub(webhook 驱动,不轮询)**。配置在 `config.yaml` 的
`channels` 段;服务由 Gateway lifespan 里的 `start_channel_service(startup_config,
get_stream_bridge=...)` 启动(§2.2)。

内部链路(`channels/AGENTS.md` 的 Message Flow):

1. 平台事件 → 各平台 `Channel` 实现 → `MessageBus.publish_inbound()`
   (`InboundMessage`:channel_name/chat_id/user_id/text/msg_type/files/metadata/…);
2. `ChannelManager._worker_loop()` 从队列消费——**每条消息包一个
   `with ensure_trace_context()`**(worker task 是长驻复用的,scope 必须随消息关闭);
3. 查 `store.py` 的 JSON 映射 `channel_name:chat_id[:topic_id]` → `thread_id`
   (无映射则经 `client.threads.create()` 建线程);`topic_id=None` 时每条消息新开线程(一次性
   问答),同 chat 同 topic 复用同一 DeerFlow 线程;
4. 分发到 chat 或 command(`/new` `/status` `/models` `/memory` `/goal` `/help` 等
   `KNOWN_CHANNEL_COMMANDS`,`/goal` 会先 PUT 目标再当 chat turn 投喂);命令本地处理或查
   Gateway API;
5. outbound:经 `OutboundMessage` → 渠道回调 → 平台回复(§10.3)。

**去重**:每条 inbound 有一个 dedupe key(平台 delivery id 优先),写入
`InboundDedupeStore`(内存/Redis/SQLite 后端,Ttl 10 分钟、4096 条上限,worker 内防重放;
worker 取消/异常会**释放** key,让平台重投可重试)。**幂等关闭**:streaming 失败吞掉时,
先发最终 outbound 再释放 dedupe key——平台重投不会抢跑终答。

### 10.2 内部认证:渠道即受信内部调用者

渠道 worker 的 SDK 客户端(`_get_client()`)注入进程内内部认证:

```python
get_client(url=self._langgraph_url, headers={
    **create_internal_auth_headers(),          # X-DeerFlow-Internal-Token
    CSRF_HEADER_NAME: self._csrf_token,
    "Cookie": f"{CSRF_COOKIE_NAME}={self._csrf_token}",   # 与头配对的 CSRF cookie
})
```

`internal_auth.py`:`X-DeerFlow-Internal-Token` 携带进程内令牌(env
`DEER_FLOW_INTERNAL_AUTH_TOKEN`,未设则随机生成),`AuthMiddleware` 验证后 stamp 一个
`system_role="internal"` 的合成用户。它还可以带 `X-DeerFlow-Owner-User-Id`:owner id 经
`make_safe_user_id` 归一化后成为 run 的 `user_id`(**文件存储/记忆/技能按 owner 落桶**,
Feishu `ou_...`、Telegram `-100...` 等平台 id 无法借 header 逃逸存储桶),平台原始用户 id
保留为 `channel_user_id` 只进 runtime context(不进 checkpoint 的 `configurable`),并经
`bash_tool` 以 `export DEERFLOW_CHANNEL_USER_ID=<id>; ` 命令前缀注入沙箱(按命令注入,
群聊一人一线程的语义才正确)。authorization 开启时内部调用者同样过 `default_role`
(§4.3),不是特权旁路。

### 10.3 三种 outbound 模式与 ChannelRunPolicy

不同渠道的"回复"能力天差地别,`ChannelManager` 按策略分发(`ChannelRunPolicy`,
`run_policy.py` 注册表;webhook 渠道在 import 时自注册):

| 模式 | 机制 | 适用 |
|---|---|---|
| 流式编辑 | `runs.stream(["messages-tuple","values"])` → 就地更新 | Feishu(card 原地 patch)、Telegram(`editMessageText`,非最终更新节流:私聊 1s/群 3s,4096 截断)、DingTalk AI Card(`PUT /v1.0/card/streaming`,失败回落 `sampleMarkdown`) |
| 一次性等待 | `runs.wait()` → 取最终回复发一次 | Slack、Discord |
| fire-and-forget | `runs.create()` 返回 pending 即走,不等待不代发 | GitHub(agent 自己在沙箱里用 `gh` 回帖)、配置了 `fire_and_forget=True` 的渠道(绕开 SDK 300s `httpx.ReadTimeout`) |

`ChannelRunPolicy` 字段:`is_interactive`(False 时禁 `ask_clarification`)、
`default_recursion_limit`(webhook 长 run 放宽)、`credentials_provider`(给 agent 铸平台
token)、`requires_bound_identity`(webhook 渠道跳过 `/connect` 绑定门)、
`serialize_thread_runs`(Feishu 同线程 follow-up 排队而非撞 busy)、
`buffer_followups_on_busy`(busy 期间评论入缓冲,run 结束经 StreamBridge watcher
`END_SENTINEL` 后合并成 `<followups-while-busy>` 输入补发,最多 20 条/线程)。

**流式文本必须白名单化**(`_accumulate_stream_text`,AGENTS.md 记录的活体事故):只允许
assistant 消息类型(`ai` / `AIMessageChunk` / `assistant` 前缀匹配,绝不 substring——普通
单词里全是 "ai")进入展示文本;`DynamicContextMiddleware` 注入的隐藏记忆 HumanMessage、
`DurableContextMiddleware` 的 `<durable_context_data>`、被改写的用户 turn 都是 `human`,
一旦放行就会把记忆块/用户原话当助手回复发到 IM。`messages-tuple` 每帧是
`[message_dict, metadata]` 二元组。

`ChannelManager` worker 池:默认 `max_concurrency=5`、`inbound_queue_maxsize=1000`;
stop 先 `begin_shutdown()` 关 admission,留 `shutdown_grace_period_seconds=3.0` 让已接收
消息跑完(平台 ack 后通常有终答),再取消 worker;follow-up watcher 立即取消。Feishu 开
`serialize_thread_runs` 时同线程 turn 在 manager 内排队。

### 10.4 用户级连接(channel_connections)

`config.yaml` 的 `channel_connections`(默认关)在渠道之上加一层**用户绑定**:Telegram
深链 `/start <code>`、其余平台 `/connect <code>`(code = `secrets.token_urlsafe(16)`,
600s TTL、一次性、只在发起浏览器显示),把外部身份 `(provider, external_account_id,
workspace_id)` 绑到 DeerFlow 用户。**单活跃 owner 语义**由 DB 部分唯一索引
`uq_channel_connection_active_identity`(WHERE status != 'revoked')在库层保证,并发绑定
只有一个能 `connected`——最新绑定赢,旧的被 revoke(ownership transfer)。绑定 code 在
`allowed_users` 过滤**之前**消费(新 allowlist 用户可借浏览器流自举首次绑定),因此
`allowed_users` 不是绑定期防线,绑定安全靠 code 机密性。绑定后消息的
`owner_user_id` 成为 run 的 user_id、文件/记忆按 owner 落桶。

### 10.5 GitHub:webhook 驱动的特例

GitHub 渠道无长连接轮询:`POST /api/webhooks/github` 收 delivery,HMAC
(`X-Hub-Signature-256`,`GITHUB_WEBHOOK_SECRET`)验真;路由**未配 secret 就 404**(fail-closed,
`github_webhooks.is_route_enabled()`),且豁免 auth/CSRF(自证身份)。webhook 路由器验真后
`fanout_event` 按 agent 绑定(仓库/事件触发器,`preferred_thread_id = UUID5(repo, number,
agent_name)` 确定性线程)把每个绑定发一条 `InboundMessage` 进 `MessageBus`。outbound
log-only;注册在 `app.gateway.github.run_policy` 的 `ChannelRunPolicy` 配
`fire_and_forget=True` + `buffer_followups_on_busy=True`。registry 缓存按 agent store 的
不透明签名失效(文件存储看 mtime,DB 存储 hash owner/name/config/soul 内容)。

---

## 11. 实操要点速查

- 本地直连 Gateway:`http://localhost:8001`(`make gateway`),health `GET /health`;浏览器一律走 `http://localhost:2026`。
- 关 `/docs`:`GATEWAY_ENABLE_DOCS=false`;CORS:`GATEWAY_CORS_ORIGINS`(精确 origin 列表,CORS 与 CSRF 同源)。
- 给脚本开 API 权限:session 下 `POST /api/v1/auth/pats` 建 `dfp_` token,scope 按需 `threads:read` 等;所有请求带 `Authorization: Bearer dfp_...`。
- 排查 run 问题:先取 `X-Trace-Id`(所有响应都有),再 `GET .../runs/{rid}/events` 看持久化事件;SSE 断线重连带 `Last-Event-ID`。
- IM 渠道排障顺序:Gateway 日志的 `[Manager] received inbound` → 线程映射(store JSON)→ run 是否创建;`channels.langgraph_url`(`DEER_FLOW_CHANNELS_LANGGRAPH_URL`,Docker 内应为 `http://gateway:8001/api`)是常见配置错位点。
- 生产安全面:只有 2026 被发布;内部头令牌请显式设 `DEER_FLOW_INTERNAL_AUTH_TOKEN`(默认随机,重启即变);GitHub webhook 不配 secret 时路由不存在。
