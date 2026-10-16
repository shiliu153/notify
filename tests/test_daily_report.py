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


class TestRequestsMetrics(TempDirTest):
    def sample_rows(self):
        return [
            {"time": "2026-10-16T09:05:00.1+08:00", "account": "a(1)", "model": "m1",
             "status": 200, "ok": True, "outcome": "success", "duration_ms": 100,
             "ttfb_ms": 50, "attempts": 1, "total_tokens": 1000,
             "cache_hit_tokens": 900, "cache_miss_tokens": 100,
             "client_ip": "1.1.1.1", "user_agent": "ua1"},
            {"time": "2026-10-16T09:30:00.2+08:00", "account": "b(2)", "model": "m1",
             "status": 200, "ok": True, "outcome": "success", "duration_ms": 300,
             "ttfb_ms": 70, "attempts": 2, "total_tokens": 2000,
             "cache_hit_tokens": 0, "cache_miss_tokens": 2000,
             "client_ip": "1.1.1.1", "user_agent": "ua2"},
            {"time": "2026-10-16T14:10:00.3+08:00", "account": "a(1)", "model": "m2",
             "status": 502, "ok": False, "outcome": "upstream_error", "duration_ms": 900,
             "ttfb_ms": 800, "attempts": 1, "total_tokens": 0,
             "cache_hit_tokens": 0, "cache_miss_tokens": 0,
             "client_ip": "2.2.2.2", "user_agent": "ua1"},
        ]

    def test_empty_rows_returns_none(self):
        self.assertIsNone(report.requests_metrics([]))

    def test_counts_and_failures(self):
        m = report.requests_metrics(self.sample_rows())
        self.assertEqual(m["count"], 3)
        self.assertEqual(m["failed"], 1)
        self.assertEqual(m["retries"], 1)

    def test_percentiles_and_max(self):
        m = report.requests_metrics(self.sample_rows())
        self.assertEqual(m["p50"], 300)
        self.assertEqual(m["p99"], 900)
        self.assertEqual(m["max"], 900)
        self.assertEqual(m["ttfb_p50"], 70)

    def test_tokens_and_cache(self):
        m = report.requests_metrics(self.sample_rows())
        self.assertEqual(m["tokens_total"], 3000)
        self.assertEqual(m["cache_hit_pct"], round(100.0 * 900 / 3000, 2))

    def test_zero_cache_denominator_is_none(self):
        m = report.requests_metrics([{"duration_ms": 1, "cache_hit_tokens": 0,
                                      "cache_miss_tokens": 0}])
        self.assertIsNone(m["cache_hit_pct"])

    def test_distributions(self):
        m = report.requests_metrics(self.sample_rows())
        self.assertEqual(m["by_account"], {"a": 2, "b": 1})
        self.assertEqual(m["by_model"], {"m1": 2, "m2": 1})
        self.assertEqual(m["by_ip"], {"1.1.1.1": 2, "2.2.2.2": 1})
        self.assertEqual(m["by_ua"], {"ua1": 2, "ua2": 1})

    def test_peak_hour_and_slowest(self):
        m = report.requests_metrics(self.sample_rows())
        self.assertEqual(m["peak_hour"], 9)
        self.assertEqual(m["peak_hour_count"], 2)
        self.assertEqual(m["slowest"]["account"], "a")
        self.assertEqual(m["slowest"]["hhmm"], "14:10")
        self.assertEqual(m["slowest"]["ms"], 900)

    def test_missing_fields_do_not_crash(self):
        m = report.requests_metrics([{}])
        self.assertEqual(m["count"], 1)
        self.assertEqual(m["failed"], 0)
        self.assertIsNone(m["peak_hour"])
        self.assertEqual(m["slowest"]["hhmm"], "—")


