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
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

AUTHOR_PAT = re.compile(
    r"(?:原作者|原\s*作者|original\s*author|credit\s*to|来源作者)"
    r"\s*[:：]\s*"
    r"([A-Za-z][A-Za-z0-9_.\-]{1,24}(?:\s+[A-Za-z][A-Za-z0-9_.\-]{1,24}){0,2}"
    r"|[\u4e00-\u9fff]{2,10})",
    re.I)

AUTHOR_NOISE = re.compile(
    r"^(?:https?|www|com|cn|net|org|patreon|youtube|youtu|bilibili|t\.me|twitter"
    r"|discord|reddit|twitch)$"
    r"|^UC[A-Za-z0-9_\-]{20,}$"
    r"|^[A-Za-z0-9_\-]{11}$"
    r"|^\d+"
    r"|^(?:the|and|for|with|from|watch|video|channel|https)$",
    re.I)

def _clean_author(s: str) -> str | None:
    if not s:
        return None
    s = s.strip().strip(".,;:，。；：、")
    if len(s) < 2 or len(s) > 28:
        return None
    if AUTHOR_NOISE.match(s):
        return None

    if re.search(r"https?|\.com|\.cn|\.be/|www\.|@|/", s, re.I):
        return None
    return s

TOPIC_PATTERNS = [
    r"到底(?:在|是)?哪(?:里|儿)?",
    r"去哪(?:了|儿)",
    r"往哪(?:里)?",
    r"来自哪(?:里)?",
    r"符合.{0,6}吗",
    r"合规吗", r"违法吗", r"犯法", r"算犯罪", r"不构成犯罪",
    r"真实吗", r"几分真", r"够真实",
    r"有多少", r"多少人", r"多少钱",
    r"人口普查", r"普查", r"失业率", r"经济",
    r"边界在哪", r"水流", r"河流", r"水系", r"水文", r"电网", r"供水", r"供电",
    r"机场", r"考据", r"考究", r"史实",
]
TOPIC_RE = re.compile("|".join(TOPIC_PATTERNS), re.I)

def _get(url, ck, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url)
    for k, v in (("User-Agent", UA), ("Referer", referer), ("Cookie", ck)):
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {str(e)[:60]}"}

class Chain:

    def __init__(self, ck: str, budget: int = 120, cooldown: float = 3.0,
                 verbose: bool = True):
        self.ck = ck
        self.budget = budget
        self.cooldown = cooldown
        self.verbose = verbose
        self.calls = 0
        self.seen = {}
        self.authors = defaultdict(set)
        self.uploaders = defaultdict(set)
        self.searched = set()
        self.expanded = set()
        self.log = []

    def _spend(self, what: str) -> bool:
        if self.calls >= self.budget:
            return False
        self.calls += 1
        if self.calls > 1:
            time.sleep(self.cooldown)
        if self.verbose:
            print(f"    [{self.calls:>3}/{self.budget}] {what}")
        return True

    def _say(self, msg):
        self.log.append(msg)
        if self.verbose:
            print(f"      {msg}")

    def add(self, bvid, title, up=None, mid=None, src="", author=None,
            title_hit=False):
        if not bvid:
            return False
        new = bvid not in self.seen
        rec = self.seen.setdefault(bvid, {
            "title": title, "up": up, "mid": mid, "sources": set(),
            "author": author, "title_hit": title_hit})
        rec["sources"].add(src)
        if author:
            rec["author"] = author
        if up:
            self.uploaders[up].add(bvid)
        if author:
            self.authors[author].add(bvid)
        return new

    def by_keyword(self, kw: str, pages: int = 1):
        if kw in self.searched:
            return 0
        self.searched.add(kw)
        import bili_shelf as bs
        total_new = 0
        for p in range(1, pages + 1):
            if not self._spend(f"搜索「{kw}」第 {p} 页"):
                return total_new
            try:
                rs = bs.search_video(bs.load_credential(), kw, p, None)
            except Exception as e:
                self._say(f"搜索失败 {type(e).__name__}")
                continue
            n = 0
            for r in rs:
                t = r.get("title") or ""
                th = bool(TOPIC_RE.search(t))
                if self.add(r.get("bvid"), t, r.get("author"), r.get("mid"),
                            f"搜索:{kw}", title_hit=th):
                    n += 1
            self._say(f"搜「{kw}」→ {len(rs)} 条，新增 {n}")
            total_new += n
        return total_new

    def _priority(self, bv: str) -> float:
        rec = self.seen.get(bv) or {}
        score = 0.0
        a = rec.get("author")
        if a:
            if len(self.authors.get(a, ())) <= 1:
                score += 10.0
            else:
                score += 0.0
        else:
            score += 5.0
        if rec.get("title_hit"):
            score += 3.0
        score += float(len(rec.get("sources") or ()))
        return score

    def expand_unexpanded(self, limit: int = 5):
        todo = [bv for bv, r in self.seen.items()
                if bv not in self.expanded and r["title_hit"]]
        todo.sort(key=lambda bv: -self._priority(bv))
        for bv in todo[:limit]:
            if self.calls >= self.budget:
                break
            self.expanded.add(bv)
            self.by_meta(bv, want_related=False)

    def by_meta(self, bvid: str, want_related=True):
        if not self._spend(f"元数据 {bvid}"):
            return
        j = _get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                 self.ck)
        d = (j or {}).get("data") or {}
        if not d:
            self._say(f"{bvid} 取不到元数据")
            return
        title, desc = d.get("title") or "", d.get("desc") or ""
        owner = d.get("owner") or {}

        authors = set()
        for m in AUTHOR_PAT.finditer(desc):
            a = _clean_author(m.group(1))
            if a:
                authors.add(a)
        self.add(bvid, title, owner.get("name"), owner.get("mid"),
                 "已知", author=next(iter(authors), None),
                 title_hit=bool(TOPIC_RE.search(title)))
        if authors:
            self._say(f"{bvid} 原作者: {sorted(authors)}")
        if want_related:
            self.by_related(bvid)

    def by_related(self, bvid: str):
        if not self._spend(f"相关推荐 {bvid}"):
            return
        j = _get(f"https://api.bilibili.com/x/web-interface/archive/related?bvid={bvid}",
                 self.ck)
        rel = (j or {}).get("data") or []
        n = 0
        for v in rel:
            t = v.get("title") or ""
            if self.add(v.get("bvid"), t, (v.get("owner") or {}).get("name"),
                        (v.get("owner") or {}).get("mid"), f"相关推荐:{bvid}",
                        title_hit=bool(TOPIC_RE.search(t))):
                n += 1
        self._say(f"{bvid} 相关推荐 {len(rel)} 条，新增 {n}")

    def by_uploader(self, mid: str, name: str = ""):
        if not self._spend(f"UP 投稿 {name or mid}"):
            return
        url = (f"https://api.bilibili.com/x/space/wbi/arc/search"
               f"?mid={mid}&ps=30&pn=1&order=pubdate")
        j = _get(url, self.ck, referer=f"https://space.bilibili.com/{mid}")
        vl = ((j or {}).get("data") or {}).get("list", {}).get("vlist") or []
        if not vl:
            self._say(f"{name or mid} 投稿取不到（code={j.get('code')}）"
                      f"—— 该接口需要 wbi 签名，当前用不了")
            return
        n = 0
        for v in vl:
            t = v.get("title") or ""
            if self.add(v.get("bvid"), t, name, mid, f"UP投稿:{name}",
                        title_hit=bool(TOPIC_RE.search(t))):
                n += 1
        self._say(f"{name or mid} 投稿 {len(vl)} 条，新增 {n}")

    def features(self):

        ac = sorted(((len(v), k) for k, v in self.authors.items() if k), reverse=True)

        hits = [r["title"] for r in self.seen.values()
                if r["title_hit"] and r["title"]]
        pat_count = defaultdict(int)
        for pat in TOPIC_PATTERNS:
            rx = re.compile(pat, re.I)
            for t in hits:
                if rx.search(t):
                    pat_count[pat] += 1
        pc = sorted(((n, p) for p, n in pat_count.items() if n >= 2), reverse=True)
        return {"authors": ac[:5], "patterns": pc[:8]}

    def report(self):
        rows = list(self.seen.items())
        by_up = defaultdict(list)
        for bv, r in rows:
            by_up[r["up"]].append(bv)
        print(f"\n{'='*84}")
        print(f"  展开结果：{len(rows)} 个视频 · {len(self.uploaders)} 位 UP "
              f"· {self.calls} 次请求")
        print(f"{'='*84}")
        feats = self.features()
        print(f"\n  提取到的特征：")
        print(f"    原作者候选: {[f'{k}({n})' for n, k in feats['authors']]}")
        print(f"    高频选题句式: {[f'{p}({n})' for n, p in feats['patterns']]}")
        print(f"\n  按搬运者：")
        for up, bvs in sorted(by_up.items(), key=lambda x: -len(x[1]))[:12]:
            print(f"    {len(bvs):>2}  @{up}")
        return self.seen

