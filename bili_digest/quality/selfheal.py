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
        msg = tail[-1][:80] if tail else "返回码 %d" % r.returncode

        if "Permission denied" in msg:
            msg += "（索引文件被占用：多半是 bili_digest.mcp.serve 常驻，先停它再重试）"
        return False, msg
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

    if st.get("提炼覆盖", ("",))[0] == "WARN":
        add("coverage", "knowledge 档有条目缺 summary（标记为该读、实际没提炼）",
            None, "需重提炼", True, False)
    if st.get("抓取失败", ("",))[0] == "WARN":
        add("fetch-fail", "近期有未修复的抓取失败（先看是退避、登录态还是条目已删）",
            None, "自查", True, False)
    if st.get("索引余量", ("",))[0] == "WARN":
        add("headroom", "向量索引容量余量低于 2%（再扩会撞上 add 极慢且不报错）",
            ["bili_digest", "index", "--rebuild"], "约 20 分钟", True, False)

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

def plan_and_log(allow_llm: bool = False) -> tuple:
    import json as _j
    import uuid
    from bili_digest.quality import budget as _bg
    from bili_digest.schema.core_schema import connect

    acts = plan(allow_llm)
    batch = "sh-" + uuid.uuid4().hex[:10]
    if not acts:
        return acts, batch
    con = connect(DB)
    try:
        _bg.record(
            con, actor="selfheal", step="selfheal:plan", status="proposed",
            rows=0, seconds=0.0, reversible=True, scope=batch,
            detail=_j.dumps({
                "batch": batch,
                "acts": [{"id": a["id"], "reversible": bool(a["reversible"]),
                          "auto": bool(a["auto"]),
                          "action": " ".join(a["action"]) if a.get("action") else None}
                         for a in acts],
            }, ensure_ascii=False))
    finally:
        con.close()
    return acts, batch

def audit(con, batch: str = None) -> list:
    import json as _j
    from bili_digest.quality import budget as _bg

    out = []

    if batch:
        row = con.execute(
            "SELECT scope, detail, ts FROM job_log WHERE actor='selfheal' "
            "AND status='proposed' AND scope=?", (batch,)).fetchone()
    else:
        row = con.execute(
            "SELECT scope, detail, ts FROM job_log WHERE actor='selfheal' "
            "AND status='proposed' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return [{"rule": "R1", "level": "P2",
                 "what": "没有 proposed 记录 —— 计划本身没留痕",
                 "evidence": "跑一次 kb selfheal（含 --dry）就会写入"}]

    b, detail, ts = row[0], row[1] or "{}", row[2]
    try:
        acts = (_j.loads(detail) or {}).get("acts") or []
    except ValueError:
        acts = []
    planned = {a.get("id") for a in acts}
    irrev = {a.get("id") for a in acts if not a.get("reversible")}

    actual = {r[0] for r in con.execute(
        "SELECT step FROM job_log WHERE actor='selfheal' AND scope=? "
        "AND status IN ('ok','fail')", (b,))}

    extra = actual - planned
    if extra:
        out.append({"rule": "R1", "level": "P0",
                    "what": "执行了计划外的动作：%s" % "、".join(sorted(extra)),
                    "evidence": "batch %s" % b})

    if irrev & actual:
        out.append({"rule": "R2", "level": "P0",
                    "what": "不可逆动作被执行：%s" % "、".join(sorted(irrev & actual)),
                    "evidence": "batch %s" % b})

    over = [r[0] for r in con.execute(
        "SELECT step FROM job_log WHERE actor='selfheal' AND scope=? "
        "AND status='overridden'", (b,))]
    if over:
        out.append({"rule": "R2", "level": "P1",
                    "what": "超预算被人工放行 %d 次：%s" % (len(over), "、".join(over)),
                    "evidence": "batch %s（放行本身可接受，但连续放行说明额度该调）" % b})

    if irrev:
        approved = [i for i in irrev if i in actual]
        out.append({"rule": "R3", "level": "P2",
                    "what": "计划里有 %d 个不可逆动作：%s%s" % (
                        len(irrev), "、".join(sorted(irrev)),
                        "（其中已执行：%s）" % "、".join(approved) if approved else "（均未执行）"),
                    "evidence": "batch %s · 这些需要你手工决定，selfheal 不会自动跑" % b})
    return out

def execute(acts: list, allow_over: bool = False, batch: str = None) -> list:
    from bili_digest.quality import budget as _bg
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    done = []
    for a in acts:
        if not (a["auto"] and a["reversible"]):
            done.append({**a, "ran": False, "note": "跳过（需人工或不可逆）"})
            continue
        if not a.get("action"):
            done.append({**a, "ran": False, "note": "只报告，无动作"})
            continue
        con = connect(DB)
        try:

            _bg.check(con, actor="selfheal", step=a["id"], rebuild=1,
                      allow_over=allow_over)
        except _bg.BudgetStop as e:
            done.append({**a, "ran": False, "blocked": True,
                         "note": "超预算，已停：%s" % str(e).splitlines()[0]})
            continue
        finally:
            con.close()
        t0 = time.time()
        ok, msg = _run(list(a["action"]), a["id"])
        con = connect(DB)
        try:
            _bg.record(con, actor="selfheal", step=a["id"],
                       status="ok" if ok else "fail",
                       seconds=round(time.time() - t0, 1),
                       rows=0, reversible=a["reversible"], detail=msg,
                       scope=batch or "*")
        finally:
            con.close()
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
    ap.add_argument("--yes", action="store_true",
                    help="超预算也执行本次（默认超预算就停，见 kb budget）")
    a = ap.parse_args()

    return run(dry=a.dry, allow_llm=a.allow_llm, yes=a.yes)

def run(dry: bool = False, allow_llm: bool = False, yes: bool = False) -> int:
    from bili_digest.quality import health as _h
    before = _h.snapshot(full=False)

    acts, batch = plan_and_log(allow_llm=allow_llm)
    if dry or not acts:
        for ln in render(acts):
            print(ln)
        _print_audit(batch)
        return 0
    results = execute(acts, allow_over=yes, batch=batch)
    time.sleep(0.3)
    ver = verify(before)
    for ln in render(acts, results, ver):
        print(ln)
    _print_audit(batch)
    bad = [r for r in results if r.get("ran") and not r.get("ok")]
    return 1 if bad else 0

def _print_audit(batch: str) -> None:
    from bili_digest.schema.core_schema import connect
    con = connect(DB)
    try:
        rows = audit(con, batch)
    finally:
        con.close()
    hit = {}
    for r in rows:
        hit.setdefault(r["rule"], []).append(r)

    print()
    print("  ── 越权审计（R1/R2/R3）—— 逐条说明，不是「无异常」──")
    base = {
        "R1": "不可触发：execute 遍历的就是 acts，actual ⊆ proposed 恒成立",
        "R2": "无输入：不可逆动作在 execute 第一关就被跳过，从不执行",
        "R3": "不适用：本计划没有不可逆动作",
    }
    for rule in ("R1", "R2", "R3"):
        items = hit.get(rule) or []
        if not items:
            print("    %s  %s" % (rule, base[rule]))
            continue
        for it in items:
            print("    %s  [%s] %s" % (rule, it["level"], it["what"]))
            print("        %s" % it["evidence"])
