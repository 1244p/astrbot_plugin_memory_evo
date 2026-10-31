"""v0.6.0 设置页签后端：schema 驱动表单、原子校验、保存即生效。"""

import json
import os
import sys
import tempfile
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import persona  # noqa: E402
import settings  # noqa: E402

SCHEMA_PATH = os.path.join(PLUGIN_DIR, "_conf_schema.json")


def _schema() -> dict:
    data = settings.load_schema(SCHEMA_PATH)
    assert data, "schema 读不出来，检查路径"
    return data


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.schema = _schema()

    def test_every_schema_key_appears_exactly_once(self):
        data = settings.describe(self.schema, {})
        seen = [f["key"] for g in data["groups"] for f in g["fields"]]
        self.assertEqual(sorted(seen), sorted(self.schema))
        self.assertEqual(len(seen), len(set(seen)))

    def test_groups_are_ordered_and_named(self):
        data = settings.describe(self.schema, {})
        ids = [g["id"] for g in data["groups"]]
        self.assertEqual(ids[:6], ["basic", "recall", "persona", "semantic", "care", "model"])
        titles = {g["id"]: g["title"] for g in data["groups"]}
        self.assertEqual(titles["care"], "主动消息")

    def test_every_group_has_description(self):
        data = settings.describe(self.schema, {})
        for g in data["groups"]:
            self.assertIn("desc", g)
            self.assertTrue(g["desc"].strip(), "分组 %s 缺少说明" % g["id"])

    def test_current_values_override_defaults(self):
        data = settings.describe(self.schema, {"top_k": 9, "care_schedule": "09:00-12:00"})
        fields = {f["key"]: f for g in data["groups"] for f in g["fields"]}
        self.assertEqual(fields["top_k"]["value"], 9)
        self.assertEqual(fields["care_schedule"]["value"], "09:00-12:00")
        self.assertEqual(fields["top_k"]["type"], "int")
        self.assertEqual(fields["top_k"]["min"], 1)
        self.assertEqual(fields["top_k"]["max"], 20)

    def test_db_path_marked_restart(self):
        data = settings.describe(self.schema, {})
        fields = {f["key"]: f for g in data["groups"] for f in g["fields"]}
        self.assertTrue(fields["db_path"]["restart"])
        self.assertFalse(fields["top_k"]["restart"])
        self.assertEqual(data["restart_fields"], ["db_path"])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.schema = _schema()

    def test_accepts_typed_payload(self):
        values, errors = settings.validate(
            self.schema,
            {"top_k": "8", "max_step": "0.05", "persona_inject": "false", "care_timezone": "Asia/Tokyo"},
        )
        self.assertEqual(errors, {})
        self.assertEqual(values["top_k"], 8)
        self.assertIsInstance(values["top_k"], int)
        self.assertAlmostEqual(values["max_step"], 0.05)
        self.assertIs(values["persona_inject"], False)
        self.assertEqual(values["care_timezone"], "Asia/Tokyo")

    def test_rejects_unknown_key(self):
        _values, errors = settings.validate(self.schema, {"nope": 1})
        self.assertEqual(errors["nope"], "未知配置项")

    def test_rejects_non_numeric_and_non_integer(self):
        _v, errors = settings.validate(self.schema, {"top_k": "abc", "flush_turns": 2.5})
        self.assertEqual(errors["top_k"], "需要是数字")
        self.assertEqual(errors["flush_turns"], "需要是整数")

    def test_rejects_out_of_range(self):
        _v, errors = settings.validate(self.schema, {"top_k": 500, "care_probability": 1.5, "max_step": -1})
        self.assertEqual(errors["top_k"], "不能大于 20")
        self.assertEqual(errors["care_probability"], "不能大于 1")
        self.assertEqual(errors["max_step"], "不能小于 0")

    def test_time_fields(self):
        _v, errors = settings.validate(self.schema, {"care_quiet_start": "25:00", "care_quiet_end": "8:30"})
        self.assertEqual(errors["care_quiet_start"], "格式要像 23:30")
        self.assertNotIn("care_quiet_end", errors)  # 8:30 也认

    def test_schedule_accepts_and_rejects(self):
        ok, errors = settings.validate(
            self.schema, {"care_schedule": "09:00-12:00，19:00-23:00"}
        )
        self.assertEqual(errors, {})
        self.assertEqual(ok["care_schedule"], "09:00-12:00，19:00-23:00")

        _v, errors = settings.validate(self.schema, {"care_schedule": "09:00~12:00"})
        self.assertIn("09:00~12:00", errors["care_schedule"])

        empty, errors = settings.validate(self.schema, {"care_schedule": "  "})
        self.assertEqual(errors, {})
        self.assertEqual(empty["care_schedule"], "")

    def test_rejects_bad_timezone(self):
        _v, errors = settings.validate(self.schema, {"care_timezone": "Mars/Olympus"})
        self.assertEqual(errors["care_timezone"], "时区名要像 Asia/Shanghai")


