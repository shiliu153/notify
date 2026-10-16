"""JSONL 日志的追加与读取。"""
import json
import tempfile
import unittest
from pathlib import Path

from notify.log import format_ts, log_event, tail


class TestLogEvent(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "nested" / "notify.jsonl"

    def test_creates_parent_directories(self):
        log_event(self.path, "delivery", channel="email:qq", ok=True)
        self.assertTrue(self.path.exists())

    def test_writes_one_json_object_per_line(self):
        log_event(self.path, "delivery", channel="email:qq", ok=True)
        log_event(self.path, "delivery", channel="email:directmail", ok=False, error="535")
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "delivery")
        self.assertEqual(first["channel"], "email:qq")
        self.assertTrue(first["ok"])
        self.assertIn("ts", first)

    def test_chinese_is_not_escaped(self):
        log_event(self.path, "delivery", title="服务器日报")
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("服务器日报", raw)

    def test_returns_the_record(self):
        record = log_event(self.path, "delivery", channel="email:qq")
        self.assertEqual(record["event"], "delivery")
        self.assertEqual(record["channel"], "email:qq")

    def test_appends_to_existing_file(self):
        log_event(self.path, "first")
        log_event(self.path, "second")
        self.assertEqual([r["event"] for r in tail(self.path)], ["first", "second"])


class TestTail(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "notify.jsonl"

    def test_missing_file_returns_empty(self):
        self.assertEqual(tail(self.path), [])

    def test_returns_last_n(self):
        for i in range(5):
            log_event(self.path, "delivery", index=i)
        records = tail(self.path, n=2)
        self.assertEqual([r["index"] for r in records], [3, 4])

    def test_unparsable_line_is_tolerated(self):
        log_event(self.path, "delivery")
        with open(str(self.path), "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        records = tail(self.path)
        self.assertEqual(records[-1]["event"], "unparsable")

    def test_blank_lines_are_skipped(self):
        log_event(self.path, "delivery")
        with open(str(self.path), "a", encoding="utf-8") as fh:
            fh.write("\n\n")
        self.assertEqual(len(tail(self.path)), 1)


class TestFormatTs(unittest.TestCase):
    def test_shape(self):
        value = format_ts(1_700_000_000.0)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$")

    def test_defaults_to_now(self):
        self.assertRegex(format_ts(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$")


if __name__ == "__main__":
    unittest.main()
