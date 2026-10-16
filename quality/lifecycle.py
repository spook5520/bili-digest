#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core_schema import (TIER_CACHE, TIER_DISCARDED, TIER_KNOWLEDGE, TIER_RAW,
                         connect, load_blocklist, now_sec, set_tier)

W = {
    "saved":      0.28,
    "marked":     0.20,
    "density":    0.18,
    "quality":    0.12,
    "freshness":  0.08,
    "source":     0.14,
    "penalty":   -0.30,
}

KEEP_THRESHOLD = 0.55
DISCARD_THRESHOLD = 0.18

MARK_TAGS = ("important", "manual", "star")

HYSTERESIS = 0.03

from score import Factor, Scorer as _Scorer, factor_bool as _fb

_ITEM_SCORER = _Scorer(
    "item",
    factors=[

        Factor("saved", W["saved"], lambda c: c["saved"]),

        Factor("marked", W["marked"], _fb("marked")),
        Factor("density", W["density"], lambda c: c["density"]),
        Factor("quality", W["quality"], lambda c: c["quality"]),
        Factor("freshness", W["freshness"], lambda c: c["freshness"]),
        Factor("source", W["source"], lambda c: c["src"]),
    ],
    penalties=[Factor("blocked", abs(W["penalty"]), _fb("blocked"))],
    bonus=[Factor("saved_extra", W["saved"], lambda c: c["saved_bonus"])],
    keep=KEEP_THRESHOLD, discard=DISCARD_THRESHOLD,
)
CAPACITY_RAW_BLOCK = 200_000

def compute_density(sub_chars: int, n_dm: int, n_cm: int, dur: int) -> tuple[float, float]:
    cps = (sub_chars / dur) if dur else 0.0
    if cps >= 0.3:

        return min(cps / 2.5, 1.0), cps

    dm_rate = (n_dm / dur) if dur else 0.0
    cm_rate = (n_cm / dur) if dur else 0.0
    d = (min(dm_rate / 2.0, 1.0) * 0.7 + min(cm_rate / 0.5, 1.0) * 0.3) * 0.85
    return d, cps

EVERGREEN_PAT = re.compile(
    r"教程|入门|原理|从零|详解|实战|指南|手册|基础|系统|全集|课程|"
    r"文档|规范|协议|算法|架构|设计模式|tutorial|guide|reference", re.I)
EPHEMERAL_PAT = re.compile(
    r"最新|首发|发布会|直播回放|热点|新闻|快讯|月报|周报|盘点|"
    r"20[2-9]\d\s*年|年度|赛季|赛事|投票|抽奖", re.I)

TTL_EVERGREEN = None
TTL_NORMAL = 365 * 86400
TTL_EPHEMERAL = 30 * 86400

def classify_freshness(title: str, published_at: int | None,
                       tags: list[str] | None = None) -> tuple[str, int | None, float]:
    blob = f"{title or ''} {' '.join(tags or [])}"
    ever = bool(EVERGREEN_PAT.search(blob))
    eph = bool(EPHEMERAL_PAT.search(blob))

    if ever and not eph:
        kind, ttl = "evergreen", TTL_EVERGREEN
        half_life = 3 * 365 * 86400
    elif eph and not ever:
        kind, ttl = "ephemeral", TTL_EPHEMERAL
        half_life = 14 * 86400
    else:
        kind, ttl = "normal", TTL_NORMAL
        half_life = 180 * 86400

    if not published_at:
        return kind, ttl, 0.5
    age = max(0, now_sec() - int(published_at))

    score = 0.5 ** (age / half_life)
    return kind, ttl, round(score, 4)

def set_mark(con: sqlite3.Connection, item_uid: str, name: str = "important",
             on: bool = True) -> bool:
    if name not in MARK_TAGS:
        name = "important"
    if on:
        con.execute("INSERT OR REPLACE INTO tag(item_uid,name,kind,source) "
                    "VALUES(?,?,?,?)", (item_uid, name, "custom", "human"))
    else:
        con.execute("DELETE FROM tag WHERE item_uid=? AND name=? AND kind='custom'",
                    (item_uid, name))
    con.commit()
    return True

