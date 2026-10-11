# -*- coding: utf-8 -*-
"""
挂载交叉验证：用一个独立数据源，倒查每一个「商品编码 -> 官网产品」的对应。

为什么需要这一步
----------------
菜单那头，一个商品编码常常代表一类商品（菜单只有「牛奶」一条），
而官网营养数据按冷热、口味分列（「冰牛奶」「热牛奶」「浓浓抹茶牛奶」）。
让谁承接这个编码，我们原来是在自己的匹配逻辑里挑的——在自己写的逻辑里
自我检查，很难发现系统性偏差。

麦当劳 MCP 提供了另一份完全独立的营养数据：list-nutrition-foods，158 条，
写法是规格直接写在名字里（「可乐中杯」「热牛奶中杯」）。于是可以这样验证：

    已知编码 507394 在结算端叫「热牛奶中杯」
    -> 去那份独立数据里查「热牛奶中杯」，得到 176 kcal / 钠 101 mg
    -> 若我们挂的是「热牛奶 中」，数值必须吻合；挂错了就一定对不上

用法
----
    export MCD_MCP_TOKEN=你的Token
    python scripts/crosscheck_nutrition.py              # 人读报告
    python scripts/crosscheck_nutrition.py --json       # 机器读
    python scripts/crosscheck_nutrition.py --strict     # 有不符与非 0 退出

返回码：0 全部吻合；1 存在不符（配合 --strict 可作门禁）。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fetch_menu import (  # noqa: E402
    McpClient, McpError, extract_text, parse_json_relaxed,
)
from solver.model import _norm, load_items, load_official_names  # noqa: E402

# 官网数据与 MCP 这份是两次独立测定，且各自做了四舍五入，因此留出容差：
# 相对误差 3%，并对小数值再给一个绝对底线，避免 0 附近的数值被误判。
KCAL_TOL_RATE = 0.03
KCAL_TOL_MIN = 2.0
SODIUM_TOL_RATE = 0.03
SODIUM_TOL_MIN = 3.0


def fetch_table(client):
    """
    取回 list-nutrition-foods 的营养表。

    返回 {归一化名: {"raw": 原名, "kcal": 数值, "sodium": 数值}}。
    解析按逗号切分后**从末尾取值**——品名里可能带逗号，从头部数会错位。
    """
    resp = client.call_tool("list-nutrition-foods", {})
    data = parse_json_relaxed(extract_text(resp)) or {}
    if not data.get("success"):
        raise McpError("营养表获取失败：%s" % data.get("message"))

    payload = data.get("data")
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)

    table = {}
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9 or parts[0] == "productName":
            continue
        try:
            kcal = float(parts[-6])      # 倒数第 6 位是千卡
            sodium = float(parts[-2])    # 倒数第 2 位是钠
        except ValueError:
            continue
        table[_norm(parts[0]).replace("-", "")] = {
            "raw": parts[0], "kcal": kcal, "sodium": sodium,
        }
    return table


def lookup(table, *candidates):
    """按候选名依次查表，命中即返回。"""
    for c in candidates:
        if not c:
            continue
        hit = table.get(_norm(c).replace("-", ""))
        if hit:
            return hit
    return None


def crosscheck(items, official_names, table):
    """逐项核对。返回三元组：(吻合, 不符, 无法核对)。"""
    ok, bad, unknown = [], [], []
    for it in items:
        if not it.code:
            continue
        oname = official_names.get(str(it.code))
        portion = (it.portion or "").strip()
        cands = []
        if oname:
            cands.append(oname)
        cands.append(it.name if portion in ("默认", "1个", "一杯", "1盒")
                     else it.name + portion)
        cands.append(it.name)

        hit = lookup(table, *cands)
        if not hit:
            unknown.append((it, oname))
            continue

        dk = abs(hit["kcal"] - (it.energy_kcal or 0))
        ds = abs(hit["sodium"] - (it.sodium or 0))
        row = {
            "code": str(it.code),
            "item": it.name,
            "portion": portion,
            "official_name": oname or "",
            "table_name": hit["raw"],
            "our_kcal": it.energy_kcal,
            "ref_kcal": hit["kcal"],
            "our_sodium": it.sodium,
            "ref_sodium": hit["sodium"],
        }
        if (dk <= max(KCAL_TOL_MIN, hit["kcal"] * KCAL_TOL_RATE)
                and ds <= max(SODIUM_TOL_MIN, hit["sodium"] * SODIUM_TOL_RATE)):
            ok.append(row)
        else:
            bad.append(row)
    return ok, bad, unknown


def main():
    ap = argparse.ArgumentParser(description="用 MCP 独立营养数据交叉验证挂载")
    ap.add_argument("--token", default=None, help="MCP Token（默认读环境变量）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="存在不符时返回码非 0")
    args = ap.parse_args()

    token = args.token or os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN。", file=sys.stderr)
        return 2

    items = load_items()
    official_names = load_official_names()
    orderable = [i for i in items if i.code]

    client = McpClient(token=token, verbose=False)
    client.initialize()
    table = fetch_table(client)

    ok, bad, unknown = crosscheck(items, official_names, table)

    if args.json:
        print(json.dumps({
            "reference_rows": len(table),
            "orderable": len(orderable),
            "matched": ok, "mismatched": bad,
            "unverifiable": [{"item": i.name, "portion": i.portion,
                              "code": str(i.code), "official_name": o or ""}
                             for i, o in unknown],
        }, ensure_ascii=False, indent=2))
    else:
        print("参考数据：MCP list-nutrition-foods，%d 条" % len(table))
        print("待核对：%d 个可下单单品\n" % len(orderable))
        print("=== 吻合 %d 个 ===" % len(ok))
        for r in ok:
            print("  %-14s %-4s %-12s 官方名=%-16s -> 查到 %-16s %skcal/钠%s"
                  % (r["item"], r["portion"], r["code"],
                     r["official_name"] or "(无)", r["table_name"],
                     r["our_kcal"], r["our_sodium"]))
        if bad:
            print("\n=== 不符 %d 个（挂错了）===" % len(bad))
            for r in bad:
                print("  %-14s %-4s %-12s 官方名=%-16s" % (
                    r["item"], r["portion"], r["code"], r["official_name"]))
                print("       ours kcal %s 钠 %s | ref %s kcal %s 钠 %s" % (
                    r["our_kcal"], r["our_sodium"], r["table_name"],
                    r["ref_kcal"], r["ref_sodium"]))
        if unknown:
            print("\n=== 无权威依据可核对 %d 个（如菜单只写泛称「新地」）===" % len(unknown))
            for it, o in unknown:
                print("  %-14s %-4s %-12s 官方名=%s"
                      % (it.name, it.portion, it.code, o or "(无)"))
        print("\n小结：吻合 %d / 不符 %d / 无从核对 %d"
              % (len(ok), len(bad), len(unknown)))

    if bad:
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except McpError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        sys.exit(1)
