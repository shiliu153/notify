"""通道接口与重试策略。"""
import unittest

from notify.channels.base import (
    Channel,
    ChannelError,
    DeliveryResult,
    Envelope,
    PermanentChannelError,
    RetryableChannelError,
    deliver_with_retry,
)


class FakeChannel(Channel):
    def __init__(self, name="email:fake", sender="me@qq.com", failures=None, message_id="<mid@test>"):
        self.name = name
        self.sender = sender
        self._failures = list(failures or [])
        self.message_id = message_id
        self.calls = 0

    def ready(self):
        return True

    def deliver(self, envelope):
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return self.message_id


def make_envelope():
    return Envelope(
        sender="me@qq.com",
        from_name="服务器日报",
        recipients=["me@qq.com"],
        subject="[日报] 测试",
        text="纯文本",
        html="<p>HTML</p>",
    )


class TestRetryPolicy(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def test_success_on_first_attempt(self):
        channel = FakeChannel()
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.message_id, "<mid@test>")
        self.assertEqual(self.sleeps, [])
        self.assertIsNone(result.error_class)

    def test_retryable_error_is_retried_then_succeeds(self):
        channel = FakeChannel(failures=[RetryableChannelError("临时")] * 3)
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(channel.calls, 4)
        self.assertEqual(self.sleeps, [1.0, 4.0, 16.0])

    def test_retryable_error_exhausts_the_budget(self):
        channel = FakeChannel(failures=[RetryableChannelError("一直失败")] * 4)
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(result.error_class, "retryable")
        self.assertIn("一直失败", result.error)

    def test_permanent_error_is_not_retried(self):
        channel = FakeChannel(failures=[PermanentChannelError("535 认证失败")])
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error_class, "permanent")
        self.assertEqual(self.sleeps, [])

    def test_unexpected_exception_is_permanent(self):
        channel = FakeChannel(failures=[RuntimeError("意料之外")])
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.error_class, "permanent")
        self.assertIn("RuntimeError", result.error)

    def test_explicit_retryable_flag_on_base_error(self):
        channel = FakeChannel(failures=[ChannelError("可重试", retryable=True)] * 4)
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(result.error_class, "retryable")

    def test_custom_delays_are_honoured(self):
        channel = FakeChannel(failures=[RetryableChannelError("x")])
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep, delays=(0.5,))
        self.assertTrue(result.ok)
        self.assertEqual(self.sleeps, [0.5])

    def test_duration_is_recorded(self):
        channel = FakeChannel()
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertIsInstance(result.duration_ms, int)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_result_channel_name_comes_from_channel(self):
        channel = FakeChannel(name="email:qq")
        result = deliver_with_retry(channel, make_envelope(), sleep=self.sleep)
        self.assertEqual(result.channel, "email:qq")


class TestEnvelopeDefaults(unittest.TestCase):
    def test_channel_base_defaults(self):
        self.assertEqual(Channel.name, "channel")
        self.assertEqual(Channel.sender, "")

    def test_delivery_result_is_a_dataclass(self):
        result = DeliveryResult(channel="x", ok=True, attempts=1, duration_ms=3)
        self.assertIsNone(result.error)
        self.assertIsNone(result.message_id)


if __name__ == "__main__":
    unittest.main()
