from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from email.message import EmailMessage
from typing import Any

import httpx

from tunarr_autoscheduler.db.database import Database
from tunarr_autoscheduler.models.config import AppConfig, NotificationRuleConfig


@dataclass
class NotificationMessage:
    event_type: str
    title: str
    message: str
    severity: str = "info"
    channel_id: str = ""
    details: dict[str, Any] | None = None


class NotificationRouter:
    def __init__(self, *, config: AppConfig, db: Database | None = None):
        self._config = config
        self._db = db
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, notification: NotificationMessage) -> list[dict[str, Any]]:
        if not self._config.notifications.enabled:
            return []
        results: list[dict[str, Any]] = []
        rules = _matching_rules(self._config.notifications.rules, notification.event_type)
        for rule in rules:
            if not rule.enabled or _in_quiet_hours(rule):
                continue
            for provider in _rule_providers(rule):
                if await self._is_throttled(notification.event_type, provider, rule):
                    results.append(await self._record(
                        notification,
                        provider,
                        "throttled",
                        {"rule": rule.model_dump(mode="json")},
                    ))
                    continue
                status = "sent"
                details: dict[str, Any] = {"rule": rule.model_dump(mode="json")}
                try:
                    await self._send_provider(provider, notification)
                except Exception as e:
                    status = "failed"
                    details["error"] = str(e)
                results.append(await self._record(notification, provider, status, details))
        return results

    async def _send_provider(self, provider: str, notification: NotificationMessage) -> None:
        if provider == "telegram":
            await self._send_telegram(notification)
        elif provider == "email":
            await self._send_email(notification)
        elif provider == "webhook":
            await self._send_webhook(notification)
        else:
            raise ValueError(f"Unknown notification provider: {provider}")

    async def _send_telegram(self, notification: NotificationMessage) -> None:
        config = self._config.notifications.telegram
        if not config.enabled or not config.bot_token or not config.chat_id:
            raise RuntimeError("Telegram notifications are not configured")
        response = await self._client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json={
                "chat_id": config.chat_id,
                "text": _plain_text(notification),
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()

    async def _send_email(self, notification: NotificationMessage) -> None:
        config = self._config.notifications.email
        if not config.enabled or not config.smtp_host or not config.to_addresses:
            raise RuntimeError("Email notifications are not configured")
        await asyncio.to_thread(_send_email_sync, config, notification)

    async def _send_webhook(self, notification: NotificationMessage) -> None:
        config = self._config.notifications.webhook
        if not config.enabled or not config.url:
            raise RuntimeError("Webhook notifications are not configured")
        response = await self._client.post(
            config.url,
            headers=config.headers,
            json={
                "event_type": notification.event_type,
                "title": notification.title,
                "message": notification.message,
                "severity": notification.severity,
                "channel_id": notification.channel_id,
                "details": notification.details or {},
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        response.raise_for_status()

    async def _is_throttled(
        self,
        event_type: str,
        provider: str,
        rule: NotificationRuleConfig,
    ) -> bool:
        if self._db is None or rule.throttle_minutes <= 0:
            return False
        row = await self._db.fetch_one(
            "SELECT created_at FROM notification_events "
            "WHERE event_type = ? AND provider = ? AND status = 'sent' "
            "ORDER BY created_at DESC LIMIT 1",
            event_type,
            provider,
        )
        if row is None:
            return False
        try:
            created_at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            return False
        return (datetime.now(UTC) - created_at).total_seconds() < rule.throttle_minutes * 60

    async def _record(
        self,
        notification: NotificationMessage,
        provider: str,
        status: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()),
            "event_type": notification.event_type,
            "provider": provider,
            "status": status,
            "title": notification.title,
            "message": notification.message,
            "details": {**(notification.details or {}), **details},
            "created_at": datetime.now(UTC).isoformat(),
        }
        if self._db is not None:
            await self._db.execute(
                "INSERT INTO notification_events "
                "(id, event_type, provider, status, title, message, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                record["id"],
                record["event_type"],
                record["provider"],
                record["status"],
                record["title"],
                record["message"],
                json.dumps(record["details"], default=str),
                record["created_at"],
            )
        return record


async def list_notification_events(
    db: Database,
    *,
    event_type: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.append(max(1, min(limit, 200)))
    rows = await db.fetch_all(
        f"SELECT * FROM notification_events {where}ORDER BY created_at DESC LIMIT ?",
        *params,
    )
    events: list[dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        try:
            event["details"] = json.loads(str(event.get("details_json") or "{}"))
        except json.JSONDecodeError:
            event["details"] = {}
        events.append(event)
    return events


async def send_notification(
    router: NotificationRouter | None,
    notification: NotificationMessage,
) -> list[dict[str, Any]]:
    if router is None:
        return []
    return await router.send(notification)


def _matching_rules(
    rules: list[NotificationRuleConfig],
    event_type: str,
) -> list[NotificationRuleConfig]:
    exact = [rule for rule in rules if rule.event_type == event_type]
    wildcard = [rule for rule in rules if rule.event_type == "*"]
    return exact or wildcard


def _rule_providers(rule: NotificationRuleConfig) -> list[str]:
    return [
        provider
        for provider in rule.providers
        if provider in {"telegram", "email", "webhook"}
    ]


def _in_quiet_hours(rule: NotificationRuleConfig) -> bool:
    if not rule.quiet_hours_start or not rule.quiet_hours_end:
        return False
    start = _parse_time(rule.quiet_hours_start)
    end = _parse_time(rule.quiet_hours_end)
    if start is None or end is None:
        return False
    now = datetime.now().time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _parse_time(value: str) -> time | None:
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return None


def _plain_text(notification: NotificationMessage) -> str:
    lines = [
        f"[{notification.severity.upper()}] {notification.title}",
        notification.message,
    ]
    if notification.channel_id:
        lines.append(f"Channel: {notification.channel_id}")
    if notification.details:
        lines.append(json.dumps(notification.details, indent=2, default=str))
    return "\n".join(line for line in lines if line)


def _send_email_sync(config: Any, notification: NotificationMessage) -> None:
    message = EmailMessage()
    message["Subject"] = f"[FlixWolf Scheduler] {notification.title}"
    message["From"] = config.from_address or config.username
    message["To"] = ", ".join(config.to_addresses)
    message.set_content(_plain_text(notification))

    security = getattr(config, "smtp_security", "starttls" if config.use_tls else "plain")
    if security == "ssl":
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            timeout=10,
            context=ssl.create_default_context(),
        ) as smtp:
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
    elif security == "starttls":
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as smtp:
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
