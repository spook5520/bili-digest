#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re

MARK = "____"

_PHRASE = re.compile(r"[A-Z][A-Za-z0-9.+#_\-]*(?:[ ][A-Z][A-Za-z0-9.+#_\-]*)+")
_MODEL = re.compile(r"[A-Za-z]+[ ]\d+(?:\.\d+)?(?![\d.]|\s?%)")
_ALNUM = re.compile(r"(?<![A-Za-z0-9./])[0-9][A-Za-z0-9.+#_\-]*[A-Za-z][A-Za-z0-9.+#_\-]*")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.+#_\-]{1,}")
_PCT = re.compile(r"\d+(?:\.\d+)?%")
_QUOTED = re.compile(r"「([^「」]{3,12})」")
_BOUND = "，。；！？,;!?"

_EN_STOP = {
    "the", "and", "for", "with", "you", "this", "that", "from", "are", "was",
    "video", "bilibili", "bv", "up", "pc", "app", "ok", "vs", "etc", "www",
    "bug", "http", "https", "com", "true", "false", "null",

    "usb", "star", "config", "github", "reference", "readme", "app", "pc",
    "log", "apply", "issue", "click", "setup", "download", "install", "run",
}
_CN_STOP = {
    "视频", "内容", "作者", "观众", "建议", "问题", "东西", "部分", "情况",
    "方式", "时候", "过程", "地方", "后面", "前面", "这里", "那里", "什么",
    "怎么", "为什么", "可以", "需要", "一个", "这个", "那个", "以及", "包括",

    "歌词", "结果", "方式", "情况",
}

_FUNC_HEAD = "是而并为要对把被与和的了在从由把以之地得即再又"

def _first_sentence(text: str, limit: int = 72) -> str:
    s = (text or "").strip().replace("\n", " ")
    if not s:
        return ""
    cut = len(s)
    for ch in "。！？":
        i = s.find(ch)
        if 0 <= i < cut:
            cut = i + 1
    s = s[:cut].strip()
    if len(s) > limit:
        s = s[:limit].rstrip() + "……"
    return s

def _latin_terms(text: str) -> list:
    out = []
    taken = []

    def free(a, b):
        return not any(a < y and x < b for x, y in taken)

    for m in _PHRASE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.group(0), 4 if len(m.group(0)) >= 6 else 3))
    for m in _MODEL.finditer(text):
        if not free(m.start(), m.end()):
            continue
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.group(0), 4))
    for m in _ALNUM.finditer(text):
        if not free(m.start(), m.end()):
            continue
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.group(0), 4))
    for m in _LATIN.finditer(text):
        if not free(m.start(), m.end()):
            continue
        t = m.group(0).strip(".-_")
        if len(t) < 3 or t.lower() in _EN_STOP:
            continue
        taken.append((m.start(), m.end()))

        score = 3 if (t[0].isupper() or any(c.isdigit() for c in t)
                      or "-" in t or len(t) >= 5) else 2
        out.append((m.start(), t, score))
    return out

def _candidates(text: str, topics: list) -> list:
    cand = []
    for _pos, t, sc in _latin_terms(text):
        cand.append((sc, t))
    for m in _PCT.finditer(text):
        cand.append((4, m.group(0)))
    for m in _QUOTED.finditer(text):
        cand.append((3, m.group(1)))
    for t in topics or []:
        t = str(t).strip()
        if 2 <= len(t) <= 6 and t not in _CN_STOP and t in text:
            cand.append((1, t))
    seen = set()
    out = []
    for sc, t in sorted(cand, key=lambda x: (-x[0], -len(x[1]))):
        if t in seen:
            continue
        seen.add(t)
        out.append((sc, t))
    return out

