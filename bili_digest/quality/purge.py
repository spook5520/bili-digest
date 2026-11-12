import os
import shutil
import sqlite3
import sys
import time

_TABLES = ("block_fts", "content_block", "collection_member", "lifecycle", "community")

def find(con, up="", bv=""):
    if bv:
        rows = con.execute("SELECT uid, item_id, title, author_name FROM item WHERE item_id = ?", (bv,)).fetchall()
    elif up:
        rows = con.execute("SELECT uid, item_id, title, author_name FROM item WHERE author_name = ?", (up,)).fetchall()
    else:
        return []
    return [dict(r) for r in rows]

def survey(con, uids):
    if not uids:
        return {}
    qm = ",".join("?" * len(uids))
    out = {}
    for t in ("item",) + _TABLES:
        col = "uid" if t == "item" else "item_uid"
        try:
            out[t] = con.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (t, col, qm), uids).fetchone()[0]
        except sqlite3.Error:
            pass
    try:
        blk = [r[0] for r in con.execute("SELECT id FROM content_block WHERE item_uid IN (%s)" % qm, uids)]
        if blk:
            out["vec_state"] = con.execute("SELECT COUNT(*) FROM vec_state WHERE block_id IN (%s)" % ",".join("?" * len(blk)), blk).fetchone()[0]
    except sqlite3.Error:
        pass
    return out

def files_of(vault, bvs):
    d = os.path.join(vault, "B站")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d)
            if f.endswith(".md") and any(bv in f for bv in bvs)]

def purge_local(con, uids, vault, bvs, backup=True):
    if not uids:
        return {}
    if backup:
        db = con.execute("PRAGMA database_list").fetchone()[2]
        if db and os.path.exists(db):
            bak = "%s.bak-purge-%s" % (db, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(db, bak)
            print("  备份 -> %s" % bak)
    qm = ",".join("?" * len(uids))
    out = {}

    try:
        blk = [r[0] for r in con.execute(
            "SELECT id FROM content_block WHERE item_uid IN (%s)" % qm, uids)]
        if blk:
            out["vec_state"] = con.execute(
                "DELETE FROM vec_state WHERE block_id IN (%s)"
                % ",".join("?" * len(blk)), blk).rowcount
    except sqlite3.Error:
        pass
    for t in _TABLES:
        out[t] = con.execute("DELETE FROM %s WHERE item_uid IN (%s)" % (t, qm), uids).rowcount
    out["item"] = con.execute("DELETE FROM item WHERE uid IN (%s)" % qm, uids).rowcount
    con.commit()
    gone = 0
    for p in files_of(vault, bvs):
        try:
            os.remove(p)
            gone += 1
        except OSError:
            pass
    out["files"] = gone
    return out

def bv_to_aid(cred, bvs):
    from bili_digest.ingest.bili_shelf import request_json, Throttle
    th = Throttle()
    out = []
    for bv in bvs:
        try:
            d = request_json("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bv, cred, throttle=th)
            if d.get("code") == 0:
                out.append((bv, d["data"]["aid"]))
        except Exception:
            pass
    return out

def unfav(cred, pairs, media_id):
    from bili_digest.ingest.bili_shelf import request_json, Throttle
    th = Throttle()
    ok, fail = 0, []
    for bv, aid in pairs:
        try:
            d = request_json("https://api.bilibili.com/x/v3/fav/resource/deal", cred,
                             method="POST", throttle=th,
                             data={"rid": aid, "type": 2, "add_media_ids": "",
                                   "del_media_ids": media_id, "csrf": cred["bili_jct"]})
            if d.get("code") == 0:
                ok += 1
            else:
                fail.append((bv, d.get("code"), d.get("message")))
        except Exception as e:
            fail.append((bv, type(e).__name__, str(e)[:50]))
    return ok, fail

def run(a) -> int:
    import argparse as _ap
    return _run(a)

def _run(a) -> int:
    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    rows = find(con, a.up, a.bv)
    if not rows:
        print("  没找到条目")
        con.close()
        return 1
    uids = [r["uid"] for r in rows]
    bvs = [r["item_id"] for r in rows]
    print("  找到 %d 条：" % len(rows))
    for r in rows[:12]:
        print("    %-14s %-16s %s" % (r["item_id"], (r["author_name"] or "")[:16], (r["title"] or "")[:40]))
    if len(rows) > 12:
        print("    ... 另 %d 条" % (len(rows) - 12))
    print()
    print("  会动到：")
    for k, v in survey(con, uids).items():
        print("    %-18s %d 行" % (k, v))
    print("    %-18s %d 个（按 BV 匹配文件名）" % ("笔记文件", len(files_of(a.vault, bvs))))
    if not a.apply:
        print()
        print("  dry - 加 --apply 才真删")
        con.close()
        return 0
    print()
    print("  本地删除：")
    for k, v in purge_local(con, uids, a.vault, bvs).items():
        print("    %-18s %d" % (k, v))
    print("  索引：跑 python -m bili_digest index 回收孤立向量")
    con.close()
    if a.remote:
        if not a.media_id:
            print("  [!] 需要 --media-id，跳过远端")
            return 0
        from bili_digest.ingest.fetcher import load_credential
        cred = load_credential()
        if not cred.get("bili_jct"):
            print("  [!] 缺 bili_jct，跳过远端")
            return 0
        print()
        print("  取消 B站收藏（media_id=%s）：" % a.media_id)
        ok, fail = unfav(cred, bv_to_aid(cred, bvs), a.media_id)
        print("    成功 %d · 失败 %d" % (ok, len(fail)))
        for x in fail[:5]:
            print("    ", x)
    return 0
