#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

def norm_key(t: str) -> str:
    return re.sub(r"[\s_\-·]+", "", t.strip().lower())

def canonical(t: str) -> str:
    return t.strip()

def merge_by_rule(all_topics: Counter) -> dict:
    groups = defaultdict(list)
    for t in all_topics:
        groups[norm_key(t)].append(t)
    mapping = {}
    for k, variants in groups.items():
        if len(variants) == 1:
            continue

        rep = max(variants, key=lambda x: (len(x), x))
        for v in variants:
            if v != rep:
                mapping[v] = rep
    return mapping

def merge_by_llm(con, all_topics: Counter, min_count: int = 1,
                 verbose: bool = True) -> dict:
    import extract
    words = [t for t, n in all_topics.most_common() if n >= min_count]
    if len(words) < 2:
        return {}

    prompt = (
        "下面是一批从视频内容里提取的主题词。请把【指同一事物】的合并成一组，"
        "没同义的就单独保留。\n\n"
        f"主题词：{json.dumps(words, ensure_ascii=False)}\n\n"
        "要求：\n"
        "1. 只合并确实同义的（'GTA4'/'GTA 4'、'安卓逆向'/'Android逆向'）\n"
        "2. 不要合并上下位关系（'游戏' 与 'GTA' 不是同义）\n"
        "3. 每组给一个代表词（优先用更通用的中文）\n"
        "4. 输出 JSON：[{\"canonical\":\"代表词\",\"aliases\":[\"别名1\",\"别名2\"]}, ...]\n"
        "5. 只输出有合并的组；没有同义的就输出 []\n"
    )
    try:
        res = extract.call_llm(prompt, timeout=180)
    except SystemExit as e:
        if verbose:
            print(f"  LLM 不可用（未配 key）: {str(e).splitlines()[0]}")
        return {}
    except Exception as e:
        if verbose:
            print(f"  LLM 归并失败: {type(e).__name__}: {str(e)[:60]}")
        return {}

    groups = None
    try:
        s = res[res.find("["): res.rfind("]") + 1]
        groups = json.loads(s)
    except Exception:

        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", res, re.S)
        if m:
            try:
                groups = json.loads(m.group(1))
            except Exception:
                groups = None
    if not isinstance(groups, list):
        if verbose:
            print(f"  LLM 返回无法解析（前 80 字）: {res[:80]!r}")
        return {}

    mapping = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        rep = str(g.get("canonical") or "").strip()
        if not rep:
            continue
        for al in (g.get("aliases") or []):
            al = str(al or "").strip()
            if al and al != rep:
                mapping[al] = rep
    return mapping

def build_vocab(con, min_count: int = 2) -> Counter:
    c = Counter()
    for r in con.execute("SELECT content FROM derived WHERE kind='topics'"):
        try:
            for t in json.loads(r["content"]):
                if isinstance(t, str) and t.strip():
                    c[t.strip()] += 1
        except Exception:
            continue
    return c

def apply_mapping(con, mapping: dict, source: str = "rule") -> int:
    from core_schema import now_sec
    n = 0
    if mapping:
        con.executemany(
            "INSERT OR REPLACE INTO topic_alias(alias, canonical, source, ts) "
            "VALUES(?,?,?,?)",
            [(a, c, source, now_sec()) for a, c in mapping.items() if a != c])
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='topics'"):
        try:
            ts = json.loads(r["content"])
        except Exception:
            continue
        new = []
        for t in ts:
            t2 = mapping.get(t, t)
            if t2 not in new:
                new.append(t2)
        if new != ts:
            con.execute("UPDATE derived SET content=? WHERE item_uid=? AND kind='topics'",
                        (json.dumps(new, ensure_ascii=False), r["item_uid"]))
            n += 1
    con.commit()
    return n

