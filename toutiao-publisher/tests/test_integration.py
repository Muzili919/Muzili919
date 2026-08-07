"""集成测试：拿真浏览器验证 CDP 生图与 Playwright 发布两条链路。

这两段是整个项目最容易坏的地方，也是纯单元测试覆盖不到的——它们的正确性
取决于「浏览器实际会怎么响应」，mock 掉就等于什么都没测。

这里用本地仿真页面代替 ChatGPT 和头条：DOM 结构复刻生产代码依赖的那几个特征，
交互行为（React 式的按钮禁用、异步出图、隐藏 file input）也一并模拟。
选择器本身仍需对真实站点验证，但**协议层与交互逻辑**在这里是真跑通的。

跳过条件：环境里没有 Chromium 时自动跳过，不影响纯逻辑测试。
"""

from __future__ import annotations

import functools
import http.server
import io
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toutiao_publisher.images.cdp_chatgpt import (  # noqa: E402
    CDPError,
    ChatGPTImageGenerator,
    find_chatgpt_target,
)

FIXTURES = Path(__file__).parent / "fixtures"

# 容器/CI 里 Chromium 的位置。本机跑测试时会退回 Playwright 自带的。
_CHROMIUM_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_chromium() -> str | None:
    for path in _CHROMIUM_CANDIDATES:
        if Path(path).exists():
            return path
    return None


CHROMIUM = _find_chromium()
# 容器/CI 里以 root 运行，必须关沙箱
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

