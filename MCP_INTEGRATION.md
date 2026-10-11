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
| `query-meals` | 查询门店当前可售卖的餐品列表，含分类、餐品编码与价格 | `scripts/fetch_menu.py` 抓取 `data/menu.json` 时 |
| `query-nearby-stores` | 查询可用门店与其营业状态，取得 `storeCode` | 用户未指定门店时自动选取，且**优先挑营业中的** |
| `calculate-price` | 计算真实券后价，同时返回 `takeWayList`（下单必用的取餐方式） | `--quote` 复核方案时；`scripts/verify_prices.py` 批量校准结算价时；`--coupons` 逐张试算券时 |
| `create-order` | 创建真实订单，返回支付信息 | 用户显式 `--place` 并通过人工确认后 |
| `query-store-coupons` | 查询指定门店 + 订单类型下**真正可用**的券，带 `couponId`/`couponCode` 与适用商品编码 | `--coupons` 时；`--quote` 时展示本店可用券 |
| `query-my-coupons` | 卡包维度的券资产（不做门店校验） | `--coupons` 时与门店券取并集 |
| `available-coupons` | 麦麦省当前**可领取**的券列表 | `scripts/coupons.py` 展示时 |
| `auto-bind-coupons` | 一键领取所有可领券（账号写操作） | 用户显式 `--bind` 时 |
| `query-promotions` | 门店满减/满折促销规则（企业团餐场景） | `--coupons` 时附带展示 |

`query-meals` 是**关键依赖**：官网营养计算器只提供营养成分，不提供价格，而「预算 45 块怎么吃」里价格是硬约束。

`calculate-price` 是**准确性依赖**，这一点比预想的更重要。实测发现菜单列表的 `price` 字段并不可靠：

| 商品 | 菜单标价 | `calculate-price` 结算价 |
|---|---|---|
| 燕麦奶铁（code 510479） | ¥9.9 | **¥23.0** |
| BFF 四宫格小食盘双人餐 | ¥62.9 | **¥122.5** |

菜单标价疑似是「起价」或展示价。本项目早期版本直接采用它，结果求解器算 ¥44.4、官方复核 ¥57.5，低估了 30%。因此 `scripts/verify_prices.py` 会用 `calculate-price` 逐个校准，产出 `data/prices_verified.json`，优先级高于菜单标价。

### 关于券的两个关键发现

**发现一：所有券接口都不返回面额。** 券上写「¥5 优惠」是印在图片上的，接口只给一个 `couponId`。所以「这张券值多少钱」在本地无解——唯一办法是带着 `couponId` 调 `calculate-price` 算一遍再比价。这是全项目里 MCP 不可替代性最强的一环：不用 MCP，这件事做不了。

**发现二：券必须注入价格层，而不是求解完再挑。** 最初的实现是"先求解、再看手里的券能不能用"，结果几乎永远命中不了——方案里恰好没有那件商品。正确的顺序是先用 `calculate-price` 把券折算成**券后价**写进 `data/prices_coupon.json`，让「冰美式有券只要 ¥9.9」从一开始就参与组合选择。实测：

| 商品 | 菜单标价 | 用券后 |
|---|---|---|
| 麦咖啡™美式（code 507411） | ¥11.9 | **¥9.9** |

价格优先级因此定为：`券后价` > `官方结算价` > `菜单标价` > 无价格。

### 已接入但未纳入求解（留作扩展）

| Tool | 现状与扩展方向 |
|---|---|
| `list-nutrition-foods` | 未接入。官网营养快照已覆盖 82 个产品 / 125 个规格，可用于交叉校验或官网数据过期时作为主数据源 |
| `query-meal-detail` | 未接入。可展开套餐组成与特制项，支持「板烧鸡腿堡不要酱」这类定制 |
| `delivery-query-stores` / `delivery-query-addresses` | 未接入。外送场景需先查地址与可达门店，本项目默认 `beType=1`（到店取餐）规避 |

