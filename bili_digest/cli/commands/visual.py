#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest.cli.helpers import db_of, _as_uid

def cmd_visual(a) -> None:
    from bili_digest.ingest import visual_desc as V

    con = db_of(a)
    try:
        av = V.available()
        deps_ok = all(av.get(k) for k in ("torch", "transformers", "soundfile",
                                          "scipy", "pillow", "torchvision"))

        if a.queue:
            q = V.candidates(con, limit=a.limit, include_low_value=a.all)
            if not q:
                print("  没有可补的空壳视频")
                return
            print("  画面补充候选 · %d 条（零字幕零提炼零评论）" % len(q))
            print("  排序：教程/工具标题优先 + 时长短优先；已滤掉音乐/影视/氛围类"
                  "（--all 可包含）")
            print()
            print("   优先  条目                                  时长   标题")
            for i, x in enumerate(q, 1):
                label = (x["uid"].split(":")[-1] + " " + x["title"])[:38]
                print("   %2d    %-38s %5ds  %s" % (
                    i, label, x["duration_sec"], x["title"][:30]))
            print()
            print("  单条实测约 %.0fs（模型常驻后）；全 %d 条约 %.1f 小时机时" % (
                V.EST_SEC_PER_ITEM, len(q), len(q) * V.EST_SEC_PER_ITEM / 3600))

            if a.dry:
                print()
                print("  依赖检查（--dry）：")
                for k, label in (("torch", "torch"), ("transformers", "transformers"),
                                 ("soundfile", "soundfile"), ("scipy", "scipy"),
                                 ("pillow", "Pillow"), ("torchvision", "torchvision"),
                                 ("yt_dlp", "yt-dlp"), ("ffmpeg", "ffmpeg")):
                    print("    %-14s %s" % (label, "OK" if av.get(k) else "缺"))
                print("    %-14s %s（%.1f GB）" % ("权重", "OK" if av.get("weights") else "缺",
                                                  av.get("weight_bytes", 0) / 1e9))
                print("    权重目录：%s" % av.get("model_dir"))
                if not deps_ok or not av.get("weights"):
                    print("    装依赖：\n" + V.install_hint(av))
            print()
            print("  下一步：kb visual <uid>（先补一条看质量）"
                  + ("；--dry 已显示依赖，去掉它即真跑" if a.dry else ""))
            return

        if a.list:
            rows = V.candidates(con, limit=a.limit or 50, include_low_value=a.all)
            for r in rows:
                print("  %-22s %5ds  %s" % (
                    r["uid"], r["duration_sec"], r["title"][:44]))
            print("  用法：kb visual BVxxxxxxxxxx")
            return

        if not a.uid:
            print("  给一个条目：kb visual BVxxxxxxxxxx   （--queue 看候选）")
            return

        uid = _as_uid(a.uid)
        row = con.execute("SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
        if not row:
            print("  库里没有这条：%s" % uid)
            return
        title = row[0] or ""
        low_val = V.low_value_title(title)
        print("  %s  %s" % (uid, title[:44]))
        if low_val and not a.all:
            print("  [!] 标题像音乐/影视/氛围类，画面补充价值低；"
                  "确实要补加 --all")

        if a.dry:
            print("    依赖：torch %s / transformers %s / 权重 %s / yt-dlp %s / ffmpeg %s" % (
                "OK" if av["torch"] else "缺",
                "OK" if av["transformers"] else "缺",
                "OK" if av["weights"] else "缺",
                "OK" if av["yt_dlp"] else "缺",
                "OK" if av["ffmpeg"] else "缺"))
            print("    权重目录：%s（%.1f GB）" % (
                av["model_dir"], av["weight_bytes"] / 1e9))
            print("    本条预估 %.0fs（%d 帧 / %d token），视频临时抓取转完即删" % (
                V.EST_SEC_PER_ITEM, a.frames or V.DEFAULT_N_FRAMES,
                a.max_tokens or V.DEFAULT_MAX_TOKENS))
            if not deps_ok or not av["weights"]:
                print("    装依赖：\n" + V.install_hint(av))
            return

        if not deps_ok or not av["weights"]:
            print("  依赖不全：\n" + V.install_hint(av))
            return

        print("    开始生成（约 %.0fs，模型加载另 %.0fs）..." % (
            V.EST_SEC_PER_ITEM, 21))
        from bili_digest.ingest.visual_desc import WemmEngine, model_dir
        eng = WemmEngine(model_dir(), max_tokens=a.max_tokens or V.DEFAULT_MAX_TOKENS)
        print("    模型载入 %.1fs" % eng.load_sec)
        r = V.visual_item(con, uid, engine=eng, video=a.video,
                          n_frames=a.frames or V.DEFAULT_N_FRAMES,
                          force=a.force, index=not a.no_index)
        if not r.get("ok"):
            print("  [x] %s" % r.get("error"))
            return
        print("  [ok] %d 字 → visual_desc 块 #%s · %.1fs / %d 帧" % (
            r["chars"], r["block_id"], r["elapsed"], r["n_frames"]))
        if r.get("indexed"):
            print("       增量索引 +%d 条" % r["indexed"])
        print("  " + "─" * 56)
        for line in r["text"].splitlines():
            print("  " + line)
        print("  " + "─" * 56)
        if not a.no_index:
            print("  现在可以直接：kb search <关键词>")
    finally:
        con.close()
