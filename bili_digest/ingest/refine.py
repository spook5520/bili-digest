#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from bili_digest.paths import DB, DIGEST_DIR, bili_uid

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

ROOT = paths.HERE

from bili_digest.schema.core_schema import (KIND_META, connect, log_job, now_sec, upsert_item)
from bili_digest.quality.lifecycle import DISCARD_THRESHOLD, KEEP_THRESHOLD, sweep

PY = sys.executable

class FetchThrottle:

    def __init__(self, base_ms: int = 15000, max_ms: int = 40000):
        self.base_ms = base_ms
        self.max_ms = max_ms
        self.n = 0
        self.risk = 0
        self.risk_streak = 0

    def gap_ms(self) -> int:
        gap = min(self.base_ms + (self.n // 20) * 5000, self.max_ms)
        gap *= (2 ** min(self.risk, 3))
        return int(gap * random.uniform(0.85, 1.15))

    def wait(self, verbose: bool = True) -> float:
        if self.n == 0:
            return 0.0
        gap = self.gap_ms() / 1000
        if verbose and gap >= 1:
            print(f"      等待 {gap:.1f}s（第 {self.n} 个，风控级别 {self.risk}）")
        time.sleep(gap)
        return gap

    def note(self, ok: bool, msg: str) -> None:
        self.n += 1
        is_risk = (not ok) and ("412" in str(msg) or "风控" in str(msg))
        if is_risk:
            self.risk = min(self.risk + 1, 3)
            self.risk_streak += 1
        elif ok:
            self.risk = max(0, self.risk - 1)
            self.risk_streak = 0

    def should_abort(self, threshold: int = 4) -> bool:
        return self.risk_streak >= threshold

    def status(self) -> str:
        return f"已抓 {self.n} 个，风控级别 {self.risk}，连续风控 {self.risk_streak}"

def _raw_complete(path, sources: str) -> bool:
    import sqlite3 as _s
    if not path.exists():
        return False
    try:
        con = _s.connect(str(path))

        try:
            r = con.execute("SELECT v FROM meta WHERE k='_done_sources'").fetchone()
            if r and (r[0] or "").strip():
                con.close()
                return True
        except _s.OperationalError:
            pass

        content_tables = ("subtitle", "danmaku", "reply", "ad")
        wanted = any(s.strip() in ("sub", "dm", "reply", "ad")
                     for s in (sources or "").split(","))
        if not wanted:
            con.close()
            return True
        have_any = False
        for t in content_tables:
            try:
                if con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0:
                    have_any = True
                    break
            except _s.OperationalError:
                pass
        con.close()
    except Exception:
        return False
    return have_any

def step_fetch(bvid: str, timeout: int = 300,
               sources: str = "meta,sub,dm,reply,ad",
               force: bool = False) -> tuple[bool, str]:
    raw = DIGEST_DIR / bvid / "raw.sqlite"
    if raw.exists() and not force:
        if _raw_complete(raw, sources):
            return True, "已存在"

        bak = raw.with_suffix(".sqlite.bak")
        try:
            import shutil
            shutil.copy2(raw, bak)
        except OSError:
            pass
        force = True
    if raw.exists() and force:

        bak = raw.with_suffix(".sqlite.bak")
        try:
            import shutil
            shutil.copy2(raw, bak)
        except OSError:
            pass
    cmd = [PY, "-m", "bili_digest.ingest.fetcher", bvid,
           f"--sources={sources}", "--strip-ad"]
    try:

        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                           timeout=timeout)
        if r.returncode == 0:
            return True, "ok（重抓）" if force else "ok"
        err = (r.stderr or r.stdout or b"").decode("utf-8", "replace")
        return False, err.strip().splitlines()[-1][:100] if err.strip() else "失败"
    except subprocess.TimeoutExpired:
        return False, "超时"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"

def step_import(con, bvid: str) -> dict:
    import importlib
    from bili_digest.adapters import adapters
    importlib.reload(adapters)
    raw = DIGEST_DIR / bvid / "raw.sqlite"
    if not raw.exists():
        return {"error": "raw.sqlite 不存在"}
    return adapters.import_digest(con, raw)

def step_judge(con, uid: str, verbose: bool = False):
    from bili_digest.distill import classify

    v = classify.classify_item(con, uid)
    ex_val = None if v.extractable is None else (1 if v.extractable else 0)
    try:
        con.execute("UPDATE lifecycle SET content_kind=?, extractable=? "
                    "WHERE item_uid=?", ("+".join(v.content_kinds), ex_val, uid))
    except sqlite3.OperationalError:
        pass
    if verbose:
        print(f"      判定 {'+'.join(v.content_kinds)} · {v.reason}")
    return v

def step_extract(con, uid: str, verbose: bool = False,
                 force: bool = False) -> bool:
    v = step_judge(con, uid, verbose)
    if v.extractable is False:
        if verbose:
            print(f"      不提炼：{v.reason}")
        return False

    from bili_digest.distill import extract
    try:
        extract.ensure_table(con)
        res = extract.extract_item(con, uid, force=force, verbose=False)
        return bool(res)
    except Exception as e:
        print(f"      提炼失败 {type(e).__name__}: {str(e)[:60]}")
        return False

def step_segment(verbose=False) -> dict:
    try:
        from bili_digest.distill import seg_boundary as sb
        r = sb.build(incremental=True, verbose=False)
        return {"segments": r["segments"], "items": r["items"]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:70]}"}

