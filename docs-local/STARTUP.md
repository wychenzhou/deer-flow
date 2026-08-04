# DeerFlow 项目启动指南

本文档汇总了本项目所有可用的启动方式，覆盖本地开发、Docker 部署、以及单独启动各个服务。

---

## 1. 一键启动（推荐本地开发）

从项目根目录执行：

```bash
# 开发模式（带热重载）
make dev

# 生产模式（预构建，无热重载）
make start

# 后台运行
make dev-daemon
make start-daemon

# 停止所有服务
make stop
```

这会同时启动：
- **Gateway** (后端 API) → http://localhost:8001
- **Frontend** (Next.js) → http://localhost:3000
- **Nginx** (反向代理) → http://localhost:2026

---

## 2. 单独启动各服务

### 2.1 前端（Frontend）

```bash
cd frontend
pnpm dev          # 开发模式（Turbopack，热重载）
pnpm build        # 生产构建
pnpm start        # 生产启动
pnpm preview      # 构建后预览
```

环境变量（`frontend/.env`）：
```bash
# 直连 Gateway（不经过 nginx 时使用）
NEXT_PUBLIC_BACKEND_BASE_URL=http://localhost:8001
NEXT_PUBLIC_LANGGRAPH_BASE_URL=http://localhost:8001/api

# SSR 内部调用
DEER_FLOW_INTERNAL_GATEWAY_BASE_URL=http://localhost:8001
DEER_FLOW_TRUSTED_ORIGINS=http://localhost:3000,http://localhost:2026
```

### 2.2 后端（Backend）

```bash
cd backend

# 开发模式（热重载）
make dev

# 生产模式
make gateway

# 仅安装依赖
make install       # 等价于 uv sync

# 测试
make test
make test-blocking-io

# 代码检查
make lint
make format
```

实际执行的命令：
```bash
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload
```

### 2.3 Nginx（仅代理，前后端已在运行）

前后端已在运行时，单独启动 nginx 容器：

```bash
# 使用已生成的 docker nginx 配置
docker run -d --name deer-flow-nginx -p 2026:2026 \
  --add-host=host.docker.internal:host-gateway \
  -v "$(pwd)/docker/nginx/nginx.docker.conf:/etc/nginx/nginx.conf:ro" \
  nginx:alpine
```

常用操作：
```bash
docker logs -f deer-flow-nginx   # 查看日志
docker stop deer-flow-nginx      # 停止
docker restart deer-flow-nginx   # 重启
```

---

## 3. Docker 全量启动

```bash
# 初始化（拉取 sandbox 镜像）
make docker-init

# 启动所有 Docker 服务（frontend + gateway + nginx + 可选 provisioner）
make docker-start

# 查看日志
make docker-logs
make docker-logs-frontend
make docker-logs-gateway

# 停止
make docker-stop
```

底层命令：
```bash
cd docker
docker compose -p deer-flow-dev -f docker-compose-dev.yaml up --build -d
```

---

## 4. 端口说明

| 服务 | 端口 | 说明 |
|------|------|------|
| Gateway | 8001 | FastAPI + LangGraph Runtime |
| Frontend | 3000 | Next.js Dev Server |
| Nginx | 2026 | 统一入口（推荐访问） |

通过 nginx 访问时，所有请求走 `http://localhost:2026`：
- `/` → Frontend
- `/api/*` → Gateway REST API
- `/api/langgraph/*` → Gateway LangGraph Runtime

---

## 5. 环境依赖检查

```bash
make check       # 检查系统工具是否齐全
make doctor      # 诊断配置问题
```

必需工具：
- **Node.js 22+** + **pnpm 10.26.2+**
- **Python 3.12+** + **uv**
- **Docker**（可选，用于 sandbox / Docker 部署）

---

## 6. 配置文件

| 文件 | 作用 |
|------|------|
| `.env` | API Keys、数据库连接、CORS 等 |
| `config.yaml` | 模型、工具、sandbox、内存等业务配置 |
| `extensions_config.json` | MCP Servers、Skills 状态 |

初始化配置：
```bash
make setup       # 交互式配置向导
make config      # 从 example 生成 config.yaml
make config-upgrade  # 合并新版配置字段
```

---

## 7. 常用开发命令速查

```bash
# 根目录
make install     # 安装前后端依赖
make dev         # 启动全部服务（开发）
make stop        # 停止全部服务

# backend 目录
make dev         # Gateway dev + 热重载
make gateway     # Gateway 生产模式
make test        # 运行测试

# frontend 目录
pnpm dev         # Next.js dev + Turbopack
pnpm build       # 生产构建
pnpm check       # ESLint + TypeCheck
pnpm test        # Vitest
```
