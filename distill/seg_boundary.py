#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import block_hash, connect, now_sec
from paths import DB, VEC, EMBED_MODEL

DEFAULT_PAUSE = 1.5
DEFAULT_JUMP = 0.25
DEFAULT_MIN = 40
DEFAULT_MAX = 400

def _cos_dist(a, b) -> float:
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 1.0
    return 1.0 - float(np.dot(a, b) / (na * nb))

def _adapt_pause(subs: list[dict], pause: float) -> float:
    if len(subs) < 5:
        return pause
    import statistics
    gaps = []
    for i in range(1, len(subs)):
        g = (subs[i].get("offset_sec") or 0) - (subs[i - 1].get("offset_sec") or 0)
        if g > 0:
            gaps.append(g)
    if len(gaps) < 4:
        return pause
    med = statistics.median(gaps)
    return max(pause, med * 1.8)

def segment_item(subs: list[dict], vecs=None, pause: float = DEFAULT_PAUSE,
                 jump: float = DEFAULT_JUMP, min_chars: int = DEFAULT_MIN,
                 max_chars: int = DEFAULT_MAX, adapt: bool = True) -> list[dict]:
    if not subs:
        return []
    if adapt:
        pause = _adapt_pause(subs, pause)

    segs = []
    cur = [subs[0]]
    cur_len = len(subs[0].get("text") or "")

    for i in range(1, len(subs)):
        prev, cur_item = subs[i - 1], subs[i]
        gap = (cur_item.get("offset_sec") or 0) - (prev.get("offset_sec") or 0)
        add = len(cur_item.get("text") or "")
        new_len = cur_len + add

        hit = None
        if gap > pause:
            hit = f"pause={gap:.1f}s"
        elif vecs is not None and i < len(vecs) and i - 1 < len(vecs):
            d = _cos_dist(vecs[i - 1], vecs[i])
            if d > jump:
                hit = f"jump={d:.2f}"
        if hit is None and new_len > max_chars:
            hit = f"len={new_len}"

        if hit:
            segs.append(cur)
            cur, cur_len = [cur_item], add
        else:
            cur.append(cur_item)
            cur_len = new_len
    if cur:
        segs.append(cur)

    merged = []
    for s in segs:
        s_len = sum(len(x.get("text") or "") for x in s)
        if merged:
            prev_len = sum(len(x.get("text") or "") for x in merged[-1])
            if s_len < min_chars and prev_len + s_len <= max_chars:
                merged[-1].extend(s)
                continue
        merged.append(list(s))

    out = []
    for s in merged:
        text = " ".join((x.get("text") or "").strip() for x in s)
        text = " ".join(text.split())
        if not text:
            continue
        out.append({
            "start": float(s[0].get("offset_sec") or 0),
            "end": float(s[-1].get("offset_sec") or 0),
            "text": text,

            "content_len": sum(len(x.get("text") or "") for x in s),
            "n": len(s),
        })
    return out

def _delete_blocks(con, where: str, args: tuple = ()) -> int:
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM content_block WHERE {where}", args)]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    con.execute(f"DELETE FROM block_fts WHERE rowid IN ({ph})", ids)
    con.execute(f"DELETE FROM content_block WHERE {where}", args)
    return len(ids)

