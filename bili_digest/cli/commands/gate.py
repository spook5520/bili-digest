#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of

def cmd_gate(a) -> None:
    from bili_digest.quality import gate as G

    con = db_of(a)
    con.row_factory = __import__("sqlite3").Row
    try:
        if a.revert:
            if not a.yes:
                print("  将撤销批次 %s（写库）。确认请重跑：--revert %s --yes"
                      % (a.revert, a.revert))
                print("  先看这个批次动了什么：kb gate")

                raise SystemExit(1)
            print("  %s" % G.revert(con, a.revert))
            return

        r = G.plan(con, a.tier)
        print("  规则版本 %s · %s 档 %d 条" % (r["rule_version"], r["tier"], r["total"]))
        for k in ("validated", "provisional", "reject", "manual"):
            if r["verdicts"].get(k):
                print("    %-12s %3d" % (k, r["verdicts"][k]))
        for tag, key in (("回 raw 的原因", "reject"), ("降 provisional 的原因", "provisional")):
            box = (r["reasons"] or {}).get(key) or {}
            if not box:
                continue
            print("  %s：" % tag)
            for k, v in sorted(box.items(), key=lambda x: -x[1]):
                print("    %-20s %3d" % (k, v))

        if not a.apply:
            print("\n  （只读。写库要 --apply --yes）")

            return 3 if (r["verdicts"].get("reject") or 0) > 0 else 0

        if not a.yes:
            n = sum(v for k, v in r["verdicts"].items() if k in ("reject", "provisional"))
            print("\n  将改动 %d 条的 epistemic 状态（回 raw / 降 provisional）。" % n)
            print("  这是写库操作，且可撤销（会返回 batch id）。")
            print("  确认请重跑：kb gate --apply --yes")
            raise SystemExit(1)

        res = G.apply(con, a.tier, dry_run=False)
        print("\n  已执行 batch=%s：%s" % (res["batch"], res["counts"]))
        print("  撤销：kb gate --revert %s --yes" % res["batch"])
        print("  复测：kb gate（看分层是否变化）· kb status（看 P2 那条是否消失）")
        return 0
    finally:
        con.close()
