#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.paths import DB

def ingest(bv_list: list, tag: str, tag_kind: str, cooldown: int,
           verbose: bool = True) -> dict:
    from bili_digest.schema import core_schema as cs
    from bili_digest.ingest import refine
    from bili_digest.adapters.adapters import BilibiliAdapter
    from bili_digest.schema.core_schema import connect

    adapter = BilibiliAdapter()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    ok = fail = skipped = 0
    outs = []

    for n, bv in enumerate(bv_list, 1):
        print(f"\n  [{n}/{len(bv_list)}] {bv}")

        ex = con.execute("SELECT uid FROM item WHERE item_id=?", (bv,)).fetchone()
        if ex:
            uid = ex["uid"]
            print(f"      已存在 {uid}")
            skipped += 1
        else:
            try:
                meta = adapter.fetch_item(bv)
            except Exception as e:
                print(f"      取元数据失败 {type(e).__name__}: {str(e)[:50]}")
                fail += 1
                continue

            uid = cs.upsert_item(
                con, platform=meta["platform"], item_id=meta["item_id"],
                title=meta.get("title"), url=meta.get("url"),
                author_uid=meta.get("author_uid"),
                author_name=meta.get("author_name"),
                published_at=meta.get("published_at"),
                saved_at=meta.get("saved_at"),
                duration_sec=meta.get("duration_sec"),
                language=meta.get("language"), cover=meta.get("cover"),
                extras={**(meta.get("extras") or {}),
                        "source": "chain", "chain_seed": "BVTestExample"})
            con.commit()
            print(f"      建条目 {uid}")

        try:
            good, msg = refine.step_fetch(bv, sources="meta,sub,dm,reply")
            print(f"      抓取: {'ok' if good else '失败'} ({msg})")
        except Exception as e:
            print(f"      抓取异常 {type(e).__name__}: {str(e)[:50]}")

        try:
            con.execute(
                "INSERT OR REPLACE INTO tag(item_uid,name,kind,source) "
                "VALUES(?,?,?,?)", (uid, tag, tag_kind, "chain"))
            con.commit()
            print(f"      tag: {tag}")
        except Exception as e:
            print(f"      打 tag 失败 {type(e).__name__}")

        outs.append(uid)
        ok += 1
        if n < len(bv_list):
            time.sleep(cooldown)
    con.close()
    return {"ok": ok, "fail": fail, "skipped": skipped, "uids": outs}

def build_shared_tag(con) -> int:
    from bili_digest.graph import graph as gp
    from bili_digest.schema import core_schema
    ts = core_schema.now_sec()
    con.execute("DELETE FROM edge WHERE kind='shared_tag'")
    pairs = {}
    rows = con.execute("SELECT name, item_uid FROM tag "
                       "WHERE kind IN ('system','custom')").fetchall()
    by_tag = {}
    for r in rows:
        by_tag.setdefault(r["name"], set()).add(r["item_uid"])
    MAX_PAIRS = 200
    for name, uids in by_tag.items():
        uids = sorted(uids)
        if len(uids) < 2:
            continue
        cnt = 0
        for i in range(len(uids)):
            for j in range(i + 1, len(uids)):
                if cnt >= MAX_PAIRS:
                    break
                a, b = uids[i], uids[j]
                pairs[(a, b)] = (name, 1.0)
                pairs[(b, a)] = (name, 1.0)
                cnt += 1
    for (a, b), (note, w) in pairs.items():
        con.execute("INSERT OR REPLACE INTO edge"
                    "(src,dst,kind,weight,note,updated_at) "
                    "VALUES(?,?,?,?,?,?)", (a, b, "shared_tag", w, note, ts))
    con.commit()
    return len(pairs)

def main() -> int:
    ap = argparse.ArgumentParser(description="把 chain 发现的条目入库并建关系")
    ap.add_argument("--list", required=True, help="BV 号清单（一行一个）")
    ap.add_argument("--tag", default="any_austin", help="打的标签名")
    ap.add_argument("--tag-kind", default="system")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    ap.add_argument("--cooldown", type=int, default=12, help="每条间隔秒数")
    ap.add_argument("--edges-only", action="store_true")
    a = ap.parse_args()

    bvs = [x.strip() for x in Path(a.list).read_text(encoding="utf-8").splitlines()
           if x.strip() and not x.startswith("#")]
    if a.limit:
        bvs = bvs[:a.limit]
    print(f"═══ 入库 {len(bvs)} 条（tag={a.tag}）═══")

    if not a.edges_only:
        r = ingest(bvs, a.tag, a.tag_kind, a.cooldown)
        print(f"\n  入库完成: 成功 {r['ok']} · 失败 {r['fail']}")

    print(f"\n═══ 建「共有 tag」边 ═══")
    from bili_digest.schema.core_schema import connect
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    n = build_shared_tag(con)
    print(f"  shared_tag  {n} 条")
    from bili_digest.graph import graph as gp

    n2 = gp.build_same_author(con)
    print(f"  same_author {n2} 条")

    n3 = gp.build_same_batch(con)
    print(f"  same_batch  {n3} 条")
    con.close()

if __name__ == "__main__":
    main()