def score_item(con: sqlite3.Connection, item_uid: str,
               rules: dict | None = None) -> dict:
    row = con.execute("SELECT * FROM item WHERE uid=?", (item_uid,)).fetchone()
    if not row:
        return {"uid": item_uid, "score": 0.0, "missing": True}

    d = {k: row[k] for k in row.keys()}
    tags = [r[0] for r in con.execute(
        "SELECT name FROM tag WHERE item_uid=?", (item_uid,))]

    ncoll = con.execute("SELECT COUNT(*) FROM collection_member WHERE item_uid=?",
                        (item_uid,)).fetchone()[0]

    saved = 1.0 if ncoll >= 1 else 0.3
    saved_bonus = min(ncoll - 1, 2) * 0.3 if ncoll > 1 else 0.0

    lc = con.execute("SELECT tier,reason FROM lifecycle WHERE item_uid=?",
                     (item_uid,)).fetchone()
    marked = 0.0
    if any(t in MARK_TAGS for t in tags):
        marked = 1.0
    elif lc and lc["reason"] and "manual" in (lc["reason"] or ""):
        marked = 1.0

    sub_chars, sub_n = 0, 0
    r = con.execute("SELECT COALESCE(SUM(LENGTH(text)),0), COUNT(*) "
                    "FROM content_block WHERE item_uid=? "
                    "AND kind IN ('subtitle','subtitle_seg')",
                    (item_uid,)).fetchone()
    sub_chars, sub_n = r[0], r[1]
    dur = d.get("duration_sec") or 0
    n_dm = con.execute("SELECT COUNT(*) FROM content_block "
                       "WHERE item_uid=? AND kind='danmaku'", (item_uid,)).fetchone()[0]
    n_cm = con.execute("SELECT COUNT(*) FROM content_block "
                       "WHERE item_uid=? AND kind='comment'", (item_uid,)).fetchone()[0]
    density, cps = compute_density(sub_chars, n_dm, n_cm, dur)

    hi = con.execute("SELECT COUNT(*) FROM content_block WHERE item_uid=? "
                     "AND kind='comment' AND weight >= 20", (item_uid,)).fetchone()[0]
    quality = min(hi / 5.0, 1.0) if n_cm else 0.0

    quality = min(quality + min(n_cm / 200.0, 0.4), 1.0)

    fkind, ttl, freshness = classify_freshness(
        d.get("title"), d.get("published_at"), tags)

    src = None
    try:
        from source_weight import up_factor as _up_factor
        src = _up_factor(con, d.get("author_uid"))
    except (sqlite3.OperationalError, TypeError, ValueError):
        pass
    if src is None:

        try:
            from source_weight import median_weight as _mw
            src = _mw(con)
        except Exception:
            src = 0.0

    penalty = 0.0
    bl_reason = ""
    if rules is None:
        rules = load_blocklist(con)
    blocked, why = _check_block(rules, d, tags)
    if blocked:
        penalty = 1.0
        bl_reason = why

    score = (W["saved"] * saved + W["marked"] * marked
             + W["density"] * density + W["quality"] * quality
             + W["freshness"] * freshness + W["source"] * src
             + W["penalty"] * penalty
             + W["saved"] * saved_bonus)

    return {
        "uid": item_uid,
        "score": round(max(0.0, min(score, 1.0)), 4),
        "saved": saved, "marked": marked, "density": round(density, 3),
        "quality": round(quality, 3), "freshness": freshness,

        "source": round(src, 3),
        "freshness_kind": fkind, "ttl": ttl,
        "sub_chars": sub_chars, "sub_n": sub_n, "dur": dur,
        "cps": round(cps, 2), "n_dm": n_dm, "n_cm": n_cm, "hi_cm": hi,
        "blocked": blocked, "block_reason": bl_reason,
    }

def _check_block(rules: dict, item: dict, tags: list[str]) -> tuple[bool, str]:
    for e in rules["video"]:
        if e["value"] == item.get("item_id"):
            return True, f"video:{e['value']}"
    for e in rules["up"]:
        if e["value"] == str(item.get("author_uid")):
            return True, f"up:{e['label'] or e['value']}"
    for e in rules["tag"]:
        if e["value"] in tags:
            return True, f"tag:{e['value']}"
    if item.get("extras"):
        try:
            ex = json.loads(item["extras"])
            part = ex.get("partition") or ex.get("tname")
            if part:
                for e in rules["partition"]:
                    if e["value"] == part:
                        return True, f"partition:{part}"
        except Exception:
            pass
    blob = f"{item.get('title') or ''} {item.get('url') or ''}"
    for val, rx in rules["_keyword_re"]:
        if rx.search(blob):
            return True, f"keyword:{val}"
    return False, ""

