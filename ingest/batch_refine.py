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

from paths import enable_utf8_stdout

enable_utf8_stdout()

HERE = Path(__file__).parent

from core_schema import now_sec
from paths import DB

STATE = HERE / "batch_state.json"
DEFAULT_BATCH = 20
DEFAULT_COOLDOWN = 90
SOURCES = "meta,sub,reply"

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "failed": [], "rounds": 0, "started_at": None}

def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")

def pending(con, state) -> list:
    rows = con.execute("""
        SELECT i.item_id, i.title FROM item i
        WHERE NOT EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid)
        ORDER BY i.saved_at DESC
    """).fetchall()
    return [(r[0], r[1]) for r in rows]

def show_status(con, state) -> None:
    all_pending = pending(con, state)
    tot = con.execute("SELECT COUNT(*) FROM item").fetchone()[0]
    done_n = tot - len(all_pending)
    print(f"  条目总数   {tot}")
    print(f"  已有内容   {done_n}  ({done_n/tot*100:.1f}%)")
    print(f"  待抓取     {len(all_pending)}")
    print(f"  本轮已处理 {len(state['done'])}（state 记录）")
    if state.get("failed"):
        print(f"  失败记录   {len(state['failed'])}")
        for f in state["failed"][-3:]:
            print(f"      {f['item_id']}  {f['reason'][:50]}")
    if state.get("rounds"):
        print(f"  已完成批次 {state['rounds']}")
    if state.get("started_at"):
        t0 = state["started_at"]
        print(f"  开始于     {time.strftime('%H:%M:%S', time.localtime(t0))}")

def next_cooldown(cur: int, base: int, blocked: bool,
                  safe: int | None) -> tuple[int, int | None]:
    if blocked:

        return min(cur * 2, 900), cur
    margin = int((safe or base) * 1.5)
    floor = max(base, margin)
    return max(floor, int(cur * 0.8)), safe

def run(batch: int, cooldown: int, max_total: int = 0,
        order: str = "saved") -> None:
    import refine

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    state = load_state()
    if not state.get("started_at"):
        state["started_at"] = now_sec()

    todo = pending(con, state)
    con.close()

    if not todo:
        print("  ✓ 没有待抓取的条目了")
        return
    if max_total:
        todo = todo[:max_total]

    try:
        import bili_digest as _bd
        _bd.load_credential()
    except SystemExit as e:
        print(f"\n  [!] 抓取需要 B站登录态，而当前没有：\n{e}")
        print("\n  （这类失败不是风控 —— 退避无用，所以在这里就停）")
        return

    print(f"  待抓取 {len(todo)} 条，每批 {batch} 条，批间冷却 {cooldown}s")
    t0 = time.time()
    processed = 0
    cur_cooldown = cooldown
    dead_batches = 0

    safe_cooldown = None
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        state["rounds"] += 1
        print(f"\n  ── 第 {state['rounds']} 批（{i+1}~{i+len(chunk)}/{len(todo)}）"
              f"  冷却基数 {cur_cooldown}s ──")

        th = refine.FetchThrottle(base_ms=5000, max_ms=15000)

        st = refine.refine(limit=len(chunk), order=order,
                           do_extract=True, do_index=False,
                           dry=False, sources=SOURCES, throttle=th)

        got = st.get("imported", 0)
        failed = st.get("failed", 0)
        aborted = st.get("aborted", False)
        processed += got
        for bv, _t in chunk:
            state["done"].append(bv)
        save_state(state)

        if aborted or (failed and failed >= len(chunk) / 2):
            cur_cooldown, safe_cooldown = next_cooldown(
                cur_cooldown, cooldown, True, safe_cooldown)
            print(f"      [!] 本批风控明显（失败 {failed}/{len(chunk)}"
                  f"{'，已中止' if aborted else ''}）→ 冷却提高到 {cur_cooldown}s"
                  f"（记住 {safe_cooldown}s 为下界）")
        elif got == len(chunk):
            cur_cooldown, safe_cooldown = next_cooldown(
                cur_cooldown, cooldown, False, safe_cooldown)

        dead_batches = dead_batches + 1 if got == 0 else 0
        if dead_batches >= 2:
            print(f"\n  [!] 连续 {dead_batches} 批零成功 —— 判定为账号级风控，"
                  f"停止本轮。")
            print(f"      建议等 1~2 小时后再跑；或改用更省请求的模式")
            print(f"      （`--sources meta,sub`：2~3 请求/视频，实测可连跑 20 个）")
            break

        el = time.time() - t0
        rate = processed / el * 60 if el > 0 else 0
        left = len(todo) - (i + len(chunk))
        eta = left / rate if rate > 0 else 0
        print(f"      批结果 {st}")
        print(f"      累计 {processed} 条，用时 {el/60:.1f} 分，"
              f"{rate:.1f} 条/分，剩余 {left} 条 ≈ {eta:.0f} 分钟")

        if left > 0:
            print(f"      冷却 {cur_cooldown}s…")
            time.sleep(cur_cooldown)

    print("\n  ── 收尾 ──")
    print("  切字幕段…", refine.step_segment())
    print("  建索引…", refine.step_index())
    print(f"\n  ✓ 完成。本轮处理 {processed} 条，总用时 {(time.time()-t0)/60:.1f} 分钟")

def main():
    import argparse
    ap = argparse.ArgumentParser(description="可断点续跑的批量提炼")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    ap.add_argument("--max", type=int, default=0, help="本次最多跑几条（0=全部）")
    ap.add_argument("--order", default="saved", choices=["saved", "hot", "short"],
                    help="挑队列的顺序：saved=收藏时间（默认）· "
                         "hot=播放量（产出高一个数量级，实测 ρ=0.903）· short=时长")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.reset:
        save_state({"done": [], "failed": [], "rounds": 0, "started_at": None})
        print("  进度已清空（注意：这只清 state，已抓的内容仍在库里）")
        return

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    if a.status:
        show_status(con, load_state())
        con.close()
        return
    con.close()

    run(a.batch, a.cooldown, a.max, order=a.order)

if __name__ == "__main__":
    main()
