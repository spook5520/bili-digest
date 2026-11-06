#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from bili_digest import paths
from bili_digest.paths import enable_utf8_stdout

MIN_TITLE_LEN = 4
MAX_DF_RATIO = 0.05
CTX_RADIUS = 24
SUBJECT_MAX_LINK = 3

GENERIC_TITLES = {
    "教程", "合集", "第一期", "第二期", "上集", "下集", "第1集", "第2集",
    "未命名", "测试", "简介", "笔记", "更新", "日常", "全流程", "入门",
    "小白必看", "干货", "攻略", "测评", "开箱", "直播回放", "番外",
}

_PUNCT = {
    "\u3000": "", "\u00a0": "", "\u200b": "", "\u200c": "", "\u200d": "",
    "\ufeff": "",
    "【": "[", "】": "]", "〔": "(", "〕": ")", "〖": "[", "〗": "]",
    "「": '"', "」": '"', "『": '"', "』": '"',
    "、": ",", "。": ".", "·": "", "•": "", "…": "...",
    "—": "-", "–": "-", "―": "-", "‐": "-", "‑": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "《": "<", "》": ">", "／": "/", "＼": "\\\\", "＋": "+", "＆": "&",
}

_MASK_RES = [
    re.compile(r"!?\[\[[^\]]*\]\]"),
    re.compile(r"\[[^\]]*\]\([^)]*\)"),
    re.compile(r"\x60\x60\x60.*?\x60\x60\x60", re.S),
    re.compile(r"\x60[^\x60\n]*\x60"),
    re.compile(r"https?://\S+"),
    re.compile(r"\^b\d+"),
]
_WIKI_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TERM = object()
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)

def canonical(s: str) -> tuple[str, list[int]]:
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):

        if unicodedata.category(ch)[0] == "C":
            continue
        piece = _PUNCT.get(ch, unicodedata.normalize("NFKC", ch)).lower()
        if not piece:
            continue
        for c in piece:
            if c.isspace() or c == "\u200b":
                continue
            out.append(c)
            idx.append(i)
    return "".join(out), idx

@dataclass
class Note:
    path: Path
    uid: str
    title: str
    stem: str
    text: str = ""
    body: str = ""
    body_off: int = 0
    stem_canon: str = ""
    sections: list = field(default_factory=list)
    links: set = field(default_factory=set)

    def section_at(self, pos: int) -> str:
        for a, b, name in self.sections:
            if a <= pos < b:
                return name
        return "（正文开头）"

@dataclass
class Hit:
    src: str
    src_title: str
    dst_title: str
    dst_stem: str
    count: int
    section: str
    snippet: str
    start: int
    end: int
    match_text: str
    dst_path: str = ""

def _split_front_matter(text: str) -> tuple[dict, int]:
    m = _FM_RE.match(text)
    if not m:
        return {}, 0
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line[:1] in (" ", "\t", "-"):
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, m.end()

def load_notes(vault: str | Path, subdir: str = "B站") -> list[Note]:
    root = Path(vault) / subdir
    out: list[Note] = []
    for p in sorted(root.glob("*.md")):

        text = p.read_bytes().decode("utf-8", "surrogateescape")
        meta, off = _split_front_matter(text)
        body = text[off:]
        note = Note(
            path=p,
            uid=meta.get("uid", ""),
            title=(meta.get("title") or p.stem).strip(),
            stem=p.stem,
            text=text,
            body=body,
            body_off=off,
            stem_canon=canonical(p.stem)[0],
        )
        heads = [(m.start(), m.group(1)) for m in _SECTION_RE.finditer(body)]
        note.sections = [
            (heads[k][0], heads[k + 1][0] if k + 1 < len(heads) else len(body), heads[k][1])
            for k in range(len(heads))
        ]
        for m in _WIKI_RE.finditer(text):
            raw = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            if raw:
                note.links.add(canonical(raw)[0])
        out.append(note)
    return out

