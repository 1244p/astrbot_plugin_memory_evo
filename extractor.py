"""把对话压成结构化事实与人格增量。只做提示词与解析，不含 IO。"""

from __future__ import annotations

import json
import re

FACT_SYSTEM = "你是一个记忆抽取器。只输出合法 JSON，不要解释、不要 Markdown 代码块以外的文字。"

FACT_PROMPT = """从下面的对话里抽取值得长期记住的信息。

规则：
1. 只抽“以后还用得上”的内容：偏好、习惯、身份、关系、约定、长期目标、明确说过的事实。
2. 必须有对话中的原话支撑，不要推测、不要编造，不要记录寒暄和一次性情绪。
3. subject 是记忆的主体（通常是用户昵称或“用户”），predicate 是简短谓词，object 是一句话的值。
4. 同一事实不要拆成多条；和「已经记住的内容」完全相同的不要重复输出。
   若这次对话更新了其中某条（例如旧的是「正在处理的任务：在修 bug」，现在说修好了），必须输出一条 predicate 与旧记录**完全一致**、object 为新值的条目，这样才能覆盖旧记录；换一个谓词等于新增一条，旧的会一直留着。
5. kind 取值：preference / profile / event / task / fact。
6. importance：1-5，5 = 长期核心（姓名、过敏、重要日期），1 = 轻微。
7. 没有可记的就返回 {"facts": []}。

输出格式：
{"facts":[{"subject":"用户","predicate":"饮食偏好","object":"不吃香菜","kind":"preference","importance":3,"confidence":0.9,"evidence":"原话片段"}]}

已经记住的内容（可能为空）：
__KNOWN__

对话：
__TRANSCRIPT__
"""

PERSONA_SYSTEM = "你是一个角色状态评估器。只输出合法 JSON。"

PERSONA_PROMPT = """根据这段对话，评估角色内在状态应该如何微调。

维度（0-1 的连续量，只能给变化量）：
- intimacy 亲密度：更亲近/更疏远
- playfulness 跳脱度：更爱玩梗/更严肃
- directness 直给度：更直接/更委婉
- initiative 主动度：更主动发起/更被动回应
- warmth 温度：更暖/更冷

规则：
1. 每个维度给出 -0.06 ~ 0.06 之间的变化量，不要更大。
2. 只依据对话中真实发生的互动；没有明显信号就给 0。
3. mood 用不超过 8 个字描述此刻心情。
4. reason 一句话说明依据。

输出格式：
{"deltas":{"intimacy":0.02,"playfulness":0.0,"directness":0.01,"initiative":-0.01,"warmth":0.02},"mood":"有点困","reason":"用户深夜还在聊，语气放松"}

对话：
__TRANSCRIPT__
"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def parse_json(text: str) -> dict:
    """容错解析：去代码块、截取第一个完整 JSON 对象。"""
    if not text:
        return {}
    text = text.strip()
    match = _FENCE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def build_transcript(items: list[tuple[str, str]], max_chars: int = 4000) -> str:
    """items: [(role, text)]，从尾部往前拼，保证不超长。"""
    lines: list[str] = []
    used = 0
    for role, text in reversed(items):
        text = (text or "").strip().replace("\n", " ")
        if not text:
            continue
        line = f"{'用户' if role == 'user' else '助手'}：{text[:600]}"
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def sanitize_facts(raw: dict, max_facts: int = 8, dropped: list[str] | None = None) -> list[dict]:
    """把模型输出变成可入库的条目。

    dropped 传进来一个 list 时，每丢一条就往里追加原因，便于日志排查「为什么 facts 一直是 0」。
    """
    out: list[dict] = []
    items = raw.get("facts") or []
    if not isinstance(items, list):
        if dropped is not None:
            dropped.append("facts 字段不是数组")
        items = []
    elif not items and dropped is not None:
        dropped.append("模型返回空数组")
    for item in items[:max_facts]:
        if not isinstance(item, dict):
            if dropped is not None:
                dropped.append("条目不是对象")
            continue
        obj = str(item.get("object") or "").strip()
        if not obj:
            if dropped is not None:
                dropped.append("object 为空")
            continue
        if len(obj) > 300:
            if dropped is not None:
                dropped.append("object 超过 300 字")
            continue
        kind = str(item.get("kind") or "fact")
        if kind not in {"preference", "profile", "event", "task", "fact"}:
            kind = "fact"
        try:
            importance = int(item.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        out.append(
            {
                "subject": str(item.get("subject") or "用户").strip()[:40] or "用户",
                "predicate": str(item.get("predicate") or "备注").strip()[:60] or "备注",
                "object": obj,
                "kind": kind,
                "importance": max(1, min(5, importance)),
                "confidence": max(0.0, min(1.0, confidence)),
                "evidence": str(item.get("evidence") or "")[:500],
            }
        )
    return out


def sanitize_persona(raw: dict) -> tuple[dict, str, str]:
    deltas = raw.get("deltas") if isinstance(raw.get("deltas"), dict) else {}
    mood = str(raw.get("mood") or "").strip()[:20]
    reason = str(raw.get("reason") or "").strip()[:200]
    return deltas, mood, reason
