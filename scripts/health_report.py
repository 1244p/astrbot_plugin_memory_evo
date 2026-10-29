#!/usr/bin/env python3
"""memory_evo 健康检查：直接读 memory.db，给你一份不用猜的报告。

用法：
    python scripts/health_report.py --db <path/to/memory.db>
    python scripts/health_report.py --db ... --scope "aiocqhttp:FriendMessage:12345"
    python scripts/health_report.py --db ... --json

不依赖 AstrBot，只用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter

DEFAULT_HINTS = [
    "data/plugin_data/astrbot_plugin_memory_evo/memory.db",
]


def find_db(explicit: str | None) -> str:
    if explicit:
        return explicit
    for hint in DEFAULT_HINTS:
        if os.path.exists(hint):
            return hint
    raise SystemExit(
        "找不到 memory.db，请用 --db 指定。常见位置：\n"
        "  <AstrBot>/data/plugin_data/astrbot_plugin_memory_evo/memory.db"
    )


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def human_age(ts: float) -> str:
    if not ts:
        return "-"
    delta = time.time() - ts
    if delta < 3600:
        return f"{delta / 60:.0f} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--scope", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db = find_db(args.db)
    if not os.path.exists(db):
        raise SystemExit(f"文件不存在：{db}")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report: dict = {"db": os.path.abspath(db), "size_mb": round(os.path.getsize(db) / 1048576, 2)}

    if not table_exists(conn, "facts"):
        raise SystemExit("这不是 memory_evo 的库：没有 facts 表")

    where, params = "", ()
    if args.scope:
        where, params = " WHERE scope=?", (args.scope,)

    total = conn.execute(f"SELECT COUNT(*) FROM facts{where}", params).fetchone()[0]
    active = conn.execute(
        f"SELECT COUNT(*) FROM facts{where}{' AND' if where else ' WHERE'} superseded_by IS NULL AND status='active'",
        params,
    ).fetchone()[0]
    suspect = conn.execute(
        f"SELECT COUNT(*) FROM facts{where}{' AND' if where else ' WHERE'} superseded_by IS NULL AND status='suspect'",
        params,
    ).fetchone()[0]
    superseded = total - active - suspect

    rows = conn.execute(
        f"SELECT * FROM facts{where}{' AND' if where else ' WHERE'} superseded_by IS NULL",
        params,
    ).fetchall()

    kinds = Counter(r["kind"] for r in rows)
    preds = Counter(r["predicate"] for r in rows)
    with_emb = sum(1 for r in rows if r["embedding"])
    models = Counter(r["embedding_model"] for r in rows if r["embedding_model"])
    never_hit = sum(1 for r in rows if (r["hits"] or 0) == 0)
    avg_conf = sum(r["confidence"] for r in rows) / len(rows) if rows else 0
    conflicts = [(r["id"], r["conflict_with"]) for r in rows if r["conflict_with"]]
    newest = max((r["updated_at"] for r in rows), default=0)
    oldest = min((r["created_at"] for r in rows), default=0)

    report.update(
        {
            "facts_total": total,
            "active": active,
            "suspect": suspect,
            "superseded": superseded,
            "kinds": dict(kinds),
            "embedding_coverage": f"{with_emb}/{len(rows)}",
            "embedding_models": dict(models),
            "never_hit": never_hit,
            "avg_confidence": round(avg_conf, 3),
            "conflicts": conflicts,
            "newest": human_age(newest),
            "oldest": human_age(oldest),
        }
    )

    persona_rows = []
    if table_exists(conn, "persona"):
        persona_rows = conn.execute(
            f"SELECT * FROM persona{where}", params
        ).fetchall()
    if persona_rows:
        p = persona_rows[0]
        report["persona"] = {
            "traits": json.loads(p["traits"]),
            "mood": p["mood"],
            "version": p["version"],
            "updated": human_age(p["updated_at"]),
        }
        log = conn.execute(
            "SELECT traits, reason, created_at FROM persona_log WHERE scope=? ORDER BY id DESC LIMIT 3",
            (p["scope"],),
        ).fetchall()
        report["persona_recent"] = [
            {"reason": r["reason"], "when": human_age(r["created_at"])} for r in log
        ]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"库文件: {report['db']}  ({report['size_mb']} MB)")
    section("规模")
    print(f"事实总数 {total}  |  有效 {active}  |  待确认 {suspect}  |  已被取代 {superseded}")
    if rows:
        print(f"最近更新 {human_age(newest)}  |  最早一条 {human_age(oldest)}")

    section("类型分布")
    for kind, cnt in kinds.most_common():
        print(f"  {kind:<12} {cnt}")

    section("槽位分布(前 8)")
    for pred, cnt in preds.most_common(8):
        print(f"  {pred:<14} {cnt}")

    section("质量指标")
    coverage = with_emb / len(rows) * 100 if rows else 0
    print(f"向量覆盖   {with_emb}/{len(rows)}  ({coverage:.0f}%)  {dict(models) or ''}")
    print(f"平均置信度 {avg_conf:.2f}")
    print(f"从未被召回 {never_hit} 条" + ("  ← 偏多，检查召回阈值或记忆太杂" if rows and never_hit == len(rows) else ""))
    if conflicts:
        print(f"未裁决冲突 {len(conflicts)} 对: {conflicts[:5]}")
    else:
        print("未裁决冲突 0")

    if "persona" in report:
        p = report["persona"]
        traits = "  ".join(f"{k}={v:.2f}" for k, v in p["traits"].items())
        print()
        section("人格")
        print(f"版本 v{p['version']}  更新 {p['updated']}  心情：{p['mood'] or '-'}")
        print(f"  {traits}")
        for item in report.get("persona_recent", []):
            print(f"  · {item['when']}: {item['reason'][:50]}")
    else:
        print()
        print("没有 persona 记录 → 人格演化没跑过（检查 persona_evolve / flush 是否触发）")

    print()
    section("怎么读这份报告")
    print("  有效=0             → 抽取没成功：模型不吐 JSON，或 flush 从未触发")
    print("  待确认占比 >15%    → 抽取在自相矛盾，先改 PREDICATE_ALIASES 或收紧提示词")
    print("  向量覆盖 <100%     → 看看 /memory reindex，或 embedding 被限流")
    print("  从未被召回=全部    → 注入没生效：检查 enable / top_k / min_confidence")
    print("  人格版本=1 且无日志 → 只跑了事实抽取，persona_evolve 关着或模型返回 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
