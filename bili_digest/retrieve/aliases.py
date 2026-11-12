# -*- coding: utf-8 -*-
import json
import os
import re

from bili_digest import paths

MIN_CO = 2
MIN_P = 0.30
MIN_LIFT = 1.5
MAX_ALIAS = 2

LAT = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,11}")
STOP = {"http", "https", "com", "www", "bilibili", "app", "up", "the", "and",
        "for", "api", "pro", "plus", "max", "mini", "beta"}

def cache_path():
    from bili_digest.paths import DATA_DIR
    return DATA_DIR / ".cache" / "aliases.json"

def mine(con, min_co: int = MIN_CO, min_p: float = MIN_P,
         min_lift: float = MIN_LIFT) -> dict:
    items = {}
    for uid, text in con.execute("SELECT item_uid, text FROM content_block"):
        d = items.setdefault(uid, {"zh": set(), "lat": set()})
        for m in LAT.finditer(text or ""):
            d["lat"].add(m.group(0).lower())
    for uid, content in con.execute(
            "SELECT item_uid, content FROM derived WHERE kind='topics'"):
        d = items.setdefault(uid, {"zh": set(), "lat": set()})
        try:
            ts = json.loads(content or "[]")
        except Exception:
            continue
        for t in ts:
            s = str(t).strip()
            if 2 <= len(s) <= 14 and any("\u4e00" <= c <= "\u9fff" for c in s):
                d["zh"].add(s)
    n_items = len(items) or 1
    from collections import Counter
    df_a = Counter(a for d in items.values() for a in d["lat"])
    df_t = Counter(t for d in items.values() for t in d["zh"])
    pair = Counter((t, a) for d in items.values()
                   for t in d["zh"] for a in d["lat"])
    out = {}
    for (t, a), n in pair.items():
        if n < min_co or len(a) < 2 or a in STOP or df_a[a] < 2:
            continue
        p_at = n / df_t[t]
        lift = p_at / max(df_a[a] / n_items, 1e-9)
        if p_at >= min_p and lift >= min_lift:
            out.setdefault(t, []).append((lift, a))
    return {t: [a for _l, a in sorted(v, reverse=True)[:MAX_ALIAS]] for t, v in out.items()}

def load(table=None) -> dict:
    if table is not None:
        return table
    p = cache_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def expand(query: str, limit: int = MAX_ALIAS, table=None) -> list:
    q = (query or "").strip()
    if not q or re.search(r"^[A-Za-z0-9 \-]+$", q):
        return []

    return [a for a in load(table).get(q, []) if a and a != q][:limit]

def build(con=None, verbose: bool = True) -> dict:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    con = con or connect(DB)
    t = mine(con)
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
    if verbose:
        print("  别名表 %d 个词 → %s" % (len(t), p))
    return t

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="语料内别名挖掘")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    if a.build:
        build()
    if a.show or not a.build:
        t = load()
        print("  表内 %d 个词" % len(t))
        for k in list(t)[:20]:
            print("   %-14s → %s" % (k, t[k]))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
