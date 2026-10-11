# -*- coding: utf-8 -*-
"""
价格校准器：把菜单里的「标价」换成官方认可的「结算价」。

为什么要这一步？实测发现菜单列表的 price 字段并不可靠：

    商品编码 510479，菜单里标价 ¥9.9，
    但 calculate-price 给出的结算价是 ¥23.0 —— 差了 2.3 倍。

菜单标价疑似是「起价」或展示价，拿它做预算约束会系统性低估总价。
本项目之前就踩过：求解器算出 ¥44.4，官方复核却是 ¥57.5。

所以预算约束要准，就必须用 calculate-price 逐个校准。

校准方式是**一个商品查一次**，不批量塞进同一个购物车。原因是真实存在
「随单购」组合优惠：抹茶阿芙佳朵单独买单 ¥15.0，与燕麦奶同单就立减
¥7.5。批量查询会把这种凑单优惠记成它的单价，求解器便以为它很便宜。
逐件查询得到的才是「只买这一件要付多少」，凑单优惠留到整单复核时兑现。

一个现实约束：价格随门店不同。优先用菜单来源门店，若该店打烊（60003）
则回退到同城营业中的门店，并在输出里标明。
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, ROOT)

from fetch_menu import McpError  # noqa: E402
from order import OrderClient, yuan  # noqa: E402
from solver.model import is_pack  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")

# 结算端返回的官方明细名，带规格（如「小杯玉米杯」），比菜单名更有权威
_OFFICIAL_NAMES = {}


def quote_one(client, store_code, code):
    """
    查一个商品的结算价，返回单价（元）；查不到返回 None。

    为什么一件一件查，而不是把它们塞进同一个购物车一次性问：

    实测「抹茶阿芙佳朵」单独买单是 ¥15.0，一旦和「燕麦奶」出现在同一张
    单上就立减 ¥7.5（整单 discount=750）。这类「随单购」优惠只在特定
    组合下成立，而批量查询会把整单优惠摊到单品明细上，让这一步记下
    「抹茶阿芙佳朵 ¥7.5」。后果很实在：求解器以为它是性价比极高的饮品，
    于是把它塞进了每一个预算方案，直到 --quote 整单复核才发现是 ¥15.0。

    单件查询拿到的才是「只买这一件要付多少」，这正是预算约束需要的口径。
    凑单优惠留到整单复核时兑现——那时只会更便宜，不会让预算失守。

    顺带解决了售罄处理：批量查询时一件售罄会拖垮整批（60006），需要二分
    拆分；逐件查询每件独立失败，无需任何拆分逻辑。
    """
    try:
        data = client.quote(store_code, [{"productCode": code, "quantity": 1}])
    except McpError as e:
        print(f"      跳过 {code}：{str(e)[:40]}")
        return None
    for p in data.get("productList") or []:
        if str(p.get("productCode")) != str(code):
            continue
        sub, qty = p.get("subtotal"), (p.get("quantity") or 1)
        # 官方返回的明细名常常带规格（菜单里只有泛称「玉米杯」，
        # 结算明细却是「小杯玉米杯」）。这个口径最接近真实下单，
        # 用它反推规格，避免把价格挂到错误的档位上。
        nm = p.get("name") or p.get("productName")
        if nm:
            _OFFICIAL_NAMES[str(code)] = str(nm)
        if sub is not None and qty:
            return round(sub / 100.0 / qty, 2)
    return None


def main():
    ap = argparse.ArgumentParser(description="用 MCP calculate-price 校准真实结算价")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--city", default="北京", help="回退用城市")
    ap.add_argument("--store-code", default=None, help="指定门店，默认用菜单来源门店")
    ap.add_argument("--menu", default=os.path.join(DATA_DIR, "menu.json"), help="菜单数据")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "prices_verified.json"),
                    help="输出路径")
    args = ap.parse_args()

    token = args.token or os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN。", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(args.menu):
        print(f"错误：缺少菜单 {args.menu}，先跑 scripts/fetch_menu.py。", file=sys.stderr)
        sys.exit(1)

    with open(args.menu, encoding="utf-8") as f:
        menu = json.load(f)

    # 组合餐没有单品结算价，也不该进求解器，直接排除。
    # 与求解器共用同一个判断，避免两边口径不一——否则会出现「校准过的价格
    # 求解器用不上、或求解器里的项从没校准过」这种对不上的情况。
    entries = [i for i in menu.get("items", [])
               if i.get("code") and not is_pack(i.get("name"), i.get("category"))]
    print(f"待校准 {len(entries)} 个单品（已排除组合餐 {len(menu.get('items', [])) - len(entries)} 个）")

    client = OrderClient(token=token, verbose=False)
    client.initialize()

    store_code = args.store_code or menu.get("store_code")
    store_name = "菜单来源门店"
    fallback = False
    if store_code:
        try:
            client.quote(store_code, [{"productCode": entries[0]["code"], "quantity": 1}])
        except McpError as e:
            print(f"  菜单来源门店 {store_code} 不可用（{str(e)[:30]}），改用营业中门店。")
            store_code = None
    if not store_code:
        s = client.pick_store(args.city)
        store_code, store_name, fallback = s.get("storeCode"), s.get("storeName", ""), True
    print(f"  门店：{store_name}（{store_code}）")

    prices, names = {}, {}
    codes = [str(e["code"]) for e in entries]
    name_of = {str(e["code"]): e.get("name") for e in entries}

    for n, code in enumerate(codes, 1):
        p = quote_one(client, store_code, code)
        if p is not None:
            prices[code] = p
            names[code] = name_of.get(code)
        if n % 10 == 0 or n == len(codes):
            print(f"  进度 {n}/{len(codes)}：已拿到 {len(prices)} 个结算价")
        if n < len(codes):
            time.sleep(0.2)   # 别把接口打太急

    # 标出与菜单标价不一致的，这正是校准的价值所在
    menu_price = {str(e["code"]): e.get("price") for e in entries}
    diffs = []
    for c, p in prices.items():
        mp = menu_price.get(c)
        if mp is not None and abs(mp - p) > 0.01:
            diffs.append((names.get(c, c), mp, p, p - mp))
    diffs.sort(key=lambda x: -abs(x[3]))

    payload = {
        "source": "mcp:calculate-price",
        "store_code": store_code,
        "store_name": store_name,
        "store_fallback": fallback,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(prices),
        "prices": prices,
        "names": names,
        # 结算端口径的明细名，常带规格；求解器用它把价格挂到正确的规格档位
        "official_names": {c: v for c, v in _OFFICIAL_NAMES.items() if c in prices},
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n校准完成：{len(prices)}/{len(entries)} 个单品，写入 {args.out}")
    if diffs:
        print(f"\n发现 {len(diffs)} 个商品的结算价与菜单标价不一致（前 8 个）：")
        for name, mp, p, d in diffs[:8]:
            arrow = "高" if d > 0 else "低"
            print(f"  {name:<20s} 菜单 ¥{mp:<6.1f} → 结算 ¥{p:<6.1f} （{arrow} ¥{abs(d):.1f}）")
        print("\n结论：菜单标价不能当结算价用，预算约束必须以结算价为准。")
    else:
        print("菜单标价与结算价全部一致。")


if __name__ == "__main__":
    try:
        main()
    except McpError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        sys.exit(1)
