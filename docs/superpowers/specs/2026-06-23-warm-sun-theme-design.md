# 活力暖阳主题设计方案

## 概述

为 DeerFlow 前端应用设计「活力暖阳」主题风格，基于品牌橙色 #F37F3E，搭配珊瑚红和明黄点缀，营造年轻活泼、温暖明快的视觉感受。

## 目标

- 将品牌色 #F37F3E 融入整体配色体系（目前仅用于聚焦高亮）
- Light 模式从冷调中性白切换为暖白底
- Dark 模式从冷灰调切换为暖棕调
- **零功能风险**：仅修改 CSS 变量，不改动任何组件逻辑代码

## 改动范围

**唯一修改文件：** `frontend/src/styles/globals.css`

修改 `:root`（Light 模式）和 `.dark`（Dark 模式）两个选择器块内的 CSS 变量值。所有 Shadcn UI 组件通过 Tailwind CSS 的 `@theme inline` 映射自动继承新配色，无需修改组件代码。

## 配色方案

### Light 模式 (`:root`)

| Token | 当前值 | 新值 | 说明 |
|-------|--------|------|------|
| `--radius` | `0.625rem` | `0.625rem` | 不变 |
| `--background` | `#ffffff` | `oklch(0.985 0.006 75)` | 纯白 → 暖白 |
| `--foreground` | `oklch(0.145 0 0)` | `oklch(0.145 0.005 50)` | 微调暖调 |
| `--card` | `#ffffff` | `oklch(0.985 0.006 75)` | 与背景同色 |
| `--card-foreground` | `oklch(0.145 0 0)` | `oklch(0.145 0.005 50)` | 与前景同色 |
| `--popover` | `#ffffff` | `oklch(0.985 0.006 75)` | 暖白 |
| `--popover-foreground` | `oklch(0.145 0 0)` | `oklch(0.145 0.005 50)` | 与前景同色 |
| `--primary` | `oklch(0 0 0)` | `oklch(0.67 0.18 50)` | 黑色 → 品牌橙 #F37F3E |
| `--primary-foreground` | `oklch(0.985 0 0)` | `oklch(0.99 0 0)` | 保持白色 |
| `--secondary` | `oklch(0.9455 0.0098 87.47)` | `oklch(0.95 0.025 70)` | 中性灰 → 暖桃 |
| `--secondary-foreground` | `oklch(0.205 0 0)` | `oklch(0.35 0.06 50)` | 深暖棕 |
| `--muted` | `oklch(0.97 0.0098 87.47)` | `oklch(0.97 0.012 70)` | 中性浅灰 → 暖浅 |
| `--muted-foreground` | `oklch(0.556 0 0)` | `oklch(0.55 0.01 50)` | 微暖灰 |
| `--accent` | `oklch(0.94 0.0098 87.47)` | `oklch(0.95 0.025 70)` | 中性灰 → 暖桃 |
| `--accent-foreground` | `oklch(0.205 0 0)` | `oklch(0.35 0.06 50)` | 深暖棕 |
| `--destructive` | `oklch(0.577 0.245 27.325)` | `oklch(0.68 0.19 25)` | 标准红 → 珊瑚红 |
| `--border` | `oklch(0.922 0.0098 87.47)` | `oklch(0.93 0.012 70)` | 暖调边框 |
| `--input` | `oklch(0.88 0.0098 87.47)` | `oklch(0.90 0.015 70)` | 暖调输入框边框 |
| `--ring` | `#F37F3E` | `#F37F3E` | 不变，品牌色 |
| `--chart-1` | `oklch(0.646 0.222 41.116)` | `oklch(0.67 0.18 50)` | 品牌橙 |
| `--chart-2` | `oklch(0.6 0.118 184.704)` | `oklch(0.68 0.19 25)` | 珊瑚红 |
| `--chart-3` | `oklch(0.398 0.07 227.392)` | `oklch(0.87 0.17 95)` | 明黄 |
| `--chart-4` | `oklch(0.828 0.189 84.429)` | `oklch(0.78 0.12 55)` | 杏色 |
| `--chart-5` | `oklch(0.769 0.188 70.08)` | `oklch(0.62 0.17 22)` | 深珊瑚 |
| `--sidebar` | `#ffffff` | `oklch(0.985 0.006 75)` | 暖白 |
| `--sidebar-foreground` | `oklch(0.145 0 0)` | `oklch(0.145 0.005 50)` | 暖黑 |
| `--sidebar-primary` | `oklch(0.205 0.0098 87.47)` | `oklch(0.67 0.18 50)` | 品牌橙 |
| `--sidebar-primary-foreground` | `oklch(0.985 0 0)` | `oklch(0.99 0 0)` | 白色 |
| `--sidebar-accent` | `oklch(0.925 0.0098 87.47)` | `oklch(0.95 0.025 70)` | 暖桃 |
| `--sidebar-accent-foreground` | `oklch(0.205 0 0)` | `oklch(0.35 0.06 50)` | 深暖棕 |
| `--sidebar-border` | `oklch(0.922 0.0098 87.47)` | `oklch(0.93 0.012 70)` | 暖调边框 |
| `--sidebar-ring` | `oklch(0.708 0 0)` | `oklch(0.67 0.18 50)` | 品牌橙 |

