#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import sqlite3
import sys
import time
from pathlib import Path
from paths import DB, VEC, EMBED_MODEL as MODEL, EMBED_DIM as DIM

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import connect, ensure_fts, has_jieba, index_block

def index_item(con, uid: str, verbose: bool = False) -> dict:
    rows = con.execute("SELECT id, text, item_uid, kind FROM content_block "
                       "WHERE item_uid=?", (uid,)).fetchall()

    try:
        ids = [r["id"] for r in rows]
        if ids:
            ph = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM block_fts WHERE rowid IN ({ph})", ids)
            con.commit()
    except Exception:
        pass
    n_fts = 0
    for r in rows:
        try:
            index_block(con, r["id"], r["text"], r["item_uid"], r["kind"])
            n_fts += 1
        except Exception:
            pass
    con.commit()
    n_vec = -1

    if verbose:
        print(f"    已索引 {n_fts} 块（FTS）· 向量未动（跑 kb.py index 增量补）")
    return {"fts": n_fts, "vec": n_vec}

def build_fts(con, verbose=True, incremental=False,
              item_uids=None):
    mode = ensure_fts(con, force=not incremental)
    if incremental:
        where, args = [], []
        if item_uids:
            ph = ",".join("?" * len(item_uids))
            where.append(f"item_uid IN ({ph})")
            args.extend(item_uids)

        sql = ("SELECT id, text, item_uid, kind FROM content_block cb "
               "WHERE NOT EXISTS(SELECT 1 FROM block_fts f WHERE f.rowid = cb.id)")
        if where:
            sql += " AND " + " AND ".join(where)
        rows = con.execute(sql, args).fetchall()
    else:
        rows = con.execute("SELECT id, text, item_uid, kind FROM content_block").fetchall()
    t0 = time.time()
    for r in rows:
        index_block(con, r["id"], r["text"], r["item_uid"], r["kind"])
    con.commit()
    if verbose:
        tag = "增量" if incremental else "重建"
        print(f"  FTS {tag} {len(rows)} 条  方案={mode}  {time.time()-t0:.1f}s")
    return mode, len(rows)

BUILD_VEC_NOTES = '''建/更新向量索引 —— 薄封装，实际实现见 rag_core.index_chunks

    为什么改成委托：这个函数与 rag_core.index_chunks 是同一件事的两处实现，
    而且「已索引」判定来源不同（本函数用 key 镜像、index_chunks 用 vec_state）。
    两条调用路径混跑时（refine.step_index 走这里、kb.py index 走那边）
    会出现重复编码或漏编码。现在统一到 index_chunks。

    rebuild=False（默认）：增量，只编码不在 key 镜像里的块。
      为什么默认增量：全量编码耗时与总块数成正比（实测 60281 块 647 秒），
      而日常新增往往只有几百到几千块。曾因为默认值写在函数签名上
      （rebuild=True）而误以为「增量逻辑不生效」—— 代码在，但从没执行。

    ★ 孤儿向量【不是「无害」】—— 这里原本写的是「检索时按 block_id 回查库、
      查不到就跳过，所以无害，只占些空间」。功能上确实不会返回错误结果，
      但实测它对【召回】有实质影响：

          索引 66409 条（孤儿 6128，占 9.2%）→ 清空重建为 60281 条
            Recall@1   87.5% → 90.0%   (+2.5pt)
            Recall@10  87.7% → 90.8%   (+3.0pt)
            Recall@50  90.7% → 97.1%   (+6.5pt)

      原因：usearch 是图索引，孤儿向量仍参与建图 ——
      它们占据了邻居位、把搜索引向不存在的 key，等于在图上多了死路。
      所以孤儿该定期回收：`kb.py index --rebuild`（清空重建）。

      实测耗时（2026-10 复测）：75104 块用 1190 秒（约 20 分钟）。
      而本项目另一处记的是「6 万块约 11 分钟」—— 块数只多 25%、时间却多了 80%，
      所以别按线性外推。重建期间索引文件是先删后写，中途 `vectors.usearch`
      不存在属正常现象，别误判为损坏。
      对照：同一批数据走【增量】索引只要 4.5 秒（补 85 块），
      所以日常该用增量，只在回收孤儿或索引损坏时才付出这 20 分钟。
      check_index --fix 【不做这件事】（它只重建 FTS 与对齐 vec_state），
      所以必须有一个显式入口 —— 这也是加 --rebuild 的原因。'''

def build_vec(con, verbose=True, batch=64, dtype="f32", rebuild=False):
    import rag_core as rc
    vi = rc.VectorIndex(VEC, dtype=dtype, rebuild=rebuild)
    r = rc.index_chunks(con, vi, batch=batch, verbose=verbose, rebuild=rebuild)
    dt = r.get("seconds", 0.0)
    if verbose:

        if r["new"] != r["total"]:
            print(f"  向量索引 本次编码 {r['new']} 条 / 索引共 {r['total']} 条  "
                  f"{dt:.1f}s ({r['new']/max(dt,0.01):.0f} 条/秒)  → {VEC.name}")
        else:
            print(f"  向量索引 {r['new']} 条  {dt:.1f}s "
                  f"({r['new']/max(dt,0.01):.0f} 条/秒)  → {VEC.name}")
    return {"indexed": r["new"], "seconds": dt, "size": r["total"]}

def hybrid(con, q, topk=8, cand=None):
    import rag_core as rc
    vi = rc.VectorIndex(str(VEC))
    return rc.hybrid(con, vi, q, top_k=topk,
                     candidate_k=cand or rc.CANDIDATE_K)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dtype", default="f32", choices=["f32", "f16", "i8"])
    ap.add_argument("--bench", metavar="QUERY", help="检索验证")
    a = ap.parse_args()

    con = connect(DB)
    con.row_factory = sqlite3.Row

    if a.bench:
        print(f"=== 「{a.bench}」===")
        for i, h in enumerate(hybrid(con, a.bench, 6), 1):
            loc = {"meta": "标题", "danmaku": "弹幕", "comment": "评论",
                   "subtitle": "字幕"}.get(h["kind"], h["kind"])
            print(f"  [{i}] {h['score']:.4f} [{loc}] {h['text'][:62]}")
        con.close()
        return

    print("=== 为 knowledge.db 建索引 ===")
    print(f"  jieba {'可用' if has_jieba() else '缺失'}")
    build_fts(con)
    build_vec(con, dtype=a.dtype)
    con.close()

if __name__ == "__main__":
    main()
