#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键体验：从空目录到跑出第一份方案。

    python scripts/quickstart.py            # 交互式，按需逐步执行
    python scripts/quickstart.py --yes      # 非交互，全部用默认（CI 用）
    python scripts/quickstart.py --full     # 有 Token 时连价格与券一起跑

为什么需要它
------------
完整的链路是「营养快照 → MCP 菜单 → 结算价校准 → 券后价折算 → 求解」，
其中后三步依赖 Token。新人 clone 下来往往不知道该先跑哪个，
也分不清哪些步骤可选。这个脚本把顺序固定下来，
并且明确区分：没 Token 能跑到哪一步，有 Token 会多跑到哪一步。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

BAR = "=" * 68
SUB = "-" * 68


def py():
    return sys.executable or "python3"


def run(args, allow_fail=False):
    """运行子命令，输出实时透传。"""
    print(f"\n$ python {' '.join(args)}")
    print(SUB)
    r = subprocess.run([py()] + args, cwd=ROOT)
    print(SUB)
    if r.returncode != 0 and not allow_fail:
        print(f"  命令退出码 {r.returncode}，后续步骤已中止。")
        return False
    return True


def ask(prompt, default_yes=False):
    suffix = "（Y/n）" if default_yes else "（y/N）"
    try:
        ans = input(f"\n{prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default_yes
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def head(title):
    print(f"\n{BAR}\n  {title}\n{BAR}")


def main():
    auto = "--yes" in sys.argv
    full = "--full" in sys.argv
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()

    print(BAR)
    print("  今天麦什么 · 一键体验")
    print(BAR)
    print(f"  Python {sys.version.split()[0]}")
    print(f"  MCP Token：{'已检测到' if token else '未设置（将只跑离线部分）'}")

    if sys.version_info < (3, 9):
        print("\n  需要 Python 3.9 或更高版本。")
        return 1

    # ---------------------------------------------------------- 1 营养快照
    head("1 / 4　营养快照（无需 Token）")
    nut_path = os.path.join(DATA, "nutrition.json")
    if os.path.exists(nut_path):
        print(f"  已存在 {os.path.relpath(nut_path, ROOT)}，跳过。")
        if not auto and ask("要重新抓取吗？", default_yes=False):
            if not run(["scripts/fetch_nutrition_web.py"]):
                return 1
    else:
        print("  营养数据缺失，需从麦当劳官网公开营养计算器抓取（约 2 分钟）。")
        if auto or ask("现在抓取？", default_yes=True):
            if not run(["scripts/fetch_nutrition_web.py"]):
                return 1
        else:
            print("  跳过。没有营养数据无法求解，下次再跑本脚本补上。")
            return 1

    # ---------------------------------------------------------- 2 求解
    head("2 / 4　离线求解（无需 Token）")
    demos = [
        ["--max-kcal", "800", "--max-sodium", "1800", "--persona", "balance"],
        ["--min-protein", "30", "--max-sodium", "1500", "--persona", "value"],
        ["--max-kcal", "600", "--persona", "indulge"],
    ]
    for i, extra in enumerate(demos, 1):
        print(f"\n  示例 {i}/{len(demos)}")
        if not run(["cli.py"] + extra, allow_fail=True):
            return 1

    # ---------------------------------------------------------- 3 价格
    head("3 / 4　价格校准（需要 Token）")
    if not token:
        print("  未设置 MCD_MCP_TOKEN，跳过价格校准。")
        print("  设置方法：export MCD_MCP_TOKEN=你的Token")
        print("  领取地址：https://open.mcd.cn/mcp")
        print("  没有价格时求解照常运行，只是 --budget 不可用。")
    else:
        print("  这一步会写入 data/menu.json 与 data/prices_verified.json。")
        if auto or full or ask("抓取在售菜单并校准官方结算价？", default_yes=True):
            if not run(["scripts/fetch_menu.py"], allow_fail=True):
                return 1
            if not run(["scripts/verify_prices.py"], allow_fail=True):
                return 1

    # ---------------------------------------------------------- 4 券
    head("4 / 4　券后价折算（需要 Token，可选）")
    if not token:
        print("  未设置 Token，跳过。")
    else:
        print("  领取券需你的账号授权，默认只查看不领取。")
        if full or (not auto and ask("查看卡包并折算券后价？", default_yes=False)):
            if not run(["scripts/coupons.py", "--calibrate"], allow_fail=True):
                return 1

    # ---------------------------------------------------------- 收尾
    head("下一步")
    print("  常用命令：")
    print("    python cli.py --budget 45 --max-kcal 1200 --persona all")
    print("    python cli.py --budget 45 --persona all --quote    # 官方复核")
    print("    python cli.py --budget 45 --persona all --coupons  # 最优用券")
    print("    python scripts/selftest.py                         # 离线自检")
    print()
    print("  想换城市：python scripts/fetch_menu.py --city 你的城市")
    print(BAR)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
