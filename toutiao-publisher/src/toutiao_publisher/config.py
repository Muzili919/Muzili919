"""配置加载与校验。

配置分两层：
  - config/config.yaml   行为参数，进版本库（示例），可读可改
  - .env                 密钥，不进版本库

启动时一次性校验，缺什么立刻报错——不要等跑到第三步才发现没填 key。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录：src/toutiao_publisher/config.py -> 上三层
ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """配置缺失或非法。信息里必须写清楚怎么修。"""


@dataclass
class Secrets:
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    serverchan_sendkey: str = ""
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""
    email_to: str = ""

    @property
    def has_email(self) -> bool:
        # 缺任何一项都发不出去，别让它半配着假装可用
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass and self.email_to)

    @property
    def has_alert_channel(self) -> bool:
        return bool(self.serverchan_sendkey or self.dingtalk_webhook or self.has_email)


@dataclass
class Config:
    raw: dict[str, Any]
    secrets: Secrets
    root: Path = field(default=ROOT)

    # --- 便捷访问器 ---
    def get(self, path: str, default: Any = None) -> Any:
        """点号取值：cfg.get("image.quality.min_width")"""
        node: Any = self.raw
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def path(self, path: str, default: str = "") -> Path:
        """取一个相对项目根的路径配置，返回绝对 Path。"""
        value = self.get(path, default)
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    @property
    def dry_run(self) -> bool:
        return bool(self.get("run.dry_run", True))

    @property
    def state_dir(self) -> Path:
        return self.root / "state"


def _require_env(name: str, hint: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"环境变量 {name} 未设置。{hint}")
    return value


def load_config(config_path: Path | None = None) -> Config:
    """加载 .env 与 config.yaml，校验必填项。"""
    load_dotenv(ROOT / ".env")

    cfg_file = config_path or (ROOT / "config" / "config.yaml")
    if not cfg_file.exists():
        raise ConfigError(
            f"找不到配置文件 {cfg_file}。\n"
            f"请先执行：cp config/config.example.yaml config/config.yaml"
        )

    with cfg_file.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    secrets = Secrets(
        llm_api_key=_require_env(
            "LLM_API_KEY", "在 .env 里填 LLM 的 API key（参考 .env.example）。"
        ),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-v4-flash").strip(),
        serverchan_sendkey=os.getenv("SERVERCHAN_SENDKEY", "").strip(),
        dingtalk_webhook=os.getenv("DINGTALK_WEBHOOK", "").strip(),
        dingtalk_secret=os.getenv("DINGTALK_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "465").strip() or 465),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_pass=os.getenv("SMTP_PASS", "").strip(),
        email_to=os.getenv("EMAIL_TO", "").strip(),
    )

    cfg = Config(raw=raw, secrets=secrets)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def load_sources(path: Path | None = None) -> list[dict[str, Any]]:
    """加载 RSS 源列表。"""
    import json

    src_file = path or (ROOT / "config" / "sources.json")
    if not src_file.exists():
        raise ConfigError(
            f"找不到 RSS 源配置 {src_file}。\n"
            f"请先执行：cp config/sources.example.json config/sources.json"
        )
    with src_file.open(encoding="utf-8") as f:
        data = json.load(f)

    sources = [s for s in data.get("sources", []) if s.get("url")]
    if not sources:
        raise ConfigError(f"{src_file} 里没有任何有效的 RSS 源（每条需要 url 字段）。")
    return sources
