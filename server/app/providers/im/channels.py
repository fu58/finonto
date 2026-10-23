"""IM 渠道适配：控制台（默认）/ 钉钉 / 飞书 / 企业微信 / Slack。

用于把「本体提案待确认」「Wiki 版本变更」「Agent 运行结果」推送到协作渠道。
未配置 webhook 时走 Console 渠道，只记录不外发，保证离线可跑。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

from ..base import ProviderError


class ConsoleChannel:
    name = "console"

    def __init__(self) -> None:
        self.outbox: list[dict] = []

    def send(self, target: str, text: str, *, title: str = "") -> dict:
        record = {"channel": self.name, "target": target, "title": title, "text": text, "ts": time.time()}
        self.outbox.append(record)
        return {"ok": True, "channel": self.name, "queued": len(self.outbox), "delivered": "local"}


class _WebhookChannel:
    name = "webhook"
    secret: str = ""

    def __init__(self, webhook_url: str, secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.secret = secret
        if not webhook_url:
            raise ProviderError(f"{self.name} 渠道缺少 webhook url")

    def _signed_url(self) -> str:
        return self.webhook_url

    def _body(self, title: str, text: str) -> dict:
        raise NotImplementedError

    def send(self, target: str, text: str, *, title: str = "") -> dict:
        import httpx

        url = self._signed_url()
        body = self._body(title or "", text)
        try:
            resp = httpx.post(url, json=body, timeout=15.0)
            resp.raise_for_status()
            return {"ok": True, "channel": self.name, "status": resp.status_code, "target": target}
        except httpx.HTTPError as exc:
            return {"ok": False, "channel": self.name, "error": str(exc)}


class DingTalkChannel(_WebhookChannel):
    name = "dingtalk"

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        digest = hmac.new(self.secret.encode(), f"{timestamp}\n{self.secret}".encode(), hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    def _body(self, title: str, text: str) -> dict:
        return {"msgtype": "markdown", "markdown": {"title": title or "FinOnto", "text": text}}


class FeishuChannel(_WebhookChannel):
    name = "feishu"

    def _body(self, title: str, text: str) -> dict:
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title or "FinOnto"}},
                "elements": [{"tag": "markdown", "content": text}],
            },
        }
        return card

    def send(self, target: str, text: str, *, title: str = "") -> dict:
        import httpx

        try:
            resp = httpx.post(self.webhook_url, json={"msg_type": "text", "content": {"text": f"{title}\n{text}"}}, timeout=15.0)
            resp.raise_for_status()
            return {"ok": True, "channel": self.name, "status": resp.status_code}
        except httpx.HTTPError as exc:
            return {"ok": False, "channel": self.name, "error": str(exc)}


class WeComChannel(_WebhookChannel):
    name = "wecom"

    def _body(self, title: str, text: str) -> dict:
        return {"msgtype": "markdown", "markdown": {"content": f"**{title or 'FinOnto'}**\n{text}"}}


class SlackChannel(_WebhookChannel):
    name = "slack"

    def _body(self, title: str, text: str) -> dict:
        return {"text": f"*{title or 'FinOnto'}*\n{text}"}


CHANNELS = {c.name: c for c in (ConsoleChannel, DingTalkChannel, FeishuChannel, WeComChannel, SlackChannel)}


def render_proposal_notice(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)[:1200]
