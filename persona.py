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


def apply_deltas(
    traits: dict[str, float],
    deltas: dict | None,
    max_step: float = 0.06,
    decay: float = 0.01,
    baseline: dict[str, float] | None = None,
) -> dict[str, float]:
    """一步演化。

    1. 每个维度的变化量被 max_step 截断（防止一次对话就性格大变）
    2. 每步向 baseline 回归 decay（防止长期漂移到极值）
    3. 强制落在 [0, 1]
    """
    baseline = baseline or {k: 0.5 for k in TRAIT_KEYS}
    out: dict[str, float] = {}
    for key in TRAIT_KEYS:
        current = float(traits.get(key, baseline.get(key, 0.5)))
        delta = 0.0
        if deltas and key in deltas:
            try:
                delta = float(deltas[key])
            except (TypeError, ValueError):
                delta = 0.0
        delta = max(-max_step, min(max_step, delta))
        value = current + delta
        value += (float(baseline.get(key, 0.5)) - value) * max(0.0, min(1.0, decay))
        out[key] = round(clamp(value), 4)
    return out


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
