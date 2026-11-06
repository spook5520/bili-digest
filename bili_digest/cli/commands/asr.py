#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of, _as_uid

def cmd_asr(a) -> None:
    from bili_digest.ingest import asr as A
    from bili_digest.quality import kb_cov
    con = db_of(a)
    try:
        av = A.available()

        if a.queue:
            max_sec = None if a.all else (a.max_minutes or 30) * 60
            q = A.queue(con, limit=a.limit, max_sec=max_sec)
            if not q["items"]:
                print("  没有可转的条目（有内容但无字幕的都已经转过了）")
                return
            print("  ASR 队列 · 候选 %d 条 · 开放问题 %d 条（命中 %d 条）" % (
                q["n"], q["questions"], q["matched"]))
            print("  排序：命中问题 > 有观众反应 > 只有标题 > 短片(<120s，实测多无语音)；"
                  "同档内按时长升序")
            print()
            print("   优先  条目                              限后/原始  预估    档  依据")
            for i, x in enumerate(q["items"], 1):
                if x["hits"]:
                    _qid, _q, words = x["hits"][0]
                    why = "#%d %s" % (_qid, "、".join(words))
                elif x["tier"] == "P":
                    why = "弹幕%d/评论%d" % (x["n_danmaku"], x["n_comment"])
                else:
                    why = "无观众反应"
                label = (x["uid"].split(":")[-1] + " " + x["title"])[:34]

                span = "%dm%02ds" % (x["eff_sec"] // 60, x["eff_sec"] % 60)
                if x["clipped"]:
                    span += "/%dm" % (x["duration_sec"] // 60)
                    span = span.ljust(10) + "*"
                print("   %2d   %-34s %-10s %6.1fm  %s  %s" % (
                    i, label, span, x["est_min"], x["tier"], why))
            print()
            if any(x["clipped"] for x in q["items"]):
                print("  * 整条超过 %.0f 分钟：默认只转前 %.0f 分钟（预估按 30 分钟算）；"
                      "整条转 kb asr <uid> --all" % (a.max_minutes, a.max_minutes))
            print("  前 %d 条合计预估 %.1f 分钟机时（按 2.6x 实时、限长后的音频算）" % (
                len(q["items"]), q["total_min"]))
            print("  全部 %d 条：音频 %.1f 小时（限长前 %.1f 小时）→ 转写约 %.1f 小时机时"
                  % (q["n"], q["audio_sec"] / 3600.0, q["audio_sec_raw"] / 3600.0,
                     q["total_all_min"] / 60.0))
            print("  这就是「批量跑一遍」的代价 —— 所以只排队不批量")
            if q["over_clip"]:
                print("  [!] 候选里有 %d 条超过 %.0f 分钟：默认只转前 %.0f 分钟；"
                      "整条转用 --all" % (q["over_clip"], a.max_minutes, a.max_minutes))
            print()
            print("  建议先做这三条（判断依据跟着每条走）：")
            for i, x in enumerate(q["items"][:3], 1):
                parts = []
                if x["hits"]:
                    _qid, _q, words = x["hits"][0]
                    parts.append("命中你登记的问题 #%d（%s）" % (_qid, "、".join(words)))
                if x["tier"] == "P":
                    parts.append("有观众反应（弹幕 %d / 评论 %d），不是无人看的空条"
                                 % (x["n_danmaku"], x["n_comment"]))
                elif x["tier"] == "M":
                    parts.append("只有标题，转完信息量最少")
                parts.append("限后 %ds，约 %.1f 分钟" % (x["eff_sec"], x["est_min"]))
                print("    %d. %s" % (i, x["uid"]))
                print("       %s" % "；".join(parts))
            print("  依据：问题相关性用 agenda 的关键词重叠（泛词按 keyword.df>100 滤掉），"
                  "档位看有无弹幕/评论，成本按 时长/2.6 折算。")
            if q["questions"] < 5:
                print("  注：问题池只有 %d 条，「命中问题」这一档的区分度就取决于你登记了多少；"
                      "登记得少，队列会退化成「按时长升序」。" % q["questions"])
            print("  下一步：kb asr %s（先转第一条），或 kb question --add 登记问题再跑 --queue"
                  % (q["items"][0]["uid"].split(":")[-1],))
            return

        if a.list:
            rows = A.candidates(con, limit=a.limit,
                                hi=int((a.max_minutes or 30) * 60))
            if not rows:
                print("  没有符合条件的条目（有内容但无字幕、时长在范围内）")
                return
            print("  有内容但无字幕，按时长升序（便宜的先转）：")
            for r in rows:
                print("    %-42s %5ds  %s" % (
                    r["uid"], r["duration_sec"] or 0, (r["title"] or "")[:40]))
            print("  以上只列 ≤%.0f 分钟的条目（限时长默认值）；更长的用 "
                  "--max-minutes 放宽，或直接 asr <uid> --all 整条转" % (
                      a.max_minutes or 30))
            print("  用法：kb asr BVxxxxxxxxxx")
            return

        if not a.uid:
            print("  给一个条目：kb asr BVxxxxxxxxxx   （--list 看候选）")
            return

        uid = _as_uid(a.uid)
        row = con.execute("SELECT duration_sec FROM item WHERE uid=?",
                          (uid,)).fetchone()
        if not row:
            print("  库里没有这条：%s" % uid)
            return

        dur = row[0] or 0
        mx = con.execute("SELECT MAX(offset_sec) FROM content_block WHERE item_uid=?",
                         (uid,)).fetchone()[0]
        if mx and mx > dur:
            dur = mx
        max_sec = None if a.all else (a.max_minutes or 30) * 60
        est = A.estimate(min(dur, max_sec) if max_sec else dur)

        dep = av["ffmpeg"] and (a.audio or av["yt_dlp"])
        if a.dry:
            print("  %s" % uid)
            print("    时长 %ds（按 %.0f 分钟/条折算）· 本次将转 %.0fs" % (
                dur, dur / 60.0, est["audio_sec"]))
            print("    估转写 %.1f 分钟（%.1fx 实时，仅供对比，真值看实跑）" % (
                est["est_min"], est["speed"]))
            if max_sec and dur > max_sec:
                print("    [!] 超过 %.0f 分钟，默认只转前 %.0f 分钟；整条转用 --all" % (
                    a.max_minutes, a.max_minutes))
            print("    依赖：ffmpeg %s / yt-dlp %s / faster-whisper %s" % (
                "OK" if av["ffmpeg"] else "缺",
                "OK" if av["yt_dlp"] else "缺",
                "OK" if av["faster_whisper"] else "缺"))
            print("    模型 %s（缓存在 %s）" % (a.model, A.paths.ASR_MODEL_DIR))
            return

        hint = A.install_hint(av)
        if hint or not dep:
            print("  " + (hint or "缺 ffmpeg"))
            return

        cov = kb_cov.of_item(con, uid)
        print("  %s" % uid)
        print("    覆盖 %s（%s）" % (cov["level"], cov["gap"]))
        if max_sec and dur > max_sec:
            print("    [!] 时长 %ds 超过 %.0f 分钟 —— 只转前 %.0f 分钟"
                  "（整条转：--all）" % (dur, a.max_minutes, a.max_minutes))

        r = A.asr_item(con, uid, model=a.model, max_sec=max_sec,
                       language=a.language, force=a.force,
                       keep_audio=a.keep_audio, audio=a.audio,
                       index=not a.no_index, threads=a.threads,
                       use_hotwords=a.hotwords)
        if not r.get("ok"):
            print("  [x] %s" % r.get("error"))
            return

        tr = r["transcribe"]
        print("  [ok] %d 段语音 → %d 个段落块 · %d 字" % (
            r["segments"], r["write"]["blocks"], r["write"]["chars"]))
        print("       %ds 音频 / %.1fs 转写 = %.2fx 实时（单条耗时 %.1f 分钟）" % (
            tr["duration"], tr["elapsed"], tr["speed"],
            (r["clip"]["seconds"] + tr["load_sec"] + tr["elapsed"]) / 60.0))
        print("       覆盖 %s → %s" % (r.get("before"), r.get("after")))
        sc = r.get("selfcheck")
        if sc:
            print("       自检：评论里 %d 个拉丁专有名词召回 %d 个%s" % (
                sc["n"], sc["hit"],
                ("（未召回：%s）" % "、".join(sc["miss"][:6])) if sc["miss"] else ""))
        if r.get("before") in ("partial", "meta_only"):
            print("       注：该条的摘要/要点是基于「无字幕」素材提炼的，已过时；"
                  "要更新跑 kb derive --uid %s --force" % uid)
        for b in con.execute(
                "SELECT offset_sec, substr(text,1,54) FROM content_block "
                "WHERE item_uid=? AND kind='subtitle_seg' "
                "AND json_extract(COALESCE(extras,'{}'),'$.producer')='asr' "
                "ORDER BY offset_sec LIMIT 3", (uid,)):
            print("       [%05.1fs] %s" % (b[0] or 0, b[1]))
        if not a.no_index:
            print("       现在可以直接：kb search <关键词>")
    finally:
        con.close()
