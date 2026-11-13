#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from bili_digest import paths
from bili_digest.schema.core_schema import now_sec

DEFAULT_MODEL = "small"
DEFAULT_MAX_SEC = 1800
DEFAULT_THREADS = 8
EST_SPEED = 3.0

def available() -> dict:
    import importlib.util
    import shutil
    return {
        "yt_dlp": importlib.util.find_spec("yt_dlp") is not None,
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }

def install_hint(av: dict | None = None) -> str:
    av = av or available()
    missing = [k for k, ok in av.items() if not ok and k != "ffmpeg"]
    if not missing:
        return ""
    pkgs = " ".join({"yt_dlp": "yt-dlp", "faster_whisper": "faster-whisper"}[m]
                    for m in missing)
    return (f"缺依赖 {'、'.join(missing)}，装一下：\n"
            f"  python -m pip install -i https://repo.huaweicloud.com/repository/pypi/simple {pkgs}"
            f"\n  （清华镜像返回 403，用华为云/腾讯云/官方 pypi）")

def estimate(seconds: float, speed: float = EST_SPEED) -> dict:
    s = max(float(seconds or 0), 0.0)
    return {"audio_sec": round(s, 1), "est_sec": round(s / max(speed, 0.1), 1),
            "est_min": round(s / max(speed, 0.1) / 60.0, 2),
            "speed": speed}

def audio_file(item_id: str) -> Path:
    return paths.ASR_AUDIO_DIR / f"{item_id}.m4a"

def download_audio(item_id: str, url: str | None = None,
                   force: bool = False) -> dict:
    dst = audio_file(item_id)
    if dst.exists() and not force:
        return {"path": dst, "seconds": 0.0, "reused": True,
                "bytes": dst.stat().st_size}
    import yt_dlp
    paths.ASR_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    url = url or paths.item_url(paths.PLATFORM, item_id)
    opt = {
        "format": "bestaudio/best",
        "outtmpl": str(paths.ASR_AUDIO_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 2,
    }
    t0 = time.time()
    with yt_dlp.YoutubeDL(opt) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
    if not p.exists():
        raise RuntimeError(f"yt-dlp 报告成功但文件不存在：{p}")
    return {"path": p, "seconds": time.time() - t0, "reused": False,
            "bytes": p.stat().st_size,
            "title": (info or {}).get("title") or ""}

def clip_audio(src: Path, max_sec: float | None) -> dict:
    tag = "full" if not max_sec else f"{int(max_sec)}s"
    out = src.with_name(f"{src.stem}.{tag}.16k.wav")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
    if max_sec:
        cmd += ["-t", f"{float(max_sec):.3f}"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 裁剪失败：{(r.stderr or '').strip()[:300]}")
    return {"path": out, "seconds": time.time() - t0,
            "bytes": out.stat().st_size if out.exists() else 0,
            "clipped": bool(max_sec)}

def load_wav(path: Path):
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        ch, sw, sr, n = (w.getnchannels(), w.getsampwidth(),
                         w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    if (sr, ch, sw) != (16000, 1, 2):
        raise RuntimeError(
            f"{path} 不是 16k 单声道 16bit wav（实际 sr={sr} ch={ch} sw={sw}）"
            f" —— 先过 clip_audio")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

SENSEVOICE_DIR = paths.MODELS_DIR / "sensevoice"
VAD_MODEL = paths.MODELS_DIR / "silero" / "silero_vad.onnx"

VAD_MIN_SILENCE = 0.30
VAD_MERGE_GAP = 0.30

VAD_MAX_SEG = 12.0
VAD_WINDOW = 512

VAD_PAD = 0.15

_SV_CACHE: dict = {}

def _sensevoice_ready() -> bool:
    return ((SENSEVOICE_DIR / "model.int8.onnx").exists()
            and (SENSEVOICE_DIR / "tokens.txt").exists()
            and VAD_MODEL.exists())

def _load_sensevoice(threads: int):
    key = ("sv", max(1, int(threads)))
    if key in _SV_CACHE:
        return _SV_CACHE[key]
    import sherpa_onnx
    t0 = time.time()
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SENSEVOICE_DIR / "model.int8.onnx"),
        tokens=str(SENSEVOICE_DIR / "tokens.txt"),
        num_threads=max(1, int(threads)), use_itn=True, debug=False,
        language="zh")
    _SV_CACHE[key] = (rec, time.time() - t0)
    return _SV_CACHE[key]

def _vad_segments(audio, sr: int, threads: int) -> list[tuple]:
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = VAD_MIN_SILENCE
    cfg.silero_vad.min_speech_duration = 0.2
    cfg.silero_vad.max_speech_duration = 25.0
    cfg.sample_rate = sr
    cfg.num_threads = max(1, int(threads))
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)

    raw = []
    for i in range(0, len(audio), VAD_WINDOW):
        vad.accept_waveform(audio[i:i + VAD_WINDOW])
        while not vad.empty():
            seg = vad.front
            raw.append((seg.start / sr, len(seg.samples) / sr))
            vad.pop()
    vad.flush()
    while not vad.empty():
        seg = vad.front
        raw.append((seg.start / sr, len(seg.samples) / sr))
        vad.pop()

    merged = []
    for st, d in raw:
        if (merged and st - (merged[-1][0] + merged[-1][1]) < VAD_MERGE_GAP
                and merged[-1][1] + d <= VAD_MAX_SEG):
            merged[-1] = (merged[-1][0], st + d - merged[-1][0])
        else:
            merged.append((st, d))
    out = []
    for st, d in merged:
        while d > VAD_MAX_SEG:
            out.append((st, VAD_MAX_SEG))
            st, d = st + VAD_MAX_SEG, d - VAD_MAX_SEG
        if d > 0:
            out.append((st, d))

    total = float(len(audio)) / sr if len(audio) else 0.0
    padded = []
    for i, (st, d) in enumerate(out):
        lo = max(0.0, st - VAD_PAD)
        hi = min(total, st + d + VAD_PAD) if total else st + d + VAD_PAD
        if i > 0:
            prev_end = out[i - 1][0] + out[i - 1][1]
            mid = (prev_end + st) / 2.0
            lo = max(lo, min(mid, st))
        padded.append((lo, hi - lo))
    return padded

