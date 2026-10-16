"""配置加载：INI 解析、权限校验、密钥注入。"""
import os
import tempfile
import unittest
from pathlib import Path

from notify.config import ConfigError, load_config

BASE_INI = """[message]
to = me@qq.com
from_name = 服务器日报
subject_prefix = [日报]

[channels.email.directmail]
host = smtpdm.aliyun.com
port = 465
username = notice@example.com
password_env = NOTIFY_DM_PASSWORD

[channels.email.qq]
host = smtp.qq.com
port = 465
username = me@qq.com
password_env = NOTIFY_QQ_AUTHCODE

[limits]
per_minute = 5
per_day = 50
dedup_window_minutes = 720
"""

ENV = {"NOTIFY_DM_PASSWORD": "dm-secret", "NOTIFY_QQ_AUTHCODE": "qq-authcode"}


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.ini"

    def write(self, text, mode=0o600):
        self.path.write_text(text, encoding="utf-8")
        if os.name != "nt":
            os.chmod(self.path, mode)
        return str(self.path)


class TestHappyPath(ConfigTestCase):
    def test_loads_channels_in_declaration_order(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertEqual([c.name for c in cfg.channels], ["directmail", "qq"])

    def test_injects_passwords_from_env(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertEqual(cfg.channels[0].password, "dm-secret")
        self.assertEqual([c.name for c in cfg.ready_channels], ["directmail", "qq"])

    def test_reads_message_section(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertEqual(cfg.recipients, ["me@qq.com"])
        self.assertEqual(cfg.from_name, "服务器日报")
        self.assertEqual(cfg.subject_prefix, "[日报]")

    def test_splits_multiple_recipients(self):
        text = BASE_INI.replace("to = me@qq.com", "to = me@qq.com, backup@163.com")
        cfg = load_config(self.write(text), env=ENV)
        self.assertEqual(cfg.recipients, ["me@qq.com", "backup@163.com"])

    def test_port_465_means_ssl(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertEqual(cfg.channels[0].security, "ssl")

    def test_port_587_means_starttls(self):
        text = BASE_INI.replace(
            "port = 465\nusername = notice@example.com",
            "port = 587\nusername = notice@example.com",
        )
        cfg = load_config(self.write(text), env=ENV)
        self.assertEqual(cfg.channels[0].security, "starttls")

    def test_explicit_security_wins(self):
        text = BASE_INI.replace(
            "port = 465\nusername = notice@example.com",
            "port = 587\nsecurity = ssl\nusername = notice@example.com",
        )
        cfg = load_config(self.write(text), env=ENV)
        self.assertEqual(cfg.channels[0].security, "ssl")

    def test_reads_limits(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertEqual(cfg.limits.per_minute, 5)
        self.assertEqual(cfg.limits.per_day, 50)
        self.assertEqual(cfg.limits.dedup_window_minutes, 720)

    def test_limits_default_when_section_missing(self):
        text = BASE_INI.split("[limits]")[0]
        cfg = load_config(self.write(text), env=ENV)
        self.assertEqual(cfg.limits.per_minute, 5)
        self.assertEqual(cfg.limits.per_day, 50)

    def test_expands_home_in_state_and_log_paths(self):
        cfg = load_config(self.write(BASE_INI), env=ENV)
        self.assertTrue(str(cfg.log_path).startswith(str(Path.home())))
        self.assertTrue(str(cfg.state_path).startswith(str(Path.home())))

    def test_percent_in_subject_prefix_is_not_interpolated(self):
        text = BASE_INI.replace("subject_prefix = [日报]", "subject_prefix = 100% 完成")
        cfg = load_config(self.write(text), env=ENV)
        self.assertEqual(cfg.subject_prefix, "100% 完成")

    def test_password_file_is_used(self):
        secret = Path(self._dir.name) / "qq.txt"
        secret.write_text("from-file\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(secret, 0o600)
        text = BASE_INI.replace(
            "password_env = NOTIFY_QQ_AUTHCODE",
            "password_file = %s" % secret,
        )
        cfg = load_config(self.write(text), env={})
        qq = [c for c in cfg.channels if c.name == "qq"][0]
        self.assertEqual(qq.password, "from-file")


class TestRejections(ConfigTestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(str(Path(self._dir.name) / "nope.ini"), env=ENV)

    def test_bad_recipient(self):
        text = BASE_INI.replace("to = me@qq.com", "to = not-an-email")
        with self.assertRaises(ConfigError):
            load_config(self.write(text), env=ENV)

    def test_empty_recipients(self):
        text = BASE_INI.replace("to = me@qq.com", "to =")
        with self.assertRaises(ConfigError):
            load_config(self.write(text), env=ENV)

    def test_no_channel_sections(self):
        text = BASE_INI.split("[channels.email.directmail]")[0]
        with self.assertRaises(ConfigError):
            load_config(self.write(text), env=ENV)

    def test_bad_port(self):
        text = BASE_INI.replace("port = 465", "port = abc")
        with self.assertRaises(ConfigError):
            load_config(self.write(text), env=ENV)

    def test_missing_host(self):
        text = BASE_INI.replace("host = smtpdm.aliyun.com\n", "")
        with self.assertRaises(ConfigError):
            load_config(self.write(text), env=ENV)

    def test_missing_password_marks_channel_unready(self):
        cfg = load_config(self.write(BASE_INI), env={})
        self.assertEqual(cfg.ready_channels, [])

    @unittest.skipIf(os.name == "nt", "Windows 无 POSIX 权限位")
    def test_wide_permissions_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write(BASE_INI, mode=0o644), env=ENV)
        self.assertIn("600", str(ctx.exception))

    def test_malformed_ini(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[message\nto = me@qq.com\n"), env=ENV)


if __name__ == "__main__":
    unittest.main()
