"""SQLite 状态：去重窗口与限流计数。"""
import tempfile
import unittest
from pathlib import Path

from notify.store import Store, local_day_start


class Clock:
    """可手动推进的假时钟。"""

    def __init__(self, start=1_700_000_000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.clock = Clock()
        self.store = Store(Path(self._dir.name) / "state.db", now=self.clock)
        self.addCleanup(self.store.close)


class TestDedup(StoreTestCase):
    def test_first_time_is_not_duplicate(self):
        self.assertFalse(self.store.is_duplicate("daily", 720))

    def test_within_window_is_duplicate(self):
        self.store.mark_sent("daily")
        self.clock.advance(60)
        self.assertTrue(self.store.is_duplicate("daily", 720))

    def test_after_window_is_not_duplicate(self):
        self.store.mark_sent("daily")
        self.clock.advance(721 * 60)
        self.assertFalse(self.store.is_duplicate("daily", 720))

    def test_zero_window_disables_dedup(self):
        self.store.mark_sent("daily")
        self.assertFalse(self.store.is_duplicate("daily", 0))

    def test_different_keys_are_independent(self):
        self.store.mark_sent("daily")
        self.assertFalse(self.store.is_duplicate("weekly", 720))

    def test_mark_sent_twice_updates_timestamp(self):
        self.store.mark_sent("daily")
        first = self.store.last_sent("daily")
        self.clock.advance(120)
        self.store.mark_sent("daily")
        self.assertGreater(self.store.last_sent("daily"), first)

    def test_state_survives_reopen(self):
        self.store.mark_sent("daily")
        path = self.store.path
        self.store.close()
        reopened = Store(path, now=self.clock)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.is_duplicate("daily", 720))


class TestRateLimit(StoreTestCase):
    def test_no_limit_when_empty(self):
        self.assertIsNone(self.store.rate_limited(5, 50))

    def test_per_minute_limit(self):
        for _ in range(5):
            self.store.record_attempt("email:directmail", True)
        self.assertEqual(self.store.rate_limited(5, 50), "per_minute")

    def test_per_minute_window_slides(self):
        for _ in range(5):
            self.store.record_attempt("email:directmail", True)
        self.clock.advance(61)
        self.assertIsNone(self.store.rate_limited(5, 50))

    def test_per_day_limit(self):
        for _ in range(3):
            self.store.record_attempt("email:directmail", True)
        self.assertEqual(self.store.rate_limited(100, 3), "per_day")

    def test_per_day_counts_only_today(self):
        for _ in range(3):
            self.store.record_attempt("email:directmail", True)
        self.clock.value = local_day_start(self.clock.value) + 86400 + 60
        self.assertIsNone(self.store.rate_limited(100, 3))

    def test_failed_attempts_also_count(self):
        for _ in range(3):
            self.store.record_attempt("email:qq", False)
        self.assertEqual(self.store.count_attempts(self.clock() - 60), 3)

    def test_zero_limit_means_unlimited(self):
        for _ in range(10):
            self.store.record_attempt("email:qq", True)
        self.assertIsNone(self.store.rate_limited(0, 0))


class TestPrune(StoreTestCase):
    def test_prune_removes_old_rows(self):
        self.store.mark_sent("old")
        self.store.record_attempt("email:qq", True)
        self.clock.advance(31 * 86400)
        self.store.prune(keep_days=30)
        self.assertIsNone(self.store.last_sent("old"))
        self.assertEqual(self.store.count_attempts(0), 0)

    def test_prune_keeps_recent_rows(self):
        self.store.mark_sent("recent")
        self.clock.advance(86400)
        self.store.prune(keep_days=30)
        self.assertIsNotNone(self.store.last_sent("recent"))


class TestLocalDayStart(unittest.TestCase):
    def test_returns_midnight_of_same_local_day(self):
        import time

        start = local_day_start(1_700_000_000.0)
        self.assertLessEqual(start, 1_700_000_000.0)
        self.assertLess(1_700_000_000.0 - start, 86400)
        self.assertEqual(time.localtime(start).tm_hour, 0)
        self.assertEqual(time.localtime(start).tm_min, 0)


if __name__ == "__main__":
    unittest.main()
