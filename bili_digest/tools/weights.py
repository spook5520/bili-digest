# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import statistics
import sys
import time

from bili_digest import paths

HISTORY = None
TOP_K = 10
KIND_KEYS = ("subtitle", "subtitle_seg", "summary", "key_points", "insights",
             "audience", "growth", "meta", "annotation", "comment", "danmaku")

SCALAR_KEYS = ("literal_boost", "short_chars", "short_penalty")

RANGES = {
    "comment": ((0.80, 0.90), "峰在 0.85；<=0.70 与 >=0.95 都可检测地变差"),
    "danmaku": ((0.20, 0.65), "区间内逐查询零差；高于 0.70 首位弹幕率开始上升"),
    "short_penalty": ((0.30, 0.60), "0.60 起首位短块率从 1% 升到 3%"),
    "literal_boost": ((2.0, 3.0), "2.0 是 RRF 支配阈值，2.0~3.0 逐查询零差"),
}

def history_path():
    from bili_digest.paths import DATA_DIR
    return DATA_DIR / ".cache" / "weights_history.jsonl"

def queries_of(con, limit=None):
    from bili_digest.tools import eval_retrieval as ev
    qs = [q for _u, q in ev.build_queries(con, "fragment", 100000)]
    return qs[:limit] if limit else qs

def fingerprint(con, vi, queries):
    from bili_digest.retrieve import rag_core as rc
    per = []
    for q in queries:
        hits = rc.hybrid(con, vi, q, top_k=TOP_K)
        ids = [h.get("block_id") for h in hits]
        per.append(hashlib.sha256("|".join(str(i) for i in ids).encode()).hexdigest()[:16])
    total = hashlib.sha256("".join(per).encode()).hexdigest()
    return total, per

def _apply_val(key, val):
    from bili_digest.retrieve import rag_core as rc
    if key in KIND_KEYS:
        saved = dict(rc.KIND_WEIGHT)
        kw = dict(rc.KIND_WEIGHT)
        kw[key] = val
        rc.KIND_WEIGHT = kw
        return lambda: setattr(rc, "KIND_WEIGHT", saved)
    attr = {"literal_boost": "LITERAL_BOOST", "short_chars": "SHORT_CHARS",
            "short_penalty": "SHORT_PENALTY"}.get(key)
    if not attr:
        raise SystemExit(f"未知键 {key}；可用：{', '.join(KIND_KEYS + SCALAR_KEYS)}")
    saved = getattr(rc, attr)
    setattr(rc, attr, val)
    return lambda: setattr(rc, attr, saved)

def impact(con, vi, queries, changes, verbose=True):
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import block_criterion as bc
    from bili_digest.tools import eval_retrieval as ev
    jc = ev._cache_load()

    def collect():
        out = []
        for q in queries:
            hits = rc.hybrid(con, vi, q, top_k=TOP_K)
            ids = [h.get("block_id") for h in hits]
            gains = [jc.get(ev._ckey(q, h.get("text") or "")) for h in hits]
            out.append((ids, gains))
        return out

    before = collect()
    restores = [_apply_val(k, v) for k, v in changes.items()]
    try:
        after = collect()
    finally:
        for r in reversed(restores):
            r()

    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a[0] != b[0]]
    cps_b = [bc.context_precision(g, TOP_K, 2) for _i, g in before
             if g and all(x is not None for x in g)]
    cps_a = [bc.context_precision(g, TOP_K, 2) for _i, g in after
             if g and all(x is not None for x in g)]
    n = min(len(cps_a), len(cps_b))
    d = [cps_a[i] - cps_b[i] for i in range(n)]
    sigma = statistics.pstdev(d) if n > 1 else 0.0
    floor = bc.detectable_delta(n, sigma)
    verdict = "测不动" if abs(statistics.mean(d) if d else 0.0) <= floor else "超下限"
    res = {"n_queries": len(queries), "n_changed": len(changed),
           "changed_idx": changed[:50], "cp_before": statistics.mean(cps_b) if cps_b else 0.0,
           "cp_after": statistics.mean(cps_a) if cps_a else 0.0,
           "delta": statistics.mean(d) if d else 0.0,
           "sigma": sigma, "floor": floor, "verdict": verdict, "n_paired": n}
    if verbose:
        print("  查询 %d 条 · 排序变化 %d 条 (%.1f%%)"
              % (res["n_queries"], res["n_changed"],
                 100.0 * res["n_changed"] / max(res["n_queries"], 1)))
        print("  CP@%d %.4f → %.4f  Δ%+.4f  σ(D) %.4f  下限 %.4f  → %s"
              % (TOP_K, res["cp_before"], res["cp_after"], res["delta"],
                 sigma, floor, verdict))
        if changed:
            print("  变化的前 %d 条查询：" % min(5, len(changed)))
            for i in changed[:5]:
                print("    #%-3d %s…" % (i, queries[i][:28]))
    return res

