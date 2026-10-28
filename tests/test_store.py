import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extractor  # noqa: E402
import persona  # noqa: E402
from store import MemoryStore  # noqa: E402

DEFAULTS = {k: 0.5 for k in persona.TRAIT_KEYS}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "m.db"))
        self.scope = "aiocqhttp:FriendMessage:10001"

    def tearDown(self):
        self.store.close()

    def test_duplicate_upsert_merges(self):
        fid, action = self.store.upsert_fact(self.scope, "用户", "饮食", "不吃香菜")
        self.assertEqual(action, "created")
        fid2, action2 = self.store.upsert_fact(self.scope, "用户", "饮食", "不吃香菜", confidence=0.9)
        self.assertEqual(action2, "updated")
        self.assertEqual(fid, fid2)
        rows = self.store.list_facts(self.scope)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["confidence"], 0.7)

    def test_changed_value_supersedes(self):
        old, _ = self.store.upsert_fact(self.scope, "用户", "作息", "每天两点睡")
        new, action = self.store.upsert_fact(self.scope, "用户", "作息", "改成十一点睡了")
        self.assertEqual(action, "superseded")
        self.assertNotEqual(old, new)
        active = self.store.list_facts(self.scope)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], new)
        history = self.store.history_of(self.scope, "用户", "作息")
        self.assertEqual(len(history), 2)

    def test_scope_isolation(self):
        self.store.upsert_fact(self.scope, "用户", "饮食", "爱吃辣")
        other = "aiocqhttp:GroupMessage:999"
        self.assertEqual(self.store.recall(other, "饮食"), [])
        self.assertEqual(len(self.store.recall(self.scope, "饮食")), 1)

    def test_recall_ranks_relevant_first(self):
        self.store.upsert_fact(self.scope, "用户", "饮食偏好", "不吃香菜", importance=4)
        self.store.upsert_fact(self.scope, "用户", "喜欢的游戏", "只玩单机", importance=2)
        hits = self.store.recall(self.scope, "香菜", k=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].predicate, "饮食偏好")

    def test_recall_budget(self):
        for i in range(20):
            self.store.upsert_fact(self.scope, "用户", f"事实{i}", "x" * 100, importance=5)
        hits = self.store.recall(self.scope, "", k=20, budget_chars=300)
        self.assertLess(len(hits), 20)

    def test_forget(self):
        fid, _ = self.store.upsert_fact(self.scope, "用户", "宠物", "养了一只猫")
        self.assertTrue(self.store.delete_fact(self.scope, fid))
        self.assertEqual(self.store.list_facts(self.scope), [])
        self.store.upsert_fact(self.scope, "用户", "宠物", "养了一只狗")
        self.assertEqual(self.store.forget_matching(self.scope, "狗"), 1)

    def test_persona_roundtrip(self):
        state = self.store.get_persona(self.scope, DEFAULTS)
        self.assertEqual(state["version"], 1)
        traits = persona.apply_deltas(state["traits"], {"intimacy": 0.05}, baseline=DEFAULTS)
        version = self.store.save_persona(self.scope, traits, "开心", "聊得很顺")
        self.assertEqual(version, 2)
        self.assertEqual(len(self.store.persona_timeline(self.scope)), 1)


class PersonaTest(unittest.TestCase):
    def test_step_is_clamped(self):
        traits = persona.apply_deltas(DEFAULTS, {"intimacy": 5.0}, max_step=0.06, decay=0.0)
        self.assertAlmostEqual(traits["intimacy"], 0.56, places=4)

    def test_bounds(self):
        high = persona.apply_deltas({k: 0.99 for k in persona.TRAIT_KEYS}, {}, max_step=0.06, decay=0.0)
        self.assertLessEqual(max(high.values()), 1.0)
        low = persona.apply_deltas({k: 0.01 for k in persona.TRAIT_KEYS}, {}, max_step=0.06, decay=0.0)
        self.assertGreaterEqual(min(low.values()), 0.0)

    def test_decay_pulls_to_baseline(self):
        traits = persona.apply_deltas({k: 0.9 for k in persona.TRAIT_KEYS}, {}, decay=0.1)
        self.assertLess(traits["warmth"], 0.9)

    def test_render_is_short(self):
        text = persona.render(DEFAULTS, "有点困")
        self.assertIn("当前状态", text)
        self.assertLessEqual(len(text), 160)
        self.assertNotIn("0.5", text)


class ExtractorTest(unittest.TestCase):
    def test_parse_fenced(self):
        raw = '```json\n{"facts":[{"object":"不吃香菜"}]}\n```'
        self.assertEqual(extractor.parse_json(raw)["facts"][0]["object"], "不吃香菜")

    def test_parse_with_prose(self):
        raw = '好的，结果如下：{"deltas":{"warmth":0.02},"mood":"暖"} 完毕'
        self.assertEqual(extractor.parse_json(raw)["mood"], "暖")

    def test_parse_garbage(self):
        self.assertEqual(extractor.parse_json("没有 JSON"), {})

    def test_sanitize_filters(self):
        raw = {
            "facts": [
                {"object": "不吃香菜", "kind": "preference", "importance": 9, "confidence": 2},
                {"object": "", "kind": "preference"},
                {"object": "x" * 400, "kind": "preference"},
            ]
        }
        facts = extractor.sanitize_facts(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["importance"], 5)
        self.assertEqual(facts[0]["confidence"], 1.0)

    def test_transcript_truncation(self):
        items = [("user", "a" * 5000), ("assistant", "b")]
        text = extractor.build_transcript(items, max_chars=1200)
        self.assertLessEqual(len(text), 1300)
        self.assertIn("助手", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
