"""编排：去重、限流、渲染、投递、降级。"""
import tempfile
import unittest
from pathlib import Path

from notify.channels.base import Channel, PermanentChannelError, RetryableChannelError
from notify.config import Config, EmailChannelConfig, Limits
from notify.core import (
    EXIT_ALL_FAILED,
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_SKIPPED,
    build_envelope,
    make_subject,
    select_channels,
    send_message,
)
from notify.log import tail
from notify.message import Message
from notify.store import Store


class FakeChannel(Channel):
    def __init__(self, name, sender, ready=True, failures=None, message_id="<mid@test>"):
        self.name = name
        self.sender = sender
        self._ready = ready
        self._failures = list(failures or [])
        self.message_id = message_id
        self.calls = 0
        self.envelopes = []

    def ready(self):
        return self._ready

    def deliver(self, envelope):
        self.calls += 1
        self.envelopes.append(envelope)
        if self._failures:
            raise self._failures.pop(0)
        return self.message_id


def make_config(**overrides):
    values = dict(
        path=Path("/tmp/config.ini"),
        recipients=["me@qq.com"],
        from_name="服务器日报",
        subject_prefix="[日报]",
        channels=[
            EmailChannelConfig("directmail", True, "smtpdm.aliyun.com", 465,
                               "notice@example.com", "dm-secret", "ssl"),
            EmailChannelConfig("qq", True, "smtp.qq.com", 465,
                               "me@qq.com", "qq-authcode", "ssl"),
        ],
        limits=Limits(per_minute=5, per_day=50, dedup_window_minutes=720),
        log_path=Path("/tmp/notify.jsonl"),
        state_path=Path("/tmp/state.db"),
    )
    values.update(overrides)
    return Config(**values)


class CoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.store = Store(self.root / "state.db")
        self.addCleanup(self.store.close)
        self.log_path = self.root / "notify.jsonl"
        self.sleeps = []
        self.primary = FakeChannel("email:directmail", "notice@example.com")
        self.secondary = FakeChannel("email:qq", "me@qq.com")
        self.config = make_config(log_path=self.log_path, state_path=self.root / "state.db")
        self.message = Message(title="服务器日报", body="- 备份完成", dedup_key="daily-digest")

    def send(self, **overrides):
        message = overrides.pop("message", self.message)
        kwargs = dict(
            store=self.store,
            channels=[self.primary, self.secondary],
            log_path=self.log_path,
            sleep=self.sleeps.append,
            hostname="ecs-01",
        )
        kwargs.update(overrides)
        return send_message(self.config, message, **kwargs)


class TestHappyPath(CoreTestCase):
    def test_success_returns_exit_0(self):
        outcome = self.send()
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message_id, "<mid@test>")
        self.assertEqual(len(outcome.results), 1)

    def test_primary_channel_is_used_first(self):
        self.send()
        self.assertEqual(self.primary.calls, 1)
        self.assertEqual(self.secondary.calls, 0)

    def test_success_marks_the_dedup_key(self):
        self.send()
        self.assertTrue(self.store.is_duplicate("daily-digest", 720))

    def test_second_send_is_skipped_as_duplicate(self):
        self.send()
        outcome = self.send()
        self.assertEqual(outcome.exit_code, EXIT_SKIPPED)
        self.assertEqual(outcome.skipped, "duplicate")
        self.assertEqual(self.primary.calls, 1)

    def test_dedup_window_zero_allows_resend(self):
        self.send()
        outcome = self.send(dedup_window=0)
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertEqual(self.primary.calls, 2)

    def test_envelope_sender_follows_the_channel(self):
        self.send()
        self.assertEqual(self.primary.envelopes[0].sender, "notice@example.com")

    def test_envelope_recipients_come_from_config(self):
        self.send()
        self.assertEqual(self.primary.envelopes[0].recipients, ["me@qq.com"])

    def test_envelope_carries_rendered_bodies(self):
        self.send()
        envelope = self.primary.envelopes[0]
        self.assertIn("服务器日报", envelope.text)
        self.assertIn("ecs-01", envelope.text)
        self.assertIn("<table", envelope.html)

    def test_delivery_is_logged(self):
        self.send()
        records = tail(self.log_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "delivery")
        self.assertEqual(records[0]["channel"], "email:directmail")
        self.assertTrue(records[0]["ok"])
        self.assertEqual(records[0]["dedup_key"], "daily-digest")

    def test_skipped_send_is_logged(self):
        self.send()
        self.send()
        self.assertEqual(tail(self.log_path)[-1]["event"], "skipped")


