"""v0.3 语义召回：有向量走余弦，没向量走字面，维度/模型不匹配自动回退。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import MemoryStore  # noqa: E402

MODEL = "test:emb-1"


class SemanticRecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = MemoryStore(os.path.join(self.tmp, "m.db"))
        self.scope = "aiocqhttp:FriendMessage:1"

    def tearDown(self):
        self.s.close()

    def _fact(self, predicate, obj, kind="preference", importance=3, conf=0.9):
        fid, _ = self.s.upsert_fact(self.scope, "用户", predicate, obj, kind, importance, conf)
        return fid

    def test_paraphrase_found_by_vector(self):
        a = self._fact("饮食", "不吃香菜")
        b = self._fact("游戏", "只玩单机")
        self.s.save_embedding(self.scope, a, [1.0, 0.0, 0.0], MODEL)
        self.s.save_embedding(self.scope, b, [0.0, 1.0, 0.0], MODEL)

        # 字面完全不重叠，只有向量能救回来
        self.assertEqual(self.s.recall(self.scope, "闻着就反胃"), [])
        hits = self.s.recall(
            self.scope, "闻着就反胃", k=2, query_vec=[0.95, 0.1, 0.0], embedding_model=MODEL
        )
        self.assertTrue(hits)
        self.assertIn("香菜", hits[0].object)

    def test_falls_back_when_no_vector(self):
        self._fact("饮食", "不吃香菜")
        self.assertTrue(self.s.recall(self.scope, "香菜"))
        self.assertFalse(self.s.recall(self.scope, "闻着就反胃"))

    def test_dimension_mismatch_falls_back(self):
        fid = self._fact("饮食", "不吃香菜")
        self.s.save_embedding(self.scope, fid, [1.0, 0.0], MODEL)
        hits = self.s.recall(
            self.scope, "香菜", query_vec=[1.0, 0.0, 0.0], embedding_model=MODEL
        )
        self.assertEqual(len(hits), 1)  # 走字面，仍然命中

    def test_model_mismatch_ignored(self):
        fid = self._fact("饮食", "不吃香菜")
        self.s.save_embedding(self.scope, fid, [1.0, 0.0, 0.0], "old:emb-0")
        hits = self.s.recall(
            self.scope, "闻着就反胃", query_vec=[1.0, 0.0, 0.0], embedding_model=MODEL
        )
        self.assertEqual(hits, [])

    def test_vectors_are_normalized(self):
        fid = self._fact("饮食", "不吃香菜")
        self.s.save_embedding(self.scope, fid, [3.0, 0.0, 0.0], MODEL)
        hits = self.s.recall(
            self.scope, "x", k=1, query_vec=[0.0, 1.0, 0.0], embedding_model=MODEL
        )
        # 余弦为 0，且 query 非空 → 不相关，不注入
        self.assertEqual(hits, [])

    def test_missing_embeddings_and_stats(self):
        a = self._fact("饮食", "不吃香菜")
        self._fact("游戏", "只玩单机")
        self.s.save_embedding(self.scope, a, [1.0, 0.0], MODEL)
        missing = self.s.missing_embeddings(self.scope, MODEL)
        self.assertEqual(len(missing), 1)
        self.assertIn("单机", missing[0][1])
        done, total = self.s.embedding_stats(self.scope, MODEL)
        self.assertEqual((done, total), (1, 2))

    def test_semantic_scores_beat_weak_literal(self):
        a = self._fact("饮食", "不吃香菜", importance=3)
        b = self._fact("作息", "凌晨两点睡", importance=3)
        self.s.save_embedding(self.scope, a, [1.0, 0.0], MODEL)
        self.s.save_embedding(self.scope, b, [0.0, 1.0], MODEL)
        hits = self.s.recall(
            self.scope, "睡", k=2, query_vec=[0.9, 0.2], embedding_model=MODEL
        )
        self.assertEqual(hits[0].predicate, "饮食")


if __name__ == "__main__":
    unittest.main(verbosity=2)
