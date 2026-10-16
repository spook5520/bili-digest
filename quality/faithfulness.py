import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sys, re, json
sys.stdout.reconfigure(encoding="utf-8"); sys.path.insert(0, ".")
from core_schema import connect
from paths import DB

NUM = re.compile(r"(?<!\d)\d{1,6}(?:\.\d+)?%?(?!\d)")
LAT = re.compile(r"[A-Z][A-Za-z0-9\-]{2,}")

GENERIC = {
    "UP", "AI", "CPU", "GPU", "API", "URL", "URI", "ID", "PC", "APP",
    "OK", "TXT", "PDF", "JSON", "HTTP", "HTTPS", "HTML", "CSS", "SDK",
    "USB", "HDMI", "TV", "OS", "UI", "UX", "MCP", "RAG", "LLM", "FPS",
}

ZH_POS = ("n", "nz", "nr", "ns", "nt", "vn", "eng")

def entities(text: str) -> set:
    out = set()
    s = text or ""
    for e in list(NUM.findall(s)) + list(LAT.findall(s)):
        if e not in GENERIC:
            out.add(e)
    try:
        import jieba.posseg as pseg
        for w, f in pseg.cut(s):
            if len(w) >= 2 and f in ZH_POS and w not in GENERIC:
                out.add(w)
    except Exception:
        pass
    return out

def materials(row, q):
    parts = [str(row["title"] or "")]
    d = row["duration_sec"] or 0
    if d:
        parts += [f"{d//60}分", f"{d%60}秒", str(d), f"{d//60}:{d%60:02d}"]
    parts += [x[0] or "" for x in q(
        "SELECT text FROM content_block WHERE item_uid=?", (row["uid"],)).fetchall()]
    try:
        ex = json.loads(row["extras"] or "{}")
        parts += [str(v) for v in ex.values()]
    except Exception:
        pass
    return " ".join(parts)

