# Kami

Document-generation skill and template system. Editorial HTML templates + PDF/PPTX/PNG build pipeline.

## 启动前

- 个人/全局规则可放在仓库外；本文件只记录 Kami 项目内的 Claude Code 入口、维护规则和完整细节。
- `AGENTS.md` 只放 agent 操作必需的短规则；仓库全貌、陷阱清单、字体链、发布流程等细节在这里。
- 模板设计规范看 `references/design.md`，写作规范看 `references/writing.md`，反模式 checklist 看 `references/anti-patterns.md`。

## 仓库地图

- `SKILL.md` — skill 路由与操作规则。
- `CHEATSHEET.md` — 快速设计参考。
- `references/` — 设计、写作、图表、生产规范：`design.md`、`writing.md`、`production.md`、`diagrams.md`、`resume-writing.md`、`anti-patterns.md`；`tokens.json`（规范色 token，`scripts/tokens.py` 校验漂移）；`checks_thresholds.json`（节奏/密度/孤行阈值，`scripts/checks.py` 读取）。
- `assets/templates/` — 文档模板（含仅浏览器用的落地页变体）与 `marp/` Markdown 幻灯片主题。
- `assets/demos/`、`assets/showcase/` — README 演示图与公开站点截图（后者不进包）。
- `assets/fonts/`、`assets/illustrations/`、`assets/diagrams/` — 字体、插画、图表源文件。
- `scripts/build.py` — 构建入口；`scripts/shared.py` — 常量与 `HTML_TEMPLATES` 注册表；`scripts/checks.py`、`scripts/tokens.py`、`scripts/highlight.py` — 校验与高亮；`scripts/ensure-fonts.sh` — 字体恢复；`scripts/package-skill.sh` — 打包；`scripts/draft-release-notes.py` — 发布说明草稿；`scripts/tests/test_build.py` — 零依赖测试。
- `index*.html`、`llms.txt`、`robots.txt`、`sitemap.xml`、`vercel.json` — 公开站点、爬虫与 AI 可见性入口。
- `.github/workflows/{check,release}.yml` — PR 校验与 tag 发布。
- `dist/kami.zip` — tracked 的发布压缩包。

Reference docs 只用英文；CN/EN/KO 的输出差异放在模板里，不要复制多份 reference。

## 常用命令

```bash
python3 scripts/build.py                   # 构建所有目标
python3 scripts/build.py --check           # 快速校验（CSS lint + token 同步 + 跨模板 :root 一致）
python3 scripts/build.py --verify          # 完整验证
python3 scripts/build.py --check-placeholders path/to/filled.html
python3 scripts/build.py --check-orphans path/to/doc.pdf
python3 scripts/build.py --check-density path/to/doc.pdf
python3 scripts/build.py --check-rhythm slides slides-en
python3 scripts/tests/test_build.py        # 测试套件
python3 scripts/draft-release-notes.py V1.4.0..HEAD --version V1.4.1 --title "Steadier Hand"
bash scripts/ensure-fonts.sh               # 字体恢复（缺字体或字体被截断时）
bash scripts/package-skill.sh              # 构建 release 压缩包
```

## 项目独有硬规则

- 改 style 时同步更新 `references/design.md` 和模板 tokens，不要只改单点。
- 加新模板：从最近的模板复制，对齐 `references/design.md`，加 demo 覆盖。
- 不要在 docs / template 注释 / 脚本输出里用图形 emoji。脚本状态用 `OK:` / `ERROR:`。
- 模板**内联** CSS，不抽公共 partial。修 CSS 漂移时跨模板同步改，不要引入 build-time include。
- `HTML_TEMPLATES` 注册表在 `scripts/shared.py`，加删模板改这一处，不要分别改 build.py。
- 不打包大体积商业字体到 `dist/kami.zip`，但模板要保留稳定的本机预览路径。
- `dist/kami.zip` 是 tracked release 制品。小修通常刷 latest release 资源即可，不必新 tag。
- 改 build / packaging 相关代码后，刷新并检查 `dist/kami.zip`；新增 helper/module/reference JSON 后，确认文件已被 Git 跟踪并进入 package。
- 不提交一次性的 review 报告或诊断快照；只把稳定规则沉淀到 `AGENTS.md`、`CLAUDE.md`、`SKILL.md` 或 `references/`。

