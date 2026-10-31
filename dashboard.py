"""Dashboard 查询层：只读统计 + 少量显式操作（删除 / 确认）。

与 store.py 分开的原因：store 关心「怎么记」，这里关心「怎么看」。
所有函数都接收 sqlite3 连接，便于脱离 AstrBot 单测。
"""

from __future__ import annotations

import json
import sqlite3
import time

KINDS = ("preference", "profile", "fact", "event", "task")
STATUSES = ("active", "suspect", "superseded")


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


_SCOPE_KIND = {
    "FriendMessage": "私聊",
    "GroupMessage": "群聊",
    "GuildMessage": "频道",
    "DMs": "私信",
}


def scope_label(scope: str) -> str:
    """`aiocqhttp:FriendMessage:123456` → `私聊 · 123456`；认不出的格式原样返回。"""
    parts = (scope or "").split(":")
    if len(parts) >= 3 and parts[0] and parts[1]:
        kind = _SCOPE_KIND.get(parts[1], parts[1])
        return f"{kind} · {':'.join(parts[2:])}"
    return scope or "(未知会话)"


def list_scopes(conn: sqlite3.Connection) -> list[dict]:
    """把所有出现过的会话列出来，带事实条数与最后活跃时间。"""
    rows = _rows(
        conn,
        """
        SELECT f.scope AS scope,
               COUNT(*) AS total,
               SUM(CASE WHEN f.superseded_by IS NULL AND f.status = 'active' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN f.status = 'suspect' THEN 1 ELSE 0 END) AS suspect,
               MAX(f.updated_at) AS last_update
        FROM facts f
        GROUP BY f.scope
        ORDER BY last_update DESC
        """,
    )
    seen = {r["scope"]: r for r in rows}
    # 有主动消息状态、但还没有事实的会话也要能选到
    for r in _rows(conn, "SELECT scope FROM proactive"):
        if r["scope"] not in seen:
            seen[r["scope"]] = {"scope": r["scope"], "total": 0, "active": 0,
                                "suspect": 0, "last_update": 0}
    return [
        {
            "scope": r["scope"],
            "label": scope_label(r["scope"]),
            "total": int(r["total"] or 0),
            "active": int(r["active"] or 0),
            "suspect": int(r["suspect"] or 0),
            "last_update": float(r["last_update"] or 0),
        }
        for r in sorted(seen.values(), key=lambda x: float(x["last_update"] or 0), reverse=True)
    ]


def overview(conn: sqlite3.Connection, scope: str) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE scope = ?", (scope,)
    ).fetchone()[0]
    by_status = {s: 0 for s in STATUSES}
    for r in _rows(
        conn, "SELECT status, COUNT(*) AS n FROM facts WHERE scope = ? GROUP BY status", (scope,)
    ):
        by_status[r["status"]] = int(r["n"])
    superseded = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE scope = ? AND superseded_by IS NOT NULL", (scope,)
    ).fetchone()[0]
    by_kind = {k: 0 for k in KINDS}
    for r in _rows(
        conn, "SELECT kind, COUNT(*) AS n FROM facts WHERE scope = ? GROUP BY kind", (scope,)
    ):
        by_kind[r["kind"]] = int(r["n"])

    row = conn.execute(
        """SELECT AVG(confidence) AS conf,
                  SUM(CASE WHEN embedding IS NULL THEN 1 ELSE 0 END) AS no_vec,
                  SUM(CASE WHEN hits = 0 THEN 1 ELSE 0 END) AS never_hit
           FROM facts WHERE scope = ? AND superseded_by IS NULL""",
        (scope,),
    ).fetchone()

    slots = [
        {"predicate": r["predicate"], "n": int(r["n"])}
        for r in _rows(
            conn,
            """SELECT predicate, COUNT(*) AS n FROM facts
               WHERE scope = ? AND superseded_by IS NULL
               GROUP BY predicate ORDER BY n DESC LIMIT 8""",
            (scope,),
        )
    ]
    unresolved = conn.execute(
        """SELECT COUNT(*) FROM facts
           WHERE scope = ? AND status = 'suspect' AND conflict_with IS NOT NULL""",
        (scope,),
    ).fetchone()[0]

    return {
        "scope": scope,
        "label": scope_label(scope),
        "total": int(total),
        "by_status": by_status,
        "by_kind": by_kind,
        "superseded": int(superseded),
        "avg_confidence": round(float(row["conf"] or 0), 3),
        "no_embedding": int(row["no_vec"] or 0),
        "never_hit": int(row["never_hit"] or 0),
        "unresolved_conflicts": int(unresolved),
        "slots": slots,
    }


def list_facts(
    conn: sqlite3.Connection,
    scope: str,
    q: str = "",
    kind: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where = ["scope = ?"]
    args: list = [scope]
    if kind and kind in KINDS:
        where.append("kind = ?")
        args.append(kind)
    if status in STATUSES:
        where.append("status = ?")
        args.append(status)
    if q:
        like = f"%{q}%"
        where.append("(subject LIKE ? OR predicate LIKE ? OR object LIKE ?)")
        args.extend([like, like, like])
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM facts WHERE {clause}", tuple(args)).fetchone()[0]
    limit = max(1, min(int(limit or 50), 500))
    rows = _rows(
        conn,
        f"""SELECT * FROM facts WHERE {clause}
            ORDER BY superseded_by IS NOT NULL, updated_at DESC
            LIMIT ? OFFSET ?""",
        tuple(args) + (limit, max(0, int(offset or 0))),
    )
    return {"total": int(total), "items": [fact_to_dict(r) for r in rows]}


def fact_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": int(r["id"]),
        "subject": r["subject"],
        "predicate": r["predicate"],
        "object": r["object"],
        "kind": r["kind"],
        "importance": int(r["importance"]),
        "confidence": round(float(r["confidence"]), 3),
        "polarity": int(r["polarity"]),
        "status": r["status"],
        "hits": int(r["hits"]),
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
        "last_hit_at": float(r["last_hit_at"]),
        "superseded_by": r["superseded_by"],
        "conflict_with": r["conflict_with"],
        "has_embedding": r["embedding"] is not None,
        "evidence": r["evidence"],
    }


