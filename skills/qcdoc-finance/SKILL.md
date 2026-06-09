---
name: qcdoc-finance
description: qcdoc 智能财务助手。当用户需要查询账单、余额、账户流水、合同费用、开票信息、计费规则、财务统计等财务相关数据时使用。触发词：查账单、账单详情、余额、账户流水、充值记录、扣费记录、调账记录、开票信息、发票状态、合同费用、存储费、调阅费、计费规则、财务统计、月度结算、费用汇总。不处理资金操作（充值/扣费/调账/新增账单），仅提供查询和统计分析能力。
---

# qcdoc 智能财务助手

让财务人员通过自然语言查询 qcdoc 系统的财务数据，无需手动操作后台页面。

## 先决条件：认证与 Token 安全

### Base URL 配置

默认使用本地开发环境地址，可根据实际环境覆盖。

**各平台设置方式**：

```bash
# Linux / macOS / Windows Git Bash
export QCDOC_BASE_URL="http://192.168.1.61:8079"

# Windows CMD
set QCDOC_BASE_URL=http://192.168.1.61:8079

# Windows PowerShell
$env:QCDOC_BASE_URL="http://192.168.1.61:8079"
```

> 如未设置环境变量，Skill 默认使用 `http://localhost:8080`。若服务部署在其他地址，请先按上方对应方式设置。

### 首次登录工作流

**Step 1 — 检查本地 Token**

```bash
_TOKEN_FILE="$HOME/.qcdoc/token"
if [ -f "$_TOKEN_FILE" ]; then
  _TOKEN=$(cat "$_TOKEN_FILE")
  _CHECK=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Qcdoc $_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{}' "${QCDOC_BASE_URL:-http://localhost:8080}/api/system/contract/list")
  [ "$_CHECK" = "200" ] && echo "TOKEN_OK" || echo "TOKEN_EXPIRED"
else
  echo "NO_TOKEN"
fi
```

**Step 2 — Token 失效/不存在时交互登录**

提示用户输入账号密码，调用员工登录接口：

```bash
curl -s -X POST "${QCDOC_BASE_URL:-http://localhost:8080}/api/auth/workerLogin" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}"
```

返回格式：
```json
{
  "code": 200,
  "msg": "登录成功",
  "data": { "token": "eyJhbGciOiJIUzI1NiIs..." }
}
```

**Step 3 — 安全存储 Token**

```bash
mkdir -p "$HOME/.qcdoc"
chmod 700 "$HOME/.qcdoc"
echo "$TOKEN" > "$_TOKEN_FILE"
chmod 600 "$_TOKEN_FILE"
```

> **安全原则**：
> - Token 文件权限严格限制为 600（仅所有者可读写）
> - 密码不在任何地方持久化存储
> - Token 过期时自动重新登录（提示用户输入密码）
> - 用户主动退出：`rm "$HOME/.qcdoc/token"`

### 所有 API 调用的通用 Header

```bash
_BASE="${QCDOC_BASE_URL:-http://localhost:8080}"
_TOKEN=$(cat "$HOME/.qcdoc/token" 2>/dev/null || echo "")
AUTH_HEADER="Authorization: Qcdoc $_TOKEN"
CONTENT_JSON="Content-Type: application/json"
```

> 每个 curl 都必须带 `-H "$AUTH_HEADER"`，否则返回 401。

### Token 刷新与自动重试

封装一个统一 API 调用函数，自动检测 401 并返回标识供上层处理：

```bash
qcdoc_api_call() {
  local method="$1"      # GET / POST / PUT / DELETE
  local endpoint="$2"    # 如 /api/business/bill/list
  local payload="$3"     # JSON 字符串，GET 可传 ''

  local url="${_BASE}${endpoint}"
  local resp

  if [ "$method" = "GET" ]; then
    resp=$(curl -s -X GET "$url" -H "$AUTH_HEADER" -H "$CONTENT_JSON")
  else
    resp=$(curl -s -X "$method" "$url" -H "$AUTH_HEADER" -H "$CONTENT_JSON" -d "$payload")
  fi

  # 检查是否返回 401
  if echo "$resp" | grep -q '"code":401'; then
    echo "TOKEN_EXPIRED"
    return 1
  fi

  echo "$resp"
  return 0
}
```

