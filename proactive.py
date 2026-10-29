"""主动开口的决策与文案生成。纯函数，方便测试。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TZ = "Asia/Shanghai"

CARE_SYSTEM = "你是角色内在的发消息冲动。只输出合法 JSON，不要解释。"

CARE_PROMPT = """你正在决定要不要主动给用户发一条消息。

当前状态：
- 本地时间：__TIME__
- 距离用户上次说话：__IDLE__ 分钟
- 今天你已经主动发了：__SENT_TODAY__ 次
- 上次主动后用户没有回应：__UNANSWERED__ 次
- 你此刻的心情/状态：__PERSONA__
- 你记得的关于 TA 的事：
__MEMORY__
- 你上次主动说的是：__LAST_TEXT__

判断要求：
1. 如果用户刚说完话、或明显在忙、或现在发过去只会打扰，就选择不发。
2. 如果要发：写一句 15~45 字的中文消息，像真人随手发的一样，可以提一件你记得的事，但不要罗列、不要像汇报、不要连续追问。
3. 不要提"记忆""系统""数据库"，不要说"根据我的记忆"。
4. 不要重复上次说过的话。
5. 同时给出下一次最早可以再发消息的间隔（分钟，15~240）。

输出格式：
{"send": true, "message": "在改那个 bug 吗，别熬太晚", "next_gap_minutes": 120}
不发就返回：{"send": false, "reason": "刚聊过", "next_gap_minutes": 90}
"""


def local_now(tz_name: str = DEFAULT_TZ) -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc).astimezone()


def parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, minute = str(value).strip().split(":")
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except Exception:
        return default


def in_quiet_hours(now: datetime, start: str = "23:30", end: str = "08:00") -> bool:
    sh, sm = parse_hhmm(start, (23, 30))
    eh, em = parse_hhmm(end, (8, 0))
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = now.hour * 60 + now.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def decide(
    now: datetime,
    state: dict,
    cfg: dict,
    rng: random.Random | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """返回 (是否发, 原因)。state/cfg 都是普通 dict，便于测试与持久化。

    force=True 时跳过空闲/冷却/每日上限/未回应/概率，只保留总开关、会话静音与免打扰。
    """
    rng = rng or random
    if not cfg.get("enabled", False):
        return False, "disabled"
    if not int(state.get("enabled", 1)):
        return False, "muted"

    unanswered_cap = int(cfg.get("max_unanswered", 2))
    if int(state.get("unanswered", 0)) >= unanswered_cap:
        return False, "unanswered_cap"

    if in_quiet_hours(now, cfg.get("quiet_start", "23:30"), cfg.get("quiet_end", "08:00")):
        return False, "quiet_hours"

    if force:
        return True, "forced"

    min_idle = float(cfg.get("min_idle_minutes", 90))
    gap = float(state.get("next_gap_minutes", 0) or 0)
    idle_minutes = (now.timestamp() - float(state.get("last_active", 0) or 0)) / 60
    if idle_minutes < max(min_idle, gap):
        return False, "too_soon"

    sent_today = int(state.get("sent_today", 0)) if state.get("sent_date") == _today(now) else 0
    if sent_today >= int(cfg.get("daily_max", 3)):
        return False, "daily_cap"

    probability = float(cfg.get("probability", 0.85))
    if probability < 1.0 and rng.random() > probability:
        return False, "probability"

    return True, "ok"


def render_prompt(
    now: datetime,
    state: dict,
    persona_text: str,
    memory_lines: list[str],
    max_chars: int = 60,
) -> str:
    idle = max(0, int((now.timestamp() - float(state.get("last_active", 0) or 0)) / 60))
    memory = "\n".join(f"  - {line}" for line in memory_lines) if memory_lines else "  （没有具体印象）"
    return (
        CARE_PROMPT.replace("__TIME__", now.strftime("%Y-%m-%d %H:%M %A"))
        .replace("__IDLE__", str(idle))
        .replace("__SENT_TODAY__", str(int(state.get("sent_today", 0) or 0)))
        .replace("__UNANSWERED__", str(int(state.get("unanswered", 0) or 0)))
        .replace("__PERSONA__", persona_text or "（无特别状态）")
        .replace("__MEMORY__", memory)
        .replace("__LAST_TEXT__", str(state.get("last_text", "") or "（还没主动说过话）"))
        + f"\n消息长度上限 {max_chars} 字。"
    )


def sanitize_reply(raw: dict, max_chars: int = 60) -> tuple[str | None, int, str]:
    """返回 (消息或 None, 下次间隔分钟, 原因)。"""
    try:
        gap = int(raw.get("next_gap_minutes", 90))
    except (TypeError, ValueError):
        gap = 90
    gap = max(15, min(240, gap))

    reason = str(raw.get("reason") or "")[:60]
    if not raw.get("send"):
        return None, gap, reason or "model_skip"

    text = str(raw.get("message") or "").strip()
    text = text.strip("。\"'“”")
    if not text:
        return None, gap, "empty"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text, gap, reason or "ok"


def next_check_time(gap_minutes: int) -> datetime:
    return local_now() + timedelta(minutes=max(1, int(gap_minutes)))
