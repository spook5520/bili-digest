#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of

def cmd_gaps(a) -> None:
    from bili_digest.quality import retrieval_gaps as RG
    from bili_digest.cli import exit_codes as EC
    con = db_of(a)
    try:
        days = getattr(a, "days", 30)
        for ln in RG.render(con, days):
            print(ln)
        rep = RG.report(con, days)
        real = (rep.get("zero") or {}).get("real") or []
        return EC.WARN if real else EC.OK
    finally:
        con.close()
