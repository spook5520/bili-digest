#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).parent

from classify import norm_tags
from core_schema import (KIND_COMMENT, KIND_DANMAKU, KIND_META, KIND_SUBTITLE,
                         add_block, add_tag, connect, is_blocked, load_blocklist,
                         log_job, make_uid, upsert_item)
from lifecycle import decide

SUB_WINDOW_SEC = 30

class Adapter:
    platform = "generic"

    def fetch_item(self, item_id: str) -> dict:
        raise NotImplementedError

    def fetch_blocks(self, item_id: str) -> list[dict]:
        raise NotImplementedError

    def search(self, query: str, page: int = 1) -> list[dict]:
        raise NotImplementedError

    def list_collections(self) -> list[dict]:
        raise NotImplementedError

    def fetch_collection(self, collection_id: str, page: int = 1) -> list[dict]:
        raise NotImplementedError

    def list_followings(self, uid: str = "", limit: int = 50) -> list[dict]:
        raise NotImplementedError

    def list_uploads(self, author_uid: str, page: int = 1) -> list[dict]:
        raise NotImplementedError

class BilibiliAdapter(Adapter):

    platform = "bilibili"

    def __init__(self, cred: dict | None = None):
        import bili_digest as bd
        import bili_shelf as bs
        self.bd, self.bs = bd, bs
        self.cred = cred or bd.load_credential()
        self.cookie = bd.cookie_header(self.cred)
        self.throttle = bs.Throttle()

    def fetch_item(self, item_id: str) -> dict:
        meta = self.bd.fetch_meta(item_id, self.cookie)
        return {
            "platform": self.platform,
            "item_id": meta.get("bvid") or item_id,
            "title": meta.get("title"),
            "url": f"https://www.bilibili.com/video/{meta.get('bvid') or item_id}",
            "author_uid": str(meta.get("owner_mid") or ""),
            "author_name": meta.get("owner"),
            "published_at": meta.get("pubdate"),
            "duration_sec": meta.get("duration"),
            "cover": meta.get("pic"),
            "language": "zh",
            "extras": {
                "aid": meta.get("aid"),
                "cid": meta.get("cid"),
                "tname": meta.get("tname"),
                "desc": meta.get("desc"),
                "tags": norm_tags(meta.get("tags")),
                "view": meta.get("stat_view") or meta.get("view"),
                "danmaku": meta.get("stat_danmaku") or meta.get("danmaku"),
                "reply": meta.get("stat_reply") or meta.get("reply"),
                "favorite": meta.get("stat_favorite") or meta.get("favorite"),

                "cid": meta.get("cid"),
                "pages": meta.get("pages") or [],
            },
        }

    def fetch_blocks(self, item_id: str) -> list[dict]:
        bd = self.bd
        meta = bd.fetch_meta(item_id, self.cookie)
        bvid = meta.get("bvid") or item_id
        out = []

        desc = (meta.get("desc") or "").strip()
        meta_text = f"{meta.get('title') or ''}\n{desc}".strip()
        if meta_text:
            out.append({"kind": KIND_META, "text": meta_text, "offset_sec": None,
                        "seq": 0, "author": None,
                        "weight": (meta.get("stat") or {}).get("view")})

        try:

            subs = bd.fetch_subtitle(meta, self.cookie) or []
        except Exception:
            subs = []

        cur, buf = None, []
        for s in subs:
            t, txt = s.get("from"), s.get("text")
            if txt is None:
                continue
            if cur is None:
                cur = t
            if t - cur > SUB_WINDOW_SEC and buf:
                out.append({"kind": KIND_SUBTITLE, "text": "".join(buf),
                            "offset_sec": float(cur), "seq": None,
                            "author": None, "weight": None})
                cur, buf = t, []
            buf.append(txt)
        if buf:
            out.append({"kind": KIND_SUBTITLE, "text": "".join(buf),
                        "offset_sec": float(cur), "seq": None,
                        "author": None, "weight": None})

        try:
            dms = bd.fetch_danmaku(meta, self.cookie) or []
        except Exception:
            dms = []

        for d in dms:
            t = d.get("t") or d.get("progress") or d.get("t_ms") or 0
            txt = (d.get("text") or d.get("content") or "").strip()
            if txt:
                out.append({"kind": KIND_DANMAKU, "text": txt,
                            "offset_sec": t / 1000.0, "seq": None,
                            "author": d.get("midHash"), "weight": None})

        try:
            reps = bd.fetch_replies(meta, self.cookie) or []
        except Exception:
            reps = []
        for i, r in enumerate(reps):
            txt = (r.get("text") or "").strip()
            if txt:
                out.append({"kind": KIND_COMMENT, "text": txt, "offset_sec": None,
                            "seq": i, "author": r.get("user"),
                            "weight": r.get("likes")})
        return out

    def search(self, query: str, page: int = 1) -> list[dict]:
        rs = self.bs.search_video(self.cred, query, page, self.throttle)
        out = []
        for v in rs:
            out.append({
                "platform": self.platform,
                "item_id": v.get("bvid"),
                "title": v.get("title"),
                "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                "author_uid": str(v.get("mid") or ""),
                "author_name": v.get("author"),
                "published_at": v.get("pubdate"),
                "duration_sec": self.bs.dur_to_sec(v.get("duration")),
                "extras": {"play": v.get("play"), "danmaku": v.get("danmaku"),
                           "description": v.get("description"),
                           "search_tag": v.get("tag")},
            })
        return out

    def list_collections(self) -> list[dict]:
        folders = self.bs.list_folders(self.cred, self.throttle)
        return [{"platform": self.platform,
                 "id": make_uid(self.platform, str(f["id"])),
                 "item_id": str(f["id"]),
                 "name": f["title"], "total": f.get("media_count", 0),
                 "extras": {"attr": f.get("attr"), "fav_time": f.get("fav_time")}}
                for f in folders]

    def list_followings(self, uid: str = "", limit: int = 50) -> list[dict]:

        import os
        uid = str(uid
                  or self.cred.get("dedeuserid")
                  or os.environ.get("KB_BILI_DEDEUSERID")
                  or "")
        if not uid:
            return []
        rows, pn = [], 1
        while len(rows) < limit and pn <= 4:
            d = self.bs.request_json(
                f"https://api.bilibili.com/x/relation/followings"
                f"?vmid={uid}&ps=50&pn={pn}",
                self.cred, throttle=self.throttle)
            if d.get("code") != 0:
                break
            lst = (d.get("data") or {}).get("list") or []
            if not lst:
                break
            rows.extend(lst)
            pn += 1
        return [{"platform": self.platform,
                 "author_uid": str(u.get("mid")),
                 "author_name": u.get("uname"),
                 "sign": u.get("sign")} for u in rows[:limit]]

    def list_uploads(self, author_uid: str, page: int = 1) -> list[dict]:
        mid = str(author_uid).split(":")[-1]

        url = self.bs.wbi_sign(
            f"https://api.bilibili.com/x/space/wbi/arc/search"
            f"?mid={mid}&ps=30&pn={page}&order=pubdate",
            self.cred, self.throttle)
        d = self.bs.request_json(
            url, self.cred, throttle=self.throttle,
            referer=f"https://space.bilibili.com/{mid}")
        if d.get("code") != 0:
            return []
        vl = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
        out = []
        for v in vl:
            dur = v.get("length") or ""
            out.append({
                "platform": self.platform,
                "item_id": v.get("bvid"),
                "title": v.get("title"),
                "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                "author_uid": str(mid),
                "author_name": v.get("author"),
                "published_at": v.get("created"),
                "duration_sec": self.bs.dur_to_sec(dur) if dur else 0,
                "extras": {"play": v.get("play"), "comment": v.get("comment")},
            })
        return out

    def fetch_collection(self, collection_id: str, page: int = 1) -> list[dict]:
        fid = collection_id.split(":")[-1]
        url = (f"https://api.bilibili.com/x/v3/fav/resource/list"
               f"?media_id={fid}&pn={page}&ps=20&platform=web&order=mtime")
        d = self.bs.request_json(url, self.cred, throttle=self.throttle)
        out = []
        for m in (d.get("data") or {}).get("medias") or []:
            if not m.get("bvid") or m.get("title") == "已失效视频":
                continue
            up = m.get("upper") or {}
            ci = m.get("cnt_info") or {}
            out.append({
                "platform": self.platform,
                "item_id": m["bvid"],
                "title": m.get("title"),
                "url": f"https://www.bilibili.com/video/{m['bvid']}",
                "author_uid": str(up.get("mid") or ""),
                "author_name": up.get("name"),
                "published_at": m.get("pubtime"),
                "saved_at": m.get("fav_time"),
                "duration_sec": m.get("duration"),
                "cover": m.get("cover"),
                "extras": {"intro": m.get("intro"), "attr": m.get("attr"),
                           "type": m.get("type"),
                           "play": ci.get("play"), "danmaku": ci.get("danmaku")},
            })
        return out

