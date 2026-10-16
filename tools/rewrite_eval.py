import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import sys
import hashlib
import time
from pathlib import Path

CACHE = Path(__file__).parent / ".cache" / "rewrite_cache.json"

PROMPT = """下面是一段视频内容要点。请把它改写成【一个用户在回忆时可能问出的检索语句】。

要求：
- 像人平时说话那样：可以省略、口语化、语序随意
- 【不要】照抄要点里的词，换成同义或更笼统的说法
- 可以只记得大概（比如只说得出用途、场景，说不得具体名词）
- 长度像一句自然的问话（15~40 字）
- 只输出这一句，不要解释

要点：{text}

输出 JSON：{{"query": "..."}}"""

def _load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save(d: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

def make(limit: int = 20, use_seg_summary: bool = True):
    from extract import call_llm
    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    cache = _load()

    src_kind = "seg_summary" if use_seg_summary else "subtitle_seg"
    rows = con.execute("""SELECT item_uid, text FROM content_block
                          WHERE kind=? GROUP BY item_uid LIMIT ?""",
                       (src_kind, limit * 2)).fetchall()
    print(f"  素材 kind={src_kind}，候选 {len(rows)} 条\n")

    out = []
    for r in rows:
        uid, text = r["item_uid"], r["text"] or ""

        if src_kind == "seg_summary":
            import re
            parts = [p.strip() for p in re.split(r"第\d+段：", text) if p.strip()]
            text = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
        text = " ".join(text.split())[:200]
        if len(text) < 20:
            continue
        hkey = hashlib.md5(f"{uid}|{text}".encode()).hexdigest()
        if hkey in cache:
            q = cache[hkey]
        else:
            try:
                raw = call_llm(PROMPT.format(text=text), timeout=120)
                q = (json.loads(raw).get("query") or "").strip()
            except Exception as e:
                print(f"    [!] {type(e).__name__}: {str(e)[:40]}")
                continue
            if not q:
                continue
            cache[hkey] = q
        out.append((uid, q, text[:60]))
        if len(out) >= limit:
            break
    _save(cache)
    print(f"  造出 {len(out)} 条改写查询（缓存 {len(cache)}）\n")
    for uid, q, src in out[:6]:
        print(f"    {q}")
        print(f"        ← 源: {src}…")
    con.close()
    return out

def evaluate(limit: int = 20):
    import rag_core as rc
    from core_schema import connect
    from paths import DB, VEC
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    qs = make(limit)
    if not qs:
        con.close()
        return

    have = {r["item_uid"] for r in con.execute(
        "SELECT DISTINCT item_uid FROM content_block WHERE kind='seg_summary'")}
    g_have = [(u, q) for u, q, _s in qs if u in have]
    g_none = [(u, q) for u, q, _s in qs if u not in have]
    print(f"  有 seg_summary 的条目: {len(g_have)} 条查询 · "
          f"没有的: {len(g_none)} 条\n")

    def mrr(items, kinds=None):
        ranks = []
        for uid, q in items:
            hits = rc.hybrid(con, vi, q, top_k=10, kinds=kinds)
            uids = [h.get("item_uid") for h in hits]
            ranks.append(uids.index(uid) + 1 if uid in uids else None)
        n = len(ranks) or 1
        hit = sum(1 for x in ranks if x)
        m = sum(1 / x for x in ranks if x) / n
        return hit / n, m, ranks

    print("═══ 结果 ═══\n")
    for label, items in (("有 seg_summary", g_have), ("无 seg_summary", g_none)):
        if not items:
            continue
        r10, m, ranks = mrr(items)
        print(f"  {label:<18} R@10 {r10*100:5.0f}%  MRR {m:.3f}   排名 {ranks}")
    print()
    print("  读法：")
    print("    · 若「有 seg_summary」组明显更好 → 段摘要确实提供了新的检索入口")
    print("      （但要小心：两组的条目本身不同，这仍是观察而非对照实验）")
    print("    · 若两组都差 → 说明改写查询这一档确实比前几档难，")
    print("      而当前检索在这种难度下的表现才是真实水平")
    con.close()

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "make"
    n = 20
    for a in sys.argv[1:]:
        if a.isdigit():
            n = int(a)
    if mode == "eval":
        evaluate(n)
    else:
        make(n)
