# -*- coding: utf-8 -*-
"""
价格校准器：把菜单里的「标价」换成官方认可的「结算价」。

为什么要这一步？实测发现菜单列表的 price 字段并不可靠：

    商品编码 510479，菜单里标价 ¥9.9，
    但 calculate-price 给出的结算价是 ¥23.0 —— 差了 2.3 倍。

菜单标价疑似是「起价」或展示价，拿它做预算约束会系统性低估总价。
本项目之前就踩过：求解器算出 ¥44.4，官方复核却是 ¥57.5。

所以预算约束要准，就必须用 calculate-price 逐个校准。
一次调用可以传多个商品，返回的 productList[].subtotal 是逐项结算价，
因此按批查询即可，不必一个个来。

两个现实约束：
  1. 价格随门店不同。优先用菜单来源门店，若该店打烊（60003）则回退到
     同城营业中的门店，并在输出里标明。
  2. 售罄商品（60006）会让整批失败。这里用二分拆分定位并剔除，
     而不是让整批数据报废。
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
from solver.model import PACK_WORDS  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
BATCH = 10          # 单批商品数，太大容易被单个售罄品拖垮
MAX_SPLIT_DEPTH = 4  # 二分拆分上限，防止无限递归


def _quote_batch(client, store_code, codes):
    """
    查一批商品的结算价。返回 {code: 单价元}。

    售罄商品会让整批报 60006，此时二分拆成两半各自重试，
    直到拆到单品——单品再失败就确认它不可用，直接丢掉。
    """
    def rec(codes_, depth):
        if not codes_:
            return {}
        lines = [{"productCode": c, "quantity": 1} for c in codes_]
        try:
            data = client.quote(store_code, lines)
        except McpError as e:
            msg = str(e)
            if ("60006" in msg or "售罄" in msg) and len(codes_) > 1 and depth < MAX_SPLIT_DEPTH:
                mid = len(codes_) // 2
                left = rec(codes_[:mid], depth + 1)
                right = rec(codes_[mid:], depth + 1)
                return {**left, **right}
            if len(codes_) > 1 and depth < MAX_SPLIT_DEPTH:
                mid = len(codes_) // 2
                return {**rec(codes_[:mid], depth + 1), **rec(codes_[mid:], depth + 1)}
            print(f"      跳过 {codes_[0]}：{msg[:40]}")
            return {}
        out = {}
        for p in data.get("productList") or []:
            code = str(p.get("productCode"))
            sub, qty = p.get("subtotal"), (p.get("quantity") or 1)
            if sub is not None and qty:
                out[code] = round(sub / 100.0 / qty, 2)
        return out

    return rec(list(codes), 0)


def main():
    ap = argparse.ArgumentParser(description="用 MCP calculate-price 校准真实结算价")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--city", default="北京", help="回退用城市")
    ap.add_argument("--store-code", default=None, help="指定门店，默认用菜单来源门店")
    ap.add_argument("--menu", default=os.path.join(DATA_DIR, "menu.json"), help="菜单数据")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "prices_verified.json"),
                    help="输出路径")
    ap.add_argument("--batch", type=int, default=BATCH, help="单批商品数")
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
    # 与求解器共用同一份 PACK_WORDS，避免两边口径不一。
    entries = [i for i in menu.get("items", [])
               if i.get("code") and not any(w in (i.get("name") or "") for w in PACK_WORDS)]
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
    batches = [codes[i:i + args.batch] for i in range(0, len(codes), args.batch)]

    for n, batch in enumerate(batches, 1):
        got = _quote_batch(client, store_code, batch)
        for c, p in got.items():
            prices[c] = p
            names[c] = name_of.get(c)
        print(f"  第 {n}/{len(batches)} 批：{len(got)}/{len(batch)} 个拿到结算价")
        if n < len(batches):
            time.sleep(0.3)   # 别把接口打太急

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
