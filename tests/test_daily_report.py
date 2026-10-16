"""daily-report.py 的单元测试。

脚本文件名带连字符、不是合法模块名，因此用 importlib 从路径加载。
全部用注入的假数据，不碰真机器，可在 Windows 上跑。
"""
import importlib.util
import io
import json
import ssl
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "daily-report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location("daily_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["daily_report"] = module
    spec.loader.exec_module(module)
    return module


report = load_report_module()


class TempDirTest(unittest.TestCase):
    """带临时目录的测试基类。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class FakeRunner:
    """假的 shell 执行器：按"命令拼接后包含某子串"返回预置输出。

    按传入 dict 的插入顺序匹配，因此更具体的 key 要放在前面。
    未命中任何 key → (-1, '')。
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, cmd, timeout=15):
        self.calls.append(list(cmd))
        joined = " ".join(str(part) for part in cmd)
        for needle, value in self.responses.items():
            if needle in joined:
                return value
        return -1, ""


class FakeResponse:
    """urllib.request.urlopen 的假返回值。"""

    def __init__(self, code, body):
        self.status = code
        self._body = body.encode("utf-8")

    def read(self, size=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """按 URL 返回预置结果；值是 Exception 时抛出它。"""

    def __init__(self, results):
        self.results = results

    def __call__(self, request, timeout=None):
        result = self.results[request.full_url]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(*result)


def green_metrics():
    """一份"全部正常"的采集结果，供判定层与端到端测试复用。"""
    return {
        "host": {"disk_pct": 45, "disk_used": 17301504, "disk_total": 40900608,
                 "mem_pct": 57, "mem_used": 1066, "mem_total": 1870,
                 "load1": 0.13, "load5": 0.17, "load15": 0.11,
                 "uptime_s": 2437500, "failed_units": []},
        "services": {"procs": [{"label": "nginx", "count": 4, "ok": True}],
                     "units": [{"label": "docker", "state": "active", "ok": True}],
                     "container": {"name": "workbuddy2api", "status": "running",
                                   "started": "2026-10-12T06:22:15", "ok": True},
                     "ports": [{"port": 80, "label": "nginx http", "ok": True}]},
        "sites": [{"label": "shiliu153.me (https)", "url": "https://shiliu153.me/", "ok": True,
                   "code": 200, "elapsed": 0.07, "note": "",
                   "known": False, "known_reason": None}],
        "backup": {"latest": "2026-10-16T03:30:01", "age_hours": 19.3, "ok": True, "size": 44936},
        "certs": {"shiliu153.me": 78, "wb.shiliu153.me": 86},
        "requests": {"count": 285, "failed": 0, "p50": 6933, "p90": 14660, "p99": 59091,
                     "max": 94396, "ttfb_p50": 3120, "ttfb_p90": 4166, "retries": 1,
                     "tokens_total": 70960000, "cache_hit_pct": 99.64,
                     "by_account": {"shiliu12318": 252},
                     "by_model": {"global:deepseek-v4.1-flash": 285},
                     "by_ip": {"113.204.50.126": 285},
                     "by_ua": {"deepseek-harness/0.2.0-rc.2": 285},
                     "peak_hour": 14, "peak_hour_count": 62,
                     "slowest": {"account": "shiliu12318", "hhmm": "16:14", "ms": 94396}},
        "credits": {"shiliu12318": {"left": 409, "total": 410}},
        "journal": {"total": 84, "by_unit": {"sshd": 82, "systemd-coredump": 2}, "sshd_scan": 82},
        "ssh": {"failed": 2, "accepted": 74},
        "nginx_errors": {"today_lines": 1, "files": 20},
    }


class TestPercentile(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(report.percentile([], 0.5))

    def test_single_value(self):
        self.assertEqual(report.percentile([7], 0.5), 7)

    def test_nearest_rank(self):
        self.assertEqual(report.percentile(list(range(1, 101)), 0.5), 51)
        self.assertEqual(report.percentile(list(range(1, 101)), 0.9), 91)
        self.assertEqual(report.percentile(list(range(1, 101)), 0.99), 100)


class TestDelta(unittest.TestCase):
    def test_growth(self):
        self.assertEqual(report.delta(300, 100), "+200%")

    def test_decline(self):
        self.assertEqual(report.delta(150, 200), "-25%")

    def test_real_pair_from_the_spec(self):
        self.assertEqual(report.delta(285, 120), "+138%")

    def test_missing_sides(self):
        self.assertEqual(report.delta(None, 5), "—")
        self.assertEqual(report.delta(5, None), "—")

    def test_zero_base(self):
        self.assertEqual(report.delta(5, 0), "—")


class TestReadJsonl(TempDirTest):
    def test_missing_file(self):
        self.assertEqual(report.read_jsonl(self.root / "nope.jsonl"), ([], 0))

    def test_blank_lines_skipped(self):
        path = self.write("a.jsonl", '{"a": 1}\n\n   \n{"a": 2}\n')
        rows, bad = report.read_jsonl(path)
        self.assertEqual([r["a"] for r in rows], [1, 2])
        self.assertEqual(bad, 0)

    def test_bad_lines_counted(self):
        path = self.write("b.jsonl", '{"a": 1}\n{"a": 2\n{"a": 3}\n')
        rows, bad = report.read_jsonl(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(bad, 1)

    def test_all_bad_lines(self):
        path = self.write("c.jsonl", "garbage\nmore garbage\n")
        rows, bad = report.read_jsonl(path)
        self.assertEqual(rows, [])
        self.assertEqual(bad, 2)


class TestMonthDayPrefix(unittest.TestCase):
    def test_space_padded_single_digit(self):
        self.assertEqual(report.month_day_prefix(datetime(2026, 10, 6)), "Oct  6 ")

    def test_two_digit_day_keeps_trailing_space(self):
        self.assertEqual(report.month_day_prefix(datetime(2026, 10, 16)), "Oct 16 ")

    def test_does_not_match_a_different_day(self):
        line = "Oct 16 22:50:01 host sshd[1]: Accepted publickey"
        self.assertFalse(line.startswith(report.month_day_prefix(datetime(2026, 10, 1))))


class TestKnownIssues(unittest.TestCase):
    def test_match(self):
        self.assertTrue(report.is_known_issue("shiliu153.cn (https)", "证书不匹配"))

    def test_reason_returned(self):
        self.assertEqual(report.known_reason("shiliu153.cn (https)", "证书不匹配"),
                         ".cn 正在备案中，暂不处理证书")

    def test_other_label_does_not_match(self):
        self.assertFalse(report.is_known_issue("shiliu153.me (https)", "证书不匹配"))

    def test_other_note_does_not_match(self):
        self.assertFalse(report.is_known_issue("shiliu153.cn (https)", "不可达"))

    def test_no_reason_when_not_matching(self):
        self.assertIsNone(report.known_reason("shiliu153.me (https)", "证书不匹配"))
