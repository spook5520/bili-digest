#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import math
import re
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core_schema import (KIND_COMMENT, KIND_DANMAKU, KIND_META, KIND_SUBTITLE,
                         connect, now_sec)

from core_schema import MIN_INFO_CHARS, info_len
from paths import EMBED_MODEL, EMBED_DIM

CANDIDATE_K = 128

RRF_K = 60

LITERAL_BOOST = 2.0

SHORT_CHARS = 12

SHORT_PENALTY = 0.55

KIND_WEIGHT = {

    "subtitle": 1.0,
    "subtitle_seg": 1.0,
    "summary": 1.0,
    "key_points": 1.0,
    "insights": 1.0,
    "audience": 1.0,
    "growth": 1.0,
    "meta": 1.0,

    "annotation": 1.0,

    "comment": 0.85,
    "danmaku": 0.45,
}

def cliff(hits: list, min_ratio: float = 0.6, min_keep: int = 2) -> dict:
    if len(hits) < 2:
        return {"cut": len(hits), "ratio": None, "flat": True}
    ss = [(h.get("score") or 0) for h in hits]
    ratios = []
    for i in range(len(ss) - 1):
        ratios.append((ss[i + 1] / ss[i]) if ss[i] else 1.0)
    mi = min(range(len(ratios)), key=lambda i: ratios[i])
    r = ratios[mi]

    flat = r >= 0.85
    cut = len(hits)
    if r < min_ratio:
        cut = max(mi + 1, min_keep)
    return {"cut": cut, "ratio": round(r, 3), "flat": flat}

DEFAULT_TOP_K = 8

STOPWORDS = set(
    "的 了 吗 呢 吧 啊 哦 嗯 是 在 和 与 或 有 没 不 这 那 个 我 你 他 她 它 "
    "什么 怎么 哪些 哪个 如何 为什么 关于 讨论 相关 一下 一些 这个 那个 "
    "可以 是否 哪里 哪些 多少".split())

_cache = {}

def deps(need_embed: bool = True):
    if not need_embed:
        return _cache
    if _cache.get("ready"):
        return _cache
    import os
    os.environ.setdefault("HF_ENDPOINT", __import__("paths").HF_ENDPOINT)
    import numpy as np
    _cache["np"] = np
    try:
        from usearch.index import Index, MetricKind, ScalarKind
        _cache["usearch"] = (Index, MetricKind, ScalarKind)
        _cache["vec_mode"] = "hnsw"
    except ImportError:
        _cache["vec_mode"] = "numpy"
    try:
        from fastembed import TextEmbedding
        _cache["model"] = TextEmbedding(model_name=EMBED_MODEL,
                                        cache_dir=str(Path(__file__).parent / "models"))
    except ImportError as e:
        raise SystemExit(
            f"缺少嵌入依赖（{e}）。安装：\n"
            "  pip install -i https://mirrors.aliyun.com/pypi/simple/ "
            "--trusted-host mirrors.aliyun.com fastembed usearch"
        )
    _cache["ready"] = True
    return _cache

