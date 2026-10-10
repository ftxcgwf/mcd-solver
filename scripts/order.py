# -*- coding: utf-8 -*-
"""
下单闭环：把求解出的方案交给麦当劳 MCP 做官方价格复核，并可选真实下单。

这一步解决的是「自己算的不算数」的问题。求解器用菜单里的单价累加，
但真实的券后价、打包费、取餐方式只有官方 calculate-price 能给出。
所以流程必须是：求解 → 官方复核 → （确认后）下单。

三个工具参战：
    query-nearby-stores  找一家正在营业的门店（打烊的店算不了价）
    calculate-price      券后价复核，同时返回 takeWayList（下单必用的取餐方式）
    create-order         创建订单

价格单位统一是「分」，MCP 全程用整数分结算，避免浮点误差。

安全约定（重要）：
    create-order 会创建一笔真实订单。虽然它仍需你自己在麦当劳 APP 或
    支付链接里完成付款，但订单是真的。因此默认只做试算，真实下单必须
    同时满足 --place 与交互确认（手动输入金额），缺一不发送。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_menu import (  # noqa: E402
    McpClient, McpError, extract_text, parse_json_relaxed,
)

# 到店场景：orderType=1，beType=1，不传 beCode
ORDER_TYPE = 1
BE_TYPE = 1


def yuan(fen):
    """分转元。MCP 的价格字段一律是整数分。"""
    if fen is None:
        return None
    return round(fen / 100.0, 2)


class OrderClient(McpClient):
    """在 McpClient 之上封装下单相关的三个调用。"""

    def pick_store(self, city, keyword="麦当劳", prefer_open=True, verbose=True):
        """
        选一家门店。优先挑正在营业的——打烊的门店调 calculate-price
        会直接报 60003「门店暂停营业」，这是最容易踩的坑。
        """
        q = {
            "beType": BE_TYPE,
            "searchType": 2,      # 1 是搜收藏餐厅，没收藏就报错；2 才是按位置搜
            "city": city,
            "keyword": keyword,   # city 与 keyword 必须同时给，缺一个报 600058
        }
        data = parse_json_relaxed(extract_text(
            self.call_tool("query-nearby-stores", q))) or {}
        if not data.get("success"):
            raise McpError(f"门店查询失败：{data.get('message')}")

        raw = data.get("data")
        stores = raw if isinstance(raw, list) else (
            raw.get("stores") if isinstance(raw, dict) else [])
        if not stores:
            raise McpError(f"「{city}」没有查到门店")

        if prefer_open:
            open_stores = [s for s in stores if s.get("businessStatus")]
            if open_stores:
                stores = open_stores
            elif verbose:
                print("  提示：该城市当前没有营业中的门店，仍会尝试下单。")

        if verbose:
            s = stores[0]
            hours = f"{s.get('businessStartTime','?')}-{s.get('businessEndTime','?')}"
            print(f"  门店：{s.get('storeName')}（{s.get('storeCode')}）营业 {hours}")
        return stores[0]

    def quote(self, store_code, lines, reservation_date=None):
        """
        券后价复核。lines 为 [{"productCode": ..., "quantity": n}, ...]。
        返回原始 data 字典（价格单位为分）。
        """
        if not lines:
            raise McpError("复核列表为空")
        arg = {
            "storeCode": store_code,
            "orderType": ORDER_TYPE,
            "beType": BE_TYPE,
            "items": lines,
        }
        if reservation_date:
            arg["reservationDate"] = reservation_date
        data = parse_json_relaxed(extract_text(
            self.call_tool("calculate-price", arg))) or {}
        if not data.get("success"):
            raise McpError(f"价格复核失败：{data.get('message')}（code={data.get('code')}）")
        return data.get("data") or {}

    def store_coupons(self, store_code):
        """查该门店当前可用券。没有券是常态，返回空列表即可。"""
        try:
            data = parse_json_relaxed(extract_text(self.call_tool(
                "query-store-coupons",
                {"storeCode": store_code, "orderType": ORDER_TYPE, "beType": BE_TYPE},
            ))) or {}
        except McpError:
            return []
        raw = data.get("data")
        return raw if isinstance(raw, list) else []

    def place(self, store_code, lines, take_way_code, remark=None):
        """
        创建真实订单。take_way_code 必须来自 calculate-price 的
        takeWayList[].code，自己编一个会被拒。
        """
        arg = {
            "storeCode": store_code,
            "orderType": ORDER_TYPE,
            "beType": BE_TYPE,
            "takeWayCode": take_way_code,
            "items": lines,
        }
        if remark:
            arg["remark"] = remark[:50]
        return parse_json_relaxed(extract_text(self.call_tool("create-order", arg))) or {}


def lines_from_plan(plan):
    """
    把求解方案转成复核/下单用的商品行。

    plan 里每个 item 必须带 code（productCode）。带不出的单品会被跳过——
    与其拿名字去猜，不如少算一件。返回 (lines, skipped)。
    """
    lines, skipped = [], []
    for it in plan:
        code = it.get("code") if isinstance(it, dict) else getattr(it, "code", None)
        if not code:
            skipped.append(it.get("name") if isinstance(it, dict) else it.name)
            continue
        qty = (it.get("quantity") if isinstance(it, dict)
               else getattr(it, "quantity", 1)) or 1
        lines.append({"productCode": str(code), "quantity": int(qty)})
    return lines, skipped


def render_quote(data, estimate=None, skipped=None):
    """把复核结果渲染成人看得懂的对比。"""
    out = []
    total = yuan(data.get("price"))
    origin = yuan(data.get("originalPrice"))
    discount = yuan(data.get("discount"))
    pack = yuan(data.get("packingPrice"))

    out.append("  官方复核（calculate-price）")
    out.append(f"    原价      ¥{origin}")
    if discount:
        out.append(f"    优惠     -¥{discount}")
    if pack:
        out.append(f"    打包费    ¥{pack}")
    out.append(f"    应付      ¥{total}")

    if estimate is not None:
        diff = round(total - estimate, 2)
        flag = "一致" if abs(diff) < 0.01 else (f"求解器低估 ¥{diff}" if diff > 0
                                               else f"求解器高估 ¥{-diff}")
        out.append(f"    与求解器估算 ¥{estimate} 相比：{flag}")

    products = data.get("productList") or []
    if products:
        out.append("    明细：")
        for p in products:
            out.append(
                f"      {p.get('productName','?'):<16s} x{p.get('quantity',1)}  "
                f"¥{yuan(p.get('subtotal'))}"
            )

    if skipped:
        out.append(f"    注意：{len(skipped)} 件无商品编码，未纳入复核 —— {', '.join(skipped[:3])}")

    ways = data.get("takeWayList") or []
    if ways:
        out.append("    取餐方式：" + "、".join(
            f"{w.get('title')}({w.get('code')})" for w in ways))
    return "\n".join(out)


def confirm_gate(total, take_way_code, store_name):
    """
    下单前的人工闸门。必须手动输入金额才放行——
    这是防止误触真实下单的最后一道保险。
    """
    print()
    print("  ┌─ 真实下单确认 ─────────────────────────────")
    print(f"  │ 门店：{store_name}")
    print(f"  │ 取餐方式：{take_way_code}")
    print(f"  │ 应付金额：¥{total}")
    print("  │")
    print("  │ 这将创建一笔真实订单（仍需你在麦当劳 APP 完成支付）。")
    print(f"  └─ 确认请输入金额 {total} 后回车，其他任意输入取消 ──")
    try:
        typed = input("  金额 > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消。")
        return False
    try:
        return abs(float(typed) - total) < 0.01
    except ValueError:
        print("  输入不是数字，已取消。")
        return False


def main():
    ap = argparse.ArgumentParser(
        description="求解方案的官方价格复核与下单（create-order 会创建真实订单）")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--city", default="北京", help="城市名")
    ap.add_argument("--store-code", default=None, help="指定门店编码，跳过自动选择")
    ap.add_argument("--plan", default=None,
                    help="方案 JSON 文件，cli.py --json 的输出")
    ap.add_argument("--item", action="append", default=[],
                    metavar="CODE:QTY", help="直接指定商品，可重复，如 1406:1")
    ap.add_argument("--estimate", type=float, default=None,
                    help="求解器估算价，用于对比")
    ap.add_argument("--take-way", default=None,
                    help="取餐方式 code，如 eat-in / take-in-store；下单时必填")
    ap.add_argument("--place", action="store_true",
                    help="真实下单（需配合交互确认，默认只试算）")
    ap.add_argument("--reservation-date", default=None,
                    help="预约时间，格式 yyyy-MM-dd HH:mm")
    ap.add_argument("--remark", default=None, help="订单备注，最多 50 字")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出原始结果")
    args = ap.parse_args()

    token = args.token or os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN。", file=sys.stderr)
        print("获取方式：https://open.mcd.cn/mcp 登录后在控制台激活。", file=sys.stderr)
        sys.exit(2)

    # 组装复核清单
    lines, skipped = [], []
    if args.plan:
        with open(args.plan, encoding="utf-8") as f:
            blob = json.load(f)
        # 接受 cli.py --json 的两种形态：单方案 或 {persona: [方案]}
        if isinstance(blob, dict) and "items" in blob:
            plan = blob["items"]
        elif isinstance(blob, dict):
            first = next(iter(blob.values()), [])
            plan = (first[0] if isinstance(first, list) and first else {}).get("items", [])
        else:
            plan = blob
        lines, skipped = lines_from_plan(plan)
    for spec in args.item:
        code, _, qty = spec.partition(":")
        lines.append({"productCode": code.strip(), "quantity": int(qty or 1)})
    if not lines:
        print("错误：没有可复核的商品。用 --plan 或 --item 指定。", file=sys.stderr)
        sys.exit(2)

    client = OrderClient(token=token, verbose=False)
    client.initialize()

    store_code = args.store_code
    store_name = "指定门店"
    if not store_code:
        store = client.pick_store(args.city)
        store_code = store.get("storeCode")
        store_name = store.get("storeName", store_code)

    if not args.json:
        print(f"\n复核 {len(lines)} 件商品 ...")

    data = client.quote(store_code, lines, args.reservation_date)

    if args.json:
        print(json.dumps({"store": {"code": store_code, "name": store_name},
                          "quote": data, "skipped": skipped},
                         ensure_ascii=False, indent=2))
        return

    print(render_quote(data, args.estimate, skipped))

    coupons = client.store_coupons(store_code)
    if coupons:
        print(f"    本店可用券 {len(coupons)} 张：" + "、".join(
            str(c.get("title")) for c in coupons[:4]))
    else:
        print("    本店当前无可用券（账号未领券或门店无活动）")

    if not args.place:
        print("\n  仅复核，未下单。加 --place 可创建真实订单。")
        return

    ways = data.get("takeWayList") or []
    take_way = args.take_way
    if not take_way:
        if not ways:
            print("\n  该门店未返回取餐方式，无法下单。", file=sys.stderr)
            sys.exit(1)
        take_way = ways[0].get("code")
        print(f"\n  未指定取餐方式，默认用「{ways[0].get('title')}」({take_way})")

    total = yuan(data.get("price"))
    if not confirm_gate(total, take_way, store_name):
        print("  未通过确认，已取消下单。")
        return

    res = client.place(store_code, lines, take_way, args.remark)
    if res.get("success"):
        d = res.get("data") or {}
        print(f"\n  下单成功。订单号：{d.get('orderNo') or d.get('orderId') or '(见返回)'}")
        print("  请在麦当劳 APP 或支付链接完成支付。")
        print(json.dumps(d, ensure_ascii=False)[:600])
    else:
        print(f"\n  下单失败：{res.get('message')}（code={res.get('code')}）")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except McpError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        sys.exit(1)
