"""LLM 客户端封装。

沿用 eq-trainer 里那套：OpenAI 兼容 SDK，默认指向 DeepSeek，换供应商只改 .env。
带重试 + JSON 容错——模型偶尔会在 JSON 外面套一层 ```json 代码块。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

log = logging.getLogger(__name__)

MAX_RETRIES = 3


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 90.0):
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat_json(self, system: str, user: str, temperature: float = 0.7) -> dict[str, Any]:
        """要求模型返回 JSON 对象。失败重试，逐次降温度提高稳定性。"""
        last_error = ""
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=max(0.2, temperature - attempt * 0.2),
                    response_format={"type": "json_object"},
                )
                text = resp.choices[0].message.content or ""
                parsed = _extract_json(text)
                if parsed is not None:
                    return parsed
                last_error = f"返回的不是合法 JSON：{text[:200]}"
            except Exception as exc:  # noqa: BLE001 — 网络/限流都在这里重试
                last_error = str(exc)
            log.warning("LLM 调用失败（第 %d/%d 次）：%s", attempt + 1, MAX_RETRIES, last_error)

        raise LLMError(f"LLM 连续 {MAX_RETRIES} 次调用失败。最后一次错误：{last_error}")


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。先直解，再剥代码块，最后正则兜底。"""
    text = text.strip()
    for candidate in (text, *_fenced(text), *_braced(text)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _fenced(text: str) -> list[str]:
    return [m.group(1) for m in _FENCE_RE.finditer(text)]


def _braced(text: str) -> list[str]:
    m = _OBJ_RE.search(text)
    return [m.group(0)] if m else []
