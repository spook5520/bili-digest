#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bili_digest.paths import EMBED_MODEL as _PATHS_EMBED_MODEL

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

BILI_HIVE_ENV = "KB_BILI_HIVE"
BILI_SESSDATA_ENV = "KB_BILI_SESSDATA"
from bili_digest.paths import AD_SERVER
SEG_MS = 60 * 6 * 1000

DEBUG = False

def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[dbg] {msg}", file=sys.stderr)

def _parse_hive(hive: "Path") -> dict:
    try:
        raw = hive.read_bytes()
    except PermissionError:

        import shutil
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "_bili_acct.hive"
        shutil.copyfile(hive, tmp)
        raw = tmp.read_bytes()
        tmp.unlink(missing_ok=True)

    text = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
    sess = re.search(r"([0-9a-fA-F]{8}%2C\d{10}%2C[0-9a-fA-F]{8})", text)
    jct = re.search(r"[0-9a-f]{32}", text)

    uid = None
    for pat in (r'"DedeUserID"\s*:\s*"?(\d{5,})',
                r"DedeUserID\D{0,6}(\d{5,})",
                r"\b(\d{15,19})\b"):
        m = re.search(pat, text)
        if m:
            uid = m.group(1)
            break
    if not sess:
        raise SystemExit(
            f"未能从 {hive} 解析出 SESSDATA。\n"
            "若不是客户端 hive 文件，请改用 --sessdata 或 KB_BILI_SESSDATA。")
    return {
        "sessdata": sess.group(1),
        "bili_jct": jct.group(0) if jct else "",
        "dedeuserid": uid or "",
    }

def _env_file_value(name: str) -> str | None:
    try:
        from bili_digest.distill.extract import _load_env_file
        return _load_env_file().get(name)
    except Exception:
        return None

def load_credential(explicit: str | None = None) -> dict:
    if explicit:
        return {"sessdata": explicit, "bili_jct": "", "dedeuserid": ""}

    import os
    src = os.environ.get(BILI_HIVE_ENV)
    if src:
        hive = Path(src).expanduser()
        if not hive.exists():
            raise SystemExit(f"{BILI_HIVE_ENV} 指向的文件不存在：{hive}")
        return _parse_hive(hive)

    sess = os.environ.get(BILI_SESSDATA_ENV)
    if sess:
        return {"sessdata": sess, "bili_jct": "", "dedeuserid": ""}

    env_sess = _env_file_value(BILI_SESSDATA_ENV)
    if env_sess:
        return {"sessdata": env_sess,
                "bili_jct": _env_file_value("KB_BILI_JCT") or "",
                "dedeuserid": _env_file_value("KB_BILI_DEDEUSERID") or ""}

    raise SystemExit(
        "未提供登录态。四种方式任选：\n"
        "  1) python qrlogin.py                 扫码登录（推荐，写入 .env）\n"
        "  2) 命令行加 --sessdata <你的 SESSDATA>\n"
        f"  3) export {BILI_SESSDATA_ENV}=<你的 SESSDATA>\n"
        f"  4) export {BILI_HIVE_ENV}=/path/to/account.hive"
        "（用客户端的话，指向它的登录态文件）")

def cookie_header(cred: dict) -> str:
    parts = [f"SESSDATA={cred['sessdata']}"]
    if cred.get("bili_jct"):
        parts.append(f"bili_jct={cred['bili_jct']}")
    if cred.get("dedeuserid"):
        parts.append(f"DedeUserID={cred['dedeuserid']}")
    return "; ".join(parts)

def http_get(url: str, cookie: str | None = None, referer: str | None = None,
             raw: bool = False, timeout: int = 20):
    if DEBUG:
        dbg(f"GET {url[:120]}{'…' if len(url) > 120 else ''}")
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", referer or "https://www.bilibili.com/")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if DEBUG:
                dbg(f"  → HTTP {r.status}  {len(data)} 字节  "
                    f"ctype={r.headers.get('Content-Type')} "
                    f"cenc={r.headers.get('Content-Encoding')}")
    except urllib.error.HTTPError as e:
        dbg(f"  ✗ HTTPError {e.code} {e.reason}  url={url[:100]}")
        raise
    except Exception as e:
        dbg(f"  ✗ {type(e).__name__}: {e}  url={url[:100]}")
        raise
    if raw:
        return data
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        dbg(f"  ✗ JSON 解析失败: {e}  前 120 字节={data[:120]!r}")
        return None