**在 workflow 中使用方式**：

```bash
resp=$(qcdoc_api_call "POST" "/api/business/bill/list" '{"pageNum":1,"pageSize":500}')
if [ "$resp" = "TOKEN_EXPIRED" ]; then
  # 触发自动刷新逻辑（见下方 401 处理）
  ...
fi
```

> 该封装保持与现有 Header 变量一致，所有请求统一走 `_BASE`、`$AUTH_HEADER`、`$CONTENT_JSON`。

### Windows 环境注意事项

- **路径分隔符**：Windows 原生路径使用反斜杠 `\`，但在 Bash（Git Bash）中仍使用正斜杠 `/`，所有脚本中的路径统一按 Bash 语法书写。
- **编码问题**：Windows 终端中 curl 返回的 JSON 若包含中文可能出现乱码，建议将响应保存到文件后用 Node.js 处理：`curl ... > /tmp/resp.json && node -e "..."`
- **Token 文件位置**：Git Bash 中 `$HOME` 映射为 `C:\Users\<用户名>`，Token 实际存储在 `C:\Users\<用户名>\.qcdoc\token`。
- **HTTP 服务器**：预览 HTML 报告时，使用 `python -m http.server` 启动本地服务器，避免直接用 `file://` 协议（Playwright 会拦截）。

## 什么时候用

### 路由决策表

| 用户在说 | 走的接口 | 关键参数 |
|---|---|---|
| "查账单"、"账单列表"、"XX公司的账单" | `POST /api/business/bill/list` | companyId / contractId / startTime / endTime |
| "账单详情"、"看下账单XX" | `GET /api/business/bill/detail?code=` 或 `GET /api/business/bill/{id}` | code 或 id |
| "余额多少"、"账户余额" | `GET /api/business/account/balance` | companyId / departmentId / userId |
| "账户流水"、"充值记录"、"扣费记录"、"调账记录" | `POST /api/business/account/list` | type: RECHARGE/DEDUCTION/ADJUSTMENT |
| "合同费用"、"某合同的本月费用"、"存储费/调阅费" | `POST /api/system/contract/billList` | contractId / billMonth(yyyy-MM) |
| "开票信息"、"发票状态"、"哪些还没开票" | `POST /api/system/contract/invoice/list` | invoiceStatusFilter: 0=需开票, 1=已开票 |
| "计费规则"、"怎么收费的" | `POST /api/system/billrule/list` | contractId |
| "财务统计"、"本月费用汇总" | `GET /api/business/account/statistics` | companyId / startTime / endTime |
| "合同列表"、"有哪些合同" | `POST /api/system/contract/list` | companyId |
| "提醒"、"开票提醒" | `POST /api/system/invoice/remind/listUserReminds` | userId |

### 通用启发

- 用户问**具体财务数据**（金额、数量、状态），**永远走 API**，不要凭记忆回答
- 用户说"最近"时，推断时间范围（本月、上周、最近30天），转为具体日期参数
- 涉及金额统一格式化为人民币元，保留两位小数
- 时间戳（createdOn/updatedOn/startOn 等）转换为北京时间展示
- 状态枚举（BillStatusEnum/AccountTypeEnum/InvoiceStatusEnum）展示中文 info，不暴露内部 code

### 分页与大数据量策略

| 场景 | pageSize | 处理方式 |
|---|---|---|
| 账单查询（数据量大，需按月汇总） | 500 | 单页拉取，客户端过滤汇总 |
| 账户流水（充值/扣费/调账记录） | 100 | 单页拉取，按类型筛选 |
| 合同费用查询（核心财务视图） | 50 | 单页拉取，按月展示 |
| 开票信息查询 | 50 | 单页拉取，按状态筛选 |
| 合同列表（前置定位） | 100 | 单页拉取，缓存 companyId |
| 计费规则查询 | 50 | 单页拉取 |
| 用户提醒列表 | 50 | 单页拉取 |

