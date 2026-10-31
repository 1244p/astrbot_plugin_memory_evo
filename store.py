"""SQLite 存储层：事实库 + 人格状态。纯标准库，可脱离 AstrBot 单独测试。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    predicate     TEXT    NOT NULL,
    object        TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'fact',
    importance    INTEGER NOT NULL DEFAULT 3,
    confidence    REAL    NOT NULL DEFAULT 0.7,
    evidence      TEXT    NOT NULL DEFAULT '',
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    superseded_by INTEGER,
    last_hit_at   REAL    NOT NULL DEFAULT 0,
    hits          INTEGER NOT NULL DEFAULT 0,
    polarity      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'active',
    conflict_with INTEGER,
    embedding     BLOB,
    embedding_model TEXT
);
CREATE TABLE IF NOT EXISTS persona (
    scope      TEXT PRIMARY KEY,
    traits     TEXT NOT NULL,
    mood       TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proactive (
    scope        TEXT PRIMARY KEY,
    last_active  REAL    NOT NULL DEFAULT 0,
    last_sent    REAL    NOT NULL DEFAULT 0,
    sent_date    TEXT    NOT NULL DEFAULT '',
    sent_today   INTEGER NOT NULL DEFAULT 0,
    unanswered   INTEGER NOT NULL DEFAULT 0,
    next_gap     INTEGER NOT NULL DEFAULT 0,
    last_text    TEXT    NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS persona_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    traits     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, superseded_by, status);
CREATE INDEX IF NOT EXISTS idx_facts_key   ON facts(scope, subject, predicate);
"""

# 半衰期按事实类型分档：偏好活得久，任务过期快
HALF_LIFE_DAYS = {
    "preference": 30.0,
    "profile": 60.0,
    "fact": 14.0,
    "event": 7.0,
    "task": 1.0,
}
DEFAULT_HALF_LIFE = 14.0

# 谓词别名归一：把「忌口 / 口味 / 吃」收敛到同一个槽位，才能发现矛盾
PREDICATE_ALIASES: dict[str, str] = {
    "饮食": "饮食",
    "忌口": "饮食",
    "口味": "饮食",
    "吃": "饮食",
    "食物": "饮食",
    "作息": "作息",
    "睡眠": "作息",
    "睡觉": "作息",
    "熬夜": "作息",
    "宠物": "宠物",
    "养": "宠物",
    "职业": "职业",
    "工作": "职业",
    "上班": "职业",
    "居住": "居住地",
    "城市": "居住地",
    "住": "居住地",
}

_NEG_WORDS = ("不吃", "不爱", "不喝", "不喜", "不喜欢", "不", "别", "没", "无", "忌", "讨厌", "反感", "拒绝", "戒", "禁")
_POS_WORDS = ("超爱", "爱吃", "爱喝", "喜欢", "爱", "常", "每天", "总", "偏好", "习惯", "每日", "最")
_CORE_STRIP = re.compile(r"[，,。.、；;：:\s的了吧呢啊]+")


def tokenize(text: str) -> set[str]:
    """CJK 单字 + 双字，拉丁按词。小规模够用，且无需分词依赖。"""
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]+", text))
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update(chars)
    tokens.update(a + b for a, b in zip(chars, chars[1:]))
    return tokens


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def canonical_predicate(predicate: str) -> str:
    key = (predicate or "").strip().replace(" ", "")
    if not key:
        return "备注"
    for alias, canon in PREDICATE_ALIASES.items():
        if alias in key:
            return canon
    return key


def analyze_object(text: str) -> tuple[int, str]:
    """返回 (极性, 核心词)。用于发现「爱吃香菜 / 不吃香菜」这类矛盾。"""
    neg = any(w in text for w in _NEG_WORDS)
    pos = any(w in text for w in _POS_WORDS)
    polarity = -1 if (neg and not pos) else (1 if (pos and not neg) else 0)
    core = text
    for word in _NEG_WORDS + _POS_WORDS:
        core = core.replace(word, "")
    core = _CORE_STRIP.sub("", core)
    return polarity, core


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        return list(vec)
    return [v / norm for v in vec]