def _transcribe_sensevoice(path: Path, language: str = "zh",
                           threads: int = DEFAULT_THREADS, log=None) -> dict:
    if not _sensevoice_ready():
        raise RuntimeError(
            "SenseVoice 模型不全：需要 %s/model.int8.onnx、tokens.txt 与 %s"
            % (SENSEVOICE_DIR, VAD_MODEL)
            + chr(10) + "  下载见 SenseVoice 实测文档（hf-mirror，约 228 MB + 1.8 MB）")
    rec, load_sec = _load_sensevoice(threads)
    audio = load_wav(path)
    sr = 16000
    dur = float(len(audio)) / sr if len(audio) else 0.0

    t1 = time.time()
    bounds = _vad_segments(audio, sr, threads)
    segs, last = [], t1
    for st, d in bounds:
        i0, i1 = int(st * sr), int((st + d) * sr)
        if i1 <= i0:
            continue
        stream = rec.create_stream()
        stream.accept_waveform(sr, audio[i0:i1])
        rec.decode_stream(stream)
        text = (stream.result.text or "").strip()
        if not text:
            continue
        segs.append({"offset_sec": float(st), "end": float(st + d), "text": text})
        if log and time.time() - last > 10:
            last = time.time()
            log(f"      … 已转 {st + d:.0f}s / {dur:.0f}s")
    elapsed = time.time() - t1
    return {
        "segments": segs,
        "duration": dur,
        "load_sec": load_sec,
        "elapsed": elapsed,
        "speed": (dur / elapsed) if elapsed > 0 and dur else 0.0,
        "language": language or "zh",

        "lang_prob": 0.0,
    }

def transcribe(path: Path, model_size: str = DEFAULT_MODEL,
               language: str = "zh", threads: int = DEFAULT_THREADS,
               log=None, hotwords: str | None = None,
               engine: str | None = None) -> dict:
    os.environ.setdefault("HF_ENDPOINT", paths.HF_ENDPOINT)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    if engine is None:
        engine = os.environ.get("KB_ASR_ENGINE") or "sensevoice"
    if engine == "sensevoice":
        return _transcribe_sensevoice(path, language=language,
                                      threads=threads, log=log)
    if engine != "whisper":
        raise ValueError("engine 只支持 whisper / sensevoice，收到 %r" % (engine,))

    from faster_whisper import WhisperModel

    t0 = time.time()
    paths.ASR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    m = WhisperModel(model_size, device="cpu", compute_type="int8",
                     download_root=str(paths.ASR_MODEL_DIR),
                     cpu_threads=max(1, int(threads)))
    load_sec = time.time() - t0

    audio = load_wav(path)
    t1 = time.time()
    seg_iter, info = m.transcribe(
        audio, language=(language or None),
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5, condition_on_previous_text=False,
        initial_prompt="以下是普通话的句子。" if (language or "").startswith("zh") else None,

        hotwords=hotwords or None,
    )
    segs, last = [], t1
    for s in seg_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        segs.append({"offset_sec": float(s.start), "end": float(s.end), "text": text})
        if log and time.time() - last > 10:
            last = time.time()
            log(f"      … 已转 {s.end:.0f}s / {info.duration:.0f}s")
    elapsed = time.time() - t1
    dur = float(getattr(info, "duration", 0) or 0)
    return {
        "segments": segs,
        "duration": dur,
        "load_sec": load_sec,
        "elapsed": elapsed,
        "speed": (dur / elapsed) if elapsed > 0 and dur else 0.0,
        "language": getattr(info, "language", None),
        "lang_prob": float(getattr(info, "language_probability", 0) or 0),
    }

