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
    MAIN_MIN_KCAL, ROLE_ZH, base_score, infer_role, infer_textures,
    load_overrides,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class Item:
    """一个可选单品（含规格）。例如「薯条 中份」和「薯条 大份」是两个 Item。"""

    __slots__ = (
        "key", "name", "slug", "category", "role", "portion", "price", "code",
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
    """
    品名归一化，用于跨源匹配。

    去掉空格、括号内容，以及品牌前缀——MCP 菜单里叫「麦咖啡™奶铁」，
    官网营养表里叫「冰奶铁」，不剥掉前缀就对不上。
    """
    s = (s or "").strip()
    s = re.sub(r"[（(].*?[)）]", "", s)
    for prefix in ("麦咖啡™", "麦咖啡", "麦当劳"):
        s = s.replace(prefix, "")
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


def load_verified_prices(path=None):
    """
    加载官方结算价。结构：{"prices": {"商品编码": 元}, ...}

    由 scripts/verify_prices.py 用 calculate-price 逐个校准得到。
    菜单列表里的 price 字段可能是「起价」，实测有商品标 ¥9.9 而结算
    ¥23.0，差 2.3 倍。所以只要这份文件存在，它就是最高优先级的价格来源。
    """
    path = path or os.path.join(DATA_DIR, "prices_verified.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("prices") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def apply_verified_prices(items, verified):
    """
    用官方结算价覆盖。按商品编码精确匹配，不猜名字——
    校准本来就是为了消灭价格歧义，匹配环节不能再引入歧义。
    """
    if not verified:
        return 0
    hit = 0
    for it in items:
        p = verified.get(str(it.code)) if it.code else None
        if p is not None:
            it.price = float(p)
            it.source = "verified"
            hit += 1
    return hit


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
        base_role = infer_role(name, cat)
        for v in it.get("variants", []):
            portion = v.get("portion", "1份")
            kcal = v.get("energy_kcal")
            # 撑不起正餐的「主食」直接降级。官网把脆薯饼（146kcal）、
            # 脆香油条（202kcal）挂在早餐分类下，而早餐被当作主食——
            # 于是它们会和汉堡抢主食名额，还经常因为便宜而胜出。
            # 只在评分里扣分不够，求解器照样会拿它们凑数；
            # 所以在角色判定阶段就按热量门槛降为小食。
            role = base_role
            if role == "main" and kcal is not None and kcal < MAIN_MIN_KCAL:
                role = "side"
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
                base=base_score(role, textures, v.get("energy_kcal")),
                source="web",
            ))
    return out


# 组合餐标识：命中任一项的 MCP 条目不作为单品价格来源。
#
# 「双人餐」这类必须排除——实测「BFF四宫格小食盘双人餐」菜单标 ¥62.9，
# 结算价 ¥122.5，差了近一倍。它是两人份的组合，价格不能当单品用，
# 混进求解器会让预算彻底失真。
PACK_WORDS = (
    "套餐", "件套", "随心选", "随心拼", "乐园餐", "双全",
    "双人餐", "三人餐", "多人餐", "分享餐", "全家餐", "超值餐",
)


def _median_variant(group):
    """多规格产品里挑「标准份」：按热量排序取居中的那个。"""
    s = sorted(group, key=lambda i: i.energy_kcal or 0)
    return s[len(s) // 2]


def apply_mcp_menu(items, menu, prices=None):
    """
    用 MCP 的实时菜单校准价格。返回成功匹配的产品数。

    menu 为 scripts/fetch_menu.py 产出的结构：
        {"items": [{"name": ..., "price": 12.5, "code": ...}, ...]}

    两处容易踩坑，已分别处理：

    1. 名称对不齐。官网叫「可口可乐」，MCP 菜单叫「可乐」；
       官网叫「吉士汉堡包」，MCP 叫「吉士汉堡」。所以精确匹配之外
       再做双向子串匹配，取最长的那个候选。

    2. 规格被平铺。官网的「薯条」有迷你/小/中/大四个规格，而 MCP
       菜单里只有一条「薯条」¥13.5。若按品名无差别赋值，四个规格
       会拿到同一个价格，求解器就会以为大份和中份同价——这是错的。
       所以多规格产品只把价格赋给「标准份」（热量居中的规格），
       其余规格留空，宁可缺失也不误导。
    """
    if not menu:
        return 0

    # 值同时带价格与商品编码。productCode 是后续 calculate-price 复核
    # 与 create-order 下单的唯一凭证，必须和价格一起带过来。
    lookup = {}
    skipped_pack = 0
    for m in menu.get("items", []):
        n = _norm(m.get("name"))
        if not n or m.get("price") is None:
            continue
        # 组合餐的价格不能当单品价格用。MCP 菜单里「吉士汉堡包套餐」¥26
        # 若被子串匹配当成「汉堡包」的单价，求解结果会严重失真，
        # 所以先把套餐、件套、随心选这类组合餐剔掉。
        if any(w in n for w in PACK_WORDS):
            skipped_pack += 1
            continue
        if n not in lookup:
            lookup[n] = (float(m["price"]), m.get("code"))
    if not lookup:
        return 0
    names = list(lookup.keys())

    # 按官网品名分组，同名的不同规格放在一起处理
    groups = {}
    for it in items:
        groups.setdefault(_norm(it.name), []).append(it)

    hit = 0
    for gname, group in groups.items():
        ent = lookup.get(gname)
        if ent is None:
            best = None
            for n in names:
                if (n in gname or gname in n) and min(len(n), len(gname)) >= 2:
                    if best is None or len(n) > len(best):
                        best = n
            if best is not None:
                ent = lookup[best]
        if ent is None:
            continue
        price, code = ent

        # 与价格同理：多规格产品只认「标准份」这一条能对应上真实商品编码，
        # 其余规格的 code 留空，避免复核与下单时张冠李戴。
        targets = group if len(group) == 1 else [_median_variant(group)]
        for t in targets:
            t.price = price
            t.code = code
            t.source = "mcp"
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

    # MCP 价格是可选增强：抓过就有，没抓过就静默跳过（预算约束会自动降级）
    menu_path = menu_path or os.path.join(DATA_DIR, "menu.json")
    if os.path.exists(menu_path):
        with open(menu_path, encoding="utf-8") as f:
            apply_mcp_menu(items, json.load(f))

    # 官方结算价优先级最高：它才是真正会扣款的数字
    apply_verified_prices(items, load_verified_prices())

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


def filter_orderable(items):
    """
    能被官方复核与下单的单品：必须有 productCode。

    编码来自 MCP 菜单，没抓过菜单或名称没对上的单品没有编码。
    这类单品照样能参与求解（营养是有的），只是不能进订单。
    """
    return [i for i in items if i.code]


def role_label(role):
    return ROLE_ZH.get(role, role)
