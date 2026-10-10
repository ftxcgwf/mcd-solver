#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过麦当劳官方 MCP Server 抓取实时在售菜单与价格。

为什么必须走 MCP
----------------
官网营养计算器只有营养，没有价格。而「预算 30 块怎么吃」这个问题，
价格是硬约束——所以价格只能来自 MCP 的 query-meals。
这也是本项目真实使用麦当劳 MCP 能力的核心证据。

本脚本直接从零实现 MCP Streamable HTTP 客户端，不依赖任何第三方包，
这样 clone 仓库的人不用装 mcp / httpx 也能跑。

获取 Token
----------
1. 打开 https://open.mcd.cn/mcp
2. 右上角登录（手机号验证码）
3. 进入控制台，点击「激活」申请 MCP Token 并同意服务协议
4. 复制 Token

用法
----
    export MCD_MCP_TOKEN=你的Token
    python scripts/fetch_menu.py

    # 或显式传入
    python scripts/fetch_menu.py --token xxxx --store-id 12345

    # 只列出可用工具，不抓菜单（用于体检连接）
    python scripts/fetch_menu.py --list-tools

限流：官方限制每分钟 600 次请求，本脚本请求量很小，不会触发。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"

# 麦当劳 MCP Server 使用的协议版本较旧，新版协议握手会失败。
# 因此按从新到旧依次尝试，握手成功即停。
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


class McpError(RuntimeError):
    pass


class McpClient:
    """极简 MCP Streamable HTTP 客户端。"""

    def __init__(self, url=MCP_URL, token=None, verbose=True):
        self.url = url
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "").strip()
        self.session_id = None
        self.protocol = None
        self.verbose = verbose
        self._id = 0

    def _log(self, msg):
        if self.verbose:
            print(msg, file=sys.stderr)

    def _post(self, payload, timeout=60):
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请先到 https://open.mcd.cn/mcp 申请，"
                "然后设置环境变量 MCD_MCP_TOKEN 或用 --token 传入。"
            )
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            # 必须显式声明 charset：中文城市名若不声明，服务端按默认编码解析会得到空值，
            # 表现为「城市名或者关键词不能为空」，很难排查
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                return self._parse(resp.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            if e.code == 401:
                raise McpError("401：MCP Token 无效、已过期或未提供，请重新申请。")
            if e.code == 429:
                raise McpError("429：触发限流（每分钟 600 次），请稍后重试。")
            raise McpError(f"HTTP {e.code}: {body[:400]}")
        except OSError as e:
            raise McpError(f"网络错误：{e}")

    @staticmethod
    def _parse(raw):
        """MCP 响应可能是纯 JSON，也可能是 SSE 流，两种都要能读。"""
        raw = (raw or "").strip()
        if not raw:
            return None
        if raw.startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return None

    def _next_id(self):
        self._id += 1
        return self._id

    def initialize(self):
        """握手，自动协商协议版本。"""
        last_err = None
        for ver in PROTOCOL_VERSIONS:
            resp = self._post({
                "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                "params": {
                    "protocolVersion": ver,
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-solver", "version": "0.1.0"},
                },
            })
            if resp and "result" in resp:
                self.protocol = ver
                self._log(f"  握手成功，协议版本 {ver}")
                # 通知服务端初始化完成（无响应体，失败可忽略）
                try:
                    self._post({
                        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
                    })
                except McpError:
                    pass
                return True
            last_err = resp.get("error") if isinstance(resp, dict) else "无响应"
            self._log(f"  协议 {ver} 握手失败：{last_err}")
        raise McpError(f"所有协议版本均握手失败：{last_err}")

    def list_tools(self):
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {},
        })
        if not resp or "result" not in resp:
            raise McpError(f"获取工具列表失败：{resp}")
        return resp["result"].get("tools", [])

    def call_tool(self, name, arguments=None):
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if not resp:
            raise McpError(f"调用 {name} 无响应")
        if "error" in resp:
            raise McpError(f"调用 {name} 出错：{resp['error']}")
        return resp.get("result", {})


