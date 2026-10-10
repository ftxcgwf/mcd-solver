# -*- coding: utf-8 -*-
"""
求解器：在预算、热量、钠、蛋白质的约束下，找出满足感最高的组合。

算法选择
--------
菜单展开后约 125 个可选项，穷举 3~5 件组合有上亿种，直接枚举不现实。
但一顿麦当劳有天然的层次结构：主食 / 小食 / 饮料 / 甜品，
且每层数量上限很小（主食最多 2 个、饮料最多 1 个……）。

于是用「分层束搜索」：
  逐层扩展，每层只保留 beam_width 个最有希望的部分解。
  因为热量、钠、花费都是单调累加的，超预算的分支可以当场剪掉，
  不必等组合完整才发现。

复杂度从 O(C(n,k)) 降到 O(层数 × beam × 每层组合数)，实测秒级出解。

这不是精确最优解，是高质量的近似解——对「今天中午吃什么」这个问题，
算到精确最优没有意义，快和多方案才重要。
"""

from itertools import combinations

from .satisfaction import PERSONAS, ROLE_LIMIT, objective, resolve_persona, totals

# 各层参与枚举的候选上限：按「满足感 / 热量」性价比预筛，保住多样性
POOL_TOP = {"main": 34, "side": 26, "drink": 22, "dessert": 16}

LAYER_ORDER = ["main", "side", "drink", "dessert"]


class Constraints:
    """一顿饭的硬约束。None 表示不限制。"""

    def __init__(self, budget=None, max_kcal=None, max_sodium=None,
                 min_protein=None, max_items=5, require_roles=None):
        self.budget = budget
        self.max_kcal = max_kcal
        self.max_sodium = max_sodium
        self.min_protein = min_protein
        self.max_items = max_items
        self.require_roles = require_roles or []

    def partial_ok(self, kc, na, spend):
        """部分解的单调约束检查（累加量只增不减，可提前剪枝）。"""
        if self.max_kcal is not None and kc > self.max_kcal:
            return False
        if self.max_sodium is not None and na > self.max_sodium:
            return False
        if self.budget is not None and spend is not None and spend > self.budget:
            return False
        return True

    def full_ok(self, t, items):
        if self.min_protein is not None and (t["protein"] or 0) < self.min_protein:
            return False
        if t["count"] > self.max_items:
            return False
        roles = {i.role for i in items}
        for r in self.require_roles:
            if r not in roles:
                return False
        return True

    def __repr__(self):
        return (f"Constraints(budget={self.budget}, max_kcal={self.max_kcal}, "
                f"max_sodium={self.max_sodium}, min_protein={self.min_protein})")


def _pool_for(items, role):
    """取出某角色的候选，按「满足感性价比」排序后截断。"""
    pool = [i for i in items if i.role == role]
    if not pool:
        return []

    def efficiency(i):
        # 没有价格时用热量做分母，避免高热量低愉悦的单品挤占候选位
        denom = i.price if i.price is not None else max((i.energy_kcal or 1) / 100.0, 1.0)
        return i.base / max(denom, 1.0)

    pool.sort(key=efficiency, reverse=True)
    return pool[: POOL_TOP.get(role, 20)]


def _subsets(pool, role, max_items_left):
    """生成某层可加入的所有子集（含空集），受角色上限与总件数限制。"""
    limit = min(ROLE_LIMIT.get(role, 1), max_items_left)
    yield ()
    for k in range(1, limit + 1):
            for combo in combinations(pool, k):
                # 同一产品的不同规格互斥：薯条中份和大份不该同时出现。
                # 用 slug 而非品名判断，因为官网存在「麦乐鸡（4块）」「麦乐鸡 (5块)」
                # 这类全半角括号不一致的同源条目。
                slugs = [c.slug or c.name for c in combo]
                if len(set(slugs)) != len(slugs):
                    continue
                yield combo


