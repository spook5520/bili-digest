import re
import sqlite3
import sys
from collections import defaultdict

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

_TIER = {
    "meta": (1, "UP 主动引用"),
    "summary": (2, "提炼时提到"),
    "key_points": (2, "提炼时提到"),
    "insights": (2, "提炼时提到"),
    "seg_summary": (2, "提炼时提到"),
}

_PAIRED = ("配套", "资料", "上篇", "下篇", "前置", "课程", "完整版",
           "全集", "字幕版", "中字", "原曲", "本家", "原作", "出处")

def cmd_clues(a) -> None:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    con = connect(DB)
    con.row_factory = sqlite3.Row

    limit = getattr(a, "limit", 40)
    tier = getattr(a, "tier", 0)

    have = {r[0] for r in con.execute(
        "SELECT item_id FROM item WHERE platform='bilibili' AND item_id IS NOT NULL")}

    seen = defaultdict(set)
    scanned = 0
    for r in con.execute("SELECT item_uid, kind, text FROM content_block "
                         "WHERE text LIKE '%BV%'"):
        scanned += 1
        uid, kind = r[0], (r[1] or "?")
        for bv in set(BV_RE.findall(r[2] or "")):
            seen[bv].add((uid, kind))

    missing = {bv: v for bv, v in seen.items() if bv not in have}

    _exp = getattr(a, "export", None)
    if _exp:
        _rows = []
        for _bv, _hits in sorted(missing.items(), key=lambda kv: -len(kv[1])):
            _ks = {k for _u, k in _hits}
            _t = 1 if "meta" in _ks else (
                2 if (_ks & {"key_points", "summary", "insights"}) else 3)
            if tier and _t != tier:
                continue
            _rows.append(_bv)
        with open(_exp, "w", encoding="utf-8", newline="\n") as _f:
            _f.write("\n".join(_rows) + "\n")
        print("导出 %d 个候选 BV -> %s" % (len(_rows), _exp))
        print("下一步（分批 30~40 个 —— Windows 命令行上限 8191 字符）：")
        print("  python -m bili_digest fetch " + " ".join(_rows[:5]) + " ...")
        con.close()
        return

    titles = {}
    for r in con.execute("SELECT uid, title, author_name FROM item"):
        titles[r[0]] = ((r[1] or ""), (r[2] or ""))

    src_authors = {}

    tiers = defaultdict(list)
    for bv, pairs in missing.items():
        best = min((_TIER.get(k, (3, "观众/其它"))[0] for _, k in pairs), default=3)
        n_items = len({u for u, _ in pairs})
        tiers[best].append((bv, n_items, pairs))

    print("扫了 %d 块 · 出现 %d 个不同 BV · **不在库 %d 个**"
          % (scanned, len(seen), len(missing)))
    print()
    print("按来源分层（越靠上越可能是真缺口）：")
    for t in (1, 2, 3):
        if tiers[t]:
            label = {1: "meta — UP 主动引用", 2: "提炼时提到", 3: "观众/其它"}[t]
            print("   第 %d 层 %-22s %4d 个" % (t, label, len(tiers[t])))
    print()

    show = [(t, x) for t in (1, 2, 3) if (tier in (0, t)) for x in sorted(tiers[t], key=lambda y: -y[1])]
    if not show:
        print("  这一层没有候选。")
        con.close()
        return

    cur = None
    for t, (bv, n_items, pairs) in show[:limit]:
        if t != cur:
            cur = t
            label = {1: "第 1 层 · UP 主动引用（最可能是真缺口）",
                     2: "第 2 层 · 提炼时提到", 3: "第 3 层 · 观众/其它（需人工扫，可能推广/灌水）"}[t]
            print("── %s ──" % label)
        kinds = "/".join(sorted({k for _, k in pairs}))
        src_uid = next(iter({u for u, _ in pairs}))
        src_title, src_author = titles.get(src_uid, ("", ""))

        print("  %s  %d 个条目提到 · 来源 %s" % (bv, n_items, kinds))
        if src_title:
            print("      被提到时所在的条目：%s" % src_title[:44])

        if src_title and any(w in src_title for w in ("教程", "入门", "指南", "第", "系列")):
            print("      （源是教程类 —— 它引的多半是配套课或前置资料，优先看）")
        print("      要看它是什么：python -m bili_digest fetch %s" % bv)

    print()
    print("这不是「B站上有、你没有」（那是 kb discover），")
    print("而是「**你自己的收藏指向了它、但你从没收藏**」。")
    print("输出是候选 + 来源，不是判决 —— 其中有一部分是你故意不收的。")
    con.close()
