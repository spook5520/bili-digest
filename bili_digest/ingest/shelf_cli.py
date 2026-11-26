#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import sqlite3
import sys

def _utf8() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

def _kb_ids(kb_path) -> set:
    con = sqlite3.connect(str(kb_path))
    try:
        return {r[0] for r in con.execute("select item_id from item where platform='bilibili'")}
    finally:
        con.close()

def shelf_ids(con: sqlite3.Connection) -> set:
    return {r[0] for r in con.execute("select bvid from video")}

def missing(con: sqlite3.Connection, known: set, limit: int = 0, rules: dict | None = None) -> list:
    rows = con.execute(
        "select bvid, title, upper, pubtime, duration, fav_time, upper_mid from video"
        " order by fav_time desc"
    ).fetchall()
    out = []
    for b, t, u, p, d, f, mid in rows:
        if b in known:
            continue
        if rules:
            from bili_digest.schema.core_schema import is_blocked
            hit, _why = is_blocked(rules, item_id=b, author_uid=str(mid or ""))
            if hit:
                continue
        out.append({"bvid": b, "title": t or "", "upper": u or "",
                    "pubtime": p, "duration": d, "fav_time": f})
    return out[:limit] if limit else out

def _triage(kb_path) -> dict:
    con = sqlite3.connect(str(kb_path), timeout=20)
    try:
        tiers = {r[0]: r[1] for r in
                 con.execute("SELECT tier, COUNT(*) FROM lifecycle GROUP BY tier")}

        need = con.execute("""
            SELECT COUNT(*) FROM lifecycle l
            WHERE l.tier='knowledge'
              AND NOT EXISTS(SELECT 1 FROM derived d
                             WHERE d.item_uid=l.item_uid AND d.kind='summary')
            """).fetchone()[0]

        from bili_digest.quality.lifecycle import DISCARD_THRESHOLD as _DT
        disc = [{"uid": r[0], "score": round(r[1] or 0, 3),
                 "title": (r[2] or "")[:56]}
                for r in con.execute("""
                    SELECT l.item_uid, l.score, i.title
                    FROM lifecycle l LEFT JOIN item i ON i.uid=l.item_uid
                    WHERE l.score < ?
                      AND l.tier != 'discarded'
                      AND l.item_uid NOT LIKE 'canary:%'
                    ORDER BY l.score ASC LIMIT 10""", (_DT,))]

        try:
            from bili_digest.quality.gate import plan as _gate_plan

            saved_factory = con.row_factory
            con.row_factory = sqlite3.Row
            try:
                verdicts = _gate_plan(con, "knowledge").get("verdicts") or {}
            finally:
                con.row_factory = saved_factory
        except Exception:
            verdicts = {}
        return {"tiers": tiers, "need_distill": need, "discardable": disc,
                "verdicts": verdicts}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:80])}
    finally:
        con.close()

def cmd_new(args) -> int:
    from bili_digest.ingest import bili_shelf as bs
    from bili_digest import paths

    if args.sync:

        import contextlib
        import sys as _sys
        cred = bs.load_credential()

        TOP_UPS = 30
        with contextlib.redirect_stdout(_sys.stderr):
            bs.do_folders(cred)
            bs.do_sync(cred, None)
            _con = bs.connect()
            paused = ""
            try:
                _thr = bs.Throttle()
                bs.sync_toview(cred, _con, _thr)
                mids = [str(r[0]) for r in _con.execute(
                    "SELECT upper_mid, COUNT(*) n FROM video "
                    "WHERE upper_mid IS NOT NULL AND upper_mid > 0 "
                    "GROUP BY upper_mid ORDER BY -n LIMIT ?", (TOP_UPS,))]
                if mids:
                    bs.sync_up_articles(cred, _con, mids, _thr)
                    bs.sync_seasons(cred, _con, mids, max_per_season=50, throttle=_thr)
            except bs.SyncPaused as e:

                paused = str(e)
                print("  [paused] " + paused, file=_sys.stderr)
            finally:
                _con.close()

    con = bs.connect()
    try:
        known = _kb_ids(paths.HERE / "knowledge.db")

        rules = None
        try:
            from bili_digest.schema.core_schema import load_blocklist
            _kb = sqlite3.connect(str(paths.HERE / "knowledge.db"))
            try:
                rules = load_blocklist(_kb)
            finally:
                _kb.close()
        except Exception:
            rules = None
        found = missing(con, known, args.limit, rules)
        total = con.execute("select count(*) from video").fetchone()[0]
    finally:
        con.close()
    out = {
        "shelf": total,
        "known": len(known),
        "missing": len(found),
        "items": found,
        "synced": bool(args.sync),
    }
    if args.sync:
        out["paused"] = paused

    if getattr(args, "triage", False):
        out["triage"] = _triage(paths.HERE / "knowledge.db")
    print(json.dumps(out, ensure_ascii=False))
    return 0

def main() -> int:
    _utf8()
    ap = argparse.ArgumentParser(description="Is the local library behind the Bilibili shelf?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="shelf entries the local library does not have")
    p.add_argument("--sync", action="store_true", help="refresh shelf.db first (slow: throttled)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--triage", action="store_true",
                   help="附带分级结果（哪些该提炼、哪些可清）")
    p.set_defaults(func=cmd_new)
    a = ap.parse_args()
    return a.func(a) or 0

if __name__ == "__main__":
    sys.exit(main())
