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
    # 生成的图片。ChatGPT 换过存放位置，所以新旧都留着——多一个候选不花成本，
    # 少一个就是"图明明出来了但认不出来"，而且报错长得像超时，极容易误判成
    # "生图失败"（2026-07-30 实测踩过：图已生成，全部候选都不匹配，白等 240 秒）。
    "generated_image": [
        # 2026-07 当前形态：/backend-api/estuary/content?id=file_xxx
        "main img[src*='estuary/content']",
        # 更早的形态，保留兼容
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
            f"连不上 {base} —— 没有 Chrome 在这个端口上开调试。\n"
            f"修复：执行 scripts/launch_chrome.sh（用独立 profile，不需要关掉别的 Chrome）。\n"
            f"如果这个端口上本来就有别的 Chrome 实例，换一个：\n"
            f"  CDP_PORT=xxxx ./scripts/launch_chrome.sh + 同步改 config.yaml 的 image.cdp_port\n"
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
    def __init__(
        self,
        port: int,
        url_contains: str,
        wait_timeout: int = 240,
        new_chat_url: str = "https://chatgpt.com/",
    ):
        self.port = port
        self.url_contains = url_contains
        self.wait_timeout = wait_timeout
        # 临时标签页开哪个地址。生产就是 ChatGPT 首页（= 一段新对话），
        # 测试里指向本地仿真页
        self.new_chat_url = new_chat_url

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
        """发提示词，等出图，返回图片二进制。

        在**临时新标签页**里开一段新对话，用完就关。不复用已经打开的那个
        ChatGPT 标签页——那是人在用的对话，每天往里塞一条生图提示词，
        几周下来对话历史就没法看了。顺带的好处是新对话里一张图都没有，
        "哪张是新出的图"判断得干干净净。
        """
        # 先确认那个 Chrome 里确实登录着 ChatGPT，再开新标签页；
        # 顺序反了的话，没登录时会留下一个空标签页
        anchor = find_chatgpt_target(self.port, self.url_contains)
        log.info("ChatGPT 已就绪（参照标签页：%s）", anchor.title[:50])

        target_id, ws_url = self._open_temp_tab()
        succeeded = False
        try:
            with CDPSession(ws_url) as sess:
                sess.command("Runtime.enable")
                sess.command("Page.enable")
                self._wait_for_composer(sess)

                before = self._image_urls(sess)
                log.info("新对话里已有 %d 张图（正常应为 0）", len(before))

                self._send_prompt(sess, prompt)
                new_url = self._wait_for_new_image(sess, before)
                log.info("检测到新图：%s", new_url[:90])

                data = self._download_in_page(sess, new_url)
                succeeded = True
                return data
        finally:
            # 失败时留着现场。几乎所有报错都在让人"打开那个标签页看一眼它实际回了
            # 什么"，顺手关掉就等于把唯一的线索删了。
            if succeeded:
                self._close_temp_tab(target_id)
            else:
                log.warning(
                    "生图失败，临时标签页留着以便排查（用完手动关）：%s", target_id[:12]
                )

    def _open_temp_tab(self) -> tuple[str, str]:
        """开一个临时标签页，返回 (targetId, 它的 page ws 地址)。"""
        base = f"http://127.0.0.1:{self.port}"
        try:
            browser_ws = httpx.get(f"{base}/json/version", timeout=5.0).json().get(
                "webSocketDebuggerUrl"
            )
        except Exception as exc:  # noqa: BLE001
            raise CDPError(f"取浏览器级 CDP 端点失败：{exc}") from exc
        if not browser_ws:
            raise CDPError(f"{base}/json/version 里没有 webSocketDebuggerUrl，开不了新标签页。")

        with CDPSession(browser_ws) as browser:
            target_id = browser.command(
                "Target.createTarget", {"url": self.new_chat_url}
            ).get("targetId")
        if not target_id:
            raise CDPError("Target.createTarget 没返回 targetId。")

        # 新标签页要等一会儿才在 /json 里带上 webSocketDebuggerUrl
        for _ in range(40):
            time.sleep(0.5)
            try:
                for t in httpx.get(f"{base}/json", timeout=5.0).json():
                    if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                        log.info("已开临时标签页 %s", target_id[:12])
                        return target_id, t["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 — 轮询期间的抖动忽略
                continue

        self._close_temp_tab(target_id)
        raise CDPError("新标签页开出来了，但 20 秒内没拿到它的调试地址。")

    def _close_temp_tab(self, target_id: str) -> None:
        """关掉临时标签页。失败只记日志——图已经拿到了，不该因为收尾失败而报错。"""
        if not target_id:
            return
        try:
            base = f"http://127.0.0.1:{self.port}"
            browser_ws = httpx.get(f"{base}/json/version", timeout=5.0).json()[
                "webSocketDebuggerUrl"
            ]
            with CDPSession(browser_ws) as browser:
                browser.command("Target.closeTarget", {"targetId": target_id})
            log.info("临时标签页已关闭")
        except Exception as exc:  # noqa: BLE001
            log.warning("临时标签页没关掉（%s），手动关一下：%s", target_id[:12], exc)

    def _wait_for_composer(self, sess: CDPSession) -> None:
        """等新对话页面真正可用。

        只等"输入框出现"是不够的：composer 的候选选择器里有
        `div[contenteditable='true']` 和 `textarea` 这种很宽的，ChatGPT 这类
        SPA 在 React 水合完成之前就可能匹配上，于是这里立刻返回、下一步却发现
        发送按钮还没渲染出来，报成"找不到发送按钮"。所以要等到
        readyState complete + 输入框在 + 连续两次都还在，再往下走。
        """
        stable = 0
        for _ in range(60):
            time.sleep(0.5)
            ready = sess.evaluate(
                f"""
                (() => {{
                    if (document.readyState !== 'complete') return false;
                    const sels = {json.dumps(_SELECTORS["composer"])};
                    return sels.some(s => document.querySelector(s));
                }})()
                """
            )
            if ready:
                stable += 1
                if stable >= 2:
                    time.sleep(1.0)  # 再给 React 一点时间把发送按钮挂上
                    return
            else:
                stable = 0

        raise CDPError(
            "新开的 ChatGPT 页面 30 秒内没进入可用状态。\n"
            "可能是这个 profile 的登录态失效了，或者页面卡在加载中。"
        )

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

        # 轮询等按钮变可点。不能只 sleep 一个固定时长——快的时候浪费，
        # 慢的时候（页面刚开、网络抖动）就误判成"按钮不存在"
        clicked = False
        for _ in range(30):
            time.sleep(0.5)
            clicked = bool(
                sess.evaluate(
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
            )
            if clicked:
                break

        if not clicked:
            # 兜底：ChatGPT 支持回车发送。按钮改版或一直禁用时还有这条路。
            log.warning("发送按钮点不动，改用回车发送")
            for event_type in ("keyDown", "char", "keyUp"):
                params: dict[str, Any] = {
                    "type": event_type,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                }
                if event_type == "char":
                    params["text"] = "\r"
                sess.command("Input.dispatchKeyEvent", params)
            time.sleep(1.0)
            # 输入框被清空 = 确实发出去了
            still_there = sess.evaluate(
                f"""
                (() => {{
                    const el = document.querySelector({json.dumps(composer)});
                    return el ? (el.value ?? el.innerText ?? '').trim().length > 0 : false;
                }})()
                """
            )
            if still_there:
                raise CDPError(
                    "提示词填进去了，但发送按钮点不动、回车也没发出去。\n"
                    "手动打开那个 ChatGPT 标签页看一眼：是不是没登录、额度用尽，\n"
                    f"或者改版了（那就更新 {__file__} 里的 _SELECTORS['send_button']）。"
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