def extract_text(result):
    """从 MCP 工具结果里取出文本内容。"""
    parts = []
    for c in result.get("content", []) or []:
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def parse_json_relaxed(text):
    """
    从 MCP 返回文本里取出 JSON 对象。

    返回体是「说明文字 + JSON」的混合格式，说明里还夹着
    `data.meals.{code}` 这类占位符——所以不能简单取第一个 `{`：
    那个花括号属于占位符，不是 JSON。

    做法：逐个花括号位置尝试 raw_decode，取第一个能完整解析、
    且看起来像接口响应的对象。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        seg = text[m.start():]
        try:
            obj, _ = decoder.raw_decode(seg)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("success" in obj or "data" in obj):
            return obj
    return None


def _pick_stores(data):
    """
    从工具返回里找出门店数组。

    麦当劳各工具的返回嵌套层级不统一（有的在 data[]、有的在 data.stores、
    还有的混在说明文字后面），与其硬编码路径，不如递归找出第一个
    「看起来是门店列表」的数组——即元素同时含门店名与门店编码。
    """
    def looks_like_store(d):
        return isinstance(d, dict) and (
            "storeCode" in d or "storeId" in d
        ) and ("storeName" in d or "name" in d or "address" in d)

    def walk(node):
        if isinstance(node, list):
            if node and looks_like_store(node[0]):
                return node
            for x in node:
                r = walk(x)
                if r:
                    return r
            return None
        if isinstance(node, dict):
            if looks_like_store(node):
                return [node]
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        return None

    return walk(data) or []


def _to_float(v):
    """价格字段是字符串，且可能带货币符号或为空。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def parse_menu(data):
    """
    解析 query-meals 的返回。

    实际结构是两段式：
      data.categories[]          分类列表，每个分类下只有餐品 code
      data.meals{code: {...}}    餐品详情映射表，含名称与现价/原价

    分类和详情分开存，所以要先建立 code -> 分类名 的映射，再回填。
    """
    if not isinstance(data, dict):
        return []
    d = data.get("data", data)
    if not isinstance(d, dict):
        return []

    cat_of = {}
    for c in (d.get("categories") or []):
        if not isinstance(c, dict):
            continue
        cname = c.get("name") or ""
        for m in (c.get("meals") or []):
            if isinstance(m, dict) and m.get("code"):
                cat_of[m["code"]] = cname

    items = []
    meals = d.get("meals")
    if isinstance(meals, dict):
        for code, m in meals.items():
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            if not name:
                continue
            items.append({
                "name": name,
                "code": m.get("code") or code,
                "price": _to_float(m.get("currentPrice")),
                "original_price": _to_float(m.get("originalPrice")),
                "discount_type": m.get("discountType"),
                "category": cat_of.get(m.get("code") or code, ""),
            })
    return items


