#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import sys
from dataclasses import dataclass, field

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

@dataclass(frozen=True)
class SourceType:
    kind: str
    role: str
    label: str
    platforms: tuple = ()

ROLES = ("producer", "container")

_BUILTIN = [

    ("bilibili", "up",         "producer",   "UP主",      ("bilibili",)),
    ("bilibili", "folder",     "container",  "收藏夹",     ("bilibili",)),
    ("bilibili", "partition",  "container",  "分区",       ("bilibili",)),
    ("youtube",  "channel",    "producer",   "频道",       ("youtube",)),
    ("youtube",  "playlist",   "container",  "播放列表",    ("youtube",)),
    ("zhihu",    "author",     "producer",   "专栏作者",    ("zhihu",)),
    ("zhihu",    "collection", "container",  "收藏夹",     ("zhihu",)),
    ("rss",      "feed",       "producer",   "订阅源",     ("rss",)),
    ("rss",      "category",   "container",  "订阅分类",    ("rss",)),
    ("local",    "dir",        "producer",   "本地目录",    ("local",)),
    ("local",    "tag",        "container",  "本地标签",    ("local",)),

    ("generic",  "keyword",    "container",  "关键词",     ()),
    ("generic",  "tag",        "container",  "标签",       ()),

    ("generic",  "platform",   "container",  "平台",       ()),
]

_REGISTRY: dict = {}
_BY_PLATFORM: dict = {}

def register(platform: str, kind: str, role: str, label: str,
             platforms: tuple = None) -> SourceType:
    if role not in ROLES:
        raise ValueError(f"role 必须是 {ROLES} 之一，收到 {role!r}")
    st = SourceType(kind=kind, role=role, label=label,
                    platforms=tuple(platforms) if platforms is not None
                    else (platform,))
    _REGISTRY[kind] = st
    for p in (st.platforms or ("*",)):
        _BY_PLATFORM.setdefault(p, [])
        if kind not in _BY_PLATFORM[p]:
            _BY_PLATFORM[p].append(kind)
    return st

for _row in _BUILTIN:
    register(*_row)

def kinds_for(platform: str = None) -> list:
    if platform is None:
        return sorted(_REGISTRY)
    out = list(_BY_PLATFORM.get(platform, []))
    for k in _BY_PLATFORM.get("*", []):
        if k not in out:
            out.append(k)
    return sorted(out)

def role_of(kind: str) -> str:
    st = _REGISTRY.get(kind)
    if st is None:
        raise KeyError(f"未注册的来源类型 {kind!r}；"
                       f"已注册：{sorted(_REGISTRY)}")
    return st.role

def label_of(kind: str) -> str:
    st = _REGISTRY.get(kind)
    return st.label if st else kind

def is_registered(kind: str) -> bool:
    return kind in _REGISTRY

def producers(platform: str = None) -> list:
    return [k for k in kinds_for(platform) if _REGISTRY[k].role == "producer"]

def containers(platform: str = None) -> list:
    return [k for k in kinds_for(platform) if _REGISTRY[k].role == "container"]

def all_types() -> dict:
    return dict(_REGISTRY)

_EXTRACTORS: dict = {}

def extractor(*kinds):
    def deco(fn):
        for k in kinds:
            _EXTRACTORS[k] = fn
        return fn
    return deco

@extractor("up", "channel", "author", "feed", "dir")
def _by_author(item, extras):
    v = item.get("author_uid")
    if not v:
        return None
    return (str(v), item.get("author_name") or str(v))

@extractor("partition")
def _by_tname(item, extras):
    v = extras.get("tname")
    return (str(v), str(v)) if v else None

@extractor("folder", "playlist", "collection", "category", "tag")
def _by_relation(item, extras):
    return None

@extractor("_platform")
def _by_platform(item, extras):
    p = item.get("platform")
    return (str(p), str(p)) if p else None

def extract_sources(item: dict, extras: dict = None) -> list:
    extras = extras or {}
    if isinstance(item.get("extras"), str) and not extras:
        import json as _json
        try:
            extras = _json.loads(item["extras"]) or {}
        except (ValueError, TypeError):
            extras = {}
    platform = item.get("platform")
    out = []
    for kind in kinds_for(platform):
        fn = _EXTRACTORS.get(kind)
        if fn is None:
            continue
        r = fn(item, extras)
        if r:
            out.append((kind, r[0], r[1]))

    p = _EXTRACTORS["_platform"](item, extras)
    if p:
        out.append(("platform", p[0], p[1]))
    return out

if __name__ == "__main__":
    print("  已注册的来源类型：")
    print(f"    {'kind':<12}{'role':<11}{'label':<12}适用平台")
    for k in sorted(_REGISTRY):
        st = _REGISTRY[k]
        print(f"    {st.kind:<12}{st.role:<11}{st.label:<12}"
              f"{', '.join(st.platforms) or '（全平台）'}")
    print(f"\n  按平台查询：")
    for p in ("bilibili", "youtube", "zhihu", "rss", "local"):
        print(f"    {p:<10} {kinds_for(p)}")
    print(f"\n  按角色查询：")
    print(f"    producer  {producers()}")
    print(f"    container {containers()}")
