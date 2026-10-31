"""v0.7.3：抽取预览（干跑）与 /memory extract 命令。"""

import asyncio
import sys
import tempfile
import unittest
from collections import deque

import test_plugin_hooks as hooks

PAYLOAD = (
    '{"facts":[{"subject":"用户","predicate":"任务","object":"bug 已修好","kind":"task",'
    '"importance":4,"confidence":0.9,"evidence":"bug 修好了"}]}'
)


class ExtractPreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), hooks._config(self.tmp)
        )
        self.plugin.context.provider = hooks.FakeProvider(PAYLOAD)

    def tearDown(self):
        asyncio.run(self.plugin.terminate())

    def _run(self, text):
        event = hooks.FakeEvent(text=text)

        async def collect():
            return [r async for r in self.plugin.memory_cmd(event)]

        return "\n".join(asyncio.run(collect()))

    def test_dry_run_reports_but_does_not_write(self):
        self.plugin.buffers["s4"] = deque([("user", "bug 修好了", 0.0)])
        text = asyncio.run(self.plugin._extract_preview("s4", call_model=True))
        self.assertIn("1 条可存", text)
        self.assertIn("没有写库", text)
        self.assertEqual(len(self.plugin.store.list_facts("s4")), 0)
        self.assertEqual(len(self.plugin.buffers["s4"]), 1)  # 缓冲不能被清

    def test_buf_mode_does_not_call_model(self):
        class Boom:
            async def text_chat(self, **kw):
                raise AssertionError("buf 模式不该调模型")

        self.plugin.context.provider = Boom()
        self.plugin.buffers["s5"] = deque([("user", "随便聊聊", 0.0)])
        text = asyncio.run(self.plugin._extract_preview("s5", call_model=False))
        self.assertIn("转写开头", text)
        self.assertIn("随便聊聊", text)

    def test_empty_buffer_explains_itself(self):
        text = asyncio.run(self.plugin._extract_preview("s6", call_model=True))
        self.assertIn("缓冲是空的", text)

    def test_command_extract_go_writes(self):
        umo = hooks.FakeEvent().unified_msg_origin
        self.plugin.buffers[umo] = deque([("user", "bug 修好了", 0.0)])
        out = self._run("/memory extract go")
        self.assertIn("已抽取", out)
        self.assertEqual(len(self.plugin.store.list_facts(umo)), 1)

    def test_command_extract_reports_overwrite(self):
        umo = hooks.FakeEvent().unified_msg_origin
        self.plugin.store.upsert_fact(umo, "用户", "任务", "在修 bug", kind="task")
        self.plugin.buffers[umo] = deque([("user", "bug 修好了", 0.0)])
        out = self._run("/memory extract")
        self.assertIn("覆盖旧值", out)
        self.assertIn("在修 bug", out)


if __name__ == "__main__":
    unittest.main()