def _drop_asr_blocks(con, item_uid: str) -> int:
    from bili_digest.distill.seg_boundary import _delete_blocks
    return _delete_blocks(
        con, "item_uid=? AND json_extract(COALESCE(extras,'{}'),'$.producer')='asr'",
        (item_uid,))

TIER_CN = {"Q": "命中问题", "P": "有观众反应", "M": "只有标题",
           "S": "短片（实测多无语音）"}

SHORT_SEC = 120

def queue(con, limit: int = 10, max_sec: float | None = DEFAULT_MAX_SEC,
          min_overlap: int = 2) -> dict:
    from bili_digest.quality import agenda as ag

    rows = candidates(con, limit=100000, lo=1, hi=10 ** 9)
    uids = [r["uid"] for r in rows]
    if not uids:
        return {"items": [], "n": 0, "total_min": 0.0, "questions": 0,
                "matched": 0, "max_sec": max_sec, "over_clip": 0}

    real_dur, n_dm, n_cm = {}, {}, {}
    for i in range(0, len(uids), 400):
        part = uids[i:i + 400]
        ph = ",".join("?" * len(part))
        for uid, mx in con.execute(
                "SELECT item_uid, MAX(offset_sec) FROM content_block "
                "WHERE item_uid IN (%s) GROUP BY item_uid" % ph, part):
            real_dur[uid] = mx or 0
        for uid, kind, n in con.execute(
                "SELECT item_uid, kind, COUNT(*) FROM content_block "
                "WHERE item_uid IN (%s) AND kind IN ('danmaku','comment') "
                "GROUP BY item_uid, kind" % ph, part):
            (n_dm if kind == "danmaku" else n_cm)[uid] = n

    try:
        matches = ag.match_content(con, uids, min_overlap=min_overlap)
    except Exception:

        matches = []
    hit = {}
    for m in matches:
        hit.setdefault(m["item_uid"], []).append(m)

    items = []
    for r in rows:
        uid = r["uid"]
        dur = float(real_dur.get(uid) or 0)
        if dur < float(r["duration_sec"] or 0):
            dur = float(r["duration_sec"] or 0)
        eff = min(dur, max_sec) if max_sec else dur
        qs = hit.get(uid, [])
        has_aud = bool(n_dm.get(uid) or n_cm.get(uid))
        tier = "Q" if qs else ("P" if has_aud else "M")
        if tier != "Q" and dur < SHORT_SEC:
            tier = "S"
        items.append({
            "uid": uid, "title": r["title"] or "", "duration_sec": int(dur),
            "eff_sec": int(eff), "clipped": bool(max_sec and dur > max_sec),
            "est_min": estimate(eff)["est_min"], "tier": tier,
            "tier_cn": TIER_CN[tier], "n_q": len(qs),
            "hits": [(x["qid"], x["question"], x["overlap"][:3]) for x in qs][:3],
            "n_danmaku": int(n_dm.get(uid, 0)), "n_comment": int(n_cm.get(uid, 0)),
        })

    rank = {"Q": 0, "P": 1, "M": 2, "S": 3}
    items.sort(key=lambda x: (rank[x["tier"]], -x["n_q"], x["est_min"], x["uid"]))
    top = items[:max(1, int(limit))]
    return {
        "items": top,
        "n": len(items),
        "total_min": round(sum(x["est_min"] for x in top), 1),
        "total_all_min": round(sum(x["est_min"] for x in items), 1),

        "audio_sec": sum(x["eff_sec"] for x in items),
        "audio_sec_raw": sum(x["duration_sec"] for x in items),
        "questions": len(ag.open_questions(con)),
        "matched": len({m["item_uid"] for m in matches}),
        "max_sec": max_sec,
        "over_clip": sum(1 for x in items if x["clipped"]),
    }

