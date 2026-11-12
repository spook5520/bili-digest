#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from bili_digest.paths import EMBED_DIM, EMBED_MODEL

KEY = "embed_fingerprint"

def fingerprint(model: str | None = None, dim: int | None = None) -> str:
    return "%s|%s" % (model or EMBED_MODEL,
                      dim if dim is not None else EMBED_DIM)

def _ensure(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v TEXT)")

def baseline(con) -> str | None:
    _ensure(con)
    row = con.execute("SELECT v FROM meta_kv WHERE k=?", (KEY,)).fetchone()
    return row[0] if row else None

def mark(con, value: str | None = None) -> str:
    _ensure(con)
    v = value or fingerprint()
    con.execute("INSERT OR REPLACE INTO meta_kv(k,v) VALUES(?,?)", (KEY, v))
    con.commit()
    return v

def check(con) -> dict:
    _ensure(con)
    cur = fingerprint()
    base = baseline(con)
    try:
        n = con.execute("SELECT COUNT(*) FROM vec_state").fetchone()[0]
    except Exception:
        n = 0
    info = {"status": "", "baseline": base, "current": cur, "indexed_blocks": n}
    if base == cur:
        info["status"] = "fresh"
    elif base is None:
        info["status"] = "baseline" if n == 0 else "unknown"
    else:
        info["status"] = "stale"
    return info

def describe(st: dict) -> str:
    cur, base, n = st["current"], st["baseline"], st["indexed_blocks"]
    if st["status"] == "stale":
        return ("embedding 换代：索引基线 %s → 当前 %s。\n"
                "  增量索引会把两个向量空间的坐标混进同一份文件，检索会静默给错结果。\n"
                "  请跑 `kb index --rebuild`（全量重编码，约 5~7 分钟）；\n"
                "  若确认磁盘索引其实已是当前模型，用 `kb index --adopt-baseline` 补记基线。"
                % (base, cur))
    if st["status"] == "unknown":
        return ("索引里已有 %d 条向量，但没记过 embedding 基线。\n"
                "  不猜它的模型：请跑 `kb index --rebuild`，\n"
                "  或确认无误后用 `kb index --adopt-baseline` 补记 %s。"
                % (n, cur))
    return ""

def guard(con, *, rebuild: bool = False) -> dict:
    st = check(con)
    if st["status"] == "baseline":
        mark(con)
        st["status"] = "fresh"
    if not rebuild and st["status"] in ("unknown", "stale"):
        raise SystemExit(describe(st))
    return st
