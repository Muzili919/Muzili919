"""通过 CDP 驱动本地 Chrome 里的 ChatGPT 生图。

前提：Chrome 必须以 --remote-debugging-port=9230 启动，并且已登录 ChatGPT。
见 scripts/launch_chrome.sh。

设计要点：
  - 图片二进制在**页面内**用 fetch 取，转 base64 回传。这样天然带着页面的
    登录 cookie，不用在 Python 侧处理鉴权。
  - 所有 DOM 选择器集中在 _SELECTORS，ChatGPT 改版时只改这一处。
  - 每一步失败都给出「具体该怎么修」的错误信息，而不是抛个 TimeoutError 了事。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.sync.client import connect as ws_connect

log = logging.getLogger(__name__)

# ChatGPT 的 DOM 选择器。改版时优先怀疑这里。
# 每项给多个候选，从前往后试，提高对小改动的容忍度。
_SELECTORS = {
    "composer": ["#prompt-textarea", "div[contenteditable='true']", "textarea"],
    "send_button": [
        "button[data-testid='send-button']",
        "#composer-submit-button",
        "button[aria-label*='Send']",
    ],
    # 生成的图片：ChatGPT 把图放在 oaiusercontent 域名下
    "generated_image": [
        "img[src*='oaiusercontent']",
        "main img[src^='blob:']",
        "main img[alt*='Generated']",
    ],
}

_WS_TIMEOUT = 30.0


class CDPError(RuntimeError):
    """CDP 连接或交互失败，错误信息里必须写明修复动作。"""


@dataclass
class CDPTarget:
    title: str
    url: str
    ws_url: str


class CDPSession:
    """一个 CDP WebSocket 会话。用 with 语句管理生命周期。"""

    def __init__(self, ws_url: str):
        self._ws_url = ws_url
        self._ws: Any = None
        self._msg_id = 0

    def __enter__(self) -> CDPSession:
        try:
            self._ws = ws_connect(self._ws_url, open_timeout=_WS_TIMEOUT, max_size=64 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            raise CDPError(f"无法建立 CDP WebSocket 连接：{exc}") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 — 关闭失败无所谓
                pass

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发一条 CDP 命令，等对应 id 的响应。"""
        self._msg_id += 1
        msg_id = self._msg_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

        # 事件和响应混在一条流里，要丢掉事件直到拿到匹配 id 的响应
        deadline = time.monotonic() + _WS_TIMEOUT
        while time.monotonic() < deadline:
            raw = self._ws.recv(timeout=max(1.0, deadline - time.monotonic()))
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"CDP 命令 {method} 返回错误：{data['error']}")
                return data.get("result", {})
        raise CDPError(f"CDP 命令 {method} 超时（{_WS_TIMEOUT}s）")

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        """在页面里执行 JS，返回结果值。"""
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CDPError(f"页面内 JS 执行出错：{text}")
        return result.get("result", {}).get("value")