### Dark 模式 (`.dark`)

| Token | 当前值 | 新值 | 说明 |
|-------|--------|------|------|
| `--background` | `oklch(0.24 0.0036 106.64)` | `oklch(0.18 0.01 50)` | 冷灰暗 → 暖棕暗 |
| `--foreground` | `oklch(0.985 0 0)` | `oklch(0.97 0.005 70)` | 纯白 → 暖白 |
| `--card` | `oklch(0.238 0.0036 106.64)` | `oklch(0.20 0.012 50)` | 暖棕卡片 |
| `--card-foreground` | `oklch(0.985 0 0)` | `oklch(0.97 0.005 70)` | 暖白 |
| `--popover` | `oklch(0.205 0.0036 106.64)` | `oklch(0.18 0.012 50)` | 暖棕深 |
| `--popover-foreground` | `oklch(0.985 0 0)` | `oklch(0.97 0.005 70)` | 暖白 |
| `--primary` | `oklch(1 0 0)` | `oklch(0.72 0.16 55)` | 白色 → 暖橙（暗色下提亮） |
| `--primary-foreground` | `oklch(0.205 0 0)` | `oklch(0.15 0.01 50)` | 深色文字 |
| `--secondary` | `oklch(0.3 0.0036 106.64)` | `oklch(0.27 0.02 50)` | 冷灰 → 暖棕 |
| `--secondary-foreground` | `oklch(0.985 0 0)` | `oklch(0.90 0.02 70)` | 暖调文字 |
| `--muted` | `oklch(0.269 0.0036 106.64)` | `oklch(0.24 0.015 50)` | 冷灰 → 暖棕浅 |
| `--muted-foreground` | `oklch(0.708 0 0)` | `oklch(0.68 0.01 50)` | 暖灰 |
| `--accent` | `oklch(0.32 0.0036 106.64)` | `oklch(0.27 0.02 50)` | 冷灰 → 暖棕 |
| `--accent-foreground` | `oklch(0.985 0 0)` | `oklch(0.90 0.02 70)` | 暖调文字 |
| `--destructive` | `oklch(0.704 0.191 22.216)` | `oklch(0.72 0.18 25)` | 珊瑚红（暗色下提亮） |
| `--border` | `oklch(1 0 0 / 10%)` | `oklch(1 0.01 70 / 10%)` | 微暖边框 |
| `--input` | `oklch(1 0 0 / 15%)` | `oklch(1 0.01 70 / 15%)` | 微暖输入框 |
| `--ring` | `transparent` | `oklch(0.72 0.16 55)` | 暗色模式也显示橙色聚焦环 |
| `--chart-1` | `oklch(0.488 0.243 264.376)` | `oklch(0.72 0.16 55)` | 暖橙 |
| `--chart-2` | `oklch(0.696 0.17 162.48)` | `oklch(0.72 0.17 25)` | 珊瑚红 |
| `--chart-3` | `oklch(0.769 0.188 70.08)` | `oklch(0.85 0.15 95)` | 明黄 |
| `--chart-4` | `oklch(0.627 0.265 303.9)` | `oklch(0.75 0.10 55)` | 杏色 |
| `--chart-5` | `oklch(0.645 0.246 16.439)` | `oklch(0.68 0.15 22)` | 深珊瑚 |
| `--sidebar` | `oklch(0.245 0.0036 106.64)` | `oklch(0.18 0.012 50)` | 暖棕暗 |
| `--sidebar-foreground` | `oklch(0.985 0 0)` | `oklch(0.97 0.005 70)` | 暖白 |
| `--sidebar-primary` | `oklch(0.488 0.243 264.376)` | `oklch(0.72 0.16 55)` | 暖橙 |
| `--sidebar-primary-foreground` | `oklch(0.985 0 0)` | `oklch(0.99 0 0)` | 白色 |
| `--sidebar-accent` | `oklch(0.29 0.0036 106.64)` | `oklch(0.27 0.02 50)` | 暖棕 |
| `--sidebar-accent-foreground` | `oklch(0.985 0 0)` | `oklch(0.90 0.02 70)` | 暖调 |
| `--sidebar-border` | `oklch(1 0 0 / 10%)` | `oklch(1 0.01 70 / 10%)` | 微暖 |
| `--sidebar-ring` | `oklch(0.556 0 0)` | `oklch(0.72 0.16 55)` | 暖橙 |
| `font-weight: 300` | 保留 | 保留 | 不变 |

## 不修改的内容

- **圆角** (`--radius`): 保持 `0.625rem`
- **动画**: 所有 keyframes 和 animation 变量不变
- **容器宽度**: `--container-width-*` 不变
- **组件代码**: 不修改任何 `.tsx`/`.jsx` 文件
- **布局**: 不修改任何间距/布局相关样式
- **Landing 页面**: 强制 dark 模式的逻辑不变
- **特殊效果 CSS**: `.ambilight`、`.golden-text` 等保持不变

## 风险评估

- **风险等级：极低** — 仅修改 CSS 变量值，不涉及任何 JavaScript 逻辑或组件结构
- **回滚方案：一行命令** — `git checkout -- frontend/src/styles/globals.css` 即可完全恢复
- **兼容性：无影响** — CSS 变量和 oklch 色彩空间已被所有现代浏览器支持