def now_id():
    from bili_digest.schema.core_schema import now_sec
    ts = now_sec()
    return "%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.localtime(ts)),
                      hashlib.sha256(str(ts).encode()).hexdigest()[:6])

def log_append(entry):
    p = history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + chr(10))

def log_read():
    p = history_path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

def write_cfg(cfg, path=None):
    from bili_digest.retrieve import weights
    txt = weights.format_toml(cfg, (path or weights.TOML_PATH).read_text(encoding="utf-8"))
    (path or weights.TOML_PATH).write_text(txt, encoding="utf-8")

def parse_kv(s):
    if "=" not in s:
        raise SystemExit("格式应为 key=value")
    k, v = s.split("=", 1)
    k = k.strip()
    if k not in KIND_KEYS + SCALAR_KEYS:
        raise SystemExit(f"未知键 {k}；可用：{', '.join(KIND_KEYS + SCALAR_KEYS)}")
    return k, (int(v) if k == "short_chars" else float(v))

def cmd_show(a):
    from bili_digest.retrieve import weights
    cfg = weights.load()
    print("来源：%s" % weights.TOML_PATH)
    print("  literal_boost %s · short_chars %s · short_penalty %s"
          % (cfg["literal_boost"], cfg["short_chars"], cfg["short_penalty"]))
    print("  kind_weight " + " ".join("%s=%s" % (k, v) for k, v in cfg["kind_weight"].items()))
    h = log_read()
    print("历史 %d 条%s" % (len(h), ("，最近：" + h[-1]["id"] + " " + h[-1]["action"]) if h else ""))

def cmd_impact(a):
    from bili_digest.retrieve import weights
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    changes = dict(parse_kv(x) for x in a.pairs)
    for k, v in changes.items():
        rng = RANGES.get(k)
        if rng and not (rng[0][0] <= v <= rng[0][1]):
            print("  [!] %s=%s 在实测区间 [%s, %s] 之外：%s"
                  % (k, v, rng[0][0], rng[0][1], rng[1]))
    impact(con, vi, queries_of(con, a.queries), changes)

def cmd_set(a):
    from bili_digest.retrieve import weights, rag_core as rc
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    qs = queries_of(con, a.queries)
    changes = dict(parse_kv(x) for x in a.pairs)
    before_cfg = weights.load()

    print("① 影响面预估（不写盘）")
    res = impact(con, vi, qs, changes)
    if not a.apply:
        print("\n  未写盘。确认后加 --apply；回滚命令将在写盘时一并存下。")
        return

    print("\n② 存前值与排序指纹")
    fp_before, per_before = fingerprint(con, vi, qs)
    print("  指纹 %s（%d 条查询）" % (fp_before[:16], len(qs)))

    new_cfg = json.loads(json.dumps(before_cfg))
    for k, v in changes.items():
        if k in KIND_KEYS:
            new_cfg["kind_weight"][k] = v
        else:
            new_cfg[k] = v
    write_cfg(new_cfg)
    weights.reload(new_cfg)
    fp_after, per_after = fingerprint(con, vi, qs)

    from bili_digest.schema.core_schema import now_sec
    entry = {"id": now_id(), "action": "set", "ts": now_sec(),
             "changes": {k: [before_cfg["kind_weight"].get(k, before_cfg.get(k)), v]
                         for k, v in changes.items()},
             "before_state": before_cfg, "after_state": new_cfg,
             "fingerprint_before": fp_before, "per_query_before": per_before,
             "fingerprint_after": fp_after, "impact": res}
    log_append(entry)
    print("③ 已写盘 %s" % weights.TOML_PATH)
    print("  指纹 %s → %s（%d 条查询的排序变了 %d 条）"
          % (fp_before[:16], fp_after[:16], len(qs), res["n_changed"]))
    print("④ 回滚：python -m bili_digest.tools.weights revert")
    print("   核对：python -m bili_digest.tools.weights verify --against %s" % entry["id"])

