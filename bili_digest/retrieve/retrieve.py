#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

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
    from bili_digest.retrieve import rag_core as rc
    return rc.hybrid(con, vi, question, top_k=top_k, **kw)

def _partitioned_hits(con, vi, model, question, top_k, **kw):
    from bili_digest.retrieve import partitioned_search as ps
    return ps.search(con, vi, model, question, top_k, **kw)

def retrieve(con, vi, question: str, granularity: str = "block",
             top_k: int = 8, mode: str = "hybrid", model=None,
             termify: bool = None, **kw) -> list[dict]:
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity 必须是 {GRANULARITIES} 之一，收到 {granularity!r}")

    _max_per_item = kw.pop("max_per_item", 0)

    _include_aud = kw.pop("include_audience_blocks", False)

    recall = RECALL_K.get(granularity) or top_k

    def recalls(n):
        if mode == "partitioned":
            if model is None:
                raise ValueError("mode='partitioned' 需要传入 model")
            raw = _partitioned_hits(con, vi, model, question, n, **kw)

            return [h for h in raw if "item_uid" in h]
        return _block_hits(con, vi, question, n, **kw)

    import os as _os_
    from bili_digest.retrieve import rag_core as rc

    if termify is None:
        _TERMIFY = (_os_.environ.get("KB_TERMIFY") or "").strip() not in ("", "0", "false")
    else:
        _TERMIFY = bool(termify)

    def recalls_with_fallback(n):
        out = recalls(n)
        if _TERMIFY:
            try:
                from bili_digest.retrieve import termify
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

        _n = max(top_k * 10, 100)
        _hits = recalls_with_fallback(_n)
        if not _include_aud and not kw.get("kinds"):
            _hits = [h for h in _hits
                     if h.get("kind") not in ("comment", "danmaku")]

        _mpp = _max_per_item
        if _mpp and _mpp > 0:
            _cnt, _keep = {}, []
            for _h in _hits:
                _u = _h.get("item_uid")
                if _cnt.get(_u, 0) >= _mpp:
                    continue
                _cnt[_u] = _cnt.get(_u, 0) + 1
                _keep.append(_h)
                if len(_keep) >= top_k:
                    break
            _hits = _keep
        return _hits[:top_k]

    hits = recalls_with_fallback(recall)
    if not hits:
        return []

    if granularity == "item":
        return _aggregate_by_item(con, hits, top_k, question=question)
    return _aggregate_by_topic(con, hits, top_k)

LATEON_DEPTH = 30

def _score_of(h) -> float:
    return float(h.get("score", h.get("part_score", 0.0)) or 0.0)

