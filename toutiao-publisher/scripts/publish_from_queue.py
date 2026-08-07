#!/usr/bin/env python3
"""从待发队列里取一篇发布。给定时任务用。

**为什么是队列而不是全自动生成**：内容生成这一步已经被证明会编——从一行标题
编出"社区学院报名排长队"，从废止的条文写出一整篇科普。这类错误在装修这种
靠专业可信度的领域是致命的，而且事后很难挽回。

所以这套的分工是：
  - **写作留在人手里**（在会话里查证、给用户过目），审过的稿子放进 queue/
  - **发布交给定时任务**，它只发已经在队列里的东西，一篇都不会自己造

队列空了不是故障，但会发邮件提醒——否则账号会像 2026-08-02 那样悄无声息地
断更，而没有任何人发现。

用法：
    python scripts/publish_from_queue.py            # 演练
    python scripts/publish_from_queue.py --live     # 真发
    python scripts/publish_from_queue.py --list     # 看队列

队列约定：
    queue/2026-08-03-层高与净高.md      稿子，第一行 `# 标题` 就是文章标题
    queue/2026-08-03-层高与净高.png     同名封面（必需，头条封面是必填项）
按文件名排序，先进先发。发完移到 queue/published/。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from publish_article import md_to_plain, publish  # noqa: E402

from toutiao_publisher.config import load_config  # noqa: E402
from toutiao_publisher.notify import build_notifier  # noqa: E402
from toutiao_publisher.publish.toutiao import ToutiaoPublisher  # noqa: E402

QUEUE = ROOT / "queue"
DONE = QUEUE / "published"


def publish_weitoutiao(md_path: Path, cover: Path, live: bool) -> int:
    """把稿子当微头条发。

    为什么要有这条路：实测同一份内容，做成文章 338 展现只换来 1 次阅读，
    做成微头条 326 展现换来 14 次——差 14 倍。长文单价再高（43.2 vs 7.8
    元/万阅读），乘以 1 次阅读还是零。这个账号当前权重下，微头条才是
    唯一有人看的形态。
    """
    cfg = load_config()
    body = md_path.read_text(encoding="utf-8")
    # 去掉草稿区（第一条 --- 之前的备选标题之类）
    if "\n---" in body:
        body = body.split("\n---", 1)[1]
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", body).strip()

    n = len("".join(text.split()))
    print(f"微头条，{n} 字，配图 {cover.name}")
    if not (100 <= n <= 900):
        print(f"❌ 字数 {n} 不在 100-900 区间，微头条太短没信息量、太长会被折叠")
        return 1

    if not live:
        print("✅ 演练：内容和配图都就位，没有发布。")
        return 0

    result = ToutiaoPublisher(cfg).publish(content=text, image_path=cover, dry_run=False)
    print("结果:", "成功" if result.ok else "失败", "|", result.detail)
    return 0 if result.ok else 1


def queued() -> list[Path]:
    QUEUE.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in QUEUE.glob("*.md") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description="从待发队列发一篇")
    ap.add_argument("--live", action="store_true", help="真发布")
    ap.add_argument("--list", action="store_true", help="只列出队列")
    args = ap.parse_args()

    items = queued()

    if args.list:
        if not items:
            print("队列是空的")
            return 0
        print(f"队列里有 {len(items)} 篇：")
        for i, p in enumerate(items, 1):
            title, _ = md_to_plain(p.read_text(encoding="utf-8"))
            cover = p.with_suffix(".png")
            kind = "文章" if title else "微头条"
            print(f"  {i}. [{kind}] {p.name}")
            if title:
                print(f"     标题：{title}")
            print(f"     配图：{'✅' if cover.exists() else '❌ 缺失，发不出去'}")
        return 0

    cfg = load_config()
    notifier = build_notifier(cfg.secrets)

    if not items:
        print("队列空了，没东西可发。")
        notifier.send(
            "头条待发队列空了",
            "今天没有可发布的稿子，账号会断更。\n\n"
            f"往 `{QUEUE}` 放 `.md` + 同名 `.png` 就能续上。\n\n"
            "（这条提醒的存在，是因为断更本身不会有任何报错。）",
        )
        return 2  # 2 = 正常跳过，不是故障

    item = items[0]
    cover = item.with_suffix(".png")
    title, _ = md_to_plain(item.read_text(encoding="utf-8"))

    print(f"取队首：{item.name}")
    if not cover.exists():
        msg = f"{item.name} 没有同名图 {cover.name}。规则是每条内容都必须带图，发不出去。"
        print(f"❌ {msg}")
        notifier.send("头条发布失败：缺图", msg)
        return 1

    # 有 `# 标题` 就当文章发，没有就当微头条发。不额外发明语法——
    # 微头条本来就没有标题这个字段。
    if title:
        rc = publish(item, live=args.live, title_override="", cover=cover)
    else:
        rc = publish_weitoutiao(item, cover, live=args.live)

    if rc == 0 and args.live:
        DONE.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        item.rename(DONE / f"{stamp}-{item.name}")
        cover.rename(DONE / f"{stamp}-{cover.name}")
        print(f"已移入 {DONE}")
        # 微头条没有标题字段，用文件名兜底，别发一封标题是空的邮件
        label = title or item.stem
        notifier.send("头条已发布", f"**{label}**\n\n来自待发队列：`{item.name}`")
    elif rc != 0:
        notifier.send(
            "头条发布失败",
            f"稿子：`{item.name}`\n\n没有发出去，稿子仍留在队列里。\n"
            f"⚠️ 重试之前先去后台作品管理确认，避免重复发布。",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
