# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from bili_digest import paths

DEFAULT_MODEL = "WeMM-Video-2B"
DEFAULT_N_FRAMES = 8
DEFAULT_MAX_TOKENS = 400
EST_SEC_PER_ITEM = 130
PROMPT = (
    "这是一个知识库视频的画面帧。请只提取对检索有用的信息："
    "①画面中出现的软件名、工具名、专有名词和界面文字；"
    "②正在进行的操作步骤（按顺序）；③视频主题。"
    "忽略人物穿着、颜色、背景等装饰性细节。用简洁的中文分条列出。"
)

_LOW_VALUE_RE = re.compile(
    r"歌单|歌词|翻唱|音乐|MV|完整版|催眠|睡觉听|白噪音|"
    r"电影版|解说|影视|短剧|先导|预告|混剪|"
    r"晚安|助眠|氛围|纯音乐|钢琴|吉他|Lo-fi|lofi"
)

def model_dir() -> Path:
    custom = os.environ.get("KB_WEMM_MODEL_DIR")
    if custom:
        return Path(custom).expanduser()
    return paths.MODELS_DIR / DEFAULT_MODEL

def available() -> dict:
    import importlib.util
    md = model_dir()
    shards = list(md.glob("model-*.safetensors")) if md.exists() else []
    return {
        "torch": bool(importlib.util.find_spec("torch")),
        "transformers": bool(importlib.util.find_spec("transformers")),
        "soundfile": bool(importlib.util.find_spec("soundfile")),
        "scipy": bool(importlib.util.find_spec("scipy")),
        "pillow": bool(importlib.util.find_spec("PIL")),
        "torchvision": bool(importlib.util.find_spec("torchvision")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "yt_dlp": bool(importlib.util.find_spec("yt_dlp")),
        "weights": len(shards) >= 1,
        "weight_bytes": sum(p.stat().st_size for p in shards),
        "model_dir": str(md),
    }

def install_hint(av: dict | None = None) -> str:
    av = av or available()
    missing = []
    for k, tip in (
        ("torch", "torch>=2.4（CPU 版：pip install torch --index-url https://download.pytorch.org/whl/cpu）"),
        ("transformers", "transformers>=5.2"),
        ("soundfile", "soundfile"),
        ("scipy", "scipy"),
        ("pillow", "pillow"),
        ("torchvision", "torchvision"),
    ):
        if not av.get(k):
            missing.append("  - " + tip)
    notes = []
    if missing:
        notes.append("  安装 Python 依赖：pip install \"bili-digest[visual]\"\n" + "\n".join(missing))
    if not av.get("weights"):
        notes.append(
            "  下载模型权重（约 6.5GB）：把 tencent/WeMM-Video-2B 的\n"
            "  两个 safetensors 分片与全部配置/自定义 .py 放到 %s\n"
            "  （HF: https://huggingface.co/tencent/WeMM-Video-2B，可设 KB_WEMM_MODEL_DIR 改路径）"
            % av.get("model_dir"))
    if not av.get("yt_dlp"):
        notes.append("  抓视频需要 yt-dlp：pip install yt-dlp（或本地已有视频用 --video 指定）")
    if not av.get("ffmpeg"):
        notes.append("  抽帧需要 ffmpeg（需在 PATH）")
    return "\n".join(notes)

def low_value_title(title: str | None) -> bool:
    return bool(_LOW_VALUE_RE.search(title or ""))

_READABLE_KINDS = ("subtitle", "subtitle_seg", "comment", "danmaku", "body")

def candidates(con, limit: int = 20, include_low_value: bool = False) -> list[dict]:
    _ph = ",".join("?" * len(_READABLE_KINDS))
    rows = con.execute(
        "SELECT i.uid, i.title, i.duration_sec, i.url "
        "FROM item i "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM content_block cb WHERE cb.item_uid = i.uid "
        "  AND cb.kind IN (%s))" % _ph, _READABLE_KINDS).fetchall()

    howto = re.compile(r"教程|怎么|如何|方法|技巧|设置|修改|安装|解锁|配置|指南|实测|对比|解决|打造|制作")
    out = []
    for uid, title, dur, url in rows:

        bare = uid.split(":")[-1]
        if not (bare.startswith("BV") or bare.startswith("av")):
            continue
        if not (dur or 0) > 0:
            continue
        if not include_low_value and low_value_title(title):
            continue
        score = (2 if howto.search(title or "") else 0) - (dur or 0) / 600.0
        out.append({"uid": uid, "title": title or "", "duration_sec": dur or 0,
                    "url": url or "", "score": score})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]