def _prompt(text: str, term: str, limit: int = 72) -> str:
    i = text.find(term)
    if i < 0:
        return ""
    bounds = [j for j, ch in enumerate(text) if ch in _BOUND]
    left = max([b + 1 for b in bounds if b < i], default=0)
    right = min([b for b in bounds if b >= i + len(term)], default=len(text))
    if left > 0 and (text[left] in _FUNC_HEAD or (right - left) < 10):
        prev = [b + 1 for b in bounds if b < left - 1]
        if prev:
            left = max(prev)
    seg = text[left:right].strip()
    if left > 0:
        seg = "…" + seg
    if len(seg) > limit:
        k = seg.find(term) if term in seg else 0
        half = max((limit - len(term)) // 2, 10)
        a = max(k - half, 0)
        b = min(k + len(term) + half, len(seg))
        seg = ("…" if a else "") + seg[a:b] + ("…" if b < len(seg) else "")
    return seg.replace(term, MARK, 1)

def _find_by_text(blocks: list, quote: str, min_len: int = 8):
    q = (quote or "").strip()
    if len(q) < min_len:
        return None
    best, best_score = None, 0
    for b in blocks:
        if b.get("offset_sec") is None:
            continue
        t = str(b.get("text") or "").strip()
        if len(t) < min_len:
            continue

        score = len(q) if q in t else (len(t) if t in q else 0)
        if score > best_score:
            best, best_score = b, score
    return best

_DOMAIN = re.compile(
    r"(?i)[a-z0-9][a-z0-9\-]*\.(com|cn|net|org|io|fun|dev|me|top|xyz|app|cc)(?![\w.])")

_URLISH = re.compile(
    r"(?i)(://|www\.|github\.com|pan\.[a-z0-9]+\.[a-z]+"
    r"|[a-z0-9\-]+\.(com|cn|net|org|io|fun|dev|me|app|cc|tv)\b"
    r"|/[a-z0-9_\-]{2,}/)")

def _leak_reason(term: str, prompt: str, title: str):
    low = term.lower()

    if re.search(r"[，、；！？,;!?]", term):
        return "phrase"
    if _DOMAIN.search(term) or "/" in term or low.startswith("www"):
        return "domain"
    if title and low in title.lower():
        return "title"
    for tok in re.split(r"[\s./_\-+（）()]+", term):
        if len(tok) >= 3 and tok.lower() in prompt.lower():
            return "prompt"

    if _URLISH.search(prompt):
        return "url"
    i = prompt.find(MARK)
    if i >= 0:
        for j, ch in enumerate(prompt):
            if ch in "：:" and abs(j - i) <= 2:
                return "label"

        pre = prompt[i - 1] if i > 0 else ""
        post = prompt[i + len(MARK)] if i + len(MARK) < len(prompt) else ""
        if pre and pre in "（(":
            return "bracket"
        if post and post in "）)":
            return "bracket"
    return None

def _loc(block) -> str:
    if not block:
        return ""
    off = block.get("offset_sec")
    if off is None:
        return ""
    m, s = divmod(int(off), 60)
    return "`%02d:%02d`" % (m, s)

def _kp_text(kp) -> str:
    if isinstance(kp, dict):
        return str(kp.get("point") or "").strip()
    return str(kp or "").strip()

def _kp_cites(kp) -> list:
    from bili_digest.distill import extract as _ex
    if isinstance(kp, dict):
        return _ex.cite_ids(kp.get("cites"))
    return []

PER_POINT = 2

def make_questions(ctx: dict, provided: dict, limit: int = 4) -> list:
    blocks = {b["id"]: b for b in ctx.get("blocks") or []}
    topics = provided.get("topics") or []
    title = str((ctx.get("item") or {}).get("title") or "")
    pool = []
    for idx, kp in enumerate(provided.get("key_points") or []):
        text = _kp_text(kp)
        if len(text) < 12:
            continue
        got_terms = []
        for sc, term in _candidates(text, topics):
            if len(got_terms) >= PER_POINT:
                break
            if len(term) < 2 or text.count(term) != 1 or term in got_terms:
                continue
            prompt = _prompt(text, term)
            if not prompt or MARK not in prompt:
                continue
            reason = _leak_reason(term, prompt, title)
            if reason is not None and reason != "title":
                continue
            if any(t.lower() in prompt.lower() for t in got_terms):
                continue
            got_terms.append(term)
            pool.append((sc, idx, term, text, kp, prompt, reason))

    pool.sort(key=lambda x: (-x[0], x[1]))
    clean = [p for p in pool if p[6] is None]
    soft = [p for p in pool if p[6] == "title"]

    used = set()
    out = []

    def _add(item):
        sc, _idx, term, text, kp, prompt, _reason = item
        if term in used or len(out) >= limit:
            return
        if any(q["term"].lower() in prompt.lower() for q in out):
            return
        used.add(term)
        loc = ""
        for cid in _kp_cites(kp):
            loc = _loc(blocks.get(cid))
            if loc:
                break
        out.append({"prompt": prompt, "term": term, "full": text, "loc": loc})

    for it in clean:
        _add(it)
    if len(out) < 2:
        for it in soft:
            _add(it)
            if len(out) >= 2:
                break
    return out

def _anchors(ctx: dict, provided: dict, limit: int = 4, min_len: int = 10) -> list:
    blist = ctx.get("blocks") or []
    blocks = {b["id"]: b for b in blist}
    seen = set()
    cand = []
    from bili_digest.distill import extract as _ex
    sources = list(provided.get("insights") or []) + list(provided.get("key_points") or [])
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw = src.get("cites")
        for cid in _ex.cite_ids(raw):
            q = ""
            if isinstance(raw, list):
                for c in raw:
                    if isinstance(c, dict) and c.get("id") == cid:
                        q = str(c.get("quote") or "").strip()
                        break
            b = blocks.get(cid)
            if not q and b:
                q = str(b.get("text") or "").strip()

            if (b is None or b.get("offset_sec") is None) and q:
                nb = _find_by_text(blist, q)
                if nb is not None:
                    b = nb
            if len(q) < min_len or q in seen:
                continue

            import re as _re_q
            q = _re_q.sub(r"\s+", " ", q).strip()

            if len(q) >= 16 and q[:8] and q.count(q[:8]) >= 3:
                continue
            if len(set(q)) <= max(3, len(q) // 12):
                continue
            seen.add(q)
            loc = _loc(b)
            cand.append((0 if loc else 1, loc, q[:120]))
    cand.sort(key=lambda x: x[0])
    hot = [c for c in cand if c[0] == 0][:limit]
    cold = [c for c in cand if c[0] == 1][:2]

    out = []
    per_loc = {}
    for _k, loc, q in hot + cold:
        if loc:
            per_loc[loc] = per_loc.get(loc, 0) + 1
            if per_loc[loc] > 2:
                continue
        out.append((loc, q))
    return out

def build_review_section(ctx: dict, provided: dict) -> list:
    body = []
    one = _first_sentence(provided.get("summary") or "")
    if one:
        body += ["> [!abstract] 一句（先只看这句）", "> " + one, ""]

    qs = make_questions(ctx, provided)
    if qs:
        body += ["**自测 %d 题**　先自己想，想不起来再展开。" % len(qs), ""]
        for i, q in enumerate(qs, 1):
            body.append("> [!question]- %d. %s" % (i, q["prompt"]))
            tail = ("　" + q["loc"]) if q["loc"] else ""
            body.append("> 答案：**%s**%s" % (q["term"], tail))
            if q["full"]:
                body.append("> 原句：%s" % q["full"])
            body.append("")

    an = _anchors(ctx, provided)
    if an:
        body.append("> [!quote]- 原文锚点（卡壳时对照）")
        for loc, q in an:
            body.append("> - " + ((loc + " ") if loc else "") + q)
        body.append("")

    while body and not body[-1].strip():
        body.pop()
    if not body:
        return []

    head = ["*看一句 → 答自测 → 卡住看锚点 → 要细节再往下读。"
            "批量抽查走 `kb quiz --due`（FSRS 排程）。*", ""]
    return head + body