def main():
    ap = argparse.ArgumentParser(description="从线索泛化展开")
    ap.add_argument("--seed", required=True, help="线索：BV号 / 关键词")
    ap.add_argument("--kind", default="video",
                    choices=("video", "keyword"), help="线索类型")
    ap.add_argument("--budget", type=int, default=60, help="最多网络请求数")
    ap.add_argument("--cooldown", type=float, default=3.0)
    ap.add_argument("--out", default="chain_result.json")
    ap.add_argument("--rounds", type=int, default=2, help="展开轮数")
    a = ap.parse_args()

    import bili_digest as bd
    ck = bd.cookie_header(bd.load_credential())
    c = Chain(ck, budget=a.budget, cooldown=a.cooldown)

    print(f"═══ 从「{a.seed}」展开（最多 {a.rounds} 轮 / {a.budget} 请求）═══\n")

    print("  ── 轮 0：种子 ──")
    if a.kind == "video":
        c.by_meta(a.seed)
    else:
        c.by_keyword(a.seed)

    for rnd in range(1, a.rounds + 1):
        feats = c.features()
        if not feats["authors"] and not feats["patterns"]:
            print(f"\n  ── 轮 {rnd}：没有新特征，停止 ──")
            break
        print(f"\n  ── 轮 {rnd}：用特征扩展 ──")
        before = len(c.seen)

        c.expand_unexpanded(limit=6)
        feats = c.features()

        for _n, author in feats["authors"][:3]:
            c.by_keyword(author)
            for _m, pat in feats["patterns"][:4]:
                word = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", pat)
                if 2 <= len(word) <= 8:
                    c.by_keyword(f"{author} {word}")

        for _m, pat in feats["patterns"][:4]:
            word = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", pat)
            if 2 <= len(word) <= 6:
                c.by_keyword(word, pages=1)

        added = len(c.seen) - before
        print(f"  → 本轮新增 {added} 条")
        if added == 0:
            print(f"  → 无新增，收敛，停止")
            break

    c.report()
    out = {bv: {**r, "sources": sorted(r["sources"])} for bv, r in c.seen.items()}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\n  已存 {a.out}（{len(out)} 条）")
    print(f"  请求数 {c.calls}/{a.budget}")

if __name__ == "__main__":
    main()
