import sys, json, re, sqlite3
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from bili_digest.tools import kb_ablation as ab
from bili_digest.schema.core_schema import connect
from bili_digest.paths import DB

BV = re.compile(r"BV[0-9A-Za-z]{10}")
SQL_SNIP = "SELECT kind, text FROM content_block WHERE item_uid=? AND kind IN ('subtitle_seg','subtitle') ORDER BY LENGTH(text) DESC LIMIT 3"

def _snippet(con, bv):
    uid = "bilibili:" + bv
    rows = con.execute(SQL_SNIP, (uid,)).fetchall()
    if rows:
        return "\n".join(r["text"][:220] for r in rows)
    r = con.execute("SELECT content FROM derived WHERE item_uid=? AND kind='summary'", (uid,)).fetchone()
    if r and r["content"]:
        return str(r["content"])[:300]
    it = con.execute("SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
    return "[库里只有标题] " + (it["title"] if it else "")

def judge(question, answer, bv, src):
    from bili_digest.distill.extract import _load_api_key
    import urllib.request
    prompt = ("判断一个引用是否成立。只输出 JSON。\n\n问题：" + question +
              "\n\n回答（可能提到多个视频）：\n" + answer[:900] +
              "\n\n被引用的视频 " + bv + " 在库里的实际内容：\n" + src[:700] +
              "\n\n请判断：回答里关于这个视频的说法，是否被上面的实际内容支持？\n"
              "返回 {\"verdict\": \"supported\"|\"unsupported\"|\"overstated\", \"why\": \"一句话\"}\n"
              "- supported   实际内容确实支持回答里的说法\n"
              "- unsupported 引用的内容与回答的说法不符\n"
              "- overstated  实际内容只说了一半，回答加了推断或夸大")
    key, info = _load_api_key()
    base, model = info.split("|", 1)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.1, "response_format": {"type": "json_object"},
                       "stream": False}).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    txt = d["choices"][0]["message"]["content"] or "{}"
    try:
        return json.loads(txt)
    except Exception:
        return {"verdict": "?", "why": txt[:80]}

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    con = connect(DB); con.row_factory = sqlite3.Row
    known = {r[0] for r in con.execute("SELECT item_id FROM item WHERE platform='bilibili' AND item_id IS NOT NULL")}
    seen, qs = set(), []
    for (q,) in con.execute("SELECT question FROM query_log WHERE question IS NOT NULL"):
        k = re.sub(r"[\W_]", "", (q or "").lower())
        if not k or k in seen or len(k) < 6: continue
        if "搜不到" in q or "zzz" in q.lower(): continue
        seen.add(k); qs.append(q)
    qs = qs[:n]
    print("问题 " + str(len(qs)))
    stat = {}
    samples = []
    for i, q in enumerate(qs, 1):
        try:
            ans = ab._ask_with_kb(q)
        except Exception as e:
            print("[%d] 失败 %s" % (i, type(e).__name__)); continue
        bvs = [b for b in set(BV.findall(ans)) if b in known]
        if not bvs:
            print("[%d] %-30s 无有效引证" % (i, q[:30])); continue
        vs = []
        for bv in bvs[:2]:
            v = judge(q, ans, bv, _snippet(con, bv))
            vd = v.get("verdict", "?")
            stat[vd] = stat.get(vd, 0) + 1
            vs.append((bv, vd, (v.get("why") or "")[:40]))
        print("[%d] %-26s %s" % (i, q[:26], " · ".join(b + "=" + d for b, d, _ in vs)))
        if any(d != "supported" for _, d, _ in vs) and len(samples) < 3:
            samples.append((q, vs))
    tot = sum(v for k, v in stat.items() if k != "?")
    print()
    print("=== 判定分布（" + str(tot) + " 条引用）===")
    for k in ("supported", "overstated", "unsupported", "?"):
        if stat.get(k):
            print("  %-12s %3d  %.0f%%" % (k, stat[k], 100*stat[k]/max(1, tot)))
    print()
    for q, vs in samples:
        print("反例: " + q[:40])
        for bv, d, w in vs:
            print("   %s -> %s · %s" % (bv, d, w))

if __name__ == "__main__":
    main()
