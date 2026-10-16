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
from pathlib import Path
from paths import DB, PLATFORM, item_url

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import connect, make_uid, now_sec, upsert_item

CAND = HERE / "discover_candidates.json"
SELF_COLLECTION = "自主添加"

def do_search(keyword: str, page: int = 1, pages: int = 1) -> list[dict]:
    from adapters import get_adapter
    ad = get_adapter(PLATFORM)
    out = []
    for p in range(page, page + pages):
        res = ad.search(keyword, page=p)
        out.extend(res)
        if not res:
            break
    return out

def do_following(per_up: int = 10, max_ups: int = 50,
                 con=None) -> list[dict]:
    from adapters import get_adapter

    ad = get_adapter(PLATFORM)
    fw = ad.list_followings(limit=max_ups)
    if not fw:
        print("  关注列表为空或取不到（检查 KB_BILI_DEDEUSERID）")
        return []
    print(f"  关注 {len(fw)} 个 UP，逐个取最近 {per_up} 条投稿…")

    from core_schema import connect as _cs
    own = con is None
    c = con or _cs(DB)
    have = {r[0] for r in c.execute("SELECT item_id FROM item")}
    if own:
        c.close()

    rows, sampled = [], 0
    for u in fw:
        ups = ad.list_uploads(u["author_uid"], page=1)[:per_up]
        sampled += len(ups)
        for v in ups:
            if v.get("item_id") and v["item_id"] not in have:
                v = dict(v)
                v["found_via"] = f"关注:{u.get('author_name') or u['author_uid']}"
                rows.append(v)
        if rows:
            print(f"    {u.get('author_name') or u['author_uid']}: "
                  f"投稿 {len(ups)} 条，其中新 {len([x for x in rows if x.get('author_uid') == u['author_uid']])} 条")
    print(f"  采样 {sampled} 条投稿 → 库里没有的 {len(rows)} 条")
    return rows

def save_candidates(keyword: str, rows: list[dict]) -> Path:
    data = {"keyword": keyword, "at": now_sec(), "rows": rows}
    CAND.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return CAND

def load_candidates() -> dict:
    if not CAND.exists():
        return {}
    try:
        return json.loads(CAND.read_text(encoding="utf-8"))
    except Exception:
        return {}

def show_candidates(data: dict, mark_blocked: bool = True):
    rows = data.get("rows") or []
    if not rows:
        print("  没有候选")
        return
    print(f"  关键词「{data.get('keyword')}」  共 {len(rows)} 条候选\n")
    print(f"  {'#':>3}  {'时长':>7}  {'播放':>9}  {'UP':<14} 标题")
    print("  " + "-" * 78)
    blocked_ids = set()
    if mark_blocked:
        try:
            from core_schema import load_blocklist, is_blocked
            con = connect(DB)
            rules = load_blocklist(con)
            for r in rows:
                hit, _ = is_blocked(rules, item_id=r.get("item_id", ""),
                                    author_uid=str(r.get("author_uid") or ""),
                                    text=r.get("title", ""))
                if hit:
                    blocked_ids.add(r.get("item_id"))
            con.close()
        except Exception:
            pass
    for i, r in enumerate(rows, 1):

        _d = r.get("duration_sec")
        dur = f"{int(_d)//60}:{int(_d)%60:02d}" if _d else ""
        play = (r.get("extras") or {}).get("play") or 0
        flag = "  [已屏蔽]" if r.get("item_id") in blocked_ids else ""
        print(f"  {i:>3}  {str(dur):>7}  {play:>9,}  "
              f"{str(r.get('author_name'))[:14]:<14} {str(r.get('title'))[:40]}{flag}")
    print(f"\n  采纳：python discover.py --adopt 1,3,5   （或 --adopt-all）")

