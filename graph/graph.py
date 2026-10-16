#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import connect, now_sec
from paths import DB

DDL = """
CREATE TABLE IF NOT EXISTS edge (
    src        TEXT NOT NULL,      -- item_uid
    dst        TEXT NOT NULL,      -- item_uid
    kind       TEXT NOT NULL,      -- same_author / shared_topic / wikilink / semantic
    weight     REAL DEFAULT 1.0,
    note       TEXT,               -- 附加说明（如共同主题词）
    updated_at INTEGER,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src, kind);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst, kind);
"""

def ensure_table(con):
    con.executescript(DDL)
    con.commit()

def build_shared(con, kind: str, groups: dict, weight_fn=None, priority_fn=None,
                 max_pairs_per_group: int = 400, max_total: int = 40000) -> int:
    import math
    if max_pairs_per_group < 1:
        raise ValueError("max_pairs_per_group 必须 >= 1")

    con.execute("DELETE FROM edge WHERE kind=?", (kind,))
    ts = now_sec()

    m_cap = int((1 + math.sqrt(1 + 8 * max_pairs_per_group)) / 2)

    batch = []
    for gkey, members in groups.items():
        uids = sorted(members)
        if len(uids) < 2:
            continue
        if len(uids) > m_cap:

            if priority_fn:
                uids = sorted(uids, key=lambda u: -priority_fn(u, gkey))[:m_cap]
            else:
                uids = uids[:m_cap]
        cnt = 0
        for i in range(len(uids)):
            for j in range(i + 1, len(uids)):
                if cnt >= max_pairs_per_group:
                    break
                a, b = uids[i], uids[j]
                w = weight_fn(gkey, a, b) if weight_fn else 1.0
                note = str(gkey)
                batch.append((a, b, kind, w, note, ts))
                batch.append((b, a, kind, w, note, ts))
                cnt += 1
                if len(batch) >= max_total * 2:
                    break
            if cnt >= max_pairs_per_group:
                break

    if not batch:
        con.commit()
        return 0
    batch = batch[: max_total * 2]
    con.executemany(
        "INSERT OR REPLACE INTO edge(src,dst,kind,weight,note,updated_at) "
        "VALUES(?,?,?,?,?,?)", batch)
    con.commit()
    return len(batch)

def _groups_same_author(con) -> dict:
    groups = {}
    for r in con.execute(
            "SELECT author_uid, uid FROM item "
            "WHERE author_uid IS NOT NULL AND author_uid<>''"):
        groups.setdefault(r["author_uid"], set()).add(r["uid"])
    return {k: v for k, v in groups.items() if len(v) >= 2}

def _groups_shared_topic(con) -> dict:
    groups = {}
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='topics'"):
        try:
            ts = json.loads(r["content"])
        except Exception:
            continue
        for t in ts:
            if isinstance(t, str) and t.strip():
                groups.setdefault(t.strip(), set()).add(r["item_uid"])
    return groups

def _groups_shared_tag(con) -> dict:
    groups = {}
    for r in con.execute("SELECT name, item_uid FROM tag "
                         "WHERE kind IN ('system','custom')"):
        groups.setdefault(r["name"], set()).add(r["item_uid"])
    return groups

def _weight_jaccard(topic, a, b):

    return 1.0

def _item_order(con) -> dict:
    return {r["uid"]: (r["saved_at"] or 0)
            for r in con.execute("SELECT uid, saved_at FROM item")}

def build_same_batch(con, window_min: int = 30, max_per_batch: int = 40) -> int:
    rows = con.execute(
        "SELECT uid, saved_at FROM item WHERE saved_at IS NOT NULL "
        "ORDER BY saved_at").fetchall()
    if not rows:
        return 0
    win = max(window_min, 1) * 60

    groups = {}
    batch_no, batch = 0, [rows[0]]
    for i in range(len(rows) - 1):
        if rows[i + 1]["saved_at"] - rows[i]["saved_at"] > win:
            if len(batch) > 1:
                groups[batch_no] = {r["uid"]: r["saved_at"] for r in batch}
            batch_no += 1
            batch = []
        batch.append(rows[i + 1])
    if len(batch) > 1:
        groups[batch_no] = {r["uid"]: r["saved_at"] for r in batch}

    plain = {k: set(v) for k, v in groups.items()}

    when = {}
    for v in groups.values():
        when.update(v)

    def weight_fn(_gkey, a, b):
        ta, tb = when.get(a, 0), when.get(b, 0)
        d = abs(ta - tb) / 60.0
        return round(1.0 / (1.0 + d / max(window_min, 1)), 4)

    return build_shared(con, "same_batch", plain, weight_fn=weight_fn,
                        max_pairs_per_group=max_per_batch)