class VectorIndex:

    def __init__(self, path: str | Path, dim: int = EMBED_DIM,
                 dtype: str = "f32", rebuild: bool = False):
        d = deps()
        self.np = d["np"]
        self.dim = dim
        self.path = Path(path)
        self.mode = d["vec_mode"]
        self.dtype_name = dtype
        self._fallback_vecs = None

        if self.mode == "hnsw":
            Index, MetricKind, ScalarKind = d["usearch"]
            self.sk = {"f32": ScalarKind.F32, "f16": ScalarKind.F16,
                       "i8": ScalarKind.I8}.get(dtype, ScalarKind.F32)
            if rebuild and self.path.exists():
                self.path.unlink()

            self.idx = Index(ndim=dim, metric=MetricKind.Cos, dtype=self.sk,
                             connectivity=32)
            if self.path.exists():
                self.idx.load(str(self.path))

            try:
                self.idx.expansion_search = 256
            except Exception:
                pass

    def clear(self) -> None:
        if self.mode == "hnsw":
            Index, MetricKind, ScalarKind = deps()["usearch"]
            self.idx = Index(ndim=self.dim, metric=MetricKind.Cos, dtype=self.sk,
                             connectivity=32)
        else:
            self._fallback_vecs = None
        try:
            self.path.unlink()
        except OSError:
            pass

    @property
    def size(self) -> int:
        if self.mode == "hnsw":
            return len(self.idx)
        return 0 if self._fallback_vecs is None else len(self._fallback_vecs[0])

    def add(self, keys, vecs) -> None:
        np = self.np
        arr = np.asarray(vecs, dtype=np.float32)
        if self.mode == "hnsw":
            self.idx.add(np.asarray(keys, dtype=np.uint64), arr)
        else:
            if self._fallback_vecs is None:
                self._fallback_vecs = (np.asarray(keys, dtype=np.uint64), arr)
            else:
                self._fallback_vecs = (
                    np.concatenate([self._fallback_vecs[0], np.asarray(keys, dtype=np.uint64)]),
                    np.concatenate([self._fallback_vecs[1], arr]))

    def add_many(self, keys, vecs, batch: int = 2000) -> int:
        keys, vecs = list(keys), list(vecs)
        n = 0
        for i in range(0, len(keys), batch):
            self.add(keys[i:i + batch], vecs[i:i + batch])
            n += len(keys[i:i + batch])
        return n

    def search(self, query_vec, k: int = 10, with_dist: bool = False):
        np = self.np
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        if self.mode == "hnsw":
            r = self.idx.search(q, k)

            _SENTINEL = (1 << 64) - 1
            keys = [b for b in r.keys.tolist() if b != _SENTINEL]
            if with_dist:
                dists = [d for b, d in zip(r.keys.tolist(),
                                           r.distances.tolist())
                         if b != _SENTINEL]
                return list(zip(keys, dists))
            return keys
        if self._fallback_vecs is None:
            return []
        keys, m = self._fallback_vecs

        sims = (m @ q) / (np.linalg.norm(m, axis=1) * (np.linalg.norm(q) + 1e-9) + 1e-9)
        order = np.argsort(-sims)[:k]
        if with_dist:
            return [(int(keys[i]), float(1.0 - sims[i])) for i in order]
        return [int(keys[i]) for i in order]

    def remove(self, keys) -> None:
        if self.mode != "hnsw":
            return
        np = self.np
        try:
            self.idx.remove(np.asarray(keys, dtype=np.uint64))
        except Exception:
            pass

    def save(self) -> None:
        if self.mode == "hnsw":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.idx.save(str(self.path))

    def compact(self) -> None:
        if self.mode == "hnsw":
            try:
                self.idx.compact()
            except Exception:
                pass

    def memory_mb(self) -> float:
        if self.mode == "hnsw":
            return self.idx.memory_usage / 1024 / 1024
        if self._fallback_vecs is None:
            return 0.0
        return self._fallback_vecs[1].nbytes / 1024 / 1024

def _keys_path(vindex_path) -> "Path":
    from pathlib import Path as _P
    return _P(vindex_path).with_suffix(".keys.json")

def load_index_keys(vindex_path) -> "set | None":
    import json
    p = _keys_path(vindex_path)
    if not p.exists():
        return None
    try:
        return {int(x) for x in json.loads(p.read_text(encoding="utf-8"))}
    except (ValueError, TypeError, OSError):
        return None

def save_index_keys(vindex_path, keys) -> None:
    import json
    p = _keys_path(vindex_path)
    try:
        p.write_text(json.dumps(sorted(int(x) for x in keys)),
                     encoding="utf-8")
    except OSError:
        pass

def short_weight(text: str) -> float:
    return SHORT_PENALTY if len((text or "").strip()) < SHORT_CHARS else 1.0

def indexable_count(con: sqlite3.Connection) -> int:
    rows = con.execute("SELECT text FROM content_block").fetchall()
    return sum(1 for r in rows if info_len(r[0]) >= MIN_INFO_CHARS)