def _pack(vec: list[float]) -> bytes:
    return array("f", _l2_normalize(vec)).tobytes()


def _unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(array("f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _cores_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 2 and shorter in longer


@dataclass
class Fact:
    id: int
    subject: str
    predicate: str
    object: str
    kind: str
    importance: int
    confidence: float
    updated_at: float
    score: float = 0.0
    status: str = "active"
    truncated: bool = False
    extra: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = {}


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()
        self.conn.executescript(INDEXES)
        self.conn.commit()

    def _migrate(self) -> None:
        """给老库补列，避免升级即报错。"""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(facts)")}
        for name, ddl in (
            ("polarity", "ALTER TABLE facts ADD COLUMN polarity INTEGER NOT NULL DEFAULT 0"),
            ("status", "ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"),
            ("conflict_with", "ALTER TABLE facts ADD COLUMN conflict_with INTEGER"),
            ("embedding", "ALTER TABLE facts ADD COLUMN embedding BLOB"),
            ("embedding_model", "ALTER TABLE facts ADD COLUMN embedding_model TEXT"),
        ):
            if name not in cols:
                self.conn.execute(ddl)
        self.conn.commit()

    # ---------------- facts ----------------

    def known_facts(self, scope: str, limit: int = 8) -> list[dict]:
        """当前有效记忆的摘要，喂给抽取提示词，让模型能输出「同 predicate 的新值」。"""
        cur = self.conn.execute(
            "SELECT predicate, object, kind FROM facts"
            " WHERE scope=? AND superseded_by IS NULL AND status='active'"
            " ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (scope, max(1, int(limit))),
        )
        return [dict(row) for row in cur.fetchall()]

    def upsert_fact(
        self,
        scope: str,
        subject: str,
        predicate: str,
        object_: str,
        kind: str = "fact",
        importance: int = 3,
        confidence: float = 0.7,
        evidence: str = "",
    ) -> tuple[int, str]:
        """同一 (主体, 归一化谓词) 只保留一条有效事实。

        返回 (id, action)，action ∈ {created, updated, superseded, conflict}。
        conflict 表示新旧内容极性相反（爱吃 / 不吃），两条都进「待确认」，不参与注入。
        """
        now = time.time()
        subject = subject.strip() or "用户"
        object_ = object_.strip()
        if not object_:
            raise ValueError("empty object")
        canon = canonical_predicate(predicate)
        polarity, core = analyze_object(object_)
        importance = max(1, min(5, int(importance)))
        confidence = max(0.0, min(1.0, float(confidence)))

        cur = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND superseded_by IS NULL AND status='active'"
            " AND lower(replace(subject,' ',''))=? AND predicate=?",
            (scope, normalize_key(subject), canon),
        )
        rows = cur.fetchall()

        for row in rows:
            row_core_pol, row_core = analyze_object(row["object"])
            if polarity * row_core_pol < 0 and _cores_overlap(core, row_core):
                cur2 = self.conn.execute(
                    "INSERT INTO facts(scope,subject,predicate,object,kind,importance,confidence,"
                    "evidence,created_at,updated_at,polarity,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (scope, subject, canon, object_, kind, importance, confidence,
                     evidence[:2000], now, now, polarity, "suspect"),
                )
                new_id = int(cur2.lastrowid or 0)
                self.conn.execute(
                    "UPDATE facts SET status='suspect', conflict_with=? WHERE id=?",
                    (new_id, int(row["id"])),
                )
                self.conn.execute(
                    "UPDATE facts SET conflict_with=? WHERE id=?", (int(row["id"]), new_id)
                )
                self.conn.commit()
                return new_id, "conflict"

            if normalize_key(row["object"]) == normalize_key(object_):
                conf = min(1.0, max(row["confidence"], confidence) + 0.05)
                self.conn.execute(
                    "UPDATE facts SET confidence=?, updated_at=?, hits=hits+1 WHERE id=?",
                    (conf, now, row["id"]),
                )
                self.conn.commit()
                return int(row["id"]), "updated"

        cur = self.conn.execute(
            "INSERT INTO facts(scope,subject,predicate,object,kind,importance,confidence,"
            "evidence,created_at,updated_at,polarity,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope, subject, canon, object_, kind, importance, confidence,
             evidence[:2000], now, now, polarity, "active"),
        )
        new_id = int(cur.lastrowid or 0)
        action = "created"
        if rows:
            self.conn.executemany(
                "UPDATE facts SET superseded_by=?, updated_at=? WHERE id=?",
                [(new_id, now, int(r["id"])) for r in rows],
            )
            action = "superseded"
        self.conn.commit()
        return new_id, action

    def recall(
        self,
        scope: str,
        query: str = "",
        k: int = 6,
        budget_chars: int = 600,
        min_confidence: float = 0.45,
        include_suspect: bool = False,
        max_per_predicate: int = 2,
        max_item_chars: int = 80,
        query_vec: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> list[Fact]:
        """按相关度取 top-k。

        有 query_vec 且该条记忆已写向量时用语义相似度，否则退回字面匹配。
        两条路共用同一套新近度/重要度/置信度权重，所以换了召回方式行为不会突变。
        """
        now = time.time()
        statuses = ("active", "suspect") if include_suspect else ("active",)
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT * FROM facts WHERE scope=? AND superseded_by IS NULL"
            f" AND status IN ({placeholders})",
            (scope, *statuses),
        ).fetchall()
        if not rows:
            return []

        q = tokenize(query)
        qv = _l2_normalize([float(x) for x in query_vec]) if query_vec else None
        semantic = bool(qv)
        scored: list[Fact] = []

        for row in rows:
            imp = int(row["importance"])
            conf = float(row["confidence"])
            if conf < min_confidence and imp < 5:
                continue

            cos = None
            if semantic:
                emb = _unpack(row["embedding"])
                if emb and len(emb) == len(qv) and (
                    embedding_model is None or row["embedding_model"] == embedding_model
                ):
                    cos = _cosine(qv, emb)

            half_life = HALF_LIFE_DAYS.get(row["kind"], DEFAULT_HALF_LIFE)
            age_days = max(0.0, (now - row["updated_at"]) / 86400)
            recency = 0.5 ** (age_days / max(0.1, half_life))
            base = 0.2 * recency + 0.2 * (imp / 5.0) + 0.1 * conf

            if cos is not None:
                score = 0.5 * cos + base
                relevant = cos >= 0.2 or not q or imp >= 5
            else:
                text_tokens = tokenize(f"{row['subject']} {row['predicate']} {row['object']}")
                overlap = len(q & text_tokens) / len(q) if q else 1.0
                score = 0.5 * overlap + base
                relevant = (not q) or overlap > 0 or imp >= 5
            if not relevant:
                continue

            obj = row["object"]
            truncated = False
            if max_item_chars and len(obj) > max_item_chars:
                obj = obj[:max_item_chars].rstrip() + "…"
                truncated = True
            scored.append(
                Fact(
                    id=int(row["id"]),
                    subject=row["subject"],
                    predicate=row["predicate"],
                    object=obj,
                    kind=row["kind"],
                    importance=imp,
                    confidence=conf,
                    updated_at=float(row["updated_at"]),
                    score=round(score, 4),
                    status=row["status"],
                    truncated=truncated,
                )
            )

        scored.sort(key=lambda f: f.score, reverse=True)
        picked: list[Fact] = []
        used = 0
        per_predicate: dict[str, int] = {}
        for fact in scored:
            if per_predicate.get(fact.predicate, 0) >= max_per_predicate:
                continue
            cost = len(fact.subject) + len(fact.predicate) + len(fact.object) + 8
            if picked and used + cost > budget_chars:
                continue
            picked.append(fact)
            per_predicate[fact.predicate] = per_predicate.get(fact.predicate, 0) + 1
            used += cost
            if len(picked) >= k:
                break

        if picked:
            ids = [(now, f.id) for f in picked]
            self.conn.executemany("UPDATE facts SET last_hit_at=?, hits=hits+1 WHERE id=?", ids)
            self.conn.commit()
        return picked

    def save_embedding(
        self, scope: str, fact_id: int, vec: list[float], model: str
    ) -> bool:
        cur = self.conn.execute(
            "UPDATE facts SET embedding=?, embedding_model=? WHERE scope=? AND id=?",
            (_pack(vec), model, scope, fact_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def missing_embeddings(
        self, scope: str, model: str, limit: int = 32
    ) -> list[tuple[int, str]]:
        rows = self.conn.execute(
            "SELECT id, subject, predicate, object FROM facts"
            " WHERE scope=? AND superseded_by IS NULL"
            " AND (embedding IS NULL OR embedding_model IS NOT ?)"
            " ORDER BY updated_at DESC LIMIT ?",
            (scope, model, limit),
        ).fetchall()
        return [(int(r["id"]), f"{r['subject']} {r['predicate']} {r['object']}") for r in rows]

    def embedding_stats(self, scope: str, model: str) -> tuple[int, int]:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE scope=? AND superseded_by IS NULL", (scope,)
        ).fetchone()[0]
        done = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE scope=? AND superseded_by IS NULL"
            " AND embedding IS NOT NULL AND embedding_model = ?",
            (scope, model),
        ).fetchone()[0]
        return int(done), int(total)

    def confirm_fact(self, scope: str, fact_id: int) -> bool:
        """把「待确认」事实转正，并把冲突的那条标记为被取代。"""
        row = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND id=?", (scope, fact_id)
        ).fetchone()
        if not row:
            return False
        now = time.time()
        other = row["conflict_with"]
        self.conn.execute(
            "UPDATE facts SET status='active', conflict_with=NULL, updated_at=? WHERE id=?",
            (now, fact_id),
        )
        if other:
            self.conn.execute(
                "UPDATE facts SET superseded_by=?, status='active', conflict_with=NULL, updated_at=?"
                " WHERE id=?",
                (fact_id, now, int(other)),
            )
        self.conn.commit()
        return True

    def list_facts(self, scope: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND superseded_by IS NULL"
            " ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (scope, limit),
        ).fetchall()

    def delete_fact(self, scope: str, fact_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM facts WHERE scope=? AND id=?", (scope, fact_id))
        self.conn.commit()
        return cur.rowcount > 0

    def clear_scope(self, scope: str) -> int:
        cur = self.conn.execute("DELETE FROM facts WHERE scope=?", (scope,))
        self.conn.commit()
        return cur.rowcount

    def history_of(self, scope: str, subject: str, predicate: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND lower(replace(subject,' ',''))=?"
            " AND predicate=? ORDER BY created_at",
            (scope, normalize_key(subject), canonical_predicate(predicate)),
        ).fetchall()

    def forget_matching(self, scope: str, keyword: str) -> int:
        kw = f"%{keyword}%"
        cur = self.conn.execute(
            "DELETE FROM facts WHERE scope=? AND (object LIKE ? OR predicate LIKE ? OR subject LIKE ?)",
            (scope, kw, kw, kw),
        )
        self.conn.commit()
        return cur.rowcount

    # ---------------- persona ----------------

    def get_persona(self, scope: str, defaults: dict[str, float]) -> dict:
        row = self.conn.execute("SELECT * FROM persona WHERE scope=?", (scope,)).fetchone()
        if not row:
            traits = dict(defaults)
            self.conn.execute(
                "INSERT INTO persona(scope,traits,mood,version,updated_at) VALUES(?,?,?,?,?)",
                (scope, json.dumps(traits, ensure_ascii=False), "", 1, time.time()),
            )
            self.conn.commit()
            return {"traits": traits, "mood": "", "version": 1}
        traits = json.loads(row["traits"])
        for key, value in defaults.items():
            traits.setdefault(key, value)
        return {"traits": traits, "mood": row["mood"], "version": int(row["version"])}

    def save_persona(self, scope: str, traits: dict[str, float], mood: str, reason: str = "") -> int:
        now = time.time()
        current = self.conn.execute("SELECT version FROM persona WHERE scope=?", (scope,)).fetchone()
        version = int(current["version"]) + 1 if current else 1
        self.conn.execute(
            "INSERT INTO persona(scope,traits,mood,version,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET traits=excluded.traits, mood=excluded.mood,"
            " version=excluded.version, updated_at=excluded.updated_at",
            (scope, json.dumps(traits, ensure_ascii=False), mood[:200], version, now),
        )
        self.conn.execute(
            "INSERT INTO persona_log(scope,traits,reason,created_at) VALUES(?,?,?,?)",
            (scope, json.dumps(traits, ensure_ascii=False), reason[:500], now),
        )
        self.conn.commit()
        return version

    def persona_timeline(self, scope: str, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM persona_log WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()

    # ---------------- 主动开口 ----------------

    def touch_activity(self, scope: str, ts: float | None = None) -> None:
        """记录用户说话：刷新活跃时间并清零未回应计数。"""
        now = float(ts or time.time())
        self.conn.execute(
            "INSERT INTO proactive(scope,last_active,unanswered) VALUES(?,?,0)"
            " ON CONFLICT(scope) DO UPDATE SET last_active=excluded.last_active,"
            " unanswered=0",
            (scope, now),
        )
        self.conn.commit()

    def known_scopes(self) -> list[str]:
        """库里出现过的会话：事实表 ∪ 主动消息表。

        主动消息的 tick 只遍历内存里的 _scopes，进程重启后如果没人说话就永远是空集合，
        表现就是「重启之后再也不主动开口」。上电时用它把集合补回来。
        """
        rows = self.conn.execute(
            "SELECT scope FROM facts UNION SELECT scope FROM proactive"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_care_state(self, scope: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM proactive WHERE scope=?", (scope,)
        ).fetchone()
        if not row:
            return {
                "last_active": 0.0,
                "last_sent": 0.0,
                "sent_date": "",
                "sent_today": 0,
                "unanswered": 0,
                "next_gap_minutes": 0,
                "last_text": "",
                "enabled": 1,
            }
        return {
            "last_active": float(row["last_active"]),
            "last_sent": float(row["last_sent"]),
            "sent_date": row["sent_date"],
            "sent_today": int(row["sent_today"]),
            "unanswered": int(row["unanswered"]),
            "next_gap_minutes": int(row["next_gap"]),
            "last_text": row["last_text"],
            "enabled": int(row["enabled"]),
        }

    def mark_care_sent(
        self,
        scope: str,
        ts: float,
        date_str: str,
        text: str,
        next_gap: int,
    ) -> None:
        state = self.get_care_state(scope)
        # 距上次主动之后用户没再说话 → 未回应计数 +1
        unanswered = state["unanswered"] + 1 if state["last_sent"] > state["last_active"] else 1
        sent_today = state["sent_today"] + 1 if state["sent_date"] == date_str else 1
        self.conn.execute(
            "INSERT INTO proactive(scope,last_sent,sent_date,sent_today,unanswered,next_gap,last_text)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET last_sent=excluded.last_sent,"
            " sent_date=excluded.sent_date, sent_today=excluded.sent_today,"
            " unanswered=excluded.unanswered, next_gap=excluded.next_gap,"
            " last_text=excluded.last_text",
            (scope, ts, date_str, sent_today, unanswered, int(next_gap), text[:200]),
        )
        self.conn.commit()

    def set_care_enabled(self, scope: str, enabled: bool) -> None:
        self.conn.execute(
            "INSERT INTO proactive(scope,enabled) VALUES(?,?)"
            " ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled",
            (scope, 1 if enabled else 0),
        )
        self.conn.commit()

    def mark_care_skipped(self, scope: str, next_gap: int) -> None:
        self.conn.execute(
            "INSERT INTO proactive(scope,next_gap) VALUES(?,?)"
            " ON CONFLICT(scope) DO UPDATE SET next_gap=excluded.next_gap",
            (scope, int(next_gap)),
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
