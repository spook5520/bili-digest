#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

GRANULARITIES = ("block", "item", "topic")

FALLBACK_VARIANTS = ()

RECALL_K = {"block": None, "item": 60, "topic": 120}

def describe_age(published_at, now=None) -> dict:
    import datetime as _dt
    s = str(published_at or "").strip()
    if not s:
        return {"date": None, "age_days": None, "age": "时间未知"}
    dt = None
    if s.isdigit():
        try:
            v = int(s)
            if v > 10 ** 12:
                v //= 1000
            dt = _dt.datetime.fromtimestamp(v)
        except Exception:
            dt = None
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = _dt.datetime.strptime(s[:19], fmt)
                break
            except Exception:
                continue
    if dt is None:
        return {"date": s[:10], "age_days": None, "age": s[:10]}
    now = now or _dt.datetime.now()
    days = max(0, (now - dt).days)
    if days == 0:
        age = "今天"
    elif days == 1:
        age = "昨天"
    elif days < 30:
        age = "%d 天前" % days
    elif days < 365:
        age = "%d 个月前" % (days // 30)
    else:
        age = "%.1f 年前" % (days / 365)
    return {"date": dt.strftime("%Y-%m-%d"), "age_days": days, "age": age}

def _rrf_items(variant_hits: list, k: int = 60) -> list:
    scores = {}
    for hits in variant_hits:
        for rank, h in enumerate(hits, 1):
            u = h.get("item_uid")
            if not u:
                continue
            scores[u] = scores.get(u, 0.0) + 1.0 / (k + rank)
    return [u for u, _s in sorted(scores.items(), key=lambda x: -x[1])]

def _block_hits(con, vi, question, top_k, **kw):
    import rag_core as rc
    return rc.hybrid(con, vi, question, top_k=top_k, **kw)

def _partitioned_hits(con, vi, model, question, top_k, **kw):
    import partitioned_search as ps
    return ps.search(con, vi, model, question, top_k, **kw)

def retrieve(con, vi, question: str, granularity: str = "block",
             top_k: int = 8, mode: str = "hybrid", model=None,
             termify: bool = None, **kw) -> list[dict]:
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity 必须是 {GRANULARITIES} 之一，收到 {granularity!r}")

    recall = RECALL_K.get(granularity) or top_k

    def recalls(n):
        if mode == "partitioned":
            if model is None:
                raise ValueError("mode='partitioned' 需要传入 model")
            raw = _partitioned_hits(con, vi, model, question, n, **kw)

            return [h for h in raw if "item_uid" in h]
        return _block_hits(con, vi, question, n, **kw)

    import os as _os_
    import rag_core as rc

    if termify is None:
        _TERMIFY = (_os_.environ.get("KB_TERMIFY") or "").strip() not in ("", "0", "false")
    else:
        _TERMIFY = bool(termify)

    def recalls_with_fallback(n):
        out = recalls(n)
        if _TERMIFY:
            try:
                import termify
                variants = termify.termify(question, n=3)
                if len(variants) > 1:
                    per = [recalls(n)]
                    for v in variants[1:]:
                        try:
                            per.append(_block_hits(con, vi, v, n, **kw))
                        except Exception:
                            continue
                    if len(per) > 1:

                        uid_score = {}
                        for hits in per:
                            best = {}
                            for rank, h in enumerate(hits, 1):
                                u = h.get("item_uid")
                                if u and u not in best:
                                    best[u] = rank
                            for u, rank in best.items():
                                uid_score[u] = uid_score.get(u, 0.0) \
                                    + 1.0 / (getattr(rc, "RRF_K", 60) + rank)
                        if uid_score:
                            out = sorted(out + [h for hits in per[1:] for h in hits],
                                         key=lambda h: -uid_score.get(h.get("item_uid"), 0))
            except Exception as _e:

                if (_os_.environ.get("KB_TERMIFY_DEBUG") or "").strip() not in ("", "0"):
                    import traceback
                    print("  [termify] 扩召回失败：%s: %s" % (type(_e).__name__, str(_e)[:80]))
                    traceback.print_exc()
            return out
        seen = {(h.get("block_id"), h.get("item_uid")) for h in out}
        for extra in FALLBACK_VARIANTS:
            kw2 = dict(kw)
            kw2.update(extra)
            try:

                for h in _block_hits(con, vi, question, n, **kw2):
                    key = (h.get("block_id"), h.get("item_uid"))
                    if key not in seen:
                        seen.add(key)
                        out.append(h)
            except Exception:
                continue
        return out

    if granularity == "block":
        return recalls_with_fallback(top_k)[:top_k]

    hits = recalls_with_fallback(recall)
    if not hits:
        return []

    if granularity == "item":
        return _aggregate_by_item(con, hits, top_k)
    return _aggregate_by_topic(con, hits, top_k)

def _score_of(h) -> float:
    return float(h.get("score", h.get("part_score", 0.0)) or 0.0)

def _aggregate_by_item(con, hits, top_k) -> list[dict]:
    best = {}
    for h in hits:
        uid = h["item_uid"]
        s = _score_of(h)
        if uid not in best or s > best[uid]["score"]:

            prev = best.get(uid)

            if prev is not None \
                    and _origin_of(con, prev["evidence"]) == "captured" \
                    and _origin_of(con, h) == "derived":
                best[uid]["score"] = max(prev["score"], s)
                continue
            best[uid] = {"score": s, "evidence": h,
                         "all_kinds": {h["kind"]}}
        else:
            best[uid]["all_kinds"].add(h["kind"])

    ranked = sorted(best.items(), key=lambda kv: -kv[1]["score"])[:top_k]
    out = []
    for uid, info in ranked:
        ev = info["evidence"]

        _age = {"age_days": None, "age": "时间未知", "date": None}
        item = con.execute(
            "SELECT title,url,item_id,author_name,saved_at,published_at,"
            "duration_sec FROM item WHERE uid=?",
            (uid,)).fetchone()
        _age = describe_age(item["published_at"] if item else None)
        summary = con.execute(
            "SELECT text FROM content_block WHERE item_uid=? AND kind='summary' "
            "ORDER BY id LIMIT 1", (uid,)).fetchone()
        out.append({
            "granularity": "item",
            "item_uid": uid,
            "item_id": item["item_id"] if item else None,
            "title": item["title"] if item else None,
            "url": item["url"] if item else None,
            "author_name": item["author_name"] if item else None,

            "published_at": item["published_at"] if item else None,
            "age_days": _age["age_days"],
            "age": _age["age"],
            "date": _age["date"],
            "score": round(info["score"], 4),
            "evidence": {
                "kind": ev["kind"], "text": ev["text"],
                "offset_sec": ev.get("offset_sec"),
                "origin_hint": _origin_of(con, ev),
            },
            "matched_kinds": sorted(info["all_kinds"]),
            "summary": (summary["text"] if summary else None),
            "n_hits": sum(1 for h in hits if h["item_uid"] == uid),
        })

    for h in hits:
        if "query_vec_sim" in h:
            out[0] = dict(out[0], query_vec_sim=h["query_vec_sim"])
            break
    return out

def _origin_of(con, h) -> str:
    if "origin" in h and h.get("origin"):
        return h["origin"]
    r = con.execute("SELECT origin FROM content_block WHERE id=?",
                    (h.get("block_id") or h.get("id"),)).fetchone()
    if r is None:
        return "captured"
    try:
        return r["origin"] or "captured"
    except (TypeError, IndexError):
        return r[0] or "captured"

def _aggregate_by_topic(con, hits, top_k) -> list[dict]:
    import json as _json
    uids = list({h["item_uid"] for h in hits})
    if not uids:
        return []
    ph = ",".join("?" * len(uids))
    topics_of = {}
    for r in con.execute(
            f"SELECT item_uid, content FROM derived WHERE kind='topics' "
            f"AND item_uid IN ({ph})", uids):
        try:
            topics_of[r["item_uid"]] = list(_json.loads(r["content"]))
        except (ValueError, TypeError):
            continue

    best_by_item = {}
    for h in hits:
        s = _score_of(h)
        if h["item_uid"] not in best_by_item or s > best_by_item[h["item_uid"]][0]:
            best_by_item[h["item_uid"]] = (s, h)

    groups = {}
    for uid, (s, h) in best_by_item.items():
        ts = topics_of.get(uid) or []

        for t in ts:
            g = groups.setdefault(t, {"topic": t, "items": [], "score": 0.0})
            g["items"].append({"item_uid": uid, "score": round(s, 4),
                               "evidence": h["text"][:80]})
            g["score"] = max(g["score"], s)

    out = []
    for g in sorted(groups.values(), key=lambda x: (-x["score"], -len(x["items"]))):
        g["items"].sort(key=lambda x: -x["score"])
        g["n_items"] = len(g["items"])
        g["score"] = round(g["score"], 4)
        g["granularity"] = "topic"
        out.append(g)
    return out[:top_k]

def main():
    import argparse
    ap = argparse.ArgumentParser(description="统一检索入口（按粒度）")
    ap.add_argument("query")
    ap.add_argument("--granularity", "-g", default="block",
                    choices=GRANULARITIES)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--mode", default="hybrid", choices=("hybrid", "partitioned"))
    ap.add_argument("--since")
    a = ap.parse_args()

    import sqlite3
    from paths import DB, VEC, EMBED_MODEL
    import rag_core as rc
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    model = None
    if a.mode == "partitioned":
        import partitioned_search as ps
        vi = ps._load_index()
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=EMBED_MODEL,
                              cache_dir=str(HERE / "models"))
    else:
        vi = rc.VectorIndex(str(VEC))

    kw = {}
    if a.since:
        kw["since"] = a.since
    res = retrieve(con, vi, a.query, granularity=a.granularity,
                   top_k=a.top_k, mode=a.mode, model=model, **kw)

    print(f"  「{a.query}」  粒度={a.granularity}  链路={a.mode}\n")
    for i, r in enumerate(res, 1):
        if a.granularity == "block":
            print(f"  [{i}] {_score_of(r):.3f} [{r['kind']}] {r['text'][:56]}")
        elif a.granularity == "item":
            print(f"  [{i}] {r['score']:.3f} 《{str(r['title'])[:36]}》"
                  f"  命中 {r['n_hits']} 块 {r['matched_kinds']}")
            ev = r["evidence"]
            print(f"        证据[{ev['kind']}/{ev['origin_hint']}] {ev['text'][:52]}")
            if r["summary"]:
                print(f"        摘要: {r['summary'][:56]}")
        else:
            print(f"  [{i}] {r['score']:.3f} 「{r['topic']}」"
                  f"  {r['n_items']} 个条目")
            for it in r["items"][:3]:
                print(f"        {it['score']:.3f} {it['evidence'][:52]}")
    con.close()

if __name__ == "__main__":
    main()