def build_same_author(con, max_per_group: int = 30) -> int:
    order = _item_order(con)
    return build_shared(con, "same_author", _groups_same_author(con),
                        priority_fn=lambda u, _g: order.get(u, 0),
                        max_pairs_per_group=max_per_group * (max_per_group - 1) // 2,
                        max_total=40000)

def build_shared_topic(con, max_pairs_per_topic: int = 400,
                       max_total_edges: int = 40000) -> int:
    groups = _groups_shared_topic(con)
    topic_sets = {r["item_uid"]: set(json.loads(r["content"]))
                  for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='topics'")}

    def w(_gkey, a, b):
        sa, sb = topic_sets.get(a) or set(), topic_sets.get(b) or set()
        if not sa or not sb:
            return 1.0
        common = len(sa & sb)
        denom = math.sqrt(len(sa) * len(sb))
        return common / denom if denom else 1.0

    def prio(uid, _gkey):
        return len(topic_sets.get(uid) or ())

    return build_shared(con, "shared_topic", groups, weight_fn=w, priority_fn=prio,
                        max_pairs_per_group=max_pairs_per_topic,
                        max_total=max_total_edges)

def build_shared_tag(con, max_pairs_per_tag: int = 200,
                     max_total_edges: int = 40000) -> int:
    order = _item_order(con)
    return build_shared(con, "shared_tag", _groups_shared_tag(con),
                        priority_fn=lambda u, _g: order.get(u, 0),
                        max_pairs_per_group=max_pairs_per_tag,
                        max_total=max_total_edges)

def build_wikilink(con, vault: str, subdir: str = "B站") -> int:
    con.execute("DELETE FROM edge WHERE kind='wikilink'")
    root = Path(vault) / subdir
    if not root.exists():
        return 0

    name2uid = {}
    for r in con.execute("SELECT uid, item_id, title FROM item"):
        name2uid[f"{r['title']} [{r['item_id']}]"] = r["uid"]
        name2uid[r["item_id"]] = r["uid"]

    bv_re = re.compile(r"(BV[0-9A-Za-z]{10})")
    link_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
    ts = now_sec()
    n = 0
    for md in root.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        src_uid = None
        m = re.search(r'^uid:\s*"?([^"\n]+)"?', text, re.M)
        if m:
            src_uid = m.group(1).strip()
        if not src_uid:
            continue
        for lm in link_re.finditer(text):
            target = lm.group(1).strip()
            dst_uid = name2uid.get(target)
            if not dst_uid:
                b = bv_re.search(target)
                if b:
                    dst_uid = name2uid.get(b.group(1))
            if dst_uid and dst_uid != src_uid:
                con.execute(
                    "INSERT OR REPLACE INTO edge(src,dst,kind,weight,note,updated_at) "
                    "VALUES(?,?,?,?,?,?)", (src_uid, dst_uid, "wikilink", 1.0, None, ts))
                n += 1
    con.commit()
    return n

def build_all(vault: str = None, verbose: bool = True) -> dict:
    con = connect(DB)
    con.row_factory = sqlite3.Row
    ensure_table(con)
    out = {}
    t0 = time.time()
    out["same_author"] = build_same_author(con)
    if verbose:
        print(f"  same_author   {out['same_author']} 条")
    out["shared_topic"] = build_shared_topic(con)
    if verbose:
        print(f"  shared_topic  {out['shared_topic']} 条")
    if vault:
        out["wikilink"] = build_wikilink(con, vault)
        if verbose:
            print(f"  wikilink      {out['wikilink']} 条")
    out["total"] = sum(v for k, v in out.items() if k != "total")
    out["seconds"] = round(time.time() - t0, 2)
    con.close()
    return out

def neighbors(con, uid: str, kinds: list[str] = None, limit: int = 20,
              require_content: bool = True) -> list[dict]:
    sql = ("SELECT e.dst,e.kind,e.weight,e.note,i.title,l.tier "
           "FROM edge e LEFT JOIN item i ON i.uid=e.dst "
           "LEFT JOIN lifecycle l ON l.item_uid=e.dst WHERE e.src=?")
    args = [uid]
    if kinds:
        sql += f" AND e.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if require_content:
        sql += (" AND EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=e.dst)")
    sql += " ORDER BY e.weight DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args)]

def expand(con, uid: str, hops: int = 2, limit_per_hop: int = 10,
           kinds: list[str] = None, decay: float = 0.6) -> dict:
    seen = {uid}
    frontier = [(uid, 1.0)]
    levels = []
    for hop in range(1, hops + 1):
        nxt, scored = [], {}
        for node, score in frontier:
            for nb in neighbors(con, node, kinds, limit_per_hop):
                if nb["dst"] in seen:
                    continue
                s = score * nb["weight"] * (decay ** (hop - 1))
                if s > scored.get(nb["dst"], 0):
                    scored[nb["dst"]] = s
                    nxt.append(nb["dst"])
        ordered = sorted(scored.items(), key=lambda x: -x[1])[:limit_per_hop * 2]
        lvl = []
        for d, s in ordered:
            if d in seen:
                continue
            seen.add(d)
            r = con.execute(
                "SELECT i.uid,i.title,i.author_name,l.tier FROM item i "
                "LEFT JOIN lifecycle l ON l.item_uid=i.uid WHERE i.uid=?", (d,)).fetchone()
            if r:
                lvl.append({"item_uid": r["uid"], "title": r["title"],
                            "author": r["author_name"], "tier": r["tier"],
                            "score": round(s, 4)})
        levels.append({"hop": hop, "items": lvl})
        frontier = [(d, s) for d, s in ordered if d in seen][:limit_per_hop]
        if not frontier:
            break
    return {"start": uid, "hops": hops, "levels": levels,
            "discovered": len(seen) - 1}

def stats(con) -> dict:
    ensure_table(con)
    out = {"by_kind": {}, "nodes": 0, "edges": 0, "isolated": 0}
    for r in con.execute("SELECT kind, COUNT(*) FROM edge GROUP BY kind ORDER BY -COUNT(*)"):
        out["by_kind"][r[0]] = r[1]
    out["edges"] = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    out["nodes"] = con.execute("SELECT COUNT(DISTINCT src) FROM edge").fetchone()[0]
    total_items = con.execute("SELECT COUNT(*) FROM item").fetchone()[0]
    out["isolated"] = total_items - out["nodes"] if out["nodes"] else total_items
    return out

def main():
    import argparse
    ap = argparse.ArgumentParser(description="知识图谱边（图扩展）")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--vault", help="Obsidian vault 路径（用于读双链）")
    ap.add_argument("--subdir", default="B站")
    ap.add_argument("--expand", metavar="UID")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    con = connect(DB)
    con.row_factory = sqlite3.Row

    if a.build:
        vault = a.vault
        if vault is None:

            from paths import find_vault
            vault = find_vault()
        if vault is None:
            print("  找不到 Obsidian vault。用 KB_VAULT 环境变量或 --vault 指定；"
                  "不指定则只建「同UP/共享主题」这两类边。")
        print("  建边中…")
        print("  " + json.dumps(build_all(vault, a.subdir if vault else None),
                                 ensure_ascii=False))
        print("  " + json.dumps(stats(con), ensure_ascii=False, indent=1))
    elif a.expand:
        r = expand(con, a.expand, hops=a.hops)
        print(f"  从 {a.expand} 扩展 {a.hops} 跳，发现 {r['discovered']} 个条目")
        for lv in r["levels"]:
            print(f"\n  ── 第 {lv['hop']} 跳 ──")
            for it in lv["items"][:8]:
                print(f"    {it['score']:.3f}  [{it['tier'] or '?'}] {str(it['title'])[:46]}")
    elif a.stats:
        print(json.dumps(stats(con), ensure_ascii=False, indent=1))
    else:
        ap.print_help()
    con.close()

if __name__ == "__main__":
    main()
