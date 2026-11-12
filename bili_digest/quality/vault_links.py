#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

def target_of(raw: str) -> str:
    t = raw.split("|", 1)[0].split("#", 1)[0].strip()
    if t.lower().endswith(".md"):
        t = t[:-3]
    return t

def scan(vault: str | Path) -> dict:
    root = Path(vault)
    if not root.is_dir():
        raise SystemExit(f"不是目录：{root}")

    by_path: dict[str, Path] = {}
    by_base: dict[str, list[str]] = defaultdict(list)
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".obsidian/") or "/.obsidian/" in rel:
            continue
        key = rel[:-3]
        by_path[key] = p
        by_base[p.stem].append(key)

    broken: list[dict] = []
    total = 0
    for key, p in by_path.items():
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in LINK_RE.finditer(text):
            t = target_of(m.group(1))
            if not t:
                continue
            total += 1
            if "/" in t:
                ok = t in by_path
            else:
                ok = bool(by_base.get(t))
            if not ok:
                line = text.count("\n", 0, m.start()) + 1
                broken.append({"src": key, "line": line, "target": t,
                               "kind": "path" if "/" in t else "basename"})

    return {"vault": str(root), "notes": len(by_path), "links": total,
            "broken_n": len(broken), "broken": broken}

def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="按路径口径检查 vault 悬空双链")
    ap.add_argument("--vault", default="_vault")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON（含每条断链）")
    a = ap.parse_args()

    r = scan(a.vault)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"  vault {r['vault']}")
        print(f"  笔记 {r['notes']} 篇 · 双链 {r['links']} 条 · 悬空 {r['broken_n']} 条"
              f"  {'✓' if r['broken_n'] == 0 else '✗'}")
        for b in r["broken"][:20]:
            print(f"    {b['src']}:{b['line']}  [[{b['target']}]] ({b['kind']})")
        if r["broken_n"] > 20:
            print(f"    …还有 {r['broken_n'] - 20} 条（--json 看全量）")
    return 0 if r["broken_n"] == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