requires_chromium = pytest.mark.skipif(
    CHROMIUM is None, reason="环境里没有 Chromium，跳过集成测试"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_png(width: int = 1024, height: int = 576) -> bytes:
    """造一张有内容的测试图。"""
    import random

    img = Image.new("RGB", (width, height), (30, 30, 30))
    rnd = random.Random(7)
    pixels = img.load()
    block = 128
    for by in range(0, height, block):
        for bx in range(0, width, block):
            shade = rnd.randint(60, 240)
            for x in range(bx, min(bx + block, width)):
                for y in range(by, min(by + block, height)):
                    j = rnd.randint(-15, 15)
                    v = max(0, min(255, shade + j))
                    pixels[x, y] = (v, v, v)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _make_png()


class _Handler(http.server.SimpleHTTPRequestHandler):
    """伺服 fixtures 目录，另外把 /oaiusercontent/* 映射成一张真 PNG。"""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 的命名约定
        if self.path.startswith("/oaiusercontent/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_BYTES)))
            self.end_headers()
            self.wfile.write(PNG_BYTES)
            return
        super().do_GET()

    def log_message(self, *args):  # 静音，别刷屏
        pass


@pytest.fixture(scope="module")
def server():
    """本地 HTTP 服务，伺服仿真页面。"""
    port = _free_port()
    handler = functools.partial(_Handler, directory=str(FIXTURES))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def chrome_with_cdp(server, tmp_path_factory):
    """启动带调试端口的 Chromium，并打开仿真 ChatGPT 页面。"""
    port = _free_port()
    profile = tmp_path_factory.mktemp("cdp-profile")

    proc = subprocess.Popen(
        [
            CHROMIUM,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--no-sandbox",  # 容器里以 root 运行必须加
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            f"{server}/fake_chatgpt.html",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 等调试端口就绪
    import httpx

    ready = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
            ready = True
            break
        except Exception:  # noqa: BLE001
            continue

    if not ready:
        proc.kill()
        pytest.skip(f"Chromium 调试端口 {port} 未能就绪")

    yield port
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------- CDP 生图链路 ----------


@requires_chromium
def test_find_target_locates_page(chrome_with_cdp):
    target = find_chatgpt_target(chrome_with_cdp, "fake_chatgpt")
    assert "fake_chatgpt" in target.url
    assert target.ws_url.startswith("ws://")


@requires_chromium
def test_find_target_reports_missing_port_clearly():
    """端口不通时的报错必须包含修复指引，而不是裸的 ConnectionError。"""
    with pytest.raises(CDPError) as exc:
        find_chatgpt_target(_free_port(), "chatgpt.com")
    assert "launch_chrome.sh" in str(exc.value)


@requires_chromium
def test_find_target_lists_open_tabs_when_no_match(chrome_with_cdp):
    """找不到目标标签页时，应把当前开着的页面列出来帮助排查。"""
    with pytest.raises(CDPError) as exc:
        find_chatgpt_target(chrome_with_cdp, "no-such-site-xyz")
    message = str(exc.value)
    assert "fake_chatgpt" in message  # 列出了实际开着的标签页
    assert "修复" in message


@requires_chromium
def test_probe_succeeds(chrome_with_cdp):
    gen = ChatGPTImageGenerator(port=chrome_with_cdp, url_contains="fake_chatgpt")
    assert gen.probe().startswith("OK")


@requires_chromium
def test_generate_image_end_to_end(chrome_with_cdp, server):
    """完整链路：开临时标签页 → 填提示词 → 点发送 → 等出图 → 页面内下载 → 返回二进制。

    这条用例同时验证了几件事：
      - execCommand 插入文本能触发页面状态更新（否则发送按钮是禁用的）
      - 轮询等待逻辑能正确识别"新增的"图片
      - 页面内 fetch + FileReader 转 base64 的回传路径是通的
    """
    gen = ChatGPTImageGenerator(
        port=chrome_with_cdp,
        url_contains="fake_chatgpt",
        wait_timeout=30,
        new_chat_url=f"{server}/fake_chatgpt.html",
    )
    data = gen.generate("画一张测试图，扁平插画风格")

    assert data == PNG_BYTES, "取回的字节应与服务端提供的 PNG 完全一致"

    # 确认拿到的确实是能解码的图片
    img = Image.open(io.BytesIO(data))
    assert img.size == (1024, 576)
    assert img.format == "PNG"


@requires_chromium
def test_generate_then_quality_gate(chrome_with_cdp, server):
    """生图产物应当能通过质量闸——两个模块的衔接是否对得上。"""
    from toutiao_publisher.images import quality

    gen = ChatGPTImageGenerator(
        port=chrome_with_cdp,
        url_contains="fake_chatgpt",
        wait_timeout=30,
        new_chat_url=f"{server}/fake_chatgpt.html",
    )
    data = gen.generate("测试")
    verdict = quality.check(
        data,
        {
            "min_width": 512,
            "min_height": 288,
            "min_bytes": 20480,
            "max_bytes": 10485760,
            "min_stddev": 12.0,
            "allowed_formats": ["PNG", "JPEG", "WEBP"],
        },
    )
    assert verdict.ok, verdict.reason


@requires_chromium
def test_wait_timeout_gives_actionable_error(chrome_with_cdp, server):
    """等不到图时的报错要说清楚下一步查什么。"""
    import httpx

    # 开一个不会产图的空白页
    httpx.put(f"http://127.0.0.1:{chrome_with_cdp}/json/new?about:blank", timeout=5.0)

    gen = ChatGPTImageGenerator(
        port=chrome_with_cdp, url_contains="fake_chatgpt", wait_timeout=4
    )
    # 直接调内部等待逻辑，避免真的发一次提示词
    from toutiao_publisher.images.cdp_chatgpt import CDPSession

    target = find_chatgpt_target(chrome_with_cdp, "fake_chatgpt")
    with CDPSession(target.ws_url) as sess:
        sess.command("Runtime.enable")
        existing = gen._image_urls(sess)
        with pytest.raises(CDPError) as exc:
            gen._wait_for_new_image(sess, existing)

    message = str(exc.value)
    assert "没等到新图" in message
    assert "额度" in message or "拒绝" in message


# ---------- Playwright 发布链路 ----------


@requires_chromium
def test_publish_end_to_end(server, tmp_path):
    """完整发布链路：打开页面 → 输入正文 → 上传配图 → 点发布 → 确认成功。"""
    from toutiao_publisher.publish.toutiao import ToutiaoPublisher

    # 造一个最小可用的 storage_state
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    image = tmp_path / "cover.png"
    image.write_bytes(PNG_BYTES)

    cfg = _FakeConfig(
        tmp_path,
        {
            "publish.storage_state": str(storage),
            "publish.publish_url": f"{server}/fake_toutiao.html",
            "publish.headless": True,
            "publish.type_delay_ms": [1, 3],  # 测试里不需要拟人延时
            "publish.confirm_timeout": 15,
            "run.timeouts.publish": 60,
            "publish.chrome_executable": CHROMIUM,
            "publish.launch_args": _LAUNCH_ARGS,
        },
    )

    publisher = ToutiaoPublisher(cfg)
    content = "这是一条测试微头条。\n\n第二段内容。"
    result = publisher.publish(content=content, image_path=image, dry_run=False)

    assert result.ok
    assert "发布成功" in result.detail


@requires_chromium
def test_publish_dry_run_does_not_publish(server, tmp_path):
    """演练模式必须真的不点发布，并留下预览截图。"""
    from toutiao_publisher.publish.toutiao import ToutiaoPublisher

    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    cfg = _FakeConfig(
        tmp_path,
        {
            "publish.storage_state": str(storage),
            "publish.publish_url": f"{server}/fake_toutiao.html",
            "publish.headless": True,
            "publish.type_delay_ms": [1, 3],
            "run.timeouts.publish": 60,
            "publish.chrome_executable": CHROMIUM,
            "publish.launch_args": _LAUNCH_ARGS,
        },
    )

    result = ToutiaoPublisher(cfg).publish(content="演练内容", image_path=None, dry_run=True)

    assert result.ok
    assert "演练" in result.detail
    assert (tmp_path / "dry-run-preview.png").exists(), "演练模式应留下预览截图"


@requires_chromium
def test_first_publish_checkbox_is_ticked_and_left_alone(server, tmp_path):
    """「头条首发」没勾就补勾，已经勾上就别动。

    第二个断言才是重点：这个勾关系到 72 小时额外激励分成，页面多数时候默认
    就是勾上的，"不看状态直接点一下"会把它取消掉——是纯亏。
    """
    from playwright.sync_api import sync_playwright

    from toutiao_publisher.publish.toutiao import ToutiaoPublisher

    publisher = ToutiaoPublisher(_FakeConfig(tmp_path, {"publish.first_publish": True}))
    checked_js = "!!document.querySelector('label.checkbot-item.byte-checkbox-checked')"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=CHROMIUM, args=_LAUNCH_ARGS, headless=True
        )
        try:
            page = browser.new_page()
            page.goto(f"{server}/fake_toutiao.html", wait_until="domcontentloaded")
            publisher._dismiss_overlays(page)  # 遮罩不关掉，任何点击都点不到

            assert page.evaluate(checked_js) is False, "仿真页应从未勾选状态开始"

            publisher._ensure_first_publish(page)
            assert page.evaluate(checked_js) is True, "未勾选时应补勾上"

            publisher._ensure_first_publish(page)
            assert page.evaluate(checked_js) is True, "已勾选时不能再点，否则等于取消"
        finally:
            browser.close()


@requires_chromium
def test_publish_without_storage_state_raises_login_expired(tmp_path):
    from toutiao_publisher.publish.toutiao import LoginExpired, ToutiaoPublisher

    cfg = _FakeConfig(tmp_path, {"publish.storage_state": str(tmp_path / "nope.json")})
    with pytest.raises(LoginExpired) as exc:
        ToutiaoPublisher(cfg).publish(content="x", image_path=None, dry_run=True)
    assert "login.py" in str(exc.value)


class _FakeConfig:
    """最小 Config 替身，只实现 ToutiaoPublisher 用到的接口。"""

    def __init__(self, state_dir: Path, values: dict):
        self._values = values
        self.state_dir = state_dir
        self.raw = {}

    def get(self, path: str, default=None):
        return self._values.get(path, default)

    def path(self, path: str, default: str = "") -> Path:
        return Path(self._values.get(path, default))
