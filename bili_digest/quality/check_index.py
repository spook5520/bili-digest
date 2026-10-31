#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import hashlib
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.paths import DB, VEC, EMBED_MODEL

def _vec_keys(con) -> set:
    try:
        from bili_digest.retrieve import rag_core as _rc
        from bili_digest.paths import VEC as _VEC
        keys = _rc.load_index_keys(str(_VEC) if not hasattr(_VEC, "path") else _VEC)
        if keys is not None:
            return set(keys)
    except Exception:
        pass
    try:
        return {r[0] for r in con.execute("SELECT block_id FROM vec_state")}
    except sqlite3.OperationalError:
        return set()

def gc_vec_state(verbose: bool = True) -> dict:
    from bili_digest import paths
    con = sqlite3.connect(str(paths.DB))
    con.row_factory = sqlite3.Row
    try:
        orphans = [r["block_id"] for r in con.execute(
            "SELECT block_id FROM vec_state "
            "WHERE block_id NOT IN (SELECT id FROM content_block)")]
        if not orphans:
            if verbose:
                print("  孤立向量记录        0  ✓")
            return {"orphans": 0}
        n = 0
        for i in range(0, len(orphans), 500):
            chunk = orphans[i:i + 500]
            cur = con.execute(
                f"DELETE FROM vec_state WHERE block_id IN ({','.join('?' * len(chunk))})",
                chunk)
            n += cur.rowcount
        con.commit()
        if verbose:
            print(f"  清理孤立向量记录    {n} 条（指向已删除的块）")
        return {"orphans": n}
    finally:
        con.close()