def main() -> int:
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    q = con.execute
    rows = q("""SELECT d.item_uid, d.content, i.uid, i.item_id, i.title, i.duration_sec, i.extras
        FROM derived d JOIN item i ON i.uid=d.item_uid WHERE d.kind='summary'""").fetchall()
    tot = miss_tot = 0
    bad = []
    for r in rows:
        s = str(r["content"])
        body = materials(r, q)
        ents = entities(s)
        miss = sorted(e for e in ents if e not in body)
        tot += len(ents); miss_tot += len(miss)
        if miss:
            bad.append((r["item_id"], str(r["title"])[:28], miss))
    print(f"  检查 {len(rows)} 条摘要 · 实体 {tot} 个 · 找不到 {miss_tot}（{miss_tot/max(tot,1):.1%}）\n")
    print(f"  仍有未找到的 {len(bad)} 条：")
    for bv, t, m in bad[:10]:
        print(f"    {bv}  《{t}》  未找到: {m[:5]}")
    if not bad:
        print("    （无）")

    print("\n" + "=" * 56)
    print("  Coverage：素材里反复出现、且不是全局常见的实体，提炼覆盖了多少\n")
    MIN_OCCUR = 3
    GLOBAL_DF = 0.05

    _all_blocks = q("SELECT item_uid, text FROM content_block "
                    "WHERE kind IN ('subtitle','subtitle_seg','comment')").fetchall()
    _df = {}
    _n_items = len({x["item_uid"] for x in _all_blocks}) or 1
    for b in _all_blocks:
        for e in entities(b["text"] or ""):
            _df[e] = _df.get(e, 0) + 1
    _common = {e for e, n in _df.items() if n / _n_items > GLOBAL_DF}

    cov_rows = cov_tot = cov_in = 0
    worst = []
    for r in rows:
        blocks = [x[0] or "" for x in q(
            "SELECT text FROM content_block WHERE item_uid=? "
            "AND kind IN ('subtitle','subtitle_seg','comment')", (r["uid"],)).fetchall()]
        if not blocks:
            continue
        freq = {}
        for b in blocks:
            for e in entities(b):
                freq[e] = freq.get(e, 0) + 1
        key = {e for e, n in freq.items()
               if n >= MIN_OCCUR and len(e) >= 2 and e not in _common}
        if not key:
            continue
        summ = str(r["content"])
        got = {e for e in key if e in summ}
        cov_rows += 1
        cov_tot += len(key); cov_in += len(got)
        if len(got) / len(key) < 0.3:
            worst.append((r["item_id"], str(r["title"])[:26], len(key), sorted(key - got)[:4]))
    print(f"  过滤掉 {len(_common)} 个全局常见实体")
    print(f"  可测条目 {cov_rows} · 该覆盖的实体 {cov_tot} 个 · 提炼覆盖 {cov_in}"
          f"（{cov_in/max(cov_tot,1):.1%}）")
    print()
    print("  ★ 这个数【不能当作「提炼漏了多少」】，但理由和先前不同了：")
    print("    · 判据已从「大写拉丁词 + 数字」扩到「+ jieba 中文名词」。"
          "此前素材以中文为主却抽不到中文实体，Coverage 恒低且无意义"
          "（实测口径修正后 6.7% → 11.2%）。长号码（用户 ID / 弹幕 ID）"
          "按位数排除，通用缩写另有一张表。")
    print("    · 剩下的问题不在抽取，而在【「该覆盖」本身是主观的】："
          "素材里出现 3 次以上的实体未必都该进摘要 —— 摘要本就有取舍，"
          "只覆盖要点。要下结论需要「该不该覆盖」的人工标注。")
    print("    · 因此 Coverage 这一层【仍不做结论】；"
          "Conformity（上面那个 0% 编造）才是可信的那半。")

    print("\n" + "=" * 56)
    print("  Consistency：summary 与 key_points 之间是否互相矛盾\n")
    VER = re.compile(r"\b(\d+)\.(\d+(?:\.\d+)?)\b")
    DUR = re.compile(r"时长[^0-9]{0,6}(\d+)\s*分")
    drows = q("""SELECT d.item_uid, d.kind, d.content, i.item_id, i.title, i.duration_sec
        FROM derived d JOIN item i ON i.uid=d.item_uid
        WHERE d.kind IN ('summary','key_points')""").fetchall()
    _by = {}
    for r in drows:
        _by.setdefault(r["item_uid"], {})[r["kind"]] = (str(r["content"]), r)
    bad = []
    for uid, ks in _by.items():
        if "summary" not in ks or "key_points" not in ks:
            continue
        s, sr = ks["summary"]
        kp, _kr = ks["key_points"]
        vs, vk = set(VER.findall(s)), set(VER.findall(kp))
        if vs and vk and not (vs & vk):
            bad.append((sr["item_id"], str(sr["title"])[:26], "版本号不同",
                        f"summary {sorted(vs)[:2]} vs key_points {sorted(vk)[:2]}"))
        d = sr["duration_sec"] or 0
        if d:
            for m in DUR.finditer(s):
                if abs(int(m.group(1)) - d // 60) > 5:
                    bad.append((sr["item_id"], str(sr["title"])[:26], "时长不符",
                                f"说 {m.group(1)} 分 · 实际 {d//60} 分"))
    n_pairs = sum(1 for u in _by if len(_by[u]) == 2)
    print(f"  检查 {n_pairs} 条（summary 与 key_points 都有的）· 发现矛盾 {len(bad)} 处")
    for bv, t, kind, detail in bad[:10]:
        print(f"    ✗ {bv}  《{t}》  {kind}：{detail}")
    if not bad:
        print("    （无）")

    print("\n" + "=" * 56)
    print("  结论 vs 提炼：数值是否对得上（仅提示）\n")
    MIN_NOTES = 8
    nrows = q("""SELECT a.item_uid, a.text AS note, i.item_id, i.title,
        (SELECT GROUP_CONCAT(content,' ') FROM derived d WHERE d.item_uid=a.item_uid
         AND d.kind IN ('summary','key_points')) AS der
        FROM annotation a JOIN item i ON i.uid=a.item_uid""").fetchall()
    if len(nrows) < MIN_NOTES:
        print(f"  只有 {len(nrows)} 条结论（门槛 {MIN_NOTES}）—— 样本不足，不下结论")
        for r in nrows:
            print(f"    · {r['item_id']}  {str(r['note'])[:52]}")
    else:
        hinted = 0
        for r in nrows:
            vn, vd = set(NUM.findall(str(r["note"]))), set(NUM.findall(str(r["der"] or "")))
            if vn and vd and not (vn & vd):
                print(f"    ? {r['item_id']}  结论的数值 {sorted(vn)[:3]} 在提炼里没出现")
                hinted += 1
        print(f"  {len(nrows)} 条结论 · 提示 {hinted} 条（仅是提示，不是判定）")
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
