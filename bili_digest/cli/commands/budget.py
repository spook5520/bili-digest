#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of

def cmd_budget(a) -> None:
    from bili_digest.quality import budget as B
    con = db_of(a)
    try:
        if a.reset:
            print("  已删除 %d 条预算覆盖，恢复默认" % B.reset(con))
        if a.set:
            k, _, v = a.set.partition("=")
            B.set_limit(con, k.strip(), float(v))
            print("  %s = %g（存 meta_kv；--reset 改回）" % (k.strip(), float(v)))
        for ln in B.render(con, days=a.days, limit=a.limit):
            print(ln)
    finally:
        con.close()
