# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

from bili_digest.schema.core_schema import make_uid

DEFAULT_TARGET = 6
MAX_COLLECT = 6

def local_hits(con, topic: str, top_k: int = 10) -> list:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv
    from bili_digest.paths import VEC
    vi = rc.VectorIndex(str(VEC))
    hits = rv.retrieve(con, vi, topic, granularity='item', top_k=top_k)
    out, seen = [], set()
    for h in hits:
        uid = h.get('item_uid')
        if uid and uid not in seen:
            seen.add(uid)
            out.append(h)
    return out

def collect(con, topic: str, need: int, verbose: bool = True) -> dict:
    from bili_digest.ingest import bili_inbox as bi
    from bili_digest.ingest import refine

    web = bi.search_web(topic, top_k=max(need, 4))
    have = {r[0] for r in con.execute('SELECT item_id FROM item')}
    fresh = [w for w in web if w.get('bvid') and w['bvid'] not in have]
    todo = fresh[:min(need, MAX_COLLECT)]
    if verbose:
        print('    站内找到 %d 条 · 未收录 %d 条 · 本次抓 %d 条'
              % (len(web), len(fresh), len(todo)))
    done, failed = [], []
    for w in todo:
        bv = w['bvid']
        try:
            ok, msg = refine.step_fetch(bv, sources='meta,sub,dm,reply')
            if not ok:
                failed.append((bv, str(msg)[:50]))
                continue
            imp = refine.step_import(con, bv)
            if imp.get('error'):
                failed.append((bv, str(imp['error'])[:50]))
                continue

            refine.step_extract(con, make_uid('bilibili', bv), verbose=False)
            done.append(bv)
            if verbose:
                print('      ✓ %s  %s' % (bv, str(w.get('title'))[:38]))
        except Exception as e:
            failed.append((bv, '%s: %s' % (type(e).__name__, str(e)[:44])))

    if done:
        try:
            from bili_digest.ingest import refine
            refine.step_segment()
            refine.step_index(verbose=False)
        except Exception as e:
            if verbose:
                print('    ! 收尾失败：%s: %s' % (type(e).__name__, str(e)[:50]))
    return {'found': len(web), 'attempted': len(todo),
            'done': done, 'failed': failed}

def research(con, topic: str, target: int = DEFAULT_TARGET,
             verbose: bool = True) -> dict:
    from bili_digest.distill import synthesize as sy

    import jieba

    hits = local_hits(con, topic)

    toks = [w.strip().lower() for w in jieba.cut(topic)
            if len(w.strip()) >= 2]

    key_tok = max(toks, key=len) if toks else ""
    hit_keys = []
    if key_tok:
        rows = con.execute("SELECT content FROM derived WHERE kind = ?",
                           ("topics",)).fetchall()
        blob = " ".join(str(r[0] or "") for r in rows).lower()
        hit_keys = [w for w in toks if w in blob]
    has_topic = bool(key_tok) and key_tok in blob

    if verbose:
        print('  库里相关 %d 条 · 关键词 %r → %s'
              % (len(hits), key_tok,
                 '库里有这个主题' if has_topic else '库里没有这个主题'))

    collected = None
    if (not has_topic) or len(hits) < target:
        if verbose:
            print('  库里不够 → 去站内搜集…' if has_topic
                  else '  库里没有这个主题 → 去站内搜集…')

        need = max(1, target - len(hits)) if has_topic else target
        collected = collect(con, topic, need, verbose=verbose)
        hits = local_hits(con, topic)
        if verbose:
            print('  搜集后库里相关 %d 条' % len(hits))

    uids = [h.get('item_uid') for h in hits if h.get('item_uid')]
    r = sy.synthesize(con, kind='report', tier='', uids=uids,
                      limit=40, verbose=verbose)
    if r.get('error'):
        return {'topic': topic, 'n_local': len(hits), 'collected': collected,
                'report': None, 'ok': False, 'note': r['error']}
    return {'topic': topic, 'n_local': len(hits), 'collected': collected,
            'report': r, 'ok': True}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description='按需研究：不够就去搜集再总结')
    ap.add_argument('topic')
    ap.add_argument('--target', type=int, default=DEFAULT_TARGET)
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()
    con = connect(DB)
    try:
        r = research(con, a.topic, target=a.target, verbose=not a.quiet)
        print()
        if r['ok']:
            import json as _j
            print(_j.dumps(r['report'], ensure_ascii=False, indent=2)[:2000])
        else:
            print('  未产出综述：%s' % r.get('note'))
    finally:
        con.close()

if __name__ == "__main__":
    main()
