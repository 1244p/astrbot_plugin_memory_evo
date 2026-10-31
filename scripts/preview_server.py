"""本地预览插件面板（开发用）：注入一个假的 bridge，把 index.html 当普通网页跑起来。

真实面板跑在 Dashboard 的受限 iframe 里，靠 `window.AstrBotPluginPage` 跟宿主通信。
这里在 HTML 的 <head> 里插一段 mock 脚本，提供的接口与真实 bridge 同名，
数据是固定的样例，用来截图、调样式，不参与插件运行。

用法：
    python scripts/preview_server.py --port 8123
    # 浏览器打开 http://127.0.0.1:8123/index.html
    # 深色：http://127.0.0.1:8123/index.html?dark=1
    # 指定页签：?tab=persona / care / settings
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS = os.path.join(ROOT, "views", "memory")
sys.path.insert(0, ROOT)

import persona  # noqa: E402
import settings as settings_mod  # noqa: E402

A = "aiocqhttp:FriendMessage:3587BDB1966C264A3A00B33E632BADF5"
B = "aiocqhttp:FriendMessage:9A1C77E0B4D24B77A55C1E7D0A6B2C10"


def fixture_facts() -> dict:
    rows = [
        ("用户", "饮食", "不吃香菜，闻到就皱眉", "preference", 4, 0.91, 12, "active"),
        ("用户", "作息", "两点睡，早上十点起", "profile", 3, 0.74, 5, "active"),
        ("用户", "工作", "在做 AstrBot 插件，主要写 Python", "fact", 5, 0.82, 9, "active"),
        ("用户", "宠物", "养了只橘猫叫年糕，三岁", "fact", 2, 0.66, 2, "active"),
        ("用户", "情绪", "最近在赶论文，压力有点大", "event", 5, 0.58, 1, "suspect"),
        ("助手", "约定", "星期二晚上提醒复诊", "task", 4, 0.7, 0, "active"),
        ("用户", "喜好", "讨厌被叫作「亲」", "preference", 3, 0.88, 7, "active"),
        ("用户", "饮食", "爱吃辣（与不吃香菜同槽）", "preference", 2, 0.41, 0, "superseded"),
        ("用户", "设备", "用的是 ROG 笔记本，Windows 11", "profile", 2, 0.79, 3, "active"),
        ("用户", "社交", "不太喜欢打电话，能打字就打字", "preference", 3, 0.72, 4, "active"),
    ]
    items = []
    for i, (subj, pred, obj, kind, imp, conf, hits, status) in enumerate(rows, start=1):
        items.append(
            {
                "id": len(rows) - i + 1,
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "kind": kind,
                "importance": imp,
                "confidence": conf,
                "hits": hits,
                "status": status,
                "updated_at": 1761700000 - i * 3600 * 7,
                "has_embedding": kind != "event",
                "polarity": "",
            }
        )
    return {"total": len(items), "items": items}


def fixture_persona() -> dict:
    traits = {
        "intimacy": 0.68,
        "playfulness": 0.54,
        "directness": 0.61,
        "initiative": 0.47,
        "warmth": 0.72,
    }
    baseline = {k: 0.5 for k in persona.TRAIT_KEYS}
    return {
        "traits": traits,
        "baseline": baseline,
        "version": 34,
        "log": [
            {
                "version": 34,
                "reason": "用户分享了工作压力，回应偏向安抚",
                "created_at": 1761698000,
                "traits": {"intimacy": 0.68, "playfulness": 0.54, "directness": 0.61,
                           "initiative": 0.47, "warmth": 0.72},
            },
            {
                "version": 33,
                "reason": "连续三轮玩笑来回，放松度上升",
                "created_at": 1761610000,
                "traits": {"intimacy": 0.65, "playfulness": 0.62, "directness": 0.55,
                           "initiative": 0.47, "warmth": 0.62},
            },
            {
                "version": 32,
                "reason": "用户明确要求别绕弯子",
                "created_at": 1761520000,
                "traits": {"intimacy": 0.58, "playfulness": 0.48, "directness": 0.66,
                           "initiative": 0.45, "warmth": 0.6},
            },
        ],
    }


def fixture_care() -> dict:
    return {
        "enabled": True,
        "sent_today": 1,
        "sent_date": "2026-10-30",
        "unanswered": 0,
        "next_at": 0,
        "next_reason": "idle",
        "ready": True,
        "last_text": "睡前别喝咖啡了，你两点才睡的话明天又要熬。",
        "last_sent_at": 1761699000,
        "checked_at": 1761699530,
        "check_reason": "probability",
        "check_text": "这轮概率没抽中",
        "settings": {
            "enabled": True,
            "daily_max": 3,
            "max_unanswered": 2,
            "min_idle_minutes": 60,
            "quiet_start": "23:30",
            "quiet_end": "08:00",
            "schedule": "09:00-23:00",
            "probability": 0.85,
            "max_chars": 45,
        },
    }


def fixture_overview() -> dict:
    return {
        "label": "私聊 · 3587…ADF5",
        "total": 10,
        "by_status": {"active": 8, "suspect": 1, "superseded": 1},
        "by_kind": {"preference": 4, "profile": 2, "fact": 2, "event": 1, "task": 1},
        "superseded": 1,
        "avg_confidence": 0.74,
        "no_embedding": 1,
        "never_hit": 2,
        "unresolved_conflicts": 1,
    }


MOCK_JS = """
<script>
(function () {
  const params = new URLSearchParams(location.search);
  if (params.get("dark") === "1") {
    document.documentElement.setAttribute("data-theme", "dark");
  }
  const DATA = __DATA__;
  function pick(endpoint, q) {
    if (endpoint === "scopes") return DATA.scopes;
    if (endpoint === "overview") {
      return Object.assign({}, DATA.overview, { persona: DATA.persona, care: DATA.care });
    }
    if (endpoint === "facts") return DATA.facts;
    if (endpoint === "persona") return DATA.persona;
    if (endpoint === "care") return DATA.care;
    if (endpoint === "settings") return DATA.settings;
    return {};
  }
  const bridge = {
    ready: async () => ({
      pluginName: "astrbot_plugin_memory_evo",
      displayName: "记忆演化",
      pageName: "memory",
      pageTitle: "记忆演化",
      locale: "zh-CN",
      isDark: params.get("dark") === "1",
    }),
    apiGet: async (endpoint, query) => pick(endpoint, query),
    apiPost: async (endpoint, body) => ({ ok: true, changed: [], groups: DATA.settings.groups }),
    download: async () => {},
    upload: async () => ({}),
    subscribeSSE: () => () => {},
  };
  window.AstrBotPluginView = bridge;
  window.AstrBotPluginPage = bridge;
  const tab = params.get("tab");
  if (tab) {
    window.addEventListener("load", () => {
      const btn = document.querySelector('[data-tab="' + tab + '"]');
      if (btn) btn.click();
    });
  }
})();
</script>
"""


def build_payload() -> dict:
    schema = settings_mod.load_schema(os.path.join(ROOT, settings_mod.SCHEMA_FILENAME))
    values = {
        "top_k": 6,
        "flush_turns": 6,
        "care_min_idle_minutes": 60,
        "care_schedule": "09:00-23:00",
        "care_probability": 0.85,
        "max_step": 0.06,
        "decay": 0.01,
        "min_confidence": 0.45,
    }
    return {
        "scopes": {
            "items": [
                {"scope": A, "label": "私聊 · 3587…ADF5", "total": 8, "last_update": 1761700000},
                {"scope": B, "label": "私聊 · 9A1C…2C10", "total": 2, "last_update": 1761680000},
            ]
        },
        "overview": fixture_overview(),
        "facts": fixture_facts(),
        "persona": fixture_persona(),
        "care": fixture_care(),
        "settings": settings_mod.describe(schema, values),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=VIEWS, **kwargs)

    def log_message(self, *args):  # 静音访问日志
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        super().do_GET()

    def _serve_index(self):
        with open(os.path.join(VIEWS, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        mock = MOCK_JS.replace("__DATA__", json.dumps(build_payload(), ensure_ascii=False))
        html = html.replace("</head>", mock + "</head>", 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"preview: http://127.0.0.1:{args.port}/index.html")
    print(f"dark   : http://127.0.0.1:{args.port}/index.html?dark=1")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