class DiffTest(unittest.TestCase):
    def test_ignores_equal_values_and_bool_int_noise(self):
        changed = settings.diff({"care_enable": False, "top_k": 6}, {"care_enable": 0, "top_k": 6})
        self.assertEqual(changed, {})
        changed = settings.diff({"top_k": 6}, {"top_k": 7})
        self.assertEqual(changed, {"top_k": 7})


class SaveFileTest(unittest.TestCase):
    def test_merges_instead_of_overwriting(self):
        path = os.path.join(tempfile.mkdtemp(), "astrbot_plugin_memory_evo_config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"top_k": 6, "care_enable": True}, fh)
        settings.save_to_file(path, {"top_k": 9})
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data, {"top_k": 9, "care_enable": True})

    def test_creates_missing_file(self):
        path = os.path.join(tempfile.mkdtemp(), "sub", "cfg.json")
        settings.save_to_file(path, {"top_k": 4})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"top_k": 4})


class PluginSaveTest(unittest.TestCase):
    def setUp(self):
        import test_plugin_hooks  # 复用它的 astrbot stub 与 main 模块

        self.hooks = test_plugin_hooks
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"db_path": os.path.join(self.tmp, "s.db"), "top_k": 5, "baseline_warmth": 0.5}
        self.plugin = test_plugin_hooks.main_mod.MemoryEvoPlugin(
            test_plugin_hooks.main_mod.Context(), self.cfg
        )

    def test_save_updates_live_config_and_file(self):
        changed = self.plugin._save_settings({"top_k": 8, "care_enable": True})
        self.assertEqual(changed, ["care_enable", "top_k"])
        self.assertEqual(self.plugin.cfg["top_k"], 8)  # 同一对象，钩子立刻看到
        self.assertTrue(self.plugin.cfg["care_enable"])
        path_mod = sys.modules["astrbot.core.utils.astrbot_path"]
        path = os.path.join(
            path_mod.get_astrbot_plugin_data_path(),
            "config",
            f"{self.hooks.main_mod.PLUGIN_NAME}_config.json",
        )
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["top_k"], 8)

    def test_noop_returns_empty(self):
        self.assertEqual(self.plugin._save_settings({"top_k": 5}), [])

    def test_baseline_change_recomputes_self_baseline(self):
        self.plugin._save_settings({"baseline_warmth": 0.9})
        self.assertAlmostEqual(self.plugin.baseline["warmth"], 0.9)

    def test_embedding_provider_change_resets_cache(self):
        self.plugin._emb_resolved = True
        self.plugin._emb_model = "x"
        self.plugin._save_settings({"embedding_provider_id": "emb2"})
        self.assertFalse(self.plugin._emb_resolved)
        self.assertEqual(self.plugin._emb_model, "")


class SettingsApiTest(unittest.TestCase):
    """走一遍 api_settings：GET 出表单、POST 校验失败不落盘、POST 成功即生效。"""

    class FakeRequest:
        def __init__(self):
            self.method = "GET"
            self.payload = {}

        async def json(self, default=None):
            return dict(self.payload) if self.payload else dict(default or {})

    def setUp(self):
        import types

        import test_plugin_hooks  # 装 astrbot stub

        self.hooks = test_plugin_hooks
        self.web = types.ModuleType("astrbot.api.web")
        self.request = self.FakeRequest()
        self.web.request = self.request
        self.web.json_response = lambda data, status_code=200: {
            "data": data,
            "status_code": status_code,
        }
        sys.modules["astrbot.api.web"] = self.web

        self.cfg = {"db_path": os.path.join(tempfile.mkdtemp(), "api.db"), "top_k": 5}
        self.plugin = test_plugin_hooks.main_mod.MemoryEvoPlugin(
            test_plugin_hooks.main_mod.Context(), self.cfg
        )

    def _call(self, method, payload=None):
        import asyncio

        self.request.method = method
        self.request.payload = payload or {}
        return asyncio.run(self.plugin.api_settings())

    def test_get_returns_groups(self):
        res = self._call("GET")
        data = res["data"]
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["groups"]), 6)
        self.assertEqual(data["restart_fields"], ["db_path"])

    def test_post_invalid_keeps_config_untouched(self):
        res = self._call("POST", {"values": {"top_k": 999}})
        data = res["data"]
        self.assertFalse(data["ok"])
        self.assertEqual(data["errors"]["top_k"], "不能大于 20")
        self.assertEqual(self.plugin.cfg["top_k"], 5)

    def test_post_valid_applies_and_reports(self):
        res = self._call("POST", {"values": {"top_k": 9, "care_schedule": "09:00-12:00"}})
        data = res["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["changed"], ["care_schedule", "top_k"])
        self.assertEqual(data["restart_required"], [])
        self.assertEqual(self.plugin.cfg["top_k"], 9)
        self.assertEqual(self.plugin.cfg["care_schedule"], "09:00-12:00")

    def test_post_db_path_flags_restart(self):
        res = self._call("POST", {"values": {"db_path": "/tmp/other.db"}})
        data = res["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["restart_required"], ["db_path"])


if __name__ == "__main__":
    unittest.main()
