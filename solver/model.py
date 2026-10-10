# -*- coding: utf-8 -*-
"""
数据模型：把「官网营养快照」与「MCP 实时菜单」统一成求解器能吃的结构。

两条数据来源：
  - data/nutrition.json  官网营养计算器，无需 Token，含六项营养但不含价格
  - data/menu.json       MCP query-meals 抓取，含真实在售价格（需 Token）

两者以品名做模糊匹配后合并。合并策略是「MCP 优先，官网兜底」：
价格只有 MCP 能给，营养以官网实验室数据为准。

价格缺失是允许的——此时求解器只按营养与满足感求解，预算约束自动降级。
"""

import json
import os
import re

from .taste import (
    ROLE_BY_CATEGORY, ROLE_ZH, base_score, infer_textures, load_overrides,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class Item:
    """一个可选单品（含规格）。例如「薯条 中份」和「薯条 大份」是两个 Item。"""

    __slots__ = (
        "key", "name", "slug", "category", "role", "portion", "price",
        "energy_kcal", "protein", "fat", "carbohydrate", "sodium", "calcium",
        "textures", "base", "source",
    )

    def __init__(self, **kw):
        for f in self.__slots__:
            setattr(self, f, kw.get(f))

    def texture_labels(self):
        return sorted(self.textures or [])

    def as_dict(self):
        return {f: getattr(self, f) for f in self.__slots__ if f != "textures"} | {
            "textures": self.texture_labels(),
        }

    def __repr__(self):
        p = f"¥{self.price:.1f}" if self.price is not None else "价格未知"
        return f"<Item {self.name}({self.portion}) {p} {self.energy_kcal}kcal 钠{self.sodium}mg>"


def _norm(s):
    """品名归一化：去空格与常见括号内容，用于跨源匹配。"""
    s = (s or "").strip()
    s = re.sub(r"[（(].*?[)）]", "", s)
    return re.sub(r"\s+", "", s)


def load_prices(path=None):
    """加载参考价表。结构：{"巨无霸": 24.0, "薯条|中": 12.5, ...}"""
    path = path or os.path.join(DATA_DIR, "prices.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def items_from_nutrition(nut, prices=None, overrides=None):
    """把官网营养快照展开为 Item 列表。"""
    prices = prices or {}
    overrides = overrides or {}
    out = []
    for it in nut.get("items", []):
        name = it.get("name", "").strip()
        if not name:
            continue
        cat = it.get("category", "")
        role = ROLE_BY_CATEGORY.get(cat, "side")
        for v in it.get("variants", []):
            portion = v.get("portion", "1份")
            textures = infer_textures(name, it.get("desc", ""), overrides)
            price = prices.get(name) or prices.get(f"{name}|{portion}")
            out.append(Item(
                key=f"{it.get('slug','')}::{portion}",
                name=name,
                slug=it.get("slug", ""),
                category=cat,
                role=role,
                portion=portion,
                price=float(price) if price is not None else None,
                energy_kcal=v.get("energy_kcal"),
                protein=v.get("protein"),
                fat=v.get("fat"),
                carbohydrate=v.get("carbohydrate"),
                sodium=v.get("sodium"),
                calcium=v.get("calcium"),
                textures=textures,
                base=base_score(role, textures),
                source="web",
            ))
    return out


def apply_mcp_menu(items, menu, prices=None):
    """
    用 MCP 的实时菜单数据校准价格。

    menu 为 scripts/fetch_menu.py 产出的结构：
        {"items": [{"name": ..., "price": 12.5, "code": ...}, ...]}

    匹配不上的单品保持原价（可能仍为 None）。返回被成功匹配的数量。
    """
    if not menu:
        return 0
    lookup = {}
    for m in menu.get("items", []):
        n = _norm(m.get("name"))
        if n and m.get("price") is not None:
            lookup[n] = float(m["price"])

    hit = 0
    for it in items:
        n = _norm(it.name)
        if n in lookup:
            it.price = lookup[n]
            it.source = "mcp"
            hit += 1
    return hit


def load_items(nutrition_path=None, menu_path=None, prices_path=None):
    """
    统一入口：加载并合并两条数据源，返回 Item 列表。

    缺 nutrition.json 时抛出明确错误，因为那是离线可跑的基石。
    """
    nutrition_path = nutrition_path or os.path.join(DATA_DIR, "nutrition.json")
    if not os.path.exists(nutrition_path):
        raise FileNotFoundError(
            f"缺少营养数据 {nutrition_path}。\n"
            f"请先运行（无需 Token）：python scripts/fetch_nutrition_web.py"
        )
    with open(nutrition_path, encoding="utf-8") as f:
        nut = json.load(f)

    prices = load_prices(prices_path)
    overrides = load_overrides(os.path.join(DATA_DIR, "taste_override.json"))
    items = dedup(items_from_nutrition(nut, prices, overrides))

    if menu_path and os.path.exists(menu_path):
        with open(menu_path, encoding="utf-8") as f:
            apply_mcp_menu(items, json.load(f))

    return items


def dedup(items):
    """
    去重。官网同一产品会以不同形态重复出现：
      - 「玉米杯」同时挂在「小食」和「开心乐园餐」下，role 不一致
      - 「薯条（迷你）」是独立产品页，与「薯条」的迷你规格是同一样东西

    因此按「归一化品名 + 热量」判重：去掉括号与空格后同名且热量相同，
    就是同一个东西，保留先出现的（分类靠前的通常是正经分类页）。
    """
    out, seen = [], set()
    for it in items:
        sig = (_norm(it.name), round(it.energy_kcal or 0, 1))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(it)
    return out


def filter_usable(items, require_price=False):
    """剔除营养数据不完整的单品；需要价格时可进一步过滤。"""
    out = []
    for it in items:
        if it.energy_kcal is None:
            continue
        if require_price and it.price is None:
            continue
        out.append(it)
    return out


def role_label(role):
    return ROLE_ZH.get(role, role)