def index_chunks(con: sqlite3.Connection, vindex: VectorIndex,
                 only_item: str | None = None, batch: int = 32,
                 verbose: bool = True, rebuild: bool = False) -> dict:
    d = deps()
    np, model = d["np"], d["model"]

    con.executescript("""
        CREATE TABLE IF NOT EXISTS vec_state (
            block_id INTEGER PRIMARY KEY,
            hash     TEXT,
            indexed_at INTEGER
        );
    """)

    sql = "SELECT id,text,hash FROM content_block WHERE 1=1"
    args = []
    if only_item:
        sql += " AND item_uid=?"
        args.append(only_item)
    rows = con.execute(sql, args).fetchall()

    rows = [r for r in rows if info_len(r["text"]) >= MIN_INFO_CHARS]

    have = load_index_keys(vindex.path)
    if rebuild or have is None:

        if rebuild or have is None:
            reason = "显式指定 --rebuild" if rebuild else "无 key 镜像"
            if verbose:
                print(f"  [!] {reason}，全量重建")
            try:
                from pathlib import Path as _P
                _P(vindex.path).unlink()
            except OSError:
                pass

            vindex.clear()
            have = set()
            todo = list(rows)
        else:
            todo = list(rows)
    else:
        todo = [r for r in rows if r["id"] not in have]

    if rebuild:
        con.execute("DELETE FROM vec_state")
        con.commit()
    done_ids = {r[0] for r in con.execute("SELECT block_id FROM vec_state")}
    miss = done_ids - have
    if miss:
        con.executemany("DELETE FROM vec_state WHERE block_id=?",
                        [(i,) for i in miss])
        con.commit()

    dropped = 0

    if not rebuild and have:
        indexable_ids = {r["id"] for r in rows}
        stale = have - indexable_ids
        if stale:
            try:
                vindex.remove(sorted(stale))
                have -= stale
                dropped = len(stale)
            except Exception:
                dropped = 0
            if dropped and verbose:
                print(f"    回收孤立向量 {dropped} 条")

    if not todo:

        if dropped:
            vindex.save()
            save_index_keys(vindex.path, have)
        return {"new": 0, "skipped": len(rows),
                "total": len(have), "seconds": 0.0, "reclaimed": dropped}

    n = 0
    t0 = time.time()
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = np.array(list(model.embed([r["text"] for r in part])),
                        dtype=np.float32)
        ids = [r["id"] for r in part]
        vindex.add_many(ids, vecs, batch=len(ids))
        for r in part:
            con.execute("INSERT OR REPLACE INTO vec_state(block_id,hash,indexed_at) "
                        "VALUES(?,?,?)", (r["id"], r["hash"], now_sec()))
        con.commit()
        n += len(part)
        have.update(ids)
        if verbose:
            print(f"    已索引 {n}/{len(todo)}  ({time.time()-t0:.1f}s)")
    vindex.save()

    save_index_keys(vindex.path, have)
    return {"new": n, "skipped": len(rows) - n, "total": len(have),
            "seconds": round(time.time() - t0, 1)}

def fts_tokens(query: str) -> list[str]:
    from core_schema import has_jieba
    if has_jieba():
        import jieba
        toks = [t.strip() for t in jieba.lcut(query)]
    else:
        import re
        toks = re.findall(r"[A-Za-z0-9_\u4e00-\u9fa5]+", query)
    return [t for t in toks
            if t and t not in STOPWORDS and (len(t) > 1 or t.isdigit())]

