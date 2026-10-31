"""v0.6.2 自检：心跳存活、会话恢复、原因文案、why 命令。"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import proactive  # noqa: E402
from store import MemoryStore  # noqa: E402

import test_plugin_hooks as hooks  # noqa: E402  复用 FakeEvent / _config / provider_stub


class ReasonTextTest(unittest.TestCase):
    def test_known_reason_is_chinese(self):
        self.assertEqual(proactive.reason_text("too_soon"), "距离上次发言还不够久")
        self.assertEqual(proactive.reason_text("daily_cap"), "今天已经发够了")

    def test_unknown_reason_passes_through(self):
        self.assertEqual(proactive.reason_text("weird_thing"), "weird_thing")
        self.assertEqual(proactive.reason_text(""), "没检查过")

    def test_every_decide_reason_has_text(self):
        """decide() 可能返回的原因都必须有中文说法，否则面板会露英文枚举。"""
        covered = {
            "disabled", "muted", "not_private", "quiet_hours", "outside_schedule",
            "too_soon", "daily_cap", "unanswered_cap", "probability", "forced",
            "send_failed", "no_text", "timeout", "error",
        }
        missing = covered - set(proactive.REASON_TEXT)
        self.assertEqual(missing, set())


class KnownScopesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = MemoryStore(os.path.join(self.tmp, "m.db"))

    def tearDown(self):
        self.s.close()

    def test_empty_db(self):
        self.assertEqual(self.s.known_scopes(), [])

    def test_includes_facts_and_care_only_scopes(self):
        self.s.upsert_fact("a:FriendMessage:1", "用户", "饮食", "吃素", importance=4)
        # 只有主动消息状态的会话（从前发过一句，还没产生记忆）
        self.s.set_care_enabled("a:FriendMessage:2", True)
        scopes = self.s.known_scopes()
        self.assertIn("a:FriendMessage:1", scopes)
        self.assertIn("a:FriendMessage:2", scopes)

    def test_no_duplicates(self):
        self.s.upsert_fact("a:FriendMessage:1", "用户", "饮食", "吃素", importance=4)
        self.s.set_care_enabled("a:FriendMessage:1", True)
        self.assertEqual(len(self.s.known_scopes()), 1)


class CareBlockCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = MemoryStore(os.path.join(self.tmp, "m.db"))

    def tearDown(self):
        self.s.close()

    def test_check_fields_are_reported(self):
        block = dashboard.care_block(
            self.s.conn, "a:FriendMessage:1", 60,
            checked_at=1000.0, check_reason="too_soon", check_text="距离上次发言还不够久",
        )
        self.assertEqual(block["checked_at"], 1000.0)
        self.assertEqual(block["check_reason"], "too_soon")
        self.assertEqual(block["check_text"], "距离上次发言还不够久")

    def test_defaults_when_never_checked(self):
        block = dashboard.care_block(self.s.conn, "a:FriendMessage:1", 60)
        self.assertEqual(block["checked_at"], 0.0)
        self.assertEqual(block["check_reason"], "")


class ScopeRestoreTest(unittest.TestCase):
    """重启后没等用户开口就应该知道有哪些会话，否则主动消息永远不会触发。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_scopes_restored_on_construct(self):
        first = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(self.tmp)
        )
        first.store.upsert_fact("a:FriendMessage:1", "用户", "饮食", "吃素", importance=4)
        asyncio.run(first.terminate())

        again = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(self.tmp)
        )
        try:
            self.assertIn("a:FriendMessage:1", again._scopes)
        finally:
            asyncio.run(again.terminate())


class CareWhyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(self.tmp)
        )

    def tearDown(self):
        asyncio.run(self.plugin.terminate())

    def _run(self, text):
        event = hooks.FakeEvent(text=text)
        async def collect():
            return [r async for r in self.plugin.memory_cmd(event)]
        return "\n".join(asyncio.run(collect()))

    def test_why_explains_current_gate(self):
        out = self._run("/memory care why")
        self.assertIn("结论：", out)
        self.assertIn("空闲", out)
        self.assertIn("上次自动检查", out)

    def test_help_mentions_why(self):
        self.assertIn("care 主动消息 on|off|status|why|now", hooks.main_mod.HELP_TEXT)

    def test_tick_records_reason(self):
        scope = "a:FriendMessage:1"
        self.plugin._scopes = {scope}
        asyncio.run(self.plugin._care_tick())
        seen = self.plugin._care_seen.get(scope)
        self.assertIsNotNone(seen)
        self.assertEqual(seen["reason"], "disabled")  # 测试配置里总开关是关的

    def test_care_block_surfaces_last_reason(self):
        scope = "a:FriendMessage:1"
        self.plugin._care_seen[scope] = {"at": time.time(), "reason": "daily_cap", "sent": False}
        block = self.plugin._care_block(scope)
        self.assertEqual(block["check_reason"], "daily_cap")
        self.assertEqual(block["check_text"], "今天已经发够了")


class JanitorSurviveTest(unittest.IsolatedAsyncioTestCase):
    """一轮出错不能让心跳退出——退出之后没人会重新拉起它。"""

    async def test_loop_keeps_running_after_exception(self):
        tmp = tempfile.mkdtemp()
        plugin = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(tmp)
        )
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            raise RuntimeError("boom")

        plugin._janitor_tick = boom
        real_sleep = asyncio.sleep

        async def fast_sleep(_seconds, *a, **kw):
            # 让循环里的 sleep(20) 立刻返回，但仍交出控制权，否则任务根本排不上
            await real_sleep(0)

        try:
            with mock.patch("asyncio.sleep", new=fast_sleep):
                task = asyncio.create_task(plugin._janitor_loop())
                for _ in range(50):
                    await real_sleep(0.002)
                    if calls["n"] >= 2:
                        break
                self.assertFalse(task.done(), "心跳不应该因为一轮异常就退出")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            await plugin.terminate()
        self.assertGreaterEqual(calls["n"], 2, "异常之后循环应该继续跑下一轮")

    async def test_tick_flush_error_is_contained(self):
        tmp = tempfile.mkdtemp()
        plugin = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(tmp)
        )

        async def boom(scope, reason="manual"):
            raise RuntimeError("flush boom")

        plugin._flush = boom
        plugin.buffers["a:FriendMessage:1"] = __import__("collections").deque(
            [("user", "hi", 0.0)]
        )
        try:
            await plugin._janitor_tick()  # 不抛异常即为通过
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