def do_adopt(indices: list[int], data: dict, refine_after: bool = False,
             limit: int = None, con: sqlite3.Connection = None) -> dict:
    rows = data.get("rows") or []
    if not rows:
        return {"error": "没有候选，先跑 --keyword 搜索"}

    if limit:
        rows = rows[:limit]
    picked = []
    for i in indices:
        if 1 <= i <= len(rows):
            picked.append(rows[i - 1])
    if not picked:
        return {"error": f"序号无效（候选 1~{len(rows)}）"}

    own_con = con is None
    if own_con:
        con = connect(DB)
    con.row_factory = sqlite3.Row

    cid = make_uid(PLATFORM, "self:" + SELF_COLLECTION)
    con.execute("""
        INSERT INTO collection(id,platform,name,total,extras,synced_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            total=excluded.total, synced_at=excluded.synced_at
    """, (cid, PLATFORM, SELF_COLLECTION, len(picked),
          json.dumps({"source": "discover", "keyword": data.get("keyword")}),
          now_sec()))

    added, skipped = 0, 0
    for r in picked:
        bvid = r.get("item_id")
        if not bvid:
            continue
        exists = con.execute("SELECT uid FROM item WHERE item_id=?", (bvid,)).fetchone()
        if exists:
            uid = exists["uid"]
            skipped += 1
        else:
            uid = upsert_item(
                con, platform=PLATFORM, item_id=bvid,
                title=r.get("title"),
                url=item_url(PLATFORM, bvid),
                author_uid=str(r.get("author_uid") or "") or None,
                author_name=r.get("author_name"),
                published_at=r.get("published_at"),
                saved_at=now_sec(),
                duration_sec=r.get("duration_sec"),
                extras={"play": (r.get("extras") or {}).get("play"),
                        "danmaku_count": (r.get("extras") or {}).get("danmaku"),
                        "desc": (r.get("extras") or {}).get("description"),
                        "tags": (r.get("extras") or {}).get("search_tag"),
                        "source": "discover", "keyword": data.get("keyword")})
            added += 1
        con.execute("""
            INSERT INTO collection_member(collection_id,item_uid,added_at)
            VALUES(?,?,?) ON CONFLICT(collection_id,item_uid) DO UPDATE SET
                added_at=excluded.added_at
        """, (cid, uid, now_sec()))

    con.commit()
    if own_con:
        con.close()

    r = {"adopted": len(picked), "new": added, "existing": skipped}
    if refine_after:
        import refine as _rf
        print(f"\n  开始提炼…")
        r["refine"] = _rf.refine(limit=len(picked), order="saved")
    return r

def add_cli_args(p, *, with_db: bool = True):
    if with_db:
        p.add_argument("--db", default=str(DB))
    p.add_argument("--keyword", help="搜索关键词（联网）")
    p.add_argument("--from-following", action="store_true",
                   help="从关注的 UP 发现新内容（主动汲取；不自动采纳）")
    p.add_argument("--per-up", type=int, default=10,
                   help="--from-following 时每个 UP 取最近 N 条")
    p.add_argument("--max-ups", type=int, default=50,
                   help="--from-following 时最多看前 N 个关注的 UP")
    p.add_argument("--pages", type=int, default=1, help="搜索页数，每页约 20 条")
    p.add_argument("--list", action="store_true", help="显示上次搜索的候选")
    p.add_argument("--adopt", help="采纳指定序号，如 1,3,5")
    p.add_argument("--adopt-all", action="store_true", help="采纳全部候选")
    p.add_argument("--limit", type=int, help="adopt-all 时只取前 N 条")
    p.add_argument("--refine", action="store_true", help="采纳后立即采集+提炼")
    return p

def run(a, con=None) -> dict:

    try:
        from core_schema import backlog_gate, connect as _cs_connect
        from paths import DB as _DB
        backlog_gate(con or _cs_connect(_DB), "搜索发现新内容")
    except SystemExit:
        raise
    except Exception:
        pass

    if getattr(a, "from_following", False):
        rows = do_following(per_up=a.per_up, max_ups=a.max_ups, con=con)
        save_candidates("关注UP", rows)
        show_candidates({"keyword": "关注UP", "rows": rows})
        return {"found": len(rows)}
    if a.keyword:
        print(f"  搜索「{a.keyword}」…")
        rows = do_search(a.keyword, pages=a.pages)
        save_candidates(a.keyword, rows)
        show_candidates({"keyword": a.keyword, "rows": rows})
        return {"found": len(rows)}
    if a.list:
        data = load_candidates()
        if not data:
            print("  还没有候选，先跑 --keyword <关键词>")
            return {"found": 0}
        show_candidates(data)
        return {"found": len(data.get("rows") or [])}
    if a.adopt or a.adopt_all:
        data = load_candidates()
        if a.adopt_all:
            n = len(data.get("rows") or [])
            if a.limit:
                n = min(n, a.limit)
            idx = list(range(1, n + 1))
        else:
            try:
                idx = [int(x.strip()) for x in a.adopt.split(",") if x.strip()]
            except ValueError:
                raise SystemExit("--adopt 格式应为 1,3,5")
        r = do_adopt(idx, data, refine_after=a.refine, con=con)
        print(f"\n  采纳结果：{json.dumps(r, ensure_ascii=False, default=str)}")

        if con is not None:
            try:
                import lifecycle as _lc
                _lc.sweep(con, apply=True)
                con.commit()
            except Exception as e:
                print(f"  [!] 重新分级失败（继续）: {type(e).__name__}: {str(e)[:60]}")
        return r
    print("  用法：--keyword <词> | --list | --adopt 1,3 | --adopt-all [--refine]")
    return {}

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="搜索视频并自主添加到知识库")
    add_cli_args(ap)
    a = ap.parse_args(argv)
    run(a)

if __name__ == "__main__":
    main()