def prompt_hint(con, top_n: int = 40, context: str = None) -> str:
    c = build_vocab(con)
    vocab = [t for t, n in c.items() if n >= 2]
    if not vocab:
        return ""

    if context:

        blob = context.lower()
        relevant = [t for t in vocab if t.lower() in blob]
        relevant.sort(key=lambda t: -c[t])

        rel_set = set(relevant[:top_n // 2])
        fill = [t for t, n in c.most_common(top_n) if n >= 2 and t not in rel_set]
        rest = fill[: max(0, top_n - len(rel_set))]
        if rel_set:
            head = "、".join(sorted(rel_set, key=lambda t: -c[t]))
            tail = ("\n另外这些是常用的泛词（能用上才用，不必勉强）：" + "、".join(rest)) if rest else ""
            return ("【已有主题词，本文可能用得上，优先复用】" + head
                    + "\n（以上词在本条内容里出现过，能用它们表达就别造新词。）"
                    + tail)

        return ("\n（本条内容与库里已有主题词都对不上 —— "
                "请为该主题【新造】准确的主题词，不要硬塞到泛泛的旧词里。）")

    common = [t for t, n in c.most_common(top_n) if n >= 2]
    if not common:
        return ""
    return ("【已有主题词，优先复用】" + "、".join(common)
            + "\n（以上是知识库里已有的主题词。能用它们表达就别造新词；"
              "但如果确实有新概念，照常新增 —— 不要硬塞。）")

def ensure_table(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS topic (
        name        TEXT PRIMARY KEY,      -- 规范名
        aliases     TEXT,                  -- JSON 数组：归并前的各种写法
        n_items     INTEGER DEFAULT 0,     -- 归属条目数
        first_seen  INTEGER,               -- 首次出现时间
        last_seen   INTEGER,
        parent      TEXT                   -- 上位主题（可选，用于层级）
    );
    CREATE TABLE IF NOT EXISTS topic_member (
        topic       TEXT NOT NULL,
        item_uid    TEXT NOT NULL,
        weight      REAL DEFAULT 1.0,
        PRIMARY KEY (topic, item_uid)
    );
    CREATE INDEX IF NOT EXISTS idx_tm_topic ON topic_member(topic);
    -- 归并映射的持久化。为什么必须独立成表：
    --   （注：这里的动机是"映射会随重建丢失"，不是"原写法搜不到" ——
    --     后者实测不成立，见 apply_mapping 的 docstring。）
    --   topic.aliases 是【衍生】字段，而 rebuild_entity 会先 DELETE FROM topic
    --   再从 derived.topics 重建 —— 那张表里只剩代表词，原名已经没了。
    --   所以若把映射只存在 topic.aliases 里，重建一次就丢一次。
    --   实测的现状正是如此：319 个主题，aliases 全空（一个都没填过），
    --   词数从 621 收敛到 319 的过程没有任何记录 —— 归并变成了静默丢弃。
    CREATE TABLE IF NOT EXISTS topic_alias (
        alias       TEXT PRIMARY KEY,   -- 归并前的写法
        canonical   TEXT NOT NULL,      -- 归并到的代表词
        source      TEXT,               -- rule / llm
        ts          INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_tm_item ON topic_member(item_uid);
    """)

def rebuild_entity(con) -> dict:
    ensure_table(con)
    con.execute("DELETE FROM topic")
    con.execute("DELETE FROM topic_member")
    import core_schema as cs
    ts_now = cs.now_sec()
    per_topic = defaultdict(set)
    for r in con.execute("""SELECT d.item_uid, d.content, i.published_at
                            FROM derived d LEFT JOIN item i ON i.uid=d.item_uid
                            WHERE d.kind='topics'"""):
        try:
            ts = json.loads(r["content"])
        except Exception:
            continue
        for t in ts:
            if isinstance(t, str) and t.strip():
                per_topic[t.strip()].add(r["item_uid"])

    aliases = defaultdict(list)
    try:
        for r in con.execute("SELECT alias, canonical FROM topic_alias"):
            aliases[r["canonical"]].append(r["alias"])
    except Exception:
        pass
    for name, uids in per_topic.items():
        for uid in uids:
            con.execute("INSERT OR REPLACE INTO topic_member(topic,item_uid,weight) "
                        "VALUES(?,?,1.0)", (name, uid))
        al = sorted(aliases.get(name) or [])
        con.execute("""INSERT OR REPLACE INTO topic(name,aliases,n_items,first_seen,last_seen)
                       VALUES(?,?,?,?,?)""",
                    (name, json.dumps(al, ensure_ascii=False) if al else None,
                     len(uids), ts_now, ts_now))
    con.commit()
    n_multi = sum(1 for u in per_topic.values() if len(u) >= 2)
    return {"topics": len(per_topic), "members": sum(len(u) for u in per_topic.values()),
            "multi_item_topics": n_multi}

def _cooc_docs(con):
    from collections import defaultdict as _dd
    docs = _dd(set)
    for r in con.execute("SELECT term, block_id FROM keyword_block"):
        docs[r[0]].add(r[1])
    n = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
    return docs, n

def _split_for_coherence(term, docs, max_parts: int = 3) -> list:
    if term in docs:
        return [term]
    import jieba as _jb
    _jb.setLogLevel(60)
    parts = [str(w).strip().lower() for w in _jb.cut(str(term))]
    return [w for w in parts if len(w) >= 2 and w in docs][:max_parts]

def npmi_pair(a, b, docs, n) -> float:
    da, db = docs[a], docs[b]
    both = len(da & db)
    if both == 0:
        return -1.0
    p_both = both / n
    import math as _m
    return _m.log(p_both / (len(da) / n * len(db) / n)) / (-_m.log(p_both))

def coherence(terms, docs, n):
    vals = []
    for t in terms:
        parts = _split_for_coherence(t, docs)
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                vals.append(npmi_pair(parts[i], parts[j], docs, n))
    return (sum(vals) / len(vals)) if vals else None

def coherence_report(con, worst: int = 5, best: int = 5) -> dict:
    import json as _j
    import statistics as _st
    docs, n = _cooc_docs(con)
    vals, skipped = [], 0
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='topics'"):
        try:
            ts = [str(x) for x in _j.loads(r[1] or "[]")]
        except Exception:
            continue
        if not ts:
            continue
        c = coherence(ts, docs, n)
        if c is None:
            skipped += 1
            continue
        vals.append((c, r[0], ts))
    vals.sort(reverse=True)
    out = {"n": len(vals), "skipped": skipped,
           "median": _st.median([v[0] for v in vals]) if vals else None,
           "mean": (sum(v[0] for v in vals) / len(vals)) if vals else None,
           "best": vals[:best], "worst": vals[-worst:] if vals else []}
    return out

def main():
    import argparse
    ap = argparse.ArgumentParser(description="主题词收敛与实体化")
    ap.add_argument("--report", action="store_true", help="只看现状")
    ap.add_argument("--apply-rule", action="store_true", help="应用规则归并")
    ap.add_argument("--apply-llm", action="store_true", help="应用 LLM 语义归并")
    ap.add_argument("--llm-min-count", type=int, default=2,
                    help="LLM 归并只处理出现≥N次的词（长尾单次词无归并收益）")
    ap.add_argument("--rebuild", action="store_true", help="重建主题实体表")
    ap.add_argument("--hint", action="store_true", help="输出提炼用的词表提示")
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    if a.hint:
        print(prompt_hint(con))
        con.close()
        return

    c = build_vocab(con)
    multi = {t: n for t, n in c.items() if n >= 2}
    print(f"═══ 主题词现状 ═══")
    print(f"  总数 {len(c)} · 出现≥2次的 {len(multi)} · 单次 {len(c)-len(multi)}"
          f" ({(len(c)-len(multi))/max(len(c),1)*100:.0f}%)")

    rule_map = merge_by_rule(c)
    print(f"\n  规则可归并（同义词不同写法）: {len(rule_map)} 组")
    for k, v in list(rule_map.items())[:10]:
        print(f"    {k!r} → {v!r}")

    if a.apply_rule and rule_map:
        n = apply_mapping(con, rule_map)
        print(f"\n  已更新 {n} 条 derived.topics")
        c = build_vocab(con)
        multi2 = {t: n for t, n in c.items() if n >= 2}
        print(f"  归并后：总数 {len(c)} · 出现≥2次的 {len(multi2)}")

    if a.apply_llm:
        print(f"\n═══ LLM 语义归并（处理出现≥{a.llm_min_count}次的词）═══")
        llm_map = merge_by_llm(con, c, min_count=a.llm_min_count)
        if llm_map:
            print(f"  归并映射 {len(llm_map)} 条:")
            for k, v in list(llm_map.items())[:15]:
                print(f"    {k!r} → {v!r}")
            n = apply_mapping(con, llm_map)
            print(f"\n  已更新 {n} 条 derived.topics")
            c = build_vocab(con)
            multi3 = {t: n for t, n in c.items() if n >= 2}
            print(f"  归并后：总数 {len(c)} · 出现≥2次的 {len(multi3)}")
        else:
            print("  没有可归并的（或 LLM 不可用）")

    if a.rebuild:
        r = rebuild_entity(con)
        print(f"\n═══ 主题实体 ═══")
        print(f"  topic {r['topics']} 个 · 归属 {r['members']} 条 · "
              f"被≥2条目共享的 {r['multi_item_topics']} 个")
        print(f"\n  规模最大的主题:")
        for row in con.execute("SELECT name, n_items FROM topic ORDER BY n_items DESC LIMIT 12"):
            print(f"    {row['n_items']:>3}  {row['name']}")
    con.close()

if __name__ == "__main__":
    main()
