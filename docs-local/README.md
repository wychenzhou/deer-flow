# docs-local — 本仓库自有文档

本目录存放 DeerFlow **fork（QcdocAgent）自有的文档与工具脚本**，与官方上游
`docs/`、`backend/docs/` 目录完全隔离，避免同步 `upstream/main` 时交叉。

## 目录结构

| 路径 | 内容 |
|------|------|
| `middleware/` | 中间件源码详解（8 篇，中文）；含[总索引](middleware/README.md)——链位 1–35 → 篇目映射 |
| `STARTUP.md` | 项目启动指南（中文） |
| `warm-sun-theme-plan.md` / `warm-sun-theme-design.md` | 活力暖阳主题实施计划与设计规范 |
| `checkpoint-tables.md` | LangGraph Checkpoint 表结构详解 |
| `scripts/` | 辅助脚本（聊天历史获取、SQL 查询） |

## 约定

- 本目录内容**不随上游同步**，属于 fork 维护者自有。
- 新增自有文档一律放入本目录，不要写进 `docs/` 或 `backend/docs/`。
- 技能包（`skills/kami`、`skills/dws`、`skills/qcdoc-finance`）是功能代码，
  按惯例保留在 `skills/` 下，不属于本目录。
