"""v0.4 主动开口：决策规则、免打扰、静音、强制发送。"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proactive  # noqa: E402
from store import MemoryStore  # noqa: E402

CFG = {
    "enabled": True,
    "min_idle_minutes": 90,
    "daily_max": 3,
    "max_unanswered": 2,
    "quiet_start": "23:30",
    "quiet_end": "08:00",
    "probability": 1.0,
}
NOW = datetime(2026, 10, 29, 15, 0, tzinfo=proactive.local_now().tzinfo)


def state(**kw):
    base = {
        "last_active": NOW.timestamp() - 120 * 60,
        "last_sent": 0.0,
        "sent_date": "",
        "sent_today": 0,
        "unanswered": 0,
        "next_gap_minutes": 0,
        "last_text": "",
        "enabled": 1,
    }
    base.update(kw)
    return base


class DecideTest(unittest.TestCase):
    def test_send_when_idle(self):
        ok, why = proactive.decide(NOW, state(), CFG)
        self.assertTrue(ok, why)

    def test_disabled(self):
        ok, why = proactive.decide(NOW, state(), dict(CFG, enabled=False))
        self.assertFalse(ok)
        self.assertEqual(why, "disabled")

    def test_muted_scope(self):
        ok, why = proactive.decide(NOW, state(enabled=0), CFG)
        self.assertEqual((ok, why), (False, "muted"))

    def test_too_soon(self):
        ok, why = proactive.decide(NOW, state(last_active=NOW.timestamp() - 10 * 60), CFG)
        self.assertEqual((ok, why), (False, "too_soon"))

    def test_next_gap_respected(self):
        ok, why = proactive.decide(
            NOW, state(last_active=NOW.timestamp() - 120 * 60, next_gap_minutes=180), CFG
        )
        self.assertEqual(why, "too_soon")

    def test_daily_cap(self):
        st = state(sent_date=NOW.strftime("%Y-%m-%d"), sent_today=3)
        ok, why = proactive.decide(NOW, st, CFG)
        self.assertEqual((ok, why), (False, "daily_cap"))

    def test_daily_counter_resets_next_day(self):
        st = state(sent_date="2026-10-28", sent_today=3)
        ok, _ = proactive.decide(NOW, st, CFG)
        self.assertTrue(ok)

    def test_unanswered_cap(self):
        ok, why = proactive.decide(NOW, state(unanswered=2), CFG)
        self.assertEqual((ok, why), (False, "unanswered_cap"))

    def test_quiet_hours_late_night(self):
        night = NOW.replace(hour=1, minute=0)
        ok, why = proactive.decide(night, state(last_active=night.timestamp() - 200 * 60), CFG)
        self.assertEqual((ok, why), (False, "quiet_hours"))

    def test_quiet_hours_boundary(self):
        self.assertTrue(proactive.in_quiet_hours(NOW.replace(hour=23, minute=30)))
        self.assertFalse(proactive.in_quiet_hours(NOW.replace(hour=8, minute=0)))
        self.assertFalse(proactive.in_quiet_hours(NOW.replace(hour=15, minute=0)))

    def test_quiet_window_without_wrap(self):
        self.assertTrue(
            proactive.in_quiet_hours(NOW.replace(hour=12, minute=0), "11:00", "13:00")
        )
        self.assertFalse(
            proactive.in_quiet_hours(NOW.replace(hour=14, minute=0), "11:00", "13:00")
        )

    def test_force_bypasses_idle_but_keeps_quiet(self):
        ok, why = proactive.decide(NOW, state(last_active=NOW.timestamp()), CFG, force=True)
        self.assertTrue(ok, why)
        night = NOW.replace(hour=2, minute=0)
        ok2, why2 = proactive.decide(night, state(), CFG, force=True)
        self.assertEqual((ok2, why2), (False, "quiet_hours"))

    def test_probability_zero_blocks(self):
        ok, why = proactive.decide(NOW, state(), dict(CFG, probability=0.0))
        self.assertEqual(why, "probability")


class SanitizeTest(unittest.TestCase):
    def test_skip(self):
        text, gap, why = proactive.sanitize_reply({"send": False, "reason": "刚聊过", "next_gap_minutes": 60})
        self.assertIsNone(text)
        self.assertEqual(gap, 60)
        self.assertEqual(why, "刚聊过")

    def test_send_and_clamp_gap(self):
        text, gap, _ = proactive.sanitize_reply({"send": True, "message": "在忙吗", "next_gap_minutes": 9999})
        self.assertEqual(text, "在忙吗")
        self.assertEqual(gap, 240)

    def test_empty_message_skips(self):
        text, gap, why = proactive.sanitize_reply({"send": True, "message": "   "})
        self.assertIsNone(text)
        self.assertEqual(why, "empty")
        self.assertEqual(gap, 90)

    def test_truncates_long_message(self):
        text, _, _ = proactive.sanitize_reply({"send": True, "message": "啊" * 100}, max_chars=20)
        self.assertLessEqual(len(text), 21)

    def test_render_prompt_contains_context(self):
        prompt = proactive.render_prompt(
            NOW, state(), "亲密度偏高", ["饮食：不吃香菜", "作息：凌晨两点睡"], 40
        )
        self.assertIn("120", prompt)   # 空闲分钟
        self.assertIn("不吃香菜", prompt)
        self.assertIn("亲密度偏高", prompt)
        self.assertNotIn("__MEMORY__", prompt)


class CareStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = MemoryStore(os.path.join(self.tmp, "m.db"))
        self.scope = "aiocqhttp:FriendMessage:1"

    def tearDown(self):
        self.s.close()

    def test_touch_activity_resets_unanswered(self):
        self.s.mark_care_sent(self.scope, time.time(), "2026-10-29", "在吗", 60)
        self.assertEqual(self.s.get_care_state(self.scope)["unanswered"], 1)
        self.s.touch_activity(self.scope)
        self.assertEqual(self.s.get_care_state(self.scope)["unanswered"], 0)

    def test_sent_counters(self):
        now = time.time()
        self.s.mark_care_sent(self.scope, now, "2026-10-29", "早", 60)
        self.s.mark_care_sent(self.scope, now + 1, "2026-10-29", "午", 90)
        st = self.s.get_care_state(self.scope)
        self.assertEqual(st["sent_today"], 2)
        self.assertEqual(st["next_gap_minutes"], 90)
        self.assertEqual(st["last_text"], "午")

    def test_mute_toggle(self):
        self.s.set_care_enabled(self.scope, False)
        self.assertEqual(self.s.get_care_state(self.scope)["enabled"], 0)
        self.s.set_care_enabled(self.scope, True)
        self.assertEqual(self.s.get_care_state(self.scope)["enabled"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