class YouTubeAdapter(Adapter):
    platform = "youtube"

    def fetch_item(self, item_id):
        raise NotImplementedError("YouTube 适配器待实现")

    def fetch_blocks(self, item_id):
        raise NotImplementedError

    def search(self, query, page=1):
        raise NotImplementedError

    def list_collections(self):
        return []

    def fetch_collection(self, collection_id, page=1):
        raise NotImplementedError

class ZhihuAdapter(Adapter):
    platform = "zhihu"

    def fetch_item(self, item_id):
        raise NotImplementedError("知乎适配器待实现")

    def fetch_blocks(self, item_id):
        raise NotImplementedError

    def search(self, query, page=1):
        raise NotImplementedError

    def list_collections(self):
        return []

    def fetch_collection(self, collection_id, page=1):
        raise NotImplementedError

ADAPTERS = {
    "bilibili": BilibiliAdapter,
    "youtube": YouTubeAdapter,
    "zhihu": ZhihuAdapter,
}

def get_adapter(platform: str, cred: dict | None = None) -> Adapter:
    cls = ADAPTERS.get(platform)
    if not cls:
        raise SystemExit(f"未知平台 {platform}，已注册 {list(ADAPTERS)}")
    return cls(cred)

def import_item(con: sqlite3.Connection, adapter: Adapter, item_id: str,
                rules: dict | None = None, fetch_content: bool = True) -> dict:
    rules = rules or load_blocklist(con)
    meta = adapter.fetch_item(item_id)

    tags = (meta.get("extras") or {}).get("tags") or []
    partition = (meta.get("extras") or {}).get("tname")

    blocked, why = is_blocked(rules, item_id=meta["item_id"],
                              author_uid=meta.get("author_uid"),
                              partition=partition, tags=tags,
                              text=meta.get("title"))
    if blocked:
        log_job(con, f"{adapter.platform}:{item_id}", "import", "skip", why)
        return {"skipped": True, "reason": why}

    uid = upsert_item(
        con, platform=meta["platform"], item_id=meta["item_id"],
        title=meta.get("title"), url=meta.get("url"),
        author_uid=meta.get("author_uid"), author_name=meta.get("author_name"),
        published_at=meta.get("published_at"), saved_at=meta.get("saved_at"),
        duration_sec=meta.get("duration_sec"), language=meta.get("language"),
        cover=meta.get("cover"), extras=meta.get("extras"))
    for t in tags:
        add_tag(con, uid, t, kind="system", source=adapter.platform)

    n_blocks = 0
    if fetch_content:
        for b in adapter.fetch_blocks(meta["item_id"]):
            if is_blocked(rules, text=b.get("text"))[0]:
                continue
            if add_block(con, uid, b["kind"], b["text"],
                         offset_sec=b.get("offset_sec"), seq=b.get("seq"),
                         author=b.get("author"), weight=b.get("weight")):
                n_blocks += 1

    d = decide(con, uid, rules, apply=True,
               has_raw=any(True for _ in con.execute(
                   "SELECT 1 FROM content_block WHERE item_uid=? AND kind=? LIMIT 1",
                   (uid, KIND_SUBTITLE))))
    con.commit()
    log_job(con, f"{adapter.platform}:{item_id}", "import", "ok",
            f"blocks={n_blocks} tier={d.get('tier')}")
    return {"uid": uid, "blocks": n_blocks, "tier": d.get("tier"),
            "score": d.get("score")}

