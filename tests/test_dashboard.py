"""v0.5.0 Dashboard 后端：只读统计、筛选分页、批量删除/确认、导出。"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import persona  # noqa: E402
from store import MemoryStore  # noqa: E402

DEFAULTS = {k: 0.5 for k in persona.TRAIT_KEYS}
A = "aiocqhttp:FriendMessage:10001"
B = "aiocqhttp:FriendMessage:10002"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "m.db"))
        self.conn = self.store.conn
        self.store.upsert_fact(A, "用户", "饮食", "不吃香菜", kind="preference", importance=4)
        self.store.upsert_fact(A, "用户", "作息", "两点睡", kind="profile", importance=3)
        self.store.upsert_fact(A, "用户", "宠物", "养了只橘猫", kind="fact", importance=2)
        self.store.upsert_fact(B, "用户", "饮食", "爱吃辣", kind="preference", importance=3)

    def tearDown(self):
        self.store.close()

    # ---- 基础 ----

    def test_scope_label_private(self):
        label = dashboard.scope_label(A)
        self.assertIn("私聊", label)
        self.assertIn("10001", label)

    def test_scope_label_fallback(self):
        self.assertEqual(dashboard.scope_label("weird:thing"), "weird:thing")
        self.assertEqual(dashboard.scope_label(""), "(未知会话)")

    def test_list_scopes_sorted_by_total(self):
        rows = dashboard.list_scopes(self.conn)
        self.assertEqual(len(rows), 2)
        totals = {r["scope"]: r["total"] for r in rows}
        self.assertEqual(totals[A], 3)
        self.assertEqual(totals[B], 1)
        self.assertIn("私聊", {r["scope"]: r["label"] for r in rows}[A])
        stamps = [r["last_update"] for r in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    # ---- 概览 ----

    def test_overview_counts(self):
        data = dashboard.overview(self.conn, A)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_kind"]["preference"], 1)
        self.assertEqual(data["by_status"]["active"], 3)
        self.assertEqual(data["superseded"], 0)
        self.assertEqual(data["never_hit"], 3)
        self.assertEqual(data["no_embedding"], 3)
        self.assertEqual(len(data["slots"]), 3)
        self.assertTrue(0 < data["avg_confidence"] <= 1)

    def test_overview_isolated_by_scope(self):
        data = dashboard.overview(self.conn, B)
        self.assertEqual(data["total"], 1)

    def test_overview_empty_scope(self):
        data = dashboard.overview(self.conn, "aiocqhttp:FriendMessage:9")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["avg_confidence"], 0.0)

    # ---- 列表 ----

    def test_list_facts_all(self):
        data = dashboard.list_facts(self.conn, A)
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["items"]), 3)

    def test_list_facts_search(self):
        data = dashboard.list_facts(self.conn, A, q="香菜")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["object"], "不吃香菜")

    def test_list_facts_kind_filter(self):
        data = dashboard.list_facts(self.conn, A, kind="profile")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["kind"], "profile")

    def test_list_facts_paging(self):
        first = dashboard.list_facts(self.conn, A, limit=2, offset=0)
        second = dashboard.list_facts(self.conn, A, limit=2, offset=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        ids = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
        self.assertEqual(len(ids), 3)

    def test_list_facts_bad_kind_ignored(self):
        data = dashboard.list_facts(self.conn, A, kind="nonsense")
        self.assertEqual(data["total"], 3)

    def test_fact_to_dict_hides_blob(self):
        row = self.conn.execute("SELECT * FROM facts WHERE scope = ? LIMIT 1", (A,)).fetchone()
        item = dashboard.fact_to_dict(row)
        self.assertNotIn("embedding", item)
        self.assertIn("has_embedding", item)
        self.assertFalse(item["has_embedding"])

    # ---- 写操作 ----

    def test_delete_many_scoped(self):
        ids = [i["id"] for i in dashboard.list_facts(self.conn, A)["items"]]
        deleted = dashboard.delete_many(self.conn, A, ids[:2])
        self.assertEqual(deleted, 2)
        self.assertEqual(dashboard.overview(self.conn, A)["total"], 1)
        self.assertEqual(dashboard.overview(self.conn, B)["total"], 1)

    def test_delete_many_ignores_other_scope_ids(self):
        b_ids = [i["id"] for i in dashboard.list_facts(self.conn, B)["items"]]
        deleted = dashboard.delete_many(self.conn, A, b_ids)
        self.assertEqual(deleted, 0)
        self.assertEqual(dashboard.overview(self.conn, B)["total"], 1)

    def test_delete_many_empty(self):
        self.assertEqual(dashboard.delete_many(self.conn, A, []), 0)

    def test_confirm_many_resolves_suspect(self):
        old, _ = self.store.upsert_fact(A, "用户", "饮食", "爱吃香菜", kind="preference")
        self.store.upsert_fact(A, "用户", "饮食", "不吃香菜", kind="preference")
        suspects = dashboard.list_facts(self.conn, A, status="suspect")["items"]
        self.assertTrue(suspects)
        changed = dashboard.confirm_many(self.conn, A, [r["id"] for r in suspects])
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(dashboard.overview(self.conn, A)["by_status"]["suspect"], 0)

    def test_confirm_many_ignores_active(self):
        active = dashboard.list_facts(self.conn, A, status="active")["items"]
        self.assertEqual(dashboard.confirm_many(self.conn, A, [active[0]["id"]]), 0)

    # ---- 人格 / 主动消息 / 导出 ----

    def test_persona_block_defaults(self):
        data = dashboard.persona_block(self.conn, A, DEFAULTS)
        self.assertEqual(data["version"], 0)
        self.assertEqual(data["traits"], DEFAULTS)
        self.assertEqual(data["log"], [])

    def test_persona_block_after_save(self):
        traits = dict(DEFAULTS, intimacy=0.62)
        self.store.save_persona(A, traits, "warm", reason="聊得开心")
        data = dashboard.persona_block(self.conn, A, DEFAULTS)
        self.assertEqual(data["version"], 1)
        self.assertAlmostEqual(data["traits"]["intimacy"], 0.62, places=3)
        self.assertEqual(data["log"][0]["reason"], "聊得开心")

    def test_care_block_default_when_no_row(self):
        data = dashboard.care_block(self.conn, A)
        self.assertEqual(data["enabled"], 1)
        self.assertEqual(data["sent_today"], 0)

    def test_care_block_after_send(self):
        self.store.mark_care_sent(A, 1_700_000_000.0, "2023-11-14", "今天降温了，记得加衣服", 45)
        data = dashboard.care_block(self.conn, A)
        self.assertEqual(data["sent_today"], 1)
        self.assertEqual(data["last_text"], "今天降温了，记得加衣服")
        self.assertEqual(data["next_gap_minutes"], 45)

    def test_care_next_at_never_sent(self):
        data = dashboard.care_block(self.conn, A, min_idle_minutes=90)
        self.assertEqual(data["next_at"], 0)
        self.assertEqual(data["next_reason"], "never_sent")
        self.assertTrue(data["ready"])

    def test_care_next_at_waiting_for_gap(self):
        now = time.time()
        self.store.mark_care_sent(A, now, "2026-10-29", "在忙吗", 45)
        data = dashboard.care_block(self.conn, A, min_idle_minutes=90)
        self.assertFalse(data["ready"])
        self.assertEqual(data["next_reason"], "gap")
        self.assertAlmostEqual(data["next_at"], now + 45 * 60, delta=2)
        self.assertGreater(data["next_at"], now)

    def test_care_next_at_waiting_for_idle(self):
        self.store.touch_activity(A)
        data = dashboard.care_block(self.conn, A, min_idle_minutes=60)
        self.assertFalse(data["ready"])
        self.assertEqual(data["next_reason"], "idle")
        self.assertGreater(data["next_at"], time.time())

    def test_care_ready_when_idle_long_enough(self):
        self.store.touch_activity(A)
        self.conn.execute(
            "UPDATE proactive SET last_active = ? WHERE scope = ?",
            (time.time() - 3 * 3600, A),
        )
        self.conn.commit()
        data = dashboard.care_block(self.conn, A, min_idle_minutes=60)
        self.assertTrue(data["ready"])

    def test_care_next_at_takes_later_gate(self):
        now = time.time()
        self.store.touch_activity(A)
        self.store.mark_care_sent(A, now, "2026-10-29", "在忙吗", 240)
        data = dashboard.care_block(self.conn, A, min_idle_minutes=5)
        # 间隔 240 分钟比空闲 5 分钟更晚，取更晚的那个
        self.assertEqual(data["next_reason"], "gap")
        self.assertAlmostEqual(data["next_at"], now + 240 * 60, delta=2)

    def test_export_payload_json_safe(self):
        self.store.save_persona(A, dict(DEFAULTS), "calm", reason="基线")
        payload = dashboard.export_payload(self.conn, A)
        self.assertEqual(payload["plugin"], "astrbot_plugin_memory_evo")
        self.assertEqual(payload["scope"], A)
        self.assertEqual(len(payload["facts"]), 3)
        self.assertNotIn("has_embedding", payload["facts"][0])
        self.assertIn("traits", payload["persona"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("不吃香菜", text)


if __name__ == "__main__":
    unittest.main()
