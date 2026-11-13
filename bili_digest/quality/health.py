#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import sys

from bili_digest import paths
from bili_digest.paths import DB, VEC

def _q(con, sql, args=()) -> object:
    try:
        return con.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        return None

def shell_stats(con) -> dict:
    total = con.execute("SELECT COUNT(*) FROM item").fetchone()[0] or 0
    empty = [r[0] for r in con.execute(
        "SELECT uid FROM item WHERE uid NOT IN "
        "(SELECT DISTINCT item_uid FROM content_block)")]
    tried = {r[0] for r in con.execute(
        "SELECT DISTINCT scope FROM job_log WHERE scope LIKE 'bilibili:%'")}
    oked = {r[0] for r in con.execute(
        "SELECT DISTINCT scope FROM job_log WHERE scope LIKE 'bilibili:%' AND status='ok'")}
    never = [u for u in empty if u not in tried]
    failed = [u for u in empty if u in tried and u not in oked]
    other = len(empty) - len(never) - len(failed)

    from bili_digest.retrieve.temporal import now_sec as _now
    _cut = _now() - 365 * 86400
    _saved = {}
    try:
        _saved = {r[0]: r[1] for r in con.execute(
            "SELECT uid, saved_at FROM item WHERE saved_at IS NOT NULL")}
    except sqlite3.OperationalError:
        pass
    stale_never = [u for u in never if (_saved.get(u) or 0) and _saved[u] < _cut]
    recent_never = [u for u in never if u not in set(stale_never)]

    pct = (100.0 * len(empty) / total) if total else 0.0
    note = "待办 %d（≤1年未尝试）· 历史沉积 %d（>1年未尝试）· 尝试失败 %d" % (
        len(recent_never), len(stale_never), len(failed))
    if other:
        note += " · 其它 %d" % other
    return {"total": total, "empty": len(empty), "never": len(never),
            "recent_never": len(recent_never), "stale_never": len(stale_never),
            "failed": len(failed), "other": other, "pct": pct, "note": note}

_ELAPSED: dict = {}

