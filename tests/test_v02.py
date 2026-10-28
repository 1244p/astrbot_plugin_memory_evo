"""v0.2 新增能力：置信度门控、分类半衰期、冲突检测、注入多样性。"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store as store_mod  # noqa: E402
from store import MemoryStore  # noqa: E402

import tempfile


class V02Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = MemoryStore(os.path.join(self.tmp, "m.db"))
        self.scope = "aiocqhttp:FriendMessage:1"

    def tearDown(self):
        self.s.close()

    # ---- 1. 置信度门控 ----
    def test_low_confidence_not_injected(self):
        self.s.upsert_fact(self.scope, "用户", "备注", "可能住在南京", confidence=0.2, importance=2)
        self.assertEqual(self.s.recall(self.scope, "南京"), [])

    def test_low_confidence_but_important_still_injected(self):
        self.s.upsert_fact(self.scope, "用户", "过敏", "疑似对花生过敏", confidence=0.2, importance=5)
        hits = self.s.recall(self.scope, "花生")
        self.assertEqual(len(hits), 1)

    # ---- 2. 分类半衰期 ----
    def test_task_expires_faster_than_preference(self):
        now = time.time()
        task_id, _ = self.s.upsert_fact(self.scope, "用户", "待办", "把周报发我", "task", 3, 0.9)
        pref_id, _ = self.s.upsert_fact(self.scope, "用户", "口味", "爱吃辣", "preference", 3, 0.9)
        eight_days_ago = now - 8 * 86400
        for fid in (task_id, pref_id):
            self.s.conn.execute("UPDATE facts SET updated_at=? WHERE id=?", (eight_days_ago, fid))
        self.s.conn.commit()
        hits = {f.id: f.score for f in self.s.recall(self.scope, "", k=5)}
        self.assertGreater(hits[pref_id], hits[task_id])

    # ---- 3. 冲突检测 ----
    def test_conflict_marks_both_suspect_and_hides_from_injection(self):
        self.s.upsert_fact(self.scope, "用户", "饮食", "爱吃香菜", "preference", 3, 0.9)
        new_id, action = self.s.upsert_fact(self.scope, "用户", "忌口", "不吃香菜", "preference", 4, 0.9)
        self.assertEqual(action, "conflict")
        self.assertEqual(self.s.recall(self.scope, "香菜"), [])  # 默认不注入待确认
        hits = self.s.recall(self.scope, "香菜", include_suspect=True)
        self.assertTrue(all(f.status == "suspect" for f in hits))
        self.assertIn(new_id, [f.id for f in hits])

    def test_confirm_resolves_conflict(self):
        old_id, _ = self.s.upsert_fact(self.scope, "用户", "饮食", "爱吃香菜", "preference", 3, 0.9)
        new_id, action = self.s.upsert_fact(self.scope, "用户", "忌口", "不吃香菜", "preference", 4, 0.9)
        self.assertEqual(action, "conflict")
        self.assertTrue(self.s.confirm_fact(self.scope, new_id))
        hits = self.s.recall(self.scope, "香菜")
        self.assertEqual([f.id for f in hits], [new_id])
        row = self.s.conn.execute("SELECT superseded_by FROM facts WHERE id=?", (old_id,)).fetchone()
        self.assertEqual(row["superseded_by"], new_id)

    def test_predicate_aliases_merge(self):
        self.assertEqual(store_mod.canonical_predicate("饮食偏好"), "饮食")
        self.assertEqual(store_mod.canonical_predicate("忌口"), "饮食")
        a, act1 = self.s.upsert_fact(self.scope, "用户", "饮食偏好", "不吃香菜")
        b, act2 = self.s.upsert_fact(self.scope, "用户", "忌口", "不吃香菜")
        self.assertEqual(act1, "created")
        self.assertEqual(act2, "updated")
        self.assertEqual(a, b)

    # ---- 4. 注入多样性 + 截断 ----
    def test_per_predicate_cap(self):
        for i in range(5):
            self.s.upsert_fact(self.scope, "用户", "饮食", f"爱吃第{i}种菜", "preference", 3, 0.9)
        hits = self.s.recall(self.scope, "菜", k=10, max_per_predicate=2)
        self.assertLessEqual(len(hits), 2)

    def test_item_truncation(self):
        self.s.upsert_fact(self.scope, "用户", "自述", "x" * 300, importance=5)
        hits = self.s.recall(self.scope, "", k=1, max_item_chars=80)
        self.assertTrue(hits[0].truncated)
        self.assertLessEqual(len(hits[0].object), 81)


class MigrationTest(unittest.TestCase):
    def test_old_schema_gets_new_columns(self):
        import sqlite3

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, subject TEXT, predicate TEXT,
                object TEXT, kind TEXT, importance INTEGER, confidence REAL, evidence TEXT,
                created_at REAL, updated_at REAL, superseded_by INTEGER,
                last_hit_at REAL DEFAULT 0, hits INTEGER DEFAULT 0);
            """
        )
        conn.execute(
            "INSERT INTO facts(scope,subject,predicate,object,kind,importance,confidence,"
            "evidence,created_at,updated_at) VALUES('s','用户','饮食','不吃香菜','preference',3,0.9,'',1,1)"
        )
        conn.commit()
        conn.close()

        s = MemoryStore(path)  # 应自动补列，不抛错
        hits = s.recall("s", "香菜")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].status, "active")
        s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
