#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from bili_digest.paths import SHELF_DB as DB_PATH

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
STATE_PATH = BASE_DIR / "shelf-state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BILI_HIVE_ENV = "KB_BILI_HIVE"
BILI_SESSDATA_ENV = "KB_BILI_SESSDATA"
DEBUG = False

RETRY_BASE_MS = 2_000
RETRY_MAX_MS = 300_000
RISK_BASE_MS = 15 * 60_000
RISK_MAX_MS = 2 * 60 * 60_000

def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[dbg] {msg}", file=sys.stderr)

def load_credential(explicit: str | None = None) -> dict:
    from bili_digest.ingest import fetcher as _bd
    import os

    if explicit:
        return {"sessdata": explicit, "bili_jct": "", "dedeuserid": ""}

    src = os.environ.get(BILI_HIVE_ENV)
    if src:
        hive = Path(src).expanduser()
        if not hive.exists():
            raise SystemExit(f"{BILI_HIVE_ENV} 指向的文件不存在：{hive}")
        return _bd._parse_hive(hive)

    sess = os.environ.get(BILI_SESSDATA_ENV)
    if sess:
        return {"sessdata": sess, "bili_jct": "", "dedeuserid": ""}

    raise SystemExit(
        "未提供登录态。三种方式任选：\n"
        "  1) 命令行加 --sessdata <你的 SESSDATA>\n"
        f"  2) export {BILI_SESSDATA_ENV}=<你的 SESSDATA>\n"
        f"  3) export {BILI_HIVE_ENV}=/path/to/account.hive"
        "（用客户端的话，指向它的登录态文件）")

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_wbi_cache: dict = {}

def wbi_mixin_key(cred: dict, throttle: "Throttle | None" = None,
                  max_age: int = 3600) -> str:
    import time as _t
    now = _t.time()
    if _wbi_cache.get("key") and now - _wbi_cache.get("at", 0) < max_age:
        return _wbi_cache["key"]
    d = request_json("https://api.bilibili.com/x/web-interface/nav",
                     cred, throttle=throttle)
    wbi = (d.get("data") or {}).get("wbi_img") or {}

    def _stem(u: str) -> str:
        return (u or "").rsplit("/", 1)[-1].split(".")[0]

    raw = _stem(wbi.get("img_url")) + _stem(wbi.get("sub_url"))
    if len(raw) < 64:
        return ""
    key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    _wbi_cache.update({"key": key, "at": now})
    return key

