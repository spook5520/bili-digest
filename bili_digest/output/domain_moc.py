#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

DOMAIN_DIR = "_领域"
MOC_DIR = "_主题"
GENERATOR = "domain_moc/v1"

DOMAINS: dict[str, list[str]] = {
    "工具与软件": [
        "效率工具", "开源工具", "开源软件", "Windows", "操作系统", "Linux",
        "浏览器", "软件推荐", "输入法", "桌面美化", "系统美化", "系统优化",
        "注册表", "投屏", "远程桌面", "内网穿透", "PPT", "Docker",
        "BIOS", "系统安装", "黑苹果",
        "开源项目",
    ],
    "声乐与虚拟歌手": [
        "歌词翻译",

        "音乐", "翻唱", "洛天依", "虚拟主播", "虚拟歌手", "AI翻唱",
        "歌词", "古风歌曲", "VOCALOID", "音乐翻唱", "二次创作",
    ],
    "游戏与模组": [
        "沉浸感",
        "游戏",

        "Minecraft 模组", "Minecraft", "Minecraft光影", "材质包", "整合包",
        "YSM模型", "游戏优化", "游戏设计", "游戏攻略", "游戏考据",
        "游戏地图设计", "游戏细节", "荒野大镖客2", "赛博朋克2077", "GTA",
        "GTA5", "GTA 4", "R星", "Steam", "开放世界", "反作弊", "Any Austin",
        "游戏模组",
        "Minecraft模组",
    ],
    "B站与文化": [
        "供应链",

        "网络迷因", "二次元",
        "猫", "怀旧", "历史考据", "电商",
    ],
    "技术专题": [
        "JavaScript",
        "Shader",
        "编译器",
        "深度学习",
        "嵌入式开发",

        "性能优化", "安卓逆向", "逆向工程", "二进制", "网络安全", "网络协议",
        "后端开发", "检索增强生成", "大模型", "大语言模型", "Java", "Python",
        "数据库", "知识图谱", "前端开发", "注意力机制", "论文解读",
        "AI agent", "Frida", "数学教育", "校园网",
    ],
    "安卓": [
        "安卓", "安卓开发", "安卓模拟器", "刷机", "Root", "KernelSU",
        "Fastboot", "模拟器",
    ],
    "硬件与设备": [
        "硬件评测", "DIY", "VR", "掌机", "迷你主机", "3D打印",
        "开源硬件", "手工制作", "笔记本",
    ],
}

HUBS = {"知识科普", "科普", "教程", "安卓", "游戏", "供应链"}
ALIASES = {"游戏模组", "Minecraft模组", "开源项目"}

_FM_MEMBERS = re.compile(r"^members:\s*(\d+)", re.M)

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名主题"

