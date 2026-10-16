#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from paths import enable_utf8_stdout

enable_utf8_stdout()

HERE = Path(__file__).parent

from core_schema import connect, now_sec
from paths import DB, bili_uid

def allowed_talkers() -> set:
    import os
    raw = (os.environ.get("KB_INBOX_ALLOW") or "").strip()
    if not raw:

        try:
            from bili_digest import _env_file_value
            raw = (_env_file_value("KB_INBOX_ALLOW") or "").strip()
        except Exception:
            pass
    return {x.strip() for x in raw.split(",") if x.strip()}

RESEARCH_CUES = (
    "需要", "帮我找", "帮我查", "帮我搜集", "搜集", "调研", "汇总", "综述",
    "总结一下", "整理一下", "给我讲讲", "讲讲", "了解一下", "研究",
)
RESEARCH_MIN_LEN = 6

def is_research_intent(q: str) -> bool:
    s = (q or "").strip()
    if len(s) < RESEARCH_MIN_LEN:
        return False
    if "http" in s.lower() or "BV" in s or "bilibili.com" in s.lower():
        return False
    return any(c in s for c in RESEARCH_CUES)

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

AV_RE = re.compile(r"\bav(\d{6,})\b", re.I)

REPLY_LIMIT = 900

def ensure_table(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS inbox_log (
        msg_id  TEXT PRIMARY KEY,   -- 私信 msg_key / @ 通知 id
        kind    TEXT NOT NULL,      -- dm | at
        talker  TEXT,               -- 发送者 uid
        bvid    TEXT,               -- 解析到的视频
        status  TEXT,               -- ok | no_bvid | failed | dry
        reply   TEXT,               -- 实际（或将要）回复的内容
        ts      INTEGER
    );
    """)
    con.commit()

DONE_STATUS = ("ok", "dry", "no_bvid")

def already_done(con, msg_id: str) -> bool:

    ph = ",".join("?" * len(DONE_STATUS))
    return con.execute(
        f"SELECT 1 FROM inbox_log WHERE msg_id=? AND status IN ({ph})",
        (msg_id, *DONE_STATUS)).fetchone() is not None

def record(con, msg_id, kind, talker, bvid, status, reply) -> None:
    con.execute("INSERT OR REPLACE INTO inbox_log"
                "(msg_id,kind,talker,bvid,status,reply,ts) VALUES(?,?,?,?,?,?,?)",
                (msg_id, kind, talker, bvid, status, (reply or "")[:4000],
                 now_sec()))
    con.commit()

def parse_bvid(*texts) -> str | None:
    for t in texts:
        if not t:
            continue
        m = BV_RE.search(str(t))
        if m:
            return m.group(1)
    return None

def parse_video_ref(*texts) -> tuple:
    for t in texts:
        if not t:
            continue
        m = BV_RE.search(str(t))
        if m:
            return "bv", m.group(1)
        m = AV_RE.search(str(t))
        if m:
            return "av", m.group(1)
    return None, None

def av_to_bvid(cred: dict, aid: str, th) -> str | None:
    import bili_shelf as bs
    s = str(aid or "").strip()
    if not s.isdigit():
        print(f"    ! av{s} 不是纯数字")
        return None
    if len(s) < 6:
        print(f"    ! av{s} 只有 {len(s)} 位 —— 旧式 av 号至少 6 位，可能是复制时截断了")
        return None
    if 11 <= len(s) <= 14:
        print(f"    ! av{s} 是 {len(s)} 位 —— 新式 av 号是 15 位、旧式是 6~10 位，"
              f"这个长度不属于任何一种，很可能【中间漏了几位】。"
              f"请回原页面重新复制完整链接。")
        return None
    d = bs.request_json(
        f"https://api.bilibili.com/x/web-interface/view?aid={s}", cred, throttle=th)
    if d.get("code") != 0:
        print(f"    ! av{s} 换 BV 失败 code={d.get('code')} {d.get('message')}"
              f"（位数正常，所以更可能是视频已被删除/设为私密，"
              f"或需要登录态）")
        return None
    return (d.get("data") or {}).get("bvid")

def dm_text(msg: dict) -> str:
    raw = msg.get("content") or ""
    try:
        c = json.loads(raw)
    except Exception:
        return str(raw)
    if not isinstance(c, dict):
        return str(raw)
    parts = [str(c.get("content") or "")]
    for k in ("title", "bvid", "url", "jump_url", "desc"):
        if c.get(k):
            parts.append(str(c[k]))
    return " ".join(p for p in parts if p)

def fetch_dms(cred: dict, th, only_unread: bool = True) -> list:
    import bili_shelf as bs
    me = str(cred.get("dedeuserid") or "")
    out = []
    d = bs.request_json(
        "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
        "?session_type=1&group_fold=1&unfollow_fold=0&sort_rule=2&build=0&mobi_app=web",
        cred, throttle=th)
    if d.get("code") != 0:
        print(f"  ! 会话列表拉取失败 code={d.get('code')} {d.get('message')}")
        return out
    for s in (d.get("data") or {}).get("session_list") or []:
        if s.get("session_type") != 1:
            continue
        if only_unread and not s.get("unread_count"):
            continue
        talker = s.get("talker_id")
        d2 = bs.request_json(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"
            f"?talker_id={talker}&session_type=1&size=20", cred, throttle=th)
        if d2.get("code") != 0:
            continue
        for m in (d2.get("data") or {}).get("messages") or []:
            if str(m.get("sender_uid")) == me:
                continue
            out.append({"msg_id": str(m.get("msg_key")), "kind": "dm",
                        "talker": str(talker), "text": dm_text(m)})
    return out

def fetch_ats(cred: dict, th) -> list:
    import bili_shelf as bs
    out = []
    d = bs.request_json("https://api.bilibili.com/x/msgfeed/at?build=0&mobi_app=web",
                        cred, throttle=th)
    if d.get("code") != 0:
        print(f"  ! @ 通知拉取失败 code={d.get('code')} {d.get('message')}")
        return out
    for it in (d.get("data") or {}).get("items") or []:
        item = it.get("item") or {}

        src = str(item.get("source_id") or "").strip()
        root = str(item.get("root_id") or "").strip()
        rpid = src or str(it.get("id") or "")
        out.append({
            "msg_id": "at:%s" % it.get("id"),
            "kind": "at",
            "talker": str((it.get("user") or {}).get("mid") or ""),

            "text": " ".join(str(item.get(k) or "")
                             for k in ("source_content", "title", "uri")),
            "oid": str(item.get("subject_id") or ""),
            "rpid": rpid,
            "root": root or rpid,
        })
    return out

def compose_reply(title: str, d: dict) -> str:
    title = (title or "总结").strip()

    lines = [title if title.startswith("【") else "【%s】" % title]
    if d.get("summary"):
        lines += ["", str(d["summary"]).strip()]

    def _pt(k):
        return (k.get("point") if isinstance(k, dict) else str(k)) or ""

    kps = [t for t in (_pt(k) for k in (d.get("key_points") or [])) if t][:6]
    if kps:
        lines += ["", "要点："] + ["· %s" % k for k in kps]
    ins = [i.get("claim") for i in (d.get("insights") or []) if i.get("claim")]
    if ins:
        lines += ["", "洞察：", "· %s" % ins[0]]

    if len(lines) > 1:
        lines.append("")
    lines.append("— bili-digest 自动总结")
    return "\n".join(lines)[:REPLY_LIMIT]

def _reply(cred: dict, m: dict, text: str, dry: bool, th) -> bool:
    if m["kind"] == "dm":
        return send_dm(cred, m["talker"], text, dry, th)
    return reply_at(cred, m, text, dry, th)

def summarize_existing(con, bvid: str) -> tuple:
    import extract as ex
    uid = bili_uid(bvid)
    d = ex.get_derived(con, uid) or {}
    if not d.get("summary"):
        return None, "库里这条还没有提炼结果（可以发它的链接让我抓取提炼）"
    row = con.execute("SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
    return compose_reply(row["title"] if row else bvid, d), ""

def summarize(con, bvid: str, th, verbose: bool = True) -> tuple:
    import refine as rf
    if verbose:
        print(f"    抓取 {bvid} …")
    ok, msg = rf.step_fetch(bvid, sources="meta,sub,dm,reply")
    if not ok:
        return None, f"抓取失败：{str(msg)[:80]}"
    r = rf.step_import(con, bvid)
    if r.get("error"):
        return None, f"入库失败：{r['error']}"
    uid = bili_uid(bvid)
    rf.step_judge(con, uid)
    rf.step_extract(con, uid)

    rf.step_index(verbose=False)
    import extract as ex
    d = ex.get_derived(con, uid) or {}
    if not d.get("summary"):
        return None, "素材不足（可能没有字幕/评论），无法总结"
    row = con.execute("SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
    return compose_reply(row["title"] if row else bvid, d), ""

def ensure_pending_table(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS inbox_pending (
        talker TEXT PRIMARY KEY,
        query  TEXT,
        items  TEXT,
        ts     INTEGER
    );
    """)
    con.commit()

