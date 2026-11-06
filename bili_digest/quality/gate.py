#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time

from bili_digest.schema.core_schema import now_sec

RULE_VERSION = "gate-1"

GATES = {
    "scale": {
        "min_blocks": 50,
        "min_chars": 1000,
        "level": "hard",
    },
    "conformity": {
        "max_missing": 0,
        "level": "hard",
    },
    "traceability": {
        "min_ok_rate": 0.80,
        "require_quote": True,
        "level": "soft",
    },
}

DERIVED_KINDS = ("summary", "key_points", "insights", "topics",
                 "audience", "scores", "seg_summary")

_DDL_AUDIT = """
CREATE TABLE IF NOT EXISTS gate_audit (
  id INTEGER PRIMARY KEY,
  item_uid TEXT NOT NULL,
  axis TEXT NOT NULL,           -- tier | epistemic
  from_value TEXT,
  to_value TEXT NOT NULL,
  gate TEXT NOT NULL,           -- scale | conformity | traceability
  reason TEXT NOT NULL,
  metrics TEXT NOT NULL,        -- 判定时的实测值（下次复测的基线）
  rule_version TEXT NOT NULL,
  batch TEXT NOT NULL,
  decided_at INTEGER NOT NULL,
  reverted_at INTEGER
)
"""

def _ensure(con) -> None:
    con.execute(_DDL_AUDIT)
    con.commit()

def _metrics(con, uid: str) -> dict:
    from bili_digest.quality import fix_cites as fc
    from bili_digest.quality.faithfulness import entities, materials

    row = con.execute("SELECT * FROM item WHERE uid=?", (uid,)).fetchone()
    if row is None:
        return {}
    d = {}
    for r in con.execute("SELECT kind,content FROM derived WHERE item_uid=?", (uid,)):
        d[r["kind"]] = r["content"]
    ph = ",".join("?" * len(DERIVED_KINDS))
    stat = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(text)),0) chars FROM content_block "
        "WHERE item_uid=? AND kind NOT IN (" + ph + ")",
        (uid,) + DERIVED_KINDS).fetchone()
    blocks = {b["id"]: b for b in con.execute(
        "SELECT id,kind,offset_sec,text FROM content_block WHERE item_uid=?", (uid,))}
    try:
        kp = json.loads(d.get("key_points") or "[]")
        ins = json.loads(d.get("insights") or "[]")
    except Exception:
        kp, ins = [], []
    cites = [c for it in (kp + ins) if isinstance(it, dict)
             for c in (it.get("cites") or [])]
    withq = sum(1 for c in cites if fc._quote_of(c))
    ok = 0
    for c in cites:
        b = blocks.get(fc._id_of(c))
        q = fc._quote_of(c)
        if b is not None and q and q in (b["text"] or ""):
            ok += 1
    body = materials(row, con.execute)
    try:
        summ = str(json.loads(d.get("summary") or '""'))
    except Exception:
        summ = str(d.get("summary") or "")
    ents = entities(summ)
    miss = [e for e in ents if e not in body]
    return {"blocks": stat["n"], "chars": stat["chars"],
            "cites": len(cites), "with_quote": withq, "cite_ok": ok,
            "ok_rate": (ok / len(cites)) if cites else None,
            "entities": len(ents), "missing": len(miss),
            "key_points": len(kp), "insights": len(ins)}

def judge(m: dict) -> list:
    fails = []
    g = GATES["scale"]
    if m.get("blocks", 0) < g["min_blocks"] and m.get("chars", 0) < g["min_chars"]:
        fails.append(("scale", g["level"],
                      "素材不足（块 %d 需 %d，字 %d 需 %d）" % (
                          m.get("blocks", 0), g["min_blocks"],
                          m.get("chars", 0), g["min_chars"])))
    g = GATES["conformity"]
    if m.get("missing", 0) > g["max_missing"]:
        fails.append(("conformity", g["level"],
                      "实体缺失 %d 个（上限 %d）" % (m["missing"], g["max_missing"])))
    g = GATES["traceability"]
    if m.get("cites", 0) == 0:
        fails.append(("traceability", g["level"], "没有引用"))
    elif m.get("with_quote", 0) == 0:
        fails.append(("traceability", g["level"], "引用无 quote，不可验证"))
    elif (m.get("ok_rate") or 0) < g["min_ok_rate"]:
        fails.append(("traceability", g["level"],
                      "可回查率 %.2f < %.2f" % (m["ok_rate"] or 0, g["min_ok_rate"])))
    return fails

def evaluate(con, tier: str = "knowledge") -> list:
    uids = [r[0] for r in con.execute(
        "SELECT item_uid FROM lifecycle WHERE tier=?", (tier,))]
    marked = {r[0] for r in con.execute(
        "SELECT item_uid FROM tag WHERE kind='marked'")}
    out = []
    for uid in uids:
        m = _metrics(con, uid)
        fails = judge(m)
        if uid in marked:
            verdict = "manual"
        elif any(f[1] == "hard" for f in fails):
            verdict = "reject"
        elif fails:
            verdict = "provisional"
        else:
            verdict = "validated"
        out.append({"uid": uid, "metrics": m, "fails": fails,
                    "manual": uid in marked, "verdict": verdict})
    return out

