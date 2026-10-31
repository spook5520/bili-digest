# -*- coding: utf-8 -*-

import os as _os
import sys as _sys
from pathlib import Path

from bili_digest import paths

_ROOT = str(paths.HERE)

LEDGER = Path(_ROOT) / "failed_directions.md"

HEADER = """# 方向账本（失败 + 已验证）

试过的方向，连同数据与结论。**动手前先查这里。**

格式：方向 | 判决 | 读数 | 原因与证据

判决取值：
  · 无效 / 未显著    —— 被数据否掉，别再试
  · 有效（已验证）    —— 有显著读数，可考虑采纳

"""

def entries() -> list:
    if not LEDGER.exists():
        return []
    out = []
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        if ln.startswith("- **"):
            out.append(ln)
    return out

def has(direction: str) -> list:
    key = direction.strip().lower()
    return [e for e in entries() if key in e.lower()]

def record(direction: str, verdict: str = "无效", metric: str = "",
           why: str = "", evidence: str = "") -> bool:
    if not LEDGER.exists():
        LEDGER.write_text(HEADER, encoding="utf-8")
    line = "- **%s** · %s · %s · %s（依据：%s）" % (
        direction, verdict, metric or "无读数", why or "未说明",
        evidence or "无")

    old = LEDGER.read_text(encoding="utf-8")
    kept = [l for l in old.splitlines()
            if not (l.startswith("- **") and direction.lower() in l.lower())]
    kept.append(line)
    LEDGER.write_text(chr(10).join(kept) + chr(10), encoding="utf-8")
    return True

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="失败账本：动手前先查")
    ap.add_argument("--list", action="store_true", help="列出全部")
    ap.add_argument("--check", help="查某个方向试过没有")
    ap.add_argument("--add", help="追加一条（方向名）")
    ap.add_argument("--metric", default="")
    ap.add_argument("--why", default="")
    ap.add_argument("--evidence", default="")
    a = ap.parse_args()

    if a.add:
        record(a.add, metric=a.metric, why=a.why, evidence=a.evidence)
        print("  已记入：%s" % a.add)
        return
    if a.check:
        hits = has(a.check)
        if hits:
            print("  这个方向试过：" + chr(10))
            for h in hits:
                print("  " + h)
        else:
            print("  账本里没有「%s」—— 可以试" % a.check)
        return
    es = entries()
    print("  失败账本 %d 条" % len(es))
    print()
    for e in es:
        print("  " + e)
    if not es:
        print("  （空）")

if __name__ == "__main__":
    main()