def find_chatgpt_target(port: int, url_contains: str) -> CDPTarget:
    """在 Chrome 的所有标签页里找到 ChatGPT 那个。"""
    base = f"http://127.0.0.1:{port}"

    try:
        resp = httpx.get(f"{base}/json/version", timeout=5.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise CDPError(
            f"连不上 {base} —— Chrome 没有开着调试端口。\n"
            f"修复：先关掉所有 Chrome 窗口，再执行 scripts/launch_chrome.sh 启动。\n"
            f"底层错误：{exc}"
        ) from exc

    try:
        targets = httpx.get(f"{base}/json", timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        raise CDPError(f"获取 Chrome 标签页列表失败：{exc}") from exc

    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if url_contains in t.get("url", ""):
            return CDPTarget(
                title=t.get("title", ""),
                url=t.get("url", ""),
                ws_url=t["webSocketDebuggerUrl"],
            )

    opened = "\n".join(f"  - {t.get('url', '')[:90]}" for t in pages) or "  （没有任何标签页）"
    raise CDPError(
        f"Chrome 里没找到包含 '{url_contains}' 的标签页。\n"
        f"修复：在那个 Chrome 窗口里打开 https://chatgpt.com 并保持登录。\n"
        f"当前打开的标签页：\n{opened}"
    )


class ChatGPTImageGenerator:
    def __init__(self, port: int, url_contains: str, wait_timeout: int = 240):
        self.port = port
        self.url_contains = url_contains
        self.wait_timeout = wait_timeout

    def probe(self) -> str:
        """健康检查用：确认端口通、标签页在、composer 可见。"""
        target = find_chatgpt_target(self.port, self.url_contains)
        with CDPSession(target.ws_url) as sess:
            sess.command("Runtime.enable")
            found = sess.evaluate(_js_find_selector(_SELECTORS["composer"]))
            if not found:
                raise CDPError(
                    "找到了 ChatGPT 标签页，但页面里没有输入框。\n"
                    "多半是没登录，或页面停在加载中。手动打开那个标签页看一眼。"
                )
        return f"OK — {target.title[:60]}"

    def generate(self, prompt: str) -> bytes:
        """发提示词，等出图，返回图片二进制。"""
        target = find_chatgpt_target(self.port, self.url_contains)
        log.info("已连上 ChatGPT 标签页：%s", target.title[:60])

        with CDPSession(target.ws_url) as sess:
            sess.command("Runtime.enable")
            sess.command("Page.enable")

            before = self._image_urls(sess)
            log.info("发送生图提示词前，页面已有 %d 张图", len(before))

            self._send_prompt(sess, prompt)
            new_url = self._wait_for_new_image(sess, before)
            log.info("检测到新图：%s", new_url[:90])

            return self._download_in_page(sess, new_url)

    # ---------- 内部步骤 ----------

    def _send_prompt(self, sess: CDPSession, prompt: str) -> None:
        """把提示词填进输入框并发送。"""
        composer = sess.evaluate(_js_find_selector(_SELECTORS["composer"]))
        if not composer:
            raise CDPError(
                "ChatGPT 页面上找不到输入框。\n"
                "可能原因：未登录 / 页面没加载完 / ChatGPT 改版。\n"
                f"改版的话，更新 {__file__} 里的 _SELECTORS['composer']。"
            )

        # contenteditable 用 execCommand 插入，能正确触发 React 的 onChange；
        # 直接改 innerText 不会触发，发送按钮会一直是禁用状态。
        ok = sess.evaluate(
            f"""
            (() => {{
                const el = document.querySelector({json.dumps(composer)});
                if (!el) return false;
                el.focus();
                if (el.tagName === 'TEXTAREA') {{
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(el, {json.dumps(prompt)});
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }} else {{
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, {json.dumps(prompt)});
                }}
                return true;
            }})()
            """
        )
        if not ok:
            raise CDPError("填写提示词失败：输入框在填写瞬间消失了。")

        time.sleep(0.6)  # 等 React 状态更新，发送按钮才会变可点

        clicked = sess.evaluate(
            f"""
            (() => {{
                const sels = {json.dumps(_SELECTORS["send_button"])};
                for (const s of sels) {{
                    const btn = document.querySelector(s);
                    if (btn && !btn.disabled) {{ btn.click(); return true; }}
                }}
                return false;
            }})()
            """
        )
        if not clicked:
            raise CDPError(
                "找不到可点击的发送按钮（或按钮是禁用状态）。\n"
                "提示词可能没真正填进去。手动看一眼那个标签页。"
            )
        log.info("提示词已发送，等待出图（最多 %d 秒）", self.wait_timeout)

    def _image_urls(self, sess: CDPSession) -> set[str]:
        urls = sess.evaluate(
            f"""
            (() => {{
                const sels = {json.dumps(_SELECTORS["generated_image"])};
                const out = new Set();
                for (const s of sels) {{
                    document.querySelectorAll(s).forEach(img => {{
                        if (img.src) out.add(img.src);
                    }});
                }}
                return Array.from(out);
            }})()
            """
        )
        return set(urls or [])

    def _wait_for_new_image(self, sess: CDPSession, before: set[str]) -> str:
        """轮询直到出现新图。"""
        deadline = time.monotonic() + self.wait_timeout
        poll = 3.0
        while time.monotonic() < deadline:
            time.sleep(poll)
            new = self._image_urls(sess) - before
            if new:
                # 多张时取最后一张（最新生成的）
                return sorted(new)[-1]
            remaining = int(deadline - time.monotonic())
            if remaining % 30 < poll:
                log.info("仍在等待出图，剩余 %d 秒", remaining)

        raise CDPError(
            f"等了 {self.wait_timeout} 秒没等到新图。\n"
            f"可能原因：ChatGPT 拒绝了这个提示词 / 额度用尽 / 生成卡住。\n"
            f"打开那个标签页看看它实际回了什么。"
        )

    def _download_in_page(self, sess: CDPSession, url: str) -> bytes:
        """在页面上下文里 fetch 图片并转 base64——这样自动带上登录 cookie。"""
        data_url = sess.evaluate(
            f"""
            (async () => {{
                const resp = await fetch({json.dumps(url)});
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                const blob = await resp.blob();
                return await new Promise((resolve, reject) => {{
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.onerror = reject;
                    reader.readAsDataURL(blob);
                }});
            }})()
            """,
            await_promise=True,
        )

        if not isinstance(data_url, str) or "," not in data_url:
            raise CDPError(f"页面内下载图片失败，返回值异常：{str(data_url)[:200]}")

        try:
            payload = base64.b64decode(data_url.split(",", 1)[1])
        except Exception as exc:  # noqa: BLE001
            raise CDPError(f"图片 base64 解码失败：{exc}") from exc

        log.info("图片已取回，%.1f KB", len(payload) / 1024)
        return payload


def _js_find_selector(selectors: list[str]) -> str:
    """生成一段 JS：返回第一个能匹配到元素的选择器，都不匹配返回 null。"""
    return f"""
    (() => {{
        const sels = {json.dumps(selectors)};
        for (const s of sels) {{
            if (document.querySelector(s)) return s;
        }}
        return null;
    }})()
    """
