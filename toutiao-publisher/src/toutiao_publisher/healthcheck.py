"""健康检查与自检。

这两个命令解决的是同一类问题："今天为什么没发出去，而我完全不知道"。

  - check：只看"最近一次成功发布是什么时候"。这是唯一能发现
    「定时任务根本没触发」的手段——流程内部的告警永远发不出这种警，
    因为流程压根没跑起来。它必须由**另一个**定时任务独立执行。

  - doctor：逐项体检各外部依赖，人工排查时用。
"""

from __future__ import annotations

import logging
from datetime import datetime

from .config import Config, load_sources
from .content.llm import LLMClient
from .images.cdp_chatgpt import ChatGPTImageGenerator
from .notify import Notifier
from .publish.toutiao import ToutiaoPublisher
from .state import StateStore

log = logging.getLogger(__name__)


def check_freshness(cfg: Config, store: StateStore, notifier: Notifier) -> bool:
    """检查距上次成功发布过了多久，超阈值就告警。返回是否健康。"""
    max_hours = float(cfg.get("health.max_hours_since_success", 26))
    last = store.last_success_at()

    if last is None:
        log.warning("没有任何成功发布记录")
        notifier.alert_stale(hours=float("inf"), last_success="")
        return False

    hours = (datetime.now() - last).total_seconds() / 3600
    if hours > max_hours:
        log.warning("距上次成功发布已 %.1f 小时，超过阈值 %.1f", hours, max_hours)
        notifier.alert_stale(hours=hours, last_success=last.isoformat(timespec="seconds"))
        return False

    log.info("健康：距上次成功发布 %.1f 小时（阈值 %.1f）", hours, max_hours)
    return True


def doctor(cfg: Config, store: StateStore, notifier: Notifier) -> bool:
    """逐项体检。返回是否全部通过。"""
    checks: list[tuple[str, bool, str]] = []

    def probe(name: str, fn) -> None:
        try:
            checks.append((name, True, fn()))
        except Exception as exc:  # noqa: BLE001 — 体检就是要把异常转成结论
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    probe("配置文件", lambda: f"OK — dry_run={cfg.dry_run}")
    probe("RSS 源配置", lambda: f"OK — {len(load_sources())} 个源")
    probe("告警渠道", lambda: _check_notifier(notifier))
    probe("LLM 接口", lambda: _check_llm(cfg))
    probe("ChatGPT CDP（生图）", lambda: _check_cdp(cfg))
    probe("头条登录态", lambda: ToutiaoPublisher(cfg).check_login())
    probe("最近运行记录", lambda: _check_runs(store))

    print("\n" + "=" * 64)
    print("  体检报告")
    print("=" * 64)
    all_ok = True
    for name, ok, detail in checks:
        mark = "✅" if ok else "❌"
        print(f"{mark}  {name}")
        # 多行错误信息（大多是修复指引）逐行缩进，保持对齐
        for line in str(detail).splitlines():
            print(f"    {line.strip()}")
        if not ok:
            all_ok = False
    print("=" * 64)
    print("全部通过 ✅" if all_ok else "存在问题 ❌ — 按上面的提示逐项修复")
    print()
    return all_ok


def _check_notifier(notifier: Notifier) -> str:
    if not notifier.enabled:
        raise RuntimeError(
            "没有配置任何告警渠道。失败时你收不到通知，等于白做监控。\n"
            "    修复：在 .env 里填 SERVERCHAN_SENDKEY 或 DINGTALK_WEBHOOK。"
        )
    channels = []
    if notifier.serverchan_key:
        channels.append("Server酱")
    if notifier.dingtalk_webhook:
        channels.append("钉钉")
    return f"OK — 已启用：{', '.join(channels)}"


def _check_llm(cfg: Config) -> str:
    llm = LLMClient(
        api_key=cfg.secrets.llm_api_key,
        base_url=cfg.secrets.llm_base_url,
        model=cfg.secrets.llm_model,
        timeout=30.0,
    )
    result = llm.chat_json(
        system='返回 JSON：{"ok": true}',
        user="测试连通性",
        temperature=0.1,
    )
    return f"OK — {cfg.secrets.llm_model} 响应正常（{result}）"


def _check_cdp(cfg: Config) -> str:
    if not cfg.get("image.enabled", True):
        return "跳过 — 配置里关闭了配图"
    generator = ChatGPTImageGenerator(
        port=int(cfg.get("image.cdp_port", 9230)),
        url_contains=cfg.get("image.target_url_contains", "chatgpt.com"),
    )
    return generator.probe()


def _check_runs(store: StateStore) -> str:
    runs = store.load_runs()
    if not runs:
        return "还没有任何运行记录（首次运行前属正常）"
    last = runs[0]
    success_count = sum(1 for r in runs if r.get("status") == "success")
    return (
        f"共 {len(runs)} 次运行，{success_count} 次成功。"
        f"最近一次：{last.get('started_at')} → {last.get('status')}"
    )
