"""IM 通知组件：none（打到日志）/ 钉钉 / 飞书 / 企业微信 / Slack。

通知失败**永远不能**影响主流程：所有实现内部捕获异常并返回 False，
调用方只记录、不抛错（提案确认已经落库，通知只是锦上添花）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("finonto.im")


class LogOnlyIM:
    name = "none"
    kind = "im"

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "provider": "log", "needs_key": False, "enabled": False}

    def send(self, text: str, *, title: str = "") -> bool:
        logger.info("[IM:none] %s %s", title, text)
        return True


class _WebhookIM:
    kind = "im"
    name = "webhook"
    payload_builder = None  # type: ignore[assignment]

    def __init__(self, webhook: str, timeout: float = 8.0) -> None:
        self.webhook = webhook
        self.timeout = timeout

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.name,
            "needs_key": True,
            "key_configured": bool(self.webhook),
        }

    def _payload(self, text: str, title: str) -> dict[str, Any]:
        raise NotImplementedError

    def send(self, text: str, *, title: str = "") -> bool:
        if not self.webhook:
            logger.warning("[IM:%s] webhook 未配置，跳过", self.name)
            return False
        try:
            import httpx

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.webhook, json=self._payload(text, title))
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - 通知不能影响主流程
            logger.warning("[IM:%s] 发送失败：%s", self.name, exc)
            return False


class DingTalkIM(_WebhookIM):
    name = "dingtalk"

    def _payload(self, text: str, title: str) -> dict[str, Any]:
        content = f"### {title}\n\n{text}" if title else text
        return {"msgtype": "markdown", "markdown": {"title": title or "FinOnto", "text": content}}


class FeishuIM(_WebhookIM):
    name = "feishu"

    def _payload(self, text: str, title: str) -> dict[str, Any]:
        content = f"**{title}**\n{text}" if title else text
        return {"msg_type": "text", "content": {"text": content}}


class WecomIM(_WebhookIM):
    name = "wecom"

    def _payload(self, text: str, title: str) -> dict[str, Any]:
        content = f"## {title}\n{text}" if title else text
        return {"msgtype": "markdown", "markdown": {"content": content}}


class SlackIM(_WebhookIM):
    name = "slack"

    def _payload(self, text: str, title: str) -> dict[str, Any]:
        content = f"*{title}*\n{text}" if title else text
        return {"text": content}


IM_IMPLEMENTATIONS = {
    "none": LogOnlyIM,
    "dingtalk": DingTalkIM,
    "feishu": FeishuIM,
    "wecom": WecomIM,
    "slack": SlackIM,
}
