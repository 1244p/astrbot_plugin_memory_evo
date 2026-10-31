"""事件日志：写入、级别过滤、按天分文件、清理。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plog  # noqa: E402


class LogBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, level="INFO", days=7):
        return plog.EventLog(self.root, level=level, days=days)


class WriteTest(LogBase):
    def test_creates_dir_and_file(self):
        log = self.make()
        rec = log.write("INFO", "plugin.start", "已加载", db="/x.db")
        self.assertIsNotNone(rec)
        self.assertEqual(len(log.files()), 1)
        self.assertEqual(rec["evt"], "plugin.start")
        self.assertEqual(rec["f"]["db"], "/x.db")

    def test_low_level_is_dropped(self):
        log = self.make(level="WARN")
        self.assertIsNone(log.write("INFO", "recall.ok", "命中 3 条"))
        self.assertIsNone(log.write("DEBUG", "llm.ok", ""))
        self.assertIsNotNone(log.write("WARN", "care.skip", "太早"))

    def test_debug_kept_when_configured(self):
        log = self.make(level="DEBUG")
        log.write("DEBUG", "flush.begin", "", turns=6)
        self.assertEqual(len(log.tail(10)), 1)

    def test_missing_fields_are_dropped(self):
        log = self.make()
        rec = log.write("INFO", "x", "", a=1, b=None, c="")
        self.assertEqual(rec["f"], {"a": 1, "c": ""})

    def test_write_failure_does_not_raise(self):
        # 把日志目录指到一个普通文件上，写必然失败，但不能抛
        path = os.path.join(self.tmp.name, "not-a-dir")
        open(path, "w").close()
        log = plog.EventLog(path)
        self.assertIsNone(log.write("INFO", "x", "y"))


class TailTest(LogBase):
    def test_newest_first(self):
        log = self.make()
        for i in range(5):
            log.write("INFO", "tick", f"第{i}条")
        got = [r["msg"] for r in log.tail(3)]
        self.assertEqual(got, ["第4条", "第3条", "第2条"])

    def test_limit_applies(self):
        log = self.make()
        for i in range(10):
            log.write("INFO", "tick", str(i))
        self.assertEqual(len(log.tail(4)), 4)

    def test_level_filter(self):
        log = self.make()
        log.write("DEBUG", "a", "")
        log.write("WARN", "b", "")
        log.write("ERROR", "c", "")
        got = [r["evt"] for r in log.tail(10, level="WARN")]
        self.assertEqual(got, ["c", "b"])

    def test_keyword_filter_checks_fields(self):
        log = self.make()
        log.write("INFO", "care.skip", "太早", scope="qq:FriendMessage:abc")
        log.write("INFO", "care.sent", "发了", scope="qq:FriendMessage:zzz")
        got = log.tail(10, q="abc")
        self.assertEqual([r["evt"] for r in got], ["care.skip"])

    def test_evt_prefix_filter(self):
        log = self.make()
        log.write("INFO", "care.skip", "")
        log.write("INFO", "care.sent", "")
        log.write("INFO", "flush.ok", "")
        self.assertEqual(len(log.tail(10, evt="care.")), 2)

    def test_bad_lines_are_skipped(self):
        log = self.make()
        log.write("INFO", "ok", "")
        with open(log.files()[0], "a", encoding="utf-8") as f:
            f.write("这不是 JSON\n\n")
        self.assertEqual(len(log.tail(10)), 1)

    def test_empty_dir_returns_empty(self):
        self.assertEqual(plog.EventLog(os.path.join(self.tmp.name, "none")).tail(5), [])


class RotateTest(LogBase):
    def test_splits_by_day(self):
        log = self.make()
        now = time.time()
        log.write("INFO", "a", "")
        # 直接构造昨天的记录：写进去后用改名把内容搬到昨天，验证读取跨文件
        today_file = log.files()[0]
        yesterday = time.strftime("%Y%m%d", time.localtime(now - 86400))
        os.replace(today_file, os.path.join(self.root, f"{plog.FILE_PREFIX}{yesterday}{plog.FILE_SUFFIX}"))
        log.write("INFO", "b", "")
        got = [r["evt"] for r in log.tail(10)]
        self.assertEqual(got, ["b", "a"])

    def test_prunes_old_files(self):
        log = self.make(days=1)
        old_day = time.strftime("%Y%m%d", time.localtime(time.time() - 5 * 86400))
        old = os.path.join(self.root, f"{plog.FILE_PREFIX}{old_day}{plog.FILE_SUFFIX}")
        os.makedirs(self.root, exist_ok=True)
        with open(old, "w", encoding="utf-8") as f:
            f.write("")
        log.write("INFO", "a", "")
        self.assertFalse(os.path.exists(old))

    def test_oversize_file_is_rotated(self):
        log = self.make()
        log.max_bytes = 64 * 1024
        for i in range(400):
            log.write("INFO", "big", "x" * 200)
        files = [os.path.basename(p) for p in log.files()]
        self.assertTrue(any(name.endswith(".log.1") for name in files), files)


class StatsClearTest(LogBase):
    def test_stats_counts_levels_and_events(self):
        log = self.make()
        log.write("INFO", "flush.ok", "")
        log.write("INFO", "flush.ok", "")
        log.write("ERROR", "flush.fail", "")
        st = log.stats()
        self.assertEqual(st["counts"]["INFO"], 2)
        self.assertEqual(st["counts"]["ERROR"], 1)
        self.assertEqual(st["events"]["flush.ok"], 2)
        self.assertGreater(st["bytes"], 0)

    def test_clear_removes_files(self):
        log = self.make()
        log.write("INFO", "a", "")
        self.assertEqual(log.clear(), 1)
        self.assertEqual(log.files(), [])
        self.assertEqual(log.tail(5), [])


class TextLineTest(unittest.TestCase):
    def test_line_has_time_level_evt_msg_fields(self):
        rec = {
            "time": "10-31 13:50:20",
            "lvl": "WARN",
            "evt": "flush.timeout",
            "msg": "抽取超时",
            "f": {"scope": "qq:FriendMessage:abc", "sec": 45.0},
        }
        line = plog.text_line(rec)
        self.assertIn("10-31 13:50:20", line)
        self.assertIn("WARN", line)
        self.assertIn("flush.timeout", line)
        self.assertIn("抽取超时", line)
        self.assertIn("sec=45.0", line)

    def test_truncate(self):
        rec = {"time": "t", "lvl": "INFO", "evt": "e", "msg": "x" * 200, "f": {}}
        self.assertLessEqual(len(plog.text_line(rec, 40)), 40)


if __name__ == "__main__":
    unittest.main()
