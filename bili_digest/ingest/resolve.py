# -*- coding: utf-8 -*-

import os as _os
import re
import sys as _sys

from bili_digest import paths

_ROOT = str(paths.HERE)

KIND_OK = "ok"
KIND_FORMAT = "format"
KIND_NOT_FOUND = "not_found"
KIND_GONE = "gone"
KIND_NEED_LOGIN = "need_login"
KIND_NETWORK = "network"
KIND_UNKNOWN = "unknown"

HINTS = {
    KIND_FORMAT: "这个号看着就不对 —— 请回原页面重新复制完整链接",
    KIND_NOT_FOUND: "号是合法格式但查不到 —— 多半也是抄错了，请重新复制",
    KIND_GONE: "视频确实不存在了（已删/私密）—— 换一个",
    KIND_NEED_LOGIN: "需要登录态才能看 —— 补上 cookie 再试",
    KIND_NETWORK: "网络问题 —— 稍后重试",
    KIND_UNKNOWN: "未知原因",
}

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d{6,})", re.I)
B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
URL_RE = re.compile(r"https?://[^\s)]+")

def classify(ref: str) -> dict:
    s = str(ref or "").strip()
    if not s:
        return {"kind": "unknown", "value": "", "note": "空引用"}

    m = B23_RE.search(s)
    if m:
        return {"kind": "short", "value": m.group(0), "note": "b23.tv 短链"}

    m = BV_RE.search(s)
    if m:
        return {"kind": "bvid", "value": m.group(1), "note": "BV 号"}

    m = AV_RE.search(s)
    if m:
        aid = m.group(1)
        n = len(aid)
        if n < 6:
            return {"kind": "bad_avid", "value": aid,
                    "note": f"av 号只有 {n} 位（旧式至少 6 位）"}
        if 11 <= n <= 14:
            return {"kind": "bad_avid", "value": aid,
                    "note": f"av 号是 {n} 位 —— 新式 15 位、旧式 6~10 位，"
                            f"这个长度不属于任何一种，很可能中间漏了几位"}
        return {"kind": "avid", "value": aid, "note": f"av 号（{n} 位）"}

    if s.isdigit():
        n = len(s)
        if n >= 6 and not (11 <= n <= 14):
            return {"kind": "avid", "value": s, "note": f"裸数字（{n} 位）"}
        return {"kind": "bad_avid", "value": s,
                "note": f"裸数字 {n} 位，不是合法的 av 号长度"}

    m = URL_RE.search(s)
    if m:
        return {"kind": "unknown", "value": m.group(0),
                "note": "是 URL 但没识别出 BV/av/短链"}

    return {"kind": "unknown", "value": s, "note": "无法识别的引用"}

def resolve(cred, ref: str, th, verbose: bool = True) -> dict:
    from bili_digest.ingest import bili_shelf as bs

    def _fail(kind, note=""):
        if verbose:
            print(f"    ! {note or HINTS.get(kind)}")
        return {"ok": False, "kind": kind, "hint": HINTS.get(kind, ""),
                "note": note, "bvid": None, "aid": None, "title": None}

    c = classify(ref)
    if verbose:
        print(f"    识别: {c['kind']} · {c['note']}")

    if c["kind"] == "bad_avid":
        return _fail(KIND_FORMAT, c["note"] + " —— " + HINTS[KIND_FORMAT])
    if c["kind"] == "unknown":
        return _fail(KIND_FORMAT, c["note"] + " —— " + HINTS[KIND_FORMAT])

    url = None
    if c["kind"] == "bvid":
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={c['value']}"
    elif c["kind"] == "avid":
        url = f"https://api.bilibili.com/x/web-interface/view?aid={c['value']}"
    elif c["kind"] == "short":

        real = expand_short(c["value"], verbose=verbose)
        if not real:
            return _fail(KIND_NETWORK, "短链展开失败")
        inner = classify(real)
        if inner["kind"] == "bvid":
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={inner['value']}"
        elif inner["kind"] == "avid":
            url = f"https://api.bilibili.com/x/web-interface/view?aid={inner['value']}"
        else:
            return _fail(KIND_UNKNOWN, f"短链展开得到 {real[:60]}，但仍未取到 BV/av")

    try:
        d = bs.request_json(url, cred, throttle=th, retries=2)
    except Exception as e:
        return _fail(KIND_NETWORK, f"{type(e).__name__}: {str(e)[:60]}")

    code = (d or {}).get("code")
    if code == 0:
        dd = d.get("data") or {}
        return {"ok": True, "kind": KIND_OK, "bvid": dd.get("bvid"),
                "aid": dd.get("aid"), "title": dd.get("title"),
                "hint": "", "note": c["note"]}

    if code in (-404,):

        return _fail(KIND_NOT_FOUND,
                     f"API 返回 {code} {d.get('message')} —— 号是合法格式但查不到")
    if code in (-403, -401):
        return _fail(KIND_NEED_LOGIN, f"API 返回 {code}（权限/登录）")
    return _fail(KIND_UNKNOWN, f"API 返回 {code} {d.get('message')}")

def expand_short(short_url: str, verbose: bool = False) -> str:
    import gzip
    import urllib.request
    import zlib
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
    req = urllib.request.Request(short_url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",

        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return r.geturl()
    except Exception as e:
        if verbose:
            print(f"    ! 短链展开失败：{type(e).__name__}: {str(e)[:60]}")
        return ""

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="解析 B站视频引用")
    ap.add_argument("ref", help="BV 号 / av 号 / 短链 / 完整 URL")
    a = ap.parse_args()

    import os
    from pathlib import Path
    for ln in Path(_ROOT, ".env").read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    from bili_digest.ingest import bili_shelf as bs
    cred = bs.load_credential()
    r = resolve(cred, a.ref, bs.Throttle())
    print()
    if r["ok"]:
        print(f"  ✓ {r['bvid']}  《{r['title']}》")
    else:
        print(f"  ✗ 失败类别：{r['kind']}")
        print(f"    下一步：{r['hint']}")

if __name__ == "__main__":
    main()