def save_pending(con, talker: str, query: str, items: list) -> None:
    ensure_pending_table(con)
    con.execute("INSERT OR REPLACE INTO inbox_pending(talker,query,items,ts)"
                " VALUES(?,?,?,?)",
                (talker, query, json.dumps(items, ensure_ascii=False), now_sec()))
    con.commit()

def load_pending(con, talker: str) -> tuple:
    ensure_pending_table(con)
    r = con.execute("SELECT query, items, ts FROM inbox_pending WHERE talker=?",
                    (talker,)).fetchone()
    if not r:
        return None, []
    import time as _t
    if now_sec() - (r["ts"] or 0) > 900:
        return None, []
    try:
        return r["query"], list(json.loads(r["items"]) or [])
    except Exception:
        return None, []

def search_local(con, query: str, top_k: int) -> list:
    import rag_core as rc
    import retrieve as rv
    from paths import VEC
    vi = rc.VectorIndex(str(VEC))
    return rv.retrieve(con, vi, query, granularity="item", top_k=top_k)

def search_web(query: str, top_k: int = 4) -> list:
    try:
        import discover
        rows = discover.do_search(query, page=1, pages=1)
    except Exception as e:
        print("    ! 站内搜索失败：%s: %s" % (type(e).__name__, str(e)[:60]))
        return []
    return [{"web": True, "bvid": r.get("item_id"), "title": r.get("title"),
             "url": r.get("url"), "author": r.get("author_name")}
            for r in rows[:top_k] if r.get("item_id")]