class TestSnapshot(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "daily-stats.jsonl"

    def row(self, day, **extra):
        base = {"date": day, "requests": {"count": 1}}
        base.update(extra)
        return base

    def test_write_then_read(self):
        report.save_snapshot(self.row("2026-10-16"), self.path, "2026-10-16")
        self.assertEqual([r["date"] for r in report.load_snapshot(self.path)], ["2026-10-16"])

    def test_same_day_replaces_instead_of_appending(self):
        report.save_snapshot(self.row("2026-10-16", requests={"count": 1}), self.path, "2026-10-16")
        report.save_snapshot(self.row("2026-10-16", requests={"count": 9}), self.path, "2026-10-16")
        rows = report.load_snapshot(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requests"]["count"], 9)

    def test_rows_sorted_by_date(self):
        report.save_snapshot(self.row("2026-10-16"), self.path, "2026-10-16")
        report.save_snapshot(self.row("2026-10-15"), self.path, "2026-10-15")
        self.assertEqual([r["date"] for r in report.load_snapshot(self.path)],
                         ["2026-10-15", "2026-10-16"])

    def test_creates_missing_directory(self):
        nested = self.root / "a" / "b" / "stats.jsonl"
        report.save_snapshot(self.row("2026-10-16"), nested, "2026-10-16")
        self.assertTrue(nested.exists())

    def test_retention_trims_rows_older_than_400_days(self):
        report.save_snapshot(self.row("2025-01-01"), self.path, "2025-01-01")
        report.save_snapshot(self.row("2026-10-16"), self.path, "2026-10-16")
        self.assertEqual([r["date"] for r in report.load_snapshot(self.path)], ["2026-10-16"])

    def test_unwritable_target_warns_without_raising(self):
        blocked = self.root / "a-directory"
        blocked.mkdir()
        report.save_snapshot(self.row("2026-10-16"), blocked, "2026-10-16")

    def test_corrupt_file_is_treated_as_no_history(self):
        self.write("daily-stats.jsonl", "{not json at all\n")
        self.assertEqual(report.load_snapshot(self.path), [])

    def test_missing_file_is_no_history(self):
        self.assertEqual(report.load_snapshot(self.root / "nope.jsonl"), [])

    def test_rows_without_date_are_dropped(self):
        self.write("daily-stats.jsonl", '{"requests": {"count": 1}}\n')
        self.assertEqual(report.load_snapshot(self.path), [])

    def test_find_yesterday(self):
        rows = [self.row("2026-10-14"), self.row("2026-10-15")]
        self.assertEqual(report.find_yesterday(rows, "2026-10-16")["date"], "2026-10-15")

    def test_find_yesterday_missing(self):
        self.assertIsNone(report.find_yesterday([self.row("2026-10-14")], "2026-10-16"))

    def test_credit_base_picks_earliest_inside_window(self):
        rows = [self.row("2026-10-05", credits={"a": {"left": 100}}),
                self.row("2026-10-10", credits={"a": {"left": 90}}),
                self.row("2026-10-15", credits={"a": {"left": 80}})]
        self.assertEqual(report.find_credit_base(rows, "2026-10-16")["date"], "2026-10-10")

    def test_credit_base_skips_rows_without_credits(self):
        rows = [self.row("2026-10-12"), self.row("2026-10-13", credits={"a": {"left": 90}})]
        self.assertEqual(report.find_credit_base(rows, "2026-10-16")["date"], "2026-10-13")

    def test_credit_base_none_when_no_history(self):
        self.assertIsNone(report.find_credit_base([], "2026-10-16"))

    def test_daily_consumption(self):
        base = self.row("2026-10-10", credits={"a": {"left": 100}})
        today = self.row("2026-10-16", credits={"a": {"left": 40}})
        self.assertAlmostEqual(report.daily_consumption(base, today)["a"], 10.0)

    def test_daily_consumption_skips_flat_missing_and_new_accounts(self):
        base = self.row("2026-10-10", credits={"a": {"left": 100}, "b": {"left": 5}})
        today = self.row("2026-10-16", credits={"a": {"left": 100}, "c": {"left": 1}})
        self.assertEqual(report.daily_consumption(base, today), {})

    def test_daily_consumption_without_base(self):
        self.assertEqual(report.daily_consumption(None, self.row("2026-10-16")), {})


class TestBackfill(TempDirTest):
    def setUp(self):
        super().setUp()
        self.path = self.root / "daily-stats.jsonl"
        self.log_dir = self.root / "request-logs"
        self.log_dir.mkdir()

    def log(self, day, count):
        lines = "".join(
            json.dumps({"time": "%sT10:0%d:00+08:00" % (day, index), "duration_ms": 100,
                        "ttfb_ms": 40, "total_tokens": 10, "cache_hit_tokens": 1,
                        "cache_miss_tokens": 0, "ok": True, "status": 200, "attempts": 1}) + "\n"
            for index in range(count))
        self.write("request-logs/requests-%s.jsonl" % day, lines)

    def test_backfills_every_missing_day(self):
        self.log("2026-10-14", 2)
        self.log("2026-10-15", 3)
        added = report.backfill_snapshot(self.path, self.log_dir, "2026-10-16")
        self.assertEqual(added, 2)
        rows = report.load_snapshot(self.path)
        self.assertEqual([r["date"] for r in rows], ["2026-10-14", "2026-10-15"])
        self.assertEqual(rows[1]["requests"]["count"], 3)

    def test_backfilled_rows_are_marked_and_the_rest_is_null(self):
        self.log("2026-10-15", 1)
        report.backfill_snapshot(self.path, self.log_dir, "2026-10-16")
        row = report.load_snapshot(self.path)[0]
        self.assertTrue(row["backfill"])
        self.assertIsNone(row["credits"])
        self.assertIsNone(row["host"])

    def test_existing_days_are_not_overwritten(self):
        report.save_snapshot({"date": "2026-10-15", "requests": {"count": 99}, "backfill": False},
                             self.path, "2026-10-15")
        self.log("2026-10-15", 3)
        self.assertEqual(report.backfill_snapshot(self.path, self.log_dir, "2026-10-16"), 0)
        self.assertEqual(report.load_snapshot(self.path)[0]["requests"]["count"], 99)

    def test_today_is_never_backfilled(self):
        self.log("2026-10-16", 1)
        self.assertEqual(report.backfill_snapshot(self.path, self.log_dir, "2026-10-16"), 0)

    def test_backfilled_rows_do_not_feed_the_credit_baseline(self):
        self.log("2026-10-15", 1)
        report.backfill_snapshot(self.path, self.log_dir, "2026-10-16")
        self.assertIsNone(report.find_credit_base(report.load_snapshot(self.path), "2026-10-16"))

    def test_missing_log_dir_is_a_noop(self):
        self.assertEqual(report.backfill_snapshot(self.path, self.root / "nope", "2026-10-16"), 0)

    def test_is_idempotent(self):
        self.log("2026-10-15", 1)
        report.backfill_snapshot(self.path, self.log_dir, "2026-10-16")
        self.assertEqual(report.backfill_snapshot(self.path, self.log_dir, "2026-10-16"), 0)

    def test_unrelated_file_names_are_ignored(self):
        self.write("request-logs/requests-broken.jsonl", "{}\n")
        self.assertEqual(report.backfill_snapshot(self.path, self.log_dir, "2026-10-16"), 0)


class TestCollectHost(TempDirTest):
    def runner(self):
        return FakeRunner({
            "df -P": (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                         "/dev/vda1 40900608 17301504 21963904 45% /"),
            "free -m": (0, "              total        used        free      shared  buff/cache"
                           "   available\n"
                           "Mem:           1870        1066         300          10"
                           "         503         804"),
            "systemctl --failed": (0, "kdump.service loaded failed failed Kdump"),
        })

    def test_disk_is_parsed(self):
        data = report.collect_host(runner=self.runner())
        self.assertEqual(data["disk_pct"], 45)
        self.assertEqual(data["disk_total"], 40900608 * 1024)
        self.assertEqual(data["disk_used"], 17301504 * 1024)

    def test_memory_is_parsed(self):
        data = report.collect_host(runner=self.runner())
        self.assertEqual(data["mem_total"], 1870)
        self.assertEqual(data["mem_used"], 1066)
        self.assertEqual(data["mem_pct"], 57)

    def test_failed_units_are_listed(self):
        self.assertEqual(report.collect_host(runner=self.runner())["failed_units"],
                         ["kdump.service"])

    def test_runner_failure_leaves_none_not_zero(self):
        data = report.collect_host(runner=FakeRunner({}))
        self.assertIsNone(data["disk_pct"])
        self.assertIsNone(data["mem_pct"])
        self.assertIsNone(data["failed_units"])


class TestCollectServices(TempDirTest):
    def runner(self, pgrep=(0, "123"), systemctl=(0, "active"),
               docker=(0, "running|2026-10-12T06:22:15.123456789Z")):
        return FakeRunner({"pgrep": pgrep, "systemctl": systemctl, "docker": docker})

    def test_all_green(self):
        data = report.collect_services(runner=self.runner(), probe=lambda port: True)
        self.assertEqual(len(data["procs"]), 3)
        self.assertTrue(all(p["ok"] for p in data["procs"]))
        self.assertTrue(data["units"][0]["ok"])
        self.assertTrue(data["container"]["ok"])
        self.assertEqual(data["container"]["started"], "2026-10-12T06:22:15")
        self.assertTrue(all(p["ok"] for p in data["ports"]))

    def test_missing_process(self):
        data = report.collect_services(runner=self.runner(pgrep=(1, "")), probe=lambda port: True)
        self.assertFalse(any(p["ok"] for p in data["procs"]))

    def test_dead_port_is_reported(self):
        data = report.collect_services(runner=self.runner(), probe=lambda port: port != 3306)
        self.assertEqual([p["port"] for p in data["ports"] if not p["ok"]], [3306])

    def test_container_query_failure(self):
        data = report.collect_services(runner=self.runner(docker=(1, "")), probe=lambda port: True)
        self.assertFalse(data["container"]["ok"])
        self.assertIsNone(data["container"]["status"])

    def test_non_active_unit(self):
        data = report.collect_services(runner=self.runner(systemctl=(3, "inactive")),
                                       probe=lambda port: True)
        self.assertFalse(data["units"][0]["ok"])
        self.assertEqual(data["units"][0]["state"], "inactive")


class TestCollectSites(TempDirTest):
    OK = {"http://shiliu153.cn/": (200, ""),
          "https://shiliu153.cn/": (200, ""),
          "https://shiliu153.me/": (200, ""),
          "https://wb.shiliu153.me/healthz": (200, '{"healthy": 3, "total": 3}')}

    def test_all_reachable_and_healthz_note(self):
        rows = report.collect_sites(opener=FakeOpener(dict(self.OK)))
        self.assertTrue(all(r["ok"] for r in rows))
        health = [r for r in rows if "healthz" in r["url"]][0]
        self.assertEqual(health["note"], "健康账号 3/3")

    def test_cert_mismatch_is_a_known_issue(self):
        results = dict(self.OK)
        results["https://shiliu153.cn/"] = urllib.error.URLError(
            ssl.SSLCertVerificationError("no alternative certificate subject name matches"))
        rows = report.collect_sites(opener=FakeOpener(results))
        cn = [r for r in rows if r["label"] == "shiliu153.cn (https)"][0]
        self.assertFalse(cn["ok"])
        self.assertEqual(cn["note"], "证书不匹配")
        self.assertTrue(cn["known"])
        self.assertEqual(cn["known_reason"], ".cn 正在备案中，暂不处理证书")

    def test_unreachable_site_is_not_known(self):
        results = dict(self.OK)
        results["https://shiliu153.me/"] = urllib.error.URLError(OSError("connection refused"))
        rows = report.collect_sites(opener=FakeOpener(results))
        me = [r for r in rows if r["label"] == "shiliu153.me (https)"][0]
        self.assertFalse(me["ok"])
        self.assertFalse(me["known"])
        self.assertEqual(me["note"], "connection refused")

    def test_http_error_code_is_recorded(self):
        results = dict(self.OK)
        results["https://shiliu153.me/"] = urllib.error.HTTPError(
            "https://shiliu153.me/", 502, "Bad Gateway", {}, None)
        rows = report.collect_sites(opener=FakeOpener(results))
        me = [r for r in rows if r["label"] == "shiliu153.me (https)"][0]
        self.assertEqual(me["code"], 502)
        self.assertFalse(me["ok"])
