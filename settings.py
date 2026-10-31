"""插件设置：把 `_conf_schema.json` 变成能在 WebUI 里编辑的表单，并在保存前做校验。

设计上的三个约束：
1. **schema 是唯一事实来源**：字段名、说明、默认值都从 `_conf_schema.json` 读，不在这里抄一遍。
2. **要么全改，要么不改**：先校验全部字段，任何一个不合法就整体拒绝，避免存进半套配置。
3. **不重启就生效**：改完直接写回插件的 AstrBotConfig，`self.cfg` 是同一个对象，钩子下一轮就读到新值。
"""

from __future__ import annotations

import json
import os
import re

SCHEMA_FILENAME = "_conf_schema.json"

# 展示顺序与分组。没列进来的 key 落到「其他」，这样新增配置项不会从面板里消失。
GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "basic",
        "基础",
        ["enable", "db_path", "flush_turns", "idle_seconds", "max_buffer", "llm_timeout"],
    ),
    (
        "recall",
        "注入与召回",
        [
            "top_k",
            "max_inject_chars",
            "min_confidence",
            "include_suspect",
            "max_per_predicate",
            "max_item_chars",
        ],
    ),
    (
        "persona",
        "人格",
        [
            "persona_inject",
            "persona_max_chars",
            "persona_evolve",
            "max_step",
            "decay",
            "baseline_intimacy",
            "baseline_playfulness",
            "baseline_directness",
            "baseline_initiative",
            "baseline_warmth",
        ],
    ),
    (
        "semantic",
        "语义召回",
        ["semantic_recall", "embedding_provider_id", "embedding_timeout"],
    ),
    (
        "care",
        "主动消息",
        [
            "care_enable",
            "care_min_idle_minutes",
            "care_daily_max",
            "care_max_unanswered",
            "care_quiet_start",
            "care_quiet_end",
            "care_schedule",
            "care_probability",
            "care_max_chars",
            "care_timezone",
            "care_provider_id",
        ],
    ),
    ("model", "抽取模型", ["extract_provider_id"]),
    ("log", "日志", ["log_level", "log_days", "log_mirror"]),
]

GROUP_TITLES = {gid: title for gid, title, _ in GROUPS}

# 面板设置页每个分组标题下面的一行说明。写清楚“这组影响什么”，而不是复述字段名。
GROUP_DESCS: dict[str, str] = {
    "basic": "总开关、抽取节奏与缓冲上限。调快抽取能看到更多细节，也更烧 token。",
    "recall": "每轮往提示词里塞多少记忆、按什么门槛筛。数字越大越啰嗦。",
    "persona": "人格五维的注入、单次演化步长与回落基线。基线决定它长期的性格底色。",
    "semantic": "句向量召回。没配嵌入模型时自动退回字面重叠匹配，功能仍在。",
    "care": "用户安静多久后主动开口，以及哪些时间不许打扰。",
    "model": "抽取用的模型。留空则跟随当前会话模型。",
    "log": "插件自己的事件日志，排查问题时看这里，不用翻 AstrBot 主日志。",
    "other": "暂未归组的配置项。",
}

# 数值边界。schema 里没写 min/max，这里补上，免得有人把 top_k 填成 500。
LIMITS: dict[str, tuple[float, float]] = {
    "top_k": (1, 20),
    "max_inject_chars": (100, 4000),
    "flush_turns": (1, 50),
    "idle_seconds": (5, 600),
    "max_buffer": (5, 200),
    "llm_timeout": (5, 180),
    "log_days": (1, 90),
    "min_confidence": (0.0, 1.0),
    "max_per_predicate": (1, 5),
    "max_item_chars": (20, 200),
    "persona_max_chars": (40, 800),
    "max_step": (0.0, 0.2),
    "decay": (0.0, 0.1),
    "embedding_timeout": (2, 60),
    "care_min_idle_minutes": (5, 1440),
    "care_daily_max": (0, 20),
    "care_max_unanswered": (1, 10),
    "care_probability": (0.0, 1.0),
    "care_max_chars": (10, 200),
}
for _k in ("baseline_intimacy", "baseline_playfulness", "baseline_directness",
           "baseline_initiative", "baseline_warmth"):
    LIMITS[_k] = (0.0, 1.0)

# 改了要重启才彻底生效的字段（库连接在 __init__ 里就建好了）
RESTART_FIELDS = {"db_path"}

_HHMM = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def load_schema(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_TZ_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){1,2}")


