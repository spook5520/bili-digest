#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

from bili_digest import paths

LEVELS = ("P0", "P1", "P2", "P3")
LEVEL_CN = {
    "P0": "坏掉的（会污染下游判断）",
    "P1": "越界的（系统在超额度做事）",
    "P2": "结构缺口（上层结论不可信）",
    "P3": "可修的小缺口",
}

def collect(con) -> dict:
    st = {}

    try:
        from bili_digest.quality import kb_cov
        st["cov"] = kb_cov.library(con)
    except Exception as e:
        st["cov"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from bili_digest.quality import health as H
        st["health"] = H.snapshot()
    except Exception as e:
        st["health"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from bili_digest.quality import budget as B
        st["budget"] = {"usage": B.usage(con), "limits": B.limits(con)}
    except Exception as e:
        st["budget"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from bili_digest.quality import harvest as HV
        st["harvest"] = HV.gaps(con)
    except Exception as e:
        st["harvest"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from bili_digest.quality import retrieval_gaps as RG
        st["retrieval"] = RG.report(con)
    except Exception as e:
        st["retrieval"] = {"error": f"{type(e).__name__}: {e}"}

    try:
        from bili_digest.quality import gate as G
        st["gate"] = G.plan(con)
    except Exception as e:
        st["gate"] = {"error": f"{type(e).__name__}: {e}"}

    return st

def _gate_counts(plan: dict) -> dict:
    out = {"validated": 0, "provisional": 0, "reject": 0,
           "total": int(plan.get("total") or 0)}
    v = plan.get("verdicts")
    if isinstance(v, dict):
        for k in ("validated", "provisional", "reject"):
            out[k] = int(v.get(k) or 0)
        return out
    rows = plan.get("rows") or plan.get("items") or []
    for r in rows:
        ep = r.get("epistemic") or r.get("to_epistemic") or ""
        if ep in out:
            out[ep] += 1
        out["total"] += 1
    return out

def priorities(st: dict) -> list:
    acts = []

    h = st.get("health") or {}
    if isinstance(h, dict) and "error" not in h:
        bad = [k for k, v in h.items()
               if isinstance(v, tuple) and v and v[0] == "FAIL"]
        if bad:
            acts.append({
                "level": "P0",
                "what": "一致性检查失败：" + "、".join(bad),
                "source": "health",
                "why": "块与索引/基线不一致时，检索会漏条，"
                       "而「某条搜不到」的判断会被误归因成内容缺失",
                "action": "python -m bili_digest index",
                "recheck": "python -m bili_digest health",
            })

    b = st.get("budget") or {}
    if isinstance(b, dict) and "error" not in b:
        over = [k for k, v in (b.get("usage") or {}).items()
                if v > (b.get("limits") or {}).get(k, 0)]
        if over:
            from bili_digest.quality import budget as B
            name = dict((k, n) for k, n, _d, _m in B.DIMENSIONS)
            acts.append({
                "level": "P1",
                "what": "自主行动超预算：" + "、".join(
                    "%s %g/%g" % (name.get(k, k), b["usage"][k], b["limits"][k])
                    for k in over),
                "source": "budget",
                "why": "超出日额度说明系统今天已经在做「平时不该做这么多」的事；"
                       "此时再叠加需要抓取/写入的建议，只会让偏差更大",
                "action": "kb budget（看明细）· 放行 kb budget --set <维度>=<值>"
                          " · 恢复默认 kb budget --reset",
                "recheck": "kb budget",
            })

    hv = st.get("harvest") or []
    if isinstance(hv, list):
        for g in hv:
            kind = g.get("kind", "")
            if kind.startswith("先清积压"):
                acts.append({
                    "level": "P2",
                    "what": g.get("what", ""),
                    "source": "harvest",
                    "why": "积压过半时，「该补哪个主题」的结论都建立在未处理的数据上；"
                           "而这条建议本身会消耗抓取预算（见 P1 的冲突）",
                    "action": g.get("action") or "kb harvest",
                    "recheck": "kb harvest",
                })
            elif "孤点" in kind and "可修" in kind:
                acts.append({
                    "level": "P3",
                    "what": g.get("what", ""),
                    "source": "harvest",
                    "why": "有素材、只差提炼 —— 一步就能接上图谱边",
                    "action": g.get("action") or "kb derive",
                    "recheck": "kb harvest",
                })

    rg = st.get("retrieval") or {}
    if isinstance(rg, dict) and "error" not in rg:
        z = rg.get("zero") or {}
        rep = rg.get("repeat") or {}
        if z.get("real"):

            lvl = "P1" if (z.get("rate") or 0) > 0.2 else "P2"
            acts.append({
                "level": lvl,
                "what": "检索失败：%d 条零结果（近 %d 天 %.1f%%），其中 %d 条是真缺口"
                        % (z.get("n_zero", 0), rg.get("days", 30),
                           100 * (z.get("rate") or 0), len(z["real"])),
                "source": "retrieval_gaps",
                "why": "搜不到 = 知识库看起来「没有」，与索引不一致是同一类误归因；"
                       "但它是局部（某几个查询表达不匹配），不是系统性漏条",
                "action": "kb gaps（看是哪几条）· 补同义词或改查询表达",
                "recheck": "kb gaps",
            })
        if rep.get("n_zero_groups"):
            acts.append({
                "level": "P3",
                "what": "重复查询里有 %d 组某一次是零结果（= 上次没找到）"
                        % rep["n_zero_groups"],
                "source": "retrieval_gaps",
                "why": "行为本身就说明上一次没找到；不需要人标注",
                "action": "kb gaps（看具体查询）",
                "recheck": "kb gaps",
            })

    g = st.get("gate") or {}
    if isinstance(g, dict) and "error" not in g:
        c = _gate_counts(g)
        if c.get("reject"):
            acts.append({
                "level": "P2",
                "what": "认识论 reject %d 条（硬门不过：素材规模不足或断言对不上）" % c["reject"],
                "source": "gate",
                "why": "这些条目的提炼结论不成立，留在 knowledge 会让检索答案带错；"
                       "回 raw 可撤销（gate --revert <batch>）",
                "action": "python -m bili_digest.quality.gate --apply",
                "recheck": "python -m bili_digest.quality.gate",
            })

    rank = {k: i for i, k in enumerate(LEVELS)}
    acts.sort(key=lambda a: rank.get(a["level"], 9))
    return acts

def _slug(status: str) -> str:
    return {"OK": "ok", "WARN": "warn", "FAIL": "fail"}.get(status, "unknown")

def as_dict(st: dict) -> dict:
    from bili_digest.schema.core_schema import now_sec as _now

    out = {"schema": "kb-status/1", "generated_at": _now(), "groups": {}}
    g = out["groups"]

    h = st.get("health") or {}
    if isinstance(h, dict) and "error" not in h:
        checks = {k: {"status": v[0], "value": v[1], "note": v[2]}
                  for k, v in h.items() if isinstance(v, tuple) and len(v) >= 3}
        sev = "fail" if any(c["status"] == "FAIL" for c in checks.values()) else (
            "warn" if any(c["status"] == "WARN" for c in checks.values()) else "ok")
        g["health"] = {"source": "quality.health.snapshot", "severity": sev,
                       "checks": checks}
    else:
        g["health"] = {"source": "quality.health.snapshot", "severity": "unknown",
                       "error": str(h.get("error") if isinstance(h, dict) else h)}

    b = st.get("budget") or {}
    if isinstance(b, dict) and "error" not in b:
        use, lim = b.get("usage") or {}, b.get("limits") or {}
        over = [k for k, v in use.items() if v > lim.get(k, 0)]
        g["budget"] = {"source": "quality.budget.usage+limits",
                       "severity": "warn" if over else "ok",
                       "used": use, "limits": lim, "over": over}
    else:
        g["budget"] = {"source": "quality.budget.usage+limits", "severity": "unknown",
                       "error": str(b.get("error") if isinstance(b, dict) else b)}

    hv = st.get("harvest") or []
    g["harvest"] = {"source": "quality.harvest.gaps",
                    "severity": "warn" if (isinstance(hv, list) and hv) else "ok",
                    "items": hv if isinstance(hv, list) else [],
                    **({"error": str(hv)} if not isinstance(hv, list) else {})}

    rg = st.get("retrieval") or {}
    if isinstance(rg, dict) and "error" not in rg:
        real = (rg.get("zero") or {}).get("real") or []
        g["retrieval"] = {"source": "quality.retrieval_gaps.report",
                          "severity": "warn" if real else "ok", "data": rg}
    else:
        g["retrieval"] = {"source": "quality.retrieval_gaps.report",
                          "severity": "unknown", "error": str(rg)}

    gp = st.get("gate") or {}
    if isinstance(gp, dict) and "error" not in gp:
        cnt = _gate_counts(gp)
        g["gate"] = {"source": "quality.gate.plan",
                     "severity": "warn" if cnt.get("reject") else "ok",
                     "verdicts": cnt, "rule_version": gp.get("rule_version")}
    else:
        g["gate"] = {"source": "quality.gate.plan", "severity": "unknown",
                     "error": str(gp)}

    cov = st.get("cov") or {}
    g["cov"] = {"source": "quality.kb_cov.library",
                "severity": "unknown" if (isinstance(cov, dict) and "error" in cov) else "ok",
                "data": cov}

    acts = priorities(st)
    out["priorities"] = acts
    out["levels"] = sorted({a["level"] for a in acts})
    return out

def render(con, st: dict = None) -> list:
    st = st if st is not None else collect(con)
    L = ["", "  ── 库状态（统一视图）──────────────────────────", ""]

    cov = st.get("cov") or {}
    if "distribution" in cov:
        d = cov["distribution"]
        total = max(cov.get("items", 1), 1)
        L.append("  覆盖      " + " · ".join(
            "%s %d（%.0f%%）" % (k, d.get(k, 0), 100.0 * d.get(k, 0) / total)
            for k in ("full", "partial", "meta_only", "empty")) +
            "   [kb_cov]")
        L.append("            注：health 报「有字幕 %.0f%%」、harvest 报「空壳 %.0f%%」，"
                  % (100.0 * d.get("full", 0) / total, 100.0 * d.get("empty", 0) / total))
        L.append("            两者都是这张四档表的切片，加起来不等于 100% —— "
                  "中间那档是「有内容无字幕」")
    else:
        L.append("  覆盖      [kb_cov 不可用] %s" % cov.get("error", ""))

    g = st.get("gate") or {}
    if isinstance(g, dict) and "error" not in g:
        c = _gate_counts(g)
        L.append("  认识论    validated %d · provisional %d · reject %d（%d 条）  [gate]"
                 % (c["validated"], c["provisional"], c["reject"], c["total"]))

    b = st.get("budget") or {}
    if isinstance(b, dict) and "error" not in b:
        from bili_digest.quality import budget as B
        u, lim = b["usage"], b["limits"]
        short = {"fetch_items": "抓取", "write_rows": "写入", "llm_calls": "LLM",
                 "rebuilds": "重建", "concurrent": "并发"}
        L.append("  预算      " + " · ".join(
            "%s %g/%g%s" % (short.get(k, k), u.get(k, 0), lim.get(k, 0),
                            " ←超" if u.get(k, 0) > lim.get(k, 0) else "")
            for k in ("fetch_items", "write_rows", "llm_calls", "rebuilds", "concurrent"))
            + "   [budget]")

    rg = st.get("retrieval") or {}
    if isinstance(rg, dict) and "error" not in rg:
        z = rg.get("zero") or {}
        rep = rg.get("repeat") or {}
        L.append("  检索      %d 条查询 · 零结果 %d（%.1f%%，真缺口 %d）· "
                 "重复查询 %d 组   [retrieval_gaps]"
                 % (z.get("n_queries", 0), z.get("n_zero", 0),
                    100 * (z.get("rate") or 0), len(z.get("real") or []),
                    rep.get("n", 0)))

    h = st.get("health") or {}
    if isinstance(h, dict) and "error" not in h:
        bad = [k for k, v in h.items() if isinstance(v, tuple) and v and v[0] == "FAIL"]
        warn = [k for k, v in h.items() if isinstance(v, tuple) and v and v[0] == "WARN"]
        L.append("  一致性    %s   [health]" % (
            ("✗ " + "、".join(bad) + ("；! " + "、".join(warn) if warn else ""))
            if bad else ("全绿" if not warn else "全绿（! " + "、".join(warn) + "）")))

    acts = priorities(st)
    L.append("")
    if not acts:
        L.append("  ✓ 没有需要立即处理的动作。")
    else:
        L.append("  ── 现在最该做的（按阻断性排序，不是按重要性投票）──")
        for a in acts:
            L.append("")
            L.append("  [%s] %s" % (a["level"], a["what"]))
            L.append("        来源：%s   为什么：%s" % (a["source"], a["why"]))
            L.append("        动作：%s" % a["action"])
            L.append("        复算：%s" % a["recheck"])

    L.append("")
    L.append("  ── 只陈述、不派动作的 ──")
    L.append("    · provisional %d 条：引用可回查率低，是认识论状态不是「该修」"
             % _gate_counts(g).get("provisional", 0))
    L.append("    · 独特内容孤点 / 薄主题：修它们等于为图谱好看而造边（harvest 的判断）")
    L.append("")
    return L

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="统一状态视图：整体怎么样、下一步做哪件")
    ap.add_argument("--json", action="store_true", help="机器可读")
    a = ap.parse_args()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    con = connect(DB)
    try:
        if a.json:
            import json as _j
            st = collect(con)
            print(_j.dumps({"priorities": priorities(st)}, ensure_ascii=False, indent=1))
        else:
            for ln in render(con):
                print(ln)
    finally:
        con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