def plan(con, tier: str = "knowledge") -> dict:
    rows = evaluate(con, tier)
    cnt = {}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1

    why = {"reject": {}, "provisional": {}}
    for r in rows:
        box = why.get(r["verdict"])
        if box is None:
            continue
        for g, _lv, _msg in r["fails"]:
            box[g] = box.get(g, 0) + 1
    return {"tier": tier, "total": len(rows), "verdicts": cnt,
            "reasons": why, "rows": rows, "rule_version": RULE_VERSION}

def apply(con, tier: str = "knowledge",
          verdicts=("reject", "provisional", "validated"),
          dry_run: bool = True) -> dict:
    rows = evaluate(con, tier)
    batch = time.strftime("%Y%m%d-%H%M%S")
    _ensure(con)
    changed = []
    for r in rows:
        if r["verdict"] not in verdicts:
            continue
        prev = con.execute(
            "SELECT tier,reason FROM lifecycle WHERE item_uid=?",
            (r["uid"],)).fetchone()
        from_tier = prev[0] if prev else None
        if r["verdict"] == "reject":
            to_tier = "raw"
            axis, from_value, to_value = "tier", from_tier, to_tier
            reason = "；".join(f[2] for f in r["fails"] if f[1] == "hard")
        elif r["verdict"] == "provisional":

            to_tier = from_tier
            axis = "verdict"
            from_value = None
            to_value = "provisional"
            reason = "；".join(f[2] for f in r["fails"])
        else:
            to_tier = from_tier
            axis = "verdict"
            from_value = None
            to_value = "validated"
            reason = "过全部门"
        changed.append({"uid": r["uid"], "verdict": r["verdict"],
                        "from": from_tier, "to": to_tier, "reason": reason})
        if dry_run:
            continue
        metrics = dict(r["metrics"])
        metrics["prev_reason"] = prev[1] if prev else None
        con.execute(
            "INSERT INTO gate_audit(item_uid,axis,from_value,to_value,gate,"
            "reason,metrics,rule_version,batch,decided_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["uid"], axis, from_value, to_value,
             (r["fails"][0][0] if r["fails"] else "all"), reason,
             json.dumps(metrics, ensure_ascii=False), RULE_VERSION, batch,
             now_sec()))
        if axis == "tier":
            con.execute("UPDATE lifecycle SET tier=?, reason=? WHERE item_uid=?",
                        (to_tier, ("gate-" + RULE_VERSION + " " + reason)[:200],
                         r["uid"]))
    if not dry_run:
        con.commit()
    return {"batch": batch, "changed": changed, "dry_run": dry_run,
            "rule_version": RULE_VERSION,
            "counts": {v: sum(1 for c in changed if c["verdict"] == v)
                       for v in verdicts}}

def revert(con, batch: str) -> dict:
    _ensure(con)
    rows = con.execute(
        "SELECT id,item_uid,axis,from_value,to_value FROM gate_audit "
        "WHERE batch=? AND reverted_at IS NULL ORDER BY id DESC", (batch,)
    ).fetchall()
    done = skipped = 0
    now = now_sec()
    for r in rows:
        later = con.execute(
            "SELECT 1 FROM gate_audit WHERE item_uid=? AND id>? "
            "AND reverted_at IS NULL LIMIT 1", (r[1], r[0])).fetchone()
        if later:
            skipped += 1
            continue
        if r[2] == "tier":
            con.execute("UPDATE lifecycle SET tier=? WHERE item_uid=?",
                        (r[3], r[1]))

        con.execute("UPDATE gate_audit SET reverted_at=? WHERE id=?", (now, r[0]))
        done += 1
    con.commit()
    return {"batch": batch, "reverted": done, "skipped": skipped}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    ap = argparse.ArgumentParser(description="认识论晋升门（默认只读）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--tier", default="knowledge")
    ap.add_argument("--apply", action="store_true", help="真的写库（否则只出方案）")
    ap.add_argument("--revert", metavar="BATCH", help="撤销某个批次")
    a = ap.parse_args()

    con = connect(a.db)
    con.row_factory = __import__("sqlite3").Row
    if a.revert:
        print(revert(con, a.revert))
        con.close()
        return 0
    r = plan(con, a.tier)
    print("  规则版本 %s · %s 档 %d 条" % (r["rule_version"], r["tier"], r["total"]))
    for k in ("validated", "provisional", "reject", "manual"):
        if r["verdicts"].get(k):
            print("    %-12s %3d" % (k, r["verdicts"][k]))
    for tag, key in (("回 raw 的原因", "reject"), ("降 provisional 的原因", "provisional")):
        box = (r["reasons"] or {}).get(key) or {}
        if not box:
            continue
        print("  %s：" % tag)
        for k, v in sorted(box.items(), key=lambda x: -x[1]):
            print("    %-20s %3d" % (k, v))
    if a.apply:
        res = apply(con, a.tier, dry_run=False)
        print("\n  已执行 batch=%s：%s" % (res["batch"], res["counts"]))
        print("  撤销：python -m bili_digest.quality.gate --revert %s" % res["batch"])
    else:
        print("\n  （只读。加 --apply 才写库）")
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