**时间范围推断**：

- "最近" → 最近 30 天（`startTime` = 今天 - 30 天，`endTime` = 今天）
- "最近一个月" / "上月" → 上个月整月（如 2026-05-01 至 2026-05-31）
- "本月" / "这个月" → 当前月份整月（如 2026-06-01 至 2026-06-30）
- "今年" / "本年度" → 当前年份 1 月 1 日至今天（如 2026-01-01 至 2026-06-02）
- "上季度" → 上一个完整季度（如 2026-01-01 至 2026-03-31）
- "本季度" → 当前季度初至今天（如 2026-04-01 至 2026-06-02）
- "上半年" → 1 月 1 日至 6 月 30 日
- "下半年" → 7 月 1 日至 12 月 31 日

> 所有时间范围最终转换为 `yyyy-MM-dd` 格式传入 API，时间戳字段按北京时间展示。

## 端点速览

| 端点 | 服务 | 用途 |
|---|---|---|
| `POST /api/business/bill/list` | business | 账单分页查询 |
| `GET /api/business/bill/detail?code=` | business | 账单详情（按编号） |
| `GET /api/business/bill/{id}` | business | 账单详情（按ID） |
| `GET /api/business/bill/records` | business | 充值/扣费记录 |
| `GET /api/business/bill/balance/{userId}` | business | 指定用户余额 |
| `POST /api/business/account/list` | business | 账户流水查询 |
| `GET /api/business/account/balance` | business | 账户余额查询 |
| `GET /api/business/account/statistics` | business | 账户统计信息 |
| `POST /api/system/contract/billList` | system | 合同账单分页（核心财务视图） |
| `POST /api/system/contract/list` | system | 合同列表 |
| `POST /api/system/contract/detail` | system | 合同详情 |
| `POST /api/system/contract/invoice/list` | system | 开票信息列表 |
| `POST /api/system/billrule/list` | system | 计费规则列表 |
| `POST /api/system/invoice/remind/listUserReminds` | system | 用户提醒列表 |

> 所有接口通过网关统一访问，Base URL = `${QCDOC_BASE_URL:-http://localhost:8080}`

## 前置流程：公司名称 → companyId 定位

用户输入公司名称（如"某某公司"）时，需先将其映射为 `companyId`。以下步骤在每次涉及公司维度的查询前执行。

### Step 1 — 查询合同列表获取候选公司

不带 `companyId` 调用合同列表接口，获取当前账号可见的全部合同：

```bash
curl -s -X POST "$_BASE/api/system/contract/list" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{"pageNum":1,"pageSize":100}'
```

返回 `data.rows[]` 中每个元素包含：
- `company.id` — 公司ID（后续查询所需的 `companyId`）
- `company.name` — 公司名称
- `id` — 合同ID（`contractId`）

用 `jq` 提取候选：
```bash
curl -s -X POST "$_BASE/api/system/contract/list" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{"pageNum":1,"pageSize":100}' | \
  jq -r '.data.rows[] | select(.company.name | contains("'$COMPANY_NAME'")) | "\(.company.id)\t\(.company.name)\t\(.id)"'
```

### Step 2 — 确认匹配结果

| 场景 | 处理 |
|---|---|
| **唯一匹配** | 直接使用该公司 `companyId`，同时记录其 `contractId` 供后续查询复用 |
| **多条匹配** | 列出候选公司供用户选择：`1) 某某科技有限公司 (ID: 123)  2) 某某贸易有限公司 (ID: 456)` |
| **无匹配** | 提示用户：`"未找到该公司，请检查名称或联系管理员"` |

### Step 3 — 缓存 companyId（可选）

将 `companyName → companyId` 映射缓存到本地文件，避免重复查询：