def search_reply(con, query: str, top_k: int = 4, local_min: int = 0) -> tuple:
    hits = search_local(con, query, top_k)
    items = [{"in_lib": True, "bvid": h.get("item_uid", "").split(":")[-1],
              "title": h.get("title"), "url": h.get("url"),
              "evidence": (h.get("evidence") or {}).get("text") or h.get("text") or ""}
             for h in hits]

    web = search_web(query, top_k)
    items += web

    if not items:
        return ("没找到相关内容，站内搜索也没有结果。\n\n"
                "换个说法再试（检索按字面+语义匹配，口语化的长句容易失配）。"), []

    lines = ["【%s】" % query.strip()[:40], ""]
    n = 0
    if any(i.get("in_lib") for i in items):
        lines.append("① 已经在库：")
        for i in items:
            if not i.get("in_lib"):
                continue
            n += 1
            lines.append("%d. %s" % (n, (i.get("title") or "")[:50]))
            ev = " ".join(str(i.get("evidence") or "").split())[:70]
            if ev:
                lines.append("   " + ev)
            lines.append("   " + str(i.get("url") or ""))
    if web:
        lines += ["", "② 站内搜到（尚未收录）："]
        for i in web:
            n += 1
            lines.append("%d. %s" % (n, (i.get("title") or "")[:50]))
            lines.append("   " + str(i.get("url") or ""))
    lines += ["",
              "回序号即可：库里那条直接给总结；站内那条我先抓取再提炼。",
              "（上面这步没花钱，选定后才用 LLM）"]

    if any(i.get("in_lib") for i in items):
        lines.append("※ 库内结果按相近度排序，只说明最接近，不代表相关。")
    lines.append("— bili-digest")
    return "\n".join(lines)[:REPLY_LIMIT], items
def send_dm(cred: dict, talker: str, text: str, dry: bool, th) -> bool:
    import bili_shelf as bs
    if dry:
        print("    [演练] 将回复私信：")
        for ln in text.splitlines():
            print("      " + ln)
        return True
    if not cred.get("bili_jct"):
        print("    ! 缺 bili_jct，无法发送")
        return False
    payload = {
        "msg[sender_uid]": str(cred.get("dedeuserid") or ""),
        "msg[receiver_id]": str(talker),
        "msg[receiver_type]": 1,
        "msg[msg_type]": 1,
        "msg[dev_id]": "00000000-0000-0000-0000-000000000000",
        "msg[timestamp]": now_sec(),
        "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
        "csrf": cred["bili_jct"],
    }
    d = bs.request_json("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
                        cred, method="POST", data=payload, throttle=th)
    ok = d.get("code") == 0
    print("    %s 私信回复 %s" % ("✓" if ok else "✗", d.get("message", "")))
    return ok

