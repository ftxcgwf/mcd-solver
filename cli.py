#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行入口：一句话约束，三套方案。

    python cli.py --max-kcal 800 --max-sodium 2000
    python cli.py --budget 30 --max-kcal 900 --persona all
    python cli.py --min-protein 30 --max-sodium 1500

无需 MCP Token 即可运行（营养数据来自官网公开营养计算器）。
有 Token 时先跑 scripts/fetch_menu.py 校准真实价格，预算约束才会生效。

加 --quote 可把方案交给麦当劳 MCP 做官方券后价复核；
加 --place 会在复核后经人工确认创建真实订单（需 Token）。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from solver import (  # noqa: E402
    PERSONAS, SODIUM_DAILY_LIMIT, Constraints, filter_usable, load_items,
    resolve_persona, role_label, solve,
)
from solver.taste import TEXTURE_ZH  # noqa: E402

BAR = "=" * 68


def fmt_price(p):
    return f"¥{p:.1f}" if p is not None else "—"


def print_plan(rank, r, persona):
    label = r.get("persona_label") or PERSONAS[persona]["label"]
    desc = r.get("persona_desc") or PERSONAS[persona]["desc"]
    t = r["totals"]
    print(f"\n  方案 {rank}｜{label}　满足感 {r['score']}")
    print(f"  {desc}")
    print("  " + "-" * 64)
    for i in r["items"]:
        tags = "·".join(TEXTURE_ZH.get(x, x) for x in sorted(i.textures)) or "—"
        print(f"    {role_label(i.role):<4s} {i.name:<16s} {i.portion:<5s} "
              f"{fmt_price(i.price):>7s}  {i.energy_kcal:>6.0f}kcal  "
              f"钠{i.sodium:>5.0f}mg  [{tags}]")
    print("  " + "-" * 64)
    sodium_flag = ""
    if t["sodium"] > SODIUM_DAILY_LIMIT:
        sodium_flag = f"  ⚠ 超过每日推荐 {t['sodium'] - SODIUM_DAILY_LIMIT:.0f}mg"
    elif t["sodium"] > SODIUM_DAILY_LIMIT * 0.75:
        sodium_flag = "  （已达每日推荐的 75%）"
    print(f"    合计 {fmt_price(t['price'])}　{t['kcal']:.0f}kcal　"
          f"蛋白 {t['protein']:.0f}g　脂肪 {t['fat']:.0f}g　"
          f"碳水 {t['carbohydrate']:.0f}g　钙 {t['calcium']:.0f}mg")
    print(f"    钠 {t['sodium']:.0f}mg{sodium_flag}")

    d = r["detail"]
    print(f"    评分明细  固有 {d['base']}　口感层次 {d['texture']}　"
          f"温度 {d['temp']}　结构 {d['structure']}　健康 {d['health']}　性价比 {d['value']}")


def print_marginal(results, persona):
    """边际效用提示：多花一点钱/热量，能多换来什么。"""
    if len(results) < 2:
        return
    a, b = results[0], results[1]
    ta, tb = a["totals"], b["totals"]
    print(f"\n  {BAR[8:]}")
    print("  换个思路")
    if ta["price"] is not None and tb["price"] is not None:
        dp = tb["price"] - ta["price"]
        dk = tb["kcal"] - ta["kcal"]
        if dp > 0:
            extra = [i.name for i in b["items"] if i.key not in {x.key for x in a["items"]}]
            if extra:
                print(f"    再加 {dp:.1f} 元、{dk:+.0f} kcal，可以换成 "
                      f"{'、'.join(extra[:3])}（满足感 {b['score'] - a['score']:+.1f}）")
        elif dp < 0:
            print(f"    少花 {-dp:.1f} 元可以做到（满足感 {b['score'] - a['score']:+.1f}），"
                  f"代价是 {tb['kcal'] - ta['kcal']:+.0f} kcal")
    dns = tb["sodium"] - ta["sodium"]
    if abs(dns) > 150:
        side = "更低" if dns < 0 else "更高"
        print(f"    另一个方案钠含量 {side} {abs(dns):.0f}mg")


