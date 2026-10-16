"""编排：去重 → 限流 → 渲染 → 投递 → 重试 → 降级。"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .channels.base import Channel, DeliveryResult, Envelope, deliver_with_retry
from .channels.email import EmailChannel
from .config import Config
from .log import format_ts, log_event
from .message import Message
from .render import render_email_html, render_email_text

EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_CONFIG = 2
EXIT_SKIPPED = 3


@dataclass
class SendOutcome:
    exit_code: int
    subject: str = ""
    results: List[DeliveryResult] = field(default_factory=list)
    skipped: Optional[str] = None
    message_id: Optional[str] = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


def build_channels(config: Config) -> List[Channel]:
    """按配置顺序构造通道。"""
    return [EmailChannel(channel) for channel in config.channels]


def make_subject(config: Config, message: Message) -> str:
    parts = [config.subject_prefix, message.subject_tag, message.title]
    return " ".join(part for part in parts if part)


def select_channels(channels: Sequence[Channel], selector: str = "auto") -> List[Channel]:
    """auto/email 取所有就绪通道；email:<名称> 只取指定通道（不走降级）。"""
    if selector in ("auto", "email"):
        return [channel for channel in channels if channel.ready()]
    if selector.startswith("email:"):
        wanted = selector.split(":", 1)[1]
        return [
            channel
            for channel in channels
            if channel.name == "email:" + wanted and channel.ready()
        ]
    raise ValueError("未知的 --to 取值：%r" % selector)


def build_envelope(
    config: Config,
    message: Message,
    subject: str,
    hostname: str,
    sender: str,
) -> Envelope:
    timestamp = message.created_at or format_ts()
    return Envelope(
        sender=sender,
        from_name=config.from_name,
        recipients=list(config.recipients),
        subject=subject,
        text=render_email_text(message, hostname, timestamp),
        html=render_email_html(message, hostname, timestamp),
    )


def _log(log_path, event: str, message: Message, **fields):
    if not log_path:
        return None
    return log_event(
        log_path, event, title=message.title, level=message.level, **fields
    )


def send_message(
    config: Config,
    message: Message,
    *,
    selector: str = "auto",
    dedup_window: Optional[int] = None,
    dry_run: bool = False,
    store,
    channels: Sequence[Channel],
    log_path=None,
    sleep=time.sleep,
    hostname: Optional[str] = None,
) -> SendOutcome:
    """投递一条消息，返回带退出码的结果。"""
    subject = make_subject(config, message)
    dedup_key = message.effective_dedup_key
    window = config.limits.dedup_window_minutes if dedup_window is None else dedup_window
    host = hostname or socket.gethostname()

    if store.is_duplicate(dedup_key, window):
        _log(log_path, "skipped", message, dedup_key=dedup_key, reason="duplicate")
        return SendOutcome(EXIT_SKIPPED, subject=subject, skipped="duplicate")

    limited = store.rate_limited(config.limits.per_minute, config.limits.per_day)
    if limited:
        reason = "rate:" + limited
        _log(log_path, "skipped", message, dedup_key=dedup_key, reason=reason)
        return SendOutcome(EXIT_SKIPPED, subject=subject, skipped=reason)

    try:
        targets = select_channels(channels, selector)
    except ValueError as exc:
        _log(log_path, "config_error", message, error=str(exc))
        return SendOutcome(EXIT_CONFIG, subject=subject, skipped=str(exc))

    if not targets:
        _log(log_path, "config_error", message, error="no-ready-channel", selector=selector)
        return SendOutcome(EXIT_CONFIG, subject=subject, skipped="no-ready-channel")

    if dry_run:
        return SendOutcome(EXIT_OK, subject=subject, dry_run=True)

    results: List[DeliveryResult] = []
    for channel in targets:
        envelope = build_envelope(config, message, subject, host, channel.sender)
        result = deliver_with_retry(channel, envelope, sleep=sleep)
        results.append(result)
        store.record_attempt(channel.name, result.ok)
        _log(
            log_path,
            "delivery",
            message,
            dedup_key=dedup_key,
            channel=result.channel,
            ok=result.ok,
            attempts=result.attempts,
            duration_ms=result.duration_ms,
            error=result.error,
            error_class=result.error_class,
            message_id=result.message_id,
        )
        if result.ok:
            store.mark_sent(dedup_key)
            return SendOutcome(
                EXIT_OK,
                subject=subject,
                results=results,
                message_id=result.message_id,
            )

    return SendOutcome(EXIT_ALL_FAILED, subject=subject, results=results)
