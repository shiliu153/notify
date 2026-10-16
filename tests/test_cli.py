"""CLI：参数校验、正文读取、退出码、输出格式。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notify.channels.base import Channel, PermanentChannelError
from notify.cli import main
from notify.config import Config, EmailChannelConfig, Limits
from notify.core import EXIT_ALL_FAILED, EXIT_CONFIG, EXIT_OK, EXIT_SKIPPED
from notify.store import Store


class FakeChannel(Channel):
    def __init__(self, name, sender="me@qq.com", ready=True, failures=None):
        self.name = name
        self.sender = sender
        self._ready = ready
        self._failures = list(failures or [])
        self.calls = 0
        self.envelopes = []
        # doctor 与 channels 子命令会读 channel.config，替身也要带上
        self.config = EmailChannelConfig(
            name.split(":")[-1], True, "smtp.qq.com", 465, sender, "pw", "ssl"
        )

    def ready(self):
        return self._ready

    def deliver(self, envelope):
        self.calls += 1
        self.envelopes.append(envelope)
        if self._failures:
            raise self._failures.pop(0)
        return "<mid@test>"


_UNSET = object()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.store = Store(self.root / "state.db")
        self.addCleanup(self.store.close)
        self.config = Config(
            path=self.root / "config.ini",
            recipients=["me@qq.com"],
            from_name="服务器日报",
            subject_prefix="[日报]",
            channels=[
                EmailChannelConfig("directmail", True, "smtpdm.aliyun.com", 465,
                                   "notice@example.com", "dm-secret", "ssl"),
            ],
            limits=Limits(),
            log_path=self.root / "notify.jsonl",
            state_path=self.root / "state.db",
        )
        self.channel = FakeChannel("email:directmail", "notice@example.com")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_cli(self, argv, channels=None, config=_UNSET):
        return main(
            argv,
            config=self.config if config is _UNSET else config,
            channels=[self.channel] if channels is None else channels,
            store=self.store,
            sleep=lambda seconds: None,
            out=self.out,
            err=self.err,
        )


class TestSendCommand(CliTestCase):
    def test_success_returns_0_and_prints_tick(self):
        code = self.run_cli(["send", "--title", "日报", "--body", "一切正常"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("✓", self.out.getvalue())
        self.assertIn("email:directmail", self.out.getvalue())

    def test_title_is_required(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["send", "--body", "正文"])
        self.assertEqual(ctx.exception.code, 2)

    def test_body_and_body_file_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["send", "--title", "t", "--body", "b", "--body-file", "x.md"])
        self.assertEqual(ctx.exception.code, 2)

    def test_one_of_body_or_body_file_is_required(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["send", "--title", "t"])
        self.assertEqual(ctx.exception.code, 2)

    def test_reads_body_from_file(self):
        body_file = self.root / "daily.md"
        body_file.write_text("- 备份完成\n- 磁盘 40%", encoding="utf-8")
        code = self.run_cli(["send", "--title", "日报", "--body-file", str(body_file)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("备份完成", self.channel.envelopes[0].text)

    def test_reads_body_from_stdin_when_dash(self):
        with mock.patch("sys.stdin", io.StringIO("来自标准输入")):
            code = self.run_cli(["send", "--title", "日报", "--body-file", "-"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("来自标准输入", self.channel.envelopes[0].text)

    def test_empty_body_returns_2(self):
        code = self.run_cli(["send", "--title", "日报", "--body", "   "])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("body", self.err.getvalue())

    def test_unknown_level_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["send", "--title", "t", "--body", "b", "--level", "fatal"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_to_value_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["send", "--title", "t", "--body", "b", "--to", "sms"])
        self.assertEqual(ctx.exception.code, 2)

    def test_duplicate_returns_3(self):
        self.run_cli(["send", "--title", "日报", "--body", "b", "--dedup-key", "daily"])
        self.out.seek(0)
        self.out.truncate(0)
        code = self.run_cli(["send", "--title", "日报", "--body", "b", "--dedup-key", "daily"])
        self.assertEqual(code, EXIT_SKIPPED)
        self.assertIn("跳过", self.out.getvalue())

    def test_dedup_window_zero_allows_resend(self):
        self.run_cli(["send", "--title", "日报", "--body", "b", "--dedup-key", "daily"])
        code = self.run_cli(
            ["send", "--title", "日报", "--body", "b", "--dedup-key", "daily", "--dedup-window", "0"]
        )
        self.assertEqual(code, EXIT_OK)

    def test_all_channels_failing_returns_1(self):
        self.channel = FakeChannel(
            "email:directmail", "notice@example.com",
            failures=[PermanentChannelError("535 认证失败")],
        )
        code = self.run_cli(["send", "--title", "日报", "--body", "b"])
        self.assertEqual(code, EXIT_ALL_FAILED)
        self.assertIn("535", self.out.getvalue())

    def test_no_ready_channel_returns_2(self):
        code = self.run_cli(["send", "--title", "日报", "--body", "b"], channels=[])
        self.assertEqual(code, EXIT_CONFIG)

    def test_dry_run_returns_0_without_delivering(self):
        code = self.run_cli(["send", "--title", "日报", "--body", "b", "--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.channel.calls, 0)
        self.assertIn("dry-run", self.out.getvalue())
        self.assertIn("me@qq.com", self.out.getvalue())

    def test_json_output_is_parseable(self):
        code = self.run_cli(["send", "--title", "日报", "--body", "b", "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(self.out.getvalue())
        self.assertEqual(payload["exit_code"], EXIT_OK)
        self.assertEqual(payload["channels"][0]["channel"], "email:directmail")
        self.assertTrue(payload["channels"][0]["ok"])

    def test_fallback_is_reported(self):
        self.channel = FakeChannel(
            "email:directmail", "notice@example.com",
            failures=[PermanentChannelError("535 认证失败")],
        )
        backup = FakeChannel("email:qq", "me@qq.com")
        code = self.run_cli(
            ["send", "--title", "日报", "--body", "b"], channels=[self.channel, backup]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("已降级", self.out.getvalue())

    def test_missing_config_file_returns_2(self):
        code = self.run_cli(
            ["send", "--title", "日报", "--body", "b", "--config", str(self.root / "nope.ini")],
            config=None,
        )
        self.assertEqual(code, EXIT_CONFIG)

    def test_no_subcommand_prints_help_and_returns_2(self):
        code = self.run_cli([])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("usage", self.err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
