# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

def bits_over_random(hit_rate: float, k: int, n_candidates: int) -> float:
    if n_candidates <= 0:
        return float("nan")
    rand = min(float(k) / float(n_candidates), 1.0)
    if rand >= 1.0:
        return 0.0
    return (hit_rate - rand) / (1.0 - rand)

def candidates_of(con, granularity: str = "item") -> int:
    if granularity == "item":
        return con.execute(
            "SELECT COUNT(*) FROM item WHERE uid IN "
            "(SELECT DISTINCT item_uid FROM content_block)").fetchone()[0]
    if granularity == "block":
        return con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
    if granularity == "topic":
        return con.execute("SELECT COUNT(*) FROM derived WHERE kind = 'topics'").fetchone()[0]
    raise ValueError("未知粒度: %r" % granularity)

def report(hit_rate: float, k: int, n: int, label: str = "") -> dict:
    bor = bits_over_random(hit_rate, k, n)
    rand = min(float(k) / max(n, 1), 1.0)
    out = {
        "hit_rate": round(hit_rate, 4),
        "k": k,
        "n_candidates": n,
        "random_rate": round(rand, 6),
        "bor": round(bor, 4),
        "label": label,
    }
    print("  %-22s R@%d %.1f%%  随机 %.3f%%  BoR %.3f  (N=%d)" % (
        label, k, hit_rate * 100, rand * 100, bor, n))
    return out

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(
        description="BoR：给 R@k 做机会校正（候选池越小，R@k 越虚高）")
    ap.add_argument("--hit", type=float, required=True, help="命中率，0~1 或 0~100")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--n", type=int, default=None, help="候选池大小（默认按 item 粒度查库）")
    ap.add_argument("--granularity", default="item", choices=["item", "block", "topic"])
    a = ap.parse_args()
    con = connect(DB)
    try:
        n = a.n if a.n else candidates_of(con, a.granularity)
        hit = a.hit / 100.0 if a.hit > 1 else a.hit
        report(hit, a.k, n, "输入")
        print()
        print("  注：N 必须与「那次实验的真实候选池」一致。用大了会让 BoR 偏乐观。")
    finally:
        con.close()

if __name__ == "__main__":
    main()