class TestFallback(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.primary = FakeChannel(
            "email:directmail", "notice@example.com",
            failures=[PermanentChannelError("535 认证失败")],
        )

    def test_falls_back_to_secondary(self):
        outcome = self.send()
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertEqual([r.channel for r in outcome.results], ["email:directmail", "email:qq"])
        self.assertFalse(outcome.results[0].ok)
        self.assertTrue(outcome.results[1].ok)
        self.assertEqual(outcome.message_id, "<mid@test>")

    def test_marks_sent_only_after_a_success(self):
        self.send()
        self.assertTrue(self.store.is_duplicate("daily-digest", 720))

    def test_all_channels_failing_returns_exit_1(self):
        self.secondary = FakeChannel(
            "email:qq", "me@qq.com", failures=[PermanentChannelError("550 拒绝")]
        )
        outcome = self.send()
        self.assertEqual(outcome.exit_code, EXIT_ALL_FAILED)
        self.assertFalse(self.store.is_duplicate("daily-digest", 720))
        self.assertEqual(len(outcome.results), 2)

    def test_explicit_selector_does_not_fall_back(self):
        outcome = self.send(selector="email:directmail")
        self.assertEqual(outcome.exit_code, EXIT_ALL_FAILED)
        self.assertEqual(self.secondary.calls, 0)


class TestGuards(CoreTestCase):
    def test_rate_limit_returns_exit_3(self):
        for _ in range(5):
            self.store.record_attempt("email:directmail", True)
        outcome = self.send()
        self.assertEqual(outcome.exit_code, EXIT_SKIPPED)
        self.assertEqual(outcome.skipped, "rate:per_minute")
        self.assertEqual(self.primary.calls, 0)

    def test_no_ready_channel_returns_exit_2(self):
        outcome = self.send(channels=[])
        self.assertEqual(outcome.exit_code, EXIT_CONFIG)
        self.assertEqual(outcome.skipped, "no-ready-channel")

    def test_unknown_selector_returns_exit_2(self):
        outcome = self.send(selector="sms:twilio")
        self.assertEqual(outcome.exit_code, EXIT_CONFIG)

    def test_dry_run_does_not_deliver(self):
        outcome = self.send(dry_run=True)
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertTrue(outcome.dry_run)
        self.assertEqual(self.primary.calls, 0)
        self.assertFalse(self.store.is_duplicate("daily-digest", 720))

    def test_failed_attempts_are_recorded_for_rate_limiting(self):
        self.primary = FakeChannel(
            "email:directmail", "notice@example.com",
            failures=[PermanentChannelError("x")],
        )
        self.send()
        self.assertEqual(self.store.count_attempts(0), 2)


class TestSelectors(unittest.TestCase):
    def test_auto_returns_only_ready_channels(self):
        ready = FakeChannel("email:directmail", "a@b.c")
        not_ready = FakeChannel("email:qq", "a@b.c", ready=False)
        self.assertEqual(select_channels([ready, not_ready]), [ready])

    def test_email_selector_matches_auto(self):
        ready = FakeChannel("email:directmail", "a@b.c")
        self.assertEqual(select_channels([ready], "email"), [ready])

    def test_named_selector_picks_one(self):
        a = FakeChannel("email:directmail", "a@b.c")
        b = FakeChannel("email:qq", "a@b.c")
        self.assertEqual(select_channels([a, b], "email:qq"), [b])

    def test_unknown_selector_raises(self):
        with self.assertRaises(ValueError):
            select_channels([], "sms")


class TestSubject(unittest.TestCase):
    def test_includes_prefix_and_title(self):
        config = make_config()
        self.assertEqual(make_subject(config, Message(title="日报", body="x")), "[日报] 日报")

    def test_warn_level_adds_tag(self):
        config = make_config()
        subject = make_subject(config, Message(title="磁盘", body="x", level="warn"))
        self.assertEqual(subject, "[日报] [注意] 磁盘")

    def test_empty_prefix_is_skipped(self):
        config = make_config(subject_prefix="")
        self.assertEqual(make_subject(config, Message(title="日报", body="x")), "日报")


class TestBuildEnvelope(unittest.TestCase):
    def test_uses_message_created_at_when_present(self):
        config = make_config()
        message = Message(title="t", body="b", created_at="2026-10-16 08:00:00")
        envelope = build_envelope(config, message, "s", "ecs-01", "me@qq.com")
        self.assertIn("2026-10-16 08:00:00", envelope.text)
        self.assertIn("ecs-01", envelope.text)

    def test_from_name_comes_from_config(self):
        config = make_config()
        envelope = build_envelope(config, Message(title="t", body="b"), "s", "h", "me@qq.com")
        self.assertEqual(envelope.from_name, "服务器日报")


if __name__ == "__main__":
    unittest.main()
