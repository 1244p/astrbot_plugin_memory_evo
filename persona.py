"""人格演化数学：有限步长 + 基线回归 + 硬边界。纯函数，易测试。"""

from __future__ import annotations

TRAIT_KEYS = ("intimacy", "playfulness", "directness", "initiative", "warmth")

TRAIT_LABELS = {
    "intimacy": "亲密度",
    "playfulness": "跳脱度",
    "directness": "直给度",
    "initiative": "主动度",
    "warmth": "温度",
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def sanitize_traits(raw: dict | None, defaults: dict[str, float]) -> dict[str, float]:
    out = dict(defaults)
    for key in TRAIT_KEYS:
        if raw and key in raw:
            try:
                out[key] = clamp(float(raw[key]))
            except (TypeError, ValueError):
                pass
    return out


DEFAULT_MAX_STEP = 0.03
DEFAULT_DAILY_BUDGET = 0.15
DEFAULT_HALF_LIFE_DAYS = 7.0


def move_step(
    max_step: float,
    hours: float,
    daily_used: float = 0.0,
    daily_budget: float = DEFAULT_DAILY_BUDGET,
) -> float:
    """这一次 flush 允许移动多少。

    三条上限取最小：
    1. max_step —— 单次硬上限
    2. 时间配速 —— max_step * min(1, 距今小时/24)，五分钟前刚演化过就几乎不动
    3. 当日余额 —— daily_budget 减去今天已经移动掉的总量

    频次不该决定演化速度：一天聊 100 轮和聊 5 轮，理论位移天花板应当一致。
    """
    hours = max(0.0, float(hours))
    used = max(0.0, float(daily_used))
    budget = max(0.0, float(daily_budget))
    paced = max(0.0, float(max_step)) * min(1.0, hours / 24.0)
    allowance = max(0.0, budget - used)
    return round(min(max(0.0, float(max_step)), paced, allowance), 4)


def relax_factor(hours: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """向基线回归的比例，按真实时间算：half_life_days 天后走完一半距离。"""
    hl_hours = max(0.5, float(half_life_days)) * 24.0
    return 1.0 - 0.5 ** (max(0.0, float(hours)) / hl_hours)


def apply_deltas(
    traits: dict[str, float],
    deltas: dict | None,
    max_step: float = DEFAULT_MAX_STEP,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    baseline: dict[str, float] | None = None,
    hours: float = 24.0,
    daily_used: float = 0.0,
    daily_budget: float = DEFAULT_DAILY_BUDGET,
) -> tuple[dict[str, float], float]:
    """一步演化，返回 (新状态, 本步用掉的位移量)。

    1. 每个维度的变化量被 move_step 截断（时间配速 + 当日预算）
    2. 每步按真实时间向 baseline 回归一半衰期（防止长期漂到极值）
    3. 强制落在 [0, 1]
    """
    baseline = baseline or {k: 0.5 for k in TRAIT_KEYS}
    step = move_step(max_step, hours, daily_used, daily_budget)
    relax = relax_factor(hours, half_life_days)
    out: dict[str, float] = {}
    spent = 0.0
    for key in TRAIT_KEYS:
        current = float(traits.get(key, baseline.get(key, 0.5)))
        delta = 0.0
        if deltas and key in deltas:
            try:
                delta = float(deltas[key])
            except (TypeError, ValueError):
                delta = 0.0
        delta = max(-step, min(step, delta))
        value = current + delta
        value += (float(baseline.get(key, 0.5)) - value) * relax
        value = round(clamp(value), 4)
        # 预算只算「模型推的那一下」，不算半衰期回归——
        # 否则离基线越远，回归本身就把预算吃光，真正想动反而动不了。
        spent += abs(delta)
        out[key] = value
    return out, round(spent, 4)


def _word(value: float) -> str:
    if value < 0.25:
        return "很低"
    if value < 0.45:
        return "偏低"
    if value < 0.6:
        return "中等"
    if value < 0.8:
        return "偏高"
    return "很高"


def render(traits: dict[str, float], mood: str = "", max_chars: int = 160) -> str:
    """渲染成一句可注入的状态描述。不罗列数字，避免模型复读数值。"""
    parts = [f"{TRAIT_LABELS[k]}{_word(float(traits.get(k, 0.5)))}" for k in TRAIT_KEYS]
    text = "当前状态：" + "、".join(parts) + "。"
    if mood:
        text += f"此刻心情：{mood.strip()}。"
    text += "自然地体现在语气和取舍里，不要直白复述这些设定。"
    return text[:max_chars]
