#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of

def cmd_status(a) -> int:
    import json as _j
    from bili_digest.quality import status as S
    from bili_digest.cli import exit_codes as EC
    con = db_of(a)
    try:
        st = S.collect(con)
        payload = S.as_dict(st)
        code = EC.from_levels(payload.get("levels") or [])
        if a.json:
            payload["exit_code"] = code
            print(_j.dumps(payload, ensure_ascii=False, indent=1, default=str))
        else:
            for ln in S.render(con, st):
                print(ln)
        return code
    finally:
        con.close()
