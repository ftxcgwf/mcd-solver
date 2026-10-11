# WorkBuddy 联动上下文

本文件用于 WorkBuddy 智能体理解并驱动本项目，参与 WorkBuddy 专项奖励。

## 项目概述

**今天麦什么**（`mcd-solver`）是一个麦当劳点单决策求解器。它在预算、热量、钠、蛋白质的约束下，求解满足感最高的点单组合，输出省钱派 / 嘴馋派 / 均衡派三套方案。

基于麦当劳 MCP 能力开发，为「麦当劳程序员创意开发大赛」参赛作品。

## 本项目的 WorkBuddy 联动方式

本项目的完整开发过程在 WorkBuddy 智能体中完成：需求分析与方向选型、竞品调研、MCP 工具清单梳理、官网营养数据源发现、爬虫与求解器实现、数据缺陷修复、参赛文档撰写，均由 WorkBuddy 驱动完成。

### 运行时联动

WorkBuddy 可通过两种方式驱动本项目：

**方式一：命令行调用（推荐）**

WorkBuddy 解析用户自然语言中的约束，转换为 CLI 参数后执行：

```bash
python cli.py --max-kcal 750 --max-sodium 1800 --persona all
python cli.py --budget 30 --persona value --json
```

`--json` 输出结构化结果，便于 WorkBuddy 二次加工成自然语言答复或表格。

**方式二：加载 SKILL.md**

将 `SKILL.md` 作为 Skill 加载后，WorkBuddy 可直接理解何时应调用本求解器、如何解释结果。SKILL.md 中包含触发词、参数说明、结果解释要点与边界提示。

### 自然语言到参数的映射

| 用户说法 | 对应参数 |
|---|---|
| 「今天只能吃 800 大卡」 | `--max-kcal 800` |
| 「别太咸」/「我血压高」 | `--max-sodium 1500` |
| 「30 块以内」 | `--budget 30` |
| 「我在增肌」/「蛋白质够不够」 | `--min-protein 30` |
| 「怎么点最划算」 | `--persona value` |
| 「想吃爽」 | `--persona indulge` |
| 「健康点」 | `--persona balance` |
| 「到底多少钱」/「帮我确认下价格」 | `--quote` |
| 「就这个，帮我下单」 | `--quote --place` |
| 「我有什么券」/「帮我领券」 | `scripts/coupons.py` |
| 「用哪张券划算」/「要不要凑单」 | `--coupons` |

## 对话示例

**用户**：晚上想吃麦当劳，但是今天已经吃了不少，控制在 600 大卡吧，别太咸。

**WorkBuddy 行为**：识别到热量上限 600、钠需控制，执行

```bash
python cli.py --max-kcal 600 --max-sodium 1500 --persona balance
```

并基于输出解释：为什么选这几样、钠控制在多少、如果想更爽可以怎么换。

**用户**：我健身，蛋白质要够，预算 40。

**WorkBuddy 行为**：

```bash
python cli.py --min-protein 30 --budget 40 --persona all
```

若提示价格缺失，引导用户先执行 `scripts/fetch_menu.py` 抓菜单、
再执行 `scripts/verify_prices.py` 校准结算价。

**用户**：这个真的 45 块以内吗？帮我确认下。

**WorkBuddy 行为**：识别到需要官方复核，执行

```bash
python cli.py --budget 45 --persona balance --quote
```

并转述复核结果：官方原价、优惠、应付金额，以及与求解器估算的差额。
若两者不一致，说明差额来自哪里（打包费 / 券 / 价格校准差异）。

**用户**：就均衡派这个，帮我下单。

**WorkBuddy 行为**：执行 `python cli.py --budget 45 --persona balance --quote --place`，
终端会显示门店、取餐方式与应付金额并要求用户手动输入金额确认。
**WorkBuddy 不得代替用户输入金额**，必须让用户自己完成这一确认，
并说明创建订单后仍需用户在麦当劳 APP 完成支付。

## 结果解释要点

WorkBuddy 在向用户转述结果时，建议遵循：

1. **给理由，不只给菜单**——说明这个组合为什么在满足感和约束之间取得了平衡
2. **主动提示钠**——很多人不知道板烧鸡腿堡钠 1041mg，比巨无霸的 961mg 还高
3. **给边际建议**——「再加 2 元可以换成…」比单纯报价更有说服力
4. **说明数据边界**——营养数据以官方公布为准，实际出品可能有差异；价格随门店浮动
5. **不同人设分数不可横向比较**——权重不同，只在同人设内比较

## 能力边界

- 本项目主体是**决策辅助**；下单需要用户明确要求，且必须让其自行输入金额确认
- 预算约束依赖 MCP 价格与官方结算价校准，缺失时自动降级并明确提示
- 券面额接口不返回，必须由 `calculate-price` 试算得出；领券（`--bind`）属账号写操作，需先征得用户同意
- 服务范围为麦当劳中国大陆，不含港澳台
- 不构成医疗或营养建议

## 文件对应关系

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Skill 定义，供 WorkBuddy 等智能体加载 |
| `cli.py` | 命令行入口，`--json` 输出便于智能体解析 |
| `scripts/coupons.py` | 券查看 / 领取 / 折算券后价 / 逐张试算 |
| `MCP_INTEGRATION.md` | MCP 调用说明（含券工具与踩坑记录） |
| `mcp-config.example.json` | 脱敏 MCP 配置示例 |
| `CONTEST_DECLARATION.md` | 原创性与合规声明 |
