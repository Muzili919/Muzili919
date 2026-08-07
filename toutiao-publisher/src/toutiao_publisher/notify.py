"""告警推送。

无人值守的核心：失败必须有人知道。支持 Server酱（微信）、钉钉群机器人、
SMTP 邮件，配了哪个发哪个，都配就都发。

邮件渠道存在的理由：Server酱 和钉钉都要先去注册/建群拿 webhook，而 SMTP
授权码大多数人手上已经有了，是唯一能"零注册"立刻用上的渠道。

刻意不让告警失败影响主流程——推送挂了顶多少收一条通知，
不能反过来把已经成功的发布流程搞崩。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import re
import smtplib
import ssl
import time
import urllib.parse
from email.message import EmailMessage

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 15.0


class Notifier:
    def __init__(
        self,
        serverchan_key: str = "",
        dingtalk_webhook: str = "",
        dingtalk_secret: str = "",
        smtp_host: str = "",
        smtp_port: int = 465,
        smtp_user: str = "",
        smtp_pass: str = "",
        email_to: str = "",
    ):
        self.serverchan_key = serverchan_key
        self.dingtalk_webhook = dingtalk_webhook
        self.dingtalk_secret = dingtalk_secret
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.email_to = email_to

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.email_to)

    @property
    def enabled(self) -> bool:
        return bool(self.serverchan_key or self.dingtalk_webhook or self.email_enabled)

    def send(self, title: str, body: str) -> None:
        """推送一条消息。任何异常都吞掉并记日志。"""
        if not self.enabled:
            log.warning("未配置任何告警渠道，本条通知只写日志：%s", title)
            return

        if self.serverchan_key:
            self._safe(self._send_serverchan, title, body)
        if self.dingtalk_webhook:
            self._safe(self._send_dingtalk, title, body)
        if self.email_enabled:
            self._safe(self._send_email, title, body)

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

    def _send_email(self, title: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = self.smtp_user
        msg["To"] = self.email_to
        msg.set_content(body)  # 纯文本兜底，纯文本客户端也读得通
        msg.add_alternative(_markdown_lite_to_html(title, body), subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self.smtp_host, self.smtp_port, context=context, timeout=_TIMEOUT
        ) as server:
            server.login(self.smtp_user, self.smtp_pass)
            server.send_message(msg)
        log.info("邮件告警已发送 → %s", self.email_to)

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


def _markdown_lite_to_html(title: str, body: str) -> str:
    """把告警正文那点 markdown 转成手机上读得舒服的 HTML。

    只处理实际用到的三种：``` 代码块、**加粗**、`行内码`。不做通用 markdown 解析，
    因为正文全由本模块自己生成，格式是已知的。
    """
    # 按 ``` 切开后，奇数段是代码块，偶数段是普通正文
    parts: list[str] = []
    for i, chunk in enumerate(re.split(r"```", body)):
        escaped = html.escape(chunk)
        if i % 2 == 1:
            # 代码块：错误堆栈常常很长，必须能横向滚动，不然手机上被截断
            parts.append(
                '<pre style="background:#f6f8fa;border-radius:6px;padding:12px;'
                'overflow-x:auto;font-size:13px;line-height:1.5;margin:12px 0;">'
                f"{escaped.strip()}</pre>"
            )
        else:
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            text = re.sub(
                r"`(.+?)`",
                r'<code style="background:#f6f8fa;padding:2px 5px;border-radius:4px;">\1</code>',
                text,
            )
            parts.append(text.strip().replace("\n", "<br>"))

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "</head><body style=\"margin:0;padding:16px;font-family:-apple-system,"
        'BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.6;'
        'color:#1f2328;background:#fff;">'
        f'<h2 style="margin:0 0 16px;font-size:18px;">{html.escape(title)}</h2>'
        f'{"".join(parts)}'
        '<p style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;'
        'color:#6b7280;font-size:12px;">toutiao-publisher 自动发送</p>'
        "</body></html>"
    )


def build_notifier(secrets) -> Notifier:
    """从 Secrets 构造 Notifier。"""
    return Notifier(
        serverchan_key=secrets.serverchan_sendkey,
        dingtalk_webhook=secrets.dingtalk_webhook,
        dingtalk_secret=secrets.dingtalk_secret,
        smtp_host=secrets.smtp_host,
        smtp_port=secrets.smtp_port,
        smtp_user=secrets.smtp_user,
        smtp_pass=secrets.smtp_pass,
        email_to=secrets.email_to,
    )