def fetch_meta(bvid: str, ck: str) -> dict:
    j = http_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", ck)
    if not j or j.get("code") != 0:
        raise SystemExit(f"取视频信息失败：{j.get('message') if j else 'network error'}")
    d = j["data"]
    owner = d.get("owner") or {}

    tags = ""
    try:
        tj = http_get(f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}", ck)
        if tj and tj.get("code") == 0:
            tags = ",".join(t.get("tag_name", "") for t in (tj.get("data") or []))
    except Exception:
        pass
    return {
        "bvid": bvid, "aid": d["aid"], "cid": d["cid"],
        "title": d["title"], "owner": owner.get("name", ""),
        "owner_mid": owner.get("mid", ""),
        "pubdate": d.get("pubdate", ""),
        "tname": d.get("tname", ""),
        "tags": tags,
        "duration": d["duration"], "desc": d.get("desc", ""),

        "pages": [{"cid": p.get("cid"), "page": p.get("page"),
                   "part": p.get("part"), "duration": p.get("duration")}
                  for p in (d.get("pages") or [])],
        "view": d["stat"]["view"], "like": d["stat"]["like"],
        "reply": d["stat"]["reply"], "danmaku": d["stat"]["danmaku"],
    }

def page_duration(meta: dict) -> int:
    cid = meta.get("cid")
    for p in (meta.get("pages") or []):
        if p.get("cid") == cid and p.get("duration"):
            return int(p["duration"])
    return int(meta.get("duration") or 0)

