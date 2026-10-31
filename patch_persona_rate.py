import io, re

NL = chr(10)

# ------------------------------------------------------------------ persona.py
p = "persona.py"
s = io.open(p, encoding="utf-8").read()

start = s.index("def apply_deltas(")
end = s.index("def _word(")
new_math = '''DEFAULT_MAX_STEP = 0.03
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
        spent += abs(value - current)
        out[key] = value
    return out, round(spent, 4)


'''
s = s[:start] + new_math + s[end:]
io.open(p, "w", encoding="utf-8").write(s)
print("persona.py rewritten")

# ------------------------------------------------------------------- store.py
p = "store.py"
s = io.open(p, encoding="utf-8").read()

s = s.replace(
    """CREATE TABLE IF NOT EXISTS persona (
    scope      TEXT PRIMARY KEY,""",
    """CREATE TABLE IF NOT EXISTS persona (
    scope      TEXT PRIMARY KEY,""",
    1,
)

# 迁移：persona 表新增时间化演化所需的三列
old_init_tail = '''INDEXES = """'''
add_migration = '''PERSONA_MIGRATIONS = (
    ("last_evolve_at", "REAL NOT NULL DEFAULT 0"),
    ("day_key", "TEXT NOT NULL DEFAULT ''"),
    ("day_used", "REAL NOT NULL DEFAULT 0"),
)


INDEXES = """'''
assert old_init_tail in s
s = s.replace(old_init_tail, add_migration, 1)

# get_persona 返回时间化字段
old_get = '''        traits = json.loads(row["traits"])
        for key, value in defaults.items():
            traits.setdefault(key, value)
        return {"traits": traits, "mood": row["mood"], "version": int(row["version"])}'''
new_get = '''        traits = json.loads(row["traits"])
        for key, value in defaults.items():
            traits.setdefault(key, value)
        return {
            "traits": traits,
            "mood": row["mood"],
            "version": int(row["version"]),
            "last_evolve_at": float(row["last_evolve_at"] or 0),
            "day_key": str(row["day_key"] or ""),
            "day_used": float(row["day_used"] or 0),
        }'''
assert old_get in s
s = s.replace(old_get, new_get, 1)

# save_persona 落库当日用量
old_save = '''    def save_persona(self, scope: str, traits: dict[str, float], mood: str, reason: str = "") -> int:
        now = time.time()
        current = self.conn.execute("SELECT version FROM persona WHERE scope=?", (scope,)).fetchone()
        version = int(current["version"]) + 1 if current else 1
        self.conn.execute(
            "INSERT INTO persona(scope,traits,mood,version,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET traits=excluded.traits, mood=excluded.mood,"
            " version=excluded.version, updated_at=excluded.updated_at",
            (scope, json.dumps(traits, ensure_ascii=False), mood[:200], version, now),
        )'''
new_save = '''    def save_persona(
        self,
        scope: str,
        traits: dict[str, float],
        mood: str,
        reason: str = "",
        day_key: str = "",
        day_used: float = 0.0,
    ) -> int:
        now = time.time()
        current = self.conn.execute("SELECT version FROM persona WHERE scope=?", (scope,)).fetchone()
        version = int(current["version"]) + 1 if current else 1
        self.conn.execute(
            "INSERT INTO persona(scope,traits,mood,version,updated_at,last_evolve_at,day_key,day_used)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET traits=excluded.traits, mood=excluded.mood,"
            " version=excluded.version, updated_at=excluded.updated_at,"
            " last_evolve_at=excluded.last_evolve_at, day_key=excluded.day_key,"
            " day_used=excluded.day_used",
            (
                scope,
                json.dumps(traits, ensure_ascii=False),
                mood[:200],
                version,
                now,
                now,
                day_key,
                float(day_used),
            ),
        )'''
assert old_save in s
s = s.replace(old_save, new_save, 1)
io.open(p, "w", encoding="utf-8").write(s)
print("store.py patched")
