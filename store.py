"""SQLite 存储层：事实库 + 人格状态。纯标准库，可脱离 AstrBot 单独测试。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
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
    hits          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, superseded_by);
CREATE INDEX IF NOT EXISTS idx_facts_key   ON facts(scope, subject, predicate);

CREATE TABLE IF NOT EXISTS persona (
    scope      TEXT PRIMARY KEY,
    traits     TEXT NOT NULL,
    mood       TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    traits     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""

_LATIN = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> set[str]:
    """CJK 单字 + 双字，拉丁按词。小规模够用，且无需分词依赖。"""
    text = (text or "").lower()
    tokens = set(_LATIN.findall(text))
    chars = _CJK.findall(text)
    tokens.update(chars)
    tokens.update(a + b for a, b in zip(chars, chars[1:]))
    return tokens


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


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
    extra: dict = field(default_factory=dict)


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

    # ---------------- facts ----------------

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
        """同一 (subject, predicate) 只保留一条有效事实，旧的标记 superseded。

        返回 (id, action)，action ∈ {"created", "updated", "superseded"}。
        """
        now = time.time()
        subject, predicate, object_ = subject.strip(), predicate.strip(), object_.strip()
        if not object_:
            raise ValueError("empty object")
        cur = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND superseded_by IS NULL"
            " AND lower(replace(subject,' ',''))=? AND lower(replace(predicate,' ',''))=?",
            (scope, normalize_key(subject), normalize_key(predicate)),
        )
        rows = cur.fetchall()

        for row in rows:
            if normalize_key(row["object"]) == normalize_key(object_):
                # 同一事实再次被确认：提升置信度，不新增行
                conf = min(1.0, max(row["confidence"], confidence) + 0.05)
                self.conn.execute(
                    "UPDATE facts SET confidence=?, updated_at=?, hits=hits+1 WHERE id=?",
                    (conf, now, row["id"]),
                )
                self.conn.commit()
                return int(row["id"]), "updated"

        importance = max(1, min(5, int(importance)))
        confidence = max(0.0, min(1.0, float(confidence)))
        cur = self.conn.execute(
            "INSERT INTO facts(scope,subject,predicate,object,kind,importance,confidence,"
            "evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (scope, subject, predicate, object_, kind, importance, confidence, evidence[:2000], now, now),
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
        recency_half_life_days: float = 7.0,
        include_pinned: bool = True,
    ) -> list[Fact]:
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND superseded_by IS NULL", (scope,)
        ).fetchall()
        if not rows:
            return []

        q = tokenize(query)
        scored: list[Fact] = []
        for row in rows:
            text_tokens = tokenize(f"{row['subject']} {row['predicate']} {row['object']}")
            overlap = len(q & text_tokens) / len(q) if q else 1.0
            age_days = max(0.0, (now - row["updated_at"]) / 86400)
            recency = 0.5 ** (age_days / max(0.1, recency_half_life_days))
            imp = row["importance"] / 5.0
            score = 0.5 * overlap + 0.2 * recency + 0.2 * imp + 0.1 * row["confidence"]
            if q and overlap == 0 and not (include_pinned and row["importance"] >= 5):
                continue
            scored.append(
                Fact(
                    id=int(row["id"]),
                    subject=row["subject"],
                    predicate=row["predicate"],
                    object=row["object"],
                    kind=row["kind"],
                    importance=int(row["importance"]),
                    confidence=float(row["confidence"]),
                    updated_at=float(row["updated_at"]),
                    score=round(score, 4),
                )
            )

        scored.sort(key=lambda f: f.score, reverse=True)
        picked: list[Fact] = []
        used = 0
        for fact in scored:
            cost = len(fact.subject) + len(fact.predicate) + len(fact.object) + 8
            if picked and used + cost > budget_chars:
                continue
            picked.append(fact)
            used += cost
            if len(picked) >= k:
                break

        if picked:
            ids = [(now, f.id) for f in picked]
            self.conn.executemany("UPDATE facts SET last_hit_at=?, hits=hits+1 WHERE id=?", ids)
            self.conn.commit()
        return picked

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
            " AND lower(replace(predicate,' ',''))=? ORDER BY created_at",
            (scope, normalize_key(subject), normalize_key(predicate)),
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

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
