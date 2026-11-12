import argparse
import json
import re
import sys
import sqlite3

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
TS_RE = re.compile(r"\b\d{1,2}:\d{2}\b")

def _ask_plain(question: str) -> str:
    from bili_digest.distill.extract import _load_api_key
    import urllib.request

    key, info = _load_api_key()
    base, model = info.split("|", 1)
    prompt = (
        "回答下面这个问题。如果你不知道确切答案，直接说不知道，不要编造出处。\n\n"
        "问题：%s" % question)
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.3, "stream": False}).encode("utf-8")
    req = urllib.request.Request("%s/chat/completions" % base, data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer %s" % key)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    return (d["choices"][0]["message"]["content"] or "").strip()

def _ask_with_kb(question: str) -> str:
    from bili_digest.distill.extract import _load_api_key
    from bili_digest.mcp.server import kb_ask
    import urllib.request

    raw = kb_ask(question, top_k=8)
    body_txt = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        kb = json.loads(body_txt)
    except Exception:
        kb = {}

    ctx = []
    for it in (kb.get("items") or [])[:3]:
        ctx.append("- [%s] %s%s" % (
            it.get("item_uid", "").split(":")[-1],
            (it.get("text") or "")[:300],
            (" @" + it["time"]) if it.get("time") else ""))
    verdict = kb.get("next") or ""
    context = "\n".join(ctx) or "（库里没有相关内容）"

    key, info = _load_api_key()
    base, model = info.split("|", 1)
    prompt = (
        "下面是用户本地知识库对这个问题的检索结果，以及一个自动判定。\n"
        "请优先基于这些结果回答；结果里没有的，明确说「你收藏里没有」。\n\n"
        "问题：%s\n\n判定：%s\n\n检索结果：\n%s" % (question, verdict, context))
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.3, "stream": False}).encode("utf-8")
    req = urllib.request.Request("%s/chat/completions" % base, data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer %s" % key)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    return (d["choices"][0]["message"]["content"] or "").strip()

def _score(ans: str, known: set) -> dict:
    bvs = set(BV_RE.findall(ans))
    return {"bv_total": len(bvs),
            "bv_real": len(bvs & known),
            "bv_fake": len(bvs - known),
            "timestamps": len(TS_RE.findall(ans)),
            "chars": len(ans)}

def main() -> None:
    from bili_digest.paths import DB

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    known = {r[0] for r in con.execute(
        "SELECT item_id FROM item WHERE platform='bilibili' AND item_id IS NOT NULL")}

    seen, qs = set(), []
    for (q,) in con.execute("SELECT question FROM query_log WHERE question IS NOT NULL"):
        k = re.sub(r"[\W_]", "", (q or "").lower())
        if not k or k in seen or len(k) < 6:
            continue
        if "搜不到" in q or "zzz" in q.lower():
            continue
        seen.add(k)
        qs.append(q)
    qs = qs[:a.n]
    print("问题 %d 个\n" % len(qs))

    rows = []
    for i, q in enumerate(qs, 1):
        try:
            plain = _ask_plain(q)
        except Exception as e:
            plain = "[失败 %s]" % type(e).__name__
        try:
            withkb = _ask_with_kb(q)
        except Exception as e:
            withkb = "[失败 %s]" % type(e).__name__
        s1, s2 = _score(plain, known), _score(withkb, known)
        rows.append({"q": q, "plain": s1, "kb": s2})
        print("[%2d/%d] %s" % (i, len(qs), q[:34]))
        print("        不接: 引证%d 编造%d 时间戳%d %d字" %
              (s1["bv_real"], s1["bv_fake"], s1["timestamps"], s1["chars"]))
        print("        接库: 引证%d 编造%d 时间戳%d %d字" %
              (s2["bv_real"], s2["bv_fake"], s2["timestamps"], s2["chars"]))

    def tot(k, f):
        return sum(r[k][f] for r in rows)

    print()
    print("=== 汇总（n=%d）===" % len(rows))
    print("%-10s %8s %8s %10s %8s" % ("条件", "引证", "编造", "时间戳", "字数"))
    for name, k in (("不接库", "plain"), ("接库", "kb")):
        print("%-10s %8d %8d %10d %8d" % (
            name, tot(k, "bv_real"), tot(k, "bv_fake"),
            tot(k, "timestamps"), tot(k, "chars")))
    print()
    print("判据：引证数高 = 答得出库里的具体内容；编造数高 = 在瞎说；")
    print("      时间戳只有接了库才拿得到（那是这个库的独有能力）。")

if __name__ == "__main__":
    main()