def fetch_subtitle(meta: dict, ck: str, verbose: bool = False) -> list[dict]:

    subs = {}
    for path in ("player/wbi/v2", "player/v2"):
        j = http_get(f"https://api.bilibili.com/x/{path}"
                     f"?aid={meta['aid']}&cid={meta['cid']}", ck)
        subs = ((j or {}).get("data") or {}).get("subtitle") or {}
        if subs.get("subtitles"):
            break
    items = subs.get("subtitles") or []
    if not items:
        return []
    if verbose:
        print("      可用字幕轨：" + "、".join(
            f"{s.get('lan')}({s.get('lan_doc', '')})" for s in items))

    def is_zh(s):
        lan = (s.get("lan") or "").lower()
        return lan.startswith("zh") or lan.startswith("ai-zh")

    cands = [s for s in items if is_zh(s)] or items
    best, best_n, picked_lan = None, -1, ""
    for s in cands[:4]:
        url = s.get("subtitle_url", "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            continue
        try:
            body = http_get(url, ck, raw=True)
            data = json.loads((body or b"").decode("utf-8"))
            entries = [{"from": round(x["from"], 2), "to": round(x["to"], 2),
                        "text": x["content"].strip()}
                       for x in data.get("body", []) if x.get("content", "").strip()]
        except Exception as e:
            dbg(f"字幕轨 {s.get('lan')} 下载/解析失败: {type(e).__name__}: {e}")
            continue
        if DEBUG:
            dbg(f"字幕轨 {s.get('lan')} → {len(entries)} 条")
        if len(entries) > best_n:
            best, best_n, picked_lan = entries, len(entries), s.get("lan", "")
    if verbose and best is not None:
        print(f"      选中：{picked_lan}（{best_n} 条，取各中文轨中最完整的一条）")

    dur = page_duration(meta)
    if best and dur > 0:
        last = max((x["from"] for x in best), default=0)
        ratio = last / dur
        if ratio > 1.5:
            dbg(f"字幕跨度 {last:.0f}s 远超时长 {dur}s（{ratio:.1f}x）→ 丢弃"
                f"（B站对无字幕视频会返回无关字幕）")
            return []
        if dur > 180 and ratio < 0.5:
            dbg(f"字幕仅覆盖 {ratio*100:.0f}%（{last:.0f}s / {dur}s）→ 丢弃"
                f"（可能是无关字幕或只抓到了片段）")
            return []

    return best or []

def _read_varint(buf: bytes, i: int):
    result, shift = 0, 0
    while True:
        b = buf[i]; i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, i

def parse_danmaku_protobuf(buf: bytes) -> list[dict]:
    out, i, n = [], 0, len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        if (tag & 7) != 2:
            break
        ln, i = _read_varint(buf, i)
        elem, i = buf[i:i + ln], i + ln
        progress = mode = 0
        content = uid = ""
        j = 0
        while j < len(elem):
            t2, j = _read_varint(elem, j)
            f, w = t2 >> 3, t2 & 7
            if w == 0:
                v, j = _read_varint(elem, j)
                if f == 2:
                    progress = v
                elif f == 3:
                    mode = v
            elif w == 2:
                l2, j = _read_varint(elem, j)
                s = elem[j:j + l2]; j += l2
                if f == 7:
                    content = s.decode("utf-8", "replace")
                elif f == 6:
                    uid = s.decode("utf-8", "replace")
            elif w == 5:
                j += 4
            elif w == 1:
                j += 8
            else:
                break
        if content:
            out.append({"t": progress, "text": content, "mode": mode, "uid": uid})
    return out

def fetch_danmaku(meta: dict, ck: str, verbose: bool = False,
                  max_workers: int = 4) -> list[dict]:
    segs = int(meta["duration"] // (SEG_MS / 1000)) + 1
    ref = f"https://www.bilibili.com/video/av{meta['aid']}"

    def fetch_one(idx: int) -> list[dict]:
        url = (f"https://api.bilibili.com/x/v2/dm/web/seg.so"
               f"?type=1&oid={meta['cid']}&segment_index={idx}")
        for attempt in range(3):
            try:
                buf = http_get(url, ck, ref, raw=True, timeout=25)
                parsed = parse_danmaku_protobuf(buf or b"")
                if DEBUG and not parsed:
                    dbg(f"seg {idx} → 空（{len(buf or b'')} 字节，该段无弹幕属正常）")
                return parsed
            except urllib.error.HTTPError as e:
                if e.code == 304 and attempt < 2:
                    dbg(f"seg {idx} → 304，第 {attempt + 1} 次重试")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                dbg(f"seg {idx} → 放弃（HTTP {e.code}）")
                return []
            except Exception as e:
                dbg(f"seg {idx} → 放弃（{type(e).__name__}: {e}）")
                return []
        return []

    all_dm = []
    if segs <= 4:
        for idx in range(1, segs + 1):
            all_dm.extend(fetch_one(idx))
            time.sleep(0.25)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for res in ex.map(fetch_one, range(1, segs + 1)):
                all_dm.extend(res)

    if verbose:
        print(f"      （共 {segs} 段，{'并发' if segs > 4 else '串行'}拉取）")

    seen, dedup = set(), []
    for d in all_dm:
        key = (d["t"] // 1000, d["text"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(d)
    return dedup

def fetch_sub_replies(meta: dict, ck: str, root: str, ps: int = 10) -> list[dict]:
    url = (f"https://api.bilibili.com/x/v2/reply/reply"
           f"?type=1&oid={meta['aid']}&root={root}&pn=1&ps={ps}")
    try:
        j = http_get(url, ck)
    except Exception:
        return []
    if not j or j.get("code") != 0:
        return []
    out = []
    for r in ((j.get("data") or {}).get("replies") or []):
        out.append({
            "user": (r.get("member") or {}).get("uname", ""),
            "text": ((r.get("content") or {}).get("message") or "").strip(),
            "like": r.get("like", 0),
        })
    return out

def fetch_replies(meta: dict, ck: str, limit: int = 200,
                  with_sub: bool = True, max_workers: int = 6) -> list[dict]:
    out, pn = [], 1
    while len(out) < limit:
        url = (f"https://api.bilibili.com/x/v2/reply?type=1&oid={meta['aid']}"
               f"&pn={pn}&ps=20&sort=2")
        j = http_get(url, ck)
        if not j or j.get("code") != 0:
            if DEBUG:
                dbg(f"评论页 {pn} 终止: code={(j or {}).get('code')} "
                    f"msg={(j or {}).get('message')}")
            break
        replies = (j.get("data") or {}).get("replies") or []
        if not replies:
            dbg(f"评论页 {pn} 无数据，翻页结束")
            break
        for r in replies:
            out.append({
                "user": (r.get("member") or {}).get("uname", ""),
                "text": ((r.get("content") or {}).get("message") or "").strip(),
                "like": r.get("like", 0),
                "rcount": r.get("rcount", 0),
                "rpid": str(r.get("rpid", "")),
                "subs": [],
            })
        pn += 1
        time.sleep(0.35)

    if with_sub:
        targets = [x for x in out if x["rcount"] > 0][:25]
        if targets:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(fetch_sub_replies, meta, ck, x["rpid"]): x for x in targets}
                for f in as_completed(futs):
                    try:
                        futs[f]["subs"] = f.result()
                    except Exception:
                        pass
    return out

def fetch_ad_segments(meta: dict) -> list[dict]:
    url = f"{AD_SERVER}/api/skipSegments?videoID={meta['bvid']}&cid={meta['cid']}"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("origin", "bili-digest")
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8"))
        if DEBUG:
            dbg(f"广告段 API → {len(data) if isinstance(data, list) else '?'} 个段")
    except Exception as e:
        dbg(f"广告段 API 失败（不影响其他数据源）: {type(e).__name__}: {e}")
        return []
    if not isinstance(data, list):
        return []
    out = []
    for seg in data:
        s = seg.get("segment") or []
        if len(s) >= 2:
            out.append({"start": round(float(s[0]), 2), "end": round(float(s[1]), 2),
                        "category": seg.get("category", ""),
                        "action": seg.get("actionType", "skip")})
    return sorted(out, key=lambda x: x["start"])

def norm_text(s: str) -> str:
    s = re.sub(r"[\s，。！？、；：,.!?;:~…《》()（）\[\]【】\"'“”‘’\-—]", "", s)
    return re.sub(r"(.)\1{2,}", r"\1", s)

EMBED_MODEL = _PATHS_EMBED_MODEL
RAG_MODEL_DIR = Path(__file__).parent / "models"

def _load_rag():
    try:
        os.environ.setdefault("HF_ENDPOINT", paths.HF_ENDPOINT)
        import sqlite_vec
        import numpy as np
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(RAG_MODEL_DIR))
        return sqlite_vec, np, model
    except ImportError as e:
        dbg(f"RAG 依赖缺失: {e}")
        raise SystemExit(
            "需要 RAG 依赖，请先安装：\n"
            "  pip install -i https://mirrors.aliyun.com/pypi/simple/ "
            "--trusted-host mirrors.aliyun.com sqlite-vec fastembed"
        )

def build_rag_index(db_path: Path, meta: dict, batch: int = 256) -> dict:
    sqlite_vec, np, model = _load_rag()
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)

    stats = {}
    plan = [
        ("danmaku",  "SELECT t_ms, text FROM danmaku",                       "danmaku"),
        ("reply",    "SELECT likes, text FROM reply",                        "reply"),
        ("subtitle", "SELECT t_from, text FROM subtitle",                    "subtitle"),
    ]
    for name, sql, mode in plan:
        rows = con.execute(sql).fetchall()
        if not rows:
            stats[name] = 0
            continue

        if mode == "subtitle":

            merged, cur_start, buf = [], None, []
            for t, txt in rows:
                if cur_start is None:
                    cur_start = t
                if t - cur_start > 30 and buf:
                    merged.append((cur_start, "".join(buf)))
                    cur_start, buf = t, []
                buf.append(txt)
            if buf:
                merged.append((cur_start, "".join(buf)))
            items = merged
        else:
            items = [(t, txt) for t, txt in rows if txt and txt.strip()]

        texts = [x[1] for x in items]
        metas = [x[0] for x in items]

        con.execute(f"DROP TABLE IF EXISTS vec_{name}")
        con.execute(
            f"CREATE VIRTUAL TABLE vec_{name} USING vec0("
            f"  emb float[{512}] distance_metric=cosine,"
            f"  +meta float, +text text)"
        )
        n = 0
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            vecs = np.array(list(model.embed(chunk)), dtype=np.float32)
            con.executemany(
                f"INSERT INTO vec_{name}(emb, meta, text) VALUES (?,?,?)",
                [(v.tobytes(), float(metas[i + j]), chunk[j]) for j, v in enumerate(vecs)]
            )
            n += len(chunk)
        stats[name] = n
        if DEBUG:
            dbg(f"索引 {name}: {n} 条")
    con.commit()
    con.close()
    return stats

