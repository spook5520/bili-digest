# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

def compare(base_rr: list, new_rr: list, names: list | None = None,
            verbose: bool = True) -> dict:
    n = min(len(base_rr), len(new_rr))
    gained, lost = [], []
    for i in range(n):
        b, x = float(base_rr[i]), float(new_rr[i])
        if b > 0 and x == 0:
            lost.append(i)
        elif b == 0 and x > 0:
            gained.append(i)

    net_zero_but_flipped = (not gained and not lost) is False and len(gained) == len(lost)

    out = {
        "n": n,
        "gained": len(gained),
        "lost": len(lost),
        "net": len(gained) - len(lost),
        "gained_idx": gained,
        "lost_idx": lost,
        "masked": net_zero_but_flipped,
        "note": ("净差为零，但 %d 条变好 / %d 条变差互相抵消 —— 只看平均会漏掉"
                 % (len(gained), len(lost))) if net_zero_but_flipped else None,
    }
    if verbose:
        print("  查询级兼容性（%d 条）" % n)
        print("    变好 %2d 条 · 变差 %2d 条 · 净 %+d" % (
            out["gained"], out["lost"], out["net"]))
        if out["masked"]:
            print("    ⚠ %s" % out["note"])
        if lost and names:
            print("    变差的查询：")
            for i in lost[:6]:
                print("      - %s" % str(names[i])[:70])
    return out

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="查询级兼容性：升级后哪些原本对的变错了")
    ap.add_argument("--demo", action="store_true", help="用内置样例说明「净差为零」的陷阱")
    a = ap.parse_args()
    if a.demo:

        base = [1.0, 0.5, 0.0, 1.0, 0.0, 0.5, 0.0, 0.0]
        new = [0.0, 0.5, 1.0, 0.0, 0.5, 0.0, 1.0, 0.0]
        names = ["q%d" % i for i in range(len(base))]
        print("  样例：两版平均完全相同")
        compare(base, new, names)
    else:
        print("  用法：在你的实验里 import，或用 --demo 看示例")

if __name__ == "__main__":
    main()