def build_trie(notes: list[Note], min_len: int = MIN_TITLE_LEN
               ) -> tuple[dict, dict, dict]:
    by_canon: dict[str, list[Note]] = {}
    skipped = {"too_short": 0, "generic": 0, "ambiguous": 0}
    for n in notes:
        c, _ = canonical(n.title)
        if len(c) < min_len:
            skipped["too_short"] += 1
            continue
        if c in GENERIC_TITLES or n.title.strip() in GENERIC_TITLES:
            skipped["generic"] += 1
            continue
        by_canon.setdefault(c, []).append(n)

    trie: dict = {}
    title_of: dict[str, Note] = {}
    for c, group in by_canon.items():
        if len(group) > 1:
            skipped["ambiguous"] += 1
            continue
        title_of[c] = group[0]
        node = trie
        for ch in c:
            node = node.setdefault(ch, {})
        node[_TERM] = c
    return trie, title_of, skipped

def _ascii_word(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()

def scan_body(cbody: str, trie: dict) -> list[tuple[int, int, str]]:
    hits: list[tuple[int, int, str]] = []
    n = len(cbody)
    for i in range(n):
        node = trie
        j = i
        while j < n and cbody[j] in node:
            node = node[cbody[j]]
            j += 1
            t = node.get(_TERM)
            if not t:
                continue

            if _ascii_word(t[0]) and i > 0 and _ascii_word(cbody[i - 1]):
                continue
            if _ascii_word(t[-1]) and j < n and _ascii_word(cbody[j]):
                continue
            hits.append((i, j, t))
    return hits

def _inside_wikilink(body: str, a: int, b: int) -> bool:
    lo = body.rfind("[[", 0, a)
    if lo < 0:
        return False
    hi = body.find("]]", lo)
    return hi >= b

def _mask_flags(body: str) -> list[bool]:
    flags = [False] * len(body)
    for rx in _MASK_RES:
        for m in rx.finditer(body):
            for k in range(m.start(), m.end()):
                flags[k] = True
    return flags

def plan(vault: str | Path, subdir: str = "B站",
         min_len: int = MIN_TITLE_LEN, max_df: float = MAX_DF_RATIO) -> dict:
    notes = load_notes(vault, subdir)
    trie, title_of, skipped = build_trie(notes, min_len)

    pairs: list[Hit] = []
    per_note: list[dict] = []
    rejected = {"already_linked": 0, "self": 0, "too_frequent": 0,
                "unsafe_text": 0, "over_cap": 0}
    seen = {"in_link": 0, "in_code_or_url": 0}
    df: dict[str, int] = {}

    for note in notes:
        if not note.body.strip():
            continue
        cbody, idx = canonical(note.body)
        flags = _mask_flags(note.body)
        raw = scan_body(cbody, trie)

        spans: dict[str, list[tuple[int, int]]] = {}
        for i, j, t in raw:
            if any(flags[idx[k]] for k in range(i, j)):

                if _inside_wikilink(note.body, idx[i], idx[j - 1] + 1):
                    seen["in_link"] += 1
                else:
                    seen["in_code_or_url"] += 1
                continue
            if title_of[t].uid and title_of[t].uid == note.uid:
                rejected["self"] += 1
                continue
            spans.setdefault(t, []).append((idx[i], idx[j - 1] + 1))
        if not spans:
            continue

        dup = {t for t in spans if title_of[t].stem_canon in note.links}
        if dup:
            rejected["already_linked"] += len(dup)
            for t in dup:
                spans.pop(t, None)
        if not spans:
            continue
        for t in spans:
            df[t] = df.get(t, 0) + 1

        ordered = sorted(spans.items(), key=lambda kv: (min(kv[1])[0], -max(kv[1])[1]))
        taken: list[tuple[int, int]] = []
        picked = []
        for t, occ in ordered:
            a, b = min(occ)[0], max(occ)[1]
            if any(a < y and x < b for x, y in taken):
                continue
            picked.append((t, occ))
            taken.append((a, b))
        if len(picked) > SUBJECT_MAX_LINK:
            rejected["over_cap"] += len(picked) - SUBJECT_MAX_LINK
            picked = picked[:SUBJECT_MAX_LINK]

        links = []
        for t, occ in picked:
            a, b = min(occ)[0], max(occ)[1]
            seg = note.text[note.body_off + a: note.body_off + b]
            if any(ch in seg for ch in "[]|#^"):
                rejected["unsafe_text"] += 1
                continue
            dst = title_of[t]
            links.append(Hit(
                src=note.uid or note.path.name,
                src_title=note.title,
                dst_title=dst.title,
                dst_stem=dst.stem,
                count=len(occ),
                section=note.section_at(a),
                snippet=_snippet(note.body, a, b),
                start=a, end=b, match_text=seg,
                dst_path=str(dst.path),
            ))
        if links:
            per_note.append({"uid": note.uid, "title": note.title, "path": str(note.path),
                             "n": len(links), "links": links})
            pairs.extend(links)

    n_notes = max(len(notes), 1)
    freq_cut = max(int(n_notes * max_df), 3)
    frequent = {t for t, c in df.items() if c > freq_cut}
    keep: set[int] = set()
    for h in pairs:
        c, _ = canonical(h.dst_title)
        if c in frequent:
            rejected["too_frequent"] += 1
            continue
        keep.add(id(h))
    keep_pairs = [h for h in pairs if id(h) in keep]
    keep_notes = []
    for item in per_note:
        item["links"] = [h for h in item["links"] if id(h) in keep]
        item["n"] = len(item["links"])
        if item["links"]:
            keep_notes.append(item)

    return {
        "notes": notes, "candidates": len(title_of), "skipped": skipped,
        "rejected": rejected, "seen": seen, "frequent": sorted(frequent)[:20],
        "diag": _corpus_diag(notes),
        "freq_cut": freq_cut, "pairs": keep_pairs, "per_note": keep_notes,
        "summary": {
            "notes_total": len(notes),
            "notes_linkable": len(keep_notes),
            "pairs": len(keep_pairs),
            "avg_per_linkable": round(len(keep_pairs) / len(keep_notes), 2) if keep_notes else 0.0,
            "avg_per_note": round(len(keep_pairs) / n_notes, 2),
        },
    }

def _corpus_diag(notes: list[Note]) -> dict:
    import statistics
    bv_re = re.compile(r"(BV[0-9A-Za-z]{10})")
    bv_owner: dict[str, list[str]] = {}
    for n in notes:
        m = bv_re.search(n.stem)
        if m:
            bv_owner.setdefault(m.group(1), []).append(n.uid)
    cross_bv = 0
    for n in notes:
        flags = _mask_flags(n.body)
        for m in bv_re.finditer(n.body):
            if any(flags[m.start(): m.end()]):
                continue
            owner = bv_owner.get(m.group(0))
            if owner and any(u != n.uid for u in owner):
                cross_bv += 1
    return {
        "title_len_median": statistics.median([len(n.title) for n in notes]) if notes else 0,
        "body_len_median": statistics.median([len(n.body) for n in notes]) if notes else 0,
        "cross_bv": cross_bv,
    }

def _snippet(body: str, a: int, b: int) -> str:
    lo = max(0, a - CTX_RADIUS)
    hi = min(len(body), b + CTX_RADIUS)
    s = re.sub(r"\s+", " ", body[lo:hi].replace("\n", " ")).strip()
    return ("…" if lo > 0 else "") + s + ("…" if hi < len(body) else "")

def _dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def _w(s: str, width: int) -> str:
    return s + " " * max(width - _dw(s), 0)

def _cut(s: str, width: int) -> str:
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > width:
            return "".join(out) + "…"
        out.append(c)
        w += cw
    return "".join(out)

def _current_wikilink_edges() -> int | None:
    try:
        import sqlite3
        con = sqlite3.connect("file:" + str(paths.DB) + "?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM edge WHERE kind='wikilink'").fetchone()[0]
        con.close()
        return n
    except Exception:
        return None

def report_dry(res: dict, top: int = 20, examples: int = 5) -> None:
    s, sk, rj = res["summary"], res["skipped"], res["rejected"]
    print("=" * 78)
    print("Unlinked mentions（批量）· DRY RUN —— 不改任何文件")
    print("=" * 78)
    print(f"扫描笔记    {s['notes_total']} 篇")
    print(f"候选标题    {res['candidates']} 个（规范化后唯一、>= 4 字）")
    print(f"标题侧排除  太短 {sk['too_short']} · 通名(黑名单) {sk['generic']} "
          f"· 重名(指向不明) {sk['ambiguous']}")
    print(f"命中侧排除  已链接 {rj['already_linked']} · 自指 {rj['self']} "
          f"· 高频通名 {rj['too_frequent']} · 超上限 {rj['over_cap']} "
          f"· 含[]| {rj['unsafe_text']}")
    print(f"通名阈值    同一标题命中 > {res['freq_cut']} 篇即丢弃"
          + (f"（本次命中 {len(res['frequent'])} 个）" if res["frequent"] else ""))
    if "seen" in res:
        print(f"已链接覆盖  标题出现在现有双链里 {res['seen']['in_link']} 处"
              f" · 落在代码/链接里 {res['seen']['in_code_or_url']} 处")
    print()
    print(f"能补链      {s['notes_linkable']} 篇 / {s['notes_total']} "
          f"（{s['notes_linkable'] / max(s['notes_total'], 1):.1%}）")
    print(f"候选对      {s['pairs']} 个")
    print(f"平均        {s['avg_per_linkable']} 条/篇（对可补的篇）"
          f" · {s['avg_per_note']} 条/篇（对全部篇）")
    cur = _current_wikilink_edges()
    if cur:
        print(f"新增边估算  +{s['pairs']}（wikilink 边 {cur} → {cur + s['pairs']}，"
              f"+{s['pairs'] / cur:.0%}）")
    print()

    if not res["pairs"] and res.get("diag"):
        d = res["diag"]
        print("── 诊断：为什么是 0 " + "─" * 52)
        print(f"   标题中位 {d['title_len_median']:.0f} 字 · 正文中位 {d['body_len_median']:.0f} 字"
              f" · 正文里出现别的笔记 BV 号 {d['cross_bv']} 处")
        print("   读法：正文是「转述」，不是「引用」；标题只在双链里出现（见上面的覆盖数）。")
        print("         这类库里 Unlinked mentions 本身就是空集 —— 面板逐篇翻也会是空的。")
        print()

    ranked = sorted(res["pairs"], key=lambda h: (-h.count, h.src))[:top]
    print(f"── top {top} 候选对 " + "─" * 56)
    print("  " + _w("#", 4) + _w("次数", 5) + _w("源笔记", 38) + "目标笔记")
    for k, h in enumerate(ranked, 1):
        print("  " + _w(str(k), 4) + _w(str(h.count), 5)
              + _w(_cut(h.src_title, 38), 38) + _cut(h.dst_title, 40))
    print()

    ex = res["per_note"][:examples]
    if ex:
        print(f"── {len(ex)} 个具体例子 " + "─" * 50)
        for item in ex:
            print("● " + _cut(item["title"], 68))
            for h in item["links"]:
                print(f"    + [[{_cut(h.dst_stem, 44)}]]"
                      f"   ［第 {h.start} 字符 · {h.section} · 共 {h.count} 次］")
                print("      " + _cut(h.snippet, 96))
            print()

def apply(vault: str | Path, subdir: str = "B站", backup_root: Path | None = None,
          limit: int | None = None) -> dict:
    res = plan(vault, subdir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = Path(vault).resolve()
    backup_root = Path(backup_root or (paths.HERE / "_backup")) / f"unlinked-{stamp}"
    changed = []

    todo = res["per_note"][:limit] if limit else res["per_note"]
    for item in todo:
        path = Path(item["path"])
        bak = backup_root / path.resolve().relative_to(root)
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bak)

        text = path.read_bytes().decode("utf-8", "surrogateescape")
        _meta, off = _split_front_matter(text)
        edits = []
        for h in item["links"]:
            a, b = off + h.start, off + h.end
            disp = h.dst_stem if h.match_text == h.dst_stem else h.dst_stem + "|" + h.match_text
            edits.append((a, b, "[[" + disp + "]]"))
        for a, b, new in sorted(edits, key=lambda e: -e[0]):
            text = text[:a] + new + text[b:]
        path.write_bytes(text.encode("utf-8", "surrogateescape"))
        changed.append({
            "path": str(path.resolve().relative_to(root)),
            "backup": str(bak),
            "links": [{"dst": Path(h.dst_path).name, "at": h.start,
                       "text": h.match_text, "section": h.section, "count": h.count}
                      for h in item["links"]],
        })

    backup_root.mkdir(parents=True, exist_ok=True)
    manifest = backup_root / "manifest.json"
    manifest.write_text(json.dumps({
        "stamp": stamp, "vault": str(root), "n_files": len(changed),
        "n_links": sum(len(c["links"]) for c in changed), "files": changed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"files": changed, "backup_root": str(backup_root), "manifest": str(manifest)}

def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="Unlinked mentions 批量版：找「正文提到标题但没加双链」，可选补链")
    ap.add_argument("--vault", default=None, help="vault 根目录（默认自动探测）")
    ap.add_argument("--subdir", default="B站", help="笔记子目录（默认 B站）")
    ap.add_argument("--dry", action="store_true", help="只打印统计（默认行为）")
    ap.add_argument("--apply", action="store_true", help="真改文件（先逐篇备份）")
    ap.add_argument("--top", type=int, default=20, help="打印前 N 个候选对")
    ap.add_argument("--examples", type=int, default=5, help="打印几个具体例子")
    ap.add_argument("--min-len", type=int, default=MIN_TITLE_LEN, help="标题最小长度")
    ap.add_argument("--limit", type=int, default=None, help="apply 最多改几篇（分批）")
    ap.add_argument("--json", default=None, help="把 plan 结果写进这个 json")
    ap.add_argument("--backup-dir", default=None, help="备份根目录（默认 <项目>/_backup）")
    a = ap.parse_args(argv)

    vault = a.vault or paths.find_vault()
    if not vault:
        print("找不到 vault：用 --vault 指定（或设 KB_VAULT）", file=sys.stderr)
        return 2

    res = plan(vault, a.subdir, min_len=a.min_len)
    report_dry(res, a.top, a.examples)

    if a.json:
        Path(a.json).write_text(json.dumps({
            "summary": res["summary"], "skipped": res["skipped"],
            "rejected": res["rejected"], "candidates": res["candidates"],
            "pairs": [{"src": h.src, "src_title": h.src_title, "dst": h.dst_title,
                       "count": h.count, "section": h.section, "snippet": h.snippet}
                      for h in res["pairs"]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("json → " + a.json)

    if a.apply:
        out = apply(vault, a.subdir, backup_root=a.backup_dir, limit=a.limit)
        print("=" * 78)
        print(f"已改 {len(out['files'])} 篇 · 补 "
              f"{sum(len(f['links']) for f in out['files'])} 条链")
        print("备份 " + out["backup_root"])
        print("清单 " + out["manifest"])
        print("回滚：把备份目录里的文件按相对路径拷回 vault")
    else:
        print("（dry run：没有改动任何文件。确认后加 --apply）")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