def run_verify(args, out, personas):
    """
    把求解结果交给麦当劳 MCP 做官方复核，并在确认后下单。

    求解器用的是菜单单价累加，真实的券后价、打包费、取餐方式只有
    calculate-price 知道。这一步的意义是：让「我算出来的」变成
    「官方认可的」，差额一目了然。
    """
    token = os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("\n  复核需要 MCP Token：export MCD_MCP_TOKEN=你的Token")
        print("  领取：https://open.mcd.cn/mcp")
        return

    from order import OrderClient, confirm_gate, lines_from_plan, render_quote, yuan

    client = OrderClient(token=token, verbose=False)
    client.initialize()

    store_code = args.store_code
    store_name = "指定门店"
    if not store_code:
        store = client.pick_store(args.city)
        store_code, store_name = store.get("storeCode"), store.get("storeName", "")
    print(f"\n{BAR}")
    print(f"  官方复核 · {store_name}（{store_code}）")
    print(BAR)

    for persona in personas:
        rs = out.get(persona) or []
        if not rs:
            continue
        r = rs[0]
        lines, skipped = lines_from_plan(r["items"])
        if not lines:
            print(f"\n  【{persona}】方案里没有带商品编码的单品，无法复核。")
            continue
        try:
            data = client.quote(store_code, lines)
        except Exception as e:                      # noqa: BLE001
            print(f"\n  【{persona}】复核失败：{e}")
            continue

        est = r["totals"]["price"]
        print(f"\n  【{r.get('persona_label') or persona}】")
        print(render_quote(data, est, skipped))

        if args.place:
            ways = data.get("takeWayList") or []
            take_way = args.take_way or (ways[0].get("code") if ways else None)
            if not take_way:
                print("    该门店未返回取餐方式，跳过下单。")
                continue
            if confirm_gate(yuan(data.get("price")), take_way, store_name):
                res = client.place(store_code, lines, take_way, args.remark)
                if res.get("success"):
                    d = res.get("data") or {}
                    print(f"    下单成功，订单号："
                          f"{d.get('orderNo') or d.get('orderId') or '(见返回)'}")
                    print("    请在麦当劳 APP 或支付链接完成支付。")
                else:
                    print(f"    下单失败：{res.get('message')}")
            else:
                print("    未通过确认，已取消。")
            # 一次只下一单，避免连点产生多笔
            break