def main():
    ap = argparse.ArgumentParser(description="通过麦当劳 MCP 抓取实时菜单与价格")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--city", default=None,
                    help="城市名，用于查询门店（如「北京」）。未收藏餐厅时必填")
    ap.add_argument("--keyword", default=None, help="门店搜索关键词，如商圈名")
    ap.add_argument("--search-type", type=int, default=2,
                    help="搜索方式：1-收藏餐厅（未收藏会报错）2-按位置搜索（默认 2）")
    ap.add_argument("--store-code", default=None,
                    help="直接指定门店编码，跳过门店查询")
    ap.add_argument("--be-type", type=int, default=1,
                    help="业务类型：1-到店取餐 2-麦乐送到家 5-得来速 6-企业团餐（默认 1）")
    ap.add_argument("--order-type", type=int, default=1,
                    help="订单类型：1-到店 2-外送（默认 1）")
    ap.add_argument("--out", default="data/menu.json", help="输出路径")
    ap.add_argument("--list-tools", action="store_true", help="只列出可用工具")
    ap.add_argument("--describe", default=None, help="打印指定工具的参数结构后退出")
    ap.add_argument("--debug", action="store_true", help="打印原始返回，便于排查解析问题")
    args = ap.parse_args()

    client = McpClient(token=args.token)
    try:
        client.initialize()
        tools = client.list_tools()
        print(f"已连接麦当劳 MCP，可用工具 {len(tools)} 个")
        if args.list_tools:
            for t in tools:
                print(f"  - {t.get('name')}: {(t.get('description') or '')[:60]}")
            return 0

        if args.describe:
            for t in tools:
                if t.get("name") == args.describe:
                    print(json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2))
                    return 0
            raise McpError(f"未找到工具 {args.describe}")

        names = {t.get("name") for t in tools}

        # 1. 门店编码：query-meals 的必填入参，不能省
        store_code, be_code = args.store_code, None
        if not store_code:
            if "query-nearby-stores" not in names:
                raise McpError("MCP 未提供 query-nearby-stores，请用 --store-code 直接指定门店")
            # searchType 是必填：1=搜收藏餐厅（未收藏会直接报错），2=按位置搜索。
            # 首次使用的人通常没有收藏餐厅，所以默认走按位置搜索。
            q = {
                "beType": args.be_type if args.be_type in (1, 5) else 1,
                "searchType": args.search_type,
            }
            if args.city:
                q["city"] = args.city
            if args.keyword:
                q["keyword"] = args.keyword
            # 实测：city 与 keyword 必须同时给。只传其中一个，服务端一律返回
            # 「城市名或者关键词不能为空」（code 600058）。给了城市就用麦当劳补全。
            if args.city and not args.keyword:
                q["keyword"] = "麦当劳"
            print(f"查询门店（beType={q['beType']}，城市={args.city or '未指定'}）...")
            r = client.call_tool("query-nearby-stores", q)
            raw = extract_text(r)
            if args.debug:
                print("--- query-nearby-stores 原始返回 ---")
                print(raw[:2000])
            data = parse_json_relaxed(raw) or {}
            if data.get("success") is False:
                raise McpError(
                    f"门店查询失败：{data.get('message')}（code={data.get('code')}）。"
                    f"请用 --city 指定城市，必要时加 --keyword。"
                )
            stores = _pick_stores(data)
            if not stores:
                raise McpError(
                    "未解析到门店列表。可用 --debug 查看原始返回，"
                    "或 --describe query-nearby-stores 核对参数。"
                )
            store_code = stores[0].get("storeCode") or stores[0].get("storeId")
            be_code = stores[0].get("beCode")
            print(f"  门店：{stores[0].get('storeName')}（storeCode={store_code}）")
            if len(stores) > 1:
                print(f"  另有 {len(stores) - 1} 家可选，用 --store-code 切换")

        # 2. 抓菜单（含价格）。storeCode / orderType / beType 三个都是必填
        if "query-meals" not in names:
            raise McpError("MCP 未提供 query-meals 工具")
        arg = {"storeCode": store_code, "orderType": args.order_type, "beType": args.be_type}
        if be_code:
            arg["beCode"] = be_code
        print("抓取在售菜单 ...")
        r = client.call_tool("query-meals", arg)
        raw = extract_text(r)
        if args.debug:
            print("--- query-meals 原始返回前 2500 字符 ---")
            print(raw[:2500])
        data = parse_json_relaxed(raw)

        norm = parse_menu(data) if data else []

        payload = {
            "source": "麦当劳中国 MCP Server (query-meals)",
            "mcp_url": MCP_URL,
            "store_code": store_code,
            "order_type": args.order_type,
            "be_type": args.be_type,
            "fetched_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "count": len(norm),
            "items": norm,
        }
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        with_price = sum(1 for i in norm if i["price"] is not None)
        print(f"已写入 {args.out}：{len(norm)} 个餐品，其中 {with_price} 个含价格")
        if with_price == 0:
            print("提示：未解析到价格字段，请用 --list-tools 与原始数据核对结构，"
                  "必要时调整 parse 逻辑。")
        return 0

    except McpError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