def index_backlog(con) -> int:
    try:
        from bili_digest.paths import VEC
        from bili_digest.retrieve import rag_core as rc
        from bili_digest.schema.core_schema import MIN_INFO_CHARS, info_len
        have = rc.load_index_keys(VEC) or set()
        n = 0
        for r in con.execute("SELECT id, text FROM content_block"):
            if r[0] in have:
                continue
            if info_len(r[1]) >= MIN_INFO_CHARS:
                n += 1
        return n
    except Exception:
        return 0

def changed_ids(old: dict, ids: list, texts: list) -> list:
    return [i for i, t in zip(ids, texts) if old.get(i) != t]

def reindex_blocks(ids: list[int], log=None) -> int:
    if not ids:
        return 0
    try:
        from bili_digest.paths import VEC
        from bili_digest.retrieve import rag_core as rc
        have = rc.load_index_keys(VEC)
        if not have:
            return 0
        hit = [i for i in ids if i in have]
        if not hit:
            return 0
        vi = rc.VectorIndex(VEC)
        vi.remove(hit)
        vi.save()
        rc.save_index_keys(VEC, have - set(hit))
        return len(hit)
    except Exception as e:
        if log:
            log(f"      重编码准备失败（{type(e).__name__}），向量可能停在旧文本上")
        return 0

def write_blocks(con, item_uid: str, paras: list[dict], meta: dict) -> dict:
    from bili_digest.schema.core_schema import add_block
    n = chars = 0
    ids, texts = [], []
    for i, p in enumerate(paras):
        text = (p.get("text") or "").strip()
        if not text:
            continue
        bid = add_block(
            con, item_uid, "subtitle_seg", text,
            offset_sec=round(float(p.get("start") or 0), 2), seq=i,
            extras={"producer": "asr", **meta})
        if bid:
            n += 1
            chars += len(text)
            ids.append(bid)
            texts.append(text)
    con.commit()
    return {"blocks": n, "chars": chars, "paragraphs": len(paras),
            "ids": ids, "texts": texts}

def has_real_subtitle(con, item_uid: str) -> bool:
    r = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE item_uid=? "
        "AND kind IN ('subtitle','subtitle_seg') "
        "AND json_extract(COALESCE(extras,'{}'),'$.producer') IS NOT 'asr'",
        (item_uid,)).fetchone()
    return bool(r and r[0])