## 工作规则（细节）

- 文档或模板任务先锁定输出契约：语言、模板、输出格式、页数或长度目标、视觉验收方式、验证命令。
- 优先用最近的现有模板和确定性校验器。除非当前请求确实做不到，否则不要新增模板、共享 CSS 层、依赖、脚本参数或可选模式。
- 落地页或文档站点改动按 `references/design.md` 第 11 节：文档外壳（侧边栏轨道、本页目录、无边框上一页/下一页、构建期零 JS 代码高亮）和「响应式截图验证」（每语言在 375px / 1280px 各截图一次 + 客观孤行扫描）后再交付。
- 内容改动避免无关 CSS churn，除非布局行为属于本次任务。
- 幻灯片默认用 WeasyPrint HTML→PDF 模板，除非用户明确要可编辑 PPTX。
- 品牌 profile 是可选上下文；公开示例放 `references/`，不要硬编码维护者的私有本地 profile。
- 多语言公开页、`llms.txt`、`robots.txt`、sitemap、JSON-LD、FAQ 在改定位或安装说明时保持一致。

## 重构与打包硬性约束

- 把 `scripts/build.py` 或打包 helper 重构成新模块时，确认新 helper 文件已被 Git 跟踪——`package-skill.sh` 基于 `git ls-files` 打包，未跟踪模块本地能 import，但会从 ZIP 里消失。
- 任何新增 scripts、模板、reference JSON、workflow 或打包输入的改动，都必须刷新并检查 `dist/kami.zip`；包新鲜度是发布就绪的一部分。
- 改动 `SKILL.md`、模板、脚本、references 或打包输入时，要显式决定是否需要刷新 `dist/kami.zip`。
- `--verify` 若只因宿主缺 `python-pptx` 等 PPTX 回退依赖而失败，在临时 venv 里单独验证 `slides` / `slides-en`，不要当成源码回归。
- 改 `.github/workflows/*.yml` 先在功能分支推上去看 CI 变绿再合并。本项目已在 CI 与本地差异上踩过坑（pip 缓存需要 manifest、`fallback_present` 缺 Ubuntu 默认字体、CI 没有商业字体）。

## 当前风险区

- WeasyPrint 渲染对字体可用性、实色十六进制标签背景、分页、CJK 回退、合成粗体敏感；模板改动要目视验证。
- 幻灯片有三条路径：默认 PDF 的 `slides-weasy*.html`、可编辑 PPTX 的 `slides*.py`、Markdown 优先的 `marp/slides-marp*.{md,css}`。
- Marp 主题 CSS 内联了完整 design token 副本（Marp 主题必须自包含）；`--sync` / `--check` 会同步 token，但 CSS lint 与离色检查仍只扫 `.html` 模板，Marp 主题的非 token CSS 需人工 review。
- AI/公开可见性横跨 `index*.html`、`llms.txt`、`robots.txt`、`sitemap.xml`、FAQ JSON-LD、README 安装文案、图表计数和 release 链接。
- `scripts/shared.py` 集中了构建脚本常量；加模板或图表前先同步路径和目标名。

## 高危陷阱

详见 `references/production.md` Part 4。

1. Tag rgba 双矩形：用实色 hex 背景。
2. 细边框 + border-radius 双环：border < 1pt 配圆角可能触发。
3. 简历 2 页溢出：小字号、回退字体、行高或 margin 变化都可能突破。
4. flex 内 `break-inside` 失效：内容包一层 block wrapper。
5. `@page` 下 `height: 100vh` 不可靠：用显式 mm。
6. 不支持的 SVG marker `orient="auto"`：手绘箭头。
7. 正文段落不要用 `max-width`：`.manifesto`、`.section-lede` 等应铺满 `.page`；例外是 `.type-sample` 和 `.footer .colophon`。
8. 图表模板改动要同步到 `index*.html` 里的迷你 SVG。

## 演示截图

所有演示 PNG 用 **1241x1754px**（150dpi 下的 A4 首页）。单页/多页文档截第 1 页：

