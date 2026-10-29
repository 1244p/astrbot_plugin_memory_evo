"""钩子级冒烟测试：用最小 stub 替代 astrbot 依赖，验证注入与写回路。

运行：cd plugins_dev && python -m unittest discover -s astrbot_plugin_memory_evo/tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
sys.path.insert(0, PLUGIN_DIR)


def _install_stubs(tmpdir: str) -> None:
    """构造 astrbot.* 的最小可用替身（仅本测试用）。"""

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    astrbot = module("astrbot")
    astrbot.logger = __import__("logging").getLogger("test")

    api = module("astrbot.api")
    api.logger = astrbot.logger
    astrbot.api = api

    class _Filter:
        def on_llm_request(self):
            return lambda fn: fn

        def on_llm_response(self):
            return lambda fn: fn

        def llm_tool(self, *a, **kw):
            return lambda fn: fn

        def command(self, *a, **kw):
            return lambda fn: fn

    event_mod = module("astrbot.api.event")
    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = object

    provider_mod = module("astrbot.api.provider")

    @dataclass
    class ProviderRequest:
        prompt: str | None = None
        system_prompt: str = ""
        contexts: list = field(default_factory=list)
        extra_user_content_parts: list = field(default_factory=list)

    @dataclass
    class LLMResponse:
        completion_text: str = ""

    provider_mod.ProviderRequest = ProviderRequest
    provider_mod.LLMResponse = LLMResponse

    star_mod = module("astrbot.api.star")

    class Context:
        def __init__(self):
            self.provider = None
            self.embedding_providers = []

        def get_provider_by_id(self, _id):
            return None

        def get_all_embedding_providers(self):
            return list(self.embedding_providers)

        def get_all_providers(self):
            return [self.provider] if self.provider is not None else []

        async def get_using_provider_async(self, _umo=None):
            return self.provider

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = lambda *a, **kw: (lambda cls: cls)

    core = module("astrbot.core")
    agent = module("astrbot.core.agent")
    message_mod = module("astrbot.core.agent.message")

    @dataclass
    class TextPart:
        text: str
        type: str = "text"

    message_mod.TextPart = TextPart

    utils = module("astrbot.core.utils")
    path_mod = module("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: tmpdir

    for mod in (astrbot, api, core, agent, utils):
        mod.__path__ = []  # 标记为包


_install_stubs(tempfile.mkdtemp())
import importlib  # noqa: E402

main_mod = importlib.import_module("astrbot_plugin_memory_evo.main")
provider_stub = sys.modules["astrbot.api.provider"]


class FakeProvider:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.calls += 1
        return provider_stub.LLMResponse(completion_text=self.payload)


class FakeEmbedder:
    """最小 embedding provider 替身。"""

    def __init__(self, vec=None):
        self.provider_config = {"id": "emb"}
        self.vec = vec or [1.0, 0.0]
        self.calls = 0

    def get_model(self):
        return "fake-1"

    async def get_embedding(self, text):
        self.calls += 1
        return list(self.vec)

    async def get_embeddings(self, texts):
        self.calls += len(texts)
        return [list(self.vec) for _ in texts]


class FakeEvent:
    def __init__(self, umo="aiocqhttp:FriendMessage:1", text="我最近只吃素，别给我推荐肉"):
        self.unified_msg_origin = umo
        self.message_str = text

    def plain_result(self, text):
        return text


def _config(tmpdir: str) -> dict:
    return {
        "db_path": os.path.join(tmpdir, "hook.db"),
        "top_k": 5,
        "max_inject_chars": 600,
        "persona_inject": True,
        "persona_evolve": True,
        "flush_turns": 2,
    }


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plugin = main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), _config(self.tmp)
        )

    def tearDown(self):
        asyncio.run(self.plugin.terminate())

    def test_inject_persona_block(self):
        req = provider_stub.ProviderRequest()
        asyncio.run(self.plugin.inject_memory(FakeEvent(), req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        text = req.extra_user_content_parts[0].text
        self.assertIn("<self_state>", text)
        self.assertIn("当前状态", text)

    def test_memory_injected_after_write(self):
        self.plugin.store.upsert_fact(
            "aiocqhttp:FriendMessage:1", "用户", "饮食禁忌", "只吃素", importance=5
        )
        req = provider_stub.ProviderRequest()
        asyncio.run(self.plugin.inject_memory(FakeEvent(), req))
        text = req.extra_user_content_parts[0].text
        self.assertIn("<memory>", text)
        self.assertIn("只吃素", text)

    def test_flush_writes_facts_and_persona(self):
        payload = (
            '{"facts":[{"subject":"用户","predicate":"饮食禁忌","object":"只吃素","kind":"preference",'
            '"importance":4,"confidence":0.9,"evidence":"我最近只吃素"}]}'
        )
        self.plugin.context.provider = FakeProvider(payload)
        from collections import deque

        self.plugin.buffers["s1"] = deque([("user", "我最近只吃素，别给我推荐肉", 0.0)])
        asyncio.run(self.plugin._flush("s1"))
        rows = self.plugin.store.list_facts("s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["object"], "只吃素")
        self.assertGreaterEqual(self.plugin.store.get_persona("s1", self.plugin.baseline)["version"], 1)

    def test_search_tool_returns_scoped_facts(self):
        self.plugin.store.upsert_fact("aiocqhttp:FriendMessage:1", "用户", "宠物", "养了两只猫")
        out = asyncio.run(self.plugin.tool_search(FakeEvent(text="猫"), "宠物", 5))
        self.assertIn("养了两只猫", out)

    def test_semantic_injection_finds_paraphrase(self):
        emb = FakeEmbedder()
        self.plugin.context.embedding_providers = [emb]
        self.plugin._emb_resolved = False
        self.plugin._embedder()
        fid, _ = self.plugin.store.upsert_fact(
            "aiocqhttp:FriendMessage:1", "用户", "饮食", "不吃香菜"
        )
        self.plugin.store.save_embedding("aiocqhttp:FriendMessage:1", fid, [1.0, 0.0], self.plugin._emb_model)

        req = provider_stub.ProviderRequest()
        asyncio.run(self.plugin.inject_memory(FakeEvent(text="闻着就反胃"), req))
        text = req.extra_user_content_parts[0].text
        self.assertIn("<memory>", text)
        self.assertIn("香菜", text)
        self.assertGreater(emb.calls, 0)

    def test_missing_extract_provider_warns_once_and_falls_back(self):
        self.plugin.cfg["extract_provider_id"] = "siliconflow"  # 不存在的旧 ID
        self.plugin.context.provider = FakeProvider("{}")
        first = asyncio.run(self.plugin._provider("s"))
        second = asyncio.run(self.plugin._provider("s"))
        self.assertIs(first, self.plugin.context.provider)  # 回退到当前会话模型
        self.assertIs(second, self.plugin.context.provider)
        self.assertIn("extract:siliconflow", self.plugin._warned)  # 有提醒
        self.assertEqual(len([k for k in self.plugin._warned if k.startswith("extract:")]), 1)

    def test_missing_embedding_provider_falls_back_to_first_available(self):
        emb = FakeEmbedder()
        self.plugin.context.embedding_providers = [emb]
        self.plugin.cfg["embedding_provider_id"] = "nope"
        self.plugin._emb_resolved = False
        self.assertIs(self.plugin._embedder(), emb)
        self.assertIn("embedding:nope", self.plugin._warned)

    def test_write_then_forget(self):
        asyncio.run(self.plugin.tool_write(FakeEvent(), "用户对花生过敏", "profile", 5))
        rows = self.plugin.store.list_facts("aiocqhttp:FriendMessage:1")
        self.assertEqual(len(rows), 1)
        fact_id = rows[0]["id"]
        asyncio.run(self.plugin.tool_forget(FakeEvent(), fact_id))
        self.assertEqual(self.plugin.store.list_facts("aiocqhttp:FriendMessage:1"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
