"""通道接口、错误分类与重试策略。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

RETRY_DELAYS = (1.0, 4.0, 16.0)
TIMEOUT_SECONDS = 10


class ChannelError(Exception):
    """通道投递失败。retryable 决定是否值得重试。"""

    retryable = False

    def __init__(self, message: str, retryable: Optional[bool] = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class RetryableChannelError(ChannelError):
    retryable = True


class PermanentChannelError(ChannelError):
    retryable = False


@dataclass
class Envelope:
    """一封渲染完成的邮件（发件人随通道变化）。"""

    sender: str
    from_name: str
    recipients: List[str]
    subject: str
    text: str
    html: str


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    attempts: int
    duration_ms: int
    error: Optional[str] = None
    error_class: Optional[str] = None
    message_id: Optional[str] = None


class Channel:
    """通道接口：新增推送类通道只需实现 ready() 与 deliver()。"""

    name = "channel"
    sender = ""

    def ready(self) -> bool:
        raise NotImplementedError

    def deliver(self, envelope: Envelope) -> Optional[str]:
        raise NotImplementedError


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def deliver_with_retry(
    channel: Channel,
    envelope: Envelope,
    sleep=time.sleep,
    delays: Sequence[float] = RETRY_DELAYS,
) -> DeliveryResult:
    """投递一封邮件：可重试错误退避重试，永久错误立刻放弃。"""
    started = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    last_class: Optional[str] = None

    for delay in (0.0,) + tuple(delays):
        if delay:
            sleep(delay)
        attempts += 1
        try:
            message_id = channel.deliver(envelope)
        except ChannelError as exc:
            last_error = str(exc)
            last_class = "retryable" if exc.retryable else "permanent"
            if not exc.retryable:
                break
        except Exception as exc:  # 未预期异常按永久错误处理，不重试
            last_error = "%s: %s" % (type(exc).__name__, exc)
            last_class = "permanent"
            break
        else:
            return DeliveryResult(
                channel=channel.name,
                ok=True,
                attempts=attempts,
                duration_ms=_elapsed_ms(started),
                message_id=message_id,
            )

    return DeliveryResult(
        channel=channel.name,
        ok=False,
        attempts=attempts,
        duration_ms=_elapsed_ms(started),
        error=last_error,
        error_class=last_class,
    )