def _aggregate_by_item(con, hits, top_k, question: str = "") -> list[dict]:
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

    ranked = sorted(best.items(), key=lambda kv: -kv[1]["score"])

    if question and len(ranked) > 1:
        try:
            from bili_digest.retrieve import lateon as _lo
            _head = ranked[:LATEON_DEPTH]
            _order = _lo.rerank(con, question, [u for u, _i in _head])
            if _order and _order != [u for u, _i in _head]:
                _pos = {u: i for i, u in enumerate(_order)}
                _head.sort(key=lambda kv: _pos.get(kv[0], len(_pos)))
                ranked = _head + ranked[LATEON_DEPTH:]
        except Exception:
            pass

    ranked = ranked[:top_k]

    _uids = [u for u, _i in ranked]
    _ph = ",".join("?" * len(_uids)) if _uids else "NULL"
    _items = {}
    _sums = {}
    _covs = {}
    if _uids:
        for r in con.execute(
                "SELECT uid,title,url,item_id,author_name,saved_at,published_at,"
                "duration_sec FROM item WHERE uid IN (" + _ph + ")", _uids):
            _items[r[0]] = r
        for r in con.execute(
                "SELECT item_uid, text FROM content_block "
                "WHERE item_uid IN (" + _ph + ") AND kind='summary' "
                "ORDER BY id", _uids):
            _sums.setdefault(r[0], r[1])
        try:
            from bili_digest.quality import kb_cov as _covb
            for r in con.execute(
                    "SELECT item_uid, kind, COUNT(*) FROM content_block "
                    "WHERE item_uid IN (" + _ph + ") GROUP BY item_uid, kind", _uids):
                _covs.setdefault(r[0], {})[r[1]] = r[2]
        except Exception:
            _covs = {}

    _nhits = {}
    for _h in hits:
        _nhits[_h["item_uid"]] = _nhits.get(_h["item_uid"], 0) + 1

    _covd = {}
    for _u, _kinds in _covs.items():
        _n = sum(_kinds.values())
        _sub = bool(_kinds.get("subtitle") or _kinds.get("subtitle_seg"))
        _dm = bool(_kinds.get("danmaku"))
        _cm = bool(_kinds.get("comment"))
        _sum2 = bool(_kinds.get("summary"))
        if _n == 0:
            _lvl = "empty"
        elif _sub:
            _lvl = "full"
        elif _dm or _cm:
            _lvl = "partial"
        else:
            _lvl = "meta_only"
        _gaps = []
        if not _sub:
            _gaps.append("无字幕")
        if not _sum2:
            _gaps.append("无提炼")
        if not _dm and not _cm:
            _gaps.append("无弹幕评论")
        _covd[_u] = {"level": _lvl, "gap": "、".join(_gaps), "need_asr": (not _sub) and _n > 0}

    _need_asr = [u for u, c in _covd.items() if c.get("need_asr")]
    _MAXOFF = {}
    if _need_asr:
        _aph = ",".join("?" * len(_need_asr))
        try:
            for _r in con.execute(
                    "SELECT item_uid, MAX(offset_sec) FROM content_block "
                    "WHERE item_uid IN (" + _aph + ") GROUP BY item_uid", _need_asr):
                _MAXOFF[_r[0]] = _r[1]
        except Exception:
            _MAXOFF = {}

    _ORIGIN_CACHE = {}
    _bids = [info["evidence"].get("block_id") or info["evidence"].get("id")
             for _u, info in ranked]
    _bids = [b for b in _bids if b is not None]
    if _bids:
        _bph = ",".join("?" * len(_bids))
        try:
            for _r in con.execute(
                    "SELECT id, origin FROM content_block WHERE id IN (" + _bph + ")", _bids):
                _ORIGIN_CACHE[_r[0]] = _r[1] or "captured"
        except Exception:
            _ORIGIN_CACHE = {}

    out = []
    for uid, info in ranked:
        ev = info["evidence"]

        _age = {"age_days": None, "age": "时间未知", "date": None}
        item = _items.get(uid)
        _age = describe_age(item[6] if item else None)
        _sumtxt = _sums.get(uid)
        out.append({
            "granularity": "item",
            "item_uid": uid,
            "item_id": item[3] if item else None,
            "title": item[1] if item else None,
            "url": item[2] if item else None,
            "author_name": item[4] if item else None,

            "published_at": item[6] if item else None,
            "age_days": _age["age_days"],
            "age": _age["age"],
            "date": _age["date"],
            "score": round(info["score"], 4),
            "evidence": {
                "kind": ev["kind"], "text": ev["text"],
                "offset_sec": ev.get("offset_sec"),
                "origin_hint": _ORIGIN_CACHE.get(ev.get("block_id") or ev.get("id"))
                               or _origin_of(con, ev),
            },
            "matched_kinds": sorted(info["all_kinds"]),
            "summary": _sumtxt,
            "n_hits": _nhits.get(uid, 0),
        })

        _c = _covd.get(uid)
        if _c is not None:
            out[-1]["coverage"] = _c["level"]
            out[-1]["coverage_note"] = _c["gap"]

            if _c["need_asr"]:
                try:
                    from bili_digest.quality import kb_cov as _cov

                    _dur = (_items[uid][7] if _items.get(uid) else 0) or 0
                    _mx = _MAXOFF.get(uid)
                    _hint = _cov.asr_hint_from(_dur, _mx)
                    if _hint:
                        out[-1]["asr_hint"] = _hint
                except Exception:
                    pass

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

def main() -> int:
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
    from bili_digest.paths import DB, VEC, EMBED_MODEL
    from bili_digest.retrieve import rag_core as rc
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    model = None
    if a.mode == "partitioned":
        from bili_digest.retrieve import partitioned_search as ps
        vi = ps._load_index()
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=EMBED_MODEL,
                              cache_dir=str(paths.MODELS_DIR))
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