```bash
_CACHE_FILE="$HOME/.qcdoc/company_cache"
[ -f "$_CACHE_FILE" ] || echo '{}' > "$_CACHE_FILE"
# 写入缓存（示例）
jq --arg name "$COMPANY_NAME" --arg id "$COMPANY_ID" \
  '. + {($name): ($id | tonumber)}' "$_CACHE_FILE" > "$_CACHE_FILE.tmp"
mv "$_CACHE_FILE.tmp" "$_CACHE_FILE"
```

> 命中缓存时直接读取，跳过 Step 1。缓存文件权限建议设为 `600`。

## 工作流

### 账单查询

**触发**："查账单"、"XX公司的账单"

> **⚠️ 注意**：该接口在某些环境下可能返回所有可见账单（忽略 `companyId` / `contractId` 过滤），因此建议在获取结果后做客户端过滤。

```bash
# 1. 拉取数据（控制 pageSize ≤ 500，避免超时）
curl -s -X POST "$_BASE/api/business/bill/list" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{
    "companyId": <公司ID>, "contractId": <合同ID>,
    "startTime": "2026-01-01", "endTime": "2026-01-31",
    "pageNum": 1, "pageSize": 500
  }' > /tmp/bill_all.json

# 2. 客户端过滤并按月汇总（Node.js，兼容 Windows）
node -e "
const fs = require('fs');
const data = JSON.parse(fs.readFileSync('/tmp/bill_all.json', 'utf8'));
const rows = (data.data && data.data.rows) || [];
const target = 'XX公司';          // 替换为实际公司名
const filtered = rows.filter(r => r.companyName === target);

// 按 code（账单月份）分组汇总
const summary = filtered.reduce((acc, r) => {
  const m = r.code || 'unknown';
  if (!acc[m]) acc[m] = { boxCount: 0, deductAmount: 0, borrowPrice: 0, otherPrice: 0, rechargeAmount: 0, balance: 0 };
  acc[m].boxCount      += r.boxCount      || 0;
  acc[m].deductAmount  += r.deductAmount  || 0;
  acc[m].borrowPrice   += r.borrowPrice   || 0;
  acc[m].otherPrice    += r.otherPrice    || 0;
  acc[m].rechargeAmount+= r.rechargeAmount|| 0;
  acc[m].balance        = r.balance        || 0; // 取最后一笔余额
  return acc;
}, {});

console.log('=== 过滤后记录数:', filtered.length, '===');
Object.entries(summary).sort().forEach(([m, s]) => {
  console.log(\`\${m} | 箱:\${s.boxCount} | 存储费:\${s.deductAmount} | 调阅费:\${s.borrowPrice} | 其他:\${s.otherPrice} | 充值:\${s.rechargeAmount} | 余额:\${s.balance}\`);
});
"
```

返回 `data.rows[]` 中关键字段（BillBean）：
- `code` — 账单编号（yyyy-MM）
- `boxCount` — 箱数量
- `boxPrice` — 箱单价
- `addedBoxCount` — 新增档案数
- `borrowEntityCount` — 原件调阅数量
- `borrowDigitalCount` — 电子化调阅数量
- `borrowPrice` — 调阅金额
- `deductAmount` — 扣除金额（存储费）
- `otherPrice` — 其他金额
- `rechargeAmount` — 充值金额
- `lastBalance` — 上月余额
- `balance` — 当前余额
- `status` — 账单状态（BillStatusEnum）
- `invoiceStatus` — 开票状态（0-需开票, 1-已开票）
- `createdOn` / `startOn` / `endOn` — 时间戳

### 余额查询

**触发**："余额"、"还剩多少钱"

```bash
curl -s -X GET "$_BASE/api/business/account/balance?companyId=&departmentId=&userId=" \
  -H "$AUTH_HEADER"
```

返回 `data` 为 Map，key 是维度标识，value 是余额数值。

### 账户流水

**触发**："账户流水"、"充值记录"、"扣费记录"、"调账记录"

