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
            "Content-Type": "application/json",
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
    """MCP 返回的内容有时是 JSON，有时是紧凑的 TOON 文本，尽力解析。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试截取第一个 JSON 对象
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def main():
    ap = argparse.ArgumentParser(description="通过麦当劳 MCP 抓取实时菜单与价格")
    ap.add_argument("--token", default=None, help="MCP Token（默认读 MCD_MCP_TOKEN）")
    ap.add_argument("--store-id", default=None, help="门店 ID，不填则尝试自动取第一个可配送门店")
    ap.add_argument("--out", default="data/menu.json", help="输出路径")
    ap.add_argument("--list-tools", action="store_true", help="只列出可用工具")
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

        names = {t.get("name") for t in tools}

        # 1. 找门店（query-meals 需要门店上下文）
        store_id = args.store_id
        if not store_id and "query-nearby-stores" in names:
            print("未指定门店，尝试查询附近门店 ...")
            r = client.call_tool("query-nearby-stores", {})
            data = parse_json_relaxed(extract_text(r))
            stores = (data or {}).get("data", {}).get("stores") or \
                     (data or {}).get("stores") or []
            if stores:
                store_id = stores[0].get("storeId") or stores[0].get("id")
                print(f"  使用门店：{stores[0].get('storeName') or store_id}（{store_id}）")

        # 2. 抓菜单（含价格）
        if "query-meals" not in names:
            raise McpError("MCP 未提供 query-meals 工具")
        arg = {"storeId": store_id} if store_id else {}
        print("抓取在售菜单 ...")
        r = client.call_tool("query-meals", arg)
        raw = extract_text(r)
        data = parse_json_relaxed(raw)

        items = []
        if data:
            # 菜单结构在不同版本里有不同嵌套，逐层尝试
            d = data.get("data", data)
            for key in ("meals", "items", "list", "categories"):
                if isinstance(d, dict) and key in d:
                    d = d[key]
                    break
            if isinstance(d, dict):
                # 分类 -> 餐品列表 的形态
                for _, v in d.items():
                    if isinstance(v, list):
                        items.extend([x for x in v if isinstance(x, dict)])
            elif isinstance(d, list):
                items = [x for x in d if isinstance(x, dict)]

        norm = []
        for it in items:
            name = it.get("name") or it.get("mealName") or it.get("productName")
            if not name:
                continue
            price = None
            for k in ("price", "salePrice", "currentPrice", "amount"):
                if it.get(k) is not None:
                    try:
                        price = float(it[k])
                        break
                    except (TypeError, ValueError):
                        continue
            norm.append({
                "name": name,
                "code": it.get("code") or it.get("mealCode") or it.get("productCode"),
                "price": price,
                "category": it.get("categoryName") or it.get("category") or "",
            })

        payload = {
            "source": "麦当劳中国 MCP Server (query-meals)",
            "mcp_url": MCP_URL,
            "store_id": store_id,
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
