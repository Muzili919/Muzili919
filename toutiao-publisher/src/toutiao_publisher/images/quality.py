"""图片质量闸。

ChatGPT 偶尔会返回错误占位图、半透明空白图、或者尺寸完全不对的图。
这些图发出去比没有图更糟。这里在上传前拦一道。

判定不合格时**不抛异常**，而是返回原因让上层决定——
配图失败不该让整条发布流程失败，纯文字微头条照样能发。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageStat

log = logging.getLogger(__name__)


@dataclass
class QualityResult:
    ok: bool
    reason: str = ""
    width: int = 0
    height: int = 0
    fmt: str = ""
    size_kb: float = 0.0
    stddev: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.fmt} {self.width}x{self.height} "
            f"{self.size_kb:.1f}KB stddev={self.stddev:.1f}"
        )


def check(data: bytes, rules: dict[str, Any]) -> QualityResult:
    """按配置里的 image.quality 规则校验图片。"""
    size_kb = len(data) / 1024

    min_bytes = int(rules.get("min_bytes", 0))
    max_bytes = int(rules.get("max_bytes", 0))

    if min_bytes and len(data) < min_bytes:
        return QualityResult(
            ok=False,
            reason=f"文件太小（{size_kb:.1f}KB < {min_bytes / 1024:.1f}KB），基本可以断定是错误占位图",
            size_kb=size_kb,
        )
    if max_bytes and len(data) > max_bytes:
        return QualityResult(
            ok=False,
            reason=f"文件太大（{size_kb:.1f}KB > {max_bytes / 1024:.1f}KB），超出上传限制",
            size_kb=size_kb,
        )

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 — 任何解码失败都算不合格
        return QualityResult(ok=False, reason=f"不是合法图片，解码失败：{exc}", size_kb=size_kb)

    fmt = (img.format or "").upper()
    width, height = img.size

    allowed = [f.upper() for f in rules.get("allowed_formats", [])]
    if allowed and fmt not in allowed:
        return QualityResult(
            ok=False,
            reason=f"格式 {fmt} 不在允许列表 {allowed} 里",
            width=width, height=height, fmt=fmt, size_kb=size_kb,
        )

    min_w = int(rules.get("min_width", 0))
    min_h = int(rules.get("min_height", 0))
    if width < min_w or height < min_h:
        return QualityResult(
            ok=False,
            reason=f"尺寸太小（{width}x{height} < {min_w}x{min_h}）",
            width=width, height=height, fmt=fmt, size_kb=size_kb,
        )

    stddev = _stddev(img)
    min_stddev = float(rules.get("min_stddev", 0))
    if min_stddev and stddev < min_stddev:
        return QualityResult(
            ok=False,
            reason=(
                f"画面接近纯色（像素标准差 {stddev:.1f} < {min_stddev}），"
                f"多半是空白图或渲染失败"
            ),
            width=width, height=height, fmt=fmt, size_kb=size_kb, stddev=stddev,
        )

    result = QualityResult(
        ok=True, width=width, height=height, fmt=fmt, size_kb=size_kb, stddev=stddev
    )
    log.info("图片通过质量闸：%s", result.describe())
    return result


def _stddev(img: Image.Image) -> float:
    """整图像素标准差。纯色/空白图会非常接近 0。

    转灰度后算，避免彩色图三通道各自标准差不好比较。
    缩到 256px 以内再算，大图省时间且不影响判断。
    """
    try:
        gray = img.convert("L")
        gray.thumbnail((256, 256))
        return float(ImageStat.Stat(gray).stddev[0])
    except Exception as exc:  # noqa: BLE001 — 算不出来就不拦，交给其他规则
        log.warning("计算像素标准差失败，跳过该项检查：%s", exc)
        return float("inf")