def wbi_sign(url: str, cred: dict, throttle: "Throttle | None" = None) -> str:
    import hashlib
    import time as _t
    import urllib.parse as _up

    mixin = wbi_mixin_key(cred, throttle)
    if not mixin:
        return url
    parts = _up.urlsplit(url)
    q = dict(_up.parse_qsl(parts.query, keep_blank_values=True))
    q["wts"] = str(int(_t.time()))
    items = sorted(q.items())
    query = _up.urlencode(items)
    q["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    new_query = _up.urlencode(sorted(q.items()))
    return _up.urlunsplit((parts.scheme, parts.netloc, parts.path,
                           new_query, parts.fragment))

def cookie(cred: dict) -> str:
    p = [f"SESSDATA={cred['sessdata']}"]
    if cred.get("bili_jct"):
        p.append(f"bili_jct={cred['bili_jct']}")
    if cred.get("dedeuserid"):
        p.append(f"DedeUserID={cred['dedeuserid']}")
    return "; ".join(p)

class Throttle:

    def __init__(self):
        self.pages_fetched = 0
        self.videos_processed = 0
        self.slow_streak = 0
        self.last_ms = 0
        self.risk_count = 0
        self.attempt = 0

    def page_gap_ms(self, folder_media_count: int) -> int:
        base = 600
        base += min(folder_media_count // 200, 6) * 320
        base += self.slow_streak * 500
        return int(base * random.uniform(0.85, 1.15))

    def folder_gap_ms(self) -> int:
        return int(random.uniform(1_800, 3_200))

    def cooldown_policy(self) -> tuple[int, int]:
        if self.videos_processed < 200:
            return (200, 22_000)
        if self.videos_processed < 800:
            return (150, 35_000)
        return (100, 50_000)

    def transient_delay_ms(self, retry_after: int | None = None) -> int:
        if retry_after and retry_after > 0:
            return retry_after
        exp = min(RETRY_BASE_MS * (2 ** min(self.attempt, 20)), RETRY_MAX_MS)
        return int(exp * random.uniform(0.75, 1.25))

    def risk_delay_ms(self) -> int:
        base = min(RISK_BASE_MS * (2 ** min(self.risk_count, 3)), RISK_MAX_MS)
        return int(base * random.uniform(0.90, 1.10))

    def note_response(self, ms: int) -> None:
        self.last_ms = ms

        if ms > 1500:
            self.slow_streak = min(self.slow_streak + 1, 6)
        else:
            self.slow_streak = max(0, self.slow_streak - 1)

    def note_success(self) -> None:
        self.attempt = max(0, self.attempt - 1)

def request_json(url: str, cred: dict, method: str = "GET", data: dict | None = None,
                 throttle: Throttle | None = None, retries: int = 3,
                 referer: str | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(1, retries + 1):
        if throttle:
            throttle.attempt = attempt
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("User-Agent", UA)

            req.add_header("Referer", referer or "https://www.bilibili.com/")
            req.add_header("Cookie", cookie(cred))
            if body:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))
            ms = int((time.time() - t0) * 1000)
            if throttle:
                throttle.note_response(ms)

            code = payload.get("code", 0)
            msg = str(payload.get("message", ""))
            if code == 0:
                if throttle:
                    throttle.note_success()
                return payload

            riskish = (code in (-412, -352, -509, 412)
                       or "风控" in msg or "risk" in msg.lower()
                       or "频繁" in msg)
            if riskish and throttle:
                throttle.risk_count += 1
                wait = throttle.risk_delay_ms()
                print(f"  [!] 命中风控（code={code} {msg}），"
                      f"第 {throttle.risk_count} 次，等待 {wait/1000:.0f}s")
                time.sleep(wait / 1000)
            elif attempt < retries:
                wait = throttle.transient_delay_ms() if throttle else 2000 * attempt
                dbg(f"code={code} {msg} → 退避 {wait}ms 后重试")
                time.sleep(wait / 1000)
            else:
                return payload
        except urllib.error.HTTPError as e:
            ms = int((time.time() - t0) * 1000)
            if throttle:
                throttle.note_response(ms)
            if e.code in (412, 429) and throttle:
                throttle.risk_count += 1
                wait = throttle.risk_delay_ms()
                print(f"  [!] HTTP {e.code}，第 {throttle.risk_count} 次，"
                      f"等待 {wait/1000:.0f}s")
                time.sleep(wait / 1000)
            elif attempt < retries:
                wait = throttle.transient_delay_ms() if throttle else 2000 * attempt
                dbg(f"HTTP {e.code} → 退避 {wait}ms")
                time.sleep(wait / 1000)
            else:
                raise
        except Exception as e:
            if attempt < retries:
                wait = throttle.transient_delay_ms() if throttle else 2000 * attempt
                dbg(f"{type(e).__name__} → 退避 {wait}ms")
                time.sleep(wait / 1000)
            else:
                raise
    return {"code": -1, "message": "重试耗尽"}

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS folder (
            id          INTEGER PRIMARY KEY,
            title       TEXT,
            media_count INTEGER,
            attr        INTEGER,
            fav_time    INTEGER,
            synced_at   INTEGER
        );
        CREATE TABLE IF NOT EXISTS video (
            bvid      TEXT PRIMARY KEY,
            title     TEXT,
            intro     TEXT,
            upper_mid INTEGER,
            upper     TEXT,
            fav_time  INTEGER,
            pubtime   INTEGER,
            duration  INTEGER,
            play      INTEGER,
            danmaku   INTEGER,
            cover     TEXT,
            attr      INTEGER,
            type      INTEGER,
            fetched_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS membership (
            folder_id INTEGER,
            bvid      TEXT,
            fav_time  INTEGER,
            PRIMARY KEY (folder_id, bvid)
        );
        CREATE INDEX IF NOT EXISTS idx_mem_folder ON membership(folder_id);
        CREATE INDEX IF NOT EXISTS idx_mem_bvid ON membership(bvid);
        CREATE INDEX IF NOT EXISTS idx_video_upper ON video(upper_mid);
    """)
    return con

def save_progress(done: dict) -> None:
    STATE_PATH.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")

def load_progress() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def list_folders(cred: dict, throttle: Throttle) -> list[dict]:
    uid = cred.get("dedeuserid")
    d = request_json(
        f"https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={uid}",
        cred, throttle=throttle)
    if d.get("code") != 0:
        raise SystemExit(f"取收藏夹失败：{d.get('code')} {d.get('message')}")
    return (d.get("data") or {}).get("list") or []

def do_folders(cred: dict) -> None:
    th = Throttle()
    folders = list_folders(cred, th)
    con = connect()
    for f in folders:
        con.execute(
            "INSERT INTO folder(id,title,media_count,attr,fav_time,synced_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title, media_count=excluded.media_count, attr=excluded.attr",
            (f["id"], f["title"], f.get("media_count", 0), f.get("attr", 0),
             f.get("fav_time", 0), 0))
    con.commit()
    print(f"共 {len(folders)} 个收藏夹：")
    for f in folders:
        priv = "私密" if f.get("attr", 0) & 2 else "公开"
        print(f"  {f['id']:<12} {f['title'][:24]:<26} {f.get('media_count',0):>5} 个  {priv}")
    con.close()

def sync_folder(cred: dict, con: sqlite3.Connection, folder: dict,
                throttle: Throttle, start_page: int = 1, max_pages: int = 0) -> int:
    fid, fname = folder["id"], folder["title"]
    total = folder.get("media_count", 0)
    ps = 20
    page = start_page
    got = 0
    last_page = max(1, (total + ps - 1) // ps)

    while True:
        if max_pages and page > max_pages:
            print(f"    （达 --max-pages {max_pages}，暂停）")
            break
        url = (f"https://api.bilibili.com/x/v3/fav/resource/list"
               f"?media_id={fid}&pn={page}&ps={ps}&platform=web&order=mtime")
        d = request_json(url, cred, throttle=throttle)
        if d.get("code") != 0:
            print(f"    第 {page} 页失败：{d.get('code')} {d.get('message')}")
            break
        data = d.get("data") or {}
        medias = data.get("medias") or []
        if not medias:
            break

        for m in medias:
            bvid = m.get("bvid")
            if not bvid or m.get("title") == "已失效视频":
                continue
            up = m.get("upper") or {}
            ci = m.get("cnt_info") or {}
            con.execute(
                "INSERT INTO video(bvid,title,intro,upper_mid,upper,fav_time,pubtime,"
                "duration,play,danmaku,cover,attr,type,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bvid) DO UPDATE SET title=excluded.title, "
                "intro=excluded.intro, upper=excluded.upper, "
                "cover=excluded.cover, play=excluded.play, danmaku=excluded.danmaku, "
                "fetched_at=excluded.fetched_at",
                (bvid, m.get("title"), m.get("intro"), up.get("mid"), up.get("name"),
                 m.get("fav_time"), m.get("pubtime"), m.get("duration"),
                 ci.get("play", 0), ci.get("danmaku", 0), m.get("cover"),
                 m.get("attr", 0), m.get("type", 0), int(time.time())))
            con.execute(
                "INSERT OR REPLACE INTO membership(folder_id,bvid,fav_time) "
                "VALUES(?,?,?)", (fid, bvid, m.get("fav_time", 0)))
            got += 1
            throttle.videos_processed += 1

            threshold, delay = throttle.cooldown_policy()
            if throttle.videos_processed % threshold == 0:
                print(f"    [冷却] 已处理 {throttle.videos_processed} 个，"
                      f"休息 {delay/1000:.0f}s")
                con.commit()
                time.sleep(delay / 1000)

        con.commit()
        throttle.pages_fetched += 1
        prog = load_progress()
        prog[str(fid)] = page
        save_progress(prog)

        print(f"    第 {page}/{last_page} 页  +{len(medias)}  "
              f"（本次累计 {got}）")

        if page >= last_page:
            break
        page += 1
        gap = throttle.page_gap_ms(total)
        time.sleep(gap / 1000)

    return page

TOVIEW_FOLDER_ID = -1

def list_toview(cred: dict, throttle: "Throttle | None" = None) -> list[dict]:
    d = request_json("https://api.bilibili.com/x/v2/history/toview",
                     cred, throttle=throttle)
    if d.get("code") != 0:
        raise SystemExit(f"取稍后再看失败：{d.get('code')} {d.get('message')}")
    return ((d.get("data") or {}).get("list")) or []

def sync_toview(cred: dict, con: sqlite3.Connection,
                throttle: "Throttle | None" = None) -> int:
    items = list_toview(cred, throttle)
    con.execute(
        "INSERT INTO folder(id,title,media_count,attr,fav_time,synced_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "media_count=excluded.media_count, synced_at=excluded.synced_at",
        (TOVIEW_FOLDER_ID, "稍后再看", len(items), 0, 0, 0))
    n = 0
    for v in items:
        bv = v.get("bvid")
        if not bv:
            continue
        owner = v.get("owner") or {}
        add_at = v.get("add_at") or 0

        con.execute(
            "INSERT OR REPLACE INTO video(bvid,title,intro,upper_mid,upper,"
            "fav_time,pubtime,duration,play,type) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bv, v.get("title"), v.get("desc") or "",
             str(owner.get("mid") or ""), owner.get("name") or "",
             add_at, v.get("ctime") or 0, v.get("duration") or 0,
             (v.get("stat") or {}).get("view") or 0, 2))
        con.execute("INSERT OR REPLACE INTO membership(folder_id,bvid,fav_time) "
                    "VALUES(?,?,?)", (TOVIEW_FOLDER_ID, bv, add_at))
        n += 1
    con.commit()
    return n

ARTICLE_TYPE = 21

ARTICLES_FOLDER_ID = -2

def list_up_articles(cred: dict, mid: str, ps: int = 30,
                     throttle: "Throttle | None" = None) -> list[dict]:
    url = wbi_sign(f"https://api.bilibili.com/x/space/wbi/article"
                   f"?mid={mid}&ps={ps}&pn=1", cred, throttle)
    d = request_json(url, cred, throttle=throttle,
                     referer=f"https://space.bilibili.com/{mid}")
    if d.get("code") != 0:
        return []
    data = d.get("data") or {}
    return data.get("articles") or []

def sync_up_articles(cred: dict, con: sqlite3.Connection, mids: list[str],
                     throttle: "Throttle | None" = None) -> int:
    n = 0
    con.execute(
        "INSERT OR REPLACE INTO folder(id,title,media_count,attr,fav_time,"
        "synced_at) VALUES(?,?,?,?,?,?)",
        (ARTICLES_FOLDER_ID, "专栏", 0, 0, 0, 0))
    for mid in mids:
        for a in list_up_articles(cred, mid, throttle=throttle):
            cvid = a.get("id") or a.get("cvid")
            if not cvid:
                continue
            author = (a.get("author") or {})
            bv = f"cv{cvid}"
            con.execute(
                "INSERT OR REPLACE INTO video(bvid,title,intro,upper_mid,upper,"
                "fav_time,pubtime,duration,play,type) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (bv, a.get("title"), a.get("summary") or "",
                 str(a.get("mid") or mid), author.get("name") or "",
                 a.get("publish_time") or 0, a.get("publish_time") or 0,
                 0, (a.get("stats") or {}).get("view") or 0, ARTICLE_TYPE))

            con.execute(
                "INSERT OR REPLACE INTO membership(folder_id,bvid,fav_time) "
                "VALUES(?,?,?)",
                (ARTICLES_FOLDER_ID, bv, a.get("publish_time") or 0))
            n += 1
    con.commit()
    return n

def fetch_article_text(cred: dict, cvid: str,
                       throttle: "Throttle | None" = None) -> list[dict]:
    import re as _re
    url = f"https://api.bilibili.com/x/article/view?id={cvid}"
    d = request_json(url, cred, throttle=throttle,
                     referer=f"https://www.bilibili.com/read/cv{cvid}")
    content = ((d.get("data") or {}).get("content")) or ""
    if not content:
        return []

    content = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", content,
                      flags=_re.S | _re.I)
    parts = _re.split(r"</(?:p|div|h[1-6]|li|br)\s*/?>|<br\s*/?>", content,
                      flags=_re.I)
    out = []
    for i, p in enumerate(parts):
        txt = _re.sub(r"<[^>]+>", "", p)
        txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">")
                  .replace("&quot;", '"').strip())
        if len(txt) >= 12:
            out.append({"offset_sec": None, "text": txt})
    return out

SEASON_ID_BASE = -1000000

def seasons_folder_id(season_id) -> int:
    return SEASON_ID_BASE - int(season_id)

def list_up_seasons(cred: dict, mid: str,
                    throttle: "Throttle | None" = None) -> list[dict]:
    url = wbi_sign(
        f"https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
        f"?mid={mid}&page_num=1&page_size=20", cred, throttle)
    d = request_json(url, cred, throttle=throttle,
                     referer=f"https://space.bilibili.com/{mid}")
    if d.get("code") != 0:
        return []
    il = ((d.get("data") or {}).get("items_lists")) or {}
    return list(il.get("seasons_list") or []) + list(il.get("series_list") or [])

def list_season_archives(cred: dict, mid: str, season_id,
                         page: int = 1, ps: int = 20,
                         throttle: "Throttle | None" = None) -> list[dict]:
    url = wbi_sign(
        f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
        f"?mid={mid}&season_id={season_id}&page_num={page}&page_size={ps}",
        cred, throttle)
    d = request_json(url, cred, throttle=throttle,
                     referer=f"https://space.bilibili.com/{mid}")
    if d.get("code") != 0:
        return []
    return ((d.get("data") or {}).get("archives")) or []

def sync_seasons(cred: dict, con: sqlite3.Connection, mids: list[str],
                 max_per_season: int = 0,
                 throttle: "Throttle | None" = None) -> dict:
    stat = {"seasons": 0, "videos": 0}
    for mid in mids:
        for s in list_up_seasons(cred, mid, throttle):
            meta = s.get("meta") or {}
            sid = meta.get("season_id") or meta.get("series_id")
            if not sid:
                continue
            fid = seasons_folder_id(sid)
            total = meta.get("total") or 0

            con.execute(
                "INSERT OR REPLACE INTO folder(id,title,media_count,attr,"
                "fav_time,synced_at) VALUES(?,?,?,?,?,?)",
                (fid, meta.get("name") or str(sid), total, 0, 0, 0))
            stat["seasons"] += 1
            page, got = 1, 0
            while True:
                arch = list_season_archives(cred, mid, sid, page,
                                            throttle=throttle)
                if not arch:
                    break
                for a in arch:
                    bv = a.get("bvid")
                    if not bv:
                        continue
                    con.execute(
                        "INSERT OR REPLACE INTO video(bvid,title,intro,"
                        "upper_mid,upper,fav_time,pubtime,duration,play,type) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (bv, a.get("title"), "",
                         str(a.get("mid") or mid), a.get("author") or "",
                         a.get("pubdate") or 0, a.get("pubdate") or 0,
                         a.get("duration") or 0,
                         (a.get("stat") or {}).get("view") or 0, 2))
                    con.execute(
                        "INSERT OR REPLACE INTO membership(folder_id,bvid,fav_time) "
                        "VALUES(?,?,?)", (fid, bv, a.get("pubdate") or 0))
                    got += 1
                    stat["videos"] += 1
                con.commit()
                if max_per_season and got >= max_per_season:
                    break
                if len(arch) < 30:
                    break
                page += 1
            print("    《%s》%d 条" % (str(meta.get("name"))[:26], got), flush=True)
    return stat

def do_sync(cred: dict, folder_ids: list[int] | None, resume: bool = True,
            max_pages: int = 0) -> None:
    th = Throttle()
    con = connect()
    folders = list_folders(cred, th)

    if folder_ids:
        folders = [f for f in folders if f["id"] in folder_ids]
    if not folders:
        raise SystemExit("没有匹配的收藏夹")

    done = load_progress() if resume else {}
    total_new = 0
    for i, f in enumerate(folders, 1):
        start = (done.get(str(f["id"]), 0) + 1) if resume and done.get(str(f["id"])) else 1
        print(f"\n[{i}/{len(folders)}] {f['title']}  "
              f"({f.get('media_count',0)} 个)"
              + (f"  从第 {start} 页续传" if start > 1 else ""))
        before = con.execute("SELECT COUNT(*) FROM membership WHERE folder_id=?",
                             (f["id"],)).fetchone()[0]
        sync_folder(cred, con, f, th, start_page=start, max_pages=max_pages)
        after = con.execute("SELECT COUNT(*) FROM membership WHERE folder_id=?",
                            (f["id"],)).fetchone()[0]
        total_new += after - before
        con.execute("UPDATE folder SET synced_at=? WHERE id=?", (int(time.time()), f["id"]))
        con.commit()
        done[str(f["id"])] = 0
        save_progress(done)

        if i < len(folders):
            gap = th.folder_gap_ms()
            dbg(f"收藏夹间隔 {gap}ms")
            time.sleep(gap / 1000)

    print(f"\n同步完成：新增/更新 {total_new} 条收藏关系")
    print(f"本地库：{con.execute('SELECT COUNT(*) FROM video').fetchone()[0]} 个视频")
    print(f"风控累计：{th.risk_count} 次")
    con.close()

def search_video(cred: dict, keyword: str, page: int = 1,
                 throttle: Throttle | None = None) -> list[dict]:
    q = urllib.parse.quote(keyword)
    url = (f"https://api.bilibili.com/x/web-interface/wbi/search/type"
           f"?search_type=video&keyword={q}&page={page}")
    d = request_json(url, cred, throttle=throttle)
    if d.get("code") != 0:
        dbg(f"搜索失败 {d.get('code')} {d.get('message')}")
        return []
    out = []
    for v in (d.get("data") or {}).get("result") or []:
        out.append({
            "bvid": v.get("bvid"),
            "title": re.sub(r"<[^>]+>", "", v.get("title", "")),
            "author": v.get("author"),
            "mid": v.get("mid"),
            "duration": v.get("duration"),
            "play": v.get("play"),
            "danmaku": v.get("video_review"),
            "pubdate": v.get("pubdate"),
            "description": re.sub(r"<[^>]+>", "", v.get("description", "")),
            "tag": v.get("tag"),
        })
    return out

def dur_to_sec(s) -> int:
    if isinstance(s, int):
        return s
    if not s:
        return 0
    parts = [int(x) for x in str(s).split(":") if x.isdigit()]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec

def do_search(cred: dict, keyword: str, pages: int = 1,
              min_play: int = 0, min_dur: int = 0, max_dur: int = 0,
              limit: int = 20) -> None:
    th = Throttle()
    hits = []
    for p in range(1, pages + 1):
        hits += search_video(cred, keyword, p, th)
        if p < pages:
            time.sleep(th.page_gap_ms(0) / 1000)

    if not hits:
        print("无结果")
        return

    print(f"「{keyword}」共 {len(hits)} 条原始结果")
    kept = []
    for h in hits:
        sec = dur_to_sec(h["duration"])
        h["sec"] = sec
        if min_play and (h["play"] or 0) < min_play:
            continue
        if min_dur and sec < min_dur:
            continue
        if max_dur and sec > max_dur:
            continue
        kept.append(h)

    print(f"过滤后 {len(kept)} 条（播放≥{min_play} 时长 {min_dur}~{max_dur or '∞'}s）：\n")
    for i, h in enumerate(kept[:limit], 1):
        m, s = divmod(h["sec"], 60)
        print(f"  [{i:>2}] {h['bvid']:<14} {m:>3}:{s:02d}  "
              f"{h['play']:>8}播放  {h['author'][:14] if h['author'] else '':<16}")
        print(f"       {h['title'][:60]}")

    out = BASE_DIR / f"search-{hashlib.md5(keyword.encode()).hexdigest()[:8]}.json"
    out.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存：{out.name}")
    print("下一步：python bili_shelf.py add --folder <ID> --from-search " + out.name)

def do_add(cred: dict, folder_id: int, bvids: list[str]) -> None:
    if not cred.get("bili_jct"):
        raise SystemExit("缺少 bili_jct，无法执行收藏（写操作需要 csrf）")
    th = Throttle()
    ok, fail = 0, []
    for i, bv in enumerate(bvids, 1):
        d = request_json(
            "https://api.bilibili.com/x/v3/fav/resource/deal",
            cred, method="POST", throttle=th,
            data={"rid": bv, "type": 2, "add_media_ids": str(folder_id),
                  "del_media_ids": "", "csrf": cred["bili_jct"]})
        if d.get("code") == 0:
            ok += 1
            print(f"  [{i}/{len(bvids)}] ✓ {bv}")
        else:
            fail.append((bv, d.get("code"), d.get("message")))
            print(f"  [{i}/{len(bvids)}] ✗ {bv}  {d.get('code')} {d.get('message')}")

        time.sleep(random.uniform(1.6, 3.2))
    print(f"\n成功 {ok}，失败 {len(fail)}")
    for bv, c, m in fail:
        print(f"  {bv}: {c} {m}")

def reply_comment(cred: dict, oid: str, message: str,
                  root: str | None = None, parent: str | None = None,
                  throttle: Throttle | None = None,
                  dry_run: bool = True) -> dict:
    if not cred.get("bili_jct"):
        raise SystemExit("缺少 bili_jct，写操作必须带 csrf")
    th = throttle or Throttle()

    payload = {"oid": oid, "type": 1, "message": message,
               "csrf": cred["bili_jct"], "plat": 1}
    if root:
        payload["root"] = root
    if parent:
        payload["parent"] = parent

    if dry_run:
        print("  [演练模式] 将提交：")
        for k, v in payload.items():
            if k == "csrf":
                v = v[:6] + "…"
            print(f"    {k} = {v}")
        print("  确认无误后加 --send 实际提交")
        return {"code": 0, "message": "dry-run"}

    d = request_json("https://api.bilibili.com/x/v2/reply/add", cred,
                     method="POST", data=payload, throttle=th)
    if d.get("code") == 0:
        rp = (d.get("data") or {}).get("rpid")
        print(f"  ✓ 已发布  rpid={rp}")
    else:
        print(f"  ✗ 失败  code={d.get('code')}  {d.get('message')}")
    return d

def do_reply(cred: dict, oid: str, message: str, root: str | None,
             parent: str | None, count: int, send: bool) -> None:
    th = Throttle()
    if send:

        threshold, delay = th.cooldown_policy()
        print(f"即将发表 {count} 条评论。写操作节流：条间隔 8~15s，"
              f"每 {threshold // 2} 条休息 {delay / 1000:.0f}s")
    ok = 0
    for i in range(count):
        print(f"\n[{i+1}/{count}]")
        d = reply_comment(cred, oid, message, root, parent, th, dry_run=not send)
        if d.get("code") == 0 and send:
            ok += 1
        if i < count - 1:

            gap = random.uniform(8.0, 15.0)

            threshold, delay = th.cooldown_policy()
            if send and (i + 1) % max(threshold // 2, 1) == 0:
                gap = delay / 1000
                print(f"  [写冷却] 休息 {gap:.0f}s")
            dbg(f"间隔 {gap:.1f}s")
            time.sleep(gap)
    if send:
        print(f"\n完成：成功 {ok}/{count}，风控累计 {th.risk_count} 次")

def do_stats() -> None:
    con = connect()
    nv = con.execute("SELECT COUNT(*) FROM video").fetchone()[0]
    nm = con.execute("SELECT COUNT(*) FROM membership").fetchone()[0]
    print(f"视频 {nv} 个，收藏关系 {nm} 条\n")
    print("按收藏夹：")
    for t, c in con.execute(
            "SELECT f.title, COUNT(m.bvid) FROM folder f "
            "LEFT JOIN membership m ON m.folder_id=f.id "
            "GROUP BY f.id ORDER BY -COUNT(m.bvid)"):
        print(f"  {t[:26]:<28} {c:>5}")
    print("\nTop UP：")
    for u, c in con.execute(
            "SELECT upper, COUNT(*) FROM video GROUP BY upper "
            "ORDER BY -COUNT(*) LIMIT 10"):
        print(f"  {(u or '?')[:24]:<26} {c:>4}")
    print("\n时长分布：")
    for lo, hi, label in ((0, 60, "<1min"), (60, 300, "1-5min"),
                          (300, 900, "5-15min"), (900, 1800, "15-30min"),
                          (1800, 3600, "30-60min"), (3600, 10**9, ">1h")):
        c = con.execute("SELECT COUNT(*) FROM video WHERE duration>=? AND duration<?",
                        (lo, hi)).fetchone()[0]
        print(f"  {label:<10} {c:>5}")
    con.close()

def main() -> int:
    ap = argparse.ArgumentParser(description="B站收藏夹同步 / 搜索 / AI 整理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("folders", help="列出收藏夹")
    sub.add_parser("toview", help="把「稍后再看」同步进本地库（folder_id=-1）")
    p_sea = sub.add_parser("seasons", help="同步指定 UP 的视频合集")
    p_sea.add_argument("--up", action="append", required=True,
                       help="UP 的 mid（可多次）")
    p_sea.add_argument("--max-per-season", type=int, default=0,
                       help="每个合集最多取几条（0=全部，试跑用）")
    p_art = sub.add_parser("articles", help="同步指定 UP 的专栏（type=21）")
    p_art.add_argument("--up", action="append", required=True,
                       help="UP 的 mid（可多次）")
    p_art.add_argument("--from-lib", action="store_true",
                       help="用本地库里出现过的 UP 作为取数范围")

    p_sync = sub.add_parser("sync", help="同步收藏夹到本地库")
    p_sync.add_argument("--folder", type=int, action="append", help="指定收藏夹 ID（可多次）")
    p_sync.add_argument("--max-pages", type=int, default=0, help="每个收藏夹最多翻几页（试跑用）")
    p_sync.add_argument("--no-resume", action="store_true", help="忽略断点，从头同步")

    p_s = sub.add_parser("search", help="搜索视频")
    p_s.add_argument("keyword")
    p_s.add_argument("--pages", type=int, default=1)
    p_s.add_argument("--min-play", type=int, default=0)
    p_s.add_argument("--min-dur", type=int, default=0, help="最短时长（秒）")
    p_s.add_argument("--max-dur", type=int, default=0, help="最长时长（秒）")
    p_s.add_argument("--limit", type=int, default=20, help="显示条数")

    p_a = sub.add_parser("add", help="收藏视频到指定收藏夹")
    p_a.add_argument("--folder", type=int, required=True)
    p_a.add_argument("--bv", action="append", default=[], help="BV 号（可多次）")
    p_a.add_argument("--from-search", default=None, help="从 search 产出的 JSON 读取")

    p_r = sub.add_parser("reply", help="发表评论/回复（默认演练，加 --send 才提交）")
    p_r.add_argument("--oid", required=True, help="视频 aid（数字）；BV 号请先转 aid")
    p_r.add_argument("--message", required=True)
    p_r.add_argument("--root", default=None, help="根评论 rpid（回复某条评论）")
    p_r.add_argument("--parent", default=None, help="父评论 rpid（楼中楼回复某人）")
    p_r.add_argument("--count", type=int, default=1, help="重复条数（默认1）")
    p_r.add_argument("--send", action="store_true", help="实际提交（不加则只演练）")

    sub.add_parser("stats", help="本地库统计")

    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    global DEBUG
    DEBUG = getattr(args, "debug", False)

    cred = load_credential()
    dbg(f"SESSDATA 长度 {len(cred['sessdata'])}, UID {cred.get('dedeuserid')}")

    if args.cmd == "seasons":
        cred = load_credential(args.sessdata)
        con = connect()
        th = Throttle()
        mids = list(dict.fromkeys(m for m in (args.up or []) if m))
        print(f"  取 {len(mids)} 个 UP 的合集…")
        st = sync_seasons(cred, con, mids,
                          max_per_season=args.max_per_season, throttle=th)
        con.close()
        print(f"  合集 {st['seasons']} 个 · 视频 {st['videos']} 条")
        return
    if args.cmd == "articles":
        cred = load_credential(args.sessdata)
        con = connect()
        th = Throttle()
        mids = list(args.up or [])
        if args.from_lib:
            rows = con.execute("SELECT upper_mid, COUNT(*) n FROM video "
                               "WHERE upper_mid != '' GROUP BY upper_mid "
                               "ORDER BY n DESC LIMIT 40").fetchall()
            mids += [r[0] for r in rows]
        mids = list(dict.fromkeys(m for m in mids if m))
        n = sync_up_articles(cred, con, mids, th)
        con.close()
        print(f"  同步 {len(mids)} 个 UP 的专栏，共 {n} 条（type=21）")
        return
    if args.cmd == "toview":
        cred = load_credential(args.sessdata)
        con = connect()
        th = Throttle()
        n = sync_toview(cred, con, th)
        con.close()
        print(f"  「稍后再看」同步 {n} 条（folder_id={TOVIEW_FOLDER_ID}）")
        return
    if args.cmd == "folders":
        do_folders(cred)
    elif args.cmd == "sync":
        do_sync(cred, args.folder, resume=not args.no_resume,
                max_pages=args.max_pages)
    elif args.cmd == "search":
        do_search(cred, args.keyword, args.pages, args.min_play,
                  args.min_dur, args.max_dur, args.limit)
    elif args.cmd == "add":
        bvs = list(args.bv)
        if args.from_search:
            p = Path(args.from_search)
            if not p.exists():
                p = BASE_DIR / args.from_search
            data = json.loads(p.read_text(encoding="utf-8"))
            bvs += [d["bvid"] for d in data if d.get("bvid")]
            print(f"从 {p.name} 读入 {len(bvs)} 个 BV")
        if not bvs:
            raise SystemExit("没有要收藏的 BV")
        do_add(cred, args.folder, bvs)
    elif args.cmd == "reply":
        do_reply(cred, args.oid, args.message, args.root, args.parent,
                 args.count, args.send)
    elif args.cmd == "stats":
        do_stats()

if __name__ == "__main__":
    main()
