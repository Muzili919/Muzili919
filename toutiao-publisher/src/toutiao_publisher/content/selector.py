"""选题：从 RSS 候选池里挑一条最值得写的。

去重在两层做：
  1. 硬去重——URL 和标题精确匹配过的直接从候选池剔除，不浪费 token
  2. 软去重——把近期发过的标题给模型看，让它避开同质选题
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..sources.rss import Entry
from .llm import LLMClient

log = logging.getLogger(__name__)

_SYSTEM = """你是一位中文科技自媒体的选题编辑，账号定位如下：

领域：{domain}
读者：{audience}

你的任务是从候选新闻里挑出**一条**最适合写成微头条的，并说明角度。

判断标准，按重要性排序：
1. 和账号领域强相关，读者会觉得"这跟我有关"
2. 有具体信息量（数据、事件、产品），不是空泛观点
3. 有可展开的角度，不是一句话说完的快讯
4. 和"近期已发布"列表里的选题不重复、不同质

严格返回 JSON：
{{"index": <候选编号，整数>, "topic": "<8-20字的选题概括>", "angle": "<一句话说明从什么角度写>", "reason": "<为什么选它>"}}

如果所有候选都不合适（比如全部离题或全部与近期重复），返回：
{{"index": -1, "topic": "", "angle": "", "reason": "<说明原因>"}}"""


@dataclass
class Selection:
    entry: Entry
    topic: str
    angle: str
    reason: str


class NoSuitableTopic(RuntimeError):
    """候选池里没有合适选题。属于正常情况，不该告警成故障。"""


def select(
    llm: LLMClient,
    entries: list[Entry],
    published: list[dict[str, Any]],
    domain: str,
    audience: str,
) -> Selection:
    """挑一条选题。"""
    fresh = _drop_duplicates(entries, published)
    if not fresh:
        raise NoSuitableTopic("候选条目在去重后一条不剩，今天的 RSS 内容全都发过了。")

    log.info("候选池 %d 条，去重后 %d 条", len(entries), len(fresh))

    candidates = "\n".join(e.to_prompt_line(i) for i, e in enumerate(fresh))
    recent = "\n".join(f"- {p['title']}" for p in published[-20:]) or "（无）"

    result = llm.chat_json(
        system=_SYSTEM.format(domain=domain, audience=audience),
        user=f"候选新闻：\n{candidates}\n\n近期已发布（避免重复）：\n{recent}",
        temperature=0.6,
    )

    index = result.get("index", -1)
    if not isinstance(index, int) or index < 0:
        raise NoSuitableTopic(result.get("reason") or "模型认为没有合适选题")
    if index >= len(fresh):
        raise NoSuitableTopic(f"模型返回的编号 {index} 超出候选范围（0-{len(fresh) - 1}）")

    chosen = fresh[index]
    log.info("选题：%s（来源：%s）", result.get("topic"), chosen.source)
    return Selection(
        entry=chosen,
        topic=str(result.get("topic") or chosen.title),
        angle=str(result.get("angle") or ""),
        reason=str(result.get("reason") or ""),
    )


def _drop_duplicates(entries: list[Entry], published: list[dict[str, Any]]) -> list[Entry]:
    """剔除 URL 或标题已发过的条目。"""
    seen_urls = {p.get("source_url", "") for p in published if p.get("source_url")}
    seen_titles = {_norm(p.get("title", "")) for p in published if p.get("title")}

    return [
        e for e in entries if e.url not in seen_urls and _norm(e.title) not in seen_titles
    ]


def _norm(title: str) -> str:
    """标题归一化，用于粗粒度比对。"""
    return "".join(title.lower().split())
