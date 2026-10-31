"""v0.7.4：面板删除/确认接口端到端（含 scope 校验）。

同时守着一条踩过的坑：插件视图 iframe 没有 allow-modals，前端不能用 confirm()，
否则点删除完全没反应——后端没问题，是浏览器把弹窗吞了。
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

import test_plugin_hooks as hooks


class FakeRequest:
    def __init__(self):
        self.method = "POST"
        self.payload = {}

    async def json(self, default=None):
        return dict(self.payload) if self.payload else dict(default or {})


class WebStub:
    def __init__(self):
        self.request = FakeRequest()

    def json_response(self, data, status_code=200):
        return {"data": data, "status_code": status_code}

    def error_response(self, message, status_code=400):
        return {"error": message, "status_code": status_code}


class DeleteApiTest(unittest.TestCase):
    def setUp(self):
        self.web = WebStub()
        sys.modules["astrbot.api.web"] = self.web
        cfg = {"db_path": os.path.join(tempfile.mkdtemp(), "del.db"), "top_k": 5}
        self.plugin = hooks.main_mod.MemoryEvoPlugin(
            sys.modules["astrbot.api.star"].Context(), cfg
        )
        self.scope = "deepsleep:FriendMessage:abc"
        self.a = self.plugin.store.upsert_fact(self.scope, "用户", "饮食", "吃香菜")[0]
        self.b = self.plugin.store.upsert_fact(self.scope, "用户", "任务", "在修 bug")[0]

    def tearDown(self):
        asyncio.run(self.plugin.terminate())

    def test_delete_removes_only_given_ids(self):
        self.web.request.payload = {"scope": self.scope, "ids": [self.a]}
        out = asyncio.run(self.plugin.api_delete_facts())
        self.assertEqual(out["data"]["deleted"], 1)
        left = [r["id"] for r in self.plugin.store.list_facts(self.scope)]
        self.assertEqual(left, [self.b])

    def test_delete_with_wrong_scope_deletes_nothing(self):
        self.web.request.payload = {"scope": "other:FriendMessage:x", "ids": [self.a]}
        out = asyncio.run(self.plugin.api_delete_facts())
        self.assertEqual(out["data"]["deleted"], 0)
        self.assertEqual(len(self.plugin.store.list_facts(self.scope)), 2)

    def test_delete_requires_scope_and_list(self):
        self.web.request.payload = {"ids": [self.a]}
        out = asyncio.run(self.plugin.api_delete_facts())
        self.assertEqual(out["status_code"], 400)

    def test_confirm_only_touches_suspect(self):
        # 极性明确相反（爱吃 / 不吃）才会进 conflict；「吃香菜」是中性极性，只会覆盖旧值
        self.plugin.store.upsert_fact(self.scope, "用户", "饮食", "爱吃香菜")
        self.plugin.store.upsert_fact(self.scope, "用户", "饮食", "不吃香菜")
        suspects = [
            int(r["id"])
            for r in self.plugin.store.conn.execute(
                "SELECT id FROM facts WHERE scope=? AND status='suspect'", (self.scope,)
            )
        ]
        self.assertTrue(suspects)
        self.web.request.payload = {"scope": self.scope, "ids": suspects + [self.b]}
        out = asyncio.run(self.plugin.api_confirm_facts())
        self.assertEqual(out["data"]["confirmed"], len(suspects))

    def test_app_js_has_no_modal_dialogs(self):
        path = os.path.join(hooks.PLUGIN_DIR, "views", "memory", "app.js")
        body = open(path, encoding="utf-8").read()
        code = "\n".join(
            line
            for line in body.splitlines()
            if not line.lstrip().startswith(("//", "*", "/*"))
        )
        self.assertNotIn("confirm(`", code)
        self.assertNotIn("alert(", code)


if __name__ == "__main__":
    unittest.main()
