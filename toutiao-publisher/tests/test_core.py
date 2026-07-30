"""核心纯逻辑的测试。

只测不依赖外部服务的部分——质量闸、去重、状态存储、JSON 抽取、钉钉签名。
浏览器和 LLM 相关的部分靠 `doctor` 命令做真实体检，不在这里 mock。
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toutiao_publisher.content.llm import _extract_json  # noqa: E402
from toutiao_publisher.content.selector import _drop_duplicates, _norm  # noqa: E402
from toutiao_publisher.images import quality  # noqa: E402
from toutiao_publisher.notify import Notifier  # noqa: E402
from toutiao_publisher.sources.rss import Entry, _clean  # noqa: E402
from toutiao_publisher.state import (  # noqa: E402
    STATUS_FAILED,
    STATUS_SUCCESS,
    RunRecord,
    StateStore,
)

RULES = {
    "min_width": 512,
    "min_height": 288,
    "min_bytes": 20480,
    "max_bytes": 10485760,
    "min_stddev": 12.0,
    "allowed_formats": ["PNG", "JPEG", "WEBP"],
}


def _png(width: int, height: int, noisy: bool = True) -> bytes:
    """造一张测试图。noisy=False 生成纯色图，用来验证空白检测。

    有内容的图 = 大色块 + 细节噪声，两者缺一不可：
      - 大色块决定缩图后的标准差（质量闸缩到 256px 才算，细噪点会被平均掉）
      - 细节噪声决定压缩后的体积（纯色块 PNG 能压到 3KB，过不了体积闸）
    真实插画两样都有，这样造才测得准。
    """
    import random

    img = Image.new("RGB", (width, height), (20, 20, 20))
    if noisy:
        draw = ImageDraw.Draw(img)
        block = max(width, height) // 8
        for row, y in enumerate(range(0, height, block)):
            for col, x in enumerate(range(0, width, block)):
                shade = 255 if (row + col) % 2 == 0 else 30
                draw.rectangle([x, y, x + block, y + block], fill=(shade, shade, shade))

        # 叠一层轻噪声，模拟真实图片的纹理，让 PNG 压不动
        rnd = random.Random(42)
        pixels = img.load()
        for x in range(width):
            for y in range(height):
                r, g, b = pixels[x, y]
                jitter = rnd.randint(-18, 18)
                pixels[x, y] = (
                    max(0, min(255, r + jitter)),
                    max(0, min(255, g + jitter)),
                    max(0, min(255, b + jitter)),
                )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------- 质量闸 ----------


def test_quality_accepts_good_image():
    result = quality.check(_png(1024, 576), RULES)
    assert result.ok, result.reason
    assert (result.width, result.height) == (1024, 576)
    assert result.fmt == "PNG"


def test_quality_rejects_tiny_file():
    result = quality.check(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, RULES)
    assert not result.ok
    assert "太小" in result.reason


def test_quality_rejects_small_dimensions():
    # 图要够大不触发 min_bytes，但尺寸不达标
    result = quality.check(_png(400, 200), {**RULES, "min_bytes": 0})
    assert not result.ok
    assert "尺寸太小" in result.reason


def test_quality_rejects_blank_image():
    """纯色图必须被拦下——这是生图失败最典型的产物。"""
    result = quality.check(_png(1024, 576, noisy=False), {**RULES, "min_bytes": 0})
    assert not result.ok
    assert "纯色" in result.reason


def test_quality_rejects_non_image():
    result = quality.check(b"this is definitely not an image" * 1000, RULES)
    assert not result.ok
    assert "不是合法图片" in result.reason


# ---------- 选题去重 ----------


def _entry(title: str, url: str) -> Entry:
    return Entry(
        title=title, url=url, summary="", source="test", weight=1, published_at=None
    )


def test_dedupe_by_url():
    entries = [_entry("A", "http://a"), _entry("B", "http://b")]
    published = [{"title": "别的标题", "source_url": "http://a"}]
    assert [e.title for e in _drop_duplicates(entries, published)] == ["B"]


def test_dedupe_by_title_ignores_whitespace_and_case():
    entries = [_entry("Hello  World", "http://x")]
    published = [{"title": "hello world", "source_url": ""}]
    assert _drop_duplicates(entries, published) == []


def test_norm_strips_whitespace():
    assert _norm("  A B\tC ") == "abc"


# ---------- LLM JSON 抽取 ----------


@pytest.mark.parametrize(
    "raw",
    [
        '{"index": 1}',
        '```json\n{"index": 1}\n```',
        '好的，这是结果：\n```\n{"index": 1}\n```',
        '前面一堆废话 {"index": 1} 后面还有',
    ],
)
def test_extract_json_handles_wrappers(raw):
    assert _extract_json(raw) == {"index": 1}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("完全不是 JSON") is None


def test_extract_json_rejects_bare_array():
    """顶层数组不是我们要的对象，应当返回 None。"""
    assert _extract_json("[1, 2, 3]") is None


# ---------- 状态存储 ----------


def test_state_store_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    record = RunRecord(started_at=datetime.now().isoformat(timespec="seconds"))
    record.status = STATUS_SUCCESS
    record.title = "测试标题"
    record.add_step("抓取 RSS", ok=True, duration_s=1.5)
    store.save_run(record)

    runs = store.load_runs()
    assert len(runs) == 1
    assert runs[0]["title"] == "测试标题"
    assert runs[0]["steps"][0]["name"] == "抓取 RSS"


def _real_run(started_at: str, status: str = STATUS_SUCCESS) -> RunRecord:
    """一条"真发出去了"的记录。dry_run 默认是 True，测真实发布必须显式关掉。"""
    rec = RunRecord(started_at=started_at, dry_run=False)
    rec.status = status
    return rec


def test_last_success_ignores_failures(tmp_path):
    store = StateStore(tmp_path)

    store.save_run(_real_run(datetime.now().isoformat(timespec="seconds"), STATUS_FAILED))
    assert store.last_success_at() is None

    store.save_run(_real_run(datetime.now().isoformat(timespec="seconds")))
    assert store.last_success_at() is not None


def test_last_success_ignores_dry_runs(tmp_path):
    """演练不能让停摆告警闭嘴——否则这个告警等于不存在。"""
    store = StateStore(tmp_path)

    rehearsal = RunRecord(started_at=datetime.now().isoformat(timespec="seconds"), dry_run=True)
    rehearsal.status = STATUS_SUCCESS
    store.save_run(rehearsal)

    assert store.last_success_at() is None


def test_run_record_defaults_to_failed():
    """默认 failed 很关键：进程被强杀时不能留下假的成功记录。"""
    assert RunRecord(started_at="2026-01-01T00:00:00").status == STATUS_FAILED


def test_posts_today_counts_only_today(tmp_path):
    store = StateStore(tmp_path)

    store.save_run(
        _real_run((datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"))
    )
    store.save_run(_real_run(datetime.now().isoformat(timespec="seconds")))

    assert store.posts_today() == 1


def test_posts_today_ignores_dry_runs(tmp_path):
    """演练不能占当天配额。

    否则"先演练确认，满意再 --live"这个正常顺序会在第二步被自己的上限挡住，
    而且报的理由看起来完全合理（"今天已发布 1 条"），根本想不到是演练造成的。
    """
    store = StateStore(tmp_path)

    # 时间戳要错开：记录文件名就是时间戳，同一秒会互相覆盖，测不出"多条"
    for offset in range(3):
        rehearsal = RunRecord(
            started_at=(datetime.now() - timedelta(minutes=offset)).isoformat(
                timespec="seconds"
            ),
            dry_run=True,
        )
        rehearsal.status = STATUS_SUCCESS
        store.save_run(rehearsal)

    assert len(store.load_runs()) == 3
    assert store.posts_today() == 0


def test_published_history_respects_window(tmp_path):
    store = StateStore(tmp_path)
    store.add_published("新文章", "http://new", "话题")

    # 手动塞一条 60 天前的
    data = json.loads(store.published_file.read_text(encoding="utf-8"))
    data["items"].append(
        {
            "title": "老文章",
            "source_url": "http://old",
            "topic": "旧话题",
            "published_at": (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds"),
        }
    )
    store.published_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    titles = [p["title"] for p in store.load_published(days=30)]
    assert titles == ["新文章"]


def test_load_runs_skips_corrupt_files(tmp_path):
    store = StateStore(tmp_path)
    record = RunRecord(started_at=datetime.now().isoformat(timespec="seconds"))
    record.status = STATUS_SUCCESS
    store.save_run(record)
    (store.runs_dir / "broken.json").write_text("{ 这不是合法 JSON", encoding="utf-8")

    assert len(store.load_runs()) == 1


# ---------- 其他 ----------


def test_notifier_disabled_without_config():
    assert not Notifier().enabled
    assert Notifier(serverchan_key="x").enabled
    assert Notifier(dingtalk_webhook="http://x").enabled


def test_dingtalk_sign_appends_params():
    signed = Notifier._sign_dingtalk("https://oapi.dingtalk.com/robot/send?access_token=abc", "secret")
    assert "timestamp=" in signed and "sign=" in signed
    assert signed.count("?") == 1


def test_notifier_send_never_raises():
    """告警渠道挂了不能把主流程带崩。"""
    Notifier(dingtalk_webhook="http://127.0.0.1:1/nonexistent").send("标题", "正文")


def test_clean_strips_html():
    assert _clean("<p>你好 <b>世界</b></p>\n\n") == "你好 世界"
