#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from pathlib import Path

def enable_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass

HERE = Path(__file__).parent

for _sub in ("core", "ingest", "distill", "retrieve",
             "graph", "output", "quality", "tools"):
    _p = str(HERE / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
DATA_DIR = Path(os.environ.get("KB_DATA_DIR") or HERE).expanduser()

DB = DATA_DIR / "knowledge.db"
VEC = DATA_DIR / "vectors.usearch"
SHELF_DB = DATA_DIR / "shelf.db"
DIGEST_DIR = DATA_DIR / "bili-digest-out"

EMBED_MODEL = os.environ.get("KB_EMBED_MODEL") or "BAAI/bge-small-zh-v1.5"
EMBED_DIM = int(os.environ.get("KB_EMBED_DIM") or 512)
MODELS_DIR = DATA_DIR / "models"

LLM_BASE = os.environ.get("KB_LLM_BASE") or "https://api.deepseek.com/v1"
LLM_MODEL = os.environ.get("KB_LLM_MODEL") or "deepseek-chat"

HF_ENDPOINT = os.environ.get("KB_HF_ENDPOINT") or "https://hf-mirror.com"

AD_SERVER = os.environ.get("KB_AD_SERVER")
if AD_SERVER is None:
    AD_SERVER = "https://bsbsb.top"

PLATFORM = "bilibili"

URL_TEMPLATES = {
    "bilibili": "https://www.bilibili.com/video/{item_id}",
}

def item_url(platform: str, item_id: str) -> str:
    tpl = URL_TEMPLATES.get(platform)
    return tpl.format(item_id=item_id) if tpl else ""

def uid(platform: str, item_id: str) -> str:
    from core_schema import make_uid
    return make_uid(platform, item_id)

def bili_uid(item_id: str) -> str:
    return uid(PLATFORM, item_id)

def source_key(kind: str, value: str, platform: str = PLATFORM) -> str:
    try:
        import sources as _src
        if not _src.is_registered(kind):
            raise KeyError(
                f"未注册的来源类型 {kind!r}；已注册 {sorted(_src.all_types())}。"
                f"新平台请在 sources.py 的注册表里加一行")
    except ImportError:
        pass
    return f"{platform}:{kind}:{value}"

def source_key_prefix(kind: str, platform: str = PLATFORM) -> str:
    return f"{platform}:{kind}:"

def find_vault() -> str | None:
    env = os.environ.get("KB_VAULT")
    if env:
        return env

    home = Path.home()

    for root in (home, home / "Documents", home / "文档", home / "Obsidian"):
        if not root.is_dir():
            continue
        try:
            for sub in root.iterdir():
                if sub.is_dir() and (sub / ".obsidian").is_dir():
                    return str(sub)
        except (PermissionError, OSError):
            continue
    return None
