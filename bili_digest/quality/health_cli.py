from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sqlite3
import sys

from bili_digest.paths import DB

def _emit(payload) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()

def _quiet(fn, *a, **kw):
    raw = io.BytesIO()
    buf = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    buf.flush()
    text = raw.getvalue().decode("utf-8", "replace")
    if text:
        sys.stderr.write(text)
    return result

def _slug(name: str) -> str:
    return re.sub(r"[\s/]+", "-", name.strip()).strip("-")

def _ages() -> dict:
    try:
        from bili_digest.quality import health as _h
        con = sqlite3.connect(str(DB))
        try:
            return _h.ages(con) or {}
        finally:
            con.close()
    except Exception:
        return {}

def cmd_findings(args) -> int:
    from bili_digest.quality import health as _h
    rows = _quiet(_h.snapshot, full=args.full)
    ages = _ages()
    findings = []
    for name, triple in rows.items():
        state, value, detail = triple
        if state == "OK":
            continue
        findings.append({
            "id": _slug(name),
            "state": state,
            "title": name,
            "value": str(value),
            "detail": str(detail),
            "age_days": ages.get(name),
        })
    findings.sort(key=lambda f: (-(f["age_days"] or 0), f["state"] != "FAIL"))
    _emit({"findings": findings, "checked": len(rows)})
    return 0

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb-health-cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("findings", help="every WARN/FAIL entry as JSON")
    f.add_argument("--full", action="store_true")
    f.set_defaults(fn=cmd_findings)
    a = p.parse_args(argv)
    return a.fn(a)

if __name__ == "__main__":
    raise SystemExit(main())
