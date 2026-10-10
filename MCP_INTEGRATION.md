# MCP 集成说明

本文档说明本项目实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。

## 1. MCP Server 信息

| 项目 | 内容 |
|---|---|
| Server | 麦当劳中国官方 MCP Server（实测开放 35 个 Tool） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| Token 申请 | https://open.mcd.cn/mcp （手机号登录 → 控制台 → 激活） |
| 限流 | 每 Token 每分钟 600 次请求 |
| 错误码 | `401` Token 无效或过期；`429` 触发限流 |
| 服务范围 | 麦当劳中国大陆，不含港澳台 |

配置示例见 `mcp-config.example.json`（只含环境变量占位符 `${MCD_MCP_TOKEN}`）。

## 2. 实际调用的 Tool

### 已接入

| Tool | 用途 | 调用时机 |
|---|---|---|
| `query-meals` | 查询门店当前可售卖的餐品列表，含分类、餐品编码与**价格** | `scripts/fetch_menu.py` 抓取 `data/menu.json` 时 |
| `query-nearby-stores` | 查询附近可用门店，取得 `storeId` | 用户未指定门店时，为 `query-meals` 准备门店上下文 |

`query-meals` 是本项目的**关键依赖**：官网营养计算器只提供营养成分，不提供价格，而「预算 30 块怎么吃」里价格是硬约束。因此价格数据只能来自 MCP，这也是本项目真实使用麦当劳 MCP 能力的核心证据。

### 可衔接（本项目未直接实现，但架构已预留）

| Tool | 衔接方式 |
|---|---|
| `list-nutrition-foods` | 与官网营养快照做交叉校验，或在官网数据过期时作为主数据源 |
| `calculate-price` | 求解器产出方案后，用真实券后价复核总额 |
| `create-order` | 用户确认方案后直接下单，返回支付链接 |
| `query-my-coupons` / `query-store-coupons` | 把可用优惠券纳入约束，求解「用哪张券最划算」 |
| `query-meal-detail` | 展开套餐组成与可替换项，支持「板烧鸡腿堡不要酱」这类定制 |
| `delivery-query-stores` / `delivery-query-addresses` | 外送场景补充配送费与可达门店 |

调用这些工具的客户端已实现在 `scripts/fetch_menu.py` 的 `McpClient` 中（`call_tool(name, arguments)`），扩展只需新增一层解析。

## 3. 调用流程

```
用户提出约束（预算 / 热量 / 钠 / 蛋白质）
        │
        ├─► [离线层] data/nutrition.json
        │      麦官网营养计算器抓取，无需 Token
        │      提供：热量、蛋白质、脂肪、碳水、钠、钙
        │
        └─► [MCP 实时层]
                │
                ├─ POST initialize          （协商协议版本 2025-06-18 / 2025-03-26 / 2024-11-05）
                ├─ POST notifications/initialized
                ├─ POST tools/list          （确认 query-meals 可用）
                ├─ POST tools/call query-nearby-stores   → storeId
                └─ POST tools/call query-meals {storeId} → 菜单与价格
                        │
                        ▼
                data/menu.json
                        │
                        ▼
        双源合并（品名归一化匹配，MCP 价格优先）
                        │
                        ▼
        分层束搜索求解 → 三套方案 → 终端 / JSON 输出
```

### 握手细节

麦当劳 MCP Server 使用的协议版本较旧，直接用最新协议握手会失败。`McpClient.initialize()` 按 `2025-06-18 → 2025-03-26 → 2024-11-05` 依次尝试，成功即停（实测 `2025-06-18` 可用）。

响应体可能是纯 JSON，也可能是 SSE 流（`text/event-stream`），`_parse()` 两种都能解析。会话 ID 从响应头 `Mcp-Session-Id` 提取并在后续请求中回传。

### 踩坑记录（都是实测才发现的）

1. **`query-meals` 三个参数全必填**：`storeCode`、`orderType`、`beType`。少任何一个都报错，`storeCode` 要先从 `query-nearby-stores` 拿。
2. **`searchType` 是必填且默认不对**：`query-nearby-stores` 默认 `searchType=1`（搜收藏餐厅），未收藏就报 `600050 收藏餐厅列表为空`。必须显式传 `searchType=2`（按位置搜索）。
3. **城市与关键词必须同时给**：只传 `city` 会报 `600058 城市名或者关键词不能为空`。给了城市就用「麦当劳」自动补关键词。
4. **返回体是「说明文字 + JSON」混合**：说明里还夹着 `data.meals.{code}` 这种占位符，直接取第一个 `{` 会解析失败。要逐点尝试 `raw_decode`，取第一个能完整解析且含 `success`/`data` 的对象。
5. **外送场景额外要 `beCode`**：`orderType=2` 且 `beType=2/5/6` 时必传，需先走 `delivery-query-addresses` → `delivery-query-stores`。本项目默认用 `beType=1`（到店取餐）规避。

### 菜单数据结构

`query-meals` 返回两段式结构，分类和详情分开存，要先建 `code → 分类名` 映射再回填：

```
data.categories[].name            分类名
data.categories[].meals[].code    该分类下的餐品编码
data.meals{code}.name             餐品名称
data.meals{code}.currentPrice     现价（字符串）
data.meals{code}.originalPrice    原价（划线价）
data.meals{code}.discountType     优惠类型：早餐卡/促销/麦金卡…
```

实测某门店返回 125 个餐品，其中相当比例是套餐（三件套/四件套/随心选），单品约占一半。

## 4. 业务价值

| 维度 | 价值 |
|---|---|
| **对用户** | 把「今天吃什么」从翻菜单的几分钟，压缩成一条命令；同时守住热量、钠、预算三条线 |
| **对控钠场景** | 官方数据揭示了反直觉事实：板烧鸡腿堡钠 1041mg，高于巨无霸的 961mg。这类结论人工翻菜单几乎发现不了 |
| **对麦当劳** | 把营养数据与在售菜单这两个原本割裂的数据源打通，让「健康搭配」从营销概念变成可计算的决策 |
| **对开发者** | 零依赖实现，可嵌入任何 Agent / 小程序；`--json` 输出便于二次集成 |

## 5. 数据一致性处理

两条数据源存在三类差异，已分别处理：

1. **重复条目**——官网同一产品会出现在多个分类页（如「玉米杯」同时在「小食」和「开心乐园餐」下），`dedup()` 按「归一化品名 + 热量」判重。
2. **规格字段串位**——部分产品页（如 `mini-fries`）的规格项解析会串到品名，`fetch_nutrition_web.py` 对规格名做长度与括号校验，异常时回退为「默认」。
3. **价格缺失**——未加载 MCP 菜单时价格为空，求解器自动降级：预算约束不生效，省钱派切换为「轻量派」并以热量代理花费，同时在终端明确提示，不静默给出错误结论。

## 6. 安全

- MCP Token 只从环境变量 `MCD_MCP_TOKEN` 或 `--token` 参数读取，不写入任何文件
- `mcp-config.example.json` 只含 `${MCD_MCP_TOKEN}` 占位符，无真实凭据
- `data/` 中的快照数据只含公开的餐品营养与价格，不含任何用户个人信息、订单或地址