def cmd_revert(a):
    from bili_digest.retrieve import weights
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc
    h = [e for e in log_read() if e["action"] == "set"]
    if not h:
        raise SystemExit("没有可回滚的改动")
    entry = h[-a.steps]
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))

    qs = queries_of(con, a.queries or entry["impact"]["n_queries"])

    print("回滚到 %s 之前：%s" % (entry["id"], entry["changes"]))
    write_cfg(entry["before_state"])
    weights.reload(entry["before_state"])
    fp, _per = fingerprint(con, vi, qs)
    ok = fp == entry["fingerprint_before"]
    from bili_digest.schema.core_schema import now_sec
    log_append({"id": now_id(), "action": "revert", "ts": now_sec(),
                "target": entry["id"], "fingerprint": fp,
                "matches_before": ok})
    print("  恢复后指纹 %s（改前 %s）→ %s"
          % (fp[:16], entry["fingerprint_before"][:16], "逐位一致 ✓" if ok else "不一致 ✗"))
    if not ok:
        print("  差异查询：" + ", ".join(
            "#%d" % i for i, (x, y) in enumerate(zip(_per, entry["per_query_before"]))
            if x != y)[:400])
    sys.exit(0 if ok else 1)

def cmd_verify(a):
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    h = log_read()
    sets = [e for e in h if e["action"] == "set"]
    if a.against:
        entry = next((e for e in h if e["id"] == a.against), None)
        if not entry:
            raise SystemExit(f"找不到 {a.against}")
        target, label, ref = entry["fingerprint_before"], "改前", entry["per_query_before"]
    else:
        if not sets:
            raise SystemExit("没有改动记录可核对")
        entry = sets[-1]
        target, label, ref = entry["fingerprint_after"], "改后", None
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    qs = queries_of(con, a.queries or entry["impact"]["n_queries"])
    fp, per = fingerprint(con, vi, qs)
    ok = fp == target
    print("当前指纹 %s vs 目标（%s）%s → %s"
          % (fp[:16], label, target[:16], "逐位一致 ✓" if ok else "不一致 ✗"))
    if not ok and ref:
        bad = [i for i, (x, y) in enumerate(zip(per, ref)) if x != y]
        print("  差异 %d 条：%s" % (len(bad), ", ".join("#%d" % i for i in bad[:20])))
    sys.exit(0 if ok else 1)

def cmd_history(a):
    for e in log_read():
        print("%s %-6s %s %s" % (e["id"], e["action"],
                                 e.get("changes") or e.get("target", ""),
                                 ("fp=%s" % e.get("fingerprint", "")[:12]) if e["action"] == "revert"
                                 else "ΔCP%+.4f 变序 %d" % (e["impact"]["delta"],
                                                            e["impact"]["n_changed"])))

def main():
    ap = argparse.ArgumentParser(description="检索权重的可撤销改动")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="当前生效值")
    p.set_defaults(fn=cmd_show)
    for name, fn, hlp in (("impact", cmd_impact, "只看影响面（不写盘）"),
                          ("set", cmd_set, "改值；默认只看影响面")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("pairs", nargs="+", help="key=value，如 comment=0.90")
        p.add_argument("--queries", type=int, default=None, help="限制查询条数（调试用）")
        p.add_argument("--apply", action="store_true", help="真正写盘")
        p.set_defaults(fn=fn)
    p = sub.add_parser("revert", help="回到前一态并逐位核对")
    p.add_argument("--steps", type=int, default=1)
    p.add_argument("--queries", type=int, default=None)
    p.set_defaults(fn=cmd_revert)
    p = sub.add_parser("verify", help="用磁盘上的配置重算指纹并核对")
    p.add_argument("--against", default=None, help="核对到某次改动【之前】")
    p.add_argument("--last", action="store_true", help="核对到最近一次改动【之后】")
    p.add_argument("--queries", type=int, default=None)
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("history", help="改动记录")
    p.set_defaults(fn=cmd_history)
    a = ap.parse_args()
    a.fn(a)

if __name__ == "__main__":
    main()