def rag_query(db_path: Path, question: str, top_k: int = 10,
              sources: list[str] | None = None) -> list[dict]:
    sqlite_vec, np, model = _load_rag()
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)

    qv = np.array(list(model.query_embed([question]))[0], dtype=np.float32)
    hits = []
    for src in (sources or ["danmaku", "reply", "subtitle"]):
        try:
            rows = con.execute(
                f"SELECT text, meta, distance FROM vec_{src} "
                f"WHERE emb MATCH ? AND k = ? ORDER BY distance",
                (qv.tobytes(), top_k)
            ).fetchall()
        except Exception as e:
            dbg(f"检索 {src} 失败: {e}")
            continue
        for text, meta_val, dist in rows:
            hits.append({"source": src, "text": text,
                         "meta": meta_val, "score": 1.0 - dist})
    con.close()
    hits.sort(key=lambda x: -x["score"])
    return hits[:top_k]

NOISE_RULES = [
    (r"^[\s\?\!\.。，、~～…！？；：'\"“”\-—_=+*#@￥$%^&()（）\[\]{}<>《》|\\/]+$", "纯符号"),
    (r"^\W*(.)\1{4,}\W*$", "单字重复"),
    (r"^[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\s]+$", "纯emoji"),
    (r"^.{0,5}第[\d一二三四五六七八九十百千万零两俩]{1,5}.{0,5}$", "抢前排"),
    (r"^(前|沙发|板凳|路过|飘过|打卡|签到|来了|前排|马克|顶|留名)[\s!！~～。]{0,3}$", "灌水"),
    (r"^\d{1,2}[:：]\d{2}([:：]\d{2})?$", "时间打卡"),
    (r"^(233+|666+|www+|hhh+|awa|qwq|orz|QAQ|OTZ|TAT)[\s!！~～。]{0,4}$", "无意义梗"),
    (r"^[\U0001F300-\U0001FAFF]{0,3}$", "空"),
]

SHORT_OK = set("好顶赞牛妙稳中典绝真实对错是否神强快慢高低大小多少")

