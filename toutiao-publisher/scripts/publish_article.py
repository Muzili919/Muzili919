#!/usr/bin/env python3
"""把一篇 Markdown 发成头条「文章」（不是微头条）。

为什么单独一个脚本：微头条和文章是两套编辑器、两个页面、两种收益规则。
平台 2026-07 起把重心移到深度长文（同样阅读量，长文收益是微头条的几倍），
所以长文这条路值得有自己的发布器。

    python scripts/publish_article.py 稿子.md              # 演练，只填不发
    python scripts/publish_article.py 稿子.md --live       # 真发
    python scripts/publish_article.py 稿子.md --title "自定义标题"

Markdown 只做最小转换——头条编辑器不认 markdown 语法，硬贴进去会把
`##` 和 `**` 原样显示出来。所以标记全部剥掉，只保留段落。
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from toutiao_publisher.config import load_config  # noqa: E402

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"

_SELECTORS = {
    "title": ["textarea[placeholder*='文章标题']", "textarea"],
    "body": ["div.ProseMirror[contenteditable='true']", "div.ProseMirror"],
    "publish": ["button:has-text('预览并发布')"],
    "overlay": [".byte-drawer-mask", ".byte-drawer-wrapper"],
}


def md_to_plain(md: str) -> tuple[str, str]:
    """把 markdown 拆成（标题, 正文纯文本）。

    只处理实际会用到的几种标记。头条编辑器不解析 markdown，留着 `##`
    和 `**` 会原样显示在文章里。
    """
    lines = md.splitlines()

    # 第一个 `# ` 当标题
    title = ""
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            lines = lines[i + 1 :]
            break

    # 跳过顶部的"标题备选"之类的草稿区：从第一条 --- 之后开始才是正文
    text = "\n".join(lines)
    if "\n---" in text:
        text = text.split("\n---", 1)[1]

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() in ("---", ""):
            out.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)       # 小标题降成普通段落
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # 去粗体标记
        line = re.sub(r"^[-*]\s+", "", line)          # 去列表符号
        out.append(line)

    # 连续空行压成一个
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return title, body


def _find(page: Page, key: str, timeout: int = 15000):
    for sel in _SELECTORS[key]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout // len(_SELECTORS[key]) + 1000)
            return loc
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(f"页面上找不到「{key}」，试过：{_SELECTORS[key]}")


def _dismiss_overlays(page: Page) -> None:
    """关掉可能盖住整页的抽屉。跟微头条那边同一个坑。"""
    for name in ("我知道了", "知道了"):
        try:
            btn = page.get_by_role("button", name=name).first
            if btn.is_visible(timeout=800):
                btn.click()
                page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
    removed = page.evaluate(
        """(sels) => { let n=0; for (const s of sels)
            for (const el of document.querySelectorAll(s)) { el.remove(); n++; } return n; }""",
        _SELECTORS["overlay"],
    )
    if removed:
        print(f"  兜底移除了 {removed} 个遮罩节点")


def _cover_present(page: Page) -> bool:
    return bool(page.evaluate("() => !!document.querySelector('.article-cover img')"))


def _try_upload_cover_once(page: Page, image: Path) -> bool:
    """传一次封面。成功返回 True。"""
    add = page.locator(
        ".article-cover [class*='add'], .article-cover [class*='upload'], "
        ".article-cover [class*='plus']"
    ).first
    add.click(timeout=8000)

    # 等弹窗真的渲染出来再动手。上一版没等，赶上渲染慢的时候
    # expect_file_chooser 就空等超时，然后退回去塞 input——而那个 input
    # 根本不是它用的那个，于是"没报错但也没传上"。
    local_btn = page.get_by_role("button", name="本地上传").first
    local_btn.wait_for(state="visible", timeout=10000)

    # 「本地上传」点了会拉起系统选择框，必须用 filechooser 接；
    # 直接往 input[type=file] 里塞（微头条配图那套）在这个弹窗里不生效。
    with page.expect_file_chooser(timeout=10000) as fc:
        local_btn.click()
    fc.value.set_files(str(image))

    # 等上传+可能的裁剪界面
    for _ in range(20):
        page.wait_for_timeout(1000)
        if _cover_present(page):
            return True
        for name in ("确定", "完成", "确认", "保存", "使用"):
            try:
                btn = page.get_by_role("button", name=name).last
                if btn.is_visible(timeout=600):
                    btn.click()
                    print(f"    点了「{name}」")
                    page.wait_for_timeout(1500)
                    break
            except Exception:  # noqa: BLE001
                continue
    return _cover_present(page)


def _upload_cover(page: Page, image: Path, attempts: int = 3) -> bool:
    """传封面，失败重试。

    「展示封面」在 DOM 上那格带 required class，是硬性必填——空着点发布会被
    前端校验拦下，页面停在原地，看起来像"点了没反应"。
    实测这条链路不稳定：同样的代码同一天内一次成功一次失败，所以要重试。
    """
    print("  上传封面…")
    for i in range(1, attempts + 1):
        try:
            if _try_upload_cover_once(page, image):
                print(f"  封面已设置（第 {i} 次尝试）")
                return True
            print(f"  第 {i} 次没成，重试")
        except Exception as exc:  # noqa: BLE001
            print(f"  第 {i} 次异常（{type(exc).__name__}: {str(exc)[:60]}），重试")
        # 关掉可能还开着的弹窗，免得下一次点不到「+」
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass
    print("  ❌ 封面上传失败")
    return False


def publish(md_path: Path, live: bool, title_override: str = "", cover: Path | None = None) -> int:
    cfg = load_config()
    title, body = md_to_plain(md_path.read_text(encoding="utf-8"))
    if title_override:
        title = title_override
    if not title:
        print("❌ 稿子里没有 `# 标题`，也没传 --title")
        return 1
    if len(title) > 30:
        print(f"❌ 标题 {len(title)} 字，超过头条上限 30 字：{title}")
        return 1

    cn = len(re.findall(r"[一-鿿]", body))
    print(f"标题（{len(title)} 字）：{title}")
    print(f"正文：{cn} 个中文字，{len(body)} 字符")
    print("模式：" + ("🔴 真实发布" if live else "演练（只填不发）"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(
                storage_state=str(cfg.path("publish.storage_state")),
                viewport={"width": 1500, "height": 1000},
            )
            page = ctx.new_page()
            page.set_default_timeout(120000)

            print(f"打开 {PUBLISH_URL}")
            page.goto(PUBLISH_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            _dismiss_overlays(page)

            # 标题。必须逐字校验——逐字符输入会**丢字**，而且丢得不固定：
            # 同一个标题连续几次跑出来分别是 21/23、20/23 字。丢的要是关键字，
            # 标题就成了病句（实测把"还是翻车了"吞成"还是车了"），而这东西是
            # 读者第一眼看到的。
            t = _find(page, "title")
            for attempt in range(1, 4):
                t.click()
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)
                t.fill(title)
                page.wait_for_timeout(500)
                got_title = t.input_value()
                if got_title == title:
                    print(f"  标题已填并校验通过（{len(title)} 字）")
                    break
                print(f"  第 {attempt} 次标题对不上（页面是「{got_title}」），重填")
            else:
                print(f"❌ 标题填了三次都不对，中止。最后一次页面上是：{t.input_value()}")
                return 1

            # 正文。先清空——编辑器会自动恢复上次草稿，不清就是往旧稿后面追加
            b = _find(page, "body")
            b.click()
            page.wait_for_timeout(400)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(400)

            print(f"  开始输入正文（约 {cn} 字，要一会儿）")
            started = time.monotonic()
            for para in body.split("\n\n"):
                para = para.strip()
                if not para:
                    continue
                page.keyboard.type(para, delay=random.uniform(8, 22))
                page.keyboard.press("Enter")
            print(f"  正文输入完成，用时 {time.monotonic() - started:.0f} 秒")

            page.wait_for_timeout(1500)
            got = page.evaluate(
                """(sel) => { const el=document.querySelector(sel);
                     return el ? (el.textContent||'').replace(/\\s/g,'').length : -1; }""",
                _SELECTORS["body"][0],
            )
            print(f"  编辑器内实际 {got} 字（预期约 {len(''.join(body.split()))}）")

            cover_ok = False
            if cover:
                cover_ok = _upload_cover(page, cover)
            else:
                print("  ⚠️ 没给封面。展示封面是必填项，不传发不出去（用 --cover 指定）")

            shot = cfg.state_dir / ("article-live.png" if live else "article-dry.png")
            page.screenshot(path=str(shot), full_page=True)
            print(f"  截图 → {shot}")

            if not live:
                print("✅ 演练结束，没有发布。确认无误后加 --live。")
                return 0

            # 封面没传上就不能往下走。硬发的结果是前端校验拦下、页面停在原地，
            # 而脚本会以为自己点过发布了——白跑一趟还容易误判成"发出去了"。
            if not cover_ok:
                print("❌ 封面是必填项但没传上，中止发布（稿子还在编辑器里，草稿已自动保存）。")
                return 1

            _dismiss_overlays(page)
            _find(page, "publish").click()
            print("  已点「预览并发布」")

            # 这里没有弹窗。点完之后**原来那个按钮自己改名**成「确认发布」，
            # 大约 3 秒后才变。所以要等这个确切的名字出现，不能泛泛去找含「发布」
            # 的按钮——"预览并发布"本身也含「发布」，会匹配到还没变的旧按钮。
            confirmed = False
            for _ in range(20):
                page.wait_for_timeout(1000)
                btn = page.locator("button:has-text('确认发布')").first
                if btn.count() and btn.is_visible():
                    btn.click()
                    print("  已点「确认发布」")
                    confirmed = True
                    break
            if not confirmed:
                page.screenshot(path=str(cfg.state_dir / "article-no-confirm.png"))
                print("❌ 等了 20 秒没等到「确认发布」按钮，中止。")
                return 1

            # 成功提示是「提交成功」，而且**只闪 1 秒左右**就没了，之后表单会重置、
            # 按钮变回「预览并发布」——看起来跟"没发出去"一模一样。
            # 我最初找的是"发布成功/审核中/已发布"，一个都匹配不上，于是每次都报
            # "没看到成功提示"，照这个判断重发，结果同一篇发了三遍。
            # 所以这里必须高频轮询，而且认对词。
            ok = False
            for _ in range(20):
                page.wait_for_timeout(500)
                txt = page.evaluate("document.body.innerText")
                if any(k in txt for k in ("提交成功", "发布成功", "审核中")):
                    ok = True
                    break

            final = cfg.state_dir / "article-published.png"
            page.screenshot(path=str(final), full_page=True)
            if ok:
                print(f"✅ 看到「提交成功」，截图 → {final}")
                print("   仍建议去后台作品管理确认一次。")
            else:
                print(f"⚠️ 没捕捉到成功提示，截图 → {final}")
                print("   ⚠️ 这**不等于**没发出去——提示只闪一秒，很容易错过。")
                print("   ⚠️ 重发之前**必须**先去后台作品管理查，否则会发重。")
            return 0 if ok else 1
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="把 Markdown 发成头条文章")
    ap.add_argument("markdown", type=Path)
    ap.add_argument("--live", action="store_true", help="真发布（不加就是演练）")
    ap.add_argument("--title", default="", help="覆盖稿子里的标题")
    ap.add_argument("--cover", type=Path, help="封面图（展示封面是必填项）")
    args = ap.parse_args()
    if not args.markdown.exists():
        print(f"找不到 {args.markdown}")
        return 1
    if args.cover and not args.cover.exists():
        print(f"找不到封面 {args.cover}")
        return 1
    return publish(args.markdown, args.live, args.title, args.cover)


if __name__ == "__main__":
    sys.exit(main())