def import_digest(con: sqlite3.Connection, raw_db: Path,
                  item_meta: dict | None = None,
                  rules: dict | None = None) -> dict:
    rules = rules or load_blocklist(con)
    raw = sqlite3.connect(str(raw_db))
    raw.row_factory = sqlite3.Row

    md = {r["k"]: r["v"] for r in raw.execute("SELECT k,v FROM meta")}
    if not md:
        raw.close()
        return {"error": "raw.sqlite 无 meta 记录"}

    bvid = md.get("bvid")
    if not bvid:
        raw.close()
        return {"error": "meta 缺 bvid"}

    def _i(k):
        try:
            return int(float(md.get(k) or 0))
        except (TypeError, ValueError):
            return None

    blocked, why = is_blocked(rules, item_id=bvid,
                              author_uid=md.get("owner_mid"),
                              tags=(md.get("tags") or "").split(",") if md.get("tags") else None,
                              partition=md.get("tname"),
                              text=md.get("title"))
    if blocked:
        raw.close()
        return {"skipped": True, "reason": why}

    uid = upsert_item(
        con, platform="bilibili", item_id=bvid,
        title=md.get("title"),
        url=f"https://www.bilibili.com/video/{bvid}",
        author_uid=md.get("owner_mid"),
        author_name=md.get("owner"),
        published_at=_i("pubdate"), duration_sec=_i("duration"),
        language="zh",
        extras={"aid": _i("aid"), "cid": _i("cid"),
                "tname": md.get("tname"), "desc": md.get("desc"),
                "view": _i("view"), "like": _i("like"),
                "reply_count": _i("reply"), "danmaku_count": _i("danmaku")})

    mt = f"{md.get('title') or ''}\n{(md.get('desc') or '').strip()}".strip()
    n_meta = 1 if add_block(con, uid, KIND_META, mt, seq=0,
                            weight=_i("view")) else 0

    counts = {"meta": n_meta, "subtitle": 0, "danmaku": 0, "comment": 0}

    for r in raw.execute("SELECT t_from,text FROM subtitle ORDER BY t_from"):
        if add_block(con, uid, KIND_SUBTITLE, r["text"] or "",
                     offset_sec=float(r["t_from"] or 0)):
            counts["subtitle"] += 1

    for r in raw.execute("SELECT t_ms,text,uid FROM danmaku"):
        if add_block(con, uid, KIND_DANMAKU, r["text"] or "",
                     offset_sec=(r["t_ms"] or 0) / 1000.0, author=r["uid"]):
            counts["danmaku"] += 1

    for r in raw.execute("SELECT user,text,likes FROM reply"):
        if add_block(con, uid, KIND_COMMENT, r["text"] or "",
                     author=r["user"], weight=r["likes"]):
            counts["comment"] += 1

    try:
        for r in raw.execute("SELECT user,text,likes FROM reply_sub"):
            if add_block(con, uid, KIND_COMMENT, r["text"] or "",
                         author=r["user"], weight=r["likes"]):
                counts["comment"] += 1
    except sqlite3.OperationalError:
        pass

    raw.close()
    has_raw = counts["subtitle"] > 0
    d = decide(con, uid, rules, apply=True, has_raw=has_raw)
    con.commit()
    return {"uid": uid, "blocks": counts, "tier": d.get("tier"),
            "score": d.get("score"), "coverage": d.get("freshness_kind")}

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="平台适配与导入")
    ap.add_argument("db", nargs="?", default="knowledge.db")
    ap.add_argument("--import-digest", metavar="RAW_SQLITE",
                    help="导入 bili_digest 的 raw.sqlite")
    ap.add_argument("--import-all", metavar="DIGEST_DIR",
                    help="批量导入目录下所有 raw.sqlite")
    ap.add_argument("--platforms", action="store_true", help="列出已注册平台")
    a = ap.parse_args()

    if a.platforms:
        for k, v in ADAPTERS.items():
            state = "已实现" if v.__module__ else ""
            try:
                get_adapter(k)
                state = "已实现"
            except Exception:
                state = "占位"
            print(f"  {k:<10} {v.__name__:<20} {state}")
        raise SystemExit

    c = connect(a.db)
    if a.import_digest:
        print(import_digest(c, Path(a.import_digest)))
    elif a.import_all:
        base = Path(a.import_all)
        tot = {}
        for d in sorted(base.iterdir()):
            raw = d / "raw.sqlite"
            if raw.exists():
                r = import_digest(c, raw)
                print(f"  {d.name}: {r}")
        print(f"  汇总: {tot}")
    else:
        print(__doc__)
    c.close()