def step_index(verbose=False) -> dict:
    try:
        from bili_digest.tools import build_kb_index as bi
        con = connect(DB)
        con.row_factory = sqlite3.Row
        derived_added = 0

        try:
            from bili_digest.ingest.migrate_unify_content import sync_derived
            pending = [r[0] for r in con.execute(
                "SELECT DISTINCT d.item_uid FROM derived d "
                "WHERE NOT EXISTS(SELECT 1 FROM content_block cb "
                "                 WHERE cb.item_uid = d.item_uid)")]
            for u in pending:
                added = sync_derived(con, u, verbose=False)
                derived_added += sum(added.values()) if added else 0
        except Exception as e:
            print(f"    [!] 派生物入检索失败：{type(e).__name__}: {str(e)[:50]}")
        mode, n_fts = bi.build_fts(con, verbose=False, incremental=True)

        r = bi.build_vec(con, verbose=False, rebuild=False)

        struct = {}
        try:
            from bili_digest.graph import refresh
            struct = refresh.refresh(con, verbose=False)
        except Exception as e:
            struct = {"error": f"{type(e).__name__}: {str(e)[:60]}"}
        con.close()
        return {"fts": n_fts, "derived_added": derived_added,
                "struct": struct, **r}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:70]}"}

def pick_queue(con, limit: int, order: str = "saved",
               respect_block: bool = True) -> list[dict]:

    order_sql = {
        "saved": "COALESCE(i.saved_at,0) DESC",
        "hot": ("COALESCE(json_extract(i.extras,'$.view'),"
                "json_extract(i.extras,'$.play'),0) DESC"),
        "short": "COALESCE(i.duration_sec,999999) ASC",

        "empty": ("(SELECT COUNT(*) FROM content_block b WHERE b.item_uid = i.uid) ASC,"
                  " CASE WHEN COALESCE(i.duration_sec,0) BETWEEN 180 AND 1800"
                  " THEN 0 ELSE 1 END ASC,"
                  " COALESCE(i.duration_sec,999999) ASC"),

        "value": ("COALESCE(json_extract(i.extras,'$.view'),"
                  "json_extract(i.extras,'$.play'),0) DESC"),
    }.get(order, "COALESCE(i.saved_at,0) DESC")

    skip_sql = " AND (l.extractable IS NULL OR l.extractable <> 0)"
    params = []

    fetch_n = limit * 3 if respect_block else limit
    rows = con.execute(f"""
        SELECT i.uid, i.item_id, i.title, i.duration_sec
        FROM item i
        JOIN lifecycle l ON l.item_uid = i.uid
        LEFT JOIN derived d ON d.item_uid = i.uid AND d.kind='summary'
        WHERE l.tier = 'cache' AND d.id IS NULL{skip_sql}
        ORDER BY {order_sql}
        LIMIT ?
    """, (*params, fetch_n)).fetchall()

    if order == "value" and rows:
        try:
            from bili_digest.quality import agenda as _ag
            qs = _ag.open_questions(con)
            if qs:
                _qtok = [(qid, q, _ag._tokens(q)) for qid, q in qs]
                scored = []
                for r in rows:
                    toks = _ag._tokens(r["title"] or "")
                    best = max((len(t & toks) for _i, _q, t in _qtok), default=0)
                    scored.append((best, r))
                scored.sort(key=lambda x: -x[0])
                rows = [r for _s, r in scored]
                hit = sum(1 for s, _r in scored if s >= 2)
                if hit:
                    print(f"  value 序：{hit} 条与开放问题相关，排在最前")
                else:

                    _with_sum = con.execute(
                        "SELECT COUNT(*) FROM item i WHERE i.uid IN (%s) AND "
                        "EXISTS(SELECT 1 FROM derived d WHERE d.item_uid=i.uid)"
                        % ",".join("?" * len(rows)),
                        [r["uid"] for r in rows]).fetchone()[0]
                    print(f"  value 序：候选 {len(rows)} 条里没有与开放问题相关的"
                          f"（问题池 {len(qs)} 条）—— 已退化为热度序")
                    if _with_sum == 0:
                        print("    候选全是空壳，标题往往与真正的内容无关 —— "
                              "先把这批抓了（含内容后再对照才准）")
                    else:
                        print(f"    候选里 {_with_sum} 条已有提炼，说明这个方向"
                              f"库里确实没有待抓内容 —— 要么去 discover 搜新的，"
                              f"要么先在 kb question 里细化问题")
            else:
                print("  value 序：问题池为空 —— 退化为热度序。"
                      "先 kb question --add 登记几个长期问题")
        except Exception as e:
            print(f"  [!] value 排序失败（退回热度序）: {type(e).__name__}: {str(e)[:50]}")

    if respect_block and rows:
        try:
            from bili_digest.distill import block_plus
            keep, blocked = block_plus.filter_queue(con, rows)
            if blocked:
                print(f"  屏蔽库剔除 {len(blocked)} 条："
                      f"{[b['reason'] for b in blocked[:3]]}")
            rows = keep[:limit]
        except Exception as e:
            print(f"  [!] 屏蔽过滤失败（继续）: {type(e).__name__}: {str(e)[:50]}")
    return rows[:limit]