```bash
curl -s -X POST "$_BASE/api/business/account/list" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{"companyId":<ID>,"type":"RECHARGE","pageNum":1,"pageSize":100}'
```

`type` 可选：`RECHARGE`（充值）、`DEDUCTION`（扣费）、`ADJUSTMENT`（调账），不传则查全部。

返回 `data.rows[]` 中关键字段（AccountBean）：
- `type` — 操作类型（AccountTypeEnum: 充值/扣费/调账）
- `money` — 操作金额
- `balance` — 操作后余额
- `usageType` — 业务类型
- `serialNo` — 业务流水号
- `payerInfo` — 付款人信息
- `billMonth` — 账单月份（yyyy-MM）
- `feeStatus` — 计费状态（0-待计费, 1-已计费, 2-已入账）
- `createdOn` — 创建时间

### 合同费用查询（核心财务视图）

**触发**："合同费用"、"本月存储费/调阅费"

```bash
curl -s -X POST "$_BASE/api/system/contract/billList" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{"companyId":<ID>,"contractId":<ID>,"billMonth":"2026-01","pageNum":1,"pageSize":50}'
```

返回 `data.rows[]` 中关键字段（ContractBillPageVo）：
- `contractCode` / `contractName` — 合同编码/名称
- `companyName` / `departmentName` — 公司/部门名称
- `userName` / `userMobile` — 用户姓名/手机号
- `storageCount` — 档案数量
- `boxPrice` — 档案单价
- `monthStorageFee` — 当月存储费
- `monthRetrievalFee` — 当月调阅费
- `monthOtherFee` — 当月其他费用
- `monthTotalDeduction` — 当月扣费合计
- `monthRechargeAmount` — 当月充值金额
- `lastMonthBalance` — 上月余额
- `balance` — 当前余额
- `billMonth` — 账单月份
- `billStatus` — 账单状态

### 开票信息查询

**触发**："开票信息"、"哪些还没开票"

```bash
curl -s -X POST "$_BASE/api/system/contract/invoice/list" \
  -H "$AUTH_HEADER" -H "$CONTENT_JSON" \
  -d '{"contractId":<ID>,"invoiceStatusFilter":0,"pageNum":1,"pageSize":50}'
```

`invoiceStatusFilter`: 0=需开票, 1=已开票, null=所有。

返回 `data.rows[]` 中关键字段（ContractInvoiceInfoBean）：
- `invoiceStatusInfo` — 开票状态（需开票/已开票）
- `paymentReceivedStatusInfo` — 到账状态（未到账/已到账）
- `invoiceCompanyName` — 开票公司名称
- `paymentCompanyName` — 付款公司名称
- `expectedInvoiceDate` — 应开票日期（时间戳）
- `actualInvoiceDate` — 实际开票日期（时间戳）
- `dueDate` — 到账日期（时间戳）
- `invoicePeriod` — 开票周期（如 2025-01-2025-03）
- `remark` — 备注

### 财务统计

**触发**："统计"、"本月费用汇总"

```bash
curl -s -X GET "$_BASE/api/business/account/statistics?companyId=&startTime=&endTime=" \
  -H "$AUTH_HEADER"
```

## 返回数据形态

统一返回 `AjaxResult`：

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": { ... }
}
```

分页数据：
```json
{
  "total": 100,
  "rows": [ ... ]
}
```

## 给用户的输出格式

> **核心原则**：输出必须是财务人员能直接看懂的中文报告。禁止暴露端点路径、raw 参数名、HTTP 状态码。

### 账单列表

```markdown
**账单查询结果**（2026年1月）

| 账单编号 | 公司 | 箱数 | 存储费 | 调阅费 | 其他费 | 充值 | 余额 | 状态 |
|---------|------|------|--------|--------|--------|------|------|------|
| 2026-01 | 某某公司 | 150 | ¥1,500.00 | ¥200.00 | ¥50.00 | ¥5,000.00 | ¥3,250.00 | 已核对 |

