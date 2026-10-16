"""SMTP 邮件通道：报文构造、错误分类、传输层注入。"""
import smtplib
import socket
import unittest
from email.header import decode_header, make_header

from notify.channels.base import Envelope, PermanentChannelError, RetryableChannelError
from notify.channels.email import (
    EmailChannel,
    build_message,
    map_smtp_error,
)
from notify.config import EmailChannelConfig


def make_config(**overrides):
    values = dict(
        name="qq",
        enabled=True,
        host="smtp.qq.com",
        port=465,
        username="me@qq.com",
        password="authcode",
        security="ssl",
    )
    values.update(overrides)
    return EmailChannelConfig(**values)


def make_envelope(**overrides):
    values = dict(
        sender="me@qq.com",
        from_name="服务器日报",
        recipients=["me@qq.com"],
        subject="[日报] 2026-10-16",
        text="纯文本正文\n第二行",
        html="<p>HTML 正文</p>",
    )
    values.update(overrides)
    return Envelope(**values)


class RecordingTransport:
    def __init__(self, error=None):
        self.envelopes = []
        self.error = error

    def send(self, envelope):
        self.envelopes.append(envelope)
        if self.error:
            raise self.error
        return "<recorded@test>"


class TestBuildMessage(unittest.TestCase):
    def setUp(self):
        self.msg = build_message(make_envelope())

    def test_has_both_text_and_html_parts(self):
        self.assertTrue(self.msg.is_multipart())
        subtypes = [part.get_content_subtype() for part in self.msg.iter_parts()]
        self.assertIn("plain", subtypes)
        self.assertIn("html", subtypes)

    def test_subject_round_trips_through_utf8(self):
        decoded = str(make_header(decode_header(self.msg["Subject"])))
        self.assertEqual(decoded, "[日报] 2026-10-16")

    def test_from_carries_display_name(self):
        decoded = str(make_header(decode_header(self.msg["From"])))
        self.assertIn("服务器日报", decoded)
        self.assertIn("me@qq.com", decoded)

    def test_reply_to_is_the_sender(self):
        self.assertIn("me@qq.com", self.msg["Reply-To"])

    def test_message_id_and_date_present(self):
        self.assertTrue(self.msg["Message-ID"].startswith("<"))
        self.assertIn(",", self.msg["Date"])

    def test_text_body_survives_encoding(self):
        part = [p for p in self.msg.iter_parts() if p.get_content_subtype() == "plain"][0]
        self.assertIn("纯文本正文", part.get_payload(decode=True).decode("utf-8"))

    def test_html_body_survives_encoding(self):
        part = [p for p in self.msg.iter_parts() if p.get_content_subtype() == "html"][0]
        self.assertIn("<p>HTML 正文</p>", part.get_payload(decode=True).decode("utf-8"))

    def test_multiple_recipients_are_joined(self):
        msg = build_message(make_envelope(recipients=["a@qq.com", "b@163.com"]))
        self.assertIn("a@qq.com", msg["To"])
        self.assertIn("b@163.com", msg["To"])


class TestErrorMapping(unittest.TestCase):
    def test_authentication_error_is_permanent(self):
        mapped = map_smtp_error(smtplib.SMTPAuthenticationError(535, b"auth failed"))
        self.assertIsInstance(mapped, PermanentChannelError)
        self.assertIn("认证失败", str(mapped))

    def test_451_is_retryable(self):
        mapped = map_smtp_error(smtplib.SMTPResponseException(451, b"try later"))
        self.assertIsInstance(mapped, RetryableChannelError)

    def test_550_is_permanent(self):
        mapped = map_smtp_error(smtplib.SMTPResponseException(550, b"no such user"))
        self.assertIsInstance(mapped, PermanentChannelError)

    def test_timeout_is_retryable(self):
        self.assertIsInstance(map_smtp_error(socket.timeout("timed out")), RetryableChannelError)

    def test_connection_refused_is_retryable(self):
        self.assertIsInstance(
            map_smtp_error(ConnectionRefusedError("refused")), RetryableChannelError
        )

    def test_server_disconnected_is_retryable(self):
        self.assertIsInstance(
            map_smtp_error(smtplib.SMTPServerDisconnected("gone")), RetryableChannelError
        )

    def test_temporary_recipient_refusal_is_retryable(self):
        exc = smtplib.SMTPRecipientsRefused({"a@qq.com": (450, b"mailbox busy")})
        self.assertIsInstance(map_smtp_error(exc), RetryableChannelError)

    def test_permanent_recipient_refusal_is_permanent(self):
        exc = smtplib.SMTPRecipientsRefused({"a@qq.com": (550, b"no such user")})
        self.assertIsInstance(map_smtp_error(exc), PermanentChannelError)

    def test_sender_refused_is_permanent(self):
        exc = smtplib.SMTPSenderRefused(553, b"bad sender", "me@qq.com")
        self.assertIsInstance(map_smtp_error(exc), PermanentChannelError)


class TestEmailChannel(unittest.TestCase):
    def test_name_and_sender_follow_config(self):
        channel = EmailChannel(make_config(name="directmail", username="notice@example.com"))
        self.assertEqual(channel.name, "email:directmail")
        self.assertEqual(channel.sender, "notice@example.com")

    def test_ready_requires_password(self):
        self.assertFalse(EmailChannel(make_config(password=None)).ready())

    def test_disabled_channel_is_not_ready(self):
        self.assertFalse(EmailChannel(make_config(enabled=False)).ready())

    def test_ready_when_enabled_and_password_present(self):
        self.assertTrue(EmailChannel(make_config()).ready())

    def test_deliver_delegates_to_injected_transport(self):
        transport = RecordingTransport()
        channel = EmailChannel(make_config(), transport=transport)
        envelope = make_envelope()
        message_id = channel.deliver(envelope)
        self.assertEqual(message_id, "<recorded@test>")
        self.assertEqual(transport.envelopes, [envelope])

    def test_unready_channel_refuses_to_deliver(self):
        channel = EmailChannel(make_config(password=None), transport=RecordingTransport())
        with self.assertRaises(PermanentChannelError):
            channel.deliver(make_envelope())


if __name__ == "__main__":
    unittest.main()