def _yaml(v) -> str:
    s = str(v)
    if not s:
        return '""'
    if re.search(r"[:#\[\]{}&*!|>'\"%@\u0060]|^\s|\s$", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s

def _width(s: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def _pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _width(s))

def _disk_mocs(moc_root: Path) -> list[str]:
    if not moc_root.is_dir():
        return []
    return [p.stem for p in sorted(moc_root.iterdir())
            if p.is_file() and p.suffix == ".md" and not p.name.startswith("_")]

def _fm_members(path: Path) -> int | None:
    try:
        head = path.read_text(encoding="utf-8")[:600]
    except OSError:
        return None
    m = _FM_MEMBERS.search(head)
    return int(m.group(1)) if m else None

def _db_counts(con: sqlite3.Connection) -> dict[str, int]:
    try:
        return {r[0]: r[1] for r in con.execute(
            "SELECT topic, COUNT(*) FROM topic_member GROUP BY topic")}
    except sqlite3.OperationalError:
        return {}

def _db_unique(con: sqlite3.Connection, topics: list[str]) -> int | None:
    if not topics:
        return 0
    ph = ",".join("?" * len(topics))
    try:
        return con.execute(
            "SELECT COUNT(DISTINCT item_uid) FROM topic_member WHERE topic IN (%s)"
            % ph, topics).fetchone()[0]
    except sqlite3.OperationalError:
        return None

def _render(name: str, rows: list[tuple[str, int]], total: int,
            fallback: int, stale: list[str], on_disk: set[str] | None = None) -> str:
    L = ["---", "type: domain", "name: %s" % _yaml(name),
         "mocs: %d" % len(rows), "members: %d" % total,
         "generator: %s" % GENERATOR, "---", "",
         "# %s" % name, "",
         "> 上位类 MOC · %d 个下属 MOC · %d 条视频（跨 MOC 去重）"
         % (len(rows), total), "",
         "## 下属 MOC", ""]
    for moc, n in rows:

        target = ("%s/%s" % (MOC_DIR, moc)) if moc == name else moc
        mark = ""
        if moc in HUBS:
            mark = " · 泛"
        elif moc in ALIASES:
            mark = " · 同义"

        if on_disk and moc in on_disk:
            L.append("- [[%s]] · %d 条%s" % (target, n, mark))
        else:
            L.append("- %s · %d 条%s" % (moc, n, mark))

    _notes = []
    if HUBS & {m for m, _ in rows}:
        _notes.append("标「泛」的主题涵盖很宽，不是具体话题")
    if ALIASES & {m for m, _ in rows}:
        _notes.append("标「同义」的主题与另一页讲的是同一件事")
    if _notes:
        L += ["", "> %s。保留它们只为链接不断，不代表它们值得单独看。" % "；".join(_notes)]
    if stale:

        L += ["", "## 本领域暂无对应主题页", "",
              " · ".join("\u0060%s\u0060" % s for s in stale)]
    if fallback:
        L += ["", "> 其中 %d 个 MOC 的成员数取自文件 frontmatter"
              "（topic_member 无该主题）。" % fallback]
    L.append("")
    return "\n".join(L)

def write_domains(con: sqlite3.Connection, vault: str | Path,
                  dry: bool = False) -> dict:
    vault = Path(vault)
    moc_root = vault / MOC_DIR
    out_dir = moc_root / DOMAIN_DIR

    disk = _disk_mocs(moc_root)
    on_disk = set(disk)
    counts = _db_counts(con)

    res: dict = {"domains": {}, "covered": 0, "total": len(disk),
                 "unmapped": [], "stale": [], "written": 0, "removed": []}
    seen: set[str] = set()
    rendered: dict[str, str] = {}

    for name, mocs in DOMAINS.items():
        rows: list[tuple[str, int]] = []
        stale: list[str] = []
        fallback = 0
        for moc in mocs:
            if moc not in on_disk:
                stale.append(moc)
                continue
            if moc in seen:

                raise ValueError("MOC 重复归属：%s 同时属于两个上位类" % moc)
            n = counts.get(moc)
            if n is None:
                n = _fm_members(moc_root / (_safe(moc) + ".md"))
                if n is None:
                    n = 0
                else:
                    fallback += 1
            rows.append((moc, n))
            seen.add(moc)
        rows.sort(key=lambda kv: (-kv[1], kv[0]))
        unique = _db_unique(con, [m for m in mocs if m in on_disk])
        if unique is None:
            unique = sum(n for _, n in rows)
        res["domains"][name] = {"mocs": len(rows), "members": unique,
                                "fallback": fallback, "stale": stale,
                                "top": rows[0][0] if rows else ""}
        rendered[name] = _render(name, rows, unique, fallback, stale, on_disk)
        res["covered"] += len(rows)
        res["stale"] += stale

    res["unmapped"] = [m for m in disk if m not in seen]

    if res["unmapped"]:
        _rows = [(m, counts.get(m, 0)) for m in res["unmapped"]]
        _rows.sort(key=lambda x: (-x[1], x[0]))
        rendered["未归类"] = _render("未归类", _rows, sum(c for _, c in _rows),
                                     len(_rows), [], on_disk)
        res["covered"] += len(_rows)

    if dry:
        _report(res, vault, len(disk), dry=True)
        return res

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (out_dir / (_safe(name) + ".md")).write_text(text, encoding="utf-8")
        res["written"] += 1

    keep = {_safe(n) for n in DOMAINS}
    for p in sorted(out_dir.glob("*.md")):
        if p.stem in keep:
            continue
        try:
            head = p.read_text(encoding="utf-8")[:400]
        except OSError:
            continue
        if "generator: %s" % GENERATOR in head:
            try:
                p.unlink()
                res["removed"].append(p.stem)
            except OSError:
                pass

    _report(res, vault, len(disk), dry=False)
    return res

def _report(res: dict, vault: Path, n_disk: int, dry: bool) -> None:
    tag = "[DRY] " if dry else "  "
    print("%s%s %4s  %8s   %s" % (tag, _pad("域", 14), "MOC", "视频", "最大下属"))
    print("%s%s %s  %s   %s" % (tag, "-" * 14, "----", "--------", "--------"))
    for name, d in res["domains"].items():
        print("%s%s %4d  %8d   %s" % (tag, _pad(name, 14), d["mocs"],
                                      d["members"], d["top"]))
    print("%s%s %4d  %8d" % (tag, _pad("合计", 14), res["covered"],
                             sum(d["members"] for d in res["domains"].values())))
    print("%s覆盖 %d/%d 个 MOC" % (tag, res["covered"], n_disk))
    if res["stale"]:
        print("%s映射表 stale %d 个（库中已无）：%s"
              % (tag, len(res["stale"]), " / ".join(res["stale"])))
    if res["unmapped"]:
        print("%s[!] 未映射 %d 个 MOC（映射表漏了，必须补）：%s"
              % (tag, len(res["unmapped"]), " / ".join(res["unmapped"])))
    if not dry:
        print("  写入 %d 个 → %s" % (res["written"], vault / MOC_DIR / DOMAIN_DIR))
        if res["removed"]:
            print("  清理旧域文件 %d 个：%s"
                  % (len(res["removed"]), " / ".join(res["removed"])))

def main(argv: list[str] | None = None) -> int:
    import argparse
    from bili_digest.schema.core_schema import connect

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="生成 7 个上位类 MOC（只写 _主题/_领域/）")
    ap.add_argument("db", nargs="?", default="knowledge.db")
    ap.add_argument("--vault", required=True, help="Obsidian vault 路径")
    ap.add_argument("--dry", action="store_true", help="只打印，不落盘")
    a = ap.parse_args(argv)

    con = connect(a.db)
    try:
        r = write_domains(con, a.vault, dry=a.dry)
    finally:
        con.close()

    if r["unmapped"]:
        print("  [warn] %d 个 MOC 未归类（已收进「未归类」域，不影响覆盖）：%s"
              % (len(r["unmapped"]), " / ".join(r["unmapped"])))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