def reply_at(cred: dict, item: dict, text: str, dry: bool, th) -> bool:
    import bili_shelf as bs
    oid, rpid = item.get("oid"), item.get("rpid")
    if not (oid and rpid):
        print("    ! @ 通知里缺 oid/rpid，跳过回复")
        return False
    if dry:
        print("    [演练] 将在视频 %s 下回复 rpid=%s：" % (oid, rpid))
        for ln in text.splitlines():
            print("      " + ln)
        return True
    payload = {"oid": oid, "type": 1, "message": text,
               "root": item.get("root") or rpid,
               "parent": rpid,
               "csrf": cred["bili_jct"], "plat": 1}
    d = bs.request_json("https://api.bilibili.com/x/v2/reply/add", cred,
                        method="POST", data=payload, throttle=th)
    ok = d.get("code") == 0
    print("    %s 评论回复 %s" % ("✓" if ok else "✗", d.get("message", "")))
    return ok

def handle_once(con, cred: dict, dry: bool = True, verbose: bool = True,
                recheck: bool = False) -> dict:
    import bili_shelf as bs
    from refine import FetchThrottle

    th = bs.Throttle()
    fth = FetchThrottle()
    st = {"dm": 0, "at": 0, "ok": 0, "no_bvid": 0, "failed": 0,
          "skipped": 0, "denied": 0}

    msgs = []
    try:
        msgs += fetch_dms(cred, th)
    except Exception as e:
        print(f"  ! 私信拉取异常：{type(e).__name__}: {str(e)[:80]}")
    try:
        msgs += fetch_ats(cred, th)
    except Exception as e:
        print(f"  ! @ 通知拉取异常：{type(e).__name__}: {str(e)[:80]}")

    allowed = allowed_talkers()
    for m in msgs:
        st[m["kind"]] += 1
        if not recheck and already_done(con, m["msg_id"]):
            st["skipped"] += 1
            continue

        if m["talker"] not in allowed:
            record(con, m["msg_id"], m["kind"], m["talker"], None,
                   "denied", "")
            st["denied"] = st.get("denied", 0) + 1
            if verbose:
                print("  · 发送者 %s 不在白名单，忽略" % m["talker"])
            continue
        kind, ref = parse_video_ref(m.get("text"))
        if not ref:
            q = (m.get("text") or "").strip()
            if len(q) < 1:
                record(con, m["msg_id"], m["kind"], m["talker"], None,
                       "no_bvid", "")
                st["no_bvid"] += 1
                continue

            m_idx = re.fullmatch(r"\s*(\d{1,2})\s*", q)
            if m_idx:
                pq, pitems = load_pending(con, m["talker"])
                i = int(m_idx.group(1)) - 1
                if not pitems or not (0 <= i < len(pitems)):
                    text = ("没有可选的候选（或已过期）。\n\n"
                            "先发一句话让我检索，再回序号。")
                    sent = _reply(cred, m, text, dry, th)
                    record(con, m["msg_id"], m["kind"], m["talker"], None,
                           "dry" if dry else ("ok" if sent else "failed"), text)
                    st["ok"] += 1
                    continue
                pick = pitems[i]
                bvid = pick.get("bvid")
                if verbose:
                    print("  · 选定第 %d 条 → %s" % (i + 1, bvid))
                if pick.get("in_lib"):

                    text, err = summarize_existing(con, bvid)
                else:
                    fth.wait()
                    text, err = summarize(con, bvid, th, verbose=verbose)
                    fth.note(not err, err)
                if not text:
                    text = "这条没能处理：%s" % err
                sent = _reply(cred, m, text, dry, th)
                record(con, m["msg_id"], m["kind"], m["talker"], bvid,
                       "dry" if dry else ("ok" if sent else "failed"), text)
                st["ok"] += 1
                continue

            if is_research_intent(q):
                if verbose:
                    print("  · 研究式请求：%s" % q[:40])
                try:
                    import research as rs
                    r = rs.research(con, q, verbose=False)
                    if r.get("ok"):
                        rep = r["report"] or {}
                        lines = ["【%s】" % q[:36], ""]
                        ov = rep.get("overview")
                        if ov:
                            lines.append(str(ov))
                        for th in (rep.get("themes") or [])[:4]:
                            lines.append("")
                            lines.append("%s：" % th.get("name"))
                            lines.append("  %s" % str(th.get("note") or "")[:120])
                        gaps = rep.get("gaps") or []
                        if gaps:
                            lines.append("")
                            lines.append("还不知道：%s" % "；".join(str(g)[:40] for g in gaps[:3]))
                        col = r.get("collected") or {}
                        if col.get("done"):
                            lines.append("")
                            lines.append("（本次新收了 %d 条）" % len(col["done"]))
                        text = chr(10).join(lines)
                    else:
                        text = "没找到可用素材：%s" % r.get("note", "")
                except Exception as e:
                    text = "研究失败：%s: %s" % (type(e).__name__, str(e)[:60])
                sent = _reply(cred, m, text, dry, th)
                record(con, m["msg_id"], m["kind"], m["talker"], None,
                       "dry" if dry else ("ok" if sent else "failed"), text)
                st["ok"] += 1
                fth.wait()
                continue

            if verbose:
                print("  · 检索：%s" % q[:40])
            text, items = search_reply(con, q)
            if items:
                save_pending(con, m["talker"], q, items)
            sent = _reply(cred, m, text, dry, th)
            record(con, m["msg_id"], m["kind"], m["talker"], None,
                   "dry" if dry else ("ok" if sent else "failed"), text)
            st["ok"] += 1
            fth.wait()
            continue
        bvid = ref
        if kind == "av":
            bvid = av_to_bvid(cred, ref, th)
            if not bvid:
                record(con, m["msg_id"], m["kind"], m["talker"], None,
                       "failed", "av → BV 换算失败")
                st["failed"] += 1
                continue
            if verbose:
                print("  · av%s → %s" % (ref, bvid))
        if verbose:
            print("  · %s → %s" % (m["msg_id"], bvid))
        fth.wait()
        text, err = summarize(con, bvid, th, verbose=verbose)
        fth.note(not err, err)
        if not text:
            record(con, m["msg_id"], m["kind"], m["talker"], bvid,
                   "failed", err)
            st["failed"] += 1
            if verbose:
                print("    ✗ " + err)
            continue
        sent = (send_dm(cred, m["talker"], text, dry, th) if m["kind"] == "dm"
                else reply_at(cred, m, text, dry, th))
        record(con, m["msg_id"], m["kind"], m["talker"], bvid,
               "dry" if dry else ("ok" if sent else "failed"), text)
        st["ok"] += 1
        fth.wait()

    return st

