#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

STATUS = {86101: "未扫码", 86090: "已扫码，请在手机上确认",
          86038: "二维码已失效", 0: "登录成功"}

def _get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
        return body, r.headers

def generate():
    body, _ = _get(GEN)
    d = body.get("data") or {}
    if not d.get("url") or not d.get("qrcode_key"):
        raise SystemExit(f"申请二维码失败：{body}")
    return d["url"], d["qrcode_key"]

def make_image(url: str, out: Path) -> bool:
    try:
        import qrcode
    except ImportError:
        print("  [!] 未装 qrcode（pip install qrcode）—— 只能打印链接，"
              "而链接需要在手机上打开才能扫")
        return False
    img = qrcode.make(url)
    img.save(str(out))
    return True

def poll_once(key: str):
    body, headers = _get(f"{POLL}?{urllib.parse.urlencode({'qrcode_key': key})}")
    code = ((body.get("data") or {}).get("code"))
    raw = headers.get_all("Set-Cookie") or []
    cred = {}
    for c in raw:
        kv = c.split(";", 1)[0]
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k in ("SESSDATA", "bili_jct", "DedeUserID"):
                cred[k] = v
    return code, cred

def save_env(cred: dict) -> Path:
    p = paths.HERE / ".env"
    lines = []
    if p.exists():
        lines = [l for l in p.read_text(encoding="utf-8").splitlines()
                 if not l.startswith("KB_BILI_")]
    lines.append(f"KB_BILI_SESSDATA={cred['SESSDATA']}")
    if cred.get("bili_jct"):
        lines.append(f"KB_BILI_JCT={cred['bili_jct']}")
    if cred.get("DedeUserID"):
        lines.append(f"KB_BILI_DEDEUSERID={cred['DedeUserID']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p

def main() -> int:
    ap = argparse.ArgumentParser(description="B站扫码登录")
    ap.add_argument("--no-save", action="store_true", help="只打印，不写 .env")
    ap.add_argument("--timeout", type=int, default=180, help="等待秒数（默认 180）")
    a = ap.parse_args()

    url, key = generate()
    png = paths.HERE / "qrcode.png"
    ok = make_image(url, png)
    print("  用手机 B站 App 扫码登录：")
    if ok:
        print(f"    二维码图片：{png}")
    print(f"    或在手机上打开：{url}")
    print(f"  （{a.timeout} 秒内有效）\n")

    t0 = time.time()
    last = None
    while time.time() - t0 < a.timeout:
        code, cred = poll_once(key)
        msg = STATUS.get(code, f"未知状态 {code}")
        if msg != last:
            print(f"    [{int(time.time()-t0):>3}s] {msg}")
            last = msg
        if code == 0 and cred.get("SESSDATA"):
            print("\n  ✓ 登录成功")
            if a.no_save:
                print("  （--no-save：未写文件）")
            else:
                print(f"  ✓ 凭证已写入 {save_env(cred)}（不进版本库）")
            return 0
        if code == 86038:
            print("\n  二维码已失效，重跑一次即可")
            return 1
        time.sleep(2)
    print("\n  等待超时，重跑一次即可")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