def delete_many(conn: sqlite3.Connection, scope: str, ids: list[int]) -> int:
    """真删。删掉的条数返回给调用方，便于前端提示。"""
    clean = [int(i) for i in ids if str(i).isdigit()]
    if not clean:
        return 0
    marks = ",".join("?" for _ in clean)
    cur = conn.execute(
        f"DELETE FROM facts WHERE scope = ? AND id IN ({marks})", (scope, *clean)
    )
    conn.commit()
    return cur.rowcount


def confirm_many(conn: sqlite3.Connection, scope: str, ids: list[int]) -> int:
    """把待确认事实标成 active，并断开冲突引用。"""
    clean = [int(i) for i in ids if str(i).isdigit()]
    if not clean:
        return 0
    marks = ",".join("?" for _ in clean)
    # 只动 suspect：已是 active 的不该被 WebUI 顺手改写
    cur = conn.execute(
        f"""UPDATE facts SET status = 'active', conflict_with = NULL, updated_at = ?
            WHERE scope = ? AND status = 'suspect' AND id IN ({marks})""",
        (time.time(), scope, *clean),
    )
    conn.commit()
    return cur.rowcount


def persona_block(conn: sqlite3.Connection, scope: str, baseline: dict[str, float]) -> dict:
    row = conn.execute("SELECT * FROM persona WHERE scope = ?", (scope,)).fetchone()
    if row is None:
        traits = dict(baseline)
        version, mood, updated = 0, "", 0.0
    else:
        try:
            traits = {**baseline, **json.loads(row["traits"])}
        except Exception:
            traits = dict(baseline)
        version, mood, updated = int(row["version"]), row["mood"], float(row["updated_at"])
    log = [
        {
            "version": idx + 1,
            "traits": json.loads(r["traits"]),
            "reason": r["reason"],
            "created_at": float(r["created_at"]),
        }
        for idx, r in enumerate(
            _rows(
                conn,
                "SELECT traits, reason, created_at FROM persona_log WHERE scope = ? "
                "ORDER BY id ASC LIMIT 200",
                (scope,),
            )
        )
    ]
    return {
        "scope": scope,
        "baseline": baseline,
        "traits": traits,
        "version": version,
        "mood": mood,
        "updated_at": updated,
        "log": log,
    }


def care_block(
    conn: sqlite3.Connection,
    scope: str,
    min_idle_minutes: float = 90.0,
    checked_at: float = 0.0,
    check_reason: str = "",
    check_text: str = "",
) -> dict:
    """主动消息状态。next_at = 两道闸门里更晚的那个时刻（epoch 秒）。"""
    row = conn.execute("SELECT * FROM proactive WHERE scope = ?", (scope,)).fetchone()
    if row is None:
        base = {
            "scope": scope, "enabled": 1, "last_active": 0.0, "last_sent": 0.0,
            "sent_date": "", "sent_today": 0, "unanswered": 0,
            "next_gap_minutes": 0, "last_text": "",
        }
    else:
        base = {
            "scope": scope,
            "enabled": int(row["enabled"]),
            "last_active": float(row["last_active"]),
            "last_sent": float(row["last_sent"]),
            "sent_date": row["sent_date"],
            "sent_today": int(row["sent_today"]),
            "unanswered": int(row["unanswered"]),
            "next_gap_minutes": int(row["next_gap"]),
            "last_text": row["last_text"],
        }

    now = time.time()
    idle_at = base["last_active"] + min_idle_minutes * 60 if base["last_active"] else 0.0
    gap_at = base["last_sent"] + base["next_gap_minutes"] * 60 if base["last_sent"] else 0.0
    gates = [(t, why) for t, why in ((idle_at, "idle"), (gap_at, "gap")) if t]
    if gates:
        next_at, reason = max(gates)
    else:
        next_at, reason = 0.0, "never_sent"
    base.update(
        {
            "idle_until": idle_at,
            "gap_until": gap_at,
            "next_at": next_at,
            "next_reason": reason,
            "ready": not next_at or next_at <= now,
            "min_idle_minutes": float(min_idle_minutes),
            "checked_at": float(checked_at or 0.0),
            "check_reason": check_reason,
            "check_text": check_text,
        }
    )
    return base


def export_payload(conn: sqlite3.Connection, scope: str) -> dict:
    rows = _rows(conn, "SELECT * FROM facts WHERE scope = ? ORDER BY id ASC", (scope,))
    persona = persona_block(conn, scope, {})
    return {
        "plugin": "astrbot_plugin_memory_evo",
        "scope": scope,
        "exported_at": time.time(),
        "facts": [
            {k: v for k, v in fact_to_dict(r).items() if k != "has_embedding"} for r in rows
        ],
        "persona": {k: persona[k] for k in ("traits", "version", "mood")},
        "care": care_block(conn, scope),
    }
