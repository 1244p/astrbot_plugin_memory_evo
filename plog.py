"""插件自己的事件日志：按天分文件、JSONL、带事件名与结构化字段。

为什么要单独一套，而不是只用 AstrBot 的 logger：
`logger.info` 混在框架几万行输出里，出问题时你 grep 到的是一堆无关的 INFO，
真要的证据（哪一轮抽取失败、主动消息为什么跳过）反而被刷掉。
这里只记本插件自己关心的事，每条都有 `evt` 名字段，面板和 `/memory logs` 直接读。
"""

from __future__ import annotations

import io
import json
import os
import threading
import time

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
FILE_PREFIX = "memory_evo-"
FILE_SUFFIX = ".log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def _day(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


def _short(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


class EventLog:
    """线程安全的最小事件日志。写失败一律吞掉——日志坏了不能连累主流程。"""

    def __init__(
        self,
        root: str,
        level: str = "INFO",
        days: int = 7,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = root
        self.level = str(level or "INFO").upper()
        self.days = max(1, int(days or 7))
        self.max_bytes = max(64 * 1024, int(max_bytes or DEFAULT_MAX_BYTES))
        self._lock = threading.Lock()
        self._ensured = False

    # ---------------- 写 ----------------

    def enabled(self, level: str) -> bool:
        return LEVELS.get(str(level).upper(), 20) >= LEVELS.get(self.level, 20)

    def write(self, level: str, evt: str, msg: str = "", **fields) -> dict | None:
        level = str(level or "INFO").upper()
        if not self.enabled(level):
            return None
        ts = time.time()
        rec = {
            "ts": round(ts, 3),
            "time": _short(ts),
            "lvl": level,
            "evt": evt,
            "msg": msg,
            "f": {k: v for k, v in fields.items() if v is not None},
        }
        try:
            with self._lock:
                self._ensure_root()
                path = self._path(ts)
                if os.path.exists(path) and os.path.getsize(path) > self.max_bytes:
                    os.replace(path, path + ".1")
                with io.open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._prune(ts)
        except Exception:  # pragma: no cover - 日志故障不影响主流程
            return None
        return rec

    def _ensure_root(self) -> None:
        if not self._ensured:
            os.makedirs(self.root, exist_ok=True)
            self._ensured = True

    def _path(self, ts: float) -> str:
        return os.path.join(self.root, f"{FILE_PREFIX}{_day(ts)}{FILE_SUFFIX}")

    def _prune(self, ts: float) -> None:
        cutoff = ts - self.days * 86400
        for name in os.listdir(self.root):
            if not (name.startswith(FILE_PREFIX) and FILE_SUFFIX in name):
                continue
            raw = name[len(FILE_PREFIX) : name.index(FILE_SUFFIX)]
            try:
                day = time.mktime(time.strptime(raw, "%Y%m%d"))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(os.path.join(self.root, name))
                except OSError:
                    pass

    # ---------------- 读 ----------------

    def files(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        names = [n for n in os.listdir(self.root) if n.startswith(FILE_PREFIX)]
        return [os.path.join(self.root, n) for n in sorted(names)]

    @staticmethod
    def _parse(line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        return rec if isinstance(rec, dict) else None

    def tail(
        self,
        limit: int = 100,
        level: str = "",
        q: str = "",
        evt: str = "",
    ) -> list[dict]:
        """从最新一天往回读，够 limit 就停。"""
        limit = max(1, min(int(limit or 100), 2000))
        want = LEVELS.get(str(level).upper(), 0) if level else 0
        needle = str(q or "").strip().lower()
        evt = str(evt or "").strip()
        out: list[dict] = []
        for path in reversed(self.files()):
            try:
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                rec = self._parse(line)
                if rec is None:
                    continue
                if want and LEVELS.get(str(rec.get("lvl")), 0) < want:
                    continue
                if evt and not str(rec.get("evt", "")).startswith(evt):
                    continue
                if needle and needle not in json.dumps(rec, ensure_ascii=False).lower():
                    continue
                out.append(rec)
                if len(out) >= limit:
                    return out
        return out

    def stats(self, ts: float | None = None) -> dict:
        """今天的按级别计数 + 文件大小，用于面板顶部那条。"""
        ts = time.time() if ts is None else ts
        counts = {k: 0 for k in LEVELS}
        evts: dict[str, int] = {}
        size = 0
        path = self._path(ts)
        if os.path.exists(path):
            size = os.path.getsize(path)
            try:
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        rec = self._parse(line)
                        if not rec:
                            continue
                        counts[str(rec.get("lvl") or "INFO")] = (
                            counts.get(str(rec.get("lvl") or "INFO"), 0) + 1
                        )
                        key = str(rec.get("evt") or "")
                        evts[key] = evts.get(key, 0) + 1
            except OSError:
                pass
        return {
            "day": _day(ts),
            "counts": counts,
            "events": dict(sorted(evts.items(), key=lambda kv: -kv[1])[:8]),
            "bytes": size,
            "level": self.level,
            "days": self.days,
        }

    def clear(self) -> int:
        removed = 0
        with self._lock:
            for path in self.files():
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed


def text_line(rec: dict, width: int = 0) -> str:
    """给 `/memory logs` 用的一行文本。"""
    fields = " ".join(f"{k}={v}" for k, v in (rec.get("f") or {}).items())
    head = f"{rec.get('time', '')} {str(rec.get('lvl', 'INFO')).ljust(5)} {rec.get('evt', '')}"
    body = rec.get("msg") or ""
    line = f"{head} {body}".rstrip()
    if fields:
        line += f"  [{fields}]"
    if width and len(line) > width:
        line = line[: width - 1] + "…"
    return line