调用这些工具的客户端已实现在 `scripts/fetch_menu.py` 的 `McpClient` 与 `scripts/order.py` 的 `OrderClient` 中（`call_tool(name, arguments)`），扩展只需新增一层解析。

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
        POST tools/call query-store-coupons → 本店可用券（--coupons，需 Token）
        POST tools/call calculate-price {items[].couponId} → 折算券后价
                        │
                        ▼
                data/prices_coupon.json
                        │
                        ▼
        四源合并（品名归一化 + 商品编码精确匹配）
                        │   优先级：券后价 > 结算价 > 菜单标价 > 无价格
                        ▼
        分层束搜索求解 → 三套方案 → 终端 / JSON 输出
                        │
                        ├─►（--coupons）逐张券带进 calculate-price 试算，
        │       输出「本单用哪张券最划算」；方案里没命中券商品时，
        │       再给出「加一件用券只要多花多少」的建议
                        │
                        ▼  （--quote，需 Token）
        POST tools/call query-nearby-stores      → 挑营业中的门店
        POST tools/call calculate-price          → 官方券后价 + takeWayList
                        │
                        ▼  （--place，需人工确认）
        POST tools/call create-order             → 订单号 + 支付链接
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
6. **价格单位是「分」不是「元」**：`calculate-price` 的 `price` / `originalPrice` / `discount` / `packingPrice` 全是整数分。`¥39.50` 在返回体里是 `3950`，直接当元用会放大 100 倍。
7. **门店打烊会报 `60003`**：`calculate-price` 对已打烊门店直接拒绝（"门店暂停营业"）。所以不能固定用菜单来源门店——`query-nearby-stores` 返回的 `businessStatus` / `businessEndTime` 要先看一眼，优先挑营业中的。实测晚上 22:30 后，菜单来源门店（22:00 打烊）已不可用。
8. **售罄商品会让整批失败**：`calculate-price` 一次传多个商品时，只要有一个售罄就整批报 `60006`。本项目用二分拆分定位并剔除坏品，而不是让整批数据报废。
9. **`create-order` 的 `takeWayCode` 不能自己编**：必须取 `calculate-price` 返回的 `data.takeWayList[].code`（实测为 `eat-in` 堂食 / `take-in-store` 外带），自己填字符串会被拒。
10. **菜单标价 ≠ 结算价**：见上文表格。这是最隐蔽的一个——它不会报错，只会让你算出一个偏低的总价。

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
| **对预算场景** | 官方结算价校准让「预算 45 元」从估算变成准数。三个人设在同一预算下钠含量 1555 / 1244 / 769mg，花的钱差不多，健康代价差一倍 |
| **对麦当劳** | 把营养数据、在售菜单与结算价这三个原本割裂的数据源打通，让「健康搭配」从营销概念变成可计算的决策；并把决策直接衔接到下单 |
| **对开发者** | 零依赖实现，可嵌入任何 Agent / 小程序；`--json` 输出便于二次集成 |

## 5. 数据一致性处理

四条数据源存在五类差异，已分别处理：

1. **重复条目**——官网同一产品会出现在多个分类页（如「玉米杯」同时在「小食」和「开心乐园餐」下），`dedup()` 按「归一化品名 + 热量」判重。
2. **规格字段串位**——部分产品页（如 `mini-fries`）的规格项解析会串到品名，`fetch_nutrition_web.py` 对规格名做长度与括号校验，异常时回退为「默认」。
3. **品名对不齐**——官网叫「可口可乐」而 MCP 叫「可乐」；官网叫「冰奶铁」而 MCP 叫「麦咖啡™奶铁」。归一化时剥离品牌前缀，精确匹配之外再做双向子串匹配取最长候选。商品编码（productCode）则严格精确匹配，不模糊——校准本来就是为了消灭歧义。
4. **价格缺失**——未加载 MCP 菜单时价格为空，求解器自动降级：预算约束不生效，省钱派切换为「轻量派」并以热量代理花费，同时在终端明确提示，不静默给出错误结论。
5. **商品售罄**——`calculate-price` 只要整批里有一件售罄，就整单报 60006，连累其他商品算不出价。方案通常只有三五件，所以失败时逐件探测、剔除售罄品再算整批（`quote_resilient()`），比二分更快也更好懂。早餐/深夜时段售罄很常见。

## 6. 安全

- MCP Token 只从环境变量 `MCD_MCP_TOKEN` 或 `--token` 参数读取，不写入任何文件
- `mcp-config.example.json` 只含 `${MCD_MCP_TOKEN}` 占位符，无真实凭据
- `data/` 中的快照数据只含公开的餐品营养与价格，不含任何用户个人信息、订单或地址

### 券数据安全

`couponCode` 是**核销凭证，等同于现金**。公开仓库一旦泄露，任何人拿到都能直接用掉你的券。因此：

- 抓取结果写入 `data/coupons.json` 与 `data/prices_coupon.json`，两者均已加入 `.gitignore`
- 仓库只提交脱敏的结构示例 `data/coupons.example.json`（券码以 `MCD****` 形式示意）
- 券码全程只在内存中参与试算，不写入 README、文档或任何提交文件

### 下单安全

`create-order` 会创建真实订单。虽然麦当劳的流程是**创建订单后仍需用户自己在 APP 或支付链接完成付款**（本项目不会也不会代为支付），但订单本身是真实的，因此设了三道闸：

1. 默认只试算，不加 `--place` 绝不发送 `create-order`
2. 加 `--place` 后必须交互确认：终端显示门店、取餐方式、应付金额，需手动输入金额数字才放行
3. 一次只下一单，逐人设循环时命中即 `break`，避免连点产生多笔订单