共 100 条，当前第 1 页
```

> 字段选择逻辑：code → companyName → boxCount → deductAmount → borrowPrice → otherPrice → rechargeAmount → balance → status.info

### 合同费用概览（核心财务视图）

```markdown
**合同费用概览 — 某某公司（2026年1月）**

| 项目 | 金额 |
|------|------|
| 档案数量 | 150 箱 |
| 档案单价 | ¥10.00/箱 |
| 当月存储费 | ¥1,500.00 |
| 当月调阅费 | ¥200.00 |
| 当月其他费用 | ¥50.00 |
| **当月合计扣费** | **¥1,750.00** |
| 当月充值金额 | ¥5,000.00 |
| 上月余额 | ¥0.00 |
| **当前余额** | **¥3,250.00** |
```

> 字段映射：monthStorageFee → monthRetrievalFee → monthOtherFee → monthTotalDeduction → monthRechargeAmount → lastMonthBalance → balance

### 账户流水

```markdown
**账户流水 — 某某公司（2026年1月）**

| 类型 | 金额 | 余额 | 流水号 | 付款人 | 账单月份 | 时间 |
|------|------|------|--------|--------|---------|------|
| 充值 | ¥5,000.00 | ¥5,000.00 | SN20260101001 | 张三 | 2026-01 | 2026-01-01 10:30 |
| 扣费 | ¥1,750.00 | ¥3,250.00 | SN20260102001 | 系统扣费 | 2026-01 | 2026-01-02 03:00 |

共 2 条
```

> 字段映射：type.info → money → balance → serialNo → payerInfo → billMonth → createdOn

### 开票信息

```markdown
**开票信息 — 某某合同**

| 开票公司 | 付款公司 | 开票状态 | 到账状态 | 应开票日期 | 实际开票日期 | 到账日期 | 开票周期 |
|---------|---------|---------|---------|-----------|------------|---------|---------|
| XX科技有限公司 | 某某公司 | 需开票 | 未到账 | 2026-01-15 | - | - | 2026-01-2026-03 |