def _download_video(uid: str, url: str | None, dest: Path) -> Path:
    if not url:
        bid = uid.split(":")[-1]
        url = f"https://www.bilibili.com/video/{bid}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["yt-dlp", "-f", "worstvideo[ext=mp4]/worstvideo/worst", "--no-playlist",
           "-o", str(dest), url]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 4096:
        raise RuntimeError("yt-dlp 抓视频失败：%s" % (r.stderr or r.stdout or "")[-300:])
    return dest

def extract_frames(video: Path, out_dir: Path, n: int = DEFAULT_N_FRAMES) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("frame_*.jpg"):
        f.unlink()
    dur = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(video),
    ], text=True).strip())
    fps = max(n / dur, 0.05)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(video),
        "-vf", f"fps={fps:.4f},scale=512:-2", str(out_dir / "frame_%03d.jpg"),
    ], check=True)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    if len(frames) > n:
        idx = [round(i * (len(frames) - 1) / (n - 1)) for i in range(n)]
        frames = [frames[i] for i in idx]
    if not frames:
        raise RuntimeError("抽帧为空：%s" % video)
    return frames

class WemmEngine:

    def __init__(self, model_path: Path, max_tokens: int = DEFAULT_MAX_TOKENS):

        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
        self.torch = torch
        md = str(model_path)
        t0 = time.time()
        self.processor = AutoProcessor.from_pretrained(md, trust_remote_code=True)
        self.model = AutoModelForImageTextToText.from_pretrained(
            md, trust_remote_code=True, dtype=torch.float32,
        ).to("cpu").eval()
        self.load_sec = time.time() - t0
        self.max_tokens = max_tokens

    def describe(self, frames: list[Path], prompt: str = PROMPT) -> dict:
        content = [{"type": "image", "image": str(f)} for f in frames]
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        inputs = self.processor.apply_chat_template(
            messages, tokenize=True, return_dict=True, return_tensors="pt",
            add_generation_prompt=True, enable_thinking=False,
        )
        inputs = inputs.to(device="cpu", dtype=self.torch.float32)
        t0 = time.time()
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_tokens, do_sample=False)
        elapsed = time.time() - t0
        text = self.processor.tokenizer.decode(
            out[0, inputs.input_ids.shape[1]:], skip_special_tokens=True)
        return {"text": text.strip(), "elapsed": elapsed}

def write_desc(con, item_uid: str, text: str, *, elapsed: float = 0.0,
               n_frames: int = 0, model: str = DEFAULT_MODEL) -> int | None:
    from bili_digest.schema.core_schema import add_block
    extras = {"producer": "wemm-video", "model": model,
              "n_frames": n_frames, "elapsed_sec": round(elapsed, 1)}
    return add_block(con, item_uid, "visual_desc", text,
                     author="wemm-video", extras=extras, index_fts=True)

def visual_item(con, item_uid: str, *, engine: WemmEngine | None = None,
                video: Path | None = None, n_frames: int = DEFAULT_N_FRAMES,
                force: bool = False, index: bool = True) -> dict:
    from bili_digest.schema.core_schema import connect
    row = con.execute(
        "SELECT title, url FROM item WHERE uid=?", (item_uid,)).fetchone()
    if not row:
        return {"ok": False, "error": "库里没有这条：%s" % item_uid}
    title, url = row[0] or "", row[1] or ""

    exists = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE item_uid=? AND kind='visual_desc'",
        (item_uid,)).fetchone()[0]
    if exists and not force:
        return {"ok": False, "skipped": True, "error": "已有画面描述（--force 覆盖）"}

    tmp_dir = paths.DATA_DIR / ".cache" / "visual"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_path = Path(video) if video else None
    own_engine = engine is None
    eng = engine
    try:
        if video_path is None:
            video_path = _download_video(item_uid, url, tmp_dir / (item_uid.split(":")[-1] + ".mp4"))
        frames = extract_frames(video_path, tmp_dir / "frames", n_frames)
        if own_engine:
            eng = WemmEngine(model_dir())
        gen = eng.describe(frames)
        if not gen["text"]:
            return {"ok": False, "error": "模型返回空描述"}
        bid = write_desc(con, item_uid, gen["text"], elapsed=gen["elapsed"],
                         n_frames=len(frames))
        con.commit()
        indexed = 0
        if index and bid:
            from bili_digest.retrieve import rag_core as rc
            vi = rc.VectorIndex(paths.VEC, dtype="f16")
            r = rc.index_chunks(con, vi)
            indexed = r.get("new", 0)
        return {"ok": True, "block_id": bid, "chars": len(gen["text"]),
                "elapsed": gen["elapsed"], "n_frames": len(frames),
                "indexed": indexed, "text": gen["text"]}
    finally:

        if video is None and video_path and video_path.exists():
            video_path.unlink()
