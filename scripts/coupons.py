# -*- coding: utf-8 -*-
"""
优惠券：从「我有什么券」到「这张券值多少钱」。

麦当劳 MCP 在券这件事上给了四个工具，但它们各说各话：
    available-coupons   能领什么（只有标题和图片，没有面额、没有 ID）
    auto-bind-coupons   一键把能领的都领了
    query-my-coupons    我卡包里有什么（不做门店校验，不承诺能用）
    query-store-coupons 这家店这张单能用哪些（带 couponId 和适用商品编码）
    query-promotions    门店满减/满折规则（团餐场景）

这里有个反直觉的点值得单独说：**所有券接口都不返回面额**。
券上写「¥5 优惠」是印在图片上的，接口只给一个 couponId。这意味着
本地根本没法估算「用这张券省多少」——唯一的办法是带着 couponId
去问 calculate-price，让它算一遍。

所以券优化不是本地预算题，而是一次 MCP 试算循环。这恰好是本项目
「真实使用 MCP 能力」最硬的证据之一：不用 MCP，这件事做不了。

无券是常态（新账号卡包为空），此时本模块会明确提示，绝不编造。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_menu import (  # noqa: E402
    McpClient, McpError, extract_text, parse_json_relaxed,
)
from order import BE_TYPE, ORDER_TYPE, OrderClient, yuan  # noqa: E402

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class CouponClient(OrderClient):
    """券相关的五个调用。"""

    def available(self):
        """
        可领取的券。接口只返回一段 markdown 文本（标题 + 状态 + 图片），
        没有结构化 ID，所以这里做正则提取，拿不到 ID 就只留标题。
        """
        text = extract_text(self.call_tool("available-coupons", {}))
        coupons, cur = [], {}
        for line in (text or "").splitlines():
            line = line.strip().rstrip("\\").strip()
            m = re.match(r"^[-*]\s*优惠券标题[：:]\s*(.+)$", line)
            if m:
                if cur.get("title"):
                    coupons.append(cur)
                cur = {"title": m.group(1).strip(), "status": "", "image": ""}
                continue
            m = re.match(r"^状态[：:]\s*(.+)$", line)
            if m and cur:
                cur["status"] = m.group(1).strip()
                continue
            m = re.search(r'<img src="([^"]+)"', line)
            if m and cur:
                cur["image"] = m.group(1).strip()
        if cur.get("title"):
            coupons.append(cur)
        return coupons

    def bind(self):
        """
        一键领取所有可领券。这是账号写操作，故只在显式调用时执行。

        该接口成功与否都返回一段 markdown 文本而非 JSON，形如
        「### 领券成功」或「### 领券失败\\n\\n错误信息：暂无可领取的优惠券」。
        所以不能只按 JSON 解析，得把标题和错误信息一起取出来，
        否则失败时只能看到一片 None。
        """
        text = extract_text(self.call_tool("auto-bind-coupons", {})) or ""
        data = parse_json_relaxed(text)
        if isinstance(data, dict):
            data.setdefault("raw", text)
            return data

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        ok = any(ln.startswith("###") and "成功" in ln for ln in lines)
        err = ""
        for ln in lines:
            m = re.match(r"^错误信息[：:]\s*(.+)$", ln)
            if m:
                err = m.group(1).strip()
                break
        return {"success": ok, "message": err or ("领券完成" if ok else "无返回结果"),
                "raw": text}

    def mine(self, page=1, page_size=200):
        """
        卡包里的券。接口不带门店校验，只在「我有什么」的场景用；
        判断这张单能不能用必须走 store()。
        """
        data = parse_json_relaxed(extract_text(self.call_tool(
            "query-my-coupons",
            {"page": str(page), "pageSize": str(page_size)},
        ))) or {}
        return _collect_coupons(data)

    def store(self, store_code):
        """当前门店 + 订单类型下真正可用的券，带 couponId/couponCode。"""
        data = parse_json_relaxed(extract_text(self.call_tool(
            "query-store-coupons",
            {"storeCode": str(store_code), "orderType": ORDER_TYPE,
             "beType": BE_TYPE},
        ))) or {}
        return _collect_coupons(data)

    def promotions(self, store_code):
        """门店满减/满折促销规则（企业团餐场景）。"""
        try:
            data = parse_json_relaxed(extract_text(self.call_tool(
                "query-promotions",
                {"storeCode": str(store_code), "orderType": ORDER_TYPE,
                 "beType": BE_TYPE},
            ))) or {}
        except McpError:
            return []
        raw = data.get("data")
        return raw if isinstance(raw, list) else []


def _collect_coupons(data):
    """
    券列表在不同接口里嵌套不统一：有的在 data[]，有的在 data.list，
    还有的直接返回空数组。统一摊平成 [{couponId, couponCode, title,
    tradeDateTime, productCodes}]。
    """
    raw = data.get("data")
    if isinstance(raw, dict):
        for key in ("coupons", "list", "items", "records"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raw = []
    if not isinstance(raw, list):
        return []

    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        codes = [str(p.get("productCode")) for p in (c.get("products") or [])
                 if isinstance(p, dict) and p.get("productCode")]
        out.append({
            "couponId": str(c.get("couponId") or ""),
            "couponCode": str(c.get("couponCode") or ""),
            "title": c.get("title") or c.get("name") or "(未命名券)",
            "tradeDateTime": c.get("tradeDateTime") or "",
            "productCodes": codes,
            "productNames": [p.get("productName") for p in (c.get("products") or [])
                             if isinstance(p, dict)],
        })
    return out


def match_coupons(lines, coupons):
    """
    把券和商品行对上。券接口不给金额，但会给适用商品编码，
    所以判断"这张券能不能用在这单"只能靠编码比对。

    返回 [(coupon, [行下标...])]，未匹配的券不返回。
    一张券可能覆盖多个商品行（比如通用券），这里全部返回，
    由调用方决定挂在哪个商品上试算。
    """
    line_codes = {str(l["productCode"]): idx for idx, l in enumerate(lines)}
    out = []
    for c in coupons:
        codes = c.get("productCodes") or []
        if not codes:
            # 没有标注适用范围的券视为通用券，挂在第一件上试算
            out.append((c, [0] if lines else []))
            continue
        hits = [line_codes[x] for x in codes if x in line_codes]
        if hits:
            out.append((c, hits))
    return out


def with_coupon(lines, idx, coupon):
    """把券挂在指定的那一行上，其余保持原样。"""
    out = []
    for n, l in enumerate(lines):
        row = {"productCode": l["productCode"], "quantity": l["quantity"]}
        if n == idx:
            if coupon.get("couponId"):
                row["couponId"] = coupon["couponId"]
            if coupon.get("couponCode"):
                row["couponCode"] = coupon["couponCode"]
        out.append(row)
    return out


def quote_resilient(client, store_code, lines, max_probe=6):
    """
    复核，并自动剔除售罄品。

    整批里只要有一件售罄，官方就整单报 60006，连累其他商品算不出价。
    方案通常只有三五件，所以失败时逐件探一次、把售罄的剔掉再算整批，
    比二分更快也更容易看懂。返回 (data, used_lines, sold_out_codes)。
    """
    if not lines:
        return None, [], []
    try:
        return client.quote(store_code, lines), list(lines), []
    except McpError as e:
        msg = str(e)
        if not ("60006" in msg or "售罄" in msg):
            raise

    # 整批失败：逐件探测哪些还能卖
    usable, sold_out = [], []
    for l in lines[:max_probe]:
        try:
            client.quote(store_code, [l])
            usable.append(l)
        except McpError:
            sold_out.append(str(l.get("productCode")))
    if not usable:
        return None, [], sold_out
    try:
        return client.quote(store_code, usable), usable, sold_out
    except McpError:
        return None, usable, sold_out


def try_coupons(client, store_code, lines, coupons):
    """
    逐张券试算，返回 [(券, 官方应付, 官方折扣额)]，按应付升序。

    为什么必须一张张问 MCP：券接口不返回面额，只给 couponId。
    所以「这张券值多少钱」在本地无解——只能带着 couponId 去
    calculate-price 算一遍，比一比才知道。这里是全项目里 MCP
    不可替代性最强的一环。

    券一般一单只能用一张，所以每张券单独试算，不做叠加。
    试算失败的券（已过期、不适用该门店等）静默跳过。
    """
    if not lines:
        return []
    results = []
    for c, hits in match_coupons(lines, coupons):
        if not hits:
            continue
        try:
            data = client.quote(store_code, with_coupon(lines, hits[0], c))
        except McpError:
            continue
        total = yuan(data.get("price"))
        if total is not None:
            results.append((c, total, yuan(data.get("discount"))))
    results.sort(key=lambda x: x[1])
    return results


def suggest_addons(client, store_code, lines, coupons, budget=None, max_add=1):
    """
    回答一个比「现有方案能不能用券」更有用的问题：
    **为了这张券，值不值得往方案里加一件东西？**

    现有方案里往往恰好没有券适用的商品，于是券就废了。但真实决策是：
    「我本来要花 36，再加 9.9 换一杯冰美式（原价 11.9，省 2 块），
    到底划不划算」——这必须带着券去问官方才知道。

    返回 [{券, 商品名, 商品编码, 加购后总价, 增量花费, 比原价省}],
    按增量花费升序。超出预算的会被剔除。
    """
    _base, used, _sold = quote_resilient(client, store_code, lines)
    if not used:
        return []
    base = yuan(_base.get("price")) if _base else None
    if base is None:
        return []

    have = {str(l["productCode"]) for l in used}
    out = []
    for c in coupons:
        codes = [x for x in (c.get("productCodes") or []) if x not in have]
        for code in codes[:max_add]:
            trial = list(used) + [{
                "productCode": code, "quantity": 1,
                **({"couponId": c["couponId"]} if c.get("couponId") else {}),
                **({"couponCode": c["couponCode"]} if c.get("couponCode") else {}),
            }]
            try:
                d = client.quote(store_code, trial)
            except McpError:
                continue
            total = yuan(d.get("price"))
            if total is None:
                continue
            if budget is not None and total > budget + 0.005:
                continue
            # 这件商品不带券要多少钱：总价减去原方案，就是它用券后的净增量
            delta = round(total - base, 2)
            out.append({
                "coupon": c, "code": code,
                "total": total, "delta": delta,
                "name": (c.get("productNames") or [""])[0] if c.get("productNames") else "",
            })
    out.sort(key=lambda x: x["delta"])
    return out


def render_addons(base_total, adds, budget=None):
    """渲染「加一件用券，实际多花多少」。"""
    out = ["  加购建议（带券试算）"]
    if not adds:
        out.append("    当前预算内没有值得加购的券商品。")
        return "\n".join(out)
    for a in adds[:3]:
        c = a["coupon"]
        line = (f"    + {a['name'] or a['code']:<18s} 用「{c['title']}」"
                f"　只需 +¥{a['delta']:.2f}　（合计 ¥{a['total']:.2f}）")
        out.append(line)
    return "\n".join(out)


def render_coupon_choice(base_total, results, store_name=""):
    """渲染「用哪张券最划算」的对比。"""
    out = []
    head = "  券优化（calculate-price 逐张试算）"
    if store_name:
        head += f" · {store_name}"
    out.append(head)
    if base_total is None:
        out.append("    未能取得基准价，无法比较。")
        return "\n".join(out)

    out.append(f"    不用券      ¥{base_total}")
    if not results:
        out.append("    当前没有可用于本单的券。")
        return "\n".join(out)

    best = results[0]
    for c, total, _disc in results[:4]:
        save = round(base_total - total, 2)
        if save > 0.005:
            out.append(f"    {c['title']:<22s} ¥{total:<7.2f} 省 ¥{save:.2f}")
        elif abs(save) < 0.005:
            out.append(f"    {c['title']:<22s} ¥{total:<7.2f} 无优惠")
        else:
            out.append(f"    {c['title']:<22s} ¥{total:<7.2f} 反而贵 ¥{-save:.2f}")

    best_save = round(base_total - best[1], 2)
    if best_save > 0.005:
        out.append(f"    → 推荐用「{best[0]['title']}」，"
                   f"应付 ¥{best[1]:.2f}（比不用券省 ¥{best_save:.2f}）")
    else:
        out.append("    → 这些券对本单都没有实际优惠。")
    return "\n".join(out)


def render_coupons(coupons, title="优惠券"):
    """渲染券列表。无券时明确说明，不编造。"""
    out = [f"  {title}（{len(coupons)} 张）"]
    if not coupons:
        out.append("    暂无。可先运行 --bind 领取麦麦省当前可领的券。")
        return "\n".join(out)
    for c in coupons:
        tags = ""
        if c.get("productNames"):
            tags = "　适用：" + "、".join(c["productNames"][:3])
        out.append(f"    · {c['title']}{tags}")
        if c.get("tradeDateTime"):
            out.append(f"      有效期 {c['tradeDateTime']}")
    return "\n".join(out)


def calibrate(client, store_code, coupons, menu_path=None):
    """
    把券折算成「券后价」，供求解器直接用。

    这是券优化的真正形态。若只在求解完再挑券，那张券几乎不可能正好
    命中方案里的商品；正确的顺序是先用券后价求解，让「冰美式有券只要
    9.9」这件事一开始就影响组合选择，再回头复核确认。

    券接口不给面额，所以只能带着 couponId 去问 calculate-price，
    用官方返回的 subtotal 反推券后单价。

    返回 {productCode: {"price": 元, "title": 券名, "name": 商品名}}。
    """
    menu_path = menu_path or os.path.join(DATA_DIR, "menu.json")
    names = {}
    if os.path.exists(menu_path):
        with open(menu_path, encoding="utf-8") as f:
            for i in (json.load(f).get("items") or []):
                if i.get("code"):
                    names[str(i["code"])] = i.get("name") or ""

    out = {}
    for c in coupons:
        for code in (c.get("productCodes") or []):
            if code in out:
                continue
            try:
                data = client.quote(store_code, [{
                    "productCode": code, "quantity": 1,
                    **({"couponId": c["couponId"]} if c.get("couponId") else {}),
                    **({"couponCode": c["couponCode"]} if c.get("couponCode") else {}),
                }])
            except McpError:
                continue
            plist = data.get("productList") or []
            if not plist:
                continue
            sub = plist[0].get("subtotal")
            if sub is None:
                continue
            out[code] = {
                "price": round(sub / 100.0, 2),
                "title": c.get("title", ""),
                "name": names.get(code) or plist[0].get("productName") or "",
            }
    return out


def main():
    ap = argparse.ArgumentParser(
        description="麦当劳优惠券：查看、领取，并试算哪张券最划算")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--city", default="北京", help="城市名")
    ap.add_argument("--store-code", default=None, help="指定门店编码，跳过自动选择")
    ap.add_argument("--bind", action="store_true",
                    help="一键领取麦麦省当前所有可领券（账号写操作）")
    ap.add_argument("--calibrate", action="store_true",
                    help="把券折算成券后价写入 data/prices_coupon.json，供求解器直接调用")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出原始结果")
    args = ap.parse_args()

    token = args.token or os.environ.get("MCD_MCP_TOKEN")
    if not token:
        print("错误：未设置 MCD_MCP_TOKEN。", file=sys.stderr)
        print("获取方式：https://open.mcd.cn/mcp 登录后在控制台激活。", file=sys.stderr)
        sys.exit(2)

    client = CouponClient(token=token, verbose=False)
    client.initialize()

    if args.bind:
        print("  正在领取麦麦省当前可领的优惠券 ...")
        res = client.bind()
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        ok = res.get("success")
        print(f"  {'领取成功' if ok else '领取失败'}：{res.get('message')}")
        if not ok:
            sys.exit(1)

    print("\n  可领取的券（available-coupons）")
    print("  " + "-" * 62)
    avail = client.available()
    for c in avail:
        print(f"    · {c['title']}　[{c.get('status') or '状态未知'}]")

    store_code = args.store_code
    if not store_code:
        store = client.pick_store(args.city)
        store_code = store.get("storeCode")

    mine = client.mine()
    usable = client.store(store_code)
    promos = client.promotions(store_code)

    print()
    print(render_coupons(mine, "卡包中的券（query-my-coupons）"))
    print()
    print(render_coupons(usable, f"门店 {store_code} 可用的券（query-store-coupons）"))

    if promos:
        print(f"\n  门店促销规则（{len(promos)} 条）")
        for p in promos[:5]:
            print(f"    · {json.dumps(p, ensure_ascii=False)[:100]}")

    if not (mine or usable):
        print("\n  说明：available-coupons 会列出活动页展示的券，但与领券接口口径不同，")
        print("        列出来的未必都能领。上表为空即表示卡包中当前确实无券。")

    if args.calibrate:
        if not usable:
            print("\n  没有可用券，跳过校准。")
            return
        print(f"\n  正在用 calculate-price 折算券后价 ...")
        prices = calibrate(client, store_code, usable)
        if not prices:
            print("  折算失败：官方未返回任何券后价。")
            return
        out_path = os.path.join(DATA_DIR, "prices_coupon.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({
                "fetched_at": _now(),
                "store_code": store_code,
                "prices": prices,
            }, f, ensure_ascii=False, indent=2)
        print(f"  已折算 {len(prices)} 个券后价：")
        for code, p in prices.items():
            print(f"    {p['name'] or code:<20s} ¥{p['price']:<6.2f}  [{p['title']}]")
        print(f"\n  已保存至 {os.path.relpath(out_path)}")

    # 落盘，供 cli.py --coupons 复用，避免每次求解都重复调用券接口
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "coupons.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "fetched_at": _now(),
            "store_code": store_code,
            "available": avail,
            "mine": mine,
            "store_usable": usable,
            "promotions": promos,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  已保存至 {os.path.relpath(out_path)}")

    if args.json:
        print(json.dumps({
            "available": avail, "mine": mine,
            "store_usable": usable, "promotions": promos,
        }, ensure_ascii=False, indent=2))
    return


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
