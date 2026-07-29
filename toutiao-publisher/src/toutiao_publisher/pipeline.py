"""编排：RSS → 选题 → 成文 → 配图 → 发布。

分级失败策略——这是整个设计的核心：
  - 配图失败 → 降级为纯文字继续发（图是锦上添花，不该阻断发布）
  - 其余任何环节失败 → 中止 + 告警
不管走到哪一步、成功还是失败，最后一定落一份运行记录。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import Config, load_sources
from .content.llm import LLMClient
from .content.selector import NoSuitableTopic, select
from .content.writer import write
from .images import quality
from .images.cdp_chatgpt import CDPError, ChatGPTImageGenerator
from .notify import Notifier
from .publish.toutiao import ToutiaoPublisher
from .sources import rss
from .state import STATUS_SKIPPED, STATUS_SUCCESS, RunRecord, StateStore

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg: Config, store: StateStore, notifier: Notifier):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.record = RunRecord(
            started_at=datetime.now().isoformat(timespec="seconds"),
            dry_run=cfg.dry_run,
        )

    def run(self) -> RunRecord:
        """跑一次完整流程。异常在这里兜住，保证运行记录一定落盘。"""
        try:
            self._run_inner()
        except NoSuitableTopic as exc:
            # 没有合适选题是正常情况，不是故障，不告警
            self.record.status = STATUS_SKIPPED
            self.record.error = str(exc)
            log.info("本次跳过：%s", exc)
        except Exception as exc:  # noqa: BLE001 — 顶层必须捕获一切
            self.record.error = f"{type(exc).__name__}: {exc}"
            log.exception("流程失败")
            failed_step = next(
                (s.name for s in reversed(self.record.steps) if not s.ok), "未知环节"
            )
            self.notifier.alert_failure(
                step=failed_step,
                error=self.record.error,
                log_file=str(self.cfg.path("log.dir", "state/logs")),
            )
        finally:
            self.store.save_run(self.record)
            self.store.prune_runs(int(self.cfg.get("log.retention_days", 30)))
        return self.record

    def _run_inner(self) -> None:
        cfg = self.cfg

        # 发布上限：防止重试或手滑导致一天刷屏
        max_posts = int(cfg.get("run.max_posts_per_day", 1))
        already = self.store.posts_today()
        if already >= max_posts:
            raise NoSuitableTopic(
                f"今天已成功发布 {already} 条，达到上限 {max_posts} 条，不再发送。"
            )

        llm = LLMClient(
            api_key=cfg.secrets.llm_api_key,
            base_url=cfg.secrets.llm_base_url,
            model=cfg.secrets.llm_model,
            timeout=float(cfg.get("run.timeouts.llm", 90)),
        )

        # --- 1. 抓 RSS ---
        with self._step("抓取 RSS"):
            sources = load_sources()
            entries = rss.fetch_all(
                sources,
                limit=int(cfg.get("content.candidate_pool", 30)),
                timeout=int(cfg.get("run.timeouts.rss_fetch", 30)),
            )
            self._detail = f"{len(entries)} 条候选，来自 {len(sources)} 个源"

        # --- 2. 选题 ---
        with self._step("AI 选题"):
            published = self.store.load_published(int(cfg.get("content.dedupe_days", 30)))
            selection = select(
                llm=llm,
                entries=entries,
                published=published,
                domain=cfg.get("content.domain", ""),
                audience=cfg.get("content.audience", ""),
            )
            self.record.topic = selection.topic
            self.record.source_url = selection.entry.url
            self._detail = selection.topic

        # --- 3. 成文 ---
        with self._step("AI 成文"):
            article = write(
                llm=llm,
                selection=selection,
                domain=cfg.get("content.domain", ""),
                audience=cfg.get("content.audience", ""),
                tone=cfg.get("content.tone", ""),
                min_chars=int(cfg.get("content.min_chars", 180)),
                max_chars=int(cfg.get("content.max_chars", 500)),
            )
            self.record.title = article.title
            self.record.content_chars = article.char_count
            self._detail = f"{article.title}（{article.char_count} 字）"

        # --- 4. 配图（失败可降级）---
        image_path = self._make_image(selection.topic, article.image_prompt)

        # --- 5. 发布 ---
        with self._step("发布到头条"):
            publisher = ToutiaoPublisher(cfg)
            result = publisher.publish(
                content=article.content, image_path=image_path, dry_run=cfg.dry_run
            )
            self.record.post_url = result.post_url
            self._detail = result.detail

        # 演练模式不计入已发布历史，否则明天就把这条选题去重掉了
        if not cfg.dry_run:
            self.store.add_published(
                title=article.title, source_url=selection.entry.url, topic=selection.topic
            )
            self.notifier.notify_success(
                title=article.title,
                post_url=result.post_url,
                image_used=image_path is not None,
            )

        self.record.status = STATUS_SUCCESS
        log.info("流程完成%s", "（演练模式）" if cfg.dry_run else "")

    def _make_image(self, topic: str, image_prompt: str) -> Path | None:
        """生图 + 质量闸。任何失败都降级为无图，不中断流程。"""
        cfg = self.cfg
        if not cfg.get("image.enabled", True):
            self.record.image_skipped_reason = "配置里关闭了配图"
            return None

        started = time.monotonic()
        try:
            template = cfg.get("image.prompt_template", "{topic}")
            prompt = template.format(topic=image_prompt or topic)

            generator = ChatGPTImageGenerator(
                port=int(cfg.get("image.cdp_port", 9230)),
                url_contains=cfg.get("image.target_url_contains", "chatgpt.com"),
                wait_timeout=int(cfg.get("image.wait_timeout", 240)),
            )
            data = generator.generate(prompt)

            verdict = quality.check(data, cfg.get("image.quality", {}) or {})
            if not verdict.ok:
                self.record.image_skipped_reason = f"未通过质量闸：{verdict.reason}"
                log.warning("配图被质量闸拦下：%s", verdict.reason)
                self.record.add_step(
                    "生成配图", ok=True, duration_s=time.monotonic() - started,
                    detail=f"降级为纯文字（{verdict.reason}）",
                )
                return None

            out_dir = cfg.state_dir / "images"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.{verdict.fmt.lower()}"
            path.write_bytes(data)

            self.record.image_path = str(path)
            self.record.add_step(
                "生成配图", ok=True, duration_s=time.monotonic() - started,
                detail=verdict.describe(),
            )
            return path

        except CDPError as exc:
            # CDP 挂了是最常见的故障，单独给一条清楚的日志
            self.record.image_skipped_reason = f"CDP 生图失败：{exc}"
            log.warning("配图失败，降级为纯文字。原因：%s", exc)
            self.record.add_step(
                "生成配图", ok=False, duration_s=time.monotonic() - started, detail=str(exc)[:300]
            )
            return None
        except Exception as exc:  # noqa: BLE001 — 配图任何失败都不该阻断发布
            self.record.image_skipped_reason = f"生图异常：{exc}"
            log.warning("配图失败，降级为纯文字。原因：%s", exc)
            self.record.add_step(
                "生成配图", ok=False, duration_s=time.monotonic() - started, detail=str(exc)[:300]
            )
            return None

    @contextmanager
    def _step(self, name: str):
        """记录一个环节的耗时与成败。"""
        log.info("=== %s ===", name)
        self._detail = ""
        started = time.monotonic()
        try:
            yield
        except Exception:
            self.record.add_step(name, ok=False, duration_s=time.monotonic() - started)
            raise
        self.record.add_step(
            name, ok=True, duration_s=time.monotonic() - started, detail=self._detail
        )
