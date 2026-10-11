#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自检：把「我以为对的」变成「机器验过的」。

设计原则：
  1. 全程离线，不需要 MCP Token（CI 里没有 Token 也要能跑）
  2. 零第三方依赖，只用标准库
  3. 断言的是**契约**而不是具体数值——价格会变、菜单会换，
     但「方案总价不得超过预算」这条永远成立

检查项：
  A. 全部 Python 文件语法可编译
  B. 四源数据可加载且规模达标
  C. 三种人设在无预算约束下都有可行解
  D. 硬约束（预算 / 热量 / 钠 / 蛋白质 / 件数）无一被违反
  E. --json 输出是合法 JSON 且结构完整
  F. 极端约束下不崩溃（返回空解而非异常）
  G. 无 Token 时 MCP 脚本优雅提示退出，而非抛 traceback
  H. 敏感文件（Token / 券码）未被 git 跟踪

用法：
    python scripts/selftest.py            # 人类可读输出
    python scripts/selftest.py --quiet    # 只报失败项，供 CI 使用
"""

import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")

# 规模下限：低于这些值说明数据管道坏了，而不是「麦当劳上新了」
MIN_PRODUCTS = 60      # 营养快照产品数
MIN_VARIANTS = 90      # 营养快照规格总数
MIN_MENU = 80          # MCP 菜单条目数

GREEN = "\033[32m" if sys.stdout.isatty() else ""
RED = "\033[31m" if sys.stdout.isatty() else ""
DIM = "\033[2m" if sys.stdout.isatty() else ""
RESET = "\033[0m" if sys.stdout.isatty() else ""


class Report:
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            if not self.quiet:
                print(f"  {GREEN}✓{RESET} {name}" +
                      (f"  {DIM}{detail}{RESET}" if detail else ""))
        else:
            self.failed += 1
            self.failures.append((name, detail))
            print(f"  {RED}✗ {name}{RESET}" +
                  (f"  {detail}" if detail else ""))
        return ok

    def section(self, title):
        if not self.quiet:
            print(f"\n{title}")


def python_exe():
    return sys.executable or "python3"


def run(cmd, env=None, timeout=180):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        [python_exe()] + cmd,
        cwd=ROOT, env=e, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


# ---------------------------------------------------------------- A 语法

def check_compile(rep):
    rep.section("A. 语法编译")
    files, errors = [], []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git")]
        for f in sorted(filenames):
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                compile(fh.read(), path, "exec")
        except SyntaxError as e:
            errors.append(f"{os.path.relpath(path, ROOT)}:{e.lineno} {e.msg}")
    rep.check(f"{len(files)} 个 Python 文件语法通过", not errors,
              "；".join(errors[:3]))


# ---------------------------------------------------------------- B 数据

def check_data(rep):
    rep.section("B. 数据完整性")
    nut_path = os.path.join(DATA, "nutrition.json")
    menu_path = os.path.join(DATA, "menu.json")

    if not rep.check("data/nutrition.json 存在", os.path.exists(nut_path)):
        return None
    if not rep.check("data/menu.json 存在", os.path.exists(menu_path)):
        return None

    with open(nut_path, encoding="utf-8") as f:
        nut = json.load(f)
    with open(menu_path, encoding="utf-8") as f:
        menu = json.load(f)

    products = nut.get("items") or []
    variants = sum(len(i.get("variants") or []) for i in products)
    meals = menu.get("items") or []

    rep.check(f"营养产品数 {len(products)} ≥ {MIN_PRODUCTS}",
              len(products) >= MIN_PRODUCTS)
    rep.check(f"营养规格数 {variants} ≥ {MIN_VARIANTS}",
              variants >= MIN_VARIANTS)
    rep.check(f"菜单条目数 {len(meals)} ≥ {MIN_MENU}",
              len(meals) >= MIN_MENU)

    # 营养字段完整性：缺钠整个控钠模型就废了
    bad = []
    for i in products:
        for v in i.get("variants") or []:
            if v.get("energy_kcal") is None or v.get("sodium") is None:
                bad.append(f"{i['name']}/{v.get('portion')}")
    rep.check("所有规格均含热量与钠", not bad,
              f"缺失 {len(bad)} 项" + (f"：{bad[:3]}" if bad else ""))

    return nut, menu


# ---------------------------------------------------------------- C/D 求解契约

def check_solver(rep):
    rep.section("C/D. 求解与硬约束")

    try:
        from solver import PERSONAS, Constraints, filter_usable, load_items, solve
    except Exception as e:  # noqa: BLE001
        rep.check("solver 可导入", False, repr(e))
        return

    try:
        items = filter_usable(load_items())
    except Exception as e:  # noqa: BLE001
        rep.check("数据可加载", False, repr(e))
        return

    rep.check(f"可用单品 {len(items)} 个", len(items) > 20)
    priced = [i for i in items if i.price is not None]
    rep.check(f"有价格单品 {len(priced)} 个", len(priced) > 10)

    # C. 三种人设在宽松约束下都应有解
    loose = Constraints(max_kcal=1500, max_items=5)
    for p in PERSONAS:
        rs = solve(items, loose, p, top_k=3, has_price=bool(priced))
        rep.check(f"人设 {p} 有可行解", len(rs) > 0,
                  f"{len(rs)} 个方案")

    # D. 硬约束不可违反（含预算）
    budget = 45.0
    tight = Constraints(budget=budget, max_kcal=900,
                        max_sodium=1600, min_protein=15, max_items=4)
    pool = priced or items
    violations = []
    plans = 0
    for p in PERSONAS:
        for r in solve(pool, tight, p, top_k=3, has_price=bool(priced)):
            plans += 1
            t = r["totals"]
            if t["price"] is not None and t["price"] > budget + 1e-6:
                violations.append(f"{p} 超预算 {t['price']}")
            if t["kcal"] > 900 + 1e-6:
                violations.append(f"{p} 超热量 {t['kcal']}")
            if t["sodium"] > 1600 + 1e-6:
                violations.append(f"{p} 超钠 {t['sodium']}")
            if (t["protein"] or 0) < 15 - 1e-6:
                violations.append(f"{p} 蛋白不足 {t['protein']}")
            if t["count"] > 4:
                violations.append(f"{p} 超件数 {t['count']}")

    rep.check(f"{plans} 个方案全部满足硬约束", not violations,
              "；".join(violations[:3]) if violations else "")

    # 结构完整性：有主食才算一顿饭
    no_main = []
    for p in PERSONAS:
        for r in solve(items, loose, p, top_k=1, has_price=bool(priced)):
            if not any(i.role == "main" for i in r["items"]):
                no_main.append(p)
    rep.check("宽松约束下首选方案均含主食", not no_main,
              "无主食：" + ",".join(no_main) if no_main else "")


# ---------------------------------------------------------------- E JSON

def check_json(rep):
    rep.section("E. JSON 输出")
    pr = run(["cli.py", "--max-kcal", "900", "--persona", "balance", "--json"])
    if not rep.check("cli --json 退出码为 0", pr.returncode == 0,
                     pr.stderr.decode("utf-8", "replace")[-200:]):
        return
    try:
        payload = json.loads(pr.stdout.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        rep.check("输出是合法 JSON", False, repr(e))
        return
    rep.check("输出是合法 JSON", True)
    rep.check("含 constraints 与 plans",
              "constraints" in payload and "plans" in payload)
    plans = payload.get("plans") or {}
    ok_shape = all(
        isinstance(v, list) and (not v or (
            "score" in v[0] and "items" in v[0] and "totals" in v[0]))
        for v in plans.values()
    )
    rep.check("方案结构完整（score/items/totals）", ok_shape)


# ---------------------------------------------------------------- F 边界

def check_edge(rep):
    rep.section("F. 边界与降级")
    cases = [
        ("极小预算 --budget 5", ["--budget", "5"]),
        ("极小热量 --max-kcal 100", ["--max-kcal", "100"]),
        ("极严钠 --max-sodium 0", ["--max-sodium", "0"]),
        ("高蛋白 --min-protein 200", ["--min-protein", "200"]),
        ("单件限制 --max-items 1", ["--max-items", "1"]),
    ]
    for name, extra in cases:
        pr = run(["cli.py"] + extra + ["--persona", "balance"])
        ok = pr.returncode == 0 and b"Traceback" not in pr.stderr
        rep.check(f"{name} 不崩溃", ok,
                  pr.stderr.decode("utf-8", "replace")[-160:] if not ok else "")

    # 无 Token 时必须优雅提示，而不是报 traceback
    env = {"MCD_MCP_TOKEN": ""}
    e = dict(os.environ)
    e.pop("MCD_MCP_TOKEN", None)
    pr = subprocess.run(
        [python_exe(), os.path.join("scripts", "fetch_menu.py")],
        cwd=ROOT, env=e, timeout=60,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    err = pr.stderr.decode("utf-8", "replace")
    ok = b"Traceback" not in pr.stderr and (
        "Token" in pr.stdout.decode("utf-8", "replace") or "Token" in err)
    rep.check("无 Token 时 fetch_menu 优雅提示退出", ok,
              err[-160:] if not ok else "")

    _ = env


# ---------------------------------------------------------------- H 安全

def check_secret(rep):
    rep.section("G. 敏感数据安全")
    try:
        pr = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tracked = pr.stdout.decode("utf-8", "replace").split()
    except Exception as e:  # noqa: BLE001
        rep.check("git ls-files 可运行", False, repr(e))
        return

    forbidden = ("data/coupons.json", "data/prices_coupon.json",
                 ".env", "data/prices.json")
    leaked = [f for f in forbidden if f in tracked]
    rep.check("敏感文件未被 git 跟踪", not leaked,
              "已跟踪：" + ",".join(leaked) if leaked else "")

    # 已跟踪文件里不得出现真实 Token 或券码。
    # 注意：文档里合法地会提到 couponCode 这个**字段名**，
    # 所以只匹配「键后面跟了疑似真实值」的情形，避免误报。
    patterns = [
        # MCD_MCP_TOKEN=真实值（排除占位符、空值与教学文案）
        re.compile(r"MCD_MCP_TOKEN\s*[=:]\s*[\"']?(?!$|\"|'|你的|\$\{|示例|<)"
                   r"[A-Za-z0-9_\-]{8,}"),
        # "couponCode": "真实券码"（脱敏值形如 MCD****）
        re.compile(r"\"couponCode\"\s*:\s*\"(?!MCD\*|示例|<)[^\"]{6,}\""),
    ]
    hits = []
    for rel in tracked:
        if not rel.endswith((".py", ".json", ".md", ".yml", ".yaml")):
            continue
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for i, pat in enumerate(patterns):
            m = pat.search(text)
            if m:
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{rel}:{line} 命中{i + 1}")

    rep.check("已跟踪文件中无真实 Token / 券码", not hits,
              "；".join(hits[:3]) if hits else "")


def main():
    quiet = "--quiet" in sys.argv
    rep = Report(quiet=quiet)

    if not quiet:
        print("=" * 68)
        print("  今天麦什么 · 离线自检")
        print("=" * 68)

    t0 = time.time()
    check_compile(rep)
    check_data(rep)
    check_solver(rep)
    check_json(rep)
    check_edge(rep)
    check_secret(rep)
    cost = time.time() - t0

    print()
    if rep.failed:
        print(f"{RED}✗ {rep.failed} 项失败{RESET}，{rep.passed} 项通过"
              f"　{DIM}耗时 {cost:.1f}s{RESET}")
        for name, detail in rep.failures:
            print(f"    - {name}" + (f"：{detail}" if detail else ""))
        return 1
    print(f"{GREEN}✓ 全部 {rep.passed} 项检查通过{RESET}"
          f"　{DIM}耗时 {cost:.1f}s{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
