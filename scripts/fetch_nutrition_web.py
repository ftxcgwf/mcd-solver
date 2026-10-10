#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从麦当劳中国官网公开的「营养计算器」抓取全量餐品营养成分。

为什么需要这个脚本
------------------
麦当劳 MCP 的 list-nutrition-foods 工具确实能返回营养数据，但它需要申请 MCP Token。
而官网 mcdonalds.com.cn 的营养计算器是面向公众开放的，无需任何鉴权。

本项目采用双数据源设计：
  - 官网快照（本脚本）  -> 离线层，clone 即可用，无需 Token
  - MCP 实时数据        -> 在线层，有 Token 时用于校准在售菜单与价格

官网营养数据由供应商信息与麦当劳全球营养程序手册标准、经国家认可实验室测定，
页面标注数据更新时间为 2025 年 4 月。抓取结果会保留 data_date 字段以便溯源。

零第三方依赖，仅用标准库。

用法:
    python scripts/fetch_nutrition_web.py
    python scripts/fetch_nutrition_web.py --out data/nutrition.json
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://www.mcdonalds.com.cn"

# 官网公开的餐品分类页
CATEGORIES = [
    ("burger", "/index/Food/menu/burger"),
    ("snacks", "/index/Food/menu/snacks-3"),
    ("beverage", "/index/Food/menu/beverage-2"),
    ("dessert", "/index/Food/menu/Dessert-11"),
    ("breakfast", "/index/Food/menu/breakfast"),
    ("happy_meals", "/index/Food/menu/happy-meals"),
    ("mccafe", "/index/Food/mccafe/mccafe-menu"),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 千焦 -> 千卡
KJ_PER_KCAL = 4.184

# 营养字段中文名 -> 输出字段名
FIELD_MAP = {
    "能量": "energy_kj",
    "蛋白质": "protein",
    "脂肪": "fat",
    "碳水化合物": "carbohydrate",
    "钠": "sodium",
    "钙": "calcium",
}


def fetch(url, retries=3, timeout=30):
    """抓取页面，带重试与退避。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  [warn] 抓取失败 {url}: {last_err}", file=sys.stderr)
    return None


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_number(s):
    """从 '2146' / '2146千焦' / '0.5' 中提取数字。"""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def list_products(category_path):
    """从分类页提取产品 slug 列表。"""
    html = fetch(BASE + category_path)
    if not html:
        return []
    slugs = []
    for m in re.finditer(r'href="[^"]*?/product/([A-Za-z0-9\-_%]+)"', html):
        s = m.group(1)
        if s not in slugs:
            slugs.append(s)
    return slugs


def parse_product(slug, category):
    """解析单个产品页：品名、描述、各规格的营养数据。"""
    html = fetch(f"{BASE}/product/{slug}")
    if not html:
        return None

    name_m = re.search(r'<h3>(.*?)</h3>', html, re.S)
    if not name_m:
        return None
    name = strip_tags(name_m.group(1))
    if not name:
        return None

    desc_m = re.search(r'<p class="detail">(.*?)</p>', html, re.S)
    desc = strip_tags(desc_m.group(1)) if desc_m else ""

    item = {
        "name": name,
        "slug": slug,
        "category": category,
        "desc": desc,
        "url": f"{BASE}/product/{slug}",
        "variants": [],
    }

    # 定位营养表区块
    tbl_m = re.search(
        r'<div class="classified-table">(.*?)</div>\s*</div>\s*</div>', html, re.S
    )
    block = tbl_m.group(1) if tbl_m else html

    # 规格名（迷你/小/中/大/1个 ...）
    portions = [strip_tags(x) for x in re.findall(r'<li[^>]*>(.*?)</li>', block)]
    portions = [p for p in portions if p and len(p) <= 12] or ["1份"]

    # 每个 table-wrap 对应一个规格的一行数据
    wraps = re.findall(r'<div class="table-wrap".*?</table>', block, re.S)
    if not wraps:
        wraps = re.findall(r'<table>.*?</table>', block, re.S)

    for idx, wrap in enumerate(wraps):
        portion = portions[idx] if idx < len(portions) else portions[0]
        # 规格名不应该是品名本身，也不该带括号——出现这种情况说明
        # table-nav 里没有规格项，正则串到了品名或面包屑上（如 mini-fries 页面）
        if portion == name or "(" in portion or "（" in portion or len(portion) > 6:
            portion = "默认"
        cells = re.findall(r"<td>(.*?)</td>", wrap, re.S)
        rec = {"portion": portion}
        got = False
        for cell in cells:
            k_m = re.search(r"<span>(.*?)</span>", cell, re.S)
            v_m = re.search(r"<strong>(.*?)</strong>", cell, re.S)
            if not k_m or not v_m:
                continue
            key = strip_tags(k_m.group(1))
            val = parse_number(strip_tags(v_m.group(1)))
            field = FIELD_MAP.get(key)
            if field and val is not None:
                rec[field] = val
                got = True
        if got:
            # 官网只给千焦，补算千卡便于直接使用
            if "energy_kj" in rec:
                rec["energy_kcal"] = round(rec["energy_kj"] / KJ_PER_KCAL, 1)
            item["variants"].append(rec)

    if not item["variants"]:
        return None
    return item


def main():
    ap = argparse.ArgumentParser(description="抓取麦当劳中国官网公开营养数据")
    ap.add_argument("--out", default="data/nutrition.json", help="输出路径")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数（礼貌抓取）")
    args = ap.parse_args()

    all_slugs = []
    seen = set()
    print("正在扫描分类页 ...")
    for cat, path in CATEGORIES:
        slugs = list_products(path)
        new = [s for s in slugs if s not in seen]
        print(f"  {cat:12s} {len(slugs):3d} 个（新增 {len(new)}）")
        for s in new:
            seen.add(s)
            all_slugs.append((cat, s))
        time.sleep(args.delay)

    print(f"\n共发现 {len(all_slugs)} 个产品，开始抓取详情 ...")
    items = []
    for i, (cat, slug) in enumerate(all_slugs, 1):
        it = parse_product(slug, cat)
        if it:
            items.append(it)
            nvar = len(it["variants"])
            kcal = it["variants"][0].get("energy_kcal")
            na = it["variants"][0].get("sodium")
            print(f"  [{i:3d}/{len(all_slugs)}] {it['name']:<20s} {nvar}规格 kcal={kcal} 钠={na}")
        else:
            print(f"  [{i:3d}/{len(all_slugs)}] (跳过) {slug}")
        time.sleep(args.delay)

    payload = {
        "source": "麦当劳中国官网营养计算器（公开页面）",
        "source_url": BASE,
        "note": (
            "官网页面标注营养成分数据更新时间为 2025 年 4 月；"
            "数据由供应商信息与麦当劳全球营养程序手册标准，经国家认可实验室测定。"
            "实际出品可能因批次、产地、工艺存在差异。"
        ),
        "data_date": "2025-04",
        "fetched_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "energy_unit": "energy_kj 为千焦，energy_kcal 为千卡（由千焦除以 4.184 换算）",
        "count": len(items),
        "items": items,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n完成：{len(items)} 个产品已写入 {args.out}")


if __name__ == "__main__":
    main()