def snapshot(full: bool = False) -> dict:
    from bili_digest.quality import check_index as ci

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = {}

    try:
        from bili_digest.quality import canary as _can
        _can.ensure(con)
        _r = _can.check(con, None)
        rows["金丝雀"] = ("OK" if _r["ok"] else "FAIL",
                          "四步全过" if _r["ok"] else "有环节不通",
                          _r["note"])
    except Exception as e:
        rows["金丝雀"] = ("WARN", "-", "%s: %s" % (type(e).__name__, str(e)[:40]))

    try:
        import numpy as _np
        from bili_digest.paths import VEC as _VEC
        from bili_digest.retrieve import rag_core as _rc
        _vi = _rc.VectorIndex(str(_VEC), readonly=True)
        _probe = _np.random.RandomState(20261112).randn(_vi.dim).astype(_np.float32)
        _got = [b for b in _vi.search(_probe, 16) if b != 0]
        _uniq = len(set(_got))
        _ok = _uniq >= 2
        rows["向量可遍历"] = (
            "OK" if _ok else "FAIL",
            "%d 个不同块" % _uniq,
            "" if _ok else
            "搜索仅返回 %r —— 图不可遍历或 keys 读取通道失效，跑一次 index --rebuild" % (_got[:4],),
        )
    except Exception as e:
        rows["向量可遍历"] = ("WARN", "未测",
                             "%s: %s" % (type(e).__name__, str(e)[:40]))

    _ci_r = None
    _T0 = __import__("time").perf_counter()
    try:
        _ci_r = ci.check(verbose=False)

        _ELAPSED["check_index"] = round(
            (__import__("time").perf_counter() - _T0) * 1000, 1)
        r = _ci_r
        rows["索引一致性"] = (
            "OK" if r["consistent"] else "FAIL",
            "库 %d / 向量 %d / FTS %s" % (r["blocks"], r["vec"], r["fts"]),
            "" if r["consistent"] else
            "缺 %d · 孤儿 %d · 陈旧 %d" % (r["missing"], r["orphan"], r["stale"]),
        )
    except Exception as e:
        rows["索引一致性"] = ("FAIL", "-", "%s: %s" % (type(e).__name__, str(e)[:50]))

    try:
        from bili_digest.schema.core_schema import info_len as _il, MIN_INFO_CHARS as _MIC

        from bili_digest.retrieve import rag_core as _rg
        _excluded = set(getattr(_rg, "_EXCLUDED", set()) or ())
        _KINDS = tuple(k for k in
                       ("subtitle_seg", "subtitle", "comment", "danmaku", "annotation")
                       if k not in _excluded)

        _ph = ",".join("?" * len(_KINDS)) or "NULL"

        _have = {}
        for _r in con.execute(
                "SELECT cb.kind, COUNT(*) AS n FROM content_block cb "
                "JOIN block_fts f ON f.rowid = cb.id "
                "WHERE cb.kind IN (%s) GROUP BY cb.kind" % _ph, _KINDS):
            _have[_r["kind"]] = _r["n"]

        _miss = {}
        _n_no_fts = 0
        for _r in con.execute(
                "SELECT cb.kind, cb.text FROM content_block cb "
                "WHERE cb.kind IN (%s) " % _ph +
                "AND NOT EXISTS (SELECT 1 FROM block_fts f WHERE f.rowid = cb.id)",
                _KINDS):
            _n_no_fts += 1
            if _il(_r["text"] or "") >= _MIC:
                _miss[_r["kind"]] = _miss.get(_r["kind"], 0) + 1

        _gaps = []
        for _k, _n in _miss.items():

            _should = _have.get(_k, 0) + _n
            if _n > max(3, int(_should * 0.005)):
                _gaps.append("%s 缺 %d/%d" % (_k, _n, _should))
        _n_have = sum(_have.values())
        _n_miss = sum(_miss.values())
        rows["FTS 覆盖"] = ("OK" if not _gaps else "FAIL",
                           "已进 %d 块 · 缺口 %d 块 · 未进 %d 块（其中 info_len<%d 的不该进）"
                           % (_n_have, _n_miss, _n_no_fts, _MIC),
                           " · ".join(_gaps))
    except Exception as e:
        rows["FTS 覆盖"] = ("WARN", "-", "%s: %s" % (type(e).__name__, str(e)[:40]))

    try:

        n_stale = len((_ci_r if _ci_r is not None else ci.check(verbose=False)).get("stale") or [])
        rows["陈旧向量"] = ("OK" if not n_stale else "FAIL",
                        "%d 条文本已变未重编" % n_stale, "")
    except Exception:
        rows["陈旧向量"] = ("WARN", "未测", "check_index 不可用")

    try:
        base = _q(con, "SELECT v FROM meta_kv WHERE k='kw_block_total'")
        real = _q(con, "SELECT COUNT(*) FROM content_block WHERE kind IN "
                       "('subtitle','subtitle_seg','danmaku','comment','meta')")
        if base is None:
            rows["关键词基线"] = ("WARN", "未记录",
                                  "跑 python -m bili_digest.retrieve.keywords --build")
        else:

            gap = int(real) - int(base)
            tol = max(50, int(real) * 0.002)
            if abs(gap) <= tol:
                rows["关键词基线"] = ("OK", "基线 %s / 实际 %d（差 %+d，容差 %d）"
                                      % (base, real, gap, tol), "")
            else:
                rows["关键词基线"] = ("WARN", "基线 %s / 实际 %d" % (base, real),
                                      "差 %+d 块 > 容差 %d · 跑 retrieve.keywords --build"
                                      % (gap, tol))
    except Exception as e:
        rows["关键词基线"] = ("WARN", "-", str(e)[:50])

    try:
        n = _q(con, """SELECT COUNT(*) FROM item i WHERE
                       EXISTS(SELECT 1 FROM content_block cb
                              WHERE cb.item_uid=i.uid AND cb.kind='subtitle')
                       AND NOT EXISTS(SELECT 1 FROM content_block cb
                              WHERE cb.item_uid=i.uid AND cb.kind='subtitle_seg')""")
        rows["切段完整性"] = ("OK" if not n else "WARN", "%d 条缺段落" % (n or 0), "")
    except Exception as e:
        rows["切段完整性"] = ("WARN", "-", str(e)[:50])

    try:
        n = _q(con, """SELECT COUNT(*) FROM item i JOIN lifecycle l ON l.item_uid=i.uid
                       WHERE l.tier='knowledge' AND NOT EXISTS(
                         SELECT 1 FROM content_block cb
                         WHERE cb.item_uid=i.uid AND cb.kind='summary')""")
        rows["提炼覆盖"] = ("OK" if not n else "WARN", "%d 条 knowledge 档缺提炼" % (n or 0), "")
    except Exception as e:
        rows["提炼覆盖"] = ("WARN", "-", str(e)[:50])

    try:
        _st = shell_stats(con)

        if not _st["empty"]:
            rows["空壳"] = ("OK", "0 条", "")
        elif not _st.get("recent_never"):
            rows["空壳"] = ("OK", "无待办（%d 条历史沉积，按需抓）" % _st["empty"],
                            _st["note"])
        else:
            rows["空壳"] = ("WARN", "待办 %d 条（空壳共 %d，%.1f%%）"
                            % (_st["recent_never"], _st["empty"], _st["pct"]),
                            _st["note"])
    except Exception as e:
        rows["空壳"] = ("WARN", "-", str(e)[:50])

    try:
        from bili_digest.retrieve.temporal import now_sec as _now
        n = _q(con, """
            SELECT COUNT(*) FROM job_log j
            WHERE j.status='fail' AND j.ts > ?
              AND NOT EXISTS(SELECT 1 FROM job_log k
                             WHERE k.scope=j.scope
                               AND k.status IN ('ok','skip') AND k.ts > j.ts)
            """, (_now() - 7 * 86400,))
        rows["抓取失败"] = ("OK" if not n else "WARN",
                            "%d 条近期未修复的失败" % (n or 0), "")
    except Exception as e:
        rows["抓取失败"] = ("WARN", "-", str(e)[:50])

    try:
        from bili_digest.retrieve import rag_core as _rc
        _vi = _rc.VectorIndex(str(paths.VEC), readonly=True)
        _sz, _cap = _vi.size, int(getattr(_vi.idx, "capacity", 0) or 0)
        _free = _cap - _sz
        if not _cap:
            rows["索引余量"] = ("WARN", "capacity 读不到（%d/%d）" % (_sz, _cap),
                                "索引文件可能损坏，跑 kb index --rebuild")
        else:
            rows["索引余量"] = ("OK", "剩 %d 位（%d/%d，不足时自动扩）" % (_free, _sz, _cap), "")
    except Exception as e:
        rows["索引余量"] = ("WARN", "-", str(e)[:40])

    try:
        import os
        exists = os.path.exists(str(paths.LATEON_CACHE))
        rows["重排缓存"] = ("OK", "已建" if exists else "未建（可选）", "")
    except Exception:
        rows["重排缓存"] = ("WARN", "-", "")

    try:
        import re as _re2
        _BV = _re2.compile(r"BV[0-9A-Za-z]{10}")
        _have = {r[0] for r in con.execute(
            "SELECT item_id FROM item WHERE platform='bilibili' AND item_id IS NOT NULL")}
        _tier1 = set()
        for _uid, _txt in con.execute(
                "SELECT item_uid, text FROM content_block WHERE kind='meta' AND text LIKE '%BV%'"):
            for _bv in _BV.findall(_txt or ""):
                if _bv not in _have:
                    _tier1.add(_bv)
        _n = len(_tier1)

        rows["缺口线索"] = ("WARN" if _n > 200 else "OK",
                            "第 1 层 %d 个候选（UP 主动引用、库里没有）" % _n,
                            "要看清单：kb clues --tier 1" if _n else "")
    except Exception as _e2:
        rows["缺口线索"] = ("WARN", "-", str(_e2)[:40])

    try:
        import json as _j
        row = con.execute("SELECT ts, detail FROM job_log "
                          "WHERE step='probe_subtitle' ORDER BY ts DESC LIMIT 1"
                          ).fetchone()
        if not row:
            rows["字幕新鲜度"] = ("WARN", "从未探测",
                                  "跑 python -m bili_digest.quality.probe_subtitle"
                                  "（约 3 分钟，857 条）")
        else:
            from bili_digest.retrieve.temporal import now_sec
            _ts, _d = int(row[0]), str(row[1] or "")
            _days = (now_sec() - _ts) / 86400.0
            try:
                _info = _j.loads(_d) if _d.startswith("{") else {}
            except ValueError:
                _info = {}
            _hit = _info.get("hit", "?")
            rows["字幕新鲜度"] = (
                "OK" if _days < 90 else "WARN",
                "上次探测 %.0f 天前 · 860 条里命中 %s" % (_days, _hit),
                "" if _days < 90 else
                "超过 90 天，建议重扫（AI 字幕是发布后异步生成的，判定会过期）")
    except Exception:
        pass

    try:
        from bili_digest.schema.types import provenance_counts as _pc
        _c = _pc(con)
        _tot = sum(v["blocks"] for v in _c.values()) or 1

        _sub = _q(con, "SELECT COUNT(*) FROM content_block WHERE kind='subtitle'") or 0
        _searchable = max(1, _tot - int(_sub))
        _prim = 0
        _prim += _q(con, "SELECT COUNT(*) FROM content_block WHERE kind='subtitle_seg'") or 0
        _prim += _q(con, "SELECT COUNT(*) FROM content_block WHERE kind='body'") or 0
        _rate = _prim / _searchable
        rows["内容层密度"] = (
            "OK" if _rate >= 0.05 else "WARN",
            "primary %d / 可搜 %d（%.1f%%）" % (_prim, _searchable, 100 * _rate),
            "观众反应占 %.0f%% · 检索时若不过滤，top-10 里正文期望不到 1 条"
            % (100 * (_c.get("reaction", {}).get("blocks", 0)) / _searchable))
    except Exception as _e4:
        rows["内容层密度"] = ("WARN", "-", str(_e4)[:40])

    try:
        _df = {r[0]: r[1] for r in con.execute(
            "SELECT topic, COUNT(*) FROM topic_member GROUP BY topic")}
        _per = {}
        for r in con.execute("SELECT item_uid, topic FROM topic_member"):
            _per.setdefault(r[0], []).append(_df.get(r[1], 1))
        _tot2 = len(_per)
        _bad2 = sum(1 for v in _per.values() if v and min(v) >= 50)
        _rate = _bad2 / _tot2 if _tot2 else 0
        rows["主题具体度"] = (
            "OK" if _rate < 0.02 else "WARN",
            "%d/%d 篇全是泛词（%.1f%%）" % (_bad2, _tot2, 100 * _rate),
            "目标 <2% · 根治要改提炼 prompt（要求至少 2 个词 df 小），重排序无用")
    except Exception as _e3:
        rows["主题具体度"] = ("WARN", "-", str(_e3)[:40])

    try:
        _tot = con.execute("SELECT COUNT(*) FROM item WHERE platform='bilibili'").fetchone()[0]

        _n_view = con.execute(
            "SELECT COUNT(*) FROM item WHERE platform='bilibili' AND ("
            "  json_extract(extras,'$.view') IS NOT NULL"
            "  OR json_extract(extras,'$.play') IS NOT NULL)").fetchone()[0]
        _n_tag = con.execute("SELECT COUNT(DISTINCT item_uid) FROM tag").fetchone()[0]
        _n_tx = con.execute(
            "SELECT COUNT(DISTINCT item_uid) FROM content_block "
            "WHERE kind IN ('subtitle','subtitle_seg')").fetchone()[0]
        _r_view = _n_view / _tot if _tot else 0
        _r_tag = _n_tag / _tot if _tot else 0
        _r_tx = _n_tx / _tot if _tot else 0
        _bad = []
        if _r_view < 0.9: _bad.append("播放量 %d/%d=%.0f%%" % (_n_view, _tot, 100*_r_view))
        if _r_tag < 0.9: _bad.append("标签 %d/%d=%.0f%%" % (_n_tag, _tot, 100*_r_tag))
        _msg = "播放量 %.0f%% · 标签 %.0f%% · 正文 %.0f%%" % (100*_r_view, 100*_r_tag, 100*_r_tx)
        if _bad:
            rows["表示一致性"] = ("WARN", " · ".join(_bad), _msg)
        else:
            rows["表示一致性"] = ("OK", _msg, "")
    except Exception as _e:
        rows["表示一致性"] = ("WARN", "-", str(_e)[:40])

    try:
        _hrows = con.execute(
            "SELECT step, status, COUNT(*) FROM job_log "
            "WHERE step LIKE 'health:%' GROUP BY step, status").fetchall()
        _agg = {}
        for _step, _status, _n in _hrows:
            _d = _agg.setdefault(_step, {"runs": 0, "bad": 0})
            _d["runs"] += _n
            if (_status or "").lower() not in ("ok", "pass", "✓"):
                _d["bad"] += _n
        _silent = [k.split(":", 1)[1] for k, d in _agg.items() if d["runs"] >= 5 and d["bad"] == 0]
        _stuck = [k.split(":", 1)[1] for k, d in _agg.items() if d["runs"] >= 5 and d["bad"] == d["runs"]]
        _live = len([k for k, d in _agg.items() if d["runs"] >= 5]) - len(_silent) - len(_stuck)
        _detail = "永远OK %d · 永远WARN %d · 有变化 %d" % (len(_silent), len(_stuck), _live)
        if _silent:
            _detail += "（永远OK: %s）" % "、".join(_silent[:3])
        if _stuck:
            _detail += "（永远WARN: %s）" % "、".join(_stuck[:2])

        rows["检查存活"] = ("OK" if not (_silent or _stuck) else "WARN", _detail,
                          "永远OK 可能是判据写宽或读不到；永远WARN 可能是判据永远为真")
    except Exception as _e:
        rows["检查存活"] = ("WARN", "-", "%s: %s" % (type(_e).__name__, str(_e)[:36]))

    con.close()

    if full:
        rows.update(_full_checks())
    return rows

