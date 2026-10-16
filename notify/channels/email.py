"""SMTP 邮件通道（DirectMail 与 QQ 邮箱共用同一实现）。"""
from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

from .base import (
    TIMEOUT_SECONDS,
    Channel,
    Envelope,
    PermanentChannelError,
    RetryableChannelError,
)

RETRYABLE_SMTP_CODES = frozenset({421, 450, 451, 452})


def build_message(envelope: Envelope) -> EmailMessage:
    """构造 multipart/alternative 邮件（纯文本 + HTML）。

    EmailMessage 走的是 EmailPolicy，头部必须是 str——传 Header 对象会报
    "Header object is not subscriptable"。中文由 EmailPolicy 自动做 RFC 2047 编码。
    """
    message = EmailMessage()
    message["From"] = formataddr((envelope.from_name, envelope.sender))
    message["To"] = ", ".join(envelope.recipients)
    message["Subject"] = envelope.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message["Reply-To"] = envelope.sender
    message.set_content(envelope.text)
    message.add_alternative(envelope.html, subtype="html")
    return message


def map_smtp_error(exc: Exception) -> Exception:
    """把 smtplib/socket 异常映射成带重试语义的 ChannelError。"""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return PermanentChannelError(
            "SMTP 认证失败（%s）：检查发信地址与密码/授权码" % exc.smtp_code
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = set(info[0] for info in exc.recipients.values())
        if codes & RETRYABLE_SMTP_CODES:
            return RetryableChannelError("收件人被临时拒绝：%s" % exc.recipients)
        return PermanentChannelError("收件人被拒绝：%s" % exc.recipients)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return PermanentChannelError("发件人被拒绝：%s" % exc.smtp_error)
    if isinstance(exc, smtplib.SMTPResponseException):
        if exc.smtp_code in RETRYABLE_SMTP_CODES:
            return RetryableChannelError(
                "SMTP 临时错误 %s：%s" % (exc.smtp_code, exc.smtp_error)
            )
        return PermanentChannelError(
            "SMTP 错误 %s：%s" % (exc.smtp_code, exc.smtp_error)
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return RetryableChannelError("连接超时：%s" % exc)
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError, OSError)):
        return RetryableChannelError("网络错误：%s: %s" % (type(exc).__name__, exc))
    if isinstance(exc, smtplib.SMTPException):
        return PermanentChannelError("SMTP 异常：%s" % exc)
    return exc


class SmtpTransport:
    """真实 SMTP 传输层。测试用替身替换（鸭子类型：只需 send(envelope)）。"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        security: str,
        timeout: int = TIMEOUT_SECONDS,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.security = security
        self.timeout = timeout

    def send(self, envelope: Envelope) -> Optional[str]:
        message = build_message(envelope)
        client = None
        try:
            if self.security == "ssl":
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                client.ehlo()
                client.starttls()
                client.ehlo()
            client.login(self.username, self.password)
            client.send_message(message)
        except Exception as exc:
            mapped = map_smtp_error(exc)
            if mapped is exc:
                raise
            raise mapped
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass
        return message["Message-ID"]


class EmailChannel(Channel):
    def __init__(self, config, transport=None):
        self.config = config
        self.name = "email:" + config.name
        self._transport = transport

    @property
    def sender(self) -> str:
        return self.config.username

    def ready(self) -> bool:
        return bool(self.config.enabled and self.config.password)

    def deliver(self, envelope: Envelope) -> Optional[str]:
        if not self.ready():
            raise PermanentChannelError("通道 %s 未就绪（被禁用或缺密码）" % self.name)
        transport = self._transport or SmtpTransport(
            host=self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            security=self.config.security,
        )
        return transport.send(envelope)