def _dims_snapshot(s: dict) -> dict:
    snap = {}
    for k in W:
        snap[k] = s.get(k)
    snap["saved_bonus"] = s.get("saved_bonus")
    snap["freshness_kind"] = s.get("freshness_kind")
    snap["score"] = s.get("score")
    snap["_W"] = dict(W)
    return snap

def _redundancy_threshold(con: sqlite3.Connection) -> float:
    try:
        vs = sorted((r[0] or 0.0) for r in con.execute(
            "SELECT COALESCE(SUM(weight),0) FROM edge GROUP BY src"))
        return float(vs[int(len(vs) * 0.75)]) if vs else 0.0
    except sqlite3.OperationalError:
        return 0.0

def _redundancy(con: sqlite3.Connection, uid: str) -> float:
    try:
        r = con.execute("SELECT COALESCE(SUM(weight),0) FROM edge "
                        "WHERE src=? OR dst=?", (uid, uid)).fetchone()
        return float(r[0] or 0.0)
    except sqlite3.OperationalError:
        return 0.0

def _decide_tier(s: dict, prev_tier: str | None, has_raw: bool,
                 detail: str = "", n_blocks: int | None = None,
                 redundancy: float | None = None,
                 red_threshold: float | None = None) -> tuple[str, str]:
    if s["blocked"]:
        return TIER_DISCARDED, f"blocked:{s['block_reason']}"

    if prev_tier == TIER_KNOWLEDGE:
        if s["score"] >= KEEP_THRESHOLD - HYSTERESIS:
            return TIER_KNOWLEDGE, f"kept({s['score']:.3f}) [{detail}]"
    else:
        if s["score"] >= KEEP_THRESHOLD + HYSTERESIS:
            return TIER_KNOWLEDGE, f"score={s['score']:.3f} [{detail}]"
    if s["score"] < DISCARD_THRESHOLD:

        if s["saved"] >= 0.5:
            return TIER_CACHE, f"low({s['score']:.3f}) but saved [{detail}]"

        if (redundancy is not None and red_threshold is not None
                and redundancy > red_threshold):
            return TIER_DISCARDED, (f"low({s['score']:.3f}) + redundant"
                                    f"({redundancy:.0f}>{red_threshold:.0f}) [{detail}]")
        return TIER_CACHE, (f"low({s['score']:.3f}) but unique"
                            f"({redundancy:.0f})  [{detail}]")
    _tag = " no-content（未抓）" if (n_blocks == 0) else ""
    return (TIER_RAW if has_raw else TIER_CACHE), \
        f"mid({s['score']:.3f}) [{detail}]{_tag}"

def decide(con: sqlite3.Connection, item_uid: str, rules: dict | None = None,
           apply: bool = False, has_raw: bool | None = None) -> dict:
    if has_raw is None:
        try:
            n = con.execute("SELECT COUNT(*) FROM content_block WHERE item_uid=?",
                            (item_uid,)).fetchone()[0]
            has_raw = n > 0
        except sqlite3.OperationalError:
            has_raw = False
    s = score_item(con, item_uid, rules)
    if s.get("missing"):
        return s

    detail = (f"saved={s.get('saved', 0):.2f} density={s.get('density', 0):.2f} "
              f"quality={s.get('quality', 0):.2f} fresh={s.get('freshness', 0):.2f}"
              + (" marked" if s.get("marked") else "")
              + (f" src={s.get('source', 0):.2f}" if s.get("source") else ""))

    prev_tier = None
    try:
        row = con.execute("SELECT tier FROM lifecycle WHERE item_uid=?",
                          (item_uid,)).fetchone()
        if row is not None:
            prev_tier = row["tier"] if hasattr(row, "keys") else row[0]
    except Exception:
        prev_tier = None

    if s["blocked"]:
        tier, reason = _decide_tier(s, prev_tier, has_raw, detail)
        s["tier"] = tier
        s["reason"] = reason
        if apply:
            set_tier(con, item_uid, tier, s["score"], reason, s["ttl"],
                     dims=_dims_snapshot(s))
        return s

    _nb = con.execute("SELECT COUNT(*) FROM content_block WHERE item_uid=?",
                      (item_uid,)).fetchone()[0]
    _red = _redundancy(con, item_uid)
    _rth = _redundancy_threshold(con)
    tier, reason = _decide_tier(s, prev_tier, has_raw, detail, n_blocks=_nb,
                                redundancy=_red, red_threshold=_rth)

    s["tier"] = tier
    s["reason"] = reason
    if apply:
        set_tier(con, item_uid, tier, s["score"], reason, s["ttl"],
                 dims=_dims_snapshot(s))
    return s

