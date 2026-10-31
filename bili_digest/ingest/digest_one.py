#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

DEFAULT_SOURCES = "meta,sub,dm,reply"

def digest_one(bv: str, sources: str = DEFAULT_SOURCES,
               force: bool = True, do_fetch: bool = True) -> dict:
    from bili_digest.ingest import refine
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    con = connect(DB)
    t0 = time.time()
    try:

        print(f"[1/4] 抓取 {bv}（源 {sources}）…")
        ok, msg = refine.step_fetch(bv, sources=sources, force=False)
        print(f"      {msg}")
        if not ok:
            raise RuntimeError(f"抓取失败: {msg}")

        print("[2/4] 入库 …")
        r2 = refine.step_import(con, bv)
        if r2.get("error") or r2.get("skipped"):
            raise RuntimeError(f"入库失败: {r2}")
        blocks = {k: v for k, v in (r2.get("blocks") or {}).items() if v}
        print(f"      {blocks}")
        uid = r2["uid"]

        from bili_digest.schema.core_schema import mark_manual
        marked = mark_manual(con, uid, note="用户指定")
        if marked:
            print("[3/4] 已标记为用户手动关注（参与评分，marked 维度 +0.20）")

        from bili_digest.distill import evidence
        ev = evidence.gather(con, uid)
        v = refine.step_judge(con, uid, verbose=False)
        con.commit()
        print("[3/4] 判定")
        print(f"      标题 {ev['title']}")
        print(f"      类型 {'+'.join(v.content_kinds)} · {v}")
        print(f"      素材 字幕 {ev['n_subtitle']}行/{ev['sub_chars']}字 · "
              f"弹幕 {ev['n_danmaku']} · 评论 {ev['n_comment']}")
        print(f"      依据 {v.evidence_used}")

        if v.extractable is False:
            print("      素材不足，不提炼（但内容已入库，可检索）")
            return {"uid": uid, "skipped": True, "verdict": v, "evidence": ev}

        print("[4/4] 提炼 …")
        from bili_digest.distill import extract
        refine.step_extract(con, uid, verbose=True, force=force)
        con.commit()
        d = extract.get_derived(con, uid)
        if not d or not d.get("summary"):
            raise RuntimeError("提炼失败")

        n_new = con.execute(
            "SELECT COUNT(*) FROM content_block cb WHERE NOT EXISTS "
            "(SELECT 1 FROM vec_state v WHERE v.block_id=cb.id)").fetchone()[0]

        return {"uid": uid, "derived": d, "verdict": v, "evidence": ev,
                "blocks": blocks, "seconds": round(time.time() - t0, 1),
                "unindexed": n_new}
    finally:
        con.close()

def render(result: dict) -> None:
    from bili_digest.distill import extract
    if result.get("skipped"):
        return
    d = result["derived"]
    ev = result["evidence"]

    print()
    print("=" * 74)
    print(f"  {ev['title']}")
    print("=" * 74)
    print(f"\n【摘要】\n  {d.get('summary')}")

    for key, label in (("key_points", "要点"), ("audience", "观众视角")):
        items = d.get(key) or []
        if items:
            print(f"\n【{label}】{len(items)} 条")
            for i, p in enumerate(items, 1):
                print(f"  {i}. {p}")

    tp = d.get("topics") or []
    if tp:
        print(f"\n【主题词】{', '.join(map(str, tp))}")

    ins = d.get("insights") or []
    if ins:
        print(f"\n【洞察】{len(ins)} 条（引用块可回查原文）")
        for i, it in enumerate(ins, 1):
            if isinstance(it, dict):
                print(f"  {i}. {it.get('claim')}")
                if it.get("cites"):
                    print(f"     ↳ 引用块 {it['cites']}")
            else:
                print(f"  {i}. {it}")

    sc = d.get("scores") or {}
    print("\n【评分】")
    for k in extract.SCORE_KEYS:
        print(f"  {k:<16} {sc.get(k)}")
    if sc.get("mean") is not None:
        print(f"  {'mean':<16} {sc['mean']}")
    if sc.get("reason"):
        print(f"  依据: {sc['reason']}")

    print(f"\n  用时 {result['seconds']}s")
    if result.get("unindexed"):
        print(f"  [!] 有 {result['unindexed']} 块尚未进索引 "
              f"→ 跑 python build_kb_index.py 后即可检索")

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="抓取并总结单个视频")
    ap.add_argument("bvid")
    ap.add_argument("sources", nargs="?", default=DEFAULT_SOURCES,
                    help=f"抓取源，默认 {DEFAULT_SOURCES}")
    ap.add_argument("--no-force", action="store_true",
                    help="已有提炼则跳过（默认重跑）")
    a = ap.parse_args()

    r = digest_one(a.bvid, a.sources, force=not a.no_force)
    render(r)

if __name__ == "__main__":
    main()
