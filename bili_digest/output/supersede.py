#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re

PLATFORMS = {
    "macos", "windows", "linux", "android", "ios", "java", "python",
    "curseforge", "modrinth", "neoforge", "forge", "fabric", "minecraft",
    "obsidian", "github", "gitlab", "steam", "bilibili", "wifi", "usb",
    "html5", "css3", "javascript", "webassembly", "docker", "kvm",
}

_CAMEL = re.compile(
    r"\b(?=[A-Za-z0-9.\-]{3,})[A-Za-z][A-Za-z0-9.\-]*"
    r"(?:[a-z][A-Z]|[A-Za-z][0-9])[A-Za-z0-9.\-]*\b")
_VER = re.compile(
    r"(?<![A-Za-z0-9])[vV]?(\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9.]+)?)(?![0-9])")

def _names(text: str) -> set:
    out = set()
    for n in _CAMEL.findall(text or ""):
        n = n.strip(".-")
        low = n.lower()
        if len(n) < 4 or low in PLATFORMS:
            continue
        if re.fullmatch(r"[vV]?\d+(\.\d+)*", n):
            continue
        out.add(low)
    return out

def _vkey(ver: str):
    m = re.match(r"[vV]?(\d+(?:\.\d+)*)", (ver or "").strip())
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])

def entries(con) -> list:
    import json
    title = {r[0]: (r[1] or "") for r in con.execute("SELECT uid,title FROM item")}
    ts = {r[0]: (r[1] or r[2] or 0) for r in con.execute(
        "SELECT uid,published_at,saved_at FROM item")}
    pts = {}
    try:
        for r in con.execute("SELECT item_uid,content FROM derived WHERE kind='key_points'"):
            try:
                v = json.loads(r[1])
            except Exception:
                continue
            pts[r[0]] = " ".join(str(x.get("point") or "") for x in v
                                 if isinstance(x, dict))
    except Exception:
        pts = {}
    out = []
    for uid, t in title.items():
        vs = _VER.findall(t)
        if not vs:
            continue
        out.append({"uid": uid, "title": t, "ver": vs[0],
                    "names": _names(t) | _names(pts.get(uid, "")),
                    "ts": int(ts.get(uid) or 0)})
    return out

_CACHE = {}

def pairs(con) -> list:
    try:
        st = con.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),0) FROM item").fetchone()
        key = (id(con), int(st[0]), int(st[1] or 0))
    except Exception:
        key = None
    if key is not None and key in _CACHE:
        return _CACHE[key]
    recs = entries(con)
    by_name = {}
    for r in recs:
        for n in r["names"]:
            by_name.setdefault(n, []).append(r)
    out = []
    seen = set()
    for name, g in by_name.items():
        if len({r["uid"] for r in g}) < 2:
            continue
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                a, b = g[i], g[j]
                if a["uid"] == b["uid"]:
                    continue
                key = tuple(sorted((a["uid"], b["uid"])))
                if key in seen:
                    continue
                seen.add(key)
                ka, kb = _vkey(a["ver"]), _vkey(b["ver"])
                if ka is not None and kb is not None:
                    if ka == kb:
                        continue
                    old, new = (a, b) if ka < kb else (b, a)
                    why = "版本号 %s < %s" % (old["ver"], new["ver"])
                elif a["ts"] and b["ts"] and a["ts"] != b["ts"]:
                    old, new = (a, b) if a["ts"] < b["ts"] else (b, a)
                    why = "版本号不可比，按发布时间"
                else:
                    continue
                out.append({"name": name, "old": old, "new": new, "why": why})
    if key is not None:
        _CACHE.clear()
        _CACHE[key] = out
    return out

def find(con, uid: str):
    for p in pairs(con):
        if p["old"]["uid"] == uid:
            return p
    return None

def render_lines(con, p: dict) -> list:
    from bili_digest.output.obsidian_out import note_filename
    new = p["new"]
    row = con.execute("SELECT title,item_id FROM item WHERE uid=?",
                      (new["uid"],)).fetchone()
    if not row:
        return []
    link = note_filename({"title": row[0], "item_id": row[1]})[:-3]
    import datetime
    d = ""
    if new["ts"]:
        d = datetime.date.fromtimestamp(int(new["ts"])).isoformat()
    return [
        "> [!warning] 已有更新版本",
        "> [[%s]]%s" % (link, ("（%s，%s）" % (new["ver"], d)) if d else "（%s）" % new["ver"]),
        "> 本条是 %s%s。依据：%s。" % (
            p["old"]["ver"], ("（%s）" % datetime.date.fromtimestamp(
                int(p["old"]["ts"])).isoformat()) if p["old"]["ts"] else "",
            p["why"]),
    ]
