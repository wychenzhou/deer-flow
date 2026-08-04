# DeerFlow 项目启动指南

本文档汇总了本项目所有可用的启动方式，覆盖本地开发、Docker 部署、以及单独启动各个服务。

> **说明**：本指南不使用 `make` 命令，全部为传统命令行方式。
> 若机器上没有安装 `make`，或遇到 `make` 无法运行的环境（如部分 Windows 环境），请按本指南操作。

---

## 1. 一键启动（推荐本地开发）

从项目根目录执行：

```bash
# 开发模式（带热重载）
./scripts/serve.sh --dev

# 生产模式（预构建，无热重载）
./scripts/serve.sh --prod

# 后台运行
./scripts/serve.sh --dev --daemon
./scripts/serve.sh --prod --daemon

# 停止所有服务
./scripts/serve.sh --stop
```

> 在 Windows 的 cmd / PowerShell 下请改用：
> ```bat
> call scripts\run-with-git-bash.cmd ./scripts/serve.sh --dev
> ```

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

> 若 `pnpm` 命令不可用，可使用 Corepack 托管的版本：
> `corepack pnpm dev`（前提是按项目的 `packageManager` 字段启用了 Corepack）。

### 2.2 后端（Backend）

```bash
cd backend

# 开发模式（热重载）
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload

# 生产模式（无热重载）
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001

# 仅安装依赖
uv sync

# 测试
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest -m "not live" tests/ -v
PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest tests/blocking_io -q --tb=short

# 代码检查
uv run ruff check .
uv run ruff format --check .

# 代码格式化
uv run ruff check . --fix && uv run ruff format .
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
./scripts/docker.sh init

# 启动所有 Docker 服务（frontend + gateway + nginx + 可选 provisioner）
./scripts/docker.sh start

# 查看日志
./scripts/docker.sh logs
./scripts/docker.sh logs --frontend
./scripts/docker.sh logs --gateway

# 停止
./scripts/docker.sh stop
```

> 在 Windows 的 cmd / PowerShell 下请改用：
> ```bat
> call scripts\run-with-git-bash.cmd ./scripts/docker.sh start
> ```

底层命令（手动等价）：
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
# 检查系统工具是否齐全
python ./scripts/check.py

# 诊断配置问题
cd backend && uv run python ../scripts/doctor.py
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
# 交互式配置向导
cd backend && uv run python ../scripts/setup_wizard.py

# 从 example 生成 config.yaml（已存在则中止）
python ./scripts/configure.py

# 合并新版配置字段（Git Bash）
./scripts/config-upgrade.sh
```

---

## 7. 常用开发命令速查

```bash
# 根目录
python ./scripts/check.py        # 检查系统依赖
./scripts/serve.sh --dev         # 启动全部服务（开发）
./scripts/serve.sh --stop        # 停止全部服务

# backend 目录
cd backend
uv sync                          # 安装依赖
PYTHONPATH=. uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload   # Gateway dev + 热重载
PYTHONPATH=. uv run pytest -m "not live" tests/ -v                                    # 运行测试

# frontend 目录
cd frontend
pnpm dev         # Next.js dev + Turbopack
pnpm build       # 生产构建
pnpm check       # ESLint + TypeCheck
pnpm test        # 单元测试
```