def _valid_tz(text: str) -> bool:
    """时区名校验：优先问 zoneinfo，查不到时区分「名字错」和「环境没有 tzdata」。"""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python 3.9 以下才会缺
        return bool(_TZ_SHAPE.fullmatch(text))

    try:
        ZoneInfo(text)
        return True
    except Exception as exc:
        if type(exc).__name__ != "ZoneInfoNotFoundError":
            return False
        try:
            ZoneInfo("UTC")
        except Exception:
            # 连 UTC 都建不出来：环境没装 tzdata，只能退回格式检查
            return bool(_TZ_SHAPE.fullmatch(text))
        return False


def _coerce(key: str, meta: dict, raw):
    """把前端传来的值转成该字段的类型，顺带检查边界。返回 (value, error)。"""
    ftype = str(meta.get("type") or "string")

    if ftype == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.lower() in {"true", "1", "yes", "on"}:
            return True, None
        if isinstance(raw, str) and raw.lower() in {"false", "0", "no", "off"}:
            return False, None
        if isinstance(raw, (int, float)):
            return bool(raw), None
        return None, "需要是开关值"

    if ftype in {"int", "float"}:
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return None, "需要是数字"
        if ftype == "int":
            if abs(num - round(num)) > 1e-9:
                return None, "需要是整数"
            num = int(round(num))
        lo, hi = LIMITS.get(key, (None, None))
        if lo is not None and num < lo:
            return None, f"不能小于 {lo:g}"
        if hi is not None and num > hi:
            return None, f"不能大于 {hi:g}"
        return num, None

    text = "" if raw is None else str(raw).strip()

    if key in {"care_quiet_start", "care_quiet_end"}:
        if not _HHMM.fullmatch(text):
            return None, "格式要像 23:30"
        return text, None

    if key == "care_schedule":
        if not text:
            return "", None  # 留空 = 不限时段
        for piece in text.replace("；", ",").replace("，", ",").split(","):
            piece = piece.strip()
            if not piece:
                continue
            left, _, right = piece.partition("-")
            if not _HHMM.fullmatch(left) or not _HHMM.fullmatch(right):
                return None, f"「{piece}」格式要像 09:00-12:00"
        return text, None

    if key == "care_timezone":
        if not text:
            return None, "不能为空"
        if not _valid_tz(text):
            return None, "时区名要像 Asia/Shanghai"
        return text, None

    return text, None


def _group_of(key: str) -> str:
    for gid, _title, keys in GROUPS:
        if key in keys:
            return gid
    return "other"


def describe(schema: dict, values: dict) -> dict:
    """给前端渲染用的字段描述：分组、类型、说明、当前值。"""
    buckets: dict[str, list[dict]] = {gid: [] for gid, _, _ in GROUPS}
    buckets["other"] = []
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        item = {
            "key": key,
            "label": str(meta.get("description") or key),
            "type": str(meta.get("type") or "string"),
            "hint": str(meta.get("hint") or ""),
            "default": meta.get("default"),
            "value": values.get(key, meta.get("default")),
            "restart": key in RESTART_FIELDS,
        }
        lo, hi = LIMITS.get(key, (None, None))
        if lo is not None:
            item["min"] = lo
            item["max"] = hi
        buckets[_group_of(key)].append(item)

    ordered = [
        {
            "id": gid,
            "title": title,
            "desc": GROUP_DESCS.get(gid, ""),
            "fields": buckets.get(gid, []),
        }
        for gid, title, _ in GROUPS
    ]
    if buckets["other"]:
        ordered.append(
            {
                "id": "other",
                "title": "其他",
                "desc": GROUP_DESCS["other"],
                "fields": buckets["other"],
            }
        )
    return {"groups": ordered, "restart_fields": sorted(RESTART_FIELDS)}


def validate(schema: dict, payload: dict) -> tuple[dict, dict]:
    """返回 (要写入的值, 每字段错误)。有任何一个错误时，调用方不要落盘。"""
    values: dict = {}
    errors: dict[str, str] = {}
    for key, raw in (payload or {}).items():
        meta = schema.get(key)
        if not isinstance(meta, dict):
            errors[key] = "未知配置项"
            continue
        value, err = _coerce(key, meta, raw)
        if err:
            errors[key] = err
        else:
            values[key] = value
    return values, errors


def diff(current: dict, new: dict) -> dict:
    changed = {}
    for key, value in new.items():
        old = current.get(key)
        if isinstance(old, bool) or isinstance(value, bool):
            if bool(old) != bool(value):
                changed[key] = value
        elif old != value:
            changed[key] = value
    return changed


def save_to_file(path: str, changed: dict) -> None:
    """没有 AstrBotConfig 时的兜底：直接把改动合进插件配置文件。"""
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data.update(changed)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=4)
