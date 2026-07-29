"""运行状态：运行记录 + 去重。

两个职责：
  1. 每次运行落一份 JSON 记录（不管成败），这是事后排查的唯一依据
  2. 记住发过什么，避免重复选题

设计上刻意用「一次运行一个文件」而不是单一大文件——并发写不会互相覆盖，
文件名带时间戳，`ls state/runs/` 一眼就能看出昨天到底跑没跑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 运行结果状态
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"  # 正常跳过，比如当天已达发布上限


@dataclass
class StepRecord:
    """单个环节的执行结果。"""

    name: str
    ok: bool
    duration_s: float
    detail: str = ""


@dataclass
class RunRecord:
    """一次完整运行的记录。"""

    started_at: str
    status: str = STATUS_FAILED  # 默认失败，成功了才改——避免异常退出时留下假的成功
    finished_at: str = ""
    dry_run: bool = True
    topic: str = ""
    title: str = ""
    source_url: str = ""
    content_chars: int = 0
    image_path: str = ""
    image_skipped_reason: str = ""
    post_url: str = ""
    error: str = ""
    steps: list[StepRecord] = field(default_factory=list)

    def add_step(self, name: str, ok: bool, duration_s: float, detail: str = "") -> None:
        self.steps.append(StepRecord(name=name, ok=ok, duration_s=round(duration_s, 2), detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateStore:
    """读写 state/ 下的运行记录与已发布历史。"""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.runs_dir = state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.published_file = state_dir / "published.json"

    # ---------- 运行记录 ----------

    def save_run(self, record: RunRecord) -> Path:
        """落盘一次运行记录。任何情况下都要调用。"""
        record.finished_at = datetime.now().isoformat(timespec="seconds")
        ts = record.started_at.replace(":", "").replace("-", "").replace("T", "-")
        out = self.runs_dir / f"{ts}-{record.status}.json"
        out.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("运行记录已保存：%s", out)
        return out

    def load_runs(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """读取运行记录，按开始时间倒序。"""
        records: list[dict[str, Any]] = []
        for f in self.runs_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("跳过损坏的运行记录：%s", f)
                continue
            if since:
                started = _parse_dt(data.get("started_at"))
                if started is None or started < since:
                    continue
            records.append(data)
        records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return records

    def last_success_at(self) -> datetime | None:
        """最近一次成功发布的时间。健康检查用。"""
        for rec in self.load_runs():
            if rec.get("status") == STATUS_SUCCESS:
                return _parse_dt(rec.get("started_at"))
        return None

    def posts_today(self) -> int:
        """今天已成功发布几条。"""
        today = datetime.now().date()
        count = 0
        for rec in self.load_runs():
            if rec.get("status") != STATUS_SUCCESS:
                continue
            started = _parse_dt(rec.get("started_at"))
            if started and started.date() == today:
                count += 1
        return count

    def prune_runs(self, retention_days: int) -> None:
        """清理过期运行记录。"""
        if retention_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=retention_days)
        for f in self.runs_dir.glob("*.json"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
            except OSError:
                pass

    # ---------- 去重 ----------

    def load_published(self, days: int) -> list[dict[str, Any]]:
        """读取近 N 天已发布条目，用于选题去重。"""
        if not self.published_file.exists():
            return []
        try:
            data = json.loads(self.published_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("published.json 损坏，按空历史处理")
            return []

        cutoff = datetime.now() - timedelta(days=days)
        items = []
        for item in data.get("items", []):
            published = _parse_dt(item.get("published_at"))
            if published and published >= cutoff:
                items.append(item)
        return items

    def add_published(self, title: str, source_url: str, topic: str) -> None:
        """记一条已发布。"""
        data: dict[str, Any] = {"items": []}
        if self.published_file.exists():
            try:
                data = json.loads(self.published_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        data.setdefault("items", []).append(
            {
                "title": title,
                "source_url": source_url,
                "topic": topic,
                "published_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        # 只留最近 500 条，避免文件无限涨
        data["items"] = data["items"][-500:]
        self.published_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
