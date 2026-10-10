# -*- coding: utf-8 -*-
"""
满足感模型：一顿麦当劳吃得「爽不爽」的量化。

为什么不满足于「最便宜」或「热量达标」
-------------------------------------
人点麦当劳时的真实心理是：这顿吃完我不能后悔。
后悔通常来自四种情况——
  · 花了钱但没吃到想吃的（口味单调）
  · 吃完了发现这顿咸得离谱（钠超标）
  · 结构不完整，吃了跟没吃一样（没主食 / 没喝的）
  · 明明有更划算的组合，自己没发现（性价比）

所以满足感 = 口味愉悦 + 结构完整 + 健康可控 + 性价比感知。
四项加权，权重可切换，于是有了三种「人设」方案。

评分拆解
--------
  S_base       单品固有满足度之和（来自 taste.py 的口感先验）
  S_texture    口感层次奖励：覆盖的口感维度越多越爽，但边际递减
  S_temp       温度反差奖励：热汉堡配冰可乐 > 全是热食
  S_structure  结构完整奖励：主食 / 小食 / 饮料 / 甜品的齐备度
  P_repeat     同类堆叠惩罚：三个汉堡不叫一顿饭
  P_health     健康软惩罚：钠超标、脂肪过高
  S_value      性价比愉悦：省得越多越开心（需价格数据）
"""

# 每顿各角色最多允许的数量。超过就是「怪异组合」
ROLE_LIMIT = {"main": 2, "side": 2, "drink": 1, "dessert": 1}

# 中国居民膳食指南推荐每日钠摄入不超过 2000mg
# （麦当劳一份主食就能吃掉一半，这是本项目强调控钠的原因）
SODIUM_DAILY_LIMIT = 2000.0

# 口感维度覆盖奖励：前 3 个维度收益最高，之后边际递减
TEXTURE_STEP = [0.0, 1.6, 1.2, 0.9, 0.5, 0.3]

# 结构完整度权重
STRUCTURE_BONUS = {"main": 2.0, "side": 1.0, "drink": 1.5, "dessert": 1.0}
NO_MAIN_PENALTY = 5.0      # 没有主食，不成一顿饭
DRINK_ONLY_PENALTY = 8.0   # 只点了饮料


def score(items, weights=None):
    """
    给一个组合打分，返回 (总分, 明细字典)。

    weights 可覆盖各项权重：
        base / texture / temp / structure / health / value
    """
    w = {
        "base": 1.0, "texture": 1.0, "temp": 1.0,
        "structure": 1.0, "health": 1.0, "value": 1.0,
    }
    if weights:
        w.update(weights)

    detail = {}

    if not items:
        return 0.0, {"total": 0.0, "note": "空组合"}

    # 1. 固有满足度
    s_base = sum(i.base for i in items)
    detail["base"] = round(s_base, 2)

    # 2. 口感层次：覆盖维度数 -> 递减奖励
    dims = set()
    for i in items:
        dims |= (i.textures or set())
    n_dim = len(dims)
    s_texture = 0.0
    for k in range(min(n_dim, len(TEXTURE_STEP) - 1)):
        s_texture += TEXTURE_STEP[k + 1]
    detail["texture"] = round(s_texture, 2)
    detail["texture_dims"] = sorted(dims)

    # 3. 温度反差
    has_cold = any("cold" in (i.textures or set()) for i in items)
    has_hot = any("hot" in (i.textures or set()) for i in items)
    if has_cold and has_hot:
        s_temp = 1.5
    elif has_cold or has_hot:
        s_temp = 0.4
    else:
        s_temp = -0.3   # 全常温，平淡
    detail["temp"] = round(s_temp, 2)

    # 4. 结构完整度
    counts = {}
    for i in items:
        counts[i.role] = counts.get(i.role, 0) + 1
    s_struct = 0.0
    for role, n in counts.items():
        if n > 0:
            s_struct += STRUCTURE_BONUS.get(role, 0.0)
    if counts.get("main", 0) == 0:
        s_struct -= NO_MAIN_PENALTY
    if len(items) == counts.get("drink", 0):
        s_struct -= DRINK_ONLY_PENALTY
    detail["structure"] = round(s_struct, 2)

    # 5. 同类堆叠惩罚
    p_repeat = 0.0
    for role, n in counts.items():
        over = n - ROLE_LIMIT.get(role, 2)
        if over > 0:
            p_repeat += 3.0 * over
    detail["repeat"] = round(-p_repeat, 2)

    # 6. 健康软惩罚：钠为主，脂肪为辅
    sodium = sum(i.sodium or 0 for i in items)
    fat = sum(i.fat or 0 for i in items)
    p_health = 0.0
    if sodium > SODIUM_DAILY_LIMIT:
        p_health += (sodium - SODIUM_DAILY_LIMIT) / 200.0
    if fat > 60:
        p_health += (fat - 60) / 15.0
    detail["health"] = round(-p_health, 2)

    # 7. 性价比愉悦（无价格数据时跳过）
    s_value = 0.0
    prices = [i.price for i in items if i.price is not None]
    if len(prices) == len(items) and items:
        # 用单品满足度之和近似「感知价值」，和实际花费比较
        perceived = sum(i.base for i in items) * 1.2
        spend = sum(prices)
        s_value = max(-2.0, min((perceived - spend) / 3.0, 3.0))
    detail["value"] = round(s_value, 2)

    total = (
        w["base"] * s_base
        + w["texture"] * s_texture
        + w["temp"] * s_temp
        + w["structure"] * s_struct
        + w["health"] * (-p_health)
        + w["value"] * s_value
        + w["structure"] * (-p_repeat)
    )
    detail["total"] = round(total, 2)
    return total, detail