def asr_item(con, item_uid: str, *, model: str = DEFAULT_MODEL,
             max_sec: float | None = DEFAULT_MAX_SEC, language: str = "zh",
             force: bool = False, keep_audio: bool = False,
             audio: str | None = None, index: bool = True,
             threads: int = DEFAULT_THREADS, use_hotwords: bool = False,
             log=print) -> dict:
    out = {"uid": item_uid, "ok": False}
    item = con.execute("SELECT item_id, title, duration_sec FROM item WHERE uid=?",
                       (item_uid,)).fetchone()
    if not item:
        out["error"] = f"库里没有这条：{item_uid}"
        return out
    item_id, title, dur = item[0], (item[1] or ""), (item[2] or 0)
    out["title"] = title
    out["duration_sec"] = dur

    if has_real_subtitle(con, item_uid) and not force:
        out["error"] = "这条已经有真字幕了，不需要 ASR（--force 可强制覆盖）"
        return out

    from bili_digest.quality import budget as _bg
    lease = None
    try:
        lease = _bg.acquire(con, actor="asr", step="asr", scope=item_uid)
    except _bg.BudgetStop as e:
        out["error"] = (
            "并发额度已满，本次不转：%s" % str(e).splitlines()[0]
            + chr(10) + "  看谁在跑：kb budget（明细里有 running 行）")
        return out

    av = available()
    if not av["ffmpeg"]:
        out["error"] = "缺 ffmpeg（PATH 里找不到）"
        return out
    if not audio and not av["yt_dlp"]:
        out["error"] = install_hint(av)
        return out

    try:
        from bili_digest.quality import kb_cov
        out["before"] = kb_cov.of_item(con, item_uid)["level"]
    except Exception:
        out["before"] = None

    if audio:
        src = Path(audio)
        if not src.exists():
            out["error"] = f"--audio 指向的文件不存在：{src}"
            return out
        out["download"] = {"path": src, "seconds": 0.0, "reused": True}
    else:
        log(f"  [1/4] 下音频 {item_id} …")
        d = download_audio(item_id)
        out["download"] = d
        log(f"        {d['bytes'] / 1048576:.1f} MB · {d['seconds']:.1f}s"
            + ("（复用已有）" if d.get("reused") else ""))

    log(f"  [2/4] 裁到前 {int(max_sec)}s 并转 16k wav" if max_sec
        else "  [2/4] 转 16k wav（不裁）")
    c = clip_audio(Path(out["download"]["path"]), max_sec)
    out["clip"] = c

    hw = comment_hotwords(con, item_uid) if use_hotwords else None
    if hw and hw["hot"]:
        log(f"  [3/4] 转写 {model}/int8/{threads}线程 · 评论热词 "
            f"{len(hw['latin'])}拉丁+{len(hw['zh'])}中文："
            f"{hw['hot'][:80]}")
        out["hotwords"] = hw
    else:
        log(f"  [3/4] 转写 {model}/int8/{threads}线程 …")
    tr = transcribe(c["path"], model_size=model, language=language,
                    threads=threads, log=log,
                    hotwords=(hw or {}).get("hot") or None)
    out["transcribe"] = {k: v for k, v in tr.items() if k != "segments"}
    log(f"        {tr['duration']:.0f}s 音频 / {tr['elapsed']:.1f}s 转写 "
        f"（{tr['speed']:.1f}x 实时，模型加载 {tr['load_sec']:.1f}s）")

    if not tr["segments"]:
        _bg.release(con, lease, "skip", detail="无语音段")
        mark_empty(con, item_uid, model)
        out["error"] = ("转写没有产出任何语音段（纯音乐/无人声）—— 已记入 "
                        "item.extras.asr_empty_at，队列不再推荐它")
        if not keep_audio:
            _cleanup(c["path"], out)
        return out

    from bili_digest.distill import seg_boundary as sb
    paras = sb.segment_item(tr["segments"], vecs=None)
    old = {r[0]: r[1] for r in con.execute(
        "SELECT id, text FROM content_block WHERE item_uid=? "
        "AND json_extract(COALESCE(extras,'{}'),'$.producer')='asr'", (item_uid,))}
    dropped = _drop_asr_blocks(con, item_uid) if old else 0
    meta = {"model": model, "lang": tr.get("language"),
            "audio_sec": round(tr["duration"], 1),

            "speed": round(tr["speed"], 2), "at": now_sec()}
    w = write_blocks(con, item_uid, paras, meta)
    out["segments"] = len(tr["segments"])
    out["write"] = {**w, "replaced": dropped}
    if hw and hw["latin"]:
        rec = term_recall(hw["latin"], "".join(x["text"] for x in tr["segments"]))
        out["selfcheck"] = rec
        log(f"        自检：评论里的 %d 个拉丁专有名词召回 %d 个"
            f"（未召回：{'、'.join(rec['miss'][:6]) or '无'}）" % (rec["n"], rec["hit"]))

    if index and w["blocks"]:
        try:
            stale = changed_ids(old, w.get("ids") or [], w.get("texts") or [])
            if stale:
                log(f"        {len(stale)} 块文本变了（id 被复用），从索引剔除后重编码")
                reindex_blocks(stale, log=log)
            elif old:
                log("        文本与上次一致，跳过重编码（向量仍然正确）")
            backlog = index_backlog(con)
            if backlog > max(50, 10 * w["blocks"]):
                log(f"        库里另有 {backlog} 块等着索引 —— build_vec 是全库增量"
                    f"入口，会一并补上，可能几分钟（赶时间：--no-index）")
            from bili_digest.tools import build_kb_index as bi
            r = bi.build_vec(con, verbose=False, rebuild=False)
            out["index"] = r
            log(f"  [4/4] 增量索引 {r.get('indexed', 0)} 条 {r.get('seconds', 0):.1f}s")
        except Exception as e:
            out["index_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            log(f"  [4/4] 索引失败：{out['index_error']}（块已写入，可手动 kb index）")
    else:
        log("  [4/4] 跳过索引")

    if not keep_audio:
        _cleanup(c["path"], out)

    try:
        from bili_digest.quality import kb_cov
        out["after"] = kb_cov.of_item(con, item_uid)["level"]
        out["asr_hint"] = kb_cov.of_item(con, item_uid).get("asr_hint")
    except Exception:
        out["after"] = None

    try:
        from bili_digest.quality import budget as _bg
        _bg.record(con, actor="asr", step="asr",
                   status="ok" if w["blocks"] else "skip",
                   rows=w["blocks"], seconds=round(tr["elapsed"], 1),
                   reversible=True, scope=item_uid,
                   detail=f"segments={len(tr['segments'])} blocks={w['blocks']}")
    except Exception:
        pass
    out["ok"] = bool(w["blocks"])
    _bg.release(con, lease, "ok" if out["ok"] else "skip",
                detail="blocks=%d" % w["blocks"],
                rows=w["blocks"], seconds=round(tr["elapsed"], 1))
    return out

STOP_LATIN = {
    "the", "a", "an", "of", "to", "is", "are", "and", "or", "in", "on", "for",
    "ok", "okay", "yes", "no", "lol", "gg", "wc", "up", "id", "app", "www",
    "com", "http", "https", "bilibili", "bv",
}

def comment_hotwords(con, item_uid: str, max_latin: int = 20,
                     max_zh: int = 8) -> dict:
    import re
    from collections import Counter
    from bili_digest.quality import agenda as ag
    from bili_digest.schema.core_schema import _tokenizer

    texts = [r[0] for r in con.execute(
        "SELECT text FROM content_block WHERE item_uid=? "
        "AND kind IN ('comment','danmaku')", (item_uid,))]
    if not texts:
        return {"hot": "", "latin": [], "zh": [], "n_texts": 0}

    lat = Counter()
    for t in texts:
        for m in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{0,19}", t or ""):
            m = m.rstrip(".-").lower()
            if len(m) >= 2 and m not in STOP_LATIN:
                lat[m] += 1
    latin = [w for w, n in lat.most_common() if n >= 2][:max_latin]

    df = {r[0]: r[1] for r in con.execute("SELECT term, df FROM keyword")}
    generic = set(ag._STOP) | {w for w, n in df.items() if n > 100}
    zhc = Counter()
    for t in texts:
        for w in _tokenizer().lcut(t or ""):
            if 2 <= len(w) <= 6 and w not in generic:
                zhc[w] += 1
    zh = [w for w, n in zhc.most_common()
          if n >= 2 and 3 <= df.get(w, 0) <= 100][:max_zh]

    return {"hot": " ".join(latin + zh), "latin": latin, "zh": zh,
            "n_texts": len(texts)}

def term_recall(latin: list, text: str) -> dict:
    low = (text or "").lower()
    hit = [w for w in latin if w in low]
    return {"hit": len(hit), "n": len(latin), "terms": hit,
            "miss": [w for w in latin if w not in low]}

def mark_empty(con, item_uid: str, model: str) -> None:
    try:
        con.execute(
            "UPDATE item SET extras = json_set(COALESCE(extras,'{}'), "
            "'$.asr_empty_at', ?, '$.asr_empty_model', ?) WHERE uid=?",
            (now_sec(), model, item_uid))
        con.commit()
    except Exception:
        pass

def _cleanup(wav: Path, out: dict) -> None:
    try:
        if wav.exists():
            wav.unlink()
            out["audio_removed"] = str(wav)
    except OSError:
        pass

def candidates(con, limit: int = 30, lo: int = 1, hi: int = 1800) -> list[dict]:
    rows = con.execute("""
        SELECT i.uid, i.item_id, i.title, i.duration_sec, COUNT(b.id) n
        FROM item i JOIN content_block b ON b.item_uid = i.uid
        WHERE NOT EXISTS (SELECT 1 FROM content_block s WHERE s.item_uid = i.uid
                          AND s.kind IN ('subtitle','subtitle_seg')
                          AND json_extract(COALESCE(s.extras,'{}'),'$.producer') IS NOT 'asr')
          -- 试过但无语音的条目直接排除：实测 9 条里 6 条是这样，
          -- 不排除的话队列每轮都会把同一批空条目重新推上来。
          AND json_extract(COALESCE(i.extras,'{}'),'$.asr_empty_at') IS NULL
        GROUP BY i.uid
        HAVING n > 0 AND i.duration_sec BETWEEN ? AND ?
        ORDER BY i.duration_sec
        LIMIT ?""", (lo, hi, limit)).fetchall()
    return [{"uid": r[0], "item_id": r[1], "title": r[2],
             "duration_sec": r[3], "n_blocks": r[4]} for r in rows]
