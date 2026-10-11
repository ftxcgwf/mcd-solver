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
        "textures", "base", "source", "coupon_title",
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


def load_official_names(path=None):
    """
    加载结算端返回的官方明细名：{"商品编码": "小杯玉米杯"}。

    与 `names`（菜单里的泛称）不同，这份来自 `calculate-price` 的实际返回，
    常常自带规格，是把价格挂到正确档位的依据。文件缺失时返回空字典，
    退化到热量居中的启发式，不影响离线可用性。
    """
    path = path or os.path.join(DATA_DIR, "prices_verified.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("official_names") or {})
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


def load_coupon_prices(path=None):
    """
    加载券后价。结构：{"prices": {"商品编码": {"price": 元, "title": 券名, "name": 品名}}}

    由 scripts/coupons.py --calibrate 用 calculate-price 折算得到。
    券接口只给 couponId 不给面额，所以「这张券值多少钱」只能靠官方试算反推。
    """
    path = path or os.path.join(DATA_DIR, "prices_coupon.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("prices") or {})
    except (OSError, json.JSONDecodeError):
        return {}


def apply_coupon_prices(items, coupon_prices):
    """
    用券后价覆盖。按商品编码精确匹配。

    放在官方结算价之后生效——券后价才是这一单真正要付的数。
    """
    if not coupon_prices:
        return 0
    hit = 0
    for it in items:
        if not it.code:
            continue
        entry = coupon_prices.get(str(it.code))
        if not entry:
            continue
        p = entry.get("price") if isinstance(entry, dict) else entry
        if p is None:
            continue
        it.price = float(p)
        it.source = "coupon"
        it.coupon_title = (entry.get("title") if isinstance(entry, dict) else "") or ""
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
    "套餐", "件套", "随心选", "随心拼", "随心配", "乐园餐", "乐园", "双全",
    "双人餐", "三人餐", "多人餐", "分享餐", "全家餐", "超值餐",
    "拼盘", "小食盘", "双拼", "精选", "十翅", "桶",
)

# 组合餐分类标识。MCP 的 category 字段比品名更可靠——品名里不带「套餐」
# 二字不代表它不是组合：「堡堡随心拼（7件任选）」看着像活动名，实际是七件
# 组合。按分类整类拦掉，比逐个猜名字防得住漏网。
PACK_CATEGORIES = (
    "套餐", "拼盘", "多人餐", "单人餐", "随心配", "随心选", "乐园", "组合",
)


def _clean_cat(s):
    """抹掉分类字段里的换行。MCP 返回的分类自带换行符，如「巨无霸\n牛鱼肉堡」。"""
    return re.sub(r"\s+", "", s or "")


def is_pack(name, category=None):
    """
    是否为不可当单品使用的组合餐：品名或分类命中任意一项即算。

    求解器与价格校准脚本共用这一个判断——两边若各自按自己的口径过滤，
    就会出现「校准过的商品求解器里被排除」或反之，价格层与实际可选项对不上。
    """
    if any(w in _norm(name) for w in PACK_WORDS):
        return True
    return any(w in _clean_cat(category) for w in PACK_CATEGORIES)


def _pick_variant(group, official_name=None):
    """
    多规格产品里挑该承接商品编码的那一档。

    菜单（MCP）通常只有一条泛称记录，而官网营养数据按规格分档，
    所以必须决定这个编码对应哪一档。两级判断：

    1. 结算端 `calculate-price` 返回的明细名往往带规格（「小杯玉米杯」），
       这是最接近真实下单口径的信息，优先拿它的文字去匹配规格档位。
    2. 没有明细名可读时，退回「热量居中」这条保守启发式——宁可偏中间档，
       也不要默认最小或最大，那样误差更大。

    实测若不用明细名，玉米杯会把菜单那条价格挂到「大」（87kcal），
    而官方结算明细写的是「小杯玉米杯」（53kcal），热量会多算六成。
    """
    if official_name:
        short = str(official_name)
        for it in group:
            p = (it.portion or "").strip()
            # 只在规格词够长时才认，避免「中」被任意一个长名字误命中
            if len(p) >= 1 and p in short:
                return it
    s = sorted(group, key=lambda i: i.energy_kcal or 0)
    return s[len(s) // 2]


def _official_fit(gname, official_name):
    """
    候选产品与官方明细名的契合度；返回 None 表示没有明细名、无从判断。

    MCP 的一个编码常常代表一类商品，官网却按冷热、口味拆成多个产品，
    所以不能只看「谁的品名更像菜单名」来决定由谁承接这个编码——
    那本质是在猜。而结算端 calculate-price 返回的明细名是权威口径：

        菜单写「牛奶」，官方明细写「热牛奶中杯」
        菜单写「燕麦奶」，官方明细写「热燕麦奶中杯」

    光凭品名相似度，「热牛奶」和「冰浓浓抹茶牛奶」一样都含「牛奶」，
    谁排前面谁就拿到编码；而明细名明确说这个编码对应的是热的那款。
    忽略这层证据，就会把热饮的编码挂到冰饮头上（热量、钠、口感全反）。

    契合计分：核心名能对上给 +10，冷热标记一致再 +3，冷热冲突 -6。
    """
    if not official_name:
        return None
    o = _norm(official_name)
    g = _norm(gname)
    if not o or not g:
        return None
    # 剥离冷热前缀再比核心名：「冰燕麦奶」与「燕麦奶中杯」应判为同一物
    core_g = g[1:] if g[:1] in ("冰", "热") else g
    core_o = o[1:] if o[:1] in ("冰", "热") else o
    if not core_g or not core_o:
        return None
    score = 10 if (core_g in core_o or core_o in core_g) else 0
    temp_g = g[:1] if g[:1] in ("冰", "热") else None
    temp_o = next((ch for ch in o if ch in ("冰", "热")), None)
    if temp_g and temp_o:
        score += 3 if temp_g == temp_o else -6
    return score


def _assign_owners(resolved, official_names):
    """
    给每个商品编码挑唯一承接者。双向唯一：一个编码只归一个产品，
    一个产品也只占一个编码。

    按契合度全局排序后贪心分配：有官方明细名佐证的配对先落定，
    剩下没有佐证的再按「精确同名 > 菜单名更长 > 原始顺序」兜底。
    """
    pairs = []
    for r in resolved:
        order, gname, _group, mname, _price, code = r
        if not code:
            continue
        fit = _official_fit(gname, (official_names or {}).get(str(code)))
        exact = 0 if (mname is not None and mname == gname) else 1
        key = (fit or 0, -exact, len(mname or ""), -order)
        pairs.append((key, r))
    pairs.sort(key=lambda p: p[0], reverse=True)

    used_codes, used_groups, out = set(), set(), []
    for _key, r in pairs:
        ckey, gkey = str(r[5]), r[1]
        if ckey in used_codes or gkey in used_groups:
            continue
        used_codes.add(ckey)
        used_groups.add(gkey)
        out.append(r)
    return out


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
        if is_pack(n, m.get("category")):
            skipped_pack += 1
            continue
        if n not in lookup:
            lookup[n] = (float(m["price"]), m.get("code"))
    if not lookup:
        return 0
    names = list(lookup.keys())
    # 结算端的权威明细名，用来把价格挂到正确的规格档位
    official_names = load_official_names()

    # 按官网品名分组，同名的不同规格放在一起处理
    groups = {}
    for it in items:
        groups.setdefault(_norm(it.name), []).append(it)

    # 先为每个官网品名组解析出候选菜单条目，不急着写入
    resolved = []
    for order, (gname, group) in enumerate(groups.items()):
        ent = lookup.get(gname)
        mname = gname if ent is not None else None
        if ent is None:
            best = None
            for n in names:
                if (n in gname or gname in n) and min(len(n), len(gname)) >= 2:
                    if best is None or len(n) > len(best):
                        best = n
            if best is not None:
                ent = lookup[best]
                mname = best
        if ent is None:
            continue
        price, code = ent
        resolved.append((order, gname, group, mname, price, code))

    # 全局唯一分配。编码被多个产品争抢时必须仲裁，且仲裁要讲证据。
    #
    # MCP 菜单的粒度比官网营养数据粗：MCP 只有一条「美式」，官网却拆成了
    # 「冰美式」「热美式」；MCP 只有「新地」，官网是「朱古力新地」「草莓新地」。
    # 放任多个官网产品抢同一个 code 会出两件事：
    #   1. 求解器输出两个共享同一商品编码的选项，下单时无法区分到底要哪个
    #   2. 券被赋给不适用的变体——实测「9.9元中杯冰美式」被赋给了「热美式」
    # 仲裁的优先级：先看结算端明细名的证据，没有证据才比品名相似度。
    hit = 0
    for r in _assign_owners(resolved, official_names):
        _order, _gname, group, _mname, price, code = r

        # 与价格同理：多规格产品只认能对应上真实商品编码的那一档，
        # 其余规格留空，避免复核与下单时张冠李戴。
        # 规格到底是哪一档，优先听结算端明细名（如「小杯玉米杯」），
        # 没有明细名时才退回「热量居中」这条启发式。
        if len(group) == 1:
            targets = group
        else:
            targets = [_pick_variant(group, (official_names or {}).get(str(code)))]
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

    # 券后价再盖一层：有券的话这一单实际就付这么多
    apply_coupon_prices(items, load_coupon_prices())

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
