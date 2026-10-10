#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行入口：一句话约束，三套方案。

    python cli.py --max-kcal 800 --max-sodium 2000
    python cli.py --budget 30 --max-kcal 900 --persona all
    python cli.py --min-protein 30 --max-sodium 1500

无需 MCP Token 即可运行（营养数据来自官网公开营养计算器）。
有 Token 时先跑 scripts/fetch_menu.py 校准真实价格，预算约束才会生效。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

    print(f"\n{BAR}")
    print("  数据来源：麦当劳中国官网营养计算器（2025-04 更新）")
    print("  营养数据以官方公布为准，实际出品可能存在差异。")
    print(BAR)


if __name__ == "__main__":
    main()