def solve(items, constraints=None, persona="indulge", beam_width=420,
          top_k=3, has_price=None):
    """
    求解。返回按目标函数降序的方案列表，每项含 items / score / detail / totals。

    has_price 为 None 时自动判断：只要有一个单品带价格，就认为价格可用。
    """
    constraints = constraints or Constraints()
    if persona not in PERSONAS:
        raise ValueError(f"未知人设 {persona}，可选：{list(PERSONAS)}")
    if has_price is None:
        has_price = any(i.price is not None for i in items)

    pools = {r: _pool_for(items, r) for r in LAYER_ORDER}
    if not any(pools.values()):
        return []

    # 部分解存为 (items, kcal, sodium, spend, base_sum)
    beam = [([], 0.0, 0.0, 0.0, 0.0)]

    for role in LAYER_ORDER:
        pool = pools[role]
        if not pool:
            continue
        nxt = []
        for p_items, kc, na, spend, bsum in beam:
            left = constraints.max_items - len(p_items)
            if left <= 0:
                nxt.append((p_items, kc, na, spend, bsum))
                continue
            for combo in _subsets(pool, role, left):
                if not combo:
                    nxt.append((p_items, kc, na, spend, bsum))
                    continue
                d_kc = sum(c.energy_kcal or 0 for c in combo)
                d_na = sum(c.sodium or 0 for c in combo)
                d_base = sum(c.base for c in combo)
                d_price = sum(c.price for c in combo) if all(
                    c.price is not None for c in combo) else None
                n_kc, n_na = kc + d_kc, na + d_na
                # 只要组合里有一个单品没有价格，总花费就不可知（None），
                # 这时预算约束无法判定，只能放行——不能拿「部分价格」当总价
                n_spend = (spend + d_price) if (d_price is not None and spend is not None) else None
                if not constraints.partial_ok(n_kc, n_na, n_spend):
                    continue
                nxt.append((p_items + list(combo), n_kc, n_na, n_spend, bsum + d_base))

        # 束剪枝：优先保留固有满足度高的，同分时保留热量占用小的（给后面留余地）
        nxt.sort(key=lambda x: (-x[4], x[1]))
        beam = nxt[:beam_width]

    # 完整解评估
    results = []
    seen = set()
    for p_items, kc, na, spend, _ in beam:
        if not p_items:
            continue
        t = totals(p_items)
        if not constraints.full_ok(t, p_items):
            continue
        sig = tuple(sorted(i.key for i in p_items))
        if sig in seen:
            continue
        seen.add(sig)
        obj, detail, tot = objective(p_items, persona, has_price)
        p = resolve_persona(persona, has_price)
        results.append({
            "items": p_items, "score": round(obj, 2),
            "detail": detail, "totals": tot,
            "persona_label": p["label"], "persona_desc": p["desc"],
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return _diversify(results, top_k)


def _diversify(results, top_k):
    """
    避免三个方案长得几乎一样。

    光看整体重合率不够——「换个规格的薯条」也算新方案，但对人没意义。
    所以额外要求：主食不能和已选方案完全相同，否则等于同一个思路。
    """
    picked = []
    for r in results:
        if len(picked) >= top_k:
            break
        keys = {i.key for i in r["items"]}
        mains = {i.key for i in r["items"] if i.role == "main"}
        too_similar = False
        for p in picked:
            pkeys = {i.key for i in p["items"]}
            pmains = {i.key for i in p["items"] if i.role == "main"}
            inter = len(keys & pkeys)
            union = len(keys | pkeys) or 1
            if inter / union > 0.6:
                too_similar = True
                break
            # 主食完全相同（或都无主食）视为同一思路
            if mains and mains == pmains:
                too_similar = True
                break
        if not too_similar:
            picked.append(r)
    # 差异阈值太严导致不足时，用高分补足
    for r in results:
        if len(picked) >= top_k:
            break
        if r not in picked:
            picked.append(r)
    return picked[:top_k]
