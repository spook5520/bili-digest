#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

def load_graph(con, kinds=None) -> dict:
    if kinds:
        q = ",".join("?" * len(kinds))
        rows = con.execute(
            f"SELECT src,dst,kind,weight FROM edge WHERE kind IN ({q})", kinds)
    else:
        rows = con.execute("SELECT src,dst,kind,weight FROM edge")
    adj = defaultdict(lambda: defaultdict(float))
    for r in rows:
        a, b, w = r["src"], r["dst"], r["weight"] or 1.0
        if a == b:
            continue
        adj[a][b] += w

    for a in list(adj):
        for b, w in list(adj[a].items()):
            if a not in adj.get(b, {}):
                adj[b][a] = w
    return adj

def _local_move(adj, deg, m2, nodes, min_gain):
    comm = {n: i for i, n in enumerate(nodes)}
    for _sweep in range(20):
        moved = 0
        for n in nodes:
            cur = comm[n]
            nb_comm_w = defaultdict(float)
            for nb, w in adj[n].items():
                nb_comm_w[comm[nb]] += w
            deg_n = deg[n]
            tot = defaultdict(float)
            for x in nodes:
                tot[comm[x]] += deg[x]
            best, best_gain = cur, 0.0
            for c, w in nb_comm_w.items():
                if c == cur:
                    continue
                gain = w - nb_comm_w.get(cur, 0.0) - deg_n * (tot[c] - tot[cur]) / m2
                if gain > best_gain + min_gain:
                    best, best_gain = c, gain
            if best != cur:
                comm[n] = best
                moved += 1
        if moved == 0:
            break

    relabel, out = {}, {}
    for n in nodes:
        c = comm[n]
        if c not in relabel:
            relabel[c] = len(relabel)
        out[n] = relabel[c]
    return out

def _aggregate(adj, deg, comm):
    new_adj = defaultdict(lambda: defaultdict(float))
    for a in adj:
        ca = comm[a]
        for b, w in adj[a].items():
            cb = comm[b]
            if ca == cb:
                new_adj[ca][ca] += w
            else:
                new_adj[ca][cb] += w
    new_deg = defaultdict(float)
    for n, c in comm.items():
        new_deg[c] += deg[n]
    return ({n: dict(v) for n, v in new_adj.items()}, dict(new_deg))

def load_keyword_graph(con, max_df_ratio: float = 0.04, max_group: int = 60) -> dict:
    n_items = con.execute(
        "SELECT COUNT(DISTINCT item_uid) FROM content_block").fetchone()[0]
    cutoff = max(2, int(n_items * max_df_ratio))
    rows = con.execute(
        "SELECT kb.term, cb.item_uid FROM keyword_block kb "
        "JOIN content_block cb ON cb.id = kb.block_id "
        "JOIN keyword k ON k.term = kb.term WHERE k.df <= ?", (cutoff,)).fetchall()
    by_term = defaultdict(set)
    for r in rows:
        by_term[r["term"]].add(r["item_uid"])
    adj = defaultdict(lambda: defaultdict(float))
    for term, members in by_term.items():
        us = sorted(members)
        if not (2 <= len(us) <= max_group):
            continue
        for i in range(len(us)):
            for j in range(i + 1, len(us)):
                adj[us[i]][us[j]] += 1.0
                adj[us[j]][us[i]] += 1.0
    return adj

def louvain(adj: dict, max_pass: int = 10, min_gain: float = 1e-7) -> dict:
    nodes = sorted(adj.keys())
    if not nodes:
        return {}

    cur_adj = {n: dict(v) for n, v in adj.items()}
    cur_deg = {n: sum(cur_adj[n].values()) for n in cur_adj}
    m2 = sum(cur_deg.values())
    mapping = {n: n for n in nodes}

    for _pass in range(max_pass):
        cur_nodes = sorted(cur_adj)
        if len(cur_nodes) <= 1:
            break
        comm = _local_move(cur_adj, cur_deg, m2, cur_nodes, min_gain)
        k = len(set(comm.values()))
        if k == len(cur_nodes):
            break

        mapping = {n: comm[mapping[n]] for n in mapping}
        cur_adj, cur_deg = _aggregate(cur_adj, cur_deg, comm)

    relabel, out = {}, {}
    for n in nodes:
        c = mapping[n]
        if c not in relabel:
            relabel[c] = len(relabel)
        out[n] = relabel[c]
    return out