def totals(items):
    """汇总营养与花费。"""
    def s(attr):
        return round(sum(getattr(i, attr) or 0 for i in items), 1)
    prices = [i.price for i in items if i.price is not None]
    return {
        "price": round(sum(prices), 1) if len(prices) == len(items) else None,
        "kcal": s("energy_kcal"),
        "protein": s("protein"),
        "fat": s("fat"),
        "carbohydrate": s("carbohydrate"),
        "sodium": s("sodium"),
        "calcium": s("calcium"),
        "count": len(items),
    }


# 三种「人设」：同一套模型，不同权重
PERSONAS = {
    "value": {
        "label": "省钱派",
        "desc": "把钱花在刀刃上，性价比优先",
        "weights": {"base": 1.0, "texture": 0.8, "temp": 0.6,
                    "structure": 0.8, "health": 1.0, "value": 2.5},
        "objective": {"price": -0.55},
    },
    "indulge": {
        "label": "嘴馋派",
        "desc": "预算之内，把这顿吃爽",
        "weights": {"base": 1.4, "texture": 1.8, "temp": 1.5,
                    "structure": 1.2, "health": 0.5, "value": 0.3},
        "objective": {"price": -0.05},
    },
    "balance": {
        "label": "均衡派",
        "desc": "控钠控油，吃得安心",
        "weights": {"base": 1.0, "texture": 1.0, "temp": 1.0,
                    "structure": 1.2, "health": 2.6, "value": 0.8},
        "objective": {"price": -0.15, "sodium": -0.006},
    },
}


def resolve_persona(persona, has_price=True):
    """
    取人设配置，并按数据可用性做适配。

    没有价格数据时，「省钱派」会退化成和嘴馋派一样的解——
    因为它的核心区分项（价格）恒为 0。这时改用热量做花费的代理：
    麦当劳的单价与热量高度正相关，热量低的一餐通常也便宜。
    人设名同步改为「轻量派」，避免误导。
    """
    p = dict(PERSONAS[persona])
    p["weights"] = dict(p["weights"])
    p["objective"] = dict(p.get("objective", {}))
    if persona == "value" and not has_price:
        p["label"] = "轻量派"
        p["desc"] = "无价格数据，以热量代理花费：吃得精，不堆量"
        p["objective"]["kcal"] = -0.02
    return p


def objective(items, persona="indulge", has_price=True):
    """综合目标函数 = 满足感 + 额外的经济性/健康性线性项。"""
    p = resolve_persona(persona, has_price)
    s, detail = score(items, p["weights"])
    t = totals(items)
    extra = 0.0
    for key, coef in p.get("objective", {}).items():
        v = t.get(key)
        if v is not None:
            extra += coef * v
    return s + extra, detail, t