def _full_checks() -> dict:
    out = {}
    try:
        import importlib
        miss = [m for m in ("fastembed", "usearch", "jieba", "numpy")
                if importlib.util.find_spec(m) is None]
        out["核心依赖"] = ("OK" if not miss else "FAIL",
                        "齐全" if not miss else "缺 " + ", ".join(miss), "")
    except Exception as e:
        out["核心依赖"] = ("WARN", "-", str(e)[:50])

    try:
        from bili_digest.ingest import fetcher
        cred = fetcher.load_credential()
        if not cred.get("sessdata"):
            out["B站登录态"] = ("WARN", "缺 SESSDATA", "未配置登录态")
        else:
            code = None
            try:
                j = fetcher.http_get("https://api.bilibili.com/x/web-interface/nav",
                                     cred.get("cookie") or cred["sessdata"])
                code = (j or {}).get("code")
            except Exception:
                code = None
            if code == 0:
                out["B站登录态"] = ("OK", "nav code=0", "凭证有效（已实际验证）")
            elif code == -101:
                out["B站登录态"] = ("FAIL", "nav code=-101",
                                    "凭证失效或格式错（有值但不可用）")
            else:
                out["B站登录态"] = ("WARN", "nav code=%s" % code,
                                    "凭证非空但无法验证（网络/接口异常，不判凭证错）")
    except Exception as e:
        out["B站登录态"] = ("WARN", "未配置", str(e)[:40])
    return out

