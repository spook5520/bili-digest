import sys, json, re, sqlite3
from collections import Counter

_EXPERIENCE = re.compile(r"我(当年|以前|之前|做过|参与|用过|也|遇到)|亲身|经历过|干过|从事")
_QUESTION = re.compile(r"[?？]$|请问|想问|求问")
_OPINION = re.compile(r"(太|很|真|特别)(好|差|烂|棒|对|错)|(不|没)(对|用|行|好)|支持|反对|赞|扯|离谱|胡说|笑死")

def _kind(text):
    t = (text or "").strip()
    if _QUESTION.search(t):
        return "提问"
    if _EXPERIENCE.search(t):
        return "经历补充"
    if _OPINION.search(t):
        return "立场表达"
    if len(t) >= 120:
        return "长评"
    return "短评"

def aggregate_comments(con, item_uid, per_kind=1, max_chars=90):
    where = "kind='comment'"
    args = ()
    if item_uid:
        where += " AND item_uid=?"
        args = (item_uid,)
    rows = con.execute(
        "SELECT text, weight FROM content_block WHERE " + where, args).fetchall()
    if not rows:
        return {"total": 0, "kinds": {}, "picks": []}
    kinds = Counter()
    by_kind = {}
    for r in rows:
        t = r["text"] or ""
        k = _kind(t)
        kinds[k] += 1
        w = r["weight"] or 0
        if k not in by_kind or w > by_kind[k][0]:
            by_kind[k] = (w, t)
    picks = []
    for k, (w, t) in sorted(by_kind.items(), key=lambda x: -x[1][0])[:per_kind * 3]:
        clean = " ".join(t.split())[:max_chars]
        picks.append((k, int(w), clean))
    return {"total": len(rows), "kinds": dict(kinds), "picks": picks}