def noise_reason(text: str) -> str | None:
    t = text.strip()
    if not t:
        return "空"
    for pat, name in NOISE_RULES:
        if re.match(pat, t, re.UNICODE):
            return name

    core = re.sub(r"[\s\W_]", "", t)
    if len(core) <= 1 and core not in SHORT_OK:
        return "过短"
    return None

STOPWORDS = {

    "这个", "那个", "什么", "怎么", "我们", "你们", "他们", "自己", "这些", "那些",
    "这样", "那样", "一样", "这里", "那里", "这是", "那是", "家伙", "事儿",

    "因为", "所以", "但是", "如果", "还是", "已经", "可能", "应该", "真的", "其实",
    "而且", "然后", "不过", "只是", "虽然", "即使", "没有", "知道", "感觉", "可以",

    "视频", "弹幕", "评论", "东西", "地方", "事情", "问题", "时候", "现在", "感觉",

    "哈哈", "哈哈哈", "算了", "不是", "就是", "于是", "一下子",
}

STOP_CHARS = set("吗呢吧啊哦嗯哈嘿诶唉呀哇咦哟的了吧着过很就也还都才又要会能可不")

def extract_keywords(texts: list[str], top_n: int = 20) -> list[tuple[str, int]]:
    from collections import Counter
    c = Counter()
    for t in texts:

        for seg in re.split(r"[，。！？、；：,\.!\?;:~\s（）()\[\]【】《》\"'“”‘’]+", t):
            seg = seg.strip()
            if not (2 <= len(seg) <= 12):
                continue
            if seg in STOPWORDS:
                continue
            if all(ch in STOP_CHARS for ch in seg):
                continue
            if len(seg) <= 3 and any(ch in STOP_CHARS for ch in seg):
                continue
            c[seg] += 1

    items = [(w, n) for w, n in c.most_common(top_n * 5) if n >= 3]
    keep = []
    for w, n in items:
        if any(w != o and w in o and on >= n for o, on in items):
            continue
        keep.append((w, n))
        if len(keep) >= top_n:
            break
    return keep

