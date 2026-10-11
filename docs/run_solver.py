#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器内运行入口（Pyodide）。

这个文件随 GitHub Pages 一起发布，被 index.html 在 Pyodide 里调用。
它复用了与命令行完全相同的求解内核（solver 包），保证「网页上算出来的」
和「本地跑出来的」逐字节一致。

注意：DATA_DIR 由 solver/model.py 按自身 __file__ 推导，
所以 solver 包必须放在文件系统的 /solver/ 下、数据放在 /data/ 下。
"""
import sys
import json

# 保证 /solver 与 /run_solver.py 都在导入路径上
if "/" not in sys.path:
    sys.path.insert(0, "/")

from solver import (  # noqa: E402
    PERSONAS, Constraints, filter_usable, load_items, solve,
)


def run(budget=None, max_kcal=None, max_sodium=None, min_protein=None,
        max_items=5, persona="all", top_k=3):
    """返回与 cli.py --json 结构一致的 dict。"""
    items = load_items()
    items = filter_usable(items)
    priced = [i for i in items if i.price is not None]
    has_price = len(priced) > 0

    if budget and priced:
        items = priced

    con = Constraints(
        budget=budget, max_kcal=max_kcal, max_sodium=max_sodium,
        min_protein=min_protein, max_items=max_items,
    )

    personas = list(PERSONAS) if persona == "all" else [persona]
    out = {}
    for p in personas:
        out[p] = solve(items, con, p, top_k=top_k, has_price=has_price)

    return {
        "constraints": {
            "budget": budget, "max_kcal": max_kcal,
            "max_sodium": max_sodium, "min_protein": min_protein,
            "max_items": max_items,
        },
        "priced": len(priced),
        "plans": {
            p: [{
                "score": r["score"],
                "persona_label": r.get("persona_label"),
                "persona_desc": r.get("persona_desc"),
                "items": [i.as_dict() for i in r["items"]],
                "totals": r["totals"],
            } for r in rs] for p, rs in out.items()
        },
    }


if __name__ == "__main__":
    # 本地自测：python run_solver.py 40
    import sys as _s
    b = float(_s.argv[1]) if len(_s.argv) > 1 else None
    print(json.dumps(run(budget=b), ensure_ascii=False, indent=2))