def check_data_completeness(verbose: bool = True) -> dict:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    r = {}

    r["缺 meta 的条目"] = con.execute(
        "SELECT COUNT(*) FROM item i WHERE EXISTS"
        "(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid) "
        "AND NOT EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='meta')"
    ).fetchone()[0]

    r["有内容但无评论的条目"] = con.execute(
        "SELECT COUNT(*) FROM item i WHERE EXISTS"
        "(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid "
        "AND cb.kind IN ('subtitle','subtitle_seg','danmaku')) "
        "AND NOT EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='comment')"
    ).fetchone()[0]

    r["弹幕<10 条的（可能截断）"] = con.execute(
        "SELECT COUNT(*) FROM (SELECT item_uid, COUNT(*) n FROM content_block "
        "WHERE kind='danmaku' GROUP BY item_uid HAVING n < 10)"
    ).fetchone()[0]

    r["有字幕但无段落的"] = con.execute(
        "SELECT COUNT(*) FROM item i WHERE EXISTS"
        "(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='subtitle') "
        "AND NOT EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='subtitle_seg')"
    ).fetchone()[0]

    try:
        r["job_log 失败记录"] = con.execute(
            "SELECT COUNT(*) FROM job_log WHERE status='fail'").fetchone()[0]
    except sqlite3.OperationalError:
        r["job_log 失败记录"] = None

    try:
        r["knowledge 档但无提炼"] = con.execute(
            "SELECT COUNT(*) FROM lifecycle l WHERE l.tier='knowledge' AND NOT EXISTS"
            "(SELECT 1 FROM derived d WHERE d.item_uid=l.item_uid AND d.kind='summary')"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        r["knowledge 档但无提炼"] = None

    try:
        from bili_digest.paths import PLATFORM as _PF
        pat = _PF + ":%"
        r["孤儿块（uid 缺平台前缀）"] = con.execute(
            "SELECT COUNT(*) FROM content_block WHERE item_uid NOT LIKE ?",
            (pat,)).fetchone()[0]
        r["孤儿批注（uid 缺平台前缀）"] = con.execute(
            "SELECT COUNT(*) FROM annotation WHERE item_uid NOT LIKE ?",
            (pat,)).fetchone()[0]
    except Exception:
        r["孤儿块（uid 缺平台前缀）"] = None
        r["孤儿批注（uid 缺平台前缀）"] = None

    try:
        _prev = con.execute(
            "SELECT v FROM meta_kv WHERE k='kw_block_total'").fetchone()
        prev = int(_prev[0]) if _prev and _prev[0] else None
        expect = con.execute(
            "SELECT COUNT(*) FROM content_block WHERE kind IN "
            "('subtitle','subtitle_seg','danmaku','comment','meta')").fetchone()[0]
        if prev is None:

            n_cov = con.execute(
                "SELECT COUNT(DISTINCT block_id) FROM keyword_block").fetchone()[0]
            r["keyword 已覆盖块"] = n_cov
        else:
            r["keyword 表差多少块"] = max(0, expect - prev)
    except sqlite3.OperationalError:
        r["keyword 表未建"] = None

    try:
        r["字幕覆盖不足一半"] = con.execute("""
            SELECT COUNT(*) FROM (
              SELECT i.uid, i.duration_sec d, MAX(cb.offset_sec) last
              FROM item i JOIN content_block cb ON cb.item_uid=i.uid
              WHERE cb.kind IN ('subtitle','subtitle_seg') AND i.duration_sec > 180
              GROUP BY i.uid HAVING COUNT(cb.id) > 5
                AND MAX(cb.offset_sec) < i.duration_sec * 0.5
            )""").fetchone()[0]
    except sqlite3.OperationalError:
        r["字幕覆盖不足一半"] = None

    try:
        r["字幕超出视频时长"] = con.execute("""
            SELECT COUNT(*) FROM (
              SELECT i.uid, i.duration_sec d, MAX(cb.offset_sec) last
              FROM item i JOIN content_block cb ON cb.item_uid=i.uid
              WHERE cb.kind IN ('subtitle','subtitle_seg') AND i.duration_sec > 0
              GROUP BY i.uid HAVING MAX(cb.offset_sec) > i.duration_sec * 1.5
            )""").fetchone()[0]
    except sqlite3.OperationalError:
        r["字幕超出视频时长"] = None

    con.close()

    if verbose:
        print("  数据完整性抽查（检静默降级的后果）：")
        for k, v in r.items():
            mark = "✓" if v in (0, None) else "!"
            print(f"    [{mark}] {k:<28} {v}")

    return r

def check_decision_consistency(verbose: bool = True, sample: int = 100000) -> dict:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.quality import lifecycle as lc
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    r = {}
    try:
        pf = lc._prefetch(con)
        rules = lc.load_blocklist(con)
        uids = [x["uid"] for x in
                con.execute(f"SELECT uid FROM item LIMIT {int(sample)}")]
        score_diff = []
        for uid in uids:
            a = lc.score_item(con, uid, rules)
            row = con.execute("SELECT * FROM item WHERE uid=?", (uid,)).fetchone()
            if not row or a.get("score") is None:
                continue
            b = lc.score_from_prefetch(dict(row), pf, rules)
            if abs((a.get("score") or 0) - (b.get("score") or 0)) > 1e-6:
                score_diff.append(uid)
        r["算分两路径差异"] = len(score_diff)
        r["算分样本"] = len(uids)

        try:
            dec = {}
            for uid in uids:
                d = lc.decide(con, uid, apply=False)
                if isinstance(d, dict) and d.get("tier"):
                    dec[uid] = d["tier"]
            sw_res = lc.sweep(con, apply=False, limit=100000, with_uid=True)
            sw_map = sw_res.get("by_uid") or {}
            if sw_map:
                diff = [u for u in dec if sw_map.get(u) and sw_map[u] != dec[u]]
                r["分级两路径差异"] = len(diff)
                r["分级样本"] = len(dec)
            else:
                r["分级两路径差异"] = None
        except Exception as e:
            r["分级两路径差异"] = None
            r["分级检查错误"] = f"{type(e).__name__}: {str(e)[:60]}"
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
    finally:
        con.close()
    if verbose:
        print("\n  判定一致性：")
        d = r.get("算分两路径差异", 0)
        print(f"    算分（score_item vs score_from_prefetch）："
              f"{'✓ 一致' if not d else f'✗ {d} 条不同'}"
              f"（样本 {r.get('算分样本', 0)}）")
        g = r.get("分级两路径差异")
        print(f"    分级（decide vs sweep）："
              f"{'✓ 一致' if g == 0 else (f'✗ {g} 条不同' if isinstance(g, int) else '（无法比对）')}"
              f"（样本 {r.get('分级样本', 0)}）")
        if r.get("分级检查错误"):
            print(f"      {r['分级检查错误']}")
        if r.get("error"):
            print(f"    ✗ {r['error']}")
    return r

def check(verbose: bool = True) -> dict:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    rows = con.execute("SELECT id, kind, text, hash FROM content_block").fetchall()

    try:
        from bili_digest.retrieve.rag_core import info_len, MIN_INFO_CHARS
        rows = [r for r in rows if info_len(r["text"]) >= MIN_INFO_CHARS]
    except Exception:
        pass

    by_id = {r["id"]: (r["kind"], r["hash"], r["text"]) for r in rows}
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1

    keys = _vec_keys(con)

    missing = sorted(set(by_id) - keys)
    orphan = sorted(keys - set(by_id))

    miss_by_kind = {}
    for bid in missing:
        k = by_id[bid][0]
        miss_by_kind[k] = miss_by_kind.get(k, 0) + 1

    stale = []
    try:

        cols = [r[1] for r in con.execute("PRAGMA table_info(vec_state)")]
        if "text_hash" not in cols:
            try:
                con.execute("ALTER TABLE vec_state ADD COLUMN text_hash TEXT")
                con.commit()
                cols.append("text_hash")
            except sqlite3.OperationalError:
                pass

        if "text_hash" in cols:
            from bili_digest.schema.core_schema import block_hash as _bh
            _miss = con.execute(
                "SELECT COUNT(*) FROM vec_state WHERE text_hash IS NULL").fetchone()[0]
            if _miss:
                _pairs = [(r[0], r[1], r[2]) for r in con.execute(
                    "SELECT v.block_id, b.kind, b.text FROM vec_state v "
                    "JOIN content_block b ON b.id = v.block_id "
                    "WHERE v.text_hash IS NULL")]
                con.executemany(
                    "UPDATE vec_state SET text_hash=? WHERE block_id=?",
                    [(_bh(k, t), bid) for bid, k, t in _pairs])
                con.commit()
        if "text_hash" in cols:
            from bili_digest.schema.core_schema import block_hash as _bh
            vs = {r["block_id"]: r["text_hash"] for r in
                  con.execute("SELECT block_id, text_hash FROM vec_state")}
            for bid, (k, h, t) in by_id.items():
                cur = vs.get(bid)
                if not cur:
                    continue
                if cur != _bh(k, t):
                    stale.append(bid)
    except sqlite3.OperationalError:
        pass

    try:
        fts_n = con.execute("SELECT COUNT(*) FROM block_fts").fetchone()[0]
    except sqlite3.OperationalError:
        fts_n = None

    fts_works = None
    if fts_n:
        try:
            from bili_digest.retrieve import rag_core as rc
            probe = con.execute(
                "SELECT tok FROM block_fts LIMIT 1").fetchone()
            word = (probe[0] if probe else "") or ""
            if word:
                fts_works = len(rc.fts_search(con, word, 3)) > 0
            else:
                fts_works = False
        except Exception:
            fts_works = False

    con.close()

    report = {
        "blocks": len(by_id), "kinds": kinds,
        "vec": len(keys),
        "missing": len(missing), "missing_by_kind": miss_by_kind,
        "orphan": len(orphan), "stale": len(stale),
        "fts": fts_n,
        "fts_works": fts_works,
        "consistent": not missing and not orphan and not stale,
    }

    if verbose:
        print(f"  content_block   {report['blocks']}")
        for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
            m = miss_by_kind.get(k, 0)
            flag = f"  ✗ 缺 {m}" if m else "  ✓"
            print(f"    {k:<16} {n:>6}{flag}")
        print(f"  向量索引        {report['vec']}")
        if missing:
            print(f"    ✗ 库里有但索引没有：{len(missing)}"
                  f"（按类型 {miss_by_kind}）")
        if orphan:
            print(f"    ✗ 索引有但库里没有：{len(orphan)}（应重建）")
        if stale:
            print(f"    ✗ 文本已变但向量未更新：{len(stale)}")
        if fts_n is not None:
            flag = "✓" if fts_n == report["blocks"] else "✗"
            extra = ""
            if fts_works is False:
                extra = "  ★ 但实际查不到任何结果（fts_search 失效）"
            elif fts_works is True:
                extra = "（实查可用）"
            print(f"  FTS 索引        {fts_n}  [{flag}]{extra}")
        print(f"\n  {'✓ 一致' if report['consistent'] else '✗ 不一致 —— 跑 build_kb_index.py 重建'}")

    return report

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="索引一致性 + 数据完整性检查")
    ap.add_argument("--fix", action="store_true", help="索引不一致时自动重建")
    ap.add_argument("--gc", action="store_true", help="清理孤立向量记录（重建前置）")
    ap.add_argument("--data", action="store_true", help="只看数据完整性")
    a = ap.parse_args()

    if a.gc:
        print("  清理孤立向量记录：")
        gc_vec_state()

    if not a.data:
        r = check()
        check_decision_consistency()
    else:
        r = {"consistent": True}

    print()
    check_data_completeness()

    if a.fix:

        print("\n  先清理孤立向量记录…")
        g = gc_vec_state()
        if not r.get("consistent") or g.get("orphans"):
            print("\n  重建索引…")
            import subprocess
            rc = subprocess.run([sys.executable, "-m", "bili_digest.tools.build_kb_index"],
                                cwd=str(paths.HERE))
            if rc.returncode == 0:
                print("\n  重建后复检：")
                r2 = check()
                print(f"\n  {'✓ 已修复' if r2['consistent'] else '✗ 仍不一致'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
