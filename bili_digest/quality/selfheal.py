#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time

from bili_digest import paths
from bili_digest.paths import DB

PY = sys.executable
ROOT = paths.HERE

def _run(args: list, label: str, timeout: int = 900) -> tuple:
    try:
        r = subprocess.run([PY, "-m"] + args, cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:40])
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, tail[-1][:80] if tail else "返回码 %d" % r.returncode
    return True, "ok"

def plan(allow_llm: bool = False) -> list:
    from bili_digest.quality import health as _h
    st = _h.snapshot(full=False)
    acts = []

    def add(aid, why, act, cost, reversible, auto) -> None:
        acts.append({"id": aid, "why": why, "action": act, "cost": cost,
                     "reversible": reversible, "auto": auto})

    if st.get("索引一致性", ("",))[0] != "OK":
        add("reindex", "索引库/向量/FTS 三方不一致", ["bili_digest", "index"], "约 45s", True, True)
    if st.get("陈旧向量", ("",))[0] != "OK":
        add("reindex-stale", "有块文本已变但向量未重编", ["bili_digest", "index"], "约 45s", True, True)
    if st.get("关键词基线", ("",))[0] == "WARN":
        add("keywords", "关键词基线与实际块数不一致（图会过期）",
            ["bili_digest.retrieve.keywords", "--build"], "约 5s", True, True)
    if st.get("切段完整性", ("",))[0] == "WARN":
        add("seg", "有字幕却没有段落块",
            ["bili_digest.ingest.seg_boundary"], "约 30s", True, True)
    if st.get("重排缓存", ("",))[0] == "WARN":
        add("lateon", "晚交互重排缓存缺失（可选功能）",
            ["bili_digest.retrieve.lateon", "--build"], "数分钟", True, False)

    try:
        from bili_digest.quality import flywheel as _fw
        fw = _fw.report(min_n=10)
        if fw.get("n"):
            worst = [r for r in fw["rows"] if r["维度"] == "块类型"][:1]
            if worst and worst[0]["负样本率"] > 0.7:
                add("kind-weight",
                    "块类型 %s 的 0 分率 %.0f%%（超过 70%%）" % (
                        worst[0]["取值"], 100 * worst[0]["负样本率"]),
                    None, "-", True, False)
    except Exception:
        pass

    try:
        import sqlite3 as _s
        con = _s.connect(str(DB))
        tot = con.execute("SELECT COUNT(*) FROM derived WHERE evidence IS NOT NULL "
                          "AND evidence != ''").fetchone()[0]
        bad = 0
        for (ev,) in con.execute("SELECT evidence FROM derived WHERE evidence IS NOT NULL "
                                 "AND evidence != '' LIMIT 300"):
            import json as _j
            try:
                ids = _j.loads(ev)
            except ValueError:
                continue
            if not ids:
                continue
            ph = ",".join("?" * len(ids))
            alive = con.execute("SELECT COUNT(*) FROM content_block WHERE id IN (%s)"
                                % ph, ids).fetchone()[0]
            if alive < len(ids):
                bad += 1
        con.close()
        if tot and bad / min(tot, 300) > 0.3:
            add("recite", "抽样 300 条里 %d%% 的 evidence 至少一条悬空（块重建导致）"
                % int(100 * bad / min(tot, 300)),
                "derive --limit 全部（重提炼，覆盖 citations）",
                "LLM 调用", False, allow_llm)
    except Exception:
        pass

    return acts

def execute(acts: list) -> list:
    done = []
    for a in acts:
        if not (a["auto"] and a["reversible"]):
            done.append({**a, "ran": False, "note": "跳过（需人工或不可逆）"})
            continue
        if not a.get("action"):
            done.append({**a, "ran": False, "note": "只报告，无动作"})
            continue
        ok, msg = _run(list(a["action"]), a["id"])
        done.append({**a, "ran": True, "ok": ok, "note": msg})
    return done

def verify(before: dict) -> dict:
    from bili_digest.quality import health as _h
    after = _h.snapshot(full=False)
    diff = []
    for k, v in after.items():
        b = before.get(k)
        if b and b[0] != v[0]:
            diff.append((k, b[0], v[0]))
    return {"after": after, "changed": diff}

def render(acts: list, results: list = None, ver: dict = None) -> list:
    L = ["", "  ── 自愈计划 ─────────────────────────", ""]
    if not acts:
        L.append("  ✓ 没有需要修的东西")
        L.append("")
        return L
    for i, a in enumerate(acts, 1):
        mark = "自动" if (a["auto"] and a["reversible"]) else "人工"
        L.append("  [%d] (%s) %s" % (i, mark, a["id"]))
        L.append("      为什么：%s" % a["why"])
        act_s = " ".join(a["action"]) if a.get("action") else "（只报告）"
        L.append("      动作  ：%s   成本 %s" % (act_s, a["cost"]))
    if results:
        L.append("")
        L.append("  ── 执行结果 ──")
        for r in results:
            if not r.get("ran"):
                L.append("    - %-12s 跳过：%s" % (r["id"], r["note"]))
            else:
                L.append("    %s %-12s %s" % ("✓" if r.get("ok") else "✗", r["id"], r["note"]))
    if ver:
        L.append("")
        L.append("  ── 验证（执行后重跑 health）──")
        if ver["changed"]:
            for k, b, a2 in ver["changed"]:
                L.append("    %s：%s → %s" % (k, b, a2))
        else:
            L.append("    状态未变")
    L.append("")
    return L

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="自愈：发现 → 动作 → 执行 → 验证")
    ap.add_argument("--dry", action="store_true", help="只出计划，不执行")
    ap.add_argument("--allow-llm", action="store_true",
                    help="额外允许需要 LLM 的动作（会覆盖既有提炼，执行前请确认）")
    a = ap.parse_args()

    from bili_digest.quality import health as _h
    before = _h.snapshot(full=False)
    acts = plan(allow_llm=a.allow_llm)
    if a.dry or not acts:
        for ln in render(acts):
            print(ln)
        return 0
    results = execute(acts)
    time.sleep(0.3)
    ver = verify(before)
    for ln in render(acts, results, ver):
        print(ln)
    bad = [r for r in results if r.get("ran") and not r.get("ok")]
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