def fts_search(con: sqlite3.Connection, query: str, k: int,
               item_filter: str | None = None) -> list[tuple[int, float]]:
    toks = fts_tokens(query)
    if not toks:
        return []
    mode = con.execute("SELECT v FROM meta_kv WHERE k='fts_mode'").fetchone()
    mode = mode["v"] if mode else "jieba"

    def _q(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'

    expr = " AND ".join(_q(t) for t in toks)

    try:
        if item_filter:
            rows = con.execute(
                f"SELECT f.rowid, bm25(block_fts), LENGTH(cb.text) "
                f"FROM block_fts f JOIN content_block cb ON cb.id = f.rowid "
                f"WHERE block_fts MATCH ? AND f.item_uid=? "
                f"ORDER BY bm25(block_fts) LIMIT ?",
                (expr, item_filter, k)).fetchall()
        else:
            rows = con.execute(
                f"SELECT f.rowid, bm25(block_fts), LENGTH(cb.text) "
                f"FROM block_fts f JOIN content_block cb ON cb.id = f.rowid "
                f"WHERE block_fts MATCH ? ORDER BY bm25(block_fts) LIMIT ?",
                (expr, k)).fetchall()
        return [(r[0], r[1]) for r in rows if (r[2] or 0) >= MIN_INFO_CHARS]
    except sqlite3.OperationalError as e:

        if "no such table" in str(e):
            return []
        raise

_INVISIBLE_CHARS = ("\u200b", "\ufeff", "\u2060")

def normalize_query(q: str) -> str:
    if not q:
        return q
    try:
        import unicodedata
        q = unicodedata.normalize("NFKC", q)
    except Exception:
        return q
    for ch in _INVISIBLE_CHARS:
        if ch in q:
            q = q.replace(ch, "")
    return q

def hybrid(con: sqlite3.Connection, vindex: VectorIndex, question: str,
           top_k: int = DEFAULT_TOP_K, candidate_k: int = CANDIDATE_K,
           item_filter: str | None = None,
           kinds: list[str] | None = None,
           since=None, until=None, time_field: str = "saved_at",
           recency: float = 0.0, kw_weight: float = 0.0,
           min_duration: int | None = None, max_duration: int | None = None,
           min_blocks: int | None = None, max_blocks: int | None = None,
           ) -> list[dict]:

    question = normalize_query(question)

    import temporal as _tp
    d = deps()
    np, model = d["np"], d["model"]

    q_vec = question
    try:

        from query_terms import strip_generic as _sg
        _q2 = _sg(con, question)
        if _q2 and _q2 != question:
            q_vec = _q2
    except Exception:
        pass

    qv = np.array(list(model.query_embed([q_vec]))[0], dtype=np.float32)

    _vec_pairs = vindex.search(qv, candidate_k, with_dist=True)
    vec_ids = [b for b, _d in _vec_pairs]
    top_vec_sim = (1.0 - float(_vec_pairs[0][1])) if _vec_pairs else 0.0

    fts_hits = fts_search(con, question, candidate_k, item_filter)
    fts_ids = [b for b, _ in fts_hits]

    scores: dict[int, float] = {}
    for rank, bid in enumerate(vec_ids):
        scores[bid] = scores.get(bid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, bid in enumerate(fts_ids):
        scores[bid] = scores.get(bid, 0.0) + 1.0 / (RRF_K + rank + 1)

    if kw_weight > 0:
        try:
            import keywords as _kw
            kw_hits = _kw.search(con, question, k=candidate_k)
            for rank, (bid, _s) in enumerate(kw_hits):
                scores[bid] = scores.get(bid, 0.0) + kw_weight / (RRF_K + rank + 1)
        except Exception:
            pass

    if not scores:
        return []

    toks = fts_tokens(question)
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:candidate_k * 2]
    has_time_filter = bool(_tp.time_range(since, until)[0] or _tp.time_range(since, until)[1])

    ranked_ids = [bid for bid, _ in ranked]
    if not ranked_ids:
        return []
    ph = ",".join("?" * len(ranked_ids))
    rows_map = {}
    for r in con.execute(
            f"SELECT cb.id,cb.item_uid,cb.kind,cb.offset_sec,cb.text,cb.author,"
            f"cb.weight,i.title,i.url,i.item_id,i.platform,i.author_name,"
            f"i.saved_at,i.published_at "
            f"FROM content_block cb JOIN item i ON i.uid=cb.item_uid "
            f"WHERE cb.id IN ({ph})", ranked_ids):
        rows_map[r["id"]] = r

    keep_uids = None
    if has_time_filter:
        uids = list({rows_map[b]["item_uid"] for b in rows_map})
        keep_uids = set(_tp.filter_items(con, uids, since, until, time_field))

    if any(v is not None for v in (min_duration, max_duration,
                                   min_blocks, max_blocks)):
        import form_filter as _ff
        uids = list({rows_map[b]["item_uid"] for b in rows_map})
        form_keep = _ff.form_filter(con, uids, min_duration, max_duration,
                                    min_blocks, max_blocks)
        keep_uids = form_keep if keep_uids is None else (keep_uids & form_keep)

    out, seen = [], set()

    dropped = {}

    def _drop(_why: str, _bid: int) -> None:
        d = dropped.setdefault(_why, {"n": 0, "sample": []})
        d["n"] += 1
        if len(d["sample"]) < 3:
            d["sample"].append(_bid)

    for bid, sc in ranked:
        row = rows_map.get(bid)
        if row is None:
            _drop("not_in_db", bid)
            continue
        if kinds and row["kind"] not in kinds:
            _drop("kind", bid)
            continue
        if item_filter and row["item_uid"] != item_filter:
            _drop("item", bid)
            continue

        if keep_uids is not None and row["item_uid"] not in keep_uids:
            _drop("time_or_form", bid)
            continue

        key = row["text"][:40]
        if key in seen:
            _drop("dup", bid)
            continue
        seen.add(key)

        boost = LITERAL_BOOST if (toks and any(t in row["text"] for t in toks)) else 1.0

        sw = short_weight(row["text"])

        kw = KIND_WEIGHT.get(row["kind"], 1.0)

        rw = 1.0
        if recency > 0:
            ts = (row["saved_at"] if time_field == "saved_at"
                  else row["published_at"])
            rw = (1.0 - recency) + recency * _tp.recency_weight(ts)

        out.append({
            "block_id": row["id"], "item_uid": row["item_uid"],
            "kind": row["kind"], "offset_sec": row["offset_sec"],
            "text": row["text"], "author": row["author"], "weight": row["weight"],
            "title": row["title"], "url": row["url"], "item_id": row["item_id"],
            "platform": row["platform"], "uploader": row["author_name"],
            "saved_at": row["saved_at"], "published_at": row["published_at"],
            "score": sc * boost * rw * sw * kw,
        })
    out.sort(key=lambda x: -x["score"])
    res = out[:top_k]

    if res:
        extra = {"query_vec_sim": round(top_vec_sim, 4)}
        if dropped:
            extra["dropped"] = dict(sorted(dropped.items(),
                                           key=lambda x: -x[1]["n"]))
        res[0] = dict(res[0], **extra)
    return res

def auto_top_k(question: str, base: int = DEFAULT_TOP_K) -> int:
    import re
    k = base
    marks = len(re.findall(r"[、和及与]|还是|或者", question))
    k += min(marks, 3) * 3
    if re.search(r"为什么|原因|关系|区别|对比|分别|各自", question):
        k += 4
    if len(question) > 24:
        k += 3
    if len(question) > 40:
        k += 3
    return max(base, min(k, base * 3, 24))

def heuristic_rerank(hits: list[dict], question: str) -> list[dict]:
    toks = fts_tokens(question)
    for h in hits:
        t = h["text"]
        lit = sum(1 for x in toks if x in t) / max(len(toks), 1)
        n = len(t)
        if n < 10:
            ls = n / 10 * 0.5
        elif n <= 200:
            ls = 1.0
        else:
            ls = max(0.6, 1.0 - (n - 200) / 800)

        likes = (h["weight"] or 0) if h["kind"] == KIND_COMMENT else 0
        social = min(math.log10(max(likes, 1) + 1) / 2.0, 1.0)
        src = {KIND_SUBTITLE: 1.15, KIND_COMMENT: 1.05,
               KIND_META: 1.10, KIND_DANMAKU: 1.0}.get(h["kind"], 1.0)
        h["rerank"] = (lit * 1.0 + ls * 0.35 + social * 0.25 + 0.10) * src
    hits.sort(key=lambda x: -x["rerank"])
    return hits

def fmt_hit(h: dict) -> str:
    if h["kind"] in (KIND_SUBTITLE, KIND_DANMAKU) and h["offset_sec"] is not None:
        m, s = divmod(int(h["offset_sec"]), 60)
        tag = "字幕" if h["kind"] == KIND_SUBTITLE else "弹幕"
        return f"[{tag} {m:02d}:{s:02d}] {h['text']}"
    if h["kind"] == KIND_COMMENT:
        return f"[评论 👍{int(h['weight'] or 0)} {h['author'] or '匿名'}] {h['text']}"
    return f"[{h['kind']}] {h['text']}"

def rebuild_all(con: sqlite3.Connection, vindex_path: str | Path,
                dtype: str = "f32", verbose: bool = True) -> dict:
    from core_schema import ensure_fts, index_block
    mode = ensure_fts(con, force=True)
    if verbose:
        print(f"  FTS 重建，方案 {mode}")

    n = 0
    for r in con.execute("SELECT id,text,item_uid,kind FROM content_block"):
        index_block(con, r["id"], r["text"], r["item_uid"], r["kind"])
        n += 1
    con.commit()
    if verbose:
        print(f"  已重建全文索引 {n} 条")

    vi = VectorIndex(vindex_path, dtype=dtype, rebuild=True)
    st = index_chunks(con, vi, verbose=verbose)
    return {"fts_mode": mode, "fts_blocks": n, **st}

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="检索核心工具")
    ap.add_argument("db", nargs="?", default="knowledge.db")
    ap.add_argument("--index", metavar="VEC_PATH", help="建/更新向量索引")
    ap.add_argument("--rebuild", metavar="VEC_PATH", help="全量重建 FTS + 向量")
    ap.add_argument("--dtype", default="f32", choices=["f32", "f16", "i8"])
    ap.add_argument("--search", metavar="QUERY", help="试检索")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    a = ap.parse_args()

    c = connect(a.db)
    if a.rebuild:
        r = rebuild_all(c, a.rebuild, dtype=a.dtype)
        print(f"  完成 {r}")
    elif a.index:
        vi = VectorIndex(a.index, dtype=a.dtype)
        print(f"  向量索引后端 {vi.mode}，当前 {vi.size} 条")
        r = index_chunks(c, vi)
        print(f"  完成 {r}  内存 {vi.memory_mb():.1f}MB")
    elif a.search:
        vi = VectorIndex("vectors.usearch")
        if not vi.size:
            raise SystemExit("向量索引为空，先跑 --index")
        hits = hybrid(c, vi, a.search, top_k=a.top_k)
        for i, h in enumerate(hits, 1):
            print(f"  [{i}] {fmt_hit(h)}")
    else:
        print(__doc__)
    c.close()
