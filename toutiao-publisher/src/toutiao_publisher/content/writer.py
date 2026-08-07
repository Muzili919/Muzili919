"""成文：把选题写成一条微头条。

字数是硬约束——微头条太短没信息量，太长会被折叠。写完本地校验，
不达标就带着具体反馈重写一次，而不是直接放弃。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .llm import LLMClient
from .selector import Selection

log = logging.getLogger(__name__)

_SYSTEM = """你是一位中文科技自媒体作者，为"微头条"写短文。

账号定位：{domain}
读者：{audience}
语气：{tone}

写作要求：
1. 字数严格控制在 {min_chars}-{max_chars} 字之间（中文字符计）
2. 开头第一句要有钩子，让人想读下去，但不要用"震惊""炸了"这类标题党词
3. 中间给具体信息：是什么、数据多少、和读者有什么关系
4. 结尾要砸一下或留余味：一句话点透的金句、不做总结的留白、或者一个冷事实。
   禁止用"你觉得是A还是B""你怎么看"这类反问收尾——每个 AI 都这么收，一眼假
5. 不要用 Markdown 语法（微头条不渲染），不要写小标题
6. 段落之间空一行，每段 2-4 句，手机上好读
7. 结尾可以带 1-3 个话题标签，格式 #标签#

严格返回 JSON：
{{"title": "<15-30字的标题>", "content": "<正文全文>", "image_prompt": "<描述这篇文章配图应该画什么，中文，20-40字>"}}"""


@dataclass
class Article:
    title: str
    content: str
    image_prompt: str

    @property
    def char_count(self) -> int:
        """中文字符数，不含空白。"""
        return len("".join(self.content.split()))


def write(
    llm: LLMClient,
    selection: Selection,
    domain: str,
    audience: str,
    tone: str,
    min_chars: int,
    max_chars: int,
) -> Article:
    """写一篇微头条。字数不合格会重写一次。"""
    system = _SYSTEM.format(
        domain=domain,
        audience=audience,
        tone=tone,
        min_chars=min_chars,
        max_chars=max_chars,
    )
    user = (
        f"选题：{selection.topic}\n"
        f"角度：{selection.angle}\n\n"
        f"素材标题：{selection.entry.title}\n"
        f"素材摘要：{selection.entry.summary[:600]}\n"
        f"素材来源：{selection.entry.source}"
    )

    article = _generate(llm, system, user)

    # 字数校验：不合格带着反馈重写一次
    if not min_chars <= article.char_count <= max_chars:
        log.warning(
            "字数 %d 不在 %d-%d 区间，重写一次", article.char_count, min_chars, max_chars
        )
        direction = "扩写" if article.char_count < min_chars else "精简"
        retry_user = (
            f"{user}\n\n"
            f"上一版正文（{article.char_count} 字）不合格，请{direction}到 "
            f"{min_chars}-{max_chars} 字，保持原有观点和结构：\n\n{article.content}"
        )
        article = _generate(llm, system, retry_user)

    if not min_chars <= article.char_count <= max_chars:
        # 重写后仍不合格就接受——字数是软目标，硬卡会让整条流程失败，不值得
        log.warning("重写后字数 %d 仍不在区间内，按现状发布", article.char_count)

    log.info("成文完成：%s（%d 字）", article.title, article.char_count)
    return article


def _generate(llm: LLMClient, system: str, user: str) -> Article:
    result = llm.chat_json(system=system, user=user, temperature=0.8)

    content = str(result.get("content") or "").strip()
    if not content:
        raise RuntimeError(f"模型没有返回正文内容。原始返回：{result}")

    return Article(
        title=str(result.get("title") or "").strip(),
        content=content,
        image_prompt=str(result.get("image_prompt") or "").strip(),
    )