def stats(con, comm: dict, adj: dict, top: int = 15) -> dict:
    by_c = defaultdict(list)
    for n, c in comm.items():
        by_c[c].append(n)

    topic_of = {}
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='topics'"):
        try:
            topic_of[r["item_uid"]] = set(json.loads(r["content"]))
        except Exception:
            topic_of[r["item_uid"]] = set()

    title_of = {r["uid"]: r["title"]
                for r in con.execute("SELECT uid, title FROM item")}

    sizes = sorted(((len(v), c) for c, v in by_c.items()), reverse=True)
    print(f"  社区数 {len(by_c)} · 最大 {sizes[0][0] if sizes else 0} "
          f"· 节点 {len(comm)}")

    out = []
    for size, c in sizes[:top]:
        members = by_c[c]
        if size < 3:
            continue

        cnt = defaultdict(int)
        for m in members:
            for t in topic_of.get(m, ()):
                cnt[t] += 1
        rep = sorted(((n, t) for t, n in cnt.items() if n >= 2), reverse=True)[:5]
        print(f"\n    ▸ 社区 {c}（{size} 条）  代表词: "
              f"{[f'{t}({n})' for n, t in rep] or '（无共同主题词）'}")
        for m in members[:6]:
            print(f"        {str(title_of.get(m))[:52]}")
        if size > 6:
            print(f"        …另有 {size-6} 条")
        out.append({"id": c, "size": size, "members": members,
                    "rep": [t for _n, t in rep]})
    return {"n_comm": len(by_c), "communities": out}

def ensure_table(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS community (
        item_uid  TEXT NOT NULL,
        comm_id   INTEGER NOT NULL,
        size      INTEGER,
        rep_terms TEXT,              -- JSON：代表词
        PRIMARY KEY (item_uid)
    );
    CREATE INDEX IF NOT EXISTS idx_comm ON community(comm_id);
    """)

def save(con, comm: dict, res: dict):
    ensure_table(con)
    con.execute("DELETE FROM community")
    rep_of = {c["id"]: c["rep"] for c in res["communities"]}
    size_of = {c["id"]: c["size"] for c in res["communities"]}
    batch = [(n, c, size_of.get(c, 1), json.dumps(rep_of.get(c, []), ensure_ascii=False))
             for n, c in comm.items()]
    con.executemany(
        "INSERT OR REPLACE INTO community(item_uid,comm_id,size,rep_terms) "
        "VALUES(?,?,?,?)", batch)
    con.commit()
    return len(batch)

def export_moc(con, res: dict, vault: str, subdir: str = "B站",
               only_existing: bool = True):
    import obsidian_out as oo
    vout = Path(vault) / subdir
    vout.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in vout.glob("*.md")} if only_existing else None
    n = 0
    for c in res["communities"]:
        if c["size"] < 3:
            continue
        name = c["rep"][0] if c["rep"] else f"社区{c['id']}"
        lines = ["---",
                 "type: moc",
                 f"community: {c['id']}",
                 f"size: {c['size']}",
                 f"rep_terms: {json.dumps(c['rep'], ensure_ascii=False)}",
                 "---", "",
                 f"# {name}", "",
                 f"> 自动生成的主题地图 · {c['size']} 个条目"
                 f"（代表词：{'、'.join(c['rep']) or '无'}）", ""]
        linked = missing = 0
        for uid in c["members"]:
            r = con.execute("SELECT title, item_id FROM item WHERE uid=?",
                            (uid,)).fetchone()
            if not r:
                continue
            fn = Path(oo.note_filename({"title": r["title"],
                                        "item_id": r["item_id"]})).stem
            if existing is not None and fn not in existing:
                missing += 1
                continue
            lines.append(f"- [[{fn}]]")
            linked += 1
        if missing:
            lines += ["", f"> 另有 {missing} 条未导出（在 cache 档，"
                          f"跑 `kb.py note --all` 时不含）"]
        safe = "".join(ch for ch in name if ch not in r'\/:*?"<>|').strip()[:60]
        p = vout / f"_MOC {safe}.md"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        n += 1
    return n

def main():
    ap = argparse.ArgumentParser(description="图谱社区发现与 MOC 导出")
    ap.add_argument("--run", action="store_true", help="跑 Louvain 并落库")
    ap.add_argument("--moc", metavar="VAULT", help="导出 MOC 到 vault")
    ap.add_argument("--kinds", help="只用这些边类型（逗号分隔）")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    kinds = a.kinds.split(",") if a.kinds else None
    adj = load_graph(con, kinds)
    print(f"  图：{len(adj)} 节点 · "
          f"{sum(len(v) for v in adj.values())//2} 边")

    comm = louvain(adj)
    res = stats(con, comm, adj, top=a.top)

    if a.run:
        n = save(con, comm, res)
        print(f"\n  已落库 {n} 条（community 表）")

    if a.moc:
        n = export_moc(con, res, a.moc)
        print(f"\n  导出 MOC {n} 篇 → {a.moc}")
    con.close()

if __name__ == "__main__":
    main()
