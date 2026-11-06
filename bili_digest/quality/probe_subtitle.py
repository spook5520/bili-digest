
import json
import sqlite3
import sys

def pending(con: sqlite3.Connection, since_days: int = 0) -> list:
    q = ("SELECT i.uid, i.item_id, i.duration_sec FROM item i "
         "WHERE i.platform='bilibili' "
         "AND NOT EXISTS(SELECT 1 FROM content_block WHERE item_uid=i.uid "
         "               AND kind IN ('subtitle_seg','body'))")
    if since_days:
        from bili_digest.retrieve.temporal import now_sec
        q += " AND i.created_at >= %d" % (now_sec() - since_days * 86400)
    return list(con.execute(q))

def probe(rows: list, verbose: bool = True) -> dict:
    import time as _t
    from bili_digest.adapters import adapters as ad
    from bili_digest.ingest import fetcher
    ap = ad.get_adapter("bilibili")

    ck = fetcher.cookie_header(fetcher.load_credential())
    hit, dead, miss, err = [], [], 0, 0
    t0 = _t.time()
    for i, r in enumerate(rows, 1):
        bv = r[1] if not hasattr(r, "keys") else r["item_id"]
        uid = r[0] if not hasattr(r, "keys") else r["uid"]
        dur = r[2] if not hasattr(r, "keys") else r["duration_sec"]
        try:

            _m = ap.fetch_item(bv)
            _ex = _m.get("extras") or {}
            meta = dict(_m, aid=_ex.get("aid"), cid=_ex.get("cid"), bvid=bv)
            subs = fetcher.fetch_subtitles_all(meta, ck)
            n = sum(len(v) for v in (subs or {}).values()) if isinstance(subs, dict) else 0
            if n:
                hit.append({"bv": bv, "uid": uid, "n": n, "dur": dur})
                if verbose:
                    print("  ✓ %s  %d 条字幕" % (bv, n), flush=True)
            else:
                miss += 1
        except SystemExit as e:
            dead.append({"bv": bv, "code": str(e)[:40]})
        except Exception as e:

            err += 1
            if verbose and err <= 3:
                print("  ! %s %s: %s" % (bv, type(e).__name__, str(e)[:60]),
                      flush=True)
        if verbose and i % 100 == 0:
            print("  ... %d/%d 命中 %d %.0fs" % (i, len(rows), len(hit), _t.time() - t0),
                  flush=True)
    return {"total": len(rows), "hit": hit, "dead": dead,
            "miss": miss, "err": err, "seconds": round(_t.time() - t0, 1)}

def main() -> int:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    since = 0
    if "--since-days" in argv:
        since = int(argv[argv.index("--since-days") + 1])
    con = connect(DB)
    rows = pending(con, since)
    print("无正文条目: %d 条" % len(rows), flush=True)
    r = probe(rows)
    print()
    print("===== 结果 =====")
    print("探测 %d：有字幕 %d (%.0f%%) · 无 %d · 稿件失效 %d · 出错 %d · %.0fs" %
          (r["total"], len(r["hit"]), 100 * len(r["hit"]) / max(1, r["total"]),
           r["miss"], len(r["dead"]), r["err"], r["seconds"]))
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        json.dump(r, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("->", out)

    try:
        from bili_digest.retrieve.temporal import now_sec
        con.execute(
            "INSERT INTO job_log(ts,scope,step,status,detail) VALUES(?,?,?,?,?)",
            (now_sec(), "health", "probe_subtitle", "ok",
             json.dumps({"n": r["total"], "hit": len(r["hit"]),
                         "dead": len(r["dead"]), "sec": r["seconds"]},
                        ensure_ascii=False)))
        con.commit()
    except Exception:
        pass
    con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