def main():
    ap = argparse.ArgumentParser(
        description="今天麦什么 —— 麦当劳点单决策求解器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python cli.py --max-kcal 800 --max-sodium 2000 --persona all",
    )
    ap.add_argument("--budget", type=float, default=None, help="预算上限（元）")
    ap.add_argument("--max-kcal", type=float, default=None, help="热量上限（千卡）")
    ap.add_argument("--max-sodium", type=float, default=None,
                    help=f"钠上限（毫克），每日推荐不超过 {SODIUM_DAILY_LIMIT:.0f}mg")
    ap.add_argument("--min-protein", type=float, default=None, help="蛋白质下限（克）")
    ap.add_argument("--max-items", type=int, default=5, help="最多几件（默认 5）")
    ap.add_argument("--persona", default="all",
                    choices=["value", "indulge", "balance", "all"],
                    help="人设：省钱派 / 嘴馋派 / 均衡派 / 全部")
    ap.add_argument("--top", type=int, default=3, help="每人设输出几个方案")
    ap.add_argument("--nutrition", default=None, help="营养数据路径")
    ap.add_argument("--menu", default=None, help="MCP 菜单数据路径（含价格）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--quote", action="store_true",
                    help="用 MCP calculate-price 做官方券后价复核（需 Token）")
    ap.add_argument("--place", action="store_true",
                    help="复核后创建真实订单（需 Token，且需交互确认）")
    ap.add_argument("--city", default="北京", help="复核/下单用的城市")
    ap.add_argument("--store-code", default=None, help="指定门店编码，跳过自动选择")
    ap.add_argument("--take-way", default=None,
                    help="取餐方式 code，如 eat-in（堂食）/ take-in-store（外带）")
    ap.add_argument("--remark", default=None, help="订单备注，最多 50 字")
    args = ap.parse_args()

    try:
        items = load_items(args.nutrition, args.menu)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    items = filter_usable(items)
    priced_items = [i for i in items if i.price is not None]
    priced = len(priced_items)

    # 用户设了预算，就必须给出真正落在预算内的方案。
    # 若允许混入无价格单品，总价不可知，预算检查会被静默绕过——
    # 那样给出的「35 元方案」其实不知道多少钱，是假答案。
    # 所以预算模式下只在有价格的单品里求解，宁可选择面窄，也不给假答案。
    if args.budget and priced_items:
        items = priced_items

    # 要复核或下单时，只在带 productCode 的单品里求解。
    # 没有编码的单品官方认不出来，选进去只会让复核少算一件。
    if args.quote or args.place:
        orderable = [i for i in items if i.code]
        if orderable:
            items = orderable
        elif not args.json:
            print("  提示：当前没有带商品编码的单品，复核可能失败。")
            print("        请先运行 scripts/fetch_menu.py 抓取菜单（需 Token）。")

    con = Constraints(
        budget=args.budget, max_kcal=args.max_kcal,
        max_sodium=args.max_sodium, min_protein=args.min_protein,
        max_items=args.max_items,
    )

    has_price = priced > 0
    personas = list(PERSONAS) if args.persona == "all" else [args.persona]
    out = {}
    for persona in personas:
        out[persona] = solve(items, con, persona, top_k=args.top, has_price=has_price)

    if args.json:
        import json
        payload = {
            "constraints": vars(con),
            "plans": {
                p: [{
                    "score": r["score"],
                    "items": [i.as_dict() for i in r["items"]],
                    "totals": r["totals"],
                } for r in rs] for p, rs in out.items()
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(BAR)
    print("  今天麦什么 · 麦当劳点单求解器")
    print(BAR)
    cond = []
    if args.budget:
        cond.append(f"预算 {args.budget} 元")
    if args.max_kcal:
        cond.append(f"热量 ≤ {args.max_kcal:.0f} kcal")
    if args.max_sodium:
        cond.append(f"钠 ≤ {args.max_sodium:.0f} mg")
    if args.min_protein:
        cond.append(f"蛋白质 ≥ {args.min_protein:.0f} g")
    print(f"  约束：{'　'.join(cond) if cond else '未设置硬约束'}")
    print(f"  可选单品 {len(items)} 个（其中 {priced} 个有价格数据）")
    if priced == 0:
        print("  提示：未加载价格，预算约束未生效。")
        print("        运行 scripts/fetch_menu.py（需 MCP Token）可校准真实价格。")
    elif args.budget:
        print(f"  预算模式：只在 {priced} 个有确定价格的单品中求解。")
        print("        MCP 菜单里其余条目多为套餐（三件套/随心选），价格不能当单品用，已排除。")
        print("        去掉 --budget 可放开到全部单品，但总价将不可知。")

    for persona in personas:
        rs = out[persona]
        print(f"\n{BAR}")
        print(f"  【{resolve_persona(persona, has_price)['label']}】")
        print(BAR)
        if not rs:
            print("  在当前约束下没有可行组合，试试放宽预算或热量上限。")
            continue
        for n, r in enumerate(rs, 1):
            print_plan(n, r, persona)
        print_marginal(rs, persona)

    if args.quote or args.place:
        run_verify(args, out, personas)

    print(f"\n{BAR}")
    print("  数据来源：麦当劳中国官网营养计算器（2025-04 更新）")
    print("  营养数据以官方公布为准，实际出品可能存在差异。")
    print(BAR)


if __name__ == "__main__":
    main()
