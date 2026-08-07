#!/usr/bin/env python3
"""从一个已登录的 Chrome 实例导出登录态，省掉扫码。

scripts/login.py 会开一个新浏览器让你扫码。但如果你本来就有一个开着调试端口、
已经登录着头条的 Chrome（比如别的自动化脚本在用的那个），登录态直接搬过来就行，
手机都不用掏。

    python scripts/import_login_from_cdp.py --port 9228

登录态过期时也是跑这个，前提是那个实例里的会话还有效。

只读操作：不新建标签页、不导航、不关窗口。源浏览器的状态一点不动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]

# 头条的登录态散落在多个字节系域名下，少一个就可能被判未登录
_DEFAULT_DOMAINS = (
    "toutiao.com",
    "snssdk.com",
    "bytedance.com",
    "bytedance.net",
    "byteimg.com",
    "toutiaoapi.com",
    "feishu.cn",
)


class CDP:
    """够用就好的 CDP 客户端：命令按 id 配对，支持 flatten 后的 sessionId。"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None, session_id: str = "") -> dict:
        self._id += 1
        msg: dict = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self.ws.send(json.dumps(msg))

        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            data = json.loads(raw)
            if data.get("id") != self._id:
                continue  # 事件或别的响应，跳过
            if "error" in data:
                raise RuntimeError(f"{method} 失败：{data['error']}")
            return data.get("result", {})


def _to_playwright_cookie(c: dict) -> dict:
    same_site = c.get("sameSite") or "Lax"
    if same_site not in ("Strict", "Lax", "None"):
        same_site = "Lax"
    return {
        "name": c["name"],
        "value": c["value"],
        "domain": c["domain"],
        "path": c.get("path", "/"),
        # CDP 用 -1 表示会话 cookie，Playwright 也是
        "expires": c.get("expires", -1) if c.get("expires", -1) > 0 else -1,
        "httpOnly": bool(c.get("httpOnly")),
        # sameSite=None 的 cookie 浏览器只在 secure 下接受，不补会被 Playwright 拒掉
        "secure": bool(c.get("secure")) or same_site == "None",
        "sameSite": same_site,
    }


async def export_state(port: int, url_contains: str, domains: tuple[str, ...]) -> dict:
    base = f"http://127.0.0.1:{port}"
    try:
        version = httpx.get(f"{base}/json/version", timeout=5).json()
        targets = httpx.get(f"{base}/json", timeout=5).json()
    except Exception as exc:
        raise SystemExit(
            f"连不上 {base}：{exc}\n"
            f"那个 Chrome 没在跑，或者没开 --remote-debugging-port={port}。"
        ) from None

    browser_ws = version.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise SystemExit(f"{base}/json/version 里没有 webSocketDebuggerUrl，无法取 cookie。")

    pages = [t for t in targets if t.get("type") == "page"]
    matched = [t for t in pages if url_contains in t.get("url", "")]
    if not matched:
        urls = "\n".join(f"  - {t.get('url', '')[:90]}" for t in pages) or "  （没有页面）"
        raise SystemExit(
            f"端口 {port} 上没有 URL 含「{url_contains}」的标签页。\n"
            f"当前页面：\n{urls}\n"
            f"先在那个浏览器里打开并登录目标站点。"
        )

    async with websockets.connect(browser_ws, max_size=64 * 1024 * 1024) as ws:
        cdp = CDP(ws)

        # 浏览器级取全部 cookie，比逐页取全
        all_cookies = (await cdp.call("Storage.getCookies")).get("cookies", [])
        kept = [
            c for c in all_cookies
            if any(c.get("domain", "").lstrip(".").endswith(d) for d in domains)
        ]

        # localStorage：头条把一部分会话信息放在这里，只搬 cookie 有时不够
        target_id = matched[0]["id"]
        attached = await cdp.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = attached["sessionId"]
        result = await cdp.call(
            "Runtime.evaluate",
            {
                "expression": (
                    "JSON.stringify({origin: location.origin, items: "
                    "Object.entries(localStorage)})"
                ),
                "returnByValue": True,
            },
            session_id=session_id,
        )
        payload = json.loads(result["result"]["value"])
        await cdp.call("Target.detachFromTarget", {"sessionId": session_id})

    origins = []
    if payload.get("items"):
        origins.append(
            {
                "origin": payload["origin"],
                "localStorage": [{"name": k, "value": v} for k, v in payload["items"]],
            }
        )

    print(f"  浏览器共 {len(all_cookies)} 个 cookie，命中目标域名 {len(kept)} 个")
    print(f"  localStorage 条目 {len(payload.get('items', []))} 个（来自 {payload.get('origin')}）")
    if not kept:
        raise SystemExit(
            "一个目标域名的 cookie 都没取到，导出没有意义。\n"
            "确认那个浏览器里确实登录着，且 --domains 覆盖了它的域名。"
        )
    return {"cookies": [_to_playwright_cookie(c) for c in kept], "origins": origins}


def main() -> int:
    ap = argparse.ArgumentParser(description="从已登录的 CDP 实例导出 Playwright 登录态")
    ap.add_argument("--port", type=int, default=9228, help="源 Chrome 的调试端口")
    ap.add_argument(
        "--url-contains", default="mp.toutiao.com", help="用它认出目标标签页"
    )
    ap.add_argument("--out", default="state/storage_state.json", help="输出路径")
    ap.add_argument(
        "--domains",
        default=",".join(_DEFAULT_DOMAINS),
        help="保留哪些域名的 cookie，逗号分隔",
    )
    args = ap.parse_args()

    domains = tuple(d.strip() for d in args.domains.split(",") if d.strip())
    print(f"从端口 {args.port} 导出登录态…")
    state = asyncio.run(export_state(args.port, args.url_contains, domains))

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(out, 0o600)  # 登录态等同密码，别留成全局可读

    print(f"✅ 已写入 {out}")
    print("   验证：python -m toutiao_publisher doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
