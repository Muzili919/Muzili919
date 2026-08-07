"""命令行入口。

    python -m toutiao_publisher run       跑一次完整流程
    python -m toutiao_publisher check     健康检查（判断是否停摆，给定时任务用）
    python -m toutiao_publisher doctor    逐项体检（人工排查用）
    python -m toutiao_publisher status    看最近的运行记录

退出码：0 成功 / 1 失败 / 2 正常跳过。
定时任务里可以据此判断，别把"今天没合适选题"当成故障。
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config
from .healthcheck import check_freshness, doctor
from .logging_setup import setup_logging
from .notify import build_notifier
from .pipeline import Pipeline
from .state import STATUS_SKIPPED, STATUS_SUCCESS, StateStore

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="toutiao_publisher", description="今日头条微头条自动发布"
    )
    parser.add_argument(
        "command", choices=["run", "check", "doctor", "status"], help="要执行的命令"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实发布（覆盖配置里的 dry_run）。不加这个参数就是演练模式。",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.live:
        cfg.raw.setdefault("run", {})["dry_run"] = False

    log_file = setup_logging(
        log_dir=cfg.path("log.dir", "state/logs"),
        level=cfg.get("log.level", "INFO"),
        retention_days=int(cfg.get("log.retention_days", 30)),
    )

    store = StateStore(cfg.state_dir)
    notifier = build_notifier(cfg.secrets)

    if args.command == "run":
        if cfg.dry_run:
            print("⚠️  演练模式：会走完全流程但不会真的发布。加 --live 才真发。\n")
        record = Pipeline(cfg, store, notifier).run()
        print(f"\n日志：{log_file}")
        if record.status == STATUS_SUCCESS:
            return EXIT_OK
        if record.status == STATUS_SKIPPED:
            return EXIT_SKIPPED
        return EXIT_FAIL

    if args.command == "check":
        return EXIT_OK if check_freshness(cfg, store, notifier) else EXIT_FAIL

    if args.command == "doctor":
        return EXIT_OK if doctor(cfg, store, notifier) else EXIT_FAIL

    if args.command == "status":
        _print_status(store)
        return EXIT_OK

    return EXIT_FAIL


def _print_status(store: StateStore) -> None:
    runs = store.load_runs()[:15]
    if not runs:
        print("还没有任何运行记录。")
        return

    icons = {"success": "✅", "failed": "❌", "skipped": "⏭️ "}
    print(f"\n最近 {len(runs)} 次运行：\n")
    for r in runs:
        icon = icons.get(r.get("status", ""), "❓")
        started = r.get("started_at", "")
        mode = "演练" if r.get("dry_run") else "实发"
        title = r.get("title") or r.get("topic") or r.get("error", "")[:50] or "—"
        print(f"  {icon} {started}  [{mode}]  {title}")
        if r.get("status") == "failed" and r.get("error"):
            print(f"      错误：{r['error'][:110]}")
        if r.get("image_skipped_reason"):
            print(f"      无图：{r['image_skipped_reason'][:110]}")

    last_success = store.last_success_at()
    print(f"\n最近一次成功：{last_success or '无'}")
    print(f"今天已发布：{store.posts_today()} 条\n")


if __name__ == "__main__":
    sys.exit(main())