def build(pause: float = DEFAULT_PAUSE, jump: float = DEFAULT_JUMP,
          min_chars: int = DEFAULT_MIN, max_chars: int = DEFAULT_MAX,
          drop_lines: bool = True, verbose: bool = True,
          incremental: bool = False) -> dict:
    import os
    os.environ.setdefault("HF_ENDPOINT", __import__("paths").HF_ENDPOINT)
    import numpy as np
    from fastembed import TextEmbedding

    con = connect(DB)
    con.row_factory = sqlite3.Row

    if incremental:

        targets = [r["uid"] for r in con.execute(
            "SELECT uid FROM item i WHERE EXISTS"
            "(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='subtitle') "
            "AND NOT EXISTS"
            "(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid AND cb.kind='subtitle_seg')")]
    else:
        targets = [r["uid"] for r in con.execute("SELECT uid FROM item")]

    if not targets:
        con.close()
        return {"segments": 0, "items": 0, "incremental": incremental,
                "params": {"pause": pause, "jump": jump, "min_chars": min_chars,
                           "max_chars": max_chars},
                "note": "没有需要处理的条目"}

    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(HERE / "models"))

    all_subs = {}
    for uid in targets:
        rows = [dict(r) for r in con.execute(
            "SELECT id, offset_sec, text FROM content_block WHERE item_uid=? "
            "AND kind='subtitle' ORDER BY offset_sec", (uid,))]
        if rows:
            all_subs[uid] = rows

    if not incremental and not all_subs:
        con.close()
        return {"segments": 0, "items": 0, "incremental": False,
                "note": ("全量重建需要原始字幕行，但库里一条 subtitle 都没有 "
                         "（可能已被上一次 --build 的 drop_lines 删掉）。"
                         "拒绝执行，以免把现有 subtitle_seg 也删掉。"
                         "恢复办法：重新抓取这些条目的字幕后再跑。"),
                "aborted": True}

    if not incremental:

        _delete_blocks(con, "kind IN ('subtitle_seg','subtitle')")
        con.commit()
    elif drop_lines:

        for uid in all_subs:
            _delete_blocks(con, "item_uid=? AND kind='subtitle'", (uid,))
        con.commit()

    made = 0
    n_items = 0
    t0 = time.time()

    for uid, subs in all_subs.items():
        texts = [(s.get("text") or "").strip() for s in subs]
        try:
            vecs = np.array(list(model.embed(texts)), dtype=np.float32)
        except Exception:
            vecs = None
        segs = segment_item(subs, vecs, pause, jump, min_chars, max_chars)
        ts = now_sec()
        for sg in segs:

            h = block_hash("subtitle_seg", sg["text"], sg["start"])
            con.execute(
                "INSERT INTO content_block(item_uid,kind,offset_sec,text,"
                "author,weight,hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (uid, "subtitle_seg", sg["start"], sg["text"], None, None, h, ts))
            made += 1
        n_items += 1
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
    by_kind = {r[0]: r[1] for r in con.execute(
        "SELECT kind, COUNT(*) FROM content_block GROUP BY kind ORDER BY -COUNT(*)")}
    con.close()
    return {"segments": made, "items": n_items,
            "params": {"pause": pause, "jump": jump,
                       "min_chars": min_chars, "max_chars": max_chars},
            "drop_lines": drop_lines, "incremental": incremental,
            "total_blocks": total, "by_kind": by_kind,
            "seconds": round(time.time() - t0, 1)}

def preview(item_id: str, pause: float = DEFAULT_PAUSE,
            jump: float = DEFAULT_JUMP):
    con = connect(DB)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT uid,title FROM item WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        print(f"  没有 {item_id}")
        con.close()
        return
    subs = [dict(x) for x in con.execute(
        "SELECT id, offset_sec, text FROM content_block WHERE item_uid=? "
        "AND kind='subtitle' ORDER BY offset_sec", (r["uid"],))]
    con.close()
    print(f"  《{r['title'][:46]}》  原始 {len(subs)} 条字幕行")
    if not subs:
        print("   （无字幕）")
        return

    import os
    os.environ.setdefault("HF_ENDPOINT", __import__("paths").HF_ENDPOINT)
    import numpy as np
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(HERE / "models"))
    texts = [(s.get("text") or "").strip() for s in subs]
    try:
        vecs = np.array(list(model.embed(texts)), dtype=np.float32)
    except Exception:
        vecs = None
    segs = segment_item(subs, vecs, pause, jump)
    print(f"  → 切成 {len(segs)} 段\n")
    for i, s in enumerate(segs[:10], 1):
        m1, s1 = divmod(int(s["start"]), 60)
        m2, s2 = divmod(int(s["end"]), 60)
        print(f"  [{i}] {m1:02d}:{s1:02d}~{m2:02d}:{s2:02d}  {s['n']} 行  "
              f"{len(s['text'])} 字")
        print(f"      {s['text'][:70]}")

def main():
    import argparse
    ap = argparse.ArgumentParser(description="按语义边界切字幕段")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE)
    ap.add_argument("--jump", type=float, default=DEFAULT_JUMP)
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX)
    ap.add_argument("--preview", metavar="BVID", help="预览切分（不写库）")
    ap.add_argument("--keep-lines", action="store_true",
                    help="保留原始字幕行块（默认删除，见文档说明）")
    a = ap.parse_args()

    if a.preview:
        preview(a.preview, a.pause, a.jump)
    elif a.build:
        r = build(a.pause, a.jump, a.min_chars, a.max_chars,
                  drop_lines=not a.keep_lines)
        if r.get("aborted"):

            print(f"  [!] 未执行：{r['note']}")
            return
        print(f"  重建 {r['segments']} 段（{r['items']} 条视频）"
              f"  耗时 {r['seconds']}s")
        print(f"  参数 {r['params']}")
        print(f"  内容块 {r['total_blocks']}:")
        for k, v in r["by_kind"].items():
            print(f"    {k:<16} {v:>6}")

        print(f"\n  [!] 索引已失效 —— 下一步必须重建：")
        print(f"      python build_kb_index.py")
        print(f"      或 python check_index.py --fix   （检查并自动重建）")
    else:
        ap.print_help()

if __name__ == "__main__":
    main()