@dataclass
class Prefetch:
    sub: dict = field(default_factory=dict)
    sub_n: dict = field(default_factory=dict)
    blk_n: dict = field(default_factory=dict)
    src_median: float = 0.0
    red: dict = field(default_factory=dict)
    red_p75: float = 0.0
    dm: dict = field(default_factory=dict)
    cm: dict = field(default_factory=dict)
    hi: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    coll: dict = field(default_factory=dict)
    lc: dict = field(default_factory=dict)
    src: dict = field(default_factory=dict)

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(f"Prefetch 没有字段 {key!r}；"
                           f"可选 {[f.name for f in fields(self)]}")

    def get(self, key, default=None):
        return getattr(self, key, default)

def _prefetch(con: sqlite3.Connection) -> Prefetch:
    pf = Prefetch()

    for r in con.execute(
            "SELECT item_uid, COALESCE(SUM(LENGTH(text)),0), COUNT(*) "
            "FROM content_block WHERE kind IN ('subtitle','subtitle_seg') "
            "GROUP BY item_uid"):
        pf["sub"][r[0]] = r[1]
        pf["sub_n"][r[0]] = r[2]

    for r in con.execute("SELECT item_uid, COUNT(*) FROM content_block "
                         "GROUP BY item_uid"):
        pf["blk_n"][r[0]] = r[1]
    for r in con.execute("SELECT item_uid, COUNT(*) FROM content_block "
                         "WHERE kind='danmaku' GROUP BY item_uid"):
        pf["dm"][r[0]] = r[1]
    for r in con.execute("SELECT item_uid, COUNT(*) FROM content_block "
                         "WHERE kind='comment' GROUP BY item_uid"):
        pf["cm"][r[0]] = r[1]
    for r in con.execute("SELECT item_uid, COUNT(*) FROM content_block "
                         "WHERE kind='comment' AND weight >= 20 GROUP BY item_uid"):
        pf["hi"][r[0]] = r[1]
    for r in con.execute("SELECT item_uid, GROUP_CONCAT(name,char(31)) "
                         "FROM tag GROUP BY item_uid"):
        pf["tags"][r[0]] = [x for x in (r[1] or "").split(chr(31)) if x]
    for r in con.execute("SELECT item_uid, COUNT(*) FROM collection_member "
                         "GROUP BY item_uid"):
        pf["coll"][r[0]] = r[1]
    for r in con.execute("SELECT item_uid, tier, reason FROM lifecycle"):
        pf["lc"][r[0]] = (r[1], r[2])

    try:
        from paths import source_key_prefix
        for r in con.execute(
                "SELECT sw.weight, i.uid FROM source_weight sw "
                "JOIN item i ON sw.source_key = ? || i.author_uid "
                "WHERE sw.kind='up'", (source_key_prefix("up"),)):
            pf["src"][r[1]] = r[0]
    except sqlite3.OperationalError:
        pass

    try:
        from source_weight import median_weight as _mw
        pf.src_median = _mw(con)
    except Exception:
        _vs = sorted(pf["src"].values())
        pf.src_median = _vs[len(_vs) // 2] if _vs else 0.0

    try:
        for r in con.execute("SELECT src,dst,weight FROM edge"):
            w = r[2] or 1.0
            pf["red"][r[0]] = pf["red"].get(r[0], 0.0) + w
            pf["red"][r[1]] = pf["red"].get(r[1], 0.0) + w
    except sqlite3.OperationalError:
        pass
    _rv = sorted(pf["red"].values())
    if _rv:

        pf.red_p75 = _rv[int(len(_rv) * 0.75)]
    return pf

def _source_factor(pf: dict, uid: str) -> float:
    v = (pf.get("src") or {}).get(uid)
    if v is None:
        return float(pf.get("src_median") or 0.0)
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return float(pf.get("src_median") or 0.0)

def score_from_prefetch(item: dict, pf: dict, rules: dict) -> dict:
    uid = item["uid"]
    tags = pf["tags"].get(uid, [])
    ncoll = pf["coll"].get(uid, 0)

    saved = 1.0 if ncoll >= 1 else 0.3
    saved_bonus = min(ncoll - 1, 2) * 0.3 if ncoll > 1 else 0.0

    lc = pf["lc"].get(uid)
    marked = 1.0 if (lc and lc[1] and "manual" in (lc[1] or "")) else 0.0

    sub_chars = pf["sub"].get(uid, 0)
    n_dm = pf["dm"].get(uid, 0)
    n_cm = pf["cm"].get(uid, 0)
    hi = pf["hi"].get(uid, 0)
    dur = item.get("duration_sec") or 0
    density, cps = compute_density(sub_chars, n_dm, n_cm, dur)

    quality = min(hi / 5.0, 1.0) if n_cm else 0.0
    quality = min(quality + min(n_cm / 200.0, 0.4), 1.0)

    fkind, ttl, freshness = classify_freshness(
        item.get("title"), item.get("published_at"), tags)

    blocked, why = _check_block(rules, item, tags)
    penalty = 1.0 if blocked else 0.0
    src = _source_factor(pf, uid)

    r = _ITEM_SCORER.evaluate({
        "saved": saved, "marked": marked, "density": density,
        "quality": quality, "freshness": freshness, "src": src,
        "blocked": penalty, "saved_bonus": saved_bonus,
    })
    score = r.score

    return {
        "uid": uid, "score": score,
        "saved": saved, "marked": marked, "density": round(density, 3),
        "quality": round(quality, 3), "freshness": freshness,
        "source": round(src, 3),
        "freshness_kind": fkind, "ttl": ttl,
        "sub_chars": sub_chars, "sub_n": pf["sub_n"].get(uid, 0), "dur": dur,
        "cps": round(cps, 2), "n_dm": n_dm, "n_cm": n_cm, "hi_cm": hi,
        "blocked": blocked, "block_reason": why,
        "factors": r.parts,
    }

def sweep(con: sqlite3.Connection, apply: bool = False,
          limit: int = 100000, with_uid: bool = False) -> dict:
    rules = load_blocklist(con)
    pf = _prefetch(con)
    cols = ("uid,platform,item_id,title,url,author_uid,author_name,"
            "published_at,duration_sec,extras")
    rows = con.execute(f"SELECT {cols} FROM item LIMIT ?", (limit,)).fetchall()
    out = {"total": 0, "by_tier": {}}
    if with_uid:
        out["by_uid"] = {}

    for r in rows:
        item = {k: r[k] for k in r.keys()}
        s = score_from_prefetch(item, pf, rules)

        has_raw = pf.get("blk_n", {}).get(item["uid"], 0) > 0

        _prev = pf["lc"].get(item["uid"])
        prev_tier = _prev[0] if _prev else None

        _d = (f"saved={s.get('saved', 0):.2f} density={s.get('density', 0):.2f} "
              f"quality={s.get('quality', 0):.2f} fresh={s.get('freshness', 0):.2f}"
              + (" marked" if s.get("marked") else "")
              + (f" src={s.get('source', 0):.2f}" if s.get("source") else ""))

        tier, reason = _decide_tier(
            s, prev_tier, has_raw, _d,
            n_blocks=pf.get("blk_n", {}).get(item["uid"], 0),
            redundancy=pf.get("red", {}).get(item["uid"], 0.0),
            red_threshold=pf.red_p75)

        out["total"] += 1
        out["by_tier"][tier] = out["by_tier"].get(tier, 0) + 1
        if with_uid:
            out["by_uid"][item["uid"]] = tier
        if apply:
            set_tier(con, item["uid"], tier, s["score"], reason, s["ttl"],
                     dims=_dims_snapshot(s))
    if apply:
        con.commit()
    return out

def expire(con: sqlite3.Connection, apply: bool = False) -> dict:
    out = {"expired": 0, "evicted": 0, "blocked_knowledge": 0}
    ts = now_sec()

    rows = con.execute(
        "SELECT item_uid FROM lifecycle WHERE tier=? AND expires_at IS NOT NULL "
        "AND expires_at < ?", (TIER_CACHE, ts)).fetchall()
    for r in rows:
        out["expired"] += 1
        if apply:
            set_tier(con, r["item_uid"], TIER_DISCARDED, reason="ttl expired")

    n_raw = con.execute("SELECT COUNT(*) FROM content_block cb JOIN lifecycle l "
                        "ON l.item_uid=cb.item_uid WHERE l.tier=?",
                        (TIER_RAW,)).fetchone()[0]
    if n_raw > CAPACITY_RAW_BLOCK:
        over = n_raw - CAPACITY_RAW_BLOCK
        victims = con.execute(
            "SELECT item_uid FROM lifecycle WHERE tier=? "
            "ORDER BY COALESCE(last_seen,0) ASC LIMIT ?",
            (TIER_RAW, max(1, over // 100))).fetchall()
        for v in victims:
            out["evicted"] += 1
            if apply:
                set_tier(con, v["item_uid"], TIER_DISCARDED, reason="raw capacity")

    rules = load_blocklist(con)

    tag_map: dict[str, list[str]] = {}
    for r in con.execute("SELECT item_uid, GROUP_CONCAT(name,char(31)) "
                         "FROM tag GROUP BY item_uid"):
        tag_map[r[0]] = [x for x in (r[1] or "").split(chr(31)) if x]

    for r in con.execute(
            "SELECT i.uid,i.item_id,i.author_uid,i.title,i.url,i.extras "
            "FROM lifecycle l JOIN item i ON i.uid=l.item_uid WHERE l.tier=?",
            (TIER_KNOWLEDGE,)).fetchall():
        blocked, why = _check_block(rules, dict(r), tag_map.get(r["uid"], []))
        if blocked:
            out["blocked_knowledge"] += 1
            if apply:
                set_tier(con, r["uid"], TIER_DISCARDED, reason=f"blocked:{why}")

    if apply:
        con.commit()
    return out

def touch(con: sqlite3.Connection, item_uid: str) -> None:
    con.execute("UPDATE lifecycle SET last_seen=? WHERE item_uid=?",
                (now_sec(), item_uid))

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="数据分级与生命周期")
    ap.add_argument("db", nargs="?", default="knowledge.db")
    ap.add_argument("--sweep", action="store_true", help="全库分级")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只演练）")
    ap.add_argument("--expire", action="store_true", help="处理 TTL 与容量")
    ap.add_argument("--explain", metavar="UID", help="解释某条为何定此档")
    a = ap.parse_args()

    c = connect(a.db)
    if a.explain:
        d = score_item(c, a.explain)
        for k, v in d.items():
            print(f"  {k:<16} {v}")
    elif a.expire:
        r = expire(c, apply=a.apply)
        print(f"  TTL 到期 {r['expired']}  容量淘汰 {r['evicted']}  "
              f"事后屏蔽 {r['blocked_knowledge']}")
        print("  演练模式；加 --apply 生效" if not a.apply else "  已生效")
    elif a.sweep:
        r = sweep(c, apply=a.apply)
        print(f"  处理 {r['total']} 条")
        for t, n in r["by_tier"].items():
            print(f"    {t:<12} {n}")
        print("  演练模式；加 --apply 生效" if not a.apply else "  已生效")
    else:
        print(f"门槛: knowledge≥{KEEP_THRESHOLD}  discarded<{DISCARD_THRESHOLD}")
        print(f"TTL: 常青=永久  普通={TTL_NORMAL//86400}天  时效={TTL_EPHEMERAL//86400}天")
        print(f"raw 容量上限: {CAPACITY_RAW_BLOCK} 块")
    c.close()
