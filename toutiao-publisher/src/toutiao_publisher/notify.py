"""告警推送。

无人值守的核心：失败必须有人知道。支持 Server酱（微信）和钉钉群机器人，
配了哪个发哪个，都配就都发。

刻意不让告警失败影响主流程——推送挂了顶多少收一条通知，
不能反过来把已经成功的发布流程搞崩。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 15.0


class Notifier:
    def __init__(self, serverchan_key: str = "", dingtalk_webhook: str = "", dingtalk_secret: str = ""):
        self.serverchan_key = serverchan_key
        self.dingtalk_webhook = dingtalk_webhook
        self.dingtalk_secret = dingtalk_secret

    @property
    def enabled(self) -> bool:
        return bool(self.serverchan_key or self.dingtalk_webhook)

    def send(self, title: str, body: str) -> None:
        """推送一条消息。任何异常都吞掉并记日志。"""
        if not self.enabled:
            log.warning("未配置任何告警渠道，本条通知只写日志：%s", title)
            return

        if self.serverchan_key:
            self._safe(self._send_serverchan, title, body)
        if self.dingtalk_webhook:
            self._safe(self._send_dingtalk, title, body)

    # ---------- 语义化封装 ----------

    def alert_failure(self, step: str, error: str, log_file: str = "") -> None:
        body = f"**失败环节**：{step}\n\n**错误**：\n```\n{error}\n```"
        if log_file:
            body += f"\n\n**日志**：`{log_file}`"
        self.send("头条自动发布失败", body)

    def alert_stale(self, hours: float, last_success: str) -> None:
        self.send(
            "头条自动发布已停摆",
            f"已经 **{hours:.1f} 小时**没有成功发布。\n\n"
            f"最近一次成功：{last_success or '无记录'}\n\n"
            f"多半是定时任务没触发，或 Chrome / 登录态失效。"
            f"先跑一次 `python -m toutiao_publisher doctor` 看看。",
        )

    def notify_success(self, title: str, post_url: str = "", image_used: bool = True) -> None:
        body = f"**标题**：{title}\n\n**配图**：{'有' if image_used else '无（质量闸拦下或生图失败）'}"
        if post_url:
            body += f"\n\n**链接**：{post_url}"
        self.send("头条已发布", body)

    # ---------- 具体渠道 ----------

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 — 告警失败绝不能影响主流程
            log.error("告警推送失败（已忽略）：%s", exc)

    def _send_serverchan(self, title: str, body: str) -> None:
        resp = httpx.post(
            f"https://sctapi.ftqq.com/{self.serverchan_key}.send",
            data={"title": title, "desp": body},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Server酱 告警已发送")

    def _send_dingtalk(self, title: str, body: str) -> None:
        url = self.dingtalk_webhook
        if self.dingtalk_secret:
            url = self._sign_dingtalk(url, self.dingtalk_secret)

        resp = httpx.post(
            url,
            json={
                "msgtype": "markdown",
                "markdown": {"title": title, "text": f"## {title}\n\n{body}"},
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"钉钉返回错误：{result}")
        log.info("钉钉告警已发送")

    @staticmethod
    def _sign_dingtalk(webhook: str, secret: str) -> str:
        """钉钉加签：timestamp + secret 做 HMAC-SHA256，再 base64 + urlencode。"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in webhook else "?"
        return f"{webhook}{sep}timestamp={timestamp}&sign={sign}"


def build_notifier(secrets) -> Notifier:
    """从 Secrets 构造 Notifier。"""
    return Notifier(
        serverchan_key=secrets.serverchan_sendkey,
        dingtalk_webhook=secrets.dingtalk_webhook,
        dingtalk_secret=secrets.dingtalk_secret,
    )