def refine(limit: int = 5, order: str = "saved", do_extract: bool = True,
           do_index: bool = True, dry: bool = False,
           sources: str = "meta,sub,dm,reply,ad",
           throttle: "FetchThrottle | None" = None) -> dict:
    con = connect(DB)
    con.row_factory = sqlite3.Row

    queue = pick_queue(con, limit, order)
    if not queue:
        print("  没有待提炼的 cache 条目（可能都处理过了）")
        con.close()
        return {"total": 0}

    print(f"  队列 {len(queue)} 条（order={order}）")
    if dry:
        for i, r in enumerate(queue, 1):
            m, s = divmod(r["duration_sec"] or 0, 60)
            print(f"    [{i}] {r['item_id']:<14} {m:>3}:{s:02d}  {str(r['title'])[:44]}")
        con.close()
        return {"total": len(queue), "dry": True}

    st = {"total": len(queue), "fetched": 0, "imported": 0,
          "extracted": 0, "promoted": 0, "discarded": 0, "failed": 0}
    th = throttle or FetchThrottle()

    for i, r in enumerate(queue, 1):
        bvid, uid = r["item_id"], r["uid"]
        print(f"\n  [{i}/{len(queue)}] {bvid}  {str(r['title'])[:42]}")

        th.wait()

        ok, msg = step_fetch(bvid, sources=sources)
        th.note(ok, msg)
        if not ok:
            print(f"      采集失败: {msg}")
            st["failed"] += 1
            log_job(con, bili_uid(bvid), "refine:fetch", "fail", msg)
            con.commit()
            if th.should_abort():
                print(f"\n  [!] 连续风控 {th.risk_streak} 次，中止本轮以免封控加重"
                      f"（{th.status()}）")
                st["aborted"] = True
                break
            continue
        st["fetched"] += 1
        print(f"      采集 ok（{msg}）")

        r2 = step_import(con, bvid)
        if r2.get("error") or r2.get("skipped"):
            print(f"      入库跳过: {r2}")
            st["failed"] += 1
            continue
        st["imported"] += 1
        n_blocks = sum(v for v in (r2.get("blocks") or {}).values()
                       if isinstance(v, int)) if isinstance(r2.get("blocks"), dict) else 0
        print(f"      入库 {n_blocks} 块")

        if do_extract:
            if step_extract(con, uid):
                st["extracted"] += 1
                print(f"      提炼 ok")
            else:
                print(f"      提炼跳过")

        con.commit()
        log_job(con, bili_uid(bvid), "refine", "ok", f"blocks={n_blocks}")

    print()
    r = sweep(con, apply=True)
    for t, n in sorted(r["by_tier"].items(), key=lambda x: -x[1]):
        print(f"    分级 {t:<12} {n}")
        if t == "knowledge":
            st["promoted"] = n
        elif t == "discarded":
            st["discarded"] = n

    if do_index:

        print("  切字幕段…")
        sr = step_segment()
        print(f"    {sr}")
        print("  建索引…")
        ir = step_index()
        print(f"    {ir}")

    con.close()
    return st

def add_cli_args(p, with_db: bool = False):
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--order", default="saved", choices=["saved", "hot", "short"],
                   help="队列排序：saved=收藏时间 / hot=播放量 / short=时长")
    p.add_argument("--sources", default="meta,sub,dm,reply,ad",
                   help="抓取哪些数据源。精简为 meta,sub,reply 可把请求数从 8~12 降到 4~5，"
                        "风控压力降约一半（LLM 提炼主要靠字幕，评论是判别力第二高的内容源）")
    p.add_argument("--base-gap", type=int, default=None,
                   help="抓取间隔毫秒；缺省按 sources 自动（精简 5s / 全量 15s）")
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.add_argument("--dry", action="store_true", help="只列队列，不执行")
    return p

def _throttle_for(a) -> "FetchThrottle | None":
    if getattr(a, "base_gap", None) is not None:
        return FetchThrottle(base_ms=a.base_gap)
    src = (getattr(a, "sources", "") or "").strip()

    lean = {"meta,sub,reply", "meta,sub", "sub", "meta", "meta,reply"}
    if src in lean:
        return FetchThrottle(base_ms=5000, max_ms=15000)
    return None

def run(a, con=None) -> dict:
    return refine(limit=a.limit, order=a.order,
                  do_extract=not a.no_extract, do_index=not a.no_index,
                  dry=a.dry, sources=a.sources,
                  throttle=_throttle_for(a))

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="提炼流水线（cache → knowledge）")
    add_cli_args(ap)
    a = ap.parse_args()

    t0 = time.time()
    st = run(a)
    print(f"\n  完成: {json.dumps(st, ensure_ascii=False)}")
    print(f"  耗时 {time.time()-t0:.1f}s")
