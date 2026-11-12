
import sqlite3
import sys
import time

def pending(con: sqlite3.Connection, limit: int = 0) -> list:
    q = ("SELECT i.uid, i.item_id FROM item i WHERE i.platform='bilibili' "
         "AND NOT EXISTS(SELECT 1 FROM content_block WHERE item_uid=i.uid "
         "               AND kind IN ('subtitle','subtitle_seg','body')) "
         "ORDER BY i.item_id")
    if limit:
        q += " LIMIT %d" % limit
    return [(r[0], r[1]) for r in con.execute(q)]

def backfill(con, rows: list, verbose: bool = True) -> dict:
    from bili_digest.adapters import adapters as ad
    from bili_digest.ingest import fetcher
    from bili_digest.schema import core_schema as cs
    ap = ad.get_adapter("bilibili")
    ck = fetcher.cookie_header(fetcher.load_credential())
    wrote = skipped = dead = err = 0
    mismatch = 0
    blocks = 0
    t0 = time.time()
    for i, (uid, bv) in enumerate(rows, 1):
        try:
            m = ap.fetch_item(bv)
            ex = m.get("extras") or {}
            meta = dict(m, aid=ex.get("aid"), cid=ex.get("cid"), bvid=bv)
            if not meta.get("cid"):
                skipped += 1
                continue
            subs = fetcher.fetch_subtitles_all(meta, ck) or {}

            lines = []
            for _no, box in (subs.items() if isinstance(subs, dict) else []):
                if isinstance(box, dict):
                    lines.extend(box.get("lines") or [])
            if not lines:
                skipped += 1
                continue

            _dur = con.execute("SELECT duration_sec FROM item WHERE uid=?", (uid,)).fetchone()
            _dur = (_dur[0] if _dur else 0) or 0
            _max_off = 0.0
            for _ln2 in lines:
                if isinstance(_ln2, dict):
                    try:
                        _max_off = max(_max_off, float(_ln2.get("from") or 0))
                    except (TypeError, ValueError):
                        pass
            if _dur and _max_off > _dur * 1.2:
                mismatch += 1
                if verbose and mismatch <= 5:
                    print("  ✗ %s 字幕错配：字幕到 %.0fs，视频只有 %ds —— 丢弃" % (bv, _max_off, _dur), flush=True)
                continue

            n = 0
            for _i, _ln in enumerate(lines):
                _txt = (_ln.get("text") if isinstance(_ln, dict) else str(_ln)) or ""
                _txt = _txt.strip()
                if not _txt:
                    continue
                _off = None
                if isinstance(_ln, dict):
                    _off = _ln.get("from")
                if cs.add_block(con, uid, "subtitle", _txt,
                                offset_sec=_off, seq=_i) is not None:
                    n += 1
            con.commit()
            blocks += n
            wrote += 1
            if verbose and wrote <= 5:
                print("  ✓ %s  %d 条" % (bv, n), flush=True)
        except SystemExit:
            dead += 1
        except Exception:
            err += 1
        if verbose and i % 50 == 0:
            print("  ... %d/%d 写入 %d 条 (%d 块) %.0fs" %
                  (i, len(rows), wrote, blocks, time.time() - t0), flush=True)
    return {"total": len(rows), "wrote": wrote, "skipped": skipped,
            "dead": dead, "err": err, "blocks": blocks,
            "mismatch": mismatch,
            "seconds": round(time.time() - t0, 1)}

def main() -> int:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    limit = 50
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    con = connect(DB)
    rows = pending(con, limit)
    print("待补字幕: %d 条" % len(rows))
    if "--dry" in argv:
        for uid, bv in rows[:10]:
            print("  ", bv)
        return 0
    r = backfill(con, rows)
    print()
    print("补入 %d 条 / %d 块 · 跳过 %d · 字幕错配 %d · 失效 %d · 出错 %d · %.0fs"
          % (r["wrote"], r["blocks"], r["skipped"], r["mismatch"],
             r["dead"], r["err"], r["seconds"]))
    if r["mismatch"]:
        print("  ★ 字幕错配 = B站给这个视频挂了别的轨道（字幕比视频长得多），整条已丢弃。")
        print("    这些条目没有章节是对的 —— 总比用错素材写出一堆错的要点好。")
    if r["blocks"]:
        print("下一步：python -m bili_digest.distill.seg_boundary --build --incremental")
    con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
