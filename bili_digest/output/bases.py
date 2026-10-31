#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from pathlib import Path

from bili_digest import paths

COLUMNS = ["file.name", "author", "saved", "duration", "coverage",
           "content_chars", "topics", "collections"]

def _graph(name: str, prop: str, depth: int = 1, height: int = 600) -> list:
    return ["  - type: base-graph",
            "    name: %s" % name,
            "    groupBy:",
            "      property: %s" % prop,
            "      direction: ASC",
            "    order:",
            "      - file.name",
            "    depth: %d" % depth,
            "    direction: outgoing",
            "    scaleLinksByCount: true",
            "    showArrows: false",
            "    height: %d" % height]

def build() -> str:
    L = ["filters:", "  and:", '    - file.hasProperty("uid")', "views:"]

    L += ["  - type: table", "    name: 全部视频", "    order:"]
    L += ["      - %s" % c for c in COLUMNS]

    L += _graph("图谱 · 按 UP", "author")

    L += _graph("图谱 · 按主题", "topics")

    L += _graph("图谱 · 按覆盖度", "coverage")

    L += ["  - type: table", "    name: 有字幕的", "    filters:",
          "      and:", '        - coverage == "full"', "    order:"]
    L += ["      - %s" % c for c in COLUMNS]

    L += ["  - type: table", "    name: 空壳待抓", "    filters:",
          "      and:", '        - coverage == "empty"', "    order:"]
    L += ["      - file.name", "      - author", "      - saved", "      - duration"]
    return chr(10).join(L) + chr(10)

def write(vault: str | Path, name: str = "B站视频.base") -> Path:
    p = Path(vault) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build(), encoding="utf-8")
    return p

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="生成 Obsidian Bases 视图（表格 + 图谱）")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--name", default="B站视频.base")
    a = ap.parse_args()
    p = write(a.vault, a.name)
    print("  已写入 %s" % p)
    print("  视图：全部视频（表）· 图谱按 UP / 主题 / 覆盖度 · 有字幕的（表）· 空壳待抓（表）")
    print("  图谱视图需要社区插件 Base Graph（obsidian-base-graph）；表格视图 Obsidian 内置。")

if __name__ == "__main__":
    sys.exit(main())
