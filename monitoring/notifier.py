"""Notification channels: console, Telegram, Discord, LINE Messaging API.

Notifiers never raise - a failed alert must not crash the trading loop.

Note: *LINE Notify* was discontinued on 2025-03-31, so :class:`LineNotifier`
uses the LINE Messaging API push endpoint (channel access token + user/group id).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests
from loguru import logger

from config.settings import AlertSettings

_LEVEL_ICON = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "🚨", "CRITICAL": "🚨"}


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        """Deliver a message; return True on success."""


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        logger.log(level if level in {"INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"} else "INFO",
                   "[ALERT] {} | {}", title, message.replace("\n", " | "))
        return True


class _HttpNotifier(Notifier):
    timeout = 10

    def __init__(self, session: requests.Session | None = None) -> None:
        self.http = session or requests.Session()

    def _post(self, url: str, **kwargs) -> bool:
        try:
            resp = self.http.post(url, timeout=self.timeout, **kwargs)
            if resp.status_code >= 400:
                logger.error("{} notify failed: HTTP {} {}", self.name, resp.status_code, resp.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            logger.error("{} notify failed: {}", self.name, exc)
            return False


class TelegramNotifier(_HttpNotifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, session: requests.Session | None = None) -> None:
        super().__init__(session)
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        text = f"{_LEVEL_ICON.get(level, '')} {title}\n{message}"[:4000]
        return self._post(self.url, json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})


class DiscordNotifier(_HttpNotifier):
    name = "discord"

    def __init__(self, webhook_url: str, session: requests.Session | None = None) -> None:
        super().__init__(session)
        self.webhook_url = webhook_url

    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        content = f"{_LEVEL_ICON.get(level, '')} **{title}**\n{message}"[:1900]
        return self._post(self.webhook_url, json={"content": content})


class LineNotifier(_HttpNotifier):
    name = "line"
    url = "https://api.line.me/v2/bot/message/push"

    def __init__(self, channel_access_token: str, to: str, session: requests.Session | None = None) -> None:
        super().__init__(session)
        self.token = channel_access_token
        self.to = to

    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        text = f"{_LEVEL_ICON.get(level, '')} {title}\n{message}"[:4900]
        return self._post(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"to": self.to, "messages": [{"type": "text", "text": text}]},
        )


@dataclass
class MultiNotifier(Notifier):
    notifiers: list[Notifier] = field(default_factory=list)
    name: str = "multi"

    def send(self, title: str, message: str, level: str = "INFO") -> bool:
        results = []
        for n in self.notifiers:
            try:
                results.append(n.send(title, message, level))
            except Exception as exc:  # defensive: never propagate
                logger.exception("Notifier {} crashed: {}", n.name, exc)
                results.append(False)
        return any(results) if results else False

    @property
    def channel_names(self) -> str:
        return ",".join(n.name for n in self.notifiers)


def build_notifier(settings: AlertSettings) -> MultiNotifier:
    """Build configured channels; channels missing credentials are skipped with a warning."""
    notifiers: list[Notifier] = []
    for ch in settings.channels:
        if ch == "console":
            notifiers.append(ConsoleNotifier())
        elif ch == "telegram":
            if settings.telegram_bot_token and settings.telegram_chat_id:
                notifiers.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
            else:
                logger.warning("Telegram enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing")
        elif ch == "discord":
            if settings.discord_webhook_url:
                notifiers.append(DiscordNotifier(settings.discord_webhook_url))
            else:
                logger.warning("Discord enabled but DISCORD_WEBHOOK_URL missing")
        elif ch == "line":
            if settings.line_channel_access_token and settings.line_to:
                notifiers.append(LineNotifier(settings.line_channel_access_token, settings.line_to))
            else:
                logger.warning("LINE enabled but LINE_CHANNEL_ACCESS_TOKEN/LINE_TO missing")
    if not notifiers:
        notifiers.append(ConsoleNotifier())
    return MultiNotifier(notifiers)
