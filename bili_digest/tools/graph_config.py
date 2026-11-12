import json
import sqlite3
import sys
from pathlib import Path

_S = {
    "net": '-path:"_主题" -path:_全部标签',
    "tree": 'path:"_主题"',
    "both": "-path:_全部标签",
}

def graph_config(vault: str = "_vault", con: sqlite3.Connection | None = None,
                 layer: str = "net") -> int:
    p = Path(vault) / ".obsidian" / "graph.json"
    if not p.exists():
        return 0
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0

    if layer not in _S:
        raise SystemExit("layer 只能是 net / tree / both")
    g["search"] = _S[layer]
    g["repelStrength"] = 60
    g["scale"] = 0.6
    g["showOrphans"] = False
    g["centerStrength"] = 0.15
    g["localJumps"] = 1

    if con is not None:
        try:
            rows = list(con.execute(
                "SELECT comm_id, size, rep_terms FROM community "
                "WHERE size > 20 ORDER BY size DESC LIMIT 8"))
            groups = [{"query": 'path:"_主题"',
                       "color": {"a": 1, "rgb": rgb(0xD4A017)}}]
            for i, r in enumerate(rows):
                try:
                    t = json.loads(r["rep_terms"] or "[]")
                except Exception:
                    t = []
                if not t:
                    continue
                groups.append({"query": "[community:%s]" % r["comm_id"],
                               "color": {"a": 1, "rgb": rgb(_PALETTE[i % len(_PALETTE)])}})
            if len(groups) > 1:
                g["colorGroups"] = groups
        except Exception:
            pass

    p.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  图谱 → %s（repel 60 · center 0.15 · scale 0.6 · 不画孤岛）" % layer)
    return 1

_PALETTE = (0x3B82F6, 0x10B981, 0xF59E0B, 0xEF4444,
            0x06B6D4, 0x84CC16, 0xF97316, 0xEC4899)

def rgb(n: int) -> int:
    return n

def main() -> int:
    import argparse
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="从数据库重建 Obsidian graph.json")
    ap.add_argument("--vault", default="_vault")
    ap.add_argument("--db", default="knowledge.db")
    ap.add_argument("--layer", default="net", choices=["net", "tree", "both"])
    a = ap.parse_args()
    con = None
    try:
        con = sqlite3.connect(a.db)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        pass
    return 0 if graph_config(a.vault, con, a.layer) else 1

if __name__ == "__main__":
    sys.exit(main())
