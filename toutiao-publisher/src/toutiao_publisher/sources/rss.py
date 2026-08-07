"""RSS 抓取与归一化。

单个源挂掉不能拖垮整批——每个源独立 try，失败只记日志。
只要最终拿到候选条目就继续走，一条都没有才算失败。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

log = logging.getLogger(__name__)

# 只要最近这么多小时内的条目，太旧的没有时效性
_MAX_AGE_HOURS = 72


@dataclass
class Entry:
    """归一化后的候选条目。"""

    title: str
    url: str
    summary: str
    source: str
    weight: int
    published_at: datetime | None

    def to_prompt_line(self, index: int) -> str:
        """喂给 LLM 的紧凑表示。摘要截断，省 token。"""
        summary = self.summary[:180].replace("\n", " ")
        return f"[{index}] {self.title}\n    来源：{self.source} | 摘要：{summary}"


def fetch_all(sources: list[dict[str, Any]], limit: int, timeout: int = 30) -> list[Entry]:
    """抓取全部源，返回按权重和时间排序的候选条目。"""
    entries: list[Entry] = []
    failed: list[str] = []

    for src in sources:
        name = src.get("name", src["url"])
        try:
            got = _fetch_one(src, timeout)
            entries.extend(got)
            log.info("RSS 源 %s：%d 条", name, len(got))
        except Exception as exc:  # noqa: BLE001 — 单源失败不中断整批
            failed.append(name)
            log.warning("RSS 源 %s 抓取失败（已跳过）：%s", name, exc)

    if failed:
        log.warning("共 %d/%d 个源失败：%s", len(failed), len(sources), ", ".join(failed))

    if not entries:
        raise RuntimeError(
            f"全部 {len(sources)} 个 RSS 源都没拿到条目。检查网络，或 config/sources.json 里的地址是否失效。"
        )

    # 按 (权重, 发布时间) 倒序，权重高的优先，同权重取新的
    entries.sort(
        key=lambda e: (e.weight, e.published_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return entries[:limit]


def _fetch_one(src: dict[str, Any], timeout: int) -> list[Entry]:
    # feedparser 不直接支持 timeout，用 socket 全局超时兜住
    import socket

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        feed = feedparser.parse(src["url"])
    finally:
        socket.setdefaulttimeout(old_timeout)

    # bozo 表示解析有问题，但很多源仍能拿到可用条目，所以只警告不中断
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"解析失败：{feed.get('bozo_exception', '未知错误')}")

    name = src.get("name", src["url"])
    weight = int(src.get("weight", 1))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_HOURS)

    out: list[Entry] = []
    for raw in feed.entries:
        title = _clean(raw.get("title", ""))
        url = raw.get("link", "").strip()
        if not title or not url:
            continue

        published = _parse_published(raw)
        if published and published < cutoff:
            continue

        out.append(
            Entry(
                title=title,
                url=url,
                summary=_clean(raw.get("summary", "") or raw.get("description", "")),
                source=name,
                weight=weight,
                published_at=published,
            )
        )
    return out


def _parse_published(raw: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = raw.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """去 HTML 标签、压缩空白。"""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()