def record(con, rows: dict) -> None:
    try:
        from bili_digest.retrieve.temporal import now_sec
        import json as _j
        ts = now_sec()
        for name, (st, val, note) in rows.items():
            con.execute(
                "INSERT INTO job_log(scope, step, status, detail, ts, actor, seconds) "
                "VALUES(?,?,?,?,?,?,?)",
                ("health", "health:" + name,
                 "fail" if st == "FAIL" else ("warn" if st == "WARN" else "ok"),
                 _j.dumps({"v": str(val)[:120], "n": str(note)[:120]},
                          ensure_ascii=False), ts, "health",
                 _ELAPSED.get("check_index") if name == "索引一致性" else None))
        con.commit()
    except Exception:
        pass

MIN_RUNS = 5

def history(con, days: int = 30) -> list:
    from bili_digest.retrieve.temporal import now_sec
    cut = now_sec() - days * 86400
    rows = list(con.execute(
        "SELECT step, status, COUNT(*) AS n FROM job_log "
        "WHERE scope='health' AND ts > ? GROUP BY step, status", (cut,)))
    agg = {}
    for r in rows:
        name = (r["step"] or "").replace("health:", "")
        agg.setdefault(name, {"ok": 0, "warn": 0, "fail": 0})[r["status"]] = r["n"]
    out = []
    for name, c in sorted(agg.items()):
        total = c["ok"] + c["warn"] + c["fail"]
        bad = c["warn"] + c["fail"]
        if total == 0:
            continue
        ratio = bad / total

        if total < MIN_RUNS:
            out.append({"check": name, "runs": total, "warn": c["warn"],
                        "fail": c["fail"], "bad_ratio": round(ratio, 2),
                        "hint": "样本不足（%d 次，至少 %d 次才判）" % (total, MIN_RUNS)})
            continue
        if ratio >= 0.8:
            hint = "阈值可能错了（几乎一直红）"
        elif bad == 0:
            hint = "从没红过（可能太松）"
        elif 0.2 <= ratio <= 0.8:
            hint = "红绿反复（判据可能不稳）"
        else:
            hint = ""
        out.append({"check": name, "runs": total, "warn": c["warn"],
                    "fail": c["fail"], "bad_ratio": round(ratio, 2), "hint": hint})
    return out

