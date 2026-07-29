#!/usr/bin/env python3
"""首次登录头条，把登录态存下来。只需要跑一次，之后自动复用。

用法：
    python scripts/login.py

会开一个浏览器窗口，你扫码登录完，回终端按回车即可。
登录态存到 state/storage_state.json（已在 .gitignore 里，不会误提交）。

登录态失效后（通常几周）再跑一次即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from toutiao_publisher.config import ConfigError, load_config  # noqa: E402

LOGIN_URL = "https://mp.toutiao.com/auth/page/login"


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        return 1

    out = cfg.path("publish.storage_state", "state/storage_state.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    print("正在打开浏览器……")
    with sync_playwright() as pw:
        # 必须有头模式——要你手动扫码
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(LOGIN_URL)

        print()
        print("=" * 60)
        print("  请在打开的浏览器窗口里完成登录（扫码或手机号）")
        print("  登录成功、能看到创作中心首页之后，回到这里按回车")
        print("=" * 60)
        input("\n登录完成后按回车继续...")

        # 简单验证：登录成功后 URL 不会还停在 login 页
        if "login" in page.url:
            print(f"\n⚠️  当前页面仍在登录页（{page.url}）。")
            confirm = input("确定已经登录成功了吗？(y/N): ").strip().lower()
            if confirm != "y":
                print("已取消，登录态未保存。")
                browser.close()
                return 1

        ctx.storage_state(path=str(out))
        browser.close()

    print(f"\n✅ 登录态已保存到：{out}")
    print("现在可以跑：python -m toutiao_publisher doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