def main() -> int:
    ap = argparse.ArgumentParser(description="私信 / @ 触发的自动总结")
    ap.add_argument("--once", action="store_true", help="处理一轮")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=int, default=90, help="轮询间隔秒（默认 90）")
    ap.add_argument("--send", action="store_true", help="真的发送（默认演练）")
    ap.add_argument("--check", action="store_true", help="只检查凭据与接口")
    ap.add_argument("--recheck", action="store_true",
                    help="忽略已处理记录，重扫所有未读（解析口径改动后用）")
    a = ap.parse_args()

    import bili_shelf as bs
    import bili_digest as bd
    cred = bd.load_credential()

    if a.check:
        ok_id = bool(cred.get("dedeuserid"))
        print(f"  凭据：sessdata {'有' if cred.get('sessdata') else '无'}"
              f" · bili_jct {'有' if cred.get('bili_jct') else '无（无法回复）'}"
              f" · DedeUserID {cred.get('dedeuserid') or '无'}")
        th = bs.Throttle()
        n = len(fetch_dms(cred, th, only_unread=False))
        m = len(fetch_ats(cred, th))
        print(f"  接口：私信会话 {n} 条 · @ 通知 {m} 条")
        return 0 if (cred.get("sessdata") and ok_id) else 1

    if not (a.once or a.loop):
        ap.print_help()
        return 0

    con = connect(DB)
    con.row_factory = sqlite3.Row
    ensure_table(con)
    dry = not a.send
    if dry:
        print("  （演练模式：只显示会回复什么，不实际发送。加 --send 真发）")

    while True:
        print(f"\n══ {time.strftime('%H:%M:%S')} 处理一轮 ══")
        try:
            st = handle_once(con, cred, dry=dry, recheck=a.recheck)
        except Exception as e:
            print(f"  ! 本轮异常：{type(e).__name__}: {str(e)[:120]}")
            st = {}
        if st:
            print("  私信 %d · @ %d → 回复 %d · 无链接 %d · 失败 %d · 已处理过 %d"
                  " · 白名单外 %d"
                  % (st["dm"], st["at"], st["ok"], st["no_bvid"],
                     st["failed"], st["skipped"], st.get("denied", 0)))
        if not a.loop:
            break
        time.sleep(max(30, a.interval))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