```bash
pdftoppm -r 150 -f 1 -l 1 -png <pdf> /tmp/p && cp /tmp/p-1.png <target>.png
```

横版幻灯片截前 2 页，各缩放到高 867px，加 20px 间隔，再扩展到 1241px 宽：

```bash
pdftoppm -r 150 -f 1 -l 2 -png <pdf> /tmp/sl
magick /tmp/sl-1.png -resize x867 /tmp/sl1.png
magick /tmp/sl-2.png -resize x867 /tmp/sl2.png
magick -size $(identify -format '%w' /tmp/sl1.png)x20 xc:'#f5f4ed' /tmp/gap.png
magick /tmp/sl1.png /tmp/gap.png /tmp/sl2.png -append /tmp/stacked.png
magick /tmp/stacked.png -gravity Center -background '#f5f4ed' -extent 1241x1754 <target>.png
```

## 验证细节

- 期望页数：one-pager 1，letter 1，resume 严格 2，long-doc 7±2，portfolio 6±2，slides 7±3，equity-report 2–3，changelog 1–2。落地页是纯浏览器 HTML，无 PDF 页数。
- `build.py` 仅在模板仍有 author placeholder 时从 `git config user.name` 或 `KAMI_AUTHOR` 写 `/Author`；`/Producer` 与 `/Creator` 保持 `Kami`。
- `assets/demos/` 是 1241x1754px 首页预览；幻灯片 demo 取前两页横版堆叠后扩展到 1241x1754px。
- 图表数量与名称必须在 `SKILL.md`、`CHEATSHEET.md`、`README.md`、`index*.html`、`assets/diagrams/` 间一致。
- 需要 `weasyprint` / `pypdf` / `PyMuPDF` 的测试必须在装好依赖的 CI job（`verify-render`）跑；`lint-and-test` 只有 Pygments，那里的 skip-guard 会静默跳过并照样打印 `OK:`。

## 验证清单

- 模板/CSS/脚本：`build.py --check` + `build.py --verify`。
- Demo：重新生成受影响输出，确认页数在范围内。
- 字体：`ensure-fonts.sh` 后重建受影响目标。
- 幻灯片节奏或 deck 改动：`--check-rhythm slides slides-en` + 渲染。
- 公开站点或 AI 可见性：一起检查 `index*.html`、`llms.txt`、`robots.txt`、`sitemap.xml`、README 链接。
- 打包：`package-skill.sh` 后确认 ZIP 足够小。
- 仅文档：检查链接与引用。

## 发布说明与发布流程

- 公开版本的说明保持简洁，需要时中英双语；中英条目一一对应，5–8 条，每条一句。
- `package-skill.sh` 写出 tracked 的 `dist/kami.zip`，排除大体积 TsangerJinKai / Source Han Serif K 字体和仅 README/公开站点用的截图。
- `dist/kami.zip` 随发布改动提交，并上传为 GitHub latest release 资源。
- README 与公开站点下载链接统一用 `https://github.com/tw93/kami/releases/latest/download/kami.zip`；小的打包或文档修复优先刷新该资源，不开新 tag。
- 只有维护者明确要版本化发布时才打 tag；tag 打在已包含最终 `dist/kami.zip` 的提交上。

## 字体

- 中文：TsangerJinKai02 W04/W05（商用需相应授权）。回退链：Source Han Serif SC → Noto Serif CJK SC → Songti SC → STSong → Georgia。
- 英文：Charter。日文：YuMincho → Hiragino Mincho ProN → Noto Serif CJK JP → Source Han Serif JP → TsangerJinKai02 → serif。
- 韩文：Source Han Serif K（`ensure-fonts.sh` 下载的 OTF 内部族名是 `Source Han Serif KR`，回退链必须保留它）。回退链：Source Han Serif K → Source Han Serif KR → Noto Serif KR → Apple SD Gothic Neo → AppleMyungjo → Charter → Georgia。
- Claude Desktop 包不含 TsangerJinKai TTF 和 Source Han Serif K OTF（OFL 字体 git-tracked 供 CDN `@font-face` 回退，但不进包）。构建中文/韩文文档前若缺字体先跑 `ensure-fonts.sh`，它装到 XDG 用户字体目录（fontconfig 可扫、在 skill 之外）。