def ages(con, days: int = 60) -> dict:
    from bili_digest.retrieve.temporal import now_sec
    now = now_sec()
    cut = now - days * 86400
    out = {}
    try:
        rs = list(con.execute(
            "SELECT step, status, ts FROM job_log WHERE scope='health' AND ts > ? "
            "ORDER BY ts DESC", (cut,)))
    except Exception:
        return out

    seen = {}
    for step, st, ts in rs:
        name = step.replace("health:", "", 1)
        if name in seen:
            continue
        seen[name] = st
    for name, st in seen.items():
        if st == "ok":
            continue
        first = None
        for step, st2, ts in rs:
            if step != "health:" + name:
                continue
            if st2 == "ok":
                break
            first = ts
        if first:
            out[name] = (now - first) / 86400.0
    return out

def render(rows: dict, ages_map: dict | None = None) -> list:
    ages_map = ages_map or {}
    STALE_DAYS = 7.0
    lines = ["", "  ── 库健康 ─────────────────────────────", ""]

    order = ["金丝雀", "向量可遍历", "索引一致性", "陈旧向量", "关键词基线", "切段完整性", "提炼覆盖",
             "空壳", "抓取失败", "索引余量", "重排缓存", "表示一致性", "主题具体度",
             "内容层密度", "字幕新鲜度", "核心依赖", "B站登录态", "缺口线索"]
    for k in order:
        if k not in rows:
            continue
        st, val, note = rows[k]
        mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}.get(st, "?")

        age = ages_map.get(k)
        if st == "WARN" and age is not None and age >= STALE_DAYS:
            mark = "✗"
            st = "FAIL"
            note = (note + " " if note else "") + "（WARN 已挂 %.0f 天，超 %.0f 天阈值）" % (
                age, STALE_DAYS)
            rows[k] = ("FAIL", val, note)
        elif age is not None and age >= 1:
            note = (note + " " if note else "") + "（挂 %.0f 天）" % age
        lines.append("  [%s] %-10s %-28s %s" % (mark, k, val, note))
    bad = [k for k, (st, _, _) in rows.items() if st == "FAIL"]
    warn = [k for k, (st, _, _) in rows.items() if st == "WARN"]
    lines.append("")
    if bad:
        lines.append("  ✗ %d 项失败：%s" % (len(bad), "、".join(bad)))
    elif warn:
        lines.append("  ✓ 无失败 · %d 项需留意：%s" % (len(warn), "、".join(warn)))
    else:
        lines.append("  ✓ 全部正常")
    return lines

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="库健康总览：一致性 / 新鲜度 / 覆盖 / 失败记录")
    ap.add_argument("--full", action="store_true", help="加依赖自检与登录态（会出网）")
    a = ap.parse_args()
    rows = snapshot(full=a.full)
    for ln in render(rows):
        print(ln)
    bad = [k for k, (st, _, _) in rows.items() if st == "FAIL"]
    warn = [k for k, (st, _, _) in rows.items() if st == "WARN"]
    return 2 if bad else (1 if warn else 0)

if __name__ == "__main__":
    sys.exit(main())
