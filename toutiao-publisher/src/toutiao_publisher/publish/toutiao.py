"""用 Playwright 发布微头条。

登录态存在 state/storage_state.json，首次由 scripts/login.py 扫码生成，
之后自动复用。头条的登录态有效期通常是几周，过期会被识别出来并告警。

两处刻意的选择：
  - headless 默认关闭。有头模式被风控识别的概率低得多，而且出问题能直接看见。
  - 正文逐字符输入并带随机延时。整段 fill() 会被前端富文本编辑器吞掉格式，
    而且瞬间填入几百字是很明显的机器特征。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

log = logging.getLogger(__name__)

# 头条创作中心的 DOM 选择器。改版时优先怀疑这里。
_SELECTORS = {
    "editor": [
        "div.ProseMirror[contenteditable='true']",
        ".syl-editor [contenteditable='true']",
        "div[contenteditable='true']",
    ],
    "image_upload_input": [
        "input[type='file'][accept*='image']",
        "input[type='file']",
    ],
    "publish_button": [
        "button:has-text('发布')",
        "div[class*='publish'] button",
    ],
    # 出现这些说明登录态失效了
    "login_indicator": [
        "text=登录后才能继续",
        "input[placeholder*='手机号']",
        "text=扫码登录",
    ],
    # 发布页会自动弹「发文助手」抽屉，是一层整页遮罩，盖住之后任何点击都落不到页面上
    "overlay": [
        ".byte-drawer-mask",
        ".byte-drawer-wrapper",
    ],
}


def _visible_len(text: str) -> int:
    """去掉所有空白后的字数。

    比对编辑器内容时不能按原样长度算：我们输入的段间空行、编辑器自己的换行处理，
    两边对不上，会把正常情况误判成异常。
    """
    return len("".join(text.split()))


def _editor_text(editor) -> str:
    """取编辑器里真正的正文，剔除占位提示。

    空编辑器里那句"有什么新鲜事想告诉大家？"是 DOM 里的真实节点，不剔掉的话
    既会让"清空后有残留"误报，又会虚增字数把比对容差吃掉。
    """
    return editor.evaluate(
        """el => {
            const clone = el.cloneNode(true);
            clone.querySelectorAll(
                '[data-placeholder],[class*=placeholder],[class*=Placeholder]'
            ).forEach(n => n.remove());
            return (clone.textContent || '').trim();
        }"""
    )


class PublishError(RuntimeError):
    """发布失败。错误信息里必须写明该怎么修。"""


class LoginExpired(PublishError):
    """登录态失效，需要重新跑 scripts/login.py。"""


@dataclass
class PublishResult:
    ok: bool
    post_url: str = ""
    detail: str = ""


class ToutiaoPublisher:
    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.storage_state = cfg.path("publish.storage_state", "state/storage_state.json")
        self.publish_url = cfg.get(
            "publish.publish_url",
            "https://mp.toutiao.com/profile_v4/weitoutiao/publish",
        )
        self.headless = bool(cfg.get("publish.headless", False))
        delay = cfg.get("publish.type_delay_ms", [40, 120])
        self.delay_range = (int(delay[0]), int(delay[1]))
        self.confirm_timeout = int(cfg.get("publish.confirm_timeout", 30))
        self.timeout_ms = int(cfg.get("run.timeouts.publish", 180)) * 1000
        # 指定浏览器可执行文件。留空则用 Playwright 自带的那份。
        self.executable_path = cfg.get("publish.chrome_executable", "") or None
        # 额外启动参数。容器/CI 里以 root 运行时需要 --no-sandbox。
        self.launch_args = list(cfg.get("publish.launch_args", []) or [])

    def _launch(self, pw, headless: bool):
        """统一的浏览器启动入口，让可执行文件和启动参数只配置一次。"""
        kwargs = {"headless": headless, "args": self.launch_args}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        return pw.chromium.launch(**kwargs)

    def check_login(self) -> str:
        """健康检查用：打开发布页，确认登录态还在。"""
        if not self.storage_state.exists():
            raise LoginExpired(
                f"登录态文件不存在：{self.storage_state}\n"
                f"修复：python scripts/login.py"
            )

        with sync_playwright() as pw:
            browser = self._launch(pw, headless=True)
            try:
                ctx = browser.new_context(storage_state=str(self.storage_state))
                page = ctx.new_page()
                page.goto(self.publish_url, timeout=self.timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                self._assert_logged_in(page)
                return "OK — 登录态有效"
            finally:
                browser.close()

    def publish(self, content: str, image_path: Path | None, dry_run: bool) -> PublishResult:
        """发布一条微头条。dry_run=True 时走完全流程但不点发布。"""
        if not self.storage_state.exists():
            raise LoginExpired(
                f"登录态文件不存在：{self.storage_state}\n"
                f"修复：python scripts/login.py（会开浏览器让你扫码，只需一次）"
            )

        with sync_playwright() as pw:
            browser = self._launch(pw, headless=self.headless)
            try:
                ctx = browser.new_context(
                    storage_state=str(self.storage_state),
                    viewport={"width": 1440, "height": 900},
                )
                page = ctx.new_page()
                page.set_default_timeout(self.timeout_ms)

                log.info("打开发布页：%s", self.publish_url)
                page.goto(self.publish_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                self._assert_logged_in(page)
                self._dismiss_overlays(page)
                self._type_content(page, content)

                if image_path:
                    self._upload_image(page, image_path)

                if dry_run:
                    shot = self.cfg.state_dir / "dry-run-preview.png"
                    page.screenshot(path=str(shot), full_page=True)
                    log.info("演练模式：没有点发布。预览截图 → %s", shot)
                    return PublishResult(
                        ok=True, detail=f"演练模式，未真实发布。预览截图：{shot}"
                    )

                return self._click_publish(page)
            finally:
                browser.close()

    # ---------- 内部步骤 ----------

    def _assert_logged_in(self, page: Page) -> None:
        for sel in _SELECTORS["login_indicator"]:
            try:
                if page.locator(sel).first.is_visible(timeout=1500):
                    raise LoginExpired(
                        "头条登录态已失效（发布页跳到了登录界面）。\n"
                        "修复：python scripts/login.py 重新扫码。"
                    )
            except PWTimeout:
                continue
            except LoginExpired:
                raise
            except Exception:  # noqa: BLE001 — 选择器不存在是正常情况
                continue
        log.info("登录态有效")

    def _find(self, page: Page, key: str, timeout: int = 15000):
        """按候选选择器依次找元素，返回第一个可见的 Locator。"""
        for sel in _SELECTORS[key]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout // len(_SELECTORS[key]) + 1000)
                return loc
            except Exception:  # noqa: BLE001 — 试下一个候选
                continue
        raise PublishError(
            f"页面上找不到「{key}」对应的元素，试过这些选择器：{_SELECTORS[key]}\n"
            f"头条改版了的话，更新 {__file__} 里的 _SELECTORS['{key}']。"
        )

    def _dismiss_overlays(self, page: Page) -> None:
        """关掉「发文助手」抽屉。

        发布页会自动弹出它，而它带一层整页遮罩——不关掉的话后面点编辑器、点发布
        全都落在遮罩上，症状是"点了没反应"，而且不报错，极难查。
        """
        for name in ("我知道了", "知道了"):
            try:
                btn = page.get_by_role("button", name=name).first
                if btn.is_visible(timeout=800):
                    btn.click()
                    page.wait_for_timeout(300)
                    log.info("已关闭发文助手抽屉（点「%s」）", name)
            except Exception:  # noqa: BLE001 — 没弹出来是常态
                pass

        # 兜底：按钮文案变了也要能脱身，直接把遮罩节点摘掉
        removed = page.evaluate(
            """(sels) => {
                let n = 0;
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) { el.remove(); n++; }
                }
                return n;
            }""",
            _SELECTORS["overlay"],
        )
        if removed:
            log.info("兜底移除了 %d 个遮罩节点", removed)

    def _type_content(self, page: Page, content: str) -> None:
        editor = self._find(page, "editor")
        editor.click()
        page.wait_for_timeout(500)

        # 清空再输入。发布页会把上次没发的草稿自动恢复到编辑器里，
        # 直接输入等于追加在旧草稿后面，会把上次的残稿一起发出去。
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        leftover = _editor_text(editor)
        if leftover:
            log.warning("清空后编辑器仍有残留：%s", leftover[:60])

        log.info("开始输入正文，%d 字", _visible_len(content))
        lo, hi = self.delay_range
        for ch in content:
            page.keyboard.type(ch, delay=random.uniform(lo, hi))

        page.wait_for_timeout(1000)

        # 正文里的 #标签# 会拉起话题下拉框，它会一直浮在页面上挡住发布按钮，
        # 最后那个标签也停在"未确认插入"的状态。Esc 关掉它，正文本身不受影响。
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        want = _visible_len(content)
        got = _visible_len(_editor_text(editor))
        log.info("正文输入完成，编辑器内实际 %d 字（预期 %d）", got, want)
        # 差太多说明要么有草稿混进来，要么输入被前端吞了，两种都不该带着往下发
        if abs(got - want) > max(15, want // 20):
            raise PublishError(
                f"编辑器内容与预期不符：应为 {want} 字，实际 {got} 字。\n"
                f"可能是草稿没清干净，或者输入被富文本编辑器吞掉了。\n"
                f"为避免发出残缺或混了旧草稿的内容，这里中止。\n"
                f"编辑器现有内容开头：{_editor_text(editor)[:80]}"
            )

    def _upload_image(self, page: Page, image_path: Path) -> None:
        log.info("上传配图：%s", image_path.name)
        try:
            upload = self._find(page, "image_upload_input", timeout=10000)
            upload.set_input_files(str(image_path))
        except PublishError:
            # 有些页面把 file input 藏起来了，locator 认为不可见。直接用 DOM 层接口。
            log.warning("常规方式找不到上传控件，改用隐藏 input 兜底")
            handle = page.query_selector("input[type='file']")
            if handle is None:
                raise PublishError(
                    "页面上完全没有文件上传控件，配图无法上传。\n"
                    "可以先把 config.yaml 里 image.enabled 设为 false，改发纯文字。"
                ) from None
            handle.set_input_files(str(image_path))

        # 等图片上传完成——等到页面里出现预览缩略图
        page.wait_for_timeout(5000)
        log.info("配图上传完成")

    def _click_publish(self, page: Page) -> PublishResult:
        # 上传配图、输入正文的过程里抽屉可能又弹回来，点之前再清一次
        self._dismiss_overlays(page)

        btn = self._find(page, "publish_button")
        log.info("点击发布")
        btn.click()

        # 等待发布结果。成功的标志是页面跳转或出现成功提示。
        deadline = time.monotonic() + self.confirm_timeout
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)

            for marker in ("发布成功", "已发布", "审核中"):
                try:
                    if page.locator(f"text={marker}").first.is_visible(timeout=1000):
                        log.info("发布成功（页面提示：%s）", marker)
                        return PublishResult(ok=True, post_url=page.url, detail=marker)
                except Exception:  # noqa: BLE001
                    continue

            # 有时不弹提示，直接跳走了
            if "publish" not in page.url:
                log.info("发布成功（页面已跳转到 %s）", page.url)
                return PublishResult(ok=True, post_url=page.url, detail="页面跳转")

        shot = self.cfg.state_dir / "publish-timeout.png"
        page.screenshot(path=str(shot), full_page=True)
        raise PublishError(
            f"点了发布，但 {self.confirm_timeout} 秒内没等到成功提示。\n"
            f"内容**可能已经发出去了**，请手动确认，避免重复发布。\n"
            f"当时的页面截图：{shot}"
        )
