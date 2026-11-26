# -*- coding: utf-8 -*-

OK = 0
USAGE = 1
FAIL = 2
WARN = 3

_SEV = {"OK": OK, "WARN": WARN, "FAIL": FAIL}

def from_severity(status: str) -> int:
    return _SEV.get(status, WARN)

def from_rows(rows: dict) -> int:
    worst = OK
    for v in (rows or {}).values():
        if not (isinstance(v, tuple) and v):
            continue
        code = _SEV.get(v[0], WARN)
        if code == FAIL:
            return FAIL
        if code == WARN:
            worst = WARN
    return worst

def from_levels(levels) -> int:
    lv = set(levels or [])
    if "P0" in lv:
        return FAIL
    if lv & {"P1", "P2", "P3"}:
        return WARN
    return OK
