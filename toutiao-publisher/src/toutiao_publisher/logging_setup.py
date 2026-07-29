"""日志配置。

无人值守脚本最怕的就是"出了事没留下痕迹"。这里做三件事：
  1. 同时写文件和 stdout（LaunchAgent 会把 stdout 也重定向到文件，双保险）
  2. 每天一个日志文件，方便按日期回溯
  3. 自动清理过期日志，不让它无限涨
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: Path, level: str = "INFO", retention_days: int = 30) -> Path:
    """配置全局日志，返回当天日志文件路径。重复调用只生效一次。"""
    global _CONFIGURED

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now():%Y-%m-%d}.log"

    if _CONFIGURED:
        return log_file

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    # 第三方库太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "openai", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _prune_old_logs(log_dir, retention_days)
    _CONFIGURED = True
    return log_file


def _prune_old_logs(log_dir: Path, retention_days: int) -> None:
    """删掉超过保留期的日志文件。清理失败不影响主流程。"""
    if retention_days <= 0:
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for f in log_dir.glob("*.log"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
        except OSError:
            pass
