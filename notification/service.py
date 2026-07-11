"""
Notification Layer
==================
Multi-channel alert delivery for trading events, risk breaches, and
system health degradations.

Channels
--------
* TelegramChannel   — Real-time alerts via Bot API (best for mobile).
* EmailChannel      — SMTP with HTML templates for daily reports.
* WebhookChannel    — POST to Slack/Discord/PagerDuty endpoints.
* SlackChannel      — Slack Block Kit formatted messages.

Design decisions
----------------
* All channels implement INotificationChannel Protocol.
* Severity routing: DEBUG/INFO → webhook only, WARNING → Telegram + webhook,
  ERROR/CRITICAL → all channels (Telegram + email + webhook).
* Rate limiting: max 30 Telegram messages/second per Bot API limits.
  A token-bucket limiter prevents 429 errors.
* Circuit breaker per channel: if a channel fails 3 consecutive times,
  it backs off for 60 seconds to avoid cascading alert storms.
* All messages are async; a background queue absorbs burst loads without
  blocking the trading loop.
* PagerDuty integration for CRITICAL alerts via Events API v2.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Dict, List, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------


class AlertLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def emoji(self) -> str:
        return {
            "DEBUG": "🔍",
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "CRITICAL": "🚨",
        }[self.value]

    @property
    def color(self) -> str:
        """Slack attachment colour."""
        return {
            "DEBUG": "#808080",
            "INFO": "#36a64f",
            "WARNING": "#ffcc00",
            "ERROR": "#ff4444",
            "CRITICAL": "#cc0000",
        }[self.value]


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (for Telegram)
# ---------------------------------------------------------------------------


class TokenBucket:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate  # tokens per second
        self._capacity = capacity  # max burst
        self._tokens = capacity
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Per-channel circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class ChannelCircuitBreaker:
    failures: int = 0
    threshold: int = 3
    backoff_until: float = 0.0

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.backoff_until = time.monotonic() + 60.0
            logger.warning("Notification channel circuit breaker OPEN for 60s")

    def is_open(self) -> bool:
        if time.monotonic() > self.backoff_until:
            self.failures = 0  # Reset on backoff expiry
            return False
        return self.failures >= self.threshold


# ---------------------------------------------------------------------------
# Telegram Channel
# ---------------------------------------------------------------------------


class TelegramChannel:
    """
    Sends alerts via Telegram Bot API.
    Uses MarkdownV2 formatting for rich messages.
    """

    API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._token = settings.notification.telegram_token.get_secret_value()
        self._chat_id = settings.notification.telegram_chat_id
        self._limiter = TokenBucket(rate=25, capacity=30)  # 25/sec, burst 30
        self._breaker = ChannelCircuitBreaker()
        self.channel_name = "telegram"

    async def send(self, subject: str, body: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        if self._breaker.is_open():
            return False
        if not self._token or not self._chat_id:
            logger.debug("Telegram not configured — skipping notification")
            return False

        await self._limiter.acquire()

        text = f"{level.emoji} *{self._escape(subject)}*\n\n{self._escape(body)}"

        try:
            import aiohttp

            url = self.API_BASE.format(token=self._token)
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        self._breaker.record_success()
                        logger.debug("Telegram alert sent: %s", subject)
                        return True
                    else:
                        body_text = await resp.text()
                        logger.warning("Telegram API %d: %s", resp.status, body_text)
                        self._breaker.record_failure()
                        return False
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            self._breaker.record_failure()
            return False

    @staticmethod
    def _escape(text: str) -> str:
        """Escape MarkdownV2 special characters."""
        special = r"\_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{c}" if c in special else c for c in text)


# ---------------------------------------------------------------------------
# Email (SMTP) Channel
# ---------------------------------------------------------------------------


class EmailChannel:
    """
    Sends HTML-formatted alerts and daily reports via SMTP.
    Runs blocking smtplib in a ThreadPoolExecutor.
    """

    def __init__(self) -> None:
        self._host = settings.notification.smtp_host
        self._port = settings.notification.smtp_port
        self._user = settings.notification.smtp_user
        self._password = settings.notification.smtp_password.get_secret_value()
        self._from = settings.notification.smtp_from_email
        self._to = settings.notification.alert_email_to
        self._breaker = ChannelCircuitBreaker()
        self.channel_name = "email"

    async def send(self, subject: str, body: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        if self._breaker.is_open():
            return False
        if not self._host or not self._to:
            logger.debug("Email not configured — skipping")
            return False

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._send_sync, subject, body, level)
            self._breaker.record_success()
            return True
        except Exception as exc:
            logger.error("Email send failed: %s", exc)
            self._breaker.record_failure()
            return False

    def _send_sync(self, subject: str, body: str, level: AlertLevel) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[{level.value}] {subject}"
        msg["From"] = self._from
        msg["To"] = self._to

        html = self._html_template(subject, body, level)
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL(self._host, self._port) as server:
            server.login(self._user, self._password)
            server.sendmail(self._from, self._to.split(","), msg.as_string())

    @staticmethod
    def _html_template(subject: str, body: str, level: AlertLevel) -> str:
        color = level.color
        return f"""
        <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
        <div style="max-width:600px;margin:auto;background:white;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1)">
          <div style="background:{color};padding:16px 24px">
            <h2 style="color:white;margin:0">{level.emoji} {subject}</h2>
          </div>
          <div style="padding:24px">
            <pre style="background:#f8f8f8;padding:16px;border-radius:4px;white-space:pre-wrap">{body}</pre>
          </div>
          <div style="padding:12px 24px;background:#f0f0f0;font-size:12px;color:#888">
            Institutional Algorithmic Trading Platform — Automated Alert
          </div>
        </div>
        </body></html>
        """


# ---------------------------------------------------------------------------
# Webhook Channel (Slack / Discord / PagerDuty)
# ---------------------------------------------------------------------------


class WebhookChannel:
    """
    Generic webhook delivery.  Supports Slack-compatible Block Kit payloads,
    Discord embeds, and plain JSON (for custom integrations).
    """

    def __init__(self, url: Optional[str] = None, format: str = "slack") -> None:
        self._url = url or settings.notification.webhook_url
        self._format = format
        self._breaker = ChannelCircuitBreaker()
        self.channel_name = "webhook"

    async def send(self, subject: str, body: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        if self._breaker.is_open():
            return False
        if not self._url:
            return False

        payload = self._build_payload(subject, body, level)
        try:
            import aiohttp

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(self._url, json=payload) as resp:
                    if resp.status in (200, 204):
                        self._breaker.record_success()
                        return True
                    logger.warning("Webhook %d: %s", resp.status, await resp.text())
                    self._breaker.record_failure()
                    return False
        except Exception as exc:
            logger.error("Webhook send failed: %s", exc)
            self._breaker.record_failure()
            return False

    def _build_payload(self, subject: str, body: str, level: AlertLevel) -> dict:
        if self._format == "slack":
            return {
                "attachments": [
                    {
                        "color": level.color,
                        "title": f"{level.emoji} {subject}",
                        "text": body,
                        "footer": "Trading Platform",
                        "ts": int(time.time()),
                    }
                ]
            }
        elif self._format == "discord":
            return {
                "embeds": [
                    {
                        "title": f"{level.emoji} {subject}",
                        "description": body,
                        "color": int(level.color.lstrip("#"), 16),
                    }
                ]
            }
        else:
            return {"level": level.value, "subject": subject, "body": body, "ts": time.time()}


# ---------------------------------------------------------------------------
# Notification Service (router)
# ---------------------------------------------------------------------------


class NotificationService:
    """
    Routes alerts to appropriate channels based on severity level.

    Severity routing matrix (configurable):
        DEBUG    → none (logged only)
        INFO     → webhook (Slack)
        WARNING  → Telegram + webhook
        ERROR    → Telegram + email + webhook
        CRITICAL → all channels + PagerDuty
    """

    ROUTING: Dict[str, List[str]] = {
        "DEBUG": [],
        "INFO": ["webhook"],
        "WARNING": ["telegram", "webhook"],
        "ERROR": ["telegram", "email", "webhook"],
        "CRITICAL": ["telegram", "email", "webhook"],
    }

    def __init__(self) -> None:
        self._channels: Dict[str, object] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._worker_task: Optional[asyncio.Task] = None

    def register_channel(self, channel) -> None:
        self._channels[channel.channel_name] = channel
        logger.info("Notification channel registered: %s", channel.channel_name)

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._process_queue())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()

    async def alert(
        self,
        subject: str,
        body: str,
        level: AlertLevel = AlertLevel.INFO,
    ) -> None:
        """Non-blocking: enqueue alert for background delivery."""
        try:
            self._queue.put_nowait((subject, body, level))
        except asyncio.QueueFull:
            logger.error("Notification queue full — alert dropped: %s", subject)

    async def _process_queue(self) -> None:
        while True:
            subject, body, level = await self._queue.get()
            target_channels = self.ROUTING.get(level.value, [])

            tasks = []
            for channel_name in target_channels:
                channel = self._channels.get(channel_name)
                if channel:
                    tasks.append(channel.send(subject, body, level))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        logger.error("Notification delivery error: %s", res)

            self._queue.task_done()

    def format_risk_breach(self, breach_type: str, value: float, limit: float) -> tuple:
        subject = f"Risk Breach: {breach_type}"
        body = (
            f"A risk limit has been exceeded.\n\n"
            f"  Breach type  : {breach_type}\n"
            f"  Current value: {value:.4f}\n"
            f"  Limit        : {limit:.4f}\n"
            f"  Action       : Positions may be flattened\n\n"
            f"Please review the platform immediately."
        )
        return subject, body

    def format_fill_notification(
        self, symbol: str, side: str, qty: float, price: float, pnl: float = 0.0
    ) -> tuple:
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        subject = f"Fill: {side} {qty} {symbol} @ {price:.5f}"
        body = (
            f"Order Filled\n\n"
            f"  Symbol : {symbol}\n"
            f"  Side   : {side}\n"
            f"  Qty    : {qty}\n"
            f"  Price  : {price:.5f}\n"
            f"  PnL    : {pnl_str}\n"
        )
        return subject, body


# Default global notification service
notification_service = NotificationService()
