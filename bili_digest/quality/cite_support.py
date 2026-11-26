# -*- coding: utf-8 -*-
import sys, json, random, sqlite3, re, argparse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from bili_digest.schema.core_schema import connect
from bili_digest.paths import DB
from bili_digest.distill.extract import call_llm

PROMPT = """以下是一个视频的【全部可得材料】：
{ctx}

结论：
{claim}

判断：这些材料合起来，能否支持这个结论？三选一：
support    结论能从材料推出（允许概括）
partial    结论里有关键信息（人名/数字/因果/限定）在材料里完全找不到
unrelated  材料与结论无关
输出 JSON：{{"verdict": "...", "why": "20字内，指出缺什么"}}"""

_KINDS = {
    "insights":   {"text": "claim", "json": "insights"},
    "key_points": {"text": "point", "json": "key_points"},
}

def _pool(con, kind: str = "insights") -> list:
    spec = _KINDS[kind]
    field, jkey = spec["text"], spec["json"]
    out = []
    for uid, content in con.execute(
            "SELECT item_uid, content FROM derived WHERE kind=?", (kind,)):
        try:
            arr = json.loads(content)
        except Exception:
            continue
        if not isinstance(arr, list):
            arr = arr.get(jkey) or []
        for it in arr:
            if not isinstance(it, dict):
                continue
            claim = (it.get(field) or "").strip()
            if len(claim) < 8:
                continue
            blocks = []
            for c in (it.get("cites") or []):
                if not isinstance(c, dict):
                    continue
                q = (c.get("quote") or "").strip()
                cid = c.get("id")
                if len(q) < 8 or not cid:
                    continue
                row = con.execute(
                    "SELECT text FROM content_block WHERE id=?", (cid,)).fetchone()
                if row and q in (row[0] or ""):

                    t = (row[0] or "")[:700]
                    if t not in blocks:
                        blocks.append(t)
            if blocks:
                out.append({"uid": uid, "claim": claim, "blocks": blocks})
    return out

def _materials(con, uid: str, blocks: list) -> str:
    tit = con.execute("SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
    parts = ["【标题】" + ((tit[0] if tit else "") or "")]
    parts.append("【引用所在块全文】" + "\n---\n".join(blocks))
    cms = [r[0] for r in con.execute(
        "SELECT text FROM content_block WHERE item_uid=? AND kind='comment' "
        "ORDER BY weight DESC LIMIT 5", (uid,))]
    if cms:
        parts.append("【评论】" + " / ".join(x[:80] for x in cms))
    dms = [r[0] for r in con.execute(
        "SELECT text FROM content_block WHERE item_uid=? AND kind='danmaku' LIMIT 10",
        (uid,))]
    if dms:
        parts.append("【弹幕】" + " / ".join(x[:40] for x in dms))
    return "\n".join(parts)[:1800]

def run(sample_n: int = 20, seed: int = 20261125, quiet: bool = False,
        kind: str = "insights") -> dict:
    con = connect(DB)
    pool = _pool(con, kind)
    random.Random(seed).shuffle(pool)
    sample = pool[:sample_n]
    if not quiet:
        print("  池 %d（claim 有逐字可查 quote 的）· 采样 %d" % (len(pool), len(sample)))
    tally = {}
    rows = []
    for i, p in enumerate(sample, 1):
        ctx = _materials(con, p["uid"], p["blocks"])
        try:
            out = call_llm(PROMPT.format(ctx=ctx, claim=p["claim"][:260]), timeout=45)
            m = re.search(r'"verdict"\s*:\s*"(support|partial|unrelated)"', out or "")
            w = re.search(r'"why"\s*:\s*"([^"]*)"', out or "")
            v = m.group(1) if m else "err"
            why = w.group(1) if w else ""
        except Exception:
            v, why = "err", ""
        tally[v] = tally.get(v, 0) + 1
        rows.append({"uid": p["uid"], "verdict": v, "why": why,
                     "claim": p["claim"][:120]})
        if not quiet:
            print("  [%2d] %-9s %s" % (i, v, why[:30]))
    n_ok = sum(v for k, v in tally.items() if k != "err")
    bad = tally.get("partial", 0) + tally.get("unrelated", 0)
    res = {"tally": tally, "n_valid": n_ok, "n_bad": bad,
           "rate": (100.0 * bad / n_ok) if n_ok else 0.0, "rows": rows}
    if not quiet:
        print()
        print("  合计: %s" % tally)
        print("  不支持率: %.1f%%（%d/%d，err 已排除）" % (res["rate"], bad, n_ok))
    return res

def main() -> int:
    ap = argparse.ArgumentParser(description="引文支持度抽样（每条一次 LLM 调用）")
    ap.add_argument("--sample", type=int, default=20, help="采样条数，默认 20")
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--kind", choices=list(_KINDS), default="insights",
                    help="insights=判断层 / key_points=事实层（实测后者不支持率更高）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（含明细）")
    a = ap.parse_args()
    r = run(a.sample, a.seed, quiet=a.json, kind=a.kind)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