def compress_danmaku(dms: list[dict], per_bucket: int = 8, verbose: bool = False) -> dict:
    if not dms:
        return {"buckets": [], "top": [], "keywords": [], "noise": {}, "stats": {}}

    noise = Counter()
    kept = []
    for d in dms:
        reason = noise_reason(d["text"])
        if reason:
            noise[reason] += 1
        else:
            kept.append(d)

    exact = Counter(d["text"] for d in kept)
    norm_groups = Counter()
    for text, cnt in exact.items():
        norm_groups[norm_text(text)] += cnt

    top = [(t, c) for t, c in exact.most_common() if c >= 3]

    keywords = extract_keywords([d["text"] for d in kept], top_n=20)

    buckets = {}
    for d in kept:
        buckets.setdefault(int(d["t"] // SEG_MS), []).append(d)
    bucket_out = []
    for b in sorted(buckets):
        items = buckets[b]
        cnt = Counter(x["text"] for x in items)
        picked = []
        for text, c in cnt.most_common(per_bucket):
            picked.append({"t": round(next(x["t"] for x in items if x["text"] == text) / 1000, 1),
                           "text": text, "n": c})
        bucket_out.append({
            "range": f"{b * 360 // 60}:00-{(b + 1) * 360 // 60}:00",
            "count": len(items), "uniq": len(cnt), "samples": picked,
        })

    return {
        "buckets": bucket_out,
        "top": top[:20],
        "keywords": keywords,
        "noise": dict(noise),
        "stats": {
            "raw": len(dms), "noise": sum(noise.values()), "kept": len(kept),
            "exact_uniq": len(exact), "norm_uniq": len(norm_groups),
            "chars_raw": sum(len(d["text"]) for d in dms),
            "chars_kept": sum(len(d["text"]) for d in kept),
            "chars_uniq": sum(len(t) for t in exact),
        },
    }

def strip_ad_from_danmaku(dms: list[dict], ads: list[dict]) -> tuple[list[dict], int]:
    if not ads or not dms:
        return dms, 0
    kept, removed = [], 0
    for d in dms:
        sec = d["t"] / 1000.0
        if any(a["start"] <= sec <= a["end"] for a in ads):
            removed += 1
        else:
            kept.append(d)
    return kept, removed

def strip_ad_from_subtitle(subs: list[dict], ads: list[dict]) -> tuple[list[dict], list[dict]]:
    if not ads or not subs:
        return subs, []
    kept, removed = [], []
    for s in subs:
        mid = (s["from"] + s["to"]) / 2
        hit = next((a for a in ads if a["start"] <= mid <= a["end"]), None)
        if hit:
            removed.append({**s, "ad_category": hit["category"]})
        else:
            kept.append(s)
    return kept, removed

def compress_replies(reps: list[dict], top_n: int = 40, sub_top: int = 3) -> dict:
    if not reps:
        return {"top": [], "stats": {}}
    by_like = sorted(reps, key=lambda x: x["like"], reverse=True)
    seen, picked = set(), []
    for r in by_like:
        key = norm_text(r["text"])[:40]
        if key in seen or not r["text"]:
            continue
        seen.add(key)
        item = dict(r)
        subs = sorted(r.get("subs") or [], key=lambda x: x.get("like", 0), reverse=True)
        item["subs"] = subs[:sub_top]
        item["subs_total"] = len(r.get("subs") or [])
        picked.append(item)
        if len(picked) >= top_n:
            break
    n_subs = sum(len(r.get("subs") or []) for r in reps)
    return {
        "top": picked,
        "stats": {
            "total": len(reps), "uniq": len(seen), "subs": n_subs,
            "chars_raw": (sum(len(r["text"]) for r in reps)
                          + sum(len(s["text"]) for r in reps for s in (r.get("subs") or []))),
        },
    }

def save_sqlite(path: Path, meta, subs, dms, reps, ads, sources=None):
    con = sqlite3.connect(path)
    c = con.cursor()
    c.executescript("""
        DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS subtitle;
        DROP TABLE IF EXISTS danmaku; DROP TABLE IF EXISTS reply;
        DROP TABLE IF EXISTS reply_sub; DROP TABLE IF EXISTS ad;
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE subtitle(t_from REAL, t_to REAL, text TEXT);
        CREATE TABLE danmaku(t_ms INTEGER, text TEXT, mode INTEGER, uid TEXT);
        CREATE TABLE reply(rpid TEXT, user TEXT, text TEXT, likes INTEGER, rcount INTEGER);
        CREATE TABLE reply_sub(root_rpid TEXT, user TEXT, text TEXT, likes INTEGER);
        CREATE TABLE ad(t_start REAL, t_end REAL, category TEXT);
    """)
    rows = [(k, str(v)) for k, v in meta.items()]

    if sources:
        rows.append(("_done_sources", ",".join(sorted(sources))))
    elif isinstance(meta, dict) and "_done_sources" in meta:
        rows.append(("_done_sources", str(meta["_done_sources"])))
    c.executemany("INSERT INTO meta VALUES(?,?)", rows)
    c.executemany("INSERT INTO subtitle VALUES(?,?,?)", [(s["from"], s["to"], s["text"]) for s in subs])
    c.executemany("INSERT INTO danmaku VALUES(?,?,?,?)", [(d["t"], d["text"], d["mode"], d["uid"]) for d in dms])
    c.executemany("INSERT INTO reply VALUES(?,?,?,?,?)",
                  [(r.get("rpid", ""), r["user"], r["text"], r["like"], r.get("rcount", 0)) for r in reps])
    sub_rows = [(r.get("rpid", ""), s["user"], s["text"], s.get("like", 0))
                for r in reps for s in (r.get("subs") or [])]
    if sub_rows:
        c.executemany("INSERT INTO reply_sub VALUES(?,?,?,?)", sub_rows)
    c.executemany("INSERT INTO ad VALUES(?,?,?)", [(a["start"], a["end"], a["category"]) for a in ads])
    con.commit()
    con.close()

def build_digest(meta, subs, clean_subs, removed_subs, dm_c, rep_c, ads, sources,
                 dm_removed: int = 0) -> str:
    L = []
    A = L.append
    A(f"# {meta['title']}")
    A("")
    A(f"- UP：{meta['owner']}　时长：{meta['duration']}s　"
      f"播放：{meta['view']}　点赞：{meta['like']}　"
      f"评论：{meta['reply']}　弹幕：{meta['danmaku']}")
    A(f"- 数据源：{'、'.join(sources)}")
    A("")

    if ads and "ad" in sources:
        A("## 广告/非正片段落（已按此排除字幕与弹幕）")
        for a in ads:
            A(f"- {a['start']:.0f}s – {a['end']:.0f}s　[{a['category']}]　action={a['action']}")
        A(f"- 字幕剔除 {len(removed_subs)} 条（{len(subs)} → {len(clean_subs)}）"
          f"　弹幕剔除 {dm_removed} 条")
        A("")

    if "sub" in sources and clean_subs:
        A("## 正片字幕（主干）")
        for s in clean_subs:
            A(f"[{int(s['from']) // 60:02d}:{int(s['from']) % 60:02d}] {s['text']}")
        A("")

    if "dm" in sources and dm_c.get("buckets"):
        st = dm_c["stats"]
        A("## 弹幕")
        A(f"- 原始 {st['raw']} 条 → 滤除噪声 {st['noise']} 条 → 保留 {st['kept']} 条")
        if dm_c.get("noise"):
            detail = "、".join(f"{k} {v}" for k, v in
                              sorted(dm_c["noise"].items(), key=lambda x: -x[1]))
            A(f"- 噪声分布：{detail}")
        A(f"- 精确去重后 {st['exact_uniq']} 种　归一化后 {st['norm_uniq']} 种")
        A(f"- 字符数 {st['chars_raw']} → {st['chars_kept']}（过滤后）"
          f" → {st['chars_uniq']}（去重后）")
        A("")

        if dm_c.get("keywords"):
            A("### 语义焦点词（高频实词，压缩时最该保留的信号）")
            A("　" + "　".join(f"{w}({n})" for w, n in dm_c["keywords"]))
            A("")

        if dm_c["top"]:
            A("### 高频弹幕（≥3 次，只留一条 + 次数）")
            A("　" + "　".join(f"{t}({c})" for t, c in dm_c["top"]))
            A("")

        A("### 时间轴代表弹幕")
        for b in dm_c["buckets"]:
            A(f"**[{b['range']}]** {b['count']}条/{b['uniq']}种")
            for sp in b["samples"]:
                n = f" ×{sp['n']}" if sp["n"] > 1 else ""
                A(f"  - {sp['t']}s {sp['text']}{n}")
        A("")

    if "reply" in sources and rep_c.get("top"):
        st = rep_c["stats"]
        A("## 评论精选")
        A(f"- 抓取 {st['total']} 条（含楼中楼 {st.get('subs', 0)} 条），"
          f"去重后按热度取前 {len(rep_c['top'])} 条")
        for r in rep_c["top"]:
            rc = f"（{r['rcount']} 回复）" if r.get("rcount") else ""
            A(f"- 👍{r['like']} **{r['user']}**{rc}：{r['text'][:200]}")
            for s in r.get("subs") or []:
                A(f"    └ 👍{s.get('like', 0)} {s['user']}：{s['text'][:120]}")
        A("")
    return "\n".join(L)

def main() -> int:
    ap = argparse.ArgumentParser(description="B站视频多源整合摘要")
    ap.add_argument("target", help="BV 号 / av 号 / 完整 URL")
    ap.add_argument("--sources", default="meta,sub,dm,reply,ad",
                    help="数据源（逗号分隔）：meta,sub,dm,reply,ad")
    ap.add_argument("--strip-ad", action="store_true", help="按广告段排除字幕与弹幕")
    ap.add_argument("--dm-per-bucket", type=int, default=8, help="每个时间桶保留弹幕数")
    ap.add_argument("--reply-limit", type=int, default=200, help="评论抓取上限")
    ap.add_argument("--reply-top", type=int, default=40, help="评论精选条数")
    ap.add_argument("--sub-reply-top", type=int, default=3, help="每条评论保留的楼中楼条数")
    ap.add_argument("--no-sub-reply", action="store_true", help="不抓楼中楼")
    ap.add_argument("--sessdata", default=None, help="手动指定 SESSDATA")
    ap.add_argument("--out", default=None, help="输出根目录（默认 ./bili-digest-out）")
    ap.add_argument("--debug", action="store_true",
                    help="打印请求/异常细节到 stderr（排查用，不影响正常输出）")

    ap.add_argument("--build-index", action="store_true",
                    help="建立向量索引（弹幕/评论/字幕编码入库，可重复执行覆盖）")
    ap.add_argument("--ask", default=None, metavar="问题",
                    help="对已建索引的视频提问，检索相关片段")
    ap.add_argument("--top-k", type=int, default=10, help="检索返回条数")
    ap.add_argument("--rag-sources", default="danmaku,reply,subtitle",
                    help="检索范围：danmaku,reply,subtitle")
    args = ap.parse_args()

    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print("[dbg] debug 模式开启", file=sys.stderr)

    m = re.search(r"(BV[0-9A-Za-z]{10})", args.target)
    if not m:
        raise SystemExit("未能从参数中识别 BV 号")
    bvid = m.group(1)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    cred = load_credential(args.sessdata)
    ck = cookie_header(cred)
    print(f"[1/5] 凭证就绪（SESSDATA 长度 {len(cred['sessdata'])}）")

    meta = fetch_meta(bvid, ck)
    print(f"[2/5] {meta['title']}　{meta['duration']}s　弹幕声明 {meta['danmaku']} 条")

    base = Path(args.out) if args.out else Path("bili-digest-out")
    outdir = base / bvid
    outdir.mkdir(parents=True, exist_ok=True)

    subs, dms, reps, ads = [], [], [], []

    _is_cv = str(bvid).lower().startswith("cv")
    if _is_cv and "sub" in sources:
        try:
            from bili_digest.ingest import bili_shelf as _shelf

            subs = _shelf.fetch_article_text(cred, str(bvid)[2:])
        except Exception as e:
            print(f"  [!] 专栏正文抓取失败: {type(e).__name__}: {str(e)[:60]}")

    jobs = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        if "sub" in sources and not _is_cv:
            jobs["sub"] = ex.submit(fetch_subtitle, meta, ck, False)
        if "dm" in sources:
            jobs["dm"] = ex.submit(fetch_danmaku, meta, ck, False)
        if "reply" in sources:
            jobs["reply"] = ex.submit(fetch_replies, meta, ck,
                                      args.reply_limit, not args.no_sub_reply)
        if "ad" in sources:
            jobs["ad"] = ex.submit(fetch_ad_segments, meta)

        failed = []
        for key, fut in jobs.items():
            try:
                r = fut.result()
            except Exception as e:
                failed.append((key, f"{type(e).__name__}: {str(e)[:70]}"))
                continue
            if key == "sub":
                subs = r
            elif key == "dm":
                dms = r
            elif key == "reply":
                reps = r
            elif key == "ad":
                ads = r

    if "sub" in sources:
        print(f"[3/5] 字幕 {len(subs)} 条")
    if "dm" in sources:
        print(f"[4/5] 弹幕 {len(dms)} 条（声明 {meta['danmaku']} 条，"
              f"拿全率 {round(len(dms) / max(meta['danmaku'], 1) * 100)}%）")
    if "reply" in sources:
        n_subs = sum(len(r.get("subs") or []) for r in reps)
        print(f"[5/5] 评论 {len(reps)} 条（楼中楼 {n_subs} 条）")
    if "ad" in sources:
        print(f"      广告段 {len(ads)} 个")
    for key, msg in failed:
        print(f"      [!] {key} 抓取失败（其余源继续）: {msg}")

    if args.strip_ad and ads:
        clean_subs, removed_subs = strip_ad_from_subtitle(subs, ads)
        clean_dms, dm_removed = strip_ad_from_danmaku(dms, ads)
    else:
        clean_subs, removed_subs = subs, []
        clean_dms, dm_removed = dms, 0

    dm_c = compress_danmaku(clean_dms, args.dm_per_bucket)
    rep_c = compress_replies(reps, args.reply_top, args.sub_reply_top)

    save_sqlite(outdir / "raw.sqlite", meta, subs, dms, reps, ads,
                sources=sources)

    if args.build_index:
        print("  [RAG] 建立向量索引…")
        t0 = time.time()
        try:
            st = build_rag_index(outdir / "raw.sqlite", meta)
            total = sum(st.values())
            print(f"  [RAG] 索引完成：{total} 条向量（{time.time()-t0:.1f}s）"
                  f"  明细 {st}")
        except SystemExit as e:
            print(f"  [RAG] 跳过：{e}")

    if args.ask:
        try:
            rag_srcs = [s.strip() for s in args.rag_sources.split(",") if s.strip()]
            hits = rag_query(outdir / "raw.sqlite", args.ask,
                             top_k=args.top_k, sources=rag_srcs)
            print(f"\n【检索】{args.ask}")
            if not hits:
                print("  无结果（索引尚未建立？先跑 --build-index）")
            for h in hits:
                if h["source"] == "danmaku":
                    m, s = divmod(int(h["meta"] / 1000), 60)
                    loc = f"[{m:02d}:{s:02d}]"
                elif h["source"] == "reply":
                    loc = f"[👍{int(h['meta'])}]"
                else:
                    m, s = divmod(int(h["meta"]), 60)
                    loc = f"[{m:02d}:{s:02d}]"
                print(f"  {h['score']:.3f} {loc:>10}  {h['text'][:90]}")
        except SystemExit as e:
            print(f"  [RAG] 跳过：{e}")

    digest = build_digest(meta, subs, clean_subs, removed_subs, dm_c, rep_c, ads,
                          sources, dm_removed)
    (outdir / "digest.md").write_text(digest, encoding="utf-8")
    (outdir / "stats.json").write_text(json.dumps({
        "meta": meta, "sources": sources,
        "subtitle": {"raw": len(subs), "kept": len(clean_subs),
                     "stripped": len(removed_subs)},
        "danmaku": {**dm_c["stats"], "ad_stripped": dm_removed,
                    "noise_detail": dm_c.get("noise", {})},
        "danmaku_keywords": dm_c.get("keywords", []),
        "reply": rep_c["stats"], "ad_segments": ads,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n输出目录：{outdir}")
    print(f"  raw.sqlite   全量数据（可回溯任意一条）")
    print(f"  digest.md    整合视图 {len(digest)} 字符")
    print(f"  stats.json   统计摘要")

if __name__ == "__main__":
    main()