共 3 条需开票
```

> 字段映射：invoiceCompanyName → paymentCompanyName → invoiceStatusInfo → paymentReceivedStatusInfo → expectedInvoiceDate → actualInvoiceDate → dueDate → invoicePeriod

### 格式约定

- **金额**：`¥12,345.67`（千分位 + 两位小数）
- **时间戳**（createdOn/updatedOn/startOn 等）：转换为"2026年1月15日 10:30"格式
- **状态枚举**：展示中文 info（"已核对"、"充值"、"需开票"），不暴露内部 code（0/1/2）
- **缺失日期**：用 "-" 占位（如未开票时 actualInvoiceDate 为空）

## 报告导出（可选）

当用户说"生成报告"、"导出 PDF"、"用 kami 输出"时，使用 kami 的 `one-pager.html` 模板生成财务报告。

### 报告内容结构

报告共 7 个部分：

1. **页头**：公司名称 + 合同编号 + 联系人 + 生成时间
2. **核心指标（4 张卡片）**：累计存储费、累计箱数、当前在库箱数、累计充值金额
3. **导语**：一句话历史摘要（如"某某公司自 2024 年 3 月合作至今，累计充值 ¥50,000.00，当前余额 ¥12,345.67"）
4. **双栏布局**：左侧费用明细表（存储费/调阅费/其他费/扣费合计），右侧计费规则摘要
5. **历史趋势表**：按时间段（月/季度）分组，展示每期箱数、存储费、调阅费、余额变化
6. **提示框**：异常数据或优化建议（如"2026-03 余额低于月均扣费，建议提醒充值"）
7. **页脚**：数据来源说明 + 报告生成时间

### 数据准备清单

填充模板前，确保已获取以下全部字段：

- `companyName` — 公司名称
- `contractCode` / `contractName` — 合同编号/名称
- `userName` / `userMobile` — 联系人及电话
- `storageCount`（累计）/ 当前在库箱数 — 需从多期账单累加或取最新值
- `monthStorageFee` / `monthRetrievalFee` / `monthOtherFee` / `monthTotalDeduction` — 各期费用（用于趋势表和汇总）
- `monthRechargeAmount` — 各期充值金额
- `balance` — 各期余额（用于趋势表）
- `billMonth` — 账单月份（用于趋势表分组）
- `boxPrice` / 计费规则详情 — 从 `POST /api/system/billrule/list` 获取
- `createdOn`（最新账单时间）— 用于生成时间

### 模板填充指南

- **页头**：直接填入 `companyName`、`contractCode`、`userName`、`userMobile`、当前北京时间
- **指标卡片**：累计存储费 = 各期 `monthStorageFee` 求和；累计箱数 = 各期 `storageCount` 求和（或去重）；当前箱数 = 最新一期 `storageCount`；累计充值 = 各期 `monthRechargeAmount` 求和
- **导语**：用模板字符串拼接，金额格式化后插入
- **费用明细表**：横向表头为费用类型，纵向为各期 `billMonth`，单元格为对应金额
- **计费规则**：从 `billrule/list` 提取 `ruleName`、`price`、`unit`，以列表形式展示
- **趋势表**：按 `billMonth` 升序排列，每行一期，列同费用明细表 + 余额
- **提示框**：基于余额趋势判断（连续下降、低于阈值等），给出一句建议
- **页脚**：固定文案"数据来源于 qcdoc 财务系统" + 当前时间

### 输出方式

| 方式 | 工具 | 说明 |
|------|------|------|
| HTML 预览 | kami 模板渲染 | 直接输出 HTML 字符串，供用户浏览器查看 |
| PDF 导出 | weasyprint | `weasyprint report.html report.pdf`，需本地安装 weasyprint |
| 截图 | Playwright | 用 headless 浏览器打开 HTML 后截图，适合嵌入聊天或邮件 |

> kami 模板位置参考 kami Skill 文档，此处不重复。模板变量使用双大括号 `{{variable}}` 语法。

## 安全边界

> **本 Skill 仅提供查询能力，禁止调用任何修改/操作类接口。**

以下接口**绝对不允许**调用：
- ❌ `POST /api/business/account`、 `POST /api/business/account/fundOperation` — 资金操作
- ❌ `POST /api/business/bill`、`POST /api/business/bill/edit`、`POST /api/business/bill/update` — 账单增改
- ❌ `POST /api/business/bill/settlement/*` — 结算任务触发
- ❌ `POST /api/system/contract/add`、`POST /api/system/contract/update` — 合同增改
- ❌ `POST /api/system/contract/invoice/add*` — 新增开票
- ❌ `POST /api/system/billrule/add` — 新增计费规则
- ❌ `POST /api/business/bill/rebuild/*` — 补录任务
- ❌ `POST /api/business/billImport/*` — 批量导入

如果用户请求涉及资金操作，明确拒绝：
> "本助手仅支持财务数据查询，资金操作请通过正式业务流程处理。"

## 常见错误处理

| 返回 | 含义 | 处理 |
|---|---|---|
| `code:401` | Token 失效或未登录 | 1. 检查本地 token 文件 `~/.qcdoc/token` 是否存在<br>2. 若存在，提示用户："Token 已过期，请重新输入密码"<br>3. 调用 `workerLogin` 获取新 token，写入 `~/.qcdoc/token`<br>4. 使用新 token 自动重试原请求<br>5. 若仍返回 401，提示用户联系管理员 |
| `code:403` | 无权限 | 提示用户："当前账号暂无查询权限，请联系管理员开通财务模块权限。" |
| `code:500` | 服务端错误 | 提示用户稍后再试 |
| `data.total:0` | 空数据 | 友好提示"未查询到相关数据，请检查查询条件" |
| 网络超时 | 服务不可达 | 检查 `QCDOC_BASE_URL` 是否配置正确 |
